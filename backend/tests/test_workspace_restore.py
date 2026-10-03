"""工作区恢复端点测试(两段式:POST 发起 + GET status 轮询)

直接调用路由函数(mock db + sandbox_tools),覆盖:
- 权限路径:任务不存在 404 / 他人任务 403 / 匿名任务可访问
- session 存活 → 幂等返回 done(不发起 job)
- 任务无 repo_url → 400
- 工作区不可用 → 立即返回 running 快照(旧版在本请求里同步克隆,被前端 axios 30s
  超时误报成"网络错误";job 生命周期见 test_workspace_restore_job.py)
- GET status:有 job 原样返回;无 job 回落 idle 并带当前可用性;同样过权限
"""
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import app.routers.workspace as ws_router
import app.services.practice.generator as gen


def _task(user_id="u1", params=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.user_id = user_id
    t.params = (
        params if params is not None
        else {"repo_url": "https://example.com/r.git", "branch": "dev"}
    )
    return t


def _db(task):
    db = MagicMock()
    db.get.side_effect = lambda model, tid: task
    return db


def _user(uid="u1"):
    u = MagicMock()
    u.id = uid
    return u


# ============================================================
# 权限路径
# ============================================================


def test_restore_task_not_found():
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(uuid.uuid4(), db, None)
    assert ei.value.status_code == 404


def test_restore_other_user_task_forbidden():
    task = _task(user_id="owner")
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(task.id, _db(task), _user("intruder"))
    assert ei.value.status_code == 403


def test_restore_anonymous_task_allowed(monkeypatch):
    """匿名任务(user_id 为空)未登录也可恢复"""
    task = _task(user_id=None)
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "sandbox"},
    )
    res = ws_router.restore_workspace(task.id, _db(task), None)
    assert res["available"] is True


# ============================================================
# 业务路径
# ============================================================


def test_restore_alive_session_idempotent(monkeypatch):
    """session 仍存活且已 clone:直接返回 done,不发起恢复 job"""
    task = _task()
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "sandbox"},
    )

    def _boom(*a, **k):
        raise AssertionError("工作区已就绪时不应发起 job")

    monkeypatch.setattr(ws_router.workspace_restore, "start", _boom)
    res = ws_router.restore_workspace(task.id, _db(task), _user())
    assert res["state"] == "done"
    assert res["available"] is True
    assert res["repo_path"] == "/repo"
    assert res["mode"] == "sandbox"


def test_restore_no_repo_url_400(monkeypatch):
    task = _task(params={})
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info", lambda tid: None,
    )
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace(task.id, _db(task), _user())
    assert ei.value.status_code == 400


def test_restore_starts_job_and_returns_running(monkeypatch):
    """工作区不可用:POST 立即返回 running 快照,克隆参数交给后台 job。

    旧版在此处同步等克隆(大仓库分钟级)并靠 500 报错——前端拿到的是 axios
    超时后的"网络错误"。现在请求本身不再承载耗时。
    """
    task = _task()
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info", lambda tid: None,
    )
    captured: list[tuple] = []

    def _fake_start(tid, repo_url, branch, tokens):
        captured.append((tid, repo_url, branch, tokens))
        return {
            "state": "running", "percent": 0, "message": "正在准备克隆...",
            "error": "", "available": False, "repo_path": "", "mode": "",
        }

    monkeypatch.setattr(ws_router.workspace_restore, "start", _fake_start)
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {"github": "tk"})

    res = ws_router.restore_workspace(task.id, _db(task), _user())
    assert res["state"] == "running"
    assert res["available"] is False
    assert captured == [
        (str(task.id), "https://example.com/r.git", "dev", {"github": "tk"}),
    ]


def test_status_returns_job_snapshot(monkeypatch):
    """GET status:有 job 时原样返回(前端据此显示进度与终态)。"""
    task = _task()
    job = {
        "state": "running", "percent": 60, "message": "Receiving objects:  60% (1/2)",
        "error": "", "available": False, "repo_path": "", "mode": "",
    }
    monkeypatch.setattr(ws_router.workspace_restore, "status", lambda tid: job)
    assert ws_router.restore_workspace_status(task.id, _db(task), _user()) == job


def test_status_idle_when_no_job(monkeypatch):
    """无 job(从未发起 / 后端重启清掉内存表):回落 idle,并带当前真实可用性。"""
    task = _task()
    monkeypatch.setattr(ws_router.workspace_restore, "status", lambda tid: None)
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "local"},
    )
    res = ws_router.restore_workspace_status(task.id, _db(task), _user())
    assert res["state"] == "idle"
    assert res["available"] is True
    assert res["repo_path"] == "/repo"


def test_status_respects_permission():
    """status 轮询同样过权限:他人任务 403。"""
    task = _task(user_id="owner")
    with pytest.raises(HTTPException) as ei:
        ws_router.restore_workspace_status(task.id, _db(task), _user("intruder"))
    assert ei.value.status_code == 403
