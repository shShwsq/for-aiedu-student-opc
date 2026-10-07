"""CLI 挂死兜底(idle 看门狗)+ 沙箱续期 单元测试

覆盖:
- _ACPCollector.has_active_tools 生命周期(tool_call → completed/failed 终结)
- _iter_lines_with_watchdog:挂死超时、心跳不重置 idle、工具在跑用宽松阈值、
  配置 0 关闭、读取层错误透传
- ACPClient.prompt 捕获 PromptIdleTimeout → cancel + 空结果 + 截断标记
- SandboxSession.renew / auto_renew(sandbox 模式调 SDK,local 模式 no-op)
- sandbox_tools._get_or_create_session 复用会话时按探活节流续期(探活已并入
  _probe_session,见 test_sandbox_session_expiry.py)
"""
import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import ACPClient, PromptIdleTimeout, _ACPCollector
from app.config import settings
from app.sandbox.client import SandboxSession
from app.tools import sandbox_tools


@pytest.fixture(autouse=True)
def _stub_db_and_sse(monkeypatch):
    """不连真实 DB:mock 掉 _add_conversation(避免构造 Conversation ORM 触发
    全量 mapper 配置)与 publish,与 test_acp_tool_intent.py 保持一致"""
    monkeypatch.setattr(
        acp_base, "_add_conversation", lambda *a, **kw: MagicMock(),
    )
    monkeypatch.setattr(acp_base, "publish", lambda *a, **kw: None)


# ============================================================
# 辅助
# ============================================================


class _FakeResponse:
    """模拟 httpx SSE response:先吐给定行,然后可选挂起(模拟 CLI 挂死)"""

    def __init__(self, lines: list[str], hang_secs: float = 0.0, error=None):
        self._lines = lines
        self._hang_secs = hang_secs
        self._error = error
        self._stop = threading.Event()

    def iter_lines(self):
        for ln in self._lines:
            yield ln
        if self._error is not None:
            raise self._error
        if self._hang_secs:
            self._stop.wait(self._hang_secs)

    def stop(self):
        self._stop.set()


def _mk_collector() -> _ACPCollector:
    task = MagicMock()
    task.id = "t-test"
    return _ACPCollector(task, MagicMock(), 1)


def _tool_msg(tool_call_id: str) -> dict:
    return {
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_call_id,
                "title": "terminal: git clone x",
                "kind": "execute",
            },
        },
    }


def _tool_update_msg(tool_call_id: str, status: str) -> dict:
    return {
        "method": "session/update",
        "params": {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": status,
                "rawOutput": "ok",
            },
        },
    }


# ============================================================
# collector:活动工具状态(idle 分级判据)
# ============================================================


def test_has_active_tools_lifecycle_completed():
    c = _mk_collector()
    assert not c.has_active_tools
    c(_tool_msg("tc1"))
    assert c.has_active_tools
    c(_tool_update_msg("tc1", "completed"))
    assert not c.has_active_tools


def test_has_active_tools_lifecycle_failed():
    """failed 也是终态:清理 pending(否则 has_active_tools 恒真,兜底误用宽松阈值)"""
    c = _mk_collector()
    c(_tool_msg("tc1"))
    assert c.has_active_tools
    c(_tool_update_msg("tc1", "failed"))
    assert not c.has_active_tools


def test_in_progress_keeps_tool_active():
    c = _mk_collector()
    c(_tool_msg("tc1"))
    c(_tool_update_msg("tc1", "in_progress"))
    assert c.has_active_tools


# ============================================================
# idle 看门狗
# ============================================================


def test_watchdog_fires_on_cli_hang(monkeypatch):
    """无活动工具 + 流挂起 → 超过 OUTPUT 阈值抛 PromptIdleTimeout"""
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_OUTPUT_SECONDS", 0.2)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_TOOL_SECONDS", 100)

    client = ACPClient("http://fake")
    resp = _FakeResponse(['data: {"a":1}', ""], hang_secs=5)
    got = []
    with pytest.raises(PromptIdleTimeout) as ei:
        for ln in client._iter_lines_with_watchdog(resp, idle_probe=lambda: False):
            got.append(ln)
    resp.stop()
    assert len(got) == 2  # 挂起前的行正常透传
    assert ei.value.tool_active is False


def test_watchdog_heartbeat_does_not_reset_idle(monkeypatch):
    """bridge 的 ': idle Ns' 心跳注释只证明 bridge 活着,不阻止挂死判定"""
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_OUTPUT_SECONDS", 0.2)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_TOOL_SECONDS", 100)

    client = ACPClient("http://fake")
    resp = _FakeResponse(['data: {"a":1}', ": idle 5s", ": idle 10s"], hang_secs=5)
    with pytest.raises(PromptIdleTimeout):
        for _ in client._iter_lines_with_watchdog(resp, idle_probe=lambda: False):
            pass
    resp.stop()


def test_watchdog_tool_active_uses_loose_threshold(monkeypatch):
    """有工具在跑:静默超过 OUTPUT 阈值但未达 TOOL 阈值 → 不误伤,流正常结束"""
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_OUTPUT_SECONDS", 0.1)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_TOOL_SECONDS", 100)

    client = ACPClient("http://fake")
    # 挂起 0.5s(> OUTPUT 0.1s)后流结束:probe=True 时不应超时
    resp = _FakeResponse(['data: {"a":1}'], hang_secs=0.5)
    got = [ln for ln in client._iter_lines_with_watchdog(resp, idle_probe=lambda: True)]
    assert got == ['data: {"a":1}']

    # 对照:同样挂起但 probe=False → 触发 OUTPUT 超时
    resp2 = _FakeResponse(['data: {"a":1}'], hang_secs=5)
    with pytest.raises(PromptIdleTimeout) as ei:
        for _ in client._iter_lines_with_watchdog(resp2, idle_probe=lambda: False):
            pass
    resp2.stop()
    assert ei.value.tool_active is False


