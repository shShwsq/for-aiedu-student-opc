<script setup lang="ts">
/**
 * 自适应练习页
 *
 * 首页(左侧目录 + 操作头 + 滚动段落,布局对齐设置页):
 * - 操作头一行常驻(在 .main 滚动区之外,无需 sticky):左动作(开始练习/出题进度/练习设置)
 *   + 右统计(能力估计/到期待复习/累计正确率/题库题数);统计的说明文案走悬浮 title
 * - 左侧目录 168px 三项锚点 + scrollspy:错题回顾 / 题库管理 / 历史记录
 *   (历史记录由 PracticeHistoryPanel 内嵌,原 /practice/history 重定向到 #history)
 * - 操作头与段落同在 content-column 里居中;左右两个抽屉仍是整页高(它们是列的兄弟节点)
 * - 知识点看板不在本页入口,走顶栏主导航(与「自适应练习」并列)
 *
 * 会话/局末态:操作头与目录全部不渲染,只保留 session-bar 与题目卡/本局统计。
 *
 * 组卷由后端 selector 完成(到期复习 > 薄弱点 > 难度匹配 > 新题),答案不下发。
 * 题目来源:审计任务详情页「生成练习题」产出并确认入库。
 * 带 ?topic=<知识点key> / ?learningTopic=<主题key> 进入时自动发起对应专项练习
 * (知识点看板卡片「专项练习」入口)。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import PracticeCodeSidebar from '@/components/PracticeCodeSidebar.vue'
import PracticeGenerateDialog from '@/components/PracticeGenerateDialog.vue'
import PracticeGenerateSidebar from '@/components/PracticeGenerateSidebar.vue'
import PracticeHistoryPanel from '@/components/PracticeHistoryPanel.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  activateQuestions,
  archiveQuestion,
  getPracticeStats,
  listGenerateJobs,
  listQuestions,
  startSession,
  submitAnswer,
} from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type {
  GenerateJobSummary,
  PracticeStats,
  QuestionListItem,
  SessionQuestion,
  SubmitAnswerResponse,
} from '@/types/practice'

const route = useRoute()

// ============================================================
// 历史任务侧栏(与首页/其他视图一致的折叠模式)
// ============================================================
/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// 出题进度侧栏(右侧):轮询 job 列表发现运行中出题(手动/自动),
// 实时进度与 LLM 流式输出由 PracticeGenerateSidebar 内部订阅 SSE 展示
// ============================================================
const genSidebarOpen = ref(false)
const generateJobs = ref<GenerateJobSummary[]>([])
let genPollTimer: ReturnType<typeof setInterval> | null = null
/** 已见过的 job id(新运行中 job 首次出现时自动展开侧栏) */
const seenJobIds = new Set<string>()

const hasRunningGenJob = computed(() =>
  generateJobs.value.some((j) => j.status === 'pending' || j.status === 'running'),
)

async function pollGenerateJobs(): Promise<void> {
  try {
    const res = await listGenerateJobs()
    generateJobs.value = res.jobs
    // 新运行中 job 首次出现 → 自动展开侧栏一次(后续由用户控制)
    for (const job of res.jobs) {
      if (!seenJobIds.has(job.job_id)) {
        seenJobIds.add(job.job_id)
        if (job.status === 'pending' || job.status === 'running') {
          genSidebarOpen.value = true
        }
      }
    }
  } catch {
    // 静默失败(如 PRACTICE_ENABLED=false 时路由未注册),保持空列表
  }
}

function toggleGenSidebar(): void {
  genSidebarOpen.value = !genSidebarOpen.value
}

// ---- 题目入库弹窗(复用任务详情页的 PracticeGenerateDialog)----
// 出题进度侧栏完成的 job 点「确认入库」→ 携带来源任务 id 打开预览勾选
const practiceDialogOpen = ref(false)
const practiceDialogTaskId = ref('')

function handleConfirmPreview(taskId: string): void {
  practiceDialogTaskId.value = taskId
  practiceDialogOpen.value = true
}

/** 弹窗关闭(无论是否确认):刷新题库与统计,让新入库的题即时可见 */
function handlePracticeDialogClose(): void {
  practiceDialogOpen.value = false
  loadQuestionBank()
  loadStats()
}

// ============================================================
// 源码查阅侧栏(右侧,仅答题会话):按当前题的 source_task_id 浏览工作区
// ============================================================
const codeSidebarOpen = ref(false)
/** 用户手动切换过的来源任务(切题时重置,默认跟随当前题) */
const codeTaskOverride = ref<string | null>(null)

/** 本局涉及的来源任务清单(去重,供侧栏下拉切换) */
const sessionTaskOptions = computed(() => {
  const seen = new Map<string, string>()
  for (const q of sessionQuestions.value) {
    if (q.source_task_id && !seen.has(q.source_task_id)) {
      seen.set(q.source_task_id, `任务 ${q.source_task_id.slice(0, 8)}`)
    }
  }
  return [...seen.entries()].map(([id, label]) => ({ id, label }))
})

/** 侧栏当前浏览的任务(手动切换优先,否则跟随当前题) */
const codeTaskId = computed<string | null>(
  () => codeTaskOverride.value ?? currentQuestion.value?.source_task_id ?? null,
)

/** 当前题的源码定位行号(取 source_lines 起始行) */
const locateLine = computed<number | null>(() => {
  const raw = currentQuestion.value?.source_lines
  if (!raw) return null
  const m = raw.match(/\d+/)
  return m ? Number(m[0]) : null
})

function toggleCodeSidebar(): void {
  codeSidebarOpen.value = !codeSidebarOpen.value
  // 同侧互斥:打开代码栏时收起出题进度栏
  if (codeSidebarOpen.value) genSidebarOpen.value = false
}

