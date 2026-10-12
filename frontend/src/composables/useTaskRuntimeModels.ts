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
 *
 * `slotLabel` / `envModel` 同样是按用户私有的展示信息,所以登出必须一并清掉
 * (`invalidateTaskModelSources`,接在 auth store 的 logout 里):不清的话下一个
 * 登录者在同一标签页里会先看到上一个账号的默认模型名。与 useLlmConfigOptions
 * 同款处理:作废同时推进世代号并释掉在途句柄,否则旧账号那次请求回来会把名字写回。
 */
import { computed, ref } from 'vue'

import { getModelSlots } from '@/api/model_slots'
import {
  ensureLlmConfigsLoaded,
  llmConfigs,
  llmConfigsLoaded,
} from './useLlmConfigOptions'
import {
  defaultOptionLabel,
  runtimeModelLabel,
  type TaskModelSources,
} from '@/utils/taskModels'

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

/**
 * 作废世代号
 *
 * 只清 `slotLabel` 不够:登出后旧账号那次在途请求回来会把它重新写回去,
 * 泄露窗口比不清还长(先例:useLlmConfigOptions 的同名机制)。
 */
let generation = 0

/** 并发去重地拉齐两份数据;任一失败都不抛(下拉退化成只剩默认档,下次挂载可重试) */
export function ensureTaskModelSources(): Promise<void> {
  const configs = ensureLlmConfigsLoaded().catch(() => undefined)
  let pending = slotInFlight
  if (!pending) {
    modelSourcesLoading.value = true
    const gen = generation
    pending = getModelSlots()
      .then((res) => {
        // 请求期间被作废过(登出):这份结果属于上一个账号,丢掉
        if (gen !== generation) return
        const review = res.slots.find((s) => s.slot === 'review_default')
        // 只在确实命中已存配置时展示名字:配置被删时后端回退环境默认,不能谎报
        slotLabel.value = review && review.source === 'slot' ? review.config_label : ''
        envModel.value = res.env_default_model || ''
      })
      .catch(() => {
        if (gen !== generation) return
        slotLabel.value = ''
        envModel.value = ''
      })
      .finally(() => {
        // 只释掉自己那一次:作废会把句柄置空并开新请求,旧请求收尾时不能把它踩掉
        if (slotInFlight === pending) slotInFlight = null
        // 过时请求的失败/收尾不能踩掉新世代刚置起来的加载标记
        if (gen === generation) modelSourcesLoading.value = false
      })
    slotInFlight = pending
  }
  return Promise.all([configs, pending]).then(() => undefined)
}

/** 作废(登出时调用):清掉上一个账号的默认模型名,并释掉在途请求 */
export function invalidateTaskModelSources(): void {
  generation += 1
  slotInFlight = null
  slotLabel.value = ''
  envModel.value = ''
  // 标成未加载:否则下一个登录者在自己的请求落地前会拿到 ready=true + 空默认档
  modelSourcesLoading.value = true
}

/** 供「对话模型」两处入口共用的选项与数据源 */
export function useTaskRuntimeModels() {
  const sources = computed<TaskModelSources>(() => ({
    configs: llmConfigs.value,
    // 失败时它保持 false → 解析不能把「找不到」当成「已删除」
    configsLoaded: llmConfigsLoaded.value,
    slotLabel: slotLabel.value,
    envModel: envModel.value,
  }))

  /**
   * 两份数据是否都到位(未到位时**什么都不该下结论**)
   *
   * 只看 `modelSourcesLoading` 不够:它只跟踪槽位请求,清单拉失败时它照样翻 false,
   * 而选项栏仍是空的 —— 那会再把「原选配置已删除」报给一条只是没拉到的配置。
   */
  const ready = computed(() => !modelSourcesLoading.value && llmConfigsLoaded.value)

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
    ready,
    sources,
    reviewOptions,
    reactOptions,
    ensureLoaded: ensureTaskModelSources,
  }
}
