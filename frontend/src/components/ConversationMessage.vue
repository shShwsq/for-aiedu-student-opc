<script setup lang="ts">
/**
 * 协作对话流中的单条消息
 *
 * 从 TaskDetailView 抽出,用于复用:平铺消息(agent2 评估/追问/总结、user 指令)、
 * 迭代内的 thinking 项、工具调用/结果项、submit 项等。
 *
 * 渲染规则:
 * - reasoning 和 content 是两个独立的卡片,不再是嵌套在同一个外层容器里
 * - reasoning 卡片由生命周期驱动展开:流式中自动展开(过阈值)、结束后折叠
 *   (判定见 utils/thinkingExpand),点击 header 可钉住覆盖自动规则
 * - content 卡片用 role/type 对应配色
 */
import { computed, nextTick, ref, watch } from 'vue'
import { escapePlainText, renderMarkdown } from '@/utils/markdown'
import { isThinkingExpanded } from '@/utils/thinkingExpand'
import type { AttachmentInfo } from '@/types/task'

interface StreamingItem {
  conv_id: string
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  /** 展开状态三要素(见 utils/thinkingExpand),由父组件维护 */
  reasoning_auto?: boolean
  reasoning_grace?: boolean
  reasoning_pin?: boolean | null
  role?: 'agent1' | 'agent2'
}

interface DisplayItem {
  id: string
  created_at: string
  is_streaming: boolean
  role?: string
  type?: string
  content?: string
  /** 完整评估(可折叠回看,如 agent2 evaluation 的覆盖情况+判断) */
  reasoning?: string | null
  /** 仅 user 追问消息有:附带上传文件展示信息(只读渲染 chip) */
  attachments?: AttachmentInfo[] | null
  streaming?: StreamingItem
}

const props = defineProps<{
  item: DisplayItem
}>()

const emit = defineEmits<{
  'toggle-reasoning': [convId: string]
}>()

type MessageVariant = 'agent2' | 'agent1' | 'tool' | 'error' | 'summary' | 'streaming' | 'user-message'

/** 仅按 role/type 决定 content 卡片配色,不再生成文案标签 */
function getVariant(item: DisplayItem): MessageVariant {
  if (item.is_streaming && item.streaming) return 'streaming'
  if (item.role === 'agent2') {
    if (item.type === 'summary') return 'summary'
    return 'agent2'
  }
  if (item.role === 'agent1') {
    if (item.type === 'tool_call' || item.type === 'tool_result') return 'tool'
    if (item.type === 'error') return 'error'
    return 'agent1'
  }
  if (item.role === 'user') {
    // type=message:用户在对话输入框主动发送的补充消息,用专门配色(与顶部 userDirective 一致)
    // type=answer:用户对澄清提问的回答,用 tool 配色(等宽小字,内容通常较短)
    if (item.type === 'message') return 'user-message'
    return 'tool'
  }
  return 'agent1'
}

const variant = computed(() => getVariant(props.item))
const isActive = computed(
  () => !!(props.item.is_streaming && props.item.streaming?.status === 'streaming'),
)

/** 正式对话项 reasoning 的折叠状态(默认折叠,点击展开) */
const evalExpanded = ref(false)

/**
 * agent2 的结构化评估块(已覆盖/未覆盖/判断/追问)属内部编排记录,
 * 不作为"思考"卡片展示给用户;agent2 真实思考链以 type=thinking
 * 单独落库并还原为流式卡片。
 */
const isUaEvaluation = computed(
  () => props.item.role === 'agent2' && props.item.type === 'evaluation',
)

/** 流式项 reasoning 的展开状态(由父组件通过 streamingItems 维护,本处只读判定) */
const streamingExpanded = computed(() => {
  const s = props.item.streaming
  return s ? isThinkingExpanded(s) : false
})

/**
 * tool_call content 拆分:后端把意图放在首行,原始调用详情放后续行。
 * 首行作为卡片标题高亮显示,其余作为等宽详情。
 * 无换行时(旧数据兼容)返回 null,走普通渲染。
 *
 * intent 首行末尾可能带 [tool_name] 标签(供 TaskDetailView 提取工具名做 plan step 推断),
 * 展示时剥掉标签,只显示人类可读部分。
 */
const toolCallParts = computed<{ intent: string; detail: string } | null>(() => {
  if (props.item.type !== 'tool_call' || !props.item.content) return null
  const idx = props.item.content.indexOf('\n')
  if (idx < 0) return null
  const rawIntent = props.item.content.slice(0, idx)
  // 剥掉末尾的 [tool_name] 标签(展示用,不暴露内部工具名给用户)
  const intent = rawIntent.replace(/\s*\[\w+\]$/, '')
  return {
    intent,
    detail: props.item.content.slice(idx + 1),
  }
})

