<script setup lang="ts">
/**
 * 应用顶栏(登录后的页面共用)
 *
 * 左侧品牌 + 导航(首页/提交任务/技能管理/记忆管理),右侧用户信息 + 问号(帮助文档)+ 齿轮(设置)+ 登出。
 *
 * 导航为 slot 的默认内容:所有界面默认显示这些项,无需每个视图重复声明;
 * 当前页高亮依赖 Vue Router 自动添加的 router-link-exact-active。
 * 个别视图若需自定义导航,仍可用 <template #nav> 覆盖默认内容。
 *
 * 硬约束:设置中心(/settings)不入主导航;主入口为齿轮按钮,顶栏邮箱为次级快捷入口(点击进入 /settings/account)。
 * 模型设置、CLI 设置、协作策略已合并到 /settings 子路由。
 * 问号按钮:打开帮助文档弹窗(所有路由行为一致,展示完整 help.md)。
 * 主题按钮:弹出浅色/深色/跟随系统三选项,选择持久化到 localStorage(useTheme)。
 */
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getPracticeSummary } from '@/api/practice'
import BrandLogo from '@/components/BrandLogo.vue'
import HelpDialog from '@/components/HelpDialog.vue'
import { useTheme } from '@/composables/useTheme'
import type { ThemeMode } from '@/composables/useTheme'
import { ensureFeaturesLoaded, practiceEnabled } from '@/composables/useFeatures'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const { mode, resolved, setMode } = useTheme()

/** 到期复习题数(「自适应练习」导航项红色徽标;0 时隐藏) */
const practiceDueCount = ref(0)

/** 静默拉取练习概览刷新徽标(未登录/网络异常不提示;练习功能关闭时跳过) */
async function refreshPracticeBadge(): Promise<void> {
  if (!practiceEnabled.value) {
    practiceDueCount.value = 0
    return
  }
  try {
    const summary = await getPracticeSummary()
    practiceDueCount.value = summary.due_count
  } catch {
    // 静默失败,徽标保持现状
  }
}

// 先拿功能开关再刷徽标(后端关闭练习功能时不拉概览、隐藏导航项)
ensureFeaturesLoaded().then(refreshPracticeBadge)
// 路由切换时刷新(任务完成后题库/到期数会变化)
watch(
  () => route.path,
  () => {
    refreshPracticeBadge()
  },
)

/** 帮助文档弹窗是否显示 */
const helpOpen = ref(false)

/** 主题菜单是否展开 */
const themeOpen = ref(false)
/** 主题按钮容器(用于点击外部关闭) */
const themeRootRef = ref<HTMLElement | null>(null)

/** 选择主题模式:应用 + 持久化 + 关闭菜单 */
function selectTheme(next: ThemeMode): void {
  setMode(next)
  themeOpen.value = false
}

/** 点击主题按钮区域外关闭菜单 */
function onDocumentClick(e: MouseEvent): void {
  if (themeRootRef.value?.contains(e.target as Node)) return
  themeOpen.value = false
}

// 菜单打开时监听外部点击,关闭时移除
watch(themeOpen, (isOpen) => {
  if (isOpen) {
    document.addEventListener('mousedown', onDocumentClick)
  } else {
    document.removeEventListener('mousedown', onDocumentClick)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentClick)
})

function handleLogout(): void {
  authStore.logout()
  router.push('/login')
}

/** 打开帮助文档弹窗 */
function handleOpenHelp(): void {
  helpOpen.value = true
}

/** 关闭帮助文档弹窗 */
function handleCloseHelp(): void {
  helpOpen.value = false
}
</script>

