<script setup lang="ts">
/**
 * 出题进度侧栏(右侧面板):实时查看后台正在进行的练习题生成
 *
 * - job 列表由 PracticeView 轮询 GET /practice/generate/jobs 后通过 props 下发
 *   (手动出题与任务完成自动出题都可见)
 * - 选中 job 后订阅 SSE(GET /practice/generate/{job_id}/stream),
 *   展示进度条、当前 finding、工具调用与 LLM 流式输出(打字机效果)
 * - 输出区自动滚动到底部;用户手动上滚时暂停跟随
 * - 运行中可「停止出题」(协作式取消,已生成的题保留为 draft);
 *   已停止可「继续出题」(重发一次同参请求:本用户已出过题的 finding
 *   后端会整条跳过,所以天然从断点接着跑,不需要恢复旧 job)
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { generateQuestions, stopGenerateJob } from '@/api/practice'
import { subscribeGenerateStream } from '@/api/practiceStream'
import { generateJobStatusLabel } from '@/utils/practiceFormat'
import type {
  GenerateCancelledData,
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
  /** 继续出题已新建 job:让父组件立刻重拉 job 列表(不等 5 秒轮询) */
  'refresh-jobs': []
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
/**
 * 已请求停止但尚未进终态(后台线程还在收尾)
 *
 * 初值取 job 摘要的 stop_requested:协作式取消可能滞后数十秒,
 * 没有它用户会以为按钮没生效;刷新页面后也能恢复这个中间态。
 */
const stopRequested = ref(false)
/** 停止/继续请求在飞(防重复点击) */
const stopping = ref(false)
const continuing = ref(false)
/** 已停止时的断点信息(已生成题数 + done/total) */
const cancelledInfo = ref<GenerateCancelledData | null>(null)
/** 展示用错误(停止/继续失败),与 job 自身的生成失败分开 */
const actionError = ref('')

/** 输出区自动跟随(用户手动上滚时暂停) */
const followBottom = ref(true)
const outputEl = ref<HTMLElement | null>(null)

const selectedJob = computed<GenerateJobSummary | null>(
  () => props.jobs.find((j) => j.job_id === selectedJobId.value) ?? null,
)

/** 本次展示是否处于可停止的运行态(排队/出题中) */
const isBusy = computed(
  () => status.value === 'pending' || status.value === 'running',
)

