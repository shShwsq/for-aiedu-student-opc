<script setup lang="ts">
/**
 * 外部 CLI agent 配置 内联面板(动态表单)
 *
 * 由 AgentConfigDialog 演化而来:去掉弹窗外壳(Teleport/mask/card/header/footer),
 * 保留动态凭据字段渲染 + 校验 + 启用开关 + 测试连接(SSE 流式) + 保存/清除。
 *
 * 每个 agent 类型在 CliSettingsView 中拥有独立 Panel 实例(v-show 切换),
 * 因此草稿、滚动位置、进行中的测试流跨 tab 切换都会保留,互不串台。
 *
 * 布局:顶部操作栏(状态 + 测试连接按钮) → 测试反馈区(流式/结果) → 描述 →
 * 字段网格(secret 占整行,其余并排) → 启用开关 → 底部保存/清除。
 * 测试按钮与反馈置于顶部,测试时无需滚动即可看到结果。
 *
 * 根据 meta.credential_fields 动态渲染输入框:
 * - secret 类型:password 输入框 + 眼睛切换显隐,font-mono 便于核对 token
 * - text 类型:普通明文输入框
 * - select 类型:下拉选择(如 provider_type)
 *
 * secret 字段已配置时占位提示「已设置,留空保留」;未配置时用字段定义的 placeholder。
 * 草稿在 detail 变化时重置(加载完成回显非 secret 字段、保存/清除后同步);
 * 保存/清除/测试由父组件调 API 持久化。
 *
 * 测试连接:已配置凭据时显示「测试连接」按钮,父组件调
 * POST /agents/configs/{type}/test(SSE 流式)启动临时沙箱验证 PAT 有效性,
 * 流式推送阶段进度/模型思考/模型回答,结果通过 testResult prop 回传展示。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import type {
  AgentConfigDetailOut,
  AgentTestResult,
  AgentTypeMeta,
  CredentialField,
  CredentialValue,
} from '@/types/agent_configs'

const props = defineProps<{
  /** 类型元数据(含 credential_fields 定义),null 时面板不渲染表单 */
  meta: AgentTypeMeta | null
  /** 当前配置状态(用于判断各字段是否已设置),null 表示加载中或未配置 */
  detail: AgentConfigDetailOut | null
  /** 提交/加载中状态(禁用表单 + spinner) */
  saving: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
  /** 测试连接中(禁用测试按钮 + spinner) */
  testing?: boolean
  /** 测试结果(父组件调 test API 后传入),null 表示未测试 */
  testResult?: AgentTestResult | null
  /** 当前测试阶段消息(流式,testing 时实时更新) */
  testStage?: string
  /** 模型思考过程增量累积(流式,testing 时实时更新) */
  testThinking?: string
  /** 模型回答增量累积(流式,testing 时实时更新) */
  testContent?: string
}>()

const emit = defineEmits<{
  (e: 'save', credentials: CredentialValue[], is_active: boolean): void
  (e: 'clear'): void
  /** 测试连接(父组件调 API,结果通过 testResult prop 回传) */
  (e: 'test'): void
}>()

/** 字段值草稿:key 为字段 key,value 为输入框当前值 */
const draft = reactive<{
  values: Record<string, string>
  /** 各字段的显隐状态(secret 类型用),key 为字段 key */
  show: Record<string, boolean>
  is_active: boolean
}>({
  values: {},
  show: {},
  is_active: true,
})

/** detail 变化时重置草稿(加载完成回显非 secret 字段、保存/清除后同步)
 *
 * 监听 detail:
 * - 面板首次挂载(detail 可能仍为 null)用空值/default 初始化
 * - detail 从 null 变成有值(异步加载完成)重新初始化,回显已配置的非 secret 字段
 * - detail 保存后更新 / 清除后变 null,同样重置
 * 加载期间 saving=true(含 detailLoading)使输入框 disabled,
 * 用户无法在 detail 到达前输入,不会被覆盖。
 */
