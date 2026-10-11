<script setup lang="ts">
/**
 * 新手引导气泡组件
 *
 * 职责:
 * - 根据 currentStep 定位目标元素(data-onboarding 属性匹配),绘制半透明遮罩 + 镂空高亮
 * - 在目标元素旁渲染气泡(标题 + 正文 + 步骤计数 + 上一步/下一步/跳过按钮)
 * - 监听 resize / scroll / 步骤变化重新定位
 * - ESC 键跳过
 *
 * 不负责:
 * - 步骤队列管理(由 useOnboarding composable 负责)
 * - 文案内容(由 data/onboardingSteps.ts 提供)
 *
 * 渲染策略:用 4 块半透明 div 拼出"挖洞"遮罩(上下左右),目标元素区域镂空,
 * 镂空区域加 2px 主色边框 + 微弱外发光强调。气泡用绝对定位,根据 placement 计算。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { useOnboarding } from '@/composables/useOnboarding'
import type { OnboardingPlacement } from '@/data/onboardingSteps'

const {
  isActive,
  currentStep,
  currentIndex,
  currentSteps,
  hasNext,
  hasPrev,
  next,
  prev,
  skip,
} = useOnboarding()

/** 镂空区域(目标元素在视口中的位置),用于 4 块遮罩定位 */
const highlight = ref({ top: 0, left: 0, width: 0, height: 0 })
/** 气泡位置(绝对定位 top/left)与实际使用的 placement(可能因边界夹紧而翻转) */
const bubblePos = ref({ top: 0, left: 0 })
const bubblePlacement = ref<OnboardingPlacement>('bottom')
/** 气泡尺寸(定位时读取,用于边界夹紧计算) */
const BUBBLE_GAP = 12
const BUBBLE_MAX_WIDTH = 360
const BUBBLE_EST_HEIGHT = 200

/** 查找当前步骤的目标元素 */
function findTarget(): HTMLElement | null {
  const step = currentStep.value
  if (!step || !step.target) return null
  return document.querySelector<HTMLElement>(`[data-onboarding="${step.target}"]`)
}

/**
 * 重新计算高亮区域与气泡位置。
 * 目标元素不存在时:高亮归零,气泡居中显示(作为兜底)。
 */
async function updatePosition(): Promise<void> {
  if (!isActive.value || !currentStep.value) return
  // 等待 DOM 更新(路由切换 / 数据加载后目标元素可能还没渲染)
  await nextTick()

  const step = currentStep.value
  if (!step) return

  // 若该步骤指定了 activateTab,先通知侧栏切换标签页(目标元素可能在隐藏 panel 内)
  if (step.activateTab) {
    window.dispatchEvent(new CustomEvent('sidebar-activate-tab', { detail: step.activateTab }))
    await nextTick()
  }

  const target = findTarget()

  if (!target) {
    // 目标不在 DOM:居中显示气泡,不绘制高亮
    highlight.value = { top: 0, left: 0, width: 0, height: 0 }
    bubblePlacement.value = step.placement ?? 'bottom'
    bubblePos.value = {
      top: Math.max(80, window.innerHeight / 2 - BUBBLE_EST_HEIGHT / 2),
      left: Math.max(20, window.innerWidth / 2 - BUBBLE_MAX_WIDTH / 2),
    }
    return
  }

  // 目标存在:滚动到可视区域再测量
  target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' })
  // scrollIntoView 异步,等一帧再读 rect
  await nextTick()

  const rect = target.getBoundingClientRect()
  highlight.value = {
    top: rect.top,
    left: rect.left,
    width: rect.width,
    height: rect.height,
  }

  // 计算气泡位置
  const placement = step.placement ?? 'bottom'
  placeBubble(rect, placement)
}

