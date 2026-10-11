/**
 * 「我保存过的 LLM 配置」共享缓存(模块级)
 *
 * 下拉选项同时被多处需要:练习设置(出题/讲解模型)、记忆设置(归纳模型)、
 * 智能体策略(任务默认审查模型)、模型设置的「任务模型默认」页。
 * 与 useAgentTypes 同款写法:并发去重、失败清缓存可重试;各面板不再各发一次
 * GET /models/configs,也不再各自复制一份「名称(厂商 / 模型)」拼接逻辑。
 *
 * 模型清单被编辑(增删改)后请调 invalidateLlmConfigs(),否则别的页面会看到旧列表。
 */
import { ref } from 'vue'

import { getMyModels } from '@/api/model_configs'
import type { LLMConfigItemOut } from '@/types/model_configs'

/** 已加载的配置列表(未加载完时为空数组) */
export const llmConfigs = ref<LLMConfigItemOut[]>([])

/** 是否已完成过一次成功加载(区分「还没拉」与「拉了但没有配置」) */
export const llmConfigsLoaded = ref(false)

let inFlight: Promise<LLMConfigItemOut[]> | null = null

/** 下拉展示名:`名称(厂商 / 模型)`,无名时回退模型名 */
export function configOptionLabel(cfg: LLMConfigItemOut): string {
  return `${cfg.name || cfg.model || '未命名配置'}(${cfg.provider} / ${cfg.model})`
}

/** 拉取并缓存(已加载则直接返回;force=true 用于模型清单保存后刷新) */
export function ensureLlmConfigsLoaded(force = false): Promise<LLMConfigItemOut[]> {
  if (llmConfigsLoaded.value && !force) return Promise.resolve(llmConfigs.value)
  if (!inFlight) {
    inFlight = getMyModels()
      .then((res) => {
        llmConfigs.value = res.llm_configs
        llmConfigsLoaded.value = true
        return llmConfigs.value
      })
      .catch((err) => {
        // 静默失败由调用方决定(下拉只剩「跟随系统默认」),这里只清缓存允许重试
        llmConfigsLoaded.value = false
        throw err
      })
      .finally(() => {
        inFlight = null
      })
  }
  return inFlight
}

/** 作废缓存(模型清单增删改后调用),下次进入下拉所在页面会重新拉取 */
export function invalidateLlmConfigs(): void {
  llmConfigsLoaded.value = false
  llmConfigs.value = []
}
