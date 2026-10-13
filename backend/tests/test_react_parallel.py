"""内置 react_agent 分面并行单元测试(不触真实 LLM/DB)。

覆盖 Part B 的承重件:
- _facet_readonly_tools / _facet_allowed_tool_names:只读子集剔除改动工具
- _review_provider_gate:按 provider+model 归组的并发天花板
- _run_facet_pass:有界只读子循环 —— 只读工具执行、改动工具被拒、pending_rows
  形状、summary 捕获、单分面异常隔离(不抛)、墙钟超时转总结收尾(timed_out)、
  分面角色作为独立 system 指令层
- _facet_stream_turn:发布 facet-tagged thinking_delta + conv_id 每次调用唯一
- _fan_in:主线程串行落库 —— 分面顺序、tool_result 邻接补 tool_call_id、facet 标签、
  stream_conv_id 透传(实时卡退役)、超时分面小标题
- _parallel_gate:开关关闭 / 追问轮 / 分面数不足 → 回落串行(返回 None)
"""
import threading
import time
from types import SimpleNamespace

import app.agents.react_agent as R
from app.config import settings


# ============================================================
# 假流式件:chunk / tool_call delta / client
# ============================================================


def _chunk(reasoning=None, content=None, deltas=None, finish=None):
    return SimpleNamespace(
        reasoning_delta=reasoning, content_delta=content,
        tool_call_deltas=deltas or [], finish_reason=finish,
    )


def _delta(index, id_, name, args_fragment):
    return SimpleNamespace(
        index=index, id=id_, name=name, arguments_fragment=args_fragment,
    )


class FakeClient:
    """chat_stream 每次调用弹出脚本里的一段(= 一个 turn 的 chunk 序列)。"""

    provider_id = "fake"
    model = "m1"
    base_url_override = ""

    def __init__(self, turns):
        self._turns = list(turns)
        self._calls = 0

    def chat_stream(self, **kwargs):
        idx = self._calls
        self._calls += 1
        if idx >= len(self._turns):
            # 脚本耗尽:回一个空 stop turn,保证循环能收
            return iter([_chunk(finish="stop")])
        return iter(self._turns[idx])


class _Task:
    def __init__(self):
        from uuid import uuid4
        self.id = uuid4()
        self.scenario = "general"
        self.user_id = None
        self.params = {}


# ============================================================
# 只读工具过滤
# ============================================================


def test_facet_readonly_tools_excludes_mutating():
    tools = R.get_all_tools()
    ro = R._facet_readonly_tools(tools)
    names = R._facet_allowed_tool_names(ro)
    # 改动工具被剔除
    for bad in ("write_file", "run_command", "clone_repo", "str_replace_editor", "run_python_code"):
        assert bad not in names
    # 只读分析工具保留
    for good in ("read_file", "search_code", "list_files", "run_semgrep"):
        assert good in names


# ============================================================
# 厂商级并发闸门
# ============================================================


def test_provider_gate_limits_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "REACT_PROVIDER_MAX_CONCURRENCY", 2)
    cl = FakeClient([])  # 只用它的 provider/model 属性做 key
    state = {"live": 0, "max": 0}
    lock = threading.Lock()

    def worker():
        with R._review_provider_gate(cl):
            with lock:
                state["live"] += 1
                state["max"] = max(state["max"], state["live"])
            time.sleep(0.05)
            with lock:
                state["live"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] <= 2


# ============================================================
# _run_facet_pass
# ============================================================


def _facet(name="check_sql_injection", body="find sqli"):
    return SimpleNamespace(name=name, body=body)


