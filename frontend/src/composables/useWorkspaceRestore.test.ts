/**
 * useWorkspaceRestore 单测:发起 → 轮询 → 终态
 *
 * 覆盖"假网络错误"修复的关键行为:
 * - running → 轮询百分比与 git 进度行
 * - done → 回调刷新(重拉文件树),状态复位
 * - failed → 展示后端带回的真实克隆原因(而不是含糊的"网络错误")
 * - 轮询途中切换任务(reset)→ 旧循环立刻停笔,不把上个任务的进度/错误写进当前 UI
 */
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { getWorkspaceRestoreStatus, startWorkspaceRestore } from '@/api/workspace'
import { useWorkspaceRestore } from './useWorkspaceRestore'

vi.mock('@/api/workspace', () => ({
  startWorkspaceRestore: vi.fn(),
  getWorkspaceRestoreStatus: vi.fn(),
}))

const startMock = startWorkspaceRestore as Mock
const statusMock = getWorkspaceRestoreStatus as Mock

const snap = (over: Record<string, unknown> = {}) => ({
  state: 'running', percent: 0, message: '', error: '',
  available: false, repo_path: '', mode: '', ...over,
})

describe('useWorkspaceRestore', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('工作区仍在:后端直接返回 done,不进轮询', async () => {
    startMock.mockResolvedValue(snap({ state: 'done', available: true, repo_path: '/tmp/x' }))
    const onDone = vi.fn()
    const { run, restoring, restoreError } = useWorkspaceRestore()

    await expect(run('t1', onDone)).resolves.toBe(true)
    expect(statusMock).not.toHaveBeenCalled()
    expect(onDone).toHaveBeenCalledTimes(1)
    expect(restoring.value).toBe(false)
    expect(restoreError.value).toBe('')
  })

  it('running → 轮询到 done:百分比实时更新,完成后回调', async () => {
    vi.useFakeTimers()
    try {
      startMock.mockResolvedValue(snap())
      statusMock
        .mockResolvedValueOnce(snap({ percent: 45, message: 'Receiving objects:  45% (1/2)' }))
        .mockResolvedValueOnce(snap({ state: 'done', percent: 100, available: true }))
      const onDone = vi.fn()
      const { run, restoring, restorePercent, restoreMessage } = useWorkspaceRestore()

      const promise = run('t1', onDone)
      await vi.advanceTimersByTimeAsync(1500)
      expect(restorePercent.value).toBe(45)
      expect(restoreMessage.value).toBe('Receiving objects:  45% (1/2)')

      await vi.advanceTimersByTimeAsync(1500)
      await expect(promise).resolves.toBe(true)
      expect(onDone).toHaveBeenCalledTimes(1)
      expect(restoring.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('failed:展示后端带回的真实克隆原因', async () => {
    vi.useFakeTimers()
    try {
      const reason =
        '仓库克隆失败(已尝试 2 种协议 x 1 种分支策略):\n' +
        '[https://gitee.com/shwsq/overleaf.git] git clone 失败: error: unable to create' +
        ' file a/deep/path: Filename too long'
      startMock.mockResolvedValue(snap())
      statusMock.mockResolvedValue(snap({ state: 'failed', error: reason }))
      const onDone = vi.fn()
      const { run, restoreError, restoring } = useWorkspaceRestore()

      const promise = run('t1', onDone)
      await vi.advanceTimersByTimeAsync(1500)
      await expect(promise).resolves.toBe(false)
      expect(restoreError.value).toContain('Filename too long')
      expect(onDone).not.toHaveBeenCalled()
      expect(restoring.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('轮询途中切任务(reset):旧循环停笔,不写入进度与错误', async () => {
    vi.useFakeTimers()
    try {
      startMock.mockResolvedValue(snap())
      statusMock.mockResolvedValue(snap({ percent: 60, message: 'Receiving objects:  60%' }))
      const { run, restorePercent, restoreError, restoring, reset } = useWorkspaceRestore()

      const promise = run('t1')
      await vi.advanceTimersByTimeAsync(1500)
      expect(restorePercent.value).toBe(60)

      reset() // 模拟切换任务:终止上个任务的轮询
      await vi.advanceTimersByTimeAsync(15000)
      await expect(promise).resolves.toBe(false)
      expect(restorePercent.value).toBe(0)
      expect(restoreError.value).toBe('')
      expect(restoring.value).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('发起请求本身失败:提取错误文案且不轮询', async () => {
    startMock.mockRejectedValue(new Error('后端未就绪'))
    const { run, restoreError } = useWorkspaceRestore()

    await expect(run('t1')).resolves.toBe(false)
    expect(restoreError.value).toBe('后端未就绪')
    expect(statusMock).not.toHaveBeenCalled()
  })

  it('重复点击被 restoring 拦下(只发一次 POST)', async () => {
    startMock.mockResolvedValue(snap())
    statusMock.mockResolvedValue(snap({ state: 'done', available: true }))
    const { run } = useWorkspaceRestore()

    const [a, b] = await Promise.all([run('t1'), run('t1')])
    expect(a).toBe(true)
    expect(b).toBe(false)
    expect(startMock).toHaveBeenCalledTimes(1)
  })
})
