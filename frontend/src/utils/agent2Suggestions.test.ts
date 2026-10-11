import { describe, it, expect } from 'vitest'
import { extractSuggestions } from './agent2Suggestions'
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
