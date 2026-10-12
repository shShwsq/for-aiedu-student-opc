"""CLI(ACP)暂停真中断单元测试(方案 B)

覆盖 agent1 走 Qoder/DeepSeek/Codex 等 CLI 执行器时,用户暂停能在流式循环内
真中断生成并冻结流水线:
- ACPClient.prompt:pause_probe 命中(ACPPromptPaused)→ cancel + 置 last_prompt_paused
  + 空结果;与 last_prompt_truncated 互斥(暂停不置截断)
- ACPClient.prompt:每次调用重置 last_prompt_paused(状态不跨轮串扰)
- ACPClient.prompt:把 pause_probe 透传给 _rpc
- ACPClient._rpc:on_event 分发前检查 pause_probe(活跃流下一条即停)
- ACPClient._iter_lines_with_watchdog:轮询空转检查 pause_probe(静默期响应)
- 未暂停时行为与现状一致(pause_probe 恒 False)
- orchestrator._consume_paused_interrupt_flag:读取并清除 _round_paused_interrupt
  (builtin 不写标志 → 恒 False,行为无变化)
"""
import queue as queue_mod
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import (
    ACPPromptPaused,
    ACPClient,
    PromptIdleTimeout,
)
from app.agents.orchestrator import _consume_paused_interrupt_flag


# ============================================================
# 辅助:伪造 httpx streaming response(同 test_acp_stream_abort)
# ============================================================


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self.status_code = status_code
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


class _BlockingStreamResponse:
    """iter_lines 阻塞不返回(模拟 CLI 静默):供看门狗空转检查暂停"""

    def __init__(self, hold_secs: float = 2.0):
        self._hold = hold_secs

    def iter_lines(self):
        time.sleep(self._hold)
        return iter(())


class _StreamCM:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


def _install_stream(monkeypatch, client: ACPClient, response) -> None:
    monkeypatch.setattr(client._client, "stream", lambda *a, **kw: _StreamCM(response))


# ============================================================
# ACPClient.prompt:暂停真中断善后
# ============================================================


def test_prompt_paused_cancels_and_sets_flag(monkeypatch):
    """pause_probe 命中:cancel + last_prompt_paused=True + 空结果;不置 truncated(互斥)"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(client, "_rpc", MagicMock(side_effect=ACPPromptPaused()))
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))

    result = client.prompt(
        "sess-1", [{"type": "text", "text": "hi"}],
        pause_probe=lambda: True,
    )

    assert result == {}
    assert cancelled == ["sess-1"]
    assert client.last_prompt_paused is True
    assert client.last_prompt_truncated is None  # 暂停 ≠ 截断,语义分流


def test_prompt_resets_paused_flag_each_call(monkeypatch):
    """每次 prompt 入口重置 last_prompt_paused:暂停后下一轮正常完成不残留"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(client, "_rpc", MagicMock(side_effect=ACPPromptPaused()))
    monkeypatch.setattr(client, "cancel", lambda sid: None)
    client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=lambda: True)
    assert client.last_prompt_paused is True

    monkeypatch.setattr(client, "_rpc", MagicMock(return_value={"stopReason": "end_turn"}))
    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=lambda: False)
    assert result == {"stopReason": "end_turn"}
    assert client.last_prompt_paused is False


def test_prompt_forwards_pause_probe_to_rpc(monkeypatch):
    """prompt 把 pause_probe 透传给 _rpc(探针不丢失)"""
    client = ACPClient("http://fake")
    captured: dict = {}

    def fake_rpc(req, on_event=None, timeout=None, idle_probe=None, pause_probe=None):
        captured["pause_probe"] = pause_probe
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    probe = lambda: False  # noqa: E731
    client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=probe)
    assert captured["pause_probe"] is probe


def test_prompt_idle_timeout_still_sets_truncated_not_paused(monkeypatch):
    """回归护栏:idle 超时仍走 truncated 路径,不误置 paused"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(client, "_rpc", MagicMock(side_effect=PromptIdleTimeout(60, False)))
    monkeypatch.setattr(client, "cancel", lambda sid: None)
    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=lambda: False)
    assert result == {}
    assert client.last_prompt_truncated is not None
    assert client.last_prompt_paused is False


# ============================================================
# ACPClient._rpc:on_event 分发前检查(活跃流路径)
# ============================================================


def test_rpc_raises_before_dispatching_notification(monkeypatch):
    """活跃流:命中暂停 → 分发通知前抛 ACPPromptPaused(on_event 不被调用)"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{"update":{}}}',
        "",
        'data: {"jsonrpc":"2.0","result":{"stopReason":"end_turn"},"id":3}',
        "",
    ]
    _install_stream(monkeypatch, client, _FakeStreamResponse(lines))
    events: list = []
    with pytest.raises(ACPPromptPaused):
        client._rpc(
            {"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3},
            on_event=events.append,
            pause_probe=lambda: True,
        )
    assert events == []  # 命中即停,不再转发


def test_rpc_no_pause_probe_behaves_normally(monkeypatch):
    """回归护栏:不传 pause_probe → 通知照常分发并返回最终结果"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{"update":{}}}',
        "",
        'data: {"jsonrpc":"2.0","result":{"stopReason":"end_turn"},"id":3}',
        "",
    ]
    _install_stream(monkeypatch, client, _FakeStreamResponse(lines))
    events: list = []
    result = client._rpc(
        {"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3},
        on_event=events.append,
        pause_probe=lambda: False,
    )
    assert result == {"stopReason": "end_turn"}
    assert len(events) == 1


# ============================================================
# 看门狗:静默期轮询空转检查暂停
# ============================================================


def test_watchdog_raises_on_pause_during_quiet(monkeypatch):
    """CLI 静默(无数据行):看门狗在 queue.Empty 分支命中暂停即抛"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.01)
    resp = _BlockingStreamResponse(hold_secs=2.0)
    # idle_probe 非 None 才启用看门狗;这里返回 False(无活动工具)
    gen = client._iter_lines_with_watchdog(resp, idle_probe=lambda: False, pause_probe=lambda: True)
    with pytest.raises(ACPPromptPaused):
        for _ in gen:
            pass


def test_watchdog_no_pause_when_probe_false(monkeypatch):
    """回归护栏:未暂停时看门狗不因暂停逻辑报错(CLI 结束后正常 return)"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.01)
    resp = _FakeStreamResponse([])  # 立即结束
    out = list(client._iter_lines_with_watchdog(resp, idle_probe=lambda: False, pause_probe=lambda: False))
    assert out == []


# ============================================================
# orchestrator._consume_paused_interrupt_flag(契约消费)
# ============================================================


class _FakeTask:
    def __init__(self, params):
        self.id = "task-1"
        self.params = params


class _FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def test_consume_paused_flag_absent_returns_false_no_commit():
    """builtin 任务不写标志 → 返回 False 且不落库(行为无变化)"""
    task = _FakeTask({"_plan": []})
    db = _FakeDb()
    assert _consume_paused_interrupt_flag(task, db) is False
    assert db.commits == 0


def test_consume_paused_flag_true_clears_and_commits():
    """命中标志 → 返回 True 并从 params 移除后落库(不影响下一轮)"""
    task = _FakeTask({"_plan": [], "_round_paused_interrupt": True})
    db = _FakeDb()
    assert _consume_paused_interrupt_flag(task, db) is True
    assert "_round_paused_interrupt" not in task.params
    assert task.params.get("_plan") == []  # 其它键保留
    assert db.commits == 1
