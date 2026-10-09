/**
 * 检查助手侧栏时间轴排序的单元测试
 *
 * 覆盖回归点(都是修复前的真实症状):
 * 1. 思考与工具按真实时序穿插,不再"整轮思考堆在工具之后";
 * 2. 实时流式卡片排在本轮已落库思考之后,不再压在旧思考之上(读起来像倒叙);
 * 3. tool_result 按 tool_call_id 归到对应 call 的行上,并行调用错开落库也不散位;
 * 4. 孤儿 tool_result 与未知 type 走兜底条目,不丢内容;
 * 5. seq 的下标基准含 agent1/user 消息(与 TaskDetailView 的 convCountPerRound
 *    同基准),否则实时卡片 insertSeq 会错位;
 * 6. suggestions 不进轮组(由面板独立区块渲染)。
 */
import { describe, expect, it } from 'vitest'

import {
  agent2RoundStats,
  buildAgent2Rounds,
  type Agent2ConvBase,
  type Agent2StreamBase,
} from './agent2Timeline'

function mkConv(overrides: Partial<Agent2ConvBase> & { id: string }): Agent2ConvBase {
  return {
    round_idx: 1,
    role: 'agent2',
    type: 'thinking',
    content: '',
    reasoning: '想一下',
    ...overrides,
  }
}

function mkStream(overrides: Partial<Agent2StreamBase> & { conv_id: string }): Agent2StreamBase {
  return {
    round_idx: 1,
    role: 'agent2',
    reasoning: '正在想',
    content: '',
    status: 'streaming',
    ...overrides,
  }
}

/** 取某轮条目的 kind 序列(顺序即界面渲染顺序) */
function kinds(round: { entries: { kind: string; key: string }[] }) {
  return round.entries.map((e) => `${e.kind}:${e.key}`)
}

