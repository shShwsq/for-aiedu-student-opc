<script setup lang="ts">
/**
 * 题目详情弹窗(完整题面 + 正确答案 + 解析 + 源码出处 + 作答统计 + 知识点讲解)
 *
 * 列表行(题库管理 / 错题回顾 / 历史明细)只渲染一行摘要:题干超长会被
 * 截断,而选项、正确答案、解析压根不在列表 payload 里(整库下发全量内容
 * 会让列表响应翻几倍)。故点开单行时按 question_id 拉
 * GET /practice/questions/{id} 展示全量。
 *
 * 两个使用场景的差别用一个可选 prop 表达:
 * - 题库管理 / 错题回顾:不传 attempt,用于入库前校对题面、回看已归档题
 * - 历史明细:传当次作答,额外标出「你选的」选项并给出本次判分与解析
 *
 * 下发含 answer_idx,因此只可在首页复盘场景打开;答题会话中题目走
 * SessionQuestion(不含答案),本组件不参与那个流程。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { getQuestionDetail, saveKnowledgeExplanation } from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import { renderMarkdown } from '@/utils/markdown'
import {
  EMPTY_TEXT,
  formatDate,
  formatDateTime,
  formatDifficulty,
  formatPercent,
  optionLetter,
  questionStatusLabel,
  qtypeLabel,
} from '@/utils/practiceFormat'
import type { QuestionDetail, SessionAttemptItem } from '@/types/practice'

const props = defineProps<{
  open: boolean
  /** 要查看的题目 id(open 变 true 时按此拉取) */
  questionId: string | null
  /** 当次作答明细(历史明细传入;题库管理侧为 null) */
  attempt?: SessionAttemptItem | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const detail = ref<QuestionDetail | null>(null)
const loading = ref(false)
const errorMsg = ref('')

/** 请求令牌:关闭弹窗或换题后丢弃过期响应(慢响应回填到别的题会串数据) */
let requestToken = 0

async function load(questionId: string): Promise<void> {
  const token = ++requestToken
  loading.value = true
  errorMsg.value = ''
  detail.value = null
  try {
    const data = await getQuestionDetail(questionId)
    if (token !== requestToken) return
    detail.value = data
  } catch (err) {
    if (token !== requestToken) return
    errorMsg.value = extractErrorMessage(err)
  } finally {
    if (token === requestToken) loading.value = false
  }
}

/** 源码出处(仓库内相对路径 + 行区间);无出处返回空串不渲染 */
const sourceRef = computed(() => {
  const d = detail.value
  if (!d?.source_file) return ''
  return d.source_lines ? `${d.source_file}:${d.source_lines}` : d.source_file
})

// ============================================================
// 知识点讲解(错题复盘时就地看/就地改)
// ============================================================
/** 讲解编辑态(null=不在编辑) */
const explainDraft = ref<string | null>(null)
const explainSaving = ref(false)
/** 讲解区提示文本(保存成功/失败都写在这里,不弹 toast) */
const explainNote = ref('')
const explainNoteError = ref(false)

/** 讲解正文 HTML(marked + DOMPurify;Markdown 来源为模型或用户自己写的) */
const explainHtml = computed(() =>
  renderMarkdown(detail.value?.knowledge_explanation ?? ''),
)

/** 可编辑(有 knowledge_key 才能定位到知识点) */
const explainKey = computed(() => detail.value?.knowledge_key || '')

function startEditExplanation(): void {
  explainNote.value = ''
  explainNoteError.value = false
  explainDraft.value = detail.value?.knowledge_explanation || ''
}

function cancelEditExplanation(): void {
  explainDraft.value = null
  explainNote.value = ''
  explainNoteError.value = false
}

/**
 * 保存手工编辑的讲解
 *
 * 写入后 source=manual,出题收尾批量与看板「更新讲解」都不会再覆盖它。
 */
