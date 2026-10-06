"""runtime 共享层单元测试 + agent2 / verifier 文本 tool_call 兜底回归

背景:三个智能体(react_agent / agent2 / verifier_agent)此前各自手抄
流式调用与工具累积实现,react_agent 为 GLM/Qwen 思考模式做的文本
tool_call 兜底没有覆盖 agent2 / verifier——这类模型把工具调用写在
正文而非结构化通道,导致 agent2 审查 JSON 解析失败、verifier 把工具
调用文本当验证总结提前返回。收敛到 runtime 后,本测试锚定:

1. 共享常量单一事实源(react_agent / agent2 与 runtime 同一对象)
2. build_tool_intent 注册表(含 agent2 / verifier 特有工具 + prefix)
3. ToolCallAccumulator 跨 chunk 累积
4. stream_llm 事件序列(start/reasoning/content/end、publish_content
   开关、error 透传)
5. agent2 审查循环的文本 tool_call 兜底(端到端:文本块 → 工具执行
   → 二次调用 → JSON 正常解析,而非 parse_failed)
6. verifier 循环的文本 tool_call 兜底(文本块 → PoC 实际执行,
   而非把工具调用文本当总结返回)

特殊标记字符不直接写在测试源码里,从 runtime 正则模式反推构造,
保证与实现逐字节一致。
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.agents.agent2 as agent2
import app.agents.verifier_agent as verifier_agent
from app.agents.runtime import llm_stream as rt_stream
from app.agents.runtime.conversation import record_conversation
from app.agents.runtime.constants import (
    MAX_HISTORY_MSG_CHARS,
    MAX_HISTORY_TOTAL_CHARS,
)
from app.agents.runtime.llm_stream import ToolCallAccumulator, stream_llm
from app.agents.runtime.tool_intent import build_tool_intent


# ============================================================
# 辅助:从 runtime 正则反推文本工具调用标记(避免手写特殊字符)
# ============================================================


def _text_tool_call_block(name: str, arguments: dict) -> str:
    """构造与 runtime 解析器匹配的 Hermes 风格文本工具调用块"""
    parts = rt_stream._TEXT_TOOL_CALL_RE.pattern.split(r"\s*")
    open_marker, close_marker = parts[0], parts[-1]
    payload = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)
    return f"{open_marker}\n{payload}\n{close_marker}"


# ============================================================
# 1) 共享常量:单一事实源
# ============================================================


def test_history_constants_single_source():
    """react_agent / agent2 的历史截断常量与 runtime 同一对象(防再手抄一份)。"""
    from app.agents import react_agent

    assert react_agent.MAX_HISTORY_MSG_CHARS is MAX_HISTORY_MSG_CHARS
    assert MAX_HISTORY_MSG_CHARS == 3000
    assert agent2.MAX_HISTORY_MSG_CHARS is MAX_HISTORY_MSG_CHARS
    assert agent2.MAX_HISTORY_TOTAL_CHARS is MAX_HISTORY_TOTAL_CHARS
    assert MAX_HISTORY_TOTAL_CHARS == 12000


# ============================================================
# 2) build_tool_intent:注册表 + prefix
# ============================================================


def test_tool_intent_agent2_read_tools_with_prefix():
    """agent2 只读核查工具:runtime 正文 + agent2 质检前缀 + 工具名标签。"""
    intent = build_tool_intent(
        "read_file", {"file_path": "src/a.py"}, prefix="[agent2 质检]"
    )
    assert intent == "[agent2 质检] 读取文件 src/a.py [read_file]"


def test_tool_intent_check_reference_truncates_url():
    """引用复核意图:url 截断 120 字符,末尾带 [check_reference]。"""
    long_url = "https://x.io/" + "a" * 300
    intent = build_tool_intent(
        "check_reference", {"url": long_url}, prefix="[agent2 质检]"
    )
    assert intent.startswith("[agent2 质检] 复核引用: ")
    assert "a" * 300 not in intent
    assert intent.endswith("[check_reference]")


def test_tool_intent_verifier_tools():
    """verifier 工具:http_request 保留验证语义,run_python_code 带代码首行预览。"""
    assert (
        verifier_agent._build_tool_intent("http_request", {"method": "get", "path": "/login"})
        == "验证请求: GET /login [http_request]"
    )
    intent = verifier_agent._build_tool_intent(
        "run_python_code", {"code": "import requests\nr = requests.get('http://x')"}
    )
    assert "执行 Python 代码: import requests" in intent
    assert intent.endswith("[run_python_code]")


def test_tool_intent_unknown_tool_fallback():
    """未知工具回退"调用 {fn_name}",前缀与标签位置不受影响。"""
    intent = build_tool_intent("custom_tool", {"x": 1}, prefix="[agent2 质检]")
    assert intent == "[agent2 质检] 调用 custom_tool [custom_tool]"


# ============================================================
# 3) ToolCallAccumulator:跨 chunk 累积
# ============================================================


def _td(index, id=None, name=None, arguments_fragment=None):
    return SimpleNamespace(
        index=index, id=id, name=name, arguments_fragment=arguments_fragment
    )


def test_tool_call_accumulator_merges_fragments():
    """工具调用参数跨 chunk 拼接,后续 chunk 可补 id/name。"""
    acc = ToolCallAccumulator()
    acc.add([_td(0, id="c1", name="read_file", arguments_fragment='{"file')])
    acc.add([_td(0, arguments_fragment='_path": "a.py"}')])
    acc.add([_td(1, name=None, arguments_fragment=None)])
    acc.add([_td(1, id="c2", name="search_code", arguments_fragment="{}")])

    calls = acc.tool_calls
    assert len(calls) == 2
    assert len(acc) == 2
    assert calls[0]["id"] == "c1"
    assert calls[0]["name"] == "read_file"
    assert calls[0]["arguments_str"] == '{"file_path": "a.py"}'
    assert calls[1]["name"] == "search_code"


def test_tool_call_accumulator_empty_add_is_noop():
    acc = ToolCallAccumulator()
    acc.add(None)
    acc.add([])
    assert acc.tool_calls == []


# ============================================================
# 4) stream_llm:事件序列 / publish_content 开关 / error 透传
# ============================================================


class _FakeChunk:
    def __init__(self, reasoning_delta="", content_delta="",
                 tool_call_deltas=None, finish_reason=None):
        self.reasoning_delta = reasoning_delta
        self.content_delta = content_delta
        self.tool_call_deltas = tool_call_deltas
        self.finish_reason = finish_reason


def _fake_client(chunks):
    client = MagicMock()
    client.chat_stream = MagicMock(return_value=iter(chunks))
    return client


@pytest.fixture()
def captured_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        rt_stream, "publish",
        lambda tid, etype, data: events.append((etype, data)),
    )
    monkeypatch.setattr(
        rt_stream, "perf_log", lambda *a, **k: None,
    )
    return events


def test_stream_llm_event_sequence(captured_events):
    """事件序列:start → reasoning → content → end;结果字段完整。"""
    client = _fake_client([
        _FakeChunk(reasoning_delta="思"),
        _FakeChunk(reasoning_delta="考", content_delta="答"),
        _FakeChunk(content_delta="案", finish_reason="stop"),
    ])
    result = stream_llm(
        client, [{"role": "user", "content": "q"}],
        task_id="t-1", round_idx=1, role="agent1",
        iteration=2, extra={"k": "v"},
    )
    assert result.reasoning == "思考"
    assert result.content == "答案"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.conv_id

    phases = [(d["phase"], d.get("k")) for _, d in captured_events]
    assert phases == [
        ("start", "v"), ("reasoning", "v"), ("reasoning", "v"),
        ("content", "v"), ("content", "v"), ("end", "v"),
    ]
    # iteration 与 extra 都进了 payload
    assert all(d.get("iteration") == 2 and d.get("role") == "agent1"
               for _, d in captured_events)


def test_stream_llm_publish_content_off(captured_events):
    """publish_content=False(agent2 用):content 增量不推,思考链照推。"""
    client = _fake_client([
        _FakeChunk(reasoning_delta="思", content_delta="JSON"),
        _FakeChunk(finish_reason="stop"),
    ])
    result = stream_llm(
        client, [{"role": "user", "content": "q"}],
        task_id="t-1", round_idx=1, role="agent2",
        publish_content=False,
    )
    assert result.content == "JSON"
    assert [d["phase"] for _, d in captured_events] == ["start", "reasoning", "end"]


def test_stream_llm_phase_start_off(captured_events):
    """phase_start=False(verifier 用):不推 start,首条即 reasoning。"""
    client = _fake_client([
        _FakeChunk(reasoning_delta="验证思考"),
        _FakeChunk(finish_reason="stop"),
    ])
    stream_llm(
        client, [{"role": "user", "content": "q"}],
        task_id="t-1", round_idx=1, role="agent2",
        phase_start=False, extra={"verify": True},
    )
    assert [d["phase"] for _, d in captured_events] == ["reasoning", "end"]
    assert all(d.get("verify") is True for _, d in captured_events)


def test_stream_llm_error_publishes_then_raises(captured_events):
    """异常:先推 error 事件,再原样抛出(降级策略归调用方)。"""
    def _boom(*a, **k):
        raise RuntimeError("网络断开")
        yield  # pragma: no cover

    client = MagicMock()
    client.chat_stream = _boom
    with pytest.raises(RuntimeError):
        stream_llm(
            client, [{"role": "user", "content": "q"}],
            task_id="t-1", round_idx=1, role="agent2",
        )
    assert captured_events[-1][1]["phase"] == "error"
    assert "网络断开" in captured_events[-1][1]["delta"]


# ============================================================
# 5) agent2 审查循环:文本 tool_call 兜底(端到端)
# ============================================================


def test_agent2_text_tool_call_fallback_executes_tool(monkeypatch):
    """GLM/Qwen 文本工具调用 → 兜底解析 → 只读工具执行 → 正常输出 JSON。

    回归锚点:兜底接入前,这条链路会把工具调用文本当最终 JSON 解析,
    结果 parse_failed、review 标记失败。
    """
    final_json = (
        '{"covered": [], "missing": [], "reasoning": "核查完成", '
        '"suggestions": [], "results": [{"title": "点", "content": "内容"}], '
        '"grouping": null}'
    )
    stream_calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        stream_calls.append([dict(m) for m in messages])
        if len(stream_calls) == 1:
            content = (
                "我先读取真实源码核对\n"
                + _text_tool_call_block(
                    "read_file", {"file_path": "src/api/users.py"}
                )
                + "\n再确认行号"
            )
            return (content, [], "第一步:核对文件")
        return (final_json, [], "第二步:输出结论")

    executed = []

    def _fake_read_tool(fn_name, args, repo_path, task_id, **kw):
        executed.append((fn_name, args))
        return json.dumps({"ok": True, "lines": ["def login(): ..."]})

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)
    monkeypatch.setattr(agent2, "_execute_read_tool", _fake_read_tool)

    result = agent2.run_agent2(
        "审计这个仓库", [{"round": 1, "summary": "agent1 总结"}],
        task_id="task-fb", db=None, round_idx=1,
        client=MagicMock(), repo_path="/tmp/ws",
    )

    # 工具被真实执行(兜底生效,而非 parse_failed)
    assert executed == [("read_file", {"file_path": "src/api/users.py"})]
    assert result.get("parse_failed") is None
    assert result["reasoning"] == "核查完成"
    assert result["results"][0]["title"] == "点"

    # 二次调用的上下文:assistant 消息已剥离工具调用文本块,
    # 并带结构化 tool_calls;工具结果以 role=tool 回灌
    msgs2 = stream_calls[1]
    assistant_msgs = [m for m in msgs2 if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert "src/api/users.py" not in assistant_msgs[0]["content"]
    assert "再确认行号" in assistant_msgs[0]["content"]
    assert assistant_msgs[0]["tool_calls"][0]["function"]["name"] == "read_file"
    tool_msgs = [m for m in msgs2 if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and "ok" in tool_msgs[0]["content"]


def test_agent2_structured_tool_calls_unaffected(monkeypatch):
    """结构化 tool_calls 通道照旧:兜底不改变正常路径行为。"""
    final_json = (
        '{"covered": [], "missing": [], "reasoning": "完成", '
        '"suggestions": [], "results": [], "grouping": null}'
    )
    calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        calls.append(1)
        if len(calls) == 1:
            return (
                "",
                [{"id": "c1", "index": 0, "name": "list_files",
                  "arguments_str": "{}"}],
                "看下结构",
            )
        return (final_json, [], "")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)
    monkeypatch.setattr(
        agent2, "_execute_read_tool",
        lambda *a, **k: json.dumps({"files": ["a.py"]}),
    )

    result = agent2.run_agent2(
        "审计", [{"round": 1, "summary": "s"}],
        task_id="task-struct", db=None, client=MagicMock(),
        repo_path="/tmp/ws",
    )
    assert result["reasoning"] == "完成"
    assert len(calls) == 2


# ============================================================
# 6) verifier 循环:文本 tool_call 兜底(PoC 真实执行)
# ============================================================


def test_verifier_text_tool_call_fallback_runs_poc(monkeypatch):
    """GLM/Qwen 文本工具调用 → 兜底解析 → PoC 实际执行。

    回归锚点:兜底接入前,verifier 会把工具调用文本当"验证总结"直接
    返回,http_request 根本没发出。
    """
    task = MagicMock()
    task.id = "task-v"
    task.verifier_auth_mode = "direct"
    task.test_env_url = "http://test-env"
    task.verifier_auth_tokens = []

    stream_calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, iteration):
        stream_calls.append([dict(m) for m in messages])
        if len(stream_calls) == 1:
            content = (
                "构造 PoC 先发个请求确认\n"
                + _text_tool_call_block(
                    "http_request",
                    {"method": "GET", "path": "/api/users/1"},
                )
            )
            return ("构造 PoC 中", content, [], "stop")
        return ("验证收尾", "验证总结:SQL 注入已确认可利用", [], "stop")

    executed = []
    monkeypatch.setattr(verifier_agent, "_stream_verifier_llm", _fake_stream)
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda name, args, tid, base, tokens: executed.append((name, args))
        or {"status_code": 200, "body": "id=2"},
    )
    recorded = []
    monkeypatch.setattr(
        verifier_agent, "record_conversation",
        lambda db, task, *, role, type, content, **kw: recorded.append((type, content))
        or SimpleNamespace(id="conv-1"),
    )
    monkeypatch.setattr(verifier_agent, "_publish_verify_end", lambda *a, **k: None)
    monkeypatch.setattr(verifier_agent, "publish", lambda *a, **k: None)

    out = verifier_agent.run_verifier_agent(
        task, MagicMock(), "验证 /api/users 的 IDOR", client=MagicMock(), round_idx=1,
    )

    # PoC 真实执行(而非把工具调用文本当总结返回)
    assert executed == [
        ("http_request", {"method": "GET", "path": "/api/users/1"})
    ]
    assert out == "验证总结:SQL 注入已确认可利用"

    # 落库:tool_call 意图正确 + tool_result 带 tool_call_id 配对
    types = [t for t, _ in recorded]
    assert types == ["tool_call", "tool_result", "thinking"]
    assert "验证请求: GET /api/users/1 [http_request]" in recorded[0][1]
    assert "SQL 注入已确认可利用" in recorded[2][1]

    # 二次调用上下文:assistant content 已剥离工具调用文本
    assistant_msgs = [m for m in stream_calls[1] if m.get("role") == "assistant"]
    assert "/api/users/1" not in assistant_msgs[0]["content"]
    assert assistant_msgs[0]["tool_calls"][0]["function"]["name"] == "http_request"


# ============================================================
# 7) record_conversation:统一落库 + 推送 payload 形状
# ============================================================


def test_record_conversation_publishes_superset_payload(monkeypatch):
    """落库 + 推送:payload 含 id/reasoning/tool_call_id 超集形状 + extra。"""
    from app.agents.runtime import conversation as rt_conv

    class _FakeConv:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.id = "c-123"
            self.created_at = None

    monkeypatch.setattr(rt_conv, "Conversation", _FakeConv)
    events = []
    monkeypatch.setattr(rt_conv, "publish",
                        lambda tid, etype, data: events.append((tid, etype, data)))

    db = MagicMock()
    task = MagicMock()
    task.id = "t-9"
    rt_conv.record_conversation(
        db, task,
        round_idx=2, role="agent2", type="tool_result",
        content="结果", tool_call_id="c-122",
        extra_payload={"verify": True},
    )
    db.add.assert_called_once()
    db.commit.assert_called_once()
    db.refresh.assert_called_once()

    tid, etype, data = events[0]
    assert (tid, etype) == ("t-9", "conversation")
    assert data["id"] == "c-123"
    assert data["round_idx"] == 2
    assert data["role"] == "agent2"
    assert data["type"] == "tool_result"
    assert data["content"] == "结果"
    assert data["reasoning"] is None
    assert data["tool_call_id"] == "c-122"
    assert data["verify"] is True
    assert data["created_at"] is None


def test_record_conversation_publish_event_off(monkeypatch):
    """publish_event=False:只落库不推事件(thinking 不再用此模式,见 test_thinking_persistence)。"""
    from app.agents.runtime import conversation as rt_conv

    class _FakeConv:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            self.id = "c-124"
            self.created_at = None

    monkeypatch.setattr(rt_conv, "Conversation", _FakeConv)
    events = []
    monkeypatch.setattr(rt_conv, "publish",
                        lambda *a: events.append(a))

    db = MagicMock()
    task = MagicMock()
    rt_conv.record_conversation(
        db, task, round_idx=1, role="agent2", type="thinking",
        content="思考", publish_event=False,
    )
    db.add.assert_called_once()
    assert events == []
