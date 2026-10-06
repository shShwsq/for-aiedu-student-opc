"""AI助手抽象层(Executor Provider)

将"AI助手"抽象为统一接口,支持多种实现:
- BuiltinReactAgent:内置 react_agent(基于 react_agent.py)
- ExternalCLIAgent:外部 CLI agent,通过 ACP 协议通信(沙箱内运行),
  按 registry 动态派发(如 qoder_cli,未来可扩展 aider / goose 等)

orchestrator 通过 get_executor(task) 拿到对应的 provider,调用 .run() 执行一轮,
无需关心底层是内置 LLM 循环还是外部 CLI 协议。

接口契约:
    run(task, db, round_idx, followup_query, client, repo_context, previous_plan)
        -> (results, summary, final_plan)

    - results: 结构化结果列表(始终为空,由 agent2 在 done 时提取)
    - summary: 本轮自然语言总结(供 agent2 评估)
    - final_plan: 本轮结束时的 plan 状态(供下一轮续接)

设计说明:
- client(LLMClient)仅对内置 provider 有意义;外部 CLI provider 自带模型配置,
  会忽略该参数(签名保持一致以符合抽象契约)。
- provider 内部负责设置 task 上下文(set_current_task)、推送 SSE 事件、
  落库 Conversation 记录,与原 run_react_agent 行为对齐。
- 外部 CLI provider 通过 registry 声明 executor_module / executor_func,
  get_executor 用 importlib 延迟加载,新增 agent 类型无需改本文件。
"""
from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.orm import Session

from app.agents.registry import get_executor_location, is_registered
from app.llm.client import LLMClient
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 执行器类型常量
# ============================================================

EXECUTOR_BUILTIN = "builtin"

# 已注册的执行器标识(供前端枚举 + 校验)
# builtin 始终可用;其余从 registry 动态读取
REGISTERED_EXECUTORS: tuple[str, ...] = (EXECUTOR_BUILTIN,)


class ExecutorAgent(ABC):
    """AI助手抽象基类

    子类必须实现 run(),签名与原 react_agent.run_react_agent 对齐,
    便于 orchestrator 无差别调用。

    子类应在 __init__ 中接收所需的依赖(如沙箱客户端、ACP 客户端等),
    但 run() 的参数列表保持统一。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """执行器标识(用于日志/事件标注)"""

    @abstractmethod
    def run(
        self,
        task: Task,
        db: Session,
        round_idx: int = 1,
        followup_query: str | None = None,
        client: LLMClient | None = None,
        repo_context: str | None = None,
        previous_plan: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        """执行一轮审计

        参数:
            task: 任务对象(含 user_input / params / scenario 等)
            db: 数据库会话(用于落库 Conversation)
            round_idx: 当前协作轮次(1 开始)
            followup_query: 追问指令。None 表示第一轮(用 task.user_input)
            client: LLMClient(仅内置 provider 使用;外部 CLI 忽略)
            repo_context: 第 1 轮专用,orchestrator 主动 clone 后的仓库上下文
            previous_plan: 上一轮结束时的 plan 状态(跨轮续接)

        返回:(results, summary, final_plan)
            results: 始终为空 list(结构化结果由 agent2 在 done 时提取)
            summary: 本轮自然语言总结
            final_plan: 本轮结束时的 plan 状态(可能为空 list)
        """
        raise NotImplementedError


# ============================================================
# 内置执行器:包装现有 react_agent
# ============================================================


class BuiltinReactAgent(ExecutorAgent):
    """内置 react_agent

    直接委托给 app.agents.react_agent.run_react_agent,
    行为与改造前完全一致(零行为变更,纯包装)。
    """

    @property
    def name(self) -> str:
        return EXECUTOR_BUILTIN

    def run(
        self,
        task: Task,
        db: Session,
        round_idx: int = 1,
        followup_query: str | None = None,
        client: LLMClient | None = None,
        repo_context: str | None = None,
        previous_plan: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        # 延迟导入避免循环依赖(react_agent 依赖 tools.schema,本模块被 orchestrator 导入)
        from app.agents.react_agent import run_react_agent

        return run_react_agent(
            task,
            db,
            round_idx=round_idx,
            followup_query=followup_query,
            client=client,
            repo_context=repo_context,
            previous_plan=previous_plan,
        )


# ============================================================
# 外部 CLI 执行器:按 registry 动态派发
# ============================================================


class ExternalCLIAgent(ExecutorAgent):
    """外部 CLI agent(通用包装)

    通过 registry 声明的 executor_module / executor_func 延迟加载具体实现
    (如 app.agents.qoder_cli_agent.run_qoder_cli_agent)。

    新增一种外部 CLI 只需在 registry 注册,无需改本文件。具体实现模块负责:
    - 从 user_agent_configs 加载凭证
    - 沙箱内启动 CLI + ACP bridge
    - ACP 通信 + 事件翻译
    - 返回 (results, summary, plan)
    """

    def __init__(self, agent_type: str):
        self._agent_type = agent_type
        # 延迟解析 executor 位置(仅校验已注册)
        if not is_registered(agent_type):
            raise ValueError(f"未注册的 agent 类型: {agent_type}")

    @property
    def name(self) -> str:
        return self._agent_type

    def _load_run_func(self):
        """从 registry 查找 executor_module.executor_func,用 importlib 加载"""
        location = get_executor_location(self._agent_type)
        if not location or not location[0] or not location[1]:
            raise RuntimeError(
                f"agent '{self._agent_type}' 未配置 executor_module/executor_func"
            )
        module_path, func_name = location
        module = importlib.import_module(module_path)
        run_func = getattr(module, func_name, None)
        if run_func is None:
            raise RuntimeError(
                f"模块 {module_path} 中未找到函数 {func_name}"
            )
        return run_func

    def run(
        self,
        task: Task,
        db: Session,
        round_idx: int = 1,
        followup_query: str | None = None,
        client: LLMClient | None = None,
        repo_context: str | None = None,
        previous_plan: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        run_func = self._load_run_func()
        logger.info(
            f"[task={task.id}] 使用 {self._agent_type} 执行器,round={round_idx}"
        )
        return run_func(
            task,
            db,
            round_idx=round_idx,
            followup_query=followup_query,
            repo_context=repo_context,
            previous_plan=previous_plan,
            agent_type=self._agent_type,
            # client 参数被忽略:外部 CLI 自带模型配置
        )


# ============================================================
# 工厂:按 task.executor 选择 provider
# ============================================================


def get_executor(task: Task) -> ExecutorAgent:
    """根据 task.executor 字段返回对应的执行器实例

    - "builtin"(默认):返回 BuiltinReactAgent
    - registry 中已注册的 agent_type(如 "qoder_cli"):返回 ExternalCLIAgent

    未知值回退到 builtin 并记录 warning(不阻塞任务执行)。
    每次调用返回新实例(provider 无状态,实例化成本低)。
    """
    executor = (task.executor or EXECUTOR_BUILTIN).strip().lower()

    if executor == EXECUTOR_BUILTIN:
        return BuiltinReactAgent()

    if is_registered(executor):
        return ExternalCLIAgent(executor)

    # 未知 executor:回退到 builtin
    logger.warning(
        f"[task={task.id}] 未知 executor='{executor}',回退到 builtin"
    )
    return BuiltinReactAgent()
