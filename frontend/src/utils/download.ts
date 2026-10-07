/**
 * 浏览器端文件下载工具
 *
 * 下载走 axios blob 响应而不是 `<a href=接口地址>`:access token 存在
 * localStorage 并由请求拦截器注入 Authorization 头(见 api/client.ts),
 * 裸链接请求带不上凭证会被判 401/403。
 *
 * 副作用是错误体也变成 Blob:`err.response.data.detail` 取不到,
 * 于是"文件过大(233.4MB)"会退化成"请求失败(413)"。`normalizeBlobError`
 * 负责把这段 JSON 读回来,让调用方仍能走 extractErrorMessage。
 */
import axios from 'axios'

/** 从 Content-Disposition 解析下载文件名
 *
 * 优先 RFC 5987 的 `filename*=UTF-8''…`(中文名在这里才不失真),
 * 再退回 RFC 6266 的 `filename="…"`,两者都没有时用 fallback(一般是路径末段)。
 */
export function filenameFromDisposition(
  header: string | null | undefined,
  fallback: string,
): string {
  const value = (header ?? '').trim()
  if (!value) return fallback

  const ext = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/i.exec(value)
  if (ext?.[1]) {
    try {
      return decodeURIComponent(ext[1].trim().replace(/^["']|["']$/g, ''))
    } catch {
      /* 百分号编码不合法时回落到 filename= */
    }
  }

  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(value)
  if (plain?.[1]) return plain[1].trim() || fallback

  return fallback
}

/** 取路径末段作为文件名(兼容 windows 反斜杠;空路径返回兜底名) */
export function basenameOf(path: string | null | undefined, fallback = 'download'): string {
  const value = (path ?? '').trim()
  if (!value) return fallback
  const name = value.replace(/\\/g, '/').split('/').filter(Boolean).pop() ?? ''
  return name || fallback
}

/** blob 响应触发的浏览器下载(用完即回收 object URL,避免内存泄漏) */
export function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/** 从错误响应正文里取 FastAPI 的 detail(非 JSON / 无 detail 返回 null) */
export function detailFromErrorBody(text: string): string | null {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown }
    return typeof parsed?.detail === 'string' && parsed.detail ? parsed.detail : null
  } catch {
    return null
  }
}

/** 读 Blob 文本:jsdom 等环境可能没有 Blob.text(),逐级降级 */
async function readBlobText(blob: Blob): Promise<string> {
  if (typeof blob.text === 'function') return blob.text()
  if (typeof blob.arrayBuffer === 'function') {
    return new TextDecoder().decode(await blob.arrayBuffer())
  }
  return ''
}

/**
 * 把 responseType='blob' 请求的 4xx 错误还原成可读 Error
 *
 * 返回原错误时调用方仍由 extractErrorMessage 给出通用文案(网络/超时/状态码);
 * 只有错误体确实是带 detail 的 JSON 时才替换,避免把 HTML 错误页整段抛给用户。
 */
export async function normalizeBlobError(err: unknown): Promise<unknown> {
  if (!axios.isAxiosError(err)) return err
  const data = err.response?.data
  if (!(data instanceof Blob)) return err
  const detail = detailFromErrorBody(await readBlobText(data))
  return detail ? new Error(detail) : err
}
