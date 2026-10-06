"""知识点主题手工修正接口测试(PUT /practice/knowledge-points/{key}/topic)

覆盖:
- 改主题成功:KnowledgePoint.learning_topic 更新,其下全部题目级联(含老题 NULL)
- 停用主题仍可选(存量不受影响,仅不再出新题)
- 越权/未知主题 key → 400
- 知识点不存在 → 404
- 同名 key 跨用户隔离

无建 schema 权限(数据库不可用)时整体跳过,同 test_practice_explain_api。
"""
import os
import time
import uuid

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
    LearningTopic,
    KnowledgePoint,
    Question,
    QuestionStatus,
    QuestionType,
    ensure_user_topics,
)
from app.models.task import Result, Task
from app.models.user import User
from app.routers import practice as practice_router

TEST_SCHEMA = f"pytest_topic_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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


def _seed(monkeypatch, factory, email="topic-alice@test.local", n_questions=2):
    """建 user + task + 一个知识点(learning_topic=security) + n_questions 道题

    第 0 题 learning_topic=security,其余题为 NULL(模拟老题),验证级联归一。
    返回 (client, kp_key, user_id, factory)。
    """
    db = factory()
    user = User(email=email, password_hash="x")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, user_input="audit")
    db.add(task)
    db.flush()
    r = Result(
        task_id=task.id, title="发现0", content="直接拼接用户输入构造查询",
        metadata_={"cwe": "CWE-89", "severity": "high"},
    )
    db.add(r)
    db.flush()
    key = "CWE-89"
    kp = KnowledgePoint(
        user_id=user.id, key=key, name="SQL 注入", category="cwe",
        learning_topic="security",
    )
    db.add(kp)
    db.flush()
    for i in range(n_questions):
        db.add(Question(
            user_id=user.id, source_task_id=task.id, source_result_id=r.id,
            knowledge_point_id=kp.id, qtype=QuestionType.SINGLE_CHOICE,
            stem=f"题干{i}", code_snippet="cursor.execute(sql)",
            options=["甲", "乙", "丙", "丁"], answer_idx=0,
            explanation="拼接导致注入", difficulty=1.5,
            status=QuestionStatus.ACTIVE, dedup_hash=uuid.uuid4().hex,
            # 首题带主题,其余留 NULL 模拟老数据
            learning_topic="security" if i == 0 else None,
            origin="repo", source_file="src/a.py", source_lines="40-46",
        ))
    db.commit()
    uid = user.id
    db.close()

    app = FastAPI()
    app.include_router(practice_router.router)

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: _reload_user(factory, uid)
    return TestClient(app), key, uid, factory


def _kp_topic(factory, uid, key):
    db = factory()
    try:
        return db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == uid, KnowledgePoint.key == key
        ).first().learning_topic
    finally:
        db.close()


def _question_topics(factory, kp_key):
    db = factory()
    try:
        kp = db.query(KnowledgePoint).filter(KnowledgePoint.key == kp_key).first()
        return [
            q.learning_topic
            for q in db.query(Question).filter(
                Question.knowledge_point_id == kp.id
            ).all()
        ]
    finally:
        db.close()


def test_set_topic_updates_kp_and_cascades_questions(monkeypatch, db_factory):
    client, key, uid, factory = _seed(monkeypatch, db_factory, n_questions=3)
    r = client.put(f"/practice/knowledge-points/{key}/topic", json={
        "topic_key": "architecture",
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out == {"knowledge_key": key, "learning_topic": "architecture"}
    assert _kp_topic(factory, uid, key) == "architecture"
    # 级联:全部题目(含原本 NULL 的老题)都归一为新主题
    assert set(_question_topics(factory, key)) == {"architecture"}


def test_set_topic_allows_disabled_topic(monkeypatch, db_factory):
    client, key, uid, factory = _seed(monkeypatch, db_factory)
    db = factory()
    try:
        ensure_user_topics(db, uid)
        coding = db.query(LearningTopic).filter(
            LearningTopic.user_id == uid, LearningTopic.key == "coding"
        ).first()
        coding.enabled = False
        db.commit()
    finally:
        db.close()
    r = client.put(f"/practice/knowledge-points/{key}/topic", json={
        "topic_key": "coding",
    })
    assert r.status_code == 200, r.text
    assert _kp_topic(factory, uid, key) == "coding"


def test_set_topic_rejects_unknown_key(monkeypatch, db_factory):
    client, key, uid, factory = _seed(monkeypatch, db_factory)
    r = client.put(f"/practice/knowledge-points/{key}/topic", json={
        "topic_key": "not_a_real_topic",
    })
    assert r.status_code == 400
    assert _kp_topic(factory, uid, key) == "security"  # 未被改动


def test_set_topic_rejects_others_custom_key(monkeypatch, db_factory):
    """他人用户名下的自定义主题 key 不可挂载(越权防护)"""
    client, key, uid, factory = _seed(monkeypatch, db_factory)
    other_db = factory()
    try:
        other = User(email="topic-bob@test.local", password_hash="x")
        other_db.add(other)
        other_db.flush()
        other_db.add(LearningTopic(
            user_id=other.id, key="custom_deadbeef", name="别人的主题",
            description="", is_builtin=False, enabled=True, sort_order=50,
        ))
        other_db.commit()
    finally:
        other_db.close()
    r = client.put(f"/practice/knowledge-points/{key}/topic", json={
        "topic_key": "custom_deadbeef",
    })
    assert r.status_code == 400


def test_set_topic_knowledge_point_not_found(monkeypatch, db_factory):
    client, _, _, _ = _seed(monkeypatch, db_factory)
    r = client.put("/practice/knowledge-points/nope-xyz/topic", json={
        "topic_key": "architecture",
    })
    assert r.status_code == 404
