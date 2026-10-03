"""local 模式 clone 目标目录处置:残留清理 / 幂等复用 / 避让 / 同任务串行

回归背景:git clone 要求目标目录不存在或为空,而 local 模式整个任务复用同一个
会话临时目录 —— 上一次失败(超时 kill)或"跳过预克隆"留下的半成品目录,会让下
一次尝试(换协议 / 换分支 / 降级为自主 clone / 工作区恢复)直接报
fatal: destination path '...' already exists and is not an empty directory,
把真实的首因错误淹没在级联错误里。旧实现只有 sandbox 模式在 _clone_repo_sandbox
里 rm -rf 兜底。
"""
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

import app.tools.sandbox_tools as st


def _ctx(tmp_path, repo_path=""):
    return {"mode": "local", "local_dir": tmp_path, "repo_path": repo_path}


def _mk_junk(root: Path, name: str) -> Path:
    """无 .git 的残留目录(空壳/半成品工作树)"""
    d = root / name
    (d / "src").mkdir(parents=True)
    (d / "src" / "a.py").write_text("x", encoding="utf-8")
    return d


def _mk_repo(root: Path, name: str) -> Path:
    """含 .git 的目录(kill 半途与成功 clone 都是这个形态)"""
    d = root / name
    (d / ".git").mkdir(parents=True)
    (d / "README.md").write_text("half", encoding="utf-8")
    return d


# ============================================================
# 基础判定与删除
# ============================================================


def test_dir_has_entries(tmp_path):
    """不存在/空目录 → False;非空目录/同名文件 → True(git 的判空口径)。"""
    assert st._dir_has_entries(tmp_path / "nope") is False
    (tmp_path / "empty").mkdir()
    assert st._dir_has_entries(tmp_path / "empty") is False
    assert st._dir_has_entries(_mk_junk(tmp_path, "full")) is True
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert st._dir_has_entries(tmp_path / "file.txt") is True


def test_remove_local_tree_confirms_removal(tmp_path):
    """删除成功返回 True 且目录真的没了;目标不存在也视为成功。"""
    junk = _mk_junk(tmp_path, "bar")
    assert st._remove_local_tree(junk) is True
    assert not junk.exists()
    assert st._remove_local_tree(tmp_path / "nope") is True


def test_remove_local_tree_retries_then_gives_up(tmp_path, monkeypatch):
    """句柄一直占用时退避重试后返回 False(而不是静默"删了一半")。"""
    junk = _mk_junk(tmp_path, "bar")
    calls: list[int] = []

    def _raise(_path):
        calls.append(1)
        raise OSError(32, "另一个程序正在使用此文件")

    monkeypatch.setattr(st.shutil, "rmtree", _raise)
    monkeypatch.setattr(st.time, "sleep", lambda _s: None)
    assert st._remove_local_tree(junk) is False
    assert len(calls) == 3  # 默认重试 3 次


# ============================================================
# 目标目录处置:复用 / 清理 / 避让
# ============================================================


def test_resolve_target_absent_or_empty(tmp_path):
    """目录不存在或为空:直接用作克隆目标。"""
    target, reusable = st._resolve_local_repo_dir(_ctx(tmp_path), "bar")
    assert (target, reusable) == (tmp_path / "bar", False)
    (tmp_path / "bar").mkdir()
    target, reusable = st._resolve_local_repo_dir(_ctx(tmp_path), "bar")
    assert (target, reusable) == (tmp_path / "bar", False)


def test_resolve_target_cleans_junk(tmp_path):
    """无 .git 的残留 → 清掉后沿用原目录名。"""
    junk = _mk_junk(tmp_path, "bar")
    target, reusable = st._resolve_local_repo_dir(_ctx(tmp_path), "bar")
    assert (target, reusable) == (tmp_path / "bar", False)
    assert not junk.exists()


def test_resolve_target_reuses_session_repo(tmp_path):
    """本会话已成功 clone 过的仓库 → 幂等复用(重复 clone 不再抹掉工作区)。"""
    repo = _mk_repo(tmp_path, "bar")
    ctx = _ctx(tmp_path, repo_path=str(repo))
    target, reusable = st._resolve_local_repo_dir(ctx, "bar")
    assert (target, reusable) == (repo, True)
    assert repo.exists()  # 未被删除


