import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { BINARY_SUFFIXES, fileSuffix, isLikelyBinaryPath } from './fileKind'

/** 从当前目录向上找后端判定源文件(jsdom 下 import.meta.url 非 file 协议,不能用它推路径) */
function findBackendSource(): string | null {
  let dir = process.cwd()
  for (let i = 0; i < 6; i += 1) {
    const candidate = path.resolve(dir, 'backend', 'app', 'file_kinds.py')
    if (existsSync(candidate)) return candidate
    const parent = path.resolve(dir, '..')
    if (parent === dir) break
    dir = parent
  }
  return null
}

const backendSource = findBackendSource()

describe('fileSuffix', () => {
  it('取小写后缀并兼容反斜杠路径', () => {
    expect(fileSuffix('a/b/DECK.PPTX')).toBe('.pptx')
    expect(fileSuffix('src\\main.Py')).toBe('.py')
    expect(fileSuffix('archive.tar.gz')).toBe('.gz')
  })

  it('dotfile 与无后缀文件返回空串', () => {
    expect(fileSuffix('.gitignore')).toBe('')
    expect(fileSuffix('Dockerfile')).toBe('')
    expect(fileSuffix('')).toBe('')
    expect(fileSuffix(null)).toBe('')
  })
})

describe('isLikelyBinaryPath', () => {
  it('命中办公文档 / 图片 / 压缩包', () => {
    for (const p of ['合同.docx', 'spec.pdf', 'assets/logo.png', 'dist/app.zip', 'lib.so']) {
      expect(isLikelyBinaryPath(p)).toBe(true)
    }
  })

  it('源码与文本文件不判二进制', () => {
    for (const p of ['main.py', 'README.md', 'notes', '.gitignore', 'index.ts']) {
      expect(isLikelyBinaryPath(p)).toBe(false)
    }
  })

  it.skipIf(!backendSource)('与后端 BINARY_SUFFIXES 完全一致(两侧各一份,必须同表)', () => {
    // 后端表是权威(决定要不要回传内容),前端表决定要不要发起请求;
    // 两者分叉会让某些文件既预览不了也看不到卡片,故直接比对源码
    const source = readFileSync(backendSource as string, 'utf-8')
    const block = /BINARY_SUFFIXES[^{]*\{([^}]*)\}/.exec(source)?.[1] ?? ''
    const backend = [...block.matchAll(/"(\.[^"]+)"/g)].map((m) => m[1])

    expect(backend.length).toBeGreaterThan(0)
    expect([...BINARY_SUFFIXES].sort()).toEqual([...new Set(backend)].sort())
  })
})
