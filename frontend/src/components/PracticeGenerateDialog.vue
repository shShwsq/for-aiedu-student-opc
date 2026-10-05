<script setup lang="ts">
/**
 * 练习题生成预览对话框(TaskDetailView「生成练习题」入口)
 *
 * 打开时先查该任务已有的待确认 draft(含任务完成后自动生成的):
 * 有 → 直接进预览勾选;无 → 才发起异步生成(轮询进度)。
 * 用户勾选保留后确认入库(转 active),未勾选的一并丢弃。
 */
import { computed, reactive, ref, watch } from 'vue'

import {
  confirmQuestions,
  generateQuestions,
  getGenerateJob,
  listDrafts,
} from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type { DraftQuestion } from '@/types/practice'

const props = defineProps<{
  open: boolean
  /** 来源任务 id(后端校验归属) */
  taskId: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  /** 确认入库完成(confirmed=入库题数) */
  (e: 'confirmed', confirmed: number): void
}>()

type Phase = 'generating' | 'preview' | 'error' | 'confirming' | 'done'
const phase = ref<Phase>('generating')
const errorMsg = ref('')
const drafts = ref<DraftQuestion[]>([])
const skippedFindings = ref(0)
/** draft 来自之前的(自动)生成而非本次出题 */
const fromExisting = ref(false)
/** 异步出题进度(已处理/总 finding 数) */
const genDone = ref(0)
const genTotal = ref(0)
/** 本次出题是否要求「忽略去重」(仅错误态下用户主动重出时为 true) */
const forceRequested = ref(false)
/** 勾选保留的题 id */
const selected = reactive<Set<string>>(new Set())
/** 确认结果(done 阶段展示) */
const confirmedCount = ref(0)
/** 轮询令牌:关闭对话框后中止未完成的轮询 */
let pollToken = 0

function resetState(): void {
  phase.value = 'generating'
  errorMsg.value = ''
  drafts.value = []
  selected.clear()
  skippedFindings.value = 0
  confirmedCount.value = 0
  fromExisting.value = false
  genDone.value = 0
  genTotal.value = 0
  forceRequested.value = false
}

function enterPreview(questions: DraftQuestion[]): void {
  drafts.value = questions
  selected.clear()
  // 默认全部保留
  for (const q of questions) selected.add(q.id)
  phase.value = 'preview'
}

/** 打开入口:先查已有 draft,没有才发起生成 */
async function loadDraftsOrGenerate(): Promise<void> {
  phase.value = 'generating'
  errorMsg.value = ''
  try {
    const existing = await listDrafts(props.taskId)
    if (existing.length > 0) {
      fromExisting.value = true
      enterPreview(existing)
      return
    }
    await generate()
  } catch (err) {
    errorMsg.value = extractErrorMessage(err)
    phase.value = 'error'
  }
}

/** 发起异步出题并轮询进度
 *
 * force=true 时传 force_regenerate,后端跳过「本用户已出过题的 finding
 * 整条短路」的默认策略(错误态下用户主动点「重新出题」按钮触发)。
 */
async function generate(force = false): Promise<void> {
  phase.value = 'generating'
  errorMsg.value = ''
  fromExisting.value = false
  forceRequested.value = force
  genDone.value = 0
  genTotal.value = 0
  const token = ++pollToken
  try {
    const { job_id: jobId } = await generateQuestions({
      task_id: props.taskId,
      force_regenerate: force || undefined,
    })
    // 轮询直到 done/error;对话框关闭(token 失效)则中止
    for (;;) {
      if (token !== pollToken) return
      await new Promise((r) => setTimeout(r, 1500))
      if (token !== pollToken) return
      const job = await getGenerateJob(jobId)
      if (token !== pollToken) return
      genDone.value = job.done
      genTotal.value = job.total
      if (job.status === 'error') {
        errorMsg.value = job.error || '出题失败'
        phase.value = 'error'
        return
      }
      if (job.status === 'done') {
        skippedFindings.value = job.skipped_findings
        if (job.questions.length === 0) {
          errorMsg.value = '未能生成任何题目(发现内容可能不适合出题或已全部去重)'
          phase.value = 'error'
          return
        }
        enterPreview(job.questions)
        return
      }
    }
  } catch (err) {
    if (token !== pollToken) return
    errorMsg.value = extractErrorMessage(err)
    phase.value = 'error'
  }
}

