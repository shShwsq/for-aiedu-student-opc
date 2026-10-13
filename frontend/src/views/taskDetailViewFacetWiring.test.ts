/**
 * 回归守卫:内置 react 子任务并行的前端接线(静态扫描源码)。
 *
 * 视图不挂载测试(约定见 taskDetailViewSuggestionWiring.test.ts),这里钉住
 * "后端加了 facet 字段,前端不接就等于没改"的几处形状:
 *
 * 1. thinking_delta 携带 facet → handleThinkingDelta 解构并按 conv_id 分桶时写入
 *    实时卡(多路并行天然隔离,不串扰);
 * 2. conversation 落库事件携带 facet → onConversation 把 facet 落到历史卡;
 * 3. 历史卡退役仍走 findRetiredCardConvId(按 stream_conv_id 精确匹配);
 * 4. 思考卡在 facet 存在且块内未标身份时渲染"子任务 · X"标签,无 facet(串行/老任务)不渲染;
 * 5. 类型契约:ThinkingDeltaEventData / ConversationEventData 均带可选 facet;
 * 6. 每个子任务聚合成一个可折叠块:运行中默认展开(正文 max-height 内滚),
 *    fan-in 落库后整块自动折叠,手动点标题可覆盖自动规则;
 * 7. 块嵌在所属执行过程组内部(不是组外的兄弟段),且子任务轨的新思考不得
 *    覆盖上一段(归档缺失 = 块内只剩最后一段思考,历史行同样中招)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const VIEW = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')
const MSG = readFileSync(resolve(process.cwd(), 'src/components/ConversationMessage.vue'), 'utf-8')
const TYPES = readFileSync(resolve(process.cwd(), 'src/types/task.ts'), 'utf-8')
// 子任务块 markup 已抽成组件(供 step-body 多锚点复用),模板/样式锚点换到该文件
const SUBTASK = readFileSync(resolve(process.cwd(), 'src/components/SubtaskBlock.vue'), 'utf-8')

/** 取定位串之后的一段源码(找不到定位串就直接失败,免得断言空跑) */
function sliceFrom(src: string, anchor: string, chars = 2000): string {
  const at = src.indexOf(anchor)
  expect(at, `源码中找不到定位串: ${anchor}`).toBeGreaterThanOrEqual(0)
  return src.slice(at, at + chars)
}

