<script setup lang="ts">
/**
 * 子任务块:内置 react 并行派发里「一个子任务 = 一个可折叠块」。
 *
 * 嵌在执行过程组(.step-body)内部渲染(折叠层级 = 执行过程 ⌄ [ 迭代… → 子任务块… ]),
 * 由父组件按锚点(`afterMainIdx`)决定落在组内哪个迭代之后。块内聚合该子任务的
 * 全部产物(思考流 + 工具调用/结果),迭代体与 step 组共用 IterationBody。
 *
 * 展开状态不在此组件里:状态源(含 round 的 key、手动意图优先)留在父视图,
 * 本组件只吃 `open` 布尔并发出点击事件 —— 保持"无内部状态"以便静态扫描测试。
 *
 * 样式自带:父视图的 scoped CSS 不会作用到子组件内部节点,状态图标与打字动画
 * 在此复制一份(与 step 组头保持同一视觉),不依赖父级 :deep。
 */
import IterationBody from '@/components/IterationBody.vue'
import type { AgentBlockRowLike } from '@/utils/toolRows'

interface SubtaskIterItem {
  id: string
  created_at: string
  is_streaming: boolean
  role?: string
  type?: string
  content?: string
  reasoning?: string | null
  tool_call_id?: string | null
  facet?: string
  streaming?: {
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
}

/** 块内一次迭代的最小结构(视图的 IterationSegment 满足;与 IterationBody 同形) */
interface SubtaskIter {
  id: string
  thinkingItems: SubtaskIterItem[]
  toolItems: SubtaskIterItem[]
  otherItems: SubtaskIterItem[]
}

interface SubtaskSegLike {
  label: string
  iterations: SubtaskIter[]
  /** 思考段数(标题概览用) */
  thinkingCount: number
  /** 工具调用条数(标题概览用) */
  toolCount: number
  /** 是否仍在运行(块内存在流式项) */
  live: boolean
}

const props = defineProps<{
  seg: SubtaskSegLike
  /** 块是否展开:运行中默认展开、fan-in 落库后自动折叠、手动意图优先(判定在父视图) */
  open: boolean
  expandedKeys: Set<string>
  collapsedKeys: Set<string>
}>()

const emit = defineEmits<{
  'toggle-subtask': []
  'toggle-reasoning': [convId: string]
  'toggle-key': [key: string]
  'toggle-agent': [row: AgentBlockRowLike]
  'open-file': [path: string]
}>()
</script>

<template>
  <div class="subtask-block" :class="{ 'subtask-live': seg.live }">
    <div class="subtask-header" @click="emit('toggle-subtask')">
      <span class="subtask-toggle">{{ open ? '▼' : '▶' }}</span>
      <span
        :class="['step-status-icon', seg.live ? 'step-status-in_progress' : 'step-status-done']"
      >{{ seg.live ? '◌' : '✓' }}</span>
      <span class="subtask-title">子任务 · {{ seg.label }}</span>
      <!-- 打字动画只在块内确有流式项时渲染;工具执行空档(仅流式卡、无新 delta)
           不显示动画,避免标题持续"看似在输出" -->
      <span v-if="seg.live" class="step-streaming-tag">
        <span class="typing-dots"><span></span><span></span><span></span></span>
      </span>
      <span class="subtask-counts">{{ seg.thinkingCount }} 段思考 · {{ seg.toolCount }} 次工具调用</span>
    </div>
    <!-- 运行中展开时正文有 max-height 内滚:子任务产物可能很长,不把整页撑走 -->
    <div v-if="open" class="subtask-body">
      <template v-for="iter in seg.iterations" :key="iter.id">
        <IterationBody
          :iter="iter"
          :expanded-keys="expandedKeys"
          :collapsed-keys="collapsedKeys"
          hide-facet-badge
          @toggle-reasoning="(id: string) => emit('toggle-reasoning', id)"
          @toggle-key="(key: string) => emit('toggle-key', key)"
          @toggle-agent="(row: AgentBlockRowLike) => emit('toggle-agent', row)"
          @open-file="(path: string) => emit('open-file', path)"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
/* ---- 子任务块容器 ---- */
.subtask-block {
  margin-bottom: var(--space-2);
  border-left: 2px solid var(--color-primary, #6366f1);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
}

.subtask-live {
  border-left-color: var(--color-warning, #f59e0b);
}

.subtask-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease;
}

.subtask-header:hover {
  background: var(--color-surface-alt);
}

.subtask-toggle {
  flex-shrink: 0;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-align: center;
}

.subtask-title {
  flex: 1;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.subtask-counts {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.subtask-body {
  max-height: min(60vh, 560px);
  overflow-y: auto;
  padding-left: var(--space-3);
  padding-bottom: var(--space-1);
  border-top: 1px solid var(--color-border);
}

/* ---- 状态图标 / 打字动画:与 TaskDetailView 的 step 组头同一视觉 ----
   (scoped CSS 不跨组件边界,父视图的同名规则管不到这里,故自带一份) */
.step-status-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  border-radius: 50%;
}

.step-status-in_progress {
  color: #f59e0b;
  background: rgba(245, 158, 11, 0.15);
  animation: step-pulse 1.5s ease-in-out infinite;
}

.step-status-done {
  color: #fff;
  background: #10b981;
}

@keyframes step-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.6; transform: scale(0.9); }
}

.step-streaming-tag {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
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
</style>
