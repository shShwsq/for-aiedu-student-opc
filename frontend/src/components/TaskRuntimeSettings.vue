<script setup lang="ts">
/**
 * 任务运行时设置:执行器 + 对话使用的模型(检查助手 + 内置 AI助手 / CLI 模型参数)
 *
 * 两种形态由 variant 选(同一个组件,选项/解析口径/保存链路全共用):
 * - bar:消息输入框上方,箭头折叠(默认)
 * - panel:右侧栏「概览」段内嵌区块,无折叠条、常显
 *
 * 允许在任务进行中修改:
 * - agent1 模型(仅 executor=builtin;CLI 执行器模型自管)
 * - agent2 评估模型
 * - CLI 执行器的模型参数(model/reasoning_effort/context_window,
 *   仅 qoder_cli / deepseek_cli;写入 task.params)
 *
 * 生效时机:running/paused 的当前执行线程仍用启动时加载的配置,
 * 修改在下一轮执行(completed 后追加消息 / failed 重试)时生效;
 * 保存成功且任务运行中/暂停中时弹 toast 提示用户。
 *
 * 保存模式:改动即保存(与详情页动态验证开关一致),成功后 emit('saved')
 * 由父组件回填 task,保证与后端一致(两个入口因此不会显示不同的值)。
 */
import { computed, onMounted, ref } from 'vue'

import BaseSelect from '@/components/BaseSelect.vue'
import { updateTaskRuntimeConfig } from '@/api/task'
import {
  contextOptionsFor,
  effortOptionsFor,
  modelOptionsFor,
  supportsModelConfig,
} from '@/data/cliModelOptions'
import { agentTypes, ensureAgentTypesLoaded } from '@/composables/useAgentTypes'
import { useTaskRuntimeModels } from '@/composables/useTaskRuntimeModels'
import { extractErrorMessage } from '@/utils/error'
import {
  resolveAgentModel,
  resolveReviewModel,
  taskModelDisplayText,
} from '@/utils/taskModels'
import type { TaskDetail } from '@/types/task'

const props = withDefaults(
  defineProps<{
    task: TaskDetail
    /** bar=输入框上方折叠条;panel=侧栏「概览」段内嵌区块 */
    variant?: 'bar' | 'panel'
  }>(),
  { variant: 'bar' },
)

const emit = defineEmits<{
  /** 保存成功,回传后端最新任务快照 */
  saved: [task: TaskDetail]
  /** 保存失败(展示错误提示用) */
  error: [message: string]
}>()

// ---- 面板状态 ----
const expanded = ref(false)
const saving = ref(false)

/** 内嵌态:没有折叠条,面板常显 */
const isEmbedded = computed(() => props.variant === 'panel')

/** 模型来源(配置清单 + 任务默认审查模型槽位),两个入口并发去重共用一次拉取 */
const {
  loading: loadingModels,
  ready: modelsReady,
  sources,
  reviewOptions,
  reactOptions,
  ensureLoaded,
} = useTaskRuntimeModels()

/** 任务是否使用内置执行器(CLI 执行器 react 模型自管,选择器禁用) */
const isBuiltin = computed(() => (props.task.executor ?? 'builtin') === 'builtin')

/** 当前执行器是否为支持任务级模型参数的 CLI(qoder_cli / deepseek_cli) */
const isCliWithModelConfig = computed(() => supportsModelConfig(props.task.executor))

/**
 * 执行器展示名:内置固定「内置」,CLI 取后端注册表 display_name
 *
 * display_name 来自 GET /agents/types(模块级缓存,与设置页共用);未加载完时
 * 先回退到原始 executor 字符串(不空着,免得概览少一行看不出在用哪个执行器)。
 */
const executorLabel = computed(() => {
  const ex = props.task.executor ?? 'builtin'
  if (ex === 'builtin') return '内置'
  return agentTypes.value.find((t) => t.agent_type === ex)?.display_name ?? ex
})

/** 当前任务已保存的 CLI 模型参数(task.params;空串/0 表示用 CLI 默认) */
const cliParams = computed(() => {
  const p = props.task.params ?? {}
  return {
    model: typeof p.model === 'string' ? p.model : '',
    reasoningEffort: typeof p.reasoning_effort === 'string' ? p.reasoning_effort : '',
    contextWindow: typeof p.context_window === 'number' ? p.context_window : 0,
  }
})

