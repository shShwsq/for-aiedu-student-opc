/**
 * composables/useLlmConfigOptions 单元测试
 *
 * 锁死模型下拉缓存的跨账号不变量:
 * 1. 作废(登出)之后,上一个账号的在途响应不得把列表写回模块缓存并标成「已加载」
 *    —— 否则下一个登录者在同一标签页里直接看到别人的模型名(展示本身就是泄露面)
 * 2. 作废要释掉在途句柄:紧接着重新拉取的人拿得到自己的列表,不用退页重进
 * 3. 过时请求的失败不能踩掉新世代刚置起来的加载标记
 * 4. 正常路径仍然并发去重(多个面板共用一次 GET /models/configs)
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getMyModels: vi.fn() }))
vi.mock('@/api/model_configs', () => ({ getMyModels: api.getMyModels }))

import {
  ensureLlmConfigsLoaded,
  invalidateLlmConfigs,
  llmConfigs,
  llmConfigsLoaded,
} from './useLlmConfigOptions'
import type { LLMConfigItemOut } from '@/types/model_configs'

function cfg(id: string, model: string): LLMConfigItemOut {
  return { id, name: id, provider: 'dashscope', model } as LLMConfigItemOut
}

function deferred() {
  let resolve!: (v: { llm_configs: LLMConfigItemOut[] }) => void
  let reject!: (e: Error) => void
  const promise = new Promise<{ llm_configs: LLMConfigItemOut[] }>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('useLlmConfigOptions 缓存世代', () => {
  beforeEach(() => {
    // 模块级状态跨用例保留,用作废接口复位(连同世代号一起在推进)
    invalidateLlmConfigs()
    api.getMyModels.mockReset()
  })

  it('作废后在途响应不再写回缓存(不泄漏上一个账号的模型名)', async () => {
    const d = deferred()
    api.getMyModels.mockReturnValue(d.promise)

    const pending = ensureLlmConfigsLoaded()
    invalidateLlmConfigs() // 模拟登出
    d.resolve({ llm_configs: [cfg('a', 'qwen-max')] }) // A 的响应现在才到
    const result = await pending

    expect(result).toEqual([])
    expect(llmConfigs.value).toEqual([])
    expect(llmConfigsLoaded.value).toBe(false)
  })

  it('作废后重新拉取用的是新世代,登录者拿得到自己的列表', async () => {
    const d = deferred()
    api.getMyModels.mockReturnValueOnce(d.promise)
    const pending = ensureLlmConfigsLoaded()
    invalidateLlmConfigs()

    // 作废必须同时释掉在途句柄,否则这次调用会复用 A 那次请求
    api.getMyModels.mockResolvedValueOnce({ llm_configs: [cfg('b', 'glm-4')] })
    const fresh = await ensureLlmConfigsLoaded()
    expect(fresh.map((c) => c.id)).toEqual(['b'])
    expect(llmConfigs.value.map((c) => c.id)).toEqual(['b'])
    expect(llmConfigsLoaded.value).toBe(true)

    // A 的响应随后到达:丢弃,且不能覆盖 B 的缓存
    d.resolve({ llm_configs: [cfg('a', 'qwen-max')] })
    await pending
    expect(llmConfigs.value.map((c) => c.id)).toEqual(['b'])
  })

  it('过时请求失败不踩掉新世代的加载标记', async () => {
    const d = deferred()
    api.getMyModels.mockReturnValueOnce(d.promise)
    const stale = ensureLlmConfigsLoaded()
    invalidateLlmConfigs()

    api.getMyModels.mockResolvedValueOnce({ llm_configs: [cfg('c', 'm')] })
    await ensureLlmConfigsLoaded()
    expect(llmConfigsLoaded.value).toBe(true)

    d.reject(new Error('network'))
    await expect(stale).rejects.toThrow('network')
    // 新世代已成功加载:过时请求的 catch 不能把它置回 false
    expect(llmConfigsLoaded.value).toBe(true)
    expect(llmConfigs.value.map((c) => c.id)).toEqual(['c'])
  })

  it('未作废时正常缓存,并发只发一次请求', async () => {
    api.getMyModels.mockResolvedValue({ llm_configs: [cfg('a', 'qwen-max')] })
    const [one, two] = await Promise.all([
      ensureLlmConfigsLoaded(),
      ensureLlmConfigsLoaded(),
    ])
    expect(api.getMyModels).toHaveBeenCalledTimes(1)
    expect(one.map((c) => c.id)).toEqual(['a'])
    expect(two).toBe(one)
    expect(llmConfigsLoaded.value).toBe(true)

    // 已加载时直接返回缓存,不再发请求
    await ensureLlmConfigsLoaded()
    expect(api.getMyModels).toHaveBeenCalledTimes(1)
    // force=true 用于清单保存后刷新
    await ensureLlmConfigsLoaded(true)
    expect(api.getMyModels).toHaveBeenCalledTimes(2)
  })
})
