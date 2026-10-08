/**
 * 练习模块展示层格式化(纯函数)
 *
 * 练习首页、历史面板、题目详情弹窗、出题进度侧栏共用同一套口径,避免各组件各写一份
 * 导致同一个「正确率 / 日期 / 难度 / job 状态」在不同段落显示格式不一致。
 * 无数据统一返回全角破折号「—」。
 */

/** 无数据占位符 */
export const EMPTY_TEXT = '—'

/** 正确率(0-1)转百分比;入参缺失或 NaN 时返回占位符 */
export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY_TEXT
  return `${(value * 100).toFixed(digits)}%`
}

/** 日期(仅年月日);无法解析时原样返回 */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return EMPTY_TEXT
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleDateString('zh-CN')
  } catch {
    return iso
  }
}

/** 日期时间(月日时分,历史作答流水用);无法解析时原样返回 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return EMPTY_TEXT
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

/** 难度(1-5,LLM 初评后按作答微调):整数不带小数,小数保留 1 位 */
export function formatDifficulty(difficulty: number | null | undefined): string {
  if (difficulty === null || difficulty === undefined || Number.isNaN(difficulty)) {
    return EMPTY_TEXT
  }
  return Number.isInteger(difficulty) ? String(difficulty) : difficulty.toFixed(1)
}

/** 选项下标 → 字母(0=A);缺失或越界返回占位符 */
export function optionLetter(idx: number | null | undefined): string {
  if (idx === null || idx === undefined || idx < 0 || idx >= 26) return EMPTY_TEXT
  return String.fromCharCode(65 + idx)
}

/** 题目状态中文标签(与题库管理筛选按钮文案一致) */
export function questionStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'draft':
      return '待确认'
    case 'active':
      return '已入库'
    case 'archived':
      return '已归档'
    default:
      return EMPTY_TEXT
  }
}

/** 题型中文标签(单选 / 判断) */
export function qtypeLabel(qtype: string | null | undefined): string {
  return qtype === 'true_false' ? '判断' : '单选'
}

/** 出题 job 状态文案的输入形状(GenerateJobSummary 的子集,便于单测) */
export interface GenerateJobStatusLike {
  status: string
  done?: number
  total?: number
  created_count?: number
  /** 已请求停止但后台线程还在收尾(协作式取消可能滞后数十秒) */
  stop_requested?: boolean
  /** 工作区恢复阶段(start/progress 时排队中要改说"恢复工作区中") */
  restore?: { phase?: string } | null
}

/**
 * 出题 job 状态中文标签
 *
 * cancelled 是用户点「停止出题」的终态(不是失败);running/pending 期间
 * 若已请求停止,拼上"正在停止" —— 不标的话按钮看起来像没生效。
 */
export function generateJobStatusLabel(
  job: GenerateJobStatusLike | null | undefined,
): string {
  if (!job) return EMPTY_TEXT
  switch (job.status) {
    case 'pending': {
      // 沙箱已清理时先重新 clone(可能数十秒),给出真实阶段避免误以为卡死
      const phase = job.restore?.phase
      if (phase === 'start' || phase === 'progress') return '恢复工作区中'
      return job.stop_requested ? '正在停止' : '排队中'
    }
    case 'running': {
      const progress = `出题中 ${job.done ?? 0}/${job.total || '?'}`
      return job.stop_requested ? `${progress} · 正在停止` : progress
    }
    case 'done':
      return `已完成 · ${job.created_count ?? 0} 题`
    case 'cancelled':
      return `已停止 · ${job.created_count ?? 0} 题`
    case 'error':
      return '失败'
    default:
      return job.status || EMPTY_TEXT
  }
}
