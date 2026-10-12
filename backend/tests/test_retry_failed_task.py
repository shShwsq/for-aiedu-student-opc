"""orchestrator.retry_failed_task 单测(纯函数级,mock DB/沙箱/执行链路)。

核心约束:
- 无 agent2/react_agent 对话(早期失败)→ 从头重跑 run_pipeline_audit
- 有进度(执行中途失败)→ 断点续跑 resume_audit_with_message(retry=True)
- 续跑前沙箱会话已被回收且任务配了 repo_url → 重新 _prepare_repo_context
"""
from unittest.mock import MagicMock

import app.agents.orchestrator as orchestrator
from app.agents.orchestrator import retry_failed_task


def _mk_task(repo_url="https://github.com/a/b", error_message="LLM 超时"):
    task = MagicMock()
    task.id = "task-1"
    task.user_id = None
    task.params = {"repo_url": repo_url}
    task.error_message = error_message
    return task


def _mk_db(has_progress: bool) -> MagicMock:
    """构造 mock db:控制 has_progress 查询结果(Conversation.role.in_ 分支)"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        ("conv-1",) if has_progress else None
    )
    return db


def _patch_env(monkeypatch, session_alive=True):
    """屏蔽执行链路/沙箱/perf 副作用,只测分流逻辑。"""
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _task: None)
    monkeypatch.setattr(orchestrator, "_load_git_tokens", lambda *a, **kw: {})
    monkeypatch.setattr(
        orchestrator, "set_current_git_tokens", lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context", MagicMock(return_value=(None, "")),
    )
    monkeypatch.setattr(
        orchestrator, "run_pipeline_audit", MagicMock(),
    )
    monkeypatch.setattr(
        orchestrator, "resume_audit_with_message", MagicMock(),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info",
        lambda _tid: ({"repo_path": "/r", "mode": "local"} if session_alive else None),
    )


def test_no_progress_reruns_from_scratch(monkeypatch):
    """早期失败(无 agent 对话)→ 从头重跑,且清除 error_message。"""
    _patch_env(monkeypatch)
    task = _mk_task()
    db = _mk_db(has_progress=False)

    retry_failed_task(task, db)

    orchestrator.run_pipeline_audit.assert_called_once_with(task, db)
    orchestrator.resume_audit_with_message.assert_not_called()
    assert task.error_message is None


def test_with_progress_resumes_with_retry_flag(monkeypatch):
    """中途失败(有 agent 对话)+ 会话存活 → 断点续跑,不重新准备仓库。"""
    _patch_env(monkeypatch, session_alive=True)
    task = _mk_task()
    db = _mk_db(has_progress=True)

    retry_failed_task(task, db)

    orchestrator.run_pipeline_audit.assert_not_called()
    orchestrator.resume_audit_with_message.assert_called_once()
    args, kwargs = orchestrator.resume_audit_with_message.call_args
    # (task, db, retry_message, retry=True);续跑消息须带上次失败原因
    assert args[0] is task and args[1] is db
    assert "LLM 超时" in args[2]
    assert kwargs.get("retry") is True
    orchestrator._prepare_repo_context.assert_not_called()


def test_session_gone_reprepares_repo_before_resume(monkeypatch):
    """中途失败 + 会话已被回收 + 配了 repo_url → 先重新 clone 再续跑。"""
    _patch_env(monkeypatch, session_alive=False)
    task = _mk_task()
    db = _mk_db(has_progress=True)

    retry_failed_task(task, db)

    orchestrator._prepare_repo_context.assert_called_once()
    orchestrator.resume_audit_with_message.assert_called_once()
    _, kwargs = orchestrator.resume_audit_with_message.call_args
    assert kwargs.get("retry") is True


def test_session_gone_without_repo_url_skips_reprepare(monkeypatch):
    """中途失败 + 会话被回收但任务未配仓库 → 无需 clone,直接续跑。"""
    _patch_env(monkeypatch, session_alive=False)
    task = _mk_task(repo_url=None)
    task.params = {}
    db = _mk_db(has_progress=True)

    retry_failed_task(task, db)

    orchestrator._prepare_repo_context.assert_not_called()
    orchestrator.resume_audit_with_message.assert_called_once()
