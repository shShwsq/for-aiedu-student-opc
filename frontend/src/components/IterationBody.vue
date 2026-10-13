<script setup lang="ts">
/**
 * 迭代体:一次 ReAct 迭代的全部产物(思考卡 + 工具渲染行 + 其他项)。
 *
 * 从 TaskDetailView 抽出:plan step 组与「子任务」块共用同一段 markup,
 * 避免"后台未回报"诚实态等分支双份维护(该分支有历史回归 bug,见
 * taskDetailViewConclusionWiring.test.ts)。工具渲染行的构造纯函数在 utils/toolRows。
 *
 * 展开状态由父组件通过两个 reactive Set 传入(与子智能体块共用同一套"手动展开/折叠"语义);
 * 子任务块内传 hideFacetBadge,身份由块标题承载,思考卡不再重复标注。
 */
import { computed } from 'vue'
import ConversationMessage from '@/components/ConversationMessage.vue'
import { isAgentBlockOpen } from '@/utils/agentBlock'
import { toolRowsOf, type AgentBlockRowLike, type ToolRenderRow } from '@/utils/toolRows'
import type { AttachmentInfo } from '@/types/task'

interface IterBodyStreaming {
  conv_id: string
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  reasoning_auto?: boolean
  reasoning_grace?: boolean
  reasoning_pin?: boolean | null
  role?: 'agent1' | 'agent2'
  facet?: string
}

interface IterBodyItem {
  id: string
  created_at: string
  is_streaming: boolean
  role?: string
  type?: string
  content?: string
  reasoning?: string | null
  tool_call_id?: string | null
  attachments?: AttachmentInfo[] | null
  facet?: string
  streaming?: IterBodyStreaming
}

interface IterBodyIteration {
  id: string
  thinkingItems: IterBodyItem[]
  toolItems: IterBodyItem[]
  otherItems: IterBodyItem[]
}

const props = withDefaults(
  defineProps<{
    iter: IterBodyIteration
    expandedKeys: Set<string>
    collapsedKeys: Set<string>
    /** 子任务块内隐藏思考卡的"子任务 · X"身份标(块标题已承载身份) */
    hideFacetBadge?: boolean
  }>(),
  { hideFacetBadge: false },
)

const emit = defineEmits<{
  'toggle-reasoning': [convId: string]
  'toggle-key': [key: string]
  'toggle-agent': [row: AgentBlockRowLike]
  'open-file': [path: string]
}>()

const rows = computed(() => toolRowsOf(props.iter))

function onToggleReasoning(convId: string): void {
  emit('toggle-reasoning', convId)
}

/** 工具渲染行是否展开(默认折叠,由用户控制;key 支持 `${callId}-think` 复合键) */
function isRowExpanded(key: string): boolean {
  return props.expandedKeys.has(key)
}

function toggleRow(key: string): void {
  emit('toggle-key', key)
}

/** 子智能体折叠块是否展开:运行中默认展开、完成默认折叠,手动展开/折叠优先
 *  (状态机在 utils/agentBlock,与主展开集共享"手动展开"语义)。
 *  只拿到后台派生回执的卡片算"未完成":结果从没回来过,折叠等于把唯一可见的
 *  诚实信号藏起来 */
function agentBlockOpen(row: ToolRenderRow): boolean {
  return isAgentBlockOpen(row.callId, row.hasResult && !row.agentAsync, {
    expanded: props.expandedKeys,
    collapsed: props.collapsedKeys,
  })
}

/** 点击子智能体标题:交给父组件翻转并记入手动集(展开进 expanded,折叠进 collapsed) */
function toggleAgentCard(row: ToolRenderRow): void {
  emit('toggle-agent', {
    callId: row.callId,
    hasResult: row.hasResult,
    agentAsync: row.agentAsync,
  })
}
</script>

