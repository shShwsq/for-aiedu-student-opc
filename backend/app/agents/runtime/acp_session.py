"""ACP 会话持久化与恢复决策(runtime 共享原语)。

背景:CLI(Qoder / DeepSeek Harness)把会话 transcript 持久化在自己的磁盘目录
(Qoder 为 `~/.qoder/projects/<cwd>/<sessionId>.jsonl`),并在 ACP `initialize`
响应里声明恢复能力:
- 标准方法 `session/load`(agentCapabilities.loadSession = true)
- 扩展方法 `session/resume`(agentCapabilities.sessionCapabilities.resume)

有了这两个方法,后端重启 / bridge 重建后仍可按 sessionId 让 CLI 自己复原上下文,
比后端把历史渲染成文本回放给 CLI 保真度更高(含工具调用与思考过程)且零 token
成本。本模块只做"决策 + 记录",协议调用留在 acp_base(与沙箱/bridge 相关)。

边界:
- 沙箱重建后 CLI 的磁盘状态随容器销毁,恢复必然失败 → 降级 session/new + 文本回放
- 只有 **支持排空恢复回放通知** 的 bridge(bridge_protocol >= 2)才允许恢复:
  恢复时 CLI 会把历史以 `session/update` 通知流回放,旧 bridge 在收到最终响应即
  关流,残留通知会被下一个请求(session/prompt)当成本轮输出,污染对话落库
- 记录只描述"哪个 session 带着哪些上下文",不改变注入内容本身
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# task.params 下的持久化字段名(与 "_plan" 同一约定:跨轮续接的系统状态)
ACP_SESSION_KEY = "_acp_session"

# 能安全处理"恢复回放通知"的 bridge 协议版本
# (acp_bridge.py 的 /health 上报;1 = 无该字段的老 bridge)
BRIDGE_PROTOCOL_REPLAY_SAFE = 2

# 恢复方法优先级:扩展 session/resume 覆盖 Qoder + dsh;dsh 不实现标准
# session/load,Qoder 两者都实现,因此先试 resume
_RESTORE_METHODS: tuple[str, ...] = ("session/resume", "session/load")

_LOAD_SESSION_CAPABILITY = "loadSession"


def parse_restore_method(init_result: dict[str, Any] | None) -> str | None:
    """从 initialize 响应判定可用的会话恢复方法(无能力时返回 None)

    只看能力声明,不按 agent_type 硬编码:各 CLI 的实现随版本变化,
    以它自己的 advertise 为准最可靠。响应结构(ACP):
        {"agentCapabilities": {
            "loadSession": true,
            "sessionCapabilities": {"resume": {}, "list": {}, "close": {}}
        }}
    扩展项的值形态因 CLI 而异(空对象/布尔/带字段的对象——Qoder 即以空对象
    {} 表示支持,故空容器一律视为支持),只排除显式 False/None(把"声明为
    关闭"误读为"支持"的来源);字符串真值("true"等)不等于布尔 True,不启用。
    """
    if not isinstance(init_result, dict):
        return None
    caps = init_result.get("agentCapabilities") or init_result.get("capabilities") or {}
    if not isinstance(caps, dict):
        return None
    session_caps = caps.get("sessionCapabilities") or {}
    if not isinstance(session_caps, dict):
        session_caps = {}

    for method in _RESTORE_METHODS:
        if method == "session/load":
            if caps.get(_LOAD_SESSION_CAPABILITY) is True:
                return method
            continue
        # 扩展族:只看 key 是否存在且未被显式关闭
        flag = session_caps.get("resume")
        if "resume" in session_caps and flag not in (False, None):
            return method
    return None


def build_session_record(
    *,
    agent_type: str,
    session_id: str,
    cwd: str,
    injected_context_hash: dict[str, str] | None,
    round_idx: int,
    restore_method: str | None = None,
    truncated: bool = False,
) -> dict[str, Any] | None:
    """构造持久化的会话记录;截断兜底轮返回 None(不生成记录)

    仅 prompt 成功送达后才应调用本函数;idle 挂死/流中断被 prompt() 内部
    降级为截断收尾时同样会走到调用点,但该轮内容可能未被 CLI 写入磁盘
    transcript(CLI 崩溃即丢),此时 round_idx 不能推进——记录保留上一次
    干净轮的值,恢复后的增量回放才能把这一轮补发给 CLI。

    `injected_context_hash` 必须随记录一起持久化:恢复出来的会话上下文里已包含
    此前注入的仓库/记忆段,不同步恢复指纹会造成同一内容重复注入。
    `round_idx` 兼作 last_accepted_round:恢复后增量回放以此为下界。
    """
    if truncated:
        return None
    return {
        "agent_type": agent_type,
        "session_id": session_id,
        "cwd": cwd,
        "injected_context_hash": dict(injected_context_hash or {}),
        "prompt_accepted": True,
        "round_idx": round_idx,
        "restore_method": restore_method,
    }


def load_session_record(params: dict[str, Any] | None, agent_type: str) -> dict[str, Any] | None:
    """读取本任务当前 CLI 的会话记录(agent_type 不符/字段缺失 → None)

    任务中途换执行器时 sessionId 属于另一个 CLI,必须视为无记录(否则会拿
    Qoder 的 sessionId 去叫 dsh 恢复)。
    """
    record = (params or {}).get(ACP_SESSION_KEY)
    if not isinstance(record, dict):
        return None
    if record.get("agent_type") != agent_type:
        return None
    if not record.get("session_id") or not record.get("prompt_accepted"):
        return None
    return record


def _rollback_quietly(db) -> None:
    """commit 失败后的补救:回滚让本轮后续 DB 操作可用(rollback 再失败仅告警)

    不回滚的话 SQLAlchemy session 会停在 pending-rollback 状态,本轮之后的所有
    DB 操作(collector 落库/orchestrator 收尾)都会抛 PendingRollbackError,
    "失败仅告警不影响执行轮"的承诺就被打破了。
    """
    try:
        db.rollback()
    except Exception as re:
        logger.warning(f"[acp_session] 回滚失败(忽略): {re}")


def save_session_record(db, task, record: dict[str, Any]) -> None:
    """把会话记录写入 task.params[ACP_SESSION_KEY](失败仅告警,不影响执行轮)"""
    try:
        task.params = {**(task.params or {}), ACP_SESSION_KEY: record}
        db.commit()
    except Exception as e:
        logger.warning(f"[task={getattr(task, 'id', '?')}] 保存 ACP 会话记录失败(忽略): {e}")
        _rollback_quietly(db)


def clear_session_record(db, task, agent_type: str | None = None) -> None:
    """删除会话记录(恢复失败 / session 已废),防下一轮反复尝试死会话

    agent_type 非空时只删该 CLI 的记录(避免换执行器后误删对方的可用记录)。
    """
    try:
        params = dict(task.params or {})
        existing = params.get(ACP_SESSION_KEY)
        if isinstance(existing, dict) and agent_type and existing.get("agent_type") != agent_type:
            return
        if ACP_SESSION_KEY not in params:
            return
        params.pop(ACP_SESSION_KEY, None)
        task.params = params
        db.commit()
    except Exception as e:
        logger.warning(f"[task={getattr(task, 'id', '?')}] 清除 ACP 会话记录失败(忽略): {e}")
        _rollback_quietly(db)


def plan_session_open(
    *,
    init_result: dict[str, Any] | None,
    record: dict[str, Any] | None,
    bridge_protocol: int,
    cwd: str,
) -> dict[str, Any]:
    """决定本轮如何打开 ACP 会话:恢复优先,否则新建

    返回 {"method": str | None, "session_id": str | None, "cwd": str | None,
    "injected_context_hash": dict, "round_idx": int, "reason": str}。

    method 为 None 表示走 session/new,reason 说明为何不恢复(进日志便于排查)。
    恢复成功的判定不在这里做(需要真实 RPC 调用),这里只给出"值得尝试"的决策。
    round_idx 取自记录(= last_accepted_round):恢复出来的 transcript 覆盖到
    该轮为止,执行侧据此决定是否还需对更新的轮次做增量回放。

    恢复的前置条件全部要满足:
    1. CLI 声明了恢复能力
    2. 本任务有该 CLI 的可用会话记录(prompt_accepted=True)
    3. bridge 会排空恢复回放通知(否则历史会污染本轮落库/推流)
    4. 工作目录与记录一致:CLI 按 cwd 定位会话,Qoder 尤其如此;不一致通常意味
       沙箱重建或仓库重 clone,磁盘状态已不可靠
    """
    method = parse_restore_method(init_result)
    if not method:
        return _new_session_decision("cli_advertises_no_restore", None)
    if not record:
        return _new_session_decision("no_persisted_session", method)
    if bridge_protocol < BRIDGE_PROTOCOL_REPLAY_SAFE:
        return _new_session_decision("bridge_cannot_drain_replay", method)
    record_cwd = str(record.get("cwd") or "")
    if not record_cwd or record_cwd != (cwd or ""):
        return _new_session_decision("cwd_changed", method)

    return {
        "method": method,
        "session_id": str(record["session_id"]),
        "cwd": record_cwd,
        "injected_context_hash": dict(record.get("injected_context_hash") or {}),
        "round_idx": int(record.get("round_idx") or 0),
        "reason": "restore",
    }


def _new_session_decision(reason: str, method: str | None) -> dict[str, Any]:
    """走 session/new 的决策(不带恢复信息与指纹)"""
    return {
        "method": None,
        "session_id": None,
        "cwd": None,
        "injected_context_hash": {},
        "round_idx": 0,
        "reason": reason,
    }
