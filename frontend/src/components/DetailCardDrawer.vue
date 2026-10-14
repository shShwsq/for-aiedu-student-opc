<script setup lang="ts">
/**
 * 右侧 Drawer 详情阅读组件
 *
 * 点击核查与结果侧栏中的 ReviewItem / TaskResult 卡片时,打开右侧滑入
 * 抽屉展示完整正文,替代原先的 inline-expand。
 *
 * 与仓库已有的弹窗模式不同点(有意为之,不要“顺手改回” Teleport to body):
 * 弹窗(VerifyActionDialog / PracticeGenerateDialog)是真 modal,遮整个视口是对的;
 * 本组件是**阅读面板**,不 Teleport,由宿主把它放在 .page-body 里,靠布局 containment
 * 拿到「AppHeader 下缘」这个偏移——不用顶栏真实高度(它是内容撑出来的,各页不一样),
 * 也就不需要一个会飘的魔法数字。同族口径见 PracticeGenerateSidebar 的覆盖态。
 *
 * 因此它也不再是 aria-modal:顶栏在遮罩之外,依旧可见可点(主题/帮助/导航不用先关
 * 抽屉);内容区与右侧 rail 仍在遮罩之下,阅读专注度不变。
 */
import { computed, onMounted, onUnmounted, watch } from 'vue'
import type { ReviewItem, TaskResult } from '@/types/task'
import { renderMarkdown } from '@/utils/markdown'
import {
  originLabel,
  reviewItemBadge,
} from '@/utils/reviewBucket'

