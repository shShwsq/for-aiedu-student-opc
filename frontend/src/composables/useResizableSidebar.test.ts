/**
 * useResizableSidebar 单测
 *
 * 覆盖右栏调宽的关键行为:
 * - 钳位:min 优先于 max(极端窄屏下比例上限被压穿时不返回非法宽度)
 * - 视口比例上限:与绝对 max 取小,视口变窄时重算已存宽度
 * - 拖拽方向:右栏手柄往左拖(clientX 变小)应变宽
 * - 窄屏态:覆盖抽屉下不响应拖拽,内联宽度交给 CSS
 * - 持久化:松手才落盘,读盘时非法回落默认、越界钳回区间
 *
 * composable 依赖 onMounted/onBeforeUnmount,用一个挂载空组件的 withSetup 壳驱动
 * 生命周期(仍不渲染任何 UI,不是组件测试套件)。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, type App } from 'vue'

import {
  buildStorageKey,
  clampWidth,
  readStoredWidth,
  resolveMaxWidth,
  useResizableSidebar,
} from './useResizableSidebar'

// ---- 纯函数部分 ----

describe('clampWidth', () => {
  it('区间内原样取整,越界钳到边界', () => {
    expect(clampWidth(420.4, 320, 640)).toBe(420)
    expect(clampWidth(100, 320, 640)).toBe(320)
    expect(clampWidth(9999, 320, 640)).toBe(640)
  })

  it('非有限值回落 min(不让 NaN 变成非法宽度)', () => {
    expect(clampWidth(Number.NaN, 320, 640)).toBe(320)
    expect(clampWidth(Number.POSITIVE_INFINITY, 320, 640)).toBe(320)
  })

  it('min 优先于 max:上限被压到 min 以下时仍返回 min', () => {
    expect(clampWidth(500, 320, 100)).toBe(320)
  })
})

describe('resolveMaxWidth', () => {
  it('ratio ≤ 0 只认绝对 max', () => {
    expect(resolveMaxWidth(640, 1400, 0)).toBe(640)
    expect(resolveMaxWidth(640, 1400, -1)).toBe(640)
  })

  it('视口宽 × ratio 与绝对 max 取小', () => {
    expect(resolveMaxWidth(640, 1000, 0.5)).toBe(500)
    expect(resolveMaxWidth(640, 2000, 0.5)).toBe(640)
  })

  it('视口不可用(0 / NaN)时退回 max', () => {
    expect(resolveMaxWidth(640, 0, 0.5)).toBe(640)
    expect(resolveMaxWidth(640, Number.NaN, 0.5)).toBe(640)
  })
})

describe('buildStorageKey', () => {
  it('email 参与编码,避免特殊字符撞键', () => {
    expect(buildStorageKey('task-detail', 'a+b@example.com')).toBe(
      'secondlook:sidebar-width:task-detail:a%2Bb%40example.com',
    )
  })

  it('无 email 落到 anon 分支(与已登录用户不共用一份宽度)', () => {
    expect(buildStorageKey('task-detail', null)).toBe('secondlook:sidebar-width:task-detail:anon')
  })
})

describe('readStoredWidth', () => {
  it('缺失 / 空串 / 非法值回落默认', () => {
    expect(readStoredWidth(null, 320, 640, 420)).toBe(420)
    expect(readStoredWidth('', 320, 640, 420)).toBe(420)
    expect(readStoredWidth('abc', 320, 640, 420)).toBe(420)
  })

  it('越界值钳回区间而不是丢弃(保住用户上次的手感)', () => {
    expect(readStoredWidth('5000', 320, 640, 420)).toBe(640)
    expect(readStoredWidth('80', 320, 640, 420)).toBe(320)
  })
})

// ---- composable 部分 ----

/**
 * 在组件上下文里跑一次 setup 并挂载,以便 onMounted / onBeforeUnmount 正常触发。
 * 组件不渲染任何 UI,只作为生命周期载体。
 */
function withSetup<T>(composable: () => T): { result: T; unmount: () => void } {
  let result!: T
  const Comp = defineComponent({
    setup() {
      result = composable()
      return () => h('div')
    },
  })
  const app: App = createApp(Comp)
  app.mount(document.createElement('div'))
  return { result, unmount: () => app.unmount() }
}

