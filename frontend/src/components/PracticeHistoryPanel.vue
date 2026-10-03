<script setup lang="ts">
/**
 * 练习记录面板(自适应练习首页的「历史记录」段)
 *
 * - 学习趋势:最近 8 周按周聚合的正确率走势(纯 SVG 迷你折线,不引图表库)
 * - 历史练习:会话列表(新到旧)+ 逐题作答明细,会话明细展开时按需拉取
 *
 * 原为独立路由页 /practice/history,现内嵌进练习首页作为左侧目录的锚点段。
 * 数据由本组件挂载时自加载;清空练习记录(在练习设置内)后回到首页会随重挂载刷新。
 * 明细拉取失败通过 toast 事件上抛给宿主展示,避免两份 toast 实现。
 *
 * 逐题明细行可点开 PracticeQuestionDetailDialog 看完整题面(含选项/正确答案/解析),
 * 并把当次作答传进去,标出「你选的」是哪一个。
 */
import { computed, onMounted, ref } from 'vue'

import PracticeQuestionDetailDialog from '@/components/PracticeQuestionDetailDialog.vue'
import { getSessionDetail, getPracticeTrend, listPracticeSessions } from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import { formatDateTime, formatPercent, optionLetter } from '@/utils/practiceFormat'
import type { SessionAttemptItem, SessionDetail, SessionListItem, TrendPoint } from '@/types/practice'

const emit = defineEmits<{
  (e: 'toast', msg: string, type: 'success' | 'error'): void
}>()

// ============================================================
// 学习趋势(按周聚合,纯 SVG 迷你折线图)
// ============================================================
const trendWeeks = ref<TrendPoint[]>([])

async function loadTrend(): Promise<void> {
  try {
    const res = await getPracticeTrend()
    trendWeeks.value = res.weeks
  } catch {
    trendWeeks.value = []
  }
}

/** 折线图几何参数(纯 SVG 手绘,不引图表库) */
const TREND_W = 560
const TREND_H = 84
const TREND_PAD_X = 14
const TREND_PAD_Y = 10

/** 有作答记录的周(无数据周不连线) */
const trendActive = computed(() => trendWeeks.value.filter((w) => w.attempts > 0))

function trendX(i: number): number {
  const n = trendActive.value.length
  if (n <= 1) return TREND_W / 2
  return TREND_PAD_X + (i * (TREND_W - TREND_PAD_X * 2)) / (n - 1)
}

function trendY(w: TrendPoint): number {
  const acc = w.correct / w.attempts
  return TREND_PAD_Y + (1 - acc) * (TREND_H - TREND_PAD_Y * 2)
}

const trendPath = computed(() =>
  trendActive.value
    .map((w, i) => `${i === 0 ? 'M' : 'L'}${trendX(i).toFixed(1)},${trendY(w).toFixed(1)}`)
    .join(' '),
)

/** 60% 正确率参考线 */
const trendGuideY = TREND_PAD_Y + 0.4 * (TREND_H - TREND_PAD_Y * 2)

function trendTip(w: TrendPoint): string {
  return `${formatWeekLabel(w.week_start)} · 作答 ${w.attempts} · 正确率 ${formatPercent(
    w.correct / w.attempts,
  )}`
}

