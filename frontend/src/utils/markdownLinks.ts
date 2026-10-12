/**
 * Markdown 预览链接的分类与解析
 *
 * 背景:预览用 renderMarkdown(marked + DOMPurify)把 README 里的链接原样渲染成
 * `<a href="...">`。若放任浏览器默认行为,相对链接会按**当前 SPA URL** 解析并整页
 * 跳转 —— 例如在 `/tasks/{uuid}` 页点 `[x](README.md)` → 落到 `/tasks/README.md` →
 * 命中 `/tasks/:id` 路由 → 用非 UUID 的 id 请求 `/api/tasks/...` → 后端 Pydantic 报
 * "Input should be a valid UUID"。故预览区所有链接点击都必须先拦下默认跳转,再按
 * 目标类型分流处理。
 *
 * 与展示解耦:纯函数便于单测(见 markdownLinks.test.ts)。
 */

/** 预览链接的处置结果 */
export type PreviewLinkAction =
  /** 绝对外链:新标签打开 */
  | { kind: 'external'; url: string }
  /** 页内锚点:在预览容器内滚动 */
  | { kind: 'anchor'; id: string }
  /** 工作区相对链接:交父组件在应用内打开(path 为工作区相对路径) */
  | { kind: 'workspace'; path: string }
  /** 无意义目标(空 / data:URI / 纯查询):什么都不做 */
  | { kind: 'ignore' }

/** 带协议的目标(http/https/mailto/tel/ftp 等) */
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i
/** 只允许新开标签的白名单协议;其余「协议式」目标(data:、javascript: 等)一律忽略 */
const SAFE_EXTERNAL_RE = /^(?:https?:|mailto:|tel:|ftp:)/i

/** 尽力解码;非法百分号编码不抛错,按原文返回 */
function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s)
  } catch {
    return s
  }
}

/** 当前文件所在目录(工作区相对路径的正斜杠父目录;无目录返回空串) */
function currentFileDir(currentFile: string): string {
  const i = currentFile.lastIndexOf('/')
  return i === -1 ? '' : currentFile.slice(0, i)
}

/**
 * 把 Markdown 相对链接解析成工作区相对路径(相对当前文件所在目录)
 *
 * - 去掉 `#anchor` / `?query`,解 `%xx`
 * - 以 `/` 开头视为**仓库根**相对;否则相对当前文件目录
 * - 折叠 `.` / `..`(与后端 browse_tree 恒为相对 repo_path 的正斜杠路径口径一致)
 *
 * 解析不出有效路径(纯锚点、只有 `..` 越出根等)返回空串。
 */
export function resolveWorkspaceLink(currentFile: string, href: string): string {
  const raw = href.split('#')[0].split('?')[0]
  if (!raw) return ''
  const path = safeDecode(raw)
  const base = currentFileDir(currentFile)
  const joined = path.startsWith('/')
    ? path.replace(/^\/+/, '')
    : base
      ? `${base}/${path}`
      : path
  const out: string[] = []
  for (const seg of joined.split('/')) {
    if (seg === '' || seg === '.') continue
    if (seg === '..') out.pop()
    else out.push(seg)
  }
  return out.join('/')
}

/**
 * 判定预览链接点击应如何处置(不做任何 DOM/window 副作用,纯决策)
 *
 * @param currentFile 当前预览文件的工作区相对路径(可为空串,如仓库根 README.md)
 * @param href        anchor 的原始 href
 */
export function classifyPreviewLink(currentFile: string, href: string): PreviewLinkAction {
  if (!href) return { kind: 'ignore' }
  // 内联资源(data:)不作为链接处置
  if (/^data:/i.test(href)) return { kind: 'ignore' }
  // 带协议:仅放行安全外链,其余(javascript: 等)忽略
  if (SCHEME_RE.test(href)) {
    return SAFE_EXTERNAL_RE.test(href) ? { kind: 'external', url: href } : { kind: 'ignore' }
  }
  // 页内锚点
  if (href.startsWith('#')) {
    const id = safeDecode(href.slice(1))
    return id ? { kind: 'anchor', id } : { kind: 'ignore' }
  }
  // 工作区相对/根相对链接
  const path = resolveWorkspaceLink(currentFile, href)
  return path ? { kind: 'workspace', path } : { kind: 'ignore' }
}
