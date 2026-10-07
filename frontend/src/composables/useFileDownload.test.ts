/**
 * useFileDownload 单测:端点选择 + 文件名回落 + 竞态停笔
 *
 * 覆盖「文本文件(含 md)新增下载入口」的关键行为:
 * - source 分流:uploads 走上传原件端点,workspace 走沙箱端点
 * - 文件名:服务端 Content-Disposition 优先,缺失时回落路径末段
 * - 失败:把后端 detail(经 extractErrorMessage)写进 downloadError 并复位 downloading
 * - 下载途中切换文件(reset):旧请求既不弹出上个文件的下载,也不把错误写进当前 UI,
 *   且 downloading 必须复位(否则新文件的下载按钮永久禁用)
 */
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { downloadWorkspaceFile, downloadWorkspaceUploadsFile } from '@/api/workspace'
import { triggerBlobDownload } from '@/utils/download'
import { useFileDownload } from './useFileDownload'

vi.mock('@/api/workspace', () => ({
  downloadWorkspaceFile: vi.fn(),
  downloadWorkspaceUploadsFile: vi.fn(),
}))

// triggerBlobDownload 依赖 URL.createObjectURL / DOM 点击,jsdom 下不真跑
vi.mock('@/utils/download', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/utils/download')>()
  return { ...mod, triggerBlobDownload: vi.fn() }
})

const wsMock = downloadWorkspaceFile as Mock
const uploadsMock = downloadWorkspaceUploadsFile as Mock
const fireMock = triggerBlobDownload as Mock

const blob = { kind: 'blob' } as unknown as Blob

describe('useFileDownload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

  it('重复点击只发一次请求', async () => {
    let resolveFirst: (v: { blob: Blob; filename: string }) => void = () => {}
    wsMock.mockImplementation(
      () => new Promise((resolve) => { resolveFirst = resolve }),
    )
    const { run } = useFileDownload()

    const first = run('t1', 'a.md')
    await expect(run('t1', 'a.md')).resolves.toBe(false)
    expect(wsMock).toHaveBeenCalledTimes(1)
    resolveFirst({ blob, filename: 'a.md' })
    await expect(first).resolves.toBe(true)
  })

  it('下载途中切文件(reset):旧请求停笔,不弹出下载、不写错误,且可再次下载', async () => {
    let resolveStale: (v: { blob: Blob; filename: string }) => void = () => {}
    wsMock.mockImplementationOnce(
      () => new Promise((resolve) => { resolveStale = resolve }),
    )
    const { run, reset, downloading, downloadError } = useFileDownload()

    const stale = run('t1', 'old.md')
    reset() // 模拟切换文件/任务
    expect(downloading.value).toBe(false)

    let resolveStaleErr: (e: unknown) => void = () => {}
    wsMock.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { resolveStaleErr = reject }),
    )
    const next = run('t1', 'new.md')
    resolveStale({ blob, filename: 'old.md' })
    await expect(stale).resolves.toBe(false)
    expect(fireMock).not.toHaveBeenCalled()

    // reset 后新一次下载照常成立:自己的错误写进自己的文案位
    resolveStaleErr(new Error('文件过大(80.0MB),下载上限 50MB'))
    await expect(next).resolves.toBe(false)
    expect(downloadError.value).toBe('文件过大(80.0MB),下载上限 50MB')
    expect(downloading.value).toBe(false)
  })
})
