"""agent2 审查子智能体按需派发(工具驱动扇出 + fan-in 收养/回灌)测试

覆盖计划(.trae/documents/agent2审查子智能体按需派发改造计划.md)的关键契约:
- 合法性翻译(_dispatchable_plan_items):开关关 / 无 repo_path → 全部拒收;
  未编号/未知 id、planned_evidence=verify 项逐条拒收;reference 关闭时降级 source;
  请求 id 去重保序;项数下限(MIN_ITEMS)整批拒绝由调用方完成(见集成用例)。
- 工具注册(dispatch_review_subagents):开关开 + 有工作区才注册;关闭不注册 = 纯串行。
- 不再自动扇出:只发 submit_review_plan(不发派发调用)→ 串行行为不变,
  worker 零调用、无 system 信封、回执无 dispatched。
- 显式派发主链路:先 submit_review_plan 再 dispatch_review_subagents(P1/P2/P3)
  → worker 每项取证、fan-in 收养证据重编号、回执带 dispatched、信封排在回执之后注入。
- 额度(AGENT2_SUBAGENT_DISPATCH_MAX_CALLS):同轮第二次派发 / 计划修订后二次派发
  均被拒(ok=false)。
- worker 只读白名单:请求 verify/发射类工具被拒并如实回告(不执行)。
- 终止/超时传播:派发期间命中 stop_check 整批丢弃;单项超时转收尾总结;
  单 worker 异常不拖垮整轮(信封标该项核查失败)。

替身策略沿用旧并行测试:主循环按模块属性替身 _stream_agent2_llm(3 元组兼容面),
worker 子循环直连 stream_llm(StreamResult),两者可独立屏蔽互不影响。
"""
import json
import time
import uuid
from collections import defaultdict, deque
from contextlib import nullcontext
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  (mapper 依赖)
import app.models.user_git_binding  # noqa: F401

import app.agents.agent2 as agent2
import app.agents.react_agent as react_agent
from app.agents.evidence import (
    EvidenceRecorder,
    remap_refs_in_text,
    score_review_item,
)
from app.agents.runtime.llm_stream import StreamResult
from app.config import settings


_FINAL_JSON = (
    '{"covered": [], "missing": [], "reasoning": "审查完成", '
    '"suggestions": [], "results": [], "grouping": null}'
)


def _tc(name, args, cid="call_1"):
    return {
        "id": cid, "index": 0, "name": name,
        "arguments_str": json.dumps(args, ensure_ascii=False),
    }


def _plan_tc(items, cid="call_plan"):
    return _tc("submit_review_plan", {"items": items, "note": "计划"}, cid=cid)


def _dispatch_tc(ids, cid="call_disp"):
    return _tc(
        "dispatch_review_subagents", {"plan_item_ids": list(ids), "reason": "并行核查"},
        cid=cid,
    )


def _sr(content="", tool_calls=None, reasoning="", conv_id=None, stopped=False):
    return StreamResult(
        conv_id=conv_id or f"conv-{uuid.uuid4().hex[:8]}",
        reasoning=reasoning, content=content,
        tool_calls=tool_calls or [], finish_reason="stop", stopped=stopped,
    )


def _plan_items(n=3, planned="source"):
    """未补 id 的计划项(供 submit_review_plan 参数用;_stamp_plan_items 会补 P1/P2…)"""
    return [
        {
            "target": f"结论{i}", "origin": "agent1_claim",
            "priority": "high", "planned_evidence": planned,
        }
        for i in range(1, n + 1)
    ]


def _pi(pid, planned="source"):
    """已带 id 的计划项(供纯函数 _dispatchable_plan_items 直接测试)"""
    return {
        "id": pid, "target": f"结论{pid}", "origin": "agent1_claim",
        "planned_evidence": planned,
    }


def _patch_common(monkeypatch, *, enabled=True, min_items=3, max_calls=1, concurrency=4):
    """派发链路公共屏蔽:闸门透传、publish 捕获、设置项钉住"""
    publishes = []
    monkeypatch.setattr(settings, "AGENT2_SUBAGENT_DISPATCH_ENABLED", enabled)
    monkeypatch.setattr(settings, "AGENT2_SUBAGENT_DISPATCH_MAX_CALLS", max_calls)
    monkeypatch.setattr(settings, "AGENT2_PARALLEL_MIN_ITEMS", min_items)
    monkeypatch.setattr(settings, "AGENT2_PARALLEL_MAX_CONCURRENCY", concurrency)
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    monkeypatch.setattr(agent2, "publish", lambda *a, **k: publishes.append((a[1], a[2]) if len(a) > 1 else a))
    return publishes


