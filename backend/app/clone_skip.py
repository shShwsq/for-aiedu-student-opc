"""预克隆跳过控制:user 在克隆阶段按下"跳过预克隆",克隆轮询循环在下一个
检查点终止当前 clone,orchestrator 改由执行阶段自主克隆(executor 不变)

设计:
- 一次性标志:task_id → True;consume_skip_clone() 读取并弹出(只触发一次),
  防止标志残留影响 followup / 重试时再次进入 _prepare_repo_context 的预克隆
- 仅对 orchestrator 预克隆路径生效:clone_repo_with_fallback 的
  cancellable=True 才检查标志;LLM 工具路径(clone_repo 委托)恒不检查
- 幂等:request 重复调用无副作用

线程安全:用 threading.Lock 保护注册表 dict。
适用单机部署;多实例部署需换 Redis 等共享存储(与 pause_controller 同)。

注意:服务重启会丢失 in-memory 标志,但最坏后果只是"跳过请求失效,
克隆继续跑",不影响任务正确性。
"""
from __future__ import annotations

import logging
import threading
from uuid import UUID

logger = logging.getLogger(__name__)

# 全局注册表:task_id(str)→ True(已请求跳过)
_requested: dict[str, bool] = {}
_lock = threading.Lock()


def request_skip_clone(task_id: str | UUID) -> None:
    """请求跳过预克隆(克隆轮询循环在下一个检查点终止并降级)

    幂等:重复调用无副作用。标志在下次 consume 时弹出;任务结束时
    由 clear_skip_state 兜底清理。
    """
    task_id_str = str(task_id)
    with _lock:
        if task_id_str not in _requested:
            logger.info(f"[task={task_id_str}] 已请求跳过预克隆")
        _requested[task_id_str] = True


def is_skip_requested(task_id: str | UUID) -> bool:
    """快速判断是否已请求跳过(不消费标志)"""
    with _lock:
        return _requested.get(str(task_id), False)


def consume_skip_clone(task_id: str | UUID) -> bool:
    """克隆检查点调用:已请求跳过则返回 True 并弹出标志(一次性语义)

    弹出保证标志不会残留到 followup / 重试的下一轮预克隆,
    也不会误伤 react_agent 执行期的自主 clone。
    """
    with _lock:
        return _requested.pop(str(task_id), False)


def clear_skip_state(task_id: str | UUID) -> None:
    """清除 task 的跳过标志(任务结束/失败/删除时调用,防内存泄漏)

    幂等:无标志时无操作。
    """
    with _lock:
        _requested.pop(str(task_id), None)