/** 根据目标矩形与期望 placement 计算气泡实际位置(含边界翻转/夹紧) */
function placeBubble(rect: DOMRect, placement: OnboardingPlacement): void {
  const vw = window.innerWidth
  const vh = window.innerHeight
  // 估算气泡尺寸(实际可能因内容长短不同,边界夹紧时按估算值计算已足够避免溢出)
  const bw = Math.min(BUBBLE_MAX_WIDTH, vw - 80)
  const bh = BUBBLE_EST_HEIGHT

  let actual: OnboardingPlacement = placement
  let top = 0
  let left = 0

  // 边界翻转:若默认方向放不下,尝试翻转
  if (placement === 'bottom' && rect.bottom + BUBBLE_GAP + bh > vh) {
    actual = rect.top - BUBBLE_GAP - bh > 0 ? 'top' : 'bottom'
  } else if (placement === 'top' && rect.top - BUBBLE_GAP - bh < 0) {
    actual = rect.bottom + BUBBLE_GAP + bh < vh ? 'bottom' : 'top'
  } else if (placement === 'right' && rect.right + BUBBLE_GAP + bw > vw) {
    actual = rect.left - BUBBLE_GAP - bw > 0 ? 'left' : 'right'
  } else if (placement === 'left' && rect.left - BUBBLE_GAP - bw < 0) {
    actual = rect.right + BUBBLE_GAP + bw < vw ? 'right' : 'left'
  }

  switch (actual) {
    case 'top':
      top = rect.top - BUBBLE_GAP - bh
      left = rect.left + rect.width / 2 - bw / 2
      break
    case 'bottom':
      top = rect.bottom + BUBBLE_GAP
      left = rect.left + rect.width / 2 - bw / 2
      break
    case 'left':
      top = rect.top + rect.height / 2 - bh / 2
      left = rect.left - BUBBLE_GAP - bw
      break
    case 'right':
      top = rect.top + rect.height / 2 - bh / 2
      left = rect.right + BUBBLE_GAP
      break
  }

  // 视口夹紧(避免气泡贴边溢出)
  left = Math.max(12, Math.min(left, vw - bw - 12))
  top = Math.max(12, Math.min(top, vh - bh - 12))

  bubblePlacement.value = actual
  bubblePos.value = { top, left }
}

// ---- 事件处理 ----

function handleKeydown(e: KeyboardEvent): void {
  if (!isActive.value) return
  if (e.key === 'Escape') {
    e.preventDefault()
    skip()
  } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
    e.preventDefault()
    next()
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault()
    prev()
  }
}

function handleResize(): void {
  void updatePosition()
}

// 滚动重定位:用 capture 监听所有滚动容器(目标可能在可滚动 main 内)
function handleScroll(): void {
  void updatePosition()
}

// ---- 生命周期:激活时绑定监听,关闭时解绑 ----

watch(isActive, (active) => {
  if (active) {
    window.addEventListener('resize', handleResize, { passive: true })
    window.addEventListener('scroll', handleScroll, { capture: true, passive: true })
    window.addEventListener('keydown', handleKeydown)
    void updatePosition()
  } else {
    window.removeEventListener('resize', handleResize)
    window.removeEventListener('scroll', handleScroll, { capture: true } as EventListenerOptions)
    window.removeEventListener('keydown', handleKeydown)
  }
})

// 步骤变化时重新定位
watch([currentStep, currentIndex], () => {
  void updatePosition()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  window.removeEventListener('scroll', handleScroll, { capture: true } as EventListenerOptions)
  window.removeEventListener('keydown', handleKeydown)
})

// ---- 派生显示数据 ----

const stepNumber = computed(() => currentIndex.value + 1)
const totalSteps = computed(() => currentSteps.value.length)
</script>

