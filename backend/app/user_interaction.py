"""用户交互管理:后台线程阻塞等待用户确认的唤醒机制

场景:
1. verifier_agent 动作授权(per_action 模式):每个 HTTP/PoC 动作
   执行前推送事件给前端弹窗,后台线程阻塞等待用户确认/拒绝
2. 危险命令确认(local 模式安全策略):LLM 执行危险命令前推送事件
   给前端弹窗,后台线程阻塞等待用户确认/拒绝

历史:此前还承载 agent2 澄清提问(pending question)与覆盖度清单确认
(pending checklist)两套阻塞机制,随"任务开始时质检评估"移除而删除。

设计:
- 每个 task 同时只有一个待处理动作(单轮确认)
- 用 threading.Event 阻塞后台线程;验证动作的等待**有上限**
  (`settings.VERIFY_AUTH_TIMEOUT_SECONDS`,可被「终止检查」提前唤醒)——
  此前无限等待,用户不点弹窗就把整轮后台审查挂死在工具执行内部,
  而审查的硬时限与终止标志只在循环边界检查,永远走不到
- pending 的动作与用户决议分别存全局 dict
- 任务结束/失败时调用 cleanup 释放内存

线程安全:用 threading.Lock 保护两个 dict。
适用单机部署;多实例部署需换 Redis 等共享存储。
"""
from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


# ============================================================
# verifier_agent 动作授权机制(per_action 模式:每个 HTTP/PoC 动作阻塞等用户确认)
# ============================================================


class _PendingVerifyAction:
    """单个 task 的待授权验证动作状态

    verifier_agent 在 per_action 模式下,每次执行 http_request / run_python_code 前:
    1. request_verify_authorization:把动作描述存起来 + 推 SSE 事件给前端弹窗
    2. wait_for_authorization:带时限阻塞等待用户确认
    3. 用户确认 → submit_verify_authorization(approved=True) → 唤醒,继续执行
       用户拒绝 → submit_verify_authorization(approved=False) → 唤醒,返回拒绝信息
       用户选"后续自动允许" → 同一 task 之后的动作不再弹窗(auto_approve)
    """

    def __init__(self) -> None:
        # 后台线程阻塞在此 Event 上
        self.event: threading.Event = threading.Event()
        # 用户提交的授权决议(action_id → approved bool)
        self.resolution: bool | None = None
        # 当前待授权的动作描述(供前端查询/展示)
        self.action_payload: dict[str, Any] | None = None
        # 用户已授权"本次及后续":True 时直接放行,不再弹窗阻塞
        # (动作照常落库进侧栏,用户仍能看到跑了什么)
        self.auto_approve: bool = False
        self.lock = threading.Lock()


_pending_verify: dict[str, _PendingVerifyAction] = {}
_pending_verify_lock = threading.Lock()


def _get_or_create_verify(task_id: str) -> _PendingVerifyAction:
    with _pending_verify_lock:
        if task_id not in _pending_verify:
            _pending_verify[task_id] = _PendingVerifyAction()
        return _pending_verify[task_id]


def _publish_verify_resolved(task_id_str: str, action_id: str, reason: str) -> None:
    """推送 verify_action_resolved 事件,让前端收起已经失效的授权弹窗

    超时/任务收尾时若只清后端状态不推事件,弹窗会挂在页面上等一个
    再也不会有人答复的决议,用户点下去只会收到"当前没有待授权动作"。
    """
    try:
        from app.event_bus import publish
        publish(task_id_str, "verify_action_resolved", {
            "action_id": action_id, "reason": reason,
        })
    except Exception as e:  # 总线已关等场景:收起弹窗失败不影响主流程
        logger.debug(f"[task={task_id_str}] verify_action_resolved 推送失败(忽略): {e}")


def verify_auto_approved(task_id: str | UUID) -> bool:
    """该 task 是否已被用户授权"后续动作自动允许"(True 则跳过弹窗)"""
    with _pending_verify_lock:
        pv = _pending_verify.get(str(task_id))
    if pv is None:
        return False
    with pv.lock:
        return bool(pv.auto_approve)


