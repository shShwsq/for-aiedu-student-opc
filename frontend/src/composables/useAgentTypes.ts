/**
 * CLI agent 类型清单(共享缓存,模块级)
 *
 * GET /agents/types 的返回同时被两处需要:
 * - SettingsLayout:渲染「CLI 设置」下的二级目录(每个 agent 一项,标签取 display_name)
 * - CliSettingsPanel:渲染 tab 栏与动态表单
 * 因此在此集中拉取一次并缓存,避免布局与面板各发一次请求。
 *
 * 与 useFeatures 的 ensureFeaturesLoaded 同款写法:并发去重;失败时清空缓存,
 * 错误抛给调用方(面板据此 toast,下次进入可重试)。
 */
import { ref } from 'vue'

import { getAgentTypes } from '@/api/agent_configs'
import type { AgentTypeMeta } from '@/types/agent_configs'

/** 已注册的 agent 类型(未加载完时为空数组) */
export const agentTypes = ref<AgentTypeMeta[]>([])

let loading: Promise<AgentTypeMeta[]> | null = null

/** 拉取并缓存 agent 类型(已缓存则直接返回;失败后允许重试) */
export function ensureAgentTypesLoaded(): Promise<AgentTypeMeta[]> {
  if (!loading) {
    loading = getAgentTypes()
      .then((types) => {
        agentTypes.value = types
        return types
      })
      .catch((err) => {
        // 清掉失败的 promise,下次调用重新拉
        loading = null
        throw err
      })
  }
  return loading
}
