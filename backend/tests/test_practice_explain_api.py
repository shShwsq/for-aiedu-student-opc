"""知识点讲解相关接口的集成测试(独立 PostgreSQL schema)

覆盖:
- GET /practice/knowledge-points 下发讲解正文/来源/模型/可生成标记
- POST /practice/knowledge-points/explain 异步 job 与结果轮询(含无题素材的早退)
- GET /practice/generate/jobs 的 sources 过滤(explain job 不混进出题侧栏)
- PUT /practice/knowledge-points/{key}/explanation 手工编辑:source=manual、
  长度校验、清空回未生成态、同名 key 跨用户隔离,且之后的自动生成不覆盖它
- POST /practice/sessions/{id}/answers 只在答错时下发讲解
- GET /practice/questions/{id} 附带所属知识点讲解

无建 schema 权限(数据库不可用)时整体跳过,同 test_practice_api。
"""
import os
import time
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user

import app.models.user  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_preference  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.practice  # noqa: F401

from app.models.practice import (
    EXPLANATION_SOURCE_AUTO,
    EXPLANATION_SOURCE_MANUAL,
    KnowledgePoint,
    Question,
    QuestionStatus,
    QuestionType,
)
from app.models.task import Result, Task
from app.models.user import User
from app.routers import practice as practice_router
from app.services.practice.explainer import explain_knowledge_points

TEST_SCHEMA = f"pytest_explain_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = (
    "practice_attempts", "practice_sessions", "practice_questions",
    "user_knowledge_states", "knowledge_points", "learning_topics",
    "practice_settings", "results", "conversations", "task_artifacts",
    "tasks", "user_preferences", "user_git_bindings", "users",
)


@pytest.fixture(scope="session")
def test_engine():
    try:
        maint = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    # 重试几次:防上一用例的后台讲解线程未退出时 TRUNCATE 死锁
    for i in range(5):
        try:
            with test_engine.begin() as conn:
                for t in _TABLES:
                    conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
            break
        except Exception:
            if i == 4:
                raise
            time.sleep(0.3)
    yield


@pytest.fixture()
def db_factory(test_engine):
    return sessionmaker(bind=test_engine, autoflush=False)


def _reload_user(factory, uid):
    db = factory()
    try:
        return db.query(User).filter(User.id == uid).first()
    finally:
        db.close()


def _seed(
    monkeypatch, factory,
    email="explain-alice@test.local",
    with_explanation=False, n_questions=1, n_results=None,
):
    """建 user + task + n_results 条发现 + n_questions 个知识点(active 题各一道)

    返回 (client, kp_keys, user_id, result_ids, session_factory)。
    """
    db = factory()
    user = User(email=email, password_hash="x")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, user_input="audit")
    db.add(task)
    db.flush()
    result_ids = []
    for i in range(n_results if n_results is not None else n_questions):
        r = Result(
            task_id=task.id, title=f"发现{i}", content="直接拼接用户输入构造查询",
            metadata_={"cwe": f"CWE-{89 + i}", "severity": "high",
                       "learning_note": "记住参数化查询"},
        )
        db.add(r)
        db.flush()
        result_ids.append(r.id)
    keys = []
    for i in range(n_questions):
        key = f"CWE-{89 + i}"
        kp = KnowledgePoint(
            user_id=user.id, key=key, name=f"知识点{i}", category="cwe",
            learning_topic="security",
            explanation="### 是什么\n参数化查询" if with_explanation else None,
            explanation_source=EXPLANATION_SOURCE_AUTO if with_explanation else "",
            explanation_model="fake-model" if with_explanation else "",
        )
        db.add(kp)
        db.flush()
        db.add(Question(
            user_id=user.id, source_task_id=task.id,
            source_result_id=result_ids[i % len(result_ids)],
            knowledge_point_id=kp.id, qtype=QuestionType.SINGLE_CHOICE,
            stem=f"题干{i}", code_snippet="cursor.execute(sql)",
            options=["甲", "乙", "丙", "丁"], answer_idx=0,
            explanation="拼接导致注入", difficulty=1.5,
            status=QuestionStatus.ACTIVE, dedup_hash=uuid.uuid4().hex,
            learning_topic="security", origin="repo",
            source_file="src/a.py", source_lines="40-46",
        ))
        keys.append(key)
    db.commit()
    uid = user.id
    db.close()

    app = FastAPI()
    app.include_router(practice_router.router)
    # 讲解 job 在后台线程里用 SessionLocal 建会话,必须指向测试 schema
    monkeypatch.setattr(practice_router, "SessionLocal", factory)

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: _reload_user(factory, uid)
    return TestClient(app), keys, uid, result_ids, factory


