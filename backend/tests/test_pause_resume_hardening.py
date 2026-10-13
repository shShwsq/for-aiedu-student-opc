"""暂停/恢复 DB 断连修复加固(Fix1/2/3)单元测试。

对应计划的三条修复,全部用替身隔离真实 DB / 沙箱,验证"去脆化"契约:
- Fix1.1 database.persist_task_failure_fresh:全新独立会话落 FAILED,
  防覆盖已终态;坏连接时 dispose 重试一次;两轮皆炸返回 False。
- Fix1.2/1.3 后台包装器崩溃兜底:主连接可能已死时,事件推送(publish error)
  与新会话落库解耦,且 scope 清理不被跳过。
- Fix2.2 pause_controller.release_connection:commit 失败退 rollback,皆失败吞异常;
  wait_if_paused 命中阻塞前先释放传入 session 的连接。
- Fix3 orchestrator._ensure_workspace_for_continuation:无仓库/工作区健在→None;
  工作区被回收→重克隆并返回 repo_context;重克隆失败→降级 None。
"""
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

import app.database as database
import app.pause_controller as pause_controller
from app.models.task import TaskStatus
from app.pause_controller import (
    clear_pause_state,
    pause_task,
    release_connection,
    resume_task,
    wait_if_paused,
)


# ============================================================
# Fix2.2 release_connection(阻塞前释放连接)
# ============================================================


def test_release_connection_commits_when_healthy():
    """活 session:commit 成功即归还连接,不应触发 rollback 分支。"""
    db = MagicMock()
    release_connection(db)
    db.commit.assert_called_once()
    db.rollback.assert_not_called()


def test_release_connection_falls_back_to_rollback_on_dead_conn():
    """死连接:commit 抛错(连接被服务端掐断)→ 退回 rollback,不外抛。"""
    db = MagicMock()
    db.commit.side_effect = OperationalError("stmt", {}, Exception("server closed"))
    release_connection(db)  # 不得抛异常
    db.rollback.assert_called_once()


def test_release_connection_swallows_double_failure():
    """commit 与 rollback 都抛错:兜底路径全程吞异常,绝不影响等待主流程。"""
    db = MagicMock()
    db.commit.side_effect = RuntimeError("boom")
    db.rollback.side_effect = RuntimeError("boom2")
    release_connection(db)  # 不抛即通过


def test_release_connection_none_is_noop():
    """未持有 session 的调用方(db=None):直接跳过,零副作用。"""
    release_connection(None)


def test_wait_if_paused_releases_before_blocking():
    """命中暂停:阻塞前先释放传入 session;恢复后正常返回。"""
    tid = str(uuid.uuid4())
    try:
        pause_task(tid)  # 置暂停 → wait_if_paused 会 park
        db = MagicMock()
        released = {"called": False}

        def _commit():
            released["called"] = True

        db.commit.side_effect = _commit

        def _resume_later():
            time.sleep(0.05)
            resume_task(tid)

        threading.Thread(target=_resume_later, daemon=True).start()
        wait_if_paused(tid, db)  # 释放连接 → park → 被 resume 唤醒
        assert released["called"] is True
    finally:
        clear_pause_state(tid)


def test_wait_if_paused_not_paused_skips_release():
    """未暂停:立即返回,不释放(零开销,不产生多余 commit)。"""
    tid = str(uuid.uuid4())
    db = MagicMock()
    wait_if_paused(tid, db)  # 默认运行态 → 不 park,不释放
    db.commit.assert_not_called()
    clear_pause_state(tid)


# ============================================================
# Fix1.1 persist_task_failure_fresh(新会话终态兜底)
# ============================================================


class _FakeORMTask:
    def __init__(self, status):
        self.status = status
        self.error_message = None
        self.current_stage = None


def _patch_session(monkeypatch, get_side_effect):
    """替换 database.SessionLocal 与 engine,返回 (会话工厂替身, dispose 记录)。"""
    fake_db = MagicMock()
    fake_db.get.side_effect = get_side_effect
    created = []

    def _factory():
        created.append(fake_db)
        return fake_db

    monkeypatch.setattr(database, "SessionLocal", _factory)
    disposed = {"count": 0}
    monkeypatch.setattr(
        database.engine, "dispose", lambda: disposed.__setitem__("count", disposed["count"] + 1),
    )
    return fake_db, disposed


