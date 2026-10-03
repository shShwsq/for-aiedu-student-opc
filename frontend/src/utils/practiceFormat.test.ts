/**
 * 练习展示格式化工具单元测试
 *
 * 覆盖三类容易在各组件走偏的口径:
 * 1. 无数据 → 统一占位符「—」(而不是 NaN / Invalid Date / undefined)
 * 2. 正确率与难度的小数位(百分比取整、难度整数不带小数)
 * 3. 选项下标越界与状态/题型标签兜底
 */
import { describe, expect, it } from 'vitest'

import {
  EMPTY_TEXT,
  formatDate,
  formatDateTime,
  formatDifficulty,
  formatPercent,
  optionLetter,
  questionStatusLabel,
  qtypeLabel,
} from './practiceFormat'

describe('formatPercent', () => {
  it('0-1 的比例按百分比取整展示', () => {
    expect(formatPercent(0.666)).toBe('67%')
    expect(formatPercent(1)).toBe('100%')
    expect(formatPercent(0)).toBe('0%')
  })

  it('可指定小数位', () => {
    expect(formatPercent(0.666, 1)).toBe('66.6%')
  })

  it('null / undefined / NaN 返回占位符(无作答记录不能显示成 0%)', () => {
    expect(formatPercent(null)).toBe(EMPTY_TEXT)
    expect(formatPercent(undefined)).toBe(EMPTY_TEXT)
    expect(formatPercent(NaN)).toBe(EMPTY_TEXT)
  })
})

describe('formatDate / formatDateTime', () => {
  it('空值返回占位符', () => {
    expect(formatDate(null)).toBe(EMPTY_TEXT)
    expect(formatDateTime('')).toBe(EMPTY_TEXT)
  })

  it('合法 ISO 时间正常格式化(历史列表口径只到月日时分)', () => {
    expect(formatDate('2026-01-05T08:00:00Z')).toContain('2026')
    const stamp = formatDateTime('2026-01-05T08:00:00Z')
    expect(stamp).toMatch(/\d{1,2}:\d{2}/)
    expect(stamp).not.toContain('2026')
    expect(stamp).not.toContain('Invalid')
  })

  it('无法解析的字符串原样返回,不显示 Invalid Date', () => {
    expect(formatDate('不是时间')).toBe('不是时间')
    expect(formatDateTime('不是时间')).toBe('不是时间')
  })
})

describe('formatDifficulty', () => {
  it('整数难度不带小数,小数难度保留 1 位', () => {
    expect(formatDifficulty(3)).toBe('3')
    expect(formatDifficulty(3.25)).toBe('3.3')
    expect(formatDifficulty(2.7)).toBe('2.7')
  })

  it('空值返回占位符', () => {
    expect(formatDifficulty(null)).toBe(EMPTY_TEXT)
    expect(formatDifficulty(undefined)).toBe(EMPTY_TEXT)
    expect(formatDifficulty(NaN)).toBe(EMPTY_TEXT)
  })
})

describe('optionLetter', () => {
  it('下标从 A 开始递增', () => {
    expect(optionLetter(0)).toBe('A')
    expect(optionLetter(3)).toBe('D')
  })

  it('空值或越界返回占位符', () => {
    expect(optionLetter(null)).toBe(EMPTY_TEXT)
    expect(optionLetter(-1)).toBe(EMPTY_TEXT)
    expect(optionLetter(26)).toBe(EMPTY_TEXT)
  })
})

describe('questionStatusLabel / qtypeLabel', () => {
  it('题目状态与题库管理筛选文案一致', () => {
    expect(questionStatusLabel('draft')).toBe('待确认')
    expect(questionStatusLabel('active')).toBe('已入库')
    expect(questionStatusLabel('archived')).toBe('已归档')
    expect(questionStatusLabel('unknown')).toBe(EMPTY_TEXT)
  })

  it('题型只区分判断与单选', () => {
    expect(qtypeLabel('true_false')).toBe('判断')
    expect(qtypeLabel('single_choice')).toBe('单选')
    expect(qtypeLabel(null)).toBe('单选')
  })
})
