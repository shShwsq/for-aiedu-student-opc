/**
 * 检查助手侧栏:agent2 消息 → 时序时间轴(纯函数,无 Vue/DOM 依赖)
 *
 * 动机:侧栏若按 type 分桶(工具一桶、思考一桶)渲染,整轮思考会全部堆在工具
 * 之后,运行中时实时流式卡片又压在已落库的历史思考之上——既看不出"为什么做这
 * 一步核查",读起来还像倒叙。本模块把一轮的 agent2 消息算成一条时序穿插的
 * 条目流(思考 → 它触发的工具 → 下一段思考 …),因果链保持完整。
 *
 * 定位口径与主对话流(TaskDetailView 的 roundGroups)完全一致,两边是同一张
 * 时间轴的两个视图:
 * - 落库记录 seq = 该 round 内的下标 * 1000(下标计数含 agent1/user 消息,
 *   与 convCountPerRound 同基准,并跳过 user question);
 * - 实时流式卡片 seq = insertSeq * 1000 - 500,insertSeq = 该卡片开始时该
 *   round 已收到的正式对话数 → 正好插在"它之后那批工具"之前。
 *
 * 为什么不按 created_at 排:落库时间戳来自服务端、流式卡片的 started_at 来自
 * 客户端,两端时钟有偏移,跨源比较会把实时卡片排到错误位置。主对话流同处也是
 * 这个取舍(见 roundGroups:"用 seq 排序,稳定,不依赖跨来源的 created_at")。
 * 两套 seq 同源,留 1000 的间隔正是为了让 -500 的插槽成立。
 *
 * 工具配对复用 toolSummary.buildToolSegments:按 tool_call_id 精确配对
 * (并行调用时 result 按完成顺序落库、不紧跟 call),老数据缺 id 时回退相邻配对,
 * 落单 result 归入兜底条目。
 *
 * 泛型 C/S 让调用方拿到自己声明的具体类型(Conversation / StreamingItem),
 * 本模块只声明排序与配对真正依赖的最小字段面。
 */

import { buildToolSegments } from './toolSummary'

/** 落库对话记录的最小字段面(types/task.Conversation 满足此接口) */
export interface Agent2ConvBase {
  id: string
  round_idx: number
  role: string
  type: string
  content: string
  reasoning?: string | null
  /** 仅 tool_result 有:对应 tool_call 的 id */
  tool_call_id?: string | null
}

/** 实时流式卡片的最小字段面(TaskDetailView 的 StreamingItem 满足此接口) */
export interface Agent2StreamBase {
  conv_id: string
  round_idx: number
  role: string
  reasoning: string
  content: string
  status: string
  /** 卡片开始时该 round 已收到的正式对话数(与 convCountPerRound 同基准) */
  insertSeq?: number
}

/**
 * 时间轴条目:一个条目 = 面板里的一行。
 * thinking=已落库思考链,stream=实时流式思考,tool=一次工具核查(call 带配对
 * result),review/evaluation/summary=结论类落库记录,other=兜底(含孤儿 result
 * 与老数据未知 type)。
 */
export type Agent2TimelineEntry<C extends Agent2ConvBase, S extends Agent2StreamBase> =
  | { kind: 'thinking'; key: string; seq: number; conv: C }
  | { kind: 'stream'; key: string; seq: number; stream: S }
  | { kind: 'tool'; key: string; seq: number; call: C; result: C | null }
  | { kind: 'review'; key: string; seq: number; conv: C }
  | { kind: 'evaluation'; key: string; seq: number; conv: C }
  | { kind: 'summary'; key: string; seq: number; conv: C }
  | { kind: 'other'; key: string; seq: number; conv: C }

export interface Agent2RoundTimeline<C extends Agent2ConvBase, S extends Agent2StreamBase> {
  round_idx: number
  entries: Agent2TimelineEntry<C, S>[]
}

/** 落库下标 → seq 的间隔(留出 -500 插槽给实时流式卡片) */
const SEQ_GAP = 1000
/** 流式卡片插在"它之后那批工具"之前 */
const STREAM_OFFSET = 500

/** 不进轮组时间轴的 type(追问建议由面板独立区块渲染) */
const EXCLUDED_TYPES = new Set(['suggestions'])

/** 工具类 type(单独走配对渲染,其余按结论/思考类成行) */
const TOOL_TYPES = new Set(['tool_call', 'tool_result'])

function entryKind(
  type: string,
): 'thinking' | 'review' | 'evaluation' | 'summary' | 'other' {
  if (type === 'thinking') return 'thinking'
  if (type === 'review') return 'review'
  if (type === 'evaluation') return 'evaluation'
  if (type === 'summary') return 'summary'
  return 'other'
}

/**
 * 按轮构建 agent2 的时序时间轴。
 *
 * @param conversations 任务的**全部**对话(不要按 role 预筛——下标基准必须与
 *        主对话流 / convCountPerRound 一致,否则实时卡片的 insertSeq 会错位)
 * @param streams 实时流式卡片(调用方按 role='agent2' 过滤后传入)
 * @returns 轮次升序的时间轴;每轮 entries 按 seq 升序穿插
 */
