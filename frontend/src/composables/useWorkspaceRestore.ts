/**
 * 工作区恢复(重新拉代码):发起后台 job + 轮询进度
 *
 * 为什么轮询:克隆是分钟级操作(大仓库 + 协议/分支回退)。后端 restore 端点旧版在
 * 请求里同步等克隆,而 api/client.ts 有 30s 全局超时 —— 请求先被打断,
 * extractErrorMessage 对"无 response"一律显示"网络错误,请检查网络连接后重试",
 * 用户看到一个假报错,真实结果几分钟后才落。现在 POST 立即返回 job,进度与真实
 * 失败原因走 GET .../restore/status。
 *
 * 用 seq 令牌防竞态:轮询期间用户切任务或组件卸载时,旧循环必须立刻停笔,否则会把
 * 上一个任务的进度/错误写进当前任务的 UI。
 */
import { getCurrentInstance, onUnmounted, ref } from 'vue'

import { getWorkspaceRestoreStatus, startWorkspaceRestore } from '@/api/workspace'
import { extractErrorMessage } from '@/utils/error'
import type { WorkspaceRestoreStatus } from '@/types/workspace'

/** 轮询间隔:后端克隆进度本身按 5% / 2s 节流,再快也拿不到新信息 */
const POLL_INTERVAL_MS = 1500

/** 前端等待上限:超时只是停止轮询(后端 job 仍在跑),提示用户稍后刷新 */
const POLL_TIMEOUT_MS = 30 * 60 * 1000

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export function useWorkspaceRestore() {
  /** 恢复进行中(按钮禁用 + 进度文案) */
  const restoring = ref(false)
  /** 失败信息(空=无错误) */
  const restoreError = ref('')
  /** 当前进度百分比(0-100) */
  const restorePercent = ref(0)
  /** 最近一条 git 进度行(可能为空) */
  const restoreMessage = ref('')

  /** 竞态令牌:reset/新一次 run 都会自增,旧循环据此停笔 */
  let seq = 0

  /** 清空状态并终止进行中的轮询(切换任务、组件卸载时调用) */
  function reset(): void {
    seq += 1
    restoring.value = false
    restoreError.value = ''
    restorePercent.value = 0
    restoreMessage.value = ''
  }

  /**
   * 发起恢复并轮询到终态;返回是否成功(done 且工作区可用)
   *
   * onDone 仅在成功时调用(通常用来重拉文件树)。重复点击由 restoring 拦下。
   */
  async function run(taskId: string, onDone?: () => void | Promise<void>): Promise<boolean> {
    if (restoring.value) return false
    const my = ++seq
    const deadline = Date.now() + POLL_TIMEOUT_MS
    restoring.value = true
    restoreError.value = ''
    restorePercent.value = 0
    restoreMessage.value = ''

    const fail = (message: string): false => {
      if (my === seq) restoreError.value = message
      return false
    }

    try {
      let snap: WorkspaceRestoreStatus
      try {
        snap = await startWorkspaceRestore(taskId)
      } catch (err) {
        return fail(extractErrorMessage(err))
      }

      // 幂等分支:工作区仍在 → 后端直接返回 done,不进轮询
      while (my === seq && snap.state === 'running') {
        if (Date.now() > deadline) {
          // 只是前端不再盯(后端 job 仍在跑);超过 30min 未终态通常是克隆卡在网络上
          return fail('仍在后台克隆中,已停止查看进度,请稍后刷新页面')
        }
        restorePercent.value = snap.percent
        restoreMessage.value = snap.message
        await sleep(POLL_INTERVAL_MS)
        if (my !== seq) return false
        try {
          snap = await getWorkspaceRestoreStatus(taskId)
        } catch (err) {
          return fail(extractErrorMessage(err))
        }
      }
      if (my !== seq) return false

      if (snap.state === 'failed') {
        // 后端带回协议回退链的真实原因(含 fatal 行),不再含糊成"网络错误"
        return fail(snap.error || '恢复工作区失败')
      }
      if (snap.state === 'idle') {
        return fail('恢复任务已丢失(后端可能重启过),请重试')
      }

      restorePercent.value = 100
      restoreMessage.value = snap.message
      await onDone?.()
      return true
    } finally {
      if (my === seq) restoring.value = false
    }
  }

  // 脱离组件作用域调用时(如单元测试)跳过生命周期注册
  if (getCurrentInstance()) onUnmounted(reset)

  return { restoring, restoreError, restorePercent, restoreMessage, run, reset }
}