/** 点题目上的源码标签:打开侧栏并跟随当前题定位 */
function openCodeAtSource(): void {
  codeTaskOverride.value = null
  codeSidebarOpen.value = true
  genSidebarOpen.value = false
}

function handleSwitchCodeTask(taskId: string): void {
  codeTaskOverride.value = taskId
}

// ============================================================
// Toast(与其他视图一致的本地实现)
// ============================================================
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)
function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4000)
}

// ============================================================
// 界面状态机:home(首页) / session(答题中) / summary(本局统计)
// ============================================================
type ViewMode = 'home' | 'session' | 'summary'
const mode = ref<ViewMode>('home')

// ============================================================
// 左侧目录(仅首页):锚点跳转 + scrollspy 高亮
// ============================================================
/** 目录段顺序(锚点 id);段首越过滚动容器上边界即视为「已进入」 */
const SECTION_IDS = ['mistakes', 'bank', 'history'] as const
type SectionId = (typeof SECTION_IDS)[number]

const navItems: { id: SectionId; label: string }[] = [
  { id: 'mistakes', label: '错题回顾' },
  { id: 'bank', label: '题库管理' },
  { id: 'history', label: '历史记录' },
]

const activeSection = ref<SectionId>('mistakes')
/** 真正滚动的容器是 .main(overflow-y: auto),观察器必须以它为 root */
const mainRef = ref<HTMLElement | null>(null)
let sectionObserver: IntersectionObserver | null = null

/** 段首判定线:与 .anchor-section 的 scroll-margin-top 保持一致,避免高亮比滚动慢半拍 */
const SECTION_GAP = 16

function scrollToSection(id: SectionId): void {
  // 点击即刻置高亮,不等观察器回调(平滑滚动过程中避免闪烁)
  activeSection.value = id
  // 操作头在 .main 之外,不会遮挡段首,直接交给 scrollIntoView + scroll-margin-top
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 重算当前段:段首已到达判定线的段中取最靠下的那个(即正在阅读的段) */
function updateActiveSection(): void {
  const root = mainRef.value
  if (!root) return
  const rootTop = root.getBoundingClientRect().top
  let current: SectionId | null = null
  let currentTop = Number.NEGATIVE_INFINITY
  for (const id of SECTION_IDS) {
    const el = document.getElementById(id)
    if (!el) continue
    const top = el.getBoundingClientRect().top - rootTop
    if (top > SECTION_GAP) continue
    if (top > currentTop) {
      currentTop = top
      current = id
    }
  }
  activeSection.value = current ?? SECTION_IDS[0]
}

function setupSectionObserver(): void {
  teardownSectionObserver()
  const targets = SECTION_IDS.map((id) => document.getElementById(id)).filter(
    (el): el is HTMLElement => el !== null,
  )
  if (targets.length === 0) return
  sectionObserver = new IntersectionObserver(updateActiveSection, {
    root: mainRef.value,
    rootMargin: '0px 0px -70% 0px',
    threshold: 0,
  })
  for (const el of targets) sectionObserver.observe(el)
}

function teardownSectionObserver(): void {
  if (sectionObserver) {
    sectionObserver.disconnect()
    sectionObserver = null
  }
}

// ============================================================
// 首页:统计(以工具条徽章呈现;薄弱点全景在知识点看板页 /practice/board)
// ============================================================
const stats = ref<PracticeStats | null>(null)
const statsLoading = ref(true)
const statsError = ref('')

async function loadStats(): Promise<void> {
  statsLoading.value = true
  statsError.value = ''
  try {
    stats.value = await getPracticeStats()
  } catch (err) {
    statsError.value = extractErrorMessage(err)
  } finally {
    statsLoading.value = false
  }
}

/** 到期徽章悬浮说明:把原先占一整行的提示收进 tooltip */
const dueBadgeTip = computed(() => {
  if (!stats.value) return ''
  return stats.value.due_count > 0
    ? `有 ${stats.value.due_count} 个知识点到期待复习,本次组卷将优先安排复习题`
    : '暂无到期待复习的知识点'
})

// ============================================================
// 开始练习(题数由工具条气泡选;topic_filter 为空表示全部知识点)
// ============================================================
const sessionCount = ref(8)
const starting = ref(false)
const startError = ref('')

/** 题数选择气泡(点「开始练习」只开关气泡,不直接组卷) */
const countPopoverOpen = ref(false)
/** 气泡与触发按钮的共同容器,用于判定「点击外部关闭」 */
const startWrapRef = ref<HTMLElement | null>(null)

function toggleCountPopover(): void {
  countPopoverOpen.value = !countPopoverOpen.value
  if (countPopoverOpen.value) startError.value = ''
}

async function handlePopoverConfirm(): Promise<void> {
  await handleStartPractice()
  // 组卷失败时保留气泡(错误文案就在气泡底部),否则错误会跟着气泡一起看不见;
  // 成功时 mode 切到 session,由 watch(mode) 负责关
  if (!startError.value) countPopoverOpen.value = false
}

function handlePopoverMouseDown(e: MouseEvent): void {
  if (!countPopoverOpen.value) return
  if (startWrapRef.value && !startWrapRef.value.contains(e.target as Node)) {
    countPopoverOpen.value = false
  }
}

function handlePopoverKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') countPopoverOpen.value = false
}

/** 会话数据 */
const sessionId = ref('')
const sessionQuestions = ref<SessionQuestion[]>([])
const currentIndex = ref(0)
const currentQuestion = computed<SessionQuestion | null>(
  () => sessionQuestions.value[currentIndex.value] ?? null,
)

/** 本局作答结果(用于统计与回顾) */
const sessionResults = ref<{ question: SessionQuestion; correct: boolean }[]>([])

