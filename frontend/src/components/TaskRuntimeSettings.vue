<script setup lang="ts">
/**
 * 任务运行时设置面板(消息输入框上方,箭头折叠)
 *
 * 允许在任务进行中修改:
 * - agent1 模型(仅 executor=builtin;CLI 执行器模型自管)
 * - agent2 评估模型
 *
 * 生效时机:running/paused 的当前执行线程仍用启动时加载的配置,
 * 修改在下一轮执行(completed 后追加消息 / failed 重试)时生效;
 * 保存成功且任务运行中/暂停中时弹 toast 提示用户。
 *
 * 保存模式:改动即保存(与详情页动态验证开关一致),成功后 emit('saved')
 * 由父组件回填 task,保证与后端一致。
 */
import { computed, onMounted, ref } from 'vue'

import BaseSelect from '@/components/BaseSelect.vue'
import { getMyModels } from '@/api/model_configs'
import { getModelSlots } from '@/api/model_slots'
import { updateTaskRuntimeConfig } from '@/api/task'
import { extractErrorMessage } from '@/utils/error'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { TaskDetail } from '@/types/task'

const props = defineProps<{
  task: TaskDetail
}>()

const emit = defineEmits<{
  /** 保存成功,回传后端最新任务快照 */
  saved: [task: TaskDetail]
  /** 保存失败(展示错误提示用) */
  error: [message: string]
}>()

// ---- 面板状态 ----
const expanded = ref(false)
const saving = ref(false)

/** 用户已保存的 LLM 配置列表(为空表示未配置,展示引导文案) */
const llmConfigs = ref<LLMConfigItemOut[]>([])
const loadingModels = ref(true)
/** 用户级「任务默认审查模型」展示名(未设则空,此时“默认”就是环境变量) */
const reviewDefaultLabel = ref('')

/** 任务是否使用内置执行器(CLI 执行器 react 模型自管,选择器禁用) */
const isBuiltin = computed(() => (props.task.executor ?? 'builtin') === 'builtin')

/** 模型下拉展示文案 */
function modelLabel(cfg: LLMConfigItemOut): string {
  const name = cfg.name || cfg.model
  return cfg.has_api_key ? name : `${name}(未配置 Key)`
}

/** agent2 评估模型选项(空=不指定本任务,用用户默认审查模型;没设则环境变量) */
const llmConfigOptions = computed(() => [
  {
    value: '',
    label: reviewDefaultLabel.value
      ? `默认模型（${reviewDefaultLabel.value}）`
      : '默认模型(环境变量)',
  },
  ...llmConfigs.value.map((cfg) => ({ value: cfg.id, label: modelLabel(cfg) })),
])

/** agent1 模型选项(空=同评估模型) */
const reactLlmConfigOptions = computed(() => [
  { value: '', label: '同评估模型' },
  ...llmConfigs.value.map((cfg) => ({ value: cfg.id, label: modelLabel(cfg) })),
])

/** 初始化:拉模型列表与默认审查模型(后者失败不影响面板可用) */
async function init(): Promise<void> {
  const [models, slots] = await Promise.all([
    getMyModels().catch(() => null),
    getModelSlots().catch(() => null),
  ])
  llmConfigs.value = models?.llm_configs ?? []
  const review = slots?.slots.find((s) => s.slot === 'review_default')
  // 只在确实命中已存配置时展示名字:配置被删时后端回退环境默认,不能谎报
  reviewDefaultLabel.value =
    review && review.source === 'slot' ? review.config_label : ''
  loadingModels.value = false
}

onMounted(() => {
  void init()
})

// ---- 顶部居中 toast(保存反馈;运行中/暂停中额外提示下一轮生效) ----
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toast.value = null
  }, 4000)
}

/**
 * 提交部分字段更新
 *
 * 成功后回填 task(父组件)+ toast 反馈:
 * - running / paused:提示"下一轮执行生效"(当前线程用启动时配置)
 * - completed / failed:下次追加消息/重试即生效,仅提示已保存
 */
