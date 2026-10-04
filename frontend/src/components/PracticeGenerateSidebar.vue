<script setup lang="ts">
/**
 * 出题进度侧栏(右侧面板):实时查看后台正在进行的练习题生成
 *
 * - job 列表由 PracticeView 轮询 GET /practice/generate/jobs 后通过 props 下发
 *   (手动出题与任务完成自动出题都可见)
 * - 选中 job 后订阅 SSE(GET /practice/generate/{job_id}/stream),
 *   展示进度条、当前 finding、工具调用与 LLM 流式输出(打字机效果)
 * - 输出区自动滚动到底部;用户手动上滚时暂停跟随
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { subscribeGenerateStream } from '@/api/practiceStream'
import type {
  GenerateDoneData,
  GenerateErrorData,
  GenerateExplainData,
  GenerateFindingData,
  GenerateJobSummary,
  GenerateProgressData,
  GenerateRestoreData,
  GenerateSnapshotData,
  GenerateTokenData,
  GenerateToolData,
} from '@/types/practice'

const props = defineProps<{
  /** 当前用户的出题 job 摘要列表(运行中优先,父组件轮询维护) */
  jobs: GenerateJobSummary[]
}>()

const emit = defineEmits<{
  close: []
  /** 请求打开题目入库弹窗(携带 job 的来源任务 id,由父组件展示 PracticeGenerateDialog) */
  'confirm-preview': [taskId: string]
}>()

// ============================================================
// 选中 job 与 SSE 订阅
// ============================================================
const selectedJobId = ref('')
/** 用户手动点选过 job(有运行中 job 时也不自动跳走) */
const userPicked = ref(false)
let es: EventSource | null = null

// [诊断] 重放/直播事件是否被 snapshotTerminal 正确拦截:
// skip 应远大于 render;若终态 job 的 render 持续增长,说明拦截失效。
// 注意:必须声明在 immediate watch 之前,否则首次触发时访问会报 TDZ 错误
const diagCounters = { tokenRendered: 0, tokenSkipped: 0, findingRendered: 0, findingSkipped: 0 }
let diagLogTimer: ReturnType<typeof setInterval> | null = null

// ============================================================
// 实时展示状态(由 SSE 事件驱动)
// 硬约束:这些 ref 必须声明在下方 immediate watch 之前——
// watch 在挂载时同步触发,经 pickDefaultJob → subscribeToJob →
// resetStreamState 访问它们;声明在 watch 之后会报 TDZ 错误,
// 导致 setup 中断、侧栏渲染失败并拖垮整页交互(历史教训)
// ============================================================
const status = ref<GenerateJobSummary['status']>('pending')
const done = ref(0)
const total = ref(0)
const currentFinding = ref('')
const errorMsg = ref('')
const doneInfo = ref<GenerateDoneData | null>(null)
/** 流式输出文本(含 finding 分隔与工具调用标记) */
const streamText = ref('')
/** snapshot 即终态时不再消费重放事件,只展示 recent_text 尾部 */
const snapshotTerminal = ref(false)
/** 出题前工作区恢复状态(沙箱已清理时重新 clone,首条 finding 到达后清除) */
const restore = ref<GenerateRestoreData | null>(null)

/** 输出区自动跟随(用户手动上滚时暂停) */
const followBottom = ref(true)
const outputEl = ref<HTMLElement | null>(null)

const selectedJob = computed<GenerateJobSummary | null>(
  () => props.jobs.find((j) => j.job_id === selectedJobId.value) ?? null,
)

/** 默认选中:运行中优先(jobs 已按运行中在前排序) */
function pickDefaultJob(): void {
  const next = props.jobs[0]
  if (next) subscribeToJob(next)
}

