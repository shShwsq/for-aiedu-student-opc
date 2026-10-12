<script setup lang="ts">
/**
 * 建议追问方向卡片组(核查与结果侧栏「概览」段)
 *
 * 检查助手审查完成后给出的 0-N 条建议:每条一个「追问」按钮,点击后
 * 把建议原文作为用户消息发出(父组件走 resume 链路 → agent1 追加一轮)。
 *
 * 原内联在 Agent2Panel 底部,随侧栏分段重构上移到「概览」段——
 * 「核查过程」只留过程输出,概览承担"现状 + 下一步"。
 * 文本解析见 utils/agent2Suggestions.ts,本组件只管展示与防重复点击的呈现。
 */
withDefaults(
  defineProps<{
    /** 建议文本清单(已由 extractSuggestions / extractExploreSuggestions 过滤) */
    suggestions: string[]
    /** 已发起追问的那条文本;非空时全部按钮禁用,该条显示"已发起追问…" */
    digging: string | null
    /** 分组标题:区分"检查助手发现的缺口"与"你可能还想深入了解"两组 */
    title?: string
  }>(),
  { title: '建议追问方向' },
)

defineEmits<{
  /** 点击"追问":把建议文本交给父组件发消息 */
  (e: 'dig', text: string): void
}>()
</script>

<template>
  <section v-if="suggestions.length > 0" class="suggestion-section">
    <h2 class="suggestion-title">
      {{ title }}
      <span class="suggestion-count">({{ suggestions.length }})</span>
    </h2>
    <div
      v-for="(s, i) in suggestions"
      :key="i"
      :class="['suggestion-card', { 'is-digging': digging === s }]"
    >
      <p class="suggestion-text">{{ s }}</p>
      <button
        type="button"
        class="suggestion-btn"
        :disabled="!!digging"
        @click="$emit('dig', s)"
      >{{ digging === s ? '已发起追问…' : '追问' }}</button>
    </div>
  </section>
</template>

<style scoped>
/* 区块容器:标题在上,卡片纵向平铺 */
.suggestion-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* 段内小标题统一 14px/600:「概览」段的三个小标题(任务清单 / 动态验证 / 本组件)
   不得比面板标题行的段名(15px/700)更大 */
.suggestion-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.suggestion-count {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-tertiary);
}

.suggestion-card {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.suggestion-card.is-digging {
  border-color: var(--color-primary-border);
  background: var(--color-primary-light);
}

.suggestion-text {
  flex: 1;
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.suggestion-btn {
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

.suggestion-btn:hover:not(:disabled) {
  background: var(--color-primary-light);
}

.suggestion-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
</style>