/** 设视口宽,并装一个按当前 innerWidth 求值的 matchMedia(jsdom 无 PointerEvent/真实媒体查询) */
function setViewport(width: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  window.matchMedia = vi.fn().mockImplementation((query: string) => {
    const m = /max-width:\s*(\d+)px/.exec(query)
    const bp = m ? Number(m[1]) : 640
    return {
      matches: window.innerWidth <= bp,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }
  }) as unknown as typeof window.matchMedia
}

/** jsdom 没有 PointerEvent 构造器;处理器只读 clientX,用同类型的 MouseEvent 顶替 */
function pointer(type: string, clientX = 0): PointerEvent {
  return new MouseEvent(type, { clientX, bubbles: true }) as unknown as PointerEvent
}

function dispatchPointer(type: string, clientX = 0): void {
  window.dispatchEvent(pointer(type, clientX))
}

beforeEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  vi.restoreAllMocks()
  setViewport(1440)
})

describe('useResizableSidebar 拖拽', () => {
  it('右栏:手柄往左拖变宽,往右拖变窄(相对起点,非累加)', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.startResize(pointer('pointerdown', 1000))

    dispatchPointer('pointermove', 900)
    expect(result.width.value).toBe(520)

    dispatchPointer('pointermove', 1050)
    expect(result.width.value).toBe(370)

    dispatchPointer('pointerup', 1050)
    expect(result.resizing.value).toBe(false)
    unmount()
  })

  it('拖不出绝对上限 max', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 0)
    expect(result.width.value).toBe(640)
    dispatchPointer('pointerup')
    unmount()
  })

  it('视口变窄后按比例上限重钳(不保留顶破上限的宽度)', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({
        min: 320, max: 640, defaultWidth: 420, narrowMax: 1024, maxViewportRatio: 0.5,
      }),
    )
    // 1440 视口:比例上限 720 > max,取 640
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 100)
    expect(result.width.value).toBe(640)
    dispatchPointer('pointerup')

    // 视口缩到 1100:上限收成 550,resize 后重钳
    setViewport(1100)
    window.dispatchEvent(new Event('resize'))
    expect(result.width.value).toBe(550)
    unmount()
  })

  it('窄屏态不响应拖拽,内联宽度交给 CSS', () => {
    setViewport(800)
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    expect(result.isNarrow.value).toBe(true)
    expect(result.inlineWidth.value).toBeUndefined()

    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 200)
    expect(result.width.value).toBe(420)
    unmount()
  })

  it('键盘微调:← 变宽 / → 变窄,与手柄同向;Home 复位', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.onResizeKeydown(new KeyboardEvent('keydown', { key: 'ArrowLeft' }))
    expect(result.width.value).toBe(444)
    result.onResizeKeydown(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    result.onResizeKeydown(new KeyboardEvent('keydown', { key: 'ArrowRight' }))
    expect(result.width.value).toBe(396)
    result.onResizeKeydown(new KeyboardEvent('keydown', { key: 'Home' }))
    expect(result.width.value).toBe(420)
    unmount()
  })

  it('卸载收尾:摘掉 window 监听,不留悬空 pointermove', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.startResize(pointer('pointerdown', 1000))
    unmount()
    dispatchPointer('pointermove', 500)
    expect(result.width.value).toBe(420)
  })
})

describe('useResizableSidebar 双击复位(不靠原生 dblclick)', () => {
  it('两次"按下没移动"的点击间隔够近 → 复位默认宽度', () => {
    vi.useFakeTimers()
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    // 先拖宽
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 900)
    dispatchPointer('pointerup')
    expect(result.width.value).toBe(520)

    // 第一次点击(按下即抬,不移动)
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointerup')
    expect(result.width.value).toBe(520) // 单击不复位

    // 100ms 内第二次点击 → 判定双击,复位
    vi.advanceTimersByTime(100)
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointerup')
    expect(result.width.value).toBe(420)
    unmount()
  })

  it('间隔过久的两次点击不复位', () => {
    vi.useFakeTimers()
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 900)
    dispatchPointer('pointerup')
    expect(result.width.value).toBe(520)

    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointerup')
    vi.advanceTimersByTime(1000) // 超过 300ms 间隔
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointerup')
    expect(result.width.value).toBe(520) // 不算双击
    unmount()
  })

  it('真拖拽(移动超容差)不计为点击,不会误触发复位', () => {
    vi.useFakeTimers()
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    // 连来两次快速"拖拽"(非点击):第二次不得复位
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 900)
    dispatchPointer('pointerup')
    vi.advanceTimersByTime(50)
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 950)
    dispatchPointer('pointerup')
    // 起点宽 520,dx=50 → 570(而非复位到默认 420)
    expect(result.width.value).toBe(570)
    unmount()
  })
})

