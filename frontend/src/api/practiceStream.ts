/**
 * 出题进度 SSE 客户端封装(对应后端 GET /practice/generate/{job_id}/stream)
 *
 * 浏览器原生 EventSource 不能自定义 header,所以通过 ?token=XXX 传递鉴权
 * (与 api/stream.ts 的任务事件流同模式)。
 *
 * 用法:
 *   const es = subscribeGenerateStream(jobId, {
 *     onSnapshot: (data) => { ... },   // 初始快照(含 recent_text 兜底)
 *     onToken: (data) => { ... },      // LLM 输出增量(打字机效果)
 *     onDone: (data) => { ... },
 *   })
 *   // 组件卸载/切换 job 时:es.close()
 */
import { getAccessToken } from './client'
import type {
  GenerateDoneData,
  GenerateErrorData,
  GenerateExplainData,
  GenerateFindingData,
  GenerateProgressData,
  GenerateRestoreData,
  GenerateSnapshotData,
  GenerateTokenData,
  GenerateToolData,
} from '@/types/practice'

/** 出题进度流事件回调 */
export interface GenerateStreamCallbacks {
  /** 连接建立的初始快照(进度/当前 finding/尾部输出文本) */
  onSnapshot?: (data: GenerateSnapshotData) => void
  /** 开始处理某条发现 */
  onFinding?: (data: GenerateFindingData) => void
  /** LLM 输出增量(打字机效果) */
  onToken?: (data: GenerateTokenData) => void
  /** 出题工具循环的工具调用记录 */
  onTool?: (data: GenerateToolData) => void
  /** 出题前工作区恢复(沙箱已清理时重新 clone)的进度 */
  onRestore?: (data: GenerateRestoreData) => void
  /** 收尾知识点讲解阶段(开关开启才有;用于补一行「讲解已更新 N 条」) */
  onExplain?: (data: GenerateExplainData) => void
  /** 进度计数更新(每处理完一条 finding) */
  onProgress?: (data: GenerateProgressData) => void
  /** 生成完成(终止事件) */
  onDone?: (data: GenerateDoneData) => void
  /** 生成失败(终止事件) */
  onError?: (data: GenerateErrorData) => void
}

/**
 * 订阅某个出题 job 的实时事件流
 *
 * 返回 EventSource 实例,调用 .close() 取消订阅。
 */
export function subscribeGenerateStream(
  jobId: string,
  callbacks: GenerateStreamCallbacks,
): EventSource {
  const token = getAccessToken()
  const params = new URLSearchParams()
  if (token) params.set('token', token)

  const url = `/api/practice/generate/${jobId}/stream?${params.toString()}`
  console.log('[gen-sse] 发起订阅', jobId)
  const t0 = performance.now()
  const es = new EventSource(url)

  // [诊断] 事件到达计数,每 2 秒汇总一次,定位事件风暴
  const counts: Record<string, number> = {}
  const countTimer = setInterval(() => {
    if (Object.keys(counts).length === 0) return
    console.warn('[gen-sse] 近 2 秒事件量', { ...counts })
    for (const k of Object.keys(counts)) delete counts[k]
  }, 2000)

  const register = <T>(type: string, cb?: (data: T) => void, terminal = false) => {
    es.addEventListener(type, (e: MessageEvent) => {
      counts[type] = (counts[type] ?? 0) + 1
      try {
        cb?.(JSON.parse(e.data) as T)
      } catch (err) {
        console.error('出题进度 SSE 事件解析失败:', err, e.data)
      }
      if (terminal) {
        clearInterval(countTimer)
        es.close()
      }
    })
  }

  register('snapshot', callbacks.onSnapshot)
  register('finding', callbacks.onFinding)
  register('token', callbacks.onToken)
  register('tool', callbacks.onTool)
  register('restore', callbacks.onRestore)
  register('explain', callbacks.onExplain)
  register('progress', callbacks.onProgress)
  register('done', callbacks.onDone, true)
  register('error', callbacks.onError, true)

  // [诊断] 原生连接错误(e.data 为 undefined)与业务 error 事件区分;
  // 同时记录连接耗时与断开原因,排查连接反复建立/中断
  es.onerror = () => {
    console.warn('[gen-sse] 连接错误/关闭', jobId, {
      readyState: es.readyState,
      sinceOpenMs: Math.round(performance.now() - t0),
    })
    clearInterval(countTimer)
  }

  return es
}