const cliModelOptions = computed(() => modelOptionsFor(props.task.executor))
const cliEffortOptions = computed(() => effortOptionsFor(props.task.executor))
const cliContextOptions = computed(() => contextOptionsFor(props.task.executor))

/** 解析输入:任务上的两个模型 id + 执行器 */
const modelIds = computed(() => ({
  reviewId: props.task.llm_config_id,
  reactId: props.task.react_llm_config_id,
  executor: props.task.executor,
}))

// 实际生效的模型:留空 / 原选配置被删 / 走默认槽位时,只看选择框看不出用的哪个
const agentModel = computed(() => resolveAgentModel(modelIds.value, sources.value))
const reviewModel = computed(() => resolveReviewModel(modelIds.value, sources.value))
const agentModelText = computed(() => taskModelDisplayText(agentModel.value))
const reviewModelText = computed(() => taskModelDisplayText(reviewModel.value))

/**
 * 「实际使用」行只在数据到位、且任务没直接指定模型时出现:
 * - 未 ready(清单/槽位还在拉,或拉挂了)时什么都不说:那时只会把「原选配置已删除」
 *   报给一条还没拉到的配置,跨账号时甚至会先报上一个登录者的默认模型名
 * - 任务直接指定过模型时选择框已经写着名字,再复述一遍是噪音
 */
const showAgentEffective = computed(
  () => modelsReady.value && isBuiltin.value && agentModel.value.source !== 'task',
)
const showReviewEffective = computed(
  () => modelsReady.value && reviewModel.value.source !== 'task',
)

/**
 * 选择框占位文案:把「还没拉到」与「拉到了但那条已被删」分开说
 *
 * BaseSelect 的 model-value 不在 options 里时回落到 placeholder,所以不能一律拿
 * 「请选择」去占:加载期那是“还没知道”,加载完后的空缺才是“原选被删”——
 * 后者用「请选择」说会让用户以为选错了,而不是配置不在了。
 * (不把已删 id 注进 options 当一项展示:选中它就会 PATCH 一个不存在的 id,后端拒 400)
 */
function selectPlaceholder(id?: string | null): string {
  if (!modelsReady.value) return '模型信息加载中…'
  if (id && !sources.value.configs.some((c) => c.id === id)) return '原选配置已删除'
  return '请选择'
}