def _wait_job(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/practice/generate/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("讲解 job 超时未完成")


# ============================================================
# 卡片字段
# ============================================================


def test_knowledge_points_card_carries_explanation_fields(monkeypatch, db_factory):
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, with_explanation=True, n_questions=2)
    cards = {c["knowledge_key"]: c for c in client.get("/practice/knowledge-points").json()}
    card = cards[keys[0]]
    assert card["explanation"].startswith("### 是什么")
    assert card["explanation_source"] == EXPLANATION_SOURCE_AUTO
    assert card["explanation_model"] == "fake-model"
    # 有入库题 → 有素材可依据,前端才亮「生成讲解」
    assert card["can_generate_explanation"] is True
    assert card["question_count"] == 1


def test_knowledge_points_card_without_explanation(monkeypatch, db_factory):
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, with_explanation=False)
    card = client.get("/practice/knowledge-points").json()[0]
    assert card["explanation"] == ""
    assert card["explanation_source"] == ""
    assert card["explanation_updated_at"] is None
    assert card["can_generate_explanation"] is True


# ============================================================
# 按需生成 job
# ============================================================


def test_explain_endpoint_runs_job(monkeypatch, db_factory):
    """讲解 job 走后台线程:done/total 语义为知识点数,讲解落库"""
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, n_questions=3)
    seen = {}

    def fake_explain(db, user_id, digests, *, client, force=False, **kw):
        seen["keys"] = sorted(digests)
        seen["force"] = force
        for key in digests:
            kp = db.query(KnowledgePoint).filter(
                KnowledgePoint.user_id == user_id, KnowledgePoint.key == key,
            ).first()
            kp.explanation = "### 是什么\n生成正文"
            kp.explanation_source = EXPLANATION_SOURCE_AUTO
            db.commit()
        return len(digests)

    monkeypatch.setattr(practice_router, "explain_knowledge_points", fake_explain)
    r = client.post("/practice/knowledge-points/explain", json={
        "knowledge_keys": keys[:2], "force": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2
    job = _wait_job(client, body["job_id"])
    assert job["status"] == "done", job
    assert job["done"] == 2 and job["total"] == 2
    assert seen["force"] is False and len(seen["keys"]) == 2
    # created_count 在 job 摘要端点上可见(轮询端点只给状态与结果)
    summary = client.get(
        "/practice/generate/jobs", params={"sources": "explain"}
    ).json()["jobs"][0]
    assert summary["created_count"] == 2

    cards = {c["knowledge_key"]: c for c in client.get("/practice/knowledge-points").json()}
    assert cards[keys[0]]["explanation"] == "### 是什么\n生成正文"


def test_explain_digests_carry_question_and_finding_context(monkeypatch, db_factory):
    """按需路径的素材也带出题上下文:题目 + 当初的审计发现原文"""
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, n_questions=1, n_results=2)
    captured = {}

    def fake_explain(db, user_id, digests, **kw):
        captured["digests"] = digests
        return 0

    monkeypatch.setattr(practice_router, "explain_knowledge_points", fake_explain)
    body = client.post(
        "/practice/knowledge-points/explain", json={"knowledge_keys": keys},
    ).json()
    _wait_job(client, body["job_id"])

    entry = captured["digests"][keys[0]]
    source = entry["sources"][0]
    assert source["finding_title"] == "发现0"
    assert source["metadata"]["learning_note"] == "记住参数化查询"
    q = source["questions"][0]
    assert q["stem"] == "题干0"
    assert q["material"] == "cursor.execute(sql)"
    assert q["answer"] == "甲"
    assert q["source"] == "src/a.py 40-46"


def test_explain_without_question_material_skips_llm(monkeypatch, db_factory):
    """没有题可依据 → 直接收口,不白跑一次 LLM(也不凭空编)"""
    client, keys, _, _, factory = _seed(monkeypatch, db_factory, n_questions=1)
    db = factory()
    try:
        db.query(Question).delete()
        db.commit()
    finally:
        db.close()

    called = []
    monkeypatch.setattr(
        practice_router, "explain_knowledge_points",
        lambda *a, **k: called.append(1) or 0,
    )
    body = client.post(
        "/practice/knowledge-points/explain", json={"knowledge_keys": keys},
    ).json()
    job = _wait_job(client, body["job_id"])
    assert job["status"] == "done"
    assert job["done"] == 0 and job["skipped_findings"] == 1
    assert called == []


def test_explain_jobs_are_filtered_out_of_generate_jobs_by_default(monkeypatch, db_factory):
    """出题侧栏只看 manual/auto;看板用 ?sources=explain 拿自己的 job"""
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, n_questions=1)
    monkeypatch.setattr(
        practice_router, "explain_knowledge_points",
        lambda db, user_id, digests, **kw: len(digests),
    )
    body = client.post(
        "/practice/knowledge-points/explain",
        json={"knowledge_keys": keys, "force": True},
    ).json()
    _wait_job(client, body["job_id"])

    default_jobs = client.get("/practice/generate/jobs").json()["jobs"]
    assert all(j["source"] != "explain" for j in default_jobs)
    explain_jobs = client.get(
        "/practice/generate/jobs", params={"sources": "explain"}
    ).json()["jobs"]
    assert len(explain_jobs) == 1
    assert explain_jobs[0]["status"] == "done"
    assert explain_jobs[0]["done"] == 1


