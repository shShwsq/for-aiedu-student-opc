/**
 * 右侧栏「左缘手柄拖拽调宽」通用逻辑
 *
 * 交互模型:栏停靠视口右缘,手柄贴在栏的左边界 —— 手柄往左拖(clientX 变小)变宽,
 * 主区按 flex 自动让位。只改宽度,不负责把面板拖离右缘(那不是本 composable 的范围)。
 *
 * 五个易踩的点收敛在这里,调用方不必各写一遍:
 * 1. 钳位:上限可再按视口比例收(maxViewportRatio),避免右栏吃掉主对话流的阅读宽度;
 *    视口变窄时重算,不让已存宽度顶破新上限。
 * 2. 窄屏:视口 ≤ narrowMax 时宿主 CSS 会把侧栏改成覆盖式抽屉,宽度归 CSS 管,
 *    此时 isNarrow 为真 —— 模板据此隐藏手柄、不加内联宽度(拖一个浮层没有意义)。
 *    断点必须与宿主 CSS 的 @media 取值一致,否则两边判断会错位。
 * 3. 指针跟手:move/up 监听挂在 window 上,手速快时指针甩出 5px 手柄也不会断拖;
 *    pointercancel(触屏手势接管 / 系统中断)同样收尾。
 * 4. 拖拽期间给 document.body 挂 `is-resizing-sidebar` 类(user-select:none + col-resize):
 *    右栏往左拖 = 指针移进主对话流,只禁侧栏选中会在主区把正文选蓝、光标反馈也会断。
 *    (pointerdown 的 preventDefault 能挡掉大部分浏览器发起的选区,但不保证跨浏览器,
 *    故再加一层全局兜底;顺带修掉源码查阅栏里 CodeMirror 抢选中 / 抢指针的老问题。)
 * 5. 双击复位不靠原生 dblclick:手柄 pointerdown 里 preventDefault 后,部分浏览器不再
 *    补发 click / dblclick;改在 pointerup 用「按下没移动 + 距上次点击 < 间隔」自行判定。
 *
 * 持久化是可选的:传 storageKey 才读写 localStorage(键建议带用户维度,见 buildStorageKey);
 * 不传则只在当前组件实例内有效,刷新回默认宽度。
 */
