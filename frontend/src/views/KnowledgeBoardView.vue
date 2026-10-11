<script setup lang="ts">
/**
 * 知识点看板页(自适应练习的「知识点掌握全景」视图)
 *
 * - 两级结构:知识点按学习主题分组(内置 安全/架构/编码/合同 + 用户自定义),
 *   主题折叠区展开后是知识点卡片网格,组内保持后端排序
 *   (weak > due > mastered > learning > fresh)
 * - 每张卡片 = 一个知识点:SM-2 记忆状态 + 作答统计 + 题库题数 + 状态徽章
 *   + 一行紧凑的「查看讲解」入口(不再在卡片内就地展开占空间):点了在右侧栏
 *     查看 / 编辑该知识点讲解(Markdown,依据出题当时的材料与题目写成),
 *     右侧栏停靠可拖拽调宽(窄屏转覆盖式抽屉),编辑走 PUT .../explanation(source=manual)
 *   + 卡片底部「改主题」下拉:手工修正自动匹配出错的主题
 *     (PUT /practice/knowledge-points/{key}/topic,知识点级改动会级联其下题目,
 *      保存后整板刷新让卡片流入新分区)
 * - 区头「练这个主题」→ 跳 /practice?learningTopic=<key>,由练习页接管组卷;
 *   卡片「专项练习」→ 跳 /practice?topic=<key>(单知识点)
 * - 未知/已删除的主题 key 兜底「未分类」组排最后;停用主题照常成区
 *   (区头带「已停用」徽章,存量题不受影响,只是不再出新题)
 * - 布局对齐练习页/设置页:取消标题,第一行常驻操作头(去练习 + 知识点主题设置 +
 *   知识点数统计 + 批量更新讲解,在 .main 滚动区之外故不随内容滚动);左侧目录一个主题一项,
 *   点击即展开该区并平滑定位,scrollspy 高亮当前主题;知识点主题设置
 *   深链到 /settings/practice#learning-topics
 *
 * 讲解生成走异步 job(POST /practice/knowledge-points/explain → 轮询
 * GET /practice/generate/{job_id}):单请求最多 20 个知识点,后端再按
 * ≤MAX_KP_PER_BATCH(explainer.py)一次 LLM 调用分批;manual(用户自己写的)
 * 讲解不会被覆盖。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import CodeMirrorEditor from '@/components/CodeMirrorEditor.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  explainKnowledgePoints,
  getGenerateJob,
  listKnowledgePoints,
  listLearningTopics,
  saveKnowledgeExplanation,
  setKnowledgeTopic,
} from '@/api/practice'
import { buildStorageKey, useResizableSidebar } from '@/composables/useResizableSidebar'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/error'
import { formatDate } from '@/utils/practiceFormat'
import { renderMarkdown } from '@/utils/markdown'
import type {
  BoardStatus,
  KnowledgePointCard,
  LearningTopicDef,
} from '@/types/practice'

const router = useRouter()
const auth = useAuthStore()

// ============================================================
// 历史任务侧栏(与首页/其他视图一致的折叠模式)
// ============================================================
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// 看板数据(卡片 + 主题定义并行加载)
// ============================================================
const cards = ref<KnowledgePointCard[]>([])
const topics = ref<LearningTopicDef[]>([])
const loading = ref(true)
const loadError = ref('')

/** 未知/已删除主题 key 的兜底组标识(不参与主题级练习) */
const UNCLASSIFIED = '__unclassified__'

async function loadBoard(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [cardList, topicList] = await Promise.all([
      listKnowledgePoints(),
      listLearningTopics(),
    ])
    cards.value = cardList
    topics.value = topicList
  } catch (err) {
    loadError.value = extractErrorMessage(err)
    cards.value = []
    topics.value = []
  } finally {
    loading.value = false
  }
}

interface TopicChip {
  status: BoardStatus
  label: string
  count: number
}

interface TopicSection {
  topicKey: string
  name: string
  disabled: boolean
  cards: KnowledgePointCard[]
  chips: TopicChip[]
  defaultOpen: boolean
  hasQuestions: boolean
}

const CHIP_STATUSES: { status: BoardStatus; label: string }[] = [
  { status: 'weak', label: '薄弱' },
  { status: 'due', label: '待复习' },
  { status: 'mastered', label: '已巩固' },
  { status: 'fresh', label: '未开始' },
]

const STATUS_LABELS: Record<BoardStatus, string> = {
  weak: '薄弱',
  due: '待复习',
  mastered: '已巩固',
  learning: '学习中',
  fresh: '未开始',
}

/** 主题折叠区:按 learning_topic 分组,主题定义排序在前,未知 key 兜底最后 */
const sections = computed<TopicSection[]>(() => {
  const byTopic = new Map<string, KnowledgePointCard[]>()
  for (const c of cards.value) {
    const key = c.learning_topic || ''
    const bucket = byTopic.get(key)
    if (bucket) bucket.push(c)
    else byTopic.set(key, [c])
  }
  const result: TopicSection[] = []
  // 主题定义顺序(sort_order,内置在前)映射分组
  for (const t of topics.value) {
    const group = byTopic.get(t.key)
    if (!group || group.length === 0) continue // 0 卡主题不渲染空区
    byTopic.delete(t.key)
    result.push(buildSection(t.key, t.name, !t.enabled, group))
  }
  // 剩余为未知/已删除 key → 「未分类」兜底组排最后
  if (byTopic.size > 0) {
    const rest = [...byTopic.values()].flat()
    result.push(buildSection(UNCLASSIFIED, '未分类', false, rest))
  }
  return result
})

