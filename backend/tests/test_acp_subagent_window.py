"""子智能体并行扇出窗口:主时间线止乱 + 每个 Agent 报告照常落库。

背景(见 logs/acp 实测 + docs):Qoder CLI 通过 ACP 的**单条 session/update**
把并行子智能体的思考/正文/内部工具无差别混发来,且**事件不带任何子智能体身份**
(chunk 的 _meta 为空、无 toolCallId;tool_call 的 _meta.qoder 只有 toolName)。
后端 _ACPCollector 若照常写入主 agent 的线性流会交织成乱码并重复渲染。

覆盖:
- 窗口内文本块(agent_thought_chunk / agent_message_chunk)不并入主流:
  既不推 thinking_delta,也不累积到 content_full / reasoning_full
- 窗口内非 Agent 工具(Bash):不落库、其 completed 也不落库(整体隔离)
- Agent 工具的 tool_call 落库(开窗),其 completed 报告作为 tool_result 落库(关窗)
- 窗口边界后主 agent 自己的正文恢复正常推送
不连真实 DB:monkeypatch _add_conversation 与 publish。
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import _ACPCollector


@pytest.fixture
def mock_task():
    task = MagicMock()
    task.id = f"test-task-{uuid.uuid4()}"
    return task


@pytest.fixture
def captured(mock_task, monkeypatch):
    """构造 collector,mock 掉落库与 SSE,捕获 publish 事件与落库 content"""
    events: list[tuple[str, dict]] = []
    saved: list[dict] = []

    def fake_add_conversation(db, task, *, round_idx, role, type, content, **kw):
        conv = MagicMock()
        conv.id = f"conv-{uuid.uuid4()}"
        saved.append({"type": type, "content": content})
        return conv

    monkeypatch.setattr(acp_base, "_add_conversation", fake_add_conversation)
    monkeypatch.setattr(
        acp_base, "publish",
        lambda task_id, event_type, data: events.append((event_type, data)),
    )
    c = _ACPCollector(mock_task, MagicMock(db=True), round_idx=1, agent_type="qoder_cli")
    return c, events, saved


# ---- 事件构造器(贴合 Qoder ACP 原样形状) ----

def su(update: dict) -> dict:
    return {"method": "session/update", "params": {"sessionId": "s", "update": update}}


def chunk(kind: str, text: str) -> dict:
    # 实测:文本块 _meta 为空、无 toolCallId
    return su({"sessionUpdate": kind, "content": {"type": "text", "text": text}})


def agent_call(tcid: str, desc: str) -> dict:
    return su({
        "sessionUpdate": "tool_call", "toolCallId": tcid, "status": "pending",
        "title": "Agent", "kind": "think",
        "rawInput": {"description": desc, "subagent_type": "Explore", "run_in_background": False},
        "_meta": {"qoder": {"toolName": "Agent"}},
    })


def bash_call(tcid: str, cmd: str) -> dict:
    return su({
        "sessionUpdate": "tool_call", "toolCallId": tcid, "status": "pending",
        "title": cmd, "kind": "execute", "rawInput": {"command": cmd},
        "_meta": {"qoder": {"toolName": "Bash"}},
    })


def completed(tcid: str, out: str) -> dict:
    return su({
        "sessionUpdate": "tool_call_update", "toolCallId": tcid,
        "status": "completed", "rawOutput": out,
    })


def thinking_events(events):
    return [d for e, d in events if e == "thinking_delta" and d.get("delta")]


def test_window_suppresses_subagent_text_and_isolates_internal_tools(captured):
    c, events, saved = captured

    def n_calls():
        return sum(1 for s in saved if s["type"] == "tool_call")

    def n_results():
        return sum(1 for s in saved if s["type"] == "tool_result")

    def calls_with(tag):
        return [s for s in saved if s["type"] == "tool_call" and tag in s["content"]]

    # 1) 主 agent 开窗前的正文:正常推送
    c(chunk("agent_message_chunk", "主agent开场"))
    assert any("主agent开场" in (d.get("delta") or "")
               for _, d in events if _ == "thinking_delta")
    assert c.content_full == "主agent开场"
    assert not c.in_subagent_window
    events.clear()

    # 2) 并行发起两个子智能体(第二个到达时窗口已开,仍照常落库/开窗)
    c(agent_call("call_b48", "Audit backend layering"))
    assert c.in_subagent_window
    c(agent_call("call_b1b", "Audit frontend architecture"))
    assert c._pending_agent_ids == {"call_b48", "call_b1b"}
    assert len(calls_with("[Agent]")) == 2  # 两条子智能体调用都落库
    events.clear()
    base_calls, base_results = n_calls(), n_results()

    # 3) 窗口内:两个子智能体的思考/正文交织 —— 一律不并入主流,也不落库
    c(chunk("agent_thought_chunk", "子A在想"))
    c(chunk("agent_message_chunk", "lib `http.server"))
    c(chunk("agent_message_chunk", "5`, `cod"))
    c(chunk("agent_thought_chunk", "子B在想"))
    assert thinking_events(events) == []          # 无乱码流
    assert c.content_full == "主agent开场"          # 未被子智能体正文污染
    assert c.reasoning_full == ""                  # 未被子智能体思考污染
    assert (n_calls(), n_results()) == (base_calls, base_results)

    # 4) 窗口内子智能体的内部工具:整体隔离(既无 call 也无 result 落库)
    c(bash_call("call_606", "ls -l"))
    c(completed("call_606", "file listing output"))
    assert (n_calls(), n_results()) == (base_calls, base_results)
    assert "call_606" not in c._pending_tool_calls   # 未进 pending,不影响 idle 判定

    # 5) 子智能体完成:各自的完整报告作为 tool_result 落库(报告在 completed 里)
    c(completed("call_b48", "# Architecture Review \u2014 backend\n完整报告A"))
    assert c.in_subagent_window                       # 仍有第二个在跑 → 窗口保持开启
    c(chunk("agent_message_chunk", "乱码绝不该进主流程"))
    assert thinking_events(events) == []
    assert c.content_full == "主agent开场"
    c(completed("call_b1b", "## Architecture Review \u2014 frontend\n完整报告B"))
    assert not c.in_subagent_window                   # 两个都完成 → 窗口关闭
    assert n_results() == base_results + 2            # 各落一条报告
    result_contents = [s["content"] for s in saved if s["type"] == "tool_result"]
    assert any("完整报告A" in x for x in result_contents)
    assert any("完整报告B" in x for x in result_contents)
    assert all("file listing" not in x for x in result_contents)
    assert len(calls_with("[Bash]")) == 0

    # 6) 窗口关闭后:主 agent 自己的收尾正文恢复正常
    events.clear()
    c(chunk("agent_message_chunk", "主agent最终总结"))
    assert any("主agent最终总结" in (d.get("delta") or "")
               for _, d in events if _ == "thinking_delta")
    assert c.content_full.endswith("主agent最终总结")


def test_single_subagent_live_output_still_suppressed_from_main(captured):
    """单路子智能体同样不并入主思考流(其报告只走 tool_result,避免重复)。"""
    c, events, saved = captured
    c(agent_call("call_x", "Only child"))
    assert c.in_subagent_window
    events.clear()
    c(chunk("agent_thought_chunk", "child reasoning that must not leak"))
    assert thinking_events(events) == []
    assert c.reasoning_full == ""
    c(completed("call_x", "child final report"))
    assert not c.in_subagent_window
    assert any("child final report" in s["content"]
               for s in saved if s["type"] == "tool_result")


def test_async_background_agent_still_detected_after_window_close(captured):
    """后台异步子智能体(run_in_background)完成即关窗,异步派生计数不受抑制影响。"""
    c, events, saved = captured
    async_ack = "Async agent launched successfully, working in the background"
    c(agent_call("call_async", "bg job"))
    assert c.in_subagent_window
    assert c.async_agent_launch_count == 0
    c(completed("call_async", async_ack))
    # 关窗 + 记录一次异步派生(供收尾续轮回收)
    assert not c.in_subagent_window
    assert c.async_agent_launch_count == 1
