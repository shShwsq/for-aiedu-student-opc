/**
 * 子智能体(Agent 工具)折叠块的展开状态 —— 纯函数,便于单测。
 *
 * 交互诉求:子智能体"输出中默认打开、输出结束后整块折叠"。
 * 后端已把并行子智能体的实时文本/内部工具挡在主时间线之外,运行中的块没有
 * 可靠的分路实时正文(ACP 不带子智能体身份),所以运行态展示"执行中…"占位,
 * 完成时把整块折叠;报告仍在其展开态里可读。
 *
 * 优先级:用户手动展开 > 用户手动折叠 > 默认规则(无结果=运行中→展开;有结果=完成→折叠)。
 * 手动意图记在两个集合里,跨 SSE 增量(运行→完成切换)保持稳定,不会被默认规则反复覆盖。
 */

export interface AgentBlockState {
  /** 用户手动展开过的 callId(覆盖“完成默认折叠”) */
  expanded: Set<string>
  /** 用户手动折叠过的 callId(覆盖“运行中默认展开”) */
  collapsed: Set<string>
}

/** 块当前是否展开 */
export function isAgentBlockOpen(
  callId: string,
  hasResult: boolean,
  state: AgentBlockState,
): boolean {
  if (state.expanded.has(callId)) return true
  if (state.collapsed.has(callId)) return false
  // 默认:运行中(尚无结果)展开,完成(已有结果)折叠
  return !hasResult
}

/**
 * 点击标题:就地翻转,写入手动集合(expanded/collapsed),返回翻转后的展开态。
 * 传入同一份 state(集合按引用修改),调用方 reactive Set 直接生效。
 */
export function toggleAgentBlock(
  callId: string,
  hasResult: boolean,
  state: AgentBlockState,
): boolean {
  const willOpen = !isAgentBlockOpen(callId, hasResult, state)
  if (willOpen) {
    state.collapsed.delete(callId)
    state.expanded.add(callId)
  } else {
    state.expanded.delete(callId)
    state.collapsed.add(callId)
  }
  return willOpen
}
