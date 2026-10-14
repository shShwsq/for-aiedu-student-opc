import { describe, it, expect } from 'vitest'
import {
  bucketizeReviewItems,
  isLegacyReviewSummary,
  originLabel,
  pendingVerifyBadge,
  planItemStatus,
  planTally,
  PLAN_STATE_LABELS,
  REVIEW_BUCKET_ORDER,
  reviewItemBadge,
  summaryCounts,
  summaryVerdictText,
  unverifiedReasonLabel,
} from './reviewBucket'
import type { ReviewBucket, ReviewItem, ReviewPlanItem, ReviewSummary } from '@/types/task'

function item(over: Partial<ReviewItem>): ReviewItem {
  return {
    id: over.id ?? Math.random().toString(),
    round_idx: 1,
    title: over.title ?? 'T',
    review_target: over.review_target ?? 'target',
    origin: over.origin ?? 'agent1_claim',
    status: over.status ?? 'covered',
    bucket: over.bucket ?? 'established',
    evidence_validated: false,
    evidence_mismatch: false,
    created_at: '',
    ...over,
  } as ReviewItem
}

function planItem(over: Partial<ReviewPlanItem>): ReviewPlanItem {
  return { id: 'P1', target: 'JWT 默认密钥', origin: 'agent1_claim', ...over }
}

describe('bucketizeReviewItems', () => {
  it('按后端 bucket 三分(已核实成立 / 不成立 / 待核实)', () => {
    const items = [
      item({ bucket: 'established' }),
      item({ bucket: 'not_established' }),
      item({ bucket: 'pending_verify' }),
      item({ bucket: 'established' }),
    ]
    const b = bucketizeReviewItems(items)
    expect(b.established).toHaveLength(2)
    expect(b.not_established).toHaveLength(1)
    expect(b.pending_verify).toHaveLength(1)
  })

  it('未知/缺省 bucket 落到待核实(不当成成立结论)', () => {
    const b = bucketizeReviewItems([item({ bucket: 'whatever' as ReviewBucket })])
    expect(b.pending_verify).toHaveLength(1)
    expect(b.established).toEqual([])
  })

  it('空输入不抛错', () => {
    expect(bucketizeReviewItems(undefined).established).toEqual([])
    expect(bucketizeReviewItems([]).pending_verify).toEqual([])
  })

  it('分档标题顺序与后端同一名单,且主语是核实结果', () => {
    expect(REVIEW_BUCKET_ORDER.map((b) => b.key)).toEqual([
      'established', 'not_established', 'pending_verify',
    ])
    expect(REVIEW_BUCKET_ORDER.map((b) => b.label)).toEqual([
      '已核实成立', '已核实不成立', '待核实',
    ])
  })
})

describe('originLabel', () => {
  it('agent1 两类来源有中文标签,legacy 两类仍保留供历史行显示', () => {
    expect(originLabel('agent1_claim')).toBe('AI助手 的结论')
    expect(originLabel('agent1_action')).toBe('AI助手 的改动/生成物')
    // legacy:agent2 不再产生,但旧任务行可能带,不露英文枚举
    expect(originLabel('user_requirement')).toBe('你的要求')
    expect(originLabel('domain_baseline')).toBe('领域经验')
  })

  it('未知/空 → 兜底不抛错', () => {
    expect(originLabel(undefined)).toBe('—')
    expect(originLabel('bogus' as ReviewItem['origin'])).toBe('bogus')
  })
})

describe('reviewItemBadge', () => {
  it('待核实档走原因徽章(非置信档)', () => {
    const b = reviewItemBadge(item({ bucket: 'pending_verify', status: 'missing' }))
    expect(b).toEqual({ label: '未核实', cls: 'conf-assertion', title: '这一项还没开始核查' })
  })

  it('成立档无 tier → 不显徽章(置信档位已从展示层去掉)', () => {
    expect(reviewItemBadge(item({ bucket: 'established', confidence: null }))).toBeNull()
    expect(reviewItemBadge(item({
      bucket: 'established', confidence: { tier: 'source_confirmed', label: '源码取证' },
    }))).toBeNull()
  })

  it('成立档 tier=verified → 标 🔬 动态验证', () => {
    const b = reviewItemBadge(item({
      bucket: 'established', confidence: { tier: 'verified', label: '已动态验证' },
    }))
    expect(b!.label).toBe('🔬 动态验证')
    expect(b!.cls).toBe('conf-verified')
  })

  it('不成立档 tier=verified → 也标 🔬 动态验证(PoC 反证)', () => {
    const b = reviewItemBadge(item({
      bucket: 'not_established', confidence: { tier: 'verified', label: '已动态验证' },
    }))
    expect(b!.label).toBe('🔬 动态验证')
  })
})