function buildSection(
  topicKey: string,
  name: string,
  disabled: boolean,
  group: KnowledgePointCard[],
): TopicSection {
  const counts = new Map<BoardStatus, number>()
  for (const c of group) {
    counts.set(c.board_status, (counts.get(c.board_status) ?? 0) + 1)
  }
  const chips: TopicChip[] = CHIP_STATUSES
    .map(({ status, label }) => ({ status, label, count: counts.get(status) ?? 0 }))
    .filter((chip) => chip.count > 0)
  return {
    topicKey,
    name,
    disabled,
    cards: group,
    chips,
    defaultOpen: (counts.get('weak') ?? 0) > 0 || (counts.get('due') ?? 0) > 0,
    hasQuestions: group.some((c) => c.question_count > 0),
  }
}

/** 卡片总数概览(操作头右侧统计) */
const totalCount = computed(() => cards.value.length)

// ============================================================
// 左侧目录(主题锚点 + scrollspy)+ 折叠区受控展开
// ============================================================
/** 真正滚动的容器是 .main,IntersectionObserver 必须以它为 root */
const mainRef = ref<HTMLElement | null>(null)
/** 当前高亮的主题 key(目录项) */
const activeKey = ref('')
/** 各折叠区展开态(按 topicKey):缺省用 defaultOpen,用户/目录点击可覆盖 */
const openMap = ref<Record<string, boolean>>({})
let sectionObserver: IntersectionObserver | null = null

/** 折叠区锚点 id(主题 key 可含下划线,统一加前缀保证合法且唯一) */
function sectionAnchor(topicKey: string): string {
  return `topic-${topicKey}`
}

/**
 * 展开目标主题区并平滑定位过去(目录点击 / 改主题后定位共用)
 *
 * 折叠区缺省只在含薄弱/待复习卡时展开;空区刚接收卡片时往往是收起态,
 * 其内卡片的「专项练习」入口也就点不到,故需显式展开再滚过去。
 */
