/**
 * 回归守卫:内置 react 分面并行的前端接线(静态扫描源码)。
 *
 * 视图不挂载测试(约定见 taskDetailViewSuggestionWiring.test.ts),这里钉住
 * "后端加了 facet 字段,前端不接就等于没改"的几处形状:
 *
 * 1. thinking_delta 携带 facet → handleThinkingDelta 解构并按 conv_id 分桶时写入
 *    实时卡(多路并行天然隔离,不串扰);
 * 2. conversation 落库事件携带 facet → onConversation 把 facet 落到历史卡;
 * 3. 历史卡退役仍走 findRetiredCardConvId(按 stream_conv_id 精确匹配);
 * 4. 思考卡在 facet 存在时渲染"分面 · X"标签,无 facet(串行/老任务)不渲染;
 * 5. 类型契约:ThinkingDeltaEventData / ConversationEventData 均带可选 facet。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const VIEW = readFileSync(resolve(process.cwd(), 'src/views/TaskDetailView.vue'), 'utf-8')
const MSG = readFileSync(resolve(process.cwd(), 'src/components/ConversationMessage.vue'), 'utf-8')
const TYPES = readFileSync(resolve(process.cwd(), 'src/types/task.ts'), 'utf-8')

/** 取定位串之后的一段源码(找不到定位串就直接失败,免得断言空跑) */
function sliceFrom(src: string, anchor: string, chars = 2000): string {
  const at = src.indexOf(anchor)
  expect(at, `源码中找不到定位串: ${anchor}`).toBeGreaterThanOrEqual(0)
  return src.slice(at, at + chars)
}

describe('分面并行:实时流式按 facet 身份分桶', () => {
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

describe('分面并行:思考卡渲染身份标签', () => {
  it('facet 存在才渲染"分面 · X"标签;无 facet 不渲染', () => {
    expect(MSG).toContain('v-if="item.streaming.facet"')
    expect(MSG).toContain('分面 · {{ item.streaming.facet }}')
    // 本地 StreamingItem 也声明 facet(可选)
    expect(sliceFrom(MSG, 'interface StreamingItem {', 700)).toContain('facet?: string')
  })
})

describe('分面并行:类型契约带可选 facet', () => {
  it('ThinkingDeltaEventData / ConversationEventData 均有 facet', () => {
    expect(sliceFrom(TYPES, 'interface ThinkingDeltaEventData', 900)).toContain('facet?: string')
    const convEvent = sliceFrom(TYPES, 'interface ConversationEventData', 900)
    expect(convEvent).toContain('facet?: string | null')
    expect(convEvent).toContain('parallel?: boolean')
  })
})