describe('buildAgent2Rounds', () => {
  it('思考紧跟它触发的工具,按时序穿插而非分桶', () => {
    const rounds = buildAgent2Rounds(
      [
        mkConv({ id: 'th1', type: 'thinking' }),
        mkConv({ id: 'tc1', type: 'tool_call' }),
        mkConv({ id: 'tr1', type: 'tool_result', tool_call_id: 'tc1' }),
        mkConv({ id: 'th2', type: 'thinking' }),
        mkConv({ id: 'tc2', type: 'tool_call' }),
      ],
      [],
    )
    // 修复前:tools 一桶在前(thinking 全部堆到工具之后)
    expect(rounds[0].entries.map((e) => e.kind)).toEqual([
      'thinking',
      'tool',
      'thinking',
      'tool',
    ])
    expect(kinds(rounds[0])).toEqual(['thinking:th1', 'tool:tc1', 'thinking:th2', 'tool:tc2'])
  })

  it('tool_result 归到对应 call 的行上,不单独占位', () => {
    const rounds = buildAgent2Rounds(
      [
        mkConv({ id: 'tc1', type: 'tool_call' }),
        mkConv({ id: 'tc2', type: 'tool_call' }),
        // 并行调用:先发起 tc1,后完成的 tc1 结果却晚于 tc2 落库
        mkConv({ id: 'tr2', type: 'tool_result', tool_call_id: 'tc2' }),
        mkConv({ id: 'tr1', type: 'tool_result', tool_call_id: 'tc1' }),
      ],
      [],
    )
    const entries = rounds[0].entries
    expect(entries.map((e) => e.kind)).toEqual(['tool', 'tool'])
    const first = entries[0]
    expect(first.kind === 'tool' && first.result?.id).toBe('tr1')
    const second = entries[1]
    expect(second.kind === 'tool' && second.result?.id).toBe('tr2')
  })

  it('无 tool_call_id 的老数据按相邻配对,孤儿 result 落兜底条目', () => {
    const legacy = buildAgent2Rounds(
      [
        mkConv({ id: 'tc1', type: 'tool_call' }),
        mkConv({ id: 'tr1', type: 'tool_result' }),
      ],
      [],
    )
    expect(legacy[0].entries.map((e) => e.kind)).toEqual(['tool'])

    const orphan = buildAgent2Rounds([mkConv({ id: 'tr9', type: 'tool_result' })], [])
    expect(orphan[0].entries.map((e) => e.kind)).toEqual(['other'])

    const unknown = buildAgent2Rounds([mkConv({ id: 'x1', type: 'answer' })], [])
    expect(unknown[0].entries.map((e) => e.kind)).toEqual(['other'])
  })

  it('实时流式卡片排在本轮已落库思考之后(不再压在旧思考之上)', () => {
    const rounds = buildAgent2Rounds(
      [
        mkConv({ id: 'th1', type: 'thinking' }),
        mkConv({ id: 'tc1', type: 'tool_call' }),
        mkConv({ id: 'tr1', type: 'tool_result', tool_call_id: 'tc1' }),
      ],
      [mkStream({ conv_id: 'live2', insertSeq: 3 })],
    )
    // 修复前:streaming 一桶排在 thinking 之前 → 最新思考跳到旧思考上面
    expect(kinds(rounds[0])).toEqual([
      'thinking:th1',
      'tool:tc1',
      'stream:live2',
    ])
  })

  it('seq 下标基准含 agent1 消息、不含 user question(与 convCountPerRound 同基准)', () => {
    const rounds = buildAgent2Rounds(
      [
        mkConv({ id: 'a1', role: 'agent1', type: 'tool_call' }),
        mkConv({ id: 'u1', role: 'user', type: 'question' }),
        mkConv({ id: 'th1', type: 'thinking' }),
        mkConv({ id: 'th2', type: 'thinking' }),
      ],
      [mkStream({ conv_id: 'live', insertSeq: 2 })],
    )
    // 下标基准:a1 计入(占 0 号位)、u1 不计,故 th1 落在下标 1、th2 落在下标 2。
    // 直接钉死 seq,才能同时抓住两种回归:误排 agent1 → th1.seq=0;误计 user
    // question → th1.seq=2000。此前只断言"th1 排在 live 之前"的相对顺序,th1
    // 无论算成下标 0 还是 1 都 < live,两种实现都成立,等于没守住这个回归点。
    const seqOf = (key: string) => rounds[0].entries.find((e) => e.key === key)?.seq
    expect(seqOf('th1')).toBe(1000)
    expect(seqOf('th2')).toBe(2000)
    // live 插在下标 2 的槽(2*1000-500=1500)→ 夹在 th1 与 th2 之间
    expect(kinds(rounds[0])).toEqual(['thinking:th1', 'stream:live', 'thinking:th2'])
  })

  it('insertSeq 缺失时退化为本轮末尾(流式卡片就是最新一段)', () => {
    const rounds = buildAgent2Rounds(
      [mkConv({ id: 'th1', type: 'thinking' }), mkConv({ id: 'tc1', type: 'tool_call' })],
      [mkStream({ conv_id: 'live' })],
    )
    expect(kinds(rounds[0]).slice(-1)).toEqual(['stream:live'])
  })

  it('轮次升序,且只收 agent2 条目;suggestions 不进轮组', () => {
    const rounds = buildAgent2Rounds(
      [
        mkConv({ id: 'r2_th', round_idx: 2 }),
        mkConv({ id: 'r1_sg', round_idx: 1, type: 'suggestions', content: '{}' }),
        mkConv({ id: 'r1_th', round_idx: 1 }),
        mkConv({ id: 'a1', role: 'agent1', type: 'thinking', round_idx: 1 }),
      ],
      [mkStream({ conv_id: 'r2_live', round_idx: 2, insertSeq: 1 })],
    )
    expect(rounds.map((r) => r.round_idx)).toEqual([1, 2])
    expect(kinds(rounds[0])).toEqual(['thinking:r1_th'])
    expect(kinds(rounds[1])).toEqual(['thinking:r2_th', 'stream:r2_live'])
  })

  it('实时卡片与同名落库记录同时存在时不去重(宁可短暂重复也不吞思考)', () => {
    const rounds = buildAgent2Rounds(
      [mkConv({ id: 'th1', reasoning: '同一段思考' })],
      [mkStream({ conv_id: 'live1', reasoning: '同一段思考', insertSeq: 1 })],
    )
    expect(rounds[0].entries).toHaveLength(2)
  })

  it('按 seq 稳定排序,同插槽保持传入顺序', () => {
    const rounds = buildAgent2Rounds(
      [],
      [
        mkStream({ conv_id: 'liveA', insertSeq: 0 }),
        mkStream({ conv_id: 'liveB', insertSeq: 0 }),
      ],
    )
    expect(kinds(rounds[0])).toEqual(['stream:liveA', 'stream:liveB'])
  })
})

describe('agent2RoundStats', () => {
  const MARKERS = ['评估完成,无需追问', '(未给出追问)', '请求用户澄清']

  it('从时间轴算出核查次数/真追问条数/是否已完成/活卡片数', () => {
    const rounds = buildAgent2Rounds(
      [
        mkConv({ id: 'th1' }),
        mkConv({ id: 'tc1', type: 'tool_call' }),
        mkConv({ id: 'tr1', type: 'tool_result', tool_call_id: 'tc1' }),
        mkConv({ id: 'ev1', type: 'evaluation', content: '请补充并发压测' }),
        mkConv({ id: 'ev2', type: 'evaluation', content: '评估完成,无需追问' }),
        mkConv({ id: 'sm1', type: 'summary', content: '结论' }),
      ],
      [],
    )
    expect(agent2RoundStats(rounds[0].entries, MARKERS)).toEqual({
      tools: 1,
      followups: 1,
      summaries: 1,
      live: 0,
    })
  })

  it('流式卡片计入 live(轮标题显示"核查中…")', () => {
    const rounds = buildAgent2Rounds([], [mkStream({ conv_id: 'live' })])
    expect(agent2RoundStats(rounds[0].entries, MARKERS).live).toBe(1)
  })
})
