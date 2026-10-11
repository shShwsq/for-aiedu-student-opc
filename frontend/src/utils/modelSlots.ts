/**
 * 「任务模型默认」页的纯计算逻辑(不碰 DOM,供面板与单测共用)
 *
 * 两件事都必须与后端保持同一口径,否则页面会「显示 A 实际用 B」:
 * - 生效模型文案:来源由后端解析链算好(source),前端只排版
 * - 编辑入口深链:后端回传 `/settings/practice#generation` 形式,拆给 RouterLink
 */
import type { ModelSlot } from '@/types/model_slots'

/** RouterLink 的 to 形状(path + 可选 hash) */
export interface EditTarget {
  path: string
  hash?: string
}

/**
 * 把后端回传的编辑入口拆成 RouterLink 两段
 *
 * 直接用整串当 `to` 会被当成 path(含 `#` 的路径匹配不到任何路由),
 * 所以这里显式拆分;无 hash 时不写 `hash` 字段(避免落到一级页的第一个子项外)。
 */
export function editTarget(path: string): EditTarget {
  const [routePath, hash] = path.split('#')
  return hash ? { path: routePath, hash: `#${hash}` } : { path: routePath }
}

/**
 * 槽位当前生效模型的展示文案
 *
 * source==='slot' 才显示配置名;否则显示「环境默认(模型名)」,
 * 环境模型名未知时退化成「环境默认」——不能编一个名字给用户看。
 */
export function effectiveLabel(slot: Pick<ModelSlot, 'source' | 'config_label'>, envModel: string): string {
  if (slot.source === 'slot' && slot.config_label) return slot.config_label
  return envModel ? `环境默认(${envModel})` : '环境默认'
}

/** 思考模式展示文案(follow/on/off → 人话);与后端 THINKING_MODES 对齐 */
export function thinkingModeLabel(mode: string): string {
  if (mode === 'on') return '强制开启'
  if (mode === 'off') return '强制关闭'
  return '跟随模型配置'
}

/**
 * 行尾编辑链接的文案(按归属面板给动词,不写「修改」这种看不出去哪儿的词)
 *
 * 未登记的面板回退「去修改」,新增槽位时不至于显示空白。
 */
const EDIT_LINK_LABEL: Record<string, string> = {
  '/settings/practice': '去练习设置',
  '/settings/memory': '去记忆设置',
  '/settings/policy': '去智能体策略',
  '/settings/models': '去模型设置',
}

export function editLabel(editPath: string): string {
  return EDIT_LINK_LABEL[editPath.split('#')[0]] || '去修改'
}