<template>
  <div class="iteration-body">
    <!-- thinking 项(流式或历史) -->
    <ConversationMessage
      v-for="t in iter.thinkingItems"
      :key="t.id"
      :item="t"
      :hide-facet-badge="hideFacetBadge"
      @toggle-reasoning="onToggleReasoning"
    />
    <!-- 工具调用渲染行:compact 单行摘要 / agent 子智能体卡片 /
         toolpair 普通工具卡片 / plain 兜底 -->
    <template v-for="row in rows" :key="row.key">
      <!-- 紧凑工具:单行摘要,点击轻量展开原始结果 -->
      <div v-if="row.kind === 'compact'" class="tool-compact">
        <div class="tool-compact-row" @click="toggleRow(row.callId)">
          <span class="tool-group-toggle">
            {{ row.hasResult ? (isRowExpanded(row.callId) ? '▼' : '▶') : '' }}
          </span>
          <span class="tool-compact-summary">
            <template v-if="row.filePath">
              {{ row.summaryPrefix }}<a
                class="tool-file-link"
                title="在文件树中打开"
                @click.stop="emit('open-file', row.filePath)"
              >{{ row.fileDisplay }}</a>{{ row.summarySuffix }}
            </template>
            <template v-else>{{ row.summary }}</template>
          </span>
        </div>
        <div
          v-if="row.hasResult && isRowExpanded(row.callId)"
          class="tool-compact-result"
        >{{ row.resultContent }}</div>
      </div>
      <!-- 子智能体卡片:标题单行,展开后参数 + 内部思考 + Markdown 报告 -->
      <div v-else-if="row.kind === 'agent'" class="tool-card tool-card-agent">
        <div class="tool-card-header" @click="toggleAgentCard(row)">
          <span class="tool-group-toggle">{{ agentBlockOpen(row) ? '▼' : '▶' }}</span>
          <span class="tool-card-title">{{ row.summary }}</span>
        </div>
        <div v-if="agentBlockOpen(row)" class="tool-card-body">
          <div v-if="row.callDetail" class="tool-card-section">
            <div class="tool-card-section-label">子任务参数</div>
            <div class="tool-card-mono">{{ row.callDetail }}</div>
          </div>
          <div v-if="row.agentThink" class="tool-card-section">
            <div
              class="tool-card-section-label tool-card-think-toggle"
              @click.stop="toggleRow(row.callId + '-think')"
            >{{ isRowExpanded(row.callId + '-think') ? '▼' : '▶' }} 内部思考</div>
            <div
              v-if="isRowExpanded(row.callId + '-think')"
              class="tool-card-think"
            >{{ row.agentThink }}</div>
          </div>
          <div
            v-if="row.hasResult"
            class="tool-card-markdown markdown-body"
            v-html="row.agentBodyHtml"
          />
          <div v-else class="tool-card-running">子智能体执行中…</div>
        </div>
      </div>
      <!-- 普通工具卡片:调用+结果整体折叠 -->
      <div v-else-if="row.kind === 'toolpair'" class="tool-card">
        <div class="tool-card-header" @click="toggleRow(row.callId)">
          <span class="tool-group-toggle">{{ isRowExpanded(row.callId) ? '▼' : '▶' }}</span>
          <span class="tool-card-title">{{ row.summary }}</span>
        </div>
        <div v-if="isRowExpanded(row.callId)" class="tool-card-body">
          <div v-if="row.callDetail" class="tool-card-section">
            <div class="tool-card-section-label">调用参数</div>
            <div class="tool-card-mono">{{ row.callDetail }}</div>
          </div>
          <div v-if="row.hasResult" class="tool-card-section">
            <div class="tool-card-section-label">工具结果</div>
            <div class="tool-card-mono">{{ row.resultContent }}</div>
          </div>
          <div v-if="!row.callDetail && !row.hasResult" class="tool-card-running">执行中…</div>
        </div>
      </div>
      <!-- plain 兜底:落单 result 等原样渲染 -->
      <template v-else-if="row.kind === 'plain'">
        <ConversationMessage
          v-for="ti in row.items"
          :key="ti.id"
          :item="ti"
          :hide-facet-badge="hideFacetBadge"
          @toggle-reasoning="onToggleReasoning"
        />
      </template>
    </template>
    <!-- 其他项(submit 等) -->
    <ConversationMessage
      v-for="o in iter.otherItems"
      :key="o.id"
      :item="o"
      :hide-facet-badge="hideFacetBadge"
      @toggle-reasoning="onToggleReasoning"
    />
  </div>
</template>

<style scoped>
/* ---- 迭代块(agent1 一次 ReAct 循环,结构容器:无摘要行、无边框包装) ---- */
.iteration-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

/* ---- 工具卡片(agent 子智能体 / toolpair 普通工具:标题行 + 折叠内容) ---- */
.tool-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  overflow: hidden;
}

/* 子智能体卡片:左边线标示嵌套层级 */
.tool-card-agent {
  border-left: 2px solid var(--color-primary, #6366f1);
}

.tool-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease;
}

.tool-card-header:hover {
  background: var(--color-surface-alt);
}

.tool-card-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-card-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3) var(--space-3);
  border-top: 1px solid var(--color-border);
}

.tool-card-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.tool-card-section-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
}

.tool-card-think-toggle {
  cursor: pointer;
  user-select: none;
}

.tool-card-mono {
  padding: var(--space-2);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 300px;
  overflow-y: auto;
}

.tool-card-think {
  padding: var(--space-2);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  font-style: italic;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

/* 子智能体报告正文:Markdown 渲染 */
.tool-card-markdown {
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  max-height: 600px;
  overflow-y: auto;
}

.tool-card-running {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-style: italic;
}

.tool-group-toggle {
  display: inline-block;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-align: center;
}

/* ---- 紧凑工具行(读文件/搜索/列目录:单行摘要 + 轻量展开原始结果) ---- */
.tool-compact {
  display: flex;
  flex-direction: column;
}

.tool-compact-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  cursor: pointer;
  user-select: none;
  border-radius: var(--radius-sm);
  transition: background 0.15s ease;
}

.tool-compact-row:hover {
  background: var(--color-surface-alt);
}

.tool-compact-summary {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 工具行摘要内的文件路径链接:点击跳转左侧文件树打开 */
.tool-file-link {
  color: var(--color-primary);
  text-decoration: underline;
  text-decoration-style: dashed;
  cursor: pointer;
}

.tool-file-link:hover {
  color: var(--color-primary-hover);
}

.tool-compact-result {
  margin: var(--space-1) var(--space-2) var(--space-1) calc(var(--space-2) + 14px + var(--space-2));
  padding: var(--space-2);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 300px;
  overflow-y: auto;
}
</style>