def request_verify_authorization(
    task_id: str | UUID,
    action_desc: dict[str, Any],
) -> None:
    """设置待授权的验证动作并推 SSE 事件(后台线程调用)

    action_desc: {"action_id": str, "type": "http_request"|"run_python_code", ...}
    推送 verify_action 事件,前端弹窗展示动作详情让用户确认/拒绝。
    """
    task_id_str = str(task_id)
    pv = _get_or_create_verify(task_id_str)
    with pv.lock:
        pv.event = threading.Event()
        pv.resolution = None
        pv.action_payload = action_desc
    # 推送事件给前端
    from app.event_bus import publish
    publish(task_id_str, "verify_action", action_desc)
    logger.info(f"[task={task_id_str}] 等待用户授权验证动作: {action_desc.get('type', '?')}")


def wait_for_authorization(
    task_id: str | UUID,
    action_id: str,
    timeout: float | None = None,
) -> str:
    """等待用户授权决议(后台线程调用),返回 "approved" / "denied" / "timeout"

    timeout=None 时保持旧行为(无限等);调用方应传入上限——被授权的动作用在
    审查线程内部,这一等就把挂钟时限和「终止检查」都挡在门外了。
    超时不只是返回 False:同时清掉 pending 动作并推 verify_action_resolved,
    前端据此收起弹窗,不留一个点了只会报错的僵尸弹窗。
    """
    task_id_str = str(task_id)
    pv = _get_or_create_verify(task_id_str)
    settled = pv.event.wait(timeout)
    with pv.lock:
        approved = pv.resolution is True
        pv.action_payload = None
    if not settled:
        logger.warning(
            f"[task={task_id_str}] 验证动作授权超时({timeout}s 未响应),按未授权跳过"
        )
        _publish_verify_resolved(task_id_str, action_id, "timeout")
        return "timeout"
    return "approved" if approved else "denied"


def submit_verify_authorization(
    task_id: str | UUID,
    action_id: str,
    approved: bool,
    remember: bool = False,
) -> bool:
    """提交用户授权决议,唤醒后台线程(API 端点调用)

    返回 True 表示成功唤醒;False 表示当前 task 没有待授权动作。
    remember=True(且 approved=True)时置 auto_approve:本 task 后续验证动作
    直接放行,不再逐个弹窗——一次 PoC 序列动辄七八个动作,逐个点"同意"
    实际会把用户逼向 direct 模式(那才是彻底没有人工把关)。
    """
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.get(task_id_str)
    if pv is None:
        return False
    with pv.lock:
        if pv.action_payload is None:
            return False
        if pv.event.is_set():
            return False
        pv.resolution = approved
        if approved and remember:
            pv.auto_approve = True
    pv.event.set()
    logger.info(
        f"[task={task_id_str}] 用户{'同意' if approved else '拒绝'}验证动作 {action_id}"
        + ("(后续自动允许)" if approved and remember else "")
    )
    return True


def get_pending_verify_action(task_id: str | UUID) -> dict[str, Any] | None:
    """查询 task 当前的待授权验证动作(前端恢复弹窗用)"""
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.get(task_id_str)
    if pv is None:
        return None
    with pv.lock:
        if pv.action_payload is None:
            return None
        return dict(pv.action_payload)


def clear_pending_verify_action(task_id: str | UUID, reason: str = "resolved") -> None:
    """清除 task 的待授权验证动作(任务结束/失败/用户终止检查时调用)

    若此刻有线程阻塞在 wait_for_authorization 上,这里把它唤醒并按"未授权"处理——
    「终止检查」正是靠这一步立即生效,否则要等到授权时限耗尽才退出。
    """
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.pop(task_id_str, None)
    if pv is not None:
        with pv.lock:
            action_id = str((pv.action_payload or {}).get("action_id") or "")
            had_pending = pv.action_payload is not None
            pv.action_payload = None
            pv.resolution = False
            pv.auto_approve = False
        pv.event.set()
        if had_pending:
            _publish_verify_resolved(task_id_str, action_id, reason)