// open 变 true 时加载;关闭时中止轮询(未确认的 draft 保留,下次打开直接进预览)
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      resetState()
      loadDraftsOrGenerate()
    } else {
      pollToken++  // 使进行中的轮询失效
    }
  },
  { immediate: true },
)

function toggleQuestion(id: string): void {
  if (selected.has(id)) selected.delete(id)
  else selected.add(id)
}

function toggleAll(): void {
  if (selected.size === drafts.value.length) {
    selected.clear()
  } else {
    for (const q of drafts.value) selected.add(q.id)
  }
}

const allSelected = computed(
  () => drafts.value.length > 0 && selected.size === drafts.value.length,
)

/** 确认入库:只传勾选的 id,同一任务的其余 draft 由后端删除 */
async function handleConfirm(): Promise<void> {
  if (phase.value === 'confirming') return
  phase.value = 'confirming'
  errorMsg.value = ''
  try {
    const res = await confirmQuestions({
      task_id: props.taskId,
      question_ids: Array.from(selected),
    })
    confirmedCount.value = res.confirmed
    phase.value = 'done'
  } catch (err) {
    errorMsg.value = extractErrorMessage(err)
    phase.value = 'preview'
  }
}

function handleClose(): void {
  if (phase.value === 'confirming') return
  emit('close')
}

function formatDifficulty(d: number): string {
  return Number.isInteger(d) ? String(d) : d.toFixed(1)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleClose">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>生成练习题</h3>
            <button
              class="dialog-close"
              :disabled="phase === 'confirming'"
              aria-label="关闭"
              @click="handleClose"
            >×</button>
          </header>

          <div class="dialog-body">
            <!-- 生成中(异步出题,展示进度) -->
            <div v-if="phase === 'generating'" class="phase-block">
              <span class="status-spinner" />
              <p>
                正在基于审计发现出题<template v-if="genTotal > 0">(已处理 {{ genDone }} / {{ genTotal }} 条发现)</template>,
                通常需要 1 分钟以内,请稍候...
              </p>
            </div>

            <!-- 失败 -->
            <div v-else-if="phase === 'error'" class="phase-block">
              <p class="error-text">生成失败:{{ errorMsg }}</p>
              <div class="phase-actions">
                <button class="btn-secondary" @click="handleClose">关闭</button>
                <button class="btn-primary" @click="generate(false)">重试</button>
                <!--
                  「已出过题的 finding 整条跳过」是后端默认短路(省 LLM 成本),
                  用户确认入库后再点生成会全被跳过走到这里;给出绕过短路的入口。
                -->
                <button
                  v-if="!forceRequested && skippedFindings > 0"
                  class="btn-secondary"
                  title="忽略去重短路,重新对已出过题的发现调用 LLM(会产生额外费用)"
                  @click="generate(true)"
                >重新出题(忽略去重)</button>
              </div>
            </div>

            <!-- 预览勾选 -->
            <template v-else-if="phase === 'preview' || phase === 'confirming'">
              <div class="preview-toolbar">
                <label class="select-all">
                  <input
                    type="checkbox"
                    :checked="allSelected"
                    :disabled="phase === 'confirming'"
                    @change="toggleAll"
                  />
                  全选
                </label>
                <span class="preview-count">
                  共 {{ drafts.length }} 题<template v-if="skippedFindings > 0"> · {{ skippedFindings }} 条发现未能出题</template><template v-if="fromExisting"> · 来自之前的生成</template>
                </span>
                <p v-if="errorMsg" class="error-text">{{ errorMsg }}</p>
              </div>
              <div class="draft-list">
                <div
                  v-for="(q, idx) in drafts"
                  :key="q.id"
                  :class="['draft-card', { 'draft-unselected': !selected.has(q.id) }]"
                >
                  <div class="draft-head">
                    <input
                      type="checkbox"
                      :checked="selected.has(q.id)"
                      :disabled="phase === 'confirming'"
                      title="保留该题"
                      @change="toggleQuestion(q.id)"
                    />
                    <span class="draft-no">题 {{ idx + 1 }}</span>
                    <span v-if="q.knowledge_name" class="tag tag-kp">{{ q.knowledge_name }}</span>
                    <span
                      v-for="lang in (q.languages ?? [])"
                      :key="lang"
                      class="tag tag-lang"
                    >{{ lang }}</span>
                    <span v-if="q.origin === 'synthetic'" class="tag tag-synthetic" title="智能体原创的虚构代码,脱离原仓库">改编</span>
                    <span class="tag">{{ q.qtype === 'true_false' ? '判断' : '单选' }}</span>
                    <span class="tag">难度 {{ formatDifficulty(q.difficulty) }}</span>
                  </div>
                  <p class="draft-stem">{{ q.stem }}</p>
                  <pre v-if="q.code_snippet" class="code-snippet"><code>{{ q.code_snippet }}</code></pre>
                  <ol class="draft-options">
                    <li
                      v-for="(opt, oi) in q.options"
                      :key="oi"
                      :class="{ 'option-correct': oi === q.answer_idx }"
                    >
                      {{ opt }}<span v-if="oi === q.answer_idx" class="correct-mark">✓ 正确答案</span>
                    </li>
                  </ol>
                  <p v-if="q.explanation" class="draft-explanation">解析:{{ q.explanation }}</p>
                </div>
              </div>
            </template>

            <!-- 完成 -->
            <div v-else-if="phase === 'done'" class="phase-block">
              <p class="done-text">
                <template v-if="confirmedCount > 0">
                  已入库 {{ confirmedCount }} 题,可到「自适应练习」页开始练习
                </template>
                <template v-else>未保留任何题目,候选题已全部丢弃</template>
              </p>
              <div class="phase-actions">
                <button class="btn-primary" @click="handleClose">完成</button>
              </div>
            </div>
          </div>

          <footer
            v-if="phase === 'preview' || phase === 'confirming'"
            class="dialog-footer"
          >
            <span class="footer-hint">已勾选 {{ selected.size }} / {{ drafts.length }} 题</span>
            <div class="footer-actions">
              <button class="btn-secondary" :disabled="phase === 'confirming'" @click="handleClose">
                取消
              </button>
              <button class="btn-primary" :disabled="phase === 'confirming'" @click="handleConfirm">
                {{ phase === 'confirming' ? '入库中...' : (selected.size === 0 ? '全部丢弃' : `确认入库(${selected.size})`) }}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-6);
  background: rgba(0, 0, 0, 0.45);
}