/**
 * 过滤 content 里的 <plan>...</plan> 块。
 * plan 由 TaskDetailView 单独渲染成清单卡片,不在 thinking 正文重复显示。
 * 流式期间 plan 块可能不完整(只有开标签),也一并清理。
 */
const PLAN_BLOCK_RE = /<plan>[\s\S]*?<\/plan>|<plan>[\s\S]*$/g
function stripPlanBlock(content: string): string {
  return content.replace(PLAN_BLOCK_RE, '').trim()
}

/** 流式项的展示 content(去掉 plan 块) */
const streamingDisplayContent = computed(() => {
  const c = props.item.streaming?.content || ''
  return stripPlanBlock(c)
})

/** 流式项 reasoning 的实际内容(trim 后) */
const streamingReasoning = computed(() => {
  const r = props.item.streaming?.reasoning ?? ''
  return r.trim()
})

/**
 * 思考框自动贴底:展开只是把框打开,真正在看的新内容在框的底端。
 * 思考框高 300px 封顶(带内部滚动),不贴住底部时用户看到的是思考的
 * 开头、最新 token 落在可视区外,自动展开就白做了。
 * 用户在框内向上滚回看时不再打扰(与外层对话流 followBottom 同一套 proximity 判断)。
 */
const reasoningRef = ref<HTMLElement | null>(null)
const innerFollow = ref(true)

function onReasoningScroll(): void {
  const el = reasoningRef.value
  if (!el) return
  innerFollow.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 12
}

