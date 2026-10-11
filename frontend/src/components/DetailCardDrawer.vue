<script setup lang="ts">
/**
 * 右侧 Drawer 详情阅读组件
 *
 * 点击核查与结果侧栏中的 ReviewItem / TaskResult 卡片时,
 * 打开右侧滑入抽屉展示完整正文,替代原先的 inline-expand。
 *
 * 复用仓库已有的 Teleport + mask + dialog-card 交互模式
 * (参见 PracticeQuestionDetailDialog / VerifyActionDialog)。
 */
import { computed, onMounted, onUnmounted, watch } from 'vue'
import type { ReviewItem, TaskResult } from '@/types/task'
import { renderMarkdown } from '@/utils/markdown'
import {
  confidenceClass,
  confidenceLabel,
  formatConfidenceScore,
} from '@/utils/reviewBucket'

const props = defineProps<{
  open: boolean
  item: ReviewItem | TaskResult | null
  type: 'review' | 'result'
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-file', item: ReviewItem): void
}>()

// ---- Esc 键关闭 ----
function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && props.open) emit('close')
}

onMounted(() => document.addEventListener('keydown', onKeydown))
onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  // 开着 drawer 时路由跳走会直接卸载本组件,watch 不再触发;
  // 若不在此恢复 overflow,body 滚动锁会泄漏到其它页面(整站不能滚)。
  if (props.open) document.body.style.overflow = ''
})

// 打开时禁止背景滚动
watch(
  () => props.open,
  (open) => {
    document.body.style.overflow = open ? 'hidden' : ''
  },
)

/** 判断当前展示的是否为审查项 (通过 type discriminator) */
const isReview = computed(() => props.type === 'review')

/** 渲染知识点 markdown */
function renderContent(content: string | null | undefined): string {
  return renderMarkdown(content)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <div
        v-if="open && item"
        class="detail-drawer-mask"
        @click.self="emit('close')"
      >
        <aside class="detail-drawer" role="dialog" aria-modal="true" aria-label="卡片详情">
          <!-- 头部:标题 + 关闭按钮 -->
          <header class="detail-drawer-header">
            <h3 class="detail-drawer-title">{{ item.title }}</h3>
            <!-- 审查项:置信度徽标 -->
            <span
              v-if="isReview"
              :class="['conf-badge', `conf-${confidenceClass((item as ReviewItem).confidence?.tier)}`]"
            >
              {{ confidenceLabel((item as ReviewItem).confidence?.tier) }}
              · {{ formatConfidenceScore((item as ReviewItem).confidence?.score) }}
            </span>
            <!-- 知识点:学习点徽标 -->
            <span
              v-if="!isReview && (item as TaskResult).metadata_?.learning_note"
              class="learning-badge"
            >值得学</span>
            <button class="detail-drawer-close" aria-label="关闭" @click="emit('close')">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </header>

          <!-- 可滚动内容区 -->
          <div class="detail-drawer-body">
            <!-- 审查项详情 -->
            <template v-if="isReview">
              <div class="drawer-section">
                <p class="rv-line"><strong>被核实对象:</strong>{{ (item as ReviewItem).review_target || '—' }}</p>
                <p v-if="(item as ReviewItem).description" class="rv-line">
                  <strong>发现问题:</strong>{{ (item as ReviewItem).description }}
                </p>
                <div
                  v-if="(item as ReviewItem).evidence?.source?.quote || (item as ReviewItem).evidence?.source?.file_path"
                  class="rv-section"
                >
                  <strong>原始证据:</strong>
                  <code
                    v-if="(item as ReviewItem).evidence?.source?.file_path"
                    class="rv-file"
                    @click="emit('open-file', item as ReviewItem)"
                  >{{ (item as ReviewItem).evidence!.source!.file_path }}<span v-if="(item as ReviewItem).evidence!.source!.line">:{{ (item as ReviewItem).evidence!.source!.line }}</span></code>
                  <pre
                    v-if="(item as ReviewItem).evidence?.source?.quote"
                    class="rv-quote"
                  >{{ (item as ReviewItem).evidence!.source!.quote }}</pre>
                </div>
                <p v-if="(item as ReviewItem).evidence?.analysis_basis?.ref_url" class="rv-line">
                  <strong>分析依据:</strong>{{ (item as ReviewItem).evidence!.analysis_basis!.ref_url }}
                </p>
                <p v-if="(item as ReviewItem).evidence?.verification" class="rv-line">
                  <strong>验证测试:</strong>{{ (item as ReviewItem).evidence!.verification!.method || '—' }}
                  <span v-if="(item as ReviewItem).evidence!.verification!.poc_evidence">
                    · {{ (item as ReviewItem).evidence!.verification!.poc_evidence }}
                  </span>
                </p>
                <p v-if="(item as ReviewItem).suggestion" class="rv-line">
                  <strong>建议:</strong>{{ (item as ReviewItem).suggestion }}
                </p>
              </div>
              <!-- 元信息 -->
              <div v-if="(item as ReviewItem).severity || (item as ReviewItem).origin" class="drawer-meta">
                <span class="origin-tag" :title="(item as ReviewItem).origin">{{ (item as ReviewItem).origin }}</span>
                <span v-if="(item as ReviewItem).severity" :class="['sev-tag', `sev-${(item as ReviewItem).severity}`]">
                  {{ (item as ReviewItem).severity }}
                </span>
              </div>
            </template>

            <!-- 知识点详情 -->
            <template v-else>
              <blockquote
                v-if="(item as TaskResult).metadata_?.learning_note"
                class="learning-note"
              >{{ (item as TaskResult).metadata_!.learning_note }}</blockquote>
              <div
                class="result-content markdown-body"
                v-html="renderContent((item as TaskResult).content)"
              />
            </template>
          </div>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* ---- Mask ---- */
.detail-drawer-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}