onMounted(() => {
  void ensureLoaded()
  // 执行器展示名需要后端注册表(模块级缓存,多处共用只发一次)
  void ensureAgentTypesLoaded().catch(() => undefined)
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
  model?: string
  reasoning_effort?: string
  context_window?: number
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

// ---- CLI 执行器模型参数改动即保存(写入 task.params) ----
function onCliModelChange(value: string | number): void {
  void save({ model: String(value) })
}
function onCliEffortChange(value: string | number): void {
  void save({ reasoning_effort: String(value) })
}
function onCliContextChange(value: string | number): void {
  void save({ context_window: Number(value) })
}
</script>

<template>
  <div :class="['runtime-settings', { 'is-embedded': isEmbedded }]">
    <!-- 展开面板(bar 位于折叠条上方,箭头收起时隐藏;内嵌态常显) -->
    <Transition name="rs-panel">
      <div v-if="expanded || isEmbedded" class="rs-panel">
        <!-- 内嵌态自己不带段名(面板标题行已拿段名),这里只是区块小标题 -->
        <h2 v-if="isEmbedded" class="rs-section-title">
          对话模型
          <span v-if="saving" class="rs-saving">保存中…</span>
        </h2>

        <!-- 未就绪时只说状态,不给结论(选择框此时同时禁用,避免拿着不完整的清单就保存) -->
        <p v-if="!modelsReady" class="rs-effective">
          {{
            loadingModels
              ? '模型信息加载中…'
              : '模型清单未加载成功,刷新页面重试'
          }}
        </p>

        <!-- 执行器(当前任务用哪个 agent1 执行;详情展示,不可在运行态切换) -->
        <div class="rs-field rs-executor-field">
          <p class="rs-executor-name" :title="executorLabel">{{ executorLabel }}</p>
        </div>

        <!-- agent1 模型 -->
        <div v-if="isBuiltin" class="rs-field">
          <span class="rs-label">AI助手模型</span>
          <BaseSelect
            :model-value="task.react_llm_config_id ?? ''"
            :options="reactOptions"
            :placeholder="selectPlaceholder(task.react_llm_config_id)"
            :disabled="!modelsReady"
            size="sm"
            aria-label="AI助手模型"
            @change="onReactModelChange"
          />
          <p v-if="showAgentEffective" class="rs-effective" :title="agentModelText">
            实际使用：{{ agentModelText }}
          </p>
        </div>

        <!-- CLI 执行器模型参数(仅 qoder_cli / deepseek_cli 可改;写入 task.params) -->
        <template v-else-if="isCliWithModelConfig">
          <div class="rs-field">
            <span class="rs-label">AI助手模型</span>
            <BaseSelect
              :model-value="cliParams.model"
              :options="cliModelOptions"
              size="sm"
              aria-label="CLI 模型"
              @change="onCliModelChange"
            />
          </div>
          <div class="rs-field">
            <span class="rs-label">思考强度</span>
            <BaseSelect
              :model-value="cliParams.reasoningEffort"
              :options="cliEffortOptions"
              size="sm"
              aria-label="CLI 思考强度"
              @change="onCliEffortChange"
            />
          </div>
          <div v-if="cliContextOptions.length > 0" class="rs-field">
            <span class="rs-label">上下文窗口</span>
            <BaseSelect
              :model-value="cliParams.contextWindow"
              :options="cliContextOptions"
              size="sm"
              aria-label="CLI 上下文窗口"
              @change="onCliContextChange"
            />
          </div>
        </template>

        <!-- 其余 CLI(如 codex_cli)模型经自身配置自管,此处不可改 -->
        <div v-else class="rs-field">
          <span class="rs-label">AI助手模型</span>
          <p class="rs-hint">当前使用 CLI 执行器,执行模型由 CLI 自管</p>
        </div>

        <!-- agent2 评估模型 -->
        <div class="rs-field">
          <span class="rs-label">检查助手模型</span>
          <BaseSelect
            :model-value="task.llm_config_id ?? ''"
            :options="reviewOptions"
            :placeholder="selectPlaceholder(task.llm_config_id)"
            :disabled="!modelsReady"
            size="sm"
            aria-label="检查助手模型"
            @change="onUserModelChange"
          />
          <p v-if="showReviewEffective" class="rs-effective" :title="reviewModelText">
            实际使用：{{ reviewModelText }}
          </p>
          <p v-if="modelsReady && sources.configs.length === 0" class="rs-hint">
            暂无可选模型,请到「模型设置」配置 LLM 凭据
          </p>
        </div>

        <p class="rs-note">
          修改即保存;任务运行中/暂停中修改,将在下一轮执行(完成后追加消息 / 失败重试)时生效
        </p>
      </div>
    </Transition>

    <!-- 折叠条:向上箭头,点击展开/收起(仅 bar 形态;内嵌态没有折叠这一说) -->
    <button
      v-if="!isEmbedded"
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

/* 内嵌态(侧栏「概览」段):撑满栏宽,不居中不留底部外边距 */
.runtime-settings.is-embedded {
  width: 100%;
  margin: 0;
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

/* 内嵌态不是浮层:去掉卡片包装,跟同栏其他区块一致 */
.runtime-settings.is-embedded .rs-panel {
  gap: var(--space-2);
  padding: 0;
  background: transparent;
  border: none;
  border-radius: 0;
  box-shadow: none;
}

/* 段内小标题 14px/600:不得大过面板标题行的段名(侧栏字号阶梯,见 docs/task-detail-view-structure.md) */
.rs-section-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.rs-saving {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-tertiary);
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

/* 执行器名:去掉「执行器」小标题后由它自己承担展示,加粗放大一档作视觉锚点 */
.rs-executor-name {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-bold);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rs-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* 「实际使用」行:选择框里是空值时补一句真正落到的模型(窄栏单行截断,全文看 title) */
.rs-effective {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