def _mk_main_stream(script):
    """主循环替身:按调用序弹 (content, tool_calls, reasoning),记录 messages 快照与 tools"""
    calls = []
    snapshots = []
    tools_snapshots = []

    def _fake(client, messages, *, task_id, round_idx, tools=None, **extra):
        snapshots.append([dict(m) for m in messages])
        tools_snapshots.append(tools)
        calls.append(1)
        if len(calls) <= len(script):
            return script[len(calls) - 1]
        return (_FINAL_JSON, [], "")

    _fake.snapshots = snapshots
    _fake.tools_snapshots = tools_snapshots
    return _fake


def _tool_names(tools_snapshots, idx=0):
    tools = tools_snapshots[idx] or []
    return {(t.get("function") or {}).get("name") for t in tools}


def _mk_worker_stream(plans):
    """worker 子循环替身:按"指令层里的计划项 id"路由各自脚本(线程安全:
    同一 pid 只被同一 worker 线程顺序消费),并记录每次调用参数"""
    queues = defaultdict(deque)
    for pid, turns in plans.items():
        queues[pid].extend(turns)
    calls = []  # (pid, kwargs, messages 快照)

    def _fake(client, messages, **kwargs):
        pid = ""
        for m in messages:
            if m.get("role") == "system" and "[审查计划项:" in str(m.get("content") or ""):
                pid = str(m["content"]).split("[审查计划项:", 1)[1].split("]", 1)[0]
                break
        calls.append((pid, kwargs, list(messages)))
        q = queues.get(pid)
        if q:
            return q.popleft()
        return _sr(content=f"{pid} 兜底结论")

    _fake.calls = calls
    _fake.queues = queues
    return _fake


def _fake_read_tool(fn, args, repo_path, task_id):
    return json.dumps(
        {"file_path": args.get("file_path", ""), "content": f"{args.get('file_path','')}:1 secret()"},
        ensure_ascii=False,
    )


def _long_read_json() -> str:
    """sandbox_tools.read_file 的真实返回形状,正文远超回灌预算(500 行)"""
    lines = [f"{i:>4}: def f_{i}(x):  # {'y' * 40}" for i in range(1, 501)]
    return json.dumps({
        "path": "big.py", "content": "\n".join(lines),
        "start_line": 1, "end_line": 500, "total_lines": 500,
        "truncated": False, "binary": False, "size": 0,
    }, ensure_ascii=False)


def _mk_task():
    task = MagicMock()
    task.id = uuid.uuid4()
    task.verifier_enabled = False
    task.test_env_url = ""
    task.params = {}  # 真实 dict,便于 _finalize_emitted 写 _review/_grouping
    return task


def _run(monkeypatch, main_script, worker, *, repo_path="C:/repo", **kw):
    monkeypatch.setattr(agent2, "_stream_agent2_llm", main_script)
    monkeypatch.setattr(agent2, "_execute_read_tool", _fake_read_tool)
    monkeypatch.setattr(agent2, "stream_llm", worker)
    return agent2.run_agent2(
        "审计这个仓库", [], task_id="task-disp", round_idx=1,
        client=MagicMock(), repo_path=repo_path, **kw,
    )


# ============================================================
# 合法性翻译(_dispatchable_plan_items)
# ============================================================


def test_dispatch_disabled_rejects_all(monkeypatch):
    _patch_common(monkeypatch, enabled=False)
    items = [_pi("P1"), _pi("P2"), _pi("P3")]
    eligible, rejected = agent2._dispatchable_plan_items(
        items, ["P1", "P2", "P3"], "C:/repo", True,
    )
    assert eligible == []
    assert {r["id"] for r in rejected} == {"P1", "P2", "P3"}


def test_dispatch_requires_repo_path(monkeypatch):
    _patch_common(monkeypatch)
    items = [_pi("P1"), _pi("P2"), _pi("P3")]
    eligible, rejected = agent2._dispatchable_plan_items(
        items, ["P1", "P2", "P3"], None, True,
    )
    assert eligible == []
    assert len(rejected) == 3