def test_explain_endpoint_validates_input(monkeypatch, db_factory):
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, n_questions=1)
    # 空列表:Pydantic 直接 422
    assert client.post(
        "/practice/knowledge-points/explain", json={"knowledge_keys": []}
    ).status_code == 422
    # 不属于当前用户的 key:404
    assert client.post(
        "/practice/knowledge-points/explain", json={"knowledge_keys": ["不存在"]}
    ).status_code == 404
    # 混有合法 key 时按合法集合起 job
    r = client.post(
        "/practice/knowledge-points/explain",
        json={"knowledge_keys": [keys[0], "不存在"]},
    )
    assert r.status_code == 200 and r.json()["total"] == 1


# ============================================================
# 手工编辑
# ============================================================


def test_manual_edit_wins_over_auto_generation(monkeypatch, db_factory):
    client, keys, _, _, factory = _seed(monkeypatch, db_factory, n_questions=1)
    r = client.put(f"/practice/knowledge-points/{keys[0]}/explanation", json={
        "markdown": "### 我自己的总结\n先看调用点",
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["explanation_source"] == EXPLANATION_SOURCE_MANUAL
    assert out["explanation_updated_at"]
    card = client.get("/practice/knowledge-points").json()[0]
    assert card["explanation_source"] == EXPLANATION_SOURCE_MANUAL

    # 真实 explainer + force=True 也不覆盖 manual(且一次 LLM 都不发)
    db = factory()
    try:
        user_id = db.query(KnowledgePoint).filter(
            KnowledgePoint.key == keys[0]
        ).first().user_id
        digests = {keys[0]: {
            "knowledge_key": keys[0], "knowledge_name": "n",
            "learning_topic": "security", "sources": [],
        }}
        written = explain_knowledge_points(
            db, user_id, digests, client=MagicMock(), force=True,
        )
        assert written == 0
        kp = db.query(KnowledgePoint).filter(KnowledgePoint.key == keys[0]).first()
        assert kp.explanation == "### 我自己的总结\n先看调用点"
        assert kp.explanation_source == EXPLANATION_SOURCE_MANUAL
    finally:
        db.close()


def test_manual_edit_length_validation_and_clear(monkeypatch, db_factory):
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, with_explanation=True, n_questions=1)
    # 超长正文在入参校验层就拦掉(schema 与路由各有一道,以 schema 先响)
    assert client.put(
        f"/practice/knowledge-points/{keys[0]}/explanation",
        json={"markdown": "x" * 4001},
    ).status_code == 422
    # 清空正文 → 回到「尚未生成」态(source 归零,自动生成可接管)
    r = client.put(
        f"/practice/knowledge-points/{keys[0]}/explanation", json={"markdown": "   "}
    )
    assert r.status_code == 200
    out = r.json()
    assert out["explanation"] == "" and out["explanation_source"] == ""
    assert out["explanation_updated_at"] is None


def test_manual_edit_isolation_per_user(monkeypatch, db_factory):
    """同名 key 的知识点分属不同用户:改自己这份不波及别人"""
    client, keys, uid, _, factory = _seed(monkeypatch, db_factory, email="explain-carol@test.local")
    other_db = factory()
    try:
        other_user = User(email="explain-dave@test.local", password_hash="x")
        other_db.add(other_user)
        other_db.flush()
        other_kp = KnowledgePoint(
            user_id=other_user.id, key=keys[0], name="别人的同名知识点",
            category="cwe", learning_topic="security",
        )
        other_db.add(other_kp)
        other_db.commit()
        other_kp_id = other_kp.id
    finally:
        other_db.close()

    assert client.put(
        f"/practice/knowledge-points/{keys[0]}/explanation",
        json={"markdown": "### 只改自己这份"},
    ).status_code == 200

    check = factory()
    try:
        mine = check.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == uid, KnowledgePoint.key == keys[0]
        ).first()
        theirs = check.query(KnowledgePoint).filter(
            KnowledgePoint.id == other_kp_id
        ).first()
        assert mine.explanation == "### 只改自己这份"
        assert theirs.explanation is None
    finally:
        check.close()
    # 自己没有的 key → 404
    assert client.put(
        "/practice/knowledge-points/nope-xyz/explanation", json={"markdown": "x"}
    ).status_code == 404


