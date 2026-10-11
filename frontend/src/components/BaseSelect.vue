<script setup lang="ts">
/**
 * 通用自定义下拉选择框
 *
 * 替代原生 <select>,提供与 ModelCombobox 一致的可定制外观:
 * - 触发器为 button,显示当前选中项 label
 * - 下拉列表 Teleport 到 body,由 @floating-ui/dom 定位,避免被弹窗裁剪
 * - 选项灰色高亮,键盘导航(↑/↓/Enter/Esc)
 * - 点击外部自动关闭
 *
 * size:
 * - 'md'(默认,36px):对齐表单主下拉
 * - 'sm'(34px):对齐紧凑面板(如 Qoder 配置行)
 *
 * 命名坑:floating-ui 的 size 中间件与 size prop 同名时,<script setup> 的顶层
 * import 绑定会遮蔽同名 prop,模板里读到的 `size` 是那个中间件函数(类名拼成
 * is-function(...) ,sm 变体失效)。故 import 起别名,模板里读 prop 写 props.size。
 */
import {
  autoUpdate,
  computePosition,
  flip,
  offset,
  size as sizeMiddleware,
} from '@floating-ui/dom'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

export interface SelectOption {
  /** 实际值(支持 string / number,兼容 v-model.number) */
  value: string | number
  /** 展示文本;不提供时展示 value */
  label?: string
}

const props = withDefaults(
  defineProps<{
    modelValue: string | number
    options: SelectOption[]
    placeholder?: string
    disabled?: boolean
    ariaLabel?: string
    size?: 'md' | 'sm'
  }>(),
  {
    placeholder: '请选择',
    disabled: false,
    ariaLabel: undefined,
    size: 'md',
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | number): void
  (e: 'change', value: string | number): void
}>()

const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
const open = ref(false)
const highlightIndex = ref(-1)

const selectedOption = computed(() =>
  props.options.find((o) => o.value === props.modelValue),
)

const displayLabel = computed(
  () => selectedOption.value?.label ?? selectedOption.value?.value ?? props.placeholder,
)

function openList(): void {
  if (props.disabled) return
  open.value = true
  const idx = props.options.findIndex((o) => o.value === props.modelValue)
  highlightIndex.value = idx >= 0 ? idx : props.options.length > 0 ? 0 : -1
}

function closeList(): void {
  open.value = false
  highlightIndex.value = -1
}

function selectOption(opt: SelectOption): void {
  emit('update:modelValue', opt.value)
  emit('change', opt.value)
  closeList()
  nextTick(() => triggerRef.value?.focus())
}

function onKeydown(e: KeyboardEvent): void {
  if (props.disabled) return
  switch (e.key) {
    case 'ArrowDown': {
      e.preventDefault()
      if (!open.value) {
        openList()
        return
      }
      if (props.options.length === 0) return
      highlightIndex.value = (highlightIndex.value + 1) % props.options.length
      scrollIntoView()
      break
    }
    case 'ArrowUp': {
      e.preventDefault()
      if (!open.value) {
        openList()
        return
      }
      if (props.options.length === 0) return
      highlightIndex.value =
        (highlightIndex.value - 1 + props.options.length) % props.options.length
      scrollIntoView()
      break
    }
    case 'Enter': {
      if (!open.value) return
      e.preventDefault()
      const opt = props.options[highlightIndex.value]
      if (opt) selectOption(opt)
      else closeList()
      break
    }
    case 'Escape': {
      if (open.value) {
        e.preventDefault()
        closeList()
      }
      break
    }
    case 'Tab': {
      if (open.value) closeList()
      break
    }
  }
}