function revealSection(topicKey: string): void {
  activeKey.value = topicKey
  openMap.value = { ...openMap.value, [topicKey]: true }
  nextTick(() => {
    document
      .getElementById(sectionAnchor(topicKey))
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

/** 目录点击:即刻高亮 + 展开目标区(收起态只滚到区头看不到卡片)+ 平滑滚动 */
function goSection(sec: TopicSection): void {
  revealSection(sec.topicKey)
}

/** <details> 原生开合(toggle 事件)同步回受控状态 */
function handleToggle(topicKey: string, e: Event): void {
  const open = (e.target as HTMLDetailsElement).open
  if (openMap.value[topicKey] !== open) {
    openMap.value = { ...openMap.value, [topicKey]: open }
  }
}

/** 段首判定线,与 .topic-section 的 scroll-margin-top 保持一致,避免高亮比滚动慢半拍 */
const SECTION_GAP = 16

/** 重算高亮:段首已越过判定线的区中取最靠下的那个(即正在阅读的区) */
function updateActive(): void {
  const root = mainRef.value
  if (!root || sections.value.length === 0) return
  const rootTop = root.getBoundingClientRect().top
  let current = sections.value[0].topicKey
  let currentTop = Number.NEGATIVE_INFINITY
  for (const sec of sections.value) {
    const el = document.getElementById(sectionAnchor(sec.topicKey))
    if (!el) continue
    const top = el.getBoundingClientRect().top - rootTop
    if (top > SECTION_GAP) continue
    if (top > currentTop) {
      currentTop = top
      current = sec.topicKey
    }
  }
  activeKey.value = current
}

function teardownObserver(): void {
  if (sectionObserver) {
    sectionObserver.disconnect()
    sectionObserver = null
  }
}

function setupObserver(): void {
  teardownObserver()
  if (!mainRef.value) return
  const targets = sections.value
    .map((sec) => document.getElementById(sectionAnchor(sec.topicKey)))
    .filter((el): el is HTMLElement => el !== null)
  if (targets.length === 0) return
  sectionObserver = new IntersectionObserver(updateActive, {
    root: mainRef.value,
    rootMargin: '0px 0px -70% 0px',
    threshold: 0,
  })
  for (const el of targets) sectionObserver.observe(el)
}

// 卡片/主题异步到达后 sections 才成形,board 也需 loading=false 才渲染;
// 监听两者变化:补齐新区缺省展开态、初始化高亮,并在渲染后重建观察器
watch(
  [loading, sections],
  ([ld, secs]) => {
    const next = { ...openMap.value }
    let changed = false
    for (const s of secs) {
      if (!(s.topicKey in next)) {
        next[s.topicKey] = s.defaultOpen
        changed = true
      }
    }
    if (changed) openMap.value = next
    if (!ld && !activeKey.value && secs.length) activeKey.value = secs[0].topicKey
    nextTick(setupObserver)
  },
  { immediate: true },
)

onBeforeUnmount(teardownObserver)

// ============================================================
// 交互
// ============================================================
/** 主题级练习:带 learningTopic 参数跳练习页,由其自动发起组卷 */
function startTopic(sec: TopicSection): void {
  router.push({ name: 'practice', query: { learningTopic: sec.topicKey } })
}

/** 专项练习:带 topic 参数跳练习页,由其自动发起该知识点的组卷 */
function startFocus(c: KnowledgePointCard): void {
  router.push({ name: 'practice', query: { topic: c.knowledge_key } })
}

// ============================================================
// 手工修正知识点所属主题(看板改主题)
// ============================================================
/** 正在改主题的知识点 key(该卡下拉置灰用) */
const topicBusyKey = ref('')
/** 改主题结果提示(操作头右侧展示,与讲解进度共用一行文本) */
const topicStatus = ref('')

/** 当前卡片的主题是否为已定义主题(否则为兜底「未分类」,需额外占位 option) */
function isKnownTopic(key: string): boolean {
  return topics.value.some((t) => t.key === key)
}

/**
 * 改知识点主题:成功后整板刷新让卡片流入新分区,并展开/定位到目标区
 *
 * 与当前主题相同则忽略(下拉初次渲染不会误触发);后端会级联更新该
 * 知识点下全部题目,故练习页/「练这个主题」会按新主题命中。
 * 刷新后目标区若无薄弱/待复习卡会缺省折叠,卡片连同「专项练习」入口
 * 都随之不可见,所以 revealSection 主动展开目标区并滚过去。
 */
async function changeTopic(c: KnowledgePointCard, event: Event): Promise<void> {
  const el = event.target as HTMLSelectElement
  const newKey = el.value
  if (!newKey || newKey === c.learning_topic) return
  topicBusyKey.value = c.knowledge_key
  topicStatus.value = ''
  try {
    await setKnowledgeTopic(c.knowledge_key, newKey)
    topicStatus.value = `已将「${c.knowledge_name}」移到新主题`
    await loadBoard()
    revealSection(newKey)
  } catch (err) {
    topicStatus.value = extractErrorMessage(err)
    // :value 绑定值未变时 Vue 不会重写 el.value,失败后需手动把下拉刷回真实主题
    el.value = c.learning_topic
  } finally {
    topicBusyKey.value = ''
    setTimeout(() => {
      if (!topicBusyKey.value) topicStatus.value = ''
    }, 6000)
  }
}

// ============================================================
// 知识点讲解(Markdown 展示 + 按需/批量生成)
// ============================================================
/** 单次请求的知识点数上限(与后端 ExplainKnowledgePointsRequest.max_length 一致) */
const EXPLAIN_BATCH_SIZE = 20
/** 轮询间隔(ms):讲解通常 1~3 次模型调用,轮询比接 SSE 更简单 */
const EXPLAIN_POLL_MS = 1500

/** 正在生成讲解的知识点 key(卡片级按钮置灰用;批量时为空串) */
const busyKey = ref('')
/** 讲解 job 进行中(含批量):屏蔽并发触发 */
const explaining = ref(false)
/** 讲解进度/失败提示文本(操作头右侧展示,不进滚动区) */
const explainStatus = ref('')

/**
 * 讲解 HTML 缓存:key → 渲染后的 Markdown
 *
 * 卡片数可能较多,逐次重渲染 marked 会浪费;这里随 cards 变化重算一次,
 * 模板只读成品 HTML。安全:renderMarkdown 已经过 DOMPurify 净化。
 */
const explanationHtml = computed<Map<string, string>>(
  () =>
    new Map(
      cards.value.map((c) => [c.knowledge_key, renderMarkdown(c.explanation)]),
    ),
)

function htmlOf(key: string): string {
  return explanationHtml.value.get(key) ?? ''
}

/** 讲解来源徽章文本(manual 额外提示不会被自动覆盖) */
function explainSourceLabel(source: string): string {
  if (source === 'manual') return '已编辑'
  if (source === 'auto') return 'AI'
  return ''
}

/** 缺讲解但有题可依据的知识点(批量生成候选) */
const missingExplanationKeys = computed<string[]>(() =>
  cards.value
    .filter((c) => c.can_generate_explanation && !c.explanation)
    .map((c) => c.knowledge_key),
)

/**
 * 轮询讲解 job 直到终态,返回实际写入的知识点数
 * 超时不无限等:抛友好提示,后端 TTL 内跑完的话下次刷新仍能看到结果
 */
async function waitExplainJob(jobId: string, timeoutMs = 180000): Promise<number> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const job = await getGenerateJob(jobId)
    if (job.status === 'done') return job.done
    if (job.status === 'error') throw new Error(job.error || '讲解生成失败')
    // 讲解 job 目前不提供给停止入口,但后端终态集合含 cancelled:
    // 不在这里跳出会一直轮询到超时
    if (job.status === 'cancelled') throw new Error('讲解生成已停止')
    if (Date.now() > deadline) throw new Error('讲解生成超时,请稍后刷新看板')
    explainStatus.value = `正在生成讲解 ${job.done}/${job.total || '…'}…`
    await new Promise((r) => setTimeout(r, EXPLAIN_POLL_MS))
  }
}

/**
 * 跑一段讲解 job 并把看板刷回最新
 *
 * force=true 时重写已有 AI 讲解(用户手改的那部分后端仍会跳过)。
 */
async function runExplain(keys: string[], force: boolean, label: string): Promise<void> {
  if (keys.length === 0) return
  explaining.value = true
  explainStatus.value = `正在${label}…`
  let written = 0
  try {
    for (let i = 0; i < keys.length; i += EXPLAIN_BATCH_SIZE) {
      const chunk = keys.slice(i, i + EXPLAIN_BATCH_SIZE)
      const { job_id: jobId } = await explainKnowledgePoints(chunk, force)
      written += await waitExplainJob(jobId)
    }
    explainStatus.value = `已更新 ${written} 条知识点讲解`
  } catch (err) {
    explainStatus.value = extractErrorMessage(err)
  } finally {
    explaining.value = false
    busyKey.value = ''
    await loadBoard()
    setTimeout(() => {
      if (!explaining.value) explainStatus.value = ''
    }, 6000)
  }
}

/** 单卡生成/更新讲解(force 取决于是否已有正文) */
function explainOne(c: KnowledgePointCard): void {
  if (explaining.value) return
  busyKey.value = c.knowledge_key
  void runExplain([c.knowledge_key], !!c.explanation, c.explanation ? '更新讲解' : '生成讲解')
}

/** 批量补全看板上缺讲解的知识点 */
function explainMissing(): void {
  void runExplain(missingExplanationKeys.value, false, `批量生成 ${missingExplanationKeys.value.length} 条讲解`)
}

// ============================================================
// 右侧栏:查看 / 编辑知识点讲解(停靠可拖拽;窄屏覆盖抽屉)
// ============================================================
/**
 * 停靠宽度:与任务详情右侧栏同款约束(min 320 / 窄屏 1024 转覆盖 / 上限收半屏),
 * 拖拽宽度按用户邮箱存 localStorage(双键口径与 buildStorageKey 一致)。
 */
const {
  inlineWidth: panelWidthStyle,
  resizing: panelResizing,
  isNarrow: panelNarrow,
  ariaValueNow: panelAriaNow,
  ariaValueMin: panelAriaMin,
  ariaValueMax: panelAriaMax,
  startResize: onPanelResizeStart,
  onResizeKeydown: onPanelResizeKeydown,
} = useResizableSidebar({
  min: 320,
  max: 640,
  defaultWidth: 440,
  narrowMax: 1024,
  maxViewportRatio: 0.5,
  storageKey: () => (auth.user ? buildStorageKey('knowledge-explain', auth.user.email) : null),
})

/** 当前在右侧栏查看的知识点 key(空 = 未选中) */
const selectedKey = ref('')
/** 右侧栏是否打开(点卡片「查看讲解」即开;关闭置 false) */
const panelOpen = ref(false)
/** 右栏展示态:view=渲染正文 / edit=CodeMirror 编辑 */
const panelMode = ref<'view' | 'edit'>('view')
/** 编辑态草稿(Markdown) */
const draft = ref('')
/** 保存中 */
const saving = ref(false)
/** 右栏错误提示(与操作头 explainStatus 分开,免得相互遮挡) */
const panelError = ref('')

/** 选中卡片(从 cards 派生:loadBoard / 保存回填后自动刷新讲解正文) */
const selectedCard = computed<KnowledgePointCard | null>(
  () => cards.value.find((c) => c.knowledge_key === selectedKey.value) ?? null,
)

/** 打开右栏查看某知识点讲解(切换卡片丢弃上一份未保存草稿,同题详情弹窗口径) */
function openExplainPanel(c: KnowledgePointCard): void {
  selectedKey.value = c.knowledge_key
  panelOpen.value = true
  panelMode.value = 'view'
  draft.value = ''
  panelError.value = ''
}

function closePanel(): void {
  panelOpen.value = false
  panelMode.value = 'view'
  draft.value = ''
  panelError.value = ''
}

function startEdit(): void {
  draft.value = selectedCard.value?.explanation ?? ''
  panelError.value = ''
  panelMode.value = 'edit'
}

function cancelEdit(): void {
  panelMode.value = 'view'
  draft.value = ''
  panelError.value = ''
}

/**
 * 保存手工编辑的讲解:成功后就地回填该卡片字段(不整板刷新)
 *
 * 写入后 source=manual,批量生成 / 「重新生成」都不再覆盖它(后端保证)。
 * 清空正文同样落库(后端把状态回退为「未生成」),回填后 htmlOf 缓存随之重算。
 */
async function saveEdit(): Promise<void> {
  const c = selectedCard.value
  if (!c || saving.value) return
  saving.value = true
  panelError.value = ''
  try {
    const updated = await saveKnowledgeExplanation(c.knowledge_key, draft.value)
    cards.value = cards.value.map((x) =>
      x.knowledge_key === updated.knowledge_key
        ? {
            ...x,
            explanation: updated.explanation,
            explanation_source: updated.explanation_source,
            explanation_model: updated.explanation_model,
            explanation_updated_at: updated.explanation_updated_at,
          }
        : x,
    )
    panelMode.value = 'view'
    draft.value = ''
  } catch (err) {
    panelError.value = extractErrorMessage(err)
  } finally {
    saving.value = false
  }
}

/** 对当前选中知识点生成 / 更新讲解(复用既有异步 job 路径,收尾 loadBoard 刷新右栏) */
function explainSelected(): void {
  if (selectedCard.value) explainOne(selectedCard.value)
}

/** 当前选中知识点发起专项练习 */
function focusSelected(): void {
  if (selectedCard.value) startFocus(selectedCard.value)
}

// ============================================================
// 展示辅助
// ============================================================
function statusLabel(status: BoardStatus): string {
  return STATUS_LABELS[status]
}

function formatPercent(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(0)}%`
}

/** 到期时间:已过期 N 天 / N 天后到期 */
function formatDue(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const diffDays = Math.ceil((d.getTime() - Date.now()) / 86400000)
  if (diffDays <= 0) return `已过期 ${-diffDays} 天`
  return `${diffDays} 天后到期`
}

onMounted(() => {
  loadBoard()
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

      <!-- 目录 + 内容整体居中(对齐练习页/设置页),目录不再贴左边缘 -->
      <div class="board-shell">
        <!-- 左侧目录:每个学习主题一项,点击展开并定位(scrollspy 高亮) -->
        <nav class="board-nav" aria-label="主题目录">
          <button
            v-for="sec in sections"
            :key="sec.topicKey"
            type="button"
            :class="['nav-item', { active: activeKey === sec.topicKey }]"
            :aria-current="activeKey === sec.topicKey ? 'location' : undefined"
            @click="goSection(sec)"
          >
            <span class="nav-label">{{ sec.name }}</span>
            <span class="nav-node" aria-hidden="true" />
          </button>
        </nav>

        <div class="content-column">
          <!-- 第一行操作头(常驻在滚动区之外):左动作 + 右统计 -->
          <div class="board-head">
            <div class="head-actions">
              <button class="btn-primary" title="进入自适应练习" @click="router.push({ name: 'practice' })">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M5 12h14" />
                  <path d="m12 5 7 7-7 7" />
                </svg>
                去练习
              </button>
              <RouterLink
                class="btn-ghost head-quiet-btn"
                title="知识点主题设置"
                :to="{ name: 'settings-practice', hash: '#learning-topics' }"
              >知识点主题设置</RouterLink>
              <button
                class="btn-ghost head-quiet-btn"
                :disabled="explaining || missingExplanationKeys.length === 0"
                :title="missingExplanationKeys.length === 0
                  ? '全部知识点都已有讲解'
                  : `为 ${missingExplanationKeys.length} 个尚无讲解的知识点生成讲解(依据出题时的材料与题目)`"
                @click="explainMissing"
              >{{ explaining ? '讲解生成中…' : `批量生成讲解(${missingExplanationKeys.length})` }}</button>
            </div>
            <div class="head-stats">
              <span v-if="explainStatus" class="explain-status">{{ explainStatus }}</span>
              <span v-if="topicStatus" class="explain-status">{{ topicStatus }}</span>
              <span class="stat" title="按学习主题分组;点区头可练整个主题,点卡片可练单个知识点">
                <span class="stat-num">{{ totalCount }}</span>
                <span class="stat-name">知识点</span>
              </span>
            </div>
          </div>

          <main ref="mainRef" class="main">
            <div v-if="loading" class="placeholder"><span class="status-spinner" /> 加载中...</div>
            <div v-else-if="loadError" class="placeholder error-text">
              加载失败: {{ loadError }}
              <button class="btn-link" @click="loadBoard">重试</button>
            </div>
            <p v-else-if="totalCount === 0" class="panel-empty">
              暂无知识点 — 到已完成审计任务的详情页生成并确认练习题后,知识点卡片会出现在这里
            </p>

            <!-- 主题折叠区:每区一个学习主题,区内知识点卡片网格 -->
            <div v-else class="board">
              <details
                v-for="sec in sections"
                :key="sec.topicKey"
                :id="sectionAnchor(sec.topicKey)"
                :open="openMap[sec.topicKey]"
                class="topic-section"
                @toggle="handleToggle(sec.topicKey, $event)"
              >
                <summary class="topic-head">
                  <span class="topic-name">
                    <svg class="chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="m9 18 6-6-6-6" />
                    </svg>
                    {{ sec.name }}
                    <span v-if="sec.disabled" class="badge badge-disabled" title="该主题已停用:不再出新题,已有题目不受影响">已停用</span>
                  </span>
                  <span class="topic-chips">
                    <span
                      v-for="chip in sec.chips"
                      :key="chip.status"
                      :class="['chip', `chip-${chip.status}`]"
                    >{{ chip.label }} {{ chip.count }}</span>
                  </span>
                  <span class="topic-meta">
                    <span class="topic-count">{{ sec.cards.length }} 个知识点</span>
                    <button
                      class="btn-secondary btn-small"
                      :disabled="!sec.hasQuestions || sec.topicKey === UNCLASSIFIED"
                      :title="sec.topicKey === UNCLASSIFIED ? '未知主题,无法发起主题练习' : (!sec.hasQuestions ? '该主题暂无入库题目' : `只练习「${sec.name}」主题的题目`)"
                      @click.stop.prevent="startTopic(sec)"
                    >练这个主题</button>
                  </span>
                </summary>

                <div class="kp-grid">
                  <article
                    v-for="c in sec.cards"
                    :key="c.knowledge_key"
                    :class="[
                      'kp-card',
                      { 'kp-card-weak': c.board_status === 'weak' },
                      { 'kp-card-selected': panelOpen && selectedKey === c.knowledge_key },
                    ]"
                  >
                    <div class="kp-head">
                      <span class="kp-name" :title="c.knowledge_name">{{ c.knowledge_name }}</span>
                      <span :class="['status-badge', `status-${c.board_status}`]">{{ statusLabel(c.board_status) }}</span>
                    </div>
                    <div class="kp-keyline">
                      <span class="kp-key">{{ c.knowledge_key }}</span>
                    </div>
                    <div v-if="c.languages.length > 0" class="kp-tags">
                      <span v-for="lang in c.languages" :key="lang" class="tag tag-lang">{{ lang }}</span>
                    </div>
                    <div class="kp-stats">
                      <span v-if="c.attempts > 0">
                        正确率 {{ formatPercent(c.accuracy) }} · {{ c.attempts }} 次作答
                      </span>
                      <span v-else class="kp-muted">尚未作答</span>
                      <span v-if="c.board_status === 'due'" class="kp-due">{{ formatDue(c.due_at) }}</span>
                    </div>
                    <div class="kp-foot">
                      <span class="kp-count">{{ c.question_count }} 道题</span>
                      <button
                        class="btn-secondary btn-small"
                        :disabled="c.question_count === 0"
                        :title="c.question_count === 0 ? '该知识点暂无入库题目' : `只练习「${c.knowledge_name}」的题目`"
                        @click="startFocus(c)"
                      >专项练习</button>
                      <select
                        class="kp-topic-select"
                        :value="c.learning_topic"
                        :disabled="topicBusyKey === c.knowledge_key"
                        title="修改所属主题(会连带更新该知识点下题目的主题)"
                        @change="changeTopic(c, $event)"
                      >
                        <option
                          v-if="!isKnownTopic(c.learning_topic)"
                          :value="c.learning_topic"
                          disabled
                        >未分类</option>
                        <option v-for="t in topics" :key="t.key" :value="t.key">
                          {{ t.name }}{{ t.enabled ? '' : '(已停用)' }}
                        </option>
                      </select>
                    </div>

                    <!-- 知识点讲解:卡片只留一行紧凑入口,正文改到右侧栏查看 / 编辑 -->
                    <div class="kp-explain-cta">
                      <span
                        v-if="explainSourceLabel(c.explanation_source)"
                        :class="['explain-badge', c.explanation_source === 'manual' ? 'explain-badge-manual' : 'explain-badge-auto']"
                        :title="c.explanation_source === 'manual'
                          ? '你自己编辑过:自动生成不会覆盖'
                          : `模型生成${c.explanation_model ? '(' + c.explanation_model + ')' : ''}`"
                      >{{ explainSourceLabel(c.explanation_source) }}</span>
                      <span v-else class="kp-muted kp-no-explain">{{ c.can_generate_explanation ? '可生成讲解' : '暂无讲解' }}</span>
                      <button
                        class="btn-secondary kp-open-btn"
                        title="在右侧查看 / 编辑知识点讲解"
                        @click="openExplainPanel(c)"
                      >{{ panelOpen && selectedKey === c.knowledge_key ? '已打开' : '查看讲解' }}</button>
                    </div>
                  </article>
                </div>
              </details>
            </div>
          </main>
        </div>
      </div>

      <!-- 右侧栏:知识点讲解查看 / 编辑(停靠可拖拽;窄屏覆盖抽屉) -->
      <aside
        v-if="panelOpen"
        class="explain-panel"
        :class="{ 'panel-narrow': panelNarrow, 'panel-resizing': panelResizing }"
        :style="panelWidthStyle"
      >
        <!-- 左缘调宽手柄(桌面态;窄屏覆盖态隐藏):拖动调宽 / 双击复位 / 键盘微调 -->
        <div
          v-if="!panelNarrow"
          class="explain-resize-handle"
          role="separator"
          aria-orientation="vertical"
          :aria-valuenow="panelAriaNow"
          :aria-valuemin="panelAriaMin"
          :aria-valuemax="panelAriaMax"
          aria-label="调整讲解面板宽度"
          tabindex="0"
          title="拖动调整宽度，双击复位"
          @pointerdown="onPanelResizeStart"
          @keydown="onPanelResizeKeydown"
        />

        <!-- 栏头：知识点名 + 状态 + 关闭 -->
        <div class="panel-header">
          <div class="panel-title">
            <span class="panel-kp-name" :title="selectedCard?.knowledge_name">{{ selectedCard?.knowledge_name }}</span>
            <span
              v-if="selectedCard"
              :class="['status-badge', `status-${selectedCard.board_status}`]"
            >{{ statusLabel(selectedCard.board_status) }}</span>
          </div>
          <button class="panel-close" aria-label="关闭讲解面板" @click="closePanel">×</button>
        </div>

        <!-- 副头：key + 来源 + 更新于 + 错误 -->
        <div class="panel-subhead">
          <span class="panel-kp-key">{{ selectedCard?.knowledge_key }}</span>
          <span
            v-if="selectedCard && explainSourceLabel(selectedCard.explanation_source)"
            :class="['explain-badge', selectedCard.explanation_source === 'manual' ? 'explain-badge-manual' : 'explain-badge-auto']"
            :title="selectedCard.explanation_source === 'manual'
              ? '你自己编辑过：自动生成不会覆盖'
              : `模型生成${selectedCard.explanation_model ? '(' + selectedCard.explanation_model + ')' : ''}`"
          >{{ explainSourceLabel(selectedCard.explanation_source) }}</span>
          <span v-if="selectedCard?.explanation_updated_at" class="panel-time">更新于 {{ formatDate(selectedCard.explanation_updated_at) }}</span>
          <span v-if="panelError" class="panel-error">{{ panelError }}</span>
        </div>

        <!-- 正文：预览 / 编辑 -->
        <div class="panel-body" :class="{ 'panel-body-edit': panelMode === 'edit' }">
          <div v-if="panelMode === 'view'" class="panel-view">
            <div v-if="selectedCard?.explanation" class="markdown-body panel-preview" v-html="htmlOf(selectedKey)" />
            <p v-else class="panel-empty">还没有讲解 — 「生成讲解」会依据出题当时的材料与题目写成，也可以直接「编辑」自己写一段。</p>
          </div>
          <CodeMirrorEditor
            v-else
            v-model="draft"
            class="panel-editor"
            placeholder="用 Markdown 编辑这段知识点讲解…"
          />
        </div>

        <!-- 底栏动作 -->
        <div class="panel-footer">
          <template v-if="panelMode === 'view'">
            <button class="btn-secondary" @click="startEdit">编辑</button>
            <button
              class="btn-secondary"
              :disabled="explaining || !selectedCard"
              :title="explaining
                ? '讲解生成中，请等待当前任务完成'
                : (selectedCard?.explanation
                  ? '重新生成这段讲解（手工编辑的讲解不会被自动覆盖）'
                  : '依据出题材料与题目生成一段讲解')"
              @click="explainSelected"
            >{{ busyKey === selectedKey ? '生成中…' : (selectedCard?.explanation ? '重新生成' : '生成讲解') }}</button>
            <button
              class="btn-secondary"
              :disabled="!selectedCard || selectedCard.question_count === 0"
              title="只练习该知识点的题目"
              @click="focusSelected"
            >专项练习</button>
          </template>
          <template v-else>
            <button class="btn-primary" :disabled="saving" @click="saveEdit">{{ saving ? '保存中…' : '保存' }}</button>
            <button class="btn-secondary" :disabled="saving" @click="cancelEdit">取消</button>
            <span class="panel-hint">保存后来源标记为「已编辑」，自动生成不再覆盖</span>
          </template>
        </div>
      </aside>
    </div>
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

/* ---- 目录 + 内容居中容器(照搬练习页:两侧留白自动均分,目录不贴左边缘) ---- */
.board-shell {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-width: 0;
  max-width: 1180px;
  margin: 0 auto;
  overflow: hidden;
}

/* 内容列:操作头常驻 + 卡片滚动 */
.content-column {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.main {
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-6) var(--space-8);
}

/* ============ 第一行操作头(左动作右统计;透明无边框) ============ */
/* 操作头在 .main 之外,不随卡片滚动 → 常驻第一行,无需 sticky */
.board-head {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3) var(--space-5);
  flex-wrap: wrap;
  padding: var(--space-4) var(--space-6) var(--space-2);
}

.head-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* 次要动作(RouterLink 版去掉下划线) */
.head-quiet-btn {
  text-decoration: none;
}

/* 统计簇:数值 + 标签;说明文案收进悬浮 title */
.head-stats {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

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

/* ============ 左侧目录(主题锚点 + scrollspy,视觉对齐练习页/设置页导航) ============ */
.board-nav {
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

/* 状态节点:空心圆,当前项填充主色并带光环 */
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

/* ============ 操作头按钮(与练习页同款) ============ */
.btn-primary,
.btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 28px;
  padding: 0 var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  border: 1px solid transparent;
  transition: all var(--transition-fast);
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.btn-ghost {
  background: transparent;
  color: var(--color-text-secondary);
}

.btn-ghost:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.error-text {
  color: var(--color-danger);
}

.btn-link {
  padding: 0;
  font-size: var(--fs-sm);
  color: var(--color-primary);
  background: transparent;
  border: none;
  cursor: pointer;
}

.panel-empty {
  margin: 0;
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

/* ============ 主题折叠区(纵向单列,每区一个学习主题) ============ */
.board {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.topic-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  /* 目录定位/scrollspy 判定线,与 SECTION_GAP 保持一致 */
  scroll-margin-top: 16px;
}

.topic-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-3);
  padding: var(--space-3) var(--space-4);
  cursor: pointer;
  list-style: none;
  user-select: none;
}

/* 隐藏 WebKit 默认三角(用自绘 chevron) */
.topic-head::-webkit-details-marker {
  display: none;
}

.topic-name {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.chevron {
  color: var(--color-text-muted);
  transition: transform var(--transition-fast);
}

.topic-section[open] > .topic-head .chevron {
  transform: rotate(90deg);
}

.badge {
  display: inline-flex;
  align-items: center;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  border-radius: 999px;
}

.badge-disabled {
  color: var(--color-text-muted);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.topic-chips {
  display: inline-flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

/* 分栏计数 chip 按语义着色(与卡片状态徽章同色系) */
.chip {
  display: inline-flex;
  align-items: center;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  border-radius: 999px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}

.chip-weak { color: var(--color-danger); border-color: var(--color-danger); }
.chip-due { color: var(--color-primary); }
.chip-mastered { color: var(--color-success); }

.topic-meta {
  display: inline-flex;
  align-items: center;
  gap: var(--space-3);
  margin-left: auto;
}

.topic-count {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ============ 区内知识点卡片网格 ============ */
.kp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-2);
  padding: 0 var(--space-4) var(--space-4);
}

/* ============ 知识点卡片 ============ */
.kp-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

/* 薄弱卡片加红色描边突出 */
.kp-card-weak {
  border-color: var(--color-danger);
}

.kp-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
  min-width: 0;
}

.kp-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}

/* 右上角状态徽章(沿用五栏语义色) */
.status-badge {
  flex-shrink: 0;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  border-radius: 999px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
}

.status-weak { color: var(--color-danger); border-color: var(--color-danger); }
.status-due { color: var(--color-primary); }
.status-mastered { color: var(--color-success); }
.status-learning { color: var(--color-text); }

.kp-keyline {
  min-width: 0;
}

.kp-key {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  word-break: break-all;
}

.kp-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  border-radius: 999px;
}

.tag-lang {
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.kp-stats {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.kp-muted {
  color: var(--color-text-muted);
}

.kp-due {
  color: var(--color-primary);
}

.kp-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.kp-count {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* 改主题下拉:紧凑,不撑破卡片;与「专项练习」并排靠右 */
.kp-topic-select {
  margin-left: var(--space-1);
  max-width: 96px;
  padding: 2px 4px;
  font-size: var(--fs-xs);
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm, 6px);
  cursor: pointer;
}

.kp-topic-select:disabled {
  opacity: 0.6;
  cursor: default;
}

.btn-secondary {
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

.btn-secondary:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ============ 知识点讲解:卡片紧凑入口 + 右侧栏 ============ */
/* 卡片只留一行入口(有正文/来源显徽章,否则显可生成/暂无提示) */
.kp-explain-cta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-1);
  padding-top: var(--space-1);
  border-top: 1px dashed var(--color-border);
}

.kp-no-explain {
  font-size: var(--fs-xs);
}

.kp-open-btn {
  margin-left: auto;
}

/* 选中卡片描边高亮(与右侧栏内容对应) */
.kp-card-selected {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

/* 讲解来源徽章(auto=AI / manual=已编辑) */
.explain-badge {
  padding: 0 var(--space-1);
  font-size: var(--fs-xs);
  border-radius: 999px;
  border: 1px solid var(--color-border);
  color: var(--color-text-muted);
  background: var(--color-surface);
}

.explain-badge-auto {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.explain-badge-manual {
  color: var(--color-success);
  border-color: var(--color-success);
}

/* ============ 右侧栏:讲解查看 / 编辑(停靠可拖拽) ============ */
.explain-panel {
  position: relative; /* 供左缘调宽手柄经对定位 */
  flex-shrink: 0;
  min-width: 320px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  /* 分隔线常态淡,悬停/拖拽时才实心(见下 :has 规则) */
  border-left: 1px solid color-mix(in srgb, var(--color-border) 20%, transparent);
  transition: border-left-color var(--transition-fast);
}

/* 手柄 hover / focus / active 或拖拽中,把左缘边界线还原成实心 */
.explain-panel:has(.explain-resize-handle:hover),
.explain-panel:has(.explain-resize-handle:focus-visible),
.explain-panel:has(.explain-resize-handle:active),
.explain-panel.panel-resizing {
  border-left-color: var(--color-border);
}

/* 左缘调宽手柄:热区压住分隔线两侧 */
.explain-resize-handle {
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

.explain-resize-handle:hover,
.explain-resize-handle:focus-visible {
  background: var(--color-primary-light);
}

/* 拖拽中整栏禁选中 */
.explain-panel.panel-resizing {
  user-select: none;
  cursor: col-resize;
}

/* 栏头:知识点名 + 状态 + 关闭 */
.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.panel-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.panel-kp-name {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}

.panel-close {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-lg);
  line-height: 1;
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
}

.panel-close:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

/* 副头:key + 来源 + 更新于 + 错误 */
.panel-subhead {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.panel-kp-key {
  color: var(--color-text-muted);
  word-break: break-all;
}

.panel-time {
  color: var(--color-text-muted);
}

.panel-error {
  color: var(--color-danger);
}

/* 正文:预览滚动 / 编辑内部滚动 */
.panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
}

.panel-body.panel-body-edit {
  overflow: hidden;
  padding: 0;
}

.panel-empty {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-muted);
}

/* 讲解预览正文(Markdown;字号比卡片略大一号,宽栏更好读) */
.panel-preview {
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.panel-preview :deep(h1),
.panel-preview :deep(h2),
.panel-preview :deep(h3) {
  margin: var(--space-3) 0 var(--space-2);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
}

.panel-preview :deep(h1) { font-size: var(--fs-lg); }
.panel-preview :deep(h2) { font-size: var(--fs-base); }
.panel-preview :deep(h3) { font-size: var(--fs-sm); }

.panel-preview :deep(p) { margin: 0 0 var(--space-3); }

.panel-preview :deep(ul),
.panel-preview :deep(ol) {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-5);
}

.panel-preview :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.875em;
  padding: 1px var(--space-1);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.panel-preview :deep(pre) {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  overflow-x: auto;
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.panel-preview :deep(pre code) { padding: 0; background: transparent; }

/* 编辑器铺满正文高度(.panel-editor 即 CodeMirrorEditor 的 .cm-host 根) */
.panel-editor {
  height: 100%;
}

/* 底栏动作 */
.panel-footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
  flex-shrink: 0;
}

.panel-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* 操作头里的讲解进度/结果提示 */
.explain-status {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
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

/* ---- 响应式:窄屏隐藏目录(退回「操作头 + 滚动」体验) ---- */
@media (max-width: 900px) {
  .board-nav {
    display: none;
  }
}

/* ---- 响应式:窄屏(≤1024)右栏改覆盖式抽屉,手柄隐藏,宽度归本 CSS ---- */
@media (max-width: 1024px) {
  .explain-panel.panel-narrow {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(90vw, 460px);
    z-index: 50;
    box-shadow: -8px 0 30px rgba(0, 0, 0, 0.18);
  }
}

/* ---- 响应式:窄屏(手机)单列堆叠 ---- */
@media (max-width: 640px) {
  .main {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  /* 操作头换行后动作与统计各自成行 */
  .board-head {
    padding: var(--space-3) var(--space-3) var(--space-2);
    align-items: flex-start;
    row-gap: var(--space-2);
  }

  .head-actions {
    flex-wrap: wrap;
  }

  .topic-meta {
    margin-left: 0;
  }
}
</style>