async function save(req: {
  llm_config_id?: string
  react_llm_config_id?: string
}): Promise<void> {
  if (saving.value) return
  saving.value = true
  try {
    const updated = await updateTaskRuntimeConfig(String(props.task.id), req)
    emit('saved', updated)
    const deferred = props.task.status === 'running' || props.task.status === 'paused'
    showToast(deferred ? '已保存,将在下一轮执行时生效' : '已保存', 'success')
  } catch (err) {
    const msg = extractErrorMessage(err)
    showToast(msg, 'error')
    emit('error', msg)
  } finally {
    saving.value = false
  }
}

function onUserModelChange(value: string | number): void {
  void save({ llm_config_id: String(value) })
}

function onReactModelChange(value: string | number): void {
  void save({ react_llm_config_id: String(value) })
}
</script>

<template>
  <div class="runtime-settings">
    <!-- 展开面板(位于折叠条上方,箭头收起时隐藏) -->
    <Transition name="rs-panel">
      <div v-if="expanded" class="rs-panel">
        <!-- agent1 模型 -->
        <div class="rs-field">
          <span class="rs-label">AI助手模型</span>
          <BaseSelect
            v-if="isBuiltin"
            :model-value="task.react_llm_config_id ?? ''"
            :options="reactLlmConfigOptions"
            :disabled="loadingModels"
            size="sm"
            aria-label="AI助手模型"
            @change="onReactModelChange"
          />
          <p v-else class="rs-hint">当前使用 CLI 执行器,执行模型由 CLI 自管</p>
        </div>

        <!-- agent2 评估模型 -->
        <div class="rs-field">
          <span class="rs-label">检查助手模型</span>
          <BaseSelect
            :model-value="task.llm_config_id ?? ''"
            :options="llmConfigOptions"
            :disabled="loadingModels"
            size="sm"
            aria-label="检查助手模型"
            @change="onUserModelChange"
          />
          <p v-if="!loadingModels && llmConfigs.length === 0" class="rs-hint">
            暂无可选模型,请到「模型设置」配置 LLM 凭据
          </p>
        </div>

        <p class="rs-note">
          修改即保存;任务运行中/暂停中修改,将在下一轮执行(完成后追加消息 / 失败重试)时生效
        </p>
      </div>
    </Transition>

    <!-- 折叠条:向上箭头,点击展开/收起 -->
    <button
      type="button"
      class="rs-toggle"
      :aria-expanded="expanded"
      :title="expanded ? '收起运行时设置' : '展开运行时设置(模型配置)'"
      @click="expanded = !expanded"
    >
      <svg
        class="rs-chevron"
        :class="{ 'is-open': expanded }"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <polyline points="18 15 12 9 6 15" />
      </svg>
      <span>运行时设置</span>
      <span v-if="saving" class="rs-spinner" aria-label="保存中" />
    </button>

    <!-- 保存反馈 toast -->
    <Teleport to="body">
      <Transition name="rs-toast">
        <div
          v-if="toast"
          :class="['rs-toast', toast.type === 'error' ? 'rs-toast-error' : 'rs-toast-success']"
          role="status"
          aria-live="polite"
        >
          {{ toast.msg }}
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.runtime-settings {
  width: 94%;
  margin: 0 auto var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

/* ---- 折叠条 ---- */
.rs-toggle {
  align-self: center;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: 2px var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast);
}

.rs-toggle:hover {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.rs-chevron {
  transition: transform var(--transition-fast);
}

.rs-chevron.is-open {
  transform: rotate(180deg);
}

.rs-spinner {
  width: 10px;
  height: 10px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: rs-spin 0.8s linear infinite;
}

@keyframes rs-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 展开面板 ---- */
.rs-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
}

.rs-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.rs-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-muted);
}

.rs-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ---- 说明文案 ---- */
.rs-note {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: 1.5;
}

/* ---- 面板展开动画 ---- */
.rs-panel-enter-active,
.rs-panel-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.rs-panel-enter-from,
.rs-panel-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

/* ---- toast ---- */
.rs-toast {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 4000;
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.rs-toast-success {
  color: var(--color-success, #16a34a);
  background: var(--color-surface);
  border: 1px solid var(--color-success, #16a34a);
}

.rs-toast-error {
  color: var(--color-danger);
  background: var(--color-surface);
  border: 1px solid var(--color-danger);
}

.rs-toast-enter-active,
.rs-toast-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.rs-toast-enter-from,
.rs-toast-leave-to {
  opacity: 0;
  transform: translate(-50%, -8px);
}
</style>
