<script setup lang="ts">
/**
 * Git 平台 OAuth 回调处理页(统一 GitHub / Gitee)
 *
 * 同一个 redirect_uri 承担两种场景,按当前登录状态分流:
 * - 已登录:用户从设置页"绑定 GitHub/Gitee"过来 → 调 POST /git/{provider}/bind 写入 access_token
 * - 未登录:用户从登录页"GitHub/Gitee 登录"过来 → 调 POST /auth/oauth/{provider} 走登录
 *
 * provider 由路由名识别('github-callback' / 'gitee-callback')。
 * 区分登录态:authStore.isAuthenticated(页面刷新场景由路由守卫已 fetchMe 恢复)
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { bindGitProvider } from '@/api/git_provider'
import { useAuthStore } from '@/stores/auth'
import type { GitProvider } from '@/types/git_provider'
import { extractErrorMessage } from '@/utils/error'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const status = ref<'loading' | 'error'>('loading')
const errorMsg = ref('')

/** 当前回调对应的 provider(由路由名识别) */
const provider = computed<GitProvider>(() =>
  route.name === 'gitee-callback' ? 'gitee' : 'github',
)

/** 平台显示名(加载/错误文案用) */
const displayName = computed(() => (provider.value === 'gitee' ? 'Gitee' : 'GitHub'))

onMounted(async () => {
  const code = route.query.code as string | undefined
  const oauthError = route.query.error as string | undefined

  // 用户拒绝授权
  if (oauthError) {
    status.value = 'error'
    errorMsg.value = `${displayName.value} 授权已取消`
    return
  }

  if (!code) {
    status.value = 'error'
    errorMsg.value = '回调参数缺少 code'
    return
  }

  try {
    if (authStore.isAuthenticated) {
      // 已登录 → 绑定流程:用 code 换 token 并加密落库
      const res = await bindGitProvider(provider.value, { code })
      // 邮箱不一致时带 query 跳设置页,由设置页弹窗询问是否同步
      // (仅 GitHub 会出现 email_mismatch=true;Gitee 不支持可验证邮箱,恒为 false)
      if (res.email_mismatch && res.provider_email) {
        await router.push({
          path: '/settings/account',
          query: {
            email_mismatch: '1',
            provider: provider.value,
            provider_email: res.provider_email,
            current_email: res.current_email ?? '',
          },
        })
      } else {
        // 绑定成功跳回设置页(用户能看到绑定状态)
        await router.push('/settings/account')
      }
    } else {
      // 未登录 → 登录流程:用 code 换 token + 创建/关联账号
      await authStore.handleOAuthCallback(provider.value, code)
      await router.push('/')
    }
  } catch (err) {
    status.value = 'error'
    errorMsg.value = extractErrorMessage(err)
  }
})
</script>

<template>
  <div class="callback-page">
    <!-- 加载中 -->
    <div v-if="status === 'loading'" class="callback-card">
      <div class="spinner-lg" />
      <p>正在完成 {{ displayName }} 登录...</p>
    </div>

    <!-- 失败 -->
    <div v-else class="callback-card">
      <div class="error-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10" />
          <line x1="15" y1="9" x2="9" y2="15" />
          <line x1="9" y1="9" x2="15" y2="15" />
        </svg>
      </div>
      <p class="error-text">{{ errorMsg }}</p>
      <!-- 已登录说明走的是绑定流程,回设置页重新授权(回登录页会被守卫弹回首页,
           反而看不到绑定入口);未登录才是登录失败 -->
      <RouterLink
        :to="authStore.isAuthenticated ? '/settings/account' : '/login'"
        class="back-link"
      >
        {{ authStore.isAuthenticated ? '返回重新绑定' : '返回登录' }}
      </RouterLink>
    </div>
  </div>
</template>

<style scoped>
.callback-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  /* 手机地址栏伸缩兜底 */
  min-height: 100dvh;
  background: var(--color-bg);
}

.callback-card {
  text-align: center;
  padding: var(--space-8);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  max-width: 360px;
}

.callback-card p {
  margin-top: var(--space-4);
  color: var(--color-text-secondary);
}

.spinner-lg {
  width: 40px;
  height: 40px;
  margin: 0 auto;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.error-icon {
  color: var(--color-danger);
}

.error-text {
  color: var(--color-danger) !important;
  font-weight: var(--fw-medium);
}

.back-link {
  display: inline-block;
  margin-top: var(--space-5);
  font-size: var(--fs-sm);
}
</style>
