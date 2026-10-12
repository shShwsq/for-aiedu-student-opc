/**
 * 认证状态管理(Pinia)
 *
 * 职责:
 * - 持有当前用户状态(user / isAuthenticated)
 * - 对接 auth API:登录/注册/OAuth/获取当前用户
 * - 管理 token 的存储与清理(委托给 api/client.ts 的工具函数)
 *
 * 不直接操作 localStorage token,通过 client.ts 的 setTokens/clearTokens 间接管理,
 * 保持职责单一:store 管用户状态,client 管 HTTP + token 注入。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import * as authApi from '@/api/auth'
import { clearTokens, getAccessToken, setTokens } from '@/api/client'
import { invalidateLlmConfigs } from '@/composables/useLlmConfigOptions'
import { invalidateTaskModelSources } from '@/composables/useTaskRuntimeModels'
import type { GitProvider } from '@/types/git_provider'
import type { User } from '@/types/auth'

export const useAuthStore = defineStore('auth', () => {
  // ---- state ----

  const user = ref<User | null>(null)
  /** 初始化时从 localStorage 判断(有 token 才值得 fetchMe) */
  const hasToken = ref<boolean>(!!getAccessToken())

  // ---- getters ----

  const isAuthenticated = computed(() => user.value !== null)
  const emailVerified = computed(() => user.value?.email_verified ?? false)

  // ---- actions ----

  /** 登录:调 API → 存 token → 设置 user */
  async function login(email: string, password: string): Promise<void> {
    const res = await authApi.login(email, password)
    setTokens(res.access_token, res.refresh_token)
    user.value = res.user
    hasToken.value = true
  }

  /** 注册:只调 API,不自动登录(需先验证邮箱) */
  async function register(email: string, password: string): Promise<string> {
    const res = await authApi.register(email, password)
    return res.message
  }

  /** Git 平台 OAuth 登录:用 code 换 token → 存 token → 设置 user(支持 GitHub / Gitee) */
  async function handleOAuthCallback(provider: GitProvider, code: string): Promise<void> {
    const res = await authApi.oauthLogin(provider, code)
    setTokens(res.access_token, res.refresh_token)
    user.value = res.user
    hasToken.value = true
  }

  /** 获取当前用户(用于应用启动时恢复会话) */
  async function fetchMe(): Promise<void> {
    if (!getAccessToken()) return
    try {
      user.value = await authApi.getMe()
    } catch {
      // token 无效或过期,client 拦截器会处理跳转
      user.value = null
      hasToken.value = false
    }
  }

  /** 登出:清空 token + user + 模型相关模块级缓存 */
  function logout(): void {
    clearTokens()
    user.value = null
    hasToken.value = false
    // 模型列表是模块级缓存(按用户私有):不清会让下一个登录者在同一标签页里
    // 看到上一个账号的模型名(归属校验会拒掉选择,但展示本身就是泄露面)
    invalidateLlmConfigs()
    // 「任务默认审查模型」槽位名与环境默认模型名也是同一类展示信息(存在
    // useTaskRuntimeModels 的模块级 ref 里),不跟着清的话详情页「对话模型」会先报
    // 上一个账号的默认模型,直到自己的请求落地
    invalidateTaskModelSources()
  }

  return {
    // state
    user,
    hasToken,
    // getters
    isAuthenticated,
    emailVerified,
    // actions
    login,
    register,
    handleOAuthCallback,
    fetchMe,
    logout,
  }
})
