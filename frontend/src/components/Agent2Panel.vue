<script setup lang="ts">
/**
 * 检查助手(agent2)核查过程侧栏面板
 *
 * 主对话流只保留 用户↔AI助手 对话与追问修正卡(见 TaskDetailView 的
 * roundGroups 过滤);检查助手的思考流/审查结论/工具核查(读码/PoC/引用复核)/
 * 最终总结全部在本面板按轮折叠展示。
 *
 * 后台审查流程(agent1 结束即任务完成):
 * - reviewStatus=running:头部"检查中"badge,审查流式实时可见,可点"终止检查"
 * - reviewStatus=done:头部"检查完成"badge;建议追问卡(suggestions)可点"追问"
 * - reviewStatus=failed:头部"检查失败"badge(保留 AI助手执行结果)
 * - reviewStatus=stopped:头部"检查已终止"badge(用户主动停,同样保留执行结果)
 *
 * 数据来源:
 * - conversations:任务 Conversation 列表(role=agent2 的历史消息)
 * - streamingItems:SSE thinking_delta 累积的流式思考(与 TaskDetailView
 *   共用同一 reactive Map,本组件只读消费)
 */
import { computed, nextTick, ref, watch } from 'vue'
import type { Conversation, ReviewStatus } from '@/types/task'
import { renderMarkdown } from '@/utils/markdown'
import { isThinkingExpanded } from '@/utils/thinkingExpand'

/** 与 TaskDetailView 内部 StreamingItem 对齐(本组件只读消费展开状态,写入走 toggle) */
interface StreamingLike {
  conv_id: string
  round_idx: number
  role: 'agent1' | 'agent2'
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  verify?: boolean
  /** 思考展开状态三要素(见 utils/thinkingExpand),与主对话流同一张卡片实体 */
  reasoning_auto?: boolean
  reasoning_grace?: boolean
  reasoning_pin?: boolean | null
}

interface ToolEntry {
  call: Conversation
  result: Conversation | null
}

interface RoundGroup {
  round_idx: number
  thinking: Conversation[]
  streaming: StreamingLike[]
  tools: ToolEntry[]
  evaluations: Conversation[]
  reviews: Conversation[]
  summaries: Conversation[]
  others: Conversation[]
}

const props = defineProps<{
  conversations: Conversation[]
  streamingItems: Map<string, StreamingLike>
  isRunning: boolean
  /** 后台审查状态(null=未审查:单 agent 模式/老任务) */
  reviewStatus?: ReviewStatus | null
  /** 终止请求已提交但尚未生效(审查线程还在收尾):按钮置灰防重复点 */
  stoppingReview?: boolean
}>()

/**
 * 事件:
 * - send-suggestion:用户点"追问"建议,交给父组件走现有发消息流程(resume)
 * - stop-review:用户终止本次检查(有些对话不需要检查),父组件调 API
 */
const emit = defineEmits<{
  (e: 'send-suggestion', text: string): void
  (e: 'stop-review'): void
}>()

// 后端落库的 tool_call 首行意图前缀(agents/agent2.py),展示时剥离
const TOOL_INTENT_PREFIX = /^\[agent2 质检\]\s*/
// 评估非追问内容标记(与 orchestrator._record_agent2 / routers/tasks.py
// _UA_EVAL_NON_FOLLOWUP_MARKERS 对齐)
const NON_FOLLOWUP_MARKERS = ['评估完成,无需追问', '(未给出追问)', '请求用户澄清']