def test_dispatch_rejects_verify_and_unknown(monkeypatch):
    """verify 项逐条拒收(沙箱共享不可并发);未知/未编号 id 拒收;其余照派"""
    _patch_common(monkeypatch)
    items = [_pi("P1"), _pi("P2", planned="verify"), _pi("P3")]
    eligible, rejected = agent2._dispatchable_plan_items(
        items, ["P1", "P2", "P9"], "C:/repo", True,
    )
    assert [it["id"] for it in eligible] == ["P1"]
    reasons = {r["id"]: r["reason"] for r in rejected}
    assert "P2" in reasons and "verify" in reasons["P2"]
    assert "P9" in reasons  # 未知 id


def test_dispatch_dedup_preserves_order(monkeypatch):
    _patch_common(monkeypatch)
    items = [_pi("P1"), _pi("P2"), _pi("P3")]
    eligible, rejected = agent2._dispatchable_plan_items(
        items, ["P3", "P1", "P3", "P2", ""], "C:/repo", True,
    )
    assert [it["id"] for it in eligible] == ["P3", "P1", "P2"]
    assert rejected == []


def test_dispatch_reference_downgrades_when_disabled(monkeypatch):
    """引用复核关闭时 reference 项降级为纯只读,仍可派发;且不篡改原计划项"""
    _patch_common(monkeypatch)
    items = [_pi("P1", planned="reference"), _pi("P2", planned="reference")]
    eligible, _ = agent2._dispatchable_plan_items(items, ["P1", "P2"], "C:/repo", False)
    assert all(it["planned_evidence"] == "source" for it in eligible)
    assert all(it["planned_evidence"] == "reference" for it in items)  # 原件不动


# ============================================================
# 工具注册条件(dispatch_review_subagents)
# ============================================================


def test_dispatch_tool_registered_when_repo_and_enabled(monkeypatch):
    _patch_common(monkeypatch, enabled=True)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([(_FINAL_JSON, [], "")])
    _run(monkeypatch, main, worker, repo_path="C:/repo")
    assert "dispatch_review_subagents" in _tool_names(main.tools_snapshots)


def test_dispatch_tool_not_registered_when_disabled(monkeypatch):
    _patch_common(monkeypatch, enabled=False)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([(_FINAL_JSON, [], "")])
    result = _run(monkeypatch, main, worker, repo_path="C:/repo")
    assert not result.get("degraded") and not result.get("stopped")
    assert "dispatch_review_subagents" not in _tool_names(main.tools_snapshots)
    assert worker.calls == []  # 未注册 → 无派发 → 纯串行


def test_dispatch_tool_not_registered_without_repo_path(monkeypatch):
    _patch_common(monkeypatch, enabled=True)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([(_FINAL_JSON, [], "")])
    _run(monkeypatch, main, worker, repo_path=None)
    assert "dispatch_review_subagents" not in _tool_names(main.tools_snapshots)


# ============================================================
# 证据收养(adopt_entries / remap_refs_in_text)
# ============================================================


def test_adopt_entries_renumbers_and_maps():
    main = EvidenceRecorder()
    main.record_read('{"content": "主台账"}', args_brief="主线程")
    worker = EvidenceRecorder()
    worker.record_read('{"content": "worker 取证"}', args_brief="P2 取证")
    worker.record_reference('{"exists": true}', args_brief="url")

    mapping = main.adopt_entries(dict(worker.ledger))
    assert mapping == {"E1": "E2", "E2": "E3"}
    assert main.ledger["E2"]["body"] == '{"content": "worker 取证"}'  # body 原样
    assert main.ledger["E2"]["kind"] == "read"
    assert main.ledger["E3"]["kind"] == "reference"
    assert "adopted:E1" in main.ledger["E2"]["call_id"]
    # 非法条目跳过不占编号
    assert main.adopt_entries({"E9": "not-a-dict"}) == {}
    assert "E9" not in main.ledger and main._seq == 3