<template>
  <header class="app-header">
    <div class="header-inner">
      <!-- 最左侧前置区(如工作区开关按钮) -->
      <div v-if="$slots.leading" class="header-leading">
        <slot name="leading" />
      </div>
      <div class="header-left">
        <RouterLink to="/" class="brand">
          <BrandLogo :size="24" />
          <span>SecondLook</span>
        </RouterLink>
        <nav class="nav" data-onboarding="app-header-nav">
          <slot name="nav">
            <RouterLink to="/">首页</RouterLink>
            <RouterLink to="/tasks/new">提交任务</RouterLink>
            <RouterLink v-if="practiceEnabled" to="/practice" data-onboarding="app-header-nav-practice">
              自适应练习
              <span v-if="practiceDueCount > 0" class="practice-badge">{{
                practiceDueCount > 99 ? '99+' : practiceDueCount
              }}</span>
            </RouterLink>
            <RouterLink v-if="practiceEnabled" to="/knowledge-board">知识点看板</RouterLink>
            <RouterLink to="/memory">记忆管理</RouterLink>
            <RouterLink to="/skills">技能管理</RouterLink>
          </slot>
        </nav>
      </div>
      <!-- 右侧前置区(如右侧栏开关按钮);空 slot 时该 div 的 margin-left:auto 仍把用户区钉到右侧 -->
      <div class="header-trailing">
        <slot name="trailing" />
      </div>
      <div class="user-area">
        <span
          class="user-email"
          title="进入账户设置"
          @click="router.push('/settings/account')"
        >{{ authStore.user?.email }}</span>
        <button
          class="btn-help"
          title="帮助文档"
          aria-label="帮助文档"
          @click="handleOpenHelp"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </button>
        <!-- 主题切换:按钮显示当前生效主题图标,点击弹出三选项菜单 -->
        <div ref="themeRootRef" class="theme-switch">
          <button
            class="btn-theme"
            :class="{ 'is-open': themeOpen }"
            title="切换主题"
            aria-label="切换主题"
            aria-haspopup="menu"
            :aria-expanded="themeOpen"
            @click="themeOpen = !themeOpen"
          >
            <!-- 深色:月亮;浅色/跟随系统生效浅色时:太阳 -->
            <svg v-if="resolved === 'dark'" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
            </svg>
            <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
            </svg>
          </button>
          <!-- 主题选择菜单:浅色 / 深色 / 跟随系统 -->
          <div v-if="themeOpen" class="theme-menu" role="menu">
            <button
              class="theme-option"
              :class="{ 'is-selected': mode === 'light' }"
              role="menuitem"
              @click="selectTheme('light')"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
              </svg>
              <span>浅色</span>
              <svg v-if="mode === 'light'" class="theme-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </button>
            <button
              class="theme-option"
              :class="{ 'is-selected': mode === 'dark' }"
              role="menuitem"
              @click="selectTheme('dark')"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
              </svg>
              <span>深色</span>
              <svg v-if="mode === 'dark'" class="theme-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </button>
            <button
              class="theme-option"
              :class="{ 'is-selected': mode === 'system' }"
              role="menuitem"
              @click="selectTheme('system')"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect width="20" height="14" x="2" y="3" rx="2" />
                <line x1="8" x2="16" y1="21" y2="21" />
                <line x1="12" x2="12" y1="17" y2="21" />
              </svg>
              <span>跟随系统</span>
              <svg v-if="mode === 'system'" class="theme-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            </button>
          </div>
        </div>
        <button
          class="btn-settings"
          title="设置"
          aria-label="设置"
          data-onboarding="app-header-settings"
          @click="router.push('/settings')"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
        <button class="btn-logout" @click="handleLogout">登出</button>
      </div>
    </div>
  </header>

  <!-- 帮助文档弹窗(所有路由通用) -->
  <HelpDialog :open="helpOpen" @close="handleCloseHelp" />
</template>

<style scoped>
.app-header {
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.header-inner {
  /* 顶栏内容撑满宽度,侧栏按钮紧贴左边缘;用户区靠 margin-left:auto 钉到右侧 */
  padding: var(--space-2) var(--space-6) var(--space-2) var(--space-4);
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: var(--space-3);
}

.header-left {
  display: flex;
  align-items: center;
  gap: var(--space-8);
}

.header-leading {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

/* 右侧前置区:用 margin-left:auto 把自身及之后的用户区推到右侧;空 slot 时仍生效 */
.header-trailing {
  display: flex;
  align-items: center;
  margin-left: auto;
  flex-shrink: 0;
}

/* 品牌:深色字标 + 主色图标,收紧字距,克制不张扬 */
.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--fw-semibold);
  font-size: var(--fs-lg);
  letter-spacing: -0.01em;
  color: var(--color-text);
}

.brand svg {
  color: var(--color-primary);
}