async function saveExplanation(): Promise<void> {
  const key = explainKey.value
  if (!key || explainSaving.value) return
  explainSaving.value = true
  try {
    const updated = await saveKnowledgeExplanation(key, explainDraft.value ?? '')
    if (detail.value) {
      detail.value = {
        ...detail.value,
        knowledge_explanation: updated.explanation,
        knowledge_explanation_source: updated.explanation_source,
      }
    }
    explainDraft.value = null
    explainNote.value = updated.explanation
      ? '已保存(自动生成不会再覆盖)'
      : '已清空讲解,后续可重新生成'
    explainNoteError.value = false
  } catch (err) {
    explainNote.value = extractErrorMessage(err)
    explainNoteError.value = true
  } finally {
    explainSaving.value = false
  }
}

/** 状态徽章样式(草稿=待确认偏警示,已入库=正常,已归档=降权) */
const statusClass = computed(() => {
  switch (detail.value?.status) {
    case 'draft':
      return 'status-draft'
    case 'archived':
      return 'status-archived'
    default:
      return 'status-active'
  }
})

function retry(): void {
  if (props.questionId) load(props.questionId)
}

function handleClose(): void {
  emit('close')
}

// open / questionId 变化:打开即拉取,关闭即清空并作废进行中的请求
watch(
  () => [props.open, props.questionId] as const,
  ([isOpen, id]) => {
    if (!isOpen) {
      requestToken++
      loading.value = false
      errorMsg.value = ''
      detail.value = null
      // 丢弃未提交的讲解草稿(避免换题时把上一题的正文带过去)
      explainDraft.value = null
      explainNote.value = ''
      explainNoteError.value = false
      return
    }
    if (id) load(id)
  },
  { immediate: true },
)

// Esc 关闭(与其他弹窗一致;监听随打开/关闭绑定解绑,避免常驻)
function handleKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && props.open) {
    e.preventDefault()
    handleClose()
  }
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) window.addEventListener('keydown', handleKeydown)
    else window.removeEventListener('keydown', handleKeydown)
  },
)

onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="qdialog-fade">
      <div v-if="open" class="qdialog-mask" @click.self="handleClose">
        <div class="qdialog-card" role="dialog" aria-modal="true" aria-label="题目详情">
          <header class="qdialog-header">
            <h3>题目详情</h3>
            <span
              v-if="detail"
              :class="['qdialog-status', statusClass]"
              :title="`当前状态:${questionStatusLabel(detail.status)}`"
            >{{ questionStatusLabel(detail.status) }}</span>
            <button class="qdialog-close" aria-label="关闭" @click="handleClose">×</button>
          </header>

          <div class="qdialog-body">
            <!-- 加载中 -->
            <div v-if="loading" class="qdialog-phase">
              <span class="status-spinner" /> 加载题目详情...
            </div>

            <!-- 拉取失败(题目可能已被清空) -->
            <div v-else-if="errorMsg" class="qdialog-phase">
              <p class="error-text">{{ errorMsg }}</p>
              <div class="phase-actions">
                <button class="btn-secondary" @click="handleClose">关闭</button>
                <button class="btn-primary" @click="retry">重试</button>
              </div>
            </div>

            <template v-else-if="detail">
              <!-- 标签行 -->
              <div class="qdialog-tags">
                <span v-if="detail.knowledge_name" class="tag tag-kp">
                  {{ detail.knowledge_name }}
                </span>
                <span
                  v-for="lang in (detail.languages ?? [])"
                  :key="lang"
                  class="tag tag-lang"
                >{{ lang }}</span>
                <span v-if="detail.origin === 'synthetic'" class="tag tag-synthetic" title="智能体原创的虚构代码,脱离原仓库">改编</span>
                <span class="tag">{{ qtypeLabel(detail.qtype) }}</span>
                <span class="tag">难度 {{ formatDifficulty(detail.difficulty) }}</span>
              </div>

              <!-- 历史复盘:本次作答结论(题库管理侧不显示) -->
              <p v-if="attempt" :class="['verdict', attempt.is_correct ? 'verdict-right' : 'verdict-wrong']">
                {{ attempt.is_correct ? '✓ 答对' : '✗ 答错' }}
                · 你选 {{ optionLetter(attempt.chosen_idx) }} · 正确答案 {{ optionLetter(attempt.correct_idx) }}
                <span class="verdict-time">{{ formatDateTime(attempt.answered_at) }}</span>
              </p>

              <h4 class="qdialog-stem">{{ detail.stem }}</h4>

              <pre v-if="detail.code_snippet" class="code-snippet"><code>{{ detail.code_snippet }}</code></pre>

              <ul class="qdialog-options">
                <li
                  v-for="(opt, idx) in detail.options"
                  :key="idx"
                  :class="[
                    'option-row',
                    {
                      'option-correct': idx === detail.answer_idx,
                      'option-chosen': attempt ? idx === attempt.chosen_idx : false,
                      'option-dimmed': attempt
                        ? idx !== detail.answer_idx && idx !== attempt.chosen_idx
                        : idx !== detail.answer_idx,
                    },
                  ]"
                >
                  <span class="option-key">{{ optionLetter(idx) }}</span>
                  <span class="option-text">{{ opt }}</span>
                  <span v-if="idx === detail.answer_idx" class="option-flag flag-correct">正确答案</span>
                  <span v-if="attempt && idx === attempt.chosen_idx" class="option-flag" :class="attempt.is_correct ? 'flag-correct' : 'flag-chosen'">你选的</span>
                </li>
              </ul>

              <div class="qdialog-explanation">
                <span class="section-label">解析</span>
                <p>{{ detail.explanation || '该题暂无解析' }}</p>
              </div>

              <!-- 知识点讲解:该知识点多个考点的归纳(依据出题当时的材料与题目写成) -->
              <div v-if="explainKey" class="qdialog-kp-explain">
                <div class="kp-explain-head">
                  <span class="section-label">知识点讲解</span>
                  <span
                    v-if="detail.knowledge_explanation_source"
                    :class="[
                      'explain-badge',
                      detail.knowledge_explanation_source === 'manual'
                        ? 'explain-badge-manual'
                        : 'explain-badge-auto',
                    ]"
                    :title="detail.knowledge_explanation_source === 'manual'
                      ? '你自己编辑过:自动生成不会覆盖'
                      : '模型生成'"
                  >{{ detail.knowledge_explanation_source === 'manual' ? '已编辑' : 'AI' }}</span>
                  <button
                    v-if="explainDraft === null"
                    class="kp-explain-edit"
                    :title="detail.knowledge_explanation
                      ? '编辑这段讲解(保存后自动生成不再覆盖)'
                      : '自己写一段讲解(保存到该知识点,所有同知识点题共享)'"
                    @click="startEditExplanation"
                  >{{ detail.knowledge_explanation ? '编辑' : '手写讲解' }}</button>
                </div>

                <template v-if="explainDraft !== null">
                  <textarea
                    v-model="explainDraft"
                    class="kp-explain-editor"
                    rows="8"
                    placeholder="用 Markdown 写:是什么 / 为什么会踩 / 怎么判断与修复 / 易错点"
                  />
                  <div class="kp-explain-actions">
                    <button class="btn-secondary" :disabled="explainSaving" @click="saveExplanation">
                      {{ explainSaving ? '保存中…' : '保存' }}
                    </button>
                    <button class="btn-secondary" :disabled="explainSaving" @click="cancelEditExplanation">取消</button>
                    <span class="kp-explain-hint">保存后标记为「已编辑」,自动生成不会再覆盖</span>
                  </div>
                </template>
                <div
                  v-else-if="detail.knowledge_explanation"
                  class="markdown-body kp-explain-body"
                  v-html="explainHtml"
                />
                <p v-else class="kp-explain-empty">
                  该知识点还没有讲解 — 可在知识点看板上按出题材料生成,也可以直接手写一段
                </p>
                <p
                  v-if="explainNote"
                  :class="['kp-explain-note', explainNoteError ? 'note-error' : 'note-ok']"
                >{{ explainNote }}</p>
              </div>

              <!-- 元信息:归类 / 统计 / 溯源 -->
              <dl class="qdialog-meta">
                <div class="meta-item">
                  <dt>知识点</dt>
                  <dd>
                    {{ detail.knowledge_name || EMPTY_TEXT }}
                    <span v-if="detail.knowledge_key" class="meta-sub">{{ detail.knowledge_key }}</span>
                  </dd>
                </div>
                <div class="meta-item">
                  <dt>分类</dt>
                  <dd>{{ detail.category || EMPTY_TEXT }}</dd>
                </div>
                <div class="meta-item">
                  <dt>学习主题</dt>
                  <dd>{{ detail.learning_topic || EMPTY_TEXT }}</dd>
                </div>
                <div class="meta-item">
                  <dt>本题作答</dt>
                  <dd>
                    {{ detail.attempts > 0 ? `${detail.attempts} 次 · 答对 ${detail.correct_count} · 正确率 ${formatPercent(detail.accuracy)}` : '尚未作答' }}
                  </dd>
                </div>
                <div class="meta-item">
                  <dt>入库时间</dt>
                  <dd>{{ formatDate(detail.created_at) }}</dd>
                </div>
                <div class="meta-item">
                  <dt>源码出处</dt>
                  <dd>
                    <code v-if="sourceRef" class="meta-file">{{ sourceRef }}</code>
                    <template v-else>{{ EMPTY_TEXT }}</template>
                  </dd>
                </div>
              </dl>
            </template>
          </div>

          <footer class="qdialog-footer">
            <RouterLink
              v-if="detail?.source_task_id"
              class="btn-secondary"
              :to="{ name: 'task-detail', params: { id: detail.source_task_id } }"
              target="_blank"
              rel="noopener"
              title="新标签页打开来源审计任务(题目改编自该任务的真实发现)"
            >查看来源任务</RouterLink>
            <button class="btn-primary" @click="handleClose">关闭</button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.qdialog-mask {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-6);
  background: rgba(0, 0, 0, 0.45);
}