describe('pendingVerifyBadge', () => {
  it('成立/不成立档不显示原因徽章(走置信徽章)', () => {
    expect(pendingVerifyBadge(item({ bucket: 'established' }))).toBeNull()
    expect(pendingVerifyBadge(item({ bucket: 'not_established' }))).toBeNull()
  })

  it('待核实档按成因给标签:missing > partial > 未取证', () => {
    expect(pendingVerifyBadge(item({
      bucket: 'pending_verify', status: 'missing',
    }))!.label).toBe('未核实')
    expect(pendingVerifyBadge(item({
      bucket: 'pending_verify', status: 'partial',
    }))!.label).toBe('部分核实')
    expect(pendingVerifyBadge(item({
      bucket: 'pending_verify', status: 'covered',
    }))!.label).toBe('未取证')
  })

  it('mismatch 不再在此出标签:交由独立「⚠ 证据未核验」徽章承载,避免字面重复', () => {
    // 旧实现下 mismatch 优先返回"证据未核验",与卡片独立徽章重复;
    // 现按 status 正常归因(mismatch 不抢占),回归点得证。
    const covered = pendingVerifyBadge(item({
      bucket: 'pending_verify', status: 'covered', evidence_mismatch: true,
    }))!
    expect(covered.label).toBe('未取证')
    expect(covered.label).not.toBe('证据未核验')
    // status 优先级不被 mismatch 覆盖:missing + mismatch 仍归"未核实"
    expect(pendingVerifyBadge(item({
      bucket: 'pending_verify', status: 'missing', evidence_mismatch: true,
    }))!.label).toBe('未核实')
  })
})

describe('planItemStatus', () => {
  it('收尾后以后端 unverified 清单为准(前端不再复刻 target 子串匹配)', () => {
    const plan: ReviewSummary['plan'] = {
      items: [planItem({ id: 'P1' }), planItem({ id: 'P2', target: '并发安全' })],
      planned: 2, executed: 1,
      unverified: [{ id: 'P2', target: '并发安全', reason: 'not_reached' }],
    }
    const items = [item({ plan_item_id: 'P1', review_target: 'backend/app/config.py:34' })]
    expect(planItemStatus(planItem({ id: 'P1' }), items, plan)).toBe('done')
    expect(planItemStatus(planItem({ id: 'P2', target: '并发安全' }), items, plan)).toBe('unverified')
  })

  it('id 已命中发射项 → done(哪怕收尾快照还没落库)', () => {
    const items = [item({ plan_item_id: 'P3' })]
    expect(planItemStatus(planItem({ id: 'P3' }), items, null)).toBe('done')
  })

  it('审查进行中且未命中 → pending(不是"未核实")', () => {
    expect(planItemStatus(planItem({ id: 'P1' }), [item({})], undefined)).toBe('pending')
  })

  it('老任务快照没有 unverified 字段 → unknown,不拿猜测当结论', () => {
    const legacy: ReviewSummary['plan'] = {
      items: [planItem({ id: 'P1' })], planned: 1, executed: 0,
    } as ReviewSummary['plan']
    expect(planItemStatus(planItem({ id: 'P1' }), [], legacy)).toBe('unknown')
  })

  it('无 id 的条目按 target 对齐 unverified 清单', () => {
    const plan: ReviewSummary['plan'] = {
      items: [], planned: 1, executed: 0,
      unverified: [{ target: '并发安全', reason: 'budget_exhausted' }],
    }
    expect(planItemStatus(planItem({ id: undefined, target: '并发安全' }), [], plan)).toBe('unverified')
  })

  it('状态文案齐全(计划卡逐行 tooltip 共用)', () => {
    expect(PLAN_STATE_LABELS.done).toBe('已核实')
    expect(PLAN_STATE_LABELS.unverified).toBe('未核实')
  })
})

