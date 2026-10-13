/**
 * 回归守卫:检查助手核查过程的"逐 token 渲染成本"接线(静态扫描源码)。
 *
 * 背景(生产任务 ba48dbe4 排查结论):后端每个 LLM chunk 推一条 thinking_delta
 * (实测 20-30 条/秒),一条增量写进 reactive 状态就让整个面板重渲染一次;而重渲染
 * 成本按"本轮已落库的思考链条目数"叠加——16 条共 4.4 万字时单次实测 70ms,主线程
 * 被占满,界面表现为"看检查助手核查时卡死十几二十秒"。
 *
 * 钉住两侧各自的形状,任何一侧被改回去都会重新引入卡顿:
 *
 * 1. Agent2Panel:正文 markdown 必须走带缓存的 mdHtml,且折叠着不渲染 —— 直接在
 *    模板里写 v-html="renderMarkdown(...)" 就等于把解析成本扣在每个 token 上;
 * 2. TaskDetailView:thinking_delta 只许进 deltaBuffer(每帧一次),边界相位与
 *    落库退役/整页快照/卸载这些时刻必须先 flush 再动卡片,置底降到每帧一次。
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

describe('Agent2Panel:正文渲染不随 token 重复解析', () => {
  it('模板里不得内联调用 renderMarkdown(必须经 mdHtml 缓存)', () => {
    expect(PANEL).not.toContain('v-html="renderMarkdown(')
    const md = sliceFrom(PANEL, 'function mdHtml(')
    expect(md).toContain('mdCache.get(key)')
    expect(md).toContain('if (hit !== undefined) return hit')
    expect(md).toContain('renderMarkdown(text)')
  })

  it('缓存按条目 key(=落库记录 id),并有上限防整页快照堆积', () => {
    expect(PANEL).toContain('const mdCache = new Map<string, string>()')
    expect(PANEL).toContain('const MD_CACHE_MAX = 400')
    // 思考链与最终总结都走缓存
    expect(PANEL).toContain('v-html="mdHtml(e.key, e.conv.reasoning || e.conv.content)"')
    expect(PANEL).toContain('v-html="mdHtml(e.key, e.conv.content)"')
  })

  it('折叠的历史条目不渲染正文(展开意图记在 openEntries)', () => {
    expect(PANEL).toContain('const openEntries = ref<Set<string>>(new Set())')
    expect(PANEL).toContain('@toggle="onEntryToggle(e.key, $event)"')
    expect(PANEL).toContain('v-if="openEntries.has(e.key)"')
  })

  it('实时流式卡仍是纯文本(不解析 markdown),逐 token 只有它该变', () => {
    const card = sliceFrom(PANEL, `v-else-if="e.kind === 'stream'"`, 900)
    expect(card).toContain('v-text="streamText(e.stream)"')
    expect(card).not.toContain('mdHtml(')
  })
})

describe('TaskDetailView:thinking_delta 合帧', () => {
  it('接线存在,且增量只进缓冲', () => {
    expect(VIEW).toContain("import { createDeltaBuffer } from '@/utils/thinkingDeltaBuffer'")
    expect(VIEW).toContain('const deltaBuffer = createDeltaBuffer({')

    const fn = sliceFrom(VIEW, 'function handleThinkingDelta(', 3000)
    expect(fn).toContain("deltaBuffer.push(conv_id, 'reasoning', delta)")
    expect(fn).toContain("deltaBuffer.push(conv_id, 'content', delta)")
    // 改回逐 token 直写响应式状态就是本次要修的 bug
    expect(fn).not.toContain('cur.reasoning += delta')
    expect(fn).not.toContain('cur.content += delta')
  })

  it('卡片不存在时增量作废(落库行带全文接管),不往已退役的卡里写字', () => {
    const buf = sliceFrom(VIEW, 'const deltaBuffer = createDeltaBuffer({', 1200)
    expect(buf.match(/if \(!item\) return/g)).toHaveLength(2)
    expect(buf).toContain('streamingItems.get(convId)')
  })

  it('边界相位先 flush 再收尾(否则最后一段增量烂在缓冲里)', () => {
    const fn = sliceFrom(VIEW, 'function handleThinkingDelta(', 3000)
    expect(fn.match(/deltaBuffer\.flush\(\)/g)).toHaveLength(3) // notice / error / end
  })

  it('落库退役与整页快照先冲缓冲再动卡片;卸载与切任务清缓冲', () => {
    const conv = sliceFrom(VIEW, 'onConversation: (data) => {', 600)
    expect(conv).toContain('deltaBuffer.flush()')

    const snap = sliceFrom(VIEW, 'function applyTaskSnapshot(', 1200)
    // 顺序即正确性:flush 必须排在 streamingItems.clear() 之前
    expect(snap.indexOf('deltaBuffer.flush()')).toBeLessThan(snap.indexOf('streamingItems.clear()'))
    expect(snap).toContain('deltaBuffer.clear()')

    expect(sliceFrom(VIEW, 'function resetTaskState(', 700)).toContain('deltaBuffer.clear()')
    expect(sliceFrom(VIEW, 'onUnmounted(() => {', 400)).toContain('deltaBuffer.clear()')
  })

  it('置底降频到每帧一次,不再逐条增量 nextTick', () => {
    const cb = sliceFrom(VIEW, 'onThinkingDelta: (data) => {', 200)
    expect(cb).toContain('handleThinkingDelta(data)')
    expect(cb).not.toContain('nextTick(scrollToBottom)')

    const buf = sliceFrom(VIEW, 'const deltaBuffer = createDeltaBuffer({', 1200)
    expect(buf).toContain('afterFlush()')
    expect(buf).toContain('nextTick(scrollToBottom)')
  })
})
