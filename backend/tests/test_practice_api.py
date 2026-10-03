"""练习模块路由集成测试(独立 PostgreSQL schema + 确定性 fake 生成器)

覆盖完整链路:generate(异步 job) → drafts → confirm → sessions → answers → stats,
外加 activate / summary / trend / 历史会话 / 错题过滤 / 越权隔离 / 鉴权。

在配置的数据库内建独立 schema(进程唯一:pytest_practice_{pid}_{rand},
会话级 drop/create),不污染开发数据、并发测试进程互不干扰;
无建 schema 权限时跳过。
"""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user

# 确保相关模型全部注册到 Base.metadata(create_all 才能建全表;
# User.git_bindings 为 selectin 关系,refresh 时会触发,必须一并导入)
import app.models.user  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_preference  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.practice  # noqa: F401

from app.models.practice import (
    KnowledgePoint,
    LearningTopic,
    Question,
    QuestionStatus,
    QuestionType,
    UserKnowledgeState,
    ensure_user_topics,
)
from app.models.task import Result, Task
from app.models.user import User
from app.routers import learning_topics as learning_topics_router
from app.routers import practice as practice_router

# schema 名含 PID + 随机后缀:并发 pytest 进程(多会话/前后台任务)各自
# 独立建 schema,session 开始的 DROP SCHEMA 不会误删他人正在用的 schema
TEST_SCHEMA = f"pytest_practice_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = (
    "practice_attempts", "practice_sessions", "practice_questions",
    "user_knowledge_states", "knowledge_points", "learning_topics",
    "results", "conversations", "task_artifacts", "tasks", "user_preferences", "user_git_bindings", "users",
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
        pass  # 清理失败不影响测试结果


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    # 重试几次:防上一用例的后台出题线程尚未退出时 TRUNCATE 死锁
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


# 确定性 fake 生成器:每条 Result 出 1 题(难度 1.5 保证冷启动可选题)
def _fake_generate(db, task, user_id, max_findings=10, client=None,
                   progress_callback=None, event_callback=None):
    findings = db.query(Result).filter(Result.task_id == task.id).all()[:max_findings]
    created = []
    for i, r in enumerate(findings):
        if event_callback:
            event_callback("finding", {"index": i + 1, "total": len(findings), "title": r.title})
            event_callback("token", {"delta": f"fake-output-{i}"})
        kp = KnowledgePoint(
            user_id=user_id, key=f"CWE-{89 + i}", name=f"知识点{i}", category="cwe"
        )
        db.add(kp)
        db.flush()
        q = Question(
            user_id=user_id,
            source_task_id=task.id,
            source_result_id=r.id,
            knowledge_point_id=kp.id,
            qtype=QuestionType.SINGLE_CHOICE,
            stem=f"题干{i}",
            options=["甲", "乙", "丙", "丁"],
            answer_idx=0,
            explanation="解析",
            difficulty=1.5,
            status=QuestionStatus.DRAFT,
            dedup_hash=uuid.uuid4().hex,
        )
        db.add(q)
        created.append(q)
        if progress_callback:
            progress_callback(i + 1, len(findings))
    db.commit()
    for q in created:
        db.refresh(q)
    return created, 0


def _wait_job(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/practice/generate/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("生成 job 超时未完成")


class Ctx:
    """一个用户的测试上下文:app/client + 库内 user/task/results"""

    def __init__(self, session_factory: sessionmaker, email: str, n_results: int):
        self.session_factory = session_factory
        self.app = FastAPI()
        self.app.include_router(practice_router.router)
        self.app.include_router(learning_topics_router.router)
        db = session_factory()
        self.user = User(email=email, password_hash="x")
        db.add(self.user)
        db.flush()
        self.task = Task(user_id=self.user.id, user_input="audit")
        db.add(self.task)
        db.flush()
        for i in range(n_results):
            db.add(Result(
                task_id=self.task.id, title=f"发现{i}", content="c",
                metadata_={"cwe": f"CWE-{89 + i}", "severity": "high"},
            ))
        db.commit()
        db.refresh(self.user)
        db.refresh(self.task)
        db.close()

        def override_get_db():
            s = session_factory()
            try:
                yield s
            finally:
                s.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app)

    def login(self):
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        return self

    def logout(self):
        self.app.dependency_overrides.pop(get_current_user, None)
        return self


@pytest.fixture()
def ctx(test_engine, monkeypatch):
    factory = sessionmaker(bind=test_engine, autoflush=False)
    # 后台出题线程与路由层都替换为 fake 生成器 + 测试库会话
    monkeypatch.setattr(practice_router, "SessionLocal", factory)
    monkeypatch.setattr(
        practice_router, "generate_questions_for_task", _fake_generate
    )
    return Ctx(factory, "alice@test.local", n_results=3)