# ============================================================
# 答题反馈与题库详情
# ============================================================


def _start_one_question_session(client):
    """组一卷,返回 (session_id, question_id)

    组卷端点不下发答案(answer_idx 前端本就不该知道),
    本题用种子数据里已知的 answer_idx=0 判定对错。
    """
    session = client.post("/practice/sessions", json={"count": 1}).json()
    return session["session_id"], session["questions"][0]["id"]


def test_submit_answer_returns_explanation_only_when_wrong(monkeypatch, db_factory):
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, with_explanation=True, n_questions=1)
    sid, qid = _start_one_question_session(client)
    r_wrong = client.post(f"/practice/sessions/{sid}/answers", json={
        "question_id": qid, "chosen_idx": 1,  # 种子题正确项是 0 → 本题答错
    }).json()
    assert r_wrong["is_correct"] is False
    assert r_wrong["knowledge_explanation"].startswith("### 是什么")

    # 答对:解析已够,不必每题都拖一段正文
    sid2, qid2 = _start_one_question_session(client)
    r_right = client.post(f"/practice/sessions/{sid2}/answers", json={
        "question_id": qid2, "chosen_idx": 0,
    }).json()
    assert r_right["is_correct"] is True
    assert r_right["knowledge_explanation"] == ""


def test_question_detail_includes_knowledge_explanation(monkeypatch, db_factory):
    client, keys, _, _, _ = _seed(monkeypatch, db_factory, with_explanation=True, n_questions=1)
    items = client.get("/practice/questions", params={"status": "active"}).json()
    detail = client.get(f"/practice/questions/{items[0]['id']}").json()
    assert detail["knowledge_explanation"].startswith("### 是什么")
    assert detail["knowledge_explanation_source"] == EXPLANATION_SOURCE_AUTO
