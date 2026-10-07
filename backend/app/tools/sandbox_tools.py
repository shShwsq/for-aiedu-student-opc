"""沙箱版工具实现(阶段 2 起)

所有工具都通过 SandboxSession 执行,接口与 local_tools.py 保持一致。

local 模式:沙箱会话的 run_command 走本地 subprocess,但 Windows 不支持
         mkdir -p / find / rg 等 Unix 命令,所以 local 模式下直接用
         Python 实现,绕过 shell
sandbox 模式:走真实沙箱,在 Linux 容器里执行 Unix 命令
"""
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

from app.clone_skip import consume_skip_clone
from app.config import settings
from app.event_bus import publish as publish_event
# 二进制判定与占位文案(单一来源:三处读取路径与下载守卫共用,口径不一致则行为分裂)
from app.file_kinds import (
    BINARY_PLACEHOLDER,
    NUL_PROBE_BYTES,
    has_nul_bytes,
    is_likely_binary,
)
from app.git_provider import get_provider_for_url
from app.pause_controller import wait_if_paused
from app.perf import perf_log, perf_timer
from app.sandbox.client import (
    SANDBOX_GONE_MESSAGE,
    SandboxGoneError,
    SandboxSession,
    check_local_write_permission,
    create_sandbox,
)
from app.services.repo_cache import cache_key as repo_cache_key
from app.services.repo_cache import ensure_bare_cache, force_rmtree, sandbox_mount
from app.services.repo_cache import normalize_repo_url
# 上传布局纯函数(单一来源:沙箱拷贝与工作区回退浏览共用,布局不一致则回退路径全错)
from app.services.upload_layout import SKIP_DIRS_LIST as _SKIP_DIRS_LIST
from app.services.upload_layout import safe_dirname as _safe_dirname
from app.user_interaction import (
    request_command_confirm,
    wait_for_command_confirm,
)

logger = logging.getLogger(__name__)


# 全局缓存 task_id -> (SandboxSession, repo_path, local_dir, completed_at)
# local 模式下,local_dir 是本地临时目录(复用 SandboxSession.local_dir),工具用 Python 直接操作
# sandbox 模式下,repo_path 是沙箱内的路径
# completed_at: 任务完成时间(用于延迟清理,任务结束后保留 session 供前端浏览工作区)
_sessions: dict[str, dict[str, Any]] = {}

# 任务完成后保留 session 的时间(秒),超时后自动清理:
# 由 settings.WORKSPACE_TTL_AFTER_COMPLETE 配置(默认 24h,原硬编码 3600)

# 整树快照缓存:task_id -> (写入时间戳, payload),TTL 秒。
# 前端文件树首屏一次拉整树,短 TTL 兼顾运行中任务的变更新鲜度
_TREE_CACHE_TTL = 30.0
_tree_cache: dict[str, tuple[float, dict]] = {}

# 后台清理:请求路径限流间隔(秒),避免频繁扫描
_CLEANUP_SCAN_INTERVAL = 60.0
_last_cleanup_scan = 0.0
_cleanup_scan_lock = threading.Lock()

# 沙箱探活节流(秒):会话缓存与容器是两套生命周期 —— 会话为"供用户回看"保留
# WORKSPACE_TTL_AFTER_COMPLETE(默认 24h),容器却按 SANDBOX_TIMEOUT_MINUTES(默认
# 30min)被 Server 回收。光看 ctx 里有没有 repo_path 分辨不出这段"看着完好、实
# 际已死"的窗口,只能问 Server。1 分钟一次既够及时,又不至于让每个浏览请求都
# 多一次往返。探活本身顺带续期,所以活跃浏览的工作区不会被 TTL 扫掉。
_SANDBOX_PROBE_INTERVAL = 60.0

# 同任务克隆互斥锁:task_id -> RLock
# 同一任务的 clone 入口不止一个(orchestrator 预克隆、降级后的 clone_repo 工具、
# 工作区恢复路由、出题前工作区恢复)。并发时两个调用都看到 repo_path 为空,并向
# 同一个会话目录动手 —— 后者必撞 git 的 "already exists"。用可重入锁把同任务
# 的克隆排成"一次真克隆 + 一次幂等复用"(同线程重入不会自锁)。
_clone_locks: dict[str, threading.RLock] = {}
_clone_locks_guard = threading.Lock()


def _get_clone_lock(task_id: str) -> threading.RLock:
    """取(或建)该任务的克隆互斥锁;session 关闭时随 close_session 一并清除"""
    with _clone_locks_guard:
        lock = _clone_locks.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _clone_locks[task_id] = lock
        return lock


# 项目记忆文件固定路径(沙箱内绝对路径,不分 project_id;每任务启动时覆盖为当前项目记忆)
# 智能体不知道 project_id,固定路径降低认知负担;"分项目"靠每任务只写当前项目记忆实现。
_MEMORY_DIR_SANDBOX = "/home/user/.agent_memory"
_MEMORY_FILE = "project_memory.md"
# 全局长期记忆文件(跨项目通用经验,每任务启动时覆盖为当前用户的全局记忆)
_GLOBAL_MEMORY_FILE = "global_memory.md"

# local 会话元信息文件名(临时目录根,进程重启后孤儿恢复按它定位 task)
_LOCAL_SESSION_META_FILE = ".secondlook_meta.json"


def _get_or_create_session(
    task_id: str,
    repo_url: str | None = None,
    branch: str | None = None,
    git_tokens: dict | None = None,
) -> dict[str, Any]:
    """获取或创建任务的沙箱上下文

    复用已有会话前先探活(顺带续期 TTL):容器已被 Server 回收时丢掉旧会话并
    重建一个新容器 —— 不这么做,"过期"会是个永久态:任何命令都回
    SANDBOX_NOT_FOUND,而恢复流程又复用 `_sessions` 里那个死会话、拿 repo_path
    当"已就绪"短路,用户点重新克隆也救不回来。

    重建出的新容器是空的(agent 下一个工具调用会看到"目录不存在"进而重
    clone)。这里不做自动重 clone:克隆需要 repo_url/git_tokens/协议与分支回退链,
    那是 clone_repo_with_fallback 的职责,不属于会话层。

    repo_url/branch/git_tokens(可选,仅新建会话时生效;复用已有会话的
    调用方不感知——容器已建好,挂载无法追加):
    - sandbox 模式:先 ensure bare 仓库缓存,再把本任务仓库自己的 bare
      目录只读挂载进容器(clone 时从挂载路径秒级本地克隆)。缓存任何
      失败仅 warning,照常建会话(clone 阶段自动降级全量远程克隆)。
    - local 模式:不在此接缓存(clone 阶段直接从宿主机 bare 目录克隆),
      只写会话元信息供进程重启后孤儿目录恢复。
    """
    ctx = _sessions.get(task_id)
    if ctx is not None and _probe_session(task_id, ctx):
        # [perf] 复用已有会话(无容器创建开销)
        perf_log(task_id, "sandbox_session", reused=True)
        return ctx
    if ctx is not None:
        logger.warning(
            f"[task={task_id}] 沙箱实例已被回收,重建会话(工作区为空,需重新 clone)"
        )

    extra_volumes: list[tuple[str, str, bool]] = []
    if settings.SANDBOX_MODE == "sandbox" and repo_url:
        try:
            bare_dir = ensure_bare_cache(
                repo_url, branch=branch, git_tokens=git_tokens, task_id=task_id
            )
            mount_info = sandbox_mount(repo_url)
            if bare_dir and mount_info:
                # 只读挂载本任务仓库的 bare 子目录(非缓存根,跨租户隔离)
                extra_volumes.append((mount_info[0], mount_info[1], True))
        except Exception as e:
            logger.warning(
                f"[sandbox] task={task_id} 仓库缓存挂载准备失败"
                f"(降级全量远程克隆): {e}"
            )
    # [perf] 新建沙箱会话(拉镜像/启容器/等 healthy,可能是大耗时点)
    with perf_timer(task_id, "sandbox_session", reused=False, mode=settings.SANDBOX_MODE):
        session = create_sandbox(extra_volumes=extra_volumes or None)
    ctx = {"session": session, "repo_path": "", "mode": settings.SANDBOX_MODE}
    # local 模式:复用 SandboxSession 自有的本地临时目录(单一临时目录,
    # 避免过去 session 一份、ctx 一份的双份临时目录问题)
    if settings.SANDBOX_MODE == "local":
        ctx["local_dir"] = session.local_dir
        _write_local_session_meta(task_id, session.local_dir)
    # sandbox 模式:缓存挂载成功才记录,clone 阶段据此走容器内本地克隆
    if settings.SANDBOX_MODE == "sandbox" and len(extra_volumes) == 1 and repo_url:
        ctx["cache_key"] = repo_cache_key(repo_url)
        ctx["cache_mount"] = extra_volumes[0][1]
    # 创建即起算 TTL。_last_probe 同时是"上次续期时刻":探活本体就是 renew,
    # 两个时间戳分开记只会多一次多余的 Server 往返
    ctx["_last_probe"] = time.monotonic()
    _sessions[task_id] = ctx
    return ctx


def _probe_session(task_id: str, ctx: dict[str, Any]) -> bool:
    """按需探活(节流 _SANDBOX_PROBE_INTERVAL):容器已回收就丢掉本地会话

    返回 False 表示"这个 ctx 不能用"(会话已被本函数丢弃),调用方要么重建
    会话(_get_or_create_session),要么把过期如实告知前端(browse_* 抛
    SandboxGoneError)。

    local 模式恒为 True:工作区是宿主机临时目录,回收由我们自己控制,问 Server
    既无意义也无从问起。session 不是 SandboxSession 实例(测试替身等)时同样
    不判定 —— 没有可靠探针就宁可当活着,不能瞎猜过期。
    """
    if ctx.get("mode") != "sandbox":
        return True
    session = ctx.get("session")
    if not isinstance(session, SandboxSession):
        return True
    if session.is_closed:
        # 已经关掉(或被标为已回收)的会话不该继续留在缓存里给人虚假的"可用"
        drop_gone_session(task_id)
        return False

    now = time.monotonic()
    # ctx 里没这个时间戳 = 从没探过活,立即探一次(拿 0.0 当默认值会在刚开机
    # 不满一个节流窗口的机器上把"从未探活"误当成"刚探过")
    last_probe = ctx.get("_last_probe")
    if last_probe is not None and now - last_probe < _SANDBOX_PROBE_INTERVAL:
        return True
    ctx["_last_probe"] = now

    # 探活成功即等于 TTL 已往后推(_last_probe 兼任"上次续期时刻")
    if session.probe_alive():
        return True

    logger.warning(
        f"[task={task_id}] 沙箱实例已被回收,丢弃本地会话(工作区需重新克隆)"
    )
    drop_gone_session(task_id)
    return False


def drop_gone_session(task_id: str) -> None:
    """丢掉容器已被回收的会话(不调 destroy:实例早没了,再问只会撞回 404)

    与 close_session 的分工:那条服务于"仍然活着的沙箱"的正常销毁(兜底捕获
    diff、停 bridge、销毁容器),这些动作全依赖沙箱活着;这里只清理后端侧状态。

    先 mark_gone() 再清 bridge 缓存:bridge 驻在已消失的容器里,标了 gone 之后的
    interrupt 会短路,只把 `_bridge_cache` 里那个指向死会话的条目摘掉(否则后续
    轮次会拿着一个连不上的 endpoint 反复重试)。

    `_clone_locks` 不动:克隆可能正在别的线程里跑着这把锁,摘掉它等于给并发克隆
    放行;它终会随 close_session / 下次重建被复用。
    """
    ctx = _sessions.pop(task_id, None)
    if ctx is None:
        return
    _tree_cache.pop(task_id, None)

    session = ctx.get("session")
    if isinstance(session, SandboxSession):
        session.mark_gone()

    try:
        # 延迟导入避免循环依赖(acp_base 依赖 sandbox_tools)
        from app.agents.acp_base import stop_task_bridge
        stop_task_bridge(task_id)
    except Exception as e:
        logger.warning(f"[task={task_id}] 清 ACP bridge 缓存失败(忽略): {e}")


@contextmanager
def _browse_call(task_id: str) -> Iterator[Any]:
    """浏览/下载调用的统一善后:命令层暴露的过期也要当场丢会话

    为什么不能只靠探活:探活有 _SANDBOX_PROBE_INTERVAL(60s)节流,而"命令真的打
    不到容器"才是实例已回收的第一手证据。不在这里丢会话,死会话最长能谎报 60s:
    期间 /workspace 仍回 available=true、POST restore 的幂等短路直接回"工作区已
    就绪"(根本不克隆),前端点完「重新克隆」就是一个空目录 + 消失的恢复入口。
    丢了之后下一次 checkAvailable 拿不到会话,无需等节流窗口到点就能亮出按钮。
    """
    try:
        yield
    except SandboxGoneError:
        drop_gone_session(task_id)
        raise


def precreate_session_for_repo(
    task_id: str, repo_url: str,
    branch: str | None = None, git_tokens: dict | None = None,
) -> None:
    """提前创建会话(sandbox 模式:预建 bare 缓存并只读挂载进容器)

    幂等:已有会话直接复用(不感知参数)。必须在任何其他会话创建调用
    (如任务启动时的记忆文件写入)之前调用——否则会话已建、容器无法
    追加挂载,缓存机会不可逆丢失。local 模式无副作用(缓存在 clone 阶段接)。
    """
    _get_or_create_session(
        task_id, repo_url=repo_url, branch=branch, git_tokens=git_tokens
    )


def _write_local_session_meta(task_id: str, local_dir: Path) -> None:
    """local 模式:写会话元信息到临时目录根(进程重启后孤儿目录恢复用,B2)

    文件为根级隐藏文件;workspace 浏览只列 repo_path 子目录,不会污染工作区列表。
    写失败仅 warning(不影响会话创建)。
    """
    try:
        meta_path = Path(local_dir) / _LOCAL_SESSION_META_FILE
        meta_path.write_text(
            json.dumps(
                {"task_id": task_id, "created_at": time.time()}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"[task={task_id}] 写会话元信息失败(忽略): {e}")


def _set_repo_path(task_id: str, repo_path: str) -> None:
    """记下工作区路径,并失效整树快照

    克隆会改写整个工作区(先 rm -rf 再 mkdir -p 后检出),之前缓存的快照从此就
    是另一个目录的状态:不丢的话,恢复完前端首屏拉一次仍能命中 30s 内的旧快照,
    看到"克隆完成但文件树是空的"。
    """
    if task_id in _sessions:
        _sessions[task_id]["repo_path"] = repo_path
    _tree_cache.pop(task_id, None)


def mark_task_completed(task_id: str) -> None:
    """标记任务完成(不关闭 session,延迟清理供前端浏览工作区)

    orchestrator 在任务结束后调用此方法而非 close_session,
    保留 session 让用户能在前端查看工作区文件结构。
    实际清理由 cleanup_expired_sessions() 在后续请求中惰性触发。
    """
    if task_id in _sessions:
        _sessions[task_id]["completed_at"] = time.time()


def cleanup_expired_sessions() -> int:
    """清理过期的已完成 session(TTL 超时)

    在 workspace 路由每次访问时调用,惰性清理。
    返回清理的 session 数。
    """
    now = time.time()
    expired = [
        tid for tid, ctx in _sessions.items()
        if ctx.get("completed_at")
        and now - ctx["completed_at"] > settings.WORKSPACE_TTL_AFTER_COMPLETE
    ]
    for tid in expired:
        close_session(tid, save_diff=True)
    return len(expired)


def cleanup_expired_sessions_bg() -> None:
    """惰性清理(非阻塞版,供 workspace 请求路径调用)

    内联只做时间戳扫描(限流:每 _CLEANUP_SCAN_INTERVAL 最多一次);
    实际 close_session 销毁丢给 daemon 线程,避免过期沙箱销毁
    (停 ACP bridge / 销毁容器)阻塞当前 HTTP 请求造成秒级尖刺。
    """
    global _last_cleanup_scan
    with _cleanup_scan_lock:
        now = time.time()
        if now - _last_cleanup_scan < _CLEANUP_SCAN_INTERVAL:
            return
        _last_cleanup_scan = now
        expired = [
            tid for tid, ctx in _sessions.items()
            if ctx.get("completed_at")
            and now - ctx["completed_at"] > settings.WORKSPACE_TTL_AFTER_COMPLETE
        ]
    if not expired:
        return

    def _cleanup() -> None:
        for tid in expired:
            try:
                close_session(tid, save_diff=True)
            except Exception as e:
                logger.warning(f"[task={tid}] 后台清理过期 session 失败: {e}")

    threading.Thread(target=_cleanup, name="session-cleanup", daemon=True).start()


def close_session(task_id: str, save_diff: bool = False) -> None:
    """关闭沙箱,清理资源

    save_diff=True(TTL 过期清理路径):销毁前兜底捕获工作区 diff——任务已完成
    但尚无 kind="git_diff" artifact(完成时捕获曾失败/异常路径)则捕获保存。
    捕获必须在 _sessions.pop 之前(workspace_diff.capture 依赖会话);
    删除任务路径(routers/tasks.py)传 False(任务即将删除,保存无意义)。
    """
    if task_id not in _sessions:
        return
    if save_diff:
        ctx_peek = _sessions.get(task_id) or {}
        if ctx_peek.get("completed_at") and ctx_peek.get("repo_path"):
            try:
                # 延迟导入避免循环依赖(workspace_diff 依赖 sandbox_tools)
                from app.services.workspace_diff import save_diff_best_effort_on_close
                save_diff_best_effort_on_close(task_id)
            except Exception as e:
                logger.warning(f"[task={task_id}] 清理前兜底保存 diff 失败(忽略): {e}")
    ctx = _sessions.pop(task_id)
    _tree_cache.pop(task_id, None)
    _clone_locks.pop(task_id, None)
    session: SandboxSession = ctx["session"]
    try:
        # 延迟导入避免循环依赖(acp_base 依赖 sandbox_tools)
        # 停掉驻留沙箱的 ACP bridge(若存在)并清其复用缓存
        from app.agents.acp_base import stop_task_bridge
        stop_task_bridge(task_id)
    except Exception as e:
        logger.warning(f"[task={task_id}] 停止 ACP bridge 失败(忽略): {e}")
    try:
        # local 模式下 session.close() 会清理统一临时目录(含 clone/workspace/memory)
        session.close()
    except Exception as e:
        logger.warning(f"[task={task_id}] 关闭沙箱失败: {e}")


def get_workspace_info(task_id: str) -> dict[str, Any] | None:
    """获取任务的工作区信息(供前端浏览)

    返回 None 表示 session 不存在(任务未执行 clone、已清理,或容器已被 Server
    回收而会话刚被丢弃)。
    返回 dict: { repo_path, mode, completed }

    这里必须探活而不能只看 ctx:容器被回收后 ctx 仍带着完好的 repo_path,拿它
    判 available=true 的后果是前端去列文件、收到一个过期错误,而「重新克隆」
    按钮的渲染条件是 available=false —— 永远不出现,用户既看不了也没恢复入口。
    恢复流程(workspace_restore.status / POST restore)也走这条,否则会被那个死
    ctx 的 repo_path 骗成"已就绪"而根本不重克隆。
    """
    ctx = _sessions.get(task_id)
    if ctx is None:
        return None
    if not _probe_session(task_id, ctx):
        return None
    return {
        "repo_path": ctx.get("repo_path", ""),
        "mode": ctx.get("mode", ""),
        "completed": "completed_at" in ctx,
    }


def browse_files(task_id: str, subdir: str = "") -> dict:
    """面向前端的文件列表(复用 list_files 逻辑)

    与 list_files 工具的区别:
    - 不需要传 repo_path(从 _sessions 取)
    - task_id 必填(前端按任务浏览)
    - 返回结构一致,前端可直接渲染树

    抛出:RuntimeError(无会话/未 clone)/ SandboxGoneError(容器已回收)/
          FileNotFoundError(目录不存在)
    """
    ctx, repo_path = _browse_repo_root(task_id)

    # 复用 list_files 的实现(local / sandbox 分支)
    mode = ctx["mode"]
    with _browse_call(task_id):
        if mode == "local":
            return _list_files_local(repo_path, subdir, 500)
        return _list_files_sandbox(ctx, repo_path, subdir, 500)


def workspace_has_files(task_id: str) -> bool:
    """轻量探测工作区根目录是否有实际条目

    供追问轮决定是否注入工作区路径提示(react_agent 中性措辞"文件已就位";
    acp_base 仅对真实仓库任务附"已 clone")。预 clone 可能失败
    降级为空目录(见预克隆失败降级处理),此时声称"已就位"会误导
    执行 agent 跳过获取动作。任何异常(session 过期 / 未 clone / 目录
    不存在 / 沙箱命令失败)均视为无文件。
    """
    try:
        listing = browse_files(task_id)
        return bool(listing.get("entries"))
    except Exception:
        return False


def browse_tree(
    task_id: str,
    max_depth: int = 4,
    max_entries: int = 3000,
    refresh: bool = False,
) -> dict:
    """面向前端的整树快照(首屏一次往返出整树,替代逐级懒加载)

    返回扁平结构:{
        "entries": [{"path": "src/main.py", "type": "file"|"dir"}, ...],
        "truncated": bool,       # 条目超上限被截断(前端未覆盖目录退回懒加载)
        "max_depth": int,        # 快照实际覆盖深度(降级时可能小于请求值)
    }

    结果带短 TTL 缓存(_TREE_CACHE_TTL),refresh=True 绕过。

    探活先于缓存:否则过期后的 30s 窗口里会拿旧快照继续报"可用",点开文件才
    发现读不到。drop_gone_session 会一并清掉树缓存。
    """
    ctx, repo_path = _browse_repo_root(task_id)

    cached = _tree_cache.get(task_id)
    if not refresh and cached is not None and time.time() - cached[0] < _TREE_CACHE_TTL:
        return cached[1]

    with _browse_call(task_id):
        if ctx["mode"] == "local":
            payload = _browse_tree_local(repo_path, max_depth, max_entries)
        else:
            payload = _browse_tree_sandbox(ctx, repo_path, max_depth, max_entries)

    _tree_cache[task_id] = (time.time(), payload)
    return payload


def _browse_tree_local(repo_path: str, max_depth: int, max_entries: int) -> dict:
    """local 模式:os.walk 剪枝遍历,条目上限截断"""
    root = Path(repo_path).resolve()
    entries: list[dict] = []
    truncated = False

    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        parts = [] if rel == "." else rel.replace("\\", "/").split("/")
        child_depth = len(parts) + 1
        # 剪噪声目录;超出深度则不再下钻
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS_LIST)
        if child_depth > max_depth:
            dirnames[:] = []
            continue
        for name in dirnames:
            entries.append({"path": "/".join(parts + [name]), "type": "dir"})
        for name in sorted(filenames):
            entries.append({"path": "/".join(parts + [name]), "type": "file"})
        if len(entries) > max_entries:
            truncated = True
            break

    return {
        "entries": entries[:max_entries],
        "truncated": truncated,
        "max_depth": max_depth,
    }