@pytest.fixture()
def ctx_b(test_engine):
    factory = sessionmaker(bind=test_engine, autoflush=False)
    return Ctx(factory, "bob@test.local", n_results=1).login()


def _generate_and_confirm(ctx) -> list[dict]:
    """辅助:走完 generate → confirm,返回入库题目"""
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    assert r.status_code == 200
    job = _wait_job(ctx.client, r.json()["job_id"])
    assert job["status"] == "done" and len(job["questions"]) == 3
    ids = [q["id"] for q in job["questions"]]
    r = ctx.client.post("/practice/questions/confirm", json={
        "task_id": str(ctx.task.id), "question_ids": ids,
    })
    assert r.status_code == 200 and r.json()["confirmed"] == 3
    return job["questions"]


# ============================================================
# 鉴权
# ============================================================


def test_unauthenticated_returns_401(ctx):
    ctx.logout()
    assert ctx.client.get("/practice/stats").status_code == 401
    assert ctx.client.get("/practice/summary").status_code == 401
    assert ctx.client.delete("/practice/records").status_code == 401


# ============================================================
# 出题模型解析(任务详情页「本次出题将使用」展示)
# ============================================================


def test_generate_model_info_env_default(ctx):
    """任务无自带配置、无练习设置时,返回 env 默认模型(source=env)"""
    ctx.login()
    r = ctx.client.get(f"/practice/tasks/{ctx.task.id}/generate-model")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "env"
    assert body["model"] == settings.LLM_MODEL


def test_generate_model_info_requires_login_and_ownership(ctx, ctx_b):
    """未登录 401;他人任务 404(与出题接口同一归属校验)"""
    ctx.logout()
    assert (
        ctx.client.get(f"/practice/tasks/{ctx.task.id}/generate-model").status_code
        == 401
    )
    ctx.login()
    assert (
        ctx.client.get(f"/practice/tasks/{ctx_b.task.id}/generate-model").status_code
        == 404
    )


# ============================================================
# 生成 → 确认 → 组卷 → 答题 → 统计 完整链路
# ============================================================


def test_generate_requires_results(ctx_b):
    r = ctx_b.client.post("/practice/generate", json={
        "task_id": str(uuid.uuid4()),
    })
    assert r.status_code == 404  # 任务不存在


