"""工作区恢复(重新 clone)的后台任务表

为什么必须异步:一次 clone 是分钟级操作(大仓库 + 慢镜像 + 协议/分支回退),旧实现
把它放在 POST /tasks/{id}/workspace/restore 里同步等,而前端 axios 客户端有全局
30s 超时(client.ts)—— 请求先被打断,extractErrorMessage 对"无 response"一律显示
"网络错误,请检查网络连接后重试",后端却仍在克隆:用户看到一个假报错,真实结果
几分钟后才落,已经没人接收。

改成"发起 + 轮询":POST 立即返回 job 快照,克隆在新线程里跑,进度由
clone_repo_with_fallback 的 progress_callback 写进本表,前端轮询 status 端点取。
进度走不了 event_bus:任务早已结束、总线已 finish,clone_progress 会被丢弃
(那也正是 progress_callback 存在的理由,见 sandbox_tools._clone_repo_local)。

表在进程内存:沿用单 worker 部署假设(与 pause_controller / clone_skip / event_bus
一致)。进程重启后 job 与沙箱 session 一起消失,status 返回 idle,用户可重新发起。
"""
import logging
import threading
import time
from typing import Any

from app.tools import sandbox_tools

logger = logging.getLogger(__name__)

# 已结束 job 的保留时长(秒):够前端取到最终状态;超时惰性清扫,防内存常驻
_FINISHED_TTL = 600.0

# 对外快照字段(内部计时字段不外泄)
_SNAPSHOT_FIELDS = (
    "state", "percent", "message", "error", "available", "repo_path", "mode",
    "started_at",
)

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job[k] for k in _SNAPSHOT_FIELDS if k in job}


def _purge_locked(now: float) -> None:
    """清掉结束超过 TTL 的 job(在锁内调用;每次 start/status 顺带触发)"""
    for tid in [
        t for t, j in _jobs.items()
        if j["state"] != "running" and now - j.get("finished_at", 0.0) > _FINISHED_TTL
    ]:
        _jobs.pop(tid, None)


def _update(task_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(task_id)
        if job is not None:
            job.update(fields)


def start(
    task_id: str, repo_url: str,
    branch: str | None = None, git_tokens: dict | None = None,
) -> dict[str, Any]:
    """发起(或复用)一次工作区恢复,立即返回 job 快照

    running 中的 job 直接复用:重复点击在克隆锁后只是把同一件事再排一遍(按钮禁用
    是第一道防线,这里挡多入口/多标签页)。done/failed 的旧 job 被新请求覆盖,
    用户点"重试"就是想要一次新的克隆。
    """
    with _lock:
        _purge_locked(time.time())
        existing = _jobs.get(task_id)
        if existing is not None and existing["state"] == "running":
            return _snapshot(existing)
        job: dict[str, Any] = {
            "state": "running",
            "percent": 0,
            "message": "正在准备克隆...",
            "error": "",
            "available": False,
            "repo_path": "",
            "mode": "",
            "started_at": time.time(),
            "finished_at": 0.0,
        }
        _jobs[task_id] = job

    threading.Thread(
        target=_run,
        args=(task_id, repo_url, branch, git_tokens or {}),
        daemon=True,
        name=f"workspace-restore-{task_id[:8]}",
    ).start()
    return _snapshot(job)


def _run(
    task_id: str, repo_url: str, branch: str | None, git_tokens: dict,
) -> None:
    """后台线程:克隆仓库恢复工作区,进度与终态写回 job(任何异常归入 failed)"""
    try:
        result = sandbox_tools.clone_repo_with_fallback(
            repo_url,
            branch=branch,
            task_id=task_id,
            git_tokens=git_tokens,
            progress_callback=lambda percent, message: _update(
                task_id, percent=percent, message=message[:200],
            ),
        )
    except Exception as e:
        # 真实原因(克隆失败的首因)整段带前端,不再让 axios 超时替它编一个"网络错误"
        err = str(e) or f"{type(e).__name__}(无错误详情)"
        logger.warning("[task=%s] 工作区恢复失败: %s", task_id, err[:500])
        _update(task_id, state="failed", error=err[:1000], finished_at=time.time())
        return

    # 恢复出的 session 属于已完成任务:标记 completed 纳入 TTL 清理序列,防常驻泄漏
    sandbox_tools.mark_task_completed(task_id)
    info = sandbox_tools.get_workspace_info(task_id) or {}
    repo_path = info.get("repo_path") or result.get("path", "")
    logger.info("[task=%s] 工作区恢复完成: %s", task_id, repo_path)
    _update(
        task_id,
        state="done", percent=100, message="克隆完成", error="",
        available=bool(repo_path), repo_path=repo_path, mode=info.get("mode", ""),
        finished_at=time.time(),
    )


def status(task_id: str) -> dict[str, Any] | None:
    """job 快照;无 job 返回 None(前端据此回落 idle 态)

    running 但工作区其实已就绪(别的入口先完成了克隆,如出题前恢复)→ 直接判 done,
    不让前端一直转圈等一个不会再有进展的 job。
    """
    with _lock:
        _purge_locked(time.time())
        job = _jobs.get(task_id)
        snap = _snapshot(job) if job is not None else None

    if snap is not None and snap["state"] == "running":
        info = sandbox_tools.get_workspace_info(task_id)
        if info and info.get("repo_path"):
            _update(
                task_id,
                state="done", percent=100, message="工作区已就绪",
                available=True, repo_path=info["repo_path"],
                mode=info.get("mode", ""), finished_at=time.time(),
            )
            with _lock:
                job = _jobs.get(task_id)
                return _snapshot(job) if job is not None else None
    return snap


def reset_for_tests() -> None:
    """清空任务表(仅测试用:job 是进程内状态,用例之间需要隔离)"""
    with _lock:
        _jobs.clear()
