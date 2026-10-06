"""思考链"落库即推事件"的回归测试(离开详情页再回来不再丢思考)

背景缺陷:思考过程有两条展示载体——实时流式卡片(SSE thinking_delta 增量,
只活在前端内存)与落库记录(Conversation type=thinking)。后端过去对落库记录
统一 `publish_event=False`,理由是"卡片已展示,推了重复";但 thinking_delta
是高频瞬时事件,事件总线不缓存(见 event_bus.publish),于是:

- 用户离开任务详情页再回来:组件重挂载 → 卡片清空;回来那一刻仍在生成的那次
  调用,增量永远补不回来,而它的落库记录又不推事件 → 这段思考在本次会话里
  彻底看不见(要等下一次整页快照),表现即"思考内容没保存";
- agent2 审查把整轮思考攒到审查结束才写一条:parse_failed / 降级 / 被新一轮
  接管等提前 return 路径根本走不到写库 → 思考真的丢了。

本文件锁死修复后的契约:
1. runtime.record_conversation 支持 stream_conv_id,并随 conversation 事件推出;
2. react_agent / ACP 执行器的 thinking 落库推事件,且带上本次流式 conv_id
   (前端据此退役实时卡片,不会双份);
3. agent2 思考链逐次流式调用落库,审查解析失败也保留已写下的部分;
4. verifier 的验证思考同样推事件。

另覆盖 `_stream_llm_response` 的返回值顺序(曾被调用方按
(..., finish_reason, conv_id) 解包而包装层返回 (..., conv_id, finish_reason),
导致 finish_reason 实际拿到 UUID、max_tokens 截断分支永不命中)。
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  注册 TaskArtifact,Task mapper 才能初始化
from app.agents.runtime import conversation as rt_conv
from app.agents.runtime.llm_stream import StreamResult
from app.models.task import Conversation


# ============================================================
# 通用替身
# ============================================================


class _FakeConv:
    """替掉 ORM 对象:只需可读回字段做断言,不触发真实建表/写入"""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id = "conv-fake-1"
        self.created_at = None


def _capture_publish(monkeypatch):
    """截住 runtime 的事件推送,返回 collected 列表 [(event_type, data)]"""
    collected = []
    monkeypatch.setattr(
        rt_conv, "Conversation", _FakeConv,
    )
    monkeypatch.setattr(
        rt_conv, "publish",
        lambda tid, etype, data: collected.append((tid, etype, data)),
    )
    return collected


# ============================================================
# 1) runtime 原语:stream_conv_id 随事件推出
# ============================================================


def test_record_conversation_publishes_stream_conv_id(monkeypatch):
    """thinking 落库默认推 conversation 事件,并把 stream_conv_id 带给前端"""
    collected = _capture_publish(monkeypatch)

    task = MagicMock()
    task.id = "t-stream"
    rt_conv.record_conversation(
        MagicMock(), task,
        round_idx=1, role="agent1", type="thinking",
        content="正文", reasoning="思考链",
        stream_conv_id="stream-42",
    )

    assert len(collected) == 1
    tid, etype, data = collected[0]
    assert (tid, etype) == ("t-stream", "conversation")
    assert data["type"] == "thinking"
    assert data["reasoning"] == "思考链"
    assert data["stream_conv_id"] == "stream-42"


def test_record_conversation_stream_conv_id_defaults_null(monkeypatch):
    """非 thinking 记录不带 stream_conv_id 也不报错(字段恒在,payload 形状统一)"""
    collected = _capture_publish(monkeypatch)

    task = MagicMock()
    task.id = "t-null"
    rt_conv.record_conversation(
        MagicMock(), task,
        round_idx=1, role="agent1", type="tool_call", content="读取文件",
    )

    assert collected[0][2]["stream_conv_id"] is None


# ============================================================
# 2) 内置 react_agent:落库推事件 + 返回值顺序
# ============================================================


def test_stream_llm_response_unpack_order_matches_contract(monkeypatch):
    """包装层返回 (reasoning, content, tool_calls, finish_reason, conv_id)

    回归点:调用方按此顺序解包 finish_reason,顺序一旦倒回
    (conv_id, finish_reason) 会让 finish_reason 拿到 UUID,
    `finish_reason == "length"` 的截断分支永不命中。
    """
    import app.agents.react_agent as react_agent

    monkeypatch.setattr(
        react_agent, "stream_llm",
        lambda *a, **kw: StreamResult(
            conv_id="conv-7", reasoning="思", content="答",
            tool_calls=[], finish_reason="length",
        ),
    )
    reasoning, content, tool_calls, finish_reason, conv_id = (
        react_agent._stream_llm_response(
            MagicMock(), MagicMock(id="t-1"), MagicMock(),
            round_idx=1, iteration=1, messages=[], tools=None,
        )
    )
    assert (reasoning, content, tool_calls) == ("思", "答", [])
    assert finish_reason == "length"
    assert conv_id == "conv-7"


def test_react_agent_thinking_published_with_stream_conv_id(monkeypatch):
    """每迭代结束的 thinking 落库必须推事件,并带上这次流式调用的 conv_id"""
    import app.agents.react_agent as react_agent

    task = MagicMock()
    task.id = "task-react"
    task.params = {}
    task.user_id = None
    task.scenario = "general"
    task.user_input = "初始任务"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []

    records = []
    monkeypatch.setattr(
        react_agent, "_add_conversation",
        lambda _db, _task, **kw: records.append(kw) or SimpleNamespace(id="c"),
    )
    monkeypatch.setattr(react_agent, "_stream_llm_response", lambda *a, **kw: (
        "先读目录结构再定位入口", "答案正文", [], "stop", "stream-99",
    ))
    monkeypatch.setattr(react_agent, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "get_all_tools", lambda: [])
    monkeypatch.setattr(react_agent, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "publish", lambda *a, **k: None)

    react_agent.run_react_agent(
        task, db, round_idx=1, followup_query=None,
        client=MagicMock(), repo_context=None, previous_plan=None,
    )

    thinkings = [r for r in records if r.get("type") == "thinking"]
    assert thinkings, f"未落库 thinking:{records}"
    rec = thinkings[0]
    assert rec["role"] == "agent1"
    # 关键:不再 publish_event=False,且带 stream_conv_id 供前端退役实时卡片
    assert rec.get("publish_event") is not False
    assert rec["stream_conv_id"] == "stream-99"


# ============================================================
# 3) ACP(外部 CLI 执行器):迭代 flush 推事件
# ============================================================


def _acp_notice(update: dict) -> dict:
    return {"method": "session/update", "params": {"sessionId": "s1", "update": update}}


def test_acp_collector_flush_publishes_thinking_with_stream_conv_id(monkeypatch):
    """thought 段在 tool_call 处 flush:落库推事件并携带该迭代的流式 conv_id"""
    import app.agents.acp_base as acp_base

    records = []
    monkeypatch.setattr(
        acp_base, "_add_conversation",
        lambda _db, _task, **kw: records.append(kw) or SimpleNamespace(id="c"),
    )
    monkeypatch.setattr(acp_base, "publish", lambda *a, **k: None)
    monkeypatch.setattr(acp_base, "perf_log", lambda *a, **k: None)

    collector = acp_base._ACPCollector(
        SimpleNamespace(id="task-acp"), MagicMock(), 1,
    )
    collector(_acp_notice({
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": "先看仓库结构。"},
    }))
    stream_conv_id = collector.current_conv_id
    collector(_acp_notice({
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-1",
        "title": "列出文件 [list_files]",
        "content": [],
    }))

    thinkings = [r for r in records if r.get("type") == "thinking"]
    assert thinkings, f"迭代 flush 未落库 thinking:{records}"
    assert thinkings[0]["reasoning"] == "先看仓库结构。"
    assert thinkings[0].get("publish_event") is not False
    assert thinkings[0]["stream_conv_id"] == stream_conv_id


# ============================================================
# 4) agent2:思考链逐次落库,解析失败也保留
# ============================================================


def _agent2_env(monkeypatch, stream_returns):
    """跑一次 run_agent2:LLM 流式与只读工具均打替身,返回落库的 Conversation 列表"""
    import app.agents.agent2 as agent2

    queue = list(stream_returns)

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        return queue.pop(0)

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)
    monkeypatch.setattr(
        agent2, "_execute_read_tool",
        lambda *a, **k: json.dumps({"hits": ["a.py:1"]}),
    )

    added = []
    real_add = MagicMock(side_effect=lambda obj: added.append(obj))
    db = MagicMock()
    db.add = real_add
    published = []
    monkeypatch.setattr(
        rt_conv, "publish",
        lambda tid, etype, data: published.append((etype, data)),
    )

    task = MagicMock()
    task.id = "task-agent2"
    task.verifier_enabled = False
    task.test_env_url = ""

    result = agent2.run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "agent1 总结"}],
        task_id="task-agent2", db=db, round_idx=1,
        client=MagicMock(), task=task, repo_path="/home/user/repo",
    )
    return result, added, published


def test_agent2_persists_thinking_per_stream_call(monkeypatch):
    """两次流式调用 → 两条 thinking(不再攒到审查结束写一条)"""
    calls = [
        ("", [{"id": "t1", "name": "search_code",
               "arguments_str": '{"query": "subprocess"}', "index": 0}],
         "第一段思考:先搜危险调用点。"),
        ('这不是合法 JSON,审查输出坏了', [], "第二段思考:结论整理。"),
    ]
    result, added, published = _agent2_env(monkeypatch, calls)

    thinkings = [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]
    assert len(thinkings) == 2
    assert [t.reasoning for t in thinkings] == [
        "第一段思考:先搜危险调用点。", "第二段思考:结论整理。",
    ]
    assert all(t.role == "agent2" and t.content == "" for t in thinkings)

    # 逐条推事件(前端据此退役侧栏的实时卡片)
    conv_events = [d for etype, d in published if etype == "conversation"]
    assert len([d for d in conv_events if d["type"] == "thinking"]) == 2


def test_agent2_thinking_survives_parse_failure(monkeypatch):
    """审查输出解析失败(提前 return)时,已生成的思考链仍在库里(旧实现整段丢失)"""
    calls = [
        ("", [{"id": "t1", "name": "read_file",
               "arguments_str": '{"path": "a.py"}', "index": 0}],
         "核查 a.py 的鉴权分支,发现 agent1 结论可靠。"),
        ("{解析不了的半截 JSON", [], ""),
    ]
    result, added, _published = _agent2_env(monkeypatch, calls)

    assert result.get("parse_failed") is True
    thinkings = [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]
    assert [t.reasoning for t in thinkings] == [
        "核查 a.py 的鉴权分支,发现 agent1 结论可靠。",
    ]


def test_agent2_empty_reasoning_not_persisted(monkeypatch):
    """非思考型模型(reasoning 为空)→ 不落空 thinking 记录"""
    calls = [('{"covered": [], "missing": [], "reasoning": "ok", '
             '"suggestions": [], "results": [], "grouping": null}', [], "   ")]
    _result, added, _published = _agent2_env(monkeypatch, calls)

    assert not [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]


# ============================================================
# 5) verifier:验证思考落库同样推事件
# ============================================================


def test_verifier_thinking_published(monkeypatch):
    """verifier 的 thinking 不再 publish_event=False(中途回来要能补上这段验证思考)"""
    import app.agents.verifier_agent as verifier_agent

    recorded = {}

    def _fake_record(db, task, **kw):
        recorded.update(kw)
        return SimpleNamespace(id="conv-v")

    monkeypatch.setattr(verifier_agent, "record_conversation", _fake_record)
    verifier_agent._record_verifier_thinking(
        MagicMock(), MagicMock(), 1, "SQL 注入已确认可利用", "构造 PoC 的过程",
    )

    assert recorded["type"] == "thinking"
    assert recorded["role"] == "agent2"
    assert recorded.get("publish_event") is not False
    assert recorded["content"].startswith("[验证结果]")
