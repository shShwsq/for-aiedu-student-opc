import { describe, expect, it } from 'vitest'

import { formatBytes } from './bytes'

describe('formatBytes', () => {
  it('0 / 空值 / 负数都归一为 "0 B"(占位而不是 NaN)', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(null)).toBe('0 B')
    expect(formatBytes(undefined)).toBe('0 B')
    expect(formatBytes(-5)).toBe('0 B')
    expect(formatBytes(Number.NaN)).toBe('0 B')
  })

  it('字节与千进制单位分档', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1024)).toBe('1.0 KB')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB')
    expect(formatBytes(3 * 1024 ** 3)).toBe('3.0 GB')
  })

  it('超过 TB 后停在 TB(不再出现无穷单位)', () => {
    expect(formatBytes(4 * 1024 ** 4)).toBe('4.0 TB')
    expect(formatBytes(4096 * 1024 ** 4)).toBe('4096.0 TB')
  })
})
