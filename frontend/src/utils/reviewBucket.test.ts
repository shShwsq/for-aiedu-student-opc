import { describe, it, expect } from 'vitest'
import {
  bucketizeReviewItems,
  confidenceClass,
  confidenceLabel,
  formatConfidenceScore,
  planItemStatus,
} from './reviewBucket'
import type { ReviewItem } from '@/types/task'

function item(over: Partial<ReviewItem>): ReviewItem {
  return {
    id: over.id ?? Math.random().toString(),
    round_idx: 1,
    title: over.title ?? 't',
    review_target: over.review_target ?? '',
    origin: over.origin ?? 'agent1_claim',
    status: over.status ?? 'covered',
    bucket: over.bucket ?? 'risk',
    evidence_validated: false,
    evidence_mismatch: false,
    created_at: '',
    ...over,
  } as ReviewItem
}

describe('bucketizeReviewItems', () => {
  it('按后端 bucket 三分', () => {
    const items = [
      item({ bucket: 'risk' }),
      item({ bucket: 'cleared' }),
      item({ bucket: 'gap' }),
      item({ bucket: 'risk' }),
    ]
    const b = bucketizeReviewItems(items)
    expect(b.risk).toHaveLength(2)
    expect(b.cleared).toHaveLength(1)
    expect(b.gap).toHaveLength(1)
  })

  it('空/undefined 不抛错,返回空桶', () => {
    expect(bucketizeReviewItems(undefined).risk).toEqual([])
    expect(bucketizeReviewItems([]).gap).toEqual([])
  })
})

describe('confidence 展示', () => {
  it('tier → 中文标签与 class', () => {
    expect(confidenceLabel('verified')).toBe('已动态验证')
    expect(confidenceLabel('source_confirmed')).toBe('源码取证')
    expect(confidenceClass('reference_corroborated')).toBe('reference')
    expect(confidenceClass('assertion_only')).toBe('assertion')
  })

  it('无 tier → 未分级 / assertion', () => {
    expect(confidenceLabel(undefined)).toBe('未分级')
    expect(confidenceClass(null)).toBe('assertion')
  })

  it('score → 百分比;null → —', () => {
    expect(formatConfidenceScore(0.78)).toBe('78%')
    expect(formatConfidenceScore(null)).toBe('—')
  })
})

describe('planItemStatus', () => {
  it('命中已发射(非回填)审查项 → done', () => {
    const items = [item({ review_target: 'users.py SQL 注入风险', title: '注入' })]
    expect(planItemStatus('users.py SQL 注入', items)).toBe('done')
  })

  it('仅命中 [计划回填] 项 → gap(回填不算已核实)', () => {
    const items = [item({ review_target: '并发安全', title: '[计划回填] 并发安全' })]
    expect(planItemStatus('并发安全', items)).toBe('gap')
  })

  it('无匹配 → gap', () => {
    expect(planItemStatus('auth 越权', [])).toBe('gap')
  })
})
