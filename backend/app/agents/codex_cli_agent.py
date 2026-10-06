"""Codex CLI 执行器:OpenAI Codex CLI 集成(通过 codex exec --json + ACP 翻译 bridge)

Codex CLI 不原生支持 ACP 协议,但提供:
- `codex exec --json`:非交互模式,输出 JSONL 流式事件
- `codex exec resume <thread_id>`:恢复之前的会话(多轮对话)
- `~/.codex/config.toml`:模型/provider/审批策略配置

集成方式:
1. registry 注册 codex_cli,指定 bridge_script="codex_bridge"(非默认的 acp_bridge)
2. codex_bridge.py 在沙箱内运行,将 ACP JSON-RPC 翻译为 codex exec 调用
3. pre_bridge_hook 向沙箱写入 ~/.codex/config.toml(模型/provider/base_url 配置)
4. 凭证按认证方式注入:
   - api_key:API Key 经环境变量 CODEX_API_KEY 注入(config.toml 的 env_key 指向它)
   - chatgpt:把用户粘贴的 ~/.codex/auth.json 写入 CODEX_HOME,codex 走 ChatGPT 后端;
     运行后由 post_bridge_hook 读回轮换后的 auth.json 并更新到用户配置

凭证字段:
- auth_mode(select):认证方式,api_key(默认)/ chatgpt
- api_key(secret,仅 api_key 模式):API Key(注入到 CODEX_API_KEY 环境变量)
- auth_json(secret,仅 chatgpt 模式):ChatGPT OAuth 凭证(~/.codex/auth.json 全文)
- base_url(text,可选,仅 api_key 模式):自定义 API 端点(留空用 OpenAI 官方)
- model(text,可选):模型名(留空用 gpt-5)
- wire_api(select,仅 api_key 模式):通信协议(固定 Responses API,chat 已被 codex 移除)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.models.task import Task
from app.models.user_agent_config import UserAgentConfig
from app.security import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)


# ============================================================
# Codex config.toml 配置
# ============================================================

# 默认模型(留空时使用)
_DEFAULT_MODEL = "gpt-5"

# wire_api 固定为 responses:Codex 已彻底移除 chat 支持
# (WireApi enum 仅 Responses 一个 variant,"chat" 会导致 config.toml 加载失败)
# 用户端点必须支持 /v1/responses(OpenAI Responses API)


def _validate_auth_json(raw: str) -> str:
    """校验用户粘贴的 ChatGPT auth.json,返回规范化文本;非法则抛可读错误。"""
    text = (raw or "").strip()
    if not text:
        raise RuntimeError(
            "ChatGPT 模式需要 auth.json:请在「智能体配置」粘贴 ~/.codex/auth.json 全文"
        )
    try:
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError(f"auth.json 不是合法 JSON: {e}") from e
    if not isinstance(obj, dict):
        raise RuntimeError("auth.json 必须是 JSON 对象")
    if not (
        obj.get("tokens")
        or obj.get("auth_mode")
        or obj.get("personal_access_token")
        or obj.get("OPENAI_API_KEY")
    ):
        logger.warning(
            "[codex_cli] auth.json 未检测到 tokens/auth_mode 字段,可能不是有效的 ChatGPT 登录态"
        )
    return text


def _codex_write_configs(
    session, config_toml: str, auth_json_text: str | None
) -> dict[str, str] | None:
    """写入 codex 配置文件到 CODEX_HOME。

    local: 写 <local_dir>/.codex/(write_file 自动建父目录),返回 CODEX_HOME 环境变量;
    sandbox: 先 mkdir ~/.codex 再写(容器独立 home),返回 None。
    auth_json_text 非 None 时一并写 auth.json(仅 chatgpt 模式)。
    """
    if getattr(session, "mode", "") == "local":
        session.write_file(".codex/config.toml", config_toml)
        if auth_json_text is not None:
            session.write_file(".codex/auth.json", auth_json_text)
        return {"CODEX_HOME": str(session.local_dir / ".codex")}

    session.run_command("mkdir -p ~/.codex", timeout=5)
    session.write_file("~/.codex/config.toml", config_toml)
    if auth_json_text is not None:
        session.write_file("~/.codex/auth.json", auth_json_text)
    return None


def _codex_pre_bridge_hook(
    session, credentials: dict[str, str], agent_type: str, task: Task | None = None
) -> dict[str, str] | None:
    """bridge 启动前:按认证方式写入 codex 的 config.toml(+ chatgpt 模式的 auth.json)

    两种认证方式(credentials["auth_mode"]):
    - api_key(默认):写含模型/provider 的 config.toml;API Key 经 CODEX_API_KEY 环境变量
      注入(config.toml 的 env_key 指向它)。base_url 留空则不写自定义 provider(用官方)。
    - chatgpt:写精简 config.toml(仅 model + approval_policy + sandbox_mode,不写 provider/env_key,
      让 Codex 用内置 provider 走 ChatGPT 后端),并写 CODEX_HOME/auth.json(用户粘贴的 OAuth 凭证)。

    sandbox 模式:写沙箱内 ~/.codex(容器独立 home)。
    local 模式:写 local_dir/.codex,并返回 CODEX_HOME 指向它(codex 支持 CODEX_HOME 重定向,
    确保 CLI 完全不碰宿主机真实 ~/.codex)。

    命令确认模式(task.params._executor_command_confirm):codex exec --json 为非交互模式,
    approval_policy 恒为 "never";per_command 不支持,降级为 always_approve 并警告。

    返回:需额外注入 bridge 进程的环境变量(local 模式 CODEX_HOME),sandbox 返回 None。
    """
    auth_mode = (credentials.get("auth_mode") or "api_key").strip()
    model = (credentials.get("model") or "").strip() or _DEFAULT_MODEL

    # 读命令确认模式:task=None(测试连接)时默认 always_approve
    approval_mode = "always_approve"
    if task and task.params:
        approval_mode = task.params.get("_executor_command_confirm", "always_approve")
    # codex exec --json 是非交互模式,approval_policy 必须为 "never"(生成的 toml 恒写 never);
    # per_command 无法暂停等待审批,降级并警告
    if approval_mode == "per_command":
        logger.warning(
            "[codex_cli] per_command 模式不被 codex exec --json 支持(非交互模式),"
            "已降级为 always_approve。如需命令确认,请改用 qoder_cli/deepseek_cli。"
        )
        approval_mode = "always_approve"

    # ---- ChatGPT 账户模式:精简 config.toml + auth.json 注入 ----
    if auth_mode == "chatgpt":
        config_toml = f"""# Codex CLI 配置(由 SecondLook 自动生成,ChatGPT 账户模式)