watch(
  () => props.detail,
  () => {
    const fields = props.meta?.credential_fields ?? []
    const values: Record<string, string> = {}
    const show: Record<string, boolean> = {}
    // 非 secret 字段已配置值(后端回传),secret 字段不在此 dict
    const savedValues = props.detail?.credential_values ?? {}
    for (const f of fields) {
      if (f.type === 'secret') {
        // secret 字段:不回显原文,留空(已配置时 placeholder 提示"已设置,留空保留")
        values[f.key] = ''
      } else if (f.key in savedValues) {
        // 非 secret 字段:已配置则回显
        values[f.key] = savedValues[f.key]
      } else if (f.type === 'select' && f.default) {
        // select 字段未配置时用默认值
        values[f.key] = f.default
      } else {
        values[f.key] = ''
      }
      show[f.key] = false
    }
    draft.values = values
    draft.show = show
    // 启用状态:已配置时沿用当前值,未配置时默认启用
    draft.is_active = props.detail?.is_active ?? true
  },
  { immediate: true },
)

/** 是否已配置(决定底部按钮文案与是否显示「清除配置」) */
const hasCredentials = computed(
  () => !!props.detail && props.detail.has_credentials,
)

/** detail 仍未加载完(且无已配置信息):显示加载占位,避免空表单误导 */
const isLoading = computed(() => props.saving && !props.detail)

/** 指定字段是否已设置(用于 secret 字段的占位提示) */
function isFieldSet(field: CredentialField): boolean {
  return props.detail?.credential_status?.[field.key] === true
}

/** 指定字段的占位提示 */
function fieldPlaceholder(field: CredentialField): string {
  if (field.type === 'secret' && isFieldSet(field)) {
    return '已设置,留空保留'
  }
  return field.placeholder
}

/** 字段校验错误信息(空串=无错误) */
function fieldError(field: CredentialField): string {
  if (!field.required) return ''
  const val = (draft.values[field.key] ?? '').trim()
  // secret 字段已配置时允许留空(保留原值)
  if (field.type === 'secret' && isFieldSet(field)) return ''
  if (!val) return `请输入${field.label}`
  return ''
}

/**
 * 字段在当前草稿下是否可见(基于 visible_when)
 *
 * 形如 visible_when={auth_mode:'chatgpt'}:仅当 draft.values.auth_mode==='chatgpt' 时可见。
 * 不可见字段不渲染、不参与校验、不提交。
 */
function isFieldVisible(field: CredentialField): boolean {
  const cond = field.visible_when
  if (!cond) return true
  return Object.entries(cond).every(
    ([depKey, depVal]) => (draft.values[depKey] ?? '') === depVal,
  )
}

/** 当前可见的字段列表(供模板渲染与提交/校验) */
const visibleFields = computed(
  () => (props.meta?.credential_fields ?? []).filter(isFieldVisible),
)

// 认证方式等切换导致字段可见性变化时,清空已隐藏字段的草稿值,
// 避免切回另一模式时把残留输入误提交(如 chatgpt→api_key 前清掉 auth_json)。
watch(
  () =>
    (props.meta?.credential_fields ?? [])
      .map((f) => (isFieldVisible(f) ? '1' : '0'))
      .join(''),
  () => {
    for (const f of props.meta?.credential_fields ?? []) {
      if (!isFieldVisible(f)) draft.values[f.key] = ''
    }
  },
)

/** 是否所有(可见)必填字段都满足提交条件 */
const canSubmit = computed(() => {
  if (props.saving) return false
  return visibleFields.value.every((f) => !fieldError(f))
})

function handleSubmit(): void {
  if (!canSubmit.value || !props.meta) return
  // 仅提交可见字段:隐藏字段不写入,后端会清除另一模式残留密钥
  const credentials: CredentialValue[] = visibleFields.value.map((f) => ({
    key: f.key,
    value: draft.values[f.key] ?? '',
  }))
  emit('save', credentials, draft.is_active)
}

function handleClear(): void {
  if (props.saving) return
  emit('clear')
}

function handleTest(): void {
  // 测试中或保存中不允许重复触发
  if (props.testing || props.saving) return
  emit('test')
}

/** 流式日志容器 ref(测试中自动滚动到底部) */
const streamLogRef = ref<HTMLElement | null>(null)