function formatWeekLabel(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${d.getMonth() + 1}/${d.getDate()}`
}

// ============================================================
// 历史练习(会话列表 + 逐题明细,明细展开时按需拉取)
// ============================================================
const sessions = ref<SessionListItem[]>([])
const sessionsLoading = ref(false)

async function loadSessions(): Promise<void> {
  sessionsLoading.value = true
  try {
    sessions.value = await listPracticeSessions()
  } catch {
    sessions.value = []
  } finally {
    sessionsLoading.value = false
  }
}

/** 已展开过的会话明细(session_id → detail) */
const sessionDetails = ref<Record<string, SessionDetail>>({})
const sessionDetailLoading = ref<Record<string, boolean>>({})

/** 展开某场会话时按需拉取逐题明细 */
async function handleSessionDetailToggle(s: SessionListItem, e: Event): Promise<void> {
  if (!(e.target as HTMLDetailsElement).open) return
  if (sessionDetails.value[s.id] || sessionDetailLoading.value[s.id]) return
  sessionDetailLoading.value[s.id] = true
  try {
    sessionDetails.value[s.id] = await getSessionDetail(s.id)
  } catch (err) {
    emit('toast', extractErrorMessage(err), 'error')
  } finally {
    sessionDetailLoading.value[s.id] = false
  }
}

// ============================================================
// 题目详情弹窗(点开逐题明细行)
// ============================================================
const detailOpen = ref(false)
/** 当前查看的那一次作答(题目 id 由它携带,避免两个状态走歪) */
const activeAttempt = ref<SessionAttemptItem | null>(null)

function openQuestionDetail(a: SessionAttemptItem): void {
  activeAttempt.value = a
  detailOpen.value = true
}

function closeQuestionDetail(): void {
  detailOpen.value = false
  activeAttempt.value = null
}

// ============================================================
// 生命周期
// ============================================================
onMounted(() => {
  loadTrend()
  loadSessions()
})
</script>

<template>
  <!-- 学习趋势(按周聚合正确率,纯 SVG 迷你折线) -->
  <section class="panel">
    <h2>学习趋势(最近 8 周)</h2>
    <p v-if="trendActive.length === 0" class="panel-empty">
      暂无作答记录 — 完成练习后这里会按周展示正确率走势
    </p>
    <div v-else class="trend-wrap">
      <svg
        :viewBox="`0 0 ${TREND_W} ${TREND_H}`"
        class="trend-svg"
        preserveAspectRatio="none"
        role="img"
        aria-label="每周正确率趋势折线图"
      >
        <!-- 60% 正确率参考线 -->
        <line
          :x1="TREND_PAD_X"
          :x2="TREND_W - TREND_PAD_X"
          :y1="trendGuideY"
          :y2="trendGuideY"
          class="trend-guide"
        />
        <path v-if="trendActive.length > 1" :d="trendPath" class="trend-line" />
        <circle
          v-for="(w, i) in trendActive"
          :key="w.week_start"
          :cx="trendX(i)"
          :cy="trendY(w)"
          r="3.5"
          class="trend-dot"
        >
          <title>{{ trendTip(w) }}</title>
        </circle>
      </svg>
      <div class="trend-axis">
        <span v-for="w in trendActive" :key="w.week_start" class="trend-axis-label">
          {{ formatWeekLabel(w.week_start) }}
        </span>
      </div>
    </div>
  </section>

  <!-- 历史练习(会话列表;逐题明细展开时按需拉取) -->
  <section class="panel">
    <h2>历史练习</h2>
    <div v-if="sessionsLoading" class="placeholder"><span class="status-spinner" /> 加载中...</div>
    <p v-else-if="sessions.length === 0" class="panel-empty">
      暂无练习记录 — 完成几局练习后,作答历史会展示在这里
    </p>
    <div v-else class="history-list">
      <details
        v-for="s in sessions"
        :key="s.id"
        class="history-item"
        @toggle="(e) => handleSessionDetailToggle(s, e)"
      >
        <summary class="history-summary">
          <span class="history-date">{{ formatDateTime(s.started_at) }}</span>
          <span>作答 {{ s.answered_count }}/{{ s.question_count }} 题</span>
          <span class="history-acc">正确率 {{ formatPercent(s.accuracy) }}</span>
        </summary>
        <div v-if="sessionDetailLoading[s.id]" class="placeholder">
          <span class="status-spinner" /> 加载明细...
        </div>
        <div v-else-if="sessionDetails[s.id]" class="attempt-list">
          <button
            v-for="(a, idx) in sessionDetails[s.id].attempts"
            :key="idx"
            type="button"
            :class="['attempt-row', a.is_correct ? 'attempt-correct' : 'attempt-wrong']"
            title="查看完整题面、选项与解析"
            @click="openQuestionDetail(a)"
          >
            <span class="attempt-mark">{{ a.is_correct ? '✓' : '✗' }}</span>
            <span class="attempt-stem">{{ a.stem }}</span>
            <span class="attempt-answer">
              你选 {{ optionLetter(a.chosen_idx) }} · 正确答案 {{ optionLetter(a.correct_idx) }}
            </span>
          </button>
        </div>
      </details>
    </div>
  </section>

  <!-- 题目详情弹窗(完整题面 + 本次作答标记) -->
  <PracticeQuestionDetailDialog
    :open="detailOpen"
    :question-id="activeAttempt?.question_id ?? null"
    :attempt="activeAttempt"
    @close="closeQuestionDetail"
  />
</template>

<style scoped>
/* ============ 通用面板(与宿主练习页同款) ============ */
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

.placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

/* ============ 学习趋势(纯 SVG 折线) ============ */
.trend-wrap {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.trend-svg {
  width: 100%;
  height: 84px;
  display: block;
}

.trend-guide {
  stroke: var(--color-border);
  stroke-width: 1;
  stroke-dasharray: 4 4;
}

.trend-line {
  fill: none;
  stroke: var(--color-primary);
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.trend-dot {
  fill: var(--color-primary);
}

.trend-axis {
  display: flex;
  justify-content: space-between;
  padding: 0 var(--space-2);
}

.trend-axis-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ============ 历史练习(会话列表) ============ */
.history-list {
  display: flex;
  flex-direction: column;
}

.history-item {
  border-top: 1px solid var(--color-border);
}

.history-item:first-child {
  border-top: none;
}

.history-summary {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
  list-style: none;
}

.history-summary::-webkit-details-marker {
  display: none;
}

.history-summary::before {
  content: '▸';
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  transition: transform var(--transition-fast);
}

.history-item[open] > .history-summary::before {
  transform: rotate(90deg);
}

.history-date {
  color: var(--color-text);
  font-weight: var(--fw-medium);
  white-space: nowrap;
}

.history-acc {
  margin-left: auto;
  font-size: var(--fs-xs);
}

.attempt-list {
  display: flex;
  flex-direction: column;
  padding-bottom: var(--space-2);
}

.attempt-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  padding: var(--space-1) 0 var(--space-1) var(--space-4);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  /* 整行是个按钮(点开题目详情):去掉按钮默认外观,保留左对齐 */
  width: 100%;
  text-align: left;
  background: none;
  border: none;
  cursor: pointer;
  transition: background var(--transition-fast);
}

.attempt-row:hover {
  background: var(--color-surface-alt);
}

.attempt-row:focus-visible {
  outline: 2px solid var(--color-primary-border);
  outline-offset: -2px;
  border-radius: var(--radius-sm);
}

.attempt-mark {
  flex-shrink: 0;
  font-weight: var(--fw-semibold);
}

.attempt-correct .attempt-mark {
  color: var(--color-success);
}

.attempt-wrong .attempt-mark {
  color: var(--color-danger);
}

.attempt-stem {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 1;
  -webkit-box-orient: vertical;
}

.attempt-answer {
  flex-shrink: 0;
  color: var(--color-text-muted);
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
</style>