async function handleStartPractice(
  topicFilter?: string,
  questionIds?: string[],
  learningTopic?: string,
): Promise<void> {
  if (starting.value) return
  starting.value = true
  startError.value = ''
  try {
    const res = await startSession({
      count: questionIds?.length ?? sessionCount.value,
      topic_filter: topicFilter ?? null,
      learning_topic: learningTopic ?? null,
      question_ids: questionIds,
    })
    if (res.message) showToast(res.message, 'success')
    sessionId.value = res.session_id
    sessionQuestions.value = res.questions
    currentIndex.value = 0
    sessionResults.value = []
    chosenIdx.value = null
    feedback.value = null
    submitting.value = false
    codeTaskOverride.value = null
    mode.value = 'session'
  } catch (err) {
    startError.value = extractErrorMessage(err)
    // 气泡外的开局路径(错题重练 / 逐题重练 / ?topic 自动开局 / 再来一局)看不到
    // 气泡内的 inline 错误,失败时用 toast 兜底;气泡内仍走 inline 展示
    if (!countPopoverOpen.value) {
      showToast(startError.value, 'error')
    }
  } finally {
    starting.value = false
  }
}

// ============================================================
// 会话:作答
// ============================================================
const chosenIdx = ref<number | null>(null)
const submitting = ref(false)
const feedback = ref<SubmitAnswerResponse | null>(null)

const isLastQuestion = computed(
  () => currentIndex.value >= sessionQuestions.value.length - 1,
)

async function handleSubmit(): Promise<void> {
  const q = currentQuestion.value
  if (!q || chosenIdx.value === null || submitting.value || feedback.value) return
  submitting.value = true
  try {
    const res = await submitAnswer(sessionId.value, {
      question_id: q.id,
      chosen_idx: chosenIdx.value,
    })
    feedback.value = res
    sessionResults.value.push({ question: q, correct: res.is_correct })
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    submitting.value = false
  }
}

function handleNext(): void {
  if (isLastQuestion.value) {
    mode.value = 'summary'
    // 后台刷新统计,返回首页时展示最新掌握度
    loadStats()
    return
  }
  currentIndex.value += 1
  chosenIdx.value = null
  feedback.value = null
  // 切题后侧栏重新跟随当前题的来源任务
  codeTaskOverride.value = null
}

/** 提前结束本局(未完成全部题目) */
function handleQuitSession(): void {
  if (sessionResults.value.length === 0) {
    backToHome()
    return
  }
  mode.value = 'summary'
  loadStats()
}

function backToHome(): void {
  mode.value = 'home'
  sessionId.value = ''
  sessionQuestions.value = []
  sessionResults.value = []
  feedback.value = null
  codeSidebarOpen.value = false
  codeTaskOverride.value = null
  loadStats()
  loadQuestionBank()
  loadMistakes()
}

/** 本局正确率(summary 页展示) */
const sessionAccuracy = computed(() => {
  const total = sessionResults.value.length
  if (!total) return null
  return sessionResults.value.filter((r) => r.correct).length / total
})

// ============================================================
// 题库管理
// ============================================================
type BankFilter = 'active' | 'draft' | 'archived'
const bankFilter = ref<BankFilter>('active')
const bankQuestions = ref<QuestionListItem[]>([])
const bankLoading = ref(false)
const bankError = ref('')

async function loadQuestionBank(): Promise<void> {
  bankLoading.value = true
  bankError.value = ''
  try {
    bankQuestions.value = await listQuestions({ status: bankFilter.value })
  } catch (err) {
    bankError.value = extractErrorMessage(err)
  } finally {
    bankLoading.value = false
  }
}

function switchBankFilter(next: BankFilter): void {
  if (bankFilter.value === next) return
  bankFilter.value = next
  loadQuestionBank()
}

async function handleArchive(q: QuestionListItem): Promise<void> {
  try {
    await archiveQuestion(q.id)
    showToast('已归档,该题不再参与组卷', 'success')
    loadQuestionBank()
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  }
}

/** 待确认 draft 逐条转正(只转正该题,不影响其余 draft) */
async function handleActivate(q: QuestionListItem): Promise<void> {
  try {
    await activateQuestions({ question_ids: [q.id] })
    showToast('已转正入库,可参与组卷', 'success')
    loadQuestionBank()
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  }
}

// ============================================================
// 错题回顾(mistake=true: 答错过的 active 题)
// ============================================================
const mistakes = ref<QuestionListItem[]>([])
const mistakesLoading = ref(false)

async function loadMistakes(): Promise<void> {
  mistakesLoading.value = true
  try {
    mistakes.value = await listQuestions({ mistake: true })
  } catch {
    // 静默失败,错题区展示空态
    mistakes.value = []
  } finally {
    mistakesLoading.value = false
  }
}

/** 用全部错题白名单组一局 */
function handleStartMistakeSession(): void {
  if (mistakes.value.length === 0) return
  handleStartPractice(undefined, mistakes.value.map((q) => q.id))
}

// ============================================================
// 展示辅助
// ============================================================
function formatPercent(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleDateString('zh-CN')
  } catch {
    return iso
  }
}

/** 难度展示(保留 1 位小数,整数不带小数) */
function formatDifficulty(d: number): string {
  return Number.isInteger(d) ? String(d) : d.toFixed(1)
}

// 段落锚点随首页渲染才存在(且统计加载完成后才绘制三段),
// 模式/加载态变化后重建观察器;离开首页则释放
watch([mode, statsLoading], async () => {
  if (mode.value !== 'home') {
    teardownSectionObserver()
    return
  }
  await nextTick()
  setupSectionObserver()
})

// 离开首页(开局/局末)时题数气泡不留残;气泡浮在工具条上,模式切换后工具条不渲染
watch(mode, () => {
  countPopoverOpen.value = false
})