/** thinking/content 有增量时自动滚到底部 */
watch(
  () => [props.testThinking, props.testContent, props.testStage],
  () => {
    if (!props.testing) return
    nextTick(() => {
      const el = streamLogRef.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
)

// ============================================================
// 帮助气泡:圆圈问号按钮,点击显示说明,点击外部关闭
//
// 两类帮助气泡共用一套"点击外部关闭"逻辑:
// - 顶部 agent 说明气泡(showHelp):展示 meta.description
// - 字段级帮助气泡(openHelpKey):展示 field.help_text / help_url,
//   同一时刻只展开一个字段气泡,点开新字段时旧的自动收起
// ============================================================
const showHelp = ref(false)
const helpWrapRef = ref<HTMLElement | null>(null)

/** 当前展开帮助气泡的字段 key(null=无展开) */
const openHelpKey = ref<string | null>(null)
/** 字段帮助气泡容器 DOM(按 field.key 索引,用于点击外部判断) */
const fieldHelpRefs = new Map<string, HTMLElement>()

function toggleHelp(): void {
  showHelp.value = !showHelp.value
}

/** 切换某字段帮助气泡:已展开则收起,未展开则展开(同时收起其他字段) */
function toggleFieldHelp(key: string): void {
  openHelpKey.value = openHelpKey.value === key ? null : key
}

/** 点击帮助容器外部时关闭对应气泡(顶部 agent 气泡 + 字段气泡) */
function onDocClick(e: MouseEvent): void {
  // 顶部 agent 说明气泡
  if (showHelp.value) {
    const el = helpWrapRef.value
    if (!el || !el.contains(e.target as Node)) {
      showHelp.value = false
    }
  }
  // 字段级帮助气泡
  if (openHelpKey.value) {
    const el = fieldHelpRefs.get(openHelpKey.value)
    if (!el || !el.contains(e.target as Node)) {
      openHelpKey.value = null
    }
  }
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})
</script>

<template>
  <div v-if="meta" class="agent-panel">
    <!-- 加载占位:detail 未到达时避免空表单误导 -->
    <div v-if="isLoading" class="panel-loading">
      <div class="loading-spinner" />
      <span>加载中...</span>
    </div>

    <template v-else>
      <div class="panel-body">
        <!-- 顶部操作栏:状态(左) + 帮助按钮 + 测试连接按钮(右) -->
        <div class="panel-topbar">
          <div :class="['status-row', hasCredentials ? 'status-ok' : 'status-warn']">
            <span class="status-dot" />
            <span class="status-text">
              {{ hasCredentials ? '当前已配置凭据' : '尚未配置凭据' }}
            </span>
          </div>
          <div class="topbar-actions">
            <!-- 帮助按钮:圆圈问号,点击显示说明气泡 -->
            <div ref="helpWrapRef" class="help-wrap">
              <button
                type="button"
                class="help-btn"
                aria-label="查看说明"
                @click.stop="toggleHelp"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
                  <line x1="12" y1="17" x2="12.01" y2="17" />
                </svg>
              </button>
              <Transition name="help-fade">
                <div v-if="showHelp" class="help-popover" role="tooltip">
                  {{ meta.description }}
                  <a
                    v-if="meta.help_url"
                    :href="meta.help_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="help-link"
                  >获取帮助 →</a>
                </div>
              </Transition>
            </div>
            <button
              v-if="hasCredentials"
              class="btn btn-test"
              :disabled="saving || testing"
              @click="handleTest"
            >
              <span v-if="testing" class="btn-spinner test-spinner" />
              {{ testing ? '测试中...' : '测试连接' }}
            </button>
          </div>
        </div>

        <!-- 测试反馈区(紧跟顶部,测试中/有结果时显示,无需滚动即可看到) -->
        <div
          v-if="hasCredentials && (testing || testResult || testStage || testThinking || testContent)"
          class="test-feedback"
        >
          <!-- 流式进度(测试中显示):阶段 + 思考 + 回答 -->
          <div
            v-if="testing && (testStage || testThinking || testContent)"
            ref="streamLogRef"
            class="test-stream"
          >
            <div v-if="testStage" class="stream-stage">
              <span class="stage-dot" />
              <span class="stage-text">{{ testStage }}</span>
            </div>
            <div v-if="testThinking" class="stream-block stream-thinking">
              <div class="stream-label">思考</div>
              <div class="stream-body">{{ testThinking }}</div>
            </div>
            <div v-if="testContent" class="stream-block stream-content">
              <div class="stream-label">回答</div>
              <div class="stream-body">{{ testContent }}</div>
            </div>
          </div>
          <!-- 测试结果 -->
          <div
            v-if="testResult"
            :class="['test-result', testResult.ok ? 'test-ok' : 'test-fail']"
          >
            <span class="test-icon">{{ testResult.ok ? '✓' : '✗' }}</span>
            <span class="test-message">{{ testResult.message }}</span>
          </div>
        </div>

        <!-- 动态凭据字段(grid 并排,secret 占整行) -->
        <div class="fields-grid">
          <div
            v-for="field in visibleFields"
            :key="field.key"
            :class="['field', { 'field-full': field.type === 'secret' }]"
          >
            <div class="field-head">
              <label :for="`agent-field-${field.key}`">
                {{ field.label }}
                <span v-if="field.required" class="required-mark">*</span>
              </label>
              <!-- 字段级帮助按钮:有 help_text/help_url 时显示问号,点击展开气泡 -->
              <div
                v-if="field.help_text || field.help_url"
                :ref="(el) => { if (el) fieldHelpRefs.set(field.key, el as HTMLElement); else fieldHelpRefs.delete(field.key) }"
                class="field-help-wrap"
              >
                <button
                  type="button"
                  class="field-help-btn"
                  :aria-label="`查看 ${field.label} 说明`"
                  @click.stop="toggleFieldHelp(field.key)"
                >
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <circle cx="12" cy="12" r="10" />
                    <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                </button>
                <Transition name="help-fade">
                  <div v-if="openHelpKey === field.key" class="field-help-popover" role="tooltip">
                    <span v-if="field.help_text" class="field-help-text">{{ field.help_text }}</span>
                    <a
                      v-if="field.help_url"
                      :href="field.help_url"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="field-help-link"
                    >如何获取? →</a>
                  </div>
                </Transition>
              </div>
            </div>

            <!-- secret 类型:password 输入 + 眼睛切换显隐;multiline 则用 textarea -->
            <div v-if="field.type === 'secret'" class="input-wrapper">
              <textarea
                v-if="field.multiline"
                :id="`agent-field-${field.key}`"
                v-model="draft.values[field.key]"
                class="field-textarea"
                rows="6"
                spellcheck="false"
                autocomplete="off"
                :placeholder="fieldPlaceholder(field)"
                :class="{ invalid: fieldError(field) }"
                :disabled="saving"
              />
              <template v-else>
                <input
                  :id="`agent-field-${field.key}`"
                  v-model="draft.values[field.key]"
                  :type="draft.show[field.key] ? 'text' : 'password'"
                  autocomplete="off"
                  :placeholder="fieldPlaceholder(field)"
                  :class="{ invalid: fieldError(field) }"
                  :disabled="saving"
                />
                <button
                  type="button"
                  class="toggle-pwd"
                  :aria-label="draft.show[field.key] ? '隐藏' : '显示'"
                  @click="draft.show[field.key] = !draft.show[field.key]"
                >
                  <svg v-if="!draft.show[field.key]" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                  <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                </button>
              </template>
            </div>

            <!-- text 类型:普通明文输入 -->
            <input
              v-else-if="field.type === 'text'"
              :id="`agent-field-${field.key}`"
              v-model="draft.values[field.key]"
              type="text"
              autocomplete="off"
              :placeholder="fieldPlaceholder(field)"
              :class="{ invalid: fieldError(field) }"
              :disabled="saving"
            />

            <!-- select 类型:下拉选择(如 provider_type) -->
            <select
              v-else-if="field.type === 'select'"
              :id="`agent-field-${field.key}`"
              v-model="draft.values[field.key]"
              :class="{ invalid: fieldError(field) }"
              :disabled="saving"
            >
              <option
                v-for="opt in field.options"
                :key="opt.value"
                :value="opt.value"
              >
                {{ opt.label }}
              </option>
            </select>

            <span v-if="fieldError(field)" class="field-error">{{ fieldError(field) }}</span>
          </div>
        </div>

      </div>

      <footer class="panel-footer">
        <span v-if="error" class="validation-error">{{ error }}</span>
        <span v-else></span>
        <div class="footer-actions">
          <!-- 已配置时显示「清除配置」按钮 -->
          <button
            v-if="hasCredentials"
            class="btn btn-danger"
            :disabled="saving || testing"
            @click="handleClear"
          >
            <span v-if="saving" class="btn-spinner danger" />
            清除配置
          </button>
          <!-- 启用/禁用切换(草稿状态,随保存生效) -->
          <button
            type="button"
            class="btn btn-toggle"
            :class="{ 'is-on': draft.is_active }"
            :disabled="saving || testing"
            :title="draft.is_active ? '点击禁用此执行器(保存后生效)' : '点击启用此执行器(保存后生效)'"
            @click="draft.is_active = !draft.is_active"
          >
            {{ draft.is_active ? '启用中' : '已禁用' }}
          </button>
          <button
            class="btn btn-primary"
            :disabled="!canSubmit || testing"
            @click="handleSubmit"
          >
            <span v-if="saving" class="btn-spinner" />
            {{ saving ? '处理中...' : (hasCredentials ? '更新' : '保存') }}
          </button>
        </div>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.agent-panel {
  display: flex;
  flex-direction: column;
}

/* ---- 加载占位 ---- */
.panel-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-16);
  color: var(--color-text-secondary);
}

