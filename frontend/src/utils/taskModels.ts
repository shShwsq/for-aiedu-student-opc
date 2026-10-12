/**
 * 任务「对话使用的模型」的纯解析与排版(不碰 DOM,供面板与单测共用)
 *
 * 必须与后端 orchestrator 的解析链同一口径,否则侧栏会出现「显示 A 实际用 B」:
 * - 检查助手(agent2):task.llm_config_id → 任务默认审查模型槽位 → 环境默认
 * - AI助手(agent1,仅内置执行器):task.react_llm_config_id → task.llm_config_id
 *   → 任务默认审查模型槽位 → 环境默认
 * - 外部 CLI 执行器:react 角色模型由 CLI 自管,后端忽略 react_llm_config_id,
 *   所以这里不能跟着编一个名字出来
 *
 * 「配置 id 存着但清单里找不到」= 那条 LLM 配置已被删除,后端此时回退默认模型。
 * 这种情况标 stale(先例:types/model_slots 的 stale 字段),文案要说清已回退,
 * 否则用户会对着一个已经不存在的配置排障。
 */
import type { LLMConfigItemOut } from '@/types/model_configs'

/** 生效模型命中的来源档位(与后端 _resolve_*_client 的分级一一对应) */
export type TaskModelSource = 'task' | 'same-as-review' | 'slot' | 'env' | 'cli'

export interface ResolvedTaskModel {
  /** 生效模型展示名;来源为 env 且模型名未知时为空串(交给文案退化成只报来源) */
  label: string
  source: TaskModelSource
  /** 任务上存着配置 id,但那条配置已被删除(实际用的是默认模型) */
  stale: boolean
}

/** 解析所需的三份外部数据(由调用方加载,本模块只算不取) */
export interface TaskModelSources {
  /** 当前用户已保存的 LLM 配置清单 */
  configs: LLMConfigItemOut[]
  /** 用户级「任务默认审查模型」槽位命中的配置展示名;空串=槽位未指向已存配置 */
  slotLabel: string
  /** 环境变量默认模型名;空串=未知 */
  envModel: string
}

/** 任务模型字段的输入形状(TaskDetail 与 TaskCreate 请求都能套进来) */
export interface TaskModelIds {
  /** 检查助手评估模型配置 id */
  reviewId?: string | null
  /** 内置 AI助手模型配置 id(空=同评估模型) */
  reactId?: string | null
  /** 执行器:"builtin" 或某个 agent_type(如 "qoder_cli") */
  executor?: string | null
}

const SOURCE_LABEL: Record<TaskModelSource, string> = {
  task: '任务配置',
  'same-as-review': '同评估模型',
  slot: '任务默认审查模型',
  env: '环境默认',
  cli: '',
}

/**
 * 运行时模型下拉与「实际使用」行共用的展示名
 *
 * 未配 Key 的配置仍可被选中,但选中即跑不通,所以名字后面要把标记带上
 * (原为 TaskRuntimeSettings 私有实现,两处入口共用后提到这里)。
 */
export function runtimeModelLabel(cfg: LLMConfigItemOut): string {
  const name = cfg.name || cfg.model
  return cfg.has_api_key ? name : `${name}(未配置 Key)`
}

/** 默认档(任务没指定或指定的已删)命中哪一级 */
function resolveDefaultModel(sources: TaskModelSources): ResolvedTaskModel {
  if (sources.slotLabel) {
    return { label: sources.slotLabel, source: 'slot', stale: false }
  }
  // 环境默认模型名未知时不编名字,只报来源(见 taskModelDisplayText 的空串分支)
  return { label: sources.envModel, source: 'env', stale: false }
}

function findConfig(id: string | null | undefined, sources: TaskModelSources): LLMConfigItemOut | undefined {
  return id ? sources.configs.find((c) => c.id === id) : undefined
}

/** 检查助手实际使用的模型 */
export function resolveReviewModel(input: TaskModelIds, sources: TaskModelSources): ResolvedTaskModel {
  const cfg = findConfig(input.reviewId, sources)
  if (cfg) return { label: runtimeModelLabel(cfg), source: 'task', stale: false }
  const fallback = resolveDefaultModel(sources)
  return { ...fallback, stale: !!input.reviewId }
}

/** AI助手实际使用的模型(CLI 执行器下由 CLI 自管,不看任务字段) */
export function resolveAgentModel(input: TaskModelIds, sources: TaskModelSources): ResolvedTaskModel {
  if ((input.executor ?? 'builtin') !== 'builtin') {
    return { label: '由 CLI 自管', source: 'cli', stale: false }
  }
  const cfg = findConfig(input.reactId, sources)
  if (cfg) return { label: runtimeModelLabel(cfg), source: 'task', stale: false }
  // react 未单独指定(或指定的已被删)→ 回退评估模型那条链
  const review = resolveReviewModel(input, sources)
  if (review.source === 'task') {
    // 与评估模型同源:选择框里 react 传的是空串,reactId 有值却落这里 = 原选已删
    return { label: review.label, source: 'same-as-review', stale: !!input.reactId }
  }
  return { ...review, stale: review.stale || !!input.reactId }
}

/**
 * 生效模型的人话文案:`模型名（来源）`,必要时补一句「原选配置已删除」
 *
 * env 档且模型名未知时只报来源(不写「环境默认（环境默认）」这种复读)。
 */
export function taskModelDisplayText(model: ResolvedTaskModel): string {
  if (model.source === 'cli') return model.label
  const sourceLabel = SOURCE_LABEL[model.source]
  let text = model.label || sourceLabel
  if (model.label && sourceLabel) text += `（${sourceLabel}）`
  if (model.stale) text += '（原选配置已删除,已回退）'
  return text
}

/**
 * 「默认模型」下拉选项的文案
 *
 * 选择框里空串代表"本任务不指定",必须说清空值实际会落到哪个模型,
 * 否则用户以为留空是"不用模型"。
 */
export function defaultOptionLabel(sources: TaskModelSources): string {
  if (sources.slotLabel) return `默认模型（${sources.slotLabel}）`
  return sources.envModel ? `默认模型（环境默认：${sources.envModel}）` : '默认模型(环境变量)'
}
