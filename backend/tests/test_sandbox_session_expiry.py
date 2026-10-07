"""沙箱实例被回收(工作区过期)的识别与处置 单元测试(不连真实 Server)

背景:后端会话缓存与容器是两套生命周期 —— 会话为"供用户回看"保留
WORKSPACE_TTL_AFTER_COMPLETE(默认 24h),容器却按 SANDBOX_TIMEOUT_MINUTES(默认
30min)被 Server 回收。窗口期里 SDK 一律回 [DOCKER::SANDBOX_NOT_FOUND]。
旧版表现:浏览端点 500 且把 Docker 错误码 + request_id 贴进文件树、「重新克隆」
按钮因 available=true 永不出现、恢复流程拿死会话的 repo_path 当"已就绪"短路。

覆盖:
- client.is_sandbox_gone:认得出实例 404,也不会把路径 404 误判成过期
- SandboxSession:SDK 错误归一为 SandboxGoneError、probe_alive 的过期/瞬时分类、
  mark_gone 让 close 不再徒劳调 destroy
- sandbox_tools:探活丢弃死会话、get_workspace_info 如实报不可用、browse_* 抛
  SandboxGoneError、过期不再回退 shell、_get_or_create_session 重建新容器
- 路由:410 / 404 / 500 三档映射与未知错误截断
- workspace_restore.status:死会话不被对账成 done
- 克隆回退链 / execute_tool:过期不被聚合错误吞掉,并当场丢弃死会话
"""
import time
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import app.routers.workspace as ws_router
import app.services.workspace_restore as wr
from app.config import settings
from app.sandbox.client import (
    SANDBOX_GONE_MESSAGE,
    SandboxGoneError,
    SandboxSession,
    is_sandbox_gone,
)
from app.tools import sandbox_tools

# Server 回收后 SDK 抛出的原文(用户实际撞到的那条,原样保留用于验证识别口径)
GONE_MSG = (
    "Failed to run command failed: Sandbox 632edc0c-62af-4288-be46-57b1700f032e "
    "not found. | [DOCKER::SANDBOX_NOT_FOUND] Sandbox "
    "632edc0c-62af-4288-be46-57b1700f032e not found. | "
    "request_id=8c4fcc1d75ae4888ba6334576f17a539"
)
# 路径不存在:同样带 404,但绝不能被当成"实例已回收"
PATH_MISSING_MSG = "Status code: 404 File not exist: /repo/a.py"


class SdkError(RuntimeError):
    """形态对齐 SDK 的 SandboxApiException(纯 Exception 派生 + status_code)"""

    def __init__(self, msg: str, status_code: int = 404):
        super().__init__(msg)
        self.status_code = status_code


def _gone_sandbox() -> MagicMock:
    sb = MagicMock()
    err = SdkError(GONE_MSG)
    sb.renew.side_effect = err
    sb.commands.run.side_effect = err
    sb.files.list_directory.side_effect = err
    sb.files.get_file_info.side_effect = err
    sb.files.read_file.side_effect = err
    return sb


def _live_sandbox() -> MagicMock:
    sb = MagicMock()
    sb.renew.return_value = MagicMock(expires_at=None)
    return sb


def _sandbox_session(sb) -> SandboxSession:
    return SandboxSession(mode="sandbox", sandbox=sb)


@pytest.fixture
def task_id():
    return f"expiry-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
def _isolate():
    """用例之间隔离进程内会话缓存(本次新种的会话一律扫掉,不依赖用例自己收尾)"""
    before = set(sandbox_tools._sessions)
    yield
    for tid in set(sandbox_tools._sessions) - before:
        sandbox_tools._sessions.pop(tid, None)
    sandbox_tools._tree_cache.clear()


def _seed(task_id: str, session: SandboxSession, repo_path: str = "/repo") -> dict:
    ctx = {"session": session, "repo_path": repo_path, "mode": "sandbox"}
    sandbox_tools._sessions[task_id] = ctx
    return ctx