function scrollIntoView(): void {
  nextTick(() => {
    const list = listRef.value
    const el = list?.querySelector<HTMLElement>(`[data-idx="${highlightIndex.value}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  })
}

// 点击外部关闭
function onDocumentClick(e: MouseEvent): void {
  const t = e.target as Node
  if (rootRef.value?.contains(t)) return
  if (listRef.value?.contains(t)) return
  closeList()
}

// ---- floating-ui 定位 ----
let cleanupFloating: (() => void) | null = null

function setupFloating(): void {
  const anchor = triggerRef.value
  const floating = listRef.value
  if (!anchor || !floating) return
  updatePosition(anchor, floating)
  cleanupFloating?.()
  cleanupFloating = autoUpdate(anchor, floating, () => updatePosition(anchor, floating))
}

function updatePosition(anchor: HTMLElement, floating: HTMLElement): void {
  computePosition(anchor, floating, {
    placement: 'bottom-start',
    // 下拉列表是 position: fixed(相对视口),默认 absolute 策略给的是文档坐标,
    // 窗口滚动后会整体下移一个 scrollY
    strategy: 'fixed',
    middleware: [
      offset(4),
      flip({ padding: 8 }),
      sizeMiddleware({
        apply({ rects, elements }) {
          Object.assign(elements.floating.style, {
            minWidth: `${rects.reference.width}px`,
            maxWidth: `${Math.max(rects.reference.width, 320)}px`,
          })
        },
        padding: 8,
      }),
    ],
  }).then(({ x, y }) => {
    Object.assign(floating.style, {
      left: `${x}px`,
      top: `${y}px`,
    })
  })
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('mousedown', onDocumentClick)
    nextTick(() => requestAnimationFrame(setupFloating))
  } else {
    document.removeEventListener('mousedown', onDocumentClick)
    cleanupFloating?.()
    cleanupFloating = null
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentClick)
  cleanupFloating?.()
})
</script>

<template>
  <div
    ref="rootRef"
    class="base-select"
    :class="[`is-${props.size}`, { 'is-disabled': disabled }]"
  >
    <button
      ref="triggerRef"
      type="button"
      class="base-select-trigger"
      :disabled="disabled"
      :aria-label="ariaLabel"
      @click="open ? closeList() : openList()"
      @keydown="onKeydown"
    >
      <span class="base-select-label" :class="{ 'is-placeholder': !selectedOption }">
        {{ displayLabel }}
      </span>
      <svg
        class="base-select-arrow"
        :class="{ 'is-open': open }"
        viewBox="0 0 16 16"
        width="12"
        height="12"
        aria-hidden="true"
      >
        <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>

    <!-- 下拉列表 Teleport 到 body,由 floating-ui 定位,避免被弹窗裁剪 -->
    <Teleport to="body">
      <ul
        v-if="open && options.length > 0"
        ref="listRef"
        class="base-select-list"
        role="listbox"
      >
        <li
          v-for="(opt, idx) in options"
          :key="opt.value"
          :data-idx="idx"
          class="base-select-option"
          :class="{
            'is-highlighted': idx === highlightIndex,
            'is-selected': opt.value === modelValue,
          }"
          role="option"
          :aria-selected="opt.value === modelValue"
          @mousedown.prevent="selectOption(opt)"
          @mouseenter="highlightIndex = idx"
        >
          <span class="base-select-option-label">{{ opt.label ?? opt.value }}</span>
        </li>
      </ul>
    </Teleport>
  </div>
</template>

<style scoped>
.base-select {
  position: relative;
  display: inline-flex;
}

/* ---- 触发器 ---- */
.base-select-trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  width: 100%;
  /* 用上下内边距撑开高度,替代固定 height,让触发器有垂直呼吸空间 */
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  text-align: left;
}

.is-sm .base-select-trigger {
  padding: var(--space-1) var(--space-2);
}

.base-select-trigger:hover:not(:disabled) {
  border-color: var(--color-primary);
}

.base-select-trigger:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.base-select.is-disabled .base-select-trigger {
  background: var(--color-surface-alt);
  cursor: not-allowed;
  opacity: 0.7;
}

.base-select-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.base-select-label.is-placeholder {
  color: var(--color-text-muted);
}

.base-select-arrow {
  flex-shrink: 0;
  transition: transform var(--transition-fast);
}

.base-select-arrow.is-open {
  transform: rotate(180deg);
}

/* ---- 下拉列表(Teleport 到 body) ---- */
.base-select-list {
  position: fixed;
  top: 0;
  left: 0;
  z-index: 3000;
  margin: 0;
  /* 不留外边距:让高亮块紧贴列表边框 */
  padding: 0;
  list-style: none;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  max-height: 200px;
  overflow-y: auto;
}

.base-select-option {
  display: flex;
  flex-direction: column;
  gap: 2px;
  /* 文字保持左右内边距可读;高亮背景由 padding 外侧填满,紧贴列表边框 */
  padding: var(--space-2) var(--space-3);
  border-radius: 0;
  cursor: pointer;
  transition: background var(--transition-fast);
}

.base-select-option.is-highlighted {
  background: var(--color-surface-alt);
}

.base-select-option.is-selected {
  color: var(--color-primary);
  font-weight: var(--fw-medium);
}

.base-select-option-label {
  font-size: var(--fs-sm);
  line-height: var(--lh-tight);
}
</style>
