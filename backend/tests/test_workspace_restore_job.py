"""工作区恢复后台任务表(app/services/workspace_restore.py)用例

为什么异步:restore 端点过去在请求里同步克隆大仓库,分钟级耗时先被前端 axios 的
30s 全局超时打断,extractErrorMessage 对"无 response"一律显示"网络错误,请检查网络
连接后重试"—— 后端几分钟后成功也没人接收,用户看到的是假报错。现在 POST 立即返回
job 快照,克隆在后台线程跑,进度与真实失败原因走 GET .../restore/status
(端点用例见 test_workspace_restore.py)。
"""
import time
import uuid

import pytest

import app.services.workspace_restore as wr
from app.tools import sandbox_tools

REPO_URL = "https://gitee.com/shwsq/overleaf.git"


@pytest.fixture(autouse=True)
def _isolate_jobs():
    """job 表是进程内状态:用例之间必须隔离"""
    wr.reset_for_tests()
    yield
    wr.reset_for_tests()


def _seed(task_id: str, **fields) -> None:
    job = {
        "state": "running", "percent": 0, "message": "", "error": "",
        "available": False, "repo_path": "", "mode": "",
        "started_at": time.time(), "finished_at": 0.0,
    }
    job.update(fields)
    wr._jobs[task_id] = job


def _stub_clone(monkeypatch, *, ok=True, path="/tmp/sandbox_local_x/overleaf", err=""):
    """把克隆换成同步假实现,并屏蔽沙箱副作用"""
    calls: list[dict] = []

    def _clone(url, branch=None, task_id="", git_tokens=None, progress_callback=None, **kw):
        calls.append({"url": url, "branch": branch, "task_id": task_id, "tokens": git_tokens})
        if not ok:
            raise RuntimeError(err)
        if progress_callback:
            progress_callback(45, "Receiving objects:  45% (1/2)")
        return {"path": path, "files_count": 6579}

    monkeypatch.setattr(sandbox_tools, "clone_repo_with_fallback", _clone)
    monkeypatch.setattr(sandbox_tools, "get_workspace_info", lambda _tid: {})
    marked: list[str] = []
    monkeypatch.setattr(sandbox_tools, "mark_task_completed", lambda tid: marked.append(tid))
    return calls, marked