.loading-spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: panel-spin 0.8s linear infinite;
}

@keyframes panel-spin {
  to { transform: rotate(360deg); }
}

.panel-body {
  padding: 0;
}

/* ---- 顶部操作栏:状态 + 帮助按钮 + 测试连接按钮 ---- */
.panel-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}

/* ---- 帮助按钮(圆圈问号)+ 说明气泡 ---- */
.help-wrap {
  position: relative;
  flex-shrink: 0;
}

.help-btn {
  width: 28px;
  height: 28px;
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

.help-btn svg {
  width: 22px;
  height: 22px;
  display: block;
}

.help-popover {
  position: absolute;
  top: calc(100% + var(--space-2));
  right: 0;
  z-index: 20;
  width: 320px;
  max-width: 90vw;
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.help-link {
  display: inline-block;
  margin-top: var(--space-2);
  color: var(--color-primary);
  text-decoration: none;
  font-size: var(--fs-xs);
}

.help-link:hover {
  text-decoration: underline;
}

/* 帮助气泡出现/消失动画 */
.help-fade-enter-active,
.help-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.help-fade-enter-from,
.help-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* ---- 状态行 ---- */
.status-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
}

.status-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.status-warn {
  background: #fef3c7;
  color: #92400e;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

.status-text {
  font-weight: var(--fw-medium);
}

/* ---- 字段网格(单列,一行一个) ---- */
.fields-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--space-3);
}