def test_persist_failure_sets_failed_and_returns_true(monkeypatch):
    """RUNNING 任务:新会话置 FAILED + 落错误/阶段,返回 True。"""
    tid = uuid.uuid4()
    task = _FakeORMTask(TaskStatus.RUNNING)
    fake_db, _ = _patch_session(monkeypatch, lambda *a, **k: task)
    ok = database.persist_task_failure_fresh(tid, "连接被掐断", "执行失败")
    assert ok is True
    assert task.status is TaskStatus.FAILED
    assert task.error_message == "连接被掐断"
    assert task.current_stage == "执行失败"
    fake_db.commit.assert_called_once()


def test_persist_failure_does_not_overwrite_terminal(monkeypatch):
    """已是 COMPLETED 终态:视为无需再落,返回 True 但不覆盖、不 commit。"""
    tid = uuid.uuid4()
    task = _FakeORMTask(TaskStatus.COMPLETED)
    fake_db, _ = _patch_session(monkeypatch, lambda *a, **k: task)
    ok = database.persist_task_failure_fresh(tid, "err", "stage")
    assert ok is True
    assert task.status is TaskStatus.COMPLETED  # 未被改写
    fake_db.commit.assert_not_called()


def test_persist_failure_retries_after_dispose(monkeypatch):
    """首撞坏连接(OperationalError):dispose 丢弃池连接后重试成功。"""
    tid = uuid.uuid4()
    task = _FakeORMTask(TaskStatus.RUNNING)
    calls = {"n": 0}

    def _get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("s", {}, Exception("server closed"))
        return task

    fake_db, disposed = _patch_session(monkeypatch, _get)
    fake_db.commit.return_value = None
    ok = database.persist_task_failure_fresh(tid, "err", "stage")
    assert ok is True
    assert disposed["count"] == 1  # 重试前 dispose 一次
    assert task.status is TaskStatus.FAILED


def test_persist_failure_returns_false_when_task_missing(monkeypatch):
    """任务不存在:get 返回 None → False(不落、不报错)。"""
    tid = uuid.uuid4()
    _patch_session(monkeypatch, lambda *a, **k: None)
    assert database.persist_task_failure_fresh(tid, "err", "stage") is False


def test_persist_failure_exhausts_retries_returns_false(monkeypatch):
    """两轮皆 OperationalError:吞异常并返回 False(兜底失败交由调用方日志)。"""
    tid = uuid.uuid4()

    def _get(*a, **k):
        raise OperationalError("s", {}, Exception("still down"))

    _patch_session(monkeypatch, _get)
    assert database.persist_task_failure_fresh(tid, "err", "stage") is False


# ============================================================
# Fix1.2/1.3 后台包装器崩溃兜底:事件推送与 DB 解耦
# ============================================================


