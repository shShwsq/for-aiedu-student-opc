/**
 * 字节数人性化显示
 *
 * 目前调用方是工作区二进制文件的下载卡片(展示文件大小)。
 */

/** 把字节数格式化为 B / KB / MB / GB(保留 1 位小数;0 与负数一律 "0 B") */
export function formatBytes(bytes: number | null | undefined): string {
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  if (n < 1024) return `${Math.round(n)} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n
  let i = -1
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(1)} ${units[i]}`
}
