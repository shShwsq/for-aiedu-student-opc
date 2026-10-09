"""agent2 结构化发射(四 submit_* 工具)集成测试 — 实施计划 Part C

覆盖:
- submit_review_item → 落 ReviewItem + 推 review_item_add + 回执含 review_item_id/confidence;
  三态分桶计数正确
- submit_knowledge_point → 落 Result(知识点)+ knowledge_point_add + 回执含 result_id
- 发射硬上限(第 16 条 submit_review_item 拒收)
- 本轮首条幂等清本轮
- 循环退出写 task.params._review + 派生 grouping + 返回紧凑汇总(_emitted)
- submit_review_plan 覆盖式 + 规划对账回填(计划 3 发射匹配 2 → 回填 1)
- agent1_ref 校验(伪造非本任务 UUID 置空,不拒收)
用 MagicMock 承接 db/task(落库断言看 db.add 调用;publish 用替身收集)。
"""
import json
import uuid
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  注册 TaskArtifact,Task mapper 才初始化
import app.models.audit  # noqa: F401  注册 ReviewItem,Task.review_items 关系可配置
import app.agents.agent2 as agent2
from app.agents.agent2 import run_agent2
from app.models.audit import ReviewItem
from app.models.task import Result, Task


def _mk_task():
    task = MagicMock()
    task.id = uuid.uuid4()
    task.verifier_enabled = False
    task.test_env_url = ""
    task.params = {}  # 真实 dict,便于 _finalize_emitted 写 _review/_grouping
    return task


def _tc(name, args, idx=0, call_id=None):
    return {
        "id": call_id or f"call_{idx}",
        "index": idx,
        "name": name,
        "arguments_str": json.dumps(args, ensure_ascii=False),
    }


def _driver(monkeypatch, turns):
    """turns: 每个元素为 list[tool_call] ;全部用完后返回无 tool_calls 结束循环。

    返回 (run 结果, publish 事件列表 [(type, data)])。
    """
    state = {"n": 0}
    events: list[tuple[str, dict]] = []

    def _fake_publish(task_id, etype, data):
        events.append((etype, data))

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        i = state["n"]
        state["n"] += 1
        if i < len(turns):
            return ("", turns[i], f"思考 {i}")
        # 结束:不再发工具(content 空,emit 路径不解析大 JSON)
        return ("", [], "")

    monkeypatch.setattr(agent2, "publish", _fake_publish)
    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    task = _mk_task()
    db = MagicMock()
    result = run_agent2(
        "审查这个仓库",
        [{"round": 1, "summary": "agent1 总结", "conversation_id": str(uuid.uuid4())}],
        task_id=str(task.id), db=db, round_idx=1,
        client=MagicMock(), task=task,
    )
    return result, events, db, task


# ============================================================
# 三态发射 + 落库 + 事件 + 回执
# ============================================================

def test_three_states_emit_and_counts(monkeypatch):
    risk = _tc("submit_review_item", {
        "title": "SQL 注入", "review_target": "users.py 注入", "origin": "agent1_claim",
        "status": "covered", "verdict": "confirmed", "severity": "high",
    })
    cleared = _tc("submit_review_item", {
        "title": "已核查无问题", "review_target": "auth 逻辑", "origin": "agent1_claim",
        "status": "covered", "verdict": "none",
    })
    gap = _tc("submit_review_item", {
        "title": "缺并发审查", "review_target": "并发安全", "origin": "domain_baseline",
        "status": "missing", "verdict": "pending",
    })
    result, events, db, _task = _driver(monkeypatch, [[risk, cleared, gap]])

    assert result["_emitted"] is True
    assert result["item_count"] == 3
    review = result["_review"]
    assert review["counts"] == {"total": 3, "risk": 1, "cleared": 1, "gap": 1}

    # 落库:3 次 db.add(ReviewItem)
    added_items = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], ReviewItem)
    ]
    assert len(added_items) == 3

    # 事件:3 条 review_item_add,首条幂等清本轮(只清一次)
    assert [t for t, _ in events].count("review_item_add") == 3

    # 无 evidence → 后端不派生 confidence(仅带证据者有)
    for _t, data in events:
        if _t == "review_item_add":
            assert data["confidence"] is None


# ============================================================
# 知识点发射 + source_review_item_id 回指(用回执 id)
# ============================================================

def test_knowledge_point_emit_and_link(monkeypatch):
    item = _tc("submit_review_item", {
        "title": "注入", "review_target": "users.py", "origin": "agent1_claim",
        "status": "covered", "verdict": "confirmed", "severity": "high",
    }, call_id="c_item")
    # 知识点用审查项回执 id 回指(需先拿到 item 的 id → 这里直接从事件里取)
    kp = _tc("submit_knowledge_point", {
        "title": "参数化查询", "content": "用占位符", "learning_note": "防注入基本功",
        "practice_worthy": True,
    }, idx=1, call_id="c_kp")
    result, events, db, _task = _driver(monkeypatch, [[item], [kp]])

    assert result["kp_count"] == 1
    added_results = [
        c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], Result)
    ]
    assert len(added_results) == 1
    kp_events = [d for t, d in events if t == "knowledge_point_add"]
    assert kp_events and kp_events[0]["metadata_"]["learning_note"] == "防注入基本功"
    # review 聚合里 knowledge_point_count=1
    assert result["_review"]["knowledge_point_count"] == 1


