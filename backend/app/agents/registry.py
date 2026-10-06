"""智能体类型注册表

声明系统支持的外部 CLI agent 类型及其元数据(显示名、凭证字段、沙箱配置)。
新增一种 agent 只需在此注册,后端 API / executor / 前端表单均据此动态生成。

每种 agent 的元数据包含:
- display_name / description:前端展示
- credential_fields:该 agent 需要的凭证字段(前端据此渲染表单,后端据此校验)
- sandbox:沙箱内运行配置(bin / install_cmd / acp 启动参数)
- help_url:凭证获取指引链接

agent_type 字符串同时作为 task.executor 的值,executor_agent.get_executor()
据此从 registry 查找对应的 executor 实现。
"""
from __future__ import annotations

from typing import Any


# ============================================================
# 凭证字段类型定义
# ============================================================

# type='secret':敏感凭据(加密存储,前端用 password 输入,API 不回传原文)
# type='text':非敏感配置(明文存储,如 base_url)
# type='select':下拉选择(明文存储,如 provider_type;options 字段提供可选项)
CREDENTIAL_FIELD_SECRET = "secret"
CREDENTIAL_FIELD_TEXT = "text"
CREDENTIAL_FIELD_SELECT = "select"


# ============================================================
# Agent 类型注册表
# ============================================================

AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "qoder_cli": {
        "display_name": "Qoder CLI",
        "description": "沙箱内运行 Qoder CLI,通过 ACP 协议通信,模型由 Qoder 账号配额管理",
        "credential_fields": [
            {
                "key": "pat",
                "label": "Personal Access Token",
                "type": CREDENTIAL_FIELD_SECRET,
                "required": True,
                "placeholder": "粘贴你的 Qoder PAT",
                "help_url": "https://qoder.com/account/integrations",
                "help_text": "在 qoder.com/account/integrations 生成,用于沙箱内 Qoder CLI 认证",
            },
        ],
        "sandbox": {
            # 可执行文件名(沙箱内 PATH 查找或绝对路径)
            # 实际值从 config.py 的 QODER_CLI_BIN 读取,这里仅声明默认值
            "bin_config_key": "QODER_CLI_BIN",
            "bin_default": "qodercli",
            # 安装命令(沙箱内未检测到时执行)
            "install_cmd_config_key": "QODER_CLI_INSTALL_CMD",
            "install_cmd_default": "npm install -g @qoder-ai/qodercli",
            # ACP 启动参数(qodercli --acp --yolo)
            #   --acp:启动 ACP 协议服务
            #   --yolo:等价 --permission-mode bypass_permissions,跳过权限确认
            #     见 https://docs.qoder.com/cli/permissions
            "acp_args": ["--acp", "--yolo"],
            # PAT 注入用的环境变量名
            "credential_env": {"pat": "QODER_PERSONAL_ACCESS_TOKEN"},
        },
        "executor_module": "app.agents.qoder_cli_agent",
        "executor_func": "run_qoder_cli_agent",
    },

    # ========================================================
    # DeepSeek Harness CLI(dsh,开源 https://github.com/deepseek-ai/deepseek-harness)
    # 原生支持 ACP:`dsh --profile acp` 启动 stdio ACP 服务
    # 模型/思考强度经 session/set_config_option 在会话创建后设置
    # 权限模式经 DSH_PERMISSION_MODE 环境变量注入(danger-full-access=跳过审批)
    # ========================================================
    "deepseek_cli": {
        "display_name": "DeepSeek CLI",
        "description": (
            "沙箱内运行 DeepSeek Harness CLI(dsh,开源 https://github.com/deepseek-ai/deepseek-harness),"
            "通过 ACP 协议通信,模型经 session/set_config_option 设置,凭证经 DEEPSEEK_API_KEY 注入"
        ),
        "credential_fields": [
            {
                "key": "api_key",
                "label": "API Key",
                "type": CREDENTIAL_FIELD_SECRET,
                "required": True,
                "placeholder": "粘贴你的 DeepSeek API Key",
                "help_url": "https://platform.deepseek.com/api_keys",
                "help_text": "DeepSeek 开放平台 API Key,经 DEEPSEEK_API_KEY 环境变量注入",
            },
            {
                "key": "base_url",
                "label": "API Base URL",
                "type": CREDENTIAL_FIELD_TEXT,
                "required": False,
                "placeholder": "https://api.deepseek.com(留空用官方默认)",
                "help_text": "自定义 API 基址(留空用 DeepSeek 官方端点),经 DEEPSEEK_BASE_URL 环境变量注入",
            },
        ],
        "sandbox": {
            # dsh 可执行文件名(沙箱内 PATH 查找或绝对路径)
            "bin_config_key": "DEEPSEEK_CLI_BIN",
            "bin_default": "dsh",
            # 安装命令(沙箱内未检测到 dsh 时执行,需沙箱镜像有 Node.js)
            # 推荐在镜像中预装,避免每次任务都拉 npm 包
            "install_cmd_config_key": "DEEPSEEK_CLI_INSTALL_CMD",
            "install_cmd_default": "npm install -g @deepseek-ai/dsh",
            # ACP 启动参数:`dsh --profile acp` 启动随附的 stdio ACP 服务
            "acp_args": ["--profile", "acp"],
            # dsh ACP 模式不接受 --model / --reasoning-effort CLI 参数,
            # 模型/思考强度经 session/set_config_option 在 session/new 后设置
            "inject_cli_model_args": False,
            # 凭证 → 环境变量映射(DEEPSEEK_BASE_URL 可选)
            "credential_env": {
                "api_key": "DEEPSEEK_API_KEY",
                "base_url": "DEEPSEEK_BASE_URL",
            },
            # DSH_PERMISSION_MODE(权限模式)由 wrapper 的 credential_env_builder
            # 按 task.params._executor_command_confirm 动态注入,无法静态映射
        },
        "executor_module": "app.agents.deepseek_cli_agent",
        "executor_func": "run_deepseek_cli_agent",
    },

    # ========================================================
    # Codex CLI(OpenAI 官方,Apache-2.0 开源)
    # 不原生支持 ACP,通过 codex_bridge.py 翻译 codex exec --json JSONL → ACP
    # 支持 codex exec resume 实现多轮会话恢复
    # 模型/provider 经 ~/.codex/config.toml 配置,API Key 经 CODEX_API_KEY 环境变量注入
    # ========================================================
    "codex_cli": {
        "display_name": "Codex CLI",
        "description": (
            "OpenAI Codex CLI(Apache-2.0 开源)。\n"
            "支持自定义 OpenAI 兼容端点(base_url + wire_api),\n"
            "通过 codex exec --json + resume 实现多轮对话。\n"
            "需 Node.js >= 16,npm install -g @openai/codex。"
        ),
        "credential_fields": [
            {
                "key": "auth_mode",
                "label": "认证方式",
                "type": "select",
                "required": True,
                "default": "api_key",
                "options": [
                    {"value": "api_key", "label": "OpenAI API Key"},
                    {"value": "chatgpt", "label": "ChatGPT 账户(粘贴 auth.json)"},
                ],
                "description": (
                    "选择 Codex 的认证方式:\n"
                    "• OpenAI API Key:用 API Key 计费,可配自定义端点;\n"
                    "• ChatGPT 账户:用 ChatGPT 订阅额度(Plus/Pro/Team/Business/Enterprise/Edu),"
                    "在本机 codex login 后粘贴 ~/.codex/auth.json。"
                ),
            },
            {
                "key": "api_key",
                "label": "API Key",
                "type": "secret",
                "required": True,
                "placeholder": "sk-...",
                "help_url": "https://platform.openai.com/api-keys",
                "visible_when": {"auth_mode": "api_key"},
                "description": "OpenAI API Key 或自定义端点的 API Key",
            },
            {
                "key": "auth_json",
                "label": "ChatGPT auth.json",
                "type": "secret",
                "required": True,
                "multiline": True,
                "placeholder": "粘贴 ~/.codex/auth.json 的完整内容",
                "help_url": "https://platform.openai.com/codex",
                "help_text": (
                    "在本机执行 `codex login` 完成 ChatGPT 登录后,打开 ~/.codex/auth.json,"
                    "把整段 JSON 粘贴到此。SecondLook 加密存储,每次运行写入沙箱 CODEX_HOME,"
                    "并在运行后回写轮换后的新 token。"
                ),
                "visible_when": {"auth_mode": "chatgpt"},
                "description": (
                    "ChatGPT 账户 OAuth 凭证(auth.json)。\n"
                    "需含 tokens(id_token/access_token/refresh_token);Codex 会用 refresh_token 自动续期。"
                ),
            },
            {
                "key": "base_url",
                "label": "API Base URL",
                "type": "text",
                "required": False,
                "placeholder": "https://api.openai.com/v1(留空用默认)",
                "visible_when": {"auth_mode": "api_key"},
                "description": (
                    "自定义 API 端点(OpenAI 兼容)。\n"
                    "留空 = OpenAI 官方;填入 = 第三方中转/Ollama/vLLM 等。\n"
                    "必须含 /v1 后缀。"
                ),
            },
            {
                "key": "model",
                "label": "Model",
                "type": "text",
                "required": False,
                "placeholder": "gpt-5(留空用默认)",
                "description": "模型名(如 gpt-5、o4-mini 等)",
            },
            {
                "key": "wire_api",
                "label": "Wire API",
                "type": "select",
                "required": False,
                "default": "responses",
                "visible_when": {"auth_mode": "api_key"},
                "options": [
                    {"value": "responses", "label": "Responses API(Codex 唯一支持)"},
                ],
                "description": (
                    "通信协议(固定 Responses API)。\n"
                    "Codex 已彻底移除 Chat Completions 支持(WireApi enum 仅 Responses),"
                    "端点必须实现 /v1/responses 接口。\n"
                    "仅支持 /v1/chat/completions 的第三方中转/Ollama/vLLM 等无法使用 Codex,"
                    "需换支持 Responses API 的端点或加协议转换层。"
                ),
            },
        ],
        "sandbox": {
            "bin_config_key": "CODEX_CLI_BIN",
            "bin_default": "codex",
            "install_cmd_config_key": "CODEX_CLI_INSTALL_CMD",
            "install_cmd_default": "npm install -g @openai/codex",
            # codex_bridge.py 自己加 --json,这里只传额外参数
            # --dangerously-bypass-approvals-and-sandbox:跳过审批 + 关闭 Codex 内部沙箱(我们用 OpenSandbox)
            # --skip-git-repo-check:允许在非 git 目录运行
            "acp_args": [
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
            ],
            # API Key 经 CODEX_API_KEY 环境变量注入(config.toml 的 env_key 指向它)
            "credential_env": {"api_key": "CODEX_API_KEY"},
            # 使用 Codex 专用 bridge(非默认的 acp_bridge)
            "bridge_script": "codex_bridge",
            # codex_bridge 不需要 inject_cli_model_args(模型经 config.toml 配置)
            "inject_cli_model_args": False,
        },
        "executor_module": "app.agents.codex_cli_agent",
        "executor_func": "run_codex_cli_agent",
    },
}


