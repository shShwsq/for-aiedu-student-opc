"""预克隆跳过机制单测(clone_skip 注册表 + sandbox_tools 检查点 + orchestrator 降级)

核心约束:
- 跳过标志一次性语义:consume 后弹出,不残留到后续轮次/自主 clone
- cancellable=False(LLM 工具路径)恒不检查标志
- CloneSkippedError 不被协议回退链吞掉(跳过后不再试下一种协议)
- orchestrator 捕获跳过后降级返回 (None, ""),与失败降级同路径
"""
import uuid
from unittest.mock import MagicMock

import pytest

import app.agents.orchestrator as orchestrator
import app.tools.sandbox_tools as st
from app.agents.orchestrator import _prepare_repo_context
from app.clone_skip import (
    clear_skip_state,
    consume_skip_clone,
    is_skip_requested,
    request_skip_clone,
)


# ============================================================
# clone_skip 注册表
# ============================================================


def test_request_consume_one_shot():
    """consume 读取并弹出标志:第二次 consume 返回 False(一次性语义)。"""
    task_id = uuid.uuid4().hex
    assert not is_skip_requested(task_id)
    request_skip_clone(task_id)
    assert is_skip_requested(task_id)
    assert consume_skip_clone(task_id) is True
    assert consume_skip_clone(task_id) is False
    assert not is_skip_requested(task_id)


def test_request_idempotent_and_clear():
    """重复 request 无副作用;clear 幂等清除。"""
    task_id = uuid.uuid4().hex
    request_skip_clone(task_id)
    request_skip_clone(task_id)
    assert consume_skip_clone(task_id) is True
    clear_skip_state(task_id)
    clear_skip_state(task_id)  # 幂等:无标志时不报错
    assert not is_skip_requested(task_id)


# ============================================================
# sandbox_tools 检查点
# ============================================================


def _fake_local_ctx(tmp_path):
    return {"mode": "local", "local_dir": tmp_path}


def test_fallback_raises_when_skip_requested_before_attempt(monkeypatch, tmp_path):
    """cancellable=True 且已请求跳过 → 尝试前检查点直接抛,不进协议回退。"""
    task_id = uuid.uuid4().hex
    # 本测试只测原协议回退链的跳过检查点:关缓存,避免 ensure_bare_cache 走真实网络
    monkeypatch.setattr(st.settings, "REPO_CACHE_ENABLED", False)
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    request_skip_clone(task_id)
    with pytest.raises(st.CloneSkippedError):
        st.clone_repo_with_fallback(
            "https://github.com/foo/bar", task_id=task_id, cancellable=True,
        )