const props = defineProps<{
  open: boolean
  item: ReviewItem | TaskResult | null
  type: 'review' | 'result'
  /** 正在发送的追问文本(与侧栏建议卡共用同一把防重复锁);非空时本抽屉的追问按钮置灰 */
  digging?: string | null
  /** 宿主的全量审查项:知识点抽屉用 source_review_item_id 回指证据(可溯源) */
  reviewItems?: ReviewItem[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'open-file', item: ReviewItem): void
  (e: 'dig', text: string): void
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

/** 审查项右上徽章(去档位:待核实说原因 / 成立仅 PoC 标记 / 否则 null) */
const reviewBadge = computed(() => {
  const it = props.item as ReviewItem | null
  return isReview.value && it ? reviewItemBadge(it) : null
})

/** 可追问内容(一句可直接发回 AI助手 的问句) */
const followUp = computed(() => {
  const it = props.item as ReviewItem | null
  return isReview.value ? (it?.follow_up ?? '').trim() : ''
})

/** 追问发送中:本条已被点击(锁属于父组件,与侧栏建议卡同机制) */
const diggingThis = computed(
  () => !!props.digging && props.digging === followUp.value,
)

/** 渲染知识点 markdown */
function renderContent(content: string | null | undefined): string {
  return renderMarkdown(content)
}

/**
 * 知识点的来源审查项(metadata_.source_review_item_id 回链)
 *
 * 知识点讲解正文里按契约不得虚构文件路径/行号,溯源证据改由这里展示:
 * 从宿主传入的审查项里按 id 解析,拿不到(旧数据/跨轮回收)则整块不渲染。
 */
const sourceReviewItem = computed<ReviewItem | null>(() => {
  if (isReview.value || !props.item || !props.reviewItems?.length) return null
  const rid = (props.item as TaskResult).metadata_?.source_review_item_id
  if (typeof rid !== 'string' || !rid) return null
  return props.reviewItems.find((r) => r.id === rid) ?? null
})
</script>

<template>
  <Transition name="drawer">
    <div
      v-if="open && item"
      class="detail-drawer-mask"
      @click.self="emit('close')"
    >
      <aside class="detail-drawer" role="dialog" aria-label="卡片详情">
        <!-- 头部:标题 + 关闭按钮 -->
        <header class="detail-drawer-header">
          <h3 class="detail-drawer-title">{{ item.title }}</h3>
          <!-- 审查项:待核实档标出未核实原因,成立/不成立档仅 PoC 标"🔬 动态验证",否则不显 -->
          <span
            v-if="isReview && reviewBadge"
            :class="['conf-badge', reviewBadge.cls]"
            :title="reviewBadge.title"
          >{{ reviewBadge.label }}</span>
          <!-- 证据核验失败:独立告警徽章(mismatch 的唯一出口,与列表卡一致) -->
          <span
            v-if="isReview && (item as ReviewItem).evidence_mismatch"
            class="conf-warn"
            title="有引用但后端核验未通过"
          >⚠ 证据未核验</span>
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
              <p class="rv-line"><strong>待核实对象:</strong>{{ (item as ReviewItem).review_target || '—' }}</p>
              <p v-if="(item as ReviewItem).conclusion" class="rv-line">
                <strong>审查结论:</strong>{{ (item as ReviewItem).conclusion }}
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
              <div v-if="followUp" class="rv-section">
                <strong>可追问内容:</strong>
                <button
                  type="button"
                  class="rv-dig"
                  :disabled="!!digging"
                  :title="diggingThis
                    ? '已提交追问,等待新一轮执行'
                    : digging ? '上一条追问发送中' : '点击把这句作为指令发给 AI助手'"
                  @click="emit('dig', followUp)"
                >{{ diggingThis ? '追问中...' : '就这点追问' }}</button>
                <p class="rv-follow-text">{{ followUp }}</p>
              </div>
            </div>
            <!-- 元信息 -->
            <div v-if="(item as ReviewItem).severity || (item as ReviewItem).origin" class="drawer-meta">
              <span class="origin-tag">{{ originLabel((item as ReviewItem).origin) }}</span>
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
            <!-- 来源证据:正文不含路径行号,靠回链的审查项证据溯源(可点文件名跳工作区) -->
            <div
              v-if="sourceReviewItem && (sourceReviewItem.evidence?.source?.file_path || sourceReviewItem.evidence?.source?.quote)"
              class="kp-source-ref"
            >
              <strong class="kp-source-label">来源证据</strong>
              <code
                v-if="sourceReviewItem?.evidence?.source?.file_path"
                class="rv-file"
                @click="sourceReviewItem && emit('open-file', sourceReviewItem)"
              >{{ sourceReviewItem?.evidence?.source?.file_path }}<span v-if="sourceReviewItem?.evidence?.source?.line">:{{ sourceReviewItem?.evidence?.source?.line }}</span></code>
              <pre
                v-if="sourceReviewItem?.evidence?.source?.quote"
                class="rv-quote"
              >{{ sourceReviewItem?.evidence?.source?.quote }}</pre>
            </div>
          </template>
        </div>
      </aside>
    </div>
  </Transition>
</template>

<style scoped>
/* ---- Mask ----
   absolute + inset:0 是相对宿主的 .page-body(它已是 position: relative + overflow: hidden),
   不是相对视口——顶栏因此不在遮罩范围内,依旧亮着可点 */
.detail-drawer-mask {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}

/* ---- Drawer panel ----
   它是 mask 的 flex 子项,默认 align-items: stretch 已经撑满内容区高度,
   不需要再自己挂一层 position: fixed(那会把基准抢回视口,又盖住顶栏) */
.detail-drawer {
  flex-shrink: 0;
  width: min(520px, 90vw);
  background: var(--color-bg);
  box-shadow: var(--shadow-xl);
  display: flex;
  flex-direction: column;
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

/* 可追问内容:按钮是动作,正文是内容,两者都要看得到 */
.rv-dig {
  margin-left: var(--space-2);
  padding: 1px 8px;
  font-size: var(--fs-xs);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-full, 999px);
  cursor: pointer;
}

.rv-dig:hover {
  filter: brightness(0.97);
}

.rv-dig:disabled {
  cursor: default;
  opacity: 0.7;
}

.rv-follow-text {
  margin: 4px 0 0;
  font-size: var(--fs-sm);
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
.conf-warn { font-size: var(--fs-xs); color: var(--color-warning, #d97706); }

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

/* 知识点来源证据块:正文之后的溯源区,内部复用 rv-file/rv-quote 样式 */
.kp-source-ref {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--color-border);
}

.kp-source-label {
  display: block;
  margin-bottom: 4px;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
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

/* ---- Mobile: fullscreen ----
   用 100% 而不是 100vw:宽度基准已经是 .page-body,写 100vw 在有纵向
   滚动条的桌面浏览器上会比容器宽几个像素,被 overflow: hidden 削掉 */
@media (max-width: 640px) {
  .detail-drawer {
    width: 100%;
  }
}
</style>
