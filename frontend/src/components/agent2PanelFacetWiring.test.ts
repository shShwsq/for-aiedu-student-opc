/**
 * 回归守卫:agent2 计划项并行核查的前端接线(静态扫描源码)。
 *
 * 镜像 taskDetailViewFacetWiring.test.ts 的思路:"后端 thinking_delta 带
 * facet,Agent2Panel 不接就等于没改"。钉住三处形状:
 *
 * 1. Agent2Panel 的 StreamingLike 声明可选 facet(与 TaskDetailView 的
 *    StreamingItem 同一张卡片实体,handleThinkingDelta 已按 conv_id 分桶写入);
 * 2. 实时流式卡 summary 在 e.stream.facet 存在时渲染"核查 · X"身份标签,
 *    无 facet(串行/老任务)不渲染;
 * 3. streamingItems Map 由 props 传入消费(多路并行天然按 conv_id 隔离)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const PANEL = readFileSync(resolve(process.cwd(), 'src/components/Agent2Panel.vue'), 'utf-8')
const VIEW = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')

/** 取定位串之后的一段源码(找不到定位串就直接失败,免得断言空跑) */
function sliceFrom(src: string, anchor: string, chars = 2000): string {
  const at = src.indexOf(anchor)
  expect(at, `源码中找不到定位串: ${anchor}`).toBeGreaterThanOrEqual(0)
  return src.slice(at, at + chars)
}

describe('agent2 计划项并行核查:侧栏思考卡身份标签', () => {
  it('StreamingLike 声明可选 facet(与主对话流同一张卡片实体的字段对齐)', () => {
    expect(sliceFrom(PANEL, 'interface StreamingLike {', 800)).toContain('facet?: string')
  })

  it('facet 存在才渲染"核查 · X"标签;无 facet 不渲染', () => {
    expect(PANEL).toContain('v-if="e.stream.facet"')
    expect(PANEL).toContain('核查 · {{ e.stream.facet }}')
  })

  it('实时卡片来自 props.streamingItems(上游 handleThinkingDelta 已按 conv_id 分桶写入 facet)', () => {
    expect(PANEL).toContain('streamingItems: Map<string, StreamingLike>')
    // 上游接线(TaskDetailView)建卡时写入 facet —— 分桶键仍是 conv_id,多路并行不串扰
    const fn = sliceFrom(VIEW, 'function handleThinkingDelta(')
    expect(fn).toContain('const { conv_id, round_idx, role, phase, delta, verify, facet } = data')
    expect(fn).toContain('streamingItems.set(conv_id, {')
  })
})
