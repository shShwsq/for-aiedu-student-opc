/**
 * 任务运行时模型来源的共享加载(LLM 配置清单 + 任务默认审查模型槽位)
 *
 * 任务详情页现在有两处编辑入口:右侧栏「概览」段的「对话模型」块,以及输入框
 * 上方的「运行时设置」折叠条。两处必须用同一份清单与同一个默认档口径,否则
 * 同一个任务在两边会显示不同名字。
 *
 * - 配置清单直接复用 useLlmConfigOptions 的模块缓存(登出与清单增删改都会作废,
 *   那边已经接好),这里不再自己 GET /models/configs;
 * - 槽位默认**只合并并发请求**,不做长期缓存:两处入口同一次挂载只发一次
 *   GET /models/defaults,而设置页改过「任务默认审查模型」后再进详情页仍能看到
 *   新默认(长期缓存会让它显示上一个默认模型)。
 */
import { computed, ref } from 'vue'

import { getModelSlots } from '@/api/model_slots'
import { ensureLlmConfigsLoaded, llmConfigs } from './useLlmConfigOptions'
import { defaultOptionLabel, runtimeModelLabel, type TaskModelSources } from '@/utils/taskModels'

/** 下拉选项(BaseSelect 的 SelectOption 同形,这里不跨 SFC 引类型) */
export interface RuntimeModelOption {
  value: string
  label: string
}

/** 用户级「任务默认审查模型」槽位命中的配置展示名(空=槽位未指向已存配置) */
const slotLabel = ref('')
/** 未配置槽位时实际使用的模型名(空=未知) */
const envModel = ref('')
/** 清单/槽位是否仍在加载(模块级:同批挂载的两处入口一起翻转) */
export const modelSourcesLoading = ref(true)

let slotInFlight: Promise<void> | null = null

/** 并发去重地拉齐两份数据;任一失败都不抛(下拉退化成只剩默认档,下次挂载可重试) */
export function ensureTaskModelSources(): Promise<void> {
  const configs = ensureLlmConfigsLoaded().catch(() => undefined)
  let slots = slotInFlight
  if (!slots) {
    modelSourcesLoading.value = true
    slots = getModelSlots()
      .then((res) => {
        const review = res.slots.find((s) => s.slot === 'review_default')
        // 只在确实命中已存配置时展示名字:配置被删时后端回退环境默认,不能谎报
        slotLabel.value = review && review.source === 'slot' ? review.config_label : ''
        envModel.value = res.env_default_model || ''
      })
      .catch(() => {
        slotLabel.value = ''
        envModel.value = ''
      })
      .finally(() => {
        // 只释掉自己那一次:后发的调用可能已经接上了同一个 promise
        if (slotInFlight === slots) slotInFlight = null
        modelSourcesLoading.value = false
      })
    slotInFlight = slots
  }
  return Promise.all([configs, slots]).then(() => undefined)
}

/** 供「对话模型」两处入口共用的选项与数据源 */
export function useTaskRuntimeModels() {
  const sources = computed<TaskModelSources>(() => ({
    configs: llmConfigs.value,
    slotLabel: slotLabel.value,
    envModel: envModel.value,
  }))

  const configOptions = computed<RuntimeModelOption[]>(() =>
    llmConfigs.value.map((cfg) => ({ value: cfg.id, label: runtimeModelLabel(cfg) })),
  )

  /** 检查助手模型选项(空=不指定本任务,用默认档) */
  const reviewOptions = computed<RuntimeModelOption[]>(() => [
    { value: '', label: defaultOptionLabel(sources.value) },
    ...configOptions.value,
  ])

  /** AI助手模型选项(空=同评估模型) */
  const reactOptions = computed<RuntimeModelOption[]>(() => [
    { value: '', label: '同评估模型' },
    ...configOptions.value,
  ])

  return {
    loading: modelSourcesLoading,
    sources,
    reviewOptions,
    reactOptions,
    ensureLoaded: ensureTaskModelSources,
  }
}