/** agent2 全部消息按轮分组(tool_call 与 tool_result 按 id 配对) */
const rounds = computed<RoundGroup[]>(() => {
  const byRound = new Map<number, RoundGroup>()
  const ensure = (r: number): RoundGroup => {
    let g = byRound.get(r)
    if (!g) {
      g = {
        round_idx: r, thinking: [], streaming: [], tools: [],
        evaluations: [], reviews: [], summaries: [], others: [],
      }
      byRound.set(r, g)
    }
    return g
  }
  for (const c of props.conversations) {
    if (c.role !== 'agent2') continue
    const g = ensure(c.round_idx)
    if (c.type === 'thinking') g.thinking.push(c)
    else if (c.type === 'tool_call') g.tools.push({ call: c, result: null })
    else if (c.type === 'tool_result') {
      const hit = c.tool_call_id
        ? g.tools.find((t) => t.call.id === c.tool_call_id)
        : undefined
      if (hit) hit.result = c
      else g.others.push(c) // 孤立结果(调用记录缺失)兜底展示
    } else if (c.type === 'evaluation') g.evaluations.push(c)
    else if (c.type === 'review') g.reviews.push(c)
    else if (c.type === 'summary') g.summaries.push(c)
    else if (c.type === 'suggestions') {
      // suggestions 不进轮组:由下方独立区块渲染(追问卡片)
      continue
    } else if (c.type) g.others.push(c) // 未知 type 容错(老数据形态)
  }
  for (const s of props.streamingItems.values()) {
    if (s.role !== 'agent2') continue
    ensure(s.round_idx).streaming.push(s)
  }
  return [...byRound.values()].sort((a, b) => a.round_idx - b.round_idx)
})

/** 建议追问方向(取最新一条 type=suggestions 的 JSON,旧版整块覆盖) */
const suggestions = computed<string[]>(() => {
  let latest: Conversation | null = null
  for (const c of props.conversations) {
    if (c.role === 'agent2' && c.type === 'suggestions') latest = c
  }
  if (!latest) return []
  try {
    const payload = JSON.parse(latest.content) as { suggestions?: unknown }
    if (!Array.isArray(payload.suggestions)) return []
    return payload.suggestions.filter(
      (s): s is string => typeof s === 'string' && !!s.trim(),
    )
  } catch {
    return [] // JSON 解析失败(老数据/异常形态)静默容错
  }
})

/** 追问按钮防重复(点击后由父组件发消息,审查重新开始) */
const diggingSuggestion = ref<string | null>(null)

/** 点击"追问":把建议文本作为用户消息发出(emit 给父组件走 resume 链路) */
function handleDig(text: string): void {
  if (diggingSuggestion.value) return
  diggingSuggestion.value = text
  emit('send-suggestion', text)
}

/** 头部审查 badge 文案 */
const reviewBadge = computed<{ text: string; cls: string } | null>(() => {
  if (props.reviewStatus === 'running') return { text: '检查中', cls: 'is-running' }
  if (props.reviewStatus === 'done') return { text: '检查完成', cls: 'is-done' }
  if (props.reviewStatus === 'failed') return { text: '检查失败', cls: 'is-failed' }
  if (props.reviewStatus === 'stopped') return { text: '检查已终止', cls: 'is-stopped' }
  return null
})

// 新一轮审查开始(resume 追问/追加消息)→ 解除追问按钮的防重复锁
watch(
  () => props.reviewStatus,
  () => { diggingSuggestion.value = null },
)

/** 轮组展开状态;默认展开最新一轮(含运行中新轮自动展开) */
const expanded = ref<Set<number>>(new Set())

watch(
  () => rounds.value.length,
  (n, o) => {
    const latest = rounds.value[rounds.value.length - 1]
    if (!latest) return
    if (n > (o ?? 0) || o === undefined) expanded.value.add(latest.round_idx)
  },
  { immediate: true },
)

function toggleRound(r: number): void {
  expanded.value.has(r) ? expanded.value.delete(r) : expanded.value.add(r)
}

// ---- 流式思考文本自动贴底(仅当用户未向上滚动时) ----
const streamRefs = new Map<number, HTMLElement | null>()
function setStreamRef(r: number, el: unknown): void {
  streamRefs.set(r, (el as HTMLElement | null) || null)
}
watch(
  () => rounds.value,
  () => {
    nextTick(() => {
      for (const el of streamRefs.values()) {
        if (el) el.scrollTop = el.scrollHeight
      }
    })
  },
)

// ---- 展示辅助 ----

