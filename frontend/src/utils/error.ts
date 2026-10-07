/**
 * 错误处理工具
 *
 * 从 axios 错误中提取后端返回的 detail 字段(FastAPI HTTPException 格式)
 */
import axios, { type AxiosError } from 'axios'

/**
 * axios 超时/主动取消:这两类同样没有 response,但含义与断网完全不同
 *
 * 旧版把它们归到"网络错误",于是长耗时接口(如同步等克隆时的 30s 全局超时)误导用户
 * 去检查网线,而后端其实正常在跑。按 code/消息先区分开。
 */
function isClientTimeout(err: AxiosError): boolean {
  if (err.code === 'ECONNABORTED' || err.code === 'ETIMEDOUT') return true
  return /timeout of \d+ms exceeded/i.test(err.message ?? '')
}

/** 带 HTTP 状态码的错误(状态与可读文案一并保留)
 *
 * responseType='blob' 的失败经 `normalizeBlobError` 还原成普通 Error 时会丢掉
 * response,于是状态码分流(如 410 工作区已过期)在错误链上第二条路径里失效。
 * 归一时把状态码带上,调用方仍能用 isHttpStatus 分流。
 */
export class HttpDetailError extends Error {
  readonly status: number | undefined

  constructor(message: string, status?: number) {
    super(message)
    this.name = 'HttpDetailError'
    this.status = status
  }
}

/** 取错误的 HTTP 状态码(拿不到返回 undefined) */
export function httpStatusOf(err: unknown): number | undefined {
  if (axios.isAxiosError(err)) return err.response?.status
  if (err instanceof HttpDetailError) return err.status
  return undefined
}

/**
 * 判断错误是否为指定 HTTP 状态码
 *
 * 后端有些状态不是"坏了"而是"可预期的状态",靠 detail 文案认不出来(文案会改、
 * 会本地化),得按状态码分流。例:410 = 工作区沙箱已被回收 → 前端应重新查可用性
 * 并亮出「重新克隆」,而不是只把一行错误摆在文件树里。
 */
export function isHttpStatus(err: unknown, status: number): boolean {
  return httpStatusOf(err) === status
}

/**
 * 从未知错误中提取人类可读的消息
 *
 * 优先级:
 * 1. axios 错误 → 后端 detail 字段
 * 2. 客户端取消/超时 → 专属文案(不能当断网报)
 * 3. 无 response / status=0 → 网络错误
 * 4. 其他 HTTP 状态 → 请求失败(状态码)
 * 5. Error 实例 → message;其他 → 未知错误
 */
export function extractErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
    // FastAPI 校验错误 detail 是数组
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0]
      if (first?.msg) return first.msg
    }
    if (err.code === 'ERR_CANCELED') return '请求已取消'
    if (isClientTimeout(err)) {
      return '请求超时:后台可能仍在执行,请稍后刷新页面查看结果'
    }
    if (err.response?.status === 0 || !err.response) {
      return '网络错误,请检查网络连接后重试'
    }
    return `请求失败(${err.response.status})`
  }
  if (err instanceof Error) return err.message
  return '未知错误,请稍后重试'
}
