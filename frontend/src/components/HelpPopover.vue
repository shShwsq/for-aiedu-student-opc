<script setup lang="ts">
/**
 * 帮助气泡(圆圈问号按钮 + 点击展开说明)
 *
 * 全站唯一的「点问号看说明」实现:CLI 设置(AgentConfigPanel)、智能体策略
 * (AgentPolicyPanel)、技能管理(SkillManagerView)、记忆设置、练习设置原先各自
 * 手写同一套「点击外部关闭」逻辑与 CSS,现统一收敛到这里。
 *
 * 用法(说明正文走默认插槽,可放 code / 列表等富文本):
 *   <HelpPopover label="查看上传说明">…说明…</HelpPopover>
 *
 * 行为约定:
 * - 点按钮开合;点气泡外部或按 Esc 关闭
 * - 全局互斥:同一时刻只展开一个气泡(点开新的自动收起旧的)
 * - 根节点 click.stop:点击不冒泡到外层 <label>(否则在开关行里点气泡会误触发切换)
 * - 定位走 @floating-ui/dom + Teleport 到 body(与 BaseSelect / ModelCombobox 同款):
 *   设置页与技能页都包在 overflow: hidden / auto 的滚动容器里,绝对定位的气泡会被
 *   裁掉(靠底的字段尤其明显),交给 floating-ui 就能自动翻到上方并贴合视口
 *
 * 命名坑(务必保留别名):floating-ui 的 size 中间件与 size prop 同名。<script setup>
 * 的顶层绑定(含 import)会暴露给模板并盖掉同名 prop —— 直接写 `size` 的话,
 * 模板里的 `help-btn-${size}` 会拼成 `help-btn-function(options){…}`,尺寸类失效、
 * 按钮塌成 0x0,全站问号按钮集体看不见。故:import 起别名 sizeMiddleware,
 * 模板里读 prop 也统一写 props.size,不给同名绑定留可乘之机。
 */
import {
  autoUpdate,
  computePosition,
  flip,
  offset,
  shift,
  size as sizeMiddleware,
} from '@floating-ui/dom'
import { computed, nextTick, onUnmounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 按钮 aria-label(读屏) */
    label?: string
    /** 按钮尺寸:sm=18px(字段/标题旁),md=28px(工具栏/顶栏) */
    size?: 'sm' | 'md'
    /** 气泡水平对齐:left=按钮左缘对齐(默认),right=按钮右缘对齐 */
    align?: 'left' | 'right'
    /** 气泡宽度(CSS width),长文案建议交给 maxWidth 自适应 */
    width?: string
    /** 气泡最大宽度:超出换行 */
    maxWidth?: string
    /** 气泡最大高度(px),超出内部滚动;视口放不下时由 size 再压扁 */
    maxHeight?: number
  }>(),
  {
    label: '查看说明',
    size: 'sm',
    align: 'left',
    width: 'max-content',
    maxWidth: 'min(300px, 80vw)',
    maxHeight: 240,
  },
)

// ============================================================
// 全局互斥 + 单一 document 监听
//
// 一个页面里可能有十几个帮助按钮(设置面板逐字段一个),逐实例挂监听既浪费
// 又要各自维护"只关自己";改为模块级注册表:谁展开谁登记,document 上只保留
// 一份 mousedown/keydown 处理器,最后一个气泡收起时再摘掉。
// ============================================================
interface OpenEntry {
  /** 判断节点是否落在该气泡(含按钮)内 */
  contains: (node: Node) => boolean
  close: () => void
}

/** 当前展开的气泡实例 */
const openEntries = new Set<OpenEntry>()
let docBound = false

function onDocMouseDown(e: MouseEvent): void {
  // 复制一份再遍历:close() 会同步修改 openEntries
  for (const entry of Array.from(openEntries)) {
    if (!entry.contains(e.target as Node)) entry.close()
  }
}

function onDocKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  for (const entry of Array.from(openEntries)) entry.close()
}

function bindDoc(): void {
  if (docBound) return
  document.addEventListener('mousedown', onDocMouseDown)
  document.addEventListener('keydown', onDocKeydown)
  docBound = true
}

function releaseDoc(): void {
  if (!docBound || openEntries.size > 0) return
  document.removeEventListener('mousedown', onDocMouseDown)
  document.removeEventListener('keydown', onDocKeydown)
  docBound = false
}

const open = ref(false)
const wrapRef = ref<HTMLElement | null>(null)
/** 气泡 DOM(Teleport 到 body,已不在 wrapRef 子树内,外部点击判定要单独带上它) */
const popoverRef = ref<HTMLElement | null>(null)

const entry: OpenEntry = {
  // 取值器形式:始终读当前 DOM,避免 v-if 重渲染后拿到过期节点
  contains: (node: Node) =>
    (wrapRef.value?.contains(node) ?? false) || (popoverRef.value?.contains(node) ?? false),
  close: () => setOpen(false),
}