function firstLine(text: string | null | undefined): string {
  return (text || '').split('\n')[0] || ''
}

function truncate(text: string | null | undefined, n: number): string {
  const s = text || ''
  return s.length > n ? s.slice(0, n) + '…' : s
}

/** 工具调用单行摘要:取 intent 首行,剥质检前缀 */
function toolIntent(c: Conversation): string {
  return truncate(firstLine(c.content).replace(TOOL_INTENT_PREFIX, ''), 70)
}

/** 评估摘要:区分"评估完成"与"发出修正指令" */
function evalDigest(e: Conversation): string {
  const content = (e.content || '').trim()
  if (content.startsWith('评估完成')) return '评估完成'
  if (!content) return '评估'
  return `修正指令:${truncate(content, 50)}`
}

/** 轮组标题文案(轮组唯一标题,不显示轮次数字):
 *  流式中 → "核查中…",否则如 "3 次核查 · 1 条修正指令 · 已完成" */
function roundDigest(g: RoundGroup): string {
  if (g.streaming.length) return '核查中…'
  const parts: string[] = []
  const toolCount = g.tools.length
  if (toolCount) parts.push(`${toolCount} 次核查`)
  const followups = g.evaluations.filter((e) => {
    const c = (e.content || '').trim()
    return !!c && !NON_FOLLOWUP_MARKERS.some((m) => c.startsWith(m))
  }).length
  if (followups) parts.push(`${followups} 条修正指令`)
  if (g.summaries.length) parts.push('已完成')
  return parts.join(' · ') || '核查完成'
}

/** 流式思考文本(reasoning 优先,限量防止 DOM 过大) */
function streamText(s: StreamingLike): string {
  const text = s.reasoning || s.content || ''
  return text.length > 6000 ? text.slice(-6000) : text
}

function charCount(text: string | null | undefined): number {
  return (text || '').length
}

/**
 * 流式思考开合:用户手动开合也写回 pin(与主对话流同一张卡片实体,两处联动)。
 * 程序性改 open(宽限期到点折叠)也会触发 toggle,结果是幂等的:
 * pin 取当前展开值,不会把自动规则翻过来反噬自己。
 */
function onStreamToggle(s: StreamingLike, ev: Event): void {
  const open = (ev.target as HTMLDetailsElement).open
  if (isThinkingExpanded(s) !== open) s.reasoning_pin = open
}
</script>