def test_facet_pass_executes_readonly_and_captures_summary(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    executed = []

    def fake_execute(fn, args):
        executed.append((fn, args))
        return {"files": ["a.py"]}

    monkeypatch.setattr(R, "execute_tool", fake_execute)

    turns = [
        # turn1:正文 + 一个只读工具调用
        [
            _chunk(content="先看结构", finish=None),
            _chunk(deltas=[_delta(0, "c1", "list_files", '{"repo_path": "/x"}')], finish="tool_calls"),
        ],
        # turn2:结论,无工具
        [
            _chunk(reasoning="推断", content="结论:发现注入点 a.py:10", finish="stop"),
        ],
    ]
    client = FakeClient(turns)
    facet_tools = R._facet_readonly_tools(R.get_all_tools())
    allowed = R._facet_allowed_tool_names(facet_tools)
    base_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]

    fr = R._run_facet_pass(
        _facet(), base_messages, facet_tools, allowed, client, _Task(),
        round_idx=1, max_iter=5, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert fr.error == ""
    assert fr.facet == "check_sql_injection"
    assert "发现注入点" in fr.summary
    assert executed and executed[0][0] == "list_files"
    # pending_rows 顺序:thinking → tool_call → tool_result → thinking
    types = [r["type"] for r in fr.pending_rows]
    assert types == ["thinking", "tool_call", "tool_result", "thinking"]
    # 每条 thinking 行都带实时流式卡片的 stream_conv_id(fan-in 据此退役实时卡)
    assert all(r.get("stream_conv_id") for r in fr.pending_rows if r["type"] == "thinking")


def test_facet_pass_blocks_mutating_tool(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    executed = []
    monkeypatch.setattr(
        R, "execute_tool",
        lambda fn, args: executed.append(fn) or {"ok": 1},
    )

    turns = [
        [
            _chunk(deltas=[_delta(0, "w1", "write_file", '{"file_path": "x", "content": "y"}')], finish="tool_calls"),
        ],
        [_chunk(content="改用了只读方案收尾", finish="stop")],
    ]
    client = FakeClient(turns)
    facet_tools = R._facet_readonly_tools(R.get_all_tools())
    allowed = R._facet_allowed_tool_names(facet_tools)

    fr = R._run_facet_pass(
        _facet("review_concurrency"), [{"role": "system", "content": "s"}],
        facet_tools, allowed, client, _Task(),
        round_idx=1, max_iter=5, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert executed == []  # 改动工具从未执行
    tool_results = [r for r in fr.pending_rows if r["type"] == "tool_result"]
    assert tool_results and "禁用" in tool_results[0]["content"]


def test_facet_pass_isolates_llm_error(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)

    class BoomClient(FakeClient):
        def chat_stream(self, **kwargs):
            raise RuntimeError("provider 500")

    facet_tools = R._facet_readonly_tools(R.get_all_tools())
    allowed = R._facet_allowed_tool_names(facet_tools)
    fr = R._run_facet_pass(
        _facet(), [{"role": "system", "content": "s"}], facet_tools, allowed,
        BoomClient([]), _Task(), round_idx=1, max_iter=3, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )
    # 异常被吞进 result.error,不抛出(单分面失败不拖垮整轮)
    assert "provider 500" in fr.error


# ============================================================
# _fan_in
# ============================================================


def test_fan_in_order_adjacency_and_facet_tag(monkeypatch):
    recorded = []

    class _Conv:
        _n = 0

        def __init__(self):
            _Conv._n += 1
            self.id = f"conv-{_Conv._n}"

    def fake_add(db, task, *, round_idx, role, type, content, reasoning=None,
                 tool_call_id=None, extra_payload=None, stream_conv_id=None):
        c = _Conv()
        recorded.append({
            "type": type, "role": role, "tool_call_id": tool_call_id,
            "facet": (extra_payload or {}).get("facet"),
            "stream_conv_id": stream_conv_id,
            "id": c.id,
        })
        return c

    monkeypatch.setattr(R, "_add_conversation", fake_add)

    fr_a = R._FacetResult(
        facet="A", summary="A结论",
        pending_rows=[
            {"role": "agent1", "type": "thinking", "content": "想A", "reasoning": "r"},
            {"role": "agent1", "type": "tool_call", "content": "call"},
            {"role": "agent1", "type": "tool_result", "content": "res"},
        ],
    )
    fr_b = R._FacetResult(
        facet="B", summary="B结论",
        pending_rows=[{"role": "agent1", "type": "thinking", "content": "想B", "reasoning": ""}],
    )

    summary = R._fan_in(_Task(), object(), 1, [fr_a, fr_b])

    facets = [r["facet"] for r in recorded]
    assert facets == ["A", "A", "A", "B"]  # 分面顺序确定
    # tool_result 的 tool_call_id 指向前一条 tool_call 落库 id
    call_row = recorded[1]
    res_row = recorded[2]
    assert call_row["type"] == "tool_call"
    assert res_row["type"] == "tool_result"
    assert res_row["tool_call_id"] == call_row["id"]
    # thinking 不带 tool_call_id
    assert recorded[0]["tool_call_id"] is None
    assert "A结论" in summary and "B结论" in summary


def test_fan_in_reports_facet_error(monkeypatch):
    monkeypatch.setattr(
        R, "_add_conversation",
        lambda *a, **k: SimpleNamespace(id="x"),
    )
    fr = R._FacetResult(facet="C", error="provider 500")
    summary = R._fan_in(_Task(), object(), 1, [fr])
    assert "C" in summary and "provider 500" in summary


# ============================================================
# _parallel_gate:门控回退
# ============================================================


def test_gate_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "REACT_PARALLEL_ENABLED", False)
    monkeypatch.setattr(R, "_resolve_facets", lambda task: [_facet()] * 5)
    assert R._parallel_gate(_Task(), 1) is None


def test_gate_followup_round_returns_none(monkeypatch):
    # 追问轮(round_idx>1)回退串行:不重新全量扇出(默认开启前的遗留守卫)
    monkeypatch.setattr(settings, "REACT_PARALLEL_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_PARALLEL_MIN_FACETS", 3)
    monkeypatch.setattr(R, "_resolve_facets", lambda task: [_facet()] * 5)
    assert R._parallel_gate(_Task(), 2) is None


def test_gate_too_few_facets_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "REACT_PARALLEL_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_PARALLEL_MIN_FACETS", 3)
    monkeypatch.setattr(R, "_resolve_facets", lambda task: [_facet()] * 2)
    assert R._parallel_gate(_Task(), 1) is None


def test_gate_enough_facets_returns_list(monkeypatch):
    monkeypatch.setattr(settings, "REACT_PARALLEL_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_PARALLEL_MIN_FACETS", 3)
    facets = [_facet(name=f"f{i}") for i in range(3)]
    monkeypatch.setattr(R, "_resolve_facets", lambda task: facets)
    out = R._parallel_gate(_Task(), 1)
    assert out is not None and len(out) == 3


# ============================================================
# 分面超时:墙钟预算到期转总结收尾(部分结论)且不拖垮整轮
# ============================================================


def test_facet_pass_timeout_isolates_and_summarizes(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(R, "execute_tool", lambda fn, args: {})
    # 唯一一段脚本给超时后的收尾轮(_finalize_turn 用独立宽限 deadline)
    turns = [[_chunk(content="超时前抓到的部分结论", finish="stop")]]
    client = FakeClient(turns)
    facet_tools = R._facet_readonly_tools(R.get_all_tools())
    allowed = R._facet_allowed_tool_names(facet_tools)

    fr = R._run_facet_pass(
        _facet(), [{"role": "system", "content": "s"}], facet_tools, allowed,
        client, _Task(), round_idx=1, max_iter=5,
        executor_command_confirm="always_approve",
        deadline=time.monotonic() - 1,  # 已过期:循环首边界即命中
    )

    assert fr.timed_out is True
    assert fr.error == ""
    assert fr.summary == "超时前抓到的部分结论"
    # pending_rows 含超时提示行 + 总结行(总结行带 stream_conv_id)
    assert any("时间预算" in (r.get("content") or "") for r in fr.pending_rows)
    assert fr.pending_rows[-1].get("stream_conv_id")


# ============================================================
# 分面独立指令层:角色约束作为 system 指令而非 user 消息
# ============================================================


def test_facet_pass_injects_system_facet_instruction(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    captured: list[list[dict]] = []

    def fake_turn(client, messages, tools, **kw):
        captured.append(list(messages))
        return ("", "本分面结论", [], "stop", "cid-1")

    monkeypatch.setattr(R, "_facet_stream_turn", fake_turn)
    facet_tools = R._facet_readonly_tools(R.get_all_tools())
    allowed = R._facet_allowed_tool_names(facet_tools)

    fr = R._run_facet_pass(
        _facet(body="find sqli"),
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}],
        facet_tools, allowed, object(), _Task(), round_idx=1, max_iter=5,
        executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert fr.summary == "本分面结论"
    sys_msgs = [m for m in captured[0] if m["role"] == "system"]
    # 分面指令作为独立 system 层下发,不污染真实 user 问题
    assert any(
        "审查分面" in m["content"] and "check_sql_injection" in m["content"]
        for m in sys_msgs
    )
    assert not any(
        "审查分面" in m["content"] for m in captured[0] if m["role"] == "user"
    )


# ============================================================
# _facet_stream_turn:发布 facet-tagged thinking_delta + conv_id 唯一
# ============================================================


def test_facet_stream_turn_publishes_facet_tagged_delta(monkeypatch):
    import app.agents.runtime.llm_stream as LS

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        LS, "publish",
        lambda task_id, etype, data: events.append((etype, data)),
    )
    client = FakeClient([[_chunk(reasoning="想", content="答", finish="stop")]])

    r, c, tool_calls, finish, cid = R._facet_stream_turn(
        client, [{"role": "system", "content": "s"}], None,
        task=_Task(), round_idx=1, facet="check_sql_injection",
        iteration=2, deadline=time.monotonic() + 60,
    )

    assert r == "想" and c == "答" and finish == "stop" and cid
    deltas = [d for e, d in events if e == "thinking_delta"]
    assert deltas
    assert all(d.get("facet") == "check_sql_injection" for d in deltas)
    assert all(d.get("parallel") is True for d in deltas)
    assert any(d.get("phase") == "start" for d in deltas)
    assert any(d.get("phase") == "end" for d in deltas)


def test_facet_stream_turn_conv_id_unique_per_call(monkeypatch):
    monkeypatch.setattr("app.agents.runtime.llm_stream.publish", lambda *a, **k: None)
    client = FakeClient([
        [_chunk(content="a", finish="stop")],
        [_chunk(content="b", finish="stop")],
    ])
    task = _Task()
    _, _, _, _, cid1 = R._facet_stream_turn(
        client, [{"role": "system", "content": "s"}], None,
        task=task, round_idx=1, facet="f", iteration=1,
        deadline=time.monotonic() + 60,
    )
    _, _, _, _, cid2 = R._facet_stream_turn(
        client, [{"role": "system", "content": "s"}], None,
        task=task, round_idx=1, facet="f", iteration=2,
        deadline=time.monotonic() + 60,
    )
    # 每分面每次调用唯一 conv_id,前端据此天然分桶不串扰
    assert cid1 != cid2


# ============================================================
# fan-in:stream_conv_id 透传 + 超时小标题
# ============================================================


def test_fan_in_forwards_stream_conv_id(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(
        R, "_add_conversation",
        lambda db, task, **kw: recorded.append(kw) or SimpleNamespace(id="x"),
    )
    fr = R._FacetResult(
        facet="A", summary="A结论",
        pending_rows=[{
            "role": "agent1", "type": "thinking",
            "content": "想A", "reasoning": "r", "stream_conv_id": "sc-1",
        }],
    )
    R._fan_in(_Task(), object(), 1, [fr])
    assert recorded[0]["stream_conv_id"] == "sc-1"
    assert recorded[0]["extra_payload"] == {"facet": "A", "parallel": True}


def test_fan_in_labels_timed_out_facet(monkeypatch):
    monkeypatch.setattr(
        R, "_add_conversation", lambda *a, **k: SimpleNamespace(id="x"),
    )
    fr = R._FacetResult(facet="T", summary="部分结论", timed_out=True)
    summary = R._fan_in(_Task(), object(), 1, [fr])
    assert "部分结论,已超时收尾" in summary
