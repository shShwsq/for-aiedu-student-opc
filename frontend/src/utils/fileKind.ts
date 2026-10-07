/**
 * 文件类型判定(前端侧)
 *
 * 与后端 backend/app/file_kinds.py 的 BINARY_SUFFIXES 是**同一张表**,
 * 改一处必须同步另一处:后端据此决定要不要回传内容,前端据此决定
 * 要不要发起那次内容请求(命中就干脆不请求,免得把几 MB 乱码字节拉过网络)。
 *
 * 后端的 binary 响应标记是最终权威 —— 本表只是零成本的前置短路,
 * 表内未覆盖的二进制(如未知后缀的 ELF)仍会由后端标记兜住。
 */

/** 二进制后缀(小写含点)。与 backend/app/file_kinds.py 同表。 */
export const BINARY_SUFFIXES: ReadonlySet<string> = new Set([
  // 办公文档(docx/xlsx 是 zip 容器,pdf 是二进制流,均无在线预览能力)
  '.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx', '.pdf',
  // 压缩包 / 归档
  '.zip', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.rar',
  // 图片
  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.tiff',
  // 音视频
  '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.ogg',
  // 可执行 / 编译产物
  '.exe', '.dll', '.so', '.dylib', '.jar', '.war', '.class', '.pyc',
  '.o', '.a', '.obj', '.lib', '.bin',
  // 字体 / 数据库 / git 对象
  '.ttf', '.otf', '.woff', '.woff2', '.sqlite', '.db', '.pack', '.idx',
  // 镜像
  '.iso', '.dmg',
])

/** 取小写后缀(含点);无后缀返回空串。兼容 windows 反斜杠路径。 */
export function fileSuffix(path: string | null | undefined): string {
  const name = (path ?? '').replace(/\\/g, '/').split('/').pop() ?? ''
  const dot = name.lastIndexOf('.')
  // 首字符即 "." 的是 dotfile(.gitignore / .env),不是后缀
  if (dot <= 0) return ''
  return name.slice(dot).toLowerCase()
}

/** 仅按后缀判定二进制(零请求成本) */
export function isLikelyBinaryPath(path: string | null | undefined): boolean {
  return BINARY_SUFFIXES.has(fileSuffix(path))
}