def test_remap_refs_in_text_rewrites_mapped_strips_others():
    text = "结论成立(见 E1/E2),PE12 与 XE3 不是引用,未收养的 E9 应被剥离"
    out = remap_refs_in_text(text, {"E1": "E4", "E2": "E5"})
    assert "见 E4/E5" in out
    # 非引用形态(相邻字母/数字)不动
    assert "PE12" in out and "XE3" in out
    # P2-2:命中不到映射的引用号被剔并标注无效,不指向别人证据
    assert "E9" not in out and "（无效引用）" in out
    assert remap_refs_in_text("", {"E1": "E2"}) == ""
    # mapping 为空时不动文本(不误伤合法引用)
    assert remap_refs_in_text("见 E1", {}) == "见 E1"


def test_adopted_refs_score_source_confirmed():
    """收养后的新 ref + 原文 quote → 源码取证档;旧局部 ref 查无此人 → 只降档不判 mismatch"""
    main = EvidenceRecorder()
    worker = EvidenceRecorder()
    body = '{"file_path": "a.py", "content": "L1 secret()"}'
    worker.record_read(body, args_brief="read_file:a.py")
    mapping = main.adopt_entries(dict(worker.ledger))
    new_ref = mapping["E1"]
    ev = {"source": {"file_path": "a.py", "quote": "secret()", "call_ref": new_ref}}
    score = score_review_item(ev, "established", main.ledger, has_repo=True, verify_available=False)
    assert score["tier"] == "source_confirmed"
    assert score["evidence_validated"] is True
    # 直接拿 worker 局部旧 ref 指主台账:主台账查无此人 → 缺可核验引用,降档为仅断言
    # (不判"谎报引用"——旧编号是机制性错配,不是模型凭空捏造)
    ev_stale = {"source": {"file_path": "a.py", "quote": "secret()", "call_ref": "E7"}}
    stale = score_review_item(ev_stale, "established", main.ledger, has_repo=True, verify_available=False)
    assert stale["tier"] == "assertion_only"
    assert stale["evidence_mismatch"] is False


# ============================================================
# run_agent2 全链路:计划 → 派发 → fan-in 回灌
# ============================================================


def test_dispatch_and_fan_in_verifies_plan_items(monkeypatch):
    """主用例:先 submit_review_plan 再 dispatch_review_subagents(P1,P2,P3)。
    → 各子循环(每项 1 次取证 + 1 次总结);fan-in 收养证据、回执带 dispatched、
    system 信封排在工具回执之后注入。"""
    publishes = _patch_common(monkeypatch)
    worker = _mk_worker_stream({
        f"P{i}": [
            _sr(tool_calls=[_tc("read_file", {"file_path": f"a{i}.py"}, cid=f"c{i}")]),
            _sr(content="结论成立,证据 E1"),
        ]
        for i in (1, 2, 3)
    })
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "先规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "派发"),
    ])
    result = _run(monkeypatch, main, worker)

    assert not result.get("degraded") and not result.get("stopped")
    assert len(worker.calls) == 6  # 每项 2 发
    # worker 请求带身份与只读白名单(无 submit_*/verify 工具)
    for _pid, kwargs, _msgs in worker.calls:
        assert kwargs["extra"]["parallel"] is True
        assert kwargs["extra"]["facet"].startswith("计划项 ")
        names = {t["function"]["name"] for t in kwargs["tools"]}
        assert "read_file" in names and not any(n.startswith("submit_") for n in names)
    fan_in_msgs = main.snapshots[-1]
    envelopes = [m for m in fan_in_msgs if m.get("role") == "system"
                 and "[系统注入|计划项结论|P" in str(m.get("content"))]
    assert [m["content"].split("|P")[1][0] for m in envelopes] == ["1", "2", "3"]
    # 每个 worker 的局部 E1 收养后按主台账重新编号并改写结论引用号
    assert "证据 E1" in envelopes[0]["content"]
    assert "证据 E2" in envelopes[1]["content"]
    assert "证据 E3" in envelopes[2]["content"]
    # 回执排在信封之前(assistant tool_calls 与 tool 结果相邻)
    idx_receipt = next(i for i, m in enumerate(fan_in_msgs)
                       if m.get("role") == "tool" and '"dispatched"' in str(m.get("content")))
    idx_env = next(i for i, m in enumerate(fan_in_msgs) if "[系统注入|并行核查]" in str(m.get("content")))
    assert idx_receipt < idx_env
    receipt = json.loads(fan_in_msgs[idx_receipt]["content"])
    assert receipt["ok"] is True and receipt["dispatched"] == ["P1", "P2", "P3"]
    assert any(p == "review_plan_update" for p, _ in publishes)
    # 派发回执以 tool 形式回灌:其前一条 assistant 必带匹配的 tool_calls(消息序列形状)
    _assert_tool_msgs_preceded_by_tool_calls(main.snapshots)


