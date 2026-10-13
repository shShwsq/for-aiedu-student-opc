"""暂停/执行中遭遇后端重启后的孤儿任务复活:单元测试。

对应《暂停任务重启续跑实施计划》的验收用例(先红后绿):
- (A) 启动对账 migrate_stale_task_status:running/paused → FAILED + interrupted_at,
  completed/failed 不动;幂等再跑不刷新。用真实 PG 的独立 schema(库不可达时跳过)。
- (B) /resume 存活探针双分支:线程健在 → 唤醒(原逻辑);孤儿 → 落地中断标记 + 复活。
- (C) 存活探针 has_active_scope 委托语义;孤儿 /resume 后状态翻 FAILED,再点被 409 拒。
- (D) /retry 接受 FAILED + interrupted_at,且起跑即清空该标记。

(B)/(C)/(D) 全部用替身隔离 DB / 后台线程,无需数据库也能跑。
"""
import uuid
from datetime import datetime, timezone

import pytest

import app.routers.tasks as tasks
from app.models.task import TaskStatus


# ============================================================
# 通用替身:一个不落库的任务对象 + 假 session
# ============================================================


class _StubTask:
    """满足 resume/retry 端点所需属性读写的最小任务替身(不绑定任何 session)。"""

    def __init__(self, status: TaskStatus, **kw):
        self.id = kw.get("id") or uuid.uuid4()
        self.user_id = None  # None → 端点跳过鉴权
        self.status = status
        self.current_stage = kw.get("current_stage")
        self.error_message = kw.get("error_message")
        self.interrupted_at = kw.get("interrupted_at")


def _stub_db(task: _StubTask):
    """假 session:db.get(Task, ...) 返回替身,commit/refresh 空实现。"""
    from unittest.mock import MagicMock

    db = MagicMock()
    db.get.return_value = task
    return db


# ============================================================
# (C) 存活探针 has_active_scope:委托 _has_active_scope 的注册/注销语义
# ============================================================


def test_has_active_scope_tracks_event_scope():
    """无 scope → False;_begin_event_scope 注册后 → True;_end_event_scope 注销 → False。

    证明它是"线程存活"的可靠探针:阻塞在 wait_if_paused 的线程会一直持有 scope,
    线程死亡(scope 被 finally 清)则返回 False。
    """
    from app.agents.orchestrator import (
        _begin_event_scope,
        _end_event_scope,
        has_active_scope,
    )

    tid = str(uuid.uuid4())
    try:
        assert has_active_scope(tid) is False
        gen = _begin_event_scope(tid)
        assert has_active_scope(tid) is True
        _end_event_scope(tid, gen)
        assert has_active_scope(tid) is False
    finally:
        # 防测试间串扰:确保该 tid 的 scope 与世代计数被清干净
        from app.agents import orchestrator as orch

        with orch._review_lock:
            orch._event_scopes.pop(tid, None)
            orch._task_gens.pop(tid, None)


# ============================================================
# (B) /resume 存活分支:线程健在 → 唤醒,不复活
# ============================================================


def test_resume_alive_thread_wakes_without_relaunch(monkeypatch):
    """有活跃 scope:走 resume_task(),状态 RUNNING,不启动后台复活线程。"""
    tid = uuid.uuid4()
    task = _StubTask(TaskStatus.PAUSED, id=tid)
    db = _stub_db(task)

    monkeypatch.setattr(tasks, "has_active_scope", lambda _id: True)
    resumed = []
    monkeypatch.setattr(tasks, "resume_task", lambda _id: resumed.append(_id))
    relaunched = []
    monkeypatch.setattr(tasks, "_run_retry_in_background", lambda s: relaunched.append(s))
    monkeypatch.setattr(tasks, "publish", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "reset_task_bus", lambda *a, **k: None)

    resp = tasks.resume_task_endpoint(tid, db=db, current_user=None)

    assert resp["message"] == "任务已恢复"
    assert task.status is TaskStatus.RUNNING
    assert resumed and relaunched == []  # 唤醒原线程,未复活
    assert task.interrupted_at is None