def has_pending_verify_action(task_id: str | UUID) -> bool:
    """task 是否有待授权的验证动作(快速判断)"""
    task_id_str = str(task_id)
    with _pending_verify_lock:
        pv = _pending_verify.get(task_id_str)
    if pv is None:
        return False
    with pv.lock:
        return pv.action_payload is not None


# ============================================================
# 危险命令确认(local 模式安全策略)
# ============================================================


class _PendingCommandConfirm:
    """单个 task 的待确认危险命令状态

    local 模式下,LLM 调用 run_command / run_python_code 执行的危险命令:
    1. request_command_confirm:把命令描述存起来 + 推 SSE 事件给前端弹窗
    2. wait_for_command_confirm:阻塞等用户确认
    3. 用户确认 → submit_command_confirm(approved=True) → 唤醒,继续执行
       用户拒绝 → submit_command_confirm(approved=False) → 唤醒,返回拒绝信息
    """

    def __init__(self) -> None:
        self.event: threading.Event = threading.Event()
        self.resolution: bool | None = None
        self.command_payload: dict[str, Any] | None = None
        self.lock = threading.Lock()


_pending_command: dict[str, _PendingCommandConfirm] = {}
_pending_command_lock = threading.Lock()


def _get_or_create_command(task_id: str) -> _PendingCommandConfirm:
    with _pending_command_lock:
        if task_id not in _pending_command:
            _pending_command[task_id] = _PendingCommandConfirm()
        return _pending_command[task_id]


def request_command_confirm(
    task_id: str | UUID,
    command_desc: dict[str, Any],
) -> None:
    """设置待确认的危险命令并推 SSE 事件(后台线程调用)

    command_desc: {"command_id": str, "command": str, "tool": str, "reason": str}
    """
    task_id_str = str(task_id)
    pc = _get_or_create_command(task_id_str)
    with pc.lock:
        pc.event = threading.Event()
        pc.resolution = None
        pc.command_payload = command_desc
    from app.event_bus import publish
    publish(task_id_str, "command_confirm", command_desc)
    logger.info(f"[task={task_id_str}] 等待用户确认危险命令: {command_desc.get('command', '?')[:80]}")


def wait_for_command_confirm(task_id: str | UUID, command_id: str) -> bool:
    """阻塞等待用户确认决议(后台线程调用)

    返回 True=用户同意执行,False=用户拒绝或任务被取消。
    """
    task_id_str = str(task_id)
    pc = _get_or_create_command(task_id_str)
    pc.event.wait()
    with pc.lock:
        approved = pc.resolution is True
        pc.command_payload = None
    return approved


def submit_command_confirm(
    task_id: str | UUID,
    command_id: str,
    approved: bool,
) -> bool:
    """提交用户确认决议,唤醒后台线程(API 端点调用)

    返回 True 表示成功唤醒;False 表示当前 task 没有待确认命令。
    """
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.get(task_id_str)
    if pc is None:
        return False
    with pc.lock:
        if pc.command_payload is None:
            return False
        if pc.event.is_set():
            return False
        pc.resolution = approved
    pc.event.set()
    logger.info(f"[task={task_id_str}] 用户{'同意' if approved else '拒绝'}执行命令 {command_id}")
    return True


def get_pending_command_confirm(task_id: str | UUID) -> dict[str, Any] | None:
    """查询 task 当前的待确认命令(前端恢复弹窗用)"""
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.get(task_id_str)
    if pc is None:
        return None
    with pc.lock:
        if pc.command_payload is None:
            return None
        return dict(pc.command_payload)


def clear_pending_command_confirm(task_id: str | UUID) -> None:
    """清除 task 的待确认命令(任务结束/失败时调用)"""
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.pop(task_id_str, None)
    if pc is not None:
        with pc.lock:
            pc.command_payload = None
            pc.resolution = False
        pc.event.set()


def has_pending_command_confirm(task_id: str | UUID) -> bool:
    """task 是否有待确认的危险命令(快速判断)"""
    task_id_str = str(task_id)
    with _pending_command_lock:
        pc = _pending_command.get(task_id_str)
    if pc is None:
        return False
    with pc.lock:
        return pc.command_payload is not None
