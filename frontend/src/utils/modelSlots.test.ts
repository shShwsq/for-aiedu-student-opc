/**
 * utils/modelSlots 单元测试
 *
 * 锁死「任务模型默认」页的两条口径:
 * 1. 生效模型文案只在后端判定命中来源时才报模型名(不谎报)
 * 2. 编辑入口深链拆成 RouterLink 的 path + hash(整串当 path 会跳不到子项)
 */
import { describe, expect, it } from 'vitest'

import { editLabel, editTarget, effectiveLabel, thinkingModeLabel } from './modelSlots'
import type { ModelSlot } from '@/types/model_slots'

function slot(patch: Partial<ModelSlot> = {}): ModelSlot {
  return {
    slot: 'practice_generate',
    label: '默认出题模型',
    description: '',
    edit_path: '/settings/practice#generation',
    writable: false,
    task_override: true,
    has_thinking_mode: true,
    llm_config_id: null,
    config_label: '',
    thinking_mode: 'follow',
    source: 'env',
    stale: false,
    ...patch,
  } as ModelSlot
}

describe('effectiveLabel', () => {
  it('命中用户级默认时报配置名', () => {
    expect(
      effectiveLabel(slot({ source: 'slot', config_label: 'A(dashscope / qwen-max)' }), 'qwen3.6-flash'),
    ).toBe('A(dashscope / qwen-max)')
  })

  it('未命中时标注环境默认并带上环境模型名', () => {
    expect(effectiveLabel(slot(), 'qwen3.6-flash')).toBe('环境默认(qwen3.6-flash)')
  })

  it('环境模型名未知时不编名字', () => {
    expect(effectiveLabel(slot(), '')).toBe('环境默认')
  })

  it('后端说 slot 但没给配置名时按环境默认展示(不显示空白)', () => {
    expect(effectiveLabel(slot({ source: 'slot', config_label: '' }), 'glm-4')).toBe('环境默认(glm-4)')
  })
})

describe('editTarget', () => {
  it('带 hash 的深链拆成 path + hash', () => {
    expect(editTarget('/settings/practice#generation')).toEqual({
      path: '/settings/practice',
      hash: '#generation',
    })
  })

  it('不带 hash 时不写 hash 字段(落到一级页默认子项)', () => {
    expect(editTarget('/settings/policy')).toEqual({ path: '/settings/policy' })
  })
})

describe('editLabel', () => {
  it('按归属面板给文案', () => {
    expect(editLabel('/settings/practice#generation')).toBe('去练习设置')
    expect(editLabel('/settings/memory')).toBe('去记忆设置')
    expect(editLabel('/settings/policy')).toBe('去智能体策略')
  })

  it('未登记的面板回退通用文案(新增槽位不会显示空白)', () => {
    expect(editLabel('/settings/whatever#x')).toBe('去修改')
  })
})

describe('thinkingModeLabel', () => {
  it('三种模式都有人话文案', () => {
    expect(thinkingModeLabel('follow')).toBe('跟随模型配置')
    expect(thinkingModeLabel('on')).toBe('强制开启')
    expect(thinkingModeLabel('off')).toBe('强制关闭')
  })
})
