<script setup lang="ts">
/**
 * 首页(工作台)
 *
 * 布局:操作行(提交新任务 / 开始练习)+ 最近任务列表(扁平行式,无卡片包裹)。
 * - 任务行点击直达 /tasks/:id;「展开全部任务」展开左侧历史任务栏(复用 WorkspaceSidebar)
 * - 状态用彩色圆点承载(运行中/检查中带呼吸动画),文字保持次要色
 * - 练习功能开关(PRACTICE_ENABLED)关闭时隐藏「开始练习」按钮
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { listTasks } from '@/api/task'
import { ensureFeaturesLoaded, practiceEnabled } from '@/composables/useFeatures'
import type { TaskListItem, TaskStatus } from '@/types/task'

const router = useRouter()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ---- 最近任务 ----

/** 首页展示的最近任务条数 */
const RECENT_TASKS_LIMIT = 8

const recentTasks = ref<TaskListItem[]>([])
const recentLoading = ref(true)
const recentError = ref('')

async function loadRecentTasks(): Promise<void> {
  recentLoading.value = true
  recentError.value = ''
  try {
    recentTasks.value = await listTasks({ limit: RECENT_TASKS_LIMIT })
  } catch {
    recentError.value = '最近任务加载失败'
  } finally {
    recentLoading.value = false
  }
}

onMounted(() => {
  ensureFeaturesLoaded()
  loadRecentTasks()
})

/** 任务行标题:自定义标题优先,回退任务输入原文(超长省略由 CSS 截断) */
function taskTitle(t: TaskListItem): string {
  return t.title || t.user_input || '(无标题)'
}

/** 状态文字(检查中 = 任务已完成、检查助手仍在后台核查,优先于基础状态显示) */
const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '等待中',
  running: '运行中',
  paused: '已暂停',
  completed: '已完成',
  failed: '已失败',
}

/** 被进程中断的失败(后端重启丢失执行线程):与"真失败"区分,显示为琥珀"已中断" */
function isInterrupted(t: TaskListItem): boolean {
  return t.status === 'failed' && Boolean(t.interrupted_at)
}

function statusLabel(t: TaskListItem): string {
  if (t.review_status === 'running') return '检查中'
  if (isInterrupted(t)) return '已中断'
  return STATUS_LABELS[t.status] ?? t.status
}

/** 状态圆点样式类(颜色与左侧历史任务栏徽标同语义:运行=蓝/暂停=琥珀/完成=绿/失败=红) */
const STATUS_DOTS: Record<TaskStatus, string> = {
  pending: 'dot-pending',
  running: 'dot-running',
  paused: 'dot-paused',
  completed: 'dot-done',
  failed: 'dot-failed',
}

function statusDotClass(t: TaskListItem): string {
  if (t.review_status === 'running') return 'dot-review'
  // 中断态取琥珀(与暂停近似),而非红色"已失败",提示"可继续"
  if (isInterrupted(t)) return 'dot-paused'
  return STATUS_DOTS[t.status] ?? 'dot-pending'
}

