/**
 * utils/taskModels 单元测试
 *
 * 锁死「对话使用的模型」这一栏的口径:
 * 1. 解析链与后端一致(任务配置 → 同评估模型 → 任务默认审查模型槽位 → 环境默认)
 * 2. 配置被删除时如实回退并标 stale(不能把已不存在的配置报成在用)
 * 3. 清单没拉到之前不下「已删除」的结论(未知不等于已删)
 * 4. CLI 执行器不编模型名(react 角色由 CLI 自管)
 * 5. 文案不出现复读(名称缺失时只报来源)
 */
import { describe, expect, it } from 'vitest'

import {
  defaultOptionLabel,
  resolveAgentModel,
  resolveReviewModel,
  runtimeModelLabel,
  taskModelDisplayText,
  type TaskModelSources,
} from './taskModels'
import type { LLMConfigItemOut } from '@/types/model_configs'

function cfg(id: string, name = id, hasKey = true): LLMConfigItemOut {
  return { id, name, provider: 'dashscope', model: `${id}-model`, has_api_key: hasKey } as LLMConfigItemOut
}

const sources = (over: Partial<TaskModelSources> = {}): TaskModelSources => ({
  configs: [cfg('a'), cfg('b')],
  configsLoaded: true,
  slotLabel: '',
  envModel: '',
  ...over,
})

describe('runtimeModelLabel', () => {
  it('未配 Key 的配置在名字后面带标记', () => {
    expect(runtimeModelLabel(cfg('a'))).toBe('a')
    expect(runtimeModelLabel(cfg('k', '我的模型', false))).toBe('我的模型(未配置 Key)')
  })

  it('无名配置回退模型名', () => {
    expect(runtimeModelLabel({ ...cfg('x'), name: '' })).toBe('x-model')
  })
})

describe('resolveReviewModel', () => {
  it('命中任务配置时报「任务配置」', () => {
    const m = resolveReviewModel({ reviewId: 'a' }, sources())
    expect(m).toEqual({ label: 'a', source: 'task', stale: false })
    expect(taskModelDisplayText(m)).toBe('a（任务配置）')
  })

  it('存的配置已被删除时回退默认并标 stale', () => {
    const m = resolveReviewModel({ reviewId: 'gone' }, sources({ slotLabel: '默认槽位名' }))
    expect(m.label).toBe('默认槽位名')
    expect(m.source).toBe('slot')
    expect(m.stale).toBe(true)
    expect(taskModelDisplayText(m)).toBe('默认槽位名（任务默认审查模型）（原选配置已删除,已回退）')
  })

  it('未指定时依次落到槽位、环境默认', () => {
    expect(resolveReviewModel({ reviewId: null }, sources({ slotLabel: 'S' })).source).toBe('slot')
    const env = resolveReviewModel({ reviewId: '' }, sources({ envModel: 'qwen-max' }))
    expect(env).toEqual({ label: 'qwen-max', source: 'env', stale: false })
    expect(taskModelDisplayText(env)).toBe('qwen-max（环境默认）')
  })

  it('环境默认模型名未知时只报来源,不编名字', () => {
    const m = resolveReviewModel({}, sources())
    expect(m.label).toBe('')
    expect(taskModelDisplayText(m)).toBe('环境默认')
  })
})

describe('resolveAgentModel', () => {
  it('外部 CLI 执行器报「由 CLI 自管」,不读任务字段', () => {
    const m = resolveAgentModel({ executor: 'qoder_cli', reactId: 'a', reviewId: 'b' }, sources())
    expect(m).toEqual({ label: '由 CLI 自管', source: 'cli', stale: false })
    expect(taskModelDisplayText(m)).toBe('由 CLI 自管')
  })

  it('内置执行器命中自己的配置', () => {
    const m = resolveAgentModel({ reactId: 'b', reviewId: 'a' }, sources())
    expect(taskModelDisplayText(m)).toBe('b（任务配置）')
  })

  it('未单独指定时同评估模型', () => {
    const m = resolveAgentModel({ reactId: '', reviewId: 'a' }, sources())
    expect(m).toEqual({ label: 'a', source: 'same-as-review', stale: false })
    expect(taskModelDisplayText(m)).toBe('a（同评估模型）')
  })

  it('react 配置被删但评估模型仍在时,报同评估并提示已回退', () => {
    const m = resolveAgentModel({ reactId: 'gone', reviewId: 'a' }, sources())
    expect(m.label).toBe('a')
    expect(m.source).toBe('same-as-review')
    expect(m.stale).toBe(true)
  })

  it('两边都未指定时走默认档,槽位优先于环境', () => {
    expect(resolveAgentModel({}, sources({ slotLabel: 'S', envModel: 'qwen-max' }))).toEqual({
      label: 'S',
      source: 'slot',
      stale: false,
    })
    expect(resolveAgentModel({}, sources({ envModel: 'qwen-max' })).source).toBe('env')
  })

  it('executor 缺失按内置处理(老任务没有该字段)', () => {
    expect(resolveAgentModel({ reactId: 'a' }, sources()).source).toBe('task')
  })
})

describe('清单未就位时不下结论', () => {
  it('configsLoaded=false 时「找不到」只算未知,不报「原选配置已删除」', () => {
    const pending = sources({ configs: [], configsLoaded: false })
    expect(resolveReviewModel({ reviewId: 'a' }, pending).stale).toBe(false)
    expect(taskModelDisplayText(resolveReviewModel({ reviewId: 'a' }, pending))).toBe('环境默认')
    expect(resolveAgentModel({ reactId: 'b' }, pending).stale).toBe(false)
  })

  it('清单拉到之后才把找不到的 id 判成已删(请求失败保持 false → 永不误报)', () => {
    expect(resolveReviewModel({ reviewId: 'gone' }, sources()).stale).toBe(true)
    expect(resolveReviewModel({ reviewId: 'gone' }, sources({ configsLoaded: false })).stale).toBe(false)
  })
})

describe('defaultOptionLabel', () => {
  it('槽位已设时把槽位模型名带进括号', () => {
    expect(defaultOptionLabel(sources({ slotLabel: '我的默认' }))).toBe('默认模型（我的默认）')
  })

  it('只有环境默认时标明是环境默认;两者都没有时不编名字', () => {
    expect(defaultOptionLabel(sources({ envModel: 'qwen-max' }))).toBe('默认模型（环境默认：qwen-max）')
    expect(defaultOptionLabel(sources())).toBe('默认模型(环境变量)')
  })
})