.field {
  display: flex;
  flex-direction: column;
}

/* secret 字段(PAT/API Key)较长,占整行 */
.field-full {
  grid-column: 1 / -1;
}

/* ---- 字段头部:标签 + 帮助按钮(问号) ---- */
.field-head {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  margin-bottom: var(--space-1);
}

.field-head label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.required-mark {
  color: var(--color-danger);
  margin-left: 2px;
}

/* secret 字段用 monospace 便于核对 token */
.field input,
.field select {
  width: 100%;
  height: 38px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  font-family: var(--font-mono);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
  font-family: var(--font-base);
}

.field input:focus,
.field select:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled,
.field select:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid,
.field select.invalid {
  border-color: var(--color-danger);
}

/* secret + multiline 的 textarea:对齐 input 观感,高度自适应、纵向可拖拽 */
.field textarea.field-textarea {
  width: 100%;
  min-height: 120px;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-family: var(--font-mono);
  line-height: 1.5;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm, 6px);
  resize: vertical;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field textarea.field-textarea::placeholder {
  color: var(--color-text-muted);
  font-family: var(--font-base);
}

.field textarea.field-textarea:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field textarea.field-textarea:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field textarea.field-textarea.invalid {
  border-color: var(--color-danger);
}

.input-wrapper {
  position: relative;
}

