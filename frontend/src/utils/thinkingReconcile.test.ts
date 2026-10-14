/**
 * 思考卡片 ↔ thinking 落库记录对账的单元测试(纯函数,无 DOM)
 *
 * 覆盖回归点:离开任务详情页再回来时,thinking 落库事件必须把对应的
 * 实时流式卡片退役,否则同一段思考会出现两份;匹配不上时宁可不退役
 * (不能把用户看得见的思考吞掉)。
 */
import { describe, expect, it } from 'vitest'

import {
  findRetiredCardConvId,
  isThinkingRecord,
  type StreamingCardLike,
  type ThinkingRecordLike,
} from './thinkingReconcile'

function mkCard(overrides: Partial<StreamingCardLike> = {}): StreamingCardLike {
  return {
    conv_id: 'card-1',
    round_idx: 1,
    role: 'agent1',
    reasoning: '先读仓库结构,再定位鉴权入口。',
    content: '',
    status: 'done',
    ...overrides,
  }
}

function mkRecord(overrides: Partial<ThinkingRecordLike> = {}): ThinkingRecordLike {
  return {
    round_idx: 1,
    role: 'agent1',
    type: 'thinking',
    content: '',
    reasoning: '先读仓库结构,再定位鉴权入口。',
    ...overrides,
  }
}

describe('isThinkingRecord', () => {
  it('仅 type=thinking 为真', () => {
    expect(isThinkingRecord(mkRecord())).toBe(true)
    expect(isThinkingRecord(mkRecord({ type: 'tool_call' }))).toBe(false)
  })
})

describe('findRetiredCardConvId', () => {
  it('非 thinking 记录不退役任何卡片', () => {
    const cards = [mkCard()]
    expect(findRetiredCardConvId(cards, mkRecord({ type: 'review' }))).toBeNull()
  })

  it('stream_conv_id 精确匹配优先(文本不同也照样退役)', () => {
    const cards = [mkCard({ conv_id: 'stream-7', reasoning: '完全不同的文本' })]
    const record = mkRecord({ stream_conv_id: 'stream-7', reasoning: '先读仓库结构' })
    expect(findRetiredCardConvId(cards, record)).toBe('stream-7')
  })

  it('stream_conv_id 指向不存在的卡片时回退文本对账', () => {
    const cards = [mkCard({ conv_id: 'card-a' })]
    const record = mkRecord({ stream_conv_id: 'unknown-id' })
    expect(findRetiredCardConvId(cards, record)).toBe('card-a')
  })

  it('无 stream_conv_id 时按 reasoning 文本相等退役(agent2 审查/动态验证场景)', () => {
    const cards = [mkCard({ role: 'agent2', conv_id: 'card-b' })]
    const record = mkRecord({ role: 'agent2' })
    expect(findRetiredCardConvId(cards, record)).toBe('card-b')
  })

  it('content 通道被后端加前缀时按包含关系退役', () => {
    const long = 'SQL 注入点在 user_service.get_user 的字符串拼接处,构造 PoC 验证可利用性。'
    const cards = [mkCard({ content: long, reasoning: '', conv_id: 'card-c' })]
    const record = mkRecord({ content: `[验证结果] ${long}`, reasoning: null })
    expect(findRetiredCardConvId(cards, record)).toBe('card-c')
  })

  it('仍在流式中的卡片不被退役(增量没收完不可能已落库)', () => {
    const cards = [mkCard({ status: 'streaming' })]
    expect(findRetiredCardConvId(cards, mkRecord())).toBeNull()
  })

  it('role 或 round 不一致时不退役', () => {
    const cards = [mkCard({ role: 'agent2' })]
    expect(findRetiredCardConvId(cards, mkRecord({ role: 'agent1' }))).toBeNull()
    const cards2 = [mkCard({ round_idx: 2 })]
    expect(findRetiredCardConvId(cards2, mkRecord({ round_idx: 1 }))).toBeNull()
  })

  it('短文本(<32 字符)不做包含匹配,避免误伤相邻相同短句', () => {
    const cards = [mkCard({ reasoning: '好的' })]
    const record = mkRecord({ reasoning: '好的,继续读取配置文件内容' })
    expect(findRetiredCardConvId(cards, record)).toBeNull()
  })

  it('短文本完全相等时仍可退役', () => {
    const cards = [mkCard({ reasoning: '好的' })]
    expect(findRetiredCardConvId(cards, mkRecord({ reasoning: '好的' }))).not.toBeNull()
  })

  it('多张候选卡片取最后一张(刚落库的是最近那段)', () => {
    const text = '先读仓库结构,再定位鉴权入口。'
    const cards = [
      mkCard({ conv_id: 'old', reasoning: text }),
      mkCard({ conv_id: 'new', reasoning: text }),
    ]
    expect(findRetiredCardConvId(cards, mkRecord({ reasoning: text }))).toBe('new')
  })

  it('空文本通道不参与匹配(防止 content 双方为空时误退役)', () => {
    const cards = [mkCard({ conv_id: 'x', reasoning: '独立思考文本', content: '' })]
    const record = mkRecord({ reasoning: null, content: '' })
    expect(findRetiredCardConvId(cards, record)).toBeNull()
  })

  it('子任务并行:同轮多张 facet 卡片按 stream_conv_id 各自退役不串扰', () => {
    // 两个子任务各一张实时卡(conv_id 唯一),落库记录带自己的 stream_conv_id,
    // 精确匹配只退役对应那张,另一张继续留在流式区
    const cards = [
      mkCard({ conv_id: 'facet-a', reasoning: '注入面思考文本,足够长可参与对账阈值以上。' }),
      mkCard({ conv_id: 'facet-b', reasoning: '并发面思考文本,足够长可参与对账阈值以上。' }),
    ]
    const record = mkRecord({
      stream_conv_id: 'facet-a',
      reasoning: '注入面思考文本,足够长可参与对账阈值以上。',
    })
    expect(findRetiredCardConvId(cards, record)).toBe('facet-a')
  })

  it('子任务并行:facet 落库记录缺 stream_conv_id 时按文本对账仍可定位对应卡片', () => {
    const shared = '两个子任务共有的一段较长思考文本内容用于文本对账兜底。'
    const cards = [
      mkCard({ conv_id: 'facet-a', reasoning: shared }),
      mkCard({ conv_id: 'facet-b', reasoning: '并发面独有且完全不同的另一段较长思考文本。' }),
    ]
    const record = mkRecord({ stream_conv_id: null, reasoning: shared })
    expect(findRetiredCardConvId(cards, record)).toBe('facet-a')
  })
})
