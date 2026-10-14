<script setup lang="ts">
/**
 * 登录 / 注册 / 忘记密码 页面
 *
 * 三种模式通过 mode 切换,共享同一卡片容器:
 * - login:    邮箱 + 密码登录
 * - register: 邮箱 + 密码注册(注册后需验证邮箱)
 * - forgot:   输入邮箱发重置链接
 *
 * 另提供 GitHub OAuth 入口。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import BrandLogo from '@/components/BrandLogo.vue'
import { forgotPassword, getOAuthAuthorizeURL } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import type { GitProvider } from '@/types/git_provider'
import { extractErrorMessage } from '@/utils/error'

type Mode = 'login' | 'register' | 'forgot'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

// ---- 表单状态 ----

const mode = ref<Mode>('login')
const email = ref('')
const password = ref('')
const showPassword = ref(false)
const loading = ref(false)
const error = ref('')
const success = ref('')

// ---- 表单校验 ----

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const errors = computed(() => {
  const e: { email?: string; password?: string } = {}
  if (!email.value.trim()) {
    e.email = '请输入邮箱'
  } else if (!emailPattern.test(email.value.trim())) {
    e.email = '邮箱格式不正确'
  }
  if (mode.value !== 'forgot') {
    if (!password.value) {
      e.password = '请输入密码'
    } else if (password.value.length < 8) {
      e.password = '密码至少 8 位'
    }
  }
  return e
})

const canSubmit = computed(() => Object.keys(errors.value).length === 0)

// ---- 提交 ----

const submitLabel = computed(() => {
  if (loading.value) return '处理中...'
  if (mode.value === 'login') return '登录'
  if (mode.value === 'register') return '注册'
  return '发送重置邮件'
})

async function handleSubmit(): Promise<void> {
  // 切换模式时清空提示
  error.value = ''
  success.value = ''

  if (Object.keys(errors.value).length > 0) return

  loading.value = true
  try {
    if (mode.value === 'login') {
      await authStore.login(email.value.trim(), password.value)
      // 登录成功 → 跳转到 redirect 参数或首页
      const redirect = (route.query.redirect as string) || '/'
      await router.push(redirect)
    } else if (mode.value === 'register') {
      const msg = await authStore.register(email.value.trim(), password.value)
      success.value = msg
      // 注册成功后切到登录 tab,保留邮箱
      password.value = ''
      mode.value = 'login'
    } else {
      const res = await forgotPassword(email.value.trim())
      success.value = res.message
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

// ---- 模式切换 ----

function switchMode(newMode: Mode): void {
  mode.value = newMode
  error.value = ''
  success.value = ''
  if (newMode === 'forgot') {
    password.value = ''
  }
}

// ---- Git 平台 OAuth(GitHub / Gitee) ----

/** 跳转到指定平台授权页,回调由 OAuthCallbackView 处理 */
function handleOAuth(provider: GitProvider): void {
  window.location.href = getOAuthAuthorizeURL(provider)
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <!-- 品牌区 -->
      <div class="auth-header">
        <div class="logo">
          <BrandLogo :size="44" />
        </div>
        <h1>SecondLook</h1>
        <p class="subtitle">AI助手 先产出、检查助手 后台独立复核</p>
      </div>

      <!-- Tab 切换 -->
      <div class="auth-tabs" v-if="mode !== 'forgot'">
        <button
          type="button"
          :class="['tab', { active: mode === 'login' }]"
          @click="switchMode('login')"
        >
          登录
        </button>
        <button
          type="button"
          :class="['tab', { active: mode === 'register' }]"
          @click="switchMode('register')"
        >
          注册
        </button>
      </div>

      <!-- 标题(forgot 模式) -->
      <h2 v-if="mode === 'forgot'" class="mode-title">重置密码</h2>

      <!-- 提示信息 -->
      <Transition name="fade">
        <div v-if="error" class="alert alert-error" role="alert">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span>{{ error }}</span>
        </div>
      </Transition>

      <Transition name="fade">
        <div v-if="success" class="alert alert-success" role="status">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
          </svg>
          <span>{{ success }}</span>
        </div>
      </Transition>

      <!-- 表单 -->
      <form @submit.prevent="handleSubmit" novalidate>
        <!-- 邮箱 -->
        <div class="field">
          <label for="email">邮箱</label>
          <input
            id="email"
            v-model.trim="email"
            type="email"
            autocomplete="email"
            placeholder="you@example.com"
            :class="{ invalid: errors.email }"
          />
          <span v-if="errors.email" class="field-error">{{ errors.email }}</span>
        </div>

        <!-- 密码 -->
        <div v-if="mode !== 'forgot'" class="field">
          <label for="password">密码</label>
          <div class="input-wrapper">
            <input
              id="password"
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
              placeholder="至少 8 位"
              :class="{ invalid: errors.password }"
            />
            <button
              type="button"
              class="toggle-pwd"
              :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              @click="showPassword = !showPassword"
            >
              <!-- 眼睛图标 -->
              <svg v-if="!showPassword" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
              <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            </button>
          </div>
          <span v-if="errors.password" class="field-error">{{ errors.password }}</span>
        </div>

        <!-- 忘记密码链接 -->
        <a
          v-if="mode === 'login'"
          class="link-right"
          @click="switchMode('forgot')"
        >
          忘记密码?
        </a>

        <!-- 提交按钮 -->
        <button
          type="submit"
          class="btn-primary"
          :disabled="loading || !canSubmit"
        >
          <span v-if="loading" class="spinner" aria-hidden="true" />
          {{ submitLabel }}
        </button>
      </form>

      <!-- 分割线 -->
      <div v-if="mode !== 'forgot'" class="divider">
        <span>或</span>
      </div>

      <!-- GitHub OAuth -->
      <button
        v-if="mode !== 'forgot'"
        type="button"
        class="btn-github"
        :disabled="loading"
        @click="handleOAuth('github')"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
          <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
        </svg>
        使用 GitHub 登录
      </button>

      <!-- Gitee OAuth -->
      <button
        v-if="mode !== 'forgot'"
        type="button"
        class="btn-gitee"
        :disabled="loading"
        @click="handleOAuth('gitee')"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
          <path d="M11.983 0H4C1.79 0 0 1.79 0 4v16c0 2.21 1.79 4 4 4h16c2.21 0 4-1.79 4-4v-7.5h-9.5v3.5h5.5v2.5h-8.5v-11h12c0-2.21-1.79-4-4-4h-3.517z" />
        </svg>
        使用 Gitee 登录
      </button>

      <!-- 返回登录(forgot 模式) -->
      <a
        v-if="mode === 'forgot'"
        class="link-center"
        @click="switchMode('login')"
      >
        ← 返回登录
      </a>
    </div>
  </div>
</template>

<style scoped>
/* ---- 页面布局:居中卡片 ---- */
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  /* 手机地址栏伸缩兜底 */
  min-height: 100dvh;
  padding: var(--space-4);
  background: linear-gradient(135deg, #f0f4ff 0%, #f8fafc 50%, #faf5ff 100%);
}

/* 暗色:保留渐变质感,色板整体加深(蓝→中性→紫,与浅色渐变对应) */
:root[data-theme='dark'] .auth-page {
  background: linear-gradient(135deg, #141b3c 0%, #0f172a 50%, #1c1330 100%);
}

/* ---- 卡片容器 ---- */
.auth-card {
  width: 100%;
  max-width: var(--auth-card-width);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  padding: 10px var(--space-8);
}

/* ---- 品牌区 ---- */
.auth-header {
  text-align: center;
  margin-bottom: var(--space-4);
}

.logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 72px;
  height: 72px;
  border-radius: var(--radius-lg);
  margin-bottom: var(--space-1);
}

.auth-header h1 {
  font-size: var(--fs-xl);
  font-weight: var(--fw-bold);
  letter-spacing: -0.02em;
}

.subtitle {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin-top: var(--space-1);
}

/* ---- Tab 切换 ---- */
.auth-tabs {
  display: flex;
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
  padding: 4px;
  margin-bottom: var(--space-6);
}

.tab {
  flex: 1;
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.tab:hover {
  color: var(--color-text);
}

.tab.active {
  background: var(--color-surface);
  color: var(--color-primary);
  box-shadow: var(--shadow-sm);
}

/* ---- 模式标题 ---- */
.mode-title {
  text-align: center;
  font-size: var(--fs-lg);
  margin-bottom: var(--space-6);
}

/* ---- 提示信息 ---- */
.alert {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert svg {
  flex-shrink: 0;
  margin-top: 2px;
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid color-mix(in srgb, var(--color-danger) 35%, transparent);
}

.alert-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border: 1px solid color-mix(in srgb, var(--color-success) 35%, transparent);
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  margin-bottom: var(--space-2);
}

.field input {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
}

.field input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input.invalid {
  border-color: var(--color-danger);
}

.field input.invalid:focus {
  box-shadow: 0 0 0 3px var(--color-danger-light);
}

/* 密码输入包装:input + 显示/隐藏按钮 */
.input-wrapper {
  position: relative;
}

.input-wrapper input {
  padding-right: 44px;
}

.toggle-pwd {
  position: absolute;
  right: var(--space-2);
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--color-text-muted);
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background var(--transition-fast);
}

.toggle-pwd:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

/* 字段错误 */
.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- 链接 ---- */
.link-right {
  display: block;
  text-align: right;
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
  cursor: pointer;
}

.link-center {
  display: block;
  text-align: center;
  font-size: var(--fs-sm);
  margin-top: var(--space-4);
  cursor: pointer;
}

/* ---- 按钮 ---- */
.btn-primary {
  width: 100%;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-primary:active:not(:disabled) {
  background: var(--color-primary-active);
}

.btn-primary:disabled {
  background: var(--color-primary);
  opacity: 0.6;
}

/* GitHub 按钮 */
.btn-github {
  width: 100%;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: white;
  background: var(--color-github);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-github:hover:not(:disabled) {
  background: var(--color-github-hover);
}

.btn-github:disabled {
  opacity: 0.6;
}

/* Gitee 按钮(Gitee 红,与 GitHub 按钮同尺寸) */
.btn-gitee {
  width: 100%;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: white;
  background: var(--color-gitee);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
  margin-top: var(--space-3);
}

.btn-gitee:hover:not(:disabled) {
  background: var(--color-gitee-hover);
}

.btn-gitee:disabled {
  opacity: 0.6;
}

/* ---- 分割线 ---- */
.divider {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: var(--space-5) 0;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

.divider::before,
.divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--color-border);
}

/* ---- 加载动画 ---- */
.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 过渡动画 ---- */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-base);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* ---- 响应式 ---- */
@media (max-width: 480px) {
  .auth-card {
    padding: 10px var(--space-5) var(--space-5);
  }
}
</style>