.nav {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.user-area {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.user-email {
  /* 次级设置入口:hover 反馈与齿轮按钮一致(换文字色 + 浅背景) */
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  letter-spacing: 0.01em;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.user-email:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

/* 设置图标按钮:与登出按钮风格一致但更紧凑,圆形 hover 反馈 */
.btn-settings,
.btn-help,
.btn-theme {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-settings:hover,
.btn-help:hover,
.btn-theme:hover,
.btn-theme.is-open {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-logout {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  letter-spacing: 0.01em;
  transition: all var(--transition-fast);
}

.btn-logout:hover {
  color: var(--color-danger);
  border-color: var(--color-danger);
  background: var(--color-danger-light);
}

/* ---- 主题切换菜单 ---- */
.theme-switch {
  position: relative;
}

/* 下拉菜单:绝对定位于按钮下方右缘,悬浮在页面内容之上 */
.theme-menu {
  position: absolute;
  top: calc(100% + var(--space-2));
  right: 0;
  z-index: 30;
  min-width: 148px;
  padding: var(--space-1);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
}

.theme-option {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  border-radius: var(--radius-md);
  text-align: left;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.theme-option:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.theme-option.is-selected {
  color: var(--color-primary);
  font-weight: var(--fw-medium);
}

/* 选中对勾:占位右侧,与未选中项文字右缘对齐 */
.theme-check {
  margin-left: auto;
  flex-shrink: 0;
}

/* 导航项(slot 里的 RouterLink/a):去掉强填充,用文字色 + 极细下划线指示当前页 */
/* 高亮依赖 Vue Router 自动添加的 router-link-exact-active(精确匹配,避免 / 在子路由也高亮) */
:deep(.nav a) {
  position: relative;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  letter-spacing: 0.01em;
  border-radius: var(--radius-md);
  transition: color var(--transition-fast), background var(--transition-fast);
}

:deep(.nav a:hover) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

:deep(.nav a.router-link-exact-active) {
  color: var(--color-primary);
  background: transparent;
  font-weight: var(--fw-semibold);
}

:deep(.nav a.router-link-exact-active)::after {
  content: '';
  position: absolute;
  left: var(--space-3);
  right: var(--space-3);
  bottom: 3px;
  height: 2px;
  background: var(--color-primary);
  border-radius: var(--radius-full);
}

/* 到期复习徽标:红色小圆角数字,钉在「自适应练习」导航项右上角 */
:deep(.practice-badge) {
  position: absolute;
  top: 1px;
  right: 0;
  min-width: 16px;
  padding: 0 4px;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  line-height: 16px;
  text-align: center;
  color: #fff;
  background: var(--color-danger);
  border-radius: var(--radius-full);
  pointer-events: none;
}

/* ---- 响应式:中等宽度先隐藏邮箱腾出空间 ---- */
@media (max-width: 1280px) {
  .user-email {
    display: none;
  }

  .user-area {
    gap: var(--space-2);
  }
}

/* ---- 响应式:窄屏/手机两行布局 ----
   第一行:侧栏开关 + 品牌 + 右侧按钮区;
   第二行:导航整行横向滚动(项多不截断,滑动可达)。 */
@media (max-width: 1080px) {
  .header-inner {
    flex-wrap: wrap;
    row-gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
  }

  /* 允许品牌与导航换行:导航 flex-basis:100% 强制独占第二行 */
  .header-left {
    flex: 1 1 auto;
    min-width: 0;
    flex-wrap: wrap;
    gap: var(--space-1) var(--space-4);
  }

  .nav {
    flex: 1 0 100%;
    overflow-x: auto;
    /* 隐藏滚动条但保留滑动能力(触屏上直接手指滑动) */
    scrollbar-width: none;
    padding-bottom: 2px;
  }

  .nav::-webkit-scrollbar {
    display: none;
  }

  :deep(.nav a) {
    flex-shrink: 0;
    white-space: nowrap;
  }

  /* 触屏目标增大:图标按钮 32→36px */
  .btn-settings,
  .btn-help,
  .btn-theme {
    width: 36px;
    height: 36px;
  }

  .btn-logout {
    padding: var(--space-2) var(--space-3);
  }
}
</style>
