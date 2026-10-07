/**
 * 单文件下载:把选中的工作区/上传文件按原始字节取回本地
 *
 * 为什么抽出来:同一个「选中文件 → 下载」的动作散在三处(二进制下载卡片、
 * 任务详情页文件面板头部、做题页源码栏头部)。各处都要维护 downloading 态与
 * 错误文案,写三遍必然漂;文本文件(尤其 md)原先干脆没有取回入口。
 *
 * source 决定走哪条端点:
 * - workspace:沙箱工作区文件,依赖 session —— 过期后后端 404,提示走「重新克隆」
 * - uploads:用户上传原件,不经沙箱,保留期内始终可取回
 *
 * 竞态用 seq 令牌防住:下载中切换文件/任务时(reset),旧请求既不能把失败文案
 * 写进当前 UI,也不能在几分钟后凭空弹出一个上个文件的下载。
 */
import { getCurrentInstance, onUnmounted, ref } from 'vue'

import { downloadWorkspaceFile, downloadWorkspaceUploadsFile } from '@/api/workspace'
import { extractErrorMessage } from '@/utils/error'
import { basenameOf, triggerBlobDownload } from '@/utils/download'

/** 文件来源(决定下载端点与过期语义) */
export type FileDownloadSource = 'workspace' | 'uploads'

export function useFileDownload() {
  /** 下载中(按钮禁用 + 转圈) */
  const downloading = ref(false)
  /** 失败信息(空=无错误);由调用方渲染在文件面板内 */
  const downloadError = ref('')

  /** 竞态令牌:reset/新一次 run 都会自增,旧请求据此停笔 */
  let seq = 0

  /** 清状态并作废进行中的请求(切换文件/任务、面板重开时调用) */
  function reset(): void {
    seq += 1
    downloading.value = false
    downloadError.value = ''
  }

  /**
   * 下载一个文件;返回是否成功(失败原因写进 downloadError)
   *
   * taskId/path 缺失直接返回 false(不发起请求,也不报错——按钮本就禁用);
   * 重复点击由 downloading 拦下。
   */
  async function run(
    taskId: string,
    path: string,
    source: FileDownloadSource = 'workspace',
  ): Promise<boolean> {
    if (!taskId || !path || downloading.value) return false
    const my = ++seq
    downloading.value = true
    downloadError.value = ''

    try {
      const fetcher =
        source === 'uploads' ? downloadWorkspaceUploadsFile : downloadWorkspaceFile
      const { blob, filename } = await fetcher(taskId, path)
      // 已被 reset/新的下载作废:不要把上个文件的文件弹出去
      if (my !== seq) return false
      // 服务端给的中文名优先,缺失时回落路径末段
      triggerBlobDownload(blob, filename || basenameOf(path))
      return true
    } catch (err) {
      if (my === seq) downloadError.value = extractErrorMessage(err)
      return false
    } finally {
      if (my === seq) downloading.value = false
    }
  }

  // 脱离组件作用域调用时(如单元测试)跳过生命周期注册
  if (getCurrentInstance()) onUnmounted(() => { seq += 1 })

  return { downloading, downloadError, run, reset }
}