def test_fallback_non_cancellable_ignores_skip_flag(monkeypatch, tmp_path):
    """cancellable=False(LLM 工具路径)不检查标志,克隆照常且标志保留。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st.settings, "REPO_CACHE_ENABLED", False)
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)
    monkeypatch.setattr(
        st, "_clone_repo_local",
        lambda *a, **kw: {"path": "/tmp/bar", "files_count": 1},
    )
    request_skip_clone(task_id)
    result = st.clone_repo_with_fallback(
        "https://github.com/foo/bar", task_id=task_id, cancellable=False,
    )
    assert result["path"] == "/tmp/bar"
    # 标志未被消费(不影响后续,任务结束时兜底清理)
    assert is_skip_requested(task_id)
    clear_skip_state(task_id)


def test_fallback_mid_chain_skip_stops_remaining_protocols(monkeypatch, tmp_path):
    """第一种协议失败后才请求跳过 → 第二次尝试前检查点抛出,不再继续回退。"""
    task_id = uuid.uuid4().hex
    monkeypatch.setattr(st.settings, "REPO_CACHE_ENABLED", False)
    calls: list[str] = []
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))

    def _fake_clone(ctx, url, repo_name, branch, task_id="", cancellable=False, progress_callback=None):
        calls.append(url)
        if len(calls) == 1:
            # 第一次失败,并模拟用户此时点了"跳过预克隆"
            request_skip_clone(task_id)
            raise RuntimeError("git clone 失败: auth denied")
        raise AssertionError("跳过后不应再尝试下一种协议")

    monkeypatch.setattr(st, "_clone_repo_local", _fake_clone)
    with pytest.raises(st.CloneSkippedError):
        st.clone_repo_with_fallback(
            "https://github.com/foo/bar", task_id=task_id, cancellable=True,
        )
    assert len(calls) == 1


class _FakeProc:
    """伪造 Popen:永不退出,供跳过检查点在轮询中触发。"""

    def __init__(self):
        self.returncode = None
        self.stderr = iter([])  # reader 线程读到空即结束
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_local_clone_kill_proc_on_skip(monkeypatch, tmp_path):
    """克隆进行中请求跳过 → 轮询检查点 kill 进程并抛 CloneSkippedError。"""
    task_id = uuid.uuid4().hex
    fake = _FakeProc()
    monkeypatch.setattr(st.subprocess, "Popen", lambda *a, **kw: fake)
    request_skip_clone(task_id)
    with pytest.raises(st.CloneSkippedError):
        st._clone_repo_local(
            _fake_local_ctx(tmp_path), "https://github.com/foo/bar", "bar",
            None, task_id=task_id, cancellable=True,
        )
    assert fake.killed


def test_local_clone_non_cancellable_no_skip_check(monkeypatch, tmp_path):
    """cancellable=False 时轮询不检查标志(用首次 poll 即完成规避死循环)。"""
    task_id = uuid.uuid4().hex

    class _DoneProc(_FakeProc):
        def __init__(self):
            super().__init__()
            self.returncode = 0

        def poll(self):
            return 0  # 立即"完成",不进检查点分支

    fake = _DoneProc()
    monkeypatch.setattr(st.subprocess, "Popen", lambda *a, **kw: fake)
    # 本用例只关"不消费跳过标志";文件数按契约必须非空(0 文件现在被判为
    # 失败:空仓库/缓存无 ref 不能再报"克隆完成但目录是空的"),故把计数固定为 1
    monkeypatch.setattr(st, "_count_repo_files", lambda _p: 1)
    request_skip_clone(task_id)
    result = st._clone_repo_local(
        _fake_local_ctx(tmp_path), "https://github.com/foo/bar", "bar",
        None, task_id=task_id, cancellable=False,
    )
    assert result["files_count"] == 1
    assert is_skip_requested(task_id)  # 标志未被消费
    clear_skip_state(task_id)


# ============================================================
# orchestrator._prepare_repo_context 降级
# ============================================================


def _mk_task(repo_url="https://github.com/a/b"):
    task = MagicMock()
    task.id = "task-1"
    task.user_id = None
    task.params = {"repo_url": repo_url, "branch": "main"}
    return task


def _patch_env(monkeypatch):
    """屏蔽 DB / 事件总线副作用,只测降级分支(同 test_prepare_repo_context)。"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _task: None)
    monkeypatch.setattr(
        orchestrator, "_write_memory_files_for_task", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        orchestrator, "_add_conversation", lambda *a, **kw: None,
    )
    # 预建会话(挂载缓存用)是沙箱副作用,本文件只测跳过检查点
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "precreate_session_for_repo",
        lambda *a, **kw: None,
    )


def test_prepare_repo_context_skip_degrades(monkeypatch):
    """预克隆被跳过 → 返回 (None, ""),走 react_agent 自主克隆降级路径。"""
    _patch_env(monkeypatch)

    def _raise_skipped(*a, **kw):
        assert kw.get("cancellable") is True, "orchestrator 必须以 cancellable=True 调用"
        raise orchestrator.sandbox_tools.CloneSkippedError("用户已跳过预克隆")

    monkeypatch.setattr(
        orchestrator.sandbox_tools, "clone_repo_with_fallback", _raise_skipped,
    )
    repo_path, repo_context = _prepare_repo_context(
        _mk_task(), MagicMock(), "task-1",
    )
    assert repo_path is None
    assert repo_context == ""
