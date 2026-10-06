"""deepseek_cli_agent:基于 DeepSeek Harness CLI(dsh)+ ACP 协议的 AI助手(薄封装)

在沙箱内启动 DeepSeek Harness CLI(开源 https://github.com/deepseek-ai/deepseek-harness)
的 ACP 服务(dsh --profile acp),通过 HTTP 桥接(acp_bridge.py)与后端通信。

共享基础设施(ACPClient / _ACPCollector / _ACPRecorder / bridge 管理 / 凭证加载 /
事件翻译 / plan 提取)在 acp_base.py 中实现,本模块仅包含 dsh 特有逻辑:

与 Qoder CLI 的关键差异:
1. ACP 启动命令:`dsh --profile acp`(随附的 stdio ACP 服务 profile)
2. 权限模式:经 DSH_PERMISSION_MODE 环境变量在 bridge 启动前注入
   (dsh 在 profile 组装时读取并应用审批策略,无 --yolo 启动参数)
   - always_approve(默认):DSH_PERMISSION_MODE=danger-full-access,审批策略 never,
     CLI 自主执行所有命令
   - per_command:DSH_PERMISSION_MODE=workspace-write(默认值,审批策略 ask),
     危险命令发 ACP request_permission → 前端确认
3. 模型选择:无 --model CLI 参数,经 session/set_config_option 在
   session/new 后设置(configId='model' / 'reasoning_effort');
   不设置时用 dsh acp profile 默认模型(deepseek-v4-flash)
4. 凭证注入:DEEPSEEK_API_KEY(必填)+ DEEPSEEK_BASE_URL(可选,自部署/代理端点)

认证说明:
  dsh 从环境变量读取凭证(与 e2e 测试同一套约定):
  - DEEPSEEK_API_KEY:API Key(必填)
  - DEEPSEEK_BASE_URL:API 基址(选填,默认 DeepSeek 官方端点)
  凭证经 bridge 进程环境变量注入,CLI 子进程继承,无需命令行明文传递。
"""
import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    ACPClient,
    _build_credential_envs,
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# agent 类型标识(与 registry 中的 key 对齐)
AGENT_TYPE = "deepseek_cli"

# dsh 权限模式环境变量(always_approve 时注入 danger-full-access 跳过审批)
_DSH_PERMISSION_MODE_ENV = "DSH_PERMISSION_MODE"
_DSH_MODE_FULL_ACCESS = "danger-full-access"   # 审批策略 never(等价 yolo)
_DSH_MODE_WORKSPACE_WRITE = "workspace-write"   # 审批策略 ask(默认,弹窗确认)


# ============================================================
# dsh 特有:权限模式环境变量注入(credential_env_builder)
# ============================================================


def _deepseek_credential_env_builder(
    credentials: dict[str, str], task: Task | None = None
) -> dict[str, str]:
    """构建 dsh 凭证环境变量 + 按命令确认模式注入 DSH_PERMISSION_MODE

    静态映射部分(api_key → DEEPSEEK_API_KEY,base_url → DEEPSEEK_BASE_URL)
    复用 registry 的 _build_credential_envs;
    DSH_PERMISSION_MODE 是任务级开关(非凭证),在此动态追加:

    命令确认模式(task.params._executor_command_confirm):
    - always_approve(默认 / task=None 测试场景):注入 danger-full-access,
      dsh 审批策略 never,CLI 自主执行所有命令(不发 request_permission)
    - per_command:不注入(用 dsh 默认 workspace-write),审批策略 ask,
      危险命令发 ACP request_permission → 前端确认弹窗

    注意:DSH_PERMISSION_MODE 参与 bridge 复用指纹(环境变量 dict 全量参与),
    任务级切换确认模式会重建 bridge,不会复用旧权限模式的进程。
    """
    # 静态凭证映射(复用 registry credential_env)
    envs = dict(_build_credential_envs(credentials, AGENT_TYPE))
    if not envs.get("DEEPSEEK_API_KEY"):
        logger.warning("[deepseek_cli] 凭证缺少 api_key,DEEPSEEK_API_KEY 将为空")

    # 读命令确认模式:task=None(测试连接)时默认 always_approve
    approval_mode = "always_approve"
    if task and task.params:
        approval_mode = task.params.get("_executor_command_confirm", "always_approve")

    if approval_mode == "per_command":
        # 不注入:用 dsh 默认 workspace-write(审批 ask),危险命令弹窗确认
        logger.info("[deepseek_cli] per_command 模式:保持 workspace-write,危险命令将弹窗确认")
    else:
        # always_approve:注入 danger-full-access 跳过权限审批
        envs[_DSH_PERMISSION_MODE_ENV] = _DSH_MODE_FULL_ACCESS
        logger.info("[deepseek_cli] 已注入 DSH_PERMISSION_MODE=danger-full-access(跳过权限审批)")

    return envs