/* ---- Drawer panel ---- */
.detail-drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(520px, 90vw);
  background: var(--color-bg);
  box-shadow: var(--shadow-xl);
  display: flex;
  flex-direction: column;
  z-index: 1001;
}

/* ---- Header ---- */
.detail-drawer-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.detail-drawer-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
  word-break: break-word;
}

.detail-drawer-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.detail-drawer-close:hover {
  background: var(--color-bg-hover, rgba(0, 0, 0, 0.05));
}

/* ---- Body ---- */
.detail-drawer-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
}

/* ---- Review item detail fields ---- */
.drawer-section {
  font-size: var(--fs-sm);
  color: var(--color-text);
  line-height: 1.6;
}

.rv-line {
  margin: 6px 0;
}

.rv-section {
  margin: 10px 0;
}

.rv-file {
  display: inline-block;
  margin: 2px 0;
  padding: 1px 6px;
  background: var(--color-bg-subtle, #f3f4f6);
  border-radius: var(--radius-sm);
  cursor: pointer;
  color: var(--color-info, #2563eb);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.rv-file:hover {
  text-decoration: underline;
}

.rv-quote {
  margin: 6px 0;
  padding: var(--space-2) var(--space-3);
  background: var(--color-bg-subtle, #f9fafb);
  border-left: 3px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}

.drawer-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

.origin-tag {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  border: 1px dashed var(--color-border);
  padding: 0 6px;
  border-radius: var(--radius-sm);
}

.sev-tag {
  font-size: var(--fs-xs);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  font-weight: var(--fw-medium);
}

.sev-critical { background: rgba(220, 38, 38, 0.12); color: #dc2626; }
.sev-high { background: rgba(234, 88, 12, 0.12); color: #ea580c; }
.sev-medium { background: rgba(202, 138, 4, 0.12); color: #ca8a04; }
.sev-low { background: rgba(22, 163, 74, 0.12); color: #16a34a; }

/* Confidence badge (reuse styles from Agent2Panel) */
.conf-badge {
  font-size: var(--fs-xs);
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  white-space: nowrap;
  flex-shrink: 0;
}
.conf-verified { background: rgba(22, 163, 74, 0.12); color: var(--color-success, #16a34a); }
.conf-source { background: rgba(37, 99, 235, 0.12); color: var(--color-info, #2563eb); }
.conf-reference { background: rgba(124, 58, 237, 0.12); color: #7c3aed; }
.conf-assertion { background: rgba(120, 120, 120, 0.14); color: var(--color-text-secondary); }

/* ---- Result / Knowledge item detail ---- */
.learning-badge {
  flex-shrink: 0;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-full, 999px);
}

.learning-note {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-primary-light);
  border-left: 3px solid var(--color-primary);
  border-radius: var(--radius-sm);
}

.result-content {
  font-size: var(--fs-sm);
  color: var(--color-text);
  word-break: break-word;
}

.result-content.markdown-body {
  white-space: normal;
}

.result-content.markdown-body pre {
  overflow-x: auto;
}

/* ---- Transition: slide-in from right ---- */
.drawer-enter-active,
.drawer-leave-active {
  transition: opacity 0.2s ease;
}

.drawer-enter-active .detail-drawer,
.drawer-leave-active .detail-drawer {
  transition: transform 0.25s ease;
}

.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
}

.drawer-enter-from .detail-drawer {
  transform: translateX(100%);
}

.drawer-leave-to .detail-drawer {
  transform: translateX(100%);
}

/* ---- Mobile: fullscreen ---- */
@media (max-width: 640px) {
  .detail-drawer {
    width: 100vw;
  }
}
</style>
