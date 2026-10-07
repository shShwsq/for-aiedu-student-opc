/**
 * useFileDownload 单测:端点选择 + 文件名回落 + 送达不被导航打断
 *
 * 覆盖「文本文件(含 md)新增下载入口」的关键行为:
 * - source 分流:uploads 走上传原件端点,workspace 走沙箱端点
 * - 文件名:服务端 Content-Disposition 优先,缺失时回落路径末段
 * - 失败:把后端 detail(经 extractErrorMessage)写进 downloadError 并复位 downloading
 * - 导航不等于取消:下载途中切文件(clearError),已点下去的那次**仍要送达**,
 *   只有它的失败文案被丢弃(那失败已不属于当前浏览的文件)
 * - 令牌只门控错误:任何终态都要把 downloading 归位,否则按钮永久禁用
 * - 工作区已过期(HTTP 410):额外回调 onExpired 供调用方亮出「重新克隆」入口,
 *   同一令牌门控(已切走的不替别的文件刷 UI)
 */
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { downloadWorkspaceFile, downloadWorkspaceUploadsFile } from '@/api/workspace'
import { triggerBlobDownload } from '@/utils/download'
import { useFileDownload } from './useFileDownload'

vi.mock('@/api/workspace', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/workspace')>()
  return {
    ...mod,
    downloadWorkspaceFile: vi.fn(),
    downloadWorkspaceUploadsFile: vi.fn(),
    // isWorkspaceExpiredError 用真实现:mock 整个模块却不导出它,调用时就是
    // undefined 直接报错,而且过期分流的本意(按真 axios 错误的 status 判)也会丢
  }
})

// triggerBlobDownload 依赖 URL.createObjectURL / DOM 点击,jsdom 下不真跑
vi.mock('@/utils/download', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/utils/download')>()
  return { ...mod, triggerBlobDownload: vi.fn() }
})

const wsMock = downloadWorkspaceFile as Mock
const uploadsMock = downloadWorkspaceUploadsFile as Mock
const fireMock = triggerBlobDownload as Mock

const blob = { kind: 'blob' } as unknown as Blob

/** 造一个后端回 410(工作区已过期)时的 axios 错误形态 */
function makeExpiredError(detail: string): unknown {
  return {
    isAxiosError: true,
    message: 'Request failed with status code 410',
    response: { status: 410, statusText: '', headers: {}, config: {}, data: { detail } },
  }
}