def test_plan_submit_without_dispatch_stays_serial(monkeypatch):
    """反转旧"自动扇出":只发 submit_review_plan(不发派发调用)→ 不扇出、
    无信封、回执无 dispatched。这是改造后的常态。"""
    _patch_common(monkeypatch)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "先规划"),
        # 模型选择自己逐项核查:直接发审查项而非派发
        ("", [_tc("submit_review_item", {
            "title": "注入", "review_target": "users.py:42", "origin": "agent1_claim",
            "plan_item_id": "P1", "status": "covered", "verdict": "confirmed",
        }, cid="call_item")], "自核 P1"),
    ])
    result = _run(monkeypatch, main, worker)

    assert not result.get("degraded") and not result.get("stopped")
    assert worker.calls == []  # 未派发 → 子循环零调用
    fan_in_msgs = main.snapshots[-1]
    assert not any("[系统注入|" in str(m.get("content"))
                   for m in fan_in_msgs if m.get("role") == "system")
    plan_receipt = json.loads(next(
        m["content"] for m in fan_in_msgs
        if m.get("role") == "tool" and '"planned"' in str(m.get("content"))))
    assert "dispatched" not in plan_receipt
    assert "dispatch_review_subagents" in plan_receipt["hint"]


def test_model_does_not_dispatch_for_small_plan(monkeypatch):
    """决策行为(止血桩):2 条计划项的小审查,模型不派发 → 纯串行自核,零子循环。"""
    _patch_common(monkeypatch, min_items=3)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([("", [_plan_tc(_plan_items(2))], "规划 2 项")])
    result = _run(monkeypatch, main, worker)

    assert not result.get("degraded") and not result.get("stopped")
    assert worker.calls == []


def test_dispatch_below_min_items_rejects_batch(monkeypatch):
    """请求只 1 条(< MIN_ITEMS)→ 整批拒绝、不扇出、回执提示自行逐项核查。"""
    _patch_common(monkeypatch, min_items=3)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1"])], "只派 1 项"),
    ])
    _run(monkeypatch, main, worker)

    assert worker.calls == []  # 整批拒绝 → 不扇出
    fan_in_msgs = main.snapshots[-1]
    reject = json.loads(next(
        m["content"] for m in fan_in_msgs
        if m.get("role") == "tool" and '"ok": false' in str(m.get("content"))))
    assert reject["ok"] is False and "不足" in reject["reason"]


def test_dispatch_mixed_rejects_verify_keeps_others(monkeypatch):
    """请求含 verify 项 + 未知 id:逐条拒收,其余合格项 ≥ MIN_ITEMS 时照派。"""
    _patch_common(monkeypatch, min_items=3)
    items = [
        {"target": "c1", "origin": "agent1_claim", "planned_evidence": "source"},
        {"target": "c2", "origin": "agent1_claim", "planned_evidence": "verify"},
        {"target": "c3", "origin": "agent1_claim", "planned_evidence": "source"},
        {"target": "c4", "origin": "agent1_claim", "planned_evidence": "source"},
    ]
    worker = _mk_worker_stream({
        f"P{i}": [_sr(content=f"P{i} 结论")] for i in (1, 3, 4)
    })
    main = _mk_main_stream([
        ("", [_plan_tc(items)], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3", "P4"])], "全派(含 1 条 verify)"),
    ])
    _run(monkeypatch, main, worker)

    fan_in_msgs = main.snapshots[-1]
    receipt = json.loads(next(
        m["content"] for m in fan_in_msgs
        if m.get("role") == "tool" and '"dispatched"' in str(m.get("content"))))
    assert receipt["dispatched"] == ["P1", "P3", "P4"]  # verify P2 被逐项拒收
    assert {r["id"] for r in receipt["rejected"]} == {"P2"}


def test_second_dispatch_rejected_by_max_calls(monkeypatch):
    """同轮第二次派发 → 额度用尽(ok=false),仅首轮扇出。"""
    _patch_common(monkeypatch, max_calls=1)
    worker = _mk_worker_stream({
        f"P{i}": [_sr(content=f"P{i} 结论")] for i in (1, 2, 3)
    })
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "首次派发"),
        ("", [_dispatch_tc(["P1", "P2", "P3"], cid="call_disp2")], "重复派发"),
    ])
    _run(monkeypatch, main, worker)

    assert len(worker.calls) == 3  # 仅首轮扇出(每项 1 发)
    fan_in_msgs = main.snapshots[-1]
    quota = next(
        json.loads(m["content"]) for m in fan_in_msgs
        if m.get("role") == "tool" and "派发次数已达上限" in str(m.get("content")))
    assert quota["ok"] is False