# ============================================================
# (B) /resume 孤儿分支:线程已死 → 落地中断标记 + 复活
# ============================================================


def test_resume_orphan_self_heals_via_retry(monkeypatch):
    """无活跃 scope(孤儿):置 FAILED + interrupted_at,复活走 _run_retry_in_background 一次。"""
    tid = uuid.uuid4()
    task = _StubTask(TaskStatus.PAUSED, id=tid)
    db = _stub_db(task)

    monkeypatch.setattr(tasks, "has_active_scope", lambda _id: False)
    resumed = []
    monkeypatch.setattr(tasks, "resume_task", lambda _id: resumed.append(_id))
    relaunched = []
    monkeypatch.setattr(tasks, "_run_retry_in_background", lambda s: relaunched.append(s))
    published = []
    monkeypatch.setattr(tasks, "publish", lambda tid_, ev, data: published.append((ev, data)))
    reset = []
    monkeypatch.setattr(tasks, "reset_task_bus", lambda _id: reset.append(_id))

    resp = tasks.resume_task_endpoint(tid, db=db, current_user=None)

    assert "已从中断处继续执行" in resp["message"]
    assert resp["status"] == "running"
    assert task.status is TaskStatus.FAILED  # 落地"可继续的中断失败"
    assert isinstance(task.interrupted_at, datetime)
    assert task.error_message and "断点" in task.error_message
    assert resumed == []  # 孤儿绝不 set 无人等待的 Event
    assert relaunched == [str(tid)]  # 复活恰好一次
    assert reset == [tid]  # 与 /retry 同理先清 done/finished


def test_resume_orphan_second_click_rejected(monkeypatch):
    """连续点两次「继续」:第一次翻成 FAILED,第二次因状态非 PAUSED 被 409 拒。

    证明孤儿复活不会重复起线程 —— 复活后状态离开 PAUSED,后续 /resume 直接拒绝。
    """
    from fastapi import HTTPException

    tid = uuid.uuid4()
    task = _StubTask(TaskStatus.PAUSED, id=tid)
    db = _stub_db(task)
    monkeypatch.setattr(tasks, "has_active_scope", lambda _id: False)
    monkeypatch.setattr(tasks, "publish", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "reset_task_bus", lambda *a, **k: None)
    relaunched = []
    monkeypatch.setattr(tasks, "_run_retry_in_background", lambda s: relaunched.append(s))

    tasks.resume_task_endpoint(tid, db=db, current_user=None)
    assert relaunched == [str(tid)]

    # 第二次:状态已变 FAILED → 仅 PAUSED 可恢复 → 409
    with pytest.raises(HTTPException) as exc:
        tasks.resume_task_endpoint(tid, db=db, current_user=None)
    assert exc.value.status_code == 409
    assert relaunched == [str(tid)]  # 未再起第二个复活线程


# ============================================================
# (D) /retry 接受 FAILED + interrupted_at,起跑即清空该标记
# ============================================================


def test_retry_accepts_interrupted_and_clears_flag(monkeypatch):
    """FAILED + interrupted_at 仍被接受(accepted=True),且开始后 interrupted_at 被清空。"""
    tid = uuid.uuid4()
    task = _StubTask(
        TaskStatus.FAILED,
        id=tid,
        error_message="服务重启,执行线程已丢失(可从断点继续)",
        interrupted_at=datetime.now(timezone.utc),
    )
    db = _stub_db(task)
    # 端点会查最近一条对话定 round_idx:返回链式 mock,.first() → None
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

    monkeypatch.setattr(tasks, "publish", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "reset_task_bus", lambda *a, **k: None)
    # 拦截后台线程:记录 args,不真起线程
    started = []

    class _NoThread:
        def __init__(self, target=None, args=(), **kw):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(tasks.threading, "Thread", _NoThread)

    resp = tasks.retry_failed_task_endpoint(tid, db=db, current_user=None)

    assert resp.accepted is True
    assert task.interrupted_at is None  # 重试开始后该标记不该再挂
    assert task.status is TaskStatus.RUNNING
    assert started and started[0][1] == (str(tid),)


