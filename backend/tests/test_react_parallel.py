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
- _facet_dispatch_names:开关关闭 / 可见技能数不足 → 不注册派发工具(返回空列表)
- _build_facet_dispatch_tool:分面名动态注入 facets 参数说明(模型只能从中选)
- _dispatch_facets:按需扇出校验(空/未知/超分面上限 → ValueError)、去重保序、
  focus 透传各分面子循环、fan-in 落库
- 主循环 dispatch_review_facets 特判:达标注册工具、派发结果作为 tool_result 回灌、
  超调用额度直接报错不扇出
"""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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
# 消息序列形状(回归 P0:子循环第 2 迭代 400)
# ============================================================


def _assert_tool_msgs_preceded_by_tool_calls(snapshots):
    """断每条 {role:tool} 消息前最近的 assistant 带 tool_calls 且 id 匹配。

    镜像 OpenAI provider 契约:'tool must be a response to a preceding message
    with tool_calls'——旧写法把 assistant 追加成纯 content,下一迭代必 400。
    """
    for seq in snapshots:
        for i, m in enumerate(seq):
            if m.get("role") != "tool":
                continue
            prev = next(
                (seq[j] for j in range(i - 1, -1, -1)
                 if seq[j].get("role") == "assistant"),
                None,
            )
            assert prev is not None, "tool 消息前无 assistant"
            assert prev.get("tool_calls"), (
                "tool 消息前 assistant 缺 tool_calls → 下一迭代必被 provider 拒 400"
            )
            assert m["tool_call_id"] in {tc["id"] for tc in prev["tool_calls"]}


def test_facet_pass_assistant_carries_tool_calls(monkeypatch):
    """回归 P0-1:捕获真实发给 chat_stream 的 messages 序列,断带工具调用的
    子循环进入下一迭代时 assistant 已带 tool_calls(而非纯 content)。"""
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(R, "execute_tool", lambda fn, args: {"ok": 1})

    turns = [
        [
            _chunk(content="先看结构", finish=None),
            _chunk(deltas=[_delta(0, "c1", "list_files", '{"repo_path": "/x"}')], finish="tool_calls"),
        ],
        [_chunk(content="结论:无问题", finish="stop")],
    ]

    class CapturingClient(FakeClient):
        def __init__(self, turns):
            super().__init__(turns)
            self.snapshots = []

        def chat_stream(self, **kwargs):
            self.snapshots.append([dict(m) for m in kwargs.get("messages", [])])
            return super().chat_stream(**kwargs)

    client = CapturingClient(turns)
    facet_tools = R._facet_readonly_tools(R.get_all_tools())
    allowed = R._facet_allowed_tool_names(facet_tools)
    fr = R._run_facet_pass(
        _facet(),
        [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
        facet_tools, allowed, client, _Task(),
        round_idx=1, max_iter=5, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert fr.error == ""
    # 至少两发:第 2 发的 messages 已含首轮的 tool 交换(即回归触发点)
    assert len(client.snapshots) >= 2
    assert any(
        m.get("role") == "tool" for seq in client.snapshots for m in seq
    ), "脚本未产生 tool 消息,断言形同虚设"
    _assert_tool_msgs_preceded_by_tool_calls(client.snapshots)


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
                 tool_call_id=None, extra_payload=None, stream_conv_id=None,
                 facet=None):
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
# _facet_dispatch_names:是否注册派发工具
# ============================================================


def test_dispatch_names_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_ENABLED", False)
    monkeypatch.setattr(R, "_resolve_facets", lambda task: [_facet()] * 5)
    assert R._facet_dispatch_names(_Task()) == []


def test_dispatch_names_too_few_facets_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MIN_FACETS", 3)
    monkeypatch.setattr(R, "_resolve_facets", lambda task: [_facet()] * 2)
    assert R._facet_dispatch_names(_Task()) == []


def test_dispatch_names_min_facets_floored_at_2(monkeypatch):
    # 阈值下界固定 2:即便配置写 1,单技能也不注册(扇出无并行收益)
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MIN_FACETS", 1)
    monkeypatch.setattr(R, "_resolve_facets", lambda task: [_facet(name="only")])
    assert R._facet_dispatch_names(_Task()) == []


def test_dispatch_names_enough_facets_returns_names(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MIN_FACETS", 3)
    facets = [_facet(name=f"f{i}") for i in range(3)]
    monkeypatch.setattr(R, "_resolve_facets", lambda task: facets)
    assert R._facet_dispatch_names(_Task()) == ["f0", "f1", "f2"]


# ============================================================
# _build_facet_dispatch_tool:分面名动态注入描述
# ============================================================


def test_build_facet_dispatch_tool_injects_names():
    tool = R._build_facet_dispatch_tool(["check_ssrf", "review_concurrency"])
    fn = tool["function"]
    assert fn["name"] == "dispatch_review_facets"
    assert "check_ssrf" in fn["description"] and "review_concurrency" in fn["description"]
    props = fn["parameters"]["properties"]
    assert "facets" in props and "focus" in props
    assert fn["parameters"]["required"] == ["facets"]
    # facets 取值说明列出可用分面名(模型只能从中选)
    assert "check_ssrf" in props["facets"]["description"]


# ============================================================
# _dispatch_facets:按需扇出校验与 focus 透传
# ============================================================


def _stub_dispatch_common(monkeypatch, names):
    monkeypatch.setattr(R, "_resolve_facets", lambda t: [
        SimpleNamespace(name=n, body="b") for n in names
    ])
    monkeypatch.setattr(
        R, "_add_conversation", lambda *a, **k: SimpleNamespace(id="c-x")
    )
    monkeypatch.setattr(R, "wait_if_paused", lambda *a, **k: None)


def test_dispatch_facets_rejects_empty():
    with pytest.raises(ValueError, match="不能为空"):
        R._dispatch_facets(
            _Task(), object(), 1, object(), [], [], "always_approve",
            [], "", ["a", "b"],
        )


def test_dispatch_facets_rejects_unknown(monkeypatch):
    _stub_dispatch_common(monkeypatch, ["a"])
    with pytest.raises(ValueError, match="未知分面"):
        R._dispatch_facets(
            _Task(), object(), 1, object(), [], [], "always_approve",
            ["z"], "", ["a"],
        )


def test_dispatch_facets_rejects_over_max(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 1)
    _stub_dispatch_common(monkeypatch, ["a", "b"])
    with pytest.raises(ValueError, match="超过上限"):
        R._dispatch_facets(
            _Task(), object(), 1, object(), [], [], "always_approve",
            ["a", "b"], "", ["a", "b"],
        )


def test_dispatch_facets_passes_focus_to_every_facet(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 0)
    _stub_dispatch_common(monkeypatch, ["a", "b"])
    captured: dict[str, str] = {}

    def fake_pass(facet, *pos, **kw):
        # focus 是 pool.submit 调用的最后一个位置实参
        captured[facet.name] = pos[-1]
        return R._FacetResult(facet=facet.name, summary=f"{facet.name}结论")

    monkeypatch.setattr(R, "_run_facet_pass", fake_pass)
    results, fan_in = R._dispatch_facets(
        _Task(), object(), 1, object(), R.get_all_tools(),
        [{"role": "system", "content": "s"}], "always_approve",
        ["a", "b"], "关注鉴权与注入", ["a", "b"],
    )
    assert captured == {"a": "关注鉴权与注入", "b": "关注鉴权与注入"}
    assert {r.facet for r in results} == {"a", "b"}
    assert "a结论" in fan_in and "b结论" in fan_in


def test_dispatch_facets_dedups_preserving_request_order(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 0)
    _stub_dispatch_common(monkeypatch, ["a", "b", "c"])
    monkeypatch.setattr(
        R, "_run_facet_pass",
        lambda facet, *p, **k: R._FacetResult(facet=facet.name, summary="x"),
    )
    results, _ = R._dispatch_facets(
        _Task(), object(), 1, object(), R.get_all_tools(), [], "always_approve",
        ["c", "a", "c", "a"], "", ["a", "b", "c"],
    )
    assert [r.facet for r in results] == ["c", "a"]  # 去重且保持请求顺序


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


# ============================================================
# 主循环按需派发:模型调 dispatch_review_facets → 扇出 → tool_result 回灌
# ============================================================


class _RoundTask:
    """run_react_agent 需要的最小 task 替身(与 _Task 区分:后者只测组件)"""

    def __init__(self):
        from uuid import uuid4
        self.id = uuid4()
        self.scenario = "general"
        self.user_id = None
        self.params = {}
        self.user_input = "审一下这个仓库"
        self.allowed_skills = None


def _mk_dispatch_round(monkeypatch, *, results, names=None, max_calls=1, stream=None):
    """驱动一整回合:第 1 迭代模型派发 dispatch_review_facets,第 2 迭代 stop 总结。

    返回 (task, db, seen);seen 逐次记录主循环 _stream_llm_response 收到的
    (iteration, messages, tool_names)。分面子循环用 stub(只产 _FacetResult),
    fan-in 落库与主循环回灌是真代码 —— 要测的正是那条接入点。
    """
    task = _RoundTask()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []

    names = names if names is not None else [fr.facet for fr in results]
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_ENABLED", True)
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_CALLS", max_calls)
    # 达标判定直接给出可派发分面名(注册逻辑走真 _build_facet_dispatch_tool)
    monkeypatch.setattr(R, "_facet_dispatch_names", lambda t: names)
    monkeypatch.setattr(
        R, "_resolve_facets",
        lambda t: [SimpleNamespace(name=fr.facet, body="b") for fr in results],
    )
    by_name = {fr.facet: fr for fr in results}
    monkeypatch.setattr(
        R, "_run_facet_pass", lambda facet, *a, **k: by_name[facet.name]
    )
    monkeypatch.setattr(
        R, "_add_conversation", lambda *a, **k: SimpleNamespace(id="c-x")
    )
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(R, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(R, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(R, "publish", lambda *a, **k: None)
    monkeypatch.setattr(R, "drain_user_messages", lambda *a, **k: [])
    monkeypatch.setattr(R, "has_pending_messages", lambda *a, **k: False)

    seen: list[dict] = []

    def _default_stream(client, t, db_, round_idx, iteration, messages, tools):
        if iteration == 1:
            call = [{
                "id": "tc-dispatch",
                "name": "dispatch_review_facets",
                "arguments_str": json.dumps({"facets": names, "focus": "关注鉴权"}),
            }]
            return ("派发分面", "", call, "tool_calls", "conv-1")
        return ("", "集成完毕:综合各面结论", [], "stop", f"conv-{iteration}")

    def fake_stream(client, t, db_, round_idx, iteration, messages, tools):
        seen.append({
            "iteration": iteration,
            "messages": list(messages),
            "tool_names": [(x.get("function") or {}).get("name") for x in (tools or [])],
        })
        return (stream or _default_stream)(
            client, t, db_, round_idx, iteration, messages, tools
        )

    monkeypatch.setattr(R, "_stream_llm_response", fake_stream)
    return task, db, seen


def _run_round(task, db, round_idx=1, followup_query=None):
    return R.run_react_agent(
        task, db, round_idx=round_idx, followup_query=followup_query,
        client=MagicMock(), repo_context=None, previous_plan=None,
    )


def _tool_messages(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("role") == "tool"]


def test_dispatch_tool_registered_when_facets_available(monkeypatch):
    results = [R._FacetResult(facet=f"f{i}", summary=f"f{i}结论") for i in (1, 2)]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results)

    _run_round(task, db)

    # 达标即注册:第 1 迭代工具列表里就有 dispatch_review_facets
    assert "dispatch_review_facets" in seen[0]["tool_names"]


def test_dispatch_tool_absent_when_below_threshold(monkeypatch):
    # _facet_dispatch_names 返回空(开关关/技能数不足) → 主循环不注册派发工具
    results = [R._FacetResult(facet="f1", summary="x")]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results, names=[])

    _run_round(task, db)

    assert "dispatch_review_facets" not in seen[0]["tool_names"]


def test_dispatch_result_returned_as_tool_result(monkeypatch):
    results = [
        R._FacetResult(facet="f1", summary="f1结论"),
        R._FacetResult(facet="f2", error="provider 500"),
    ]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results)

    _res, summary, _plan = _run_round(task, db)

    # 派发回灌后主循环继续跑第 2 迭代做集成
    assert seen[0]["iteration"] == 1
    assert len(seen) >= 2
    tools_msgs = _tool_messages(seen[1]["messages"])
    assert tools_msgs, "派发结论必须以 role=tool 消息回灌主循环"
    payload = json.loads(tools_msgs[-1]["content"])
    status = {f["facet"]: f["status"] for f in payload["facets"]}
    assert status == {"f1": "ok", "f2": "error"}  # error 措辞保留在 tool_result
    assert any(
        f["facet"] == "f1" and "f1结论" in f["summary"] for f in payload["facets"]
    )
    assert summary == "集成完毕:综合各面结论"  # 主循环自然总结


def test_dispatch_over_call_cap_errors_without_fanout(monkeypatch):
    results = [R._FacetResult(facet=f"f{i}", summary="x") for i in (1, 2)]
    task, db, seen = _mk_dispatch_round(
        monkeypatch, results=results, max_calls=0,
    )

    _run_round(task, db)

    # 额度为 0:首次派发即被守卫拦下,tool_result 是上限提示而非分面结论
    tools_msgs = _tool_messages(seen[1]["messages"])
    assert tools_msgs and "上限" in tools_msgs[-1]["content"]


def test_dispatch_available_on_followup_round(monkeypatch):
    # 新模型不再按 round_idx 屏蔽派发:_facet_dispatch_names 无轮次参数,
    # 追问轮同样注册(与 _parallel_gate 时代"round_idx>1 回落串行"的关键差异)
    results = [R._FacetResult(facet=f"f{i}", summary=f"f{i}结论") for i in (1, 2)]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results)

    _run_round(task, db, round_idx=2, followup_query="继续核查鉴权")

    assert "dispatch_review_facets" in seen[0]["tool_names"]


def test_facet_result_items_maps_status_and_clips(monkeypatch):
    """状态映射与 _fan_in 小标题同源;结论按单面 token 上限截断"""
    monkeypatch.setattr(settings, "REACT_FACET_SUMMARY_MAX_TOKENS", 50)
    items = R._facet_result_items([
        R._FacetResult(facet="E", error="provider 500"),
        R._FacetResult(facet="T", summary="部分结论", timed_out=True),
        R._FacetResult(facet="L", summary="漏" * 400),
        R._FacetResult(facet="S", summary="短结论"),
    ])

    assert [(n, st) for n, st, _ in items] == [
        ("E", "error"), ("T", "timed_out"), ("L", "ok"), ("S", "ok"),
    ]
    assert items[0][2] == "provider 500"          # 错误短注不裁
    assert items[3][2] == "短结论"                  # 未超限原样
    long_body = items[2][2]
    assert len(long_body) < 400 and "结论过长已截断" in long_body
    assert "原文 400 字符" in long_body


def test_clip_to_tokens_passthrough_and_truncate():
    assert R._clip_to_tokens("", 10) == ""
    assert R._clip_to_tokens("abc", 10) == "abc"
    assert R._clip_to_tokens("短", 0) == "短"  # max_tokens<=0 视为不限制
    text = "洞" * 4000
    out = R._clip_to_tokens(text, 3000)
    assert out.startswith("洞") and "结论过长已截断" in out
    assert R._estimate_tokens(out.split("\n…(")[0]) <= 3000
