"""任务侧驱动的看板讲解同步(sync_knowledge_explanations)单元测试

覆盖(不依赖数据库:按模型分派的假 session + fake LLM client):
- 规则 1 单任务采用:content 直落 explanation,model=task_side,零 LLM 调用
- 规则 2 跨任务合并:一次 LLM 调用,双任务 id 都记入贡献集合;client 缺失时留待下次
- 规则 3 无任务来源:no_source_keys 上报,不动库不发 LLM
- 幂等:贡献集合未变且已有讲解 → 跳过;新增贡献任务 → 触发动作
- manual 守卫:内存预筛 + 条件 UPDATE rowcount=0(并发手编)两层
- 素材识别:临时汇总行(metadata=None)不当知识点;同任务多条取最新;
  链接列 NULL 的老数据回落 Question.source_result_id
- 合并分批:>MAX_KP_PER_BATCH 个跨任务 KP 分多批,不逐 KP 往返
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import app.models.task_artifact  # noqa: F401  让 Task mapper 能解析 TaskArtifact 关联
import app.services.practice.explainer as ex
from app.models.practice import (
    EXPLANATION_SOURCE_AUTO,
    EXPLANATION_SOURCE_MANUAL,
    KnowledgePoint,
)
from app.models.task import Result, Task
from app.services.practice.explainer import (
    _is_knowledge_result,
    _task_contributions,
    sync_knowledge_explanations,
)


def _kp(kp_id, key="CWE-89", name="SQL 注入", explanation=None, source="",
        contributors=None):
    kp = KnowledgePoint(
        user_id="u1", key=key, name=name, learning_topic="security",
        explanation=explanation, explanation_source=source,
    )
    kp.id = kp_id
    # transient 对象未走 flush 默认值,显式给迁移后的库同款空列表
    kp.explanation_contributor_tasks = contributors if contributors is not None else []
    return kp


def _result(task_id, content, kp_id=None, meta="kp"):
    """一条任务侧 Result;meta='kp' 为知识点行,'interim' 为 agent1 汇总占位"""
    r = SimpleNamespace(
        id=str(uuid4()), task_id=task_id, content=content,
        knowledge_point_id=kp_id, metadata_=None,
    )
    if meta == "kp":
        r.metadata_ = {"learning_note": "值得记", "practice_worthy": True,
                       "learning_topic": "security"}
    return r


def _task(task_id, title="仓库审计任务"):
    return SimpleNamespace(id=task_id, title=title, user_input="x" * 200)


class _RowsQuery:
    """按模型分派的假 query:filter/order_by 透传,all() 返回预置行"""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)


class _SeqQuery:
    """同模型多次查询返回不同结果集(链接命中/回落兜底的分派靠调用次序)"""

    def __init__(self, batches):
        self._batches = list(batches)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        # 多于一片时按次序消费(链接查询→回落查询交替);最后一片粘住,
        # 保证同一 db 被多次 sync 复用时不会空尽
        if len(self._batches) > 1:
            return list(self._batches.pop(0))
        return list(self._batches[-1]) if self._batches else []


def _fake_db(kps, results, question_rids, tasks, exec_rowcount=1):
    """results 传 list(每模型一批)或 dict(model → [批次列表]);返回 (db, 执行计数)"""
    db = MagicMock()
    state = {"execute_count": 0}
    result_batches = results if isinstance(results, dict) else {"any": [results]}

    def query(target):
        if target is KnowledgePoint:
            return _RowsQuery(kps)
        if target is Task:
            return _RowsQuery(tasks)
        if target is Result:
            batches = result_batches.get("any") or result_batches.get(id(target))
            seq = getattr(query, "_result_seq", None)
            if seq is None:
                seq = _SeqQuery(batches)
                query._result_seq = seq
            return seq
        # Question.source_result_id 列查询:返回 (rid,) 元组列表
        return _RowsQuery(question_rids)
    db.query.side_effect = query
    query._result_seq = None

    def execute(stmt):
        state["execute_count"] += 1
        res = MagicMock()
        res.rowcount = exec_rowcount
        return res
    db.execute.side_effect = execute
    return db, state


class _ExplodingClient:
    """采用路径的守门员:任何 LLM 调用都是回归"""
    model = "should-not-be-called"

    def chat_stream(self, messages, **kw):
        raise AssertionError("单任务采用不应调用 LLM")


def _EchoClient(outputs):
    """按调用次序返回预设文本,并记录 messages(同 test_practice_explainer 风格)"""
    calls = []

    class C:
        model = "fake-merge-model"

        @staticmethod
        def chat_stream(messages, **kw):
            calls.append(messages)
            text = outputs.pop(0) if outputs else "[]"
            return iter([SimpleNamespace(content_delta=text, reasoning_delta="",
                                         tool_call_deltas=None, finish_reason="stop")])
    c = C()
    c.calls = calls
    return c


# ============================================================
# 素材识别与贡献任务
# ============================================================


def test_is_knowledge_result_excludes_interim_summary():
    """agent1 汇总占位行(metadata=None)不是知识点,不能被当成讲解素材"""
    assert _is_knowledge_result(_result(uuid4(), "x")) is True
    assert _is_knowledge_result(_result(uuid4(), "x", meta="interim")) is False


def test_task_contributions_latest_wins_and_title_from_task():
    t1 = uuid4()
    kp_id = uuid4()
    r_early = _result(t1, "### 是什么\n旧版讲解", kp_id=kp_id)
    r_late = _result(t1, "### 是什么\n新版讲解", kp_id=kp_id)
    db, _ = _fake_db([_kp(kp_id)], [r_early, r_late], [], [_task(t1, "任务甲")])
    contribs = _task_contributions(db, _kp(kp_id))
    assert len(contribs) == 1
    # 同任务多条(跨轮)取 created_at 最新一条
    assert contribs[0]["explanation"] == "### 是什么\n新版讲解"
    assert contribs[0]["task_title"] == "任务甲"


def test_task_contributions_falls_back_to_question_source_result():
    """老数据 results.knowledge_point_id 为 NULL → 回落题目回链取素材"""
    t1 = uuid4()
    kp_id = uuid4()
    r = _result(t1, "### 是什么\n题干回链讲解", kp_id=None)
    # 第一次查链接列返回空,第二次按 rids 返回该行
    db, _ = _fake_db([_kp(kp_id)], {"any": [[], [r]]}, [(r.id,)], [_task(t1)])
    contribs = _task_contributions(db, _kp(kp_id))
    assert contribs and contribs[0]["task_id"] == str(t1)


# ============================================================
# 规则 1:单任务采用
# ============================================================


def test_sync_single_task_adopts_content_without_llm():
    t1 = uuid4()
    kp_id = uuid4()
    kp = _kp(kp_id)
    r = _result(t1, "### 是什么\n### 实战要点", kp_id=kp_id)
    db, state = _fake_db([kp], [r], [], [_task(t1)])
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=_ExplodingClient())
    assert stats["adopted"] == 1
    assert kp.explanation.startswith("### 是什么")
    assert kp.explanation_source == EXPLANATION_SOURCE_AUTO
    # 采用未经模型:explanation_model 记 task_side,贡献集合已落库
    assert kp.explanation_model == ex._ADOPTED_MODEL
    assert kp.explanation_contributor_tasks == [str(t1)]
    assert state["execute_count"] == 1
    db.commit.assert_called_once()


def test_sync_idempotent_when_contributors_unchanged():
    t1 = uuid4()
    kp_id = uuid4()
    kp = _kp(kp_id, explanation="### 是什么\n已采用", source=EXPLANATION_SOURCE_AUTO,
             contributors=[str(t1)])
    r = _result(t1, "### 是什么\n任务侧正文", kp_id=kp_id)
    db, state = _fake_db([kp], [r], [], [_task(t1)])
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=_ExplodingClient())
    assert stats["skipped"] == 1 and stats["adopted"] == 0
    assert state["execute_count"] == 0
    # force=True 才重写(看板「更新讲解」语义)
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=_ExplodingClient(), force=True)
    assert stats["adopted"] == 1


def test_sync_new_contributor_triggers_regeneration():
    """第二个任务加入贡献集合 → 幂等判定失效,跨任务合并被触发"""
    t1, t2 = uuid4(), uuid4()
    kp_id = uuid4()
    kp = _kp(kp_id, explanation="### 是什么\n上次的合并版", source=EXPLANATION_SOURCE_AUTO,
             contributors=[str(t1)])
    r1 = _result(t1, "讲解一", kp_id=kp_id)
    r2 = _result(t2, "讲解二", kp_id=kp_id)
    out = json.dumps([{"knowledge_key": "CWE-89", "markdown": "### 是什么\n新合并版"}],
                     ensure_ascii=False)
    client = _EchoClient([out])
    db, _ = _fake_db([kp], [r1, r2], [], [_task(t1), _task(t2, "追问轮任务")])
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=client)
    assert stats["merged"] == 1
    assert kp.explanation == "### 是什么\n新合并版"
    assert kp.explanation_model == "fake-merge-model"
    assert set(kp.explanation_contributor_tasks) == {str(t1), str(t2)}


# ============================================================
# 规则 2:跨任务合并
# ============================================================


def test_sync_cross_task_merge_uses_task_titles_as_material():
    t1, t2 = uuid4(), uuid4()
    kp_id = uuid4()
    kp = _kp(kp_id)
    r1 = _result(t1, "任务一视角的讲解", kp_id=kp_id)
    r2 = _result(t2, "任务二视角的讲解", kp_id=kp_id)
    out = json.dumps([{"knowledge_key": "CWE-89", "markdown": "### 是什么\n合并版"}],
                     ensure_ascii=False)
    events = []
    client = _EchoClient([out])
    db, _ = _fake_db([kp], [r1, r2], [], [_task(t1, "登录安全审计"), _task(t2, "依赖升级复查")])
    stats = sync_knowledge_explanations(
        db, "u1", [kp_id], client=client,
        event_callback=lambda etype, data: events.append((etype, data["phase"])),
    )
    assert stats["merged"] == 1
    assert kp.explanation == "### 是什么\n合并版"
    # 合并素材带任务标题与各任务正文(模型才能去重归纳)
    user_prompt = client.calls[0][1]["content"]
    assert "登录安全审计" in user_prompt and "依赖升级复查" in user_prompt
    assert "任务一视角的讲解" in user_prompt
    # explain 阶段事件照常推送(看板 job 进度复用)
    assert ("explain", "start") in events and ("explain", "done") in events


def test_sync_merge_without_client_defers():
    t1, t2 = uuid4(), uuid4()
    kp_id = uuid4()
    kp = _kp(kp_id)
    r1 = _result(t1, "讲解一", kp_id=kp_id)
    r2 = _result(t1, "讲解二", kp_id=kp_id)
    r2.task_id = t2
    db, state = _fake_db([kp], [r1, r2], [], [_task(t1), _task(t2)])
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=None)
    assert stats["merged"] == 0 and stats["skipped"] == 1
    assert state["execute_count"] == 0
    # 未落贡献集合:下次同步仍会重试合并
    assert kp.explanation_contributor_tasks == []


def test_sync_merge_batches_per_max_kp():
    """7 个跨任务 KP 分 2 批(≤MAX_KP_PER_BATCH/批),不逐 KP 往返"""
    keys = [f"CWE-{i}" for i in range(7)]
    kps, results, tasks = [], [], []
    for i, key in enumerate(keys):
        kp_id = uuid4()
        t1, t2 = uuid4(), uuid4()
        kps.append(_kp(kp_id, key=key))
        results.append(_result(t1, f"{key} 讲解一", kp_id=kp_id))
        results.append(_result(t2, f"{key} 讲解二", kp_id=kp_id))
        tasks += [_task(t1, f"任务{i}A"), _task(t2, f"任务{i}B")]

    calls = []

    def fake_stream(messages, **kw):
        calls.append(messages)
        payload = json.loads(messages[1]["content"].split(":\n")[1].rsplit("\n请", 1)[0])
        text = json.dumps(
            [{"knowledge_key": it["knowledge_key"], "markdown": "### 是什么\n合并"}
             for it in payload], ensure_ascii=False,
        )
        return iter([SimpleNamespace(content_delta=text, reasoning_delta="",
                                     tool_call_deltas=None, finish_reason="stop")])
    client = _EchoClient([])
    client.chat_stream = fake_stream
    db, _ = _fake_db(kps, results, [], tasks)
    stats = sync_knowledge_explanations(
        db, "u1", [kp.id for kp in kps], client=client,
    )
    assert stats["merged"] == 7
    assert len(calls) == 2  # 7 = 6 + 1,两批
    assert all(kp.explanation == "### 是什么\n合并" for kp in kps)


# ============================================================
# 规则 3 与覆盖守卫
# ============================================================


def test_sync_no_task_source_reports_keys_without_llm():
    """对话降级伪发现(无 Result 行)→ no_source_keys 上报,交给按需路径"""
    kp_id = uuid4()
    kp = _kp(kp_id)
    db, state = _fake_db([kp], [], [], [])
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=_ExplodingClient())
    assert stats["no_source_keys"] == ["CWE-89"] and stats["skipped"] == 1
    assert state["execute_count"] == 0


def test_sync_never_overwrites_manual():
    kp_id = uuid4()
    kp = _kp(kp_id, explanation="我的总结", source=EXPLANATION_SOURCE_MANUAL)
    r = _result(uuid4(), "任务侧讲解", kp_id=kp_id)
    db, state = _fake_db([kp], [r], [], [_task(r.task_id)])
    stats = sync_knowledge_explanations(
        db, "u1", [kp_id], client=_ExplodingClient(), force=True,
    )
    assert stats["skipped"] == 1 and state["execute_count"] == 0
    assert kp.explanation == "我的总结"


def test_sync_conditional_update_blocks_concurrent_manual():
    """采用期间用户并发 PUT manual → rowcount=0,讲解与贡献集合都不落"""
    t1 = uuid4()
    kp_id = uuid4()
    kp = _kp(kp_id)
    r = _result(t1, "任务侧讲解", kp_id=kp_id)
    db, state = _fake_db([kp], [r], [], [_task(t1)], exec_rowcount=0)
    stats = sync_knowledge_explanations(db, "u1", [kp_id], client=_ExplodingClient())
    assert stats["adopted"] == 0 and stats["skipped"] == 1
    assert kp.explanation is None
    assert kp.explanation_contributor_tasks == []
    assert state["execute_count"] == 1


# ============================================================
# 出题收尾 hook(generator._sync_explanations_after_generate)
# ============================================================


def test_after_generate_hook_collects_kp_ids_and_client(monkeypatch):
    """去重收集本轮题目的 KP,讲解槽位模型可用则带上(供跨任务合并)"""
    import app.services.practice.generator as gen

    called = {}

    def fake_sync(db, user_id, kp_ids, **kw):
        called["kp_ids"] = list(kp_ids)
        called["client"] = kw.get("client")
        return {"adopted": 0, "merged": 0, "skipped": 0, "no_source_keys": []}

    monkeypatch.setattr(ex, "sync_knowledge_explanations", fake_sync)
    monkeypatch.setattr(
        gen, "resolve_explain_client_for_user", lambda db, uid, s: "MERGE-CLIENT",
    )
    created = [
        SimpleNamespace(knowledge_point_id="k1"),
        SimpleNamespace(knowledge_point_id="k1"),  # 同 KP 多题 → 去重
        SimpleNamespace(knowledge_point_id=None),   # 理论上不出现,防御跳过
    ]
    gen._sync_explanations_after_generate(MagicMock(), "u1", created, None, None)
    assert called["kp_ids"] == ["k1"]
    assert called["client"] == "MERGE-CLIENT"


def test_after_generate_hook_survives_client_resolve_failure(monkeypatch):
    """模型解析失败不阻断:采用照常(client=None 时 sync 内部将合并留待下次)"""
    import app.services.practice.generator as gen

    called = {}

    def fake_sync(db, user_id, kp_ids, **kw):
        called["client"] = kw.get("client")
        return {"adopted": 0, "merged": 0, "skipped": 0, "no_source_keys": []}

    def boom(*a, **k):
        raise RuntimeError("无可用模型")

    monkeypatch.setattr(ex, "sync_knowledge_explanations", fake_sync)
    monkeypatch.setattr(gen, "resolve_explain_client_for_user", boom)
    gen._sync_explanations_after_generate(
        MagicMock(), "u1", [SimpleNamespace(knowledge_point_id="k1")], None, None,
    )
    assert called["client"] is None


def test_after_generate_hook_skips_when_no_kp(monkeypatch):
    """本轮未命中任何 KP → 一次 sync 都不起(对话降级全伪发现场景)"""
    import app.services.practice.generator as gen

    def never(*a, **k):
        raise AssertionError("不应调用 sync")

    monkeypatch.setattr(ex, "sync_knowledge_explanations", never)
    gen._sync_explanations_after_generate(
        MagicMock(), "u1", [SimpleNamespace(knowledge_point_id=None)], None, None,
    )
