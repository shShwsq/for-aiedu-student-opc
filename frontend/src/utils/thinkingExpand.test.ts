/**
 * 思考卡片展开状态判定的单元测试(纯函数,无 DOM)
 *
 * 覆盖回归点:
 * - 流式中跨过阈值才自动展开(短思考不展开,避免逐迭代频闪);
 * - 结束后自动折叠,但宽限期内保持展开(收起不与正文出现同帧);
 * - 用户手动点过之后,生命周期规则不得覆盖用户意图。
 */
import { describe, expect, it } from 'vitest'

import {
  AUTO_EXPAND_MIN_CHARS,
  isThinkingExpanded,
  shouldLatchAutoExpand,
  type ThinkingStateLike,
} from './thinkingExpand'

function mkState(overrides: Partial<ThinkingStateLike> = {}): ThinkingStateLike {
  return {
    status: 'streaming',
    reasoning: 'x'.repeat(AUTO_EXPAND_MIN_CHARS),
    ...overrides,
  }
}

describe('shouldLatchAutoExpand', () => {
  it('流式且达到阈值为真', () => {
    expect(shouldLatchAutoExpand(mkState())).toBe(true)
  })

  it('流式但未达阈值不展开(短思考保持单行标题)', () => {
    const short = mkState({ reasoning: '先看一眼目录结构。' })
    expect(shouldLatchAutoExpand(short)).toBe(false)
  })

  it('已结束的流式项不再触发闩锁', () => {
    expect(shouldLatchAutoExpand(mkState({ status: 'done' }))).toBe(false)
  })
})

describe('isThinkingExpanded', () => {
  it('流式中:闩锁置起则展开,未置起则折叠', () => {
    expect(isThinkingExpanded(mkState({ reasoning_auto: true }))).toBe(true)
    expect(isThinkingExpanded(mkState())).toBe(false)
  })

  it('结束后:宽限期内展开,过期折叠', () => {
    expect(isThinkingExpanded(mkState({ status: 'done', reasoning_grace: true }))).toBe(true)
    expect(isThinkingExpanded(mkState({ status: 'done', reasoning_auto: true }))).toBe(false)
  })

  it('error 结束同样折叠(不因为出错就留着展开)', () => {
    expect(isThinkingExpanded(mkState({ status: 'error' }))).toBe(false)
  })

  it('用户 pin 优先于流式闩锁与宽限期', () => {
    // 流式中用户手动收起:后续增量不得把他重新展开
    expect(
      isThinkingExpanded(mkState({ reasoning_auto: true, reasoning_pin: false })),
    ).toBe(false)
    // 结束后用户手动留着看:不得被自动收起规则吞掉
    expect(
      isThinkingExpanded(mkState({ status: 'done', reasoning_pin: true })),
    ).toBe(true)
  })
})
