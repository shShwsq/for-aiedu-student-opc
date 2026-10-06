"""agent 智能体策略:默认值定义 + 用户级/任务级合并解析

agent2(质检智能体)的智能体策略入口:
- DEFAULT_AGENT_POLICY:全字段默认值(与 AgentPolicy 表列对齐)
- resolve_agent_policy:合并 用户级默认(agent_policies 表)+ 任务级覆盖
  (task.params["_agent_policy"]),返回最终生效的策略

策略项覆盖:agent2 开关、验证权限(allow_verify /
verifier_auth_mode_default)、引用复核(allow_reference_check)、
AI助手确认策略默认模式等。

历史:曾有 max_rounds(协作总轮次)设置,agent2 审查移到后台执行后
初始运行只有 1 轮 agent1、多轮由用户驱动(resume),该设置已移除。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 默认策略 + 配置解析
# ============================================================

DEFAULT_AGENT_POLICY: dict[str, Any] = {
    "agent2_enabled": True,  # 是否启用 agent2(关闭=单 agent 模式,跳过评估/验证)
    "allow_verify": False,  # agent2 是否能调用 verifier_agent(需任务配了 test_env_url)
    "verifier_auth_mode_default": "per_action",  # 验证授权默认模式(任务级可覆盖)
    "executor_command_confirm_default": "always_approve",  # AI助手确认策略默认模式(任务级 _executor_command_confirm 可覆盖)
    # agent2 是否能调用 check_reference 复核 agent1 引用的网址
    # (后端安全抓取,SSRF 防护;独立于 repo_path / test_env_url,任何任务可用)
    "allow_reference_check": True,
}


def verify_default_scenarios() -> set[str]:
    """默认开启 PoC 验证的场景 id 集合(逗号分隔配置,见 config)

    场景命中且用户/任务级均未显式设置 allow_verify 时自动开启;
    实际是否跑 PoC 仍取决于任务是否配置了 test_env_url。
    """
    return {
        s.strip()
        for s in settings.VERIFY_DEFAULT_SCENARIOS.split(",")
        if s.strip()
    }


def resolve_agent_policy(task: Task, db: Session) -> dict[str, Any]:
    """合并用户级默认 + 任务级覆盖,返回最终生效的策略

    优先级:任务级覆盖(task.params["_agent_policy"]) > 用户级默认
    (agent_policies 表) > 场景默认(安全类场景自动 allow_verify)>
    DEFAULT_AGENT_POLICY

    场景默认插在 DEFAULT 与用户级之间:安全类场景(如 code_security_audit)
    自动开启 allow_verify,但用户显式保存过策略(无论开关)优先于场景默认。

    任务的 user_id 可能为 None(匿名任务),此时只用默认值 + 场景默认 + 任务级覆盖。
    """
    defaults = dict(DEFAULT_AGENT_POLICY)

    # 场景默认:安全类任务自动开启 PoC 验证开关
    # (用户显式保存过策略会覆盖此值,任务级覆盖再覆盖用户级)
    # 场景 id 经别名解析,老任务存的旧 id(如 code_security_audit)同样命中
    from app.scenarios.base import resolve_scenario_id
    if resolve_scenario_id(task.scenario or "") in verify_default_scenarios():
        defaults["allow_verify"] = True

    # 加载用户级默认(若用户已登录且保存过智能体策略)
    if task.user_id is not None:
        try:
            from app.models.agent_policy import AgentPolicy
            policy_row = (
                db.query(AgentPolicy)
                .filter(AgentPolicy.user_id == task.user_id)
                .first()
            )
            if policy_row is not None:
                defaults.update(policy_row.to_dict())
        except Exception as e:
            logger.warning(
                f"[task={task.id}] 加载用户级 agent_policy 失败(用默认): {e}"
            )

    # 合并任务级覆盖(老任务 params 里可能残留 max_rounds,忽略该键)
    overrides = (task.params or {}).get("_agent_policy") or {}
    merged = {**defaults, **{
        k: v for k, v in overrides.items() if k != "max_rounds"
    }}

    # 把 executor_command_confirm_default 映射到 task.params._executor_command_confirm
    # (若任务级未显式设置 _executor_command_confirm),让 4 个 CLI agent wrapper 能读到
    # 优先级:task.params._executor_command_confirm > executor_command_confirm_default > "always_approve"
    if task.params is not None and not task.params.get("_executor_command_confirm"):
        default_mode = merged.get("executor_command_confirm_default", "always_approve")
        if default_mode in ("always_approve", "per_command"):
            task.params["_executor_command_confirm"] = default_mode

    return merged
