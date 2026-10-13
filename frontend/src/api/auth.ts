/**
 * 认证 API 模块
 *
 * 每个函数对应后端 app/routers/auth.py 的一个端点。
 * 返回值已解包(取 response.data),调用方直接拿业务数据。
 */
import client from './client'
import { getOAuthAuthorizeURL } from '@/api/git_provider'
import type { GitProvider } from '@/types/git_provider'
import type {
  ChangePasswordRequest,
  MessageResponse,
  RefreshResponse,
  TokenResponse,
  User,
} from '@/types/auth'

// ---- 注册 + 邮箱验证 ----

export function register(email: string, password: string): Promise<MessageResponse> {
  return client.post('/auth/register', { email, password }).then((r) => r.data)
}

export function verifyEmail(token: string): Promise<MessageResponse> {
  return client.post('/auth/verify-email', { token }).then((r) => r.data)
}

// ---- 登录 + 刷新 ----

export function login(email: string, password: string): Promise<TokenResponse> {
  return client.post('/auth/login', { email, password }).then((r) => r.data)
}

export function refreshToken(refresh_token: string): Promise<RefreshResponse> {
  return client.post('/auth/refresh', { refresh_token }).then((r) => r.data)
}

// ---- 当前用户 ----

export function getMe(): Promise<User> {
  return client.get('/auth/me').then((r) => r.data)
}

// ---- 忘记密码 + 重置密码 ----

export function forgotPassword(email: string): Promise<MessageResponse> {
  return client.post('/auth/password/forgot', { email }).then((r) => r.data)
}

export function resetPassword(token: string, new_password: string): Promise<MessageResponse> {
  return client.post('/auth/password/reset', { token, new_password }).then((r) => r.data)
}

// ---- 修改密码(已登录) ----

export function changePassword(req: ChangePasswordRequest): Promise<MessageResponse> {
  return client.post('/auth/password/change', req).then((r) => r.data)
}

// ---- 删除账号(硬删除,需输入邮箱二次确认) ----

export function deleteAccount(email: string): Promise<MessageResponse> {
  return client.delete('/auth/account', { data: { email } }).then((r) => r.data)
}

// ---- Git 平台 OAuth 登录(GitHub / Gitee 统一入口) ----

/**
 * Git 平台 OAuth 登录:用 code 换 token + 创建/关联账号
 *
 * 对应后端 POST /auth/oauth/{provider},provider ∈ 'github' | 'gitee'。
 */
export function oauthLogin(provider: GitProvider, code: string): Promise<TokenResponse> {
  return client.post(`/auth/oauth/${provider}`, { code }).then((r) => r.data)
}

/**
 * 拼接 Git 平台 OAuth 授权链接(登录用,scope 仅用户信息)
 *
 * 已委托给 api/git_provider.ts 的 getOAuthAuthorizeURL,统一 GitHub / Gitee 元信息。
 * 前端直接跳转到此 URL,用户授权后平台回调到 redirect_uri?code=XXX
 */
export { getOAuthAuthorizeURL }
