/**
 * 审查项(ReviewItem)展示派生的纯函数(证据驱动可信审查 · 前端分区)
 *
 * 集中三分档 / 置信档位 / 来源标签的展示口径,避免散落在组件里漂移;判读本身
 * 在后端 app/models/audit.classify_status(前端直接用后端下发的 bucket,这里的
 * bucketize 只做分组,不再重算)。纯函数便于单测。
 *
 * 分档主语是"检查助手对这条待核实对象的核实结果",不是"你的项目里有几类条目"
 * ——旧口径(发现风险 / 已核查·剔除误报 / 缺口·待改进)让用户分不清在说谁。
 */
import type {
  ConfidenceTier,
  ReviewBucket,
  ReviewItem,
  ReviewOrigin,
  ReviewPlanItem,
  ReviewSummary,
} from '@/types/task'

export interface ReviewBuckets {
  established: ReviewItem[]
  not_established: ReviewItem[]
  pending_verify: ReviewItem[]
}

/** 三分档的展示顺序与标题(与后端 audit.BUCKET_ORDER / BUCKET_LABELS 同名单) */
export const REVIEW_BUCKET_ORDER: { key: ReviewBucket; label: string }[] = [
  { key: 'established', label: '已核实成立' },
  { key: 'not_established', label: '已核实不成立' },
  { key: 'pending_verify', label: '待核实' },
]

/** origin 的用户可读标签(旧版直接把 agent1_claim 这类枚举丢给用户)
 * agent2 只复核 agent1 产出,现只产生 agent1_claim/agent1_action 两类;后两项是 legacy,
 * 历史行仍可能带,保留映射以免旧任务报告露出英文枚举。*/
export const ORIGIN_LABELS: Record<string, string> = {
  agent1_claim: 'AI助手 的结论',
  agent1_action: 'AI助手 的改动/生成物',
  user_requirement: '你的要求',
  domain_baseline: '领域经验',
}

export function originLabel(origin: ReviewOrigin | string | undefined | null): string {
  if (!origin) return '—'
  return ORIGIN_LABELS[origin] ?? origin
}