# ============================================================
# 发射硬上限:第 16 条 submit_review_item 拒收
# ============================================================

def test_review_item_cap_rejects(monkeypatch):
    # 16 条审查项分 16 轮发射(每轮 1 条),第 16 条应被拒(上限 15)
    turns = []
    for i in range(16):
        turns.append([_tc("submit_review_item", {
            "title": f"item{i}", "review_target": f"t{i}", "origin": "agent1_claim",
            "status": "covered", "verdict": "confirmed",
        }, call_id=f"c{i}")])
    result, events, db, _task = _driver(monkeypatch, turns)

    assert result["item_count"] == 15
    added_items = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ReviewItem)]
    assert len(added_items) == 15


# ============================================================
# 规划对账:计划 3,发射匹配 2 → 回填 1 条 missing;review_plan_update 覆盖式
# ============================================================

def test_plan_reconciliation_backfill(monkeypatch):
    plan = _tc("submit_review_plan", {"items": [
        {"target": "users.py SQL 注入", "origin": "agent1_claim", "priority": 1},
        {"target": "auth 越权", "origin": "agent1_claim", "priority": 2},
        {"target": "并发安全(未碰)", "origin": "domain_baseline", "priority": 3},
    ]})
    a = _tc("submit_review_item", {
        "title": "注入确认", "review_target": "users.py SQL 注入风险", "origin": "agent1_claim",
        "status": "covered", "verdict": "confirmed",
    }, idx=1, call_id="c_a")
    b = _tc("submit_review_item", {
        "title": "越权已核查", "review_target": "auth 越权", "origin": "agent1_claim",
        "status": "covered", "verdict": "none",
    }, idx=2, call_id="c_b")
    result, events, _db, _task = _driver(monkeypatch, [[plan], [a], [b]])

    review = result["_review"]
    assert review["plan"]["planned"] == 3
    assert review["plan"]["executed"] == 2
    assert review["plan"]["backfilled_gap"] == 1
    # 回填项计入 total(2 发射 + 1 回填 = 3),且回填为 gap
    assert review["counts"]["total"] == 3
    assert review["counts"]["gap"] == 1
    # 计划覆盖式:只 1 条 review_plan_update 事件
    assert [t for t, _ in events].count("review_plan_update") == 1


# ============================================================
# agent1_ref 校验:非本任务 UUID 置空(不拒收)
# ============================================================

def test_agent1_ref_invalid_nullified(monkeypatch):
    fake = str(uuid.uuid4())  # 不在 agent1_summaries 的 conversation_id 集里
    item = _tc("submit_review_item", {
        "title": "注入", "review_target": "users.py", "origin": "agent1_claim",
        "agent1_ref": fake, "status": "covered", "verdict": "confirmed",
    })
    result, events, _db, _task = _driver(monkeypatch, [[item]])
    ev = [d for t, d in events if t == "review_item_add"][0]
    assert ev["agent1_ref"] is None


# ============================================================
# 循环退出写 task.params._review + 派生 grouping
# ============================================================

def test_finalize_writes_params_and_grouping(monkeypatch):
    a = _tc("submit_review_item", {
        "title": "高危注入", "review_target": "users.py", "origin": "agent1_claim",
        "status": "covered", "verdict": "confirmed", "severity": "high",
    })
    b = _tc("submit_review_item", {
        "title": "低危日志", "review_target": "log.py", "origin": "agent1_claim",
        "status": "covered", "verdict": "confirmed", "severity": "low",
    }, idx=1, call_id="c_b")
    result, _events, _db, task = _driver(monkeypatch, [[a, b]])

    assert task.params.get("_review") == result["_review"]
    grouping = task.params.get("_grouping")
    assert grouping is not None and grouping["field"] == "severity"
    # ordered severity 值:high order < low order
    vals = {v["value"]: v["order"] for v in grouping["values"]}
    assert vals["high"] < vals["low"]


# ============================================================
# 两条 submit_review_item 后终止:ReviewItem 保留、stopped 不触发聚合下游
# ============================================================

def test_partial_commit_preserves_items_on_stop(monkeypatch):
    a = _tc("submit_review_item", {
        "title": "注入", "review_target": "users.py", "origin": "agent1_claim",
        "status": "covered", "verdict": "confirmed",
    })
    b = _tc("submit_review_item", {
        "title": "越权", "review_target": "auth", "origin": "agent1_claim",
        "status": "covered", "verdict": "suspected",
    }, idx=1, call_id="c_b")

    state = {"n": 0}

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None, stop_check=None):
        state["n"] += 1
        if state["n"] == 1:
            return ("", [a, b], "发射两条")
        return ("", [], "")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)
    monkeypatch.setattr(agent2, "publish", lambda *a, **k: None)

    # 两条发射完成后,下一次循环顶 stop_check 命中
    stop_state = {"stop": False}

    def _stop_check():
        # 第一轮工具跑完(两条已落库)后开始终止
        return state["n"] >= 2 or stop_state["stop"]

    task = _mk_task()
    db = MagicMock()
    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "s"}],
        task_id=str(task.id), db=db, round_idx=1,
        client=MagicMock(), task=task, stop_check=_stop_check,
    )

    assert result.get("stopped") is True
    added_items = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ReviewItem)]
    assert len(added_items) == 2          # 已发射的两条保留
    assert "_review" not in result        # 终止不聚合下游