# 模型配置
model = "{model}"

# 审批策略:never = 从不审批(非交互模式必须)
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"
"""
        auth_json_text = _validate_auth_json(credentials.get("auth_json", ""))
        envs = _codex_write_configs(session, config_toml, auth_json_text)
        logger.info(
            "[codex_cli] config.toml + auth.json 已写入(mode=chatgpt, model=%s, "
            "CODEX_HOME=%s, approval_mode=%s)",
            model,
            (envs or {}).get("CODEX_HOME", "~/.codex(sandbox)"),
            approval_mode,
        )
        return envs

    # ---- OpenAI API Key 模式(默认):保持原有 provider/env_key 逻辑 ----
    base_url = (credentials.get("base_url") or "").strip()
    # wire_api 固定 responses(不读用户配置):codex 已移除 chat 支持(WireApi enum
    # 仅 Responses 一个 variant),用户凭证里若存了旧的 "chat" 会导致 config.toml
    # 加载失败。端点必须支持 /v1/responses(OpenAI Responses API)。
    wire_api = "responses"

    # base_url 留空时不写 model_provider,让 Codex 用默认 OpenAI provider
    if base_url:
        config_toml = f"""# Codex CLI 配置(由 SecondLook 自动生成)
# 模型配置
model = "{model}"
model_provider = "secondlook"

# 审批策略:never = 从不审批(非交互模式必须)
# 合法值:untrusted / on-failure / on-request / granular / never
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"

# 自定义 model provider
[model_providers.secondlook]
name = "SecondLook Custom Provider"
base_url = "{base_url}"
wire_api = "{wire_api}"
env_key = "CODEX_API_KEY"
"""
    else:
        config_toml = f"""# Codex CLI 配置(由 SecondLook 自动生成)
# 模型配置
model = "{model}"

