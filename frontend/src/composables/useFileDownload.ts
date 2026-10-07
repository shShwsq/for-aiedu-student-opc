/**
 * 单文件下载:把选中的工作区/上传文件按原始字节取回本地
 *
 * 为什么抽出来:同一个「选中文件 → 下载」的动作散在三处(二进制下载卡片、
 * 任务详情页文件面板头部、做题页源码栏头部)。各处都要维护 downloading 态与
 * 错误文案,写三遍必然漂;文本文件(尤其 md)原先干脆没有取回入口。
 *
 * source 决定走哪条端点:
 * - workspace:沙箱工作区文件,依赖 session —— 容器被回收后后端回 410(已过期),
 *   提示走「重新克隆」;回调见 run 的 onExpired
 * - uploads:用户上传原件,不经沙箱,保留期内始终可取回
 *
 * 令牌只门控**错误文案**,不门控**送达**:用户点过下载就该收到文件,哪怕他随后
 * 去点别的文件/关掉面板(大文件流式可达 120s,等待中顺手浏览是常态)。把导航
 * 当成取消会静默丢弃这次下载且毫无提示 —— 那是浏览器行为的反直觉面,不是中止按钮。
 * 代价:切走之后那次下载的失败不再显示(它已不属于当前文件,冒出来反而误导)。
 */
import { ref } from 'vue'

import {
  downloadWorkspaceFile,
  downloadWorkspaceUploadsFile,
  isWorkspaceExpiredError,
} from '@/api/workspace'
import { extractErrorMessage } from '@/utils/error'
import { basenameOf, triggerBlobDownload } from '@/utils/download'

/** 文件来源(决定下载端点与过期语义) */
export type FileDownloadSource = 'workspace' | 'uploads'

export function useFileDownload() {
  /** 下载中(按钮禁用 + 转圈) */
  const downloading = ref(false)
  /** 失败信息(空=无错误);由调用方渲染在文件面板内 */
  const downloadError = ref('')

  /** 错误令牌:clearError/新一次 run 都会自增,旧请求的失败据此不写进当前 UI */
  let errorToken = 0

  /** 只清失败文案(切换文件/任务时调用);进行中的下载不受影响,照常送达 */
  function clearError(): void {
    errorToken += 1
    downloadError.value = ''
  }

  /**
   * 下载一个文件;返回是否成功(失败原因写进 downloadError)
   *
   * taskId/path 缺失直接返回 false(不发起请求,也不报错——按钮本就禁用);
   * 重复点击由 downloading 拦下(同一按钮连点不会排队多个大文件请求)。
   *
   * onExpired:工作区已过期(HTTP 410)时的后续动作。不能只留一行错误文案——
   * 沙箱已经没了,这个任务的工作区在重新克隆前任何读取都不会成功,调用方
   * 据此重查可用性才能亮出「重新克隆」入口。
   */
  async function run(
    taskId: string,
    path: string,
    source: FileDownloadSource = 'workspace',
    onExpired?: () => void | Promise<void>,
  ): Promise<boolean> {
    if (!taskId || !path || downloading.value) return false
    // 占位错误令牌:此后若发生切换(clearError)或新的下载,本次的失败就不再展示
    const my = ++errorToken
    downloading.value = true
    downloadError.value = ''

    try {
      const fetcher =
        source === 'uploads' ? downloadWorkspaceUploadsFile : downloadWorkspaceFile
      const { blob, filename } = await fetcher(taskId, path)
      // 送达不看令牌:用户点过的下载必须落到本地
      // 服务端给的中文名优先,缺失时回落路径末段(path 为调用时快照,不会错标)
      triggerBlobDownload(blob, filename || basenameOf(path))
      return true
    } catch (err) {
      if (my === errorToken) downloadError.value = extractErrorMessage(err)
      // 过期单独走恢复入口(仍受同一令牌门控:切走之后的失败不该再打扰当前文件)
      if (my === errorToken && isWorkspaceExpiredError(err)) {
        try {
          await onExpired?.()
        } catch {
          // 恢复入口自身的失败不掩盖下载错误
        }
      }
      return false
    } finally {
      downloading.value = false
    }
  }

  return { downloading, downloadError, run, clearError }
}
