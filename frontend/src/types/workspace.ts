/**
 * 工作区浏览相关类型
 *
 * 对应后端 app/routers/workspace.py 的端点
 */

/** 目录条目(文件或子目录) */
export interface WorkspaceEntry {
  name: string
  type: 'file' | 'dir'
  size: number
}

/** 列出目录的响应 */
export interface WorkspaceFilesResponse {
  path: string
  entries: WorkspaceEntry[]
  total: number
  truncated: boolean
}

/** 工作区信息 */
export interface WorkspaceInfo {
  available: boolean
  reason: string | null
  repo_path: string
  completed: boolean
  mode: string
  /** 任务是否带用户上传(沙箱过期后前端回退浏览的依据;旧后端无此字段) */
  has_uploads?: boolean
  /** 任务是否带 repo_url(工作区过期后「重新克隆」按钮的显示依据;纯上传任务不显示) */
  can_restore?: boolean
}

/** 恢复 job 状态:idle=无 job(从未发起 / 后端重启清了), running=克隆中 */
export type WorkspaceRestoreState = 'idle' | 'running' | 'done' | 'failed'

/** 工作区恢复 job 快照
 *
 * POST .../workspace/restore 发起(立即返回),GET .../workspace/restore/status 轮询。
 * 克隆是分钟级操作,旧版同步等请求被 axios 30s 超时打断后只能报"网络错误",
 * 改成两段式后的进度与真实失败原因都在这里。
 */
export interface WorkspaceRestoreStatus {
  state: WorkspaceRestoreState
  available: boolean
  repo_path: string
  mode: string
  /** 克隆进度百分比(running 时实时更新) */
  percent: number
  /** 最近一条 git 进度行(展示用,可能为空) */
  message: string
  /** failed 时的真实原因(协议回退链聚合错误) */
  error: string
  /** 发起时间戳(后端 time.time(),前端只作参考) */
  started_at?: number
}

/** 整树快照条目(相对仓库根的路径) */
export interface WorkspaceTreeEntry {
  path: string
  type: 'file' | 'dir'
}

/** 整树快照的响应 */
export interface WorkspaceTreeResponse {
  entries: WorkspaceTreeEntry[]
  /** 超上限截断(未覆盖目录前端退回懒加载) */
  truncated: boolean
  /** 快照实际覆盖深度(降级时可能小于请求值) */
  max_depth: number
}

/** 沙箱过期后回退浏览的用户上传文件树响应(GET .../workspace/uploads/tree) */
export interface WorkspaceUploadsTreeResponse extends WorkspaceTreeResponse {
  /** 已被 GC 清理的上传占位标签(展示"已清理"标记) */
  unavailable: string[]
}

/** 读取文件的响应 */
export interface WorkspaceFileResponse {
  path: string
  content: string
  start_line: number
  end_line: number
  total_lines: number
  truncated: boolean
  /** 二进制文件(docx/pdf/图片…):content 为占位文案,改渲染下载卡片 */
  binary: boolean
  /** 文件字节数(仅 binary=true 时有意义;文本态后端回 0) */
  size: number
}

/** 下载文件的响应(blob + 服务端建议的文件名) */
export interface WorkspaceDownloadResult {
  blob: Blob
  /** 服务端 Content-Disposition 给出的文件名(中文名靠它),缺失时回落路径末段 */
  filename: string
}
