/**
 * Vue Router 配置
 *
 * 路由职责划分:
 * - 公开路由:login / auth/* (无需登录)
 * - 受保护路由: / (需要登录,meta.requiresAuth = true)
 *
 * 守卫逻辑:
 * - 受保护路由未登录 → 跳 /login?redirect=原始路径
 * - 已登录访问 /login → 跳 / (避免重复登录)
 * - 有 token 但 user 未加载(页面刷新)→ 先 fetchMe 恢复会话
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { ensureFeaturesLoaded, practiceEnabled } from '@/composables/useFeatures'
import { useAuthStore } from '@/stores/auth'
import { useUnsavedGuardStore } from '@/stores/unsavedGuard'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/HomeView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/tasks/new',
    name: 'task-create',
    component: () => import('@/views/TaskCreateView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/tasks/:id',
    name: 'task-detail',
    component: () => import('@/views/TaskDetailView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings',
    component: () => import('@/views/settings/SettingsLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', redirect: '/settings/account' },
      { path: 'account', name: 'settings-account', component: () => import('@/views/settings/AccountSettingsPanel.vue') },
      { path: 'models', name: 'settings-models', component: () => import('@/views/settings/ModelSettingsPanel.vue') },
      { path: 'cli', name: 'settings-cli', component: () => import('@/views/settings/CliSettingsPanel.vue') },
      { path: 'policy', name: 'settings-policy', component: () => import('@/views/settings/AgentPolicyPanel.vue') },
      { path: 'practice', name: 'settings-practice', component: () => import('@/views/settings/PracticeSettingsPanel.vue') },
    ],
  },
  // 旧路径重定向(兼容书签)
  { path: '/models', redirect: '/settings/models' },
  { path: '/cli', redirect: '/settings/cli' },
  { path: '/agent-policy', redirect: '/settings/policy' },
  {
    path: '/memory',
    name: 'memory',
    component: () => import('@/views/MemoryView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/skills',
    name: 'skills',
    component: () => import('@/views/SkillManagerView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/practice',
    name: 'practice',
    component: () => import('@/views/PracticeView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 练习记录已内嵌进练习首页(左侧目录「历史记录」段),旧路径重定向保留书签兼容
    path: '/practice/history',
    redirect: '/practice#history',
  },
  {
    // 知识点看板页(知识点掌握全景 + 专项练习入口),顶栏一级导航「知识点看板」进入;
    // 与「自适应练习」并列,路径为一级;name 保持 practice-board(功能开关守卫按名匹配)
    path: '/knowledge-board',
    name: 'practice-board',
    component: () => import('@/views/KnowledgeBoardView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 旧看板路径曾嵌在 /practice 下,升级为一级路径后保留重定向兼容书签
    path: '/practice/board',
    redirect: '/knowledge-board',
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { guestOnly: true },
  },
  {
    path: '/auth/github/callback',
    name: 'github-callback',
    component: () => import('@/views/OAuthCallbackView.vue'),
  },
  {
    path: '/auth/gitee/callback',
    name: 'gitee-callback',
    component: () => import('@/views/OAuthCallbackView.vue'),
  },
  {
    path: '/auth/verify-email',
    name: 'verify-email',
    component: () => import('@/views/VerifyEmailView.vue'),
  },
  {
    path: '/auth/password/reset',
    name: 'reset-password',
    component: () => import('@/views/ResetPasswordView.vue'),
  },
  // 兜底:未匹配的路由跳首页
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() {
    // 切换页面时滚到顶部
    return { top: 0 }
  },
})

// 标记是否已尝试恢复会话(避免每次路由都 fetchMe)
let sessionRestored = false

router.beforeEach(async (to, from) => {
  // 未保存改动守卫:当前页有未保存改动且要切到别的页面时,弹窗询问
  // (仅拦截真正换页;同路由 query/params 变化不拦截)
  const unsavedStore = useUnsavedGuardStore()
  if (unsavedStore.dirty && to.name !== from.name) {
    const proceed = await unsavedStore.confirmLeave()
    if (!proceed) return false
  }

  const authStore = useAuthStore()

  // 有 token 但 user 为空(页面刷新场景):先尝试恢复
  if (authStore.hasToken && !authStore.isAuthenticated && !sessionRestored) {
    sessionRestored = true
    await authStore.fetchMe()
  }

  // 受保护路由:未登录 → 跳登录
  if (to.meta.requiresAuth && !authStore.isAuthenticated) {
    return {
      name: 'login',
      query: { redirect: to.fullPath },
    }
  }

  // 练习功能开关:后端关闭时直连练习相关页回退(入口已隐藏,此处兜底)
  // —— /practice 与看板页回首页;/settings/practice 回账户设置页
  // (旧 /practice/history、/practice/board 均已重定向,由各自目标分支覆盖)
  if (
    to.name === 'practice' ||
    to.name === 'practice-board' ||
    to.name === 'settings-practice'
  ) {
    await ensureFeaturesLoaded()
    if (!practiceEnabled.value) {
      return to.name === 'settings-practice' ? { name: 'settings-account' } : { name: 'home' }
    }
  }

  // 已登录访问登录页 → 跳首页
  if (to.meta.guestOnly && authStore.isAuthenticated) {
    return { name: 'home' }
  }

  return true
})

export default router