.input-wrapper input {
  padding-right: 40px;
}

.toggle-pwd {
  position: absolute;
  right: var(--space-2);
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  color: var(--color-text-muted);
  border: none;
  background: transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.toggle-pwd:hover {
  color: var(--color-text);
}

/* ---- 字段级帮助按钮(问号)+ 说明气泡 ---- */
.field-help-wrap {
  position: relative;
  flex-shrink: 0;
  display: inline-flex;
}

.field-help-btn {
  width: 18px;
  height: 18px;
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

.field-help-btn:hover {
  color: var(--color-primary);
}

.field-help-btn svg {
  width: 16px;
  height: 16px;
  display: block;
}

.field-help-popover {
  position: absolute;
  top: calc(100% + var(--space-1));
  left: 0;
  z-index: 20;
  width: max-content;
  min-width: 2em;
  max-width: min(300px, 80vw);
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
}

.field-help-text {
  display: block;
  word-break: break-word;
}

.field-help-link {
  display: inline-block;
  margin-top: var(--space-2);
  color: var(--color-primary);
  text-decoration: none;
  font-size: var(--fs-xs);
}

.field-help-link:hover {
  text-decoration: underline;
}

.field-error {
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- 启用/禁用切换按钮 ---- */
.btn-toggle {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
}

.btn-toggle:hover:not(:disabled) {
  color: var(--color-text-secondary);
  border-color: var(--color-border-strong);
}

.btn-toggle.is-on {
  color: var(--color-success);
  background: var(--color-success-light);
  border-color: var(--color-success);
}

.btn-toggle.is-on:hover:not(:disabled) {
  filter: brightness(0.96);
}

/* ---- footer ---- */
.panel-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-border);
}

/* ---- 测试按钮(顶部) ---- */
.btn-test {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border-strong);
  height: 32px;
  padding: 0 var(--space-3);
  flex-shrink: 0;
}

.btn-test:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.test-spinner {
  border-color: rgba(100, 116, 139, 0.3);
  border-top-color: var(--color-text-secondary);
}

/* ---- 测试反馈区(紧跟顶部) ---- */
.test-feedback {
  margin-bottom: var(--space-3);
}

.test-result {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  max-height: 300px;
  overflow-y: auto;
}

.test-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.test-fail {
  background: #fef2f2;
  color: var(--color-danger);
}

.test-icon {
  font-weight: var(--fw-bold);
  flex-shrink: 0;
}

.test-message {
  word-break: break-word;
  white-space: pre-wrap;
}

/* ---- 流式进度区 ---- */
.test-stream {
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  max-height: 160px;
  overflow-y: auto;
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

.stream-stage {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  padding-bottom: var(--space-1);
  margin-bottom: var(--space-1);
  border-bottom: 1px dashed var(--color-border);
}

.stream-stage .stage-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-primary);
  flex-shrink: 0;
  animation: stage-pulse 1.2s ease-in-out infinite;
}

@keyframes stage-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

.stream-stage .stage-text {
  font-weight: var(--fw-medium);
}

.stream-block {
  margin-top: var(--space-2);
}

.stream-block:first-of-type {
  margin-top: 0;
}

.stream-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--space-1);
}

.stream-thinking .stream-label {
  color: #7c3aed;
}

.stream-content .stream-label {
  color: var(--color-primary);
}

.stream-body {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text);
  font-family: var(--font-base);
}

.stream-thinking .stream-body {
  color: var(--color-text-secondary);
  font-style: italic;
}

.validation-error {
  font-size: var(--fs-sm);
  color: var(--color-danger);
  flex: 1;
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
}

.btn {
  height: 36px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-danger {
  background: transparent;
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger);
  color: var(--color-text-inverse);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

.btn-spinner.danger {
  border-color: rgba(220, 38, 38, 0.3);
  border-top-color: var(--color-danger);
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}
</style>