def test_resubmit_plan_still_bounded_by_max_calls(monkeypatch):
    """计划重发全量修订(P4)后仍受 MAX_CALLS=1 约束:第二次派发被拒。"""
    _patch_common(monkeypatch, max_calls=1)
    worker = _mk_worker_stream({
        f"P{i}": [_sr(content=f"P{i} 结论")] for i in (1, 2, 3)
    })
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "首次派发"),
        ("", [_plan_tc(_plan_items(4))], "修订重发"),
        ("", [_dispatch_tc(["P1", "P2", "P3", "P4"], cid="call_disp2")], "修订后再派"),
    ])
    _run(monkeypatch, main, worker)

    assert len(worker.calls) == 3  # 仍是首轮 3 项各 1 发
    fan_in_msgs = main.snapshots[-1]
    assert any(
        "派发次数已达上限" in str(m.get("content"))
        for m in fan_in_msgs if m.get("role") == "tool"
    )


def test_worker_readonly_whitelist_blocks_verify(monkeypatch):
    """worker 请求 verify:不执行、如实回告,子循环继续可正常收尾"""
    _patch_common(monkeypatch)
    worker = _mk_worker_stream({
        "P1": [
            _sr(tool_calls=[_tc("verify", {"verification_request": "跑 PoC"})]),
            _sr(content="源码结论成立,不必动态验证"),
        ],
        **{f"P{i}": [_sr(content="ok")] for i in (2, 3)},
    })
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "派发"),
    ])
    _run(monkeypatch, main, worker)

    p1_calls = [m for pid, _kw, m in worker.calls if pid == "P1"]
    blocked = [m for m in p1_calls[-1] if m.get("role") == "tool" and "已禁用" in str(m.get("content"))]
    assert blocked and "verify" in blocked[0]["content"]
    # 被拒的调用不执行不进台账:P1 脚本跑完(无剩余轮次)
    assert not worker.queues.get("P1")


def test_user_stop_during_fan_out_discards_all(monkeypatch):
    """派发期间命中用户终止:部分结果整批丢弃(不落库、无信封),审查按 stopped 收尾"""
    _patch_common(monkeypatch)
    flag = {"stop": False}

    def _worker(client, messages, **kwargs):
        flag["stop"] = True  # 第一发起,终止被按下
        return _sr(content="半途结论", stopped=True)

    rows = []
    monkeypatch.setattr(
        agent2, "record_conversation",
        lambda db, task, **kw: rows.append(kw) or MagicMock(id="x"),
    )
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "派发"),
    ])
    result = _run(
        monkeypatch, main, _worker,
        db=MagicMock(), task=_mk_task(), stop_check=lambda: flag["stop"],
    )

    assert result.get("stopped") is True
    # 丢弃 = 不落库不回灌:没有任何带并行身份的落库行
    assert not [r for r in rows if (r.get("extra_payload") or {}).get("parallel")]


def test_single_worker_error_does_not_sink_round(monkeypatch):
    """单 worker 流式异常:该项信封标核查失败,其余项正常回灌"""
    _patch_common(monkeypatch)

    def _worker(client, messages, **kwargs):
        pid = next((str(m["content"]).split("[审查计划项:", 1)[1].split("]", 1)[0]
                    for m in messages if m.get("role") == "system"
                    and "[审查计划项:" in str(m.get("content"))), "")
        if pid == "P2":
            raise RuntimeError("provider 429")
        return _sr(content=f"{pid} 结论")

    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "派发"),
    ])
    result = _run(monkeypatch, main, _worker)

    assert not result.get("degraded") and not result.get("stopped")
    envs = [str(m["content"]) for m in main.snapshots[-1]
            if m.get("role") == "system" and "[系统注入|计划项结论" in str(m.get("content"))]
    assert any("P2" in c and "核查失败" in c and "429" in c for c in envs)
    assert any("P1" in c and "P1 结论" in c for c in envs)