def test_resolve_target_avoids_foreign_repo(tmp_path):
    """有 .git 但不是本会话 clone 的(执行 agent 自行克隆的成果)→ 避让换名,绝不删。"""
    repo = _mk_repo(tmp_path, "bar")
    target, reusable = st._resolve_local_repo_dir(_ctx(tmp_path), "bar")
    assert reusable is False
    assert target == tmp_path / "bar-2"
    assert repo.exists()


def test_resolve_target_avoids_when_removal_fails(tmp_path, monkeypatch):
    """残留删不掉(Windows 句柄占用)→ 避让换名,克隆不因目录冲突而失败。"""
    monkeypatch.setattr(st, "_remove_local_tree", lambda *a, **kw: False)
    _mk_junk(tmp_path, "bar")
    target, reusable = st._resolve_local_repo_dir(_ctx(tmp_path), "bar")
    assert reusable is False
    assert target.name == "bar-2"


def test_unique_repo_dir_skips_taken_names(tmp_path):
    """非空候选名逐个避让(空目录 git 能写,不算占用),全被占时用随机后缀。"""
    taken = {f"bar-{i}" for i in range(2, 21)}
    for name in taken:
        d = tmp_path / name
        (d / ".git").mkdir(parents=True)
        (d / "f.txt").write_text("x", encoding="utf-8")
    target = st._unique_repo_dir(tmp_path, "bar")
    assert target.name not in taken
    assert re.fullmatch(r"bar-[0-9a-f]{6}", target.name)


def test_unique_repo_dir_reuses_empty_dir(tmp_path):
    """空的 bar-2 仍可直接用(git 对空目录不报错)。"""
    _mk_junk(tmp_path, "bar")
    (tmp_path / "bar-2").mkdir()
    assert st._unique_repo_dir(tmp_path, "bar") == tmp_path / "bar-2"


# ============================================================
# _clone_repo_local:复用短路 + 克隆前清残留
# ============================================================


class _FakeProc:
    """伪造 Popen:首次 poll 即"成功退出",并在目标目录写出工作树。"""

    def __init__(self, create_path: str):
        self.returncode = 0
        self.stderr = iter([])
        self._create_path = create_path

    def poll(self):
        Path(self._create_path, "README.md").parent.mkdir(parents=True, exist_ok=True)
        Path(self._create_path, "README.md").write_text("ok", encoding="utf-8")
        return 0

    def kill(self):
        pass


def test_local_clone_returns_existing_repo_without_git(tmp_path, monkeypatch):
    """已 clone 的仓库再调一次 clone_repo:不调 git,直接返回既有工作区。"""
    repo = _mk_repo(tmp_path, "bar")
    (repo / "a.py").write_text("x", encoding="utf-8")

    def _boom(*a, **kw):
        raise AssertionError("复用现有工作区时不应启动 git")

    monkeypatch.setattr(st.subprocess, "Popen", _boom)
    result = st._clone_repo_local(
        _ctx(tmp_path, repo_path=str(repo)), "https://github.com/foo/bar", "bar", None,
    )
    assert result["path"] == str(repo)
    assert result["files_count"] == 2  # README.md + a.py(不计 .git)


def test_local_clone_cleans_leftover_before_git(tmp_path, monkeypatch):
    """残留目录在启动 git 前被清掉:目标名沿用 bar,不会撞 already exists。"""
    _mk_junk(tmp_path, "bar")
    recorded: dict[str, object] = {}

    def _fake_popen(cmd, *a, **kw):
        # 此处相当于 git 看到的现场:目标名必须可写(不存在或为空)
        recorded["dest"] = cmd[-1]
        recorded["dest_free"] = not st._dir_has_entries(Path(cmd[-1]))
        return _FakeProc(cmd[-1])

    monkeypatch.setattr(st.subprocess, "Popen", _fake_popen)
    result = st._clone_repo_local(
        _ctx(tmp_path), "https://github.com/foo/bar", "bar", None,
    )
    assert recorded["dest"] == str(tmp_path / "bar")
    assert recorded["dest_free"] is True
    assert not (tmp_path / "bar" / "src").exists()  # 旧残留已被新工作树替掉
    assert result["files_count"] == 1