describe('子任务并行:实时流式按 facet 身份分桶', () => {
  it('handleThinkingDelta 解构 facet,并按 conv_id 建卡时写入 facet', () => {
    const fn = sliceFrom(VIEW, 'function handleThinkingDelta(')
    expect(fn).toContain('const { conv_id, round_idx, role, phase, delta, verify, facet } = data')
    // 两处建卡(start 事件 / 缺 start 兜底)都以 conv_id 为 key 且带 facet
    expect(fn.match(/streamingItems\.set\(conv_id, \{/g)).toHaveLength(2)
    expect(fn.match(/facet,\r?\n/g)).not.toBeNull()
    // 分桶键仍是 conv_id(天然隔离,不串扰)
    expect(fn).toContain('streamingItems.get(conv_id)')
  })

  it('onConversation 把落库 facet 写入历史卡,退役仍按 stream_conv_id 对账', () => {
    // 切片放宽到 3500:头部新增的「用户补充消息转入」块把 facet 行顶出了
    // 默认 2000 窗口(仍在 onConversationUpdate 之前,断言对象不变)
    const fn = sliceFrom(VIEW, 'onConversation: (data) => {', 3500)
    expect(fn).toContain('facet: data.facet ?? null,')
    expect(fn).toContain('findRetiredCardConvId(streamingItems.values(), data)')
  })
})

describe('子任务并行:思考卡渲染身份标签', () => {
  it('facet 存在且块内未标身份时才渲染"子任务 · X";块内隐藏(身份由块标题承载)', () => {
    expect(MSG).toContain('v-if="item.streaming.facet && !hideFacetBadge"')
    expect(MSG).toContain('子任务 · {{ item.streaming.facet }}')
    expect(sliceFrom(MSG, 'const props = defineProps<{', 400)).toContain('hideFacetBadge?: boolean')
    // 本地 StreamingItem 也声明 facet(可选)
    expect(sliceFrom(MSG, 'interface StreamingItem {', 700)).toContain('facet?: string')
  })
})

describe('子任务并行:类型契约带可选 facet', () => {
  it('ThinkingDeltaEventData / ConversationEventData 均有 facet', () => {
    expect(sliceFrom(TYPES, 'interface ThinkingDeltaEventData', 900)).toContain('facet?: string')
    const convEvent = sliceFrom(TYPES, 'interface ConversationEventData', 900)
    expect(convEvent).toContain('facet?: string | null')
    expect(convEvent).toContain('parallel?: boolean')
  })
})

describe('子任务块:整块聚合 + 运行中展开 / 完成自动折叠', () => {
  it('双轨分流:facet 项不进主轨,聚合为 SubtaskSegment', () => {
    expect(VIEW).toContain('const facetTracks = new Map<string, FacetTrack>()')
    expect(VIEW).toContain('const facet = itemFacetOf(item)')
    expect(VIEW).toContain("kind: 'subtask',")
    // 主轨变量名保持 iterations(结论提取/plain 定位仍只认主轨)
    expect(VIEW).toContain('const iterations: IterationSegment[] = []')
    // 放置锚点取该子任务首现时的主轨迭代序号
    expect(sliceFrom(VIEW, 'const ensureTrack = (label: string): FacetTrack', 600))
      .toContain('afterMainIdx: iterCounter')
  })

  it('子任务轨归档上一段(否则新思考覆盖旧思考,块内只剩最后一段)', () => {
    expect(VIEW).toContain('const closeTrack = (t: FacetTrack)')
    // closeTracks 复用单轨归档,不双份实现
    expect(sliceFrom(VIEW, 'const closeTracks = (', 200)).toContain('closeTrack(t)')
    // 关键:facet 思考开新迭代前先归档上一段(与主轨 closeCurrent 对称)
    const branch = sliceFrom(VIEW, 'if (isReactThinkingItem(item)) {', 500)
    expect(branch).toContain('closeTrack(t)')
  })

  it('派发工具的 tool_result 按 tool_call_id 认领回主轨(否则漏进子任务块)', () => {
    const fn = sliceFrom(VIEW, 'function claimBackByToolCallId(')
    expect(fn).toContain('item.tool_call_id')
    expect(fn).toContain('iter.toolItems.push(item)')
    // 调用点存在(漏调 = 永远走兜底)
    expect(VIEW).toContain(
      'claimBackByToolCallId(item, current ? [...iterations, current] : iterations)',
    )
  })

  it('子任务块 markup 在 SubtaskBlock 组件里:标题「子任务 · X」+ max-height 内滚正文', () => {
    const tpl = sliceFrom(SUBTASK, 'class="subtask-header"', 1400)
    expect(tpl).toContain('子任务 · {{ seg.label }}')
    expect(tpl).toContain('class="subtask-body"')
    expect(SUBTASK).toContain('max-height: min(60vh, 560px)')
    // 块内思考卡不再重复身份标(身份由块标题承载)
    expect(SUBTASK).toContain('hide-facet-badge')
  })

  it('子任务块嵌在执行过程组内部,不再作 round 级兄弟段', () => {
    // 组体携带嵌套子块的两个字段
    expect(sliceFrom(VIEW, 'interface StepGroup {', 1400)).toContain('subtasksByIter: Map<number, SubtaskSegment[]>')
    expect(sliceFrom(VIEW, 'interface StepGroup {', 1400)).toContain('tailSubtasks: SubtaskSegment[]')
    // 两个组构造点都必须初始化(漏一个 = 运行时 undefined)
    expect(VIEW.match(/subtasksByIter: new Map\(\),/g)).toHaveLength(2)
    expect(VIEW.match(/tailSubtasks: \[\],/g)).toHaveLength(2)
    // 按锚点定位渲染(0 = 首个迭代之前),非简单 append
    expect(VIEW).toContain('function subtasksAfter(group: StepGroup, iterIdx: number)')
    // 组体内出现子块,且 round 级模板分枝已删
    // 切片放到 4200:step-body 内要一直读到组尾的 tailSubtasks 兜底块
    const body = sliceFrom(VIEW, 'class="step-body"', 4200)
    expect(body).toContain('<SubtaskBlock')
    expect(body).toContain('seg.tailSubtasks')
    expect(VIEW).not.toContain(`v-else-if="seg.kind === 'subtask'"`)
  })

  it('含 live 子任务的组保持展开(嵌套后子块可见性受所属组约束)', () => {
    expect(VIEW).toContain('function groupHasLiveSubtask(group: StepGroup): boolean')
    const pass = sliceFrom(VIEW, 'const subLive = groupHasLiveSubtask(g)', 400)
    expect(pass).toContain('g.hasStreaming = true')
    expect(pass).toContain('leadingGroup || subLive')
    // 子任务灌组必须在前沿组判定之前(live 要计入嵌套子块)
    const attach = VIEW.indexOf('group.subtasksByIter.set(anchor, list)')
    const live = VIEW.indexOf('const subLive = groupHasLiveSubtask(g)')
    expect(attach).toBeGreaterThanOrEqual(0)
    expect(live).toBeGreaterThan(attach)
  })

  it('v-for key 对 subtask 单独分支(否则 seg.item 为 undefined 会崩)', () => {
    expect(VIEW).toContain(':key="segmentKey(seg)"')
    const fn = sliceFrom(VIEW, 'function segmentKey(seg: RoundSegment)')
    expect(fn).toContain('subtask-${seg.id}')
  })

  it('展开状态机复用子智能体块:运行中展开 / 完成折叠 + 手动意图优先', () => {
    const open = sliceFrom(VIEW, 'function subtaskOpen(')
    expect(open).toContain('isAgentBlockOpen(')
    expect(open).toContain('seg.hasResult')
    const toggle = sliceFrom(VIEW, 'function toggleSubtask(')
    expect(toggle).toContain('toggleAgentBlock(')
    // hasResult 判据取"块内有非流式行"(不用 hasStreaming,工具空档不频闪)
    expect(sliceFrom(VIEW, 'interface SubtaskSegment {', 1400)).toContain('hasResult: boolean')
    // 状态 key 带 round,避免同名子任务跨轮串键
    expect(sliceFrom(VIEW, 'function subtaskKey(')).toContain('subtask:${seg.roundIdx}:${seg.label}')
  })

  it('roundGroups 普通行带 facet(否则子任务落库行认不出身份)', () => {
    expect(VIEW).toContain('facet: c.facet ?? undefined,')
  })
})