def test_fan_in_pending_rows_pair_tool_call_id(monkeypatch):
    """fan-in 落库:tool_result 邻接配对 tool_call_id;thinking 带 stream_conv_id;
    extra_payload 携带计划项身份("落库必 publish"由 record_conversation 内部保证)"""
    _patch_common(monkeypatch)
    rows = []
    seq = iter(range(1, 100))

    def _capture(db, task, **kwargs):
        rows.append(kwargs)
        return MagicMock(id=f"conv{next(seq)}")

    monkeypatch.setattr(agent2, "record_conversation", _capture)
    worker = _mk_worker_stream({
        "P1": [
            _sr(tool_calls=[_tc("read_file", {"file_path": "a.py"}, cid="c1")], conv_id="s-thinking-1"),
            _sr(content="结论 E1", conv_id="s-thinking-2"),
        ],
        **{f"P{i}": [_sr(content="ok", conv_id=f"s-{i}")] for i in (2, 3)},
    })
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "派发"),
    ])
    _run(monkeypatch, main, worker, db=MagicMock(), task=_mk_task())

    p1_rows = [r for r in rows if (r.get("extra_payload") or {}).get("facet") == "计划项 P1"]
    types = [r["type"] for r in p1_rows]
    assert types[:2] == ["thinking", "tool_call"]
    i_result = types.index("tool_result")
    # tool_result 的 tool_call_id 指向前一行 tool_call 落库返回的 conv id
    tool_call_row_idx = rows.index(p1_rows[i_result - 1])
    assert p1_rows[i_result]["tool_call_id"] == f"conv{tool_call_row_idx + 1}"
    assert p1_rows[0]["stream_conv_id"] == "s-thinking-1"
    assert all(r["role"] == "agent2" and r["extra_payload"]["parallel"] is True for r in p1_rows)


def test_dispatch_records_tool_call_line_for_timeline(monkeypatch):
    """派发是过程,应落一条 tool_call 对话行(intent 含"派发 N 个审查子智能体"),
    与发射类工具(submit_*)不落行形成对照。"""
    _patch_common(monkeypatch)
    rows = []

    def _capture(db, task, **kwargs):
        rows.append(kwargs)
        return MagicMock(id=f"conv{len(rows)}")

    monkeypatch.setattr(agent2, "record_conversation", _capture)
    worker = _mk_worker_stream({f"P{i}": [_sr(content="ok")] for i in (1, 2, 3)})
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_dispatch_tc(["P1", "P2", "P3"])], "派发"),
    ])
    _run(monkeypatch, main, worker, db=MagicMock(), task=_mk_task())

    call_rows = [
        r for r in rows
        if r["type"] == "tool_call" and "派发 3 个审查子智能体" in str(r.get("content"))
    ]
    assert call_rows, "派发应落一条 tool_call 对话行"
    assert "dispatch_review_subagents" in call_rows[0]["content"]
    # 结果行邻接配对到调用行的 conv id
    result_rows = [r for r in rows if r["type"] == "tool_result" and r.get("tool_call_id")]
    assert result_rows


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


def test_plan_item_pass_assistant_carries_tool_calls(monkeypatch):
    """回归 P0-2:直连 _run_plan_item_pass,捕获 stream_llm 每次收到的 messages,
    断首轮调工具后第二迭代的 assistant 已带 tool_calls(而非纯 content)。"""
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    monkeypatch.setattr(agent2, "_execute_read_tool", _fake_read_tool)
    snapshots = []
    script = deque([
        _sr(tool_calls=[_tc("read_file", {"file_path": "a.py"}, cid="cA")]),
        _sr(content="结论:核查成立"),
    ])

    def _worker(client, messages, **kwargs):
        snapshots.append(list(messages))
        return script.popleft()

    monkeypatch.setattr(agent2, "stream_llm", _worker)
    result = agent2._run_plan_item_pass(
        {"id": "P1", "target": "t", "planned_evidence": "source"},
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        MagicMock(), "task-shape", 1, "C:/repo",
        read_cap=5, max_iter=8, item_timeout=60,
    )

    assert result.error == ""
    assert len(snapshots) >= 2  # 第二发的 messages 已含首轮 tool 交换(回归触发点)
    assert any(m.get("role") == "tool" for seq in snapshots for m in seq), \
        "脚本未产生 tool 消息,断言形同虚设"
    _assert_tool_msgs_preceded_by_tool_calls(snapshots)


