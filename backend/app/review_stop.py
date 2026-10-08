"""检查助手(agent2)后台审查终止控制:user 在审查进行中按下"终止检查"

场景:agent2 后台审查动辄数分钟(只读核查 / PoC / 引用复核多次 LLM 往返),
而有些对话并不需要检查。本模块提供"按轮登记"的协作式终止标志:后台审查线程在
检查点(工具循环边界、LLM 流 chunk 边界)查询 should_stop(),命中即自行收尾
退出 —— 不阻塞、不杀线程(与 pause_controller 的"门控阻塞"语义相反:审查期间
任务早已 COMPLETED,把线程挂在检查点上只会一直占着 DB session 与事件活跃期)。

设计:
- 登记表记录"当前正在审查的轮次":begin_review(task_id, round_idx) 登记并清掉
  上一轮遗留的终止标志。should_stop(task_id, round_idx) 只在轮次匹配时为 True,
  因此终止请求绝不泄漏到追问开启的新一轮审查(否则新审查一启动就被终止)
- request_stop 返回 None 表示当前没有登记的审查(线程已死 / 尚未开始),
  路由据此直接写终态,避免前端"检查中"角标永久卡住
- end_review 在审查收尾时注销登记,注册表不随任务数无限增长

线程安全:threading.Lock 保护注册表 dict。
适用单机部署;多实例部署需换 Redis 等共享存储(与 pause_controller / clone_skip 同)。
服务重启会丢失标志,最坏后果是终止请求失效、审查照常跑完,不影响数据正确性。
"""
from __future__ import annotations

import logging
import threading
from uuid import UUID

logger = logging.getLogger(__name__)


class _ReviewStopState:
    """单个 task 的审查终止状态

    round_idx:当前正在审查的轮次(None = 没有审查在跑)
    stop_round:已请求终止的轮次(None = 未请求)
    """

    __slots__ = ("round_idx", "stop_round")

    def __init__(self) -> None:
        self.round_idx: int | None = None
        self.stop_round: int | None = None


# 全局注册表:task_id(str)→ _ReviewStopState
_states: dict[str, _ReviewStopState] = {}
_states_lock = threading.Lock()


def _get_or_create(task_id: str | UUID) -> _ReviewStopState:
    """获取(或创建)task 的终止状态(调用方持锁)"""
    key = str(task_id)
    state = _states.get(key)
    if state is None:
        state = _ReviewStopState()
        _states[key] = state
    return state


def begin_review(task_id: str | UUID, round_idx: int) -> None:
    """登记"本轮审查已开始"(后台线程调用)

    同时清掉更早轮次遗留的终止标志:用户上一轮点了终止、随后追问开了新一轮
    审查时,新审查必须正常跑,不能被旧请求秒掉。
    """
    with _states_lock:
        state = _get_or_create(task_id)
        if state.stop_round is not None and state.stop_round != round_idx:
            logger.info(
                f"[task={task_id}] 新一轮审查(round={round_idx})开始,"
                f"清除 round={state.stop_round} 的遗留终止标志"
            )
            state.stop_round = None
        state.round_idx = round_idx


def end_review(task_id: str | UUID, round_idx: int | None = None) -> None:
    """注销审查登记(后台线程收尾时调用,无论成功/失败/终止)

    round_idx 非空时只在轮次匹配才清理 —— 并行语义下老审查收尾不能把
    新轮刚登记的在跑审查一并清掉(那样新一轮就再也收不到终止请求)。
    """
    with _states_lock:
        state = _states.get(str(task_id))
        if state is None:
            return
        if round_idx is not None and state.round_idx != round_idx:
            return  # 已被新一轮审查接管,保留其登记
        _states.pop(str(task_id), None)


def request_stop(task_id: str | UUID) -> int | None:
    """请求终止当前正在跑的审查,返回被终止的轮次

    返回 None 表示没有审查在跑(标志不落,避免残留影响下一轮);
    调用方(路由)此时应直接把 review_status 写成终态。
    幂等:同一轮重复请求无副作用。
    """
    with _states_lock:
        state = _get_or_create(task_id)
        round_idx = state.round_idx
        if round_idx is None:
            logger.info(f"[task={task_id}] 收到终止检查请求,但当前没有审查在跑")
            return None
        if state.stop_round != round_idx:
            logger.info(f"[task={task_id}] 已请求终止检查(round={round_idx})")
            state.stop_round = round_idx
        return round_idx


def should_stop(task_id: str | UUID, round_idx: int | None = None) -> bool:
    """后台线程检查点调用:本轮审查是否已被请求终止

    round_idx 非空时按轮次比对(只终止发起请求的那一轮);
    未登记终止的任务返回 False,几乎零开销。
    """
    with _states_lock:
        state = _states.get(str(task_id))
        if state is None or state.stop_round is None:
            return False
        if round_idx is None:
            return True
        return state.stop_round == round_idx


def has_active_review(task_id: str | UUID) -> bool:
    """是否有登记的审查在跑(诊断/测试用)"""
    with _states_lock:
        state = _states.get(str(task_id))
        return state is not None and state.round_idx is not None


def clear_review_stop_state(task_id: str | UUID) -> None:
    """清除 task 的审查终止状态(任务删除时调用,防内存泄漏)

    幂等:无记录时无操作。
    """
    with _states_lock:
        _states.pop(str(task_id), None)