/** job 列表更新:维护选中项(删除/新运行中 job 的自动切换) */
watch(
  () => props.jobs,
  (jobs) => {
    if (!jobs.length) {
      closeStream()
      selectedJobId.value = ''
      return
    }
    const current = jobs.find((j) => j.job_id === selectedJobId.value)
    if (!current) {
      // 选中项已不在列表(过期清理):回到默认
      userPicked.value = false
      pickDefaultJob()
      return
    }
    // 当前看的不是运行中 job,且用户没手动锁定 → 切到运行中的
    const isCurrentActive = current.status === 'pending' || current.status === 'running'
    if (!isCurrentActive && !userPicked.value) {
      const running = jobs.find((j) => j.status === 'pending' || j.status === 'running')
      if (running && running.job_id !== selectedJobId.value) {
        subscribeToJob(running)
      }
    }
  },
  { immediate: true },
)

function handlePickJob(job: GenerateJobSummary): void {
  if (job.job_id === selectedJobId.value) return
  userPicked.value = true
  subscribeToJob(job)
}

// ============================================================
// 输出区滚动与流状态维护(函数声明会提升,不受声明顺序影响)
// ============================================================
function scheduleScroll(): void {
  if (!followBottom.value) return
  nextTick(() => {
    const el = outputEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function handleOutputScroll(): void {
  const el = outputEl.value
  if (!el) return
  followBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 12
}

function appendOutput(text: string): void {
  streamText.value += text
  scheduleScroll()
}

// [诊断] 每 2 秒汇总输出一次事件处理计数(状态声明在文件前部)
function ensureDiagLog(): void {
  if (diagLogTimer) return
  diagLogTimer = setInterval(() => {
    const { tokenRendered, tokenSkipped, findingRendered, findingSkipped } = diagCounters
    if (tokenRendered || tokenSkipped || findingRendered || findingSkipped) {
      console.warn('[gen-sidebar] 事件处理计数', {
        tokenRendered, tokenSkipped, findingRendered, findingSkipped,
        streamTextLen: streamText.value.length,
        snapshotTerminal: snapshotTerminal.value,
        status: status.value,
      })
    }
    diagCounters.tokenRendered = 0
    diagCounters.tokenSkipped = 0
    diagCounters.findingRendered = 0
    diagCounters.findingSkipped = 0
  }, 2000)
}

function resetStreamState(): void {
  status.value = 'pending'
  done.value = 0
  total.value = 0
  currentFinding.value = ''
  errorMsg.value = ''
  doneInfo.value = null
  streamText.value = ''
  snapshotTerminal.value = false
  restore.value = null
  followBottom.value = true
}

function closeStream(): void {
  if (es) {
    es.close()
    es = null
  }
}

/** 切换订阅目标:重置展示状态并建立 SSE */
function subscribeToJob(job: GenerateJobSummary): void {
  // [诊断] 订阅目标与当前列表规模
  console.warn('[gen-sidebar] 订阅 job', job.job_id, {
    status: job.status, done: job.done, total: job.total, jobsLen: props.jobs.length,
  })
  ensureDiagLog()
  closeStream()
  selectedJobId.value = job.job_id
  resetStreamState()
  // 先用列表摘要兜底展示(避免 SSE 连接前空白)
  status.value = job.status
  done.value = job.done
  total.value = job.total
  currentFinding.value = job.current_finding
  errorMsg.value = job.error

  es = subscribeGenerateStream(job.job_id, {
    onSnapshot: handleSnapshot,
    onFinding: handleFinding,
    onToken: handleToken,
    onTool: handleTool,
    onRestore: handleRestore,
    onExplain: handleExplain,
    onProgress: handleProgress,
    onDone: handleDone,
    onError: handleError,
  })
}

// ---- SSE 事件处理 ----

function handleSnapshot(data: GenerateSnapshotData): void {
  // [诊断] snapshot 状态决定后续重放事件是否被跳过,异常时重点排查这里
  console.warn('[gen-sidebar] snapshot', {
    status: data.status,
    done: data.done,
    total: data.total,
    recentLen: data.recent_text?.length ?? 0,
  })
  status.value = data.status
  done.value = data.done
  total.value = data.total
  currentFinding.value = data.current_finding
  errorMsg.value = data.error
  // 中途接入时正在恢复工作区:用 snapshot 的 restore 字段兜底展示
  if (data.restore) restore.value = data.restore
  // 已终态的 job:只展示输出尾部文本,跳过事件重放(历史 job 一眼带过)
  if (data.status === 'done' || data.status === 'error') {
    snapshotTerminal.value = true
    streamText.value = data.recent_text
    if (data.status === 'done') {
      doneInfo.value = { created: data.created_count, skipped: data.skipped_findings }
    }
    scheduleScroll()
  }
}

function handleFinding(data: GenerateFindingData): void {
  if (snapshotTerminal.value) {
    diagCounters.findingSkipped++
    return
  }
  diagCounters.findingRendered++
  currentFinding.value = data.title
  restore.value = null // 开始出题说明工作区阶段已结束,收起恢复横幅
  appendOutput(`\n\n━━ 发现 ${data.index}/${data.total}:${data.title} ━━\n`)
}

function handleToken(data: GenerateTokenData): void {
  if (snapshotTerminal.value) {
    diagCounters.tokenSkipped++
    return
  }
  diagCounters.tokenRendered++
  appendOutput(data.delta)
}

function handleTool(data: GenerateToolData): void {
  if (snapshotTerminal.value) return
  appendOutput(`\n[工具] ${data.summary}\n`)
}

function handleRestore(data: GenerateRestoreData): void {
  if (snapshotTerminal.value) return
  restore.value = data
}

/**
 * 收尾知识点讲解阶段(仅在用户开启「出题后自动更新讲解」时才有)
 *
 * 直接追写到输出区(与「[工具]」行同款),不新增模板与样式;
 * 讲解写入的知识点数就是用户需要看到的唯一信息。
 */
function handleExplain(data: GenerateExplainData): void {
  if (snapshotTerminal.value) return
  if (data.phase === 'start') {
    appendOutput(`\n[知识点讲解] 开始生成 ${data.total ?? 0} 条(分 ${data.batches ?? 1} 批)…\n`)
  } else if (data.phase === 'done') {
    appendOutput(`\n[知识点讲解] 已更新 ${data.written ?? 0} 条\n`)
  } else {
    appendOutput(`\n[知识点讲解] 生成失败(不影响已生成题目):${data.message || '未知原因'}\n`)
  }
}

function handleProgress(data: GenerateProgressData): void {
  done.value = data.done
  total.value = data.total
}

function handleDone(data: GenerateDoneData): void {
  status.value = 'done'
  doneInfo.value = data
  if (total.value > 0) done.value = total.value
  closeStream()
}

function handleError(data: GenerateErrorData): void {
  status.value = 'error'
  errorMsg.value = data.message
  closeStream()
}

onBeforeUnmount(() => {
  closeStream()
  // [诊断] 清理汇总定时器,并输出最终计数
  if (diagLogTimer) {
    clearInterval(diagLogTimer)
    diagLogTimer = null
  }
  console.warn('[gen-sidebar] 组件卸载,累计计数', { ...diagCounters, streamTextLen: streamText.value.length })
})

// ============================================================
// 展示辅助
// ============================================================
const progressPercent = computed(() => {
  if (!total.value) return 0
  return Math.min(100, Math.round((done.value / total.value) * 100))
})

function statusLabel(job: GenerateJobSummary): string {
  if (job.status === 'pending') {
    // 恢复工作区阶段(克隆可能耗时数十秒),给出明确状态避免误以为卡死
    const phase = job.restore?.phase
    if (phase === 'start' || phase === 'progress') return '恢复工作区中'
    return '排队中'
  }
  if (job.status === 'running') return `出题中 ${job.done}/${job.total || '?'}`
  if (job.status === 'done') return `已完成 · ${job.created_count} 题`
  return '失败'
}

/** 打开题目入库弹窗(仅 job 关联了来源任务时可用) */
function handleConfirmPreview(): void {
  const taskId = selectedJob.value?.task_id
  if (taskId) emit('confirm-preview', taskId)
}
</script>

<template>
  <aside class="gen-sidebar">
    <div class="gen-head">
      <h3 class="gen-title">出题进度</h3>
      <button class="gen-close-btn" title="收起出题进度" @click="emit('close')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
          <path d="M18 6L6 18M6 6l12 12" />
        </svg>
      </button>
    </div>

    <p v-if="jobs.length === 0" class="gen-empty">
      暂无出题任务 — 在审计任务详情页点「生成练习题」,或等待任务完成后自动出题
    </p>

    <template v-else>
      <!-- job 列表(运行中在前) -->
      <div class="gen-job-list">
        <button
          v-for="job in jobs"
          :key="job.job_id"
          :class="['gen-job-item', { 'gen-job-active': job.job_id === selectedJobId }]"
          @click="handlePickJob(job)"
        >
          <span class="gen-job-title">{{ job.task_title || '未命名任务' }}</span>
          <span class="gen-job-meta">
            <span :class="['gen-source-tag', job.source === 'auto' ? 'gen-source-auto' : 'gen-source-manual']">
              {{ job.source === 'auto' ? '自动' : '手动' }}
            </span>
            <span :class="['gen-status', `gen-status-${job.status}`]">{{ statusLabel(job) }}</span>
          </span>
        </button>
      </div>

      <!-- 选中 job 的实时详情 -->
      <div v-if="selectedJob" class="gen-detail">
        <div class="gen-progress-row">
          <div class="gen-progress-track">
            <div
              :class="['gen-progress-fill', { 'gen-progress-error': status === 'error' }]"
              :style="{ width: `${progressPercent}%` }"
            />
          </div>
          <span class="gen-progress-text">
            {{ total > 0 ? `${done}/${total}` : '—' }}
          </span>
        </div>

        <p v-if="status === 'pending'" class="gen-hint">排队中,等待开始...</p>
        <p v-else-if="currentFinding && status === 'running'" class="gen-finding">
          正在出题:{{ currentFinding }}
        </p>

        <!-- 工作区恢复横幅(沙箱已清理时重新 clone,含克隆进度) -->
        <div v-if="restore" :class="['gen-restore', `gen-restore-${restore.phase}`]">
          <template v-if="restore.phase === 'failed'">
            <p class="gen-restore-text">工作区恢复失败,已降级为无代码上下文出题</p>
            <p v-if="restore.message" class="gen-restore-msg">{{ restore.message }}</p>
          </template>
          <template v-else-if="restore.phase === 'done'">
            <p class="gen-restore-text">工作区已恢复,即将基于源码出题</p>
          </template>
          <template v-else>
            <p class="gen-restore-text">
              <span class="gen-restore-spinner" aria-hidden="true" />
              正在恢复工作区(重新克隆仓库)<template v-if="restore.percent != null"> {{ restore.percent }}%</template>
            </p>
            <div v-if="restore.percent != null" class="gen-restore-track">
              <div class="gen-restore-fill" :style="{ width: `${restore.percent}%` }" />
            </div>
            <p v-if="restore.message" class="gen-restore-msg">{{ restore.message }}</p>
          </template>
        </div>

        <!-- LLM 流式输出区 -->
        <div ref="outputEl" class="gen-output" @scroll="handleOutputScroll">
          <pre class="gen-output-text">{{ streamText || (status === 'running' ? '等待模型输出...' : '') }}</pre>
        </div>

        <!-- 终止态提示 -->
        <div v-if="status === 'done'" class="gen-footer gen-footer-done">
          <p class="gen-done-text">
            已生成 {{ doneInfo?.created ?? 0 }} 道题<template v-if="(doneInfo?.skipped ?? 0) > 0">(另有 {{ doneInfo?.skipped }} 条发现未能出题)</template>
          </p>
          <button
            v-if="selectedJob?.task_id"
            class="btn-primary btn-small gen-confirm-btn"
            title="预览候选题并勾选入库"
            @click="handleConfirmPreview"
          >确认入库</button>
        </div>
        <div v-else-if="status === 'error'" class="gen-footer gen-footer-error">
          生成失败:{{ errorMsg || '未知错误' }}
        </div>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.gen-sidebar {
  flex-shrink: 0;
  width: 380px;
  border-left: 1px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 手机窄屏:生成进度侧栏改为右侧覆盖式抽屉(定位基准为宿主 .page-body) */
@media (max-width: 640px) {
  .gen-sidebar {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    width: min(380px, 92vw);
    box-shadow: var(--shadow-xl);
  }
}

.gen-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.gen-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.gen-close-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.gen-close-btn:hover {
  color: var(--color-text);
  background: var(--color-bg-secondary);
}

.gen-empty {
  margin: var(--space-4);
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--color-text-secondary);
}

/* ---- job 列表 ---- */
.gen-job-list {
  flex-shrink: 0;
  max-height: 180px;
  overflow-y: auto;
  border-bottom: 1px solid var(--color-border);
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.gen-job-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.gen-job-item:hover {
  background: var(--color-bg-secondary);
}

.gen-job-active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.gen-job-title {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gen-job-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 11px;
  color: var(--color-text-secondary);
}

.gen-source-tag {
  padding: 0 var(--space-1);
  border-radius: var(--radius-sm);
  font-size: 10px;
  line-height: 16px;
}

.gen-source-auto {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.gen-source-manual {
  color: var(--color-text-secondary);
  background: var(--color-bg-secondary);
}

.gen-status-running { color: var(--color-primary); }
.gen-status-done { color: var(--color-success, #16a34a); }
.gen-status-error { color: var(--color-danger); }
.gen-status-pending { color: var(--color-text-secondary); }

/* ---- 详情区 ---- */
.gen-detail {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: var(--space-3) var(--space-4);
  gap: var(--space-2);
}

.gen-progress-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.gen-progress-track {
  flex: 1;
  height: 6px;
  background: var(--color-bg-secondary);
  border-radius: 3px;
  overflow: hidden;
}

.gen-progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: 3px;
  transition: width var(--transition-normal, 0.3s ease);
}

.gen-progress-error {
  background: var(--color-danger);
}

.gen-progress-text {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

.gen-hint,
.gen-finding {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gen-finding {
  color: var(--color-text);
}

/* ---- 工作区恢复横幅 ---- */
.gen-restore {
  flex-shrink: 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-secondary);
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.gen-restore-text {
  margin: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text);
}

.gen-restore-done { color: var(--color-success, #16a34a); }
.gen-restore-failed { color: var(--color-danger); }

.gen-restore-spinner {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: gen-restore-spin 0.8s linear infinite;
}

@keyframes gen-restore-spin {
  to { transform: rotate(360deg); }
}

.gen-restore-track {
  margin-top: var(--space-1);
  height: 4px;
  background: var(--color-border);
  border-radius: 2px;
  overflow: hidden;
}

.gen-restore-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: 2px;
  transition: width var(--transition-normal, 0.3s ease);
}

.gen-restore-msg {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 流式输出区 ---- */
.gen-output {
  flex: 1;
  min-height: 120px;
  overflow-y: auto;
  padding: var(--space-3);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.gen-output-text {
  margin: 0;
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: 11px;
  line-height: 1.6;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---- 终止态提示 ---- */
.gen-footer {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
}

.gen-footer-done {
  color: var(--color-success, #16a34a);
  background: var(--color-bg-secondary);
}

.gen-done-text {
  margin: 0 0 var(--space-2);
}

.gen-confirm-btn {
  width: 100%;
}

.gen-footer-error {
  color: var(--color-danger);
  background: var(--color-bg-secondary);
}
</style>
