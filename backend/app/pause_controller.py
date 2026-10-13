"""任务暂停/恢复控制:user 在 agent 运行时按下暂停按钮,后台线程阻塞在检查点

场景:agent 在 ReAct 循环的迭代边界、工具调用前,调用 wait_if_paused()。
若用户已按下暂停,该调用阻塞,直到用户按下恢复按钮(set Event)。

CLI/ACP 执行器(agent1 走 Qoder/DeepSeek/Codex)不阻塞在 wait_if_paused:
其流式循环把 `is_paused()` 作为 pause_probe 传入,命中即向 CLI 发 session/cancel
真中断生成(见 acp_base.ACPPromptPaused),执行轮结束后由编排层在轮后
wait_if_paused 门控冻结。恢复后的收尾语义由 settings.ACP_AUTO_CONTINUE_ON_RESUME
决定:默认(开)在同一活跃 ACP session 自动补发一轮"继续"让 agent1 接着做,
直到某轮正常跑完才收尾进审查;关闭(或达 ACP_AUTO_CONTINUE_MAX_ROUNDS 上限)
时退回旧行为——对半截结果直接收尾。见 orchestrator._run_agent1_rounds_with_auto_continue。

设计:
- 每个 task 对应一个 _PauseState,内含一个 threading.Event
- Event.set()  = 运行中(默认)
- Event.clear() = 已暂停
- wait_if_paused() 调用 Event.wait():set 时立即返回,clear 时阻塞
- pause/resume 是幂等操作,重复调用无副作用

线程安全:用 threading.Lock 保护注册表 dict。
适用单机部署;多实例部署需换 Redis 等共享存储。

注意:
- 与 user_interaction.py 的待授权动作 Event 不同,这里 Event 是"运行门控"
  (set=放行,clear=阻塞),而后者是"一次性唤醒"(wait 等待 set,被 set 后唤醒一次)。
- 服务重启会丢失 in-memory 状态,任务会卡在 PAUSED 状态(后台线程已死)。
  这是单机部署的已知限制,生产环境应配合任务恢复机制。
"""
from __future__ import annotations

import logging
import threading
from uuid import UUID

logger = logging.getLogger(__name__)


class _PauseState:
    """单个 task 的暂停/运行门控状态

    run_event:
        set()   = 运行中(默认),wait_if_paused() 立即返回
        clear() = 已暂停,wait_if_paused() 阻塞直到 set()
    """

    def __init__(self) -> None:
        self.run_event: threading.Event = threading.Event()
        self.run_event.set()  # 默认运行中

    def pause(self) -> None:
        self.run_event.clear()

    def resume(self) -> None:
        self.run_event.set()

    def wait_if_paused(self) -> None:
        """阻塞直到任务处于运行态(若已运行则立即返回)"""
        self.run_event.wait()

    def is_paused(self) -> bool:
        return not self.run_event.is_set()


# 全局注册表:task_id(str)→ _PauseState
_states: dict[str, _PauseState] = {}
_states_lock = threading.Lock()


def _get_or_create(task_id: str) -> _PauseState:
    """获取(或创建)task 的暂停状态"""
    with _states_lock:
        if task_id not in _states:
            _states[task_id] = _PauseState()
        return _states[task_id]


def pause_task(task_id: str | UUID) -> None:
    """暂停 task(后台线程会在下一个检查点阻塞)

    幂等:重复调用无副作用。若任务已在 PAUSED,无操作。
    """
    state = _get_or_create(str(task_id))
    if not state.is_paused():
        state.pause()
        logger.info(f"[task={task_id}] 已暂停")


def resume_task(task_id: str | UUID) -> None:
    """恢复 task(唤醒在检查点阻塞的后台线程)

    幂等:重复调用无副作用。若任务未暂停,无操作。
    """
    state = _get_or_create(str(task_id))
    if state.is_paused():
        state.resume()
        logger.info(f"[task={task_id}] 已恢复")


def wait_if_paused(task_id: str | UUID, db: object | None = None) -> None:
    """后台线程调用:若已暂停则阻塞,直到恢复

    在 agent 的检查点(iteration 边界、工具调用前)调用。
    未暂停时立即返回,几乎零开销(Event.wait() 内部仅检查标志位)。

    db: 可选的 SQLAlchemy Session。传入且确实需要阻塞时,会先释放其连接
    (见 release_connection),避免长时间暂停期间连接被服务端掐断。
    不传(未持有 session 的调用方)则行为与原先完全一致。
    """
    state = _get_or_create(str(task_id))
    if state.is_paused():
        # 即将进入无上限阻塞:先把连接归还池(未暂停走上面直接返回,零开销)
        release_connection(db)
    state.wait_if_paused()


def release_connection(db: object | None) -> None:
    """阻塞等待前把 session 事务落定、归还底层连接(去脆化)。

    commit 会把已 checkout 的连接还给池;唤醒后首查会重新取连接并经
    pool_pre_ping 校验,规避"长时间 park 期间连接被服务端掐断 → 醒来首查炸"。
    连接若已死 commit 会抛 → 退回 rollback(同样释放连接)。全程吞异常:
    这只是阻塞前的预防性动作,不得因其失败影响等待/执行主流程。db 为 None 直接跳过。
    """
    if db is None:
        return
    try:
        db.commit()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 主连接可能已断,回退同样归还连接
        try:
            db.rollback()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def is_paused(task_id: str | UUID) -> bool:
    """查询 task 是否处于暂停态(快速判断)"""
    state = _get_or_create(str(task_id))
    return state.is_paused()


def clear_pause_state(task_id: str | UUID) -> None:
    """清除 task 的暂停状态(任务结束/失败时调用)

    同时 resume 一下,防止后台线程在 finally 之前的检查点被卡住无法退出。
    """
    task_id_str = str(task_id)
    with _states_lock:
        state = _states.pop(task_id_str, None)
    if state is not None:
        # 唤醒可能阻塞在 wait_if_paused 的线程,让它继续走清理流程
        state.resume()