<template>
  <Teleport to="body">
    <div
      v-if="isActive && currentStep"
      class="onboarding-root"
      role="dialog"
      aria-modal="true"
      :aria-label="`新手引导 第 ${stepNumber} 步,共 ${totalSteps} 步`"
    >
      <!-- 4 块遮罩拼出镂空高亮 -->
      <div class="overlay overlay-top" :style="{ height: `${highlight.top}px` }" @click="skip" />
      <div
        class="overlay overlay-bottom"
        :style="{ top: `${highlight.top + highlight.height}px` }"
        @click="skip"
      />
      <div
        class="overlay overlay-left"
        :style="{
          top: `${highlight.top}px`,
          height: `${highlight.height}px`,
          width: `${highlight.left}px`,
        }"
        @click="skip"
      />
      <div
        class="overlay overlay-right"
        :style="{
          top: `${highlight.top}px`,
          height: `${highlight.height}px`,
          left: `${highlight.left + highlight.width}px`,
        }"
        @click="skip"
      />

      <!-- 高亮边框(强调目标元素轮廓) -->
      <div
        v-if="highlight.width > 0 && highlight.height > 0"
        class="highlight-frame"
        :style="{
          top: `${highlight.top}px`,
          left: `${highlight.left}px`,
          width: `${highlight.width}px`,
          height: `${highlight.height}px`,
        }"
      />

      <!-- 气泡 -->
      <div
        class="bubble"
        :class="[`placement-${bubblePlacement}`]"
        :style="{ top: `${bubblePos.top}px`, left: `${bubblePos.left}px` }"
      >
        <div class="bubble-header">
          <span class="step-counter">{{ stepNumber }} / {{ totalSteps }}</span>
          <h3 class="bubble-title">{{ currentStep.title }}</h3>
        </div>
        <p class="bubble-content">{{ currentStep.content }}</p>
        <div class="bubble-footer">
          <button class="btn-skip" type="button" @click="skip">跳过引导</button>
          <div class="btn-group">
            <button
              v-if="hasPrev"
              class="btn-secondary"
              type="button"
              @click="prev"
            >上一步</button>
            <button
              class="btn-primary"
              type="button"
              @click="next"
            >{{ hasNext ? '下一步' : '完成' }}</button>
          </div>
        </div>
        <!-- 气泡小箭头(指向目标) -->
        <span class="bubble-arrow" :class="[`arrow-${bubblePlacement}`]" />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩:半透明黑,盖住非目标区域;点击空白处跳过引导 */
.overlay {
  position: fixed;
  background: rgba(15, 23, 42, 0.55);
  z-index: 1000;
  cursor: pointer;
}

.overlay-top {
  top: 0;
  left: 0;
  right: 0;
}

.overlay-bottom {
  bottom: 0;
  left: 0;
  right: 0;
}

.overlay-left {
  left: 0;
}

.overlay-right {
  right: 0;
}

/* 目标元素轮廓高亮:2px 主色边框 + 微弱外发光 */
.highlight-frame {
  position: fixed;
  border: 2px solid var(--color-primary);
  border-radius: var(--radius-md);
  box-shadow: 0 0 0 4px rgba(79, 70, 229, 0.18), 0 0 12px rgba(79, 70, 229, 0.3);
  pointer-events: none;
  z-index: 1001;
  transition: all 0.18s ease;
}

/* 气泡:绝对定位,固定宽度,圆角 + 阴影,主色顶栏强调 */
.bubble {
  position: fixed;
  width: 360px;
  max-width: calc(100vw - 24px);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xl);
  z-index: 1002;
  animation: bubble-in 0.2s ease;
  font-family: var(--font-sans);
}

@keyframes bubble-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.bubble-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-5) var(--space-2);
  border-bottom: 1px solid var(--color-border);
}

.step-counter {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-full);
  padding: 2px var(--space-2);
  letter-spacing: 0.02em;
  flex-shrink: 0;
}

.bubble-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  margin: 0;
}

.bubble-content {
  padding: var(--space-3) var(--space-5) var(--space-4);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  margin: 0;
}

.bubble-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5) var(--space-4);
}

.btn-skip {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  transition: color var(--transition-fast), background var(--transition-fast);
}

.btn-skip:hover {
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
}

.btn-group {
  display: flex;
  gap: var(--space-2);
}

.btn-secondary,
.btn-primary {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.btn-secondary {
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-color: var(--color-border);
}

.btn-secondary:hover {
  color: var(--color-text);
  background: var(--color-surface);
  border-color: var(--color-border-strong);
}

.btn-primary {
  color: var(--color-text-inverse);
  background: var(--color-primary);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

/* 气泡小箭头:用 border 三角形指向目标元素 */
.bubble-arrow {
  position: absolute;
  width: 0;
  height: 0;
  border: 8px solid transparent;
}

.arrow-top {
  bottom: -16px;
  left: 50%;
  transform: translateX(-50%);
  border-top-color: var(--color-surface);
}

.arrow-bottom {
  top: -16px;
  left: 50%;
  transform: translateX(-50%);
  border-bottom-color: var(--color-surface);
}

.arrow-left {
  right: -16px;
  top: 50%;
  transform: translateY(-50%);
  border-left-color: var(--color-surface);
}

.arrow-right {
  left: -16px;
  top: 50%;
  transform: translateY(-50%);
  border-right-color: var(--color-surface);
}
</style>