<template>
  <section class="agent2-panel" data-onboarding="detail-agent2">
    <h2 class="panel-title">
      检查助手核查
      <span v-if="reviewBadge" :class="['panel-review-badge', reviewBadge.cls]">
        {{ reviewBadge.text }}
      </span>
      <span v-else-if="isRunning" class="panel-live-dot" aria-hidden="true" />
      <!-- 终止检查:审查动辄数分钟,有些对话不需要检查。
           后端是协作式取消(LLM 流 chunk 边界/工具循环边界生效),
           已提交未生效期间按 stoppingReview 置灰 -->
      <button
        v-if="reviewStatus === 'running'"
        type="button"
        class="panel-stop-btn"
        :disabled="stoppingReview"
        :title="stoppingReview
          ? '已提交终止请求,检查将在下一个检查点停止'
          : '停止本次后台核查,保留 AI助手执行结果'"
        @click="emit('stop-review')"
      >{{ stoppingReview ? '终止中...' : '终止检查' }}</button>
    </h2>

    <div v-for="g in rounds" :key="g.round_idx" class="panel-round">
      <button type="button" class="panel-round-head" @click="toggleRound(g.round_idx)">
        <span class="panel-toggle">{{ expanded.has(g.round_idx) ? '▼' : '▶' }}</span>
        <span class="panel-round-digest">{{ roundDigest(g) }}</span>
      </button>

      <div v-if="expanded.has(g.round_idx)" class="panel-round-body">
        <!-- 工具核查(读码核对 / PoC / 引用复核):执行步骤,提到思考之前、与思考同级 -->
        <details v-for="tool in g.tools" :key="tool.call.id" class="panel-item panel-tool">
          <summary>{{ toolIntent(tool.call) }}</summary>
          <div v-if="tool.result" class="panel-tool-result">{{ truncate(tool.result.content, 1500) }}</div>
          <div v-else class="panel-tool-pending">执行中…</div>
        </details>

        <!-- 实时流式思考(SSE thinking_delta):流式中自动展开、结束后折叠,
             与主对话流思考卡同一套生命周期规则(审查动辄数分钟,不能只看字数跑) -->
        <details
          v-for="s in g.streaming"
          :key="s.conv_id"
          class="panel-item panel-stream-item"
          :open="isThinkingExpanded(s)"
          @toggle="onStreamToggle(s, $event)"
        >
          <summary>
            <span :class="['panel-stream-label', { 'is-verify': s.verify }]">
              {{ s.verify ? '动态验证' : '思考中' }}{{ s.status === 'streaming' ? '…' : '' }}
            </span>
            <span class="panel-row-count">· {{ charCount(s.reasoning || s.content) }} 字</span>
          </summary>
          <div
            class="panel-stream-text"
            :ref="(el) => setStreamRef(g.round_idx, el)"
            v-text="streamText(s)"
          />
        </details>

        <!-- 历史思考链(刷新页面后由落库记录接管) -->
        <details v-for="t in g.thinking" :key="t.id" class="panel-item">
          <summary>思考链 · {{ charCount(t.reasoning || t.content) }} 字</summary>
          <div class="markdown-body panel-md" v-html="renderMarkdown(t.reasoning || t.content)" />
        </details>

        <!-- 审查结论(后台审查模式):结论行 + 展开完整审查 -->
        <details v-for="rv in g.reviews" :key="rv.id" class="panel-item panel-review">
          <summary>{{ firstLine(rv.content) || '审查结论' }}</summary>
          <pre class="panel-full-text">{{ rv.reasoning || rv.content }}</pre>
        </details>

        <!-- 评估(resume 消息分析):结论行 + 展开完整评估 -->
        <details v-for="e in g.evaluations" :key="e.id" class="panel-item panel-eval">
          <summary>{{ evalDigest(e) }}</summary>
          <pre class="panel-full-text">{{ e.reasoning || e.content }}</pre>
        </details>

        <!-- 最终总结(高亮) -->
        <div v-for="s in g.summaries" :key="s.id" class="panel-summary">
          <span class="panel-summary-label">最终结论</span>
          <div class="markdown-body panel-md" v-html="renderMarkdown(s.content)" />
        </div>

        <!-- 未知类型容错(老数据形态) -->
        <details v-for="o in g.others" :key="o.id" class="panel-item">
          <summary>{{ o.type }} · {{ truncate(firstLine(o.content), 50) }}</summary>
          <pre class="panel-full-text">{{ o.content }}</pre>
        </details>
      </div>
    </div>

    <!-- 建议追问方向(审查完成后;点击"追问"作为用户消息发出去走 resume) -->
    <div v-if="suggestions.length > 0" class="panel-suggestions">
      <p class="panel-suggestions-title">建议追问方向</p>
      <div
        v-for="(s, i) in suggestions"
        :key="i"
        :class="['panel-suggestion-card', { 'is-digging': diggingSuggestion === s }]"
      >
        <p class="panel-suggestion-text">{{ s }}</p>
        <button
          type="button"
          class="panel-suggestion-btn"
          :disabled="!!diggingSuggestion"
          @click="handleDig(s)"
        >{{ diggingSuggestion === s ? '已发起追问…' : '追问' }}</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.agent2-panel {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.panel-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text-primary);
}

/* 运行中呼吸点(与出题进度红点同风格) */
.panel-live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-success);
  animation: panel-pulse 1.4s ease-in-out infinite;
}

/* 后台审查状态 badge(检查中/检查完成/检查失败/检查已终止) */
.panel-review-badge {
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}

.panel-review-badge.is-running {
  color: var(--color-info);
  border-color: var(--color-info);
  animation: panel-pulse 1.4s ease-in-out infinite;
}

