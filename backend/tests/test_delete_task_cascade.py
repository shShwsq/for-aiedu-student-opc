"""删除任务的级联回归测试。

钉住两个历史 bug(均在删除链路上,同一条 delete_task 触发):
1. ReviewItem.task 的 backref(Task.review_items)未声明删除级联,默认 cascade 仅
   save-update/merge,删 Task 时 ORM 抢先发 `UPDATE review_items SET task_id = NULL`,
   task_id 非空 → NotNullViolation → 500。
   修复:backref 显式 `cascade="all, delete-orphan", passive_deletes=True`。
2. ORM 删 Task 时逐行预删 conversations/results,results 的删除触发对
   practice_questions 的 SET NULL 更新;线上库(PolarDB)的触发器顺序里
   practice_questions.source_task_id 的 SET NULL 动作先于 review_items 级联执行,
   排队的 FK 检查落在 review_items 已删之后 → ForeignKeyViolation → 500。
   (全新 schema 建表顺序不同、动作触发器顺序相反,该层只在存量库复现。)
   修复:Task 三个子关系均 passive_deletes=True,flush 只发一条 DELETE FROM tasks。

故本测试除结果断言外,还钉住「flush 只发 DELETE FROM tasks」这一 SQL 不变量——
它是本地 CI 唯一能防住第 2 层 bug 的手段。

本用例直连真实 PostgreSQL(库不可达时由 conftest 统一 skip),因为 bug 只在
真实 flush/commit + DB 约束下才复现,mock 会话拦不到。
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy import event as sa_event
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.audit import ReviewItem
from app.models.task import Conversation, Result, Task, TaskStatus
from app.models.task_artifact import TaskArtifact  # noqa: F401 注册 mapper,Task relationship 才完整
from app.models.practice import (  # noqa: F401 注册 knowledge_points 等练习表(results 的 FK 引用它)
    KnowledgePoint, Question, QuestionKnowledgePoint, QuestionStatus, QuestionType,
)
import app.models.practice  # noqa: F401
import app.models.user  # noqa: F401 tasks.user_id 的 FK 指向 users,create_all 需其已注册
import app.models.user_git_binding  # noqa: F401
from app.models.user import User

pytestmark = pytest.mark.db  # 库不可达时由 tests/conftest.py 统一跳过
TEST_SCHEMA = f"pytest_delcascade_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = (
    "review_items", "results", "conversations", "task_artifacts", "tasks",
    "question_knowledge_points", "practice_questions", "knowledge_points",
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
    """建一个带 conversations/results/review_items/练习题 的任务,提交后返回 task_id。

    练习题(用户级数据)通过 source_task_id/source_result_id/source_review_item_id
    回指本任务,钉住 SET NULL 语义:删任务只置空来源引用,不能误删题目。
    """
    user = User(email=f"delcascade-{uuid.uuid4().hex[:8]}@test.local",
                password_hash="x")
    db.add(user)
    db.flush()
    task = Task(
        user_id=user.id,
        user_input="审查这个仓库", scenario="general", status=TaskStatus.COMPLETED,
    )
    db.add(task)
    db.flush()
    conv = Conversation(task_id=task.id, round_idx=1, role="react_agent",
                        type="message", content="x")
    result = Result(task_id=task.id, round_idx=1, title="KP", content="body",
                     metadata_={"learning_topic": "coding"})
    ri1 = ReviewItem(task_id=task.id, origin="agent1_claim", status="established",
                     verdict="confirmed", title="ri-1")
    ri2 = ReviewItem(task_id=task.id, origin="agent1_claim", status="established",
                     verdict="confirmed", title="ri-2")
    db.add_all([conv, result, ri1, ri2])
    db.flush()
    kp = KnowledgePoint(user_id=user.id, key="CWE-89", name="SQL注入",
                        languages=["python"])
    db.add(kp)
    db.flush()
    q1 = Question(user_id=user.id, knowledge_point_id=kp.id,
                  qtype=QuestionType.SINGLE_CHOICE,
                  stem="哪段代码有SQL注入?", options=["a", "b"], answer_idx=0,
                  dedup_hash=uuid.uuid4().hex, status=QuestionStatus.DRAFT,
                  source_task_id=task.id, source_result_id=result.id,
                  source_review_item_id=ri1.id)
    db.add_all([q1])
    db.flush()
    db.add(QuestionKnowledgePoint(question_id=q1.id, knowledge_point_id=kp.id,
                                   is_primary=True))
    db.commit()
    return task.id  # task.id 本就是 uuid.UUID(as_uuid=True),勿再包一层


@pytest.mark.parametrize("_retry", range(2))
def test_delete_task_cascades_review_items_without_notnull_violation(db, test_engine, _retry):
    """删任务不得因子表级联链报约束违规;任务子表清空、用户级练习题存活且来源置空。"""
    task_id = _seed_task_with_children(db)
    q1_id = db.scalar(select(Question.id).limit(1))

    written: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().splitlines()[0]
        if head.upper().startswith(("DELETE", "UPDATE", "INSERT")):
            written.append(head)

    db.get(Task, task_id)  # 与线上 delete_task 相同:先取任务再删

    sa_event.listen(test_engine, "before_cursor_execute", _capture)
    try:
        # 核心断言区:修复前(audit 层)这里 raise IntegrityError(NotNullViolation);
        # 修复前(task.py 层)ORM 会先逐行 DELETE conversations/results,
        # 线上库触发器顺序下后续 DELETE FROM tasks 报 ForeignKeyViolation。
        task = db.get(Task, task_id)
        db.delete(task)
        db.commit()  # 不抛即通过
    finally:
        sa_event.remove(test_engine, "before_cursor_execute", _capture)

    # SQL 不变量:flush 期间只允许一条 DELETE,且必须打在 tasks 上。
    # 任何对子表的逐行 DELETE/UPDATE(含对 review_items 的 SET NULL 断开)都会
    # 在线上库的触发器顺序下踩雷,故直接钉死。
    deletes = [s for s in written if s.upper().startswith("DELETE")]
    assert len(written) == 1 and len(deletes) == 1, (
        f"flush 期间应只发 1 条写语句(DELETE FROM tasks),实际:{written}"
    )
    assert "tasks" in written[0], f"唯一的写语句应打在 tasks 上,实际:{written[0]}"

    # 任务本体与全部子行都已消失(交 DB ON DELETE CASCADE)
    assert db.get(Task, task_id) is None
    for model in (ReviewItem, Conversation, Result):
        remaining = db.scalar(
            select(func.count()).select_from(model).where(model.task_id == task_id)
        )
        assert remaining == 0, f"{model.__name__} 未被级联删除"

    # 用户级练习题存活:仅来源引用被置空(SET NULL),题目本体与知识点关联保留
    survived = db.get(Question, q1_id)
    assert survived is not None, "练习题不应被任务级联删除(它是用户级数据)"
    assert survived.source_task_id is None
    assert survived.source_result_id is None
    assert survived.source_review_item_id is None
    assert db.scalar(
        select(func.count()).select_from(QuestionKnowledgePoint)
    ) == 1, "题目↔知识点关联不应受任务删除影响"