/** 已生成的题是否值得保留展示(停止后决定要不要给「确认入库」入口) */
const createdCount = computed(
  () => cancelledInfo.value?.created ?? doneInfo.value?.created ?? 0,
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
  stopRequested.value = false
  cancelledInfo.value = null
  actionError.value = ''
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
  // 防重复点:列表里已标了"请求过停止"的 job,连接建立前就先把状态还原
  stopRequested.value = !!job.stop_requested

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
    onCancelled: handleCancelled,
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
  stopRequested.value = !!data.stop_requested
  // 中途接入时正在恢复工作区:用 snapshot 的 restore 字段兜底展示
  if (data.restore) restore.value = data.restore
  // 已终态的 job:只展示输出尾部文本,跳过事件重放(历史 job 一眼带过)
  if (data.status === 'done' || data.status === 'error' || data.status === 'cancelled') {
    snapshotTerminal.value = true
    streamText.value = data.recent_text
    if (data.status === 'done') {
      doneInfo.value = { created: data.created_count, skipped: data.skipped_findings }
    } else if (data.status === 'cancelled') {
      // 刷新/中途接入已停止的 job:断点信息从 snapshot 字段还原(不是事件重放)
      cancelledInfo.value = {
        created: data.created_count,
        skipped: data.skipped_findings,
        done: data.done,
        total: data.total,
      }
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
  stopRequested.value = false
  closeStream()
}

function handleError(data: GenerateErrorData): void {
  status.value = 'error'
  errorMsg.value = data.message
  stopRequested.value = false
  closeStream()
}

/**
 * 用户停止出题的终止事件:状态置 cancelled,保留"已生成几题"与断点进度
 *
 * 已生成的 draft 仍在库里(与 done 同构),所以终止后仍可直接「确认入库」。
 */
function handleCancelled(data: GenerateCancelledData): void {
  status.value = 'cancelled'
  cancelledInfo.value = data
  doneInfo.value = { created: data.created, skipped: data.skipped }
  if (typeof data.total === 'number' && data.total > 0) {
    total.value = data.total
    done.value = data.done ?? data.total
  }
  stopRequested.value = false
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

/** job 列表项状态文案(口径抽到 utils/practiceFormat 做单测) */
function statusLabel(job: GenerateJobSummary): string {
  return generateJobStatusLabel(job)
}

/**
 * restore failed 横幅标题
 *
 * 后端在两种情形下都推 phase=failed:① 真的恢复失败(降级为无源码上下文继续
 * 出题),② 用户在克隆阶段按了停止(job 直接收 cancelled,根本没继续出题)。
 * ② 沿用①的文案会谎称"已降级继续出题",与旁边的"已停止"footer 自相矛盾
 * (刷新/中途接入走 snapshot 路径时同样会看到这条横幅)。
 */
const restoreFailedText = computed(() => {
  const msg = restore.value?.message || ''
  if (msg.includes('用户请求停止') || status.value === 'cancelled') {
    return '工作区恢复已按请求停止'
  }
  return '工作区恢复失败,已降级为无代码上下文出题'
})

/** 打开题目入库弹窗(仅 job 关联了来源任务时可用) */
function handleConfirmPreview(): void {
  const taskId = selectedJob.value?.task_id
  if (taskId) emit('confirm-preview', taskId)
}

/**
 * 停止出题:置后端标志后立即返回,真正生效在下一个检查点
 *
 * 本地先把 stopRequested 拉起来(不依赖下一轮轮询/snapshot),
 * 终态 cancelled 仍由 SSE 事件送达 —— 单一写者,不会前后不一致。
 */
async function handleStop(): Promise<void> {
  const jobId = selectedJobId.value
  if (!jobId || stopping.value || !isBusy.value) return
  stopping.value = true
  actionError.value = ''
  try {
    await stopGenerateJob(jobId)
    // await 期间可能已被 SSE 收口(handleCancelled 把标志复位并关了流),
    // 也可能列表轮询使本 job 进终态后 watch 已切到新 job:
    // 只在"还是那个 job、仍在跑"时置位,否则这个 true 会残留或写到新 job 上
    if (selectedJobId.value === jobId && isBusy.value) stopRequested.value = true
  } catch (err) {
    actionError.value = errMessage(err)
  } finally {
    stopping.value = false
  }
}

/**
 * 继续出题:重发一次同参请求(新建 job,不复活旧 job)
 *
 * 后端 force_regenerate=false 会把本用户已出过题的 finding 整条跳过,
 * 因此"从断点接着跑"无需恢复旧 job 的线程与事件序号;_select_findings
 * 顺序确定(practice_worthy 优先 + created_at),两次选到的集合一致。
 * 同用户集合里已出过题的那部分会先被计入进度,所以一上来 done 会跳一大截。
 */
async function handleContinue(): Promise<void> {
  const job = selectedJob.value
  if (!job?.task_id || continuing.value) return
  continuing.value = true
  actionError.value = ''
  try {
    await generateQuestions({
      task_id: job.task_id,
      max_findings: job.max_findings ?? undefined,
    })
    // 解除手动锁定:下方 watch 会自动改订阅到新跑起来的 job
    userPicked.value = false
    emit('refresh-jobs')
  } catch (err) {
    actionError.value = errMessage(err)
  } finally {
    continuing.value = false
  }
}

/** 接口错误文案(无 response 时是网络问题,不是后端 detail) */
function errMessage(err: unknown): string {
  const e = err as { response?: { data?: { detail?: string } }; message?: string }
  return e?.response?.data?.detail || e?.message || '操作失败,请稍后重试'
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

        <!-- 运行中操作区:停止出题(协作式取消,已生成的题仍保留) -->
        <div v-if="isBusy" class="gen-actions">
          <button
            class="gen-stop-btn"
            :disabled="stopping || stopRequested"
            :title="stopRequested
              ? '已提交停止请求,已生成的题仍会保留,等待当前一条跑完'
              : '停止后续出题;已生成的候选题仍会保留待确认'"
            @click="handleStop"
          >{{ stopping || stopRequested ? '正在停止...' : '停止出题' }}</button>
        </div>

        <!-- 工作区恢复横幅(沙箱已清理时重新 clone,含克隆进度) -->
        <div v-if="restore" :class="['gen-restore', `gen-restore-${restore.phase}`]">
          <template v-if="restore.phase === 'failed'">
            <p class="gen-restore-text">{{ restoreFailedText }}</p>
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
        <!-- 已停止:说明断点与保留结果,给「继续出题」与「确认入库」入口 -->
        <div v-else-if="status === 'cancelled'" class="gen-footer gen-footer-cancelled">
          <p class="gen-done-text">
            已停止出题<template v-if="total">(跑到 {{ done }}/{{ total }})</template>
            <template v-if="createdCount > 0">· 已生成 {{ createdCount }} 道候选题仍待确认</template>
            <template v-else>· 停止前未生成题目</template>
          </p>
          <button
            v-if="selectedJob?.task_id"
            class="btn-primary btn-small gen-confirm-btn"
            title="接着未出题的发现继续(已出过题的发现会整条跳过,不重复花成本)"
            :disabled="continuing"
            @click="handleContinue"
          >{{ continuing ? '提交中...' : '继续出题' }}</button>
          <button
            v-if="selectedJob?.task_id && createdCount > 0"
            class="gen-secondary-btn gen-confirm-btn"
            title="预览已生成的候选题并勾选入库"
            @click="handleConfirmPreview"
          >确认入库</button>
        </div>

        <!-- 停止/继续请求失败(与 job 自身的生成失败分开展示) -->
        <p v-if="actionError" class="gen-action-error">{{ actionError }}</p>
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
/* 用户停止:中性色(不是失败,是"中途收手且结果已保留") */
.gen-status-cancelled { color: var(--color-text-secondary); }

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

/* 运行中操作区(停止出题):靠右不抢进度条位置,与进度行之间留小间距 */
.gen-actions {
  display: flex;
  justify-content: flex-end;
}

/* 停止出题按钮:描边危险色(比实心主按钮弱一级,避免误读为"主操作") */
.gen-stop-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.gen-stop-btn:hover:not(:disabled) {
  border-color: var(--color-danger);
  background: var(--color-bg-secondary);
}

.gen-stop-btn:disabled {
  color: var(--color-text-secondary);
  cursor: default;
  opacity: 0.7;
}

/* 次要按钮(已停止后的「确认入库」,主位给「继续出题」) */
.gen-secondary-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.gen-secondary-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

/* 已停止区两个按钮上下堆叠(gen-confirm-btn 是 100% 宽),拉开一点间距 */
.gen-footer-cancelled .gen-confirm-btn + .gen-confirm-btn {
  margin-top: var(--space-1);
}

.gen-footer-error {
  color: var(--color-danger);
  background: var(--color-bg-secondary);
}

/* 已停止:中性底色(有保留结果要告知,但不是错误) */
.gen-footer-cancelled {
  color: var(--color-text);
  background: var(--color-bg-secondary);
}

/* 停止/继续请求失败的一行提示(不与 job 生成失败混在一起) */
.gen-action-error {
  margin: 0;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-danger);
  background: var(--color-bg-secondary);
  border-radius: var(--radius-sm);
}
</style>
