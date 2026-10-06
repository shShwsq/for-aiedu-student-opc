/**
 * 外部 CLI agent 配置相关类型定义
 *
 * 对应后端 /agents 系列 API。后端采用动态多 agent 架构:
 * - /agents/types 返回所有已注册 agent 类型及其凭据字段定义(动态表单)
 * - /agents/configs 管理当前用户已配置的 agent(每个 agent_type 一条记录)
 *
 * 当前注册 qoder_cli / deepseek_cli / codex_cli,未来可扩展 aider / goose 等。
 */

/**
 * 凭据字段定义(后端动态返回,前端据此渲染表单)
 *
 * - secret 类型:password 输入框 + 眼睛切换显隐,加密存储且不回传明文
 * - text 类型:普通输入框,明文存储
 * - select 类型:下拉选择,明文存储(如 provider_type)
 */
export interface CredentialField {
  /** 字段 key,如 'pat' */
  key: string
  /** 展示标签,如 'Personal Access Token' */
  label: string
  /** 输入类型:secret=密码输入+加密存储;text=明文;select=下拉选择 */
  type: 'secret' | 'text' | 'select'
  /** 是否必填 */
  required: boolean
  /** 占位提示(text/secret) */
  placeholder: string
  /** 帮助链接(可选) */
  help_url: string | null
  /** 帮助文本(可选) */
  help_text: string | null
  /** select 类型的可选项 */
  options?: Array<{ value: string; label: string }>
  /** select 类型的默认值(未配置时使用) */
  default?: string
  /**
   * 条件显隐:仅当当前凭证满足此约束时,前端才展示该字段并参与校验/提交。
   * 形如 { auth_mode: 'chatgpt' } 表示仅当 auth_mode=chatgpt 时可见;缺省则始终可见。
   */
  visible_when?: Record<string, string> | null
  /** secret 类型专用:是否用多行 textarea 输入(如粘贴 auth.json 这类较长凭据) */
  multiline?: boolean
}

/** agent 类型元数据(GET /agents/types) */
export interface AgentTypeMeta {
  /** agent 类型标识,如 'qoder_cli' */
  agent_type: string
  /** 展示名称,如 'Qoder CLI' */
  display_name: string
  /** 描述说明 */
  description: string
  /** 凭据字段定义(动态表单) */
  credential_fields: CredentialField[]
  /** 帮助链接(可选) */
  help_url: string | null
}

/** agent 配置列表项(GET /agents/configs) */
export interface AgentConfigOut {
  /** agent 类型标识 */
  agent_type: string
  /** 展示名称 */
  display_name: string
  /** 是否启用 */
  is_active: boolean
  /** 是否已填写凭据 */
  has_credentials: boolean
}

/** agent 配置详情(GET /agents/configs/{agent_type}、PUT 响应) */
export interface AgentConfigDetailOut extends AgentConfigOut {
  /**
   * 各凭据字段的填写状态
   *
   * key 为字段名,value 为是否已设置。如 { "pat": true }
   */
  credential_status: Record<string, boolean>
  /**
   * 非 secret(text/select)字段的已配置值,供编辑时回显
   *
   * secret 字段不在此 dict 中(绝不回传原文)。如 { "base_url": "https://..." }
   */
  credential_values: Record<string, string>
}

/** 单个凭据值(保存时提交) */
export interface CredentialValue {
  /** 字段 key */
  key: string
  /**
   * 字段值
   *
   * secret 字段:传空串=保留已存值,传非空=更新;
   * text 字段:直接存。
   */
  value: string
}

/** 保存 agent 配置请求(PUT /agents/configs/{agent_type} body) */
export interface SaveAgentConfigRequest {
  /** 凭据列表 */
  credentials: CredentialValue[]
  /** 是否启用 */
  is_active: boolean
}

/** agent 配置列表响应(GET /agents/configs、DELETE 后响应) */
export interface AgentConfigListResponse {
  configs: AgentConfigOut[]
}

/** 测试 agent 凭证连通性的响应(POST /agents/configs/{agent_type}/test) */
export interface AgentTestResult {
  /** 是否通过 */
  ok: boolean
  /** 人类可读的结果说明(成功/失败原因) */
  message: string
}
