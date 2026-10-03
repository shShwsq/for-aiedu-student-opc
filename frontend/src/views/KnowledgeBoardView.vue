<script setup lang="ts">
/**
 * 知识点看板页(自适应练习的「知识点掌握全景」视图)
 *
 * - 两级结构:知识点按学习主题分组(内置 安全/架构/编码/合同 + 用户自定义),
 *   主题折叠区展开后是知识点卡片网格,组内保持后端排序
 *   (weak > due > mastered > learning > fresh)
 * - 每张卡片 = 一个知识点:SM-2 记忆状态 + 作答统计 + 题库题数 + 状态徽章
 * - 区头「练这个主题」→ 跳 /practice?learningTopic=<key>,由练习页接管组卷;
 *   卡片「专项练习」→ 跳 /practice?topic=<key>(单知识点)
 * - 未知/已删除的主题 key 兜底「未分类」组排最后;停用主题照常成区
 *   (区头带「已停用」徽章,存量题不受影响,只是不再出新题)
 * - 布局对齐练习页/设置页:取消标题,第一行常驻操作头(去练习 + 知识点主题设置 +
 *   知识点数统计,在 .main 滚动区之外故不随内容滚动);左侧目录一个主题一项,
 *   点击即展开该区并平滑定位,scrollspy 高亮当前主题;知识点主题设置
 *   深链到 /settings/practice#learning-topics
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { listKnowledgePoints, listLearningTopics } from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type {
  BoardStatus,
  KnowledgePointCard,
  LearningTopicDef,
} from '@/types/practice'

const router = useRouter()

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

/** 目录点击:即刻高亮 + 展开目标区(收起态只滚到区头看不到卡片)+ 平滑滚动 */
function goSection(sec: TopicSection): void {
  activeKey.value = sec.topicKey
  openMap.value = { ...openMap.value, [sec.topicKey]: true }
  nextTick(() => {
    document
      .getElementById(sectionAnchor(sec.topicKey))
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
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
            </div>
            <div class="head-stats">
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
                    :class="['kp-card', { 'kp-card-weak': c.board_status === 'weak' }]"
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
                    </div>
                  </article>
                </div>
              </details>
            </div>
          </main>
        </div>
      </div>
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
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.kp-count {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
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