.dialog-card {
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

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-header h3 {
  margin: 0;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.dialog-close {
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

.dialog-close:hover:not(:disabled) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.dialog-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
}

.phase-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-8) var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  text-align: center;
}

.phase-block p {
  margin: 0;
  line-height: var(--lh-relaxed);
}

.phase-actions {
  display: flex;
  gap: var(--space-3);
}

.error-text {
  color: var(--color-danger);
}

.done-text {
  font-size: var(--fs-base);
  color: var(--color-text);
}

.preview-toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.select-all {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  cursor: pointer;
}

.preview-count {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.preview-toolbar .error-text {
  width: 100%;
  margin: 0;
  font-size: var(--fs-xs);
}

.draft-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.draft-card {
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg);
}

.draft-unselected {
  opacity: 0.55;
}

.draft-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.draft-no {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
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

/* 编程语言标签:低权重中性样式 */
.tag-lang {
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
}

/* 改编题标签:区分虚构代码来源 */
.tag-synthetic {
  color: var(--color-warning, #b45309);
  background: color-mix(in srgb, var(--color-warning, #b45309) 10%, transparent);
}

.draft-stem {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.code-snippet {
  margin: 0 0 var(--space-2);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

.draft-options {
  margin: 0 0 var(--space-2);
  padding-left: var(--space-5);
  font-size: var(--fs-sm);
  color: var(--color-text);
}

.draft-options li {
  margin: var(--space-1) 0;
  line-height: var(--lh-base);
}

.option-correct {
  color: var(--color-success);
  font-weight: var(--fw-medium);
}

.correct-mark {
  margin-left: var(--space-2);
  font-size: var(--fs-xs);
}

.draft-explanation {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.footer-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.footer-actions {
  display: flex;
  gap: var(--space-3);
}

.btn-primary,
.btn-secondary {
  display: inline-flex;
  align-items: center;
  height: 34px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-primary:disabled,
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.status-spinner {
  display: inline-block;
  width: 18px;
  height: 18px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: pg-spin 0.8s linear infinite;
}

@keyframes pg-spin {
  to { transform: rotate(360deg); }
}

.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity var(--transition-base);
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