# ============================================================
# client:识别"实例已回收"
# ============================================================


def test_is_sandbox_gone_recognizes_server_error():
    assert is_sandbox_gone(SdkError(GONE_MSG)) is True
    # 只有错误码没有 uuid 措辞时也要认得
    assert is_sandbox_gone(SdkError("[DOCKER::SANDBOX_NOT_FOUND] gone")) is True


def test_is_sandbox_gone_not_fooled_by_path_404():
    """路径 404 与实例 404 在 SDK 里同码,靠文案分开:误判会白丢整个工作区"""
    assert is_sandbox_gone(SdkError(PATH_MISSING_MSG)) is False
    assert is_sandbox_gone(RuntimeError("connection reset by peer")) is False


def test_run_command_normalizes_gone():
    s = _sandbox_session(_gone_sandbox())
    with pytest.raises(SandboxGoneError) as ei:
        s.run_command("ls")
    assert str(ei.value) == SANDBOX_GONE_MESSAGE
    assert "request_id" not in str(ei.value)  # Docker 细节留在日志,不进文案


def test_run_command_keeps_path_semantics():
    """非过期异常原样抛出(交给调用方按"目录/文件不存在"处理)"""
    sb = MagicMock()
    sb.commands.run.side_effect = SdkError(PATH_MISSING_MSG)
    with pytest.raises(SdkError):
        _sandbox_session(sb).run_command("ls")


def test_list_directory_normalizes_gone_before_404_rule():
    """必须先判实例回收:list_directory 把 404 归一为 FileNotFoundError,
    若顺序颠倒,"沙箱没了"会被伪装成"目录不存在"(404 而非 410)"""
    s = _sandbox_session(_gone_sandbox())
    with pytest.raises(SandboxGoneError):
        s.list_directory("/repo")


def test_stat_size_normalizes_gone():
    s = _sandbox_session(_gone_sandbox())
    with pytest.raises(SandboxGoneError):
        s.stat_size("/repo/a.py")


# ============================================================
# client:活性探针
# ============================================================


def test_probe_alive_gone_returns_false():
    assert _sandbox_session(_gone_sandbox()).probe_alive() is False


def test_probe_alive_transient_error_keeps_session():
    """网络抖动不能被报成过期,否则一次瞬时故障就足以废掉已 clone 好的工作区"""
    sb = MagicMock()
    sb.renew.side_effect = RuntimeError("connection reset by peer")
    assert _sandbox_session(sb).probe_alive() is True


def test_probe_alive_renews_ttl():
    sb = _live_sandbox()
    assert _sandbox_session(sb).probe_alive() is True
    assert sb.renew.call_count == 1


def test_probe_alive_local_mode_is_true():
    s = SandboxSession(mode="local")
    try:
        assert s.probe_alive() is True
    finally:
        s.close()


def test_mark_gone_skips_destroy_on_close():
    """实例已经没了:close 不该再发 destroy(那是第二次必败的 404 往返)"""
    sb = _live_sandbox()
    s = _sandbox_session(sb)
    s.mark_gone()
    assert s.is_closed is True
    s.close()
    assert sb.kill.call_count == 0


def test_mark_gone_ignored_in_local_mode():
    """local 模式的 close() 负责删宿主临时目录,不能被 mark_gone 短路"""
    s = SandboxSession(mode="local")
    tmp_dir = s.local_dir
    s.mark_gone()
    assert s.is_closed is False
    s.close()
    assert not tmp_dir.exists()


# ============================================================
# sandbox_tools:探活 + 丢弃死会话
# ============================================================


def test_probe_drops_gone_session(task_id):
    ctx = _seed(task_id, _sandbox_session(_gone_sandbox()))
    ctx["_tree_cache_witness"] = True
    sandbox_tools._tree_cache[task_id] = (0.0, {"entries": []})

    assert sandbox_tools._probe_session(task_id, ctx) is False
    assert task_id not in sandbox_tools._sessions
    assert task_id not in sandbox_tools._tree_cache
    # 被标为已回收:后续任何销毁动作都不再打 Server
    assert ctx["session"].is_closed is True


