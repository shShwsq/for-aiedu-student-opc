/**
 * 思考卡片的展开状态判定(纯函数,无 Vue/DOM 依赖)
 *
 * 设计取向:思考链属于"运行中的直播内容",由生命周期驱动展开/折叠,
 * 而不是让用户点一次才能看见一次:
 * - 流式中:reasoning 累积过阈值后置闩锁(auto),卡片展开跟着 token 走;
 *   阈值之下的短思考不展开,避免一次任务里几十次"展开↔收起"频闪。
 * - 结束后:折叠回一行标题(过程噪音不该长期留在聊天记录里),但保留一段
 *   宽限期(grace),让"收起"不跟"正文出现"挤在同一帧。
 * - 用户手动点过:pin 优先于以上两条规则,后续增量不得覆盖用户意图。
 *
 * 与执行过程组(step group)的展开规则同构(见 TaskDetailView.isStepExpanded:
 * 活跃轮的前沿组展开、轮闭合后收起),两层合起来就是"看直播 + 读干净的记录"。
 */

/** 思考链累积到多少字符才自动展开(中文约一句半;短思考保持单行标题) */
export const AUTO_EXPAND_MIN_CHARS = 80

/** 流式结束后保持展开的宽限时长(ms),之后自动折叠 */
export const COLLAPSE_GRACE_MS = 400

/** 思考卡展开状态的最小结构形状(与 TaskDetailView.StreamingItem 兼容) */
export interface ThinkingStateLike {
  /** 'streaming' 仍在收增量 / 'done' 已收到 end / 'error' 流式失败 */
  status: string
  reasoning?: string
  /** 闩锁:流式中已跨过自动展开阈值(单调,避免每字符重新决策) */
  reasoning_auto?: boolean
  /** 宽限期:结束后暂不折叠(由定时器在 COLLAPSE_GRACE_MS 后清掉) */
  reasoning_grace?: boolean
  /** 用户手动钉住:true 强制展开 / false 强制折叠 / null|undefined 跟随生命周期 */
  reasoning_pin?: boolean | null
}

/**
 * 本次增量后是否该置起自动展开闩锁。
 * 供事件处理调用(写入侧),不在渲染期改状态。
 */
export function shouldLatchAutoExpand(state: ThinkingStateLike): boolean {
  return (
    state.status === 'streaming' &&
    (state.reasoning ?? '').length >= AUTO_EXPAND_MIN_CHARS
  )
}

/** 当前该不该展开:用户意图 > 流式闩锁 > 结束宽限期 */
export function isThinkingExpanded(state: ThinkingStateLike): boolean {
  if (typeof state.reasoning_pin === 'boolean') return state.reasoning_pin
  if (state.status === 'streaming') return state.reasoning_auto === true
  return state.reasoning_grace === true
}