export function buildAgent2Rounds<C extends Agent2ConvBase, S extends Agent2StreamBase>(
  conversations: C[],
  streams: S[],
): Agent2RoundTimeline<C, S>[] {
  // 第一遍:给每条对话按"该 round 内下标"定 seq(与主对话流同基准),
  // 同时按轮收集 agent2 的落库记录。
  const seqOf = new Map<string, number>()
  const idxPerRound = new Map<number, number>()
  const rowsPerRound = new Map<number, C[]>()
  for (const c of conversations) {
    // 用户指令不进 round 下标(与 roundGroups / convCountPerRound 同基准)
    if (c.role === 'user' && c.type === 'question') continue
    const localIdx = idxPerRound.get(c.round_idx) ?? 0
    idxPerRound.set(c.round_idx, localIdx + 1)
    if (c.role !== 'agent2' || EXCLUDED_TYPES.has(c.type)) continue
    seqOf.set(c.id, localIdx * SEQ_GAP)
    const rows = rowsPerRound.get(c.round_idx)
    if (rows) rows.push(c)
    else rowsPerRound.set(c.round_idx, [c])
  }

  const entriesPerRound = new Map<number, Agent2TimelineEntry<C, S>[]>()
  const ensure = (roundIdx: number): Agent2TimelineEntry<C, S>[] => {
    let list = entriesPerRound.get(roundIdx)
    if (!list) {
      list = []
      entriesPerRound.set(roundIdx, list)
    }
    return list
  }

  for (const [roundIdx, rows] of rowsPerRound) {
    const entries = ensure(roundIdx)
    // seq 已在第一遍按全量下标定好;取不到(理论上不该发生)时退到轮尾
    const seqOfConv = (c: C): number =>
      seqOf.get(c.id) ?? (idxPerRound.get(roundIdx) ?? 0) * SEQ_GAP

    // 工具项交给 buildToolSegments 配对:call 定行、result 附到 call 的槽位上,
    // 孤儿 result 落 plain 兜底(它们不进任何一张工具卡)。
    const toolRows = rows.filter((c) => TOOL_TYPES.has(c.type))
    const consumed = new Set<string>()
    for (const seg of buildToolSegments(toolRows)) {
      if (seg.kind === 'plain') {
        for (const it of seg.items) {
          consumed.add(it.id)
          entries.push({ kind: 'other', key: it.id, seq: seqOfConv(it), conv: it })
        }
        continue
      }
      consumed.add(seg.call.id)
      if (seg.result) consumed.add(seg.result.id)
      // 并行调用时 result 可能晚于别的工具落库:整条工具行按 call 的时刻定位,
      // 结果只作为该 call 的附属展示,不再单独占位。
      entries.push({
        kind: 'tool',
        key: seg.call.id,
        seq: seqOfConv(seg.call),
        call: seg.call,
        result: seg.result ?? null,
      })
    }

    // 非工具项:思考链与结论类记录各自成行(已配走/工具项在此跳过)
    for (const c of rows) {
      if (consumed.has(c.id) || TOOL_TYPES.has(c.type)) continue
      entries.push({
        kind: entryKind(c.type),
        key: c.id,
        seq: seqOfConv(c),
        conv: c,
      } as Agent2TimelineEntry<C, S>)
    }
  }

  // 实时流式卡片:落在"它开始时该 round 已有多少条对话"的插槽上。
  // insertSeq 缺失(异常形态)时退化为"排在本轮已落库记录之后"——
  // 流式卡片本就是当前最新的一段思考。
  for (const s of streams) {
    if (s.role !== 'agent2') continue
    const insertSeq =
      typeof s.insertSeq === 'number' && Number.isFinite(s.insertSeq)
        ? s.insertSeq
        : (idxPerRound.get(s.round_idx) ?? 0)
    ensure(s.round_idx).push({
      kind: 'stream',
      key: s.conv_id,
      seq: insertSeq * SEQ_GAP - STREAM_OFFSET,
      stream: s,
    })
  }

  // Array.prototype.sort 稳定:同一插槽(极少见,如审查与动态验证同时开跑)
  // 保持传入顺序,不会来回跳。
  return [...entriesPerRound.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([round_idx, entries]) => ({
      round_idx,
      entries: entries.sort((x, y) => x.seq - y.seq),
    }))
}

/** 轮摘要计数:从时间轴条目算出,供轮标题的" N 次核查 · …"文案使用 */
export interface Agent2RoundStats {
  /** 工具核查次数 */
  tools: number
  /** 真追问条数(评估内容不以"非追问标记"开头) */
  followups: number
  /** 最终结论条数 */
  summaries: number
  /** 活着的实时流式卡片数 */
  live: number
}

/**
 * 算轮标题计数。
 * @param nonFollowupMarkers 评估内容以这些前缀开头视为"没发追问"(与后端
 *        _UA_EVAL_NON_FOLLOWUP_MARKERS 对齐),由调用方传入以保持文案单一来源。
 */
export function agent2RoundStats<C extends Agent2ConvBase, S extends Agent2StreamBase>(
  entries: Agent2TimelineEntry<C, S>[],
  nonFollowupMarkers: string[],
): Agent2RoundStats {
  let tools = 0
  let followups = 0
  let summaries = 0
  let live = 0
  for (const e of entries) {
    if (e.kind === 'tool') tools += 1
    else if (e.kind === 'stream') live += 1
    else if (e.kind === 'summary') summaries += 1
    else if (e.kind === 'evaluation') {
      const content = (e.conv.content || '').trim()
      if (content && !nonFollowupMarkers.some((m) => content.startsWith(m))) followups += 1
    }
  }
  return { tools, followups, summaries, live }
}
