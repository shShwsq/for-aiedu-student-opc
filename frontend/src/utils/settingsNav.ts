/**
 * 设置页目录的纯计算逻辑(不碰 DOM,供 SettingsLayout 与单测共用)
 *
 * 三个函数覆盖二级目录渲染需要的全部判断:
 * 1. resolveNavChildren:把 children 声明解析成具体子项('agent-types' 依赖后端注册表)
 * 2. resolveActiveChildId:当前 hash 对应哪个子项(无 hash / 未知 hash 回退第一项)
 * 3. isNavItemActive:一级项是否为当前路由(手风琴展开条件)
 *
 * 高亮之所以不依赖 `router-link-active`:Vue Router 只比 path、忽略 hash,
 * 同一父项下的所有子链接会被同时判为 active,父项也跟着亮,必须由这些函数显式判定。
 */
import type { AgentTypeMeta } from '@/types/agent_configs'
import type { SettingsNavChild, SettingsNavItem } from '@/data/settingsNav'

/**
 * 解析一级项的二级目录
 *
 * - 静态数组直接返回;
 * - 'agent-types' 用后端注册表映射为子项(id=agent_type,label=display_name),
 *   因此注册第 4 种 CLI agent 时二级目录会自动多一项,前端无需改动;
 * - 未声明 children 返回空数组。
 */
export function resolveNavChildren(
  item: SettingsNavItem,
  agentTypes: AgentTypeMeta[],
): SettingsNavChild[] {
  if (!item.children) return []
  if (item.children === 'agent-types') {
    return agentTypes.map((meta) => ({ id: meta.agent_type, label: meta.display_name }))
  }
  return item.children
}

/**
 * 当前应高亮的二级项 id
 *
 * 非当前路由返回空串(折叠状态下不参与高亮);
 * hash 命中子项则返回该 id,无 hash 或 hash 未知(书签失效/agent 类型被下线)
 * 一律回退第一个子项,保证「进入一级页即点亮默认子项」。
 */
export function resolveActiveChildId(
  item: SettingsNavItem,
  children: SettingsNavChild[],
  path: string,
  hash: string | undefined,
): string {
  if (children.length === 0) return ''
  if (path !== item.path) return ''
  const id = (hash ?? '').startsWith('#') ? (hash as string).slice(1) : (hash ?? '')
  if (id && children.some((child) => child.id === id)) return id
  return children[0].id
}

/** 一级项是否为当前路由(决定是否展开其二级目录) */
export function isNavItemActive(item: SettingsNavItem, path: string): boolean {
  return path === item.path
}