describe('useFileDownload', () => {
  beforeEach(() => {
    // resetAllMocks 而非 clearAllMocks:clear 只清调用记录,持久实现
    // (mockResolvedValue / mockImplementation)会跨用例泄漏,后续用例忘设 mock 时
    // 会拿到上个用例遗留的挂起 Promise 而静默超时
    vi.resetAllMocks()
  })

  it('默认走工作区端点,并采用服务端给出的中文名', async () => {
    wsMock.mockResolvedValue({ blob, filename: '建设方案.md' })
    const { run, downloading, downloadError } = useFileDownload()

    await expect(run('t1', 'docs/建设方案.md')).resolves.toBe(true)
    expect(wsMock).toHaveBeenCalledWith('t1', 'docs/建设方案.md')
    expect(uploadsMock).not.toHaveBeenCalled()
    expect(fireMock).toHaveBeenCalledWith(blob, '建设方案.md')
    expect(downloading.value).toBe(false)
    expect(downloadError.value).toBe('')
  })

  it('source=uploads:走上传原件端点(不经沙箱)', async () => {
    uploadsMock.mockResolvedValue({ blob, filename: 'a.docx' })
    const { run } = useFileDownload()

    await expect(run('t1', '0-附件.zip/a.docx', 'uploads')).resolves.toBe(true)
    expect(uploadsMock).toHaveBeenCalledWith('t1', '0-附件.zip/a.docx')
    expect(wsMock).not.toHaveBeenCalled()
  })

  it('服务端没给文件名:回落路径末段', async () => {
    wsMock.mockResolvedValue({ blob, filename: '' })
    const { run } = useFileDownload()

    await run('t1', 'docs/readme.md')
    expect(fireMock).toHaveBeenCalledWith(blob, 'readme.md')
  })

  it('工作区已过期:展示后端带回的原因,状态复位', async () => {
    wsMock.mockRejectedValue(new Error('工作区不可用:尚未 clone 仓库'))
    const { run, downloading, downloadError } = useFileDownload()

    await expect(run('t1', 'a.md')).resolves.toBe(false)
    expect(downloadError.value).toBe('工作区不可用:尚未 clone 仓库')
    expect(fireMock).not.toHaveBeenCalled()
    expect(downloading.value).toBe(false)
  })

  it('taskId 或 path 缺失:不发请求也不报错(按钮禁用态的兜底)', async () => {
    const { run, downloadError } = useFileDownload()

    await expect(run('', 'a.md')).resolves.toBe(false)
    await expect(run('t1', '')).resolves.toBe(false)
    expect(wsMock).not.toHaveBeenCalled()
    expect(downloadError.value).toBe('')
  })

  it('下载中再点(同文件或换文件)被拦下:不排队并发请求', async () => {
    let resolveFirst: (v: { blob: Blob; filename: string }) => void = () => {}
    wsMock.mockImplementation(
      () => new Promise((resolve) => { resolveFirst = resolve }),
    )
    const { run } = useFileDownload()

    const first = run('t1', 'a.md')
    await expect(run('t1', 'a.md')).resolves.toBe(false)
    await expect(run('t1', 'b.md')).resolves.toBe(false)
    expect(wsMock).toHaveBeenCalledTimes(1)
    resolveFirst({ blob, filename: 'a.md' })
    await expect(first).resolves.toBe(true)
  })

  it('下载途中切文件:已点下去的下载仍要送达(导航不等于取消)', async () => {
    let resolveStale: (v: { blob: Blob; filename: string }) => void = () => {}
    wsMock.mockImplementationOnce(
      () => new Promise((resolve) => { resolveStale = resolve }),
    )
    const { run, clearError, downloading } = useFileDownload()

    const inFlight = run('t1', 'docs/大合同.docx')
    clearError() // 用户切去浏览别的文件
    resolveStale({ blob, filename: '大合同.docx' })

    await expect(inFlight).resolves.toBe(true)
    expect(fireMock).toHaveBeenCalledWith(blob, '大合同.docx')
    expect(downloading.value).toBe(false)
  })

  it('切走之后那次下载才失败:过期失败不写进当前 UI,但状态照常归位', async () => {
    let rejectStale: (e: unknown) => void = () => {}
    wsMock.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectStale = reject }),
    )
    const { run, clearError, downloadError, downloading } = useFileDownload()

    const inFlight = run('t1', 'old.md')
    clearError()
    rejectStale(new Error('工作区不可用:尚未 clone 仓库'))

    await expect(inFlight).resolves.toBe(false)
    expect(downloadError.value).toBe('')
    expect(downloading.value).toBe(false)
  })

  it('工作区已过期(410):调用 onExpired 亮出重新克隆入口', async () => {
    wsMock.mockRejectedValue(makeExpiredError('沙箱已过期:实例已被回收'))
    const expired: number[] = []
    const { run, downloadError } = useFileDownload()

    await expect(run('t1', 'a.docx', 'workspace', () => { expired.push(1) })).resolves.toBe(false)
    expect(expired).toEqual([1])
    // 失败文案仍照旧(不让回调替后端编报错)
    expect(downloadError.value).toBe('沙箱已过期:实例已被回收')
  })

  it('非过期的失败不触发 onExpired(不能把任意错误都当工作区已没)', async () => {
    wsMock.mockRejectedValue(new Error('文件不存在: a.md'))
    const expired: number[] = []
    const { run } = useFileDownload()

    await expect(run('t1', 'a.md', 'workspace', () => { expired.push(1) })).resolves.toBe(false)
    expect(expired).toEqual([])
  })

  it('已切走才报过期:不替别的文件刷 UI,onExpired 同受令牌门控', async () => {
    let rejectStale: (e: unknown) => void = () => {}
    wsMock.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectStale = reject }),
    )
    const expired: number[] = []
    const { run, clearError } = useFileDownload()

    const inFlight = run('t1', 'old.md', 'workspace', () => { expired.push(1) })
    clearError()
    rejectStale(makeExpiredError('沙箱已过期'))

    await expect(inFlight).resolves.toBe(false)
    expect(expired).toEqual([])
  })
})
