/**
 * 工作区浏览 API
 *
 * 对应后端 app/routers/workspace.py:
 * - GET /tasks/{id}/workspace          工作区信息
 * - GET /tasks/{id}/workspace/files    列出目录
 * - GET /tasks/{id}/workspace/file     读取文件
 * - GET /tasks/{id}/workspace/uploads/tree 沙箱过期后回退浏览用户上传文件树
 * - GET /tasks/{id}/workspace/uploads/file 回退读取上传文件内容
 * - POST /tasks/{id}/workspace/restore 发起过期工作区重新 clone(后台执行)
 * - GET  /tasks/{id}/workspace/restore/status 查询恢复进度(轮询)
 */
import client from './client'
import type {
  WorkspaceFileResponse,
  WorkspaceFilesResponse,
  WorkspaceInfo,
  WorkspaceRestoreStatus,
  WorkspaceTreeResponse,
  WorkspaceUploadsTreeResponse,
} from '@/types/workspace'

/** 获取工作区信息(是否可浏览) */
export function getWorkspaceInfo(taskId: string): Promise<WorkspaceInfo> {
  return client.get(`/tasks/${taskId}/workspace`).then((r) => r.data)
}

/** 获取整树快照(首屏一次拉取,替代逐级懒加载) */
export function getWorkspaceTree(
  taskId: string,
  refresh: boolean = false,
): Promise<WorkspaceTreeResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/tree`, { params: { refresh } })
    .then((r) => r.data)
}

/** 列出工作区某目录下的文件(单层,懒加载树) */
export function listWorkspaceFiles(
  taskId: string,
  subdir: string = '',
): Promise<WorkspaceFilesResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/files`, { params: { subdir } })
    .then((r) => r.data)
}

/** 读取工作区内文件内容(原始文本 + 分页,前端自行渲染行号) */
export function readWorkspaceFile(
  taskId: string,
  path: string,
  offset: number = 1,
  maxLines: number = 500,
): Promise<WorkspaceFileResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/file`, {
      params: { path, offset, max_lines: maxLines },
    })
    .then((r) => r.data)
}

/** 沙箱过期后回退浏览:拉用户上传文件树(不经过沙箱,保留期内内容可读) */
export function getWorkspaceUploadsTree(
  taskId: string,
  refresh: boolean = false,
): Promise<WorkspaceUploadsTreeResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/uploads/tree`, { params: { refresh } })
    .then((r) => r.data)
}

/** 沙箱过期后回退浏览:读用户上传文件内容(原始文本 + 分页) */
export function readWorkspaceUploadsFile(
  taskId: string,
  path: string,
  offset: number = 1,
  maxLines: number = 500,
): Promise<WorkspaceFileResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/uploads/file`, {
      params: { path, offset, max_lines: maxLines },
    })
    .then((r) => r.data)
}

/** 发起工作区恢复(用户显式操作):后台重新 clone,立即返回 job 快照
 *
 * 不在本请求里等克隆完成:大仓库分钟级,而 api/client.ts 有 30s 全局超时,
 * 旧版同步等会被当成"网络错误"报给用户(真实结果几分钟后才落)。
 * 进度与终态走 getWorkspaceRestoreStatus 轮询。
 */
export function startWorkspaceRestore(taskId: string): Promise<WorkspaceRestoreStatus> {
  return client.post(`/tasks/${taskId}/workspace/restore`).then((r) => r.data)
}

/** 查询恢复进度与终态(state: idle / running / done / failed) */
export function getWorkspaceRestoreStatus(taskId: string): Promise<WorkspaceRestoreStatus> {
  return client.get(`/tasks/${taskId}/workspace/restore/status`).then((r) => r.data)
}