def test_probe_throttled_within_interval(task_id):
    ctx = _seed(task_id, _sandbox_session(_gone_sandbox()))
    ctx["_last_probe"] = time.monotonic()
    # 窗口内不重复问 Server,按活着处理
    assert sandbox_tools._probe_session(task_id, ctx) is True
    assert task_id in sandbox_tools._sessions


def test_probe_local_mode_never_probes(task_id):
    """local 模式没有容器可问:不探活,也不会被误判过期"""
    session = SandboxSession(mode="local")
    try:
        ctx = {"session": session, "repo_path": "/repo", "mode": "local"}
        sandbox_tools._sessions[task_id] = ctx
        assert sandbox_tools._probe_session(task_id, ctx) is True
        assert task_id in sandbox_tools._sessions
    finally:
        session.close()


def test_get_workspace_info_reports_dead_session_unavailable(task_id):
    """核心可用性谎言:死会话的 repo_path 不能再让前端以为工作区可用"""
    _seed(task_id, _sandbox_session(_gone_sandbox()))
    assert sandbox_tools.get_workspace_info(task_id) is None
    assert task_id not in sandbox_tools._sessions


def test_browse_files_raises_gone_then_plain_unavailable(task_id):
    _seed(task_id, _sandbox_session(_gone_sandbox()))
    with pytest.raises(SandboxGoneError):
        sandbox_tools.browse_files(task_id)
    # 会话已丢弃:第二次落到"会话不存在"(前端此时已能看到重新克隆入口)
    with pytest.raises(RuntimeError, match="会话已过期清理"):
        sandbox_tools.browse_files(task_id)


def test_browse_read_file_raises_gone(task_id):
    _seed(task_id, _sandbox_session(_gone_sandbox()))
    with pytest.raises(SandboxGoneError):
        sandbox_tools.browse_read_file(task_id, "a.py")


def test_browse_tree_drops_session_when_command_reports_gone(task_id):
    """命令层(而非探活)发现的过期也要当场丢会话

    探活有 60s 节流,不丢的话死会话能谎报整整一个窗口:期间 POST restore 会被它
    的 repo_path 短路成"工作区已就绪"而根本不克隆,用户点完「重新克隆」就是一个
    空目录。丢弃之后下一次 checkAvailable 拿不到会话,无需等窗口到点就亮出按钮。
    """
    sb = MagicMock()
    sb.renew.return_value = MagicMock(expires_at=None)  # 探活说:还在
    sb.commands.run.side_effect = SdkError(GONE_MSG)     # find 说:容器已经没了
    _seed(task_id, _sandbox_session(sb))

    with pytest.raises(SandboxGoneError):
        sandbox_tools.browse_tree(task_id)
    assert task_id not in sandbox_tools._sessions


def test_browse_read_file_drops_session_when_command_reports_gone(task_id):
    """读文件同样反应式丢会话(不能只有 browse_files 一条路径会洗干净)"""
    sb = MagicMock()
    sb.renew.return_value = MagicMock(expires_at=None)
    sb.commands.run.side_effect = SdkError(GONE_MSG)
    _seed(task_id, _sandbox_session(sb))

    with pytest.raises(SandboxGoneError):
        sandbox_tools.browse_read_file(task_id, "a.py")
    assert task_id not in sandbox_tools._sessions


def test_set_repo_path_invalidates_tree_cache(task_id):
    """克隆改写工作区后必须失效整树快照

    否则恢复完成后的首屏仍命中 30s TTL 内那份"刚 mkdir -p、还没检出"的空快照,
    就是"克隆完成但目录是空的"另一个成因(后端不洗,前端只能靠 refresh=true)。
    """
    _seed(task_id, _sandbox_session(_live_sandbox()))
    sandbox_tools._tree_cache[task_id] = (time.time(), {"entries": [{"path": "a.py", "type": "file"}]})

    sandbox_tools._set_repo_path(task_id, "/home/user/repos/x")
    assert task_id not in sandbox_tools._tree_cache
    assert sandbox_tools._sessions[task_id]["repo_path"] == "/home/user/repos/x"


