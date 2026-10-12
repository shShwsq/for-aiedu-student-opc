/**
 * 回归守卫:子智能体卡片"诚实态"与轮结论外移的视图层接线(静态扫描源码)。
 *
 * 本项目的视图不做挂载测试(约定见 taskDetailViewSuggestionWiring.test.ts),
 * 纯函数侧覆盖在 utils/toolSummary.test.ts;这里只钉住 TaskDetailView 内部的
 * 接线形状 —— 这两处都是"数据在库里、视图不接就看不见"的典型:
 *
 * 1. 后台派生回执(Qoder Agent 工具 fire-and-forget)的卡片:旧版因 trace.status
 *    为 null 落到 else 分支打上"已完成",正文却只是"working in the background"
 *    启动回执;并且完成态会自动折叠,用户根本点不到那行谎。
 * 2. 本轮最终回答:旧规则要求"最后一个迭代是纯思考"才外移成结论,CLI 收尾前只要
 *    还有一次工具调用,整段结论就留在默认折叠的「执行过程」组里(任务 f4ff8649
 *    的 5409 字符报告正是这么被藏住的)。现在按后端 params._round_conclusion 的
 *    conv_id 定位,存量任务回退旧启发式。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// vitest 的工作目录是 frontend 根
const SRC = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')

/** 取定位串之后的一段源码(找不到定位串就直接失败,免得断言空跑) */
function sliceFrom(anchor: string, chars = 900): string {
  const at = SRC.indexOf(anchor)
  expect(at, `源码中找不到定位串: ${anchor}`).toBeGreaterThanOrEqual(0)
  return SRC.slice(at, at + chars)
}

describe('子智能体卡片:后台未回报的诚实态', () => {
  it('回执形状标"后台未回报",不再走"已完成"分支', () => {
    const block = sliceFrom("if (seg.kind === 'agent') {")
    expect(block).toContain('const asyncPending = !!trace?.asyncPending')
    expect(block).toContain("else if (asyncPending) tags.push(")
    expect(block).toContain("'后台未回报'")
    // "已完成"仍保留给真正有报告的卡片(不能一刀切)
    expect(block).toContain("else tags.push('已完成')")
  })

  it('未回报卡片不被自动折叠(展开判定用 hasResult && !agentAsync)', () => {
    const open = sliceFrom('function agentBlockOpen(')
    const toggle = sliceFrom('function toggleAgentBlockRow(')
    expect(open).toContain('row.hasResult && !row.agentAsync')
    expect(toggle).toContain('row.hasResult && !row.agentAsync')
  })

  it('回执不折叠成空壳:agentAsync 进入渲染行字段并有默认值', () => {
    expect(sliceFrom('interface ToolRenderRow {', 1400)).toContain('agentAsync: boolean')
    expect(sliceFrom('const empty = {', 600)).toContain('agentAsync: false')
  })
})

describe('轮结论外移:按后端指针定位,旧任务回退启发式', () => {
  it('读 task.params._round_conclusion 的 conv_id 作为指针', () => {
    const fn = sliceFrom('function conclusionConvIdOf(')
    expect(fn).toContain('_round_conclusion')
    expect(fn).toContain('conv_id')
  })

  it('思考行被还原成只读卡时 id 带 stream:history: 前缀,两种形式都要能命中', () => {
    expect(sliceFrom('function itemMatchesConvId(')).toContain('stream:history:${convId}')
  })

  it('segmentRoundItems 收到指针参数并优先按指针外移', () => {
    expect(sliceFrom('function segmentRoundItems(', 500)).toContain('conclusionConvId = ')
    const stage = sliceFrom('// 一阶段半:轮闭合时提取该轮最终结论', 1800)
    expect(stage).toContain('liftConclusionByConvId(iterations, conclusionConvId)')
    // 指针未命中(存量任务)仍走原位置启发式:两条路径并存,不是替换
    expect(stage).toContain('if (!conclusion) {')
    expect(stage).toContain('last.toolItems.length === 0 &&')
  })

  it('调用点把该轮指针传进去(漏传=永远走兜底,等于没接)', () => {
    expect(sliceFrom('segments: segmentRoundItems(')).toContain('conclusionConvIdOf(roundIdx)')
  })

  it('整块提出结论迭代时补回 plain 定位的虚拟槽位(位置语义不因新路径失真)', () => {
    const stage = sliceFrom('// 一阶段半:轮闭合时提取该轮最终结论', 1800)
    expect(stage).toContain('iterations.length < iterCountBefore')
    expect(SRC).toContain('iterations.length + (conclusionPopped ? 1 : 0)')
  })
})
