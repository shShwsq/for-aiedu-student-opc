"""智能体配置相关的 Pydantic 模型

用户可配置多种外部 CLI agent,任务创建时选择一种作为执行器。

安全约定(与 schemas/model_configs.py 一致):
- 响应(GET)绝不回传凭据原文,只返回 has_credentials 布尔,避免凭据经 HTTP 泄露
- 请求(PUT)中 secret 类型字段传空字符串表示"保留已存的值",非空表示"更新为新值"
"""
from pydantic import BaseModel, Field


# ============================================================
# Agent 类型元数据(前端渲染表单用)
# ============================================================


class CredentialField(BaseModel):
    """凭证字段定义(前端据此渲染输入框)"""

    key: str
    label: str
    # secret:敏感凭据(password 输入,加密存储,API 不回传原文)
    # text:非敏感配置(明文存储)
    # select:下拉选择(明文存储,如 provider_type;options 提供可选项)
    type: str
    required: bool = True
    placeholder: str = ""
    help_url: str | None = None
    help_text: str | None = None
    # select 类型专用:可选项列表
    options: list[dict[str, str]] | None = None
    # select 类型专用:默认值(未配置时使用)
    default: str | None = None
    # 条件显隐:仅当当前凭证满足此约束时前端才展示该字段并参与校验/提交。
    # 形如 {"auth_mode": "chatgpt"} 表示仅当 auth_mode=chatgpt 时可见。
    # None 表示始终可见。
    visible_when: dict[str, str] | None = None
    # secret 类型专用:是否用多行 textarea 输入(如粘贴 auth.json 这类较长凭据)
    multiline: bool = False


class AgentTypeMeta(BaseModel):
    """已注册的 agent 类型元数据(GET /agents/types 返回)"""

    agent_type: str
    display_name: str
    description: str = ""
    credential_fields: list[CredentialField] = Field(default_factory=list)
    help_url: str | None = None


# ============================================================
# 凭证写入请求(PUT /agents/configs/{agent_type})
# ============================================================


class CredentialValue(BaseModel):
    """单个凭证字段的值

    secret 类型:value 传空串表示保留已存值,非空表示更新
    text 类型:value 直接存储(可为空)
    """

    key: str
    # 上限放宽到 32768:ChatGPT 模式的 auth_json 是整段含 JWT 的 JSON,远超普通密钥长度
    value: str = Field(default="", max_length=32768)


class SaveAgentConfigRequest(BaseModel):
    """保存某 agent 配置请求"""

    credentials: list[CredentialValue] = Field(default_factory=list)
    is_active: bool = True


# ============================================================
# 配置读取响应(GET,不含凭据原文)
# ============================================================


class AgentConfigOut(BaseModel):
    """用户已配置的 agent(不含凭据原文)"""

    agent_type: str
    display_name: str
    is_active: bool
    # 是否已填写凭据(任一 secret 字段有值即为 True)
    has_credentials: bool

    model_config = {"from_attributes": True}


class AgentConfigDetailOut(AgentConfigOut):
    """单个 agent 配置详情(含各字段填写状态 + 非 secret 字段回显值)

    安全约定:
    - secret 字段:只返回 credential_status[key]=bool,绝不回传原文
    - text/select 字段:非敏感明文配置,在 credential_values 中回传已配置的值,
      便于前端编辑时回显(用户不用每次重新填 base_url/model/provider_type 等)
    """

    # 各凭证字段的填写状态:{"pat": true, ...}(所有字段)
    credential_status: dict[str, bool] = Field(default_factory=dict)
    # 非 secret(text/select)字段的已配置值:{"base_url": "https://...", "model": "..."}
    # secret 字段不在此 dict 中(绝不回传)
    credential_values: dict[str, str] = Field(default_factory=dict)


class AgentConfigListResponse(BaseModel):
    """用户已配置的 agent 列表"""

    configs: list[AgentConfigOut] = Field(default_factory=list)


# ============================================================
# 凭证测试响应(POST /agents/configs/{agent_type}/test)
# ============================================================


class AgentTestResponse(BaseModel):
    """测试 agent 凭证连通性的响应

    用于「智能体配置」页面的「测试连接」按钮。
    在临时沙箱内启动 CLI + ACP 握手,验证凭证有效性。
    """

    ok: bool
    message: str