def test_browse_tree_raises_gone(task_id):
    _seed(task_id, _sandbox_session(_gone_sandbox()))
    with pytest.raises(SandboxGoneError):
        sandbox_tools.browse_tree(task_id)


def test_browse_tree_stale_cache_not_served_for_dead_session(task_id):
    """探活先于缓存:不能让 30s 旧快照继续假装工作区可用"""
    _seed(task_id, _sandbox_session(_gone_sandbox()))
    sandbox_tools._tree_cache[task_id] = (
        time.time(), {"entries": [{"path": "a.py", "type": "file"}]},
    )
    with pytest.raises(SandboxGoneError):
        sandbox_tools.browse_tree(task_id)
    assert task_id not in sandbox_tools._sessions


def test_browse_repo_root_requires_repo_path_before_gone(task_id):
    """会话在但还没 clone:文案仍是"尚未 clone"(探活通过后按 repo_path 判定)"""
    sb = _live_sandbox()
    _seed(task_id, _sandbox_session(sb), repo_path="")
    with pytest.raises(RuntimeError, match="尚未 clone"):
        sandbox_tools.browse_files(task_id)


# ============================================================
# sandbox_tools:过期不再走注定失败的 shell 回退
# ============================================================


def test_list_files_sandbox_does_not_fall_back_when_gone(monkeypatch, task_id):
    shell_called: list[int] = []
    monkeypatch.setattr(
        sandbox_tools,
        "_list_files_sandbox_shell",
        lambda *a, **k: shell_called.append(1),
    )
    ctx = _seed(task_id, _sandbox_session(_gone_sandbox()))
    with pytest.raises(SandboxGoneError):
        sandbox_tools._list_files_sandbox(ctx, "/repo", "", 200)
    assert shell_called == []


def test_list_files_sandbox_still_falls_back_on_other_errors(monkeypatch):
    """旧 Server 不支持该 API 时回退 shell 的行为不能丢"""
    sentinel = {"entries": [], "fallback": True}
    monkeypatch.setattr(
        sandbox_tools, "_list_files_sandbox_shell", lambda *a, **k: sentinel
    )
    sb = MagicMock()
    sb.files.list_directory.side_effect = SdkError("501 Not Implemented")
    ctx = {"session": _sandbox_session(sb), "repo_path": "/repo", "mode": "sandbox"}
    assert sandbox_tools._list_files_sandbox(ctx, "/repo", "", 200) is sentinel


def test_browse_tree_sandbox_does_not_degrade_when_gone(monkeypatch):
    single_level: list[int] = []
    monkeypatch.setattr(
        sandbox_tools,
        "_list_files_sandbox",
        lambda *a, **k: single_level.append(1) or {"entries": []},
    )
    ctx = {"session": _sandbox_session(_gone_sandbox()), "repo_path": "/repo"}
    with pytest.raises(SandboxGoneError):
        sandbox_tools._browse_tree_sandbox(ctx, "/repo", 4, 3000)
    assert single_level == []


# ============================================================
# sandbox_tools:复用会话时发现已回收 → 重建新容器
# ============================================================


def test_session_reuse_recreates_when_container_gone(monkeypatch, task_id):
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")
    dead = _sandbox_session(_gone_sandbox())
    _seed(task_id, dead, repo_path="/home/user/repos/old")

    fresh = _sandbox_session(_live_sandbox())
    created: list[int] = []

    def _create(extra_volumes=None):
        created.append(1)
        return fresh

    monkeypatch.setattr(sandbox_tools, "create_sandbox", _create)

    ctx = sandbox_tools._get_or_create_session(task_id)
    assert created == [1]
    assert ctx["session"] is fresh
    # 新容器是空的:旧的 repo_path 不能带过来骗后续流程"工作区还在"
    assert ctx["repo_path"] == ""
    assert ctx is sandbox_tools._sessions[task_id]


