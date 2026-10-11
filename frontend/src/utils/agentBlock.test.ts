/**
 * 子智能体折叠块展开状态的单元测试(纯函数,无 DOM)。
 *
 * 回归诉求(本次改造):
 * - 运行中(尚无结果)默认展开,完成(已有结果)默认折叠;
 * - 用户手动展开/折叠优先,且跨“运行→完成”切换保持稳定,不被默认规则反覆盖。
 */
import { describe, expect, it } from 'vitest'

import { isAgentBlockOpen, toggleAgentBlock, type AgentBlockState } from './agentBlock'

function freshState(): AgentBlockState {
  return { expanded: new Set<string>(), collapsed: new Set<string>() }
}

describe('isAgentBlockOpen', () => {
  it('运行中默认展开、完成默认折叠(无手动干预)', () => {
    const s = freshState()
    expect(isAgentBlockOpen('a1', /* hasResult */ false, s)).toBe(true)
    expect(isAgentBlockOpen('a1', /* hasResult */ true, s)).toBe(false)
  })

  it('手动展开优先:完成后仍展开;手动折叠优先:运行中仍折叠', () => {
    const expandedOnly = freshState()
    expandedOnly.expanded.add('a1')
    expect(isAgentBlockOpen('a1', true, expandedOnly)).toBe(true)

    const collapsedOnly = freshState()
    collapsedOnly.collapsed.add('a1')
    expect(isAgentBlockOpen('a1', false, collapsedOnly)).toBe(false)
  })

  it('展开集与折叠集同时命中时展开优先(防御:理论不应并存)', () => {
    const s: AgentBlockState = { expanded: new Set(['a1']), collapsed: new Set(['a1']) }
    expect(isAgentBlockOpen('a1', true, s)).toBe(true)
  })
})

describe('toggleAgentBlock', () => {
  it('运行中点一次=手动折叠,切到完成后保持折叠(意图跨结果到达稳定)', () => {
    const s = freshState()
    // 运行中默认展开,点击 → 折叠
    expect(toggleAgentBlock('a1', /* hasResult */ false, s)).toBe(false)
    expect(s.collapsed.has('a1')).toBe(true)
    expect(s.expanded.has('a1')).toBe(false)
    // 完成后:用户已手动折叠,保持折叠
    expect(isAgentBlockOpen('a1', /* hasResult */ true, s)).toBe(false)
  })

  it('完成后默认折叠,点一次=手动展开;结果再次到达也保持展开', () => {
    const s = freshState()
    expect(isAgentBlockOpen('a1', /* hasResult */ true, s)).toBe(false)
    // 点击展开
    expect(toggleAgentBlock('a1', true, s)).toBe(true)
    expect(s.expanded.has('a1')).toBe(true)
    expect(s.collapsed.has('a1')).toBe(false)
    expect(isAgentBlockOpen('a1', true, s)).toBe(true)
  })

  it('再次点击从手动展开翻回手动折叠(集合互斥,不残留)', () => {
    const s = freshState()
    toggleAgentBlock('a1', true, s) // 展开
    const before = isAgentBlockOpen('a1', true, s)
    expect(before).toBe(true)
    expect(toggleAgentBlock('a1', true, s)).toBe(false)
    expect(s.expanded.has('a1')).toBe(false)
    expect(s.collapsed.has('a1')).toBe(true)
  })
})