describe('useResizableSidebar 拖拽期间的全局态', () => {
  it('startResize 挂 body 类 + 全屏遮罩,pointerup 撤干净', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.startResize(pointer('pointerdown', 1000))
    expect(document.body.classList.contains('is-resizing-sidebar')).toBe(true)
    expect(document.querySelector('.sidebar-resize-overlay')).not.toBeNull()

    dispatchPointer('pointerup')
    expect(document.body.classList.contains('is-resizing-sidebar')).toBe(false)
    expect(document.querySelector('.sidebar-resize-overlay')).toBeNull()
    unmount()
  })

  it('拖拽中途卸载也要撤掉全局态(不留悬空遮罩)', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.startResize(pointer('pointerdown', 1000))
    unmount()
    expect(document.body.classList.contains('is-resizing-sidebar')).toBe(false)
    expect(document.querySelector('.sidebar-resize-overlay')).toBeNull()
  })
})

describe('useResizableSidebar 无障碍取值', () => {
  it('ariaValueNow 跟随宽度,min 固定,max 按视口比例上限收', () => {
    const { result, unmount } = withSetup(
      () => useResizableSidebar({
        min: 320, max: 640, defaultWidth: 420, narrowMax: 1024, maxViewportRatio: 0.5,
      }),
    )
    expect(result.ariaValueNow.value).toBe(420)
    expect(result.ariaValueMin.value).toBe(320)
    // 1440 视口 × 0.5 = 720 > max → 取静态 max 640
    expect(result.ariaValueMax.value).toBe(640)

    // 视口缩到 1000(>1024? 否,仍宽屏外)→ 用 1100:上限 550
    setViewport(1100)
    window.dispatchEvent(new Event('resize'))
    expect(result.ariaValueMax.value).toBe(550)
    unmount()
  })
})

describe('useResizableSidebar 持久化', () => {
  it('给了 storageKey 才落盘,且松手时写一次(拖动中不频繁写)', () => {
    const key = buildStorageKey('task-detail', 'me@example.com')
    const { result, unmount } = withSetup(
      () => useResizableSidebar({
        min: 320, max: 640, defaultWidth: 420, narrowMax: 1024,
        storageKey: () => key,
      }),
    )
    result.startResize(pointer('pointerdown', 1000))
    dispatchPointer('pointermove', 950)
    expect(result.width.value).toBe(470)
    expect(localStorage.getItem(key)).toBeNull()
    dispatchPointer('pointerup')
    expect(localStorage.getItem(key)).toBe('470')
    unmount()
  })

  it('不传 storageKey 时完全不碰 localStorage', () => {
    const spy = vi.spyOn(Storage.prototype, 'setItem')
    const { result, unmount } = withSetup(
      () => useResizableSidebar({ min: 320, max: 640, defaultWidth: 420, narrowMax: 1024 }),
    )
    result.resetWidth()
    expect(spy).not.toHaveBeenCalled()
    unmount()
  })

  it('挂载时读回上次宽度', () => {
    const key = buildStorageKey('task-detail', 'me@example.com')
    localStorage.setItem(key, '560')
    const { result, unmount } = withSetup(
      () => useResizableSidebar({
        min: 320, max: 640, defaultWidth: 420, narrowMax: 1024,
        storageKey: () => key,
      }),
    )
    expect(result.width.value).toBe(560)
    unmount()
  })

  it('storageKey 解析为 null(用户尚未就位)时既不读也不写', () => {
    const key = buildStorageKey('task-detail', 'me@example.com')
    localStorage.setItem(key, '560')
    const { result, unmount } = withSetup(
      () => useResizableSidebar({
        min: 320, max: 640, defaultWidth: 420, narrowMax: 1024,
        storageKey: () => null,
      }),
    )
    expect(result.width.value).toBe(420)
    result.resetWidth()
    expect(localStorage.getItem(key)).toBe('560') // 原样未动
    unmount()
  })
})