def test_session_reuse_keeps_live_container(monkeypatch, task_id):
    """探活通过 → 原会话照用,不重建容器(克隆好的工作区不能白白丢)"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")
    live = _sandbox_session(_live_sandbox())
    _seed(task_id, live, repo_path="/home/user/repos/keep")
    ctx = sandbox_tools._get_or_create_session(task_id)
    assert ctx["session"] is live
    assert ctx["repo_path"] == "/home/user/repos/keep"


# ============================================================
# 路由:410 / 404 / 500 三档
# ============================================================


def _task(params=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.user_id = None
    t.params = params if params is not None else {}
    return t


def _db(task):
    db = MagicMock()
    db.get.side_effect = lambda model, tid: task
    return db


@pytest.fixture
def _no_cleanup(monkeypatch):
    monkeypatch.setattr(
        sandbox_tools, "cleanup_expired_sessions_bg", lambda: None
    )


def _list_files(task, subdir=""):
    return ws_router.list_workspace_files(
        task.id, subdir=subdir, db=_db(task), current_user=None
    )


def test_route_expired_maps_to_410(monkeypatch, _no_cleanup):
    """过期不是故障:410 + 面向用户的文案,绝不把 Docker 错误码贴进界面"""
    monkeypatch.setattr(
        sandbox_tools, "browse_files", lambda *a, **k: (_ for _ in ()).throw(
            SandboxGoneError(SANDBOX_GONE_MESSAGE)
        )
    )
    with pytest.raises(HTTPException) as ei:
        _list_files(_task())
    assert ei.value.status_code == 410
    assert ei.value.detail == SANDBOX_GONE_MESSAGE
    assert "request_id" not in ei.value.detail


def test_route_unavailable_maps_to_404(monkeypatch, _no_cleanup):
    monkeypatch.setattr(
        sandbox_tools, "browse_files",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("工作区不可用:尚未 clone 仓库")),
    )
    with pytest.raises(HTTPException) as ei:
        _list_files(_task())
    assert ei.value.status_code == 404


def test_route_missing_path_maps_to_404(monkeypatch, _no_cleanup):
    monkeypatch.setattr(
        sandbox_tools, "browse_files",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("目录不存在: src")),
    )
    with pytest.raises(HTTPException) as ei:
        _list_files(_task())
    assert ei.value.status_code == 404


def test_route_path_traversal_maps_to_404(monkeypatch, _no_cleanup):
    """穿越(ValueError)与不存在同语义,不透露存在但在根外"""
    monkeypatch.setattr(
        sandbox_tools, "browse_files",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("非法路径:不能超出仓库根目录")),
    )
    with pytest.raises(HTTPException) as ei:
        _list_files(_task())
    assert ei.value.status_code == 404


def test_route_unknown_error_is_500_and_truncated(monkeypatch, _no_cleanup):
    """未知异常仍 500,但正文截断:一整个 traceback 不该原样进界面"""

    class Boom(Exception):
        pass

    monkeypatch.setattr(
        sandbox_tools, "browse_files",
        lambda *a, **k: (_ for _ in ()).throw(Boom("y" * 5000)),
    )
    with pytest.raises(HTTPException) as ei:
        _list_files(_task())
    assert ei.value.status_code == 500
    assert ei.value.detail.startswith("列出文件失败:")
    assert len(ei.value.detail) < 400


def test_route_download_expired_maps_to_410(monkeypatch, _no_cleanup):
    """下载端点同样分流:它在开流前 stat_size,过期必须回 410 而不是半截文件"""
    def _gone(*a, **k):
        raise SandboxGoneError(SANDBOX_GONE_MESSAGE)

    monkeypatch.setattr(sandbox_tools, "browse_download", _gone)
    task = _task()
    with pytest.raises(HTTPException) as ei:
        ws_router.download_workspace_file(
            task.id, path="a.docx", db=_db(task), current_user=None
        )
    assert ei.value.status_code == 410


# ============================================================
# 恢复流程:死会话不再被当成"已就绪"
# ============================================================


def test_restore_endpoint_does_not_short_circuit_on_dead_session(monkeypatch):
    """POST restore:容器已回收 → 真正发起克隆(旧版直接回 done + 100%)"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")
    task = _task(params={"repo_url": "https://example.com/r.git"})
    _seed(str(task.id), _sandbox_session(_gone_sandbox()))

    started: list[tuple] = []

    def _fake_start(tid, repo_url, branch, tokens):
        started.append((tid, repo_url))
        return {"state": "running", "percent": 0, "message": "", "error": "",
                "available": False, "repo_path": "", "mode": ""}

    monkeypatch.setattr(ws_router.workspace_restore, "start", _fake_start)

    snap = ws_router.restore_workspace(task.id, db=_db(task), current_user=None)
    assert snap["state"] == "running"
    assert started == [(str(task.id), "https://example.com/r.git")]
    # 死会话已被探活丢弃,不会再把"已就绪"透给后面的流程
    assert str(task.id) not in sandbox_tools._sessions


