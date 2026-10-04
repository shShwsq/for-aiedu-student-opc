/**
 * 长期记忆管理 API 模块
 *
 * 对应后端 app/routers/memory.py 的端点(全部鉴权):
 * - GET    /memory/preferences         User Profile (未配置返回空默认值)
 * - PUT    /memory/preferences         保存 User Profile (get_or_create)
 * - GET    /memory/global              全局长期记忆(未配置返回空)
 * - PUT    /memory/global              保存全局长期记忆(get_or_create)
 * - GET    /memory/projects            项目记忆列表(按 updated_at 倒序)
 * - GET    /memory/projects/{id}       单个项目记忆详情
 * - PUT    /memory/projects/{id}       更新 alias/note/memory_content
 * - DELETE /memory/projects/{id}       删除项目记忆(返回剩余列表)
 *
 * 返回值已解包(取 response.data),调用方直接拿业务数据。
 */
import client from './client'
import type {
  ProjectListResponse,
  ProjectOut,
  SaveAgentPolicyRequest,
  SaveMemorySettingsRequest,
  SavePracticeSettingsRequest,
  SaveProjectRequest,
  SaveUserMemoryRequest,
  SaveUserPreferenceRequest,
  StructureDefaults,
  UserMemoryOut,
  UserPreferenceOut,
} from '@/types/memory'

// ============================================================
// User Profile (1:1)
// ============================================================

/** 获取当前 User Profile (未配置返回空默认值) */
export function getPreferences(): Promise<UserPreferenceOut> {
  return client.get('/memory/preferences').then((r) => r.data)
}

/** 保存/更新 User Profile (get_or_create) */
export function savePreferences(body: SaveUserPreferenceRequest): Promise<UserPreferenceOut> {
  return client.put('/memory/preferences', body).then((r) => r.data)
}

/** 保存/更新练习设置(任务完成后自动生成练习题开关) */
export function savePracticeSettings(
  body: SavePracticeSettingsRequest,
): Promise<UserPreferenceOut> {
  return client.put('/memory/preferences/practice', body).then((r) => r.data)
}

/** 保存/更新 agent 策略配置(agent2 启停、验证权限等) */
export function saveAgentPolicy(body: SaveAgentPolicyRequest): Promise<UserPreferenceOut> {
  return client.put('/memory/preferences/agent_policy', body).then((r) => r.data)
}

/** 保存/更新记忆生成设置(总开关 / 归纳模型 / 结构模式 / 思考模式 / 注入上限) */
export function saveMemorySettings(
  body: SaveMemorySettingsRequest,
): Promise<UserPreferenceOut> {
  return client.put('/memory/preferences/memory_settings', body).then((r) => r.data)
}

/** 获取系统默认结构化类别(内置静态常量;供面板对照与「恢复系统默认」) */
export function getStructureDefaults(): Promise<StructureDefaults> {
  return client.get('/memory/preferences/structure_defaults').then((r) => r.data)
}

// ============================================================
// 全局长期记忆(1:1)
// ============================================================

/** 获取当前用户的全局长期记忆(未配置返回空) */
export function getGlobalMemory(): Promise<UserMemoryOut> {
  return client.get('/memory/global').then((r) => r.data)
}

/** 保存/更新全局长期记忆(get_or_create) */
export function saveGlobalMemory(body: SaveUserMemoryRequest): Promise<UserMemoryOut> {
  return client.put('/memory/global', body).then((r) => r.data)
}

// ============================================================
// 分项目记忆(1:N)
// ============================================================

/** 获取当前用户的所有项目记忆列表(按 updated_at 倒序) */
export function listProjects(): Promise<ProjectListResponse> {
  return client.get('/memory/projects').then((r) => r.data)
}

/** 获取单个项目记忆详情 */
export function getProject(projectId: string): Promise<ProjectOut> {
  return client.get(`/memory/projects/${projectId}`).then((r) => r.data)
}

/** 更新项目记忆的 alias/note/memory_content(用户手动编辑) */
export function saveProject(projectId: string, body: SaveProjectRequest): Promise<ProjectOut> {
  return client.put(`/memory/projects/${projectId}`, body).then((r) => r.data)
}

/** 删除项目记忆(整行删除,返回剩余列表) */
export function deleteProject(projectId: string): Promise<ProjectListResponse> {
  return client.delete(`/memory/projects/${projectId}`).then((r) => r.data)
}