def test_full_chain(ctx):
    ctx.login()
    questions = _generate_and_confirm(ctx)

    # drafts 已清空
    assert ctx.client.get("/practice/drafts").json() == []

    # 组卷(冷启动:全为低难度新题)
    r = ctx.client.post("/practice/sessions", json={"count": 3})
    assert r.status_code == 200
    body = r.json()
    session_id = body["session_id"]
    assert len(body["questions"]) == 3
    assert all("answer_idx" not in q for q in body["questions"])

    # 逐题作答:第 1 题答对,其余答错
    for i, q in enumerate(body["questions"]):
        r = ctx.client.post(f"/practice/sessions/{session_id}/answers", json={
            "question_id": q["id"], "chosen_idx": 0 if i == 0 else 1,
        })
        assert r.status_code == 200
        assert r.json()["is_correct"] == (i == 0)
        assert r.json()["answered_count"] == i + 1

    # 重复作答 409
    r = ctx.client.post(f"/practice/sessions/{session_id}/answers", json={
        "question_id": body["questions"][0]["id"], "chosen_idx": 0,
    })
    assert r.status_code == 409

    # 统计:1/3 正确率(SM-2 最小间隔 1 天,刚答完均未到期)
    stats = ctx.client.get("/practice/stats").json()
    assert stats["total_attempts"] == 3
    assert stats["total_correct"] == 1
    assert stats["active_question_count"] == 3
    assert stats["due_count"] == 0
    assert len(stats["weak_points"]) == 3

    # 回拨两个知识点的 due_at 到过去,验证到期计数与 summary
    db = ctx.session_factory()
    states = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == ctx.user.id
    ).all()
    for s in states[:2]:
        s.due_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    db.close()
    assert ctx.client.get("/practice/stats").json()["due_count"] == 2
    assert ctx.client.get("/practice/summary").json()["due_count"] == 2

    # 历史会话列表/明细
    sessions = ctx.client.get("/practice/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["answered_count"] == 3
    assert sessions[0]["correct_count"] == 1
    detail = ctx.client.get(f"/practice/sessions/{session_id}").json()
    assert len(detail["attempts"]) == 3
    assert sum(1 for a in detail["attempts"] if a["is_correct"]) == 1

    # 错题过滤 + 错题重练
    mistakes = ctx.client.get("/practice/questions?mistake=true").json()
    assert len(mistakes) == 2
    r = ctx.client.post("/practice/sessions", json={
        "count": 5, "question_ids": [m["id"] for m in mistakes],
    })
    assert r.status_code == 200
    assert len(r.json()["questions"]) == 2

    # 趋势:本周有 3 次作答
    trend = ctx.client.get("/practice/trend").json()
    assert len(trend["weeks"]) == 8
    assert trend["weeks"][-1]["attempts"] == 3
    assert trend["weeks"][-1]["correct"] == 1


# ============================================================
# 草稿消费 / 转正 / 汇总
# ============================================================


def test_drafts_filtered_by_task_and_activate(ctx, ctx_b):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job = _wait_job(ctx.client, r.json()["job_id"])
    ids = [q["id"] for q in job["questions"]]

    # 按任务过滤 draft
    drafts = ctx.client.get(f"/practice/drafts?task_id={ctx.task.id}").json()
    assert len(drafts) == 3

    # 只转正前 1 题,其余 draft 保留
    r = ctx.client.post("/practice/questions/activate", json={"question_ids": ids[:1]})
    assert r.json()["activated"] == 1
    assert len(ctx.client.get("/practice/drafts").json()) == 2

    # summary:draft 2 条,无到期复习
    summary = ctx.client.get("/practice/summary").json()
    assert summary == {"due_count": 0, "draft_count": 2}

    # confirm 清场语义:确认剩余 2 条中的 1 条,另 1 条被丢弃
    r = ctx.client.post("/practice/questions/confirm", json={
        "task_id": str(ctx.task.id), "question_ids": ids[1:2],
    })
    assert r.json() == {"confirmed": 1, "discarded": 1}
    assert ctx.client.get("/practice/drafts").json() == []

    # 越权:Bob 看不到 Alice 的 draft,也不能转正 Alice 的题
    assert ctx_b.client.get("/practice/drafts").json() == []
    r = ctx_b.client.post("/practice/questions/activate", json={"question_ids": ids[:1]})
    assert r.json()["activated"] == 0
    # Bob 用 Alice 的 task_id 生成 → 404
    r = ctx_b.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    assert r.status_code == 404


def test_responses_carry_origin_and_languages(ctx):
    """job/draft/组卷/题库响应携带 origin 与 languages 字段"""
    ctx.login()
    questions = _generate_and_confirm(ctx)
    for q in questions:
        assert q["origin"] == "repo"
        assert q["languages"] == []

    # 组卷响应同样携带两字段
    r = ctx.client.post("/practice/sessions", json={"count": 3})
    assert r.status_code == 200
    for q in r.json()["questions"]:
        assert q["origin"] == "repo"
        assert q["languages"] == []

    # 知识点回写语言后,题库列表透传语言标签
    db = ctx.session_factory()
    kp = db.query(KnowledgePoint).first()
    kp.languages = ["python"]
    db.commit()
    db.close()
    items = ctx.client.get("/practice/questions").json()
    assert items and any(i["languages"] == ["python"] for i in items)
    assert all(i["origin"] in ("repo", None) for i in items)


def test_other_user_session_detail_404(ctx, ctx_b):
    ctx.login()
    _generate_and_confirm(ctx)
    session_id = ctx.client.post("/practice/sessions", json={"count": 1}).json()["session_id"]
    assert ctx_b.client.get(f"/practice/sessions/{session_id}").status_code == 404
    assert ctx_b.client.get("/practice/sessions").json() == []


def test_generate_job_of_other_user_404(ctx, ctx_b):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job_id = r.json()["job_id"]
    assert ctx_b.client.get(f"/practice/generate/{job_id}").status_code == 404
    # 等后台线程跑完,避免与下个用例的 TRUNCATE 冲突
    _wait_job(ctx.client, job_id)


# ============================================================
# 题目详情(完整题面按需拉取:题库管理 / 错题回顾 / 历史明细点开单行)
# ============================================================


def test_question_detail_returns_full_content(ctx):
    """详情携带选项/正确答案/解析/出处与本题作答统计;列表项保持摘要"""
    ctx.login()
    _generate_and_confirm(ctx)
    qid = ctx.client.get("/practice/questions").json()[0]["id"]

    # 列表只给一行摘要:全量内容不下发(整库带 options+解析会让 payload 翻几倍)
    item = ctx.client.get("/practice/questions").json()[0]
    assert "options" not in item and "answer_idx" not in item and "explanation" not in item

    # 强制用该题组一局并答对,详情应体现统计
    session = ctx.client.post(
        "/practice/sessions", json={"count": 1, "question_ids": [qid]}
    ).json()
    answered = ctx.client.post(
        f"/practice/sessions/{session['session_id']}/answers",
        json={"question_id": qid, "chosen_idx": 0},
    ).json()
    assert answered["is_correct"] is True

    d = ctx.client.get(f"/practice/questions/{qid}").json()
    assert d["id"] == qid
    assert d["options"] == ["甲", "乙", "丙", "丁"]
    assert d["answer_idx"] == 0
    assert d["explanation"] == "解析"
    assert d["difficulty"] == 1.5
    assert d["status"] == "active"
    assert d["origin"] == "repo"
    assert d["code_snippet"] is None
    assert d["knowledge_key"].startswith("CWE-")
    assert d["category"] == "cwe"
    assert d["source_task_id"] == str(ctx.task.id)
    assert d["attempts"] == 1 and d["correct_count"] == 1
    assert d["accuracy"] == 1.0
    assert d["created_at"]

    # 组卷端点仍不下发答案(与详情端点的职责分离)
    q = ctx.client.post("/practice/sessions", json={"count": 1}).json()["questions"][0]
    assert "answer_idx" not in q and "explanation" not in q


def test_question_detail_covers_draft_and_archived(ctx):
    """draft 可在转正前校对题面;归档题仍可回看(只是退出组卷)"""
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job = _wait_job(ctx.client, r.json()["job_id"])
    qid = job["questions"][0]["id"]

    assert ctx.client.get(f"/practice/questions/{qid}").json()["status"] == "draft"

    ctx.client.post("/practice/questions/activate", json={"question_ids": [qid]})
    assert ctx.client.get(f"/practice/questions/{qid}").json()["status"] == "active"

    ctx.client.post(f"/practice/questions/{qid}/archive")
    d = ctx.client.get(f"/practice/questions/{qid}").json()
    assert d["status"] == "archived"
    assert d["options"] == ["甲", "乙", "丙", "丁"]


def test_question_detail_isolation(ctx, ctx_b):
    """越权/不存在 404,未登录 401(与归档、会话明细同一归属校验口径)"""
    ctx.login()
    _generate_and_confirm(ctx)
    qid = ctx.client.get("/practice/questions").json()[0]["id"]

    assert ctx_b.client.get(f"/practice/questions/{qid}").status_code == 404
    assert ctx.client.get(f"/practice/questions/{uuid.uuid4()}").status_code == 404

    ctx.logout()
    assert ctx.client.get(f"/practice/questions/{qid}").status_code == 401


# ============================================================
# 清空练习记录
# ============================================================


def _play_one_round(ctx, n: int):
    """辅助:出题→确认→组卷并全部答错,产生流水/记忆状态/难度调整"""
    _generate_and_confirm(ctx)
    r = ctx.client.post("/practice/sessions", json={"count": n})
    assert r.status_code == 200
    body = r.json()
    for q in body["questions"]:
        resp = ctx.client.post(f"/practice/sessions/{body['session_id']}/answers", json={
            "question_id": q["id"], "chosen_idx": 1,  # 正确答案为 0,故全部答错
        })
        assert resp.status_code == 200
    return body["session_id"]


def test_clear_records_keeps_questions(ctx):
    """基础档:流水/会话/记忆状态清空,题库保留且难度重置回 3.0"""
    ctx.login()
    _play_one_round(ctx, 3)
    stats = ctx.client.get("/practice/stats").json()
    assert stats["total_attempts"] == 3
    assert len(stats["weak_points"]) == 3

    r = ctx.client.delete("/practice/records")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_sessions"] == 1
    assert body["deleted_attempts"] == 3
    assert body["deleted_questions"] == 0

    # 统计归零,题库仍在
    stats = ctx.client.get("/practice/stats").json()
    assert stats["total_attempts"] == 0
    assert stats["weak_points"] == []
    assert stats["active_question_count"] == 3
    assert stats["due_count"] == 0

    # 历史/错题/趋势均清空
    assert ctx.client.get("/practice/sessions").json() == []
    assert ctx.client.get("/practice/questions?mistake=true").json() == []
    assert all(w["attempts"] == 0 for w in ctx.client.get("/practice/trend").json()["weeks"])

    # 难度重置回初值 3.0(fake 出题为 1.5,答错后已被调高)
    questions = ctx.client.get("/practice/questions").json()
    assert len(questions) == 3
    assert all(q["difficulty"] == 3.0 for q in questions)
    assert all(q["attempts"] == 0 for q in questions)

    # SM-2 记忆状态已删
    db = ctx.session_factory()
    states = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == ctx.user.id
    ).count()
    db.close()
    assert states == 0


