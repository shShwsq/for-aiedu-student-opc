/**
 * composables/useTaskRuntimeModels 单元测试
 *
 * 锁死「对话模型」两处入口共用的数据层不变量:
 * 1. 并发去重:概览段与输入框上方折叠条同批挂载只发一次 GET /models/defaults
 * 2. 跨账号不残留:登出作废后,上一个账号的在途响应不得把默认模型名写回
 *    (展示本身就是泄露面,与 useLlmConfigOptions 同一口径)
 * 3. 作废要释掉在途句柄:下一个登录者紧接着进详情页拿得到自己的默认档
 * 4. ready 要同时看两份数据:槽位拉成功但清单拉挂时仍算未就绪
 *    (否则「找不到配置」会被报成「原选配置已删除」)
 * 5. 槽位不做长期缓存:每次挂载重取,设置页改过默认模型后不会被读旧
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getMyModels: vi.fn(),
  getModelSlots: vi.fn(),
}))
vi.mock('@/api/model_configs', () => ({ getMyModels: api.getMyModels }))
vi.mock('@/api/model_slots', () => ({ getModelSlots: api.getModelSlots }))

import {
  ensureTaskModelSources,
  invalidateTaskModelSources,
  modelSourcesLoading,
  useTaskRuntimeModels,
} from './useTaskRuntimeModels'
import { invalidateLlmConfigs, llmConfigs } from './useLlmConfigOptions'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { ModelSlot, ModelSlotsResponse } from '@/types/model_slots'

function cfg(id: string, model: string): LLMConfigItemOut {
  return { id, name: id, provider: 'dashscope', model, has_api_key: true } as LLMConfigItemOut
}

/** 造一份「任务默认审查模型」槽位响应(只填本 composable 真正读的那几个字段) */
function slotsResp(configLabel: string, source: ModelSlot['source'] = 'slot'): ModelSlotsResponse {
  return {
    slots: [{ slot: 'review_default', source, config_label: configLabel }],
    info_rows: [],
    env_default_model: '',
  } as unknown as ModelSlotsResponse
}

function deferred() {
  let resolve!: (v: ModelSlotsResponse) => void
  let reject!: (e: Error) => void
  const promise = new Promise<ModelSlotsResponse>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** 默认档选项文案(第 0 项就是「默认模型（…）」,拿它当 slotLabel 的观察点) */
function defaultOptionText(): string {
  return useTaskRuntimeModels().reviewOptions.value[0].label
}

beforeEach(() => {
  // 模块级状态跨用例保留,两个作废接口一起复位
  invalidateLlmConfigs()
  invalidateTaskModelSources()
  api.getMyModels.mockReset()
  api.getModelSlots.mockReset()
  api.getMyModels.mockResolvedValue({ llm_configs: [cfg('a', 'qwen-max')] })
})

describe('useTaskRuntimeModels 加载与作废', () => {
  it('同批并发挂载只发一次槽位请求', async () => {
    api.getModelSlots.mockResolvedValue(slotsResp('A 的默认'))
    await Promise.all([ensureTaskModelSources(), ensureTaskModelSources()])
    expect(api.getModelSlots).toHaveBeenCalledTimes(1)
    expect(defaultOptionText()).toBe('默认模型（A 的默认）')
    expect(modelSourcesLoading.value).toBe(false)
  })

  it('每次挂载都重取槽位(不做长期缓存:设置页改过默认模型不能被读旧)', async () => {
    api.getModelSlots.mockResolvedValueOnce(slotsResp('旧默认'))
    await ensureTaskModelSources()
    expect(defaultOptionText()).toBe('默认模型（旧默认）')

    api.getModelSlots.mockResolvedValueOnce(slotsResp('新默认'))
    await ensureTaskModelSources()
    expect(api.getModelSlots).toHaveBeenCalledTimes(2)
    expect(defaultOptionText()).toBe('默认模型（新默认）')
  })

  it('作废后在途响应不写回默认模型名(不泄漏上一个账号的配置)', async () => {
    const d = deferred()
    api.getModelSlots.mockReturnValueOnce(d.promise)
    const pending = ensureTaskModelSources()

    invalidateTaskModelSources() // 模拟登出
    expect(defaultOptionText()).toBe('默认模型(环境变量)')

    d.resolve(slotsResp('A 的默认')) // A 的响应现在才到
    await pending
    expect(defaultOptionText()).toBe('默认模型(环境变量)')
  })

  it('作废释掉在途句柄:下一个登录者立刻发自己的请求,不复用上一个账号那次', async () => {
    const d = deferred()
    api.getModelSlots.mockReturnValueOnce(d.promise)
    const stale = ensureTaskModelSources()

    invalidateTaskModelSources()
    api.getModelSlots.mockResolvedValueOnce(slotsResp('B 的默认'))
    await ensureTaskModelSources()
    expect(api.getModelSlots).toHaveBeenCalledTimes(2)
    expect(defaultOptionText()).toBe('默认模型（B 的默认）')

    // A 的响应随后到达:丢弃,不能覆盖 B 的
    d.resolve(slotsResp('A 的默认'))
    await stale
    expect(defaultOptionText()).toBe('默认模型（B 的默认）')
  })

  it('槽位请求失败不卡住加载标记(下次挂载可重试)', async () => {
    api.getModelSlots.mockRejectedValueOnce(new Error('boom'))
    await ensureTaskModelSources()
    expect(modelSourcesLoading.value).toBe(false)
    expect(defaultOptionText()).toBe('默认模型(环境变量)')

    api.getModelSlots.mockResolvedValueOnce(slotsResp('C 的默认'))
    await ensureTaskModelSources()
    expect(defaultOptionText()).toBe('默认模型（C 的默认）')
  })

  it('清单拉挂时 ready 仍为 false(不能把「没拉到」报成「原选配置已删除」)', async () => {
    api.getMyModels.mockRejectedValueOnce(new Error('401'))
    api.getModelSlots.mockResolvedValue(slotsResp('D 的默认'))
    await ensureTaskModelSources()

    const { ready, sources } = useTaskRuntimeModels()
    expect(modelSourcesLoading.value).toBe(false) // 槽位确实到位了
    expect(ready.value).toBe(false) // 但清单没到位,整体不算就绪
    expect(sources.value.configsLoaded).toBe(false)
    expect(llmConfigs.value).toEqual([])
  })

  it('两份数据都到位才算 ready', async () => {
    api.getModelSlots.mockResolvedValue(slotsResp('E 的默认'))
    await ensureTaskModelSources()
    const { ready, sources } = useTaskRuntimeModels()
    expect(sources.value.configs.map((c) => c.id)).toEqual(['a'])
    expect(ready.value).toBe(true)
  })
})
