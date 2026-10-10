<script setup lang="ts">
/**
 * 任务详情页
 *
 * 布局区域:
 * 1. 主区协作对话流:按 round_idx 分组,展示 agent2 与 agent1 的来回
 * 2. 右侧栏:结果清单(默认折叠,分组由 task.params._grouping 驱动)/
 *    任务概览 / 动态验证配置
 *
 * 实时更新:SSE 接收每条对话/状态变更 + thinking_delta(流式 token 增量)。
 * 初始加载 GET /tasks/{id} 拿快照(补历史),然后 SSE 接收增量。
 *
 * 流式思考显示(thinking_delta):
 * - 一次 LLM 调用对应一个 conv_id,前端按 conv_id 累积 reasoning + content
 * - 流式期间以"流式思考卡片"显示打字机效果;思考链(reasoning)在流式期间
 *   自动展开(过阈值后,见 utils/thinkingExpand),流式结束后自动折叠回一行标题
 *   —— 与执行过程组的"运行时展开、结束收起"同一套生命周期取向:
 *   看的是直播,留下的是干净的聊天记录
 * - 思考链同时以 type=thinking 落库,落库即推 conversation 事件
 *   (带 stream_conv_id),前端据此把实时卡片退役成只读历史卡片;
 *   刷新/中途离开页面再回来时由 GET /tasks/{id} 快照还原同一批卡片
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { jsonrepair } from 'jsonrepair'

import AppHeader from '@/components/AppHeader.vue'
import Agent2Panel from '@/components/Agent2Panel.vue'
import ConversationMessage from '@/components/ConversationMessage.vue'
import PracticeGenerateDialog from '@/components/PracticeGenerateDialog.vue'
import UserMessageInput from '@/components/UserMessageInput.vue'
import TaskRuntimeSettings from '@/components/TaskRuntimeSettings.vue'
import CommandConfirmDialog from '@/components/CommandConfirmDialog.vue'
import VerifyActionDialog from '@/components/VerifyActionDialog.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  downloadTaskReportMarkdown,
  getPendingVerifyAction,
  getPendingCommandConfirm,
  getTask,
  getTaskReportHtml,
  pauseTask,
  resumeTask,
  retryTask,
  sendTaskMessage,
  skipPreClone,
  stopTaskReview,
  submitVerifyAction,
  submitCommandConfirm,
  updateTaskVerifierConfig,
  withdrawTaskMessage,
} from '@/api/task'
import { subscribeTaskStream } from '@/api/stream'
import { listDrafts, listGenerateJobs, getTaskGenerateModel } from '@/api/practice'
import { ensureFeaturesLoaded, practiceEnabled } from '@/composables/useFeatures'
import { buildStorageKey, useResizableSidebar } from '@/composables/useResizableSidebar'
import { useAuthStore } from '@/stores/auth'
import { listArtifacts } from '@/api/taskArtifacts'
import { clientLog } from '@/utils/clientLog'
import { extractErrorMessage } from '@/utils/error'
import { parseDiffFileSegments } from '@/utils/diffFiles'
import { triggerBlobDownload } from '@/utils/download'
import { renderMarkdown } from '@/utils/markdown'
import { buildToolSegments, buildToolSummary, parseAgentTrace, toolFileTargetOf } from '@/utils/toolSummary'
import { COLLAPSE_GRACE_MS, isThinkingExpanded, shouldLatchAutoExpand } from '@/utils/thinkingExpand'
import { findRetiredCardConvId } from '@/utils/thinkingReconcile'
import {
  bucketizeReviewItems,
  confidenceClass,
  confidenceLabel,
  formatConfidenceScore,
} from '@/utils/reviewBucket'
import type {
  AttachmentInfo,
  CloneProgressEventData,
  Conversation,
  KnowledgePointEventData,
  PlanStep,
  ReviewItem,
  ReviewItemEventData,
  ReviewPlanUpdateEventData,
  ReviewSummary,
  SendMessageResponse,
  TaskDetail,
  TaskResult,
  TaskStatus,
  ThinkingDeltaEventData,
  VerifyActionEventData,
  CommandConfirmEventData,
} from '@/types/task'
import type { TaskArtifact } from '@/types/taskArtifact'
import type { GenerateJobSummary, GenerateModelInfo } from '@/types/practice'

const route = useRoute()
const router = useRouter()

const task = ref<TaskDetail | null>(null)
const loading = ref(true)
const error = ref('')
let eventSource: EventSource | null = null
/** 刚发起 resume/retry 的窗口标志:onDone 触发时校验是否竞态误推用 */
const resumingRef = ref(false)
/**
 * 运行中发送、尚未被 agent1 消费的用户补充消息(TRAE 式待处理条目,
 * 展示在输入框上方)。消费时收到同 id 的 conversation 事件 → 转入对话流。
 * SSE 历史补播可重建(刷新后快照里它在流中,pending 事件到达时移出)
 */
const pendingUserMessages = ref<Conversation[]>([])
/** 组件已卸载标志(onDone 异步窗口内防止泄漏新 SSE 连接) */
let unmountedFlag = false
/** 对话流容器引用,用于自动滚动到底部 */
const conversationRef = ref<HTMLElement | null>(null)
/** 自动跟随底部开关:用户手动上滚时暂停,滚回底部附近自动恢复
 *  (参考 PracticeGenerateSidebar 的 followBottom 模式;流式期间高频
 *  thinking_delta 不再无条件覆盖用户的滚动位置) */
const followBottom = ref(true)

/** 工作区侧栏是否折叠(默认折叠,完全隐藏) */
const workspaceCollapsed = ref(true)

/** 核查与结果侧栏是否折叠(右侧栏,默认展开;折叠时完全隐藏,主区聚焦对话) */
const detailCollapsed = ref(false)

/**
 * 核查与结果侧栏宽度:左缘手柄拖拽调宽,松手按「用户 email + 栏位」存 localStorage。
 * narrowMax / maxViewportRatio 两个约束都对应下面的 CSS 与主区体验:
 * - 1024px 以下侧栏改成覆盖式抽屉(见 @media),那档宽度归 CSS,手柄隐藏;
 * - 上限收一半视口:右栏每变宽 1px 都是从主对话流身上扣的,别让它顶掉阅读宽度。
 */
const auth = useAuthStore()
const {
  inlineWidth: detailSidebarStyle,
  resizing: detailSidebarResizing,
  isNarrow: detailSidebarNarrow,
  ariaValueNow: detailSidebarWidth,
  ariaValueMin: detailSidebarMin,
  ariaValueMax: detailSidebarMax,
  startResize: onDetailResizeStart,
  onResizeKeydown: onDetailResizeKeydown,
} = useResizableSidebar({
  min: 320,
  max: 640,
  defaultWidth: 420,
  narrowMax: 1024,
  maxViewportRatio: 0.5,
  storageKey: () => (auth.user ? buildStorageKey('task-detail', auth.user.email) : null),
})

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

function toggleDetail(): void {
  detailCollapsed.value = !detailCollapsed.value
}

// 从首页「最近任务」进入时带 ?workspace=1:自动展开历史任务侧栏
// (列表→详情的浏览连续性),读取后清掉参数,避免用户手动折叠后刷新又被强制展开
if (route.query.workspace === '1') {
  workspaceCollapsed.value = false
  router.replace({ query: { ...route.query, workspace: undefined } })
}

// ---- 工作区变更(任务完成时捕获的 git diff patch) ----

/** 任务的工作区 diff 产物(任务完成时由后端捕获,kind="git_diff") */
const workspaceArtifact = ref<TaskArtifact | null>(null)
/** 仓库树快照产物(clone 时保底/任务结束时捕获,kind="repo_tree";无变更时的侧栏兜底) */
const repoTreeArtifact = ref<TaskArtifact | null>(null)
/** 工作区变更区折叠状态(默认展开) */
const workspaceChangesCollapsed = ref(false)

/** diff 产物元信息(从 metadata_ 解析,缺省 0/false) */
const artifactMeta = computed(() => {
  const m = workspaceArtifact.value?.metadata_
  return {
    files_changed: Number(m?.files_changed ?? 0),
    char_count: Number(m?.char_count ?? 0),
    truncated: Boolean(m?.truncated ?? false),
  }
})

/** patch 文本按行拆分(供模板逐行着色,文本插值自动转义,无 XSS 风险) */
const diffLines = computed<string[]>(() => {
  const c = workspaceArtifact.value?.content ?? ''
  return c ? c.split('\n') : []
})

/** 按行首字符判定 diff 行类型(纯 CSS 着色) */
function diffLineClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-line-meta'
  if (line.startsWith('@@')) return 'diff-line-hunk'
  if (line.startsWith('+')) return 'diff-line-add'
  if (line.startsWith('-')) return 'diff-line-del'
  return 'diff-line-ctx'
}

/** 按文件块解析 diff(路径 + 起始行号),供变更文件列表与点击跳转锚点使用 */
const diffSegments = computed(() => parseDiffFileSegments(diffLines.value))

/** 变更文件清单(按出现顺序去重);工作区不可用时传给侧栏兜底展示 */
const changedFiles = computed(() => {
  const seen = new Set<string>()
  const files: string[] = []
  for (const seg of diffSegments.value) {
    if (!seen.has(seg.path)) {
      seen.add(seg.path)
      files.push(seg.path)
    }
  }
  return files
})

/** 仓库文件清单(repo_tree 快照,逐行路径);无变更文件时传给侧栏二级兜底 */
const repoFiles = computed(() =>
  (repoTreeArtifact.value?.content ?? '')
    .split('\n')
    .filter((p) => p.trim()),
)

/** diff 行号 → 锚点 id 映射(仅每个文件块起始行有锚点) */
const diffAnchorByLine = computed(() => {
  const m = new Map<number, string>()
  diffSegments.value.forEach((seg, i) => m.set(seg.lineIndex, `diff-file-${i}`))
  return m
})