def test_run_task_background_falls_back_and_publishes(monkeypatch):
    """run_pipeline_audit 崩溃:主连接落库改用新会话兜底 + 必推 error + 清 scope。"""
    import app.routers.tasks as tasks

    tid = str(uuid.uuid4())
    fake_db = MagicMock()
    fake_db.get.return_value = object()  # 任务存在
    monkeypatch.setattr(tasks, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(
        tasks, "run_pipeline_audit",
        MagicMock(side_effect=OperationalError("s", {}, Exception("dead"))),
    )
    persisted = []
    monkeypatch.setattr(
        tasks, "persist_task_failure_fresh",
        lambda tid_, err, stage: persisted.append((tid_, stage)),
    )
    published = []
    monkeypatch.setattr(
        tasks, "publish",
        lambda tid_, ev, data: published.append((ev, data)),
    )
    cleaned = []
    monkeypatch.setattr(
        tasks, "force_cleanup_event_scopes",
        lambda tid_: cleaned.append(tid_),
    )
    monkeypatch.setattr(tasks.logger, "exception", lambda *a, **k: None)

    tasks._run_task_in_background(tid)

    assert persisted and persisted[0][0] == tid  # 新会话兜底落 FAILED
    assert any(ev == "error" for ev, _ in published)  # 前端可收终态
    assert cleaned == [tid]  # scope 泄漏已防
    fake_db.close.assert_called_once()


def test_run_resume_background_falls_back_and_publishes(monkeypatch):
    """resume 后台崩溃:同样走新会话兜底 + 推 error + 收尾 scope,不复用死连接。"""
    import app.agents.orchestrator as orchestrator

    tid = str(uuid.uuid4())
    fake_db = MagicMock()
    fake_db.get.return_value = object()
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(
        orchestrator, "resume_audit_with_message",
        MagicMock(side_effect=OperationalError("s", {}, Exception("dead"))),
    )
    persisted = []
    monkeypatch.setattr(
        orchestrator, "persist_task_failure_fresh",
        lambda tid_, err, stage: persisted.append((tid_, stage)),
    )
    published = []
    monkeypatch.setattr(
        orchestrator, "publish",
        lambda tid_, ev, data: published.append((ev, data)),
    )
    scoped = []
    monkeypatch.setattr(
        orchestrator, "_end_event_scope",
        lambda tid_, gen: scoped.append(tid_),
    )

    orchestrator._run_resume_in_background(tid, "继续", flow_gen=1)

    assert persisted and persisted[0][0] == tid
    assert any(ev == "error" for ev, _ in published)
    assert scoped == [tid]  # finally/except 收尾 scope
    fake_db.close.assert_called_once()


# ============================================================
# Fix3 _ensure_workspace_for_continuation(续轮前探活+重克隆)
# ============================================================


class _TaskStub:
    def __init__(self, params):
        self.id = uuid.uuid4()
        self.params = params
        self.current_stage = None


def test_ensure_workspace_no_repo_returns_none(monkeypatch):
    """纯上传任务(无 repo_url):跳过探活,返回 None,不碰沙箱/DB。"""
    import app.agents.orchestrator as orchestrator

    task = _TaskStub({})
    db = MagicMock()
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "workspace_has_files",
        MagicMock(side_effect=AssertionError("不应被调用")),
    )
    assert orchestrator._ensure_workspace_for_continuation(task, db, str(task.id), {}) is None
    db.commit.assert_not_called()


def test_ensure_workspace_alive_returns_none(monkeypatch):
    """工作区健在(有文件):无需重克隆,返回 None(续轮照旧 repo_ctx=None)。"""
    import app.agents.orchestrator as orchestrator

    task = _TaskStub({"repo_url": "https://x/y.git"})
    db = MagicMock()
    monkeypatch.setattr(orchestrator.sandbox_tools, "workspace_has_files", lambda tid: True)
    prep = MagicMock()
    monkeypatch.setattr(orchestrator, "_prepare_repo_context", prep)
    assert orchestrator._ensure_workspace_for_continuation(task, db, str(task.id), {}) is None
    prep.assert_not_called()


def test_ensure_workspace_reclones_when_reclaimed(monkeypatch):
    """工作区被回收(无文件):重克隆并返回其 repo_context。"""
    import app.agents.orchestrator as orchestrator

    task = _TaskStub({"repo_url": "https://x/y.git"})
    db = MagicMock()
    monkeypatch.setattr(orchestrator.sandbox_tools, "workspace_has_files", lambda tid: False)
    monkeypatch.setattr(orchestrator, "_publish_status", lambda t: None)
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context",
        lambda t, d, tid, gt: ("/workspace/repo", "REPO_CONTEXT_TEXT"),
    )
    ctx = orchestrator._ensure_workspace_for_continuation(task, db, str(task.id), {})
    assert ctx == "REPO_CONTEXT_TEXT"


def test_ensure_workspace_reclone_failure_degrades_to_none(monkeypatch):
    """重克隆抛错:仅告警,降级返回 None(续轮由 CLI 侧自主 clone)。"""
    import app.agents.orchestrator as orchestrator

    task = _TaskStub({"repo_url": "https://x/y.git"})
    db = MagicMock()
    monkeypatch.setattr(orchestrator.sandbox_tools, "workspace_has_files", lambda tid: False)
    monkeypatch.setattr(orchestrator, "_publish_status", lambda t: None)
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context",
        MagicMock(side_effect=RuntimeError("clone failed")),
    )
    assert orchestrator._ensure_workspace_for_continuation(task, db, str(task.id), {}) is None