/** 按后端下发的 bucket 三分(已核实成立 / 已核实不成立 / 待核实) */
export function bucketizeReviewItems(items: ReviewItem[] | undefined | null): ReviewBuckets {
  const buckets: ReviewBuckets = { established: [], not_established: [], pending_verify: [] }
  for (const it of items ?? []) {
    if (it.bucket === 'established') buckets.established.push(it)
    else if (it.bucket === 'not_established') buckets.not_established.push(it)
    else buckets.pending_verify.push(it)
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

/** 无 evidence → confidence 为空 → "无证据(未取证)"(旧文案"未分级"读不出原因) */
export function confidenceLabel(tier: ConfidenceTier | undefined | null): string {
  if (!tier) return '无证据'
  return TIER_LABELS[tier] ?? '无证据'
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
 * 审查项的状态徽章:待核实档不显示置信档位(它没有证据,分数无意义),
 * 改显示"为什么还没核实"。
 *
 * 返回 null = 按常规置信徽章显示。
 */
export function pendingVerifyBadge(
  item: ReviewItem,
): { label: string; title: string } | null {
  if (item.bucket !== 'pending_verify') return null
  if (item.evidence_mismatch) {
    return { label: '证据未核验', title: '引用了取证记录,但后端核验未通过' }
  }
  if (item.status === 'missing') {
    return { label: '未核实', title: '这一项还没开始核查' }
  }
  if (item.status === 'partial') {
    return { label: '部分核实', title: '只核实了一部分,结论仍待补证' }
  }
  return { label: '未取证', title: '存疑但本轮未取到独立证据,不作成立结论' }
}

/** 计划条目的对账展示态 */
export type PlanItemDisplayState = 'done' | 'unverified' | 'pending' | 'unknown'

/** 计划条目的对账状态文案(计划卡逐行 + 计数 chip 共用) */
export const PLAN_STATE_LABELS: Record<PlanItemDisplayState, string> = {
  done: '已核实',
  unverified: '未核实',
  pending: '核查中',
  unknown: '状态未知',
}

/**
 * 单条计划项的对账状态。
 *
 * 口径全在后端:收尾后 `_review.plan.unverified` 就是对账结果(旧版在这里复刻
 * target 子串匹配,与后端各自漂移);审查进行中按 plan_item_id 是否已被发射项命中判。
 * 老任务的收尾快照没有 unverified 字段 → unknown(不拿猜测当结论)。
 */
export function planItemStatus(
  planItem: ReviewPlanItem,
  items: ReviewItem[] | undefined | null,
  finalPlan?: ReviewSummary['plan'],
): PlanItemDisplayState {
  const matchedById = !!planItem.id && (items ?? []).some((i) => i.plan_item_id === planItem.id)
  if (matchedById) return 'done'
  if (finalPlan) {
    if (!Array.isArray(finalPlan.unverified)) return 'unknown'
    const hit = finalPlan.unverified.some(
      (u) => (u.id && planItem.id ? u.id === planItem.id : u.target === planItem.target),
    )
    return hit ? 'unverified' : 'done'
  }
  // 审查尚未收尾:没有发射项命中就是还在核
  return 'pending'
}

/** 未核实原因 → 计划卡 tooltip 文案 */
export function unverifiedReasonLabel(reason: string | undefined): string {
  if (reason === 'budget_exhausted') return '取证额度用尽'
  if (reason === 'not_reached') return '本轮未排到取证'
  return '未核实到'
}

/**
 * 旧任务收尾快照的识别:改造前 `_review.plan` 没有 unverified 字段(只有 backfilled_gap)。
 *
 * 为什么要单独识别:那类快照里的 counts / confidence_tally / verdict_summary 全部把
 * "对账失败的伪造缺口"算进去了(实测一条任务虚增 8 条)。启动迁移删了那些假行,
 * 但存在 task.params 里的旧聚合数字不会跟着变 → 不特殊处理就会顶部写"共 16 项"而列表只剩 8 条。
 */
export function isLegacyReviewSummary(
  summary: ReviewSummary | null | undefined,
): boolean {
  return !!summary?.plan && !Array.isArray(summary.plan.unverified)
}

/** 分档计数:旧快照按现存审查项现算,新快照直接用后端下发的数字 */
export function summaryCounts(
  summary: ReviewSummary | null | undefined,
  items: ReviewItem[] | undefined | null,
): ReviewSummary['counts'] | null {
  if (!summary) return null
  if (!isLegacyReviewSummary(summary)) return summary.counts
  const b = bucketizeReviewItems(items)
  return {
    total: (items ?? []).length,
    established: b.established.length,
    not_established: b.not_established.length,
    pending_verify: b.pending_verify.length,
  }
}

/** 置信分布:同上,旧快照按现存审查项重算(旧数字把被删的假缺口计进了"未取证") */
export function summaryConfidenceTally(
  summary: ReviewSummary | null | undefined,
  items: ReviewItem[] | undefined | null,
): ReviewSummary['confidence_tally'] | null {
  if (!summary) return null
  if (!isLegacyReviewSummary(summary)) return summary.confidence_tally
  const tally = {
    verified: 0, source_confirmed: 0, reference_corroborated: 0,
    assertion_only: 0, no_evidence: 0,
  } as ReviewSummary['confidence_tally']
  for (const it of items ?? []) {
    const tier = it.confidence?.tier
    tally[tier && tier in tally ? tier : 'no_evidence'] += 1
  }
  return tally
}

/** 总体判定文本:旧快照的那句带着虚增数字,宁可不显示也不能报错给用户 */
export function summaryVerdictText(
  summary: ReviewSummary | null | undefined,
): string {
  if (!summary) return ''
  if (isLegacyReviewSummary(summary)) {
    return '历史任务的汇总按旧口径生成(当时会把对账失败的计划项当成缺口),已不再引用其数字;以下计数按现存审查项现算。'
  }
  return summary.verdict_summary || ''
}

/**
 * 计划卡计数:规划 N / 核实 M / 未核实 K。
 *
 * 旧版第三个数字叫"回填缺口"——那是把对账失败伪造成缺口审查项的产物,现在
 * 未核实项不再占结果位,只在计划卡显示。收尾前 K 为 null(还不晓得会漏几条)。
 */
export function planTally(
  finalPlan: ReviewSummary['plan'],
  items: ReviewItem[] | undefined | null,
  livePlan: ReviewPlanItem[] | undefined | null,
): { planned: number; executed: number | null; unverified: number | null } | null {
  if (finalPlan) {
    if (!Array.isArray(finalPlan.unverified)) {
      // 旧快照:executed 是"planned - 回填数"算出来的假数字,只报规划条数
      return { planned: finalPlan.planned, executed: null, unverified: null }
    }
    return {
      planned: finalPlan.planned,
      executed: finalPlan.executed,
      unverified: finalPlan.unverified.length,
    }
  }
  const planItems = livePlan ?? []
  if (!planItems.length) return null
  const executed = planItems.filter((p) => planItemStatus(p, items) === 'done').length
  return { planned: planItems.length, executed, unverified: null }
}
