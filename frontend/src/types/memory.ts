/**
 * 长期记忆管理相关类型定义
 *
 * 对应后端 /memory 系列 API(app/routers/memory.py):
 * - User Profile (1:1,结构化字段 + 自由文本):影响 agent2 评判标准与 checklist 生成
 * - 全局长期记忆(1:1,自由文本):跨项目通用经验,注入 agent2
 * - 分项目记忆(1:N,按 repo_url 聚合):注入 agent1,影响审计方向
 *
 * 三类记忆均可由用户手动编辑;分项目记忆还会在任务完成时由 agent 自动归纳写入。
 */

/**
 * User Profile (1:1)
 *
 * user_profile 为自由文本 Markdown(用户在记忆管理页编辑),注入 agent2,
 * 影响评判标准与 checklist 生成(≤ 2000 字符)。
 */
export interface UserPreferenceOut {
  /** 自由文本 Markdown(≤ 2000 字符) */
  user_profile: string
  /** agent 策略配置(agent2 启停、验证权限等),null=未配置(用系统默认) */
  agent_policy: SaveAgentPolicyRequest | null
  /** 任务完成后是否自动生成练习题 draft(默认开;产出仍需预览确认) */
  auto_generate_practice: boolean
  /** 出题前沙箱已清理时是否重新 clone 恢复工作区(默认关) */
  restore_workspace_for_practice: boolean
  /** 用户级默认出题模型(UserLLMConfig 配置 id;null=未设置,回退任务级/env 默认) */
  default_llm_config_id: string | null
  /** 始终用默认出题模型(忽略任务自带模型配置;默认关) */
  force_default_llm: boolean
  /** 出题思考模式覆盖(follow=跟随模型配置/on=强制开/off=强制关,默认 follow) */
  thinking_mode_for_practice: PracticeThinkingMode
  /** 记忆生成设置(后端总是返回;未配置时为预置默认) */
  memory_settings: MemorySettingsOut
  /** 最后更新时间(ISO 字符串,未配置时为 null) */
  updated_at: string | null
}

/** 出题思考模式(覆盖出题模型配置的思考开关) */
export type PracticeThinkingMode = 'follow' | 'on' | 'off'

/** 记忆结构化预设模式 */
export type MemoryStructureMode = 'structured' | 'freeform'

/** 结构化类别定义(标题 + 描述;描述用于指导归纳模型归类,可空) */
export interface MemoryCategoryDef {
  title: string
  description: string
}

/** 保存 User Profile 请求(PUT /memory/preferences body) */
export interface SaveUserPreferenceRequest {
  user_profile: string
}

/** 保存练习设置请求(PUT /memory/preferences/practice body)
 *
 * restore_workspace_for_practice / default_llm_config_id /
 * force_default_llm / thinking_mode_for_practice 可选,不传表示本次不修改
 * (后端 None 语义);default_llm_config_id 传空串表示清空(回退任务级/env 默认)。
 * (learning_topic 已移除:出题主题改为按发现内容自动匹配)
 */
export interface SavePracticeSettingsRequest {
  auto_generate_practice: boolean
  restore_workspace_for_practice?: boolean
  default_llm_config_id?: string | null
  force_default_llm?: boolean
  thinking_mode_for_practice?: PracticeThinkingMode
}

/**
 * agent 策略配置(PUT /memory/preferences/agent_policy body)
 *
 * 作为用户级默认值,任务级可通过 task.params["_agent_policy"] 覆盖。
 */
export interface SaveAgentPolicyRequest {
  /** 是否启用 agent2(关闭=单 agent 模式,跳过评估/验证) */
  agent2_enabled: boolean
  /** agent2 是否能自己验证(实验性,先留开关) */
  allow_verify: boolean
  /** agent2 是否能复核 AI助手引用的网址(后端安全抓取+SSRF 防护,结果仅供参考信号) */
  allow_reference_check: boolean
  /** 验证授权默认模式:"direct" 直接执行 / "per_action" 逐动作授权(任务级可覆盖) */
  verifier_auth_mode_default: 'direct' | 'per_action'
  /** 执行智能体命令确认默认模式(任务级 _executor_command_confirm 可覆盖):
   *  - "always_approve":自动批准所有命令(注入 YOLO/never 策略,不弹窗)
   *  - "per_command":每个危险命令弹窗确认(CLI 走 ACP request_permission 通道) */
  executor_command_confirm_default: 'always_approve' | 'per_command'
}