# ============================================================
# 回退链:跳过/失败都要清理半成品,且不并发撞目录
# ============================================================


def _patch_offline_fallback(monkeypatch, tmp_path):
    """关掉仓库缓存并注入固定会话,让回退链只跑到被 patch 的克隆实现。"""
    monkeypatch.setattr(st.settings, "REPO_CACHE_ENABLED", False)
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _ctx(tmp_path))
    monkeypatch.setattr(st, "_set_repo_path", lambda *a, **kw: None)


def test_fallback_cleans_leftover_on_skip(monkeypatch, tmp_path):
    """克隆进行中被跳过(kill 半途)后:目录被清理且异常继续向上传播。

    旧实现里跳过路径直接 raise,不进失败清理 —— 降级为自主 clone 后复用
    同一个会话目录,必撞 git 的 "already exists"。
    """
    task_id = uuid.uuid4().hex
    _patch_offline_fallback(monkeypatch, tmp_path)
    attempts: list[str] = []

    def _fake_clone(ctx, url, repo_name, branch, **kw):
        attempts.append(url)
        _mk_repo(tmp_path, repo_name)  # 模拟 kill 半途留下的 .git + 部分工作树
        raise st.CloneSkippedError(f"用户已跳过预克隆: {repo_name}")

    monkeypatch.setattr(st, "_clone_repo_local", _fake_clone)
    with pytest.raises(st.CloneSkippedError):
        st.clone_repo_with_fallback(
            "https://github.com/foo/bar", task_id=task_id, cancellable=True,
        )
    assert not (tmp_path / "bar").exists()
    assert len(attempts) == 1  # 跳过不进协议回退


def test_fallback_next_protocol_sees_clean_target(monkeypatch, tmp_path):
    """第一种协议失败留下残留后,下一种协议看到的是空目标(不再撞 already exists)。"""
    task_id = uuid.uuid4().hex
    _patch_offline_fallback(monkeypatch, tmp_path)
    occupied: list[bool] = []

    def _fake_clone(ctx, url, repo_name, branch, **kw):
        occupied.append((tmp_path / repo_name).exists())
        if len(occupied) == 1:
            _mk_repo(tmp_path, repo_name)
            raise RuntimeError("git clone 失败: fatal: early EOF")
        return {"path": str(tmp_path / repo_name), "files_count": 1}

    monkeypatch.setattr(st, "_clone_repo_local", _fake_clone)
    result = st.clone_repo_with_fallback(
        "https://github.com/foo/bar", task_id=task_id,
    )
    assert len(occupied) == 2
    assert occupied == [False, False]  # 两次尝试的目标都不被残留占用
    assert result["path"] == str(tmp_path / "bar")


def test_fallback_serializes_same_task(monkeypatch):
    """同 task_id 的并发克隆串行执行(同一时刻回退链内只有一个在跑)。"""
    task_id = uuid.uuid4().hex
    active: list[int] = []
    overlap: list[int] = []

    def _fake_impl(repo_url, **kw):
        active.append(1)
        if len(active) > 1:
            overlap.append(1)
        time.sleep(0.05)
        active.pop()
        return {"path": "/tmp/bar", "files_count": 0}

    monkeypatch.setattr(st, "_clone_repo_fallback", _fake_impl)
    threads = [
        threading.Thread(
            target=lambda: st.clone_repo_with_fallback(
                "https://github.com/foo/bar", task_id=task_id,
            )
        )
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap


def test_fallback_different_tasks_use_different_locks():
    """不同 task_id 不互相阻塞;session 关闭后锁登记表不残留。"""
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    assert st._get_clone_lock(a) is st._get_clone_lock(a)
    assert st._get_clone_lock(a) is not st._get_clone_lock(b)
    st._clone_locks.pop(a, None)
    st._clone_locks.pop(b, None)