def test_plan_item_long_read_is_line_bounded_for_worker(monkeypatch):
    """并行 worker 的回灌口径与主循环一致:超长 read 按**整行**裁剪并修正范围。

    回归点:worker 路径此前直接用 _truncate_for_llm 硬砍 3000 字符,导致 worker 模型
    看不懂区间尾部、且回传 end_line 失真。改为共用 _bound_for_llm 后,模型看到的是
    一段完整、自陈真实范围的代码(可据 offset=end_line+1 续读)。
    """
    import re

    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    monkeypatch.setattr(agent2, "_execute_read_tool", lambda *a, **k: _long_read_json())
    snapshots = []
    script = deque([
        _sr(tool_calls=[_tc("read_file", {"file_path": "big.py"}, cid="cB")]),
        _sr(content="结论"),
    ])

    def _worker(client, messages, **kwargs):
        snapshots.append(list(messages))
        return script.popleft()

    monkeypatch.setattr(agent2, "stream_llm", _worker)
    agent2._run_plan_item_pass(
        {"id": "P1", "target": "t", "planned_evidence": "source"},
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        MagicMock(), "task-shape", 1, "C:/repo",
        read_cap=5, max_iter=8, item_timeout=60,
    )

    tool_msgs = [m["content"] for seq in snapshots for m in seq if m.get("role") == "tool"]
    assert tool_msgs, "脚本未产生 tool 消息"
    fed = tool_msgs[-1]
    assert "工具结果过长已截断" not in fed  # 不再是硬砍
    obj = json.loads(fed)
    assert obj["_evidence_ref"] == "E1"  # 注入的引用号在最前,未被裁掉
    assert obj["end_line"] < 500 and obj["truncated"] is True  # 范围自陈
    assert all(re.match(r"^\s*\d+:", ln) for ln in obj["content"].splitlines())


# ============================================================
# 子循环引擎单元(_run_plan_item_pass):超时宽限收尾
# ============================================================


def test_plan_item_deadline_converts_to_finalize_summary(monkeypatch):
    """单项墙钟到期 → timed_out + 宽限收尾轮(部分结论保留,不整项作废)"""
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    seen = []

    def _worker(client, messages, **kwargs):
        seen.append(kwargs)
        return _sr(content="已核实到一半的结论,证据 E1")

    monkeypatch.setattr(agent2, "stream_llm", _worker)
    result = agent2._run_plan_item_pass(
        {"id": "P1", "target": "t", "planned_evidence": "source"},
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        MagicMock(), "task-x", 1, "C:/repo",
        read_cap=5, max_iter=8, item_timeout=60, hard_deadline=time.monotonic() - 1.0,
    )

    assert result.timed_out is True
    assert result.error == ""
    assert "结论" in result.summary
    assert len(seen) == 1  # 主预算已到期,只花收尾轮一发
    assert seen[0]["iteration"] == 9  # max_iter+1:收尾轮编号在预算外
    notices = [r for r in result.pending_rows if "强制转入" in str(r.get("content"))]
    assert notices and notices[0]["type"] == "thinking"


def test_plan_item_stop_check_marks_aborted(monkeypatch):
    """worker 循环顶命中终止 → aborted(整批丢弃由 fan-in 边界负责)"""
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())

    def _worker(client, messages, **kwargs):  # 不应被调用
        raise AssertionError("aborted 时不应发起 LLM 调用")

    monkeypatch.setattr(agent2, "stream_llm", _worker)
    result = agent2._run_plan_item_pass(
        {"id": "P9", "target": "t", "planned_evidence": "source"},
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        MagicMock(), "task-y", 1, "C:/repo",
        read_cap=5, max_iter=8, item_timeout=60,
        stop_check=lambda: True,
    )
    assert result.aborted is True
    assert result.pending_rows == []