.qdialog-card {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 720px;
  max-height: 86vh;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.qdialog-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.qdialog-header h3 {
  margin: 0;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.qdialog-status {
  padding: 2px var(--space-2);
  font-size: var(--fs-xs);
  border-radius: var(--radius-sm);
}

.status-active {
  color: var(--color-success);
  background: var(--color-success-light);
}

.status-draft {
  color: var(--color-warning);
  background: var(--color-warning-light);
}

.status-archived {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
}

.qdialog-close {
  margin-left: auto;
  width: 28px;
  height: 28px;
  font-size: var(--fs-lg);
  line-height: 1;
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.qdialog-close:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.qdialog-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
}

.qdialog-phase {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-8) var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  text-align: center;
}

.phase-actions {
  display: flex;
  gap: var(--space-3);
}

.error-text {
  margin: 0;
  color: var(--color-danger);
}

/* ---- 标签行 ---- */
.qdialog-tags {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 2px var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.tag-kp {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.tag-lang {
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
}

.tag-synthetic {
  color: var(--color-warning, #b45309);
  background: color-mix(in srgb, var(--color-warning, #b45309) 10%, transparent);
}

/* ---- 本次作答结论(历史复盘) ---- */
.verdict {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  border-radius: var(--radius-md);
}

.verdict-right {
  color: var(--color-success);
  background: var(--color-success-light);
}

.verdict-wrong {
  color: var(--color-danger);
  background: var(--color-danger-light);
}

.verdict-time {
  margin-left: auto;
  color: var(--color-text-muted);
}

/* ---- 题干 / 代码 ---- */
.qdialog-stem {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
  white-space: pre-wrap;
}

.code-snippet {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

/* ---- 选项 ---- */
.qdialog-options {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0 0 var(--space-4);
}

.option-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  word-break: break-word;
}

.option-key {
  flex-shrink: 0;
  width: 20px;
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
}

.option-text {
  flex: 1;
  min-width: 0;
}

.option-flag {
  flex-shrink: 0;
  align-self: center;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.flag-correct {
  color: var(--color-success);
  background: var(--color-success-light);
}

.flag-chosen {
  color: var(--color-danger);
  background: var(--color-danger-light);
}

.option-correct {
  border-color: var(--color-success);
  background: var(--color-success-light);
}

.option-chosen:not(.option-correct) {
  border-color: var(--color-danger);
}

.option-dimmed {
  opacity: 0.6;
}

/* ---- 解析 ---- */
.qdialog-explanation {
  margin-bottom: var(--space-4);
}

.section-label {
  display: block;
  margin-bottom: var(--space-1);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
}

.qdialog-explanation p {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---- 知识点讲解 ---- */
.qdialog-kp-explain {
  margin-bottom: var(--space-4);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
}

.kp-explain-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.kp-explain-head .section-label {
  margin-bottom: 0;
}

.explain-badge {
  padding: 0 var(--space-2);
  font-size: var(--fs-xs);
  border-radius: 999px;
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
}

.explain-badge-auto {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.explain-badge-manual {
  color: var(--color-success);
  border-color: var(--color-success);
}

.kp-explain-edit {
  margin-left: auto;
  padding: 0;
  font-size: var(--fs-xs);
  color: var(--color-primary);
  background: transparent;
  border: none;
  cursor: pointer;
}

.kp-explain-edit:hover {
  text-decoration: underline;
}

.kp-explain-body {
  margin-top: var(--space-2);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.kp-explain-body :deep(h3) {
  margin: var(--space-3) 0 var(--space-1);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.kp-explain-body :deep(p),
.kp-explain-body :deep(ul),
.kp-explain-body :deep(ol) {
  margin: 0 0 var(--space-2);
}

.kp-explain-body :deep(ul),
.kp-explain-body :deep(ol) {
  padding-left: var(--space-5);
}

.kp-explain-body :deep(code) {
  padding: 0 var(--space-1);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.kp-explain-editor {
  width: 100%;
  margin-top: var(--space-2);
  padding: var(--space-2);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  resize: vertical;
}

.kp-explain-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.kp-explain-actions .btn-secondary {
  height: 28px;
  padding: 0 var(--space-3);
  font-size: var(--fs-xs);
}

.kp-explain-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.kp-explain-empty {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.kp-explain-note {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs);
}

.note-ok {
  color: var(--color-success);
}

.note-error {
  color: var(--color-danger);
}

/* ---- 元信息 ---- */
.qdialog-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-3) var(--space-4);
  margin: 0;
  padding: var(--space-3) var(--space-4);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.qdialog-meta dt {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.qdialog-meta dd {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-text);
  word-break: break-word;
}

.meta-sub {
  margin-left: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.meta-file {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  word-break: break-all;
}

/* ---- 底部动作 ---- */
.qdialog-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.btn-primary,
.btn-secondary {
  display: inline-flex;
  align-items: center;
  height: 34px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  text-decoration: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
  color: var(--color-text-inverse);
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.status-spinner {
  display: inline-block;
  width: 18px;
  height: 18px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: qdialog-spin 0.8s linear infinite;
}

@keyframes qdialog-spin {
  to { transform: rotate(360deg); }
}

.qdialog-fade-enter-active,
.qdialog-fade-leave-active {
  transition: opacity var(--transition-base);
}

.qdialog-fade-enter-from,
.qdialog-fade-leave-to {
  opacity: 0;
}
</style>