function setOpen(value: boolean): void {
  if (open.value === value) return
  open.value = value
  if (value) {
    // 互斥:先收起其他已展开的气泡
    for (const other of Array.from(openEntries)) {
      if (other !== entry) other.close()
    }
    openEntries.add(entry)
    bindDoc()
    // 等 v-if 把气泡挂出来再定位(rAF 让 Teleport 后的布局稳定)
    nextTick(() => requestAnimationFrame(setupFloating))
  } else {
    openEntries.delete(entry)
    releaseDoc()
    cleanupFloating?.()
    cleanupFloating = null
  }
}

// ---- floating-ui 定位 ----
let cleanupFloating: (() => void) | null = null

function setupFloating(): void {
  const anchor = wrapRef.value
  const floating = popoverRef.value
  if (!anchor || !floating) return
  updatePosition(anchor, floating)
  cleanupFloating?.()
  cleanupFloating = autoUpdate(anchor, floating, () => updatePosition(anchor, floating))
}

function updatePosition(anchor: HTMLElement, floating: HTMLElement): void {
  computePosition(anchor, floating, {
    // 下方放不下时 flip 自动翻到上方
    placement: props.align === 'right' ? 'bottom-end' : 'bottom-start',
    // 气泡是 position: fixed(相对视口),默认 absolute 策略给的是文档坐标,
    // 窗口一滚动就会把气泡整体推下去一个 scrollY(严重时推出视口=看不到)
    strategy: 'fixed',
    middleware: [
      offset(4),
      flip({ padding: 8 }),
      // flip 只管主轴;靠右边缘的按钮(分区标题、工具栏)还需沿交叉轴推回视口内
      shift({ padding: 8 }),
      sizeMiddleware({
        padding: 8,
        apply({ availableHeight, elements }) {
          Object.assign(elements.floating.style, {
            // 视口剩余空间不足时压扁(留 80px 下限,避免被压成一条缝)
            maxHeight: `${Math.max(80, Math.min(props.maxHeight, availableHeight))}px`,
          })
        },
      }),
    ],
  }).then(({ x, y }) => {
    Object.assign(floating.style, {
      left: `${x}px`,
      top: `${y}px`,
    })
  })
}

const popoverStyle = computed(() => ({
  width: props.width,
  maxWidth: props.maxWidth,
}))

onUnmounted(() => {
  setOpen(false)
  cleanupFloating?.()
})

defineExpose({
  /** 父组件在触发动作(如切换 tab)前主动收起气泡 */
  close: () => setOpen(false),
})
</script>

<template>
  <div ref="wrapRef" class="help-wrap" @click.stop>
    <!-- 尺寸类读 props.size:模板里的裸 `size` 会被同名顶层绑定(import)遮住 -->
    <button
      type="button"
      class="help-btn"
      :class="`help-btn-${props.size}`"
      :aria-label="label"
      :aria-expanded="open"
      @click="setOpen(!open)"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
    </button>

    <!-- 气泡 Teleport 到 body 再由 floating-ui 定位:避开滚动容器裁剪 -->
    <Teleport to="body">
      <Transition name="help-fade">
        <div
          v-if="open"
          ref="popoverRef"
          class="help-popover"
          :style="popoverStyle"
          role="tooltip"
        >
          <slot />
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.help-wrap {
  position: relative;
  display: inline-flex;
  flex-shrink: 0;
}

.help-btn {
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: color var(--transition-fast);
}

.help-btn:hover {
  color: var(--color-primary);
}

.help-btn-sm {
  width: 18px;
  height: 18px;
}

.help-btn-sm svg {
  width: 16px;
  height: 16px;
  display: block;
}

.help-btn-md {
  width: 28px;
  height: 28px;
}

.help-btn-md svg {
  width: 22px;
  height: 22px;
  display: block;
}

/* Teleport 到 body:坐标由 floating-ui 写 left/top(不占文档流,不撑出横向滚动条) */
.help-popover {
  position: fixed;
  top: 0;
  left: 0;
  /* 与 BaseSelect 下拉同档:压过弹窗 mask(1000)与抽屉(1200) */
  z-index: 3000;
  min-width: 2em;
  overflow-y: auto;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  word-break: break-word;
}

/* 插槽内容属父作用域,富文本样式需从这边穿透 */
.help-popover :deep(code) {
  font-family: var(--font-mono, monospace);
  font-size: 0.875em;
  padding: 1px 5px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.help-popover :deep(ul) {
  margin: 0;
  padding-left: var(--space-4);
}

.help-popover :deep(li) {
  margin: var(--space-1) 0;
}

.help-popover :deep(p) {
  margin: 0 0 var(--space-2);
}

.help-popover :deep(p:last-child) {
  margin-bottom: 0;
}

/* 气泡出现/消失动画(位移留给 transform,坐标归 left/top) */
.help-fade-enter-active,
.help-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.help-fade-enter-from,
.help-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