/**
 * 全局长期记忆(1:1)
 *
 * 跨项目通用经验,注入 agent2。自由文本,≤ 20000 字符。
 * 后端写入时还有合并截断(10000)+ 注入截断(2000)两道防线。
 */
export interface UserMemoryOut {
  content: string
  /** 最后更新时间(ISO 字符串,未配置时为 null) */
  updated_at: string | null
}

/** 保存全局长期记忆请求(PUT /memory/global body) */
export interface SaveUserMemoryRequest {
  content: string
}

/**
 * 记忆生成设置(嵌在 UserPreferenceOut.memory_settings 内)
 *
 * 控制任务完成后自动归纳记忆的行为:总开关 / 归纳模型 / 结构模式 / 思考模式 / 注入上限。
 * 后端总是返回;类别列在建行时即播种为内置默认(可增删改)。系统默认列表另走
 * getStructureDefaults()一次性拉取(全体用户一致),不随本响应携带。
 */
export interface MemorySettingsOut {
  /** 是否启用自动归纳(关闭则任务完成不再写记忆) */
  memory_enabled: boolean
  /** 记忆归纳/精简专用模型 id(null=未指定,回退 env 默认) */
  curator_llm_config_id: string | null
  /** 归纳/精简思考模式(follow/on/off,默认 follow) */
  thinking_mode: PracticeThinkingMode
  /** 结构化预设模式 */
  structure_mode: MemoryStructureMode
  /** 结构化类别(项目/全局各一套;按顺序即优先级,末位为杂项桶) */
  project_categories: MemoryCategoryDef[]
  global_categories: MemoryCategoryDef[]
  /** 精简版记忆注入字符上限 */
  inject_max_chars: number
}

/** 系统默认结构化类别(GET /memory/preferences/structure_defaults;供对照与恢复) */
export interface StructureDefaults {
  project_categories: MemoryCategoryDef[]
  global_categories: MemoryCategoryDef[]
}

/** 保存记忆设置请求(PUT /memory/preferences/memory_settings body)
 *
 * thinking_mode / structure_mode / inject_max_chars / 类别列表 可选,不传表示本次不修改(后端 None 语义);
 * curator_llm_config_id 传空串表示清空(回退 env 默认);类别空列表会被后端回退内置默认。
 */
export interface SaveMemorySettingsRequest {
  memory_enabled: boolean
  curator_llm_config_id?: string | null
  thinking_mode?: PracticeThinkingMode
  structure_mode?: MemoryStructureMode
  inject_max_chars?: number
  project_categories?: MemoryCategoryDef[]
  global_categories?: MemoryCategoryDef[]
}

/**
 * 分项目记忆(1:N,按 repo_url 聚合)
 *
 * 项目由 orchestrator 在任务完成时自动归纳创建(_get_or_create_project),
 * 前端不提供"新建项目"入口,只能编辑/删除已由 agent 自动归纳产生的项目记录。
 */
export interface ProjectOut {
  /** 项目记忆 UUID(字符串) */
  id: string
  /** 归一化后的 repo_url,作为项目身份(不可改) */
  repo_url_normalized: string
  /** 原始 repo_url(展示用) */
  repo_url_raw: string
  /** 项目别名(用户可改,如 'SecondLook 主仓库') */
  alias: string | null
  /** 项目备注(用户可改,自由文本) */
  note: string | null
  /** 分项目记忆正文(注入 agent1,≤ 20000 字符) */
  memory_content: string
  /** 上次自动归纳时间(ISO 字符串,可空) */
  last_summary_at: string | null
  created_at: string | null
  updated_at: string | null
}

/**
 * 保存分项目记忆请求(PUT /memory/projects/{id} body)
 *
 * 仅允许编辑 alias/note/memory_content(不修改 repo_url 与 last_summary_at)。
 */
export interface SaveProjectRequest {
  alias: string | null
  note: string | null
  memory_content: string
}

/** 分项目记忆列表响应(GET /memory/projects、DELETE 后响应) */
export interface ProjectListResponse {
  projects: ProjectOut[]
}
