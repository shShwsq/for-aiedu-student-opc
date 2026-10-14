/**
 * thinking_delta 合帧缓冲单元测试。
 *
 * 钉住这段逻辑之所以存在的理由:后端每 chunk 推一条增量(实测 20-30 条/秒),
 * 逐条写响应式状态 = 逐 token 重渲染整个对话面板;审查阶段单次重渲染实测
 * 45-70ms,主线程被占满就是"看检查助手时界面卡死十几秒"。
 * 所以缓冲必须做到:每帧只写一次、顺序不串、边界不丢字。
 */
import { describe, expect, it, vi } from 'vitest'
import { createDeltaBuffer, type DeltaSink } from './thinkingDeltaBuffer'

/** 手动驱动的调度器:测试里自己决定"这一帧什么时候到来" */
function createClock() {
  const queue: Array<() => void> = []
  let scheduled = 0
  return {
    schedule: (cb: () => void) => {
      scheduled += 1
      queue.push(cb)
    },
    /** 放行一帧;返回是否真有活干 */
    tick(): boolean {
      const cb = queue.shift()
      if (!cb) return false
      cb()
      return true
    },
    get scheduled() {
      return scheduled
    },
    get pendingFrames() {
      return queue.length
    },
  }
}

/** 记录写入轨迹的 sink */
function createRecorder() {
  const calls: Array<{ fn: string; convId: string; text: string }> = []
  let afterFlushCount = 0
  const sink: DeltaSink = {
    appendReasoning: (convId, text) => calls.push({ fn: 'reasoning', convId, text }),
    appendContent: (convId, text) => calls.push({ fn: 'content', convId, text }),
    afterFlush: () => {
      afterFlushCount += 1
    },
  }
  return { sink, calls, afterFlushCount: () => afterFlushCount }
}

describe('createDeltaBuffer:合帧', () => {
  it('同一卡片的多条增量合并成一次写入,调度只排一帧', () => {
    const clock = createClock()
    const { sink, calls } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('c1', 'reasoning', 'a')
    buf.push('c1', 'reasoning', 'b')
    buf.push('c1', 'reasoning', 'c')

    expect(clock.scheduled).toBe(1)
    expect(clock.pendingFrames).toBe(1)
    expect(calls).toHaveLength(0) // 帧没到,一个字都不该写

    clock.tick()
    expect(calls).toEqual([{ fn: 'reasoning', convId: 'c1', text: 'abc' }])
  })

  it('reasoning / content 各归各的,且同卡片内思考先于正文', () => {
    const clock = createClock()
    const { sink, calls } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('c1', 'content', '答案')
    buf.push('c1', 'reasoning', '想想')
    buf.push('c1', 'content', '继续')
    clock.tick()

    expect(calls).toEqual([
      { fn: 'reasoning', convId: 'c1', text: '想想' },
      { fn: 'content', convId: 'c1', text: '答案继续' },
    ])
  })

  it('不同 conv_id(并行子任务/子智能体)互不串扰', () => {
    const clock = createClock()
    const { sink, calls } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('a', 'reasoning', 'A1')
    buf.push('b', 'reasoning', 'B1')
    buf.push('a', 'reasoning', 'A2')
    clock.tick()

    expect(calls).toEqual([
      { fn: 'reasoning', convId: 'a', text: 'A1A2' },
      { fn: 'reasoning', convId: 'b', text: 'B1' },
    ])
  })

  it('每帧一次 afterFlush(置底之类动作据此降频)', () => {
    const clock = createClock()
    const { sink, afterFlushCount } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('a', 'reasoning', '1')
    buf.push('a', 'reasoning', '2')
    buf.push('a', 'reasoning', '3')
    clock.tick()
    expect(afterFlushCount()).toBe(1)
  })

  it('空增量不入缓冲、不排帧(后端 start/end 相位 delta 为空串)', () => {
    const clock = createClock()
    const { sink, calls } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('a', 'reasoning', '')
    expect(clock.scheduled).toBe(0)
    expect(buf.pendingCount).toBe(0)
    clock.tick()
    expect(calls).toHaveLength(0)
  })

  it('手动 flush 同步落盘,且幂等(空缓冲不再触发 afterFlush)', () => {
    const clock = createClock()
    const { sink, calls, afterFlushCount } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('a', 'reasoning', 'tail')
    buf.flush()
    expect(calls).toEqual([{ fn: 'reasoning', convId: 'a', text: 'tail' }])
    expect(buf.pendingCount).toBe(0)
    expect(afterFlushCount()).toBe(1)

    buf.flush() // 收尾相位会再 flush 一次:不该产生第二次 afterFlush
    expect(afterFlushCount()).toBe(1)
  })

  it('flush 之后同一帧排到的回调空跑,不会重复写(不丢也不重)', () => {
    const clock = createClock()
    const { sink, calls } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('a', 'reasoning', 'x')
    buf.flush() // 边界提前冲(如 conversation 落库退役卡片)
    clock.tick() // 原来排的那帧现在才到
    expect(calls).toEqual([{ fn: 'reasoning', convId: 'a', text: 'x' }])

    buf.push('a', 'reasoning', 'y')
    expect(clock.scheduled).toBe(2) // 重新排帧,后续增量照常生效
    clock.tick()
    expect(calls).toHaveLength(2)
    expect(calls[1]).toEqual({ fn: 'reasoning', convId: 'a', text: 'y' })
  })

  it('clear 丢弃缓冲:卡片被清空/视图卸载后不再回写', () => {
    const clock = createClock()
    const { sink, calls } = createRecorder()
    const buf = createDeltaBuffer(sink, clock.schedule)

    buf.push('a', 'reasoning', 'dropped')
    buf.clear()
    expect(buf.pendingCount).toBe(0)
    clock.tick()
    expect(calls).toHaveLength(0)
  })

  it('sink 找不到卡片时由调用方决定作废:缓冲本身仍会交出去', () => {
    // 卡片已被 conversation 事件退役 → TaskDetailView 的 sink 里 return,
    // 落库行带全文接管展示;这里只保证缓冲不会自己吞字
    const clock = createClock()
    const seen: string[] = []
    const buf = createDeltaBuffer(
      {
        appendReasoning: (_id, text) => seen.push(text),
        appendContent: (_id, text) => seen.push(text),
      },
      clock.schedule,
    )
    buf.push('retired', 'reasoning', '全文')
    clock.tick()
    expect(seen).toEqual(['全文'])
  })

  it('默认调度在没有 rAF 的环境退到定时器(不在浏览器里也不抛)', () => {
    // 退出条件:SSR / 无 rAF 的宿主。退到 setTimeout 后仍要把字写出来
    vi.useFakeTimers()
    const raf = globalThis.requestAnimationFrame
    // @ts-expect-error 故意摘掉 rAF,验证退化分支
    globalThis.requestAnimationFrame = undefined
    try {
      const { sink, calls } = createRecorder()
      const buf = createDeltaBuffer(sink) // 不传 schedule → 走默认调度
      buf.push('a', 'reasoning', 'later')
      expect(calls).toHaveLength(0)
      vi.advanceTimersByTime(50)
      expect(calls).toEqual([{ fn: 'reasoning', convId: 'a', text: 'later' }])
    } finally {
      globalThis.requestAnimationFrame = raf
      vi.useRealTimers()
    }
  })
})
