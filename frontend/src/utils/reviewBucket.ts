/**
 * 审查项(ReviewItem)展示派生的纯函数(证据驱动可信审查 · 前端分区)
 *
 * 集中三态分桶 / 置信档位展示口径,避免散落在组件里漂移;与后端
 * app/models/audit.classify_status 的判读一致(前端直接用后端下发的 bucket,
 * 这里的 bucketize 只做分组,不再重算判读)。纯函数便于单测。
 */
import type { ReviewItem, ConfidenceTier } from '@/types/task'

export interface ReviewBuckets {
  risk: ReviewItem[]
  cleared: ReviewItem[]
  gap: ReviewItem[]
}

/** 按后端下发的 bucket 三分(风险 / 已核查·剔除误报 / 缺口·待改进) */
export function bucketizeReviewItems(items: ReviewItem[] | undefined | null): ReviewBuckets {
  const buckets: ReviewBuckets = { risk: [], cleared: [], gap: [] }
  for (const it of items ?? []) {
    if (it.bucket === 'risk') buckets.risk.push(it)
    else if (it.bucket === 'cleared') buckets.cleared.push(it)
    else buckets.gap.push(it)
  }
  return buckets
}

/** 置信档位 → 展示短标签(与后端 evidence.py TIER_LABELS 对齐) */
const TIER_LABELS: Record<ConfidenceTier, string> = {
  verified: '已动态验证',
  source_confirmed: '源码取证',
  reference_corroborated: '外部佐证',
  assertion_only: '仅断言·待核实',
}

/** 无 evidence → confidence 为空 → "未分级"(老任务优雅回退) */
export function confidenceLabel(tier: ConfidenceTier | undefined | null): string {
  if (!tier) return '未分级'
  return TIER_LABELS[tier] ?? '未分级'
}

/** 置信档位 → CSS class 后缀(conf-<class>);未知/缺省 → assertion */
export function confidenceClass(tier: ConfidenceTier | undefined | null): string {
  if (tier === 'verified') return 'verified'
  if (tier === 'source_confirmed') return 'source'
  if (tier === 'reference_corroborated') return 'reference'
  return 'assertion'
}

/** 0-1 score → 百分比字符串;无 → '—' */
export function formatConfidenceScore(score: number | undefined | null): string {
  if (score == null || Number.isNaN(score)) return '—'
  return `${Math.round(score * 100)}%`
}

/**
 * 审查计划条目对账状态(镜像后端宽松匹配:target 归一化后互为子串即算命中)。
 * 回填项(标题 [计划回填])不算"已核实"。
 */
export function planItemStatus(
  planTarget: string,
  items: ReviewItem[] | undefined | null,
): 'done' | 'gap' {
  const pt = (planTarget || '').trim().toLowerCase()
  if (!pt) return 'gap'
  for (const it of items ?? []) {
    if ((it.title || '').startsWith('[计划回填]')) continue
    const et = (it.review_target || '').trim().toLowerCase()
    if (et && (pt.includes(et) || et.includes(pt))) return 'done'
  }
  return 'gap'
}