// ---- 相对时间 ----

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  )
}

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** 相对时间:刚刚 / N 分钟前 / N 小时前 / 昨天 HH:mm / MM-DD(时钟偏差的未来时间也按日期显示) */
function formatRelativeTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const diffMin = Math.floor((now.getTime() - d.getTime()) / 60000)
  if (diffMin < 0) return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  if (diffMin < 24 * 60 && isSameDay(d, now)) return `${Math.floor(diffMin / 60)} 小时前`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (isSameDay(d, yesterday)) return `昨天 ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

function goToTask(id: string): void {
  // 带 workspace=1:进入详情页时自动展开历史任务侧栏(首页列表 → 详情的浏览连续性)
  router.push({ path: `/tasks/${id}`, query: { workspace: '1' } })
}
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          data-onboarding="home-workspace-toggle"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <!-- 操作行:提交新任务(主)+ 开始练习(次,练习开关关闭时隐藏) -->
        <div class="actions" data-onboarding="home-actions">
          <button
            class="btn-primary"
            data-onboarding="home-cta"
            @click="router.push('/tasks/new')"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            提交新任务
          </button>
          <button
            v-if="practiceEnabled"
            class="btn-secondary"
            @click="router.push('/practice')"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 20h9" />
              <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
            </svg>
            开始练习
          </button>
        </div>

        <!-- 最近任务 -->
        <section class="recent" data-onboarding="home-recent-tasks">
          <div class="recent-head">
            <h1 class="recent-title">最近任务</h1>
            <button
              v-if="recentTasks.length > 0"
              class="recent-more"
              title="展开左侧历史任务栏"
              @click="workspaceCollapsed = false"
            >
              展开全部任务
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
          </div>

          <!-- 加载骨架 -->
          <div v-if="recentLoading" class="task-list" aria-hidden="true">
            <div v-for="i in 4" :key="i" class="skeleton-row">
              <span class="skeleton-dot" />
              <span class="skeleton-label" />
              <span class="skeleton-bar" :class="{ 'skeleton-bar-short': i % 2 === 0 }" />
            </div>
          </div>

          <!-- 加载失败(轻量行内提示,不阻塞提交新任务) -->
          <div v-else-if="recentError" class="state-inline">
            <span>{{ recentError }}</span>
            <button class="retry-link" @click="loadRecentTasks">重试</button>
          </div>

          <!-- 空状态(无卡片,居中文案引导视线到上方主按钮) -->
          <div v-else-if="recentTasks.length === 0" class="empty">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="12" y1="18" x2="12" y2="12" />
              <line x1="9" y1="15" x2="15" y2="15" />
            </svg>
            <p class="empty-title">还没有任务</p>
            <p class="empty-sub">提交第一个任务:AI 助手执行,检查助手质检结果</p>
          </div>

          <!-- 任务列表(扁平行,细分割线,整行可点) -->
          <div v-else class="task-list">
            <button
              v-for="t in recentTasks"
              :key="t.id"
              class="task-row"
              @click="goToTask(t.id)"
            >
              <span class="task-status">
                <span :class="['task-dot', statusDotClass(t)]" />
                <span class="task-label">{{ statusLabel(t) }}</span>
              </span>
              <span class="task-title" :title="taskTitle(t)">{{ taskTitle(t) }}</span>
              <span class="task-time">{{ formatRelativeTime(t.created_at) }}</span>
            </button>
          </div>
        </section>
      </main>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  /* 手机浏览器地址栏伸缩:用 dvh 保证底部不被遮挡(vh 兜底旧浏览器) */
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
  min-width: 0;
  max-width: var(--content-width);
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-12) var(--space-6);
}

/* ---- 操作行 ---- */
.actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 44px;
  padding: 0 var(--space-5);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.btn-secondary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 44px;
  padding: 0 var(--space-5);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  background: transparent;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition:
    background var(--transition-fast),
    border-color var(--transition-fast);
}

.btn-secondary:hover {
  background: var(--color-surface-alt);
  border-color: var(--color-text-muted);
}

/* ---- 最近任务区 ---- */
.recent {
  margin-top: var(--space-6);
}

.recent-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  padding-bottom: var(--space-1);
}

.recent-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.recent-more {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: none;
  border: none;
  cursor: pointer;
  transition: color var(--transition-fast);
}

.recent-more:hover {
  color: var(--color-primary-hover);
}

/* ---- 任务列表(扁平行) ---- */
.task-list {
  display: flex;
  flex-direction: column;
}

.task-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-3) var(--space-2);
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--color-border);
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-fast);
}

.task-row:hover {
  background: var(--color-surface-alt);
}

.task-status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: none;
  width: 76px;
}

.task-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
}

/* 状态点配色与左侧历史任务栏徽标同语义 */
.dot-pending {
  background: var(--color-text-muted);
}

.dot-running {
  background: var(--color-info);
  --dot-glow: var(--color-info);
}

.dot-review {
  background: var(--color-primary);
  --dot-glow: var(--color-primary);
}

.dot-paused {
  background: var(--color-warning);
}

.dot-done {
  background: var(--color-success);
}

.dot-failed {
  background: var(--color-danger);
}

/* 运行中/检查中:呼吸光环(仅允许动画的设备) */
@media (prefers-reduced-motion: no-preference) {
  .dot-running,
  .dot-review {
    animation: dot-pulse 2s ease-out infinite;
  }

  @keyframes dot-pulse {
    0% {
      box-shadow: 0 0 0 0 color-mix(in srgb, var(--dot-glow) 35%, transparent);
    }
    100% {
      box-shadow: 0 0 0 6px transparent;
    }
  }
}

.task-label {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.task-title {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-base);
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.task-time {
  flex: none;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ---- 加载骨架 ---- */
.skeleton-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-2);
  border-bottom: 1px solid var(--color-border);
}

.skeleton-dot,
.skeleton-label,
.skeleton-bar {
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.skeleton-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
}

.skeleton-label {
  flex: none;
  width: 48px;
  height: 14px;
}

.skeleton-bar {
  flex: 1;
  height: 14px;
  max-width: 420px;
}

.skeleton-bar-short {
  max-width: 260px;
}

@media (prefers-reduced-motion: no-preference) {
  .skeleton-dot,
  .skeleton-label,
  .skeleton-bar {
    animation: skeleton-blink 1.4s ease-in-out infinite;
  }

  @keyframes skeleton-blink {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.45;
    }
  }
}

/* ---- 加载失败(行内) ---- */
.state-inline {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4) var(--space-2);
  border-bottom: 1px solid var(--color-border);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.retry-link {
  padding: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: none;
  border: none;
  cursor: pointer;
  transition: color var(--transition-fast);
}

.retry-link:hover {
  color: var(--color-primary-hover);
}

/* ---- 空状态 ---- */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-10) 0;
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text-muted);
}

.empty-title {
  margin-top: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
}

.empty-sub {
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 640px) {
  .main {
    padding: var(--space-6) var(--space-3);
  }

  .actions {
    flex-wrap: wrap;
  }

  /* 窄屏隐藏状态文字,只留圆点腾出标题空间 */
  .task-label {
    display: none;
  }

  .task-status {
    width: auto;
  }
}
</style>