# ============================================================
# 查询辅助
# ============================================================


def get_registered_types() -> list[str]:
    """返回所有已注册的 agent 类型标识"""
    return list(AGENT_REGISTRY.keys())


def get_agent_meta(agent_type: str) -> dict[str, Any] | None:
    """获取某 agent 类型的元数据,未注册返回 None"""
    return AGENT_REGISTRY.get(agent_type)


def is_registered(agent_type: str) -> bool:
    """判断 agent 类型是否已注册"""
    return agent_type in AGENT_REGISTRY


def get_executor_location(agent_type: str) -> tuple[str, str] | None:
    """获取 executor 的模块路径和函数名

    返回 (module_path, func_name),未注册返回 None。
    executor_agent 据此延迟导入对应的执行函数。
    """
    meta = AGENT_REGISTRY.get(agent_type)
    if not meta:
        return None
    return meta.get("executor_module", ""), meta.get("executor_func", "")


def get_credential_fields(agent_type: str) -> list[dict[str, Any]]:
    """获取某 agent 类型的凭证字段定义(前端表单渲染用)"""
    meta = AGENT_REGISTRY.get(agent_type)
    if not meta:
        return []
    return meta.get("credential_fields", [])


def get_sandbox_config(agent_type: str) -> dict[str, Any] | None:
    """获取某 agent 类型的沙箱运行配置"""
    meta = AGENT_REGISTRY.get(agent_type)
    if not meta:
        return None
    return meta.get("sandbox")