# 审批策略:never = 从不审批(非交互模式必须)
# 合法值:untrusted / on-failure / on-request / granular / never
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"
"""

    envs = _codex_write_configs(session, config_toml, None)
    logger.info(
        f"[codex_cli] config.toml 已写入(mode=api_key, model={model}, "
        f"base_url={'自定义' if base_url else 'OpenAI默认'}, wire_api={wire_api}, "
        f"approval_mode={approval_mode})"
    )
    return envs


def _codex_post_bridge_hook(
    session, credentials: dict[str, str], agent_type: str, db: Session, user_id
) -> None:
    """bridge 运行后:chatgpt 模式下读回轮换后的 auth.json,更新到用户配置。

    Codex 用 ChatGPT 账户时会用 refresh_token 换新 access_token 并就地重写 auth.json;
    而沙箱/本地环境是一次性的,不回捞会导致下次注入的旧 token 逐步失效。此 hook 在运行
    结束、沙箱关闭前把最新 auth.json 合并写回 UserAgentConfig(仅替换 auth_json 字段)。
    尽力而为:任何读取/解析/DB 异常都只记日志,不影响主流程。非 chatgpt 模式直接返回。
    """
    if (credentials.get("auth_mode") or "api_key").strip() != "chatgpt":
        return

    is_local = getattr(session, "mode", "") == "local"
    path = ".codex/auth.json" if is_local else "~/.codex/auth.json"
    try:
        text = (session.read_file(path) or "").strip()
    except Exception as e:
        logger.info(f"[codex_cli] auth.json 回写跳过(读取失败): {e}")
        return
    if not text:
        return
    try:
        obj = json.loads(text)
    except Exception:
        logger.warning("[codex_cli] 运行后 auth.json 非法 JSON,跳过回写")
        return
    if not isinstance(obj, dict) or not obj.get("tokens"):
        logger.info("[codex_cli] 运行后 auth.json 无 tokens,跳过回写")
        return
    new_auth = json.dumps(obj, ensure_ascii=False)

    row = (
        db.query(UserAgentConfig)
        .filter(
            UserAgentConfig.user_id == user_id,
            UserAgentConfig.agent_type == agent_type,
        )
        .first()
    )
    if row is None or not row.credentials_encrypted:
        return
    try:
        creds = json.loads(decrypt_secret(row.credentials_encrypted))
    except Exception as e:
        logger.warning(f"[codex_cli] 回写前解密失败,跳过: {e}")
        return
    if not isinstance(creds, dict) or creds.get("auth_json") == new_auth:
        return  # 无变化,不写库
    creds["auth_json"] = new_auth
    row.credentials_encrypted = encrypt_secret(json.dumps(creds, ensure_ascii=False))
    db.commit()
    logger.info("[codex_cli] 已回写轮换后的 ChatGPT auth.json")


# ============================================================
# 执行器入口
# ============================================================


def run_codex_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = "codex_cli",
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 Codex CLI 执行器

    通过 codex_bridge.py(ACP 翻译层)与 codex exec --json 交互:
    1. pre_bridge_hook 写入 ~/.codex/config.toml(chatgpt 模式另写 auth.json)
    2. codex_bridge.py 启动,监听 HTTP 端口
    3. ACPClient 发送 initialize → session/new → session/prompt
    4. codex_bridge.py 收到 session/prompt 后运行 codex exec --json
    5. JSONL 事件翻译为 ACP 通知,经 SSE 流式返回
    6. 首次调用提取 thread_id,后续用 codex exec resume 恢复会话
    7. post_bridge_hook(chatgpt 模式):读回轮换后的 auth.json 并回写用户配置
    """
    return run_acp_agent(
        task=task,
        db=db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type=agent_type,
        pre_bridge_hook=_codex_pre_bridge_hook,
        post_bridge_hook=_codex_post_bridge_hook,
        # Codex 不需要 credential_env_builder:
        # api_key 模式的 API Key 经 config.toml 的 env_key="CODEX_API_KEY" 指定,
        # registry 的 credential_env 映射 api_key → CODEX_API_KEY 即可;
        # chatgpt 模式无 api_key 值,凭证经 auth.json 注入,不依赖环境变量
    )


def test_credential_streaming(db: Session, user_id, agent_type: str):
    """流式测试 Codex CLI 凭证连通性

    流程:
    1. 创建临时沙箱
    2. 写入 ~/.codex/config.toml(chatgpt 模式另写 auth.json)
    3. 启动 codex_bridge.py
    4. 发送测试 prompt("你好,请简短回复确认连接正常")
    5. 流式返回 stage/thinking/content/done 事件
    6. post_bridge_hook(chatgpt 模式):回写轮换后的 auth.json
    7. 销毁临时沙箱
    """
    return _base_test_streaming(
        db=db,
        user_id=user_id,
        agent_type=agent_type,
        pre_bridge_hook=_codex_pre_bridge_hook,
        post_bridge_hook=_codex_post_bridge_hook,
    )