.panel-review-badge.is-done {
  color: var(--color-success);
  border-color: var(--color-success);
}

.panel-review-badge.is-failed {
  color: var(--color-warning);
  border-color: var(--color-warning);
}

/* 用户终止检查:中性偏灰(不是失败,是"没检查") */
.panel-review-badge.is-stopped {
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

/* 终止检查按钮(右上角,靠 badge 推开;小尺寸不抢面板标题权重) */
.panel-stop-btn {
  margin-left: auto;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.panel-stop-btn:hover:not(:disabled) {
  border-color: var(--color-danger);
  background: var(--color-bg-secondary);
}

.panel-stop-btn:disabled {
  color: var(--color-text-secondary);
  cursor: default;
  opacity: 0.7;
}

/* 审查结论(与评估同结构,中性色:审查不是修正指令) */
.panel-review summary {
  color: var(--color-text-secondary);
  font-weight: var(--fw-medium);
}

/* 建议追问卡片 */
.panel-suggestions {
  margin-top: var(--space-3);
}

.panel-suggestions-title {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text-primary);
}

.panel-suggestion-card {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-2);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.panel-suggestion-card.is-digging {
  border-color: var(--color-primary-border);
  background: var(--color-primary-light);
}

.panel-suggestion-text {
  flex: 1;
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.panel-suggestion-btn {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: transparent;
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.panel-suggestion-btn:hover:not(:disabled) {
  background: var(--color-primary-light);
}

.panel-suggestion-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

@keyframes panel-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.7); }
}

.panel-round {
  margin-bottom: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.panel-round-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-primary);
  background: var(--color-surface);
  border: none;
  cursor: pointer;
  text-align: left;
}

.panel-round-head:hover {
  background: var(--color-primary-light);
}

.panel-toggle {
  flex-shrink: 0;
  font-size: 10px;
  color: var(--color-text-tertiary);
}

/* 轮组标题 = 核查摘要文案(不显示轮次数字) */
.panel-round-digest {
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel-round-body {
  padding: var(--space-2) var(--space-3) var(--space-3);
  border-top: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* 流式思考(折叠行,与工具核查同级) */
.panel-stream-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-success);
}

.panel-stream-label.is-verify {
  color: var(--color-info);
}

/* 折叠行尾部的字数提示(思考中… · N 字) */
.panel-row-count {
  color: var(--color-text-tertiary);
}

/* 工具执行中(只收到 tool_call、结果未回)的占位 */
.panel-tool-pending {
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-tertiary);
}

.panel-stream-text {
  max-height: 220px;
  padding: var(--space-2);
  overflow-y: auto;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
}

/* 折叠条目(思考链 / 工具核查 / 评估) */
.panel-item summary {
  cursor: pointer;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  list-style: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.panel-item summary::-webkit-details-marker {
  display: none;
}

.panel-item summary::before {
  content: '▸ ';
  color: var(--color-text-tertiary);
}

.panel-item[open] summary::before {
  content: '▾ ';
}

.panel-item summary:hover {
  color: var(--color-primary);
}

.panel-item > *:not(summary) {
  margin-top: var(--space-1);
}

.panel-md {
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-2);
  font-size: var(--fs-xs);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.panel-tool-result {
  max-height: 200px;
  overflow-y: auto;
  padding: var(--space-2);
  font-size: var(--fs-xs);
  font-family: var(--font-mono, monospace);
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
}

.panel-eval summary {
  color: var(--color-warning);
  font-weight: var(--fw-medium);
}

.panel-full-text {
  max-height: 240px;
  overflow-y: auto;
  margin: var(--space-1) 0 0;
  padding: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-word;
}

/* 最终结论高亮卡 */
.panel-summary {
  padding: var(--space-2) var(--space-3);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-md);
}

.panel-summary-label {
  display: inline-block;
  margin-bottom: var(--space-1);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-primary);
}

.panel-summary .panel-md {
  max-height: none;
  background: transparent;
  border: none;
  padding: 0;
}
</style>
