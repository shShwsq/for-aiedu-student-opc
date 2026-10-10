"""异步后台子 Agent 提前收尾的续轮回收逻辑单元测试

覆盖:
- _looks_like_pending_async_report:命中"在等后台结果"口吻(中英),完整报告不误判
- _ASYNC_AGENT_LAUNCH_RE:Qoder Agent "Async agent launched…background" 回执识别
- _async_agent_autoccontinue_enabled:全局开关 + agent 类型白名单
- _async_agent_collect_results:补发续轮的三种结局(交付/续等后交付/耗尽)+ 连接异常善后

背景:Qoder 的 Agent 工具 fire-and-forget,模型常在子 Agent 未回报时就 end_turn,
平台据此显"已完成";修复在同一活跃 session 上补发 prompt 回收结果。
"""
from types import SimpleNamespace

import pytest

from app.agents import acp_base as A
from app.config import settings


# ============================================================
# 待完成口吻判定
# ============================================================

def test_pending_report_matches_english_and_chinese():
    assert A._looks_like_pending_async_report(
        "INTERIM: launched 2 background sub-agents, will deliver combined report after they finish."
    )
    assert A._looks_like_pending_async_report(
        "已完成初步排查。等待 4 路并行审计代理返回详细结果后我会给完整报告。"
    )
    assert A._looks_like_pending_async_report("still waiting for the other sub-agents")


def test_pending_report_ignores_full_report():
    # 真实完整报告:不含"在等结果"口吻 → 不误判
    assert not A._looks_like_pending_async_report(
        "# 1. JWT 安全陷阱\n- alg:none 混淆\n# 2. SQL 注入\n- 使用参数化查询防止注入"
    )
    assert not A._looks_like_pending_async_report("")


# ============================================================
# Agent 异步派生回执识别
# ============================================================

def test_async_launch_regex_matches_qoder_ack():
    ack = (
        "Async agent launched successfully.\n"
        "The agent is working in the background. You will be notified automatically "
        "when it completes."
    )
    assert A._ASYNC_AGENT_LAUNCH_RE.search(ack)
    assert not A._ASYNC_AGENT_LAUNCH_RE.search("子任务完成,发现 3 处问题")


# ============================================================
# 启用门控
# ============================================================

def test_autoccontinue_gate_flag_and_type(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", True)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_TYPES", "qoder_cli,codex_cli")
    assert A._async_agent_autoccontinue_enabled("qoder_cli") is True
    assert A._async_agent_autoccontinue_enabled("deepseek_cli") is False

    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", False)
    assert A._async_agent_autoccontinue_enabled("qoder_cli") is False


# ============================================================
# 续轮回收循环
# ============================================================

class _StubCollector:
    def __init__(self, initial: str = ""):
        self.content_full = initial
        self.has_active_tools = False
        self.async_agent_launch_count = 2


class _StubClient:
    """每次 prompt 追加一段预设文本到 collector;遇 Exception 则抛出"""

    def __init__(self, collector, responses):
        self._collector = collector
        self._responses = responses
        self._i = 0
        self.calls = 0

    def prompt(self, sid, msgs, on_event=None, idle_probe=None):
        item = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        self._collector.content_full += item
        return {"stopReason": "end_turn"}


@pytest.fixture()
def _fast_settings(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_MAX_CONTINUE", 3)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_CONTINUE_WAIT_SECONDS", 0)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_CONTINUE_BACKOFF_FACTOR", 1.0)


task = SimpleNamespace(id="t-verify")


def test_continue_delivers_on_first_followup(_fast_settings):
    col = _StubCollector("INTERIM: will deliver report after they finish.")
    client = _StubClient(col, ["# Complete combined report: JWT + SQL findings."])
    ctx = {"continued": True, "still_pending": False}
    A._async_agent_collect_results(
        client, "sess", col, ctx, task=task, round_idx=1, agent_type="qoder_cli"
    )
    assert client.calls == 1
    assert ctx["still_pending"] is False
    assert "Complete combined report" in col.content_full


def test_continue_loops_until_delivered(_fast_settings):
    col = _StubCollector("waiting for the background agents")
    client = _StubClient(col, [
        "still waiting for sub-agent B",
        "# Final full report delivered now.",
    ])
    ctx = {"continued": True, "still_pending": False}
    A._async_agent_collect_results(
        client, "sess", col, ctx, task=task, round_idx=1, agent_type="qoder_cli"
    )
    assert client.calls == 2
    assert ctx["still_pending"] is False


def test_continue_exhausts_max_attempts(_fast_settings):
    col = _StubCollector("will report after they finish")
    client = _StubClient(col, ["still waiting for background results"])
    ctx = {"continued": True, "still_pending": False}
    A._async_agent_collect_results(
        client, "sess", col, ctx, task=task, round_idx=1, agent_type="qoder_cli"
    )
    assert client.calls == settings.ACP_ASYNC_AGENT_MAX_CONTINUE
    assert ctx["still_pending"] is True


def test_continue_stops_on_connection_error(_fast_settings):
    col = _StubCollector("waiting")
    client = _StubClient(col, [ConnectionError("bridge down")])
    ctx = {"continued": True, "still_pending": False}
    A._async_agent_collect_results(
        client, "sess", col, ctx, task=task, round_idx=1, agent_type="qoder_cli"
    )
    assert client.calls == 1
    assert ctx["still_pending"] is True