watch(
  () => [streamingReasoning.value, streamingExpanded.value] as const,
  () => {
    // 只给"还在流"的卡片贴底:历史卡用户点开是为了从头读,跳到底部反而错位
    if (!isActive.value || !streamingExpanded.value || !innerFollow.value) return
    nextTick(() => {
      const el = reasoningRef.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
)

/**
 * 是否渲染该消息组。
 * 非流式项始终渲染;流式项仅在"有 reasoning / 有 content / 正在流式"时任一成立时渲染。
 */
const showCard = computed(() => {
  if (!(props.item.is_streaming && props.item.streaming)) return true
  return (
    streamingReasoning.value.length > 0 ||
    streamingDisplayContent.value.length > 0 ||
    isActive.value
  )
})

/** 正式对话项的展示 content(tool_call 拆分时用 detail,否则过滤 plan 块) */
const displayContent = computed(() => {
  if (toolCallParts.value) return toolCallParts.value.detail
  const c = props.item.content || ''
  if (props.item.type === 'thinking') return stripPlanBlock(c)
  return c
})

/**
 * Markdown 渲染后的 HTML(用于 v-html 注入)。
 *
 * 应用范围:reasoning(思考)、thinking 正文、agent1/agent2 正式输出、
 * 用户补充消息、summary 等所有可能含 markdown 语法的 LLM/用户文本。
 *
 * 不渲染:tool_call/tool_result 详情(代码/参数,等宽纯文本更清晰)。
 * tool_call 的 intent 标题是单行意图,也不渲染。
 */
const streamingReasoningHtml = computed(() => renderMarkdown(streamingReasoning.value))
/**
 * 思考框实际注入的 HTML:流式中只转义不排版(逐 token 重解析 markdown 会把
 * 渲染成本扣在每个增量上,而思考链可达数千字符),结束后一次性渲染。
 * 两个形态共用同一个元素:避免 keyed 分支交换时新旧两块短暂共存把框高撑一倍。
 */
const streamingReasoningHtmlOut = computed(() =>
  isActive.value ? escapePlainText(streamingReasoning.value) : streamingReasoningHtml.value,
)
const streamingContentHtml = computed(() => renderMarkdown(streamingDisplayContent.value))
const reasoningHtml = computed(() => renderMarkdown(props.item.reasoning || ''))
const displayContentHtml = computed(() => {
  // tool_call 详情走等宽纯文本,不渲染 markdown
  if (toolCallParts.value) return ''
  return renderMarkdown(displayContent.value)
})

/**
 * tool_result / tool_call 长详情可折叠:
 * - tool_result:子智能体输出可能数千字符(审计报告)
 * - tool_call:子智能体参数可能很长(Agent 的 prompt + description)
 * 超过阈值(10 行 / 500 字符)时默认折叠,点击展开。
 */
const DETAIL_COLLAPSE_LINES = 10
const detailExpanded = ref(false)
const shouldCollapseDetail = computed(() => {
  const t = props.item.type
  if (t !== 'tool_result' && t !== 'tool_call') return false
  const c = displayContent.value || ''
  if (c.length > 500) return true
  return c.split('\n').length > DETAIL_COLLAPSE_LINES
})
/** 折叠区标签:tool_result 显示"工具结果",tool_call 显示"调用参数" */
const detailLabel = computed(() =>
  props.item.type === 'tool_call' ? '调用参数' : '工具结果',
)

/** 附件列表(仅 user 追问消息有;其余为空) */
const attachmentList = computed<AttachmentInfo[]>(() => props.item.attachments || [])

/** 字节数格式化(附件 chip 用) */
function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  )
  return `${(bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`
}
</script>

<template>
  <div v-if="showCard" class="msg-group">
    <!-- 流式思考项:reasoning 卡片 + content 卡片,两者独立 -->
    <template v-if="item.is_streaming && item.streaming">
      <!-- reasoning 独立卡片(流式中自动展开,结束后折叠) -->
      <div
        v-if="streamingReasoning"
        class="msg-reasoning-card"
        :class="{ 'msg-reasoning-streaming': isActive }"
      >
        <div
          class="msg-reasoning-header"
          @click="emit('toggle-reasoning', item.streaming.conv_id)"
        >
          <span class="msg-reasoning-toggle">
            {{ streamingExpanded ? '▼' : '▶' }}
          </span>
          <span class="msg-reasoning-label">思考</span>
          <span v-if="streamingReasoning" class="msg-reasoning-meta">
            {{ streamingReasoning.length }} 字符
          </span>
          <span v-if="isActive" class="msg-streaming-tag">
            <span class="typing-dots">
              <span></span><span></span><span></span>
            </span>
          </span>
        </div>
        <Transition name="thinking-box">
          <div
            v-if="streamingExpanded"
            ref="reasoningRef"
            :class="['msg-reasoning-content', isActive ? 'msg-reasoning-live' : 'markdown-body']"
            v-html="streamingReasoningHtmlOut"
            @scroll="onReasoningScroll"
          />
        </Transition>
      </div>

      <!-- content 独立卡片 -->
      <div
        v-if="streamingDisplayContent"
        :class="['msg-content-card', 'markdown-body', `msg-${variant}`]"
        v-html="streamingContentHtml"
      />

      <div
        v-if="isActive && !item.streaming.reasoning && !streamingDisplayContent"
        class="msg-content-card msg-content-muted"
      >
        等待模型响应...
      </div>
    </template>

    <!-- 正式对话项:reasoning 卡片(可折叠) + content 卡片,两者独立 -->
    <template v-else>
      <!-- reasoning 独立卡片(可折叠);agent2 结构化评估块不展示 -->
      <div v-if="item.reasoning && !isUaEvaluation" class="msg-reasoning-card">
        <div
          class="msg-reasoning-header"
          @click="evalExpanded = !evalExpanded"
        >
          <span class="msg-reasoning-toggle">
            {{ evalExpanded ? '▼' : '▶' }}
          </span>
          <span class="msg-reasoning-label">思考</span>
          <span class="msg-reasoning-meta">
            {{ item.reasoning.length }} 字符
          </span>
        </div>
        <div v-if="evalExpanded" class="msg-reasoning-content markdown-body" v-html="reasoningHtml" />
      </div>

      <!-- content 独立卡片 -->
      <!-- tool_call:首行作为意图标题高亮,其余作为等宽调用详情 -->
      <div v-if="toolCallParts" class="msg-tool-intent">{{ toolCallParts.intent }}</div>

      <!-- tool_result / tool_call 超长详情:可折叠(子智能体输出/参数可能数千字符) -->
      <div
        v-if="shouldCollapseDetail"
        :class="['msg-content-card', 'msg-tool-result', `msg-${variant}`]"
      >
        <div
          class="msg-tool-result-header"
          @click="detailExpanded = !detailExpanded"
        >
          <span class="msg-reasoning-toggle">
            {{ detailExpanded ? '▼' : '▶' }}
          </span>
          <span class="msg-tool-result-label">{{ detailLabel }}</span>
          <span class="msg-reasoning-meta">{{ displayContent.length }} 字符</span>
        </div>
        <div v-if="detailExpanded" class="msg-tool-result-body">{{ displayContent }}</div>
      </div>

      <!-- 其他类型:普通卡片(markdown 渲染) -->
      <div
        v-else-if="displayContentHtml"
        :class="['msg-content-card', 'markdown-body', `msg-${variant}`]"
        v-html="displayContentHtml"
      />

      <!-- 用户追问附带的文件 chip(只读展示) -->
      <div v-if="attachmentList.length" class="msg-attachments">
        <span
          v-for="att in attachmentList"
          :key="att.upload_id"
          class="msg-att-chip"
          :title="att.filename"
        >
          <svg
            class="msg-att-icon"
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          <span class="msg-att-name">{{ att.filename }}</span>
          <span class="msg-att-size">{{ formatSize(att.size) }}</span>
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 消息组:纯布局容器,不再是卡片;reasoning 与 content 各自独立成卡片 */
.msg-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* reasoning 独立卡片:淡灰背景,与 content 卡片视觉区分 */
.msg-reasoning-card {
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.msg-reasoning-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  user-select: none;
  padding: var(--space-1) 0;
  transition: background 0.15s ease;
  border-radius: var(--radius-sm);
}

.msg-reasoning-header:hover {
  background: var(--color-msg-header-hover);
}

.msg-reasoning-toggle {
  display: inline-block;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  text-align: center;
}

.msg-reasoning-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.msg-reasoning-meta {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin-left: auto;
}

.msg-reasoning-content {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  font-style: italic;
  line-height: var(--lh-relaxed);
  max-height: 300px;
  overflow-y: auto;
  margin-top: var(--space-1);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
}

/* markdown-body 容器覆盖 pre-wrap:marked 已把换行转成 <p>/<br>,
   再保留 pre-wrap 会在 <p> 之间产生多余空白行。 */
.msg-reasoning-content.markdown-body,
.msg-content-card.markdown-body {
  white-space: normal;
}

/* 流式中的思考框:纯文本直出(pre-wrap 保留换行,不走 markdown 解析) */
.msg-reasoning-live {
  white-space: pre-wrap;
}

/* 思考框展开/收起过渡:宽限期到点后折叠不把下方消息"猛地拽上去" */
.thinking-box-enter-active,
.thinking-box-leave-active {
  transition:
    max-height 0.18s ease,
    padding-top 0.18s ease,
    margin-top 0.18s ease,
    opacity 0.18s ease;
  overflow: hidden;
}

.thinking-box-enter-from,
.thinking-box-leave-to {
  max-height: 0;
  padding-top: 0;
  margin-top: 0;
  opacity: 0;
}

/* 流式思考时给 reasoning 卡片加脉冲动画 */
.msg-reasoning-streaming {
  box-shadow: 0 0 0 2px var(--color-msg-pulse-soft);
  animation: streaming-pulse 2s ease-in-out infinite;
}

@keyframes streaming-pulse {
  0%, 100% { box-shadow: 0 0 0 2px var(--color-msg-pulse-soft); }
  50% { box-shadow: 0 0 0 4px var(--color-msg-pulse-strong); }
}

.msg-streaming-tag {
  display: inline-flex;
  align-items: center;
  margin-left: var(--space-2);
}

.msg-streaming-tag .typing-dots span {
  background: var(--color-msg-streaming-dot);
}

.typing-dots {
  display: inline-flex;
  gap: 3px;
}

.typing-dots span {
  width: 6px;
  height: 6px;
  background: var(--color-text-muted);
  border-radius: 50%;
  animation: typing 1.4s infinite;
}

.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes typing {
  0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-4px); }
}