def test_restore_status_not_done_for_dead_session(task_id):
    """轮询对账:running 的 job 不能因为死会话的 repo_path 被判成 done"""
    _seed(task_id, _sandbox_session(_gone_sandbox()))
    wr.reset_for_tests()
    wr._jobs[task_id] = {
        "state": "running", "percent": 5, "message": "", "error": "",
        "available": False, "repo_path": "", "mode": "",
        "started_at": time.time(), "finished_at": 0.0,
    }
    snap = wr.status(task_id)
    assert snap["state"] == "running"
    wr.reset_for_tests()


# ============================================================
# 克隆回退链 / 工具层:过期不被吞成聚合错误
# ============================================================


def test_clone_fallback_reraises_gone_without_retrying_protocols(monkeypatch, task_id):
    """容器没了 → 直接上抛:协议回退每一种都打同一个不存在的实例,聚合错误
    只会把真正原因埋进"已尝试 N 种协议"里(还白耗几分钟克隆超时)"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")
    _seed(task_id, _sandbox_session(_live_sandbox()))

    attempts: list[str] = []

    def _gone_clone(ctx, url, *a, **k):
        attempts.append(url)
        raise SandboxGoneError(SANDBOX_GONE_MESSAGE)

    monkeypatch.setattr(sandbox_tools, "_clone_repo_sandbox", _gone_clone)

    with pytest.raises(SandboxGoneError):
        sandbox_tools.clone_repo_with_fallback(
            "https://gitee.com/shwsq/overleaf.git", branch="main", task_id=task_id
        )
    assert len(attempts) <= 1, f"过期后仍在试其他协议: {attempts}"


def test_execute_tool_drops_gone_session_for_rebuild(monkeypatch, task_id):
    """工具层报错时也当场丢弃死会话:下一次工具调用就能拿到新容器

    不等探活节流(最长 60s),否则整轮工具调用全部砸在同一个死会话上,
    模型只会反复看到同一个错。
    """
    from app.tools import schema

    _seed(task_id, _sandbox_session(_live_sandbox()))
    monkeypatch.setitem(
        schema.TOOL_FUNCTIONS, "list_files",
        lambda **kw: (_ for _ in ()).throw(SandboxGoneError(SANDBOX_GONE_MESSAGE)),
    )
    schema.set_current_task(task_id)

    with pytest.raises(SandboxGoneError):
        schema.execute_tool("list_files", {"repo_path": "/repo"})
    assert task_id not in sandbox_tools._sessions
