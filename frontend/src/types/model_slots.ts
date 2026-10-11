/**
 * 模型槽位(任务模型默认)相关类型
 *
 * 对应后端 app/schemas/model_slots.py:一处会调 LLM 的任务(槽位)默认用哪条
 * 已保存的 LLM 配置。「任务模型默认」页只做展示与深链,编辑发生在各功能自己的
 * 设置面板(归属面板端点写穿到同一张表)。
 */

/** 槽位标识(与后端 app/models/model_slot.py 的常量一一对应) */
export type ModelSlotId =
  | 'review_default'
  | 'practice_generate'
  | 'practice_explain'
  | 'memory_curator'

/** 解析命中的来源:slot=用户级默认 / env=回退环境默认 */
export type ModelSlotSource = 'slot' | 'env'

export type ThinkingMode = 'follow' | 'on' | 'off'

/** 一个槽位的展示数据(后端已算好 label 与来源,前端不再自己拼优先级) */
export interface ModelSlot {
  slot: ModelSlotId
  label: string
  /** 该槽位用在哪、优先级如何(HelpPopover 正文) */
  description: string
  /** 编辑入口深链,如 /settings/practice#generation */
  edit_path: string
  /** 是否允许 PUT /models/defaults 写本槽位 */
  writable: boolean
  /** 解析链是否有任务级前置(任务自带模型 > 本槽位默认) */
  task_override: boolean
  has_thinking_mode: boolean
  /** 当前分配的模型配置 id(null=未指定) */
  llm_config_id: string | null
  /** 命中配置的展示名「名称(厂商 / 模型)」;空串表示未命中 */
  config_label: string
  thinking_mode: ThinkingMode
  source: ModelSlotSource
  /** 配了 id 但配置已被删除:实际用的是环境默认,需要提示用户重选 */
  stale: boolean
}

/** 非槽位的模型去向说明(不可配置) */
export interface ModelSlotInfoRow {
  id: string
  label: string
  description: string
}

export interface ModelSlotsResponse {
  slots: ModelSlot[]
  info_rows: ModelSlotInfoRow[]
  /** 未配置槽位时实际使用的模型名 */
  env_default_model: string
}

export interface SaveModelSlotItem {
  /** null/undefined=本次不修改,空串=清空(回退环境默认) */
  llm_config_id?: string | null
  thinking_mode?: ThinkingMode | null
}

export interface SaveModelSlotsRequest {
  slots: Partial<Record<ModelSlotId, SaveModelSlotItem>>
}

