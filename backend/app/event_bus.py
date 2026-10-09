"""任务事件总线

为前端 SSE 实时推送提供内存级事件订阅。

设计:
- 每个 task_id 对应一个订阅者列表(每个订阅者是一个 Queue)
- orchestrator / react_agent 落库 conversation 时调用 publish() 推送事件
- SSE 端点调用 subscribe() 拿到 Queue,阻塞读事件
- 任务结束/失败时推送终止事件,通知订阅者关闭

线程安全:用 threading.Lock 保护订阅者字典。
适用单机部署;多实例部署需换 Redis Pub/Sub(阶段 9+)。

事件格式:dict,字段:
- type: conversation / status / thinking_delta / done / error
- data: 对应业务数据(与 ConversationResponse / TaskResponse 字段一致)
- task_id: str
- timestamp: ISO 格式

thinking_delta 事件(阶段 7+):
  LLM 流式输出时每个 token 片段推送一次,前端打字机效果。
  data 字段:
    - conv_id: str       该次 LLM 调用的临时 ID(前端按此 key 累积)
    - round_idx: int     协作轮次
    - role: str          react_agent / agent2
    - phase: str         reasoning / content / tool_call / tool_result
    - delta: str         增量文本
    - index: int|None    工具调用索引(tool_call/tool_result phase 才有)
  流式结束后,完整内容仍会通过 conversation 事件推送一次,
  让迟到的订阅者(补播历史)也能看到完整内容。

clone_progress 事件(仓库克隆进度):
  local 模式下 _clone_repo_local 用 Popen 流式读 git clone 的 stderr,
  解析 "Receiving objects: X%" 等进度行后推送。高频瞬时事件,节流推送
  (百分比变化 >=5 或距上次推送 >=2s)。data 字段:
    - percent: int       进度百分比 0-100
    - message: str       原始进度行文本(截断到 200 字符)
  不入历史缓存:进度是瞬时的,迟到的订阅者(刷新页面)看到过时进度无意义。
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

logger = logging.getLogger(__name__)

# 事件类型
EventType = Literal[
    "conversation", "status", "thinking_delta", "plan",
    "question",  # agent2 请求用户澄清(选择题/填空题弹窗)
    "agent1_done",  # agent1 执行完成(任务标记 COMPLETED,后台审查开始;非终止事件)
    "review_done",  # 后台审查完成/失败(非终止事件,随后推 done)
    "review_plan_update",  # agent2 审查计划发射/修订(覆盖式,侧栏计划卡)
    "review_item_add",  # agent2 逐条审查项即时落库(侧栏审查结果区实时增长)
    "knowledge_point_add",  # agent2 逐条知识点即时落库(侧栏知识点区实时增长)
    "done", "error",
    "clone_progress",  # 仓库克隆进度(local 模式 Popen 流式解析 git stderr)
]

# 单个订阅者的队列容量上限(防止消费者过慢导致内存膨胀)
_QUEUE_MAXSIZE = 256


class _TaskBus:
    """单个 task 的事件总线(内部用)"""

    def __init__(self) -> None:
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._lock = threading.Lock()
        # 缓存最近的事件,供迟到的订阅者补播(只缓存最近 500 条)
        self._history: list[dict[str, Any]] = []
        self._finished = False

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        """订阅事件,返回 Queue。会先补播历史事件。"""
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        with self._lock:
            # 补播历史(订阅前已发生的事件)
            for event in self._history:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    logger.warning("订阅补播时队列满,丢弃旧事件")
                    break
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        """取消订阅"""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        """推送事件给所有订阅者,并缓存到历史

        thinking_delta / clone_progress 不入历史缓存:
        这类事件数量极大(每个 token / 每个进度行一条),会挤掉 conversation/status
        等重要事件。流式效果只对在线订阅者有意义,迟到的订阅者直接看完整 conversation
        即可(clone 进度是瞬时的,过时无意义)。
        """
        with self._lock:
            if self._finished:
                return
            etype = event.get("type")
            # 高频瞬时事件不缓存,只推给在线订阅者
            if etype not in ("thinking_delta", "clone_progress"):
                self._history.append(event)
                # 限制历史长度
                if len(self._history) > 500:
                    self._history = self._history[-500:]
            # 非阻塞推给所有订阅者
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # 高频流式事件满了就丢(不要求可靠性)
                    if etype in ("thinking_delta", "clone_progress"):
                        continue
                    logger.warning("订阅者队列满,丢弃事件")

    def finish(self) -> None:
        """标记任务结束,拒绝后续事件"""
        with self._lock:
            self._finished = True


# ============================================================
# 全局注册表:task_id → _TaskBus
# ============================================================

_buses: dict[str, _TaskBus] = {}
_buses_lock = threading.Lock()


def _get_bus(task_id: str) -> _TaskBus:
    """获取(或创建)task 的事件总线"""
    with _buses_lock:
        if task_id not in _buses:
            _buses[task_id] = _TaskBus()
        return _buses[task_id]


def subscribe(task_id: str | UUID) -> queue.Queue[dict[str, Any]]:
    """订阅指定 task 的事件流"""
    bus = _get_bus(str(task_id))
    q = bus.subscribe()
    # [诊断] SSE 连接建立日志:与 stream_task_events 的 initial_status 快照对拍,
    # 可定位"前端显示失败但后端 running"时是否有历史 error/done 事件被补播
    logger.info(
        f"[bus] task={task_id} SSE 订阅建立,订阅者数={len(bus._subscribers)},"
        f"补播历史条数={len(bus._history)}"
    )
    return q


def unsubscribe(task_id: str | UUID, q: queue.Queue[dict[str, Any]]) -> None:
    """取消订阅"""
    _get_bus(str(task_id)).unsubscribe(q)
    logger.info(f"[bus] task={task_id} SSE 订阅断开,剩余订阅者数={len(_get_bus(str(task_id))._subscribers)}")


def publish(
    task_id: str | UUID,
    type: EventType,
    data: dict[str, Any],
) -> None:
    """推送事件"""
    event = {
        "type": type,
        "task_id": str(task_id),
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    bus = _get_bus(str(task_id))
    # [诊断] 终止事件全量记录:error/done 是前端显示失败/结束的唯一事件源,
    # 记录推送时刻与订阅者数,与前端 client.log 对拍定位"未知失败"
    if type in ("done", "error"):
        logger.info(
            f"[bus] task={task_id} 推送终止事件 type={type} "
            f"订阅者数={len(bus._subscribers)} data={str(data)[:200]}"
        )
    bus.publish(event)


def finish_task(task_id: str | UUID) -> None:
    """标记任务结束(后续 publish 静默丢弃)"""
    _get_bus(str(task_id)).finish()


def is_task_finished(task_id: str | UUID) -> bool:
    """查询任务事件总线是否已标记结束

    resume/retry 启动前 reset_task_bus 会清除 _finished 标记。
    供 SSE 端点区分"任务确实结束"与"新执行已启动但 DB 状态尚未更新"
    (时序竞态:后台线程还未把 COMPLETED/FAILED 改成 RUNNING)。
    """
    with _buses_lock:
        bus = _buses.get(str(task_id))
    if bus is None:
        # 无总线记录说明从未 publish 过(不可能已结束),保守视为已结束
        return True
    with bus._lock:
        return bus._finished


def reset_task_bus(task_id: str | UUID) -> None:
    """重置 task 的事件总线(恢复审计时调用)

    任务完成后 _finished=True,后续 publish 全部被丢弃。
    用户追加消息触发 resume_audit_with_message 时,需先调用此函数
    清除 _finished 标记和 _history 缓存(含旧 done 事件),
    让新的 conversation/status/thinking_delta 等事件能正常推送,
    且前端重连 SSE 时不会因历史 done 事件立即关闭。

    注意:必须在 publish 之前调用,且在后台 resume 线程启动之前
    (API 端点同步调用,时序有保证)。
    """
    bus = _get_bus(str(task_id))
    with bus._lock:
        bus._finished = False
        bus._history.clear()
    logger.info(f"[task={task_id}] 事件总线已重置(清除 finished 标记 + 历史缓存)")


def cleanup_task(task_id: str | UUID) -> None:
    """清理 task 的事件总线(任务完成后释放内存)"""
    with _buses_lock:
        _buses.pop(str(task_id), None)