def _browse_tree_sandbox(ctx: dict, repo_path: str, max_depth: int, max_entries: int) -> dict:
    """sandbox 模式:单条 find 命令拉整树快照(服务端剪枝噪声目录)

    用 find 而非 SDK list_directory(depth=N):find 能在沙箱内剪掉
    .git/node_modules 等噪声目录,避免大仓库撑爆响应。
    find 不可用(镜像缺 findutils)时降级为根目录单层列出,树退回懒加载。
    """
    session: SandboxSession = ctx["session"]
    prune_expr = " -o ".join(f"-name {shlex.quote(d)}" for d in sorted(_SKIP_DIRS_LIST))
    # %y=类型字符 %P=相对起点路径;head 限流防大仓库输出失控
    cmd = (
        f"find {shlex.quote(repo_path)} -maxdepth {max_depth} "
        f"\\( {prune_expr} \\) -prune -o -printf '%y\\t%P\\n' "
        f"| head -n {max_entries + 1}"
    )

    def _fallback_single_level() -> dict:
        listing = _list_files_sandbox(ctx, repo_path, "", max_entries)
        return {
            "entries": [
                {"path": e["name"], "type": e["type"]} for e in listing["entries"]
            ],
            "truncated": True,
            "max_depth": 1,
        }

    try:
        output = session.run_command(cmd, timeout=30)
    except SandboxGoneError:
        # 容器没了就明说:降级走单层列出同样会抛 SandboxGoneError,
        # 中间只差一次必败的往返
        raise
    except Exception as e:
        logger.warning(f"[workspace] find 树快照失败,降级根目录单层列出: {e}")
        return _fallback_single_level()

    # find 正常时至少会输出起点行(d\t);完全没有 tab 分隔行说明 find 不可用/报错
    lines = output.splitlines()
    if not any("\t" in ln for ln in lines):
        logger.warning("[workspace] find 无有效输出,降级根目录单层列出")
        return _fallback_single_level()

    entries = []
    for line in lines:
        if "\t" not in line:
            continue
        t, rel = line.split("\t", 1)
        if not rel:  # 起点行(%P 为空)
            continue
        entries.append({"path": rel, "type": "dir" if t == "d" else "file"})

    truncated = len(entries) > max_entries
    return {
        "entries": entries[:max_entries],
        "truncated": truncated,
        "max_depth": max_depth,
    }


def _browse_repo_root(task_id: str) -> tuple[dict, str]:
    """前端浏览/下载共用的工作区前置检查,返回 (会话上下文, repo_path)

    两类异常文案与处置完全不同,前端据此分流(旧版只到 RuntimeError 一档,
    于是"容器被回收"和"还没克隆"在界面上长得一样,后者还不给恢复入口):
    - RuntimeError:会话不存在 / 尚未 clone —— 工作区本就不可用
    - SandboxGoneError:会话还在但容器已被 Server 回收(本函数已顺手丢弃该会话,
      下一次请求就落到上一档;路由把这条映射成 410 而非 500)
    """
    ctx = _sessions.get(task_id)
    if ctx is None:
        raise RuntimeError("工作区不可用:任务未 clone 仓库或会话已过期清理")

    if not _probe_session(task_id, ctx):
        raise SandboxGoneError(SANDBOX_GONE_MESSAGE)

    repo_path = ctx.get("repo_path", "")
    if not repo_path:
        raise RuntimeError("工作区不可用:尚未 clone 仓库")
    return ctx, repo_path


def browse_read_file(task_id: str, file_path: str, offset: int = 1, max_lines: int = 500) -> dict:
    """面向前端的文件读取(复用 read_file 逻辑,但不带行号)

    默认读 500 行(比 LLM 工具的 200 行多,前端查看用)。
    与 read_file 工具的区别:content 返回原始文本(不带行号前缀),
    因为前端 WorkspaceSidebar 会自己渲染行号列(start_line + i),
    若后端再带行号会造成两列行号重复。

    二进制文件不再回传原始字节:返回 binary=True + 占位正文,前端转下载卡片
    (见 app/file_kinds.py 的判定口径)。
    """
    ctx, repo_path = _browse_repo_root(task_id)
    with _browse_call(task_id):
        if ctx["mode"] == "local":
            return _read_file_local(repo_path, file_path, max_lines, offset, with_line_numbers=False)
        return _read_file_sandbox(ctx, repo_path, file_path, max_lines, offset, with_line_numbers=False)


def _iter_local_file(path: Path, chunk_size: int) -> Iterator[bytes]:
    """本地文件分块产出(句柄在生成器结束时关闭)"""
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def browse_download(task_id: str, file_path: str, chunk_size: int = 65536) -> tuple[int, Iterator[bytes]]:
    """为前端下载准备 (文件字节数, 分块字节流)

    返回字节数是为了在**开始响应之前**做上限检查(流已开始就无法再回 413)。

    两种模式都不借道 shell:execd 的文本通道会改写原始字节(正是乱码的成因)。
    - local:直接 open 宿主文件。不走 SandboxSession._local_resolve_path ——
      它会把绝对宿主路径往临时目录里映射,与 _read_file_local 的路径口径不同
    - sandbox:SDK read_bytes_stream 惰性产出,超大文件不会整份进后端内存

    抛出:RuntimeError(工作区不可用)/ ValueError(路径穿越)/
          FileNotFoundError(不存在或不是文件)
    """
    ctx, repo_path = _browse_repo_root(task_id)

    with _browse_call(task_id):
        if ctx["mode"] == "local":
            root = Path(repo_path).resolve()
            full_path = Path(repo_path) / file_path
            if not full_path.resolve().is_relative_to(root):
                raise ValueError("非法路径:不能超出仓库根目录")
            if not full_path.is_file():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            return full_path.stat().st_size, _iter_local_file(full_path, chunk_size)

        session: SandboxSession = ctx["session"]
        full_path = f"{repo_path.rstrip('/')}/{file_path.lstrip('/')}"
        size = session.stat_size(full_path)
        return size, session.read_bytes_stream(full_path, chunk_size=chunk_size)


# ============================================================
# 工具 1:clone_repo
# ============================================================


class CloneSkippedError(RuntimeError):
    """用户主动跳过预克隆(克隆轮询检查点抛出)

    由 clone_repo_with_fallback 向上传播,orchestrator 单独捕获并降级为
    react_agent 自主克隆;回退链内部不得吞掉(否则跳过后会默默再试
    下一种协议)。
    """


def clone_repo(repo_url: str, branch: str | None = None, task_id: str = "", git_tokens: dict | None = None) -> dict:
    """克隆 Git 仓库(LLM 工具入口)

    内部委托给 clone_repo_with_fallback,复用同一套协议回退逻辑:
    HTTPS+token → SSH → HTTPS 匿名。

    git_tokens 由 execute_tool 从 ContextVar 注入,{provider: token},LLM 不可见。
    clone_repo_with_fallback 按 repo_url 主机匹配 provider 取对应 token。
    """
    return clone_repo_with_fallback(repo_url, branch, task_id, git_tokens or {})


def _copy_local_dir_into_workspace(
    ctx: dict[str, Any], src_local: Path, dest_path: str, clear_dest: bool,
) -> str:
    """把本地目录 src_local 拷进工作区的 dest_path(绝对路径),返回 dest_path

    传输底层原语,transfer_upload_to_workspace 与 add_uploads_to_workspace 共用:
    - local 模式:copytree(clear_dest 时先 rmtree dest)
    - sandbox 模式:内存重打包 zip → base64 分块写入(文本接口)→
      沙箱内解码解压。SandboxSession.write_file 只支持 UTF-8 文本,
      二进制安全传输必须走 base64;重打包条目均为相对 posix 路径,
      extractall 无 zip-slip 风险(上传时的 zip-slip 已在 services/uploads.py 校验过)。

    不碰 _set_repo_path(由调用方决定是否重定向工作根)。
    """
    import base64
    import io
    import zipfile

    mode = ctx["mode"]
    if mode == "local":
        dest = Path(dest_path)
        if clear_dest and dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_local, dest)
        return str(dest)

    session: SandboxSession = ctx["session"]
    # 上传树 → 内存 zip → base64 文本
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_local.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src_local).as_posix())
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    # 单块 3MB b64 文本(ASCII,可作 UTF-8 安全走 write_file 文本接口)
    chunk_size = 3 * 1024 * 1024
    tmp_dir = f"/tmp/upload_{uuid.uuid4().hex[:8]}"
    rm_dest = f"rm -rf {shlex.quote(dest_path)} && " if clear_dest else ""
    session.run_command(
        f"{rm_dest}mkdir -p {shlex.quote(dest_path)} {shlex.quote(tmp_dir)}"
    )
    try:
        for i in range(0, len(b64), chunk_size):
            session.write_file(
                f"{tmp_dir}/{i // chunk_size:05d}.b64", b64[i:i + chunk_size]
            )
        # 拼接解码 + 解压(base64 为 coreutils 常备;python3 沙箱必有)
        session.run_command(
            f"cat {tmp_dir}/*.b64 | base64 -d > {tmp_dir}.zip && "
            f"python3 -c \"import zipfile;"
            f"zipfile.ZipFile('{tmp_dir}.zip').extractall('{dest_path}')\"",
            timeout=300, check=True,
        )
    finally:
        session.run_command(
            f"rm -rf {shlex.quote(tmp_dir)} {shlex.quote(tmp_dir)}.zip"
        )
    return dest_path


# 上传工作根目录名:创建上传铺在这里,无仓库任务(纯对话开场)首次收到追问
# 附件时也按需建同名工作根 —— 两条路径共用一个名字,工作根口径才能与
# upload_layout.compute_upload_layout(回退浏览按它拼树)保持一致
UPLOAD_WORK_ROOT_NAME = "uploaded_files"


def _uploads_work_root(
    ctx: dict[str, Any], dest_name: str = UPLOAD_WORK_ROOT_NAME,
) -> str:
    """上传工作根的绝对路径(按模式:local=临时目录下子目录,sandbox=/home/user/repos/*)"""
    if ctx["mode"] == "local":
        return str(Path(ctx["local_dir"]) / dest_name)
    return f"/home/user/repos/{dest_name}"


def _ensure_upload_work_root(ctx: dict[str, Any], task_id: str) -> str:
    """无工作根的任务按需建一个上传工作根,返回其绝对路径(已 _set_repo_path)

    既没 repo_url 也没创建上传的任务(用户先发了句"hi")从来没有人设过
    repo_path,追问附件因此无处可放:旧实现直接抛"工作区尚未就绪",调用方
    catch+log 后静默丢文件,模型却照常收到"文件已在 followup_uploads/"的提示,
    结果就是用户看着空工作区、模型对着不存在的文件编答案。这里补上缺失的
    一步:建根 + 记路径,附件照常用 followup_uploads/{i}-{name} 布局落位,前端
    工作区也从 available=false 变成可浏览。

    与之后可能发生的真实 clone 不冲突:克隆的幂等复用按 ctx["clone_source"]
    判定(本函数不写该字段),目标目录避让也认这条"只记了 repo_path 的目录
    不能当残留删掉"的口径(见 _resolve_local_repo_dir)。
    """
    root = _uploads_work_root(ctx)
    if ctx["mode"] == "local":
        Path(root).mkdir(parents=True, exist_ok=True)
    else:
        # 只建不删:repo_path 为空不代表容器是新的(可能是降级/未 clone 的任务),
        # 每个上传槽位由传输侧 clear_dest=True 各自清空,不会串内容
        ctx["session"].run_command(f"mkdir -p {shlex.quote(root)}")
    _set_repo_path(task_id, root)
    logger.info(f"[uploads] 无工作根任务按需建根: task={task_id}, repo_path={root}")
    return root


def transfer_upload_to_workspace(
    task_id: str, files_dir: str, dest_name: str = UPLOAD_WORK_ROOT_NAME,
) -> str:
    """把服务端上传目录(files_dir)传输进任务沙箱工作区,返回 repo_path

    orchestrator 上传分支调用(任务 params 含创建上传时替代 clone)。
    上传内容无法像 git 仓库那样被 agent 自主重新获取,本函数是创建上传
    进入沙箱的入口(resume/重试链路经 _prepare_repo_context 复用,
    天然幂等:目标目录已存在时先清空再写入)。

    - local 模式:拷到 {local_dir}/{dest_name}
    - sandbox 模式:拷到 /home/user/repos/{dest_name}

    传输完成即 _set_repo_path(与 clone_repo_with_fallback 一致),
    react_agent / workspace 路由可直接通过 task_id 复用。
    """
    src = Path(files_dir)
    if not src.is_dir():
        raise ValueError(f"上传目录不存在: {files_dir}")

    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    dest_path = _uploads_work_root(ctx, dest_name)

    repo_path = _copy_local_dir_into_workspace(ctx, src, dest_path, clear_dest=True)
    _set_repo_path(task_id, repo_path)
    logger.info(
        f"[uploads] 传输完成: task={task_id}, mode={mode}, repo_path={repo_path}"
    )
    return repo_path


