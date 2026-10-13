/**
 * CLI 执行器的模型参数选项(集中口径,供「新建任务」与详情页「运行时设置」共用)
 *
 * 这些下拉选项 value 必须与 CLI 接受的名称严格大小写匹配:
 * - Qoder:见 https://docs.qoder.cn/cli/model 及 ACP 错误返回的 Available models 列表
 * - DeepSeek:dsh deepseek-official provider 目录(deepseek-v4-flash / deepseek-v4-pro)
 *
 * 支持任务级模型参数的执行器集合与后端 `tasks._CLI_MODEL_PARAM_EXECUTORS` 同一口径:
 * 仅 qoder_cli / deepseek_cli(codex_cli 模型经 ~/.codex/config.toml 自管,不在此列)。
 */

export interface CliSelectOption {
  value: string
  label: string
}

/** 支持任务级模型参数(model/reasoning_effort[,context_window])的 CLI 执行器 */
export const CLI_MODEL_PARAM_EXECUTORS: ReadonlySet<string> = new Set([
  'qoder_cli',
  'deepseek_cli',
])

/** 上下文窗口仅 Qoder CLI 支持(dsh 无该概念) */
export const CLI_CONTEXT_WINDOW_EXECUTORS: ReadonlySet<string> = new Set(['qoder_cli'])

/** 该执行器是否可在运行时编辑模型参数(内置与 codex 返回 false) */
export function supportsModelConfig(executor?: string | null): boolean {
  return CLI_MODEL_PARAM_EXECUTORS.has(executor ?? '')
}

// ---- Qoder CLI ----

export const QODER_MODEL_OPTIONS: CliSelectOption[] = [
  { value: '', label: '默认(智能路由 Auto)' },
  { value: 'Auto', label: '智能路由 (Auto)' },
  { value: 'Qwen3.8-Max', label: 'Qwen3.8-Max' },
  { value: 'Qwen3.8-Flash', label: 'Qwen3.8-Flash' },
  { value: 'Qwen3.7-Max', label: 'Qwen3.7-Max' },
  { value: 'Qwen3.7-Plus', label: 'Qwen3.7-Plus' },
  { value: 'Qwen3.7-Flash', label: 'Qwen3.7-Flash' },
  { value: 'DeepSeek-V4-Pro', label: 'DeepSeek-V4-Pro' },
  { value: 'DeepSeek-V4-Flash', label: 'DeepSeek-V4-Flash' },
  { value: 'GLM-5.3', label: 'GLM-5.3' },
  { value: 'GLM-5.2', label: 'GLM-5.2' },
  { value: 'Kimi-K2.7-Code', label: 'Kimi-K2.7-Code' },
  { value: 'MiniMax-M2.7', label: 'MiniMax-M2.7' },
]

export const QODER_EFFORT_OPTIONS: CliSelectOption[] = [
  { value: '', label: '默认' },
  { value: 'low', label: 'Low(最快)' },
  { value: 'medium', label: 'Medium(适中)' },
  { value: 'high', label: 'High(深入)' },
  { value: 'xhigh', label: 'XHigh(深度分析)' },
  { value: 'max', label: 'Max(最大推理)' },
]

export const QODER_CONTEXT_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: '默认' },
  { value: 200000, label: '200K' },
  { value: 400000, label: '400K' },
  { value: 1000000, label: '1M' },
]

// ---- DeepSeek CLI(dsh ACP 模式经 session/set_config_option 设置) ----

export const DEEPSEEK_MODEL_OPTIONS: CliSelectOption[] = [
  { value: '', label: '默认(DeepSeek-V4-Flash)' },
  { value: 'deepseek-v4-flash', label: 'DeepSeek-V4-Flash(轻量)' },
  { value: 'deepseek-v4-pro', label: 'DeepSeek-V4-Pro' },
]

/** dsh 广播等级:off / low / high / max */
export const DEEPSEEK_EFFORT_OPTIONS: CliSelectOption[] = [
  { value: '', label: '默认(Max)' },
  { value: 'off', label: 'Off(不思考)' },
  { value: 'low', label: 'Low(最快)' },
  { value: 'high', label: 'High(深入)' },
  { value: 'max', label: 'Max(最大推理)' },
]

/** 按执行器取模型下拉选项(未知/不支持返回空) */
export function modelOptionsFor(executor?: string | null): CliSelectOption[] {
  if (executor === 'deepseek_cli') return DEEPSEEK_MODEL_OPTIONS
  if (executor === 'qoder_cli') return QODER_MODEL_OPTIONS
  return []
}

/** 按执行器取思考强度下拉选项 */
export function effortOptionsFor(executor?: string | null): CliSelectOption[] {
  if (executor === 'deepseek_cli') return DEEPSEEK_EFFORT_OPTIONS
  if (executor === 'qoder_cli') return QODER_EFFORT_OPTIONS
  return []
}

/** 按执行器取上下文窗口下拉选项(仅 Qoder 有) */
export function contextOptionsFor(
  executor?: string | null,
): { value: number; label: string }[] {
  return executor === 'qoder_cli' ? QODER_CONTEXT_OPTIONS : []
}