def test_clear_records_include_questions(ctx):
    """全清档:连题目与知识点词典一并删除"""
    ctx.login()
    _play_one_round(ctx, 3)

    r = ctx.client.delete("/practice/records", params={"include_questions": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_questions"] == 3
    assert body["deleted_attempts"] == 3

    # 题库与知识点全部清空
    assert ctx.client.get("/practice/questions").json() == []
    assert ctx.client.get("/practice/stats").json()["active_question_count"] == 0
    db = ctx.session_factory()
    kps = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == ctx.user.id
    ).count()
    qs = db.query(Question).filter(Question.user_id == ctx.user.id).count()
    db.close()
    assert kps == 0 and qs == 0


def test_clear_records_user_isolation(ctx, ctx_b):
    """多用户隔离:Alice 清空不影响 Bob 的记录"""
    ctx.login()
    _play_one_round(ctx, 3)

    # Bob 只有 1 条 finding,单走一轮出题→确认→作答
    r = ctx_b.client.post("/practice/generate", json={"task_id": str(ctx_b.task.id)})
    job = _wait_job(ctx_b.client, r.json()["job_id"])
    assert job["status"] == "done" and len(job["questions"]) == 1
    qid = job["questions"][0]["id"]
    r = ctx_b.client.post("/practice/questions/confirm", json={
        "task_id": str(ctx_b.task.id), "question_ids": [qid],
    })
    assert r.json()["confirmed"] == 1
    session_id = ctx_b.client.post(
        "/practice/sessions", json={"count": 1}
    ).json()["session_id"]
    resp = ctx_b.client.post(f"/practice/sessions/{session_id}/answers", json={
        "question_id": qid, "chosen_idx": 1,
    })
    assert resp.status_code == 200

    r = ctx.client.delete("/practice/records")
    assert r.status_code == 200

    # Bob 的会话/统计/题库均不受影响
    assert len(ctx_b.client.get("/practice/sessions").json()) == 1
    assert ctx_b.client.get("/practice/stats").json()["total_attempts"] == 1
    assert len(ctx_b.client.get("/practice/questions").json()) == 1
    # Alice 已清空
    assert ctx.client.get("/practice/sessions").json() == []


# ============================================================
# 知识点看板(GET /practice/knowledge-points)
# ============================================================


def _make_kp_q_state(
    db_session, user_id, key, name, *,
    attempts, correct, repetitions, due_at, question_count=1,
    learning_topic="security",
):
    """直接造一个知识点 + active 题 + SM-2 状态(attempts=0 时不造状态行)"""
    kp = KnowledgePoint(
        user_id=user_id, key=key, name=name, category="cwe", languages=["python"],
        learning_topic=learning_topic,
    )
    db_session.add(kp)
    db_session.flush()
    for i in range(question_count):
        db_session.add(Question(
            user_id=user_id, knowledge_point_id=kp.id,
            qtype=QuestionType.SINGLE_CHOICE, stem=f"{key}-题{i}",
            options=["甲", "乙"], answer_idx=0, explanation="",
            difficulty=3.0, status=QuestionStatus.ACTIVE,
            dedup_hash=uuid.uuid4().hex,
        ))
    if attempts > 0:
        db_session.add(UserKnowledgeState(
            user_id=user_id, knowledge_point_id=kp.id,
            attempts=attempts, correct_count=correct,
            repetitions=repetitions, interval_days=1.0, due_at=due_at,
        ))
    db_session.commit()
    db_session.close()
    return kp


def test_knowledge_points_board_statuses(ctx):
    """看板分栏:薄弱/待复习/已巩固/学习中/未开始 各状态正确派生"""
    ctx.login()
    s = ctx.session_factory()
    now = datetime.now(timezone.utc)
    # 错误率 75% > 40% 且作答 ≥ 3 次 → weak
    _make_kp_q_state(s, ctx.user.id, "CWE-100", "薄弱点",
                     attempts=4, correct=1, repetitions=0, due_at=None)
    # SM-2 已到期(未到期不满足 mastered 之前先判 due)→ due
    _make_kp_q_state(s, ctx.user.id, "CWE-200", "待复习点",
                     attempts=2, correct=2, repetitions=2,
                     due_at=now - timedelta(days=1))
    # 连续答对且正确率 100% ≥ 75%,未到期 → mastered
    _make_kp_q_state(s, ctx.user.id, "CWE-300", "已巩固点",
                     attempts=5, correct=5, repetitions=5,
                     due_at=now + timedelta(days=30))
    # 有作答但样本不足/未到期/未巩固 → learning
    _make_kp_q_state(s, ctx.user.id, "CWE-400", "学习中点",
                     attempts=2, correct=1, repetitions=1,
                     due_at=now + timedelta(days=2))
    # 从未作答 → fresh
    _make_kp_q_state(s, ctx.user.id, "CWE-500", "未开始点",
                     attempts=0, correct=0, repetitions=0, due_at=None)

    r = ctx.client.get("/practice/knowledge-points")
    assert r.status_code == 200
    cards = r.json()
    by_key = {c["knowledge_key"]: c for c in cards}
    assert by_key["CWE-100"]["board_status"] == "weak"
    assert by_key["CWE-200"]["board_status"] == "due"
    assert by_key["CWE-300"]["board_status"] == "mastered"
    assert by_key["CWE-400"]["board_status"] == "learning"
    assert by_key["CWE-500"]["board_status"] == "fresh"
    # 卡片字段:题数与作答统计
    assert by_key["CWE-100"]["question_count"] == 1
    assert by_key["CWE-100"]["attempts"] == 4
    assert by_key["CWE-500"]["accuracy"] is None
    # 排序:薄弱栏最前
    assert cards[0]["board_status"] == "weak"


def test_knowledge_points_weak_over_due_priority(ctx):
    """既薄弱又到期 → weak 优先(薄弱栏更能引起注意)"""
    ctx.login()
    s = ctx.session_factory()
    now = datetime.now(timezone.utc)
    _make_kp_q_state(s, ctx.user.id, "CWE-900", "既薄弱又到期",
                     attempts=5, correct=1, repetitions=0,
                     due_at=now - timedelta(days=1))
    r = ctx.client.get("/practice/knowledge-points")
    assert r.status_code == 200
    assert r.json()[0]["board_status"] == "weak"


def test_knowledge_points_auth_and_user_isolation(ctx, ctx_b):
    """未登录 401;数据 per-user 隔离(Bob 看不到 Alice 的知识点)"""
    ctx.logout()
    assert ctx.client.get("/practice/knowledge-points").status_code == 401
    ctx.login()
    s = ctx.session_factory()
    _make_kp_q_state(s, ctx.user.id, "CWE-700", "alice 的知识点",
                     attempts=0, correct=0, repetitions=0, due_at=None)
    r = ctx_b.client.get("/practice/knowledge-points")
    assert r.status_code == 200
    assert all(c["knowledge_key"] != "CWE-700" for c in r.json())


def test_knowledge_points_return_learning_topic(ctx):
    """看板卡片返回知识点所属学习主题(前端按主题分组展示)"""
    ctx.login()
    s = ctx.session_factory()
    now = datetime.now(timezone.utc)
    _make_kp_q_state(s, ctx.user.id, "CWE-A", "合同点",
                     attempts=0, correct=0, repetitions=0, due_at=None,
                     learning_topic="contract")
    _make_kp_q_state(s, ctx.user.id, "CWE-B", "安全点",
                     attempts=0, correct=0, repetitions=0, due_at=now,
                     learning_topic="security")
    r = ctx.client.get("/practice/knowledge-points")
    assert r.status_code == 200
    by_key = {c["knowledge_key"]: c for c in r.json()}
    assert by_key["CWE-A"]["learning_topic"] == "contract"
    assert by_key["CWE-B"]["learning_topic"] == "security"


# ============================================================
# 学习主题:播种幂等 + CRUD(GET/POST/PATCH/DELETE /practice/topics)
# ============================================================


def test_ensure_user_topics_seeds_and_idempotent(ctx):
    """播种:补齐内置 4 行;重复调用不重复插行;文案与排序正确"""
    ctx.login()
    s = ctx.session_factory()
    try:
        topics = ensure_user_topics(s, ctx.user.id)
        s.commit()
        assert [t.key for t in topics] == [
            "security", "architecture", "coding", "contract",
        ]
        assert all(t.is_builtin and t.enabled for t in topics)
        assert [t.sort_order for t in topics] == [10, 20, 30, 40]
        # 幂等:重复调用不重复插行
        ensure_user_topics(s, ctx.user.id)
        s.commit()
        count = s.query(LearningTopic).filter(
            LearningTopic.user_id == ctx.user.id
        ).count()
        assert count == 4
        # 内置描述非空(分类提示词与设置页展示用)
        assert all(t.description for t in topics)
    finally:
        # 显式关闭:防事务悬挂阻塞下一用例的 TRUNCATE
        s.close()


def test_topics_list_seeds_builtins(ctx):
    """GET /practice/topics:懒播种内置 4 行,按 sort_order 排序"""
    ctx.login()
    r = ctx.client.get("/practice/topics")
    assert r.status_code == 200
    body = r.json()
    assert [t["key"] for t in body] == [
        "security", "architecture", "coding", "contract",
    ]
    assert all(t["is_builtin"] and t["enabled"] for t in body)
    assert all(t["kp_count"] == 0 for t in body)


def test_topics_create_and_name_uniqueness(ctx):
    """POST /practice/topics:自定义主题创建;重名 400;空名 422"""
    ctx.login()
    ctx.client.get("/practice/topics")  # 先播种
    r = ctx.client.post("/practice/topics", json={
        "name": "算法", "description": "复杂度分析与正确性证明",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("custom_")
    assert len(body["key"]) == len("custom_") + 8
    assert not body["is_builtin"]
    assert body["enabled"] is True
    assert body["sort_order"] >= 50
    # 重名(与自定义同名)→ 400
    r2 = ctx.client.post("/practice/topics", json={"name": "算法"})
    assert r2.status_code == 400
    # 重名(与内置名「安全」)→ 400
    r3 = ctx.client.post("/practice/topics", json={"name": "安全"})
    assert r3.status_code == 400
    # 空名 → 422
    r4 = ctx.client.post("/practice/topics", json={"name": ""})
    assert r4.status_code == 422


def test_topics_create_custom_limit(ctx):
    """自定义主题上限 10 个,超出 400"""
    ctx.login()
    ctx.client.get("/practice/topics")
    for i in range(10):
        r = ctx.client.post("/practice/topics", json={"name": f"主题{i}"})
        assert r.status_code == 201
    r = ctx.client.post("/practice/topics", json={"name": "超额主题"})
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]


def test_topics_update_builtin_and_custom(ctx):
    """PATCH:内置仅接受 enabled(改 name 400);自定义全字段可改"""
    ctx.login()
    body = ctx.client.get("/practice/topics").json()
    security = next(t for t in body if t["key"] == "security")

    # 内置改 name → 400
    r = ctx.client.patch(
        f"/practice/topics/{security['id']}", json={"name": "改名"},
    )
    assert r.status_code == 400
    # 内置停用 → 200(其他 3 个仍启用,不触发保护)
    r = ctx.client.patch(
        f"/practice/topics/{security['id']}", json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # 自定义:改 name/description → 200
    created = ctx.client.post("/practice/topics", json={"name": "算法"}).json()
    r = ctx.client.patch(
        f"/practice/topics/{created['id']}",
        json={"name": "算法与数据结构", "description": "复杂度、边界、证明"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "算法与数据结构"
    assert r.json()["description"] == "复杂度、边界、证明"

    # 他人主题 → 404(per-user 隔离)
    other = Ctx(ctx.session_factory, "eve@test.local", n_results=0).login()
    r2 = other.client.patch(
        f"/practice/topics/{created['id']}", json={"enabled": False},
    )
    assert r2.status_code == 404


def test_topics_cannot_disable_or_delete_last_enabled(ctx):
    """启用数不可归零:最后一个启用的主题不能停/删(400)"""
    ctx.login()
    body = ctx.client.get("/practice/topics").json()
    for t in body[:-1]:
        r = ctx.client.patch(f"/practice/topics/{t['id']}", json={"enabled": False})
        assert r.status_code == 200
    last = body[-1]
    # 停用最后一个启用主题 → 400
    r = ctx.client.patch(f"/practice/topics/{last['id']}", json={"enabled": False})
    assert r.status_code == 400
    # 删除最后一个启用主题 → 400
    r = ctx.client.delete(f"/practice/topics/{last['id']}")
    assert r.status_code == 400


def test_topics_delete_rules(ctx):
    """DELETE:内置 400;有知识点的自定义主题 400;无知识点自定义主题 204"""
    ctx.login()
    ctx.client.get("/practice/topics")
    # 内置不可删
    builtin = ctx.client.get("/practice/topics").json()[0]
    r = ctx.client.delete(f"/practice/topics/{builtin['id']}")
    assert r.status_code == 400
    # 自定义主题 + 关联知识点 → 400
    created = ctx.client.post("/practice/topics", json={"name": "算法"}).json()
    s = ctx.session_factory()
    s.add(KnowledgePoint(
        user_id=ctx.user.id, key="CWE-ALGO", name="复杂度",
        learning_topic=created["key"],
    ))
    s.commit()
    s.close()
    r = ctx.client.delete(f"/practice/topics/{created['id']}")
    assert r.status_code == 400
    assert "知识点" in r.json()["detail"]
    # 清掉关联 KP → 删除成功 204
    s = ctx.session_factory()
    s.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == ctx.user.id,
        KnowledgePoint.key == "CWE-ALGO",
    ).delete()
    s.commit()
    s.close()
    r = ctx.client.delete(f"/practice/topics/{created['id']}")
    assert r.status_code == 204
    # 列表中不再出现
    keys = [t["key"] for t in ctx.client.get("/practice/topics").json()]
    assert created["key"] not in keys


# ============================================================
# 会话:主题级练习(learning_topic 过滤组卷)
# ============================================================


def test_start_session_learning_topic_filter(ctx):
    """learning_topic 过滤组卷:只出该主题知识点的题;stats 快照记录主题"""
    ctx.login()
    _generate_and_confirm(ctx)  # 3 个 KP(security)各 1 题
    # 把第二个 KP 改为 contract 主题
    s = ctx.session_factory()
    kps = s.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == ctx.user.id
    ).all()
    assert len(kps) == 3
    contract_kp = kps[1]
    contract_kp.learning_topic = "contract"
    s.commit()
    s.close()

    # security 主题组卷 → 只含 security KP 的题
    r = ctx.client.post("/practice/sessions", json={"learning_topic": "security"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["questions"]) == 2
    # contract 主题组卷 → 只含 contract KP 的题
    r = ctx.client.post("/practice/sessions", json={"learning_topic": "contract"})
    assert r.status_code == 200
    assert len(r.json()["questions"]) == 1
    # stats 快照记录 learning_topic
    s = ctx.session_factory()
    from app.models.practice import PracticeSession
    latest = (
        s.query(PracticeSession)
        .filter(PracticeSession.user_id == ctx.user.id)
        .order_by(PracticeSession.started_at.desc())
        .first()
    )
    assert latest is not None
    assert latest.stats["learning_topic"] == "contract"
    s.close()


def test_start_session_learning_topic_not_found(ctx):
    """learning_topic 无匹配知识点 → 404"""
    ctx.login()
    _generate_and_confirm(ctx)
    r = ctx.client.post("/practice/sessions", json={
        "learning_topic": "custom_nonexist",
    })
    assert r.status_code == 404


def test_start_session_learning_topic_validation(ctx):
    """learning_topic 格式非法 → 422;与 topic_filter 同传 → 422"""
    ctx.login()
    _generate_and_confirm(ctx)
    # 格式非法(大写/特殊字符)→ 422
    r = ctx.client.post("/practice/sessions", json={"learning_topic": "Bad Key!"})
    assert r.status_code == 422
    # 与 topic_filter 同传 → 422
    r = ctx.client.post("/practice/sessions", json={
        "learning_topic": "security", "topic_filter": "CWE-89",
    })
    assert r.status_code == 422


# ============================================================
# 迁移:knowledge_points.learning_topic 存量回填(众数)
# ============================================================


def test_kp_learning_topic_migration_backfill(test_engine, ctx, monkeypatch):
    """迁移回填:KP 主题取其题目主题众数(并列按主题名),无题落默认 security"""
    import app.database as database_module
    from app.models.practice import migrate_practice_learning_columns

    ctx.login()
    # 造数据:
    # kp_majority:2 题 contract + 1 题 security → contract
    # kp_single:1 题 architecture → architecture
    # kp_no_questions:无题 → security(默认)
    # kp_topicless_questions:题全部无主题 → security(默认)
    s = ctx.session_factory()

    def _mk_kp(key):
        kp = KnowledgePoint(user_id=ctx.user.id, key=key, name=key)
        s.add(kp)
        s.flush()
        return kp

    def _mk_q(kp, topic=None):
        s.add(Question(
            user_id=ctx.user.id, knowledge_point_id=kp.id,
            qtype=QuestionType.SINGLE_CHOICE, stem=f"{kp.key}-{topic}",
            options=["甲", "乙"], answer_idx=0, explanation="",
            difficulty=3.0, status=QuestionStatus.ACTIVE,
            dedup_hash=uuid.uuid4().hex, learning_topic=topic,
        ))

    kp_majority = _mk_kp("CWE-MAJ")
    _mk_q(kp_majority, "contract")
    _mk_q(kp_majority, "contract")
    _mk_q(kp_majority, "security")
    kp_single = _mk_kp("CWE-SINGLE")
    _mk_q(kp_single, "architecture")
    kp_no_q = _mk_kp("CWE-NOQ")
    kp_topicless = _mk_kp("CWE-TOPICLESS")
    _mk_q(kp_topicless, None)
    s.commit()
    s.close()

    # DROP 列模拟老库
    with test_engine.begin() as conn:
        conn.execute(text("ALTER TABLE knowledge_points DROP COLUMN learning_topic"))

    # monkeypatch engine 后执行迁移(函数内 from-import 在调用时取属性)
    monkeypatch.setattr(database_module, "engine", test_engine)
    migrate_practice_learning_columns()

    # 断言回填结果
    s = ctx.session_factory()
    topics = {
        kp.key: kp.learning_topic
        for kp in s.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == ctx.user.id
        ).all()
    }
    assert topics["CWE-MAJ"] == "contract"       # 众数 2:1
    assert topics["CWE-SINGLE"] == "architecture"
    assert topics["CWE-NOQ"] == "security"       # 无题 → 默认
    assert topics["CWE-TOPICLESS"] == "security"  # 题无主题 → 默认
    s.close()

    # 幂等:列已存在时再跑不报错、值不变
    migrate_practice_learning_columns()
    s = ctx.session_factory()
    kp = s.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == ctx.user.id,
        KnowledgePoint.key == "CWE-MAJ",
    ).one()
    assert kp.learning_topic == "contract"
    s.close()
