"""qoder_cli_agent:基于 Qoder CLI + ACP 协议的 AI助手(薄封装)

在沙箱内启动 Qoder CLI 的 ACP(Agent Client Protocol)服务,通过 HTTP 桥接
(acp_bridge.py)与后端通信。模型配置由 Qoder 账号配额管理,后端不直接管理
LLM 调用 —— Qoder CLI 内部自主完成 ReAct 循环(思考→工具→观察)。

共享基础设施(ACPClient / _ACPCollector / _ACPRecorder / bridge 管理 / 凭证加载 /
事件翻译 / plan 提取)在 acp_base.py 中实现,本模块仅包含 Qoder 特有逻辑:
- --yolo 启动参数(经 registry acp_args 配置,无需 post_session_setup)
- print 模式快速 PAT 诊断(ACP 认证失败时的补充诊断)
- 测试连接用 DeepSeek-V4-Flash + low 思考强度(最小化 credits 消耗)

工作流程详见 acp_base.py 文档。
"""
import logging
from collections.abc import Generator

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    ACPClient,
    _build_credential_envs,
    _ensure_cli_env,
    _get_bin,
    _load_credentials,
    _start_acp_bridge,
    _stop_acp_bridge,
    _wait_for_bridge_ready,
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.config import settings
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# agent 类型标识(与 registry 中的 key 对齐)
AGENT_TYPE = "qoder_cli"

# 测试连接时强制使用的最便宜模型配置(最小化 credits 消耗)
# DeepSeek-V4-Flash:Flash 系列轻量模型,credits 最低
# low:最少思考强度,几乎不思考
# 注:文档 https://docs.qoder.cn/cli/model 称可用 --model efficient 切换经济分级,
# 但实测 CLI 报 "Invalid model 'efficient'",分级模型只能通过 TUI /model 切换,
# --model 仅接受具体模型名(Auto/Qwen3.x-*/DeepSeek-V4-*/GLM-5.2 等)
_TEST_ACP_ARGS = ["--model", "Qwen3.8-Flash", "--reasoning-effort", "low"]


# ============================================================
# print 模式 PAT 快速诊断(Qoder 特有)
# ============================================================


def _quick_verify_pat_print(
    session,
    credential_envs: dict[str, str],
    agent_type: str = AGENT_TYPE,
    timeout: int = 15,
) -> tuple[bool, str]:
    """用 print 模式快速验证 PAT 有效性(ACP 认证超时时的补充诊断)

    通过 `qoderclicn -p "OK"` 直接发一个最简 prompt:
    - 几秒内返回文本 → PAT 有效,问题在 ACP 认证流程
    - 报认证错误      → PAT 无效/过期
    - 超时/卡住       → 网络问题或 CLI 异常

    注意:print 模式会消耗少量 LLM credits,仅作为 ACP 认证失败后的
    补充诊断调用,不在主流程中执行。

    返回 (ok, detail):ok=True 表示 PAT 验证通过,detail 含 CLI 输出摘要。
    """
    cli_bin = _get_bin(agent_type)
    logger.info(
        f"[qoder_cli_test] PAT 快速诊断(print 模式): "
        f"cmd={cli_bin} -p 'OK', timeout={timeout}s (会消耗少量 credits)"
    )

    try:
        if getattr(session, "mode", "") == "local":
            # local 模式:argv 启动 + envs 注入(不走 shell;
            # export/单引号在 Windows cmd.exe 不可用)
            # 注:仅取 stdout,CLI 的 stderr 诊断信息不在此合并
            output = session.run_command_argv(
                [cli_bin, "-p", "OK"],
                envs=credential_envs,
                timeout=timeout,
            )
        else:
            # sandbox 模式:环境变量通过 env 内联 export 注入
            env_exports = " ".join(f'{k}="{v}"' for k, v in credential_envs.items())
            cmd = f"{env_exports} {cli_bin} -p 'OK' 2>&1"
            output = session.run_command(cmd, timeout=timeout, check=False)
        output = (output or "").strip()
        logger.info(
            f"[qoder_cli_test] print 模式返回: 输出长度={len(output)}, "
            f"内容预览={output[:300]}"
        )
        if output and "error" not in output.lower() and "unauthorized" not in output.lower():
            return True, output[:500]
        return False, output[:500] if output else "无输出"
    except Exception as e:
        err_str = str(e)
        logger.warning(f"[qoder_cli_test] print 模式异常: {err_str[:300]}")
        if "timeout" in err_str.lower() or "timed out" in err_str.lower():
            return False, f"print 模式超时({timeout}s),CLI 可能挂起(网络/PAT 问题)"
        return False, f"print 模式执行异常: {err_str[:300]}"


# ============================================================
# 主入口:run_qoder_cli_agent(薄封装)
# ============================================================


def run_qoder_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list | None = None,
    agent_type: str = AGENT_TYPE,
) -> tuple[list, str, list]:
    """跑一轮 Qoder CLI 执行器

    与 run_react_agent 签名对齐(不含 client 参数,Qoder CLI 自带模型配置)。
    agent_type 固定为 AGENT_TYPE("qoder_cli"),由 executor_agent.ExternalCLIAgent 注入。

    Qoder 特有:--yolo 已在 registry acp_args 中,无需 post_session_setup。

    返回:(results, summary, final_plan)
    """
    return run_acp_agent(
        task, db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type=agent_type,
        # Qoder 无需 post_session_setup:--yolo 在 acp_args 中,模型经 --model CLI 参数设置
    )


# ============================================================
# 凭证测试:用于「智能体配置」页面的测试连接按钮
# ============================================================


def test_credential(db: Session, user_id, agent_type: str = AGENT_TYPE) -> tuple[bool, str]:
    """测试 Qoder CLI 凭证是否可用(非流式版,收集 streaming 结果)

    在临时沙箱内启动 ACP bridge,依次验证:
    1. 沙箱镜像含 CLI(qodercli / qoderclicn)
    2. PAT 有效(Qoder 服务端认证通过,ACP initialize + authenticate)
    3. 模型可响应(发送「你好」prompt,确认 LLM 正常工作,消耗少量 credits)

    返回 (ok, message)。
    """
    for event in _base_test_streaming(
        db, user_id, agent_type,
        test_acp_args=_TEST_ACP_ARGS,
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

    Qoder 特有:测试时强制用 DeepSeek-V4-Flash + low 思考强度,最小化 credits 消耗。
    """
    yield from _base_test_streaming(
        db, user_id, agent_type,
        test_acp_args=_TEST_ACP_ARGS,
    )