onMounted(async () => {
  const statsReady = loadStats()
  loadQuestionBank()
  loadMistakes()
  // 出题进度:进页先拉一次,之后每 5 秒轮询发现运行中 job
  pollGenerateJobs()
  genPollTimer = setInterval(pollGenerateJobs, 5000)
  document.addEventListener('mousedown', handlePopoverMouseDown)
  document.addEventListener('keydown', handlePopoverKeydown)

  // 知识点看板跳转:带 ?topic=<知识点key> 进入时自动发起该知识点的专项练习;
  // 带 ?learningTopic=<主题key> 进入时自动发起主题级练习(知识点/主题级互斥,topic 优先)
  const topic = route.query.topic
  if (typeof topic === 'string' && topic) {
    await handleStartPractice(topic)
    return
  }
  const learningTopic = route.query.learningTopic
  if (typeof learningTopic === 'string' && learningTopic) {
    await handleStartPractice(undefined, undefined, learningTopic)
    return
  }

  // 书签兼容:旧 /practice/history 重定向为 /practice#history,进首页后定位到对应段
  // (自动开局已在上面 return,不会与此抢位置)
  const hash = SECTION_IDS.find((id) => route.hash === `#${id}`)
  if (hash) {
    await statsReady
    await nextTick()
    scrollToSection(hash)
  }
})

