"""ACP CLI 运行时预热:把冷启动链路挪进预克隆窗口

背景(实测,见 docs/agent-architecture.md §4.3 与 .trae/documents/CLI冷启动提速实施计划.md):
外部 CLI 执行器从 `acp_enter` 到 `acp_prompt_send` 的握手链路 median ≈12.5s(max 20.4s),
全程串行,而它前面的预克隆要 24~235s —— 那段等待完全可以被利用:

    主线程:  建会话 → 写记忆/skill → ───── clone(24~235s) ───── → session/new → prompt
    预热线:                                └ ensure_cli_env → 起 bridge → 等就绪 → initialize ┘

只有 `session/new` 留在克隆之后:它的 cwd 必须是已存在的仓库目录,且 cwd 口径决定
CLI 自己落盘的会话能否被恢复(见 acp_base 的 cwd 一致性注释)。

三条硬约束(改动前先读):
1. **凭证在 bridge spawn 时经 env 绑定**,所以预热的 `credential_envs` / `acp_args`
   必须与 `run_acp_agent` 稍后算出的完全一致 —— 两边都走 `_get_acp_args` +
   `_bridge_fingerprint` 同一实现,不一致时 consume 直接丢弃回落,只损失时间不损失正确性。
2. **预热线程不碰 ORM Session**(SQLAlchemy Session 非线程安全)。需要 db 的部分
   (如 git token 解密)由主线程先做完,把结果 dict 递进来。
3. **预热失败必须静默回落**:任何异常都只丢 handle + 停 bridge,绝不把任务 fail 掉。

跨线程并发打同一个沙箱会话的前提由 `backend/scripts/smoke_sandbox_concurrency.py`
实测(sandbox 模式:命令层可并发、输出不串话、单次往返单价不劣化),因此这里
连 `_ensure_cli_env` 也放进了预热线程;`should_prewarm` 仍只在 sandbox 模式放行。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.agents import acp_base
from app.config import settings
from app.perf import perf_log

logger = logging.getLogger(__name__)

# task_id -> 已预热但未被认领的 handle
# 与 acp_base._bridge_cache 分开:那张表记"任务正在用的 bridge",这里记"还没人拿的热运行态"
_handles: dict[str, "PrewarmHandle"] = {}
_handles_lock = threading.Lock()


@dataclass
class PrewarmHandle:
    """一次预热的产物(被 consume 取走后由调用方接管 bridge 生命周期)"""

    task_id: str
    agent_type: str
    fingerprint: str
    session: Any
    bridge_exec_id: str = ""
    bridge_port: int = 0
    endpoint_url: str = ""
    endpoint_headers: dict[str, str] = field(default_factory=dict)
    init_result: dict[str, Any] = field(default_factory=dict)
    bridge_protocol: int = 1
    cli_env_ready: bool = False
    git_cred_done: bool = False
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def wait(self, secs: float) -> bool:
        """等预热收工(含失败);返回是否在预算内结束"""
        return self.done.wait(timeout=max(0.0, secs))


def should_prewarm(agent_type: str) -> bool:
    """该 agent 是否走预热(总开关 + 运行模式 + 类型白名单)

    - 只在 sandbox 模式放行:命令层跨线程并发的前提只在真实沙箱实测过,
      local 模式(Windows 宿主机 Popen + 后台进程表)未验证,按原串行执行。
    - 白名单只放"无 wrapper 钩子"的 agent:deepseek 的 credential_env_builder 会让
      envs 与预热期不一致(靠指纹自动作废),codex 的 pre_bridge_hook 要写
      ~/.codex/config.toml,两者都还没纳入预热链路,先别放进来。
    """
    if not settings.ACP_PREWARM_ENABLED:
        return False
    if (settings.SANDBOX_MODE or "").lower() != "sandbox":
        return False
    allowed = {
        a.strip().lower()
        for a in (settings.ACP_PREWARM_AGENTS or "").split(",")
        if a.strip()
    }
    return agent_type in allowed


def _stop_bridge_quietly(handle: PrewarmHandle, reason: str) -> None:
    """停掉预热起来的 bridge(尽力而为):没被认领 / 出错 / 指纹不符时都不能留进程"""
    if not handle.bridge_exec_id:
        return
    try:
        acp_base._stop_acp_bridge(handle.session, handle.bridge_exec_id, handle.agent_type)
        logger.info(
            f"[task={handle.task_id}] 预热 bridge 已停止(reason={reason}): "
            f"{handle.bridge_exec_id}"
        )
    except Exception as e:
        # 停不掉也不能往上抛:沙箱会话销毁时容器连带回收,最坏留一个死进程
        logger.warning(f"[task={handle.task_id}] 停止预热 bridge 失败(忽略): {e}")


def _run_prewarm(
    handle: PrewarmHandle,
    credential_envs: dict[str, str],
    git_tokens: dict[str, str],
    acp_args: list[str],
    ctx: dict[str, Any],
) -> None:
    """预热线程主体:环境准备 → (git 凭证) → 起 bridge → 等就绪 → initialize"""
    t0 = time.perf_counter()
    try:
        # 1) CLI 运行环境(建脚本目录 + 检查 CLI + 写 bridge 脚本);
        #    ctx 用任务真正的那份,记下的就绪标记能让后续链路也跳过
        acp_base._ensure_cli_env(handle.session, handle.agent_type, ctx)
        handle.cli_env_ready = True

        # 2) git token 注入 credential cache(token 已在主线程解密好,这里不碰 db)
        if git_tokens:
            acp_base._inject_git_tokens_to_cache(handle.session, handle.task_id, git_tokens)
            handle.git_cred_done = True

        # 3) 起 bridge(后台命令,几乎不耗时);启动参数由主线程算好传入
        handle.bridge_exec_id, handle.bridge_port = acp_base._start_acp_bridge(
            handle.session, credential_envs, task=None, agent_type=handle.agent_type,
            acp_args=acp_args,
        )

        # 4) 端口转发 + 等 HTTP 就绪
        handle.endpoint_url, handle.endpoint_headers = handle.session.get_endpoint(
            handle.bridge_port
        )
        bridge_health = acp_base._wait_for_bridge_ready(
            handle.session, handle.bridge_exec_id,
            handle.endpoint_url, handle.endpoint_headers, handle.agent_type,
        )
        try:
            handle.bridge_protocol = int(bridge_health.get("bridge_protocol") or 1)
        except (TypeError, ValueError):
            handle.bridge_protocol = 1

        # 5) ACP 握手(能力协商);session/new 留给消费方(要等仓库就位)
        client = acp_base.ACPClient(
            handle.endpoint_url, handle.endpoint_headers,
        )
        try:
            handle.init_result = client.initialize()
        finally:
            client.close()

        perf_log(
            handle.task_id, "acp_prewarm_ready", time.perf_counter() - t0,
            agent_type=handle.agent_type, ok=True,
            protocol=handle.bridge_protocol, git_cred=handle.git_cred_done,
        )
    except Exception as e:
        handle.error = f"{type(e).__name__}: {e}"
        perf_log(
            handle.task_id, "acp_prewarm_ready", time.perf_counter() - t0,
            agent_type=handle.agent_type, ok=False,
        )
        logger.warning(
            f"[task={handle.task_id}] CLI 预热失败(回落全串行链路,不影响任务): "
            f"{handle.error}"
        )
        _stop_bridge_quietly(handle, "prewarm_error")
        # 半截状态不能半途交出:清掉所有产物,消费方按"没有预热"处理
        handle.bridge_exec_id = ""
        handle.endpoint_url = ""
        handle.endpoint_headers = {}
        handle.init_result = {}
        handle.cli_env_ready = False
        handle.git_cred_done = False
    finally:
        handle.done.set()


def prewarm_agent_runtime(
    task_id: str,
    session,
    ctx: dict[str, Any],
    agent_type: str,
    fingerprint: str,
    acp_args: list[str],
    credential_envs: dict[str, str],
    git_tokens: dict[str, str] | None = None,
) -> PrewarmHandle | None:
    """发起一次预热(幂等:同一 task 已在预热就返回既有 handle)

    返回 None 表示该 handle 已失败(调用方照常走全串行链路)。
    acp_args / credential_envs / fingerprint 必须与 run_acp_agent 同口径(由调用方
    用主线程的 db+task 算好),不一致时 consume 会丢弃并回落。
    """
    task_id = str(task_id)
    with _handles_lock:
        existing = _handles.get(task_id)
        if existing is not None:
            # single-flight:重复触发不重复起 bridge(端口冲突 + 白烧容器 TTL)
            if existing.error is None:
                return existing
            _handles.pop(task_id, None)
        handle = PrewarmHandle(
            task_id=task_id, agent_type=agent_type,
            fingerprint=fingerprint, session=session,
        )
        _handles[task_id] = handle

    perf_log(task_id, "acp_prewarm_start", agent_type=agent_type)
    threading.Thread(
        target=_run_prewarm,
        args=(handle, credential_envs, git_tokens or {}, list(acp_args), ctx),
        daemon=True,
        name=f"acp-prewarm-{task_id[:8]}",
    ).start()
    return handle


def consume_prewarm(
    task_id: str, agent_type: str, fingerprint: str, task: Any = None,
) -> PrewarmHandle | None:
    """认领预热成果(一次性);不可用则丢弃并返回 None,调用方走全串行

    预算 = ACP_PREWARM_TIMEOUT_SECONDS 减去 handle 已经跑的秒数 —— 不允许与
    `BRIDGE_STARTUP_TIMEOUT` 叠加成两倍等待。
    """
    task_id = str(task_id)
    with _handles_lock:
        handle = _handles.pop(task_id, None)

    if handle is None:
        return None

    reason = ""
    got = handle
    if handle.agent_type != agent_type or handle.fingerprint != fingerprint:
        reason = "fingerprint"
    else:
        budget = max(0.0, settings.ACP_PREWARM_TIMEOUT_SECONDS - handle.elapsed)
        if not handle.wait(budget):
            reason = "timeout"
        elif handle.error is not None or not handle.init_result or not handle.bridge_exec_id:
            reason = "error"
        else:
            got = handle

    if reason:
        _discard(handle, reason)
        perf_log(task_id, "acp_prewarm_hit", agent_type=agent_type, hit=False, reason=reason)
        return None

    perf_log(
        task_id, "acp_prewarm_hit", agent_type=agent_type, hit=True,
        saved=round(handle.elapsed, 2), git_cred=handle.git_cred_done,
    )
    return got


def _discard(handle: PrewarmHandle, reason: str) -> None:
    """丢弃 handle:停 bridge + 清注册表(任务被停/克隆失败/指纹不符都走这里)"""
    with _handles_lock:
        _handles.pop(handle.task_id, None)
    _stop_bridge_quietly(handle, reason)
    perf_log(handle.task_id, "acp_prewarm_discard", agent_type=handle.agent_type,
             reason=reason)


def discard_task_prewarm(task_id: str, reason: str = "abandoned") -> None:
    """丢弃某任务的预热(克隆失败/跳过/取消、任务删除等路径调用)

    幂等:没有 handle 时静默返回。预热线程仍在跑也会被停掉 —— handle 被摘出注册表后
    线程收尾时找不到归属,bridge 由这里一并中断,不会留下空转进程。
    """
    task_id = str(task_id)
    with _handles_lock:
        handle = _handles.pop(task_id, None)
    if handle is None:
        return
    _stop_bridge_quietly(handle, reason)
    perf_log(task_id, "acp_prewarm_discard", agent_type=handle.agent_type, reason=reason)