# ============================================================
# (A) 启动对账:真实 PG 独立 schema(库不可达时由 conftest 统一跳过)
# ============================================================

import os  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.task import (  # noqa: E402
    Task,
    migrate_stale_task_status,
    migrate_task_add_interrupted_column,
)

# 确保相关模型注册到 Base.metadata(create_all 才能建全表;Task FK 依赖 users)
import app.models.user  # noqa: E402,F401
import app.models.user_git_binding  # noqa: E402,F401

TEST_SCHEMA = f"pytest_orphan_{os.getpid()}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
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


@pytest.mark.db
def test_reconcile_marks_orphans_and_is_idempotent(test_engine):
    """running/paused → FAILED + interrupted_at;completed/failed 不动;再跑不刷新。"""
    Session = sessionmaker(bind=test_engine)

    def _seed():
        with Session() as db:
            db.query(Task).delete()
            base = dict(user_input="audit", scenario="general", interrupted_at=None)
            db.add_all([
                Task(id=uuid.uuid4(), status=TaskStatus.RUNNING, **base),
                Task(id=uuid.uuid4(), status=TaskStatus.PAUSED, **base),
                Task(id=uuid.uuid4(), status=TaskStatus.COMPLETED, **base),
                Task(id=uuid.uuid4(), status=TaskStatus.FAILED,
                     error_message="真失败", **base),
            ])
            db.commit()
            ids = {
                "running": db.query(Task).filter(Task.status == TaskStatus.RUNNING).first().id,
                "paused": db.query(Task).filter(Task.status == TaskStatus.PAUSED).first().id,
            }
        return ids

    ids = _seed()
    migrate_task_add_interrupted_column()  # 幂等:测试 schema 由 create_all 已建列 → 直接返回
    migrate_stale_task_status(bind=test_engine)

    with Session() as db:
        r = db.get(Task, ids["running"])
        p = db.get(Task, ids["paused"])
        c = db.query(Task).filter(Task.status == TaskStatus.COMPLETED).first()
        f = db.query(Task).filter(
            Task.status == TaskStatus.FAILED, Task.error_message == "真失败"
        ).first()
        assert r.status is TaskStatus.FAILED and r.interrupted_at is not None
        assert p.status is TaskStatus.FAILED and p.interrupted_at is not None
        assert c is not None and c.interrupted_at is None  # 未动的 completed
        assert f is not None and f.interrupted_at is None  # 既有真失败不受影响
        first_ts = r.interrupted_at

    # 幂等:再跑一次已无 running/paused → interrupted_at 不被刷新
    migrate_stale_task_status(bind=test_engine)
    with Session() as db:
        r2 = db.get(Task, ids["running"])
        assert r2.interrupted_at == first_ts


@pytest.mark.db
def test_status_enum_stored_by_member_name(test_engine):
    """锁定实现前提:SQLAlchemy 按成员**名称**(大写)落 PG 原生枚举。

    这正是对账必须走 ORM(而非裸 SQL 小写字面量)的原因:计划里手写
    `WHERE status IN ('running','paused')` 会因大小写错配而匹配不到。
    """
    Session = sessionmaker(bind=test_engine)
    tid = uuid.uuid4()
    with Session() as db:
        db.query(Task).delete()
        db.add(Task(id=tid, user_input="x", scenario="general", status=TaskStatus.PAUSED))
        db.commit()
    with test_engine.connect() as conn:
        raw = conn.execute(
            text("SELECT status FROM tasks WHERE id = :i"), {"i": str(tid)}
        ).scalar()
    assert str(raw).upper() == "PAUSED"
