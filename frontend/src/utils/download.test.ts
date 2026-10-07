import { describe, expect, it } from 'vitest'

import {
  basenameOf,
  detailFromErrorBody,
  filenameFromDisposition,
  normalizeBlobError,
} from './download'
import { HttpDetailError, extractErrorMessage, isHttpStatus } from './error'

/** 浏览器形态的 Blob(有 text());jsdom 自带的 Blob 没实现 text()/arrayBuffer(),
 * 拿它直接测会因读不回正文而假负(降级链最后一级返空串)*/
function textualBlob(text: string): Blob {
  return Object.assign(new Blob([text]), { text: () => Promise.resolve(text) })
}

describe('filenameFromDisposition', () => {
  it('优先 RFC 5987 的 filename*(中文名靠它才不失真)', () => {
    const header =
      'attachment; filename=".pdf"; filename*=UTF-8\'\'%E9%99%84%E4%BB%B6.pdf'
    expect(filenameFromDisposition(header, 'fallback.pdf')).toBe('附件.pdf')
  })

  it('无 filename* 时回退 filename="…"', () => {
    expect(filenameFromDisposition('attachment; filename="report.md"', 'x')).toBe('report.md')
    expect(filenameFromDisposition('attachment; filename=plain.txt', 'x')).toBe('plain.txt')
  })

  it('缺失头 / 空值 / 非法百分号编码都用 fallback', () => {
    expect(filenameFromDisposition(undefined, 'a.docx')).toBe('a.docx')
    expect(filenameFromDisposition('', 'a.docx')).toBe('a.docx')
    expect(filenameFromDisposition('attachment; filename*=UTF-8\'\'%', 'a.docx')).toBe('a.docx')
  })
})

describe('basenameOf', () => {
  it('取路径末段并兼容反斜杠', () => {
    expect(basenameOf('src/main.py')).toBe('main.py')
    expect(basenameOf('合同\\附件.docx')).toBe('附件.docx')
    expect(basenameOf('/repo/', 'x')).toBe('repo')
    expect(basenameOf(null)).toBe('download')
  })
})

describe('detailFromErrorBody', () => {
  it('取出 FastAPI detail(blob 响应下 response.data 不是对象,需先读正文)', () => {
    expect(
      detailFromErrorBody(JSON.stringify({ detail: '文件过大(233.4MB),下载上限 50MB' })),
    ).toBe('文件过大(233.4MB),下载上限 50MB')
  })

  it('非 JSON 正文 / 无 detail / 空 detail 都返回 null', () => {
    expect(detailFromErrorBody('<html>502 Bad Gateway</html>')).toBeNull()
    expect(detailFromErrorBody(JSON.stringify({ msg: 'x' }))).toBeNull()
    expect(detailFromErrorBody(JSON.stringify({ detail: '' }))).toBeNull()
    expect(detailFromErrorBody('')).toBeNull()
  })
})

describe('normalizeBlobError', () => {
  it('非 axios 错误原样返回', async () => {
    const err = new Error('boom')
    expect(await normalizeBlobError(err)).toBe(err)
  })

  it('错误体不是 Blob 时原样返回', async () => {
    const { default: axios } = await import('axios')
    const err = Object.assign(new Error('Request failed with status code 403'), {
      isAxiosError: true,
      response: { status: 403, data: { detail: '无权访问此任务' } },
    })
    expect(axios.isAxiosError(err)).toBe(true)
    expect(await normalizeBlobError(err)).toBe(err)
  })

  it('换成可读错误时保留状态码(410 工作区已过期靠它分流到重新克隆)', async () => {
    const detail = '沙箱已过期:实例已被回收'
    const err = Object.assign(new Error('Request failed with status code 410'), {
      isAxiosError: true,
      response: { status: 410, data: textualBlob(JSON.stringify({ detail })) },
    })

    const normalized = await normalizeBlobError(err)
    expect(normalized).toBeInstanceOf(HttpDetailError)
    // 文案与状态两件事都不能丢:前者给人看,后者决定要不要亮出恢复入口
    expect(extractErrorMessage(normalized)).toBe(detail)
    expect(isHttpStatus(normalized, 410)).toBe(true)
    expect(isHttpStatus(normalized, 404)).toBe(false)
  })
})