/* content 独立卡片:用 role/type 配色 */
.msg-content-card {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-lg);
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: var(--lh-relaxed);
}

.msg-agent2 {
  background: var(--color-info-light);
}

.msg-agent1 {
  background: var(--color-msg-react-bg);
}

.msg-tool {
  background: var(--color-surface-alt);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  max-height: 200px;
  overflow-y: auto;
}

/* 用户补充消息(type=message):蓝色右对齐气泡样式,与顶部 userDirective 视觉一致 */
/* 注意:外层右对齐由父容器(.user-directive 或 TaskDetailView 中的样式)控制,
   这里只负责配色 */
.msg-user-message {
  background: var(--color-primary-light);
  color: var(--color-text);
}

.msg-error {
  background: var(--color-danger-light);
}

.msg-summary {
  background: var(--color-msg-summary-bg);
}

.msg-streaming {
  background: var(--color-msg-streaming-bg);
}

.msg-content-muted {
  color: var(--color-text-muted);
  font-style: italic;
}

/* tool_call 意图标题:人类可读的一句话,高亮显示在调用详情上方 */
.msg-tool-intent {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-primary);
  padding: var(--space-1) var(--space-2);
  background: var(--color-primary-light);
  border-radius: var(--radius-sm);
}

/* tool_result / tool_call 长详情可折叠区域:超长内容默认折叠 */
.msg-tool-result {
  padding: 0;
  overflow: hidden;
}

.msg-tool-result-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  user-select: none;
  padding: var(--space-1) var(--space-2);
  transition: background 0.15s ease;
}

.msg-tool-result-header:hover {
  background: var(--color-msg-header-hover);
}

.msg-tool-result-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
}

.msg-tool-result-body {
  padding: var(--space-2);
  border-top: 1px solid var(--color-border);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-xs);
  max-height: 500px;
  overflow-y: auto;
}

/* 用户追问附件 chip(只读展示) */
.msg-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.msg-att-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 220px;
  padding: 3px 8px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.msg-att-icon {
  flex-shrink: 0;
}

.msg-att-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-att-size {
  flex-shrink: 0;
  color: var(--color-text-muted);
}

</style>