/** 侧栏变更文件点击:展开"工作区变更"并滚动到对应文件的 diff 块 */
async function scrollToDiffFile(path: string): Promise<void> {
  const idx = diffSegments.value.findIndex((s) => s.path === path)
  if (idx < 0) return
  workspaceChangesCollapsed.value = false
  await nextTick()
  document
    .getElementById(`diff-file-${idx}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 拉取任务的工作区产物(git_diff + repo_tree);静默失败,不影响主流程 */
async function loadArtifact(taskId: string): Promise<void> {
  try {
    const res = await listArtifacts(taskId)
    workspaceArtifact.value =
      res.artifacts.find((a) => a.kind === 'git_diff') ?? null
    repoTreeArtifact.value =
      res.artifacts.find((a) => a.kind === 'repo_tree') ?? null
  } catch (err) {
    console.warn('加载工作区产物失败:', err)
    workspaceArtifact.value = null
    repoTreeArtifact.value = null
  }
}

// ---- 流式思考项(thinking_delta 累积)----
// key: conv_id, value: 流式思考项状态
interface StreamingItem {
  conv_id: string
  round_idx: number
  role: 'agent1' | 'agent2'
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  started_at: string
  finished_at?: string
  /** 思考链展开状态(判定见 utils/thinkingExpand):生命周期自动驱动 + 用户可钉住
   * - reasoning_auto:流式中已跨过自动展开阈值的闩锁(单调,不逐字符重算)
   * - reasoning_grace:结束后的收起宽限期,到期才折叠(不与正文出现同帧)
   * - reasoning_pin:用户手动点过的意图,优先于以上两条规则;null=未干预 */
  reasoning_auto?: boolean
  reasoning_grace?: boolean
  reasoning_pin?: boolean | null
  /** 全局递增序号(流式项到达顺序,用于调试) */
  seq: number
  /** 该流式 thinking 开始时,其所在 round 已收到的正式对话数(用于计算插入位置) */
  insertSeq: number
  /** 是否为动态验证的思考流(verifier_agent 产生,显示"正在验证"而非"正在思考") */
  verify?: boolean
}

const streamingItems = reactive<Map<string, StreamingItem>>(new Map())
/**
 * 历史回放思考项的展开状态(仅存用户手动意图)
 * key: conv_id(形如 history:${c.id});value: pin
 *
 * 历史思考项在 roundGroups computed 里每次重算都会新建 streamingItem 对象,
 * 状态无法持久,且其 conv_id 未注册进 streamingItems,故 toggleReasoning 找不到。
 * 这里用独立 Map 持久化用户意图,computed 读取它,toggle 时修改它触发重算。
 * 未点过的项不在 Map 里(=pin null),回放态是 done 所以按自动规则折叠。
 */
const historyReasoningPins = reactive<Map<string, boolean>>(new Map())
/** 思考卡收起宽限定时器(卸载时清掉,避免跨任务残留回调) */
const graceTimers = new Set<ReturnType<typeof setTimeout>>()
/** 全局序号计数器:流式项到达顺序 */
let streamingSeqCounter = 0
/** 每 round 已收到的正式对话数(用于给 streamingItem 计算插入位置 seq) */
const convCountPerRound = reactive<Map<number, number>>(new Map())

// ---- 计划清单(plan 事件 + 历史回放)----
// key: round_idx,value: 该 round 最新一次的 plan 步骤列表(覆盖式更新)
const planPerRound = reactive<Map<number, PlanStep[]>>(new Map())

// ---- 验证动作授权弹窗(verify_action 事件)----
// verifier_agent 在 per_action 模式下,每次执行 http_request / run_python_code 前
// 推送 verify_action 事件,前端弹出 VerifyActionDialog 让用户确认/拒绝。
// 对用户透明:不出现 verifier_agent 字样,只显示"验证动作需要授权"。
const verifyActionOpen = ref(false)
const verifyActionData = ref<VerifyActionEventData | null>(null)
const submittingVerifyAction = ref(false)

// ---- 危险命令确认弹窗(command_confirm 事件)----
// local 模式下,LLM 调 run_command 执行的危险命令(如 rm -rf /)会推送
// command_confirm 事件,前端弹出 CommandConfirmDialog 让用户确认/拒绝。
const commandConfirmData = ref<CommandConfirmEventData | null>(null)
const submittingCommandConfirm = ref(false)

// ---- 仓库克隆进度(clone_progress 事件)----
// local 模式下后端用 Popen 流式读 git clone 的 stderr,解析百分比后推送。
// 克隆完成(后端推 status 切换 current_stage)或任务结束时清除。
const cloneProgress = ref<CloneProgressEventData | null>(null)

// 跳过预克隆:克隆阶段用户不想等时,请求后端终止 clone 并降级为自主克隆
const skipClonePending = ref(false)

/** 是否处于预克隆阶段(协议回退间隙无进度事件时也显示跳过按钮) */
const isPreCloning = computed(
  () =>
    !!cloneProgress.value ||
    (task.value?.current_stage || '').includes('正在克隆仓库'),
)

async function handleSkipPreClone(): Promise<void> {
  if (!task.value?.id || skipClonePending.value) return
  skipClonePending.value = true
  try {
    await skipPreClone(String(task.value.id))
    // 阶段切换由 SSE status 事件驱动(后端降级后推新 stage),不本地改写
  } catch (err) {
    error.value = extractErrorMessage(err)
    skipClonePending.value = false
  }
}

/** 从 VerifyActionEventData 填充弹窗数据并打开 */
function openVerifyActionDialog(action: VerifyActionEventData): void {
  verifyActionData.value = action
  verifyActionOpen.value = true
}

/** 用户同意执行验证动作 */
async function handleApproveVerifyAction(actionId: string): Promise<void> {
  if (!task.value?.id || submittingVerifyAction.value) return
  submittingVerifyAction.value = true
  try {
    const resp = await submitVerifyAction(String(task.value.id), {
      action_id: actionId,
      approved: true,
    })
    if (resp.accepted) {
      verifyActionOpen.value = false
      verifyActionData.value = null
    } else {
      error.value = resp.message || '授权提交失败,任务可能已结束'
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    submittingVerifyAction.value = false
  }
}

/** 用户拒绝执行验证动作 */
async function handleRejectVerifyAction(actionId: string): Promise<void> {
  if (!task.value?.id || submittingVerifyAction.value) return
  submittingVerifyAction.value = true
  try {
    const resp = await submitVerifyAction(String(task.value.id), {
      action_id: actionId,
      approved: false,
    })
    if (resp.accepted) {
      verifyActionOpen.value = false
      verifyActionData.value = null
    } else {
      error.value = resp.message || '授权提交失败,任务可能已结束'
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    submittingVerifyAction.value = false
  }
}

/** 刷新页面后恢复待授权验证动作弹窗(若后端有 pending verify action) */
async function restorePendingVerifyAction(taskId: string): Promise<void> {
  try {
    const pending = await getPendingVerifyAction(taskId)
    if (pending && pending.action_id) {
      openVerifyActionDialog(pending)
    }
  } catch {
    // 无 pending verify action 或任务已结束,静默忽略
  }
}

/** 用户同意执行危险命令 */
async function handleApproveCommand(commandId: string) {
  if (!task.value || submittingCommandConfirm.value) return
  submittingCommandConfirm.value = true
  try {
    await submitCommandConfirm(task.value.id, { command_id: commandId, approved: true })
    commandConfirmData.value = null
  } catch (e) {
    console.error('同意命令确认失败:', e)
  } finally {
    submittingCommandConfirm.value = false
  }
}

/** 用户拒绝执行危险命令 */
async function handleRejectCommand(commandId: string) {
  if (!task.value || submittingCommandConfirm.value) return
  submittingCommandConfirm.value = true
  try {
    await submitCommandConfirm(task.value.id, { command_id: commandId, approved: false })
    commandConfirmData.value = null
  } catch (e) {
    console.error('拒绝命令确认失败:', e)
  } finally {
    submittingCommandConfirm.value = false
  }
}

/** 恢复危险命令确认弹窗(页面刷新后,若后端有 pending command confirm) */
async function restorePendingCommandConfirm(taskId: string): Promise<void> {
  try {
    const pendingCmd = await getPendingCommandConfirm(taskId)
    if (pendingCmd) {
      commandConfirmData.value = pendingCmd
    }
  } catch (e) {
    console.error('获取待确认命令失败:', e)
  }
}

// ---- 运行时验证配置切换(任务运行界面调整授权模式) ----
const verifierConfigSaving = ref(false)

/** 切换验证授权模式(direct ↔ per_action),立即保存到后端 */
async function toggleVerifierAuthMode(): Promise<void> {
  if (!task.value?.id || verifierConfigSaving.value) return
  const newMode = task.value.verifier_auth_mode === 'direct' ? 'per_action' : 'direct'
  verifierConfigSaving.value = true
  try {
    const updated = await updateTaskVerifierConfig(String(task.value.id), {
      verifier_auth_mode: newMode,
    })
    task.value = updated
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    verifierConfigSaving.value = false
  }
}

/** 切换验证开关,立即保存到后端 */
async function toggleVerifierEnabled(): Promise<void> {
  if (!task.value?.id || verifierConfigSaving.value) return
  const newEnabled = !task.value.verifier_enabled
  verifierConfigSaving.value = true
  try {
    const updated = await updateTaskVerifierConfig(String(task.value.id), {
      verifier_enabled: newEnabled,
    })
    task.value = updated
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    verifierConfigSaving.value = false
  }
}

/** 任务是否启用了动态验证 */
const verifierActive = computed(
  () => !!task.value?.verifier_enabled && !!task.value?.test_env_url,
)

/** 脱敏展示 token 值(只显示前 8 + 后 4 字符,中间用 *** 代替) */
function maskTokenValue(value: string): string {
  if (!value) return ''
  if (value.length <= 12) return '***'
  return value.slice(0, 8) + '***' + value.slice(-4)
}

// ---- 加载 + SSE 订阅 ----

async function initTask(): Promise<void> {
  const taskId = route.params.id as string
  // 切换/重载任务:清空上一任务的实时审查计划
  reviewPlanLive.value = null
  expandedReviewItems.value = new Set()
  try {
    // 拉任务快照(场景降级后不再需要单独拉 /scenarios:结果分组/meta
    // 从 task.params._grouping 和 results 的 metadata keys 推断)
    const taskData = await getTask(taskId)
    task.value = taskData
    error.value = ''
    loading.value = false
    // 初始加载完成:强制置底并重置跟随开关(语义上的"应跳转"时机)
    forceScrollToBottom()
    // [诊断] 任务快照拉取:记录后端返回的状态(与前端显示对拍)
    clientLog(taskId, 'task_fetch', {
      status: taskData.status,
      error_message: taskData.error_message,
      current_stage: taskData.current_stage,
    })

    // 从历史对话提取 plan(刷新页面/迟到订阅者回放)
    // agent1 的 type=thinking content 里可能含 <plan>...</plan>,
    // 每个 round 取最后一次出现的 plan(可能被后续思考更新过状态)
    extractPlanFromHistory(taskData.conversations)

    // 恢复 convCountPerRound(按 round 统计历史对话数,含 thinking,跳过 user question)
    // 必须和 roundGroups 里 localIdx 的基准一致:localIdx 跳过 user question,
    // 这里也跳过,否则刷新后新 thinking 的 insertSeq 偏大,seq 排到 tool_call 之前
    convCountPerRound.clear()
    for (const c of taskData.conversations) {
      if (c.role === 'user' && c.type === 'question') continue
      convCountPerRound.set(
        c.round_idx,
        (convCountPerRound.get(c.round_idx) ?? 0) + 1,
      )
    }

    // 2. 若任务仍在进行(含暂停态),连接 SSE 接收实时事件
    //    双 agent 模式:任务 COMPLETED 但后台审查未结束(review_status=running)
    //    时事件总线仍打开,刷新页面后同样需要重连接收审查事件
    if (
      task.value &&
      (task.value.status === 'pending' ||
        task.value.status === 'running' ||
        task.value.status === 'paused' ||
        task.value.review_status === 'running')
    ) {
      connectSSE(taskId)
      // 恢复可能存在的待授权验证动作弹窗(per_action 模式刷新页面后)
      void restorePendingVerifyAction(taskId)
      // 恢复可能存在的待确认危险命令弹窗(local 模式刷新页面后)
      void restorePendingCommandConfirm(taskId)
    }

    // 3. 加载工作区变更(任务完成时捕获的 diff;进行中任务此时为空,完成时由 SSE done 触发重拉)
    void loadArtifact(taskId)
  } catch (err) {
    error.value = extractErrorMessage(err)
    loading.value = false
  }
}

// ---- 报告导出(Markdown 下载 / PDF 打印) ----

const exporting = ref(false)

/** 导出 Markdown:下载 .md 文件 */
async function exportMarkdown(): Promise<void> {
  if (!task.value?.id || exporting.value) return
  exporting.value = true
  try {
    const blob = await downloadTaskReportMarkdown(String(task.value.id))
    triggerBlobDownload(blob, `task-${task.value.id}.md`)
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    exporting.value = false
  }
}

/** 导出 PDF:获取 HTML 报告,新窗口渲染并调起打印 */
async function exportPdf(): Promise<void> {
  if (!task.value?.id || exporting.value) return
  exporting.value = true
  try {
    const html = await getTaskReportHtml(String(task.value.id))
    const w = window.open('', '_blank')
    if (!w) {
      error.value = '无法打开新窗口,请检查浏览器弹窗拦截设置'
      return
    }
    w.document.write(html)
    w.document.close()
    w.focus()
    // 等待渲染后调起打印对话框(用户可选"另存为 PDF")
    setTimeout(() => w.print(), 400)
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    exporting.value = false
  }
}

/**
 * 用最新快照替换本地任务并重建派生状态(plan 提取 + 轮对话计数)
 *
 * - clearStreaming=true:任务全部结束,清空所有流式卡片(历史对话接管)
 * - clearStreaming=false:agent1_done 时调用,只清 agent1 流式卡片,
 *   保留 agent2 的(后台审查的实时思考继续由流式卡片展示)
 */
function applyTaskSnapshot(fresh: TaskDetail, clearStreaming: boolean): void {
  task.value = fresh
  // 重新提取 plan(快照可能含最新一轮的 plan 更新)
  extractPlanFromHistory(fresh.conversations)
  // 恢复 convCountPerRound(与 roundGroups 里 localIdx 的基准一致:跳过 user question)
  convCountPerRound.clear()
  for (const c of fresh.conversations) {
    if (c.role === 'user' && c.type === 'question') continue
    convCountPerRound.set(c.round_idx, (convCountPerRound.get(c.round_idx) ?? 0) + 1)
  }
  if (clearStreaming) {
    streamingItems.clear()
  } else {
    for (const [key, s] of streamingItems) {
      if (s.role === 'agent1') streamingItems.delete(key)
    }
  }
}

function connectSSE(taskId: string): void {
  // 关闭旧连接
  if (eventSource) eventSource.close()

  eventSource = subscribeTaskStream(taskId, {
    onConnected: (data) => {
      // 更新状态(可能任务已结束)
      if (task.value) {
        task.value.status = data.status
        task.value.current_stage = data.current_stage
      }
    },
    onUserMessagePending: (data) => {
      if (!task.value) return
      // 刷新场景:快照里该消息已在对话流中(落库即入快照),移出改为待处理条目
      const idx = task.value.conversations.findIndex((c) => c.id === data.id)
      if (idx !== -1) task.value.conversations.splice(idx, 1)
      // 去重加入(SSE 历史补播 + 实时事件可能重复到达)
      if (!pendingUserMessages.value.some((m) => m.id === data.id)) {
        pendingUserMessages.value.push({
          id: data.id,
          round_idx: data.round_idx,
          role: data.role,
          type: data.type,
          content: data.content,
          reasoning: data.reasoning ?? null,
          tool_call_id: null,
          attachments: data.attachments ?? null,
          created_at: data.created_at || new Date().toISOString(),
        })
      }
    },
    onUserMessageWithdrawn: (data) => {
      // 待处理消息被撤回(本端或多端):移除条目(记录已由后端删除)
      pendingUserMessages.value = pendingUserMessages.value.filter(
        (m) => m.id !== data.id,
      )
    },
    onConversation: (data) => {
      if (!task.value) return
      // 用户补充消息被 agent1 消费(drain 注入):待处理条目转入对话流
      if (data.role === 'user' && data.type === 'message') {
        pendingUserMessages.value = pendingUserMessages.value.filter(
          (m) => m.id !== data.id,
        )
      }
      // 去重:刷新/中途离开详情页再回来时,快照已含全部落库记录,
      // 而事件总线的历史补播会把同一条 conversation 事件再推一遍
      // (重复 id 直接忽略,否则对话流里出现两条一模一样的消息)
      if (task.value.conversations.some((c) => c.id === data.id)) return
      // 思考落库:退役与之对应的实时流式卡片(否则同一段思考会以
      // "实时卡片 + 只读历史卡片"两份出现在对话流里)。
      // 内置 react_agent 与 CLI 执行器带 stream_conv_id 精确匹配;
      // agent2 审查/动态验证拿不到该 id,按 reasoning/content 文本对账。
      if (data.type === 'thinking') {
        const retired = findRetiredCardConvId(streamingItems.values(), data)
        if (retired) {
          // 交接:把实时卡上用户钉住的展开意图带到只读历史卡(键形如 history:${id}),
          // 否则"我点开想盯着看的思考"会在落库那一刻被无声收回去
          const card = streamingItems.get(retired)
          if (card && typeof card.reasoning_pin === 'boolean') {
            historyReasoningPins.set(`history:${data.id}`, card.reasoning_pin)
          }
          streamingItems.delete(retired)
        }
      }
      // 追加到对话列表
      // 注意:type=thinking 的落库记录也会走这里 —— 它是"中途离开页面再回来"
      // 时那段思考的唯一实时来源(thinking_delta 增量是瞬时的,总线不缓存,
      // 断线期间的增量永远收不到),不推就得等下一次整页快照才显示。
      const conv: Conversation = {
        id: data.id,
        round_idx: data.round_idx,
        role: data.role,
        type: data.type,
        content: data.content,
        reasoning: data.reasoning ?? null,
        tool_call_id: data.tool_call_id ?? null,
        attachments: data.attachments ?? null,
        created_at: data.created_at || new Date().toISOString(),
      }
      task.value.conversations.push(conv)
      // 维护该 round 的正式对话计数(供 streamingItem 计算插入位置 seq)
      // 必须与 roundGroups 里 localIdx 的基准一致:localIdx 跳过 user question
      // (user question 单独提到顶部 userDirective 渲染),这里也跳过,
      // 否则每 round 多算 1,流式 thinking 的 insertSeq 偏大,seq 排到 tool_call 之后,
      // 导致 thinking 不再是迭代起点,首个 tool_call 被甩进 plains(界面最底部)。
      if (!(data.role === 'user' && data.type === 'question')) {
        convCountPerRound.set(
          data.round_idx,
          (convCountPerRound.get(data.round_idx) ?? 0) + 1,
        )
      }
      // 自动滚动到底部
      nextTick(scrollToBottom)
    },
    onConversationUpdate: (data) => {
      if (!task.value) return
      // 更新已有对话项的 content(如 Kimi 增量参数补全后刷新 tool_call 显示)
      const conv = task.value.conversations.find((c) => c.id === data.id)
      if (conv) {
        conv.content = data.content
      }
    },
    onStatus: (data) => {
      if (task.value) {
        task.value.status = data.status
        task.value.current_stage = data.current_stage
      }
      // 阶段切换(如"正在读取仓库根目录结构...")意味着克隆已完成,清除进度条
      cloneProgress.value = null
      skipClonePending.value = false
    },
    onThinkingDelta: (data) => {
      handleThinkingDelta(data)
      nextTick(scrollToBottom)
    },
    onCloneProgress: (data) => {
      cloneProgress.value = data
      nextTick(scrollToBottom)
    },
    onPlan: (data) => {
      // 覆盖式更新:每个 round 只保留最新一次 plan
      planPerRound.set(data.round_idx, data.steps)
      nextTick(scrollToBottom)
    },
    onVerifyAction: (data: VerifyActionEventData) => {
      // 动态验证动作需要授权(per_action 模式):弹出 VerifyActionDialog
      openVerifyActionDialog(data)
    },
    onCommandConfirm: (data) => {
      commandConfirmData.value = data
    },
    onAgent1Done: async () => {
      // agent1 结束即任务完成:本地置 completed 并拉快照展示临时结果
      // (后端已落 1 条"检查助手整理中"临时结果);事件总线保持打开,
      // 后台审查的 conversation/thinking_delta 事件继续送达侧栏
      if (task.value) {
        task.value.status = 'completed'
        task.value.current_stage = '检查助手后台核查中'
      }
      try {
        const fresh = await getTask(taskId)
        if (fresh && !unmountedFlag) {
          // 只清 agent1 流式卡片(其思考已落库);agent2 审查流式尚未开始,
          // 保留 Map 不影响后续 thinking_delta 继续累积
          applyTaskSnapshot(fresh, false)
        }
      } catch {
        // 快照拉取失败:保持本地状态,onReviewDone/onDone 会再拉
      }
    },
    onReviewPlanUpdate: (data: ReviewPlanUpdateEventData) => {
      // 审查计划发射/修订:覆盖式整体替换(重发=全量修订,不重复累积)
      reviewPlanLive.value = data
    },
    onReviewItemAdd: (data: ReviewItemEventData) => {
      // 审查项即时落库:按 id 去重 push;review_done 拉快照权威兜底
      if (!task.value) return
      const arr = task.value.review_items ?? []
      if (!arr.some((i) => i.id === data.id)) {
        task.value.review_items = [...arr, data as unknown as ReviewItem]
      }
    },
    onKnowledgePointAdd: (data: KnowledgePointEventData) => {
      // 知识点即时落库:按 id 去重 push 进 results(知识点区)
      if (!task.value) return
      const arr = task.value.results ?? []
      if (!arr.some((r) => r.id === data.id)) {
        task.value.results = [...arr, data]
      }
    },
    onReviewDone: async (data) => {
      // 后台审查结束:done=重点与知识点已替换临时结果 / failed=审查失败
      // 拉快照同步最终 results 与 suggestions(终止 done 事件随后到达)
      if (task.value) {
        task.value.review_status = data.review_status
      }
      try {
        const fresh = await getTask(taskId)
        if (fresh && !unmountedFlag) {
          applyTaskSnapshot(fresh, false)
          // 审查已结束:agent2 的思考/工具步骤已落库并随快照返回,
          // 清掉 agent2 流式项,避免与历史 thinking 记录在侧栏重复显示
          for (const [key, s] of streamingItems) {
            if (s.role === 'agent2') streamingItems.delete(key)
          }
        }
      } catch {
        // 快照拉取失败:onDone 兜底再拉一次
      }
    },
    onDone: async () => {
      // [诊断] done 事件处理:记录是否走了 resume 竞态校验分支
      clientLog(taskId, 'view_on_done', { resuming: resumingRef.value })
      // 竞态防御:completed 追问 / failed 重试后立即重连的 SSE,可能被后端
      // 按旧快照(COMPLETED/FAILED)误推 done 关闭(后端已同步改 RUNNING +
      // 事件总线双重防御,这里作前端兜底)。重新校验任务状态,若实际仍在
      // 运行则重连 SSE 继续接收新一轮事件,不执行任务结束清理。
      if (resumingRef.value) {
        resumingRef.value = false
        try {
          const fresh = await getTask(taskId)
          if (
            fresh &&
            (fresh.status === 'running' ||
              fresh.status === 'paused' ||
              fresh.status === 'pending')
          ) {
            if (unmountedFlag) return // 组件已卸载,不再重连(防止连接泄漏)
            task.value = fresh
            connectSSE(taskId)
            return
          }
        } catch {
          // 拉取失败走正常 done 流程(下方还会再拉一次)
        }
      }
      // 任务完成:拉取最终结果(含 results)
      try {
        const fresh = await getTask(taskId)
        if (fresh) {
          // 全部结束:清空流式卡片(历史对话接管显示)
          applyTaskSnapshot(fresh, true)
        }
      } catch (err) {
        console.error('拉取最终结果失败:', err)
      }
      // 清除克隆进度条(任务结束)+ 待处理条目(队列已被后端清理)
      cloneProgress.value = null
      skipClonePending.value = false
      pendingUserMessages.value = []
      // 任务完成时后端刚写入工作区 diff,重拉一次展示(失败兜底,静默)
      void loadArtifact(taskId)
    },
    onError: async (data) => {
      // [诊断] 前端显示"失败"的唯一入口:记录触发时本地状态,
      // 与后端 client.log / 事件总线日志对拍定位"未知失败"
      clientLog(taskId, 'view_on_error', {
        local_status: task.value?.status,
        error_message: data.error_message,
        resuming: resumingRef.value,
      })
      // 退出 resume 窗口(任务真实失败,不再需要竞态校验)
      resumingRef.value = false
      if (task.value) {
        task.value.status = 'failed'
        task.value.error_message = data.error_message || '执行失败'
      }
      // 清除克隆进度条(任务失败)+ 待处理条目(队列已被后端清理)
      cloneProgress.value = null
      skipClonePending.value = false
      pendingUserMessages.value = []
    },
  })
}

// ---- 流式增量处理 ----

function handleThinkingDelta(data: ThinkingDeltaEventData): void {
  const { conv_id, round_idx, role, phase, delta, verify } = data

  if (phase === 'start') {
    // 创建新的流式项:思考链按生命周期自动展开(过阈值后),完成后折叠
    // 记录该 round 当前已收到的正式对话数,用于后续 seq 计算(让 thinking 排在
    // 它之后的 tool_call 之前,而非所有 thinking 都挤在最前面)
    const insertSeq = convCountPerRound.get(round_idx) ?? 0
    streamingItems.set(conv_id, {
      conv_id,
      round_idx,
      role,
      reasoning: '',
      content: '',
      status: 'streaming',
      started_at: new Date().toISOString(),
      reasoning_auto: false,
      reasoning_grace: false,
      reasoning_pin: null,
      seq: streamingSeqCounter++,
      insertSeq,
      verify,
    })
    return
  }

  const item = streamingItems.get(conv_id)
  if (!item) {
    // 没收到 start 事件就来了 delta,创建一个
    const insertSeq = convCountPerRound.get(round_idx) ?? 0
    streamingItems.set(conv_id, {
      conv_id,
      round_idx,
      role,
      reasoning: '',
      content: '',
      status: 'streaming',
      started_at: new Date().toISOString(),
      reasoning_auto: false,
      reasoning_grace: false,
      reasoning_pin: null,
      seq: streamingSeqCounter++,
      insertSeq,
      verify,
    })
  }

  const cur = streamingItems.get(conv_id)!
  if (phase === 'reasoning') {
    cur.reasoning += delta
    // 跨过阈值才置自动展开闩锁(单调):短思考保持单行标题,
    // 避免一次任务里几十个迭代各自"闪一下收起"
    if (!cur.reasoning_auto && shouldLatchAutoExpand(cur)) cur.reasoning_auto = true
  } else if (phase === 'content') {
    cur.content += delta
  } else if (phase === 'error') {
    // 错误文本仍归入思考卡(整项失败的完整提示由任务级 error_message 承担)
    cur.reasoning += `\n[错误] ${delta}`
    finishThinking(cur, conv_id, 'error')
  } else if (phase === 'end') {
    finishThinking(cur, conv_id, 'done')
  }
}

/**
 * 流式收尾:标记完成,展开态进入宽限期后自动折叠
 *
 * 不移除卡片:它是本次会话内唯一的实时展示载体;
 * 落库后收到同一段的 conversation 事件时退役(见 onConversation),
 * 中途离开页面再回来则由快照还原为只读卡片。
 *
 * 宽限期只给"曾经自动展开过"的卡片:让收起发生在正文渲染之后一拍,
 * 而不是与正文出现抢同一帧把用户视线拽走。用户 pin 过的不受影响
 * (isThinkingExpanded 里 pin 优先)。
 */
function finishThinking(
  cur: StreamingItem,
  convId: string,
  status: 'done' | 'error',
): void {
  cur.status = status
  cur.finished_at = new Date().toISOString()
  cur.reasoning_grace = cur.reasoning_auto === true
  cur.reasoning_auto = false
  if (!cur.reasoning_grace) return
  const timer = setTimeout(() => {
    graceTimers.delete(timer)
    // 卡片可能已随落库退役(或被整页快照清空),找不到就不必再改
    const it = streamingItems.get(convId)
    if (it) it.reasoning_grace = false
  }, COLLAPSE_GRACE_MS)
  graceTimers.add(timer)
}

/** 清掉思考卡收起宽限定时器(卸载/切换任务时不留残余回调) */
function clearGraceTimers(): void {
  for (const t of graceTimers) clearTimeout(t)
  graceTimers.clear()
}

/** 切换思考卡展开/折叠:写成用户意图(pin),覆盖生命周期自动规则 */
function toggleReasoning(convId: string): void {
  // 实时流式项:状态存在 streamingItems 里
  const item = streamingItems.get(convId)
  if (item) {
    item.reasoning_pin = !isThinkingExpanded(item)
    return
  }
  // 历史回放项:conv_id 形如 history:xxx,未注册进 streamingItems,
  // 用独立 Map 持久化用户意图(修改后触发 roundGroups computed 重算)
  historyReasoningPins.set(convId, !(historyReasoningPins.get(convId) ?? false))
}

// ---- plan 提取工具(与后端 _extract_plan 逻辑一致:优先 JSON 格式,回退逐行格式)----

const PLAN_BLOCK_RE = /<plan>\s*([\s\S]*?)\s*<\/plan>/
const PLAN_LINE_RE = /^\s*(?:\d+[.、)]\s*)?(?:\[([\w_]+)\]\s*)?(.+)$/
/** 含文字字符(字母/数字/下划线/中文)才算有效步骤行,纯符号行("["、"]")跳过 */
const PLAN_LINE_HAS_TEXT_RE = /[\w\u4e00-\u9fff]/

/** 尝试把 plan 块按 JSON 解析(对象数组,或逐行多个对象),失败返回 null
 *
 * system prompt 示范的是 JSON 数组格式,模型照做时逐行解析会把整行 JSON
 * 当成步骤文本;这里优先按 JSON 解析。容错:无包裹数组时补 [ ],
 * 原生 JSON.parse 失败后用 jsonrepair 修复(尾逗号/截断/缺引号等,
 * 与后端 json_repair 对齐)。
 */
function parsePlanJson(block: string): PlanStep[] | null {
  const trimmed = block.trim()
  if (!trimmed || (trimmed[0] !== '[' && trimmed[0] !== '{')) return null
  const candidate = trimmed[0] === '[' ? trimmed : `[${trimmed}]`
  let parsed: unknown = null
  try {
    parsed = JSON.parse(candidate)
  } catch {
    try {
      parsed = JSON.parse(jsonrepair(candidate))
    } catch {
      return null
    }
  }
  if (!Array.isArray(parsed)) return null
  const steps: PlanStep[] = []
  for (const e of parsed) {
    if (!e || typeof e !== 'object') continue
    const obj = e as Record<string, unknown>
    const text = String(obj.text ?? obj.content ?? '').trim()
    if (!text) continue
    let status = String(obj.status ?? 'pending').trim() as PlanStep['status']
    if (status !== 'pending' && status !== 'in_progress' && status !== 'done') {
      status = 'pending'
    }
    steps.push({ id: steps.length + 1, text, status })
  }
  return steps.length > 0 ? steps : null
}

/** 从单段 content 提取 plan 步骤列表,无 plan 块返回 null */
function parsePlanFromContent(content: string): PlanStep[] | null {
  const m = content.match(PLAN_BLOCK_RE)
  if (!m) return null
  const block = m[1]
  // 优先 JSON 解析(模型按 system prompt 示范输出 JSON 数组)
  const jsonSteps = parsePlanJson(block)
  if (jsonSteps) return jsonSteps
  const steps: PlanStep[] = []
  let id = 0
  for (const line of block.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    if (!PLAN_LINE_HAS_TEXT_RE.test(trimmed)) continue
    const lm = trimmed.match(PLAN_LINE_RE)
    if (!lm) continue
    id += 1
    let status: PlanStep['status'] = (lm[1] as PlanStep['status']) || 'pending'
    if (status !== 'pending' && status !== 'in_progress' && status !== 'done') {
      status = 'pending'
    }
    steps.push({ id, text: lm[2].trim(), status })
  }
  return steps.length > 0 ? steps : null
}

/** 从 kimi code CLI 的 TodoList tool_call 提取计划清单
 *
 * 落库 content 格式为 intent 首行(`调用 TodoList [TodoList]`)+ 完整入参 JSON
 * ({todos: [{title, status}]}),jsonrepair 容错解析。
 * 查询/清空模式(无 todos/空数组)返回 null,保持最后已知计划。
 */
function parseTodoListToolCall(content: string): PlanStep[] | null {
  const nl = content.indexOf('\n')
  if (nl < 0) return null
  if (!content.slice(0, nl).trimEnd().endsWith('[TodoList]')) return null
  const detail = content.slice(nl + 1).trim()
  if (!detail.startsWith('{') && !detail.startsWith('[')) return null
  let parsed: unknown = null
  try {
    parsed = JSON.parse(detail)
  } catch {
    try {
      parsed = JSON.parse(jsonrepair(detail))
    } catch {
      return null
    }
  }
  const todos =
    parsed && typeof parsed === 'object'
      ? (parsed as Record<string, unknown>).todos
      : null
  if (!Array.isArray(todos) || todos.length === 0) return null
  const steps: PlanStep[] = []
  for (const t of todos) {
    if (!t || typeof t !== 'object') continue
    const obj = t as Record<string, unknown>
    const text = String(obj.title ?? '').trim()
    if (!text) continue
    let statusStr = String(obj.status ?? 'pending').trim()
    if (statusStr === 'completed') statusStr = 'done'
    const status: PlanStep['status'] =
      statusStr === 'pending' || statusStr === 'in_progress' || statusStr === 'done'
        ? statusStr
        : 'pending'
    steps.push({ id: steps.length + 1, text, status })
  }
  return steps.length > 0 ? steps : null
}

/** 从历史对话提取 plan,每个 round 取最后一次出现的 plan(可能被更新过状态)
 *
 * 两个来源:
 * - 内置 agent1:thinking content 里的 <plan> 块
 * - kimi code CLI 等外部执行器:TodoList tool_call 落库的入参 JSON
 */
function extractPlanFromHistory(conversations: Conversation[]): void {
  // 按 round 收集所有含 plan 的记录,保留每个 round 最后一次
  const lastPlanPerRound = new Map<number, PlanStep[]>()
  for (const c of conversations) {
    if (c.role !== 'agent1' || !c.content) continue
    let steps: PlanStep[] | null = null
    if (c.type === 'thinking') {
      steps = parsePlanFromContent(c.content)
    } else if (c.type === 'tool_call') {
      steps = parseTodoListToolCall(c.content)
    }
    if (steps) {
      lastPlanPerRound.set(c.round_idx, steps)
    }
  }
  for (const [roundIdx, steps] of lastPlanPerRound) {
    planPerRound.set(roundIdx, steps)
  }
}

/** 对话流滚动监听:用户离开底部 → 暂停跟随;滚回底部附近 → 恢复跟随。
 * 程序化置底总是恰好停在底部,proximity 判断天然不会误判。 */
function handleConversationScroll(): void {
  const el = conversationRef.value
  if (!el) return
  followBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 12
}

/** 仅在跟随状态下置底(SSE 增量事件调用;用户上滚回看时不打扰) */
function scrollToBottom(): void {
  if (!followBottom.value) return
  if (conversationRef.value) {
    conversationRef.value.scrollTop = conversationRef.value.scrollHeight
  }
}

/** 强制置底并重置跟随开关(初始加载/发送消息/重试等语义上的"应跳转"时机) */
function forceScrollToBottom(): void {
  followBottom.value = true
  nextTick(() => {
    if (conversationRef.value) {
      conversationRef.value.scrollTop = conversationRef.value.scrollHeight
    }
  })
}

onMounted(initTask)
onUnmounted(() => {
  unmountedFlag = true
  if (eventSource) eventSource.close()
  clearGraceTimers()
})

/**
 * 切换任务时清理旧任务状态(组件复用,route.params.id 变化)
 */
function resetTaskState(): void {
  // 断开旧 SSE,避免向旧任务写数据
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  // 清空流式/计划/对话计数等运行态
  streamingItems.clear()
  planPerRound.clear()
  convCountPerRound.clear()
  historyReasoningPins.clear()
  clearGraceTimers()
  // 重置 resume 窗口标志(防止跨任务误触发 onDone 校验)
  resumingRef.value = false
  // 清空待处理消息条目(旧任务的)
  pendingUserMessages.value = []
  // 重置任务视图态
  task.value = null
  loading.value = true
  error.value = ''
}

// 同一组件复用下,route.params.id 变化时重新加载任务
watch(
  () => route.params.id,
  (newId, oldId) => {
    if (!newId || newId === oldId) return
    resetTaskState()
    void initTask()
  },
)

// ---- 对话流分组:按 round_idx → 按 plan step 分组迭代 → 再按迭代分段 ----
//
// 层级结构:
//   round
//     ├─ plain segment     (agent2 评估/追问/总结、user 指令等关键节点,平铺)
//     ├─ step group        (plan step,文字=step.text,内含多个迭代;无 plan 时回退为单个"执行过程"折叠组)
//     │    └─ iteration segment (agent1 一次 ReAct 循环:thinking + N 个工具调用/结果)
//     └─ conclusion segment (该轮 agent1 最终回答,轮闭合后提出组外,纯正文消息;
//                            最终思考 reasoning 留在 step 组内)
//
// 迭代识别:遇到 agent1 的 thinking 项(实时流式或历史 type=thinking)就开新迭代,
// 后续 agent1 的 tool_call/tool_result/submit 归入当前迭代,
// 直到遇到下一个 thinking(开新迭代)或非 agent1 消息(关闭迭代,平铺该消息)。
// 缺失 thinking 锚点时开无 thinking 的兜底迭代承接工具项,不退化为 plain 段。
//
// step 归属推断:用迭代内首个工具调用的工具名匹配 plan step 关键词
// (复用后端 _TOOL_STEP_KEYWORDS 映射,与 plan 状态推进逻辑一致)
//
// 折叠策略(过程整体收起,结论直接可见;运行中自动展开,轮结束/任务完成自动收起):
// - 无 plan:所有迭代进单个"执行过程"折叠组(结论已提出组外,组内是纯过程噪音);
// - 有 plan:活跃轮只展开"前沿组"(最新迭代所属那个),轮闭合后各组默认收起,
//   无法归属的迭代进"执行过程"兜底折叠组;
// - 结论段:该轮最终回答不折叠,像正常消息一样直接可见(无特殊标签,ChatGPT 式);
// - 工具行:默认折叠(compact 单行 / agent、toolpair 卡片,按 tool_call id 记录展开)。

interface DisplayItem {
  /** 正式对话用 UUID,流式项用 `stream:${conv_id}` */
  id: string
  round_idx: number
  created_at: string
  /** 是否流式思考项 */
  is_streaming: boolean
  /** 稳定排序序号:正式对话用数组下标,流式项用全局计数器(避免跨来源 created_at 时钟漂移) */
  seq: number
  /** 正式对话字段 */
  role?: string
  type?: string
  content?: string
  /** 完整评估/思考链(如 agent2 evaluation),可折叠回看 */
  reasoning?: string | null
  /** 仅 type=tool_result 有:对应 tool_call 会话记录的 id(并行调用时精确配对) */
  tool_call_id?: string | null
  /** 仅 user 追问消息有:附带上传文件展示信息(只读渲染 chip) */
  attachments?: AttachmentInfo[] | null
  /** 流式项字段 */
  streaming?: StreamingItem
}

/** 平铺段:agent2/user 等关键消息,直接渲染为单张卡片 */
interface PlainSegment {
  kind: 'plain'
  item: DisplayItem
  /** 平铺消息在该轮内的原始位置:位于第几个迭代之后(0 = 轮首,首个迭代之前;轮内无迭代时恒为 0) */
  afterIterationIdx: number
}

/** 迭代段:agent1 一次 ReAct 循环的所有产物 */
interface IterationSegment {
  kind: 'iteration'
  /** 迭代在 round 内的序号(从 1 开始) */
  iterationIdx: number
  /** 唯一标识:`${roundIdx}-${iterationIdx}` */
  id: string
  /** 该迭代的 thinking 项(流式或历史,通常 1 条;锚点缺失的兜底迭代可为空) */
  thinkingItems: DisplayItem[]
  /** 该迭代内的工具调用项(tool_call + tool_result),按时间顺序 */
  toolItems: DisplayItem[]
  /** 该迭代内的其他 agent1 项(submit 等) */
  otherItems: DisplayItem[]
  /** 是否包含正在流式中的项(自动展开用) */
  hasStreaming: boolean
}

/** plan step 分组:把归属同一 step 的迭代合并 */
interface StepGroup {
  kind: 'step'
  /** step 唯一标识:`${roundIdx}-step-${stepId}` 或 `${roundIdx}-nostep` */
  id: string
  /** step 文字(无 plan 时为"执行过程") */
  text: string
  /** step 状态(无 plan 时为 in_progress) */
  status: PlanStep['status'] | 'none'
  /** 该 step 下的迭代列表 */
  iterations: IterationSegment[]
  /** 是否含流式中(任一迭代流式则为 true) */
  hasStreaming: boolean
  /** 活跃轮的前沿组:该轮运行期间整段保持展开(工具执行的空档也不收起) */
  live: boolean
  /** 该 step 内的平铺消息(如用户追问/回答,位于组内迭代边界;含此消息的组默认展开) */
  plains: PlainSegment[]
}

/** 结论段:该轮 agent1 的最终回答(最后一个纯思考迭代),轮闭合后提出组外,
 *  渲染在所有 step 组之后、修正指令卡之前,纯正文消息(无标签无思考卡,
 *  最终思考 reasoning 留在过程组内) */
interface ConclusionSegment {
  kind: 'conclusion'
  item: DisplayItem
}

type RoundSegment = PlainSegment | StepGroup | ConclusionSegment

interface RoundGroup {
  roundIdx: number
  segments: RoundSegment[]
  /** 该 round 的计划清单(复杂任务时 agent1 输出,空数组表示无 plan) */
  planSteps: PlanStep[]
}

/** 用户手动展开过的 step 组 id */
const expandedSteps = reactive<Set<string>>(new Set())
/** 用户手动收起过的 step 组 id(优先级最高,覆盖“最后一组默认展开”) */
const collapsedSteps = reactive<Set<string>>(new Set())
/** 用户展开过的工具行(紧凑行轻量展开 / 子智能体卡片 / 普通工具卡片),存 tool_call id,
 * 内部思考小卡用 `${callId}-think` 复合 key */
const expandedToolRows = reactive<Set<string>>(new Set())

/** 判断 DisplayItem 是否为 agent1 的 thinking(迭代起点) */
function isReactThinkingItem(item: DisplayItem): boolean {
  if (item.is_streaming) {
    return item.streaming?.role === 'agent1'
  }
  return item.role === 'agent1' && item.type === 'thinking'
}

/** 判断 DisplayItem 是否属于 agent1(用于归入当前迭代) */
function isReactAgentItem(item: DisplayItem): boolean {
  if (item.is_streaming) return item.streaming?.role === 'agent1'
  return item.role === 'agent1'
}

/** 判断 DisplayItem 是否正在流式 */
function isStreamingActive(item: DisplayItem): boolean {
  return !!(item.is_streaming && item.streaming?.status === 'streaming')
}

/** 工具名 → plan step 关键词映射(与后端 _TOOL_STEP_KEYWORDS 保持一致) */
const TOOL_STEP_KEYWORDS: Record<string, string[]> = {
  clone_repo:     ['克隆', 'clone', '仓库'],
  list_files:     ['结构', '目录', '查看', 'list'],
  read_file:      ['读取', '依赖', '清单', 'read'],
  query_cve:      ['依赖', 'cve', '漏洞'],
  list_dependencies: ['依赖', '清单', 'dependency', '锁文件', 'lockfile'],
  search_code:    ['注入', '密钥', '反序列化', 'ssrf', '路径', '认证', '授权',
                    '审计', '代码审计', 'search'],
  run_semgrep:    ['semgrep', 'sast', '静态分析'],
  run_lint:       ['lint', '风格', '规范', '静态检查', 'ruff', 'eslint'],
  run_coverage:   ['覆盖率', '覆盖', '测试', 'coverage'],
  git_diff:       ['diff', '变更', '增量', '改动', '对比'],
  list_skills:    ['skill', '技能'],
  skill:          ['skill', '技能'],
  submit_results: ['提交', '汇总', 'submit'],
}

/** 从工具调用 content 提取工具名 */
function extractToolNameFromContent(content: string): string {
  // 新格式(向 qoder CLI 看齐):"人类可读意图 [tool_name]\n{参数JSON}"
  // 匹配首行末尾的 [tool_name] 标签(首行是 intent,后续行是参数 JSON)
  const firstLine = content.split('\n', 1)[0]
  const m = firstLine.match(/\[(\w+)\]$/)
  return m ? m[1] : ''
}

/** 推断迭代归属哪个 plan step,返回 step id(无匹配返回 null) */
function inferStepFromIteration(
  iter: IterationSegment,
  planSteps: PlanStep[],
): number | null {
  if (!planSteps.length) return null
  // 取迭代内首个 tool_call 的工具名
  const firstToolCall = iter.toolItems.find(
    (i) => !i.is_streaming && i.type === 'tool_call',
  )
  if (!firstToolCall) return null
  const toolName = extractToolNameFromContent(firstToolCall.content || '')
  if (!toolName) return null
  const keywords = TOOL_STEP_KEYWORDS[toolName]
  if (!keywords) return null

  const kwLower = keywords.map((k) => k.toLowerCase())
  // 先找 pending(进入新步骤),再找 in_progress(同步骤内)
  for (const s of planSteps) {
    if (s.status !== 'pending') continue
    if (kwLower.some((k) => s.text.toLowerCase().includes(k))) return s.id
  }
  for (const s of planSteps) {
    if (s.status !== 'in_progress') continue
    if (kwLower.some((k) => s.text.toLowerCase().includes(k))) return s.id
  }
  return null
}

/** 读取 DisplayItem 的正文/思考文本(流式与正式两种形态) */
function itemFieldText(item: DisplayItem | undefined, field: 'content' | 'reasoning'): string {
  if (!item) return ''
  if (item.is_streaming && item.streaming) return item.streaming[field] || ''
  return (field === 'content' ? item.content : item.reasoning) || ''
}

/** 克隆项并清空正文(仅保留思考卡):最终思考留在过程组内的展示形态 */
function stripItemContent(item: DisplayItem): DisplayItem {
  if (item.is_streaming && item.streaming) {
    return { ...item, id: `${item.id}-think`, streaming: { ...item.streaming, content: '' } }
  }
  return { ...item, id: `${item.id}-think`, content: '' }
}

/** 克隆项并清空思考(仅保留正文卡):组外结论消息的展示形态 */
function stripItemReasoning(item: DisplayItem): DisplayItem {
  if (item.is_streaming && item.streaming) {
    return { ...item, id: `${item.id}-body`, streaming: { ...item.streaming, reasoning: '' } }
  }
  return { ...item, id: `${item.id}-body`, reasoning: null }
}

/** 把单个 round 内的 DisplayItem 列表先按迭代分段,再按 plan step 分组。
 *  roundClosed:该轮是否已结束(由 roundGroups 按 agent2 活动/运行态/非末轮判定);
 *  闭合时把最后一个"纯思考"迭代提为结论段平铺,避免总结被折叠的过程组藏住 */
function segmentRoundItems(
  roundIdx: number,
  items: DisplayItem[],
  planSteps: PlanStep[],
  roundClosed: boolean,
): RoundSegment[] {
  // 第一阶段:按 thinking 起点切迭代(原逻辑)
  const iterations: IterationSegment[] = []
  const plains: PlainSegment[] = []
  let current: IterationSegment | null = null
  let iterCounter = 0

  const closeCurrent = () => {
    if (current) {
      iterations.push(current)
      current = null
    }
  }

  for (const item of items) {
    if (isReactThinkingItem(item)) {
      closeCurrent()
      iterCounter++
      current = {
        kind: 'iteration',
        iterationIdx: iterCounter,
        id: `${roundIdx}-${iterCounter}`,
        thinkingItems: [item],
        toolItems: [],
        otherItems: [],
        hasStreaming: isStreamingActive(item),
      }
    } else if (isReactAgentItem(item)) {
      if (!current) {
        // 缺失 thinking 锚点(空 thinking 未落库 / CLI agent 未发文本直接工具调用 /
        // 前面的非 react 消息关闭了迭代):开一个无 thinking 的兜底迭代承接,
        // 避免工具项退化为 plain 段被追加到"执行过程"折叠块末尾
        iterCounter++
        current = {
          kind: 'iteration',
          iterationIdx: iterCounter,
          id: `${roundIdx}-${iterCounter}`,
          thinkingItems: [],
          toolItems: [],
          otherItems: [],
          hasStreaming: false,
        }
      }
      if (item.is_streaming) {
        current.thinkingItems.push(item)
      } else if (item.type === 'tool_call' || item.type === 'tool_result') {
        current.toolItems.push(item)
      } else {
        current.otherItems.push(item)
      }
      if (isStreamingActive(item)) current.hasStreaming = true
    } else {
      closeCurrent()
      // 记录消息在轮内的原始位置(已完成迭代数),第二阶段按位置穿插,
      // 避免用户追问等轮首/轮中消息被统一追加到轮末
      plains.push({ kind: 'plain', item, afterIterationIdx: iterCounter })
    }
  }
  closeCurrent()

  // 一阶段半:轮闭合时提取该轮最终结论
  // agent1 的每轮总结 = 该轮最后一条 thinking 的 content(无工具调用即结束 ReAct 循环,
  // 见后端 react_agent)。未闭合的轮不提取——运行中新迭代开头也是"纯思考",
  // 后续还会跟工具调用,提前提出会造成结论闪现再跳回过程组。
  // 思考(reasoning)不随结论外移:有思考时组内该项只保留思考卡、结论只保留正文卡,
  // 保持"过程(含最终思考)全在组内、组外只有纯正文消息"的干净结构。
  let conclusion: DisplayItem | null = null
  // 结论迭代是否整块提出(无思考可留时):true 时 plain 定位需补一个虚拟槽位
  let conclusionPopped = false
  if (roundClosed && iterations.length > 0) {
    const last = iterations[iterations.length - 1]
    const lastThinking = last.thinkingItems[last.thinkingItems.length - 1]
    const conclusionText = itemFieldText(lastThinking, 'content')
    const reasoningText = itemFieldText(lastThinking, 'reasoning')
    if (
      last.toolItems.length === 0 &&
      last.otherItems.length === 0 &&
      conclusionText.trim()
    ) {
      if (reasoningText.trim() && lastThinking) {
        // 思考留组内:组内只显示思考卡,结论只显示正文卡(克隆时改 id 保证 key 唯一)
        last.thinkingItems[last.thinkingItems.length - 1] = stripItemContent(lastThinking)
        conclusion = stripItemReasoning(lastThinking)
      } else {
        // 无思考可留:整迭代提出,组内不留空壳
        conclusion = lastThinking!
        iterations.pop()
        conclusionPopped = true
      }
    }
  }

  // 第二阶段:按 plan step 分组迭代
  // - 无 plan:所有迭代归入单个"执行过程"折叠组(结论已提出组外,组内是纯过程)
  // - 有 plan:迭代归属各 step 折叠组;无法归属的迭代进"执行过程"兜底折叠组
  const segments: RoundSegment[] = []
  const stepGroupsMap = new Map<number, StepGroup>()
  const noStepGroup: StepGroup = {
    kind: 'step',
    id: `${roundIdx}-nostep`,
    text: '执行过程',
    status: 'none',
    iterations: [],
    hasStreaming: false,
    live: false,
    plains: [],
  }

  /** 迭代序号 → 所属 step 组(用于把平铺消息穿插到对应组内边界) */
  const groupByIterIdx = new Map<number, StepGroup>()
  /** 最新迭代所属组 = 活跃轮的前沿组(iterations 按迭代序递增遍历,后者覆盖前者) */
  let leadingGroup: StepGroup | null = null

  for (const iter of iterations) {
    const stepId = inferStepFromIteration(iter, planSteps)
    if (stepId !== null) {
      // 归入 plan step 组
      let group = stepGroupsMap.get(stepId)
      if (!group) {
        const step = planSteps.find((s) => s.id === stepId)
        group = {
          kind: 'step',
          id: `${roundIdx}-step-${stepId}`,
          text: step?.text || '(未知步骤)',
          status: step?.status || 'pending',
          iterations: [],
          hasStreaming: false,
          live: false,
          plains: [],
        }
        stepGroupsMap.set(stepId, group)
      }
      group.iterations.push(iter)
      if (iter.hasStreaming) group.hasStreaming = true
      groupByIterIdx.set(iter.iterationIdx, group)
      leadingGroup = group
    } else {
      // 无法归属(无 plan 或工具名无匹配)→ 归入无 step 组
      noStepGroup.iterations.push(iter)
      if (iter.hasStreaming) noStepGroup.hasStreaming = true
      groupByIterIdx.set(iter.iterationIdx, noStepGroup)
      leadingGroup = noStepGroup
    }
  }

  // 按 plan step 顺序输出 step 组(无 plan 时只有 noStepGroup)
  const orderedGroups: StepGroup[] = []
  for (const step of planSteps) {
    const group = stepGroupsMap.get(step.id)
    if (group) {
      // 同步最新状态(plan 可能已被 LLM 更新)
      group.status = step.status
      orderedGroups.push(group)
    }
  }
  // 追加无法归属的迭代组(如果有)
  if (noStepGroup.iterations.length > 0) {
    orderedGroups.push(noStepGroup)
  }

  // 活跃轮的"前沿组"整段保持展开。此前只看 hasStreaming:thinking 一收完就转去
  // 跑工具(clone_repo / 子智能体可能几十秒),这段空档 hasStreaming 变 false,
  // 组正好在"工具正在跑"的时候收起,下一次 thinking 到达再展开 —— 直播在
  // 迭代边界上反复断流。前沿组之外的早期 step 组仍保持折叠,不形成刷屏长垄。
  if (!roundClosed && leadingGroup) {
    for (const g of orderedGroups) {
      g.live = g === leadingGroup || g.hasStreaming
    }
  }

  // 平铺消息按原始位置穿插到 step 组之间或组内迭代边界,不再统一追加到轮末
  // (此前 44f5d7d 重构出 step 分组时丢掉了 plain 的位置语义,导致用户追问等
  //  轮首/轮中消息显示在所有 agent1 响应之后)。
  // 规则(afterIterationIdx = 消息位于第几个迭代之后,0 = 轮首):
  // - 轮首(0)→ 输出到最前
  // - 组内边界(下一迭代与它同组)→ 挂到组上,模板在组内迭代边界渲染
  // - 组间边界 → 插到所属组之后
  // - 轮末(>= 迭代总数)→ 追加末尾(天然轮末的评估/总结保持原样)
  const headPlains: PlainSegment[] = []
  const tailPlains: PlainSegment[] = []
  const afterGroupPlains = new Map<StepGroup, PlainSegment[]>()
  // 虚拟迭代数:结论迭代整块提出时(无思考留组内)仍占一个边界槽位,保证
  // "结论前"的 plain 落在过程组与结论之间(时间顺序正确),
  // "结论后"的 plain(评估/修正指令)仍落轮末;思考留组内时迭代未动,无需补偿
  const virtualIterLen = iterations.length + (conclusionPopped ? 1 : 0)
  for (const p of plains) {
    const n = p.afterIterationIdx
    if (n <= 0) {
      headPlains.push(p)
    } else if (n >= virtualIterLen) {
      tailPlains.push(p)
    } else {
      const group = groupByIterIdx.get(n)
      const nextGroup = groupByIterIdx.get(n + 1)
      if (group && nextGroup === group) {
        // 组内边界:挂到组,渲染在迭代 n 与 n+1 之间
        group.plains.push(p)
      } else if (group) {
        // 组间边界:插到所属组之后
        let list = afterGroupPlains.get(group)
        if (!list) {
          list = []
          afterGroupPlains.set(group, list)
        }
        list.push(p)
      } else {
        // 兜底(理论上不可达):追加末尾
        tailPlains.push(p)
      }
    }
  }

  segments.push(...headPlains)
  for (const g of orderedGroups) {
    segments.push(g)
    const after = afterGroupPlains.get(g)
    if (after) segments.push(...after)
  }
  // 结论段排在所有过程组之后、轮末平铺消息(如修正指令卡)之前:
  // 时间顺序上 agent1 总结 → agent2 评估/追问,视觉上"过程(折叠) → 结论 → 修正指令"
  if (conclusion) segments.push({ kind: 'conclusion', item: conclusion })
  segments.push(...tailPlains)

  return segments
}

// ---- agent2 消息主界面过滤(阶段重构:检查助手过程移入右侧栏)----
// agent2 的输出中,唯一允许出现在主对话流的是"真追问"(修正指令)——
// 它会驱动 agent1 再跑一轮,是用户需要关注的事件;其余(思考/工具核查/
// 评估完成/总结)全部由右侧栏 Agent2Panel 展示。
// 判定逻辑与后端 routers/tasks.py 的 _is_ua_followup_evaluation 逐字对齐
// (常量 = _UA_EVAL_NON_FOLLOWUP_MARKERS,含旧版澄清文案,老数据兼容)。
const AGENT2_NON_FOLLOWUP_MARKERS = ['评估完成,无需追问', '(未给出追问)', '请求用户澄清']

/** agent2 评估消息是否为"真追问"(主对话流唯一保留的 agent2 内容) */
function isAgent2Followup(c: { role?: string; type?: string; content?: string }): boolean {
  if (c.role !== 'agent2' || c.type !== 'evaluation') return false
  const content = (c.content || '').trim()
  return !!content && !AGENT2_NON_FOLLOWUP_MARKERS.some((m) => content.startsWith(m))
}

const roundGroups = computed<RoundGroup[]>(() => {
  if (!task.value?.conversations && streamingItems.size === 0) return []

  const groups = new Map<number, DisplayItem[]>()

  // 加入正式对话:
  // - type=thinking 且有 reasoning/content → 转成流式卡片样式展示(只读,状态 done,reasoning 折叠)
  //   这样刷新页面后历史的思考过程仍以流式卡片的形式展示,和实时流式视觉一致;
  //   只有 content 没有思考链也算(非思考型模型/CLI 只出正文的迭代,
  //   实时阶段本来就是卡片形态,落库后不该降级成普通消息气泡)
  // - role=user type=question(用户指令) → 跳过,单独提取到顶部 userDirective 显示
  // - 其他类型 → 正常对话项
  //
  // seq 计算:用"该 round 内的下标 * 1000"(每条间隔 1000,留出空间给实时流式 thinking 插入)
  // 历史回放场景:thinking 和 tool_call 都在 convs,下标交替,顺序天然正确。
  const convs = task.value?.conversations ?? []
  const roundCounter = new Map<number, number>() // 每 round 内的下标计数
  convs.forEach((c) => {
    // 用户指令不进 round 分组,提到最顶部单独渲染
    if (c.role === 'user' && c.type === 'question') return

    const localIdx = roundCounter.get(c.round_idx) ?? 0
    roundCounter.set(c.round_idx, localIdx + 1)
    const seq = localIdx * 1000

    // agent2 消息默认移入右侧栏(仅真追问保留在主对话流)。
    // 注意:必须放在 localIdx 计数递增之后——convCountPerRound
    // (onConversation/onDone)仍计数全部消息,此处同基准跳过,
    // seq 留空洞无害(相对顺序不变),避免 agent1 流式 thinking 的
    // insertSeq 定位错位。
    if (c.role === 'agent2' && !isAgent2Followup(c)) return

    if (c.type === 'thinking' && (c.reasoning?.trim() || c.content?.trim())) {
      // 还原为流式卡片(只读模式)
      const historyConvId = `history:${c.id}`
      const streamingItem: StreamingItem = {
        conv_id: historyConvId,
        round_idx: c.round_idx,
        role: c.role as 'agent1' | 'agent2',
        reasoning: c.reasoning ?? '',
        content: c.content,
        status: 'done',
        started_at: c.created_at,
        finished_at: c.created_at,
        // 从独立 Map 读取用户手动意图(实时流式项不在此处读取);
        // 未点过则为 null,回放态(done)按自动规则折叠
        reasoning_pin: historyReasoningPins.get(historyConvId) ?? null,
        seq: 0,
        insertSeq: localIdx,
      }
      if (!groups.has(c.round_idx)) groups.set(c.round_idx, [])
      groups.get(c.round_idx)!.push({
        id: `stream:history:${c.id}`,
        round_idx: c.round_idx,
        created_at: c.created_at,
        is_streaming: true,
        seq,
        streaming: streamingItem,
      })
    } else {
      // 正常对话项
      if (!groups.has(c.round_idx)) groups.set(c.round_idx, [])
      groups.get(c.round_idx)!.push({
        id: c.id,
        round_idx: c.round_idx,
        created_at: c.created_at,
        is_streaming: false,
        seq,
        role: c.role,
        type: c.type,
        content: c.content,
        reasoning: c.reasoning,
        tool_call_id: c.tool_call_id,
        attachments: c.attachments,
      })
    }
  })

  // 加入实时流式思考项(SSE 期间)
  // 实时流式 thinking 不在 convs(后端 publish_event=False),只在 streamingItems 里。
  // 用 insertSeq(该 thinking 开始时该 round 已收到的正式对话数)定位插入位置:
  //   seq = insertSeq * 1000 - 500
  // 排在 convs[insertSeq-1](seq=(insertSeq-1)*1000)之后、convs[insertSeq](seq=insertSeq*1000)之前。
  // 例:thinking1 在 convCount=0 时开始(insertSeq=0),seq=-500,排在 tool_call1(seq=0)之前;
  //     thinking2 在 convCount=2 时开始(insertSeq=2),seq=1500,排在 tool_result1(seq=1000)
  //     之后、tool_call2(seq=2000)之前。这样每个 thinking 紧跟它之后的 tool_call/tool_result,
  //     正确归入各自迭代,不会出现"所有 thinking 挤前面、所有 tool_call 堆最后"的错乱。
  for (const item of streamingItems.values()) {
    // agent2 流式思考(含动态验证)由右侧栏 Agent2Panel 渲染,不进主对话流
    if (item.role === 'agent2') continue
    if (!groups.has(item.round_idx)) groups.set(item.round_idx, [])
    groups.get(item.round_idx)!.push({
      id: `stream:${item.conv_id}`,
      round_idx: item.round_idx,
      created_at: item.started_at,
      is_streaming: true,
      seq: item.insertSeq * 1000 - 500,
      streaming: item,
    })
  }

  // 轮闭合判定(结论提取的前提):
  // - 该轮已有 agent2 活动(思考/评估在 agent1 该轮结束后才开始记录)→ 闭合;
  // - 任务不在运行中(completed/failed/paused)→ 全部闭合;
  // - 不是最后一轮(后续轮已开跑,前轮必然结束)→ 闭合。
  // 运行中的末轮不闭合:新迭代开头也是纯思考,提前提取会造成结论闪现再跳回。
  const agent2Rounds = new Set(
    convs.filter((c) => c.role === 'agent2').map((c) => c.round_idx),
  )
  const lastRoundIdx = groups.size ? Math.max(...groups.keys()) : -1

  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([roundIdx, items]) => {
      // 用 seq 排序(稳定,不依赖跨来源的 created_at)
      const sorted = items.sort((a, b) => a.seq - b.seq)
      const steps = planPerRound.get(roundIdx) ?? []
      const roundClosed =
        agent2Rounds.has(roundIdx) || !isRunning.value || roundIdx !== lastRoundIdx
      return {
        roundIdx,
        segments: segmentRoundItems(roundIdx, sorted, steps, roundClosed),
        planSteps: steps,
      }
    })
})

/** 用户指令(从对话中提取,单独显示在最顶部) */
const userDirective = computed<DisplayItem | null>(() => {
  const c = task.value?.conversations?.find(
    (x) => x.role === 'user' && x.type === 'question',
  )
  if (!c) return null
  return {
    id: c.id,
    round_idx: c.round_idx,
    created_at: c.created_at,
    is_streaming: false,
    seq: 0,
    role: c.role,
    type: c.type,
    content: c.content,
  }
})

/** 侧栏"任务清单":取有 plan 的最大 round(计划随轮次更新,最新一轮即当前进度);
 *  数据来自 SSE plan 事件与历史对话提取(extractPlanFromHistory),均写入 planPerRound */
const latestPlanSteps = computed<PlanStep[]>(() => {
  let best: PlanStep[] = []
  let bestRound = -1
  for (const [roundIdx, steps] of planPerRound) {
    if (steps.length > 0 && roundIdx > bestRound) {
      bestRound = roundIdx
      best = steps
    }
  }
  return best
})

// ---- 折叠状态查询/切换 ----

/** step 组是否展开:手动收起优先;否则手动展开 OR 活跃轮前沿组(直播中)
 * OR 含流式 OR 组内有用户消息(追问/回答必须可见)。最终总结已提为结论段平铺,
 * 轮闭合后的过程组一律默认折叠 */
function isStepExpanded(group: StepGroup): boolean {
  if (collapsedSteps.has(group.id)) return false
  // 前沿组/含流式/含组内平铺消息(如用户追问/回答)时自动展开,保证直播内容可见
  if (
    expandedSteps.has(group.id) ||
    group.live ||
    group.hasStreaming ||
    group.plains.length > 0
  ) return true
  return false
}

function toggleStep(group: StepGroup): void {
  if (isStepExpanded(group)) {
    collapsedSteps.add(group.id)
    expandedSteps.delete(group.id)
  } else {
    collapsedSteps.delete(group.id)
    expandedSteps.add(group.id)
  }
}

/** step 组的状态图标:done=✓,in_progress=◌,pending=○,none=· */
function stepStatusIcon(status: PlanStep['status'] | 'none'): string {
  switch (status) {
    case 'done': return '✓'
    case 'in_progress': return '◌'
    case 'pending': return '○'
    default: return '·'
  }
}

/** 工具渲染行是否展开(默认折叠,由用户控制;key 支持 `${callId}-think` 复合键) */
function isRowExpanded(key: string): boolean {
  return expandedToolRows.has(key)
}

function toggleRow(key: string): void {
  if (expandedToolRows.has(key)) {
    expandedToolRows.delete(key)
  } else {
    expandedToolRows.add(key)
  }
}

/** 工具渲染行:compact 单行摘要 / agent 子智能体卡片 / toolpair 普通工具卡片 / plain 兜底。
 * 字段扁平化避免模板内联合类型收窄 */
interface ToolRenderRow {
  key: string
  kind: 'compact' | 'agent' | 'toolpair' | 'plain'
  /** compact/agent/toolpair:tool_call id(展开状态 key) */
  callId: string
  /** compact:单行摘要;agent/toolpair:卡片标题 */
  summary: string
  /** compact:可跳转的文件路径(工作区相对路径,无则空串) */
  filePath: string
  /** compact:摘要中展示的文件路径文本 */
  fileDisplay: string
  /** compact:摘要拆分前后缀(filePath 非空时,摘要 = prefix + fileDisplay + suffix) */
  summaryPrefix: string
  summarySuffix: string
  /** 是否已有结果(执行中为 false) */
  hasResult: boolean
  /** 结果文本(compact 轻量展开 / toolpair 结果块) */
  resultContent: string
  /** agent/toolpair:调用参数 detail(展开后等宽块) */
  callDetail: string
  /** agent:内部思考(<think> 块内容) */
  agentThink: string
  /** agent:Markdown 渲染的报告正文 HTML */
  agentBodyHtml: string
  /** plain:该段内的工具项(兜底原渲染) */
  items: DisplayItem[]
}

/** tool_call content 拆分为 intent(首行,剥 [tool_name] 标签)与 detail(其后内容) */
function callPartsOf(call: DisplayItem): { intent: string; detail: string } {
  const content = call.content || ''
  const idx = content.indexOf('\n')
  const rawIntent = idx < 0 ? content : content.slice(0, idx)
  return {
    intent: rawIntent.replace(/\s*\[\w+\]$/, ''),
    detail: idx < 0 ? '' : content.slice(idx + 1),
  }
}

/**
 * 迭代内工具项拆分为渲染行:
 * compact(浏览型单行摘要)、agent(子智能体卡片,Markdown 报告)、
 * toolpair(普通工具,调用+结果整体一张折叠卡)。
 */
function toolRowsOf(iter: IterationSegment): ToolRenderRow[] {
  const empty = {
    resultContent: '',
    callDetail: '',
    agentThink: '',
    agentBodyHtml: '',
    filePath: '',
    fileDisplay: '',
    summaryPrefix: '',
    summarySuffix: '',
    items: [] as DisplayItem[],
  }
  return buildToolSegments(iter.toolItems).map((seg, idx) => {
    if (seg.kind === 'plain') {
      return {
        key: `${iter.id}-plain-${idx}`,
        kind: 'plain' as const,
        callId: '',
        summary: '',
        hasResult: false,
        ...empty,
        items: seg.items,
      }
    }
    const callId = seg.call.id
    const parts = callPartsOf(seg.call)
    if (seg.kind === 'compact') {
      // 读文件工具提取跳转目标:摘要中路径文本包成链接,点击在工作区文件树打开
      const target = toolFileTargetOf(seg.call, seg.result)
      return {
        key: callId,
        kind: 'compact' as const,
        callId,
        summary: buildToolSummary(seg.call, seg.result),
        hasResult: !!seg.result,
        ...empty,
        resultContent: seg.result?.content || '',
        filePath: target?.path || '',
        fileDisplay: target?.display || '',
        summaryPrefix: target?.prefix || '',
        summarySuffix: target?.suffix || '',
      }
    }
    if (seg.kind === 'agent') {
      const trace = seg.result ? parseAgentTrace(seg.result.content || '') : null
      // 标题:子任务意图 + 子智能体类型/状态后缀
      const tags: string[] = []
      if (trace?.subType) tags.push(trace.subType)
      if (!seg.result) tags.push('执行中…')
      else if (trace?.status && trace.status !== 'completed') tags.push(trace.status)
      else tags.push('已完成')
      return {
        key: callId,
        kind: 'agent' as const,
        callId,
        summary: `🤖 ${parts.intent}${tags.length ? ' · ' + tags.join(' · ') : ''}`,
        hasResult: !!seg.result,
        ...empty,
        callDetail: parts.detail,
        agentThink: trace?.think || '',
        agentBodyHtml: trace ? renderMarkdown(trace.body) : '',
      }
    }
    // toolpair:普通工具(写操作/非只读命令等),调用+结果整体一张折叠卡
    return {
      key: callId,
      kind: 'toolpair' as const,
      callId,
      summary: `🔧 ${parts.intent}`,
      hasResult: !!seg.result,
      ...empty,
      callDetail: parts.detail,
      resultContent: seg.result?.content || '',
    }
  })
}

/** plan 进度文本:已完成 / 总数 */
function planProgress(steps: PlanStep[]): string {
  const done = steps.filter((s) => s.status === 'done').length
  return `${done}/${steps.length}`
}

// ---- 结果分组:由 task.params._grouping 驱动 ----
//
// 场景降级后,分组声明由 agent2 在 done 时写入 task.params._grouping,
// 不再从场景声明读取。grouping 结构与原 ScenarioResultGrouping 一致:
// - 无 _grouping:不分组,所有结果放入单个"结果"组平铺
// - type=ordered:按声明 values 的 order 排序,metadata 缺失该字段用 default 组
// - type=dynamic:按 metadata 实际值动态分组,缺失用 default 组
//
// color 与前端 sev-<color> CSS class 对齐(安全场景保留原视觉)

/** 分组枚举值(对应原 ScenarioResultGroupValue) */
interface ResultGroupValue {
  value: string
  label: string
  /** 颜色 key,对应前端 CSS class 后缀(如 critical/high/medium) */
  color: string
  /** 排序序号 */
  order: number
}

/** 分组声明(对应原 ScenarioResultGrouping,从 task.params._grouping 读取) */
interface ResultGrouping {
  /** 从 result.metadata 取该字段分组 */
  field: string
  /** ordered(固定枚举+顺序) | dynamic(按值动态分组) */
  type: 'ordered' | 'dynamic'
  /** ordered 时的固定枚举值 */
  values: ResultGroupValue[]
  /** 元数据缺失该字段时的分组名 */
  default_label: string
  /** 默认分组颜色 key(对应前端 sev-<color> CSS class) */
  default_color: string
}

interface ResultGroup {
  /** 分组 key(severity 值或 '__default__' / 'all') */
  key: string
  label: string
  /** 颜色 key,对应 sev-<color> CSS class */
  color: string
  results: TaskResult[]
}

/** 从 task.params._grouping 读取分组声明(可能不存在) */
const resultGrouping = computed<ResultGrouping | null>(() => {
  const g = task.value?.params?.['_grouping'] as Partial<ResultGrouping> | undefined
  if (!g || !g.field) return null
  // 形态校验通过即可,字段完整性由后端保证
  return g as ResultGrouping
})

const resultGroups = computed<ResultGroup[]>(() => {
  if (!task.value?.results) return []
  const results = task.value.results
  const grouping = resultGrouping.value

  // 不分组:单个平铺组
  if (!grouping) {
    return [{ key: 'all', label: '重点与知识点', color: 'unknown', results }]
  }

  // 按 grouping.field 从 metadata 取值分组
  const buckets = new Map<string, TaskResult[]>()
  for (const r of results) {
    const raw = (r.metadata_?.[grouping.field] as string | undefined) ?? ''
    const key = raw || '__default__'
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key)!.push(r)
  }

  if (grouping.type === 'ordered') {
    // 按声明 values 的 order 排序,仅渲染有结果的组
    const ordered: ResultGroup[] = []
    for (const v of [...grouping.values].sort((a, b) => a.order - b.order)) {
      const rs = buckets.get(v.value) || []
      if (rs.length > 0) {
        ordered.push({ key: v.value, label: v.label, color: v.color, results: rs })
      }
    }
    // default 组(metadata 缺失字段的结果)
    const defaultRs = buckets.get('__default__') || []
    if (defaultRs.length > 0) {
      ordered.push({
        key: '__default__',
        label: grouping.default_label,
        color: grouping.default_color,
        results: defaultRs,
      })
    }
    return ordered
  }

  // dynamic:按实际值动态分组,缺失用 default
  const dyn: ResultGroup[] = []
  for (const [key, rs] of buckets) {
    if (key === '__default__') {
      dyn.push({ key, label: grouping.default_label, color: grouping.default_color, results: rs })
    } else {
      dyn.push({ key, label: key, color: grouping.default_color, results: rs })
    }
  }
  return dyn
})

// ---- 审查结果(ReviewItem):证据驱动可信审查,按三态分桶展示 ----

const reviewItems = computed<ReviewItem[]>(() => task.value?.review_items ?? [])
const reviewBuckets = computed(() => bucketizeReviewItems(reviewItems.value))
const hasReviewResults = computed(() => reviewItems.value.length > 0)
/** 聚合审查结论(task.params._review,review_done 快照权威) */
const reviewSummary = computed<ReviewSummary | null>(() => {
  const r = task.value?.params?.['_review'] as ReviewSummary | undefined
  return r && r.counts ? r : null
})
/** 实时审查计划(SSE review_plan_update;review_done 后以 _review.plan 为准) */
const reviewPlanLive = ref<ReviewPlanUpdateEventData | null>(null)

const expandedReviewItems = ref<Set<string>>(new Set())
function toggleReviewItem(id: string): void {
  const s = new Set(expandedReviewItems.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  expandedReviewItems.value = s
}

/** 三态分桶的展示顺序与标题(发现风险 → 已核查·剔除误报 → 缺口·待改进) */
const REVIEW_BUCKET_ORDER = [
  { key: 'risk', label: '发现风险' },
  { key: 'cleared', label: '已核查·剔除误报' },
  { key: 'gap', label: '缺口·待改进' },
] as const

/** 审查项证据里的源码/原文文件:复用工作区文件跳转 */
async function openReviewFile(it: ReviewItem): Promise<void> {
  const path = it.evidence?.source?.file_path
  if (!path) return
  await onToolFileClick(path)
}

// ---- 状态徽章 ----
const statusConfig: Record<TaskStatus, { label: string; class: string }> = {
  pending: { label: '等待中', class: 'badge-pending' },
  running: { label: '进行中', class: 'badge-running' },
  paused: { label: '已暂停', class: 'badge-paused' },
  completed: { label: '已完成', class: 'badge-completed' },
  failed: { label: '已失败', class: 'badge-failed' },
}

// ---- 是否运行中(控制滚动区域提示) ----
// paused 也算"活跃"状态:仍在 SSE 订阅,UI 显示暂停徽标 + 恢复按钮
const isRunning = computed(
  () =>
    task.value?.status === 'pending' ||
    task.value?.status === 'running' ||
    task.value?.status === 'paused',
)

/** 是否处于暂停态(控制按钮文案:暂停 ↔ 恢复) */
const isPaused = computed(() => task.value?.status === 'paused')

// ---- 检查助手侧栏面板(阶段重构:agent2 过程输出全部移入右侧栏)----
/** 检查助手是否有活动内容(历史消息或流式思考/审查状态);无则隐藏面板(单 agent 模式/未开始) */
const hasAgent2Activity = computed(
  () =>
    !!task.value?.review_status ||
    !!task.value?.conversations?.some((c) => c.role === 'agent2') ||
    [...streamingItems.values()].some((s) => s.role === 'agent2'),
)
/** 检查助手核查进行中(呼吸点仅在其核查阶段亮,agent1 执行时不亮) */
const isAgent2Running = computed(
  () =>
    task.value?.status === 'running' &&
    (task.value?.current_stage || '').includes('检查助手'),
)

/** 暂停/恢复按钮 loading 态(防止重复点击) */
const pausing = ref(false)

/**
 * 终止检查请求已提交(后台审查线程收尾前保持置灰)
 *
 * 后端是协作式取消:LLM 流 chunk 边界/工具循环边界才生效,可能滞后数十秒。
 * review_status 离开 running(收到 review_done=stopped 或新一轮重置)即复位。
 */
const stoppingReview = ref(false)

watch(
  () => task.value?.review_status,
  (s) => { if (s !== 'running') stoppingReview.value = false },
)

/**
 * 点击"终止检查":有些对话不需要检查,停下后台核查
 *
 * 终态由审查线程写并随 review_done 事件送达(本函数不轮询等终态)。
 * 例外:后端发现已无审查线程在跑(遗留"检查中"角标)时会就地收尾,
 * 此时拉一次快照兜底,避免 SSE 已断时前端停在旧状态。
 */
async function handleStopReview(): Promise<void> {
  if (!task.value?.id || stoppingReview.value) return
  stoppingReview.value = true
  const taskId = String(task.value.id)
  try {
    const res = await stopTaskReview(taskId)
    clientLog(taskId, 'stop_review', { review_status: res.review_status })
    if (res.review_status !== 'running') {
      const fresh = await getTask(taskId)
      if (fresh && !unmountedFlag) applyTaskSnapshot(fresh, false)
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
    stoppingReview.value = false  // 失败允许重试
  }
}

/** 点击暂停/恢复按钮:根据当前状态调对应 API */
async function handleTogglePause(): Promise<void> {
  if (!task.value?.id || pausing.value) return
  pausing.value = true
  try {
    if (isPaused.value) {
      await resumeTask(String(task.value.id))
    } else if (task.value.status === 'running' || task.value.status === 'pending') {
      await pauseTask(String(task.value.id))
    }
    // 状态变更由 SSE status 事件驱动更新,这里不本地改写
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    pausing.value = false
  }
}

// ---- 结果卡片 metadata 渲染:动态推断 ----
//
// 场景降级后,展示字段不再由场景声明 result_meta_fields 决定,
// 而是从所有 results 的 metadata keys 动态推断:
// - 收集所有 results 的 metadata keys 的并集(保持首次出现顺序)
// - file_path 视为 file 类型(可点击跳转源码),其余视为 text
// - 单条 result 渲染时,只展示该 result 实际有值(非空)的字段

/** 推断的 metadata 展示字段 */
interface InferredMetaField {
  name: string
  type: 'text' | 'file'
}

interface ResultMetaItem {
  field: InferredMetaField
  value: string
}

/**
 * 推断结果 metadata 展示字段:从所有 results 的 metadata keys 动态收集
 *
 * 取并集(保持首次出现顺序),file_path 视为 file 类型,其余为 text。
 */
const inferredMetaFields = computed<InferredMetaField[]>(() => {
  const results = task.value?.results ?? []
  const seen = new Set<string>()
  const fields: InferredMetaField[] = []
  // 学习点专用字段不进通用 meta 标签区:learning_note 有独立引用块展示,
  // practice_worthy 为布尔标记(出题用),显示出来只会是 "true"
  const META_FIELD_SKIP = new Set(['learning_note', 'practice_worthy'])
  for (const r of results) {
    if (!r.metadata_) continue
    for (const key of Object.keys(r.metadata_)) {
      if (seen.has(key) || META_FIELD_SKIP.has(key)) continue
      seen.add(key)
      fields.push({
        name: key,
        type: key === 'file_path' ? 'file' : 'text',
      })
    }
  }
  return fields
})

/** 结果清单正文 markdown 渲染(审计结果通常含标题/列表/代码块) */
function renderResultContent(content: string | null | undefined): string {
  return renderMarkdown(content)
}

function getResultMetaItems(r: TaskResult): ResultMetaItem[] {
  const items: ResultMetaItem[] = []
  for (const f of inferredMetaFields.value) {
    const v = r.metadata_?.[f.name]
    if (v != null && v !== '') {
      items.push({ field: f, value: String(v) })
    }
  }
  return items
}

// ---- 结果项点击跳转源码位置(B1) ----

/** 工作区侧栏组件引用,用于调用 openTaskFile 跳转 */
const sidebarRef = ref<InstanceType<typeof WorkspaceSidebar> | null>(null)

/** 从 line_range 字符串(如 "10-20" / "10")解析起始行号 */
function parseStartLine(lineRange?: string): number | undefined {
  if (!lineRange) return undefined
  const m = lineRange.match(/(\d+)/)
  return m ? parseInt(m[1], 10) : undefined
}

/** 点击结果项的文件标签:展开侧栏并打开文件定位行号 */
async function onResultFileClick(r: TaskResult): Promise<void> {
  const path = r.metadata_?.['file_path'] as string | undefined
  if (!path || !task.value?.id) return
  const startLine = parseStartLine(r.metadata_?.['line_range'] as string | undefined)
  // 展开工作区侧栏(若折叠)
  workspaceCollapsed.value = false
  await nextTick()
  await sidebarRef.value?.openTaskFile(String(task.value.id), path, startLine)
}

/** 点击工具行摘要中的文件路径:展开工作区侧栏并在文件树中打开该文件 */
async function onToolFileClick(path: string): Promise<void> {
  if (!path || !task.value?.id) return
  workspaceCollapsed.value = false
  await nextTick()
  await sidebarRef.value?.openTaskFile(String(task.value.id), path)
}

// ---- 侧栏事件:任务被删除 / 标题被修改 ----

/** 侧栏删除任务后:若删除的是当前详情页任务,跳转离开(避免停在 404 页) */
function onSidebarTaskDeleted(taskId: string): void {
  if (task.value && task.value.id === taskId) {
    // 关闭 SSE 等资源
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    router.replace('/tasks/new')
  }
}

/** 侧栏修改标题后:若改的是当前详情页任务,同步本地 task.title */
function onSidebarTaskTitleUpdated(taskId: string, title: string | null): void {
  if (task.value && task.value.id === taskId) {
    task.value = { ...task.value, title }
  }
}

// ---- 格式化时间 ----

function formatTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** 无标题时回退到 user_input,并截断到 max 字符(与历史任务侧栏一致) */
function truncateInput(s: string, max = 40): string {
  if (s.length <= max) return s
  return s.slice(0, max) + '...'
}

// ---- 用户补充消息输入框事件处理 ----

/**
 * 用户消息发送成功后的处理
 *
 * - completed 态:后端已同步置 RUNNING 并启动 resume 线程(追问直达
 *   agent1,不等老审查 —— 老审查与新轮并行,由最后活跃流收尾)。
 *   本地同步状态 + 重连 SSE(老审查在跑时总线本就打开,重连经历史
 *   补播无缝衔接;上一轮已收尾时后端已 reset,重连接收新事件)。
 * - running / paused 态:SSE 已连接,conversation 事件由 onConversation 自动接收,
 *   无需额外处理。
 */
function handleMessageSent(_resp: SendMessageResponse): void {
  if (task.value?.status === 'completed') {
    // 后端端点已把状态同步改为 RUNNING,本地同步 + 重连 SSE
    task.value.status = 'running'
    task.value.current_stage = '用户追加消息,重启执行'
    // 重置审查状态:新一轮 agent1 执行 → 新一轮审查尚未开始,
    // 旧值(done/failed)会误导侧栏 badge(老审查的 review_done
    // 事件到达时会再更新;并行期间 badge 可能有短暂抖动)
    task.value.review_status = null
    // 标记 resume 窗口:onDone 若在窗口内触发,需校验是否竞态误推
    resumingRef.value = true
    connectSSE(String(task.value.id))
  }
  forceScrollToBottom()
}

/** 用户消息发送失败:展示错误提示 */
function handleMessageError(message: string): void {
  error.value = message
}

/** 正在撤回的消息 id 集合(防重复点击) */
const withdrawingIds = ref<Set<string>>(new Set())

/**
 * 撤回待处理消息(运行中发送、尚未被 agent1 消费的)
 *
 * 后端从队列移除 + 删除 Conversation + 推 user_message_withdrawn 事件
 * (onUserMessageWithdrawn 移除条目,多端同步;此处乐观移除防闪烁)。
 * 已被消费则后端拒绝,提示用户。
 */
async function handleWithdrawPendingMessage(messageId: string): Promise<void> {
  if (!task.value?.id || withdrawingIds.value.has(messageId)) return
  withdrawingIds.value.add(messageId)
  try {
    const resp = await withdrawTaskMessage(String(task.value.id), messageId)
    if (resp.success) {
      // 乐观移除(SSE 事件到达时幂等)
      pendingUserMessages.value = pendingUserMessages.value.filter(
        (m) => m.id !== messageId,
      )
    } else {
      error.value = resp.message || '消息已被处理,无法撤回'
      // 撤回失败(已被消费):条目本来也会被 conversation 事件转走,防御性移除
      pendingUserMessages.value = pendingUserMessages.value.filter(
        (m) => m.id !== messageId,
      )
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    withdrawingIds.value.delete(messageId)
  }
}

/**
 * 侧栏"建议追问":把检查助手的建议文本作为用户消息发出
 *
 * 任务已 COMPLETED(agent1 结束即完成),后端走 resume 链路:
 * agent1 追加执行一轮 → 新一轮后台审查(老审查若仍在跑则并行,
 * 不阻塞)。发送成功后同 handleMessageSent 的 completed 分支
 * (置 running + 标记 resume 窗口 + 重连 SSE)。
 */
async function handleSuggestionDig(text: string): Promise<void> {
  if (!task.value?.id) return
  try {
    const resp = await sendTaskMessage(String(task.value.id), { content: text })
    clientLog(String(task.value.id), 'suggestion_dig', {
      accepted: resp.accepted,
      message: resp.message,
    })
    if (resp.accepted) {
      handleMessageSent(resp)
    } else {
      error.value = resp.message || '消息发送失败,请稍后再试'
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  }
}

/** 运行时设置(agent1 / agent2 模型)保存成功:回填后端最新快照 */
function handleRuntimeConfigSaved(updated: TaskDetail): void {
  task.value = updated
}

// ---- 失败任务重试(底部重试条,替换输入框位置)----

/** 重试请求是否进行中(按钮 loading 态,防重复点击) */
const retrying = ref(false)

/**
 * 重试失败任务
 *
 * 后端按失败阶段自动分流(断点续跑优先,无可续进度从头重跑)。
 * 启动成功后乐观置 running + 清错误信息 + 重连 SSE
 * (同 handleMessageSent 的 completed 分支;后端已 reset_task_bus,
 * 重试标记对话等事件会通过 SSE 历史补播送达)。
 */
async function handleRetry(): Promise<void> {
  if (!task.value || task.value.status !== 'failed' || retrying.value) return
  // [诊断] 用户点击重试:与后端 retry 拒绝日志对拍(定位"running 不能重试")
  clientLog(String(task.value.id), 'retry_clicked', {
    local_status: task.value.status,
    error_message: task.value.error_message,
  })
  retrying.value = true
  try {
    const resp = await retryTask(String(task.value.id))
    clientLog(String(task.value.id), 'retry_response', {
      accepted: resp.accepted,
      message: resp.message,
    })
    if (resp.accepted) {
      task.value.status = 'running'
      task.value.error_message = null
      task.value.current_stage = '重试失败任务...'
      // 标记 resume 窗口(同 handleMessageSent:onDone 校验竞态误推)
      resumingRef.value = true
      connectSSE(String(task.value.id))
      forceScrollToBottom()
    } else {
      error.value = resp.message || '重试启动失败'
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    retrying.value = false
  }
}

/** 判断 DisplayItem 是否为用户补充消息(type=message,需右对齐展示) */
function isUserMessageItem(item: DisplayItem): boolean {
  return !item.is_streaming && item.role === 'user' && item.type === 'message'
}

/** 筛选 step 组内应显示在迭代 iterIdx 之后的平铺消息(0 = 首个迭代之前) */
function plainsAfter(group: StepGroup, iterIdx: number): PlainSegment[] {
  return group.plains.filter((p) => p.afterIterationIdx === iterIdx)
}

// ---- 右侧栏结果清单展开状态(默认折叠,点击卡片展开正文) ----
const expandedResults = reactive<Set<string>>(new Set())

// ---- 生成练习题(把结果清单的真实发现转为自适应练习题,见 PracticeGenerateDialog) ----
// 后端 PRACTICE_ENABLED=false 时隐藏入口并跳过 draft 拉取
ensureFeaturesLoaded()
const practiceDialogOpen = ref(false)
/** 该任务待确认 draft 数(>0 时按钮提示「确认练习题(N)」,含任务完成后自动生成的) */
const pendingDraftCount = ref(0)

async function refreshPracticeDraftCount(): Promise<void> {
  if (!practiceEnabled.value) {
    pendingDraftCount.value = 0
    return
  }
  if (!task.value || task.value.status !== 'completed') {
    pendingDraftCount.value = 0
    return
  }
  try {
    pendingDraftCount.value = (await listDrafts(String(task.value.id))).length
  } catch {
    pendingDraftCount.value = 0  // 匿名/失败不提示
  }
}

/** 本次出题将使用的模型(与后端出题同一解析逻辑;任务完成 + 功能开启时展示) */
const generateModelInfo = ref<GenerateModelInfo | null>(null)

async function refreshGenerateModel(): Promise<void> {
  if (!practiceEnabled.value || !task.value || task.value.status !== 'completed') {
    generateModelInfo.value = null
    return
  }
  try {
    generateModelInfo.value = await getTaskGenerateModel(String(task.value.id))
  } catch {
    generateModelInfo.value = null  // 匿名/失败不展示
  }
}

/** 模型来源的中文说明(任务详情页「本次出题将使用」后缀) */
const generateModelSourceLabel = computed(() => {
  const source = generateModelInfo.value?.source
  if (source === 'task') return '任务配置'
  if (source === 'default') return '练习默认设置'
  return '环境默认'
})

watch(
  () => task.value?.status,
  (status) => {
    if (status === 'completed') {
      refreshPracticeDraftCount()
      refreshGenerateModel()
      // 自动出题在任务完成时触发:开始轮询本任务关联的运行中 job,展示跳转入口
      startGenJobPoll()
    } else {
      stopGenJobPoll()
    }
  },
)

// ---- 出题进度跳转入口:轮询本任务关联的出题 job(手动/自动),运行中时展示 ----
const runningGenJob = ref<GenerateJobSummary | null>(null)
let genJobPollTimer: ReturnType<typeof setInterval> | null = null

async function pollRunningGenJob(): Promise<void> {
  if (!practiceEnabled.value || !task.value || task.value.status !== 'completed') {
    runningGenJob.value = null
    return
  }
  try {
    const res = await listGenerateJobs()
    const taskId = String(task.value.id)
    runningGenJob.value = res.jobs.find(
      (j) => j.task_id === taskId && (j.status === 'pending' || j.status === 'running'),
    ) ?? null
  } catch {
    runningGenJob.value = null  // 匿名/失败不提示
  }
}

function startGenJobPoll(): void {
  if (genJobPollTimer) return
  void pollRunningGenJob()
  genJobPollTimer = setInterval(pollRunningGenJob, 5000)
}

function stopGenJobPoll(): void {
  if (genJobPollTimer) {
    clearInterval(genJobPollTimer)
    genJobPollTimer = null
  }
  runningGenJob.value = null
}

onUnmounted(stopGenJobPoll)

/** 跳转自适应练习页查看出题进度(练习页会自动展开出题进度侧栏) */
function goToPracticeProgress(): void {
  router.push({ name: 'practice' })
}

function openPracticeGenerate(): void {
  // 打开出题对话框前刷新一次模型展示(设置里可能刚换过默认模型)
  void refreshGenerateModel()
  practiceDialogOpen.value = true
}

function toggleResult(id: string): void {
  if (expandedResults.has(id)) {
    expandedResults.delete(id)
  } else {
    expandedResults.add(id)
  }
}
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          v-if="task"
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          data-onboarding="detail-workspace-toggle"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
    <!-- 左侧:历史任务栏 + 按需切换工作区(v-show 保留已加载的任务列表/文件树状态,折叠不销毁) -->
    <WorkspaceSidebar
      v-show="!workspaceCollapsed"
      ref="sidebarRef"
      :changed-files="changedFiles"
      :repo-files="repoFiles"
      @task-deleted="onSidebarTaskDeleted"
      @task-title-updated="onSidebarTaskTitleUpdated"
      @open-diff-file="scrollToDiffFile"
    />

    <main class="main">
      <!-- 标题/状态行(固定在滚动容器外,不随对话滚动;有任务即显示) -->
      <div v-if="task" class="conv-header">
        <!-- 左侧:对话标题 + 创建时间 -->
        <div class="conv-header-info">
          <span class="conv-header-title" :title="task?.title || task?.user_input">
            {{ truncateInput(task?.title || task?.user_input || '') }}
          </span>
          <span v-if="task?.created_at" class="conv-header-time">{{ formatTime(task.created_at) }}</span>
        </div>
        <!-- 右侧:暂停态橙色徽标 + 恢复按钮;运行态红色实时徽标 + 暂停按钮 -->
        <template v-if="isRunning">
          <span v-if="isPaused" class="paused-indicator">
            <span class="paused-bars" /><span>已暂停</span>
          </span>
          <span v-else class="live-indicator">
            <span class="live-dot" />实时
          </span>
          <button
            class="btn-pause"
            :disabled="pausing"
            :title="isPaused ? '恢复执行' : '暂停执行'"
            data-onboarding="detail-pause"
            @click="handleTogglePause"
          >
            {{ pausing ? '处理中...' : isPaused ? '恢复' : '暂停' }}
          </button>
        </template>
      </div>
      <div ref="conversationRef" class="main-scroll" @scroll="handleConversationScroll">
      <!-- 加载中 -->
      <div v-if="loading" class="loading-state">
        <div class="spinner-lg" />
        <p>加载任务详情...</p>
      </div>

      <!-- 错误 -->
      <div v-else-if="error" class="error-state">
        <p>{{ error }}</p>
        <RouterLink to="/tasks/new">提交新任务</RouterLink>
      </div>

      <!-- 任务详情(主区聚焦协作对话流;任务概览/任务清单/动态验证/检查助手核查/重点与知识点均在右侧栏) -->
      <template v-else-if="task">
        <!-- 协作对话流(无外框,顶部仅在运行时显示实时徽标) -->
        <section
          v-if="roundGroups.length > 0 || isRunning"
          class="conversation-section"
          data-onboarding="detail-conversation"
        >
          <!-- 用户指令(右对齐,像聊天界面的用户消息气泡) -->
          <div v-if="userDirective" class="user-directive">
            <ConversationMessage
              :item="userDirective"
              @toggle-reasoning="toggleReasoning"
            />
          </div>

          <div v-for="group in roundGroups" :key="group.roundIdx" class="round-group">
            <!-- 计划清单已移至右侧栏"任务清单"(latestPlanSteps),主对话流只保留消息流 -->
            <div class="messages">
              <template
                v-for="seg in group.segments"
                :key="seg.kind === 'step' ? `step-${seg.id}` : `${seg.kind}-${seg.item.id}`"
              >
                <!-- 平铺段:agent2 追问卡、user 指令等关键消息 -->
                <!-- 用户补充消息(type=message)右对齐,与顶部 userDirective 视觉一致 -->
                <div
                  v-if="seg.kind === 'plain'"
                  :class="{ 'user-msg-row': isUserMessageItem(seg.item) }"
                >
                  <!-- agent2 真追问:醒目"修正指令"卡(主对话流唯一保留的检查助手内容) -->
                  <div
                    v-if="isAgent2Followup(seg.item)"
                    class="followup-card"
                    data-onboarding="detail-followup"
                  >
                    <div class="followup-card-header">
                      <span class="followup-card-icon" aria-hidden="true">⚠</span>
                      <span class="followup-card-title">检查助手修正指令</span>
                      <span class="followup-card-sub">已要求 AI助手 修正/补充上述问题</span>
                    </div>
                    <ConversationMessage
                      :item="seg.item"
                      @toggle-reasoning="toggleReasoning"
                    />
                  </div>
                  <ConversationMessage
                    v-else
                    :item="seg.item"
                    @toggle-reasoning="toggleReasoning"
                  />
                </div>

                <!-- step 分组:plan step 各自折叠;无 plan 时为单个"执行过程"折叠组。
                     过程整体默认收起,该轮最终回答由结论段在组外直接展示 -->
                <div
                  v-else-if="seg.kind === 'step'"
                  class="step-block"
                  :class="{
                    'step-streaming': seg.hasStreaming,
                    'step-done': seg.status === 'done',
                    'step-expanded': isStepExpanded(seg),
                  }"
                >
                  <div class="step-header" @click="toggleStep(seg)">
                    <span class="step-toggle">{{ isStepExpanded(seg) ? '▼' : '▶' }}</span>
                    <span
                      v-if="seg.status !== 'none'"
                      :class="['step-status-icon', `step-status-${seg.status}`]"
                    >{{ stepStatusIcon(seg.status) }}</span>
                    <span class="step-text">{{ seg.text }}</span>
                    <span v-if="seg.hasStreaming" class="step-streaming-tag">
                      <span class="typing-dots"><span></span><span></span><span></span></span>
                    </span>
                  </div>
                  <div v-if="isStepExpanded(seg)" class="step-body">
                    <!-- 该 step 下的所有迭代:不再折叠,内容直接平铺
                         (折叠单位上移到 step 组,浏览型工具已单行化) -->
                    <!-- 组内平铺消息(如用户追问/回答):渲染在迭代边界处(0 = 首个迭代之前) -->
                    <template v-for="p in plainsAfter(seg, 0)" :key="`plain-${p.item.id}`">
                      <div :class="{ 'user-msg-row': isUserMessageItem(p.item) }">
                        <ConversationMessage
                          :item="p.item"
                          @toggle-reasoning="toggleReasoning"
                        />
                      </div>
                    </template>
                    <template v-for="iter in seg.iterations" :key="iter.id">
                    <!-- 迭代内容直接平铺:无摘要行、无边框包装(wrapper 仅作结构容器) -->
                    <div class="iteration-block">
                      <div class="iteration-body">
                        <!-- thinking 项(流式或历史) -->
                        <ConversationMessage
                          v-for="t in iter.thinkingItems"
                          :key="t.id"
                          :item="t"
                          @toggle-reasoning="toggleReasoning"
                        />
                        <!-- 工具调用渲染行:compact 单行摘要 / agent 子智能体卡片 /
                             toolpair 普通工具卡片 / plain 兜底 -->
                        <template v-for="row in toolRowsOf(iter)" :key="row.key">
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
                                    @click.stop="onToolFileClick(row.filePath)"
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
                            <div class="tool-card-header" @click="toggleRow(row.callId)">
                              <span class="tool-group-toggle">{{ isRowExpanded(row.callId) ? '▼' : '▶' }}</span>
                              <span class="tool-card-title">{{ row.summary }}</span>
                            </div>
                            <div v-if="isRowExpanded(row.callId)" class="tool-card-body">
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
                              @toggle-reasoning="toggleReasoning"
                            />
                          </template>
                        </template>
                        <!-- 其他项(submit 等) -->
                        <ConversationMessage
                          v-for="o in iter.otherItems"
                          :key="o.id"
                          :item="o"
                          @toggle-reasoning="toggleReasoning"
                        />
                      </div>
                    </div>
                    <!-- 组内平铺消息(如用户追问):渲染在该迭代之后,与迭代内容保持时间顺序 -->
                    <template v-for="p in plainsAfter(seg, iter.iterationIdx)" :key="`plain-${p.item.id}`">
                      <div :class="{ 'user-msg-row': isUserMessageItem(p.item) }">
                        <ConversationMessage
                          :item="p.item"
                          @toggle-reasoning="toggleReasoning"
                        />
                      </div>
                    </template>
                    </template>
                  </div>
                </div>

                <!-- 结论段:该轮 agent1 最终回答,纯正文消息直接可见
                     (无标签无思考卡;最终思考留在上方过程组内) -->
                <ConversationMessage
                  v-else-if="seg.kind === 'conclusion'"
                  :item="seg.item"
                  @toggle-reasoning="toggleReasoning"
                />
              </template>
            </div>
          </div>
          <!-- 运行中等待提示(没有流式项时才显示) -->
          <!-- 优先用后端推送的 current_stage(如"正在克隆仓库..."),无则回退通用文案 -->
          <!-- 暂停态:不显示打字动画(已暂停,不再思考) -->
          <div
            v-if="isRunning && streamingItems.size === 0"
            class="waiting-hint"
            :class="{ 'waiting-hint-paused': isPaused }"
          >
            <template v-if="cloneProgress && !isPaused">
              <div class="clone-progress">
                <div class="clone-progress-info">
                  <span class="clone-progress-stage">
                    {{ task?.current_stage || '正在克隆仓库...' }}
                  </span>
                  <span class="clone-progress-percent">{{ cloneProgress.percent }}%</span>
                </div>
                <div class="clone-progress-bar">
                  <div
                    class="clone-progress-fill"
                    :style="{ width: cloneProgress.percent + '%' }"
                  ></div>
                </div>
                <div class="clone-progress-msg">{{ cloneProgress.message }}</div>
                <div class="clone-progress-actions">
                  <button
                    class="clone-skip-btn"
                    :disabled="skipClonePending"
                    :title="'跳过后改由执行阶段自主克隆(可能多耗时几十秒)'"
                    @click="handleSkipPreClone"
                  >{{ skipClonePending ? '正在跳过...' : '跳过预克隆' }}</button>
                </div>
              </div>
            </template>
            <template v-else>
              <span v-if="!isPaused" class="typing-dots">
                <span></span><span></span><span></span>
              </span>
              {{ isPaused ? '已暂停,点击恢复按钮继续执行' : (task?.current_stage || '智能体思考中...') }}
              <button
                v-if="!isPaused && isPreCloning"
                class="clone-skip-btn"
                :disabled="skipClonePending"
                :title="'跳过后改由执行阶段自主克隆(可能多耗时几十秒)'"
                @click="handleSkipPreClone"
              >{{ skipClonePending ? '正在跳过...' : '跳过预克隆' }}</button>
            </template>
          </div>
        </section>

        <!-- 工作区变更(任务完成时捕获的 git diff patch,只读;文本插值自动转义,无 XSS 风险) -->
        <section v-if="workspaceArtifact" class="workspace-changes-section">
          <div
            class="wc-header"
            @click="workspaceChangesCollapsed = !workspaceChangesCollapsed"
          >
            <h2>
              工作区变更
              <span class="count">({{ artifactMeta.files_changed }} 个文件)</span>
            </h2>
            <span class="wc-meta">
              {{ artifactMeta.char_count }} 字符
              <span v-if="artifactMeta.truncated" class="wc-truncated">
                · 已截断(仅查看,不可 git apply)
              </span>
            </span>
            <button
              class="wc-toggle"
              :title="workspaceChangesCollapsed ? '展开' : '折叠'"
            >
              {{ workspaceChangesCollapsed ? '▸' : '▾' }}
            </button>
          </div>
          <div v-if="!workspaceChangesCollapsed" class="diff-view">
            <div
              v-for="(line, i) in diffLines"
              :key="i"
              :id="diffAnchorByLine.get(i)"
              :class="['diff-line', diffLineClass(line)]"
            >{{ line }}</div>
          </div>
        </section>
      </template>
      </div>

      <!-- 运行时设置面板(输入框上方箭头展开:react/agent2 模型;
           运行中/暂停中修改在下一轮执行生效,组件内 toast 提示) -->
      <TaskRuntimeSettings
        v-if="task && (task.status === 'running' || task.status === 'paused' || task.status === 'completed' || task.status === 'failed')"
        :key="String(task.id)"
        :task="task"
        @saved="handleRuntimeConfigSaved"
      />

      <!-- 运行中发送的待处理消息(TRAE 式:输入框上方待处理条目,
           agent1 消费时经 conversation 事件转入对话流) -->
      <div
        v-if="task && (task.status === 'running' || task.status === 'paused') && pendingUserMessages.length"
        class="pending-messages"
      >
        <div
          v-for="msg in pendingUserMessages"
          :key="msg.id"
          class="pending-message-chip"
          :title="msg.content"
        >
          <svg
            class="pending-message-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <circle cx="12" cy="12" r="10" />
            <polyline points="12 6 12 12 16 14" />
          </svg>
          <span class="pending-message-text">{{ truncateInput(msg.content, 60) }}</span>
          <span class="pending-message-tag">待处理</span>
          <button
            class="pending-message-withdraw"
            title="撤回这条消息"
            :disabled="withdrawingIds.has(msg.id)"
            @click="handleWithdrawPendingMessage(msg.id)"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
      </div>

      <!-- 用户补充消息输入框(running/paused/completed 可见,pending 隐藏) -->
      <UserMessageInput
        v-if="task && (task.status === 'running' || task.status === 'paused' || task.status === 'completed')"
        :task-id="String(task.id)"
        :task-status="task.status"
        @sent="handleMessageSent"
        @error="handleMessageError"
      />

      <!-- 失败任务重试条(failed 状态替换输入框位置) -->
      <div v-if="task && task.status === 'failed'" class="retry-bar">
        <div class="retry-bar-text">
          <span class="retry-bar-label">任务执行失败,可重试</span>
          <span
            v-if="task.error_message"
            class="retry-bar-error"
            :title="task.error_message"
          >{{ truncateInput(task.error_message, 80) }}</span>
        </div>
        <button class="retry-btn" :disabled="retrying" @click="handleRetry">
          {{ retrying ? '重试启动中...' : '重试' }}
        </button>
      </div>
    </main>

    <!-- 右侧:核查与结果侧栏(抽屉把手式;展开时顶部条带标题+折叠按钮,折叠时悬浮把手在 page-body 右上角;
         宽度可经左缘手柄拖拽调整,双击复位) -->
    <aside
      v-if="task && !detailCollapsed"
      class="detail-sidebar"
      :class="{ 'detail-sidebar-resizing': detailSidebarResizing }"
      :style="detailSidebarStyle"
    >
      <!-- 桌面态左缘调宽手柄(窄屏覆盖抽屉态隐藏):悬停染主色提示可拖;
           可聚焦 separator 按 WAI-ARIA Window Splitter 暴露取值范围,双击复位由
           useResizableSidebar 在 pointerup 里自行判定(不依赖原生 dblclick) -->
      <div
        v-if="!detailSidebarNarrow"
        class="detail-resize-handle"
        role="separator"
        aria-orientation="vertical"
        :aria-valuenow="detailSidebarWidth"
        :aria-valuemin="detailSidebarMin"
        :aria-valuemax="detailSidebarMax"
        aria-label="调整核查与结果侧栏宽度"
        tabindex="0"
        title="拖动调整宽度,双击复位"
        @pointerdown="onDetailResizeStart"
        @keydown="onDetailResizeKeydown"
      />
      <div class="detail-sidebar-header">
        <span class="detail-sidebar-title">核查与结果</span>
        <!-- 状态徽标 + 下载/打印:自任务概览区块顶部迁入,排在折叠按钮左侧
             完成时间不再单列,改为悬浮徽标时以 title 展示 -->
        <span
          :class="['badge', statusConfig[task.status].class]"
          :title="task.completed_at ? '完成时间 ' + formatTime(task.completed_at) : undefined"
        >
          {{ statusConfig[task.status].label }}
        </span>
        <div
          v-if="task.status === 'completed' || task.results.length > 0"
          class="overview-actions"
        >
          <button
            class="btn-export"
            :disabled="exporting"
            title="下载 Markdown 报告"
            @click="exportMarkdown"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
          </button>
          <button
            class="btn-export"
            :disabled="exporting"
            title="打印或另存为 PDF"
            @click="exportPdf"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6 9 6 2 18 2 18 9" />
              <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" />
              <rect x="6" y="14" width="12" height="8" />
            </svg>
          </button>
        </div>
        <WorkspaceToggleButton
          side="right"
          :collapsed="false"
          expand-title="展开核查与结果"
          collapse-title="折叠核查与结果"
          @toggle="toggleDetail"
        />
      </div>
      <div class="detail-sidebar-body">
        <!-- 侧栏概览(精简后仅保留运行期实时阶段与错误提示):
             场景/创建时间/完成时间已移除——场景降级为模板无展示价值,
             创建时间主区标题行已有,完成时间改悬浮"已完成"徽章查看。
             当前阶段是运行/暂停态唯一的实时进度文案,故仅活跃期保留;
             任务进入终态(完成/失败)后收起,避免"任务完成,…"这类冗余收尾行。 -->
        <section
          v-if="(isRunning && task.current_stage) || task.error_message"
          class="overview-section"
        >
          <div v-if="isRunning && task.current_stage" class="overview-stage">
            <span class="label">当前阶段</span>
            <p>{{ task.current_stage }}</p>
          </div>
          <div v-if="task.error_message" class="alert alert-error">
            {{ task.error_message }}
          </div>
        </section>

        <!-- 任务清单(原主对话流"计划清单"卡迁入;复杂任务时 agent1 输出,
             最新一轮的计划与实时进度,随 SSE plan 事件/历史提取更新) -->
        <section v-if="latestPlanSteps.length > 0" class="plan-section">
          <h2 class="plan-section-title">
            任务清单
            <span class="plan-progress">{{ planProgress(latestPlanSteps) }}</span>
          </h2>
          <div class="plan-steps">
            <div
              v-for="s in latestPlanSteps"
              :key="s.id"
              :class="['plan-step', `plan-step-${s.status}`]"
            >
              <span class="plan-step-icon">{{
                s.status === 'done' ? '✓' : s.status === 'in_progress' ? '◌' : '○'
              }}</span>
              <span class="plan-step-text">{{ s.text }}</span>
            </div>
          </div>
        </section>

        <!-- 动态验证配置(仅当任务配了测试环境 URL 时显示)
             对用户透明:不出现 verifier_agent 字样,只显示"动态验证"。
             运行时可切换开关与授权模式,立即保存到后端。 -->
        <section v-if="task.test_env_url" class="verifier-section">
          <h2 class="verifier-title">
            动态验证
            <span
              :class="['verifier-status', verifierActive ? 'verifier-on' : 'verifier-off']"
            >{{ verifierActive ? '运行中' : '已关闭' }}</span>
          </h2>
          <div class="verifier-env">
            <span class="label">测试环境</span>
            <code :title="task.test_env_url">{{ task.test_env_url }}</code>
          </div>
          <button
            type="button"
            class="verifier-toggle-btn"
            :disabled="verifierConfigSaving"
            @click="toggleVerifierEnabled"
          >
            {{ task.verifier_enabled ? '关闭验证' : '开启验证' }}
          </button>
          <button
            v-if="task.verifier_enabled"
            type="button"
            class="verifier-toggle-btn"
            :disabled="verifierConfigSaving"
            @click="toggleVerifierAuthMode"
          >
            {{ task.verifier_auth_mode === 'direct' ? '模式:直接执行' : '模式:逐动作授权' }}
          </button>

          <!-- 已配置的登录凭证(只读展示,header_value 脱敏) -->
          <div
            v-if="task.verifier_enabled && task.verifier_auth_tokens && task.verifier_auth_tokens.length > 0"
            class="verifier-tokens"
          >
            <span class="label">登录凭证</span>
            <div
              v-for="(token, idx) in task.verifier_auth_tokens"
              :key="idx"
              class="verifier-token-item"
            >
              <span class="token-label">{{ token.label }}</span>
              <code class="token-header">{{ token.header_name }}: {{ maskTokenValue(token.header_value) }}</code>
            </div>
          </div>
        </section>

        <!-- 检查助手核查过程(思考/评估/工具核查/最终结论;主对话流只留追问卡) -->
        <Agent2Panel
          v-if="hasAgent2Activity"
          :conversations="task.conversations"
          :streaming-items="streamingItems"
          :is-running="isAgent2Running"
          :review-status="task.review_status"
          :stopping-review="stoppingReview"
          :review-summary="reviewSummary"
          :review-plan-live="reviewPlanLive"
          :review-items="reviewItems"
          @send-suggestion="handleSuggestionDig"
          @stop-review="handleStopReview"
        />

        <!-- 审查结果(ReviewItem):证据驱动可信审查,按三态分桶(风险/已核查/缺口) -->
        <section v-if="hasReviewResults" class="sidebar-review" data-onboarding="detail-review">
          <h2>
            审查结果 <span class="count">({{ reviewItems.length }})</span>
            <span v-if="task.review_status === 'running'" class="review-interim-hint">检查助手核查中</span>
          </h2>
          <div v-for="bucket in REVIEW_BUCKET_ORDER" :key="bucket.key" class="review-bucket">
            <template v-if="reviewBuckets[bucket.key].length">
              <h3 class="review-bucket-head">
                <span :class="['bucket-tag', `bucket-${bucket.key}`]">{{ bucket.label }}</span>
                <span class="count">{{ reviewBuckets[bucket.key].length }}</span>
              </h3>
              <article
                v-for="it in reviewBuckets[bucket.key]"
                :key="it.id"
                :class="['review-card', { 'review-card-expanded': expandedReviewItems.has(it.id) }]"
                @click="toggleReviewItem(it.id)"
              >
                <div class="review-header">
                  <span class="review-toggle">{{ expandedReviewItems.has(it.id) ? '▼' : '▶' }}</span>
                  <h4>{{ it.title }}</h4>
                  <span :class="['conf-badge', `conf-${confidenceClass(it.confidence?.tier)}`]">
                    {{ confidenceLabel(it.confidence?.tier) }} · {{ formatConfidenceScore(it.confidence?.score) }}
                  </span>
                  <span v-if="it.evidence_mismatch" class="conf-warn" title="有引用但后端核验未通过">⚠ 证据未核验</span>
                </div>
                <div v-if="it.severity || it.origin" class="review-meta">
                  <span class="origin-tag" :title="it.origin">{{ it.origin }}</span>
                  <span v-if="it.severity" :class="['sev-tag', `sev-${it.severity}`]">{{ it.severity }}</span>
                </div>
                <div v-if="expandedReviewItems.has(it.id)" class="review-body">
                  <p class="rv-line"><strong>被核实对象:</strong>{{ it.review_target || '—' }}</p>
                  <p v-if="it.description" class="rv-line"><strong>发现问题:</strong>{{ it.description }}</p>
                  <div v-if="it.evidence?.source?.quote || it.evidence?.source?.file_path" class="rv-section">
                    <strong>原始证据:</strong>
                    <code
                      v-if="it.evidence?.source?.file_path"
                      class="rv-file"
                      @click.stop="openReviewFile(it)"
                    >{{ it.evidence.source.file_path }}<span v-if="it.evidence.source.line">:{{ it.evidence.source.line }}</span></code>
                    <pre v-if="it.evidence?.source?.quote" class="rv-quote">{{ it.evidence.source.quote }}</pre>
                  </div>
                  <p v-if="it.evidence?.analysis_basis?.ref_url" class="rv-line">
                    <strong>分析依据:</strong>{{ it.evidence.analysis_basis.ref_url }}
                  </p>
                  <p v-if="it.evidence?.verification" class="rv-line">
                    <strong>验证测试:</strong>{{ it.evidence.verification.method || '—' }}
                    <span v-if="it.evidence.verification.poc_evidence"> · {{ it.evidence.verification.poc_evidence }}</span>
                  </p>
                  <p v-if="it.suggestion" class="rv-line"><strong>建议:</strong>{{ it.suggestion }}</p>
                </div>
              </article>
            </template>
          </div>
        </section>

        <!-- 重点与知识点(原"结果清单";分组由 task.params._grouping 驱动,卡片默认折叠;置底展示) -->
        <section
          v-if="task.results.length > 0"
          class="sidebar-results"
          data-onboarding="detail-results"
        >
          <h2>
            重点与知识点 <span class="count">({{ task.results.length }})</span>
            <!-- 后台审查进行中:当前为临时结果,审查完成后由检查助手整理的重点与知识点替换 -->
            <span
              v-if="task.review_status === 'running'"
              class="review-interim-hint"
              title="检查助手正在后台核查,当前为临时结果,完成后自动更新"
            >
              检查助手整理中
            </span>
            <!-- 用户终止检查:临时结果就是最终结果(不会再有知识点替换) -->
            <span
              v-else-if="task.review_status === 'stopped'"
              class="review-interim-hint is-stopped"
              title="已终止检查,保留 AI助手执行结果;如需检查可继续追问(新一轮会自动重新核查)"
            >
              检查已终止
            </span>
            <!-- 本任务的出题 job 运行中时,隐藏「生成练习题」入口,改为展示跳转练习页看实时进度 -->
            <button
              v-if="runningGenJob"
              class="gen-progress-entry"
              title="跳转到自适应练习查看出题进度"
              @click="goToPracticeProgress"
            >
              <span class="gen-pulse-dot" aria-hidden="true" />
              正在出题<template v-if="runningGenJob.total">({{ runningGenJob.done }}/{{ runningGenJob.total }})</template>
              · 查看进度
            </button>
            <button
              v-else-if="task.status === 'completed' && practiceEnabled"
              class="practice-generate-btn"
              :title="pendingDraftCount > 0 ? '存在待确认的候选题,点击预览入库' : '把审计发现改编为自适应练习题'"
              @click="openPracticeGenerate"
            >{{ pendingDraftCount > 0 ? `确认练习题(${pendingDraftCount})` : '生成练习题' }}</button>
          </h2>
          <!-- 出题入口旁:展示本次出题将使用的模型,避免用户困惑为什么没用默认模型 -->
          <p v-if="generateModelInfo && task.status === 'completed' && practiceEnabled" class="generate-model-hint">
            本次出题将使用：<strong class="generate-model-name">{{ generateModelInfo.model }}</strong>
            <span class="generate-model-source">（{{ generateModelSourceLabel }}）</span>
          </p>
          <template v-for="group in resultGroups" :key="group.key">
            <h3 v-if="resultGrouping" class="sidebar-result-group">
              <span :class="['severity-tag', `sev-${group.color}`]">{{ group.label }}</span>
              <span class="count">{{ group.results.length }}</span>
            </h3>
            <div class="result-cards">
              <article
                v-for="r in group.results"
                :key="r.id"
                :class="['result-card', { 'result-card-expanded': expandedResults.has(r.id) }]"
                @click="toggleResult(r.id)"
              >
                <div class="result-header">
                  <span class="result-toggle">{{ expandedResults.has(r.id) ? '▼' : '▶' }}</span>
                  <h4>{{ r.title }}</h4>
                  <!-- 学习点徽标(agent2 标记了 learning_note 的知识点) -->
                  <span
                    v-if="r.metadata_?.learning_note"
                    class="learning-badge"
                    title="检查助手标记的学习点"
                  >值得学</span>
                </div>
                <div v-if="getResultMetaItems(r).length > 0" class="result-meta">
                  <span
                    v-for="item in getResultMetaItems(r)"
                    :key="item.field.name"
                    :class="['meta-tag', { 'meta-file': item.field.type === 'file' }]"
                    @click.stop="item.field.type === 'file' ? onResultFileClick(r) : undefined"
                  >
                    {{ item.value }}
                  </span>
                </div>
                <div
                  v-if="expandedResults.has(r.id)"
                  class="result-content-wrapper"
                >
                  <!-- 学习点说明:知识点正文上方的引用块(agent2 提炼的学习价值) -->
                  <blockquote
                    v-if="r.metadata_?.learning_note"
                    class="learning-note"
                  >{{ r.metadata_.learning_note }}</blockquote>
                  <div
                    class="result-content markdown-body"
                    v-html="renderResultContent(r.content)"
                  />
                </div>
              </article>
            </div>
          </template>
        </section>
      </div>
    </aside>

    <!-- 折叠态:page-body 右上角悬浮把手(header 下方右侧,点击重新展开;有结果时显示数量角标) -->
    <div v-else-if="task" class="detail-handle">
      <WorkspaceToggleButton
        side="right"
        :collapsed="true"
        expand-title="展开核查与结果"
        collapse-title="折叠核查与结果"
        @toggle="toggleDetail"
      />
      <span v-if="task.results.length > 0" class="detail-handle-badge">
        {{ task.results.length }}
      </span>
    </div>
    </div>

    <!-- 验证动作授权弹窗(per_action 模式,每个 HTTP/PoC 动作需用户确认) -->
    <VerifyActionDialog
      :open="verifyActionOpen"
      :action="verifyActionData"
      :submitting="submittingVerifyAction"
      @approve="handleApproveVerifyAction"
      @reject="handleRejectVerifyAction"
    />

    <!-- 练习题生成预览对话框(结果清单发现 → LLM 出题 → 预览确认入库) -->
    <PracticeGenerateDialog
      v-if="task"
      :open="practiceDialogOpen"
      :task-id="String(task.id)"
      @close="practiceDialogOpen = false; refreshPracticeDraftCount()"
      @confirmed="practiceDialogOpen = false; refreshPracticeDraftCount()"
    />

    <!-- 危险命令确认弹窗(local 模式,危险命令需用户确认) -->
    <CommandConfirmDialog
      :open="!!commandConfirmData"
      :command="commandConfirmData"
      :submitting="submittingCommandConfirm"
      @approve="handleApproveCommand"
      @reject="handleRejectCommand"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  /* 手机地址栏伸缩兜底 */
  height: 100dvh;
  overflow: hidden;
  background: var(--color-bg);
}

.page-body {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
  overflow: hidden;
  position: relative; /* 给折叠态悬浮把手定位 */
}

.main {
  flex: 1;
  min-width: 0;
  max-width: var(--content-width);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

/* 可滚动内容区(结果清单 + 协作对话流) */
.main-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-6) var(--space-6) var(--space-12);
}

/* 用户补充消息(type=message):右对齐,与顶部 userDirective 一致 */
.user-msg-row {
  display: flex;
  justify-content: flex-end;
}

.user-msg-row :deep(.msg-group) {
  max-width: 80%;
}

/* ---- 右侧核查与结果栏(抽屉把手式;展开时顶部条+滚动内容区,折叠时不渲染) ---- */
.detail-sidebar {
  position: relative; /* 供左缘调宽手柄绝对定位 */
  flex-shrink: 0;
  /* 宽度由 useResizableSidebar 内联下发(拖拽 + localStorage 记忆);
     窄屏走下面的 @media 覆盖抽屉态 */
  min-width: 320px;
  /* 与左侧历史任务栏对称的分隔线:主区与侧栏同底色,不画线看不出边界 */
  border-left: 1px solid var(--color-border);
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
}

/* 左缘调宽手柄:常态只占位透明(分隔线已交代边界),悬停/聚焦染主色提示"这里可拖"。
   left:-3px 让热区压住分隔线两侧,不必像素级对准 */
.detail-resize-handle {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -3px;
  width: 6px;
  z-index: 6;
  cursor: col-resize;
  /* 触屏:阻止浏览器把 pointermove 当滚动手势接管 */
  touch-action: none;
  background: transparent;
  transition: background var(--transition-fast);
}

.detail-resize-handle:hover,
.detail-resize-handle:focus-visible {
  background: var(--color-primary-light);
}

/* 拖拽中:整栏禁选中(栏内全是报告正文,否则一路选蓝)+ 光标保持左右箭头 */
.detail-sidebar.detail-sidebar-resizing {
  user-select: none;
  cursor: col-resize;
}

/* 顶部条:标题 + 折叠按钮(header 下方,固定不滚) */
.detail-sidebar-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.detail-sidebar-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

/* 标题行内的状态徽标与导出按钮:紧凑不换行、不被压缩,
   窄屏下优先保标题截断 */
.detail-sidebar-header .badge {
  flex-shrink: 0;
  white-space: nowrap;
}

.detail-sidebar-header .overview-actions {
  flex-shrink: 0;
}

.detail-sidebar-header .btn-export {
  width: 28px;
  height: 28px;
}

/* 内容区:独立滚动,卡片间距用 gap 统一 */
.detail-sidebar-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}



/* ---- 折叠态悬浮把手(page-body 右上角,header 下方右侧) ---- */
.detail-handle {
  position: absolute;
  right: var(--space-3);
  top: var(--space-3);
  z-index: 5;
}

/* 折叠态结果数量角标(提示折叠栏内有结果) */
.detail-handle-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  color: var(--color-surface);
  background: var(--color-primary);
  border-radius: var(--radius-full);
  pointer-events: none;
}

/* ---- 响应式:窄屏下右侧栏改为覆盖式抽屉 ---- */
@media (max-width: 1024px) {
  .detail-sidebar {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    z-index: 20;
    width: min(420px, 85vw);
    box-shadow: var(--shadow-xl);
  }

  /* 抽屉态宽度归 CSS,拖宽没有意义(断点须与 useResizableSidebar 的 narrowMax 一致) */
  .detail-resize-handle {
    display: none;
  }
}

/* ---- 手机档:抽屉更宽、主区滚动内边距收紧 ---- */
@media (max-width: 640px) {
  .detail-sidebar {
    width: min(420px, 92vw);
  }

  .main-scroll {
    padding: var(--space-4) var(--space-3) var(--space-8);
  }
}

/* ---- 加载 / 错误状态 ---- */
.loading-state,
.error-state {
  text-align: center;
  padding: var(--space-16) var(--space-6);
  color: var(--color-text-secondary);
}

.spinner-lg {
  width: 40px;
  height: 40px;
  margin: 0 auto var(--space-4);
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* ---- 侧栏概览(扁平化) ---- */
.overview-section {
  padding: var(--space-2) 0;
}

/* 临时结果提示(后台审查进行中,结果清单标题行;呼吸点提示将自动更新) */
.review-interim-hint {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  margin-left: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
}

.review-interim-hint::before {
  content: '';
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: gen-pulse 1.4s ease-in-out infinite;
}

/* 检查已终止:临时结果就是最终结果,呼吸点静止(不再暗示"还在跑") */
.review-interim-hint.is-stopped::before {
  background: var(--color-text-secondary);
  animation: none;
}

/* 出题进度跳转入口(位于结果清单标题行,与「生成练习题」按钮互斥;呼吸红点提示运行中) */
.gen-progress-entry {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  margin-left: auto;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.gen-progress-entry:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.gen-progress-entry .gen-pulse-dot {
  position: absolute;
  top: 4px;
  right: 4px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: gen-pulse 1.4s ease-in-out infinite;
}

@keyframes gen-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.7); }
}

.overview-actions {
  display: flex;
  gap: var(--space-2);
}

.btn-export {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-export:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-export:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.overview-stage {
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.overview-stage .label {
  display: block;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin-bottom: var(--space-1);
}

.overview-stage p {
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 120px;
  overflow-y: auto;
}

/* ---- 状态徽章 ---- */
.badge {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  border-radius: var(--radius-full);
}

.badge-pending { background: var(--color-surface-alt); color: var(--color-text-secondary); }
.badge-running { background: var(--color-info-light); color: var(--color-info); }
.badge-paused { background: var(--color-warning-light); color: var(--color-warning); }
.badge-completed { background: var(--color-success-light); color: var(--color-success); }
.badge-failed { background: var(--color-danger-light); color: var(--color-danger); }

/* ---- 失败任务重试条(failed 状态替换底部补充消息输入框位置) ---- */
.retry-bar {
  width: 94%;
  margin: 0 auto var(--space-4);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-3);
  box-shadow: var(--shadow-md);
  background: var(--color-danger-light);
}

/* 运行中发送的待处理消息(TRAE 式:输入框上方待处理条目) */
.pending-messages {
  width: 94%;
  margin: 0 auto var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.pending-message-chip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-1) var(--space-2);
  background: var(--color-bg-secondary, var(--color-bg));
  color: var(--color-text-secondary);
  font-size: var(--fs-xs);
}

.pending-message-icon {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  opacity: 0.7;
}

.pending-message-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pending-message-tag {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  opacity: 0.75;
}

.pending-message-withdraw {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--color-text-secondary);
  opacity: 0.6;
  cursor: pointer;
}

.pending-message-withdraw svg {
  width: 11px;
  height: 11px;
}

.pending-message-withdraw:hover:not(:disabled) {
  opacity: 1;
  color: var(--color-danger);
  background: var(--color-danger-light);
}

.pending-message-withdraw:disabled {
  opacity: 0.3;
  cursor: default;
}

.retry-bar-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.retry-bar-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-danger);
}

.retry-bar-error {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.retry-btn {
  flex-shrink: 0;
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.retry-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.retry-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* ---- 提示 ---- */
.alert {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-top: var(--space-3);
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

/* ---- agent2 追问修正卡(主对话流唯一保留的检查助手内容) ---- */
.followup-card {
  padding: var(--space-3) var(--space-4) var(--space-2);
  background: var(--color-warning-light);
  border-left: 3px solid var(--color-warning);
  border-radius: var(--radius-md);
}

.followup-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
  font-size: var(--fs-xs);
}

.followup-card-icon {
  color: var(--color-warning);
}

.followup-card-title {
  font-weight: var(--fw-semibold);
  color: var(--color-warning);
}

.followup-card-sub {
  color: var(--color-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 通用 section ---- */
/* 对话流:无外框,直接铺在主区背景上(聊天式) */
.conversation-section {
  margin-bottom: var(--space-6);
}

/* 右侧栏分区标题("重点与知识点"区块;原"覆盖度"区块已随覆盖度清单功能移除) */
.sidebar-results h2 {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
}

/* 结果清单标题旁的「生成练习题」入口(仅任务完成后可用) */
.practice-generate-btn {
  margin-left: auto;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.practice-generate-btn:hover {
  color: var(--color-text-inverse);
  background: var(--color-primary);
}

/* 出题入口旁的「本次出题将使用」提示(位于结果清单标题行下方) */
.generate-model-hint {
  display: flex;
  align-items: baseline;
  gap: var(--space-1);
  margin: var(--space-2) 0 var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.generate-model-name {
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.generate-model-source {
  color: var(--color-text-muted);
}

/* 结果分组头(仅有分组声明时显示) */
.sidebar-result-group {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  margin: var(--space-3) 0 var(--space-2);
}

.count {
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
  font-size: var(--fs-sm);
}

/* ---- 动态验证配置(右侧栏,仅 test_env_url 存在时显示)---- */
.verifier-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.verifier-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.verifier-status {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  padding: 1px 8px;
  border-radius: var(--radius-full);
}

.verifier-on {
  background: var(--color-success-light);
  color: var(--color-success);
}

.verifier-off {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.verifier-env {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--fs-xs);
}

.verifier-env .label {
  color: var(--color-text-muted);
  font-weight: var(--fw-medium);
}

.verifier-env code {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  color: var(--color-text-secondary);
  word-break: break-all;
}

.verifier-toggle-btn {
  align-self: flex-start;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.verifier-toggle-btn:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.verifier-toggle-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 登录凭证列表(只读展示,header_value 脱敏) */
.verifier-tokens {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
  font-size: var(--fs-xs);
}

.verifier-tokens .label {
  color: var(--color-text-muted);
  font-weight: var(--fw-medium);
}

.verifier-token-item {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.verifier-token-item .token-label {
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.verifier-token-item .token-header {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  word-break: break-all;
}

/* ---- 结果清单(右侧栏,卡片默认折叠) ---- */
.severity-tag {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  border-radius: var(--radius-full);
}

.sev-critical { background: var(--color-sev-critical-bg); color: var(--color-sev-critical-fg); border: 1px solid var(--color-sev-critical-border); }
.sev-high { background: var(--color-danger-light); color: var(--color-danger); border: 1px solid var(--color-sev-high-border); }
.sev-medium { background: var(--color-warning-light); color: var(--color-warning); border: 1px solid var(--color-sev-medium-border); }
.sev-low { background: var(--color-sev-low-bg); color: var(--color-sev-low-fg); border: 1px solid var(--color-sev-low-border); }
.sev-info { background: var(--color-info-light); color: var(--color-info); border: 1px solid var(--color-sev-info-border); }
.sev-unknown { background: var(--color-surface-alt); color: var(--color-text-secondary); border: 1px solid var(--color-border); }

.result-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.result-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.result-card:hover {
  border-color: var(--color-border-strong);
}

.result-card-expanded {
  border-color: var(--color-border-strong);
}

.result-header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
}

/* 学习点徽标(agent2 标记 learning_note 的知识点卡片) */
.learning-badge {
  flex-shrink: 0;
  margin-top: 1px;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-full, 999px);
}

/* 学习点说明引用块(知识点正文上方) */
.learning-note {
  margin: 0 0 var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-primary-light);
  border-left: 3px solid var(--color-primary);
  border-radius: var(--radius-sm);
}

.result-toggle {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: var(--fs-xs);
  line-height: var(--lh-tight);
  color: var(--color-text-muted);
}

.result-header h4 {
  flex: 1;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
  word-break: break-word;
}

.result-content {
  margin-top: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  word-break: break-word;
}

/* markdown-body 容器覆盖 pre-wrap:marked 已处理换行 */
.result-content.markdown-body {
  white-space: normal;
}

/* 右侧栏窄宽:代码块横向滚动,避免撑破卡片 */
.result-content.markdown-body pre {
  overflow-x: auto;
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.meta-tag {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.meta-file {
  color: var(--color-primary);
  background: var(--color-primary-light);
  cursor: pointer;
  transition: filter var(--transition-fast);
}

.meta-file:hover {
  filter: brightness(0.95);
}

/* ---- 对话流 ---- */
/* 用户指令:右对齐气泡,像聊天界面的用户消息(消息卡头部已含"用户指令"标签) */
.user-directive {
  display: flex;
  justify-content: flex-end;
  margin-bottom: var(--space-6);
}

.user-directive :deep(.message) {
  max-width: 80%;
}

.round-group {
  margin-bottom: var(--space-6);
}

.round-group:last-child {
  margin-bottom: 0;
}

/* ---- 任务清单(右侧栏,原主对话流"计划清单"卡迁入) ---- */
.plan-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.plan-section-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.plan-progress {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-plan-progress);
  padding: var(--space-1) var(--space-2);
  background: var(--color-plan-progress-bg);
  border-radius: var(--radius-full);
}

.plan-steps {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.plan-step {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-sm);
  border-radius: var(--radius-sm);
  transition: background 0.15s ease;
}

.plan-step-icon {
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

.plan-step-text {
  flex: 1;
  word-break: break-word;
}

.plan-step-pending {
  color: var(--color-text-muted);
}

.plan-step-pending .plan-step-icon {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
}

.plan-step-in_progress {
  color: var(--color-text);
  background: var(--color-plan-active-bg);
}

.plan-step-in_progress .plan-step-icon {
  color: var(--color-plan-active-icon);
  background: var(--color-plan-active-icon-bg);
  animation: plan-step-pulse 1.5s ease-in-out infinite;
}

.plan-step-done {
  color: var(--color-text-secondary);
  text-decoration: line-through;
  text-decoration-color: var(--color-text-muted);
}

.plan-step-done .plan-step-icon {
  color: #fff;
  background: #10b981;
}

@keyframes plan-step-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.6; transform: scale(0.9); }
}

.messages {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-left: var(--space-4);
}

/* ---- step 分组(plan step,可折叠,内含多个迭代) ---- */
/* 左竖线已移除:状态色由 step header 内的状态图标(step-status-*)承载 */
.step-block {
  margin-bottom: var(--space-2);
}

.step-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: background 0.15s ease;
  user-select: none;
}

.step-header:hover {
  background: var(--color-surface-alt);
}

.step-toggle {
  flex-shrink: 0;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-align: center;
}

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

.step-status-pending {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
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

.step-text {
  flex: 1;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-done .step-text {
  color: var(--color-text-secondary);
  text-decoration: line-through;
  text-decoration-color: var(--color-text-muted);
}

.step-streaming-tag {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
}

.step-body {
  padding-left: var(--space-3);
  padding-top: var(--space-1);
  padding-bottom: var(--space-1);
}

/* ---- 迭代块(agent1 一次 ReAct 循环,结构容器:无摘要行、无边框包装) ----
   .iteration-block 为透明容器,仅承载 iteration-body 的间距 */
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

/* ---- 对话区头部 + 实时指示器(标题已移除,仅在运行时右对齐显示实时徽标) ---- */
.conv-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-6);
}

.conv-header-info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.conv-header-title {
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-header-time {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-danger);
  padding: var(--space-1) var(--space-3);
  background: var(--color-danger-light);
  border-radius: var(--radius-full);
}

/* 暂停徽标:橙色,带两条竖线图标(CSS 绘制,不用 emoji) */
.paused-indicator {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-warning);
  padding: var(--space-1) var(--space-3);
  background: var(--color-warning-light);
  border-radius: var(--radius-full);
}

.paused-bars {
  display: inline-block;
  width: 8px;
  height: 8px;
  /* 两条竖线 = 暂停符号,用线性渐变绘制 */
  background:
    linear-gradient(
      to right,
      var(--color-warning) 0,
      var(--color-warning) 2px,
      transparent 2px,
      transparent 3px,
      var(--color-warning) 3px,
      var(--color-warning) 5px,
      transparent 5px
    );
}

/* 暂停/恢复按钮:与实时徽标并排 */
.btn-pause {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-pause:hover:not(:disabled) {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.btn-pause:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.live-dot {
  width: 8px;
  height: 8px;
  background: var(--color-danger);
  border-radius: 50%;
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.85); }
}

/* ---- 运行中等待提示 ---- */
.waiting-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-4);
  padding: var(--space-3) var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
}

/* 暂停态:橙色提示,不闪烁 */
.waiting-hint-paused {
  color: var(--color-warning);
  background: var(--color-warning-light);
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

/* ---- 仓库克隆进度条 ---- */
.clone-progress {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: 100%;
}

.clone-progress-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--fs-sm);
}

.clone-progress-stage {
  color: var(--color-text-secondary);
}

.clone-progress-percent {
  color: var(--color-primary);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.clone-progress-bar {
  width: 100%;
  height: 6px;
  background: var(--color-border);
  border-radius: var(--radius-full, 999px);
  overflow: hidden;
}

.clone-progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: var(--radius-full, 999px);
  transition: width 0.3s ease;
}

.clone-progress-msg {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-family: var(--font-mono, monospace);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.clone-progress-actions {
  display: flex;
  justify-content: flex-end;
}

.clone-skip-btn {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md, 6px);
  padding: 2px 10px;
  cursor: pointer;
  transition: color 0.15s ease, border-color 0.15s ease;
}

.clone-skip-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.clone-skip-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* ---- 工作区变更(diff/patch 展示) ---- */
.workspace-changes-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-6);
  margin-bottom: var(--space-6);
}
.wc-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  cursor: pointer;
  user-select: none;
}
.wc-header h2 {
  font-size: var(--fs-lg);
  margin: 0;
}
.wc-meta {
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}
.wc-truncated {
  color: var(--color-warning);
}
.wc-toggle {
  margin-left: auto;
  background: none;
  border: none;
  color: var(--color-text-muted);
  cursor: pointer;
  font-size: var(--fs-sm);
  padding: 0 var(--space-1);
}
.diff-view {
  margin-top: var(--space-4);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  overflow-x: auto;
  max-height: 70vh;
}
.diff-line {
  white-space: pre;
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
}
.diff-line-add { background: rgba(22, 163, 74, 0.12); }
.diff-line-del { background: rgba(220, 38, 38, 0.12); }
.diff-line-hunk { color: var(--color-text-muted); }
.diff-line-meta { color: var(--color-text-secondary); font-weight: var(--fw-medium); }
.diff-line-ctx { color: var(--color-text); }

/* ---- 审查结果(ReviewItem):三态分桶 + 证据链 + 置信徽标 ---- */
.sidebar-review { margin-top: var(--space-4); }
.review-bucket { margin-top: var(--space-2); }
.review-bucket-head { display: flex; align-items: center; gap: var(--space-2); margin: var(--space-2) 0; }
.bucket-tag { font-size: var(--fs-sm); font-weight: var(--fw-semibold); padding: 1px 8px; border-radius: var(--radius-sm); }
.bucket-risk { background: var(--color-danger-light); color: var(--color-danger); }
.bucket-cleared { background: rgba(22, 163, 74, 0.12); color: var(--color-success, #16a34a); }
.bucket-gap { background: rgba(217, 119, 6, 0.14); color: var(--color-warning, #d97706); }
.review-card { border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-2) var(--space-3); margin-bottom: var(--space-2); cursor: pointer; }
.review-card:hover { background: var(--color-bg-hover, rgba(0,0,0,0.03)); }
.review-card-expanded { background: var(--color-bg-subtle, rgba(0,0,0,0.02)); }
.review-header { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }
.review-header h4 { margin: 0; flex: 1; font-size: var(--fs-sm); }
.review-toggle { color: var(--color-text-muted); font-size: var(--fs-xs); }
.conf-badge { font-size: var(--fs-xs); padding: 1px 6px; border-radius: var(--radius-sm); border: 1px solid var(--color-border); white-space: nowrap; }
.conf-verified { background: rgba(22,163,74,0.12); color: var(--color-success,#16a34a); }
.conf-source { background: var(--color-info-light, rgba(37,99,235,0.12)); color: var(--color-info,#2563eb); }
.conf-reference { background: rgba(124,58,237,0.12); color: #7c3aed; }
.conf-assertion { background: rgba(120,120,120,0.14); color: var(--color-text-secondary); }
.conf-warn { font-size: var(--fs-xs); color: var(--color-warning,#d97706); }
.review-meta { display: flex; gap: var(--space-2); margin-top: 4px; flex-wrap: wrap; }
.origin-tag { font-size: var(--fs-xs); color: var(--color-text-muted); border: 1px dashed var(--color-border); padding: 0 6px; border-radius: var(--radius-sm); }
.review-body { margin-top: var(--space-2); font-size: var(--fs-sm); color: var(--color-text); }
.rv-line { margin: 4px 0; }
.rv-section { margin: 6px 0; }
.rv-file { display: inline-block; margin: 2px 0; padding: 1px 6px; background: var(--color-bg-subtle, #f3f4f6); border-radius: var(--radius-sm); cursor: pointer; color: var(--color-info,#2563eb); }
.rv-quote { margin: 4px 0; padding: 6px 8px; background: var(--color-code-bg, #0b1021); color: inherit; border-radius: var(--radius-sm); white-space: pre-wrap; word-break: break-all; font-size: var(--fs-xs); }
</style>
