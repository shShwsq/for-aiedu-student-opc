import { describe, it, expect } from 'vitest'
import {
  SUGGESTION_CONV_TYPES,
  extractExploreSuggestions,
  extractSuggestions,
  isSuggestionConv,
} from './agent2Suggestions'
import type { Conversation } from '@/types/task'

/** 只填必要字段的 agent2 suggestions 记录 */
function suggestionConv(content: unknown, over: Partial<Conversation> = {}): Conversation {
  return {
    id: over.id ?? Math.random().toString(),
    round_idx: over.round_idx ?? 1,
    role: over.role ?? 'agent2',
    type: over.type ?? 'suggestions',
    content: typeof content === 'string' ? content : JSON.stringify(content),
    ...over,
  } as Conversation
}

describe('extractSuggestions', () => {
  it('解析单条建议数组', () => {
    const convs = [suggestionConv({ suggestions: ['查并发', '补错误处理'] })]
    expect(extractSuggestions(convs)).toEqual(['查并发', '补错误处理'])
  })

  it('多条时取最后一条(整块覆盖语义)', () => {
    const convs = [
      suggestionConv({ suggestions: ['旧一轮建议'] }),
      suggestionConv({ suggestions: ['最新一轮建议'] }),
    ]
    expect(extractSuggestions(convs)).toEqual(['最新一轮建议'])
  })

  it('忽略非 agent2 / 非 suggestions 的记录', () => {
    const convs = [
      { ...suggestionConv({ suggestions: ['用户消息'] }), role: 'user' } as Conversation,
      { ...suggestionConv({ suggestions: ['工具调用'] }), type: 'tool_call' } as Conversation,
      suggestionConv({ suggestions: ['有效建议'] }),
    ]
    expect(extractSuggestions(convs)).toEqual(['有效建议'])
  })

  it('无匹配记录 / 空列表 / undefined / null 均返回空数组', () => {
    expect(extractSuggestions([])).toEqual([])
    expect(extractSuggestions(undefined)).toEqual([])
    expect(extractSuggestions(null)).toEqual([])
    expect(
      extractSuggestions([
        { ...suggestionConv({ suggestions: ['x'] }), type: 'review' } as Conversation,
      ]),
    ).toEqual([])
  })

  it('非法 JSON 静默返回空数组(老数据 / 异常形态)', () => {
    expect(extractSuggestions([suggestionConv('{不是合法 JSON')])).toEqual([])
  })

  it('缺 content 字段静默返回空数组', () => {
    const broken = { id: '1', round_idx: 1, role: 'agent2', type: 'suggestions' } as Conversation
    expect(extractSuggestions([broken])).toEqual([])
  })

  it('suggestions 非数组(对象 / 字符串 / null / 缺字段)返回空数组', () => {
    for (const payload of [
      { suggestions: { a: 1 } },
      { suggestions: '查并发' },
      { suggestions: null },
      {},
    ]) {
      expect(extractSuggestions([suggestionConv(payload)])).toEqual([])
    }
  })

  it('过滤非字符串项与纯空白项', () => {
    const convs = [
      suggestionConv({ suggestions: ['  带空白  ', '', '   ', 42, null, { x: 1 }, '真建议'] }),
    ]
    expect(extractSuggestions(convs)).toEqual(['  带空白  ', '真建议'])
  })
})

describe('isSuggestionConv(时间轴排除 / 面板可见性 / 角标共用的判定)', () => {
  it('两类建议 type 都在清单里(新增 type 只改这一处)', () => {
    expect([...SUGGESTION_CONV_TYPES]).toEqual(['suggestions', 'followup_suggestions'])
  })

  it('agent2 的两类建议记录都判 true', () => {
    expect(isSuggestionConv(suggestionConv({ suggestions: ['缺口'] }))).toBe(true)
    expect(
      isSuggestionConv(suggestionConv({ suggestions: ['探索'] }, { type: 'followup_suggestions' })),
    ).toBe(true)
  })

  it('非 agent2 角色 / 非建议 type 都判 false(审查过程不能误排)', () => {
    expect(isSuggestionConv({ role: 'user', type: 'suggestions' })).toBe(false)
    expect(isSuggestionConv({ role: 'agent1', type: 'thinking' })).toBe(false)
    expect(isSuggestionConv({ role: 'agent2', type: 'review' })).toBe(false)
    expect(isSuggestionConv({ role: 'agent2', type: 'tool_call' })).toBe(false)
    expect(isSuggestionConv({})).toBe(false)
  })
})

describe('extractExploreSuggestions', () => {
  /** type=followup_suggestions 的 agent2 记录 */
  const explore = (content: unknown): Conversation =>
    suggestionConv(content, { type: 'followup_suggestions' })

  it('解析探索建议数组', () => {
    expect(extractExploreSuggestions([explore({ suggestions: ['看架构', '看安全'] })])).toEqual([
      '看架构',
      '看安全',
    ])
  })

  it('多条时取最后一条(整块覆盖语义)', () => {
    const convs = [explore({ suggestions: ['旧一轮'] }), explore({ suggestions: ['最新一轮'] })]
    expect(extractExploreSuggestions(convs)).toEqual(['最新一轮'])
  })

  it('忽略 type=suggestions 的缺口记录(两组互不串)', () => {
    const convs = [suggestionConv({ suggestions: ['缺口建议'] })]
    expect(extractExploreSuggestions(convs)).toEqual([])
  })

  it('容错口径同 extractSuggestions:非法 JSON / 非数组 / 无记录均空', () => {
    expect(extractExploreSuggestions([explore('不是 JSON')])).toEqual([])
    expect(extractExploreSuggestions([explore({ suggestions: '非数组' })])).toEqual([])
    expect(extractExploreSuggestions([])).toEqual([])
    expect(extractExploreSuggestions(null)).toEqual([])
  })
})

describe('两组互不串', () => {
  it('extractSuggestions 不误取 followup_suggestions 记录', () => {
    const convs = [suggestionConv({ suggestions: ['探索建议'] }, { type: 'followup_suggestions' })]
    expect(extractSuggestions(convs)).toEqual([])
  })
})