def _wait_terminal(task_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = wr.status(task_id)
        if snap and snap["state"] != "running":
            return snap
        time.sleep(0.01)
    raise AssertionError("job 未在预期时间内进入终态")


# ============================================================
# 生命周期
# ============================================================


def test_start_completes_and_marks_session_completed(monkeypatch):
    """成功:后台克隆 → done + 工作区路径 + 标记 completed 纳入 TTL 清理序列。"""
    task_id = uuid.uuid4().hex
    calls, marked = _stub_clone(monkeypatch)

    job = wr.start(task_id, REPO_URL, "main", {"gitee": "t"})
    # 快照就是当前态:命中缓存的快克隆可能在返回前已跑完(前端两种都要能接)
    assert job["state"] in ("running", "done")
    final = _wait_terminal(task_id)
    assert final["state"] == "done"
    assert final["available"] is True
    assert final["percent"] == 100
    assert final["repo_path"] == "/tmp/sandbox_local_x/overleaf"
    # 参数原样透传给回退链(分支/git tokens 都在这条路上)
    assert calls == [{
        "url": REPO_URL, "branch": "main", "task_id": task_id, "tokens": {"gitee": "t"},
    }]
    assert marked == [task_id]


def test_repo_path_prefers_workspace_info(monkeypatch):
    """done 后的 repo_path 以 workspace info 为准(避让换名时与克隆返回值可能不同)。"""
    task_id = uuid.uuid4().hex
    _stub_clone(monkeypatch, path="/tmp/x/overleaf-2")
    monkeypatch.setattr(
        sandbox_tools, "get_workspace_info",
        lambda _tid: {"repo_path": "/tmp/x/overleaf-2", "mode": "local"},
    )
    wr.start(task_id, REPO_URL)
    assert _wait_terminal(task_id)["repo_path"] == "/tmp/x/overleaf-2"


def test_progress_callback_reaches_status(monkeypatch):
    """克隆进度写进 job:前端轮询拿得到实时百分比。

    进度走不了 event_bus —— 任务早已结束、总线已 finish,clone_progress 会被丢弃,
    所以 clone_repo_with_fallback 才有 progress_callback。
    """
    task_id = uuid.uuid4().hex
    _stub_clone(monkeypatch)
    wr.start(task_id, REPO_URL)
    final = _wait_terminal(task_id)
    assert final["state"] == "done"
    assert final["percent"] == 100


def test_failure_surfaces_real_clone_error(monkeypatch):
    """失败:把协议回退链的聚合错误原样带给前端,而不是让超时替它编个"网络错误"。"""
    task_id = uuid.uuid4().hex
    err = (
        "仓库克隆失败(已尝试 2 种协议 x 1 种分支策略):\n"
        "[git@gitee.com:shwsq/overleaf.git] git clone 失败(退出码 128): fatal: early EOF\n"
        "[https://gitee.com/shwsq/overleaf.git] git clone 失败: error: unable to create"
        " file a/deep/path: Filename too long"
    )
    _stub_clone(monkeypatch, ok=False, err=err)
    wr.start(task_id, REPO_URL, "main", {})
    final = _wait_terminal(task_id)
    assert final["state"] == "failed"
    assert "Filename too long" in final["error"]
    assert final["available"] is False


def test_start_reuses_running_job(monkeypatch):
    """已有 running job:复用快照,不再排第二次克隆(多入口/多标签页点按钮)。"""
    task_id = uuid.uuid4().hex
    _seed(task_id, percent=30, message="Receiving objects:  30% (1/2)")
    calls, _ = _stub_clone(monkeypatch)
    snap = wr.start(task_id, REPO_URL, "main", {})
    assert snap["state"] == "running"
    assert snap["percent"] == 30
    time.sleep(0.05)
    assert calls == []


def test_start_restarts_after_previous_failure(monkeypatch):
    """上次 failed 后点重试:覆盖旧 job,真的再克隆一次。"""
    task_id = uuid.uuid4().hex
    _seed(task_id, state="failed", error="上次失败", finished_at=time.time())
    calls, _ = _stub_clone(monkeypatch)
    wr.start(task_id, REPO_URL, "main", {})
    _wait_terminal(task_id)
    assert len(calls) == 1


def test_status_reconciles_when_other_entry_finished(monkeypatch):
    """running 但工作区已被别的入口(如出题前恢复)克隆好 → 判 done,前端不空转。"""
    task_id = uuid.uuid4().hex
    _seed(task_id, percent=5)
    monkeypatch.setattr(
        sandbox_tools, "get_workspace_info",
        lambda _tid: {"repo_path": "/tmp/x/overleaf", "mode": "local"},
    )
    snap = wr.status(task_id)
    assert snap["state"] == "done"
    assert snap["repo_path"] == "/tmp/x/overleaf"
    assert snap["mode"] == "local"


def test_status_none_when_never_started():
    """从未发起过 → None(端点据此返回 idle)。"""
    assert wr.status(uuid.uuid4().hex) is None


def test_finished_jobs_purged_after_ttl():
    """结束超 TTL 的 job 惰性清扫,防进程内存常驻。"""
    task_id = uuid.uuid4().hex
    _seed(task_id, state="done", finished_at=time.time() - wr._FINISHED_TTL - 1)
    assert wr.status(task_id) is None


def test_snapshot_hides_internal_fields():
    """finished_at 只用于 TTL 清扫,不外泄给前端。"""
    task_id = uuid.uuid4().hex
    _seed(task_id)
    assert "finished_at" not in wr.status(task_id)
