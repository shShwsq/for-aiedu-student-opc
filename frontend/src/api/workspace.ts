/**
 * 工作区浏览 API
 *
 * 对应后端 app/routers/workspace.py:
 * - GET /tasks/{id}/workspace          工作区信息
 * - GET /tasks/{id}/workspace/files    列出目录
 * - GET /tasks/{id}/workspace/file     读取文件
 * - GET /tasks/{id}/workspace/download 下载文件(二进制文件的出口)
 * - GET /tasks/{id}/workspace/uploads/tree 沙箱过期后回退浏览用户上传文件树
 * - GET /tasks/{id}/workspace/uploads/file 回退读取上传文件内容
 * - GET /tasks/{id}/workspace/uploads/download 回退下载上传文件
 * - POST /tasks/{id}/workspace/restore 发起过期工作区重新 clone(后台执行)
 * - GET  /tasks/{id}/workspace/restore/status 查询恢复进度(轮询)
 */
import client from './client'
import { basenameOf, filenameFromDisposition, normalizeBlobError } from '@/utils/download'
import type {
  WorkspaceDownloadResult,
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

/** 下载单文件的超时(毫秒)
 *
 * 必须显式覆盖 api/client.ts 的 30s 全局超时:下载几百 MB 会被打断并误报成
 * "网络错误"(工作区重新克隆当年踩的是同一个坑)。
 */
const DOWNLOAD_TIMEOUT_MS = 120_000

/** 拉 blob 并还原服务端文件名;4xx 的 JSON 错误体在 blob 响应下读不出 detail,先归一 */
async function fetchWorkspaceBlob(
  url: string,
  path: string,
): Promise<WorkspaceDownloadResult> {
  try {
    const r = await client.get(url, {
      params: { path },
      responseType: 'blob',
      timeout: DOWNLOAD_TIMEOUT_MS,
    })
    return {
      blob: r.data as Blob,
      filename: filenameFromDisposition(
        r.headers?.['content-disposition'] as string | undefined,
        basenameOf(path),
      ),
    }
  } catch (err) {
    throw await normalizeBlobError(err)
  }
}

/** 下载工作区文件(二进制文件的出口;沙箱 session 过期后不可用) */
export function downloadWorkspaceFile(
  taskId: string,
  path: string,
): Promise<WorkspaceDownloadResult> {
  return fetchWorkspaceBlob(`/tasks/${taskId}/workspace/download`, path)
}

/** 回退下载用户上传文件(不经沙箱,上传保留期内始终可取回原件) */
export function downloadWorkspaceUploadsFile(
  taskId: string,
  path: string,
): Promise<WorkspaceDownloadResult> {
  return fetchWorkspaceBlob(`/tasks/${taskId}/workspace/uploads/download`, path)
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