# ============================================================
# dsh 特有:session/new 后的配置设置(post_session_setup)
# ============================================================


def _deepseek_post_session_setup(
    client: ACPClient, session_id: str, task: Task | None
) -> None:
    """dsh 特有:session/new 后经 set_config_option 设置模型/思考强度

    dsh 的 ACP 模式无 --model / --reasoning-effort 启动参数,
    通过 ACP session/set_config_option 在 session/new 后设置:
    - model:模型名(若 task.params.model 有值;不设置用 profile 默认模型)
    - reasoning_effort:思考强度(若 task.params.reasoning_effort 有值,
      且当前模型声明了该选项)

    权限模式不在此设置 —— DSH_PERMISSION_MODE 是进程级环境变量,
    在 bridge 启动前经 credential_env_builder 注入。
    """
    if not (task and task.params):
        return

    model = task.params.get("model")
    if model:
        try:
            client.set_config_option(session_id, "model", str(model))
            logger.info(f"[deepseek_cli] 已设置 model={model}")
        except Exception as e:
            # 模型设置失败不阻塞主流程(dsh 会用 profile 默认模型继续)
            logger.warning(f"[deepseek_cli] 设置 model={model} 失败(忽略): {e}")

    effort = task.params.get("reasoning_effort")
    if effort:
        try:
            client.set_config_option(session_id, "reasoning_effort", str(effort))
            logger.info(f"[deepseek_cli] 已设置 reasoning_effort={effort}")
        except Exception as e:
            # 思考强度设置失败不阻塞主流程(模型可能不支持该 effort 值)
            logger.warning(f"[deepseek_cli] 设置 reasoning_effort={effort} 失败(忽略): {e}")


# ============================================================
# 主入口:run_deepseek_cli_agent(薄封装)
# ============================================================


def run_deepseek_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = AGENT_TYPE,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 DeepSeek Harness CLI 执行器

    与 run_react_agent 签名对齐(不含 client 参数,dsh 自带模型配置)。

    dsh 特有:
    - bridge 启动前经 credential_env_builder 注入 DSH_PERMISSION_MODE
      (always_approve=danger-full-access / per_command=workspace-write)
    - session/new 后调 set_config_option(model / reasoning_effort)
    - 不支持 --model / --yolo 等 CLI 参数

    返回:(results, summary, final_plan)
    """
    return run_acp_agent(
        task, db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type=agent_type,
        post_session_setup=_deepseek_post_session_setup,
        credential_env_builder=_deepseek_credential_env_builder,
    )


# ============================================================
# 凭证测试:用于「智能体配置」页面的测试连接按钮
# ============================================================


def test_credential(db: Session, user_id, agent_type: str = AGENT_TYPE) -> tuple[bool, str]:
    """测试 dsh 凭证是否可用(非流式版,收集 streaming 结果)

    在临时沙箱内启动 ACP bridge,依次验证:
    1. 沙箱镜像含 dsh CLI(无则尝试 npm install)
    2. API Key 有效(DEEPSEEK_API_KEY 环境变量注入后 dsh 可调 LLM)
    3. 模型可响应(发送「你好」prompt,确认 LLM 正常工作)

    返回 (ok, message)。
    """
    for event in _base_test_streaming(
        db, user_id, agent_type,
        post_session_setup=_deepseek_post_session_setup,
        credential_env_builder=_deepseek_credential_env_builder,
        # dsh 无需 test_acp_args:模型/权限均经环境变量与会话配置注入
    ):
        if event.get("type") == "done":
            data = event.get("data", {})
            return data.get("ok", False), data.get("message", "")
        if event.get("type") == "error":
            data = event.get("data", {})
            return False, data.get("message", "测试异常")
    return False, "测试未返回结果"


def test_credential_streaming(
    db: Session, user_id, agent_type: str = AGENT_TYPE
) -> Generator[dict, None, None]:
    """流式版测试凭证:yield SSE 事件 dict(供路由层格式化为 SSE)

    dsh 特有:bridge 启动前注入 DSH_PERMISSION_MODE=danger-full-access
    (task=None 测试场景默认 always_approve),模型用 profile 默认。
    """
    yield from _base_test_streaming(
        db, user_id, agent_type,
        post_session_setup=_deepseek_post_session_setup,
        credential_env_builder=_deepseek_credential_env_builder,
    )
