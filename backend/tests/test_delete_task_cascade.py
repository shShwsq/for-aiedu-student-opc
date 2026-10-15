"""删除任务的级联回归测试。

钉住的历史 bug:ReviewItem.task 的 backref(Task.review_items)未声明删除级联,
默认 cascade 仅 save-update/merge。删 Task 时 SQLAlchemy ORM 会抢先发
`UPDATE review_items SET task_id = NULL` 去"断开"子行,而 task_id 列非空 →
psycopg.errors.NotNullViolation → delete_task 500(见 tasks.py:delete_task)。

修复:backref 显式 `cascade="all, delete-orphan", passive_deletes=True`,
让 ORM 不碰子行、交 FK 的 ON DELETE CASCADE 级联删除。

本用例直连真实 PostgreSQL(库不可达时由 conftest 统一 skip),因为该 bug 只在
真实 flush/commit + DB NOT NULL 约束下才复现,mock 会话拦不到。
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.audit import ReviewItem
from app.models.task import Conversation, Result, Task, TaskStatus
from app.models.task_artifact import TaskArtifact  # noqa: F401 注册 mapper,Task relationship 才完整
import app.models.practice  # noqa: F401 注册 knowledge_points 等练习表(results.knowledge_point_id
# 的 FK 引用它;测试进程内该模块没人导入时,create_all 解析不到目标表直接报
# NoReferencedTableError,fixture 误报"数据库不可用"并 skip,测试从未真正执行)

pytestmark = pytest.mark.db  # 库不可达时由 tests/conftest.py 统一跳过
TEST_SCHEMA = f"pytest_delcascade_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = (
    "review_items", "results", "conversations", "task_artifacts", "tasks",
)


@pytest.fixture(scope="session")
def test_engine():
    try:
        maint = create_engine(
            settings.DATABASE_URL, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"connect_timeout": 5, "options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(
            settings.DATABASE_URL, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    with test_engine.begin() as conn:
        for t in _TABLES:
            conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))


@pytest.fixture()
def db(test_engine):
    Session = sessionmaker(bind=test_engine, autoflush=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _seed_task_with_children(db):
    """建一个带 review_items/conversations/results 的任务,提交后返回 task_id。"""
    task = Task(
        user_input="审查这个仓库", scenario="general", status=TaskStatus.COMPLETED,
    )
    db.add(task)
    db.flush()
    db.add_all([
        Conversation(task_id=task.id, round_idx=1, role="react_agent",
                     type="message", content="x"),
        Result(task_id=task.id, round_idx=1, title="KP", content="body",
               metadata_={"learning_topic": "coding"}),
        ReviewItem(task_id=task.id, origin="agent1_claim", status="established",
                   verdict="confirmed", title="ri-1"),
        ReviewItem(task_id=task.id, origin="agent1_claim", status="established",
                   verdict="confirmed", title="ri-2"),
    ])
    db.commit()
    return task.id


@pytest.mark.parametrize("_retry", range(2))
def test_delete_task_cascades_review_items_without_notnull_violation(db, _retry):
    """删任务不得因 review_items.task_id 非空报 NotNullViolation;子表应被级联清空。

    跑两遍(第二条 param):第一遍若命中旧 bug,commit 后事务已 abort 会被 rollback;
    第二遍在干净数据上重申幂等,顺带验证级联删除不会把连接/库搞挂。
    """
    task_id = _seed_task_with_children(db)  # task.id 本就是 uuid.UUID(as_uuid=True),勿再包一层

    # 核心断言:修复前这里 raise IntegrityError(NotNullViolation) → 500 的根因
    task = db.get(Task, task_id)
    db.delete(task)
    db.commit()  # 不抛即通过

    # 任务本体与全部子行都已消失(交 DB ON DELETE CASCADE / ORM delete-orphan)
    assert db.get(Task, task_id) is None
    for model in (ReviewItem, Conversation, Result):
        remaining = db.scalar(
            select(func.count()).select_from(model).where(model.task_id == task_id)
        )
        assert remaining == 0, f"{model.__name__} 未被级联删除"