def _exclude_from_git(ctx: dict[str, Any], repo_path: str, dest_subdir: str) -> None:
    """把追问上传目录写入 .git/info/exclude(幂等),避免污染 git diff

    仅当工作区是 git 仓库(存在 .git)时生效;非 git 工作区静默跳过。
    失败不报错(追问文件已落地,排除仅为避免 diff 噪声)。
    """
    line = f"{dest_subdir.rstrip('/')}/"
    try:
        if ctx["mode"] == "local":
            git_dir = Path(repo_path) / ".git"
            if not git_dir.is_dir():
                return
            info = git_dir / "info"
            info.mkdir(parents=True, exist_ok=True)
            excl = info / "exclude"
            existing = excl.read_text(encoding="utf-8") if excl.exists() else ""
            if line not in existing.splitlines():
                with open(excl, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        else:
            session: SandboxSession = ctx["session"]
            rp = shlex.quote(repo_path)
            ln = shlex.quote(line)
            session.run_command(
                f"if [ -d {rp}/.git ]; then mkdir -p {rp}/.git/info; "
                f"grep -qxF {ln} {rp}/.git/info/exclude 2>/dev/null || "
                f"echo {ln} >> {rp}/.git/info/exclude; fi"
            )
    except Exception as e:
        logger.warning(f"[uploads] 写入 .git/info/exclude 失败(忽略): {e}")


def add_uploads_to_workspace(
    task_id: str, upload_ids: list[str], dest_subdir: str = "followup_uploads",
) -> list[str]:
    """把追问上传的多个文件追加进现有工作区,返回相对工作根的 posix 路径列表

    与 transfer_upload_to_workspace 的关键区别:
    - 不重定向 repo_path(不调 _set_repo_path),agent 继续在原工作根作业;
    - 不清空现有内容(每个上传落入 {repo_path}/{dest_subdir}/{i}-{name}/ 独立子目录);
    - git 工作区把 {dest_subdir}/ 写入 .git/info/exclude,不进 diff。

    调用方按 params.followup_upload_ids 全量累积列表传入(全局下标),
    与沙箱回收后的重放 / 工作区回退浏览共用同一布局约定。
    供运行中追问(react_agent drain)与完成后 resume 共用。
    工作区未就绪(repo_path 为空)时**按需建根**而不是报错(见
    _ensure_upload_work_root):无仓库任务的追问附件同样要有落点。

    单个上传传输失败(如已被 GC 清理)仅跳过该上传并记 warning,不阻断
    其余上传 —— 全量重放语义下,一个过期旧附件不应拖垮本轮新文件。
    """
    from app.services.uploads import load_upload_meta, materialize_upload_files

    if not upload_ids:
        return []
    ctx = _get_or_create_session(task_id)
    repo_path = ctx.get("repo_path", "")
    if not repo_path:
        repo_path = _ensure_upload_work_root(ctx, task_id)

    added: list[str] = []
    for i, uid in enumerate(upload_ids):
        try:
            meta = load_upload_meta(uid)
        except Exception:
            meta = {}
        name = _safe_dirname(meta.get("filename") or "", fallback=uid[:12] or "upload")
        dest_rel = f"{dest_subdir.strip('/')}/{i}-{name}"
        if ctx["mode"] == "local":
            dest_abs = str(Path(repo_path) / dest_rel)
        else:
            dest_abs = f"{repo_path.rstrip('/')}/{dest_rel}"
        try:
            with materialize_upload_files(uid) as files_dir:
                _copy_local_dir_into_workspace(ctx, files_dir, dest_abs, clear_dest=True)
            added.append(dest_rel)
            logger.info(f"[uploads] 追问文件已追加: task={task_id}, upload_id={uid}, dest={dest_abs}")
        except Exception as e:
            logger.warning(
                f"[uploads] 追问文件传输失败(跳过该上传): task={task_id}, upload_id={uid}, err={e}"
            )

    _exclude_from_git(ctx, repo_path, dest_subdir)
    return added


def transfer_uploads_to_workspace_root(
    task_id: str, upload_ids: list[str], dest_name: str = UPLOAD_WORK_ROOT_NAME,
) -> str:
    """创建时的多上传:建空工作根 {dest_name}/ 并把每个上传拷进子目录,返回 repo_path

    与单个上传的 transfer_upload_to_workspace(文件直接铺在根)不同:
    多上传时工作根 = {dest_name}/,各上传各占 {i}-{清洗文件名}/ 子目录防碰撞。
    会 _set_repo_path 为工作根(与单个上传一致,agent 以根为工作区)。
    """
    from app.services.uploads import load_upload_meta, materialize_upload_files

    if not upload_ids:
        raise ValueError("upload_ids 为空")
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    repo_path = _uploads_work_root(ctx, dest_name)
    if mode == "local":
        if Path(repo_path).exists():
            shutil.rmtree(repo_path, ignore_errors=True)
        Path(repo_path).mkdir(parents=True, exist_ok=True)
    else:
        ctx["session"].run_command(
            f"rm -rf {shlex.quote(repo_path)} && mkdir -p {shlex.quote(repo_path)}"
        )
    _set_repo_path(task_id, repo_path)

    for i, uid in enumerate(upload_ids):
        try:
            meta = load_upload_meta(uid)
        except Exception:
            meta = {}
        name = _safe_dirname(meta.get("filename") or "", fallback=uid[:12] or "upload")
        dest_rel = f"{i}-{name}"
        dest_abs = (
            str(Path(repo_path) / dest_rel) if mode == "local"
            else f"{repo_path}/{dest_rel}"
        )
        with materialize_upload_files(uid) as files_dir:
            _copy_local_dir_into_workspace(ctx, files_dir, dest_abs, clear_dest=True)
    logger.info(
        f"[uploads] 多上传传输完成: task={task_id}, mode={mode}, "
        f"count={len(upload_ids)}, repo_path={repo_path}"
    )
    return repo_path


def _clone_depth_args() -> list[str]:
    """克隆深度参数(据 settings.REPO_CLONE_DEPTH:0=不限制完整克隆,>0=--depth N)

    供 _clone_repo_local / _clone_repo_sandbox 共用,集中管理避免硬编码分歧。
    """
    depth = settings.REPO_CLONE_DEPTH
    return ["--depth", str(depth)] if depth > 0 else []


def _pause_checkpoint(task_id: str, deadline: float) -> float:
    """clone 轮询循环的暂停检查点:已暂停则阻塞到恢复,返回顺延后的 deadline

    暂停期间不计入克隆超时(否则卡住的 clone 会在暂停中吃满 timeout,
    恢复即报超时)。未暂停时立即返回原 deadline,几乎零开销。
    """
    if not task_id:
        return deadline
    t0 = time.monotonic()
    wait_if_paused(task_id)
    paused_for = time.monotonic() - t0
    if paused_for > 0.1:
        logger.info(f"[clone] task={task_id} 暂停 {paused_for:.0f}s 后恢复克隆轮询")
    return deadline + paused_for


def _count_repo_files(repo_dir: Path) -> int:
    """仓库内文件数(不计 .git 下的对象文件)"""
    return sum(
        1
        for _ in repo_dir.rglob("*")
        if _.is_file() and ".git" not in _.parts
    )


def _dir_has_entries(path: Path) -> bool:
    """目录存在且非空(git 正是按这个判定拒绝克隆的)

    不存在或空目录返回 False —— 空目录 git 能直接写入,无需处置。
    """
    if not path.exists():
        return False
    if not path.is_dir():
        return True  # 同名文件占用(极罕见),按非空处理
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    except OSError:
        return True
    return True


def _remove_local_tree(path: Path, retries: int = 3) -> bool:
    """删除本地文件/目录树并确认删干净;失败返回 False(不抛)

    委托 repo_cache.force_rmtree:Windows 上 git 把 .git/objects/pack/*.pack|*.idx
    标为只读,普通 shutil.rmtree 碰只读文件直接 WinError 5 拒访且**重试无效**
    (是文件属性问题不是瞬时锁),必须先 chmod 再删 —— force_rmtree 的 onexc 就
    是干这个的,同时保留对瞬时句柄占用(AV/索引器/刚被 kill 的 git 子进程)的退避重试。
    之前这里自己写 rmtree:第一版用 ignore_errors=True("删一半失败"也静默返回),
    第二版只重试不 chmod,含 pack 的残留永远删不掉。force_rmtree 只认目录,
    同名文件占用单独 unlink。
    """
    if path.exists() and not path.is_dir():
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning(f"[local] 删除残留文件失败: {path} -> {e}")
            return False
    if not force_rmtree(path, retries=retries):
        logger.warning(f"[local] 删除残留目录失败(重试 {retries} 次): {path}")
        return False
    return True


def _unique_repo_dir(local_dir: Path, repo_name: str) -> Path:
    """{repo_name} 被占用且不便删除时取 {repo_name}-2、-3…(首个空闲候选)"""
    for i in range(2, 21):
        cand = local_dir / f"{repo_name}-{i}"
        if not _dir_has_entries(cand):
            return cand
    # 20 个候选都被占(理论上不会):随机后缀保证不撞
    return local_dir / f"{repo_name}-{uuid.uuid4().hex[:6]}"


def _resolve_local_repo_dir(ctx: dict, repo_name: str) -> Path:
    """确定 local clone 的目标目录并按归属处置残留(幂等复用不在此判)

    local 模式的会话临时目录在整个任务生命周期内固定,而 clone 会被多次触发
    (协议回退的每个候选、分支回退、跳过预克隆后的自主 clone、工作区恢复入口),
    上一次留下的目录会让 git 直接
    `fatal: destination path '...' already exists and is not an empty directory`。
    按归属处置(sandbox 模式由 _clone_repo_sandbox 的 rm -rf 兜底):
    - 不存在/为空 → 原样可用(git 能往空目录里写)
    - 本会话 clone 出来且完好(clone_source + repo_path 同路径 + 含 .git)→ 避让换名:
      能走到真克隆说明调用方要另一份检出(显式换分支/换源),但旧检出里可能有
      未提交的改动,不能由克隆动作抹掉(会话结束随临时目录一起回收)
    - 本会话 clone 出来但已残缺(同路径无 .git)→ 删除重建:残缺检出无保留价值
    - 已记为 repo_path 但不是 clone 出来的(上传工作区)、含 .git 的外来目录
      (执行 agent 自行 clone 并编辑的成果)→ 避让换名,内容一律不删
    - 其余残留(kill 半途的半成品/空壳)→ 删除重建;仍删不掉则避让换名
    """
    local_dir: Path = ctx["local_dir"]
    repo_dir = local_dir / repo_name
    if not _dir_has_entries(repo_dir):
        return repo_dir

    recorded = str(ctx.get("repo_path") or "")
    is_recorded = bool(recorded) and os.path.normcase(recorded) == os.path.normcase(str(repo_dir))
    if is_recorded and ctx.get("clone_source") is not None:
        if (repo_dir / ".git").is_dir():
            # 自己的完好检出:保留原目录,换个名字克隆
            alt = _unique_repo_dir(local_dir, repo_name)
            logger.warning(
                f"[local] 保留 {repo_dir.name} 现有检出(可能有未提交改动),"
                f"改为克隆到 {alt.name}"
            )
            return alt
        if _remove_local_tree(repo_dir):
            logger.info(f"[local] 清理本会话残缺工作区(无 .git),重新克隆: {repo_dir}")
            return repo_dir
        # 删不掉(句柄长期占用):只能避让,不能带着残留让 git 报错
        alt = _unique_repo_dir(local_dir, repo_name)
        logger.warning(f"[local] 旧工作区删不掉,改为克隆到 {alt.name}")
        return alt
    if is_recorded or (repo_dir / ".git").is_dir():
        # 上传工作区 / 外来仓库:内容不是我们能扔的(仓库名与上传目录撞名也不能删)
        alt = _unique_repo_dir(local_dir, repo_name)
        logger.warning(
            f"[local] {repo_dir.name} 已存在非本会话 clone 的目录,"
            f"避免覆盖,改为克隆到 {alt.name}"
        )
        return alt
    if _remove_local_tree(repo_dir):
        logger.info(f"[local] 已清理上次克隆残留目录: {repo_dir}")
        return repo_dir
    return _unique_repo_dir(local_dir, repo_name)


def _reuse_existing_clone(ctx: dict, repo_url: str, want_branch: str | None) -> dict | None:
    """本会话已成功 clone 过同一来源 → 返回既有工作区(不再动磁盘),否则 None

    重复 clone 请求的来源:空仓库/list_files 降级后 LLM 再调一次 clone_repo、工作区
    恢复入口按 repo_path 判空、失败后的重试、并发调用(排队那个)。重新 clone 不仅
    白花几分钟下载,local 模式还会先撞 git 的 "already exists"。

    判断按"归一化 URL + 分支 + 落盘路径"而不是目录名:避让换名后工作区可能落在
    overleaf-2,按目录名找不回来就会再克隆一份(而且旧目录还在 → 一路 bar-3、bar-4
    累加);把路径一并编进来源记录,则 repo_path 被别的流程改写(如上传工作区传输)
    时不会把上传目录当成仓库返回。
    分支取宽松口径 —— 任一侧没写分支都算不冲突(远端默认分支与任务参数常对不上,
    严格判等会退化成重新克隆)。

    完好性探测的口径按模式分岔:
    - local:直接探盘(目录 + .git 都在才算完好)
    - sandbox:记的是容器内路径(/home/user/repos/xxx),宿主机上必然不存在 —— 以前
      因此直接"只信记录",结果恢复流程会命中一条"容器里根本没这个目录"的记录而
      秒回 done、根本不克隆(前端就是一个空目录)。现在进容器验一把:一次轻量
      `test -d` 往返相比重下整个仓库可以忽略。
      只有真实 SandboxSession 才能验(测替身等拿不到可靠回答时宁可信记录,也
      不能让探测把复用变成死代码 —— 那是当年踩过的坑)。
    """
    recorded = str(ctx.get("repo_path") or "")
    source = ctx.get("clone_source")
    if not recorded or source is None:
        return None
    recorded_url, recorded_branch, source_path = source
    if os.path.normcase(source_path) != os.path.normcase(recorded):
        return None
    if recorded_url != normalize_repo_url(repo_url):
        return None
    want, have = (want_branch or "").strip(), (recorded_branch or "").strip()
    if want and have and want != have:
        return None

    result: dict[str, Any] = {"path": recorded, "reused": True}
    if ctx.get("mode") == "local":
        # local 能探盘:目录与 .git 都在才算完好(残缺返 None 走真克隆)
        try:
            recorded_path = Path(recorded)
            if not recorded_path.is_dir() or not (recorded_path / ".git").is_dir():
                return None
        except (OSError, ValueError):
            return None
        result["files_count"] = _count_repo_files(recorded_path)
    else:
        session = ctx.get("session")
        if isinstance(session, SandboxSession):
            # .git 在才算完好:半途被打断的 rm -rf + mkdir -p 会留下无 .git 的空壳,
            # 复用这种目录等于把"恢复成功"报给用户而工作区是空的
            probe_cmd = (
                f"if test -d {shlex.quote(recorded.rstrip('/') + '/.git')}; "
                f"then echo PRESENT; else echo ABSENT; fi"
            )
            try:
                probe = session.run_command(probe_cmd, timeout=30)
            except SandboxGoneError:
                # 容器已回收是"不可复用",不是探测失败:让上层去重建会话
                raise
            except Exception as e:
                logger.warning(
                    f"[clone_fallback] 容器内完好性探测失败(仍复用记录): {e}"
                )
            else:
                if "ABSENT" in probe:
                    logger.info(
                        f"[clone_fallback] 记录的容器内工作区已不在,重新克隆: {recorded}"
                    )
                    return None
    logger.info(f"[clone_fallback] 复用会话已 clone 的工作区: {recorded}")
    return result


def _record_clone_source(
    ctx: dict, repo_url: str, branch: str | None, repo_path: str,
) -> None:
    """记下本次 clone 的来源(归一化 URL + 实际检出分支 + 落盘路径)

    供 _reuse_existing_clone 判断。路径一起记:避让换名后工作区不在目录名上,
    而 repo_path 也可能被上传传输等流程改写,光比 URL 会认错工作区。
    写在 ctx 上随 session 一起销毁(_get_or_create_session 返回的就是 _sessions 里
    那个 dict,原地改即生效)。
    """
    ctx["clone_source"] = (normalize_repo_url(repo_url), branch or "", str(repo_path))


def _clone_repo_local(
    ctx: dict, clone_url: str, repo_name: str, branch: str | None,
    task_id: str = "", cancellable: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
    use_depth: bool = True,
) -> dict:
    """local 模式:本地 git clone(Popen 流式读进度 + 推 SSE)

    用 subprocess.Popen 逐行读 git 的 stderr 进度输出(需 --progress 强制非 tty
    也输出),解析 "Receiving objects: X%" 等行后通过 event_bus 推 clone_progress
    事件给前端。节流:百分比变化 >=5 或距上次推送 >=2s 才推一次。

    progress_callback(percent, message):可选的直连进度回调(与 event_bus 推送
    同点位同节流)。任务结束后的调用方(如出题工作区恢复)总线已 finish,
    clone_progress 会被丢弃,只能走这个回调拿进度;回调异常不影响克隆。

    超时用 deadline + poll 机制(而非 subprocess.run 的 timeout),超时主动 kill
    进程并 join 读线程,避免大仓库卡死时无反馈。

    cancellable=True 时(仅 orchestrator 预克隆路径),轮询中检查跳过标志,
    用户请求跳过预克隆时 kill 进程并抛 CloneSkippedError。

    目标目录经 _resolve_local_repo_dir 按归属处置(残留清理 / 避让换名);失败与被跳过
    时在本函数内清掉本次克隆的目录 —— 只有克隆者自己知道落到了哪个目录,幂等复用
    判定已在 _clone_repo_fallback 入口完成,走到这里就意味着真要动手。

    use_depth=False 时不拼 --depth(本地路径克隆对 depth 仅告警且无意义,
    bare 缓存路径恒全量)。
    """
    repo_dir = _resolve_local_repo_dir(ctx, repo_name)

    # Windows 默认 260 字符路径上限会让大仓库检出失败(error: unable to create
    # file ...: Filename too long),而 local 模式的克隆根是
    # %LOCALAPPDATA%\Temp\sandbox_local_xxxxxxxx\<repo>\... 这种长前缀目录。
    # core.longpaths 让 git 走长路径 API(需系统开启 LongPathsEnabled,git-for-windows
    # 自带 manifest 支持);非 Windows 不传,避免无谓的配置覆盖
    longpaths = ["-c", "core.longpaths=true"] if os.name == "nt" else []
    cmd = ["git", *longpaths, "clone", "--progress"] + (_clone_depth_args() if use_depth else [])
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([clone_url, str(repo_dir)])

    logger.info(f"[local] git clone: {clone_url}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stderr_lines: list[str] = []
    last_percent = -1
    last_push_ts = time.monotonic()
    timeout = settings.REPO_CLONE_TIMEOUT
    deadline = time.monotonic() + timeout

    def _read_stderr() -> None:
        nonlocal last_percent, last_push_ts
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            percent = _parse_git_progress(line)
            if percent is None or not task_id:
                continue
            now = time.monotonic()
            # 节流:百分比增加 >=5 或距上次推送 >=2s
            if percent > last_percent and (
                percent - last_percent >= 5 or now - last_push_ts >= 2.0
            ):
                publish_event(task_id, "clone_progress", {
                    "percent": percent,
                    "message": line.strip()[:200],
                })
                if progress_callback:
                    try:
                        progress_callback(percent, line.strip()[:200])
                    except Exception:
                        logger.warning("[local] clone 进度回调异常(忽略)", exc_info=True)
                last_percent = percent
                last_push_ts = now

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()

    try:
        while True:
            ret = proc.poll()
            if ret is not None:
                break
            # 暂停检查点:已暂停则阻塞到恢复,暂停时长不计入超时
            deadline = _pause_checkpoint(task_id, deadline)
            # 跳过检查点:用户请求跳过预克隆 → kill 进程并抛(向上传播降级)
            if cancellable and consume_skip_clone(task_id):
                proc.kill()
                reader.join(timeout=2)
                raise CloneSkippedError(f"用户已跳过预克隆: {repo_name}")
            if time.monotonic() > deadline:
                proc.kill()
                reader.join(timeout=2)
                raise RuntimeError(f"git clone 超时({timeout}s)")
            time.sleep(0.5)
    except BaseException:
        # 超时 kill / 用户跳过 / 意外异常:本次克隆的目录是半成品,清掉它,下一次
        # 尝试(换协议 / 换分支 / 降级为自主 clone)才不会撞 git 的 "already exists"
        _remove_local_tree(repo_dir)
        raise
    finally:
        reader.join(timeout=5)

    if proc.returncode != 0:
        err_tail = _git_error_tail(stderr_lines)
        # 同上:非零退出的目录也是半成品,先取走错误信息再清(清理不能盖掉原因)
        _remove_local_tree(repo_dir)
        raise RuntimeError(
            f"git clone 失败(退出码 {proc.returncode}): {err_tail}"
        )

    files_count = _count_repo_files(repo_dir)
    if files_count == 0:
        # 空壳也得清:目录解析逻辑会把"带 .git 的目录"当已有工作区避让换名,
        # 留着它下一次尝试会克隆到 bar-2 而不是原地重下
        _remove_local_tree(repo_dir)
        _reject_empty_clone(str(repo_dir), clone_url)
    # local 模式下,path 返回本地路径(后续 read/search 工具会用 Python 直接读)
    return {"path": str(repo_dir), "files_count": files_count}


# git clone 进度行正则:匹配各阶段的 "X%"
#   Receiving objects: 45% (1234/5678), 1.23 MiB | 2.34 MiB/s
#   Resolving deltas: 30% (123/456)
#   Counting objects: 100% (1234/1234), done.
#   Compressing objects: 45% (12/27)
_GIT_PROGRESS_RE = re.compile(
    r"(?:Receiving objects|Resolving deltas|Counting objects|Compressing objects):\s+(\d+)%"
)


def _parse_git_progress(line: str) -> int | None:
    """从 git clone 的 stderr 行解析进度百分比,非进度行返回 None"""
    m = _GIT_PROGRESS_RE.search(line)
    return int(m.group(1)) if m else None


# checkout 阶段进度行("Updating files: 94% (6185/6579)"):大仓库能刷出上百行,
# 不滤掉就会把真正的 fatal/error 挤出错误信息窗口(Windows 长路径错误就是这么丢的)
_GIT_CHECKOUT_PROGRESS_RE = re.compile(r"^Updating files:\s+\d+%")


def _git_error_tail(stderr_lines: list[str], limit: int = 1200) -> str:
    """从 git stderr 里取错误部分(剔除进度刷新行,保留尾部)

    直接取 [-500:] 会被 "Receiving objects: xx%" / "Updating files: xx%" 这类进度行
    占满,把 fatal 行整段挤掉 —— 报错只看到一屏进度、看不出真实原因的现场就是这样。
    """
    kept = [
        ln for ln in stderr_lines
        if ln.strip()
        and not _GIT_PROGRESS_RE.search(ln)
        and not _GIT_CHECKOUT_PROGRESS_RE.search(ln.strip())
    ]
    text = "".join(kept)
    # 全是进度行(被 kill 在检出中途等):明确说出来,不让上层拿到空白错误
    return text[-limit:] if text else "(仅剩进度输出,疑似中途被打断)"


def _reject_empty_clone(repo_dir: str, clone_url: str) -> None:
    """克隆回来 0 文件 = 空工作区,不能当成功返回

    `git clone` 对"没有任何 ref 的 bare 缓存"和真空仓库都是 **exit 0 + 一句
    warning: You appear to have cloned an empty repository**。只看退出码就会把
    "恢复完成"报给用户,而文件树就是一片空白(日志里却写着成功,根本无从查起)。

    报错而不是警告:缓存快路径抛异常会自动降级远程克隆(缓存无 ref 正是这种
    情形),远程也为空则带着真实原因进入回退链聚合错误,前端看得到。
    """
    raise RuntimeError(
        f"克隆结果为空({repo_dir} 下无任何文件,已排除 .git):"
        f"空仓库、该分支无提交,或克隆源({clone_url})不含可用 ref"
    )


def _clone_repo_sandbox(
    ctx: dict, clone_url: str, repo_name: str, branch: str | None,
    task_id: str = "", cancellable: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
    use_depth: bool = True,
) -> dict:
    """sandbox 模式:在沙箱里 git clone(后台命令 + 进度文件轮询流式推进度)

    进度采集为何不用 execd 日志(get_background_logs):
    git 进度输出用 \r 刷新同一行,只在阶段 done. 时才打 \n,而 Server 端
    execd 日志采集按 \n 分行缓存,\r 进度块拿不到(实测 cursor 长期不动)。
    改为把 stderr 重定向到沙箱内进度文件(文件写入无行缓冲,\r 实时落盘),
    轮询 read_file 解析最新进度行推 clone_progress 事件
    (节流:百分比变化 >=5 或距上次推送 >=2s)。progress_callback 与
    event_bus 推送同点位同节流(任务结束后总线已 finish 的调用方走它拿进度)。

    完成判定:轮询 get_command_status(命令未退出时恒为 200,避免每轮
    read_file 退出码标记文件触发 404 + SDK ERROR traceback 污染日志);
    退出后若 status 拿不到 exit_code,再读一次退出码标记文件兜底。
    超时 interrupt + 抛异常。
    """
    session: SandboxSession = ctx["session"]
    repo_dir = f"/home/user/repos/{repo_name}"
    # 清理可能残留的半成品目录(上次失败/中断残留),
    # 避免 git clone 报 "destination path already exists" 直接失败
    session.run_command(f"rm -rf {shlex.quote(repo_dir)} && mkdir -p {shlex.quote(repo_dir)}")

    # 进度文件 + 退出码标记文件(沙箱内 /tmp,仅本次 clone 使用)
    run_tag = uuid.uuid4().hex[:8]
    progress_file = f"/tmp/clone_progress_{run_tag}.log"
    exit_file = f"/tmp/clone_exit_{run_tag}.code"

    # --progress 强制非 tty(后台命令无 tty)也输出进度到 stderr;
    # stderr 重定向到进度文件(\r 实时落盘),退出码写标记文件供轮询判完成
    # use_depth=False 时不拼 --depth(从容器内挂载的 bare 缓存克隆,本地路径 depth 无意义)
    git_cmd = "git clone --progress " + " ".join(_clone_depth_args() if use_depth else [])
    if branch:
        git_cmd += f" --branch {shlex.quote(branch)}"
    git_cmd += f" {shlex.quote(clone_url)} {shlex.quote(repo_dir)}"
    cmd = (
        f"{git_cmd} 2> {shlex.quote(progress_file)}; "
        f"echo $? > {shlex.quote(exit_file)}"
    )

    logger.info(f"[sandbox] git clone: {clone_url}")
    exec_id = session.run_command_background(cmd)

    timeout = settings.REPO_CLONE_TIMEOUT
    deadline = time.monotonic() + timeout
    last_percent = -1
    last_push_ts = time.monotonic()
    last_content = ""

    try:
        while True:
            # 暂停检查点:已暂停则阻塞到恢复(放在轮询顶部,暂停期间
            # 不发 HTTP 请求),暂停时长不计入超时
            deadline = _pause_checkpoint(task_id, deadline)

            # 跳过检查点:用户请求跳过预克隆 → 中断沙箱内命令并抛
            # (向上传播降级;finally 会清理进度/退出码临时文件)
            if cancellable and consume_skip_clone(task_id):
                try:
                    session.interrupt_command(exec_id)
                except Exception:
                    pass
                raise CloneSkippedError(f"用户已跳过预克隆: {repo_name}")

            # 1) 进度:读进度文件,按 \r/\n 拆行取最新进度行推前端
            try:
                last_content = session.read_file(progress_file)
            except Exception:
                last_content = ""  # 文件尚未创建(命令刚启动)
            if last_content and task_id:
                for line in reversed(last_content.replace("\r", "\n").splitlines()):
                    percent = _parse_git_progress(line)
                    if percent is None:
                        continue
                    now = time.monotonic()
                    if percent > last_percent and (
                        percent - last_percent >= 5 or now - last_push_ts >= 2.0
                    ):
                        publish_event(task_id, "clone_progress", {
                            "percent": percent,
                            "message": line.strip()[:200],
                        })
                        if progress_callback:
                            try:
                                progress_callback(percent, line.strip()[:200])
                            except Exception:
                                logger.warning("[sandbox] clone 进度回调异常(忽略)", exc_info=True)
                        last_percent = percent
                        last_push_ts = now
                    break

            # 2) 完成判定:查命令状态(未退出时恒 200,不会像 read_file
            #    未创建的标记文件那样每轮 404 + SDK ERROR traceback)
            running, exit_code = session.get_command_status(exec_id)
            if not running:
                if exit_code is None:
                    # status 没给退出码,兜底读标记文件;文件还没写出说明
                    # 状态滞后(命令刚退出 shell 尾部还没执行完),再等一轮
                    try:
                        exit_text = session.read_file(exit_file).strip()
                    except Exception:
                        exit_text = ""
                    if not exit_text:
                        time.sleep(1.0)
                        if time.monotonic() > deadline:
                            raise RuntimeError(f"git clone 超时({timeout}s)")
                        continue
                    try:
                        exit_code = int(exit_text)
                    except ValueError:
                        exit_code = 1
                if exit_code != 0:
                    # 报错信息在进度文件尾部(如 fatal: Remote branch xxx not found)
                    err_tail = last_content[-500:].replace("\r", "\n")
                    raise RuntimeError(
                        f"git clone 失败(退出码 {exit_code}): {err_tail}"
                    )
                break

            if time.monotonic() > deadline:
                try:
                    session.interrupt_command(exec_id)
                except Exception:
                    pass
                raise RuntimeError(f"git clone 超时({timeout}s)")
            # 两次跨公网 read_file 有延迟,轮询间隔给 1s
            time.sleep(1.0)
    finally:
        # 清理临时文件(尽力而为,失败不阻断)
        try:
            session.run_command(
                f"rm -f {shlex.quote(progress_file)} {shlex.quote(exit_file)}"
            )
        except Exception:
            pass

    count_cmd = f"find {shlex.quote(repo_dir)} -type f -not -path '*/.git/*' | wc -l"
    files_count = int(session.run_command(count_cmd).strip() or "0")
    if files_count == 0:
        # 不额外发一次 rm:下一次尝试开头就是 rm -rf + mkdir -p,容器里不会残留
        _reject_empty_clone(repo_dir, clone_url)

    return {"path": repo_dir, "files_count": files_count}


# ============================================================
# 工具 2:list_files(参考 Claude Code LS:单层列出,不递归)
# ============================================================


def list_files(
    repo_path: str,
    subdir: str = "",
    max_entries: int = 200,
    task_id: str = "",
) -> dict:
    """列出仓库内某目录下的文件和子目录(单层,不递归)

    参考 Claude Code 的 LS 工具设计:
    - 单层列出指定目录的内容,不递归整树(避免大仓库撑爆上下文)
    - 跳过噪声目录(.git / node_modules / __pycache__ / venv 等)
    - 区分 file / dir,便于 LLM 决定下一步进哪个子目录或读哪个文件
    - 目录排前、文件排后,各自按名字排序
    - 限制返回条数(max_entries),超出则 truncated=true

    参数:
        repo_path: clone_repo 返回的 path
        subdir: 仓库内相对路径,默认根目录。如 "src"、"tests/unit"
        max_entries: 最多返回条目数,默认 200

    返回:{
        "path": "src/",          # 本次列出的目录(相对仓库)
        "entries": [
            {"name": "main.py", "type": "file", "size": 1024},
            {"name": "utils", "type": "dir", "size": 0},
            ...
        ],
        "total": int,
        "truncated": bool,
    }
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "local":
        return _list_files_local(repo_path, subdir, max_entries)
    else:
        return _list_files_sandbox(ctx, repo_path, subdir, max_entries)


def _list_files_local(repo_path: str, subdir: str, max_entries: int) -> dict:
    """local 模式:用 Path.iterdir 直接列"""
    root = Path(repo_path).resolve()
    target = (root / subdir).resolve() if subdir else root

    # 防路径穿越
    if not target.is_relative_to(root):
        raise ValueError("非法路径:不能超出仓库根目录")
    if not target.is_dir():
        raise FileNotFoundError(f"目录不存在: {subdir or '(根)'}")

    entries = []
    for entry in target.iterdir():
        # 跳过噪声目录(只跳目录,不跳同名文件)
        if entry.is_dir() and entry.name in _SKIP_DIRS_LIST:
            continue
        if entry.is_dir():
            entries.append({"name": entry.name, "type": "dir", "size": 0})
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            entries.append({"name": entry.name, "type": "file", "size": size})

    # 排序:目录在前、文件在后;各自按名字大小写不敏感排序
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    truncated = len(entries) > max_entries
    entries = entries[:max_entries]

    return {
        "path": (subdir.rstrip("/") + "/") if subdir else ".",
        "entries": entries,
        "total": len(entries),
        "truncated": truncated,
    }


def _list_files_sandbox(
    ctx: dict, repo_path: str, subdir: str, max_entries: int
) -> dict:
    """sandbox 模式:用 SDK 原生文件系统 API 单层列出(单次 HTTP 往返)

    比旧方案(test -d + ls 两次远程 shell)快得多。
    SDK 调用异常(非目录不存在)时自动回退 shell 实现,兼容旧 Server。
    实例已被回收则不回退:那条路只会再撞一次同样的 404,并把成因盖成
    "Failed to run command"(历史上的 500 + Docker 错误码进界面就是这么来的)。
    """
    session: SandboxSession = ctx["session"]
    full_path = (
        f"{repo_path.rstrip('/')}/{subdir.lstrip('/')}"
        if subdir else repo_path
    )

    try:
        raw = session.list_directory(full_path)
    except SandboxGoneError:
        raise
    except FileNotFoundError:
        raise FileNotFoundError(f"目录不存在: {subdir or '(根)'}")
    except Exception as e:
        logger.warning(
            f"SDK list_directory 失败,回退 shell 列出: subdir={subdir or '(根)'} err={e}"
        )
        return _list_files_sandbox_shell(ctx, repo_path, subdir, max_entries)

    entries = []
    for item in raw:
        if item["is_dir"] and item["name"] in _SKIP_DIRS_LIST:
            continue
        entries.append({
            "name": item["name"],
            "type": "dir" if item["is_dir"] else "file",
            # SDK 直接给出真实大小(旧 shell 版为省 N 次 stat 固定返 0)
            "size": 0 if item["is_dir"] else item["size"],
        })

    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    truncated = len(entries) > max_entries
    entries = entries[:max_entries]

    return {
        "path": (subdir.rstrip("/") + "/") if subdir else ".",
        "entries": entries,
        "total": len(entries),
        "truncated": truncated,
    }


def _list_files_sandbox_shell(
    ctx: dict, repo_path: str, subdir: str, max_entries: int
) -> dict:
    """sandbox 模式 shell 回退:用 ls -Ap1 单层列出(SDK API 不可用时)

    -A:列出除 . 和 .. 外的所有条目(含隐藏文件)
    -p:目录名末尾加 /(便于解析)
    -1:每行一个
    """
    session: SandboxSession = ctx["session"]
    full_path = (
        f"{repo_path.rstrip('/')}/{subdir.lstrip('/')}"
        if subdir else repo_path
    )

    # 检查目录是否存在
    check = session.run_command(
        f"test -d {shlex.quote(full_path)} && echo OK || echo MISSING"
    )
    if "MISSING" in check:
        raise FileNotFoundError(f"目录不存在: {subdir or '(根)'}")

    # 单层列出
    output = session.run_command(f"ls -Ap1 {shlex.quote(full_path)}")

    entries = []
    for line in output.splitlines():
        name = line.strip()
        if not name:
            continue
        is_dir = name.endswith("/")
        name = name.rstrip("/")
        if is_dir and name in _SKIP_DIRS_LIST:
            continue
        if is_dir:
            entries.append({"name": name, "type": "dir", "size": 0})
        else:
            # 不查文件大小(避免 N 次 stat,LLM 不需要精确大小)
            entries.append({"name": name, "type": "file", "size": 0})

    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    truncated = len(entries) > max_entries
    entries = entries[:max_entries]

    return {
        "path": (subdir.rstrip("/") + "/") if subdir else ".",
        "entries": entries,
        "total": len(entries),
        "truncated": truncated,
    }


# ============================================================
# 项目记忆文件写入(orchestrator 在 clone 后调用,供 react_agent / CLI 随时 read_file 查阅)
# ============================================================


def write_project_memory_file(task_id: str, content: str) -> None:
    """把完整项目记忆写入沙箱固定路径,供 react_agent / CLI 智能体随时 read_file 查阅。

    固定路径 /home/user/.agent_memory/project_memory.md(不分 project_id,每任务启动时
    覆盖为当前项目记忆)。content 为空也写(清空旧文件,避免看到上一个项目的记忆)。

    local 模式:写 ctx["local_dir"]/.agent_memory/project_memory.md(Python 直接写)。
    sandbox 模式:mkdir -p 记忆目录 + session.write_file 写绝对路径。
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    if mode == "local":
        mem_dir = Path(ctx["local_dir"]) / ".agent_memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / _MEMORY_FILE).write_text(content, encoding="utf-8")
    else:
        session: SandboxSession = ctx["session"]
        session.run_command(f"mkdir -p {shlex.quote(_MEMORY_DIR_SANDBOX)}")
        session.write_file(f"{_MEMORY_DIR_SANDBOX}/{_MEMORY_FILE}", content)


def write_global_memory_file(task_id: str, content: str) -> None:
    """把全局长期记忆写入沙箱固定路径,供 react_agent / CLI 智能体随时 read_file 查阅。

    固定路径 /home/user/.agent_memory/global_memory.md(每任务启动时覆盖为当前
    用户的全局记忆)。content 为空也写(清空旧文件,避免看到上一个用户的记忆)。

    与 write_project_memory_file 同构:local 模式写本地目录,sandbox 模式写沙箱绝对路径。
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    if mode == "local":
        mem_dir = Path(ctx["local_dir"]) / ".agent_memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / _GLOBAL_MEMORY_FILE).write_text(content, encoding="utf-8")
    else:
        session: SandboxSession = ctx["session"]
        session.run_command(f"mkdir -p {shlex.quote(_MEMORY_DIR_SANDBOX)}")
        session.write_file(f"{_MEMORY_DIR_SANDBOX}/{_GLOBAL_MEMORY_FILE}", content)


def _is_memory_file_path(file_path: str) -> bool:
    """file_path 是否指向记忆目录(白名单绝对路径,不受 repo_path 限制)

    仅放行 /home/user/.agent_memory/ 开头的绝对路径,其余路径维持原仓库内校验。
    """
    return file_path.startswith(_MEMORY_DIR_SANDBOX + "/")


def _read_memory_file(
    ctx: dict, file_path: str, max_lines: int, offset: int,
) -> dict:
    """读取记忆目录文件(白名单绝对路径,不受 repo_path 限制)

    复用 _read_file_local / _read_file_sandbox:把"记忆目录"当作虚拟 repo_path,
    file_path 取记忆目录下的相对 basename。仍带行号 + 分页,与仓库 read_file 一致体验。

    local 模式:映射到 local_dir/.agent_memory/<basename>(write_project_memory_file 写入处)。
    sandbox 模式:直接读沙箱内绝对路径 /home/user/.agent_memory/<basename>。
    """
    # 去掉目录前缀得到 basename,并防穿越(basename 不应含 .. 或绝对路径成分)
    basename = file_path[len(_MEMORY_DIR_SANDBOX) + 1:].lstrip("/")
    if not basename or ".." in Path(basename).parts or Path(basename).is_absolute():
        raise ValueError(f"非法记忆文件路径: {file_path}")

    mode = ctx["mode"]
    if mode == "local":
        # 虚拟 repo_path = 本地 local 记忆目录
        repo_path = str(Path(ctx["local_dir"]) / ".agent_memory")
        return _read_file_local(repo_path, basename, max_lines, offset)
    else:
        # 虚拟 repo_path = 沙箱记忆目录绝对路径
        return _read_file_sandbox(ctx, _MEMORY_DIR_SANDBOX, basename, max_lines, offset)


# ============================================================
# 工具 3:read_file(参考 Claude Code / TRAE Read:带行号 + offset 分页)
# ============================================================


def read_file(
    repo_path: str,
    file_path: str,
    max_lines: int = 200,
    offset: int = 1,
    task_id: str = "",
) -> dict:
    """读取仓库内文件内容(带行号,支持分页)

    参考 Claude Code / TRAE Read 工具设计:
    - 返回内容带行号(cat -n 格式),便于 LLM 精确定位行号
    - 支持 offset 从第 N 行开始读,配合 max_lines 翻页,避免大文件一次性撑爆上下文
    - 默认读前 200 行;需要看后面时调 offset=N 再读

    参数:
        repo_path: clone_repo 返回的 path
        file_path: 仓库内相对路径
        max_lines: 本次最多返回行数,默认 200
        offset: 从第几行开始读(1-based),默认 1

    返回:{
        "path": str,           # 文件相对路径
        "content": str,        # 带行号的内容(cat -n 格式)
        "start_line": int,     # 本次返回的起始行号
        "end_line": int,       # 本次返回的结束行号
        "total_lines": int,    # 文件总行数
        "truncated": bool,     # 是否还有更多未读(本次未读到文件尾)
        "binary": bool,        # 二进制文件(docx/pdf/图片…):content 为占位文案
        "size": int            # 二进制文件的字节数(文本态恒 0)
    }

    二进制文件不做内容回传:两种模式下原始字节既无法在文本通道/UTF-8 文本里
    正确表达,喂给模型只会污染上下文。模型需要文档正文时,由用户下载后
    以文本形式重新上传。

    特例:file_path 以 /home/user/.agent_memory/ 开头(记忆文件白名单)时,
    不受 repo_path 限制,直接读记忆目录文件(供查阅完整项目记忆 / 全局记忆)。
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    # 记忆文件白名单:绝对路径 /home/user/.agent_memory/* 不受 repo_path 限制
    if _is_memory_file_path(file_path):
        return _read_memory_file(ctx, file_path, max_lines, offset)

    if mode == "local":
        return _read_file_local(repo_path, file_path, max_lines, offset)
    else:
        return _read_file_sandbox(ctx, repo_path, file_path, max_lines, offset)


def _format_numbered_lines(lines: list[str], start_line: int) -> str:
    """把行列表格式化成 cat -n 风格的字符串(行号右对齐 + 冒号)"""
    width = len(str(start_line + len(lines) - 1))
    width = max(width, 4)  # 至少 4 位,视觉对齐
    return "\n".join(
        f"{str(i):>{width}}: {line}"
        for i, line in enumerate(lines, start=start_line)
    )


def _read_head(path: Path, n: int) -> bytes:
    """读文件前 n 字节(不整份进内存;空文件返回 b'')"""
    with path.open("rb") as f:
        return f.read(n)


def _binary_read_result(file_path: str, size: int) -> dict:
    """二进制文件的统一读取结果(三条读取路径共用)

    行号字段全 0 + 占位正文:前端的分页条据此隐藏,调用方(generator 预读)
    按占位文案跳过;binary 标记让前端不必匹配后缀表也能进入"下载卡片"视图。
    """
    return {
        "path": file_path,
        "content": BINARY_PLACEHOLDER,
        "start_line": 0,
        "end_line": 0,
        "total_lines": 0,
        "truncated": False,
        "binary": True,
        "size": int(size),
    }


def _text_read_result(
    file_path: str, body: str,
    start_line: int, end_line: int, total_lines: int,
) -> dict:
    """文本读取结果的统一形状

    size 恒为 0:字节数只在二进制下载卡片上用(见 _binary_read_result),文本态
    前端不展示它;两种模式都回 0,免得同一个字段按运行模式给出不同含义。
    """
    return {
        "path": file_path,
        "content": body,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": end_line < total_lines,
        "binary": False,
        "size": 0,
    }


def _read_file_local(
    repo_path: str, file_path: str, max_lines: int, offset: int,
    with_line_numbers: bool = True,
) -> dict:
    """local 模式:直接用 Python 读

    with_line_numbers:
        True(LLM 工具 read_file):content 带 cat -n 风格行号前缀
        False(前端 browse_read_file):content 为原始文本,前端自行渲染行号列
    """
    full_path = Path(repo_path) / file_path
    # 防路径穿越
    if not full_path.resolve().is_relative_to(Path(repo_path).resolve()):
        raise ValueError("非法路径:不能超出仓库根目录")

    if not full_path.is_file():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    size = full_path.stat().st_size
    # 二进制判定:后缀命中(零 IO)或头部窗口含 NUL。刻意不按"UTF-8 解码失败"
    # 判二进制 —— 那会把 GBK 中文文本也拦掉,而这些文件是有内容可看的。
    if is_likely_binary(file_path) or has_nul_bytes(_read_head(full_path, NUL_PROBE_BYTES)):
        return _binary_read_result(file_path, size)

    # errors="replace":二进制已在上一步拦下,残余的非 UTF-8 字节(GBK 文本等)
    # 以替换字符呈现,不再整份判为不可读
    content = full_path.read_bytes().decode("utf-8", errors="replace")

    all_lines = content.splitlines()
    total_lines = len(all_lines)

    # offset 是 1-based,转 0-based 切片
    start_idx = max(0, min(offset - 1, total_lines))
    end_idx = min(start_idx + max_lines, total_lines)
    selected = all_lines[start_idx:end_idx]

    start_line = start_idx + 1
    end_line = start_idx + len(selected)

    if with_line_numbers:
        body = _format_numbered_lines(selected, start_line)
    else:
        body = "\n".join(selected)

    return _text_read_result(
        file_path, body, start_line, end_line, total_lines
    )


def _sandbox_file_size(session: SandboxSession, full_path: str) -> int:
    """沙箱内二进制文件的字节数(仅命中二进制时查,文本路径不带这次额外往返)

    后缀短路分支没做过存在性检查,这里一并判存在:不存在的 .docx 应与其它文件
    一样抛 FileNotFoundError,而不是回一个 size=0 的"二进制"占位。
    """
    output = session.run_command(
        f"if [ -f {shlex.quote(full_path)} ]; then wc -c < {shlex.quote(full_path)}; "
        f"else echo MISSING; fi"
    )
    first = output.splitlines()[0].strip() if output.splitlines() else ""
    if first == "MISSING" or not first:
        raise FileNotFoundError(f"文件不存在: {full_path}")
    return int(first) if first.isdigit() else 0


def _sandbox_nul_probe(p: str) -> str:
    """NUL 字节探测的 shell 片段(p 为已 shlex.quote 的路径)

    `tr -dc '\000'` 只留 NUL,再由 wc -c 计数 —— 计数 > 0 即二进制。
    只看头部窗口(head -c),避免大文件整份管道传输。
    """
    return f"head -c {NUL_PROBE_BYTES} {p} | tr -dc '\\000' | wc -c"


def _read_file_sandbox(
    ctx: dict, repo_path: str, file_path: str, max_lines: int, offset: int,
    with_line_numbers: bool = True,
) -> dict:
    """sandbox 模式:在沙箱里用 awk 读(带行号 + 范围)

    with_line_numbers:
        True(LLM 工具 read_file):content 带 cat -n 风格行号前缀
        False(前端 browse_read_file):content 为原始文本,前端自行渲染行号列

    二进制拦截(必做,不是可选优化):本函数走 execd 的**文本通道**,原始字节会被
    当成文本回传,docx/pdf 因此在浏览器里渲染成乱码、在智能体上下文里污染推理。
    """
    session: SandboxSession = ctx["session"]
    full_path = f"{repo_path.rstrip('/')}/{file_path.lstrip('/')}"

    start = max(1, offset)
    end = start + max_lines - 1
    p = shlex.quote(full_path)

    # 后缀命中直接拦下:零往返,连文件都不用打开
    if is_likely_binary(file_path):
        return _binary_read_result(file_path, _sandbox_file_size(session, full_path))

    if not with_line_numbers:
        # 前端浏览路径:存在性检查 + 二进制探测 + 总行数 + 范围截取合并为单条命令
        # (1 次往返,探测不额外加账)
        # 输出约定:首行 MISSING=不存在 / BINARY=二进制,否则首行为总行数、其后为内容行
        awk_script = (
            f"NR>={start} && NR<={end} "
            f"{{printf \"%s\\n\", $0}}"
        )
        output = session.run_command(
            f"if [ -f {p} ]; then "
            f"if [ \"$({_sandbox_nul_probe(p)})\" -gt 0 ]; then echo BINARY; "
            f"else wc -l < {p}; awk '{awk_script}' {p}; fi; "
            f"else echo MISSING; fi"
        )
        out_lines = output.splitlines()
        if not out_lines or out_lines[0].strip() == "MISSING":
            raise FileNotFoundError(f"文件不存在: {file_path}")
        if out_lines[0].strip() == "BINARY":
            return _binary_read_result(file_path, _sandbox_file_size(session, full_path))
        total_str = out_lines[0].strip()
        total_lines = int(total_str) if total_str.isdigit() else 0
        content = "\n".join(out_lines[1:])
    else:
        check = session.run_command(
            f"if [ -f {p} ]; then "
            f"if [ \"$({_sandbox_nul_probe(p)})\" -gt 0 ]; then echo BINARY; else echo OK; fi; "
            f"else echo MISSING; fi"
        )
        if "MISSING" in check:
            raise FileNotFoundError(f"文件不存在: {file_path}")
        if "BINARY" in check:
            return _binary_read_result(file_path, _sandbox_file_size(session, full_path))

        total_lines_str = session.run_command(f"wc -l < {p}").strip()
        total_lines = int(total_lines_str) if total_lines_str.isdigit() else 0

        # 用 awk 一次性完成:行号格式化 + 范围截取
        awk_script = (
            f"NR>={start} && NR<={end} "
            f"{{printf \"%6d: %s\\n\", NR, $0}}"
        )
        content = session.run_command(f"awk '{awk_script}' {p}")

    start_line = min(start, total_lines) if total_lines > 0 else 0
    end_line = min(end, total_lines) if total_lines > 0 else 0

    return _text_read_result(file_path, content, start_line, end_line, total_lines)


# ============================================================
# 工具 4:search_code
# ============================================================


def search_code(
    repo_path: str,
    pattern: str,
    *,
    file_glob: str | None = None,
    case_sensitive: bool = False,
    max_matches: int = 50,
    context_lines: int = 0,
    output_mode: str = "content",
    offset: int = 0,
    task_id: str = "",
) -> dict:
    """在仓库里搜索代码(支持上下文、多种输出模式、分页)

    参考 TRAE Grep 工具设计:
    - output_mode:
        - "content"(默认):返回匹配行 + 行号 + 上下文
        - "files_with_matches":只返回含匹配的文件路径(快速定位)
        - "count":返回每个文件的匹配数
    - context_lines:匹配行前后各显示 N 行(仅 content 模式有效),
        安全审计场景建议设 3-5,便于理解漏洞上下文
    - offset:分页偏移,跳过前 N 个匹配

    返回(content):{"matches": [{file,line,content,context_before,context_after}], "total_matches", "truncated", "offset"}
    返回(files_with_matches):{"files": [...], "total_files", "truncated", "offset"}
    返回(count):{"counts": {file: count}, "total_matches"}
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "local":
        return _search_code_local(
            repo_path, pattern, file_glob, case_sensitive,
            max_matches, context_lines, output_mode, offset,
        )
    else:
        return _search_code_sandbox(
            ctx, repo_path, pattern, file_glob, case_sensitive,
            max_matches, context_lines, output_mode, offset,
        )


def _search_code_local(
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
    context_lines: int,
    output_mode: str,
    offset: int,
) -> dict:
    """local 模式:用 Python 实现搜索"""
    import fnmatch

    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    text_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
        ".kt", ".scala", ".sh", ".bash", ".yaml", ".yml", ".json",
        ".xml", ".html", ".css", ".scss", ".md", ".txt", ".toml",
        ".cfg", ".ini", ".env",
    }

    need_context = output_mode == "content" and context_lines > 0
    all_matches: list[dict] = []

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in text_exts:
                continue
            if file_glob and not fnmatch.fnmatch(fname, file_glob):
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            except (PermissionError, OSError):
                continue

            rel = os.path.relpath(fpath, repo_path)
            for i, line in enumerate(lines):
                if regex.search(line):
                    m = {"file": rel, "line": i + 1, "content": line.rstrip()}
                    if need_context:
                        start = max(0, i - context_lines)
                        end = i + 1 + context_lines
                        m["context_before"] = [l.rstrip() for l in lines[start:i]]
                        m["context_after"] = [l.rstrip() for l in lines[i + 1:end]]
                    all_matches.append(m)

    if output_mode == "count":
        counts: dict[str, int] = {}
        for m in all_matches:
            counts[m["file"]] = counts.get(m["file"], 0) + 1
        return {"counts": counts, "total_matches": len(all_matches)}

    if output_mode == "files_with_matches":
        files = sorted(set(m["file"] for m in all_matches))
        total = len(files)
        page = files[offset:offset + max_matches]
        return {
            "files": page,
            "total_files": total,
            "truncated": offset + len(page) < total,
            "offset": offset,
        }

    # output_mode == "content"
    total = len(all_matches)
    page = all_matches[offset:offset + max_matches]
    for m in page:
        m.setdefault("context_before", [])
        m.setdefault("context_after", [])
    return {
        "matches": page,
        "total_matches": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


def _search_code_sandbox(
    ctx: dict,
    repo_path: str,
    pattern: str,
    file_glob: str | None,
    case_sensitive: bool,
    max_matches: int,
    context_lines: int,
    output_mode: str,
    offset: int,
) -> dict:
    """sandbox 模式:用 ripgrep"""
    session: SandboxSession = ctx["session"]

    # ---- files_with_matches 模式:只返回文件路径 ----
    if output_mode == "files_with_matches":
        cmd_parts = ["rg", "--files-with-matches", "--color=never"]
        if not case_sensitive:
            cmd_parts.append("-i")
        if file_glob:
            cmd_parts.extend(["--glob", shlex.quote(file_glob)])
        cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])
        output = session.run_command(f"{' '.join(cmd_parts)} || true")
        files = []
        for line in output.splitlines():
            f = line.strip()
            if not f:
                continue
            if f.startswith(repo_path):
                f = f[len(repo_path):].lstrip("/")
            files.append(f)
        files.sort()
        total = len(files)
        page = files[offset:offset + max_matches]
        return {
            "files": page,
            "total_files": total,
            "truncated": offset + len(page) < total,
            "offset": offset,
        }

    # ---- count 模式:返回每个文件的匹配数 ----
    if output_mode == "count":
        cmd_parts = ["rg", "--count", "--color=never"]
        if not case_sensitive:
            cmd_parts.append("-i")
        if file_glob:
            cmd_parts.extend(["--glob", shlex.quote(file_glob)])
        cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])
        output = session.run_command(f"{' '.join(cmd_parts)} || true")
        counts = {}
        total = 0
        for line in output.splitlines():
            # 格式: path:count
            idx = line.rfind(":")
            if idx < 0:
                continue
            f = line[:idx]
            c_str = line[idx + 1:]
            c = int(c_str) if c_str.isdigit() else 0
            if f.startswith(repo_path):
                f = f[len(repo_path):].lstrip("/")
            counts[f] = c
            total += c
        return {"counts": counts, "total_matches": total}

    # ---- content 模式(默认):匹配行 + 可选上下文 ----
    # 用 rg -A/-B 一次性带上下文,避免对每个匹配单独跑 awk(N+1 沙箱往返)
    cmd_parts = ["rg", "--line-number", "--no-heading", "--color=never"]
    cmd_parts.extend(["--max-count", str(offset + max_matches)])
    if context_lines > 0:
        cmd_parts.extend([
            f"--before-context={context_lines}",
            f"--after-context={context_lines}",
        ])
    if not case_sensitive:
        cmd_parts.append("-i")
    if file_glob:
        cmd_parts.extend(["--glob", shlex.quote(file_glob)])
    cmd_parts.extend(["-e", shlex.quote(pattern), shlex.quote(repo_path)])
    cmd = " ".join(cmd_parts)
    logger.info(f"[sandbox] search: {cmd}")
    output = session.run_command(f"{cmd} || true")

    all_matches = _parse_search_output_with_context(output, repo_path)
    total = len(all_matches)
    page = all_matches[offset:offset + max_matches]

    return {
        "matches": page,
        "total_matches": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


# rg 输出解析正则:
# - 匹配行格式: path:line:content(分隔符为 :)
# - 上下文行格式: path-line-content(分隔符为 -)
# 贪婪 .* 从右往左定位 ":数字:" / "-数字-",可正确处理路径含 : 或 - 的情况
_MATCH_LINE_RE = re.compile(r"^(.*):(\d+):(.*)$")
_CONTEXT_LINE_RE = re.compile(r"^(.*)-(\d+)-(.*)$")


def _parse_search_output_with_context(output: str, repo_path: str) -> list[dict]:
    """解析 rg 输出(支持 -A/-B 上下文模式)

    rg --no-heading 输出格式:
    - 匹配行: path:line:content
    - 上下文行: path-line-content(用 - 区分匹配行的 :)
    - 多个匹配之间用 -- 分隔(仅当带 -A/-B 时)

    无上下文时全是匹配行(无 -- 分隔),本函数同样适用:
    每个 match 的 context_before/after 为空列表。

    优先按匹配行格式解析(:line:),失败再按上下文行格式(-line-),
    避免上下文行的 content 含 ":N:" 时被误判。
    """
    matches: list[dict] = []
    current: dict | None = None
    before: list[str] = []
    after: list[str] = []

    def _finalize() -> None:
        nonlocal current, before, after
        if current is not None:
            current["context_before"] = before
            current["context_after"] = after
            matches.append(current)
            current = None
            before = []
            after = []

    for line in output.splitlines():
        if not line:
            continue
        if line == "--":
            _finalize()
            continue
        # 先尝试匹配行格式 path:N:content
        m = _MATCH_LINE_RE.match(line)
        if m:
            # 遇到新匹配,先收尾上一个(无 -- 分隔时也兼容)
            _finalize()
            path, line_no, content = m.groups()
            if path.startswith(repo_path):
                path = path[len(repo_path):].lstrip("/")
            current = {
                "file": path,
                "line": int(line_no),
                "content": content,
            }
            continue
        # 再尝试上下文行格式 path-N-content
        m = _CONTEXT_LINE_RE.match(line)
        if m and current is not None:
            _path, line_no, content = m.groups()
            ln = int(line_no)
            if ln < current["line"]:
                before.append(content)
            else:
                after.append(content)
            continue
        # 无法解析的行,跳过

    _finalize()
    return matches


# ============================================================
# 工具:find_files(按文件名 glob 查找,参考 TRAE Glob 工具)
# ============================================================


def find_files(
    repo_path: str,
    pattern: str,
    max_results: int = 100,
    offset: int = 0,
    task_id: str = "",
) -> dict:
    """按 glob 模式递归查找仓库内文件路径(不看内容)

    参考 TRAE Glob 工具设计:
    - 按文件名 pattern 匹配,不读取文件内容
    - 递归查找(支持 ** 通配)
    - 跳过噪声目录(.git / node_modules / __pycache__ / venv 等)
    - 返回相对仓库根的路径列表,按路径排序
    - 支持分页(offset + max_results)

    与 list_files 的区别:
    - list_files:列单层目录,看结构
    - find_files:按 pattern 递归定位文件,知道文件名/扩展名时用

    与 search_code 的区别:
    - search_code:按文件内容搜索(正则)
    - find_files:按文件名 pattern 搜索

    pattern 示例:
    - "**/*.py":所有层级的 .py 文件(递归)
    - "src/**/*.ts":src 下所有 .ts 文件
    - "**/test_*.py":所有 test_ 开头的 .py 文件
    - "**/*.{js,ts}":所有 .js 和 .ts 文件(brace expansion)

    参数:
        repo_path: clone_repo 返回的 path
        pattern: glob 模式(支持 *、**、?、{a,b})
        max_results: 最多返回文件数,默认 100
        offset: 分页偏移,跳过前 N 个结果,默认 0

    返回:{
        "pattern": str,
        "files": ["src/main.py", "src/utils.py", ...],  # 相对路径
        "total": int,
        "truncated": bool,
        "offset": int,
    }
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "local":
        return _find_files_local(repo_path, pattern, max_results, offset)
    else:
        return _find_files_sandbox(ctx, repo_path, pattern, max_results, offset)


def _expand_braces(pattern: str) -> list[str]:
    """展开 {a,b} brace expansion 成多个 glob pattern

    Python pathlib.glob 不支持 {a,b} 语法(rg --glob 原生支持),
    local 模式手动展开以保持与 sandbox 模式行为一致。
    支持嵌套(递归处理)。无 brace 时返回 [pattern]。
    """
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    options = m.group(1).split(",")
    expanded: list[str] = []
    for opt in options:
        sub = pattern[:m.start()] + opt.strip() + pattern[m.end():]
        expanded.extend(_expand_braces(sub))
    return expanded


def _find_files_local(
    repo_path: str, pattern: str, max_results: int, offset: int,
) -> dict:
    """local 模式:用 pathlib.Path.glob 递归匹配

    Python pathlib.glob 语义:
    - "*.py" 只匹配根目录(不递归)
    - "**/*.py" 递归所有层级
    - "src/**/*.py" 递归 src 下所有层级
    与 rg --glob 的"*.py 递归"语义有差异,文档里提示 LLM 用 ** 明确递归。
    """
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"仓库目录不存在: {repo_path}")

    # Python pathlib 不支持 {a,b},手动展开成多个 pattern
    patterns = _expand_braces(pattern)
    seen: set[str] = set()
    matched: list[str] = []
    for pat in patterns:
        for p in root.glob(pat):
            if not p.is_file():
                continue
            rel_parts = p.relative_to(root).parts
            # 跳过噪声目录下的文件(检查除文件名外的父目录)
            if any(part in _SKIP_DIRS_LIST for part in rel_parts[:-1]):
                continue
            rel = str(p.relative_to(root))
            if rel not in seen:
                seen.add(rel)
                matched.append(rel)

    matched.sort()
    total = len(matched)
    page = matched[offset:offset + max_results]
    return {
        "pattern": pattern,
        "files": page,
        "total": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


def _find_files_sandbox(
    ctx: dict, repo_path: str, pattern: str, max_results: int, offset: int,
) -> dict:
    """sandbox 模式:用 rg --files --glob 递归匹配

    rg --files 列出所有文件路径(每行一个),--glob 按 gitignore 风格 glob 过滤。
    rg 的 --glob 语义:
    - "*.py" 递归匹配任意层级(与 Python pathlib 不同)
    - "**/*.py" 同上
    - "src/**/*.py" 匹配 src 下任意层级
    - 支持 {a,b} brace expansion

    --no-ignore:不遵守 .gitignore(列出所有文件,含被 ignore 的配置文件)
    --hidden:包含隐藏文件(如 .env.example)
    然后手动排除噪声目录,保证与 local 模式行为一致。
    """
    session: SandboxSession = ctx["session"]

    # 检查仓库目录存在
    check = session.run_command(
        f"test -d {shlex.quote(repo_path)} && echo OK || echo MISSING"
    )
    if "MISSING" in check:
        raise FileNotFoundError(f"仓库目录不存在: {repo_path}")

    # rg --files 列出所有文件路径,--glob 过滤
    cmd_parts = ["rg", "--files", "--color=never", "--no-ignore", "--hidden"]
    # 排除噪声目录(rg --glob 用 ! 前缀表示排除,匹配任意层级)
    for skip in _SKIP_DIRS_LIST:
        cmd_parts.extend(["--glob", f"!**/{skip}/**"])
    # 用户的 pattern
    cmd_parts.extend(["--glob", shlex.quote(pattern)])
    cmd_parts.append(shlex.quote(repo_path))

    cmd = " ".join(cmd_parts)
    logger.info(f"[sandbox] find_files: {cmd}")
    output = session.run_command(f"{cmd} || true")

    files: list[str] = []
    for line in output.splitlines():
        f = line.strip()
        if not f:
            continue
        # 去掉 repo_path 前缀,转成相对路径
        if f.startswith(repo_path):
            f = f[len(repo_path):].lstrip("/")
        files.append(f)
    files.sort()
    total = len(files)
    page = files[offset:offset + max_results]

    return {
        "pattern": pattern,
        "files": page,
        "total": total,
        "truncated": offset + len(page) < total,
        "offset": offset,
    }


# ============================================================
# 工具:write_file / run_python_code(独立工作区,原仓库只读)
# ============================================================

# 工作区根路径(sandbox 模式);local 模式用 ctx["local_dir"]/workspace
_WORKSPACE_DIR_SANDBOX = "/home/user/workspace"
# 单次 run_python_code 执行超时(秒)
_RUN_CODE_TIMEOUT = 60
# 输出截断阈值(stdout/stderr 合计)
_RUN_CODE_OUTPUT_LIMIT = 5000
# 单次写入文件大小上限(防 LLM 写入超大文件撑爆沙箱)
_WRITE_FILE_SIZE_LIMIT = 200_000


def _get_workspace_dir(ctx: dict) -> str:
    """获取(并按需创建)任务的工作区目录

    工作区独立于仓库 clone 路径,react_agent 在这里写 PoC、补丁、报告等产物,
    不污染原仓库(保持审计可追溯)。

    local 模式:本地临时目录下的 workspace 子目录
    sandbox 模式:/home/user/workspace(沙箱内)
    """
    mode = ctx["mode"]
    if mode == "local":
        ws_dir: Path = ctx["local_dir"] / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        return str(ws_dir)
    else:
        session: SandboxSession = ctx["session"]
        session.run_command(f"mkdir -p {shlex.quote(_WORKSPACE_DIR_SANDBOX)}")
        return _WORKSPACE_DIR_SANDBOX


def _resolve_workspace_path(ws_dir: str, file_path: str) -> str:
    """把相对 file_path 解析到工作区内的绝对路径,防路径穿越

    禁止 file_path 含 .. 或绝对路径(防止逃逸工作区改原仓库或系统文件)。
    """
    if not file_path:
        raise ValueError("file_path 不能为空")
    # 统一用 / 分隔(沙箱是 Linux,LLM 传 \ 也能容错)
    normalized = file_path.replace("\\", "/").lstrip("/")
    if ".." in normalized.split("/"):
        raise ValueError("file_path 不能含 .. (防止路径穿越)")
    if Path(normalized).is_absolute():
        raise ValueError("file_path 必须是相对路径(相对工作区根)")
    return f"{ws_dir.rstrip('/')}/{normalized}"


def write_file(
    file_path: str,
    content: str,
    mode: str = "write",
    task_id: str = "",
) -> dict:
    """在工作区写入文件(不影响原仓库)

    工作区是独立目录,与 clone 的仓库隔离。react_agent 在这里写 PoC 脚本、
    修复补丁、分析报告等产物。原仓库保持只读,保证审计可追溯。

    参数:
        file_path: 工作区内相对路径(如 "poc/sqli_test.py"、"patches/fix.diff")
            不能含 .. 或绝对路径(防路径穿越)
        content: 文件内容(文本)
        mode: 写入模式
            - "write"(默认):覆盖写入(文件不存在则创建,存在则覆盖)
            - "append":追加写入(在文件末尾追加)

    返回:{
        "path": str,       # 工作区内相对路径
        "abs_path": str,   # 绝对路径(供 run_python_code 等引用)
        "bytes": int,      # 写入字节数
        "mode": str,       # 实际使用的写入模式
    }
    """
    if not isinstance(content, str):
        raise TypeError("content 必须是字符串")
    if len(content) > _WRITE_FILE_SIZE_LIMIT:
        raise ValueError(
            f"文件内容过大({len(content)} 字符),上限 "
            f"{_WRITE_FILE_SIZE_LIMIT}。建议拆分多次写入或精简内容。"
        )
    if mode not in ("write", "append"):
        raise ValueError(f"mode 必须是 'write' 或 'append',收到: {mode}")

    ctx = _get_or_create_session(task_id)
    ws_dir = _get_workspace_dir(ctx)
    abs_path = _resolve_workspace_path(ws_dir, file_path)

    sandbox_mode = ctx["mode"]
    if sandbox_mode == "local":
        # local 模式:直接用 Python 写(带写权限检查:.git/只读目录保护)
        p = Path(abs_path)
        check_local_write_permission(p.resolve(), Path(ws_dir), file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append" and p.exists():
            existing = p.read_text(encoding="utf-8")
            content = existing + content
        p.write_text(content, encoding="utf-8")
    else:
        # sandbox 模式:复用 SandboxSession.write_file
        #   write 模式:直接写(底层会覆盖)
        #   append 模式:先读后写(沙箱没原生 append 接口,模拟)
        session: SandboxSession = ctx["session"]
        parent = str(Path(abs_path).parent)
        session.run_command(f"mkdir -p {shlex.quote(parent)}")
        if mode == "append":
            try:
                existing = session.read_file(abs_path)
            except Exception:
                existing = ""
            content = existing + content
        session.write_file(abs_path, content)

    return {
        "path": file_path,
        "abs_path": abs_path,
        "bytes": len(content.encode("utf-8")),
        "mode": mode,
    }


def run_python_code(
    code: str,
    task_id: str = "",
    timeout: int = _RUN_CODE_TIMEOUT,
) -> dict:
    """在沙箱里执行 Python 代码,返回 stdout/stderr/exit_code

    用于:
    - 验证漏洞 PoC(如触发 SQL 注入、跑反序列化 payload)
    - 跑分析脚本(如解析依赖树、调用图分析)
    - 执行仓库测试用例验证假设

    执行环境:
    - 工作目录:工作区根(/home/user/workspace 或 local 等价目录)
    - Python:沙箱内置的 python3
    - 网络:依赖沙箱配置(默认沙箱禁外网,防数据外泄/C2 回连)
    - 超时:默认 60s,超时强制终止

    参数:
        code: Python 代码(字符串)。多行直接写,无需转义
        timeout: 执行超时秒数,默认 60,上限 120

    返回:{
        "stdout": str,      # 标准输出(截断到 _RUN_CODE_OUTPUT_LIMIT)
        "stderr": str,      # 标准错误(截断)
        "exit_code": int,   # 退出码(0 表示成功)
        "duration_ms": int, # 执行耗时(毫秒)
        "truncated": bool,  # 输出是否被截断
        "timed_out": bool,  # 是否超时被强制终止
    }
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code 不能为空")
    timeout = max(1, min(timeout, 120))

    ctx = _get_or_create_session(task_id)
    ws_dir = _get_workspace_dir(ctx)
    sandbox_mode = ctx["mode"]

    # 代码写到临时文件再执行(避免 shlex 转义复杂代码出错)
    # 文件名加 uuid 避免并发冲突
    script_name = f"_run_{uuid.uuid4().hex[:8]}.py"
    write_file(script_name, code, mode="write", task_id=task_id)
    script_abs = _resolve_workspace_path(ws_dir, script_name)

    start = time.time()
    timed_out = False
    if sandbox_mode == "local":
        # local 模式:本地 subprocess 执行
        try:
            result = subprocess.run(
                ["python", script_abs],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=ws_dir,
            )
            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.returncode
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or "") if isinstance(e.stdout, str) else ""
            stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
            stderr = (stderr + f"\n[执行超时({timeout}s),被强制终止]")
            exit_code = -1
            timed_out = True
    else:
        # sandbox 模式:沙箱里执行
        # 命令拼接:cd 工作区 → timeout 限时 → python3 执行 → 末尾 echo exit code
        # 2>&1 合并 stdout/stderr(沙箱 run_command 只返回 stdout 一个通道)
        # exit code 用 echo "EXIT_CODE:$?" 附加到输出末尾,本地解析
        session: SandboxSession = ctx["session"]
        cmd = (
            f"cd {shlex.quote(ws_dir)} && "
            f"timeout {timeout} python3 {shlex.quote(script_abs)} 2>&1; "
            f'echo "EXIT_CODE:$?"'
        )
        try:
            combined = session.run_command(cmd, timeout=timeout + 5)
            # 从输出末尾解析 "EXIT_CODE:N" 行
            stdout = combined
            stderr = ""
            exit_code = 0
            # 找最后一个 EXIT_CODE: 行(防代码本身输出过这个串)
            m = None
            for line in reversed(combined.splitlines()):
                if line.startswith("EXIT_CODE:"):
                    m = line
                    break
            if m:
                code_str = m[len("EXIT_CODE:"):].strip()
                # timeout 命令超时返回 124
                exit_code = int(code_str) if code_str.lstrip("-").isdigit() else -1
                # 去掉这行,剩余作为真实输出
                stdout = combined.rsplit(m, 1)[0].rstrip("\n")
                if exit_code == 124:
                    timed_out = True
                    stderr = f"[执行超时({timeout}s),被 timeout 命令终止]"
        except Exception as e:
            stdout = ""
            stderr = f"[沙箱执行失败: {e}]"
            exit_code = -1

    duration_ms = int((time.time() - start) * 1000)

    # 输出截断
    truncated = False
    if len(stdout) + len(stderr) > _RUN_CODE_OUTPUT_LIMIT:
        total = len(stdout) + len(stderr)
        # 按比例裁剪,保留尾部(通常错误信息在尾部)
        if stdout:
            keep_stdout = max(200, int(_RUN_CODE_OUTPUT_LIMIT * len(stdout) / total))
            if len(stdout) > keep_stdout:
                stdout = "[...输出过长,已截断头部...]\n" + stdout[-keep_stdout:]
        if stderr:
            keep_stderr = max(200, int(_RUN_CODE_OUTPUT_LIMIT * len(stderr) / total))
            if len(stderr) > keep_stderr:
                stderr = "[...输出过长,已截断头部...]\n" + stderr[-keep_stderr:]
        truncated = True

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "timed_out": timed_out,
    }


# ============================================================
# 工具:git_log / git_blame(让 agent 直达 git 历史)
# ============================================================

# git 只读子命令(log/blame)的执行超时(本地操作,给 60s 足够)
_GIT_CMD_TIMEOUT = 60


def _run_git(
    repo_path: str,
    args: list[str],
    task_id: str = "",
    output_limit: int = _RUN_CODE_OUTPUT_LIMIT,
) -> dict:
    """在仓库目录里运行 git 只读子命令(local 本地 subprocess / sandbox session.run_command)

    供 git_log / git_blame / git_diff 共用。所有参数以列表形式传递,repo_path 用 -C 指定,
    文件路径参数由调用方以 "--" 元素分隔(防选项注入),sandbox 模式再逐个 shlex.quote。

    output_limit: 输出截断上限。默认复用 run_python_code 的上限;
    git_diff 等输出天然较大的工具可传更大值。

    返回:{
        "output": str,      # git 输出(stdout + 必要时 stderr,截断到 output_limit)
        "exit_code": int,   # 0 表示成功
        "truncated": bool,  # 输出是否被截断
    }
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    output = ""
    exit_code = 0
    truncated = False

    if mode == "local":
        # local 模式:本地 subprocess,列表形式无需 shell,无注入风险
        cmd = ["git", "-C", repo_path] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_GIT_CMD_TIMEOUT,
            )
            output = result.stdout
            exit_code = result.returncode
            # 失败时附上 stderr 便于排查(如非 git 仓库、文件不存在)
            if exit_code != 0 and result.stderr:
                output = (output + ("\n" if output else "") + result.stderr).strip()
        except subprocess.TimeoutExpired:
            output = f"[git 执行超时({_GIT_CMD_TIMEOUT}s)]"
            exit_code = -1
        except FileNotFoundError:
            output = "[宿主机未安装 git,local 模式无法运行 git 子命令]"
            exit_code = -1
    else:
        # sandbox 模式:session.run_command(单通道),2>&1 合并 + 末尾 echo exit code
        session: SandboxSession = ctx["session"]
        quoted_args = " ".join(shlex.quote(a) for a in args)
        cmd = (
            f"git -C {shlex.quote(repo_path)} {quoted_args} 2>&1; "
            f'echo "EXIT_CODE:$?"'
        )
        try:
            combined = session.run_command(cmd, timeout=_GIT_CMD_TIMEOUT + 5)
            output = combined
            # 从末尾解析 EXIT_CODE 行(防代码本身输出过这个串)
            m = None
            for line in reversed(combined.splitlines()):
                if line.startswith("EXIT_CODE:"):
                    m = line
                    break
            if m:
                code_str = m[len("EXIT_CODE:"):].strip()
                exit_code = int(code_str) if code_str.lstrip("-").isdigit() else -1
                output = combined.rsplit(m, 1)[0].rstrip("\n")
        except Exception as e:
            output = f"[沙箱执行 git 失败: {e}]"
            exit_code = -1

    # 输出截断(保留尾部——错误信息常在尾部;git_diff 等传更大 output_limit)
    if len(output) > output_limit:
        output = "[...输出过长,已截断头部...]\n" + output[-output_limit:]
        truncated = True

    return {"output": output, "exit_code": exit_code, "truncated": truncated}


def git_log(
    repo_path: str,
    max_count: int = 20,
    file_path: str | None = None,
    oneline: bool = True,
    task_id: str = "",
) -> dict:
    """查看仓库提交历史(默认 --oneline 紧凑输出)

    完整克隆(默认)可见全部历史;浅克隆(--depth 1)仅 1 条 commit。

    参数:
        repo_path: clone_repo 返回的 path
        max_count: 最多返回提交数,默认 20,上限 200
        file_path: 可选,只看某文件的历史(仓库内相对路径)
        oneline: True=--oneline 紧凑输出(默认,一行一提交);False=含作者/日期/正文

    返回:{"output": str, "exit_code": int, "truncated": bool}
    """
    max_count = max(1, min(int(max_count or 20), 200))
    args: list[str] = ["log"]
    if oneline:
        args.append("--oneline")
    args += ["-n", str(max_count)]
    if file_path:
        # "--" 分隔,防止 file_path 被解析为选项(选项注入)
        args += ["--", file_path]
    return _run_git(repo_path, args, task_id)


def git_blame(
    repo_path: str,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    task_id: str = "",
) -> dict:
    """追溯某文件(可指定行区间)每行的最后修改提交/作者/时间

    完整克隆(默认)可见完整 blame;浅克隆下 blame 信息受限(无历史可追溯)。

    参数:
        repo_path: clone_repo 返回的 path
        file_path: 仓库内相对路径(必填)
        start_line: 起始行号(1-based,可选)
        end_line: 结束行号(1-based,可选)。只传一个行号时按单行区间处理

    返回:{"output": str, "exit_code": int, "truncated": bool}
    """
    args: list[str] = ["blame"]
    if start_line is not None and end_line is not None:
        s = max(1, int(start_line))
        e = max(s, int(end_line))
        args += ["-L", f"{s},{e}"]
    elif start_line is not None or end_line is not None:
        ln = max(1, int(start_line if start_line is not None else end_line))
        args += ["-L", f"{ln},{ln}"]
    # "--" 分隔,防止 file_path 被解析为选项
    args += ["--", file_path]
    return _run_git(repo_path, args, task_id)


# git_diff 输出预算:diff 天然比 log/blame 大,拉高截断上限后再按文件结构化截断
_GIT_DIFF_OUTPUT_LIMIT = 40000
# 单文件 patch 截断上限 / 最多返回文件数(控 token,防大区间 diff 冲爆上下文)
_GIT_DIFF_PATCH_LIMIT = 2000
_GIT_DIFF_MAX_FILES = 30


def _validate_git_ref(ref: str, name: str) -> str:
    """校验 git ref(分支/提交/标签),拒绝选项注入与空白字符

    ref 会作为 git diff 的位置参数,若以 - 开头可能被 git 解析为选项;
    空白字符在 sandbox 拼接命令时也会造成歧义,一并拒绝。
    """
    ref = (ref or "").strip()
    if not ref:
        raise ValueError(f"{name} 不能为空")
    if ref.startswith("-"):
        raise ValueError(f"{name} 不能以 - 开头(防选项注入): {ref}")
    if any(c.isspace() for c in ref):
        raise ValueError(f"{name} 不能含空白字符: {ref}")
    return ref


def _parse_numstat(output: str) -> list[dict]:
    """解析 git diff --numstat 输出为每文件增删行数清单

    行格式:added\tdeleted\tpath(二进制文件 added/deleted 为 -)
    """
    files = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        files.append({
            "path": path,
            "additions": int(added_s) if added_s.lstrip("-").isdigit() else 0,
            "deletions": int(deleted_s) if deleted_s.lstrip("-").isdigit() else 0,
        })
    return files


def _parse_diff_patches(output: str) -> dict[str, str]:
    """把 git diff 全量输出按文件切块,返回 {path: patch}

    以 "diff --git " 行分块;路径从 "+++ b/<path>" 提取,
    新增文件取 "--- a/<path>"(此时 +++ 是 /dev/null)。重命名块用头行 b/ 路径兜底。
    """
    patches: dict[str, str] = {}
    lines = output.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith("diff --git ")]
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        chunk_lines = lines[s:e]
        path = ""
        minus_path = ""
        for ln in chunk_lines[1:]:
            if ln.startswith("--- "):
                minus_path = ln[4:].strip()
            elif ln.startswith("+++ "):
                target = ln[4:].strip()
                if target == "/dev/null":
                    # 删除文件:从 --- a/<path> 取
                    path = minus_path[2:] if minus_path.startswith("a/") else minus_path
                else:
                    path = target[2:] if target.startswith("b/") else target
                break
        if not path:
            # 兜底:从 "diff --git a/x b/x" 头行取 b/ 路径
            header = chunk_lines[0][len("diff --git "):].strip()
            if " b/" in header:
                path = header.rsplit(" b/", 1)[1]
        if path:
            patches[path] = "".join(chunk_lines)
    return patches


def git_diff(
    repo_path: str,
    base: str = "HEAD~1",
    head: str = "HEAD",
    file_path: str | None = None,
    stat_only: bool = False,
    task_id: str = "",
) -> dict:
    """查看两个 ref(提交/分支/标签)之间的结构化 diff(增量审查/演化分析用)

    参数:
        repo_path: clone_repo 返回的 path
        base: 起始 ref,默认 HEAD~1(即默认看最近一次提交的变更)
        head: 结束 ref,默认 HEAD。也可传分支名比较分支差异(如 base="main" head="feature")
        file_path: 可选,只看某文件的 diff(仓库内相对路径)
        stat_only: True=只返回每文件增删行数(不看 patch,大区间先用它总览)

    返回:{
        "base": str, "head": str,
        "files": [{"path", "additions", "deletions", "patch"}],  # stat_only 时无 patch
        "total_files": int,      # 变更文件总数(可能大于 files 长度——超上限截断)
        "truncated": bool,       # 文件数或单文件 patch 被截断
        "exit_code": int,        # 0=成功;非 0 时附 error(ref 不存在等)
    }
    """
    base = _validate_git_ref(base, "base")
    head = _validate_git_ref(head, "head")

    range_args: list[str] = ["diff"]
    if file_path:
        # numstat + 路径过滤:"--" 分隔防选项注入
        range_args += ["--numstat", base, head, "--", file_path]
    else:
        range_args += ["--numstat", base, head]
    stat_result = _run_git(repo_path, range_args, task_id, output_limit=_GIT_DIFF_OUTPUT_LIMIT)
    if stat_result["exit_code"] != 0:
        return {
            "base": base, "head": head, "files": [], "total_files": 0,
            "truncated": False, "exit_code": stat_result["exit_code"],
            "error": stat_result["output"][:500] or "git diff --numstat 执行失败",
        }

    stats = _parse_numstat(stat_result["output"])
    total_files = len(stats)
    truncated = stat_result["truncated"]

    if stat_only:
        files = stats[:_GIT_DIFF_MAX_FILES]
        return {
            "base": base, "head": head,
            "files": files,
            "total_files": total_files,
            "truncated": truncated or total_files > len(files),
            "exit_code": 0,
        }

    # 全量 patch(同区间再跑一次,拿到后按文件切块)
    patch_args: list[str] = ["diff", base, head]
    if file_path:
        patch_args += ["--", file_path]
    patch_result = _run_git(repo_path, patch_args, task_id, output_limit=_GIT_DIFF_OUTPUT_LIMIT)
    patches = _parse_diff_patches(patch_result["output"]) if patch_result["exit_code"] == 0 else {}
    truncated = truncated or patch_result["truncated"]

    files = []
    for st in stats[:_GIT_DIFF_MAX_FILES]:
        patch = patches.get(st["path"], "")
        if len(patch) > _GIT_DIFF_PATCH_LIMIT:
            patch = patch[:_GIT_DIFF_PATCH_LIMIT] + "\n[...单文件 diff 过长,已截断...]"
            truncated = True
        files.append({**st, "patch": patch})

    return {
        "base": base, "head": head,
        "files": files,
        "total_files": total_files,
        "truncated": truncated or total_files > len(files),
        "exit_code": 0,
    }


# ============================================================
# 工具:run_command / str_replace_editor(向 CLI 看齐:跑 shell + 精准编辑)
# ============================================================


def _classify_command(command: str) -> tuple[str, str | None]:
    """分类 local 模式命令安全等级

    返回 (level, matched_pattern):
    - ("safe", None): 安全命令,所有子命令都匹配安全前缀,直接执行
    - ("dangerous", pattern): 危险命令,某个子命令匹配危险正则,需用户确认
    - ("normal", None): 普通命令,执行但记录日志

    对复合命令(用 && / ; / | 连接),按分隔符拆分逐个检查,
    任一子命令危险则整个命令危险。
    """
    safe_prefixes = [
        s.strip() for s in settings.SANDBOX_LOCAL_SAFE_COMMANDS.split(",") if s.strip()
    ]
    dangerous_patterns = [
        p.strip() for p in settings.SANDBOX_LOCAL_DANGEROUS_COMMANDS.split(",") if p.strip()
    ]
    # 按 && / ; / | 分割(简单分割,不处理引号内分隔符——LLM 生成的命令极少含引号包裹的分隔符)
    sub_commands = re.split(r"\s*(?:&&|;|\|)\s*", command)
    sub_commands = [s.strip() for s in sub_commands if s.strip()]

    # 先检查危险(优先级最高)
    for sub in sub_commands:
        for pattern in dangerous_patterns:
            try:
                if re.search(pattern, sub):
                    return ("dangerous", pattern)
            except re.error:
                continue  # 配置的正则无效,跳过

    # 再检查是否全部安全
    if not sub_commands:
        return ("normal", None)
    all_safe = all(
        any(sub.startswith(prefix) for prefix in safe_prefixes)
        for sub in sub_commands
    )
    return ("safe", None) if all_safe else ("normal", None)


def run_command(
    command: str,
    repo_path: str = "",
    timeout: int = 60,
    task_id: str = "",
    command_confirm_mode: str = "always_approve",
) -> dict:
    """在沙箱里执行任意 shell 命令(与 CLI 的 bash 工具对齐)

    用于跑构建/测试/脚本等,如 ./build.sh、pytest -x、npm test、pip show pkg。
    命令在沙箱内执行,沙箱即隔离边界(与 run_python_code 同等风险面);
    网络访问依赖沙箱配置(默认禁外网)。

    参数:
        command: shell 命令字符串(agent 自拟,非用户输入插值,无注入问题)
        repo_path: 可选,clone_repo 返回的 path。提供则在仓库目录下执行(cd repo && command)
        timeout: 超时秒,默认 60,上限 300(构建/测试可能较久)
        command_confirm_mode: 命令确认模式(execute_tool 从 ContextVar 自动注入)
            "always_approve":危险命令直接执行不弹窗(默认)
            "per_command":危险命令推前端 CommandConfirmDialog 弹窗确认
            local 模式下 dangerous 命令始终推确认(宿主机直接执行,无视此参数);
            sandbox 模式下仅 per_command 时 dangerous 命令推确认。

    返回:{"output": str, "exit_code": int, "truncated": bool}
    """
    timeout = max(1, min(int(timeout or 60), 300))
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    output = ""
    exit_code = 0
    truncated = False

    # 命令分类(safe / normal / dangerous),local 与 sandbox 共用
    level, pattern = _classify_command(command)

    if mode == "local":
        # local 模式:命令在宿主机直接执行,dangerous 命令始终推前端确认(无视 command_confirm_mode)
        # 因为宿主机无隔离边界,即使 always_approve 也不能跳过危险命令确认
        if level == "dangerous":
            command_id = f"cmd_{uuid.uuid4().hex[:8]}"
            request_command_confirm(task_id, {
                "command_id": command_id,
                "command": command,
                "tool": "run_command",
                "reason": f"匹配危险命令模式: {pattern}",
            })
            approved = wait_for_command_confirm(task_id, command_id)
            if not approved:
                return {
                    "output": "[用户拒绝执行此命令]",
                    "exit_code": -1,
                    "truncated": False,
                }
        elif level == "normal":
            logger.info(f"[task={task_id}] local 模式执行普通命令: {command[:100]}")
        # safe 命令直接执行,不记录

        # 本地 subprocess(shell=True)。用 cwd 而非命令里 cd,避开 Windows 盘符问题
        try:
            result = subprocess.run(
                command, shell=True, cwd=repo_path or None,
                capture_output=True, text=True, timeout=timeout,
            )
            output = result.stdout
            exit_code = result.returncode
            if exit_code != 0 and result.stderr:
                output = (output + ("\n" if output else "") + result.stderr).strip()
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else ""
            err = e.stderr if isinstance(e.stderr, str) else ""
            output = (out + ("\n" if out and err else "") + err).strip()
            output = (output + ("\n" if output else "") + f"[命令执行超时({timeout}s)]").strip()
            exit_code = -1
    else:
        # sandbox 模式:容器内执行,沙箱即隔离边界
        # per_command 模式下,dangerous 命令推前端确认(对齐 local 模式的 _PendingCommandConfirm 机制)
        # always_approve 模式下直接执行(沙箱已隔离,危险命令破坏范围限于容器内)
        if command_confirm_mode == "per_command" and level == "dangerous":
            command_id = f"cmd_{uuid.uuid4().hex[:8]}"
            request_command_confirm(task_id, {
                "command_id": command_id,
                "command": command,
                "tool": "run_command",
                "reason": f"匹配危险命令模式: {pattern}",
            })
            approved = wait_for_command_confirm(task_id, command_id)
            if not approved:
                return {
                    "output": "[用户拒绝执行此命令]",
                    "exit_code": -1,
                    "truncated": False,
                }

        # session.run_command(单通道),2>&1 合并 + 末尾 echo exit code
        session: SandboxSession = ctx["session"]
        full = command if not repo_path else f"cd {shlex.quote(repo_path)} && {command}"
        cmd = f"{full} 2>&1; " f'echo "EXIT_CODE:$?"'
        try:
            combined = session.run_command(cmd, timeout=timeout + 5)
            output = combined
            # 从末尾解析 EXIT_CODE 行(防命令本身输出过这个串)
            m = None
            for line in reversed(combined.splitlines()):
                if line.startswith("EXIT_CODE:"):
                    m = line
                    break
            if m:
                code_str = m[len("EXIT_CODE:"):].strip()
                exit_code = int(code_str) if code_str.lstrip("-").isdigit() else -1
                output = combined.rsplit(m, 1)[0].rstrip("\n")
        except Exception as e:
            output = f"[沙箱执行命令失败: {e}]"
            exit_code = -1

    # 输出截断(复用 run_python_code 的上限,保留尾部——错误信息常在尾部)
    if len(output) > _RUN_CODE_OUTPUT_LIMIT:
        output = "[...输出过长,已截断头部...]\n" + output[-_RUN_CODE_OUTPUT_LIMIT:]
        truncated = True

    return {"output": output, "exit_code": exit_code, "truncated": truncated}


def _resolve_repo_file(repo_path: str, file_path: str, mode: str) -> str:
    """解析仓库内文件为绝对路径,防路径穿越(禁止 .. / 绝对路径)

    供 str_replace_editor 共用。local 模式额外用 Path.resolve().is_relative_to 复核
    (同 _read_file_local);sandbox 模式靠 .. 组件检查(主机无法 resolve 容器路径)。
    返回 "repo_path/normalized" 字符串(local 下亦是本地路径)。
    """
    if not file_path:
        raise ValueError("file_path 不能为空")
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("file_path 必须是相对路径(不能以 / 开头)")
    if ".." in normalized.split("/"):
        raise ValueError("file_path 不能含 .. (防止路径穿越)")
    if Path(normalized).is_absolute():
        raise ValueError("file_path 必须是相对路径(相对仓库根)")
    abs_path = f"{repo_path.rstrip('/')}/{normalized}"
    if mode == "local":
        # 复核:解析后不得逃出仓库根(同 _read_file_local)
        if not Path(abs_path).resolve().is_relative_to(Path(repo_path).resolve()):
            raise ValueError("非法路径:不能超出仓库根目录")
    return abs_path


def str_replace_editor(
    command: str,
    repo_path: str,
    file_path: str,
    file_text: str = "",
    old_str: str = "",
    new_str: str = "",
    insert_line: int = 0,
    replace_all: bool = False,
    task_id: str = "",
) -> dict:
    """对仓库文件做外科手术式编辑(对齐 CLI 的 str_replace_editor)

    与 write_file(全量覆写工作区)互补:本工具就地编辑仓库代码,精准、省 token、
    不需重写整文件。可逆性由完整克隆+git 保证(git diff 回看、git checkout 回退)。

    command:
        - create: 创建新文件(file_text 为完整内容);文件必须不存在
        - str_replace: 精确替换(old_str 必须唯一匹配,或 replace_all=True 全换)
        - insert: 在 insert_line 行之后插入 new_str(0=末尾追加)

    返回:{"command": str, "path": str, "abs_path": str, "lines": int, "snippet": str}
      snippet 为编辑后该区域带行号的预览(便于 agent 确认结果)
    """
    if command not in ("create", "str_replace", "insert"):
        raise ValueError(f"command 必须是 create/str_replace/insert,收到: {command}")
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    abs_path = _resolve_repo_file(repo_path, file_path, mode)

    # ---- 读写原语(双模式)----
    def _exists() -> bool:
        if mode == "local":
            return Path(abs_path).is_file()
        session: SandboxSession = ctx["session"]
        return "OK" in session.run_command(
            f"test -f {shlex.quote(abs_path)} && echo OK || echo MISSING"
        )

    def _read() -> str:
        if mode == "local":
            return Path(abs_path).read_text(encoding="utf-8")
        session: SandboxSession = ctx["session"]
        return session.read_file(abs_path)

    def _mkdir_parent() -> None:
        if mode == "local":
            Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        else:
            # sandbox:用字符串 rsplit 保 Linux 分隔符(避免 Windows Path 把 / 转 \)
            parent = abs_path.rsplit("/", 1)[0]
            session: SandboxSession = ctx["session"]
            session.run_command(f"mkdir -p {shlex.quote(parent)}")

    def _write(content: str) -> None:
        if mode == "local":
            check_local_write_permission(Path(abs_path).resolve(), Path(repo_path), file_path)
            Path(abs_path).write_text(content, encoding="utf-8")
        else:
            session: SandboxSession = ctx["session"]
            session.write_file(abs_path, content)

    # ---- 三命令逻辑(read-modify-write)----
    if command == "create":
        if not file_text:
            raise ValueError("create 需要 file_text(新文件完整内容)")
        if _exists():
            raise FileExistsError(f"文件已存在,create 拒绝覆盖: {file_path}")
        _mkdir_parent()
        _write(file_text)
        new_content = file_text
        anchor_line = 1

    elif command == "str_replace":
        if not old_str:
            raise ValueError("str_replace 需要 old_str(被替换的精确字符串)")
        if old_str == new_str:
            raise ValueError("old_str 与 new_str 相同,无需替换")
        if not _exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        content = _read()
        occurrences = content.count(old_str)
        if occurrences == 0:
            raise ValueError(f"old_str 在文件中未找到,请先用 read_file 核对内容: {file_path}")
        if occurrences > 1 and not replace_all:
            raise ValueError(
                f"old_str 匹配 {occurrences} 处,需提供更长上下文以唯一匹配,或设 replace_all=True"
            )
        new_content = content.replace(old_str, new_str) if replace_all else content.replace(old_str, new_str, 1)
        _write(new_content)
        # snippet 锚点:首个替换处附近(new_str 为空即删除,锚点取文件头)
        anchor_line = new_content[: new_content.find(new_str)].count("\n") + 1 if new_str else 1

    else:  # insert
        if not new_str:
            raise ValueError("insert 需要 new_str(要插入的文本)")
        if not _exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        content = _read()
        lines = content.splitlines(keepends=True)
        total = len(lines)
        if insert_line < 0:
            raise ValueError("insert_line 不能为负(0=末尾追加,正数=在该行之后插入)")
        # clamp:0 或 > total 都按末尾追加
        pos = insert_line if 0 < insert_line <= total else total
        chunk = new_str if new_str.endswith("\n") else new_str + "\n"
        lines.insert(pos, chunk)
        new_content = "".join(lines)
        _write(new_content)
        anchor_line = pos + 1

    # ---- snippet:编辑区域带行号预览 ----
    all_lines = new_content.splitlines()
    total_lines = len(all_lines)
    sn_start = max(1, anchor_line - 10)
    sn_end = min(total_lines, anchor_line + 10)
    snippet_lines = all_lines[sn_start - 1 : sn_end]
    snippet = _format_numbered_lines(snippet_lines, sn_start)
    if total_lines > sn_end:
        snippet += f"\n...(共 {total_lines} 行,已显示 {sn_start}-{sn_end})"

    return {
        "command": command,
        "path": file_path,
        "abs_path": abs_path,
        "lines": total_lines,
        "snippet": snippet,
    }


# ============================================================
# 辅助:克隆凭证清理(工作区里不留 token)
# ============================================================

# git 会把"这次从哪个 URL 取的数据"原样记进工作区:
# - `.git/config` 的 remote.origin.url(长期生效)
# - `.git/FETCH_HEAD` 的 "… of <url>"(上次 fetch 的账本)
# - `.git/logs/` 下的 reflog:"clone: from <url>" —— clone 同时写 HEAD 与被检出的
#   分支 ref(`logs/refs/heads/main`),只清 logs/HEAD 会漏掉分支那份
# 私有仓库走的是 `https://{user}:{token}@host/...`,于是用户授权的 OAuth token
# 会长期躺在可预览、可下载的工作区里。与 bare 缓存同一套纪律(repo_cache 建立缓存后
# 立刻 set-url 并校验 config 不含 token):克隆成功后把 URL 里的凭证抹掉。
_GIT_URL_RECORD_FILES = (".git/config", ".git/FETCH_HEAD")
_GIT_LOG_DIR = ".git/logs"

# 只剥 http(s) URL 的 userinfo:SSH 形态的 git@host:path 不是凭证注入,不该被误伤
_URL_USERINFO_RE = re.compile(r"(\bhttps?://)[^/\s@]+@", re.IGNORECASE)

# 上面那条正则在沙箱侧的两个等价写法(集中一份,避免"洗的口径"与"复查的口径"分叉):
# - sed 用 @ 作分隔符(URL 里有 /),只替换 userinfo,不动主机与路径
# - grep -E 用于复查残留
_SANDBOX_STRIP_USERINFO_SED = "s@(https?://)[^[:space:]/@]+@\\1@"
_URL_USERINFO_GREP = "https?://[^[:space:]/@]+@"


def _looks_credentialed(url: str) -> bool:
    """URL 是否在 userinfo 里带了凭证(HTTPS+token 形态)"""
    return bool(_URL_USERINFO_RE.search(url or ""))


def _scrub_url_userinfo(text: str) -> str:
    """抹掉文本里所有 http(s) URL 的 userinfo

    按模式匹配而非替换 token 字面值:既不需要把 token 传进 shell 命令(那会让它
    出现在命令行与 execd 记录的命令里,等于换个泄漏面),也能覆盖 config/FETCH_HEAD/
    reflog 各处不同写法。
    """
    return _URL_USERINFO_RE.sub(r"\1", text)


def _credential_record_files(repo_path: str) -> list[Path]:
    """local 模式:列出 git 记过克隆 URL 的既存普通文件(config/FETCH_HEAD/全部 reflog)"""
    root = Path(repo_path)
    files = [root / rel for rel in _GIT_URL_RECORD_FILES]
    log_dir = root / _GIT_LOG_DIR
    if log_dir.is_dir():
        files.extend(p for p in sorted(log_dir.rglob("*")) if p.is_file())
    return [p for p in files if p.is_file()]


def _scrub_clone_credentials(
    ctx: dict, repo_path: str, used_url: str, anon_url: str, task_id: str = "",
) -> None:
    """克隆成功后把带 token 的 URL 从工作区里抹掉

    步骤:① `git remote set-url origin <匿名 URL>`(今后 remote 指向无凭证形态)
    ② 清洗 git 记过 URL 的文本文件(config / FETCH_HEAD / logs/ 下全部 reflog)
    ③ 复查仍含 userinfo 则记 error —— 但**不推翻克隆结果**(工作区已就绪是主目标,
    失败面只在这一处,且匿名化后 agent 也拿不到凭证原文)

    副作用(有意为之):清洗后工作区不再具备联网凭证,agent 在里面跑 `git fetch/pull`
    对私有仓库会匿名失败。审计快照本就不需要写回远端,而把 token 留在盘上才是真风险。
    """
    if not _looks_credentialed(used_url):
        return  # 匿名 HTTPS / SSH 克隆:git 记的 URL 本就不含凭证
    session: SandboxSession = ctx["session"]
    repo = str(repo_path)
    try:
        if ctx.get("mode") == "local":
            # 不经 shell:Windows cmd.exe 没有 POSIX 引号规则,argv 才可靠
            session.run_command_argv(
                ["git", "-C", repo, "remote", "set-url", "origin", anon_url],
                timeout=30,
            )
            residual = []
            for f in _credential_record_files(repo):
                text = f.read_text(encoding="utf-8", errors="replace")
                cleaned = _scrub_url_userinfo(text)
                if cleaned != text:
                    # newline="" 关翻译:Windows 上默认会把 \n 换成 \r\n,而 git
                    # 的 config/FETCH_HEAD/reflog 是 LF 文件,翻一次行尾就等于整文件
                    # 变更,下游工作区 diff 会误报"每行都改过"
                    f.write_text(cleaned, encoding="utf-8", newline="")
                # 回读复查:写失败/只读属性(Windows 的 git pack 文件)都在这暴露
                if _looks_credentialed(f.read_text(encoding="utf-8", errors="replace")):
                    residual.append(str(f))
        else:
            q = shlex.quote(repo)
            # 固定两处 + logs 目录整树:reflog 除 logs/HEAD 还有分支那份
            # (logs/refs/heads/main),用 find -exec 枚举 —— 它按 NUL 分组传参,
            # 不像 $(find …) 那样被路径里的空格拆坏
            plain = " ".join(f"{q}/{rel}" for rel in _GIT_URL_RECORD_FILES)
            log_dir = f"{q}/{_GIT_LOG_DIR}"
            scrub = (
                f"for p in {plain}; do [ -f \"$p\" ] && "
                f"sed -i -E '{_SANDBOX_STRIP_USERINFO_SED}' \"$p\"; done; "
                f"[ -d {log_dir} ] && find {log_dir} -type f "
                f"-exec sed -i -E '{_SANDBOX_STRIP_USERINFO_SED}' {{}} +; true"
            )
            scan = (
                f"for p in {plain}; do [ -f \"$p\" ] && "
                f"grep -El '{_URL_USERINFO_GREP}' \"$p\"; done; "
                f"[ -d {log_dir} ] && find {log_dir} -type f "
                f"-exec grep -El '{_URL_USERINFO_GREP}' {{}} +; true"
            )
            session.run_command(
                f"git -C {q} remote set-url origin {shlex.quote(anon_url)}; {scrub}",
                timeout=30,
            )
            residual = [ln.strip() for ln in session.run_command(scan).splitlines() if ln.strip()]
        if residual:
            logger.error(
                f"[task={task_id}] 克隆凭证清理未彻底,仍含 URL 凭证: {residual}"
            )
        else:
            logger.info(f"[task={task_id}] 已清除工作区内的克隆凭证 URL 记录")
    except Exception as e:
        # 清理失败不推翻克隆:工作区可用是主目标,但要留下可排查的显式记录
        logger.error(
            f"[task={task_id}] 克隆凭证清理失败(工作区可能仍留有 token URL): {str(e)[:200]}"
        )


# ============================================================
# 辅助:URL 转换(委托给 git_provider 抽象,按主机识别平台)
# ============================================================


def clone_repo_with_fallback(
    repo_url: str, branch: str | None = None, task_id: str = "",
    git_tokens: dict | None = None, cancellable: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict:
    """克隆仓库(同任务串行的对外入口,实际回退流程见 _clone_repo_fallback)

    同一 task_id 的并发克隆会各自看到 repo_path 为空,并向同一个会话目录动手
    (预克隆与降级后的 clone_repo、工作区恢复路由、出题前工作区恢复之间都可能
    撞上)—— 后者必报 git 的 "already exists",把先一个调用的真实结果淹没在
    级联错误里。按 task_id 加可重入锁,把它们排成"一次真克隆 + 一次幂等复用"。
    """
    # task_id 为空(脱离任务的调用)没有共享会话目录可争,不必排队
    lock = _get_clone_lock(task_id) if task_id else nullcontext()
    with lock:
        return _clone_repo_fallback(
            repo_url, branch=branch, task_id=task_id, git_tokens=git_tokens,
            cancellable=cancellable, progress_callback=progress_callback,
        )


def _clone_repo_fallback(
    repo_url: str, branch: str | None = None, task_id: str = "",
    git_tokens: dict | None = None, cancellable: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict:
    """克隆仓库(协议回退:HTTPS+token → SSH → HTTPS 匿名)

    供 orchestrator 在 agent2 评估前主动调用,也供 clone_repo 工具委托。

    cancellable=True 时(仅 orchestrator 预克隆路径),每次尝试前/轮询中
    检查跳过标志,用户请求跳过预克隆时抛 CloneSkippedError 终止整个回退链
    (不会继续尝试下一种协议);LLM 工具路径恒为 False,不受影响。

    按 repo_url 主机识别 provider(github / gitee / 未知),取该 provider 的
    access_token(git_tokens[provider.id])做 HTTPS 注入;未知主机无 token,
    走 SSH / 匿名 HTTPS。

    回退链(按顺序尝试,首个成功即返回):
    1. HTTPS + token(该 provider 有 token 时,可访问私有仓库)
    2. SSH(依赖宿主机/沙箱的 SSH key 配置,适合公开仓库)
    3. HTTPS 匿名(无 token,仅公开仓库)

    分支回退:指定 branch 时先带 --branch 跑完整回退链;若全部失败,
    再不带分支重跑一遍(远端默认分支兜底)。分支错误与协议无关,
    协议回退救不了,常见于前端自动填充的 default_branch 与远端不符
    (如空仓库 default_branch 为 null 被兜底成 main/master)。

    所有组合都失败才抛 RuntimeError。

    复用同一套 session 管理(_get_or_create_session + _set_repo_path),
    所以 clone 完成后 react_agent / workspace 路由可直接通过 task_id 复用会话。

    progress_callback(percent, message):可选直连进度回调,透传给
    _clone_repo_local/_clone_repo_sandbox(任务结束后的调用方 event_bus
    已 finish,clone_progress 事件会被丢弃,只能走此回调拿实时进度)。
    """
    git_tokens = git_tokens or {}
    provider = get_provider_for_url(repo_url)
    # 该 provider 的 token(未知主机则为空)
    token = git_tokens.get(provider.id, "") if provider else ""

    # 构造候选 URL:HTTPS+token、SSH、HTTPS 匿名(去重)
    if provider:
        https_anon = provider.to_https_url(repo_url)
        ssh_url = provider.to_ssh_url(repo_url)
        https_with_token = provider.inject_token_in_https(https_anon, token) if token else ""
    else:
        # 未知主机:原样当作 HTTPS,只试匿名 + SSH(若已是 git@ 形式)
        https_anon = repo_url
        ssh_url = repo_url if repo_url.startswith("git@") else repo_url
        https_with_token = ""

    candidates: list[str] = []
    for u in [https_with_token, ssh_url, https_anon]:
        if u and u not in candidates:
            candidates.append(u)

    # 从 URL 提取仓库名(两种格式都支持)
    match = re.search(r"/([^/]+?)(?:\.git)?$", repo_url)
    if not match:
        raise ValueError(f"无法从 URL 解析仓库名: {repo_url}")
    repo_name = match.group(1)

    # 传 repo_url/branch:新建会话时(sandbox 模式)预建缓存并挂载;已有会话不受影响
    ctx = _get_or_create_session(
        task_id, repo_url=repo_url, branch=branch, git_tokens=git_tokens
    )
    mode = ctx["mode"]

    # 幂等复用:本会话已 clone 过同一来源 → 直接返回既有工作区(不动磁盘)。
    # 锁内判定,并发排队的第二个调用正好命中这里
    reused = _reuse_existing_clone(ctx, repo_url, branch)
    if reused is not None:
        return reused

    # ---- bare 仓库缓存快路径(任何失败落入下方原远程候选链,缓存永不阻塞任务) ----
    if mode == "local" and settings.REPO_CACHE_ENABLED:
        try:
            bare_dir = ensure_bare_cache(
                repo_url, branch=branch, git_tokens=git_tokens, task_id=task_id
            )
        except Exception as e:
            # 防御:ensure 内部已兜底,此处再包一层,缓存异常绝不阻塞克隆
            bare_dir = None
            logger.warning(
                f"[clone_fallback] task={task_id} bare 缓存准备异常,"
                f"降级远程克隆: {str(e)[:200]}"
            )
        if bare_dir:
            try:
                logger.info(
                    f"[clone_fallback] task={task_id} 命中 bare 缓存,本地克隆: {bare_dir}"
                )
                result = _clone_repo_local(
                    ctx, str(bare_dir), repo_name, branch, task_id=task_id,
                    cancellable=cancellable, progress_callback=progress_callback,
                    use_depth=False,
                )
                _set_repo_path(task_id, result["path"])
                _record_clone_source(ctx, repo_url, branch, result["path"])
                logger.info(f"[clone_fallback] task={task_id} 缓存本地克隆成功")
                return result
            except CloneSkippedError:
                # 用户主动跳过:向上传播降级(半成品目录已由 _clone_repo_local 清掉)
                raise
            except Exception as e:
                logger.warning(
                    f"[clone_fallback] task={task_id} 缓存本地克隆失败,"
                    f"降级远程克隆: {str(e)[:200]}"
                )
    elif mode == "sandbox" and ctx.get("cache_key") == repo_cache_key(repo_url):
        # 会话创建时已把本任务仓库的 bare 缓存只读挂载进容器 → 容器内本地克隆
        try:
            logger.info(
                f"[clone_fallback] task={task_id} 命中挂载的 bare 缓存,"
                f"容器内本地克隆: {ctx['cache_mount']}"
            )
            result = _clone_repo_sandbox(
                ctx, ctx["cache_mount"], repo_name, branch, task_id=task_id,
                cancellable=cancellable, progress_callback=progress_callback,
                use_depth=False,
            )
            _set_repo_path(task_id, result["path"])
            _record_clone_source(ctx, repo_url, branch, result["path"])
            logger.info(f"[clone_fallback] task={task_id} 缓存容器内克隆成功")
            return result
        except CloneSkippedError:
            # 用户主动跳过:向上传播(沙箱内的半成品目录已由 _clone_repo_sandbox 清掉)
            raise
        except SandboxGoneError:
            # 容器被回收:继续走远程链只会在同一个死会话上再失败 N 次(每次含克隆
            # 超时,分钟级),不如把干净的过期原因交给调用方(前端可重新克隆)
            raise
        except Exception as e:
            logger.warning(
                f"[clone_fallback] task={task_id} 缓存容器内克隆失败,"
                f"降级远程克隆: {str(e)[:200]}"
            )
    elif mode == "sandbox":
        # LLM 运行中克隆其他仓库(容器无法追加挂载)或缓存未开 → 走原远程链
        logger.info(
            f"[clone_fallback] task={task_id} 会话未挂载该仓库的 bare 缓存,走远程克隆"
        )

    errors: list[str] = []
    # 分支尝试顺序:指定了 branch 先带 --branch,全失败后不带分支再跑一遍
    branch_attempts: list[str | None] = [branch, None] if branch else [None]
    for attempt_idx, attempt_branch in enumerate(branch_attempts):
        if attempt_idx > 0:
            logger.warning(
                f"[clone_fallback] task={task_id} 带 branch={branch} 全部协议失败,"
                f"回退为不带分支重试(用远端默认分支)"
            )
        for idx, url in enumerate(candidates):
            # 跳过检查点(尝试前):已请求跳过则立即终止整个回退链,
            # 不再启动下一种协议(协议间间隙可能持续数十秒,轮询内
            # 检查点覆盖不到)
            if cancellable and consume_skip_clone(task_id):
                raise CloneSkippedError(f"用户已跳过预克隆: {repo_name}")
            # 日志里不打印 token(脱敏)
            safe_url = url.split("@")[-1] if "@" in url else url
            try:
                logger.info(
                    f"[clone_fallback] task={task_id} 尝试第 {idx + 1} 种协议"
                    f"(branch={attempt_branch}): {safe_url}"
                )
                if mode == "local":
                    result = _clone_repo_local(
                        ctx, url, repo_name, attempt_branch, task_id=task_id,
                        cancellable=cancellable, progress_callback=progress_callback,
                    )
                else:
                    result = _clone_repo_sandbox(
                        ctx, url, repo_name, attempt_branch, task_id=task_id,
                        cancellable=cancellable, progress_callback=progress_callback,
                    )
                _set_repo_path(task_id, result["path"])
                _record_clone_source(ctx, repo_url, attempt_branch, result["path"])
                # token 不落盘:带凭证的 HTTPS 克隆会把 URL 写进 .git/config、
                # FETCH_HEAD 与初始 reflog,克隆成功后一律抹掉
                _scrub_clone_credentials(
                    ctx, result["path"], url, https_anon, task_id=task_id
                )
                logger.info(f"[clone_fallback] task={task_id} 克隆成功(协议 {safe_url})")
                return result
            except CloneSkippedError:
                # 用户主动跳过:直接向上传播,不进协议回退/错误聚合
                # (本次克隆落在哪个目录只有 _clone_repo_local 知道,半成品已由它清理;
                # 这里不能再按仓名猜目录去删,否则会抹掉避让保护的外来工作区)
                raise
            except SandboxGoneError:
                # 容器已回收:协议回退救不了它(每一种都打同一个不存在的实例),
                # 反而把真正原因埋进"已尝试 N 种协议"的聚合错误里
                raise
            except Exception as e:
                err_msg = str(e)[:300]
                errors.append(f"[{safe_url}] {err_msg}")
                logger.warning(
                    f"[clone_fallback] task={task_id} 协议 {safe_url} 克隆失败: {err_msg}"
                )

    raise RuntimeError(
        f"仓库克隆失败(已尝试 {len(candidates)} 种协议 x {len(branch_attempts)} 种分支策略):\n"
        + "\n".join(errors)
    )


# ============================================================
# 工具 5:run_semgrep(阶段 3)
# ============================================================


def run_semgrep(
    repo_path: str,
    config: str = "auto",
    task_id: str = "",
) -> dict:
    """运行 Semgrep 静态分析

    参数:
        repo_path: clone_repo 返回的 path
        config: semgrep 配置,默认 "auto"(自动选规则集)。
                也可指定 "p/python"、"p/javascript" 等

    返回:{
        "findings": [
            {
                "rule_id": "python.lang.security...",
                "severity": "HIGH",
                "file": "src/main.py",
                "line": 42,
                "message": "..."
            },
            ...
        ],
        "total": int,
        "truncated": bool
    }

    local 模式:若宿主机已安装 semgrep(shutil.which 检测到)则直接本地执行;
              否则返回提示让 LLM 知道本工具不可用(可用 pip install semgrep 安装)
    sandbox 模式:在沙箱里执行 semgrep(未安装时自动 pip 安装)
    """
    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]

    if mode == "local":
        return _run_semgrep_local(repo_path, config)

    return _run_semgrep_sandbox(ctx, repo_path, config)


def _parse_semgrep_json(output: str, repo_path: str) -> dict:
    """解析 semgrep --json 的 stdout,提取 findings(供 local / sandbox 共用)"""
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        return {
            "findings": [],
            "total": 0,
            "truncated": False,
            "error": f"semgrep 输出解析失败: {e}",
        }

    results = data.get("results", [])
    findings = []
    for r in results[:100]:  # 限制最多 100 个,防超长
        # 提取信息
        path = r.get("path", "")
        # 去掉 repo_path 前缀
        if path.startswith(repo_path):
            path = path[len(repo_path):].lstrip("/")

        findings.append({
            "rule_id": r.get("check_id", ""),
            "severity": _map_semgrep_severity(r.get("extra", {}).get("severity", "")),
            "file": path,
            "line": r.get("start", {}).get("line", 0),
            "message": r.get("extra", {}).get("message", "")[:200],
        })

    total = len(findings)
    return {
        "findings": findings,
        "total": total,
        "truncated": total >= 100,
    }


def _run_semgrep_local(repo_path: str, config: str) -> dict:
    """local 模式:检测宿主机 semgrep,有则本地执行,无则返回不可用提示"""
    semgrep_bin = shutil.which("semgrep")
    if not semgrep_bin:
        return {
            "findings": [],
            "total": 0,
            "truncated": False,
            "note": (
                "local 模式未检测到 semgrep。可执行 `pip install semgrep` 安装后重试,"
                "或通过 search_code + read_file 进行手动 SAST 检查,"
                "或切换 SANDBOX_MODE=sandbox(沙箱会自动安装 semgrep)。"
            ),
        }

    cmd = [semgrep_bin, "--json", "--quiet", "--config", config, repo_path]
    logger.info(f"[local] semgrep: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {
            "findings": [],
            "total": 0,
            "truncated": False,
            "error": "semgrep 本地执行超时(300s)",
        }
    if result.returncode not in (0, 1):
        # semgrep 退出码 0=无发现/1=有发现/其他=报错
        return {
            "findings": [],
            "total": 0,
            "truncated": False,
            "error": f"semgrep 执行失败(退出码 {result.returncode}): {result.stderr[:300]}",
        }
    return _parse_semgrep_json(result.stdout, repo_path)


# Ubuntu 24.04 系统 Python 是 PEP 668 externally-managed,直接 pip install 会被拒;
# 沙箱以非 root user 运行,用 --user 装到 ~/.local/bin,--break-system-packages 绕过 PEP 668
# (沙箱是一次性环境,污染系统包的风险可接受)
_SEMGREP_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$PATH"; '


def _run_semgrep_sandbox(ctx: dict, repo_path: str, config: str) -> dict:
    """sandbox 模式:在沙箱里运行 semgrep"""
    session: SandboxSession = ctx["session"]

    # 先检查 semgrep 是否已安装(带 ~/.local/bin,兜底 --user 安装的场景)
    check = session.run_command(_SEMGREP_PATH_PREFIX + "command -v semgrep || echo MISSING")
    if "MISSING" in check:
        # 尝试 pip 安装(semgrep wheel 几十 MB,国内直连 PyPI 慢,超时给足)
        logger.info("[sandbox] semgrep 未安装,尝试 pip install semgrep")
        install_result = session.run_command(
            "pip install --user --break-system-packages semgrep 2>&1 | tail -5",
            timeout=300,
        )
        logger.info(f"[sandbox] semgrep 安装输出: {install_result.strip()[-300:]}")
        # 再次检查
        check2 = session.run_command(_SEMGREP_PATH_PREFIX + "command -v semgrep || echo MISSING")
        if "MISSING" in check2:
            return {
                "findings": [],
                "total": 0,
                "truncated": False,
                "error": (
                    "semgrep 安装失败,请检查沙箱镜像或手动安装。"
                    f"安装输出: {install_result.strip()[-300:]}"
                ),
            }

    # 运行 semgrep,输出 JSON
    # --json 输出到 stdout
    # --quiet 只输出结果,不输出 banner
    # --config auto 自动选规则
    cmd = (
        _SEMGREP_PATH_PREFIX
        + f"semgrep --json --quiet --config {shlex.quote(config)} {shlex.quote(repo_path)}"
    )
    logger.info(f"[sandbox] semgrep: {cmd}")
    output = session.run_command(cmd, timeout=300)  # semgrep 可能慢,5 分钟超时

    return _parse_semgrep_json(output, repo_path)


def _map_semgrep_severity(sev: str) -> str:
    """把 semgrep 的 severity 映射到统一格式"""
    mapping = {
        "ERROR": "HIGH",
        "WARNING": "MEDIUM",
        "INFO": "LOW",
    }
    return mapping.get(sev.upper(), sev.upper())
