"""agent2 计划项并行核查(扇出只读子循环 + fan-in 收养/回灌)测试

覆盖计划(docs/agent-architecture.md 第 2 节"计划项并行核查")的关键契约:
- 门控(_plan_item_gate):开关关 / 无 repo_path / 合格项不足 → 不扇出,
  串行行为不变;planned_evidence=verify 项永不进子循环;
- worker 只读白名单:请求 verify/发射类工具被拒并如实回告(不执行);
- 证据收养:EvidenceRecorder.adopt_entries 重编号 + remap_refs_in_text 改写
  结论引用号,收养后 score_review_item 按新 ref 正常派置信度;
- fan-in:receipt.parallel_verified、system 信封注入顺序、pending_rows 落库
  配对 tool_call_id、只扇出一次(计划重发修订不二次扇出);
- 终止/超时传播:扇出期间命中 stop_check 整批丢弃;单项超时转收尾总结;
- 单 worker 异常不拖垮整轮(信封标该项核查失败)。

替身策略沿用 test_agent2_stream_degrade:主循环按模块属性替身
_stream_agent2_llm(3 元组兼容面),worker 子循环直连 stream_llm(StreamResult),
两者可独立屏蔽互不影响。
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


def _plan_tc(items):
    return _tc("submit_review_plan", {"items": items, "note": "计划"})


def _sr(content="", tool_calls=None, reasoning="", conv_id=None, stopped=False):
    return StreamResult(
        conv_id=conv_id or f"conv-{uuid.uuid4().hex[:8]}",
        reasoning=reasoning, content=content,
        tool_calls=tool_calls or [], finish_reason="stop", stopped=stopped,
    )


def _plan_items(n=3, planned="source"):
    return [
        {
            "target": f"结论{i}", "origin": "agent1_claim",
            "priority": "high", "planned_evidence": planned,
        }
        for i in range(1, n + 1)
    ]


def _patch_common(monkeypatch, *, enabled=True, min_items=3, concurrency=4):
    """并行链路公共屏蔽:闸门透传、publish 捕获、设置项钉住"""
    publishes = []
    monkeypatch.setattr(settings, "AGENT2_PARALLEL_ENABLED", enabled)
    monkeypatch.setattr(settings, "AGENT2_PARALLEL_MIN_ITEMS", min_items)
    monkeypatch.setattr(settings, "AGENT2_PARALLEL_MAX_CONCURRENCY", concurrency)
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    monkeypatch.setattr(agent2, "publish", lambda *a, **k: publishes.append((a[1], a[2]) if len(a) > 1 else a))
    return publishes


def _mk_main_stream(script):
    """主循环替身:按调用序弹 (content, tool_calls, reasoning),并记录 messages 快照"""
    calls = []
    snapshots = []

    def _fake(client, messages, *, task_id, round_idx, tools=None, **extra):
        snapshots.append([dict(m) for m in messages])
        calls.append(1)
        if len(calls) <= len(script):
            return script[len(calls) - 1]
        return (_FINAL_JSON, [], "")

    _fake.snapshots = snapshots
    return _fake


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
        "审计这个仓库", [], task_id="task-par", round_idx=1,
        client=MagicMock(), repo_path=repo_path, **kw,
    )


# ============================================================
# 门控(_plan_item_gate)
# ============================================================


def test_gate_disabled_returns_empty(monkeypatch):
    _patch_common(monkeypatch, enabled=False)
    assert agent2._plan_item_gate(_plan_items(4), "C:/repo", True) == []


def test_gate_requires_repo_path(monkeypatch):
    _patch_common(monkeypatch)
    assert agent2._plan_item_gate(_plan_items(4), None, True) == []


def test_gate_below_min_items_returns_empty(monkeypatch):
    _patch_common(monkeypatch, min_items=3)
    assert agent2._plan_item_gate(_plan_items(2), "C:/repo", True) == []


def test_gate_excludes_verify_items(monkeypatch):
    """verify 项不进子循环(沙箱共享不可并发);非 verify 项不足 MIN_ITEMS 不扇出"""
    _patch_common(monkeypatch, min_items=3)
    items = _plan_items(2) + [{"target": "PoC", "planned_evidence": "verify"}] + _plan_items(1)
    eligible = agent2._plan_item_gate(items, "C:/repo", True)
    assert len(eligible) == 3
    assert all(str(it.get("planned_evidence")) != "verify" for it in eligible)

    # 全 verify → 无合格项
    assert agent2._plan_item_gate(
        [{"target": f"t{i}", "planned_evidence": "verify"} for i in range(4)],
        "C:/repo", True,
    ) == []


def test_gate_reference_downgrades_when_disabled(monkeypatch):
    """引用复核关闭时 reference 项降级为纯只读,仍可并行(读档核查成立)"""
    _patch_common(monkeypatch, min_items=3)
    eligible = agent2._plan_item_gate(_plan_items(3, planned="reference"), "C:/repo", False)
    assert len(eligible) == 3
    assert all(it["planned_evidence"] == "source" for it in eligible)


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


def test_remap_refs_in_text_only_rewrites_mapped():
    text = "结论成立(见 E1/E2),PE12 与 XE3 不是引用,未收养的 E9 原样"
    out = remap_refs_in_text(text, {"E1": "E4", "E2": "E5"})
    assert "见 E4/E5" in out
    assert "PE12" in out and "XE3" in out and "E9" in out
    assert remap_refs_in_text("", {"E1": "E2"}) == ""


def test_adopted_refs_score_source_confirmed():
    """收养后的新 ref + 原文 quote → 源码取证档;旧局部 ref 查无此人 → mismatch"""
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
    # 直接拿 worker 局部旧 ref 指主台账:命中校验① mismatch 压到仅断言
    ev_stale = {"source": {"file_path": "a.py", "quote": "secret()", "call_ref": "E7"}}
    stale = score_review_item(ev_stale, "established", main.ledger, has_repo=True, verify_available=False)
    assert stale["evidence_mismatch"] is True


# ============================================================
# run_agent2 全链路:扇出 + fan-in 回灌
# ============================================================


def test_fan_out_and_fan_in_parallel_verifies_plan_items(monkeypatch):
    """3 项计划 → 各子循环(1 次只读取证 + 1 次总结);fan-in 收养证据、
    回执带 parallel_verified、system 信封排在工具回执之后注入"""
    publishes = _patch_common(monkeypatch)
    worker = _mk_worker_stream({
        f"P{i}": [
            _sr(tool_calls=[_tc("read_file", {"file_path": f"a{i}.py"}, cid=f"c{i}")]),
            _sr(content=f"结论成立,证据 E1"),
        ]
        for i in (1, 2, 3)
    })
    main = _mk_main_stream([("", [_plan_tc(_plan_items(3))], "先规划")])
    result = _run(monkeypatch, main, worker)

    # emit 路径走结构化收尾:reasoning 是聚合对账文案(非 LLM JSON),
    # 只断"未降级未终止"的完成态
    assert not result.get("degraded") and not result.get("stopped")
    assert len(worker.calls) == 6  # 每项 2 发
    # worker 请求带身份与只读白名单(无 submit_*/verify 工具)
    for _pid, kwargs, _msgs in worker.calls:
        assert kwargs["extra"]["parallel"] is True
        assert kwargs["extra"]["facet"].startswith("计划项 ")
        names = {t["function"]["name"] for t in kwargs["tools"]}
        assert "read_file" in names and not any(n.startswith("submit_") for n in names)
    # 信封排在 submit_review_plan 回执之后(assistant tool_calls 与 tool 结果相邻)
    fan_in_msgs = main.snapshots[-1]
    envelopes = [m for m in fan_in_msgs if m.get("role") == "system"
                 and "[系统注入|计划项结论|P" in str(m.get("content"))]
    assert [m["content"].split("|P")[1][0] for m in envelopes] == ["1", "2", "3"]
    # 每个 worker 的局部 E1 收养后按主台账重新编号并改写结论引用号
    # (主台账此前无证据:P1→E1 恒等、P2→E2、P3→E3)
    assert "证据 E1" in envelopes[0]["content"]
    assert "证据 E2" in envelopes[1]["content"]
    assert "证据 E3" in envelopes[2]["content"]
    # 信封排在 submit_review_plan 回执之后(assistant tool_calls 与 tool 结果相邻)
    idx_receipt = next(i for i, m in enumerate(fan_in_msgs)
                       if m.get("role") == "tool" and "parallel_verified" in str(m.get("content")))
    idx_env = next(i for i, m in enumerate(fan_in_msgs) if "[系统注入|并行核查]" in str(m.get("content")))
    assert idx_receipt < idx_env
    # 回执列出并行完成项
    receipt = json.loads(fan_in_msgs[idx_receipt]["content"])
    assert receipt["parallel_verified"] == ["P1", "P2", "P3"]
    assert any(p == "review_plan_update" for p, _ in publishes)


def test_gate_miss_keeps_serial_path(monkeypatch):
    """无 repo_path → 不扇出:stream_llm 零调用、无信封、串行行为不变"""
    _patch_common(monkeypatch)
    worker = _mk_worker_stream({})
    main = _mk_main_stream([("", [_plan_tc(_plan_items(3))], "先规划")])
    result = _run(monkeypatch, main, worker, repo_path=None)

    assert not result.get("degraded") and not result.get("stopped")
    assert worker.calls == []
    fan_in_msgs = main.snapshots[-1]
    assert not any("[系统注入|" in str(m.get("content")) for m in fan_in_msgs if m.get("role") == "system")
    receipt = json.loads(next(m["content"] for m in fan_in_msgs if m.get("role") == "tool" and m["content"].startswith("{")))
    assert "parallel_verified" not in receipt


def test_fan_out_only_once_across_plan_resubmits(monkeypatch):
    """计划重发全量修订不再二次扇出(parallel_done 标志)"""
    _patch_common(monkeypatch)
    worker = _mk_worker_stream({
        f"P{i}": [
            _sr(tool_calls=[_tc("read_file", {"file_path": "a.py"}, cid=f"c{i}")]),
            _sr(content="ok"),
        ]
        for i in (1, 2, 3)
    })
    main = _mk_main_stream([
        ("", [_plan_tc(_plan_items(3))], "规划"),
        ("", [_plan_tc(_plan_items(4))], "修订重发"),  # 4 项重发也命中门控,但不得二次扇出
    ])
    result = _run(monkeypatch, main, worker)

    assert not result.get("degraded") and not result.get("stopped")
    assert len(worker.calls) == 6  # 仍是首轮 3 项 × 2 发


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
    main = _mk_main_stream([("", [_plan_tc(_plan_items(3))], "规划")])
    _run(monkeypatch, main, worker)

    p1_calls = [m for pid, _kw, m in worker.calls if pid == "P1"]
    blocked = [m for m in p1_calls[-1] if m.get("role") == "tool" and "已禁用" in str(m.get("content"))]
    assert blocked and "verify" in blocked[0]["content"]
    # 被拒的调用不执行不进台账:P1 脚本跑完(无剩余轮次)
    assert not worker.queues.get("P1")


def test_user_stop_during_fan_out_discards_all(monkeypatch):
    """扇出期间命中用户终止:部分结果整批丢弃(不落库、无信封),审查按 stopped 收尾"""
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
    main = _mk_main_stream([("", [_plan_tc(_plan_items(3))], "规划")])
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

    main = _mk_main_stream([("", [_plan_tc(_plan_items(3))], "规划")])
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
    main = _mk_main_stream([("", [_plan_tc(_plan_items(3))], "规划")])
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
        read_cap=5, max_iter=8, deadline=time.monotonic() - 1.0,
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
        read_cap=5, max_iter=8, deadline=time.monotonic() + 60,
        stop_check=lambda: True,
    )
    assert result.aborted is True
    assert result.pending_rows == []
