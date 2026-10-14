"""内置 react_agent 子任务并行单元测试(不触真实 LLM/DB)。

覆盖"子任务按需派发"重构的承重件:
- _subtask_readonly_tools / _subtask_tools / _subtask_allowed_tool_names:
  只读基集剔除改动工具与嵌套派发工具;skills 为空时剔除 list_skills/skill
- _review_provider_gate:按 provider+model 归组的并发天花板
- _run_subtask_pass:有界只读子循环 —— spec{label,instruction,skills}、只读工具执行、
  改动工具与臆造的嵌套派发调用被拒、pending_rows 形状、summary 捕获、单子任务异常隔离(不抛)、
  墙钟超时转总结收尾(timed_out)、子任务角色作为独立 system 指令层(不注入 skill 正文)
- _subtask_stream_turn:发布 label-tagged thinking_delta + conv_id 每次调用唯一
- _fan_in:主线程串行落库 —— 子任务顺序、tool_result 邻接补 tool_call_id、facet 列写 label、
  stream_conv_id 透传(实时卡退役)、超小子标题
- _visible_skill_names:去重保序、可为空(不再决定注册闸门)
- _build_subtask_dispatch_tool:subagents/instruction/skills/label schema + 技能名注入描述
- _dispatch_subtasks:校验(空/缺 instruction/未知技能/超上限 → ValueError)、label 缺省
  「子任务 N」、skills 去重保序、fan-in 落库、交接基座剔除本轮未收口的 assistant tool_calls
- 主循环 dispatch_review_subtasks 特判:开关开即注册、派发结果作为 tool_result 回灌、
  超调用额度直接报错不扇出
- 消息序列契约(provider 400 的两半):"tool 必有归属 call"与"call 必有 tool 应答"
  双向断言,覆盖子循环第 2 迭代与子任务首调用这两个真实踩过的坑
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
    # 与 LLMClient 契约一致:调用方会读它作为单次输出上限
    max_output_tokens = 8192

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
        self.allowed_skills = None


# ============================================================
# 只读工具过滤 + 按子任务技能子集裁剪
# ============================================================


def test_subtask_readonly_tools_excludes_mutating():
    tools = R.get_all_tools()
    ro = R._subtask_readonly_tools(tools)
    names = R._subtask_allowed_tool_names(ro)
    # 改动工具被剔除
    for bad in ("write_file", "run_command", "clone_repo", "str_replace_editor", "run_python_code"):
        assert bad not in names
    # 只读分析工具保留
    for good in ("read_file", "search_code", "list_files", "run_semgrep"):
        assert good in names
    # skill 工具在只读基集里(非改动工具)
    assert "list_skills" in names and "skill" in names


def test_subtask_tools_drops_skill_tools_when_no_skills():
    """skills 为空 → 从工具清单剔除 list_skills/skill;非空 → 保留。"""
    readonly = R._subtask_readonly_tools(R.get_all_tools())
    names_with = R._subtask_allowed_tool_names(R._subtask_tools(readonly, ["a"]))
    assert "list_skills" in names_with and "skill" in names_with
    names_without = R._subtask_allowed_tool_names(R._subtask_tools(readonly, []))
    assert "list_skills" not in names_without and "skill" not in names_without
    # 其余只读工具保留(剔除范围只针对 skill 工具)
    assert "read_file" in names_without


def test_subtask_readonly_tools_excludes_dispatch_tool():
    """回归:派发工具不得泄漏进子循环工具清单(线上任务 4ead4ea1 / 4a9c1719)。

    主循环把工具集交给 _dispatch_subtasks 时含 dispatch_review_subtasks,而该工具
    只在主循环有 handler(特判走 _dispatch_subtasks),子循环走 execute_tool 注册表
    必报「未知工具」。子模型看到工具定义 + 共享 prompt 的"仅当工具列表里有…
    适用"同时成立,就会在自己的子循环里再派发一次。
    """
    # 用主循环同一拼法现构全集(不硬编码工具名以外的结构假设)
    parent_tools = R.get_all_tools() + [R._build_subtask_dispatch_tool([])]
    assert "dispatch_review_subtasks" in R._subtask_allowed_tool_names(parent_tools)

    readonly = R._subtask_readonly_tools(parent_tools)
    names = R._subtask_allowed_tool_names(readonly)
    assert "dispatch_review_subtasks" not in names
    # 两条裁剪路径都要覆盖:skills 非空时直接返回 readonly 基集,为空时再剔 skill 工具
    assert "dispatch_review_subtasks" not in R._subtask_allowed_tool_names(
        R._subtask_tools(readonly, ["review_concurrency"])
    )
    assert "dispatch_review_subtasks" not in R._subtask_allowed_tool_names(
        R._subtask_tools(readonly, [])
    )
    # 剔除范围只多了一项派发工具:只读分析工具照常保留
    assert "read_file" in names and "search_code" in names


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
# _run_subtask_pass
# ============================================================


def _spec(label="子任务 1", instruction="find sqli", skills=None):
    return {"label": label, "instruction": instruction, "skills": skills or []}


def test_subtask_pass_executes_readonly_and_captures_summary(monkeypatch):
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
    readonly = R._subtask_readonly_tools(R.get_all_tools())
    base_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]

    fr = R._run_subtask_pass(
        _spec(label="check_sql_injection"), base_messages, readonly, client, _Task(),
        round_idx=1, max_iter=5, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert fr.error == ""
    assert fr.label == "check_sql_injection"
    assert "发现注入点" in fr.summary
    assert executed and executed[0][0] == "list_files"
    # pending_rows 顺序:thinking → tool_call → tool_result → thinking
    types = [r["type"] for r in fr.pending_rows]
    assert types == ["thinking", "tool_call", "tool_result", "thinking"]
    # 每条 thinking 行都带实时流式卡片的 stream_conv_id(fan-in 据此退役实时卡)
    assert all(r.get("stream_conv_id") for r in fr.pending_rows if r["type"] == "thinking")


def test_subtask_pass_blocks_mutating_tool(monkeypatch):
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
    readonly = R._subtask_readonly_tools(R.get_all_tools())

    fr = R._run_subtask_pass(
        _spec(label="review_concurrency"), [{"role": "system", "content": "s"}],
        readonly, client, _Task(),
        round_idx=1, max_iter=5, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert executed == []  # 改动工具从未执行
    tool_results = [r for r in fr.pending_rows if r["type"] == "tool_result"]
    assert tool_results and "禁用" in tool_results[0]["content"]


def test_subtask_pass_blocks_nested_dispatch_call(monkeypatch):
    """子模型臆造嵌套派发 → 走只读白名单拦截分支(诚实告知),不进 execute_tool。"""
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    executed = []
    monkeypatch.setattr(
        R, "execute_tool",
        lambda fn, args: executed.append(fn) or {"ok": 1},
    )

    turns = [
        [
            _chunk(deltas=[_delta(0, "d1", "dispatch_review_subtasks",
                                  '{"subagents": [{"label": "子子任务", "instruction": "x"}]}')],
                   finish="tool_calls"),
        ],
        [_chunk(content="改为自行只读核查后收尾", finish="stop")],
    ]
    client = FakeClient(turns)
    # 与主循环一致:交给派发的全集含派发工具,子循环基集必须把它剔除
    parent_tools = R.get_all_tools() + [R._build_subtask_dispatch_tool([])]
    readonly = R._subtask_readonly_tools(parent_tools)

    fr = R._run_subtask_pass(
        _spec(label="并行测试-A"), [{"role": "system", "content": "s"}],
        readonly, client, _Task(),
        round_idx=1, max_iter=5, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert executed == []  # 派发从不由子循环执行
    tool_results = [r for r in fr.pending_rows if r["type"] == "tool_result"]
    assert tool_results and "禁用" in tool_results[0]["content"]
    # 不能把旧的「未知工具」报错透传给子模型(它会把子任务推向越权/降级结论)
    assert all("未知工具" not in r["content"] for r in tool_results)


def test_subtask_pass_isolates_llm_error(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)

    class BoomClient(FakeClient):
        def chat_stream(self, **kwargs):
            raise RuntimeError("provider 500")

    readonly = R._subtask_readonly_tools(R.get_all_tools())
    fr = R._run_subtask_pass(
        _spec(), [{"role": "system", "content": "s"}], readonly,
        BoomClient([]), _Task(), round_idx=1, max_iter=3, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )
    # 异常被吞进 result.error,不抛出(单个子任务失败不拖垮整轮)
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


def _assistant_with_calls(*ids, content=""):
    """构造带结构化 tool_calls 的 assistant 消息(契约断言的公共素材)。"""
    msg: dict = {"role": "assistant"}
    if content:
        msg["content"] = content
    msg["tool_calls"] = [
        {
            "id": i, "type": "function",
            "function": {"name": "list_files", "arguments": "{}"},
        }
        for i in ids
    ]
    return msg


def _assert_tool_calls_fully_answered(snapshots):
    """断每条 assistant.tool_calls 的每个 id 其后都有 role=tool 响应(反向契约)。

    与 _assert_tool_msgs_preceded_by_tool_calls 成对,覆盖 provider 契约的两侧:
    前者管"响应有归属"(tool 必须回应某个 call),本函数管"调用有响应"(call 不能
    悬空)。线上子任务首调用 400 是被后者拒的——
    "An assistant message with 'tool_calls' must be followed by tool messages
     responding to each 'tool_call_id'"。
    """
    for seq in snapshots:
        for i, m in enumerate(seq):
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                continue
            answered = {
                x.get("tool_call_id") for x in seq[i + 1:] if x.get("role") == "tool"
            }
            missing = [tc["id"] for tc in m["tool_calls"] if tc["id"] not in answered]
            assert not missing, (
                f"assistant tool_calls 悬空未应答 {missing} → provider 必拒 400"
            )


def test_subtask_pass_assistant_carries_tool_calls(monkeypatch):
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
    readonly = R._subtask_readonly_tools(R.get_all_tools())
    fr = R._run_subtask_pass(
        _spec(),
        [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}],
        readonly, client, _Task(),
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


def test_subtask_pass_repairs_dangling_base_at_send_point(monkeypatch):
    """L2 单独也能救场:即便交接处漏了清洗(L1),子任务第一次调用发给 provider 的
    序列也要被 runtime 发送口归一化补成合法序列(线上 400 的直接死因)。

    这里绕过 _dispatch_subtasks 直接把含悬空 assistant 的基座交给 _run_subtask_pass,
    捕获真实进 chat_stream 的 messages —— 断的是"兜底真的在发送口上"。
    """
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(R, "execute_tool", lambda fn, args: {"ok": 1})

    turns = [
        [_chunk(content="本任务结论", finish="stop")],
        [_chunk(content="第二迭代", finish="stop")],
    ]

    class CapturingClient(FakeClient):
        def __init__(self, turns):
            super().__init__(turns)
            self.snapshots = []

        def chat_stream(self, **kwargs):
            self.snapshots.append([dict(m) for m in kwargs.get("messages", [])])
            return super().chat_stream(**kwargs)

    client = CapturingClient(turns)
    readonly = R._subtask_readonly_tools(R.get_all_tools())
    dangling_base = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        _assistant_with_calls("tc-dispatch", content="派发子任务"),  # 未收口
    ]

    fr = R._run_subtask_pass(
        _spec(), dangling_base, readonly, client, _Task(),
        round_idx=1, max_iter=2, executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert fr.error == "", "悬空基座不该让子任务报错"
    assert client.snapshots, "子任务从未发起 LLM 调用,断言形同虚设"
    first = client.snapshots[0]
    _assert_tool_calls_fully_answered([first])
    _assert_tool_msgs_preceded_by_tool_calls([first])
    # 补的是合成响应(不是把整条 call 丢掉):派发动作仍在序列里留痕
    assert any(tc["id"] == "tc-dispatch" for m in first
               for tc in (m.get("tool_calls") or []))


def test_dispatch_subtasks_base_is_closed_sequence(monkeypatch):
    """回归 P0(线上任务 2b38f1ef):派发此刻本轮 assistant(tool_calls)尚未收口,
    原样当基座交给子任务子循环 → 子循环第一次 LLM 调用即被 provider 拒 400。

    断言三件事:① 交给 worker 的基座里没有悬空 tool_calls;② 已收口的工具交换
    仍在(子循环要靠它继承主循环已积累的任务理解,不是退化成 system+user);
    ③ 主循环自己的 messages 未被就地改动(回灌照旧)。
    """
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 0)
    _stub_dispatch_common(monkeypatch)
    seen: dict[str, list] = {}

    def _spy(spec, base, *a, **k):
        seen.setdefault("base", list(base))
        return R._SubtaskResult(label=spec["label"], summary="x")

    monkeypatch.setattr(R, "_run_subtask_pass", _spy)
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        # 已收口的一轮工具交换:必须原样保留在基座里
        _assistant_with_calls("tc-old", content="先看结构"),
        {"role": "tool", "tool_call_id": "tc-old", "content": "{}"},
        # 本轮刚入栈、结果尚未回灌的 assistant —— 正是悬空点
        _assistant_with_calls("tc-dispatch", content="派发子任务"),
    ]

    R._dispatch_subtasks(
        _Task(), object(), 1, object(), R.get_all_tools(), msgs,
        "always_approve", [{"instruction": "查 a", "label": "a"}], ["a"],
    )

    base = seen["base"]
    _assert_tool_calls_fully_answered([base])
    assert [m.get("tool_call_id") for m in base if m.get("role") == "tool"] == ["tc-old"]
    assert any(m.get("role") == "user" for m in base), "基座被过度清洗,丢了用户原话"
    # 别名安全:主循环的列表形状不受影响(派发返回后还要往这里回灌 tool_result)
    assert msgs[-1].get("tool_calls") and len(msgs) == 5


# ============================================================
# _fan_in
# ============================================================


def test_fan_in_order_adjacency_and_label_tag(monkeypatch):
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

    fr_a = R._SubtaskResult(
        label="A", summary="A结论",
        pending_rows=[
            {"role": "agent1", "type": "thinking", "content": "想A", "reasoning": "r"},
            {"role": "agent1", "type": "tool_call", "content": "call"},
            {"role": "agent1", "type": "tool_result", "content": "res"},
        ],
    )
    fr_b = R._SubtaskResult(
        label="B", summary="B结论",
        pending_rows=[{"role": "agent1", "type": "thinking", "content": "想B", "reasoning": ""}],
    )

    summary = R._fan_in(_Task(), object(), 1, [fr_a, fr_b])

    # facet 列写 label 值(列名不变)
    facets = [r["facet"] for r in recorded]
    assert facets == ["A", "A", "A", "B"]  # 子任务顺序确定
    # tool_result 的 tool_call_id 指向前一条 tool_call 落库 id
    call_row = recorded[1]
    res_row = recorded[2]
    assert call_row["type"] == "tool_call"
    assert res_row["type"] == "tool_result"
    assert res_row["tool_call_id"] == call_row["id"]
    # thinking 不带 tool_call_id
    assert recorded[0]["tool_call_id"] is None
    assert "A结论" in summary and "B结论" in summary


def test_fan_in_reports_subtask_error(monkeypatch):
    monkeypatch.setattr(
        R, "_add_conversation",
        lambda *a, **k: SimpleNamespace(id="x"),
    )
    fr = R._SubtaskResult(label="C", error="provider 500")
    summary = R._fan_in(_Task(), object(), 1, [fr])
    assert "C" in summary and "provider 500" in summary


# ============================================================
# _visible_skill_names:可用技能名(不再决定注册闸门)
# ============================================================


def test_visible_skill_names_dedups_preserving_order(monkeypatch):
    import app.skills.loader as SL
    monkeypatch.setattr(
        SL, "resolve_visible_skills",
        lambda user_id, allowed: [
            SimpleNamespace(name="a"), SimpleNamespace(name="b"), SimpleNamespace(name="a"),
        ],
    )
    assert R._visible_skill_names(_Task()) == ["a", "b"]


def test_visible_skill_names_empty_allowed(monkeypatch):
    """无可见技能也返回空列表(子任务可纯 instruction,不据此决定是否注册工具)。"""
    import app.skills.loader as SL
    monkeypatch.setattr(SL, "resolve_visible_skills", lambda user_id, allowed: [])
    assert R._visible_skill_names(_Task()) == []


# ============================================================
# _build_subtask_dispatch_tool:schema 与技能名注入描述
# ============================================================


def test_build_subtask_dispatch_tool_schema():
    tool = R._build_subtask_dispatch_tool(["check_ssrf", "review_concurrency"])
    fn = tool["function"]
    assert fn["name"] == "dispatch_review_subtasks"
    assert "check_ssrf" in fn["description"] and "review_concurrency" in fn["description"]
    props = fn["parameters"]["properties"]
    assert "subagents" in props
    assert fn["parameters"]["required"] == ["subagents"]
    item = props["subagents"]["items"]
    assert set(["instruction", "skills", "label"]) <= set(item["properties"])
    assert item["required"] == ["instruction"]
    # skills 描述列出可用技能名(模型只能从中选子集)
    assert "check_ssrf" in item["properties"]["skills"]["description"]


def test_build_subtask_dispatch_tool_empty_skills():
    """无可见技能:技能名清单为(无),描述不残留花括号,仍可注册(纯 instruction 派发)。"""
    tool = R._build_subtask_dispatch_tool([])
    fn = tool["function"]
    assert "{skill_names}" not in fn["description"]
    item = fn["parameters"]["properties"]["subagents"]["items"]
    assert "无可见技能" in item["properties"]["skills"]["description"]


# ============================================================
# _dispatch_subtasks:扇出校验
# ============================================================


def _stub_dispatch_common(monkeypatch):
    monkeypatch.setattr(
        R, "_add_conversation", lambda *a, **k: SimpleNamespace(id="c-x")
    )
    monkeypatch.setattr(R, "wait_if_paused", lambda *a, **k: None)


def test_dispatch_subtasks_rejects_empty():
    with pytest.raises(ValueError, match="不能为空"):
        R._dispatch_subtasks(
            _Task(), object(), 1, object(), [], [], "always_approve",
            [], ["a", "b"],
        )


def test_dispatch_subtasks_rejects_blank_instruction(monkeypatch):
    _stub_dispatch_common(monkeypatch)
    with pytest.raises(ValueError, match="instruction 不能为空"):
        R._dispatch_subtasks(
            _Task(), object(), 1, object(), [], [], "always_approve",
            [{"instruction": "   ", "skills": ["a"]}], ["a"],
        )


def test_dispatch_subtasks_rejects_unknown_skill(monkeypatch):
    _stub_dispatch_common(monkeypatch)
    with pytest.raises(ValueError, match="未知技能"):
        R._dispatch_subtasks(
            _Task(), object(), 1, object(), [], [], "always_approve",
            [{"instruction": "查", "skills": ["z"]}], ["a"],
        )


def test_dispatch_subtasks_rejects_over_max(monkeypatch):
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 1)
    _stub_dispatch_common(monkeypatch)
    with pytest.raises(ValueError, match="超过上限"):
        R._dispatch_subtasks(
            _Task(), object(), 1, object(), [], [], "always_approve",
            [{"instruction": "x"}, {"instruction": "y"}], ["a", "b"],
        )


def test_dispatch_subtasks_label_defaults_and_dedups_skills(monkeypatch):
    """label 缺省按序号「子任务 N」;显式 label 保留;skills 去重保序。"""
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 0)
    _stub_dispatch_common(monkeypatch)
    captured: dict[str, dict] = {}

    def fake_pass(spec, base, readonly, *pos, **kw):
        captured[spec["label"]] = spec
        return R._SubtaskResult(label=spec["label"], summary=f"{spec['label']}结论")

    monkeypatch.setattr(R, "_run_subtask_pass", fake_pass)
    results, fan_in = R._dispatch_subtasks(
        _Task(), object(), 1, object(), R.get_all_tools(),
        [{"role": "system", "content": "s"}], "always_approve",
        [
            {"instruction": "关注鉴权", "skills": ["a", "a", "b"]},  # 无 label → 子任务 1
            {"instruction": "关注注入", "label": "专项", "skills": []},
        ],
        ["a", "b"],
    )
    assert set(captured) == {"子任务 1", "专项"}
    assert captured["子任务 1"]["skills"] == ["a", "b"]  # 去重保序
    assert captured["专项"]["skills"] == []
    assert {r.label for r in results} == {"子任务 1", "专项"}
    assert "子任务 1结论" in fan_in and "专项结论" in fan_in


def test_dispatch_subtasks_no_dedup_by_label(monkeypatch):
    """不再按名去重:两项就是两个子任务(即便 label 相同)。"""
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 0)
    _stub_dispatch_common(monkeypatch)
    seen_labels: list[str] = []

    def fake_pass(spec, *p, **k):
        seen_labels.append(spec["label"])
        return R._SubtaskResult(label=spec["label"], summary="x")

    monkeypatch.setattr(R, "_run_subtask_pass", fake_pass)
    results, _ = R._dispatch_subtasks(
        _Task(), object(), 1, object(), R.get_all_tools(), [], "always_approve",
        [{"instruction": "x"}, {"instruction": "y"}], ["a"],
    )
    assert len(results) == 2
    assert seen_labels == ["子任务 1", "子任务 2"]


# ============================================================
# 子任务超时:墙钟预算到期转总结收尾(部分结论)且不拖垮整轮
# ============================================================


def test_subtask_pass_timeout_isolates_and_summarizes(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(R, "execute_tool", lambda fn, args: {})
    # 唯一一段脚本给超时后的收尾轮(_finalize_turn 用独立宽限 deadline)
    turns = [[_chunk(content="超时前抓到的部分结论", finish="stop")]]
    client = FakeClient(turns)
    readonly = R._subtask_readonly_tools(R.get_all_tools())

    fr = R._run_subtask_pass(
        _spec(), [{"role": "system", "content": "s"}], readonly,
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
# 子任务独立指令层:角色/要求作为 system 指令而非 user 消息,不注入 skill 正文
# ============================================================


def test_subtask_pass_injects_system_instruction(monkeypatch):
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    captured: list[list[dict]] = []

    def fake_turn(client, messages, tools, **kw):
        captured.append(list(messages))
        return ("", "本子任务结论", [], "stop", "cid-1")

    monkeypatch.setattr(R, "_subtask_stream_turn", fake_turn)
    readonly = R._subtask_readonly_tools(R.get_all_tools())

    fr = R._run_subtask_pass(
        _spec(label="鉴权专项", instruction="核对鉴权绕过"),
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}],
        readonly, object(), _Task(), round_idx=1, max_iter=5,
        executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert fr.summary == "本子任务结论"
    sys_msgs = [m for m in captured[0] if m["role"] == "system"]
    # 子任务指令作为独立 system 层下发,含 label 与主 agent instruction
    assert any(
        "子任务" in m["content"] and "鉴权专项" in m["content"] for m in sys_msgs
    )
    assert any("核对鉴权绕过" in m["content"] for m in sys_msgs)
    # 不污染真实 user 问题
    assert not any(
        "子任务:" in m["content"] for m in captured[0] if m["role"] == "user"
    )


def test_subtask_pass_sets_allowed_skills_subset(monkeypatch):
    """子任务拿到所选技能子集;空技能 → allowed_skills 为空(None)且 skill 工具被剔。"""
    captured: dict = {}
    monkeypatch.setattr(
        R, "set_current_task",
        lambda *a, **k: captured.update(k),
    )
    monkeypatch.setattr(R, "_subtask_stream_turn", lambda *a, **k: ("", "结论", [], "stop", "c"))
    readonly = R._subtask_readonly_tools(R.get_all_tools())

    # 非空 skills:allowed_skills = 子集
    R._run_subtask_pass(
        _spec(label="A", skills=["a", "b"]), [{"role": "system", "content": "s"}],
        readonly, object(), _Task(), round_idx=1, max_iter=1,
        executor_command_confirm="always_approve", deadline=time.monotonic() + 60,
    )
    assert captured.get("allowed_skills") == ["a", "b"]

    # 空 skills:allowed_skills 为 None(空列表无法表达"不可见"),并靠剔工具实现不可见
    captured.clear()
    R._run_subtask_pass(
        _spec(label="B", skills=[]), [{"role": "system", "content": "s"}],
        readonly, object(), _Task(), round_idx=1, max_iter=1,
        executor_command_confirm="always_approve", deadline=time.monotonic() + 60,
    )
    assert not captured.get("allowed_skills")  # None 或空都算"未限定子集"


# ============================================================
# _subtask_stream_turn:发布 label-tagged thinking_delta + conv_id 唯一
# ============================================================


def test_subtask_stream_turn_publishes_label_tagged_delta(monkeypatch):
    import app.agents.runtime.llm_stream as LS

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        LS, "publish",
        lambda task_id, etype, data: events.append((etype, data)),
    )
    client = FakeClient([[_chunk(reasoning="想", content="答", finish="stop")]])

    r, c, tool_calls, finish, cid = R._subtask_stream_turn(
        client, [{"role": "system", "content": "s"}], None,
        task=_Task(), round_idx=1, label="check_sql_injection",
        iteration=2, deadline=time.monotonic() + 60,
    )

    assert r == "想" and c == "答" and finish == "stop" and cid
    deltas = [d for e, d in events if e == "thinking_delta"]
    assert deltas
    # facet 列名不变,值写 label
    assert all(d.get("facet") == "check_sql_injection" for d in deltas)
    assert all(d.get("parallel") is True for d in deltas)
    assert any(d.get("phase") == "start" for d in deltas)
    assert any(d.get("phase") == "end" for d in deltas)


def test_subtask_stream_turn_conv_id_unique_per_call(monkeypatch):
    monkeypatch.setattr("app.agents.runtime.llm_stream.publish", lambda *a, **k: None)
    client = FakeClient([
        [_chunk(content="a", finish="stop")],
        [_chunk(content="b", finish="stop")],
    ])
    task = _Task()
    _, _, _, _, cid1 = R._subtask_stream_turn(
        client, [{"role": "system", "content": "s"}], None,
        task=task, round_idx=1, label="f", iteration=1,
        deadline=time.monotonic() + 60,
    )
    _, _, _, _, cid2 = R._subtask_stream_turn(
        client, [{"role": "system", "content": "s"}], None,
        task=task, round_idx=1, label="f", iteration=2,
        deadline=time.monotonic() + 60,
    )
    # 每子任务每次调用唯一 conv_id,前端据此天然分桶不串扰
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
    fr = R._SubtaskResult(
        label="A", summary="A结论",
        pending_rows=[{
            "role": "agent1", "type": "thinking",
            "content": "想A", "reasoning": "r", "stream_conv_id": "sc-1",
        }],
    )
    R._fan_in(_Task(), object(), 1, [fr])
    assert recorded[0]["stream_conv_id"] == "sc-1"
    assert recorded[0]["extra_payload"] == {"facet": "A", "parallel": True}


def test_fan_in_labels_timed_out_subtask(monkeypatch):
    monkeypatch.setattr(
        R, "_add_conversation", lambda *a, **k: SimpleNamespace(id="x"),
    )
    fr = R._SubtaskResult(label="T", summary="部分结论", timed_out=True)
    summary = R._fan_in(_Task(), object(), 1, [fr])
    assert "部分结论,已超时收尾" in summary


# ============================================================
# 主循环按需派发:模型调 dispatch_review_subtasks → 扇出 → tool_result 回灌
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


def _mk_dispatch_round(monkeypatch, *, results, names=None, max_calls=1, stream=None,
                       bases=None, enabled=True):
    """驱动一整回合:第 1 迭代模型派发 dispatch_review_subtasks,第 2 迭代 stop 总结。

    返回 (task, db, seen);seen 逐次记录主循环 _stream_llm_response 收到的
    (iteration, messages, tool_names)。子任务子循环用 stub(只产 _SubtaskResult),
    fan-in 落库与主循环回灌是真代码 —— 要测的正是那条接入点。

    bases(可选):传一个列表就会按派发顺序记录每个子任务收到的基座快照
    [(label, base_messages)],用于断"交给子循环的序列必须收口"。
    enabled=False:关闭注册闸门,工具不出现在列表(用于断"开关关=不注册")。
    """
    task = _RoundTask()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []

    names = names if names is not None else [fr.label for fr in results]
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_ENABLED", enabled)
    monkeypatch.setattr(settings, "REACT_FACET_DISPATCH_MAX_CALLS", max_calls)
    # 可用技能名直接给出(注册逻辑走真 _build_subtask_dispatch_tool)
    monkeypatch.setattr(R, "_visible_skill_names", lambda t: names)
    by_label = {fr.label: fr for fr in results}

    def _stub_pass(spec, *a, **k):
        if bases is not None:
            bases.append((spec["label"], [dict(m) for m in (a[0] or [])]))
        return by_label[spec["label"]]

    monkeypatch.setattr(R, "_run_subtask_pass", _stub_pass)
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
                "name": "dispatch_review_subtasks",
                "arguments_str": json.dumps({
                    "subagents": [
                        {"instruction": f"查 {n}", "label": n} for n in names
                    ],
                }),
            }]
            return ("派发子任务", "", call, "tool_calls", "conv-1")
        return ("", "集成完毕:综合各任务结论", [], "stop", f"conv-{iteration}")

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


def test_dispatch_tool_registered_when_enabled(monkeypatch):
    results = [R._SubtaskResult(label=f"f{i}", summary=f"f{i}结论") for i in (1, 2)]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results)

    _run_round(task, db)

    # 开关开即注册:第 1 迭代工具列表里就有 dispatch_review_subtasks
    assert "dispatch_review_subtasks" in seen[0]["tool_names"]


def test_dispatch_tool_absent_when_disabled(monkeypatch):
    # 注册闸门只看 ENABLED:关闭 → 不注册派发工具(纯串行)
    def _stop_stream(client, t, db_, round_idx, iteration, messages, tools):
        return ("", "无需派发,直接结束", [], "stop", f"conv-{iteration}")

    results = [R._SubtaskResult(label="f1", summary="x")]
    task, db, seen = _mk_dispatch_round(
        monkeypatch, results=results, enabled=False, stream=_stop_stream,
    )

    _run_round(task, db)

    assert "dispatch_review_subtasks" not in seen[0]["tool_names"]


def test_dispatch_tool_registered_even_with_no_visible_skills(monkeypatch):
    """可见技能数为 0 仍注册:子任务可纯 instruction,技能数不再决定闸门。"""
    results = [R._SubtaskResult(label="纯指令任务", summary="x")]
    task, db, seen = _mk_dispatch_round(
        monkeypatch, results=results, names=["纯指令任务"],
    )

    _run_round(task, db)

    assert "dispatch_review_subtasks" in seen[0]["tool_names"]


def test_dispatch_result_returned_as_tool_result(monkeypatch):
    results = [
        R._SubtaskResult(label="f1", summary="f1结论"),
        R._SubtaskResult(label="f2", error="provider 500"),
    ]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results)

    _res, summary, _plan = _run_round(task, db)

    # 派发回灌后主循环继续跑第 2 迭代做集成
    assert seen[0]["iteration"] == 1
    assert len(seen) >= 2
    tools_msgs = _tool_messages(seen[1]["messages"])
    assert tools_msgs, "派发结论必须以 role=tool 消息回灌主循环"
    payload = json.loads(tools_msgs[-1]["content"])
    status = {s["label"]: s["status"] for s in payload["subagents"]}
    assert status == {"f1": "ok", "f2": "error"}  # error 措辞保留在 tool_result
    assert any(
        s["label"] == "f1" and "f1结论" in s["summary"] for s in payload["subagents"]
    )
    assert summary == "集成完毕:综合各任务结论"  # 主循环自然总结


def test_main_loop_subtask_base_is_closed_sequence(monkeypatch):
    """端到端(最贴近线上 400):主循环里模型调 dispatch_review_subtasks 时,
    交给每个子任务的基座必须是收口的消息序列。

    与 test_dispatch_subtasks_base_is_closed_sequence 的分工:那条直接调
    _dispatch_subtasks 并手工构造基座;这条跑整回合,验证"主循环此刻的 messages
    形状"与交接清洗真的接上了(悬空 assistant 由本轮 assistant 入栈产生)。
    """
    results = [R._SubtaskResult(label=f"f{i}", summary="x") for i in (1, 2)]
    bases: list[tuple[str, list]] = []
    task, db, _seen = _mk_dispatch_round(
        monkeypatch, results=results, bases=bases,
    )

    _run_round(task, db)

    assert sorted(name for name, _ in bases) == ["f1", "f2"], "两个子任务都应拿到基座"
    for name, base in bases:
        # 悬空 tool_calls 直接在助手函数里抛 AssertionError(带缺应答的 id)
        _assert_tool_calls_fully_answered([base])
        assert any(m.get("role") == "user" for m in base), "基座应带上主循环上下文"


def test_dispatch_over_call_cap_errors_without_fanout(monkeypatch):
    results = [R._SubtaskResult(label=f"f{i}", summary="x") for i in (1, 2)]
    task, db, seen = _mk_dispatch_round(
        monkeypatch, results=results, max_calls=0,
    )

    _run_round(task, db)

    # 额度为 0:首次派发即被守卫拦下,tool_result 是上限提示而非子任务结论
    tools_msgs = _tool_messages(seen[1]["messages"])
    assert tools_msgs and "上限" in tools_msgs[-1]["content"]


def test_dispatch_available_on_followup_round(monkeypatch):
    # 注册闸门无轮次参数:追问轮同样注册(是否派发交给模型)
    results = [R._SubtaskResult(label=f"f{i}", summary=f"f{i}结论") for i in (1, 2)]
    task, db, seen = _mk_dispatch_round(monkeypatch, results=results)

    _run_round(task, db, round_idx=2, followup_query="继续核查鉴权")

    assert "dispatch_review_subtasks" in seen[0]["tool_names"]


def test_subtask_result_items_maps_status_and_clips(monkeypatch):
    """状态映射与 _fan_in 小标题同源;结论按单份子任务 token 上限截断"""
    monkeypatch.setattr(settings, "REACT_FACET_SUMMARY_MAX_TOKENS", 50)
    items = R._subtask_result_items([
        R._SubtaskResult(label="E", error="provider 500"),
        R._SubtaskResult(label="T", summary="部分结论", timed_out=True),
        R._SubtaskResult(label="L", summary="漏" * 400),
        R._SubtaskResult(label="S", summary="短结论"),
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


def test_subtask_pass_zero_output_truncation_retries(monkeypatch):
    """回归:子任务首轮 finish=length 且零产出时,应注入收尾提示有界
    重试,重试轮拿到正文后 summary 非空。旧实现直接 break:只发 1 次调用
    且 summary=""。另验证未将空 assistant 落入上下文(脏上下文/400 隐患)。"""
    monkeypatch.setattr(R, "set_current_task", lambda *a, **k: None)
    seen = []
    turns = [
        ("思考" * 200, "", [], "length", "cid-1"),  # 零产出截断
        ("", "重试后结论", [], "stop", "cid-2"),        # 重试拿到正文
    ]
    box = {"i": 0 }

    def fake_turn(client, messages, tools, **kw):
        seen.append(list(messages))
        r = turns[box["i"]]
        box["i"] += 1
        return r

    monkeypatch.setattr(R, "_subtask_stream_turn", fake_turn)
    readonly = R._subtask_readonly_tools(R.get_all_tools())
    fr = R._run_subtask_pass(
        _spec(label="截断重试"),
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        readonly, object(), _Task(), round_idx=1, max_iter=5,
        executor_command_confirm="always_approve",
        deadline=time.monotonic() + 60,
    )

    assert len(seen) == 2                    # 旧实现只 1 次
    assert fr.summary == "重试后结论"          # 旧实现为 ""
    assert seen[1][-1]["role"] == "user"      # 重试轮末尾是注入的收尾提示
    assert "截断" in seen[1][-1]["content"]
    assert not any(
        m["role"] == "assistant" and not m.get("content") and not m.get("tool_calls")
        for m in seen[1]
    )