def test_watchdog_disabled_when_threshold_zero(monkeypatch):
    """两个阈值都为 0 → 关闭兜底,挂起也不超时(流结束后正常返回)"""
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_OUTPUT_SECONDS", 0)
    monkeypatch.setattr(settings, "ACP_IDLE_TIMEOUT_TOOL_SECONDS", 0)

    client = ACPClient("http://fake")
    resp = _FakeResponse(['data: {"a":1}'], hang_secs=0.3)
    got = [ln for ln in client._iter_lines_with_watchdog(resp, idle_probe=lambda: False)]
    assert got == ['data: {"a":1}']


def test_watchdog_propagates_stream_error(monkeypatch):
    """读取层错误(连接断开等)按原样透传,与原同步行为一致"""
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.05)
    client = ACPClient("http://fake")
    boom = ConnectionError("stream broken")
    resp = _FakeResponse([], error=boom)
    with pytest.raises(ConnectionError):
        for _ in client._iter_lines_with_watchdog(resp, idle_probe=lambda: False):
            pass


# ============================================================
# prompt 善后:cancel + 截断标记 + 空结果
# ============================================================


def test_prompt_idle_timeout_salvages(monkeypatch):
    client = ACPClient("http://fake")
    monkeypatch.setattr(
        client, "_rpc",
        MagicMock(side_effect=PromptIdleTimeout(300.0, False)),
    )
    cancelled = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))

    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}])

    assert result == {}  # 不抛异常,调用方用已累积输出收尾
    assert cancelled == ["sess-1"]
    assert client.last_prompt_truncated is not None
    assert "300s" in client.last_prompt_truncated


def test_prompt_resets_truncated_flag(monkeypatch):
    """每次 prompt 前重置截断标记,避免上一轮的状态串到本轮"""
    client = ACPClient("http://fake")
    client.last_prompt_truncated = "stale"
    monkeypatch.setattr(client, "_rpc", MagicMock(return_value={"stopReason": "end"}))
    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}])
    assert result == {"stopReason": "end"}
    assert client.last_prompt_truncated is None


# ============================================================
# 沙箱续期
# ============================================================


def test_renew_sandbox_mode_calls_sdk():
    mock_sb = MagicMock()
    mock_sb.renew.return_value = MagicMock(expires_at="2026-08-14T22:00:00Z")
    s = SandboxSession(mode="sandbox", sandbox=mock_sb)
    assert s.renew(timeout_minutes=30) is True
    mock_sb.renew.assert_called_once_with(timedelta(minutes=30))


def test_renew_failure_returns_false():
    """续期失败(沙箱已被回收)不抛异常,返回 False 由调用方决策"""
    mock_sb = MagicMock()
    mock_sb.renew.side_effect = RuntimeError("Status code: 404")
    s = SandboxSession(mode="sandbox", sandbox=mock_sb)
    assert s.renew() is False


def test_renew_local_mode_noop():
    s = SandboxSession(mode="local")
    try:
        assert s.renew() is True  # local 无 TTL 概念
    finally:
        s.close()


def test_auto_renew_calls_periodically():
    mock_sb = MagicMock()
    s = SandboxSession(mode="sandbox", sandbox=mock_sb)
    with s.auto_renew(interval_minutes=0.005):  # 0.3s 间隔
        time.sleep(0.8)
    assert mock_sb.renew.call_count >= 1  # 至少续期一次


def test_auto_renew_local_mode_noop():
    s = SandboxSession(mode="local")
    try:
        with s.auto_renew(interval_minutes=0.005):
            pass  # 不抛异常即可
    finally:
        s.close()


# ============================================================
# sandbox_tools:会话复用时按探活节流续期
# (探活走真 SandboxSession.probe_alive,它内部就是 SDK 的 renew —— 一次往返
# 同时拿到"容器还在吗"和"TTL 往后推",所以不再有单独的 renew 节流分支)
# ============================================================


def _sandbox_session_with_mock():
    """真 SandboxSession + mock 的 SDK 对象(探活只对自家会话生效)"""
    mock_sb = MagicMock()
    return SandboxSession(mode="sandbox", sandbox=mock_sb), mock_sb


def test_session_reuse_renews_after_interval(monkeypatch):
    monkeypatch.setattr(sandbox_tools, "_SANDBOX_PROBE_INTERVAL", 0.0)
    session, mock_sb = _sandbox_session_with_mock()
    monkeypatch.setattr(sandbox_tools, "create_sandbox", lambda **kw: session)
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")

    tid = "test-renew-task"
    sandbox_tools._sessions.pop(tid, None)
    try:
        sandbox_tools._get_or_create_session(tid)
        assert mock_sb.renew.call_count == 0  # 创建即起算 TTL,不续期
        sandbox_tools._get_or_create_session(tid)
        assert mock_sb.renew.call_count == 1  # 节流=0:复用即探活顺带续期
    finally:
        sandbox_tools._sessions.pop(tid, None)


def test_session_reuse_throttles_renew_within_interval(monkeypatch):
    session, mock_sb = _sandbox_session_with_mock()
    monkeypatch.setattr(sandbox_tools, "create_sandbox", lambda **kw: session)
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")

    tid = "test-renew-task-2"
    sandbox_tools._sessions.pop(tid, None)
    try:
        sandbox_tools._get_or_create_session(tid)
        sandbox_tools._get_or_create_session(tid)  # 节流窗口内复用
        assert mock_sb.renew.call_count == 0  # 不调 Server API
    finally:
        sandbox_tools._sessions.pop(tid, None)