import { computed, onBeforeUnmount, onMounted, ref, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'

/** localStorage 键前缀(栏位 + 用户维度拼在后面) */
const LS_KEY_PREFIX = 'secondlook:sidebar-width'

/** 键盘微调步长(px):方向键每次挪多少 */
const KEY_STEP = 24

/** 双击判定间隔(ms):两次"按下没移动"的点击隔在这之内算一次双击复位 */
const DOUBLE_CLICK_MS = 300

/** 双击判定的位移容差(px):按下到抬起移动超过它就当成真拖拽,不再计为"点击" */
const CLICK_SLOP_PX = 4

/** 拖拽期间挂在 document.body 上的类名(全局禁选中 + 统一光标) */
const BODY_RESIZE_CLASS = 'is-resizing-sidebar'

/** 拖拽期间盖在 body 上的全屏透明遮罩类名(接管指针,锁光标 / 防正文选区 / 防 iframe·CodeMirror 抢事件) */
const OVERLAY_CLASS = 'sidebar-resize-overlay'

export interface ResizableSidebarOptions {
  /** 最小宽度:再窄内容(卡片/正文)就开始折行,不宜再小 */
  min: number
  /** 最大宽度:绝对上限,与视口比例上限取小 */
  max: number
  /** 默认宽度,同时是双击手柄的复位值 */
  defaultWidth: number
  /** 窄屏断点(px):视口 ≤ 该值视为覆盖抽屉态,禁用拖拽与内联宽度。默认 640 */
  narrowMax?: number
  /** 上限按视口宽度再收一道(0~1;0 = 不额外限制),保护主区阅读宽度 */
  maxViewportRatio?: number
  /** 返回 localStorage 键;返 null / 未提供则不持久化。
   *  读只在挂载时发生一次(受保护路由的守卫已 await fetchMe,届时 email 已就位)*/
  storageKey?: MaybeRefOrGetter<string | null>
}

/**
 * 钳位到 [min, max],取整。
 * min 优先于 max:极端窄屏下比例上限可能被压到 min 以下,此时保内容不塌陷,
 * 让主区(flex)自行压缩,而不是算出非法宽度。
 */
export function clampWidth(raw: number, min: number, max: number): number {
  if (!Number.isFinite(raw)) return min
  const hi = Math.max(min, max)
  return Math.round(Math.min(hi, Math.max(min, raw)))
}

/** 实际上限:max 与「视口宽 × ratio」取小(ratio ≤ 0 或视口不可用时只认 max) */
export function resolveMaxWidth(max: number, viewportWidth: number, ratio: number): number {
  if (ratio <= 0 || !Number.isFinite(viewportWidth) || viewportWidth <= 0) return max
  return Math.min(max, Math.floor(viewportWidth * ratio))
}

/** 持久化键:secondlook:sidebar-width:{slot}:{email}(email 编码,可能含特殊字符) */
export function buildStorageKey(slot: string, email?: string | null): string {
  const who = email ? encodeURIComponent(email) : 'anon'
  return `${LS_KEY_PREFIX}:${slot}:${who}`
}

/** 读已存宽度:缺失 / 非法回落默认,越界钳回区间(不直接丢弃用户的手感) */
export function readStoredWidth(
  raw: string | null | undefined,
  min: number,
  max: number,
  fallback: number,
): number {
  if (raw === null || raw === undefined || raw === '') return fallback
  const n = Number(raw)
  if (!Number.isFinite(n)) return fallback
  return clampWidth(n, min, max)
}

export function useResizableSidebar(options: ResizableSidebarOptions) {
  const { min, max, defaultWidth } = options
  const narrowMax = options.narrowMax ?? 640
  const ratio = options.maxViewportRatio ?? 0

  const width = ref(defaultWidth)
  const resizing = ref(false)
  /** 窄屏(覆盖抽屉态):手柄隐藏,内联宽度不生效 */
  const isNarrow = ref(false)
  /** 视口宽(随 resize 更新),供比例上限重算 */
  const viewportWidth = ref(0)

  let startX = 0
  let startW = 0
  /** 本次按下到抬起是否真的拖动过(位移超容差),用于区分"点击"与"拖拽" */
  let movedDuringDrag = false
  /** 上一次"点击"(按下没移动)的时间戳,配合 DOUBLE_CLICK_MS 判定双击复位 */
  let lastTapAt = 0
  /** 拖拽期间的透明遮罩(接管指针,松手 / 卸载即移除) */
  let overlay: HTMLElement | null = null
  let mql: MediaQueryList | null = null

  /** 当前视口下允许的上限 */
  const upperBound = computed(() => resolveMaxWidth(max, viewportWidth.value, ratio))

  function applyWidth(next: number): void {
    width.value = clampWidth(next, min, upperBound.value)
  }

  /** 拖拽期间的全局态:body 类(禁选中)+ 全屏遮罩(接管指针,锁光标)。
   *  右栏往左拖指针会移进主区,只禁侧栏会在主区选蓝正文、光标也会断(见文件头说明 4)。 */
  function setBodyResizing(on: boolean): void {
    if (typeof document === 'undefined') return
    document.body.classList.toggle(BODY_RESIZE_CLASS, on)
    if (on && !overlay) {
      overlay = document.createElement('div')
      overlay.className = OVERLAY_CLASS
      // 遮罩不带内容,纯接管指针;样式见 global.css(不能放 scoped,它挂在 body 上)
      document.body.appendChild(overlay)
    } else if (!on && overlay) {
      overlay.remove()
      overlay = null
    }
  }

  /** 摘掉 window 上的拖拽监听(松手 / 卸载都要走,别留悬空 pointermove) */
  function removeWindowListeners(): void {
    window.removeEventListener('pointermove', onResizeMove)
    window.removeEventListener('pointerup', onResizeEnd)
    window.removeEventListener('pointercancel', onResizeEnd)
  }

  /** 手柄 pointerdown:记录起点,把 move/up 挂到 window(见文件头说明 3) */
  function startResize(e: PointerEvent): void {
    if (isNarrow.value) return
    resizing.value = true
    startX = e.clientX
    startW = width.value
    movedDuringDrag = false
    setBodyResizing(true)
    window.addEventListener('pointermove', onResizeMove)
    window.addEventListener('pointerup', onResizeEnd)
    window.addEventListener('pointercancel', onResizeEnd)
    // 阻止默认:避免拖拽被当成文本选择 / 触摸滚动手势
    e.preventDefault()
  }

  function onResizeMove(e: PointerEvent): void {
    if (!resizing.value) return
    if (Math.abs(e.clientX - startX) > CLICK_SLOP_PX) movedDuringDrag = true
    // 栏靠右:手柄往左拖(clientX 变小)应变宽,故 dx 取反
    applyWidth(startW + (startX - e.clientX))
  }

  function onResizeEnd(): void {
    removeWindowListeners()
    if (!resizing.value) return
    resizing.value = false
    setBodyResizing(false)
    if (movedDuringDrag) {
      // 真拖过:松手才落盘(拖动过程中不频繁写)
      lastTapAt = 0
      persist()
      return
    }
    // 没移动 → 记为一次"点击";两次间隔够近就当双击复位(见文件头说明 5)
    const now = Date.now()
    if (now - lastTapAt < DOUBLE_CLICK_MS) {
      lastTapAt = 0
      resetWidth()
    } else {
      lastTapAt = now
    }
  }

  /** 键盘微调(无障碍):← 变宽 / → 变窄,与手柄拖动同向;Home 复位 */
  function onResizeKeydown(e: KeyboardEvent): void {
    if (isNarrow.value) return
    if (e.key === 'ArrowLeft') applyWidth(width.value + KEY_STEP)
    else if (e.key === 'ArrowRight') applyWidth(width.value - KEY_STEP)
    else if (e.key === 'Home') resetWidth()
    else return
    e.preventDefault()
  }

  /** 复位默认宽度(双击手柄触发,onResizeEnd 里判定;键盘 Home 也走这里) */
  function resetWidth(): void {
    applyWidth(defaultWidth)
    persist()
  }

  function currentKey(): string | null {
    return options.storageKey ? toValue(options.storageKey) : null
  }

  function persist(): void {
    const key = currentKey()
    if (!key) return
    try {
      localStorage.setItem(key, String(width.value))
    } catch {
      // 隐私模式 / 禁用 localStorage:静默,本次会话内仍可调
    }
  }

  function restore(): void {
    const key = currentKey()
    if (!key) return
    try {
      applyWidth(readStoredWidth(localStorage.getItem(key), min, max, defaultWidth))
    } catch {
      // 读不到就当没存过,用默认宽度
    }
  }

  function onNarrowChange(): void {
    isNarrow.value = mql?.matches ?? false
  }

  /** 视口变化:更新视口宽并按新上限重钳(不把顶破上限的宽度留在原地) */
  function onViewportResize(): void {
    viewportWidth.value = window.innerWidth
    applyWidth(width.value)
  }

  onMounted(() => {
    viewportWidth.value = window.innerWidth
    mql = window.matchMedia(`(max-width: ${narrowMax}px)`)
    isNarrow.value = mql.matches
    mql.addEventListener('change', onNarrowChange)
    window.addEventListener('resize', onViewportResize)
    restore()
  })

  onBeforeUnmount(() => {
    // 卸载收尾:摘监听 + 撤全局态(不走 onResizeEnd,免得半途离开时误触发双击复位 / 落盘)
    removeWindowListeners()
    resizing.value = false
    setBodyResizing(false)
    mql?.removeEventListener('change', onNarrowChange)
    mql = null
    window.removeEventListener('resize', onViewportResize)
  })

  /** 模板用:窄屏交给 CSS 定宽,其余时刻内联宽度 */
  const inlineWidth = computed(() =>
    isNarrow.value ? undefined : { width: `${width.value}px` },
  )

  // 无障碍:可聚焦 separator 须暴露取值范围(WAI-ARIA Window Splitter)。
  // valuemax 用「当前视口下的实际上限」而非静态 max,读屏才不会被引导去拖到钳位外。
  const ariaValueNow = computed(() => width.value)
  const ariaValueMin = computed(() => min)
  const ariaValueMax = computed(() => Math.max(min, upperBound.value))

  return {
    width,
    resizing,
    isNarrow,
    inlineWidth,
    ariaValueNow,
    ariaValueMin,
    ariaValueMax,
    startResize,
    onResizeKeydown,
    resetWidth,
  }
}