onBeforeUnmount(() => {
  if (genPollTimer) {
    clearInterval(genPollTimer)
    genPollTimer = null
  }
  teardownSectionObserver()
  document.removeEventListener('mousedown', handlePopoverMouseDown)
  document.removeEventListener('keydown', handlePopoverKeydown)
})
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <!-- 目录 + 内容整体居中(与设置页 .settings-shell 同款),目录不再贴左边 -->
      <div class="practice-shell">
        <!-- ============ 左侧目录(仅首页:锚点跳转 + scrollspy 高亮) ============ -->
        <nav v-if="mode === 'home'" class="practice-nav" aria-label="练习页目录">
          <button
            v-for="item in navItems"
            :key="item.id"
            type="button"
            :class="['nav-item', { active: activeSection === item.id }]"
            @click="scrollToSection(item.id)"
          >
            <span class="nav-label">{{ item.label }}</span>
            <span class="nav-node" aria-hidden="true" />
          </button>
        </nav>

      <!-- 内容列:操作头常驻 + 段落滚动。右侧两个抽屉在本 shell 之外,保持原来的整页高度 -->
      <div class="content-column">
        <!-- ============ 首页操作头(左动作右统计,无背景;与段落同列宽) ============ -->
        <div v-if="mode === 'home'" class="practice-head">
          <div class="head-actions">
            <!-- 开始练习:点开展开题数气泡,确认后才组卷 -->
            <div ref="startWrapRef" class="start-wrap">
              <button
                class="btn-primary btn-small"
                :disabled="!stats || stats.active_question_count === 0 || starting"
                @click="toggleCountPopover"
              >
                开始练习
                <span class="start-caret" aria-hidden="true">▾</span>
              </button>

              <div v-if="countPopoverOpen" class="count-popover" role="group" aria-label="选择题数">
                <div class="popover-row">
                  <span class="popover-label">题数</span>
                  <div class="popover-modes">
                    <button
                      v-for="n in [4, 8, 12, 16]"
                      :key="n"
                      type="button"
                      :class="['mode-btn', { active: sessionCount === n }]"
                      @click="sessionCount = n"
                    >{{ n }}</button>
                  </div>
                </div>
                <button
                  class="btn-primary btn-small popover-confirm"
                  :disabled="starting"
                  @click="handlePopoverConfirm"
                >
                  {{ starting ? '组卷中...' : '开始练习' }}
                </button>
                <p v-if="startError" class="action-error">{{ startError }}</p>
              </div>
            </div>

            <button
              :class="['btn-ghost', 'btn-small', 'head-quiet-btn', { 'head-btn-active': genSidebarOpen }]"
              :title="genSidebarOpen ? '收起出题进度' : '查看出题进度'"
              @click="toggleGenSidebar"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
              </svg>
              出题进度
              <span v-if="hasRunningGenJob" class="gen-pulse-dot" aria-hidden="true" />
            </button>

            <RouterLink
              class="btn-ghost btn-small head-quiet-btn"
              title="练习设置"
              :to="{ name: 'settings-practice' }"
            >练习设置</RouterLink>
          </div>

          <!-- 统计:数值 + 标签,组间细竖线分隔;说明文案全部收进悬浮 title -->
          <div class="head-stats">
            <span class="stat" title="按作答历史估计的题目难度,越高说明能答越难的题">
              <span class="stat-num">{{ stats ? formatDifficulty(stats.ability) : '—' }}</span>
              <span class="stat-name">能力估计</span>
            </span>
            <span class="stat-sep" aria-hidden="true" />
            <span
              :class="['stat', { 'stat-alert': stats && stats.due_count > 0 }]"
              :title="dueBadgeTip"
            >
              <span class="stat-num">{{ stats ? stats.due_count : '—' }}</span>
              <span class="stat-name">到期待复习</span>
            </span>
            <span class="stat-sep" aria-hidden="true" />
            <span class="stat" :title="stats ? `累计 ${stats.total_attempts} 题` : ''">
              <span class="stat-num">{{ stats ? formatPercent(stats.accuracy) : '—' }}</span>
              <span class="stat-name">累计正确率</span>
            </span>
            <span class="stat-sep" aria-hidden="true" />
            <span
              class="stat"
              :title="stats && stats.draft_question_count > 0 ? `另有 ${stats.draft_question_count} 题待确认` : ''"
            >
              <span class="stat-num">{{ stats ? stats.active_question_count : '—' }}</span>
              <span class="stat-name">题库题数</span>
            </span>
          </div>
        </div>

      <main ref="mainRef" class="main">
      <!-- ============ 答题会话 ============ -->
      <template v-if="mode === 'session' && currentQuestion">
        <div class="session-bar">
          <button class="btn-ghost" @click="handleQuitSession">退出练习</button>
          <button
            :class="['btn-ghost', 'code-toggle-btn', { 'code-toggle-active': codeSidebarOpen }]"
            :disabled="sessionTaskOptions.length === 0"
            :title="sessionTaskOptions.length === 0
              ? '本局题目无来源任务,无法浏览代码'
              : (codeSidebarOpen ? '收起源码查阅' : '打开源码查阅,边看代码边答题')"
            @click="toggleCodeSidebar"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <polyline points="16 18 22 12 16 6" />
              <polyline points="8 6 2 12 8 18" />
            </svg>
            源码查阅
          </button>
          <div class="session-progress">
            <span class="session-progress-text">
              第 {{ currentIndex + 1 }} / {{ sessionQuestions.length }} 题
            </span>
            <div class="progress-track">
              <div
                class="progress-fill"
                :style="{ width: `${(currentIndex / sessionQuestions.length) * 100}%` }"
              />
            </div>
          </div>
        </div>

        <div class="question-card">
          <div class="question-tags">
            <span v-if="currentQuestion.knowledge_name" class="tag tag-kp">
              {{ currentQuestion.knowledge_name }}
            </span>
            <span
              v-for="lang in (currentQuestion.languages ?? [])"
              :key="lang"
              class="tag tag-lang"
            >{{ lang }}</span>
            <span v-if="currentQuestion.origin === 'synthetic'" class="tag tag-synthetic" title="智能体原创的虚构代码,脱离原仓库">改编</span>
            <span class="tag">{{ currentQuestion.qtype === 'true_false' ? '判断题' : '单选题' }}</span>
            <span class="tag">难度 {{ formatDifficulty(currentQuestion.difficulty) }}</span>
            <button
              v-if="currentQuestion.source_file"
              type="button"
              class="tag tag-source"
              :title="`打开右侧源码查阅并定位到 ${currentQuestion.source_file}`"
              @click="openCodeAtSource"
            >
              {{ currentQuestion.source_file }}<template v-if="currentQuestion.source_lines">:{{ currentQuestion.source_lines }}</template>
            </button>
          </div>

          <h2 class="question-stem">{{ currentQuestion.stem }}</h2>

          <pre v-if="currentQuestion.code_snippet" class="code-snippet"><code>{{ currentQuestion.code_snippet }}</code></pre>

          <div class="options">
            <button
              v-for="(opt, idx) in currentQuestion.options"
              :key="idx"
              type="button"
              :class="[
                'option',
                {
                  'option-selected': !feedback && chosenIdx === idx,
                  'option-correct': feedback && idx === feedback.correct_idx,
                  'option-wrong': feedback && chosenIdx === idx && !feedback.is_correct,
                  'option-dimmed': feedback && idx !== feedback.correct_idx && chosenIdx !== idx,
                },
              ]"
              :disabled="!!feedback"
              @click="chosenIdx = idx"
            >
              <span class="option-key">{{ String.fromCharCode(65 + idx) }}</span>
              <span class="option-text">{{ opt }}</span>
            </button>
          </div>

          <!-- 未作答:提交按钮;已作答:判分反馈 -->
          <div v-if="!feedback" class="question-actions">
            <button
              class="btn-primary"
              :disabled="chosenIdx === null || submitting"
              @click="handleSubmit"
            >
              {{ submitting ? '提交中...' : '提交答案' }}
            </button>
          </div>

          <div v-else :class="['feedback', feedback.is_correct ? 'feedback-correct' : 'feedback-wrong']">
            <div class="feedback-head">
              <span class="feedback-verdict">{{ feedback.is_correct ? '回答正确' : '回答错误' }}</span>
              <span v-if="feedback.state" class="feedback-kp">
                {{ feedback.state.knowledge_name }} · 正确率
                {{ formatPercent(feedback.state.accuracy) }}({{ feedback.state.correct_count }}/{{ feedback.state.attempts }})
                <template v-if="feedback.state.due_at"> · 下次复习 {{ formatDate(feedback.state.due_at) }}</template>
              </span>
            </div>
            <p v-if="feedback.explanation" class="feedback-explanation">{{ feedback.explanation }}</p>
            <div class="question-actions">
              <button class="btn-primary" @click="handleNext">
                {{ isLastQuestion ? '查看本局统计' : '下一题' }}
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- ============ 本局统计 ============ -->
      <template v-else-if="mode === 'summary'">
        <div class="summary-card">
          <h2>练习完成</h2>
          <div class="summary-grid">
            <div class="summary-item">
              <span class="summary-value">{{ sessionResults.length }}</span>
              <span class="summary-label">作答题数</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{{ sessionResults.filter((r) => r.correct).length }}</span>
              <span class="summary-label">答对</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{{ sessionResults.filter((r) => !r.correct).length }}</span>
              <span class="summary-label">答错</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{{ formatPercent(sessionAccuracy) }}</span>
              <span class="summary-label">正确率</span>
            </div>
          </div>

          <div v-if="sessionResults.length" class="summary-list">
            <div
              v-for="(r, idx) in sessionResults"
              :key="idx"
              :class="['summary-row', r.correct ? 'summary-row-correct' : 'summary-row-wrong']"
            >
              <span class="summary-row-mark">{{ r.correct ? '✓' : '✗' }}</span>
              <span class="summary-row-stem">{{ r.question.stem }}</span>
            </div>
          </div>

          <div class="summary-actions">
            <button class="btn-secondary" @click="backToHome">返回首页</button>
            <button class="btn-primary" :disabled="starting" @click="handleStartPractice()">
              再来一局
            </button>
          </div>
        </div>
      </template>

      <!-- ============ 首页(段落;操作头在 content-column 里常驻) ============ -->
      <template v-else>
        <p v-if="statsError" class="head-message head-message-error">
          统计加载失败: {{ statsError }}
          <button class="btn-link" @click="loadStats">重试</button>
        </p>
        <p v-else-if="!statsLoading && stats && stats.active_question_count === 0" class="head-message">
          题库为空 — 请到已完成审计任务的详情页,点「结果清单」旁的「生成练习题」把真实发现转成题目
        </p>

        <!-- 统计加载中:失败提示在上方,段落照常渲染(各自有数据源) -->
        <div v-if="statsLoading" class="placeholder">
          <span class="status-spinner" /> 加载中...
        </div>

        <template v-else>
          <!-- 错题回顾 -->
          <section id="mistakes" class="panel anchor-section">
            <div class="bank-head">
              <h2>错题回顾</h2>
              <button
                v-if="mistakes.length > 0"
                class="btn-secondary btn-small"
                :disabled="starting"
                title="用全部错题组一局重练"
                @click="handleStartMistakeSession"
              >错题重练({{ mistakes.length }})</button>
            </div>
            <div v-if="mistakesLoading" class="placeholder"><span class="status-spinner" /> 加载中...</div>
            <p v-else-if="mistakes.length === 0" class="panel-empty">
              暂无错题 — 答错的题目会自动收录到这里,可一键重练
            </p>
            <div v-else class="bank-list">
              <div v-for="q in mistakes" :key="q.id" class="bank-item">
                <div class="bank-item-main">
                  <span class="bank-stem">{{ q.stem }}</span>
                  <span class="bank-meta">
                    <span v-if="q.knowledge_name" class="tag tag-kp">{{ q.knowledge_name }}</span>
                    <span
                      v-for="lang in (q.languages ?? [])"
                      :key="lang"
                      class="tag tag-lang"
                    >{{ lang }}</span>
                    <span v-if="q.origin === 'synthetic'" class="tag tag-synthetic" title="智能体原创的虚构代码,脱离原仓库">改编</span>
                    <span>{{ q.qtype === 'true_false' ? '判断' : '单选' }}</span>
                    <span>作答 {{ q.attempts }} 次 · 正确率 {{ formatPercent(q.accuracy) }}</span>
                  </span>
                </div>
                <button
                  class="btn-secondary btn-small"
                  :disabled="starting"
                  title="只用这道题组一局"
                  @click="handleStartPractice(undefined, [q.id])"
                >重练</button>
              </div>
            </div>
          </section>

          <!-- 题库管理 -->
          <section id="bank" class="panel anchor-section">
            <div class="bank-head">
              <h2>题库管理</h2>
              <div class="bank-filters" role="group" aria-label="题库状态筛选">
                <button
                  v-for="f in ([['active', '已入库'], ['draft', '待确认'], ['archived', '已归档']] as [BankFilter, string][])"
                  :key="f[0]"
                  :class="['mode-btn', { active: bankFilter === f[0] }]"
                  @click="switchBankFilter(f[0])"
                >{{ f[1] }}</button>
              </div>
            </div>
            <div v-if="bankLoading" class="placeholder"><span class="status-spinner" /> 加载中...</div>
            <p v-else-if="bankError" class="action-error">{{ bankError }}</p>
            <p v-else-if="bankQuestions.length === 0" class="panel-empty">该状态下暂无题目</p>
            <div v-else class="bank-list">
              <div v-for="q in bankQuestions" :key="q.id" class="bank-item">
                <div class="bank-item-main">
                  <span class="bank-stem">{{ q.stem }}</span>
                  <span class="bank-meta">
                    <span v-if="q.knowledge_name" class="tag tag-kp">{{ q.knowledge_name }}</span>
                    <span
                      v-for="lang in (q.languages ?? [])"
                      :key="lang"
                      class="tag tag-lang"
                    >{{ lang }}</span>
                    <span v-if="q.origin === 'synthetic'" class="tag tag-synthetic" title="智能体原创的虚构代码,脱离原仓库">改编</span>
                    <span>{{ q.qtype === 'true_false' ? '判断' : '单选' }}</span>
                    <span>难度 {{ formatDifficulty(q.difficulty) }}</span>
                    <template v-if="q.attempts > 0">
                      <span>作答 {{ q.attempts }} 次 · 正确率 {{ formatPercent(q.accuracy) }}</span>
                    </template>
                    <span class="bank-date">{{ formatDate(q.created_at) }}</span>
                  </span>
                </div>
                <template v-if="q.status === 'draft'">
                  <button
                    class="btn-secondary btn-small"
                    title="直接转正入库,不影响其余候选题"
                    @click="handleActivate(q)"
                  >转正</button>
                </template>
                <button
                  v-if="q.status !== 'archived'"
                  class="btn-secondary btn-small"
                  title="归档后不再参与组卷"
                  @click="handleArchive(q)"
                >归档</button>
              </div>
            </div>
          </section>

          <!-- 历史记录(原 /practice/history 独立页内嵌为目录锚点段) -->
          <div id="history" class="anchor-section">
            <PracticeHistoryPanel @toast="showToast" />
          </div>
        </template>
      </template>
      </main>
      </div>
      </div>

      <PracticeCodeSidebar
        v-if="mode === 'session' && codeSidebarOpen"
        :task-id="codeTaskId"
        :task-options="sessionTaskOptions"
        :locate-file="currentQuestion?.source_file ?? null"
        :locate-line="locateLine"
        @close="codeSidebarOpen = false"
        @switch-task="handleSwitchCodeTask"
      />

      <PracticeGenerateSidebar
        v-if="genSidebarOpen && !(mode === 'session' && codeSidebarOpen)"
        :jobs="generateJobs"
        @close="genSidebarOpen = false"
        @confirm-preview="handleConfirmPreview"
      />
    </div>

    <!-- ============ 题目入库弹窗(出题进度侧栏入口,与任务详情页同款) ============ -->
    <PracticeGenerateDialog
      v-if="practiceDialogTaskId"
      :open="practiceDialogOpen"
      :task-id="practiceDialogTaskId"
      @close="handlePracticeDialogClose"
      @confirmed="handlePracticeDialogClose"
    />

    <!-- ============ 浮动提示 ============ -->
    <Teleport to="body">
      <Transition name="toast-slide">
        <div
          v-if="toast"
          :class="['toast-popup', toast.type === 'error' ? 'toast-error' : 'toast-success']"
          role="status"
          aria-live="polite"
        >
          <span class="toast-msg">{{ toast.msg }}</span>
        </div>
      </Transition>
    </Teleport>
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
}

