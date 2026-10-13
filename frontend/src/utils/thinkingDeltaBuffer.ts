/**
 * 流式增量合帧缓冲(纯内核,无 Vue/DOM 依赖)
 *
 * 动机:后端每个 LLM chunk 推一条 thinking_delta(实测 20-30 条/秒),而一条增量
 * 直接写进 reactive 的 streamingItems,就让整个对话面板重渲染一次。重渲染的成本
 * 不是常数:它按"本轮已落库的思考链条目数"叠加(生产任务 ba48dbe4 实测——落库
 * 9 条时单次 45ms、16 条共 4.4 万字时单次 70ms)。25 条/秒 × 45ms 就是主线程每秒
 * 要干一秒多的活,表现为"看检查助手核查时界面卡死十几二十秒,然后跳一下"。
 *
 * 解法:增量先攒进**非响应式**缓冲,每帧只往响应式状态写一次。文本仍逐帧增长
 * (打字机效果照旧),但重渲染次数从"每 token"降到"每帧"。
 *
 * 边界必须 flush():phase=end/error/notice、conversation 落库退役卡片、整页快照、
 * 组件卸载这些"这段文本不会再有下一次增量"的时刻,如果不同步落盘,最后一段
 * 增量就会烂在缓冲里。规则:**先 flush 再改/删卡片**。
 */

/** 缓冲把合好的文本交给谁:实时卡片实体由调用方持有,本模块不碰响应式状态 */
export interface DeltaSink {
  /** 思考链(reasoning 相位)增量 */
  appendReasoning(convId: string, text: string): void
  /** 正文(content 相位)增量 */
  appendContent(convId: string, text: string): void
  /** 每次 flush 末尾回调一次(把"每帧一次"的动作挂这里,如滚动置底) */
  afterFlush?: () => void
}

export interface DeltaBuffer {
  /**
   * 累积一条增量。同一 conv_id 内 reasoning / content 各按到达顺序拼接,
   * 两类文本互不串位;不同 conv_id(并行分面/子智能体)天然各自独立。
   */
  push(convId: string, phase: 'reasoning' | 'content', text: string): void
  /** 同步把缓冲写入 sink(幂等:空缓冲直接返回,不触发 afterFlush) */
  flush(): void
  /** 丢弃缓冲(卡片即将被清空/退役,或视图已卸载) */
  clear(): void
  /** 待写入的卡片数(测试与诊断用) */
  readonly pendingCount: number
}

/**
 * 默认调度:优先 requestAnimationFrame —— 后台标签页不触发回调,增量安静地攒着,
 * 回到前台一次性补上(既省 CPU 也不丢内容);没有 rAF 的环境退到 16ms 定时。
 */
function defaultSchedule(cb: () => void): void {
  const raf = (globalThis as { requestAnimationFrame?: (cb: () => void) => unknown })
    .requestAnimationFrame
  if (typeof raf === 'function') raf(cb)
  else setTimeout(cb, 16)
}

interface PendingEntry {
  reasoning: string
  content: string
}

/**
 * 创建合帧缓冲。
 *
 * @param sink 落文本的地方(调用方按 conv_id 找到实时卡片写入;找不到就作废)
 * @param schedule 帧调度注入点(测试里传一个可手动触发的实现)
 */
export function createDeltaBuffer(
  sink: DeltaSink,
  schedule: (cb: () => void) => void = defaultSchedule,
): DeltaBuffer {
  const pending = new Map<string, PendingEntry>()
  let scheduled = false

  function flush(): void {
    scheduled = false
    if (pending.size === 0) return
    // 快照键列表再遍历:边写边删时不依赖迭代器在删除中的行为
    for (const convId of [...pending.keys()]) {
      const buf = pending.get(convId)
      if (!buf) continue
      pending.delete(convId)
      // 顺序照旧:同一张卡的 reasoning 先于 content(与逐条处理时的相对顺序一致)
      if (buf.reasoning) sink.appendReasoning(convId, buf.reasoning)
      if (buf.content) sink.appendContent(convId, buf.content)
    }
    sink.afterFlush?.()
  }

  return {
    push(convId, phase, text) {
      if (!text) return
      let buf = pending.get(convId)
      if (!buf) {
        buf = { reasoning: '', content: '' }
        pending.set(convId, buf)
      }
      if (phase === 'reasoning') buf.reasoning += text
      else buf.content += text
      if (!scheduled) {
        scheduled = true
        schedule(flush)
      }
    },
    flush,
    clear() {
      pending.clear()
      scheduled = false
    },
    get pendingCount() {
      return pending.size
    },
  }
}