describe('unverifiedReasonLabel', () => {
  it('两种成因说人话', () => {
    expect(unverifiedReasonLabel('budget_exhausted')).toBe('取证额度用尽')
    expect(unverifiedReasonLabel('not_reached')).toBe('本轮未排到取证')
    expect(unverifiedReasonLabel(undefined)).toBe('未核实到')
  })
})

describe('planTally', () => {
  it('收尾后:规划 / 核实 / 未核实 三个数字自洽', () => {
    const plan: ReviewSummary['plan'] = {
      items: [planItem({ id: 'P1' })], planned: 1, executed: 0,
      unverified: [{ id: 'P1', target: 'JWT 默认密钥', reason: 'not_reached' }],
    }
    expect(planTally(plan, [], null)).toEqual({ planned: 1, executed: 0, unverified: 1 })
  })

  it('收尾前:未核实数量还不知道,返回 null(不显示一个猜出来的数字)', () => {
    const live = [planItem({ id: 'P1' }), planItem({ id: 'P2', target: '并发' })]
    const items = [item({ plan_item_id: 'P1' })]
    expect(planTally(null, items, live)).toEqual({ planned: 2, executed: 1, unverified: null })
  })

  it('旧快照没有 unverified 字段 → 只报规划条数(旧 executed 是假数字)', () => {
    const legacy = {
      items: [planItem({ id: 'P1' })], planned: 8, executed: 0, backfilled_gap: 8,
    } as unknown as ReviewSummary['plan']
    expect(planTally(legacy, [], null)).toEqual({ planned: 8, executed: null, unverified: null })
  })

  it('无计划 → null(计划卡整块不渲染)', () => {
    expect(planTally(null, [], null)).toBeNull()
    expect(planTally(undefined, [], [])).toBeNull()
  })
})

describe('旧快照兼容(假缺口已被启动迁移删掉)', () => {
  function summary(over: Partial<ReviewSummary>): ReviewSummary {
    return {
      counts: { total: 16, established: 3, not_established: 1, pending_verify: 12 },
      status: { covered: 7, partial: 1, missing: 8 },
      confidence_tally: {
        verified: 0, source_confirmed: 1, reference_corroborated: 0,
        assertion_only: 1, no_evidence: 14,
      },
      knowledge_point_count: 0,
      verdict_summary: '核实 16 项:…',
      suggestions: [],
      plan: { items: [], planned: 8, executed: 0, backfilled_gap: 8 },
      ...over,
    } as ReviewSummary
  }

  const survivors = [
    item({ bucket: 'established', confidence: { tier: 'source_confirmed', label: '源码取证' } }),
    item({ bucket: 'not_established' }),
    item({ bucket: 'pending_verify' }),
  ]

  it('无 unverified 字段的旧快照被识别为 legacy', () => {
    expect(isLegacyReviewSummary(summary({}))).toBe(true)
    expect(isLegacyReviewSummary(summary({
      plan: { items: [], planned: 1, executed: 1, unverified: [] },
    }))).toBe(false)
    expect(isLegacyReviewSummary(summary({ plan: null }))).toBe(false)
    expect(isLegacyReviewSummary(null)).toBe(false)
  })

  it('分档计数按现存审查项现算,新快照直接用后端数字', () => {
    expect(summaryCounts(summary({}), survivors)).toEqual({
      total: 3, established: 1, not_established: 1, pending_verify: 1,
    })
    const fresh = summary({
      plan: { items: [], planned: 1, executed: 1, unverified: [] },
    })
    expect(summaryCounts(fresh, survivors)).toEqual(fresh.counts)
    expect(summaryCounts(null, survivors)).toBeNull()
  })

  it('判定行:旧快照换成口径说明,不拿虚增数字骗用户', () => {
    expect(summaryVerdictText(summary({}))).toContain('历史任务')
    expect(summaryVerdictText(summary({
      plan: { items: [], planned: 1, executed: 1, unverified: [] },
    }))).toBe('核实 16 项:…')
    expect(summaryVerdictText(null)).toBe('')
  })
})