.main {
  flex: 1;
  min-height: 0;
  min-width: 0;
  max-width: 860px;
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-8) var(--space-6);
}

/* ---- 目录 + 内容居中容器(照搬设置页:两侧留白自动均分,目录不贴左边缘) ---- */
.practice-shell {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-width: 0;
  max-width: 1180px;
  margin: 0 auto;
  overflow: hidden;
}

/* ============ 内容列(操作头常驻 + 段落滚动) ============ */
.content-column {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ============ 首页操作头(左动作右统计;无背景无边框,与段落同列宽) ============ */
/* 操作头与段落之间的下边距要落在头自身上(常驻不滚走),
   .main 的 padding-top 是滚动内容的一部分,滚下去就没了 */
.practice-head {
  flex-shrink: 0;
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
  padding: var(--space-3) var(--space-6) var(--space-4);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3) var(--space-5);
  flex-wrap: wrap;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* 次要动作:ghost 无边框;相对定位供「出题进度」呼吸红点,RouterLink 版去掉下划线 */
.head-quiet-btn {
  position: relative;
  text-decoration: none;
}

.head-btn-active {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

/* 统计簇:数值 + 标签,组间细竖线分隔(取代原先一排卡片 / 胶囊) */
.head-stats {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

/* 说明文案全部在悬浮 title 里,cursor 提示可悬停 */
.stat {
  display: inline-flex;
  align-items: baseline;
  gap: var(--space-1);
  cursor: help;
}

.stat-num {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}

.stat-name {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* 有到期复习项时数值标主色(沿用原统计卡 alert 语义) */
.stat-alert .stat-num {
  color: var(--color-primary);
}

.stat-sep {
  flex-shrink: 0;
  width: 1px;
  height: 12px;
  background: var(--color-border);
}

/* 非常驻的临时提示行(统计失败 / 题库为空),与段落同宽放在 .main 内 */
.head-message {
  margin: 0 0 var(--space-4);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.head-message-error {
  color: var(--color-danger);
}

/* ============ 左侧目录(锚点 + scrollspy,视觉对齐设置页导航) ============ */
.practice-nav {
  flex-shrink: 0;
  width: 168px;
  display: flex;
  flex-direction: column;
  padding: var(--space-6) var(--space-4);
  overflow-y: auto;
}

.nav-item {
  position: relative;
  display: block;
  padding: 10px 36px 10px var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  text-align: left;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast);
}

/* 节点间的连接竖线(位于目录项右侧,与节点同心) */
.nav-item::before {
  content: '';
  position: absolute;
  right: 20px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--color-border);
}

/* 首尾项竖线各截去一半,使线只在节点之间延伸 */
.nav-item:first-child::before {
  top: 50%;
}

.nav-item:last-child::before {
  bottom: 50%;
}

/* 状态节点:空心圆,当前段填充主色并带光环 */
.nav-node {
  position: absolute;
  right: 16px;
  top: 50%;
  transform: translateY(-50%);
  box-sizing: border-box;
  width: 10px;
  height: 10px;
  border: 2px solid var(--color-border-strong);
  border-radius: 50%;
  background: var(--color-surface);
  transition: all var(--transition-fast);
}

.nav-item:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.nav-item.active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  font-weight: var(--fw-semibold);
}

.nav-item.active .nav-node {
  border-color: var(--color-primary);
  background: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

/* 目录锚点段:滚到容器顶部时留一点呼吸 */
.anchor-section {
  scroll-margin-top: var(--space-4);
}

/* 「出题进度」有运行中 job 时的呼吸小红点(定位基准为 .head-quiet-btn) */
.gen-pulse-dot {
  position: absolute;
  top: 2px;
  right: 2px;
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

.placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.btn-link {
  background: none;
  border: none;
  padding: 4px 8px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
}

.btn-link:hover {
  text-decoration: underline;
}

/* ============ 通用面板 ============ */
.panel {
  padding: var(--space-5);
  margin-bottom: var(--space-5);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.panel h2 {
  margin: 0 0 var(--space-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.panel-empty {
  margin: 0;
  padding: var(--space-3) 0;
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

.action-error {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ============ 开始练习气泡(工具条内弹出) ============ */
.start-wrap {
  position: relative;
}

.start-caret {
  font-size: var(--fs-xs);
  opacity: 0.7;
}

.count-popover {
  position: absolute;
  top: calc(100% + var(--space-2));
  left: 0;
  z-index: 30;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.popover-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.popover-label {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.popover-modes {
  display: inline-flex;
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.popover-confirm {
  align-self: flex-start;
}

.count-popover .action-error {
  margin: 0;
}

/* ============ 题库管理 ============ */
.bank-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.bank-filters {
  display: inline-flex;
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.mode-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.mode-btn:hover:not(.active) {
  color: var(--color-text);
}

.mode-btn.active {
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: var(--shadow-sm);
}

.bank-list {
  display: flex;
  flex-direction: column;
}

.bank-item {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-border);
}

.bank-item:first-child {
  border-top: none;
}

.bank-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.bank-stem {
  font-size: var(--fs-sm);
  color: var(--color-text);
  line-height: var(--lh-base);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}

.bank-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.bank-date {
  margin-left: auto;
}

/* ============ 会话 ============ */
.session-bar {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

.session-progress {
  flex: 1;
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

/* 会话顶栏「源码查阅」切换按钮(打开时高亮;无来源任务时置灰) */
.code-toggle-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--fs-xs);
}

.code-toggle-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.code-toggle-active {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

/* 题目上的源码定位标签(可点击,打开代码栏并定位) */
.tag-source {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--font-mono);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: none;
  cursor: pointer;
}

.tag-source:hover {
  text-decoration: underline;
}

.session-progress-text {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.progress-track {
  flex: 1;
  height: 6px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: var(--radius-full);
  transition: width var(--transition-base);
}

.question-card {
  padding: var(--space-6);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.question-tags {
  display: flex;
  align-items: center;
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

/* 编程语言标签:低权重中性样式 */
.tag-lang {
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
}

/* 改编题标签:区分虚构代码来源 */
.tag-synthetic {
  color: var(--color-warning, #b45309);
  background: color-mix(in srgb, var(--color-warning, #b45309) 10%, transparent);
}

.question-stem {
  margin: 0 0 var(--space-4);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.code-snippet {
  margin: 0 0 var(--space-4);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

.options {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.option {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  text-align: left;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.option:hover:not(:disabled) {
  border-color: var(--color-primary);
}

.option-selected {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.option-correct {
  border-color: var(--color-success);
  background: var(--color-success-light);
}

.option-wrong {
  border-color: var(--color-danger);
  background: var(--color-danger-light);
}

.option-dimmed {
  opacity: 0.6;
}

.option:disabled {
  cursor: default;
}

.option-key {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.option-text {
  flex: 1;
  line-height: var(--lh-relaxed);
  word-break: break-word;
}

.question-actions {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-4);
}

.feedback {
  margin-top: var(--space-4);
  padding: var(--space-4);
  border-radius: var(--radius-md);
}

.feedback-correct {
  background: var(--color-success-light);
  border: 1px solid color-mix(in srgb, var(--color-success) 35%, transparent);
}

.feedback-wrong {
  background: var(--color-danger-light);
  border: 1px solid color-mix(in srgb, var(--color-danger) 35%, transparent);
}

.feedback-head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.feedback-verdict {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.feedback-correct .feedback-verdict {
  color: var(--color-success);
}

.feedback-wrong .feedback-verdict {
  color: var(--color-danger);
}

.feedback-kp {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.feedback-explanation {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

/* ============ 本局统计 ============ */
.summary-card {
  padding: var(--space-6);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}

.summary-card h2 {
  margin: 0 0 var(--space-4);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-3);
  margin-bottom: var(--space-5);
}

.summary-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.summary-value {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.summary-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.summary-list {
  display: flex;
  flex-direction: column;
  margin-bottom: var(--space-5);
}

.summary-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--color-border);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.summary-row:first-child {
  border-top: none;
}

.summary-row-mark {
  flex-shrink: 0;
  font-weight: var(--fw-semibold);
}

.summary-row-correct .summary-row-mark {
  color: var(--color-success);
}

.summary-row-wrong .summary-row-mark {
  color: var(--color-danger);
}

.summary-row-stem {
  line-height: var(--lh-base);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  line-clamp: 1;
  -webkit-box-orient: vertical;
}

.summary-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
}

/* ============ 按钮 ============ */
.btn-primary,
.btn-secondary,
.btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 36px;
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

.btn-ghost {
  background: transparent;
  color: var(--color-text-secondary);
}

.btn-ghost:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-small {
  height: 28px;
  padding: 0 var(--space-3);
  font-size: var(--fs-xs);
}

.btn-primary:disabled,
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* spinner */
.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ============ Toast ============ */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  max-width: 420px;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  box-shadow: var(--shadow-lg);
  border: 1px solid transparent;
}

.toast-success {
  background: var(--color-success-light);
  color: var(--color-success);
}

.toast-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

.toast-msg {
  word-break: break-word;
  white-space: pre-wrap;
}

.toast-slide-enter-active,
.toast-slide-leave-active {
  transition: opacity var(--transition-base), transform var(--transition-base);
}

.toast-slide-enter-from,
.toast-slide-leave-to {
  opacity: 0;
  transform: translate(-50%, -12px);
}

/* ---- 响应式:窄屏隐藏目录(退回「工具条 + 滚动」体验) ---- */
@media (max-width: 900px) {
  .practice-nav {
    display: none;
  }
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 640px) {
  .main {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  /* 操作头换行后动作与统计各自成行 */
  .practice-head {
    padding: var(--space-3) var(--space-3) var(--space-4);
    align-items: flex-start;
    row-gap: var(--space-3);
  }

  .head-actions {
    flex-wrap: wrap;
  }

  .head-stats {
    flex-wrap: wrap;
    gap: var(--space-2) var(--space-3);
  }

  /* 会话顶栏允许换行 */
  .session-bar {
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .question-card,
  .summary-card {
    padding: var(--space-4) var(--space-3);
  }

  /* 题目标签行允许换行(源码路径标签较长) */
  .question-tags {
    flex-wrap: wrap;
  }

  .tag-source {
    max-width: 100%;
  }

  /* 本局统计 4→2 列 */
  .summary-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: var(--space-2);
  }

  /* toast 不超出视口 */
  .toast-popup {
    max-width: calc(100vw - var(--space-6));
  }
}
</style>
