<script setup lang="ts">
/**
 * 智能体策略设置面板(嵌套在 SettingsLayout 内)
 *
 * 作为 agent2(检查助手)行为与执行命令策略的用户级默认配置:
 * - agent2 启停
 * - 验证权限:agent2 是否能自行调用工具验证(实验性)
 * - 验证授权模式:验证动作的默认授权模式(直接执行 / 逐动作授权)
 * - AI助手确认策略
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import BaseSelect from '@/components/BaseSelect.vue'
import { getPreferences, saveAgentPolicy } from '@/api/memory'
import { useUnsavedGuard } from '@/composables/useUnsavedGuard'
import { extractErrorMessage } from '@/utils/error'
import type { SaveAgentPolicyRequest } from '@/types/memory'

// ============================================================
// 默认策略值(与后端 DEFAULT_AGENT_POLICY 对齐)
// ============================================================

const DEFAULT_POLICY = {
  agent2_enabled: true,
  allow_verify: false,
  allow_reference_check: true,
  verifier_auth_mode_default: 'per_action' as 'direct' | 'per_action',
  executor_command_confirm_default: 'always_approve' as 'always_approve' | 'per_command',
}

// ============================================================
// 表单状态
// ============================================================
/** 是否启用 agent2(关闭=单 agent 模式,跳过评估/验证) */
const policyAgent2Enabled = ref(DEFAULT_POLICY.agent2_enabled)
/** agent2 是否能自己验证(实验性) */
const policyAllowVerify = ref(DEFAULT_POLICY.allow_verify)
/** agent2 是否能复核 AI助手引用的网址(后端安全抓取,结果仅供参考信号) */
const policyAllowReference = ref(DEFAULT_POLICY.allow_reference_check)
/** 验证授权默认模式(任务级可覆盖) */
const policyVerifierAuthMode = ref<'direct' | 'per_action'>(DEFAULT_POLICY.verifier_auth_mode_default)
/** AI助手确认策略默认模式(任务级 _executor_command_confirm 可覆盖) */
const policyExecutorCommandConfirm = ref<'always_approve' | 'per_command'>(DEFAULT_POLICY.executor_command_confirm_default)

/** 验证授权模式选项(对齐 BaseSelect {value,label} 结构) */
const verifierAuthModeOptions = computed(() => [
  { value: 'per_action' as 'direct' | 'per_action', label: '逐动作授权(每个动作弹窗确认)' },
  { value: 'direct' as 'direct' | 'per_action', label: '直接执行(不弹窗)' },
])

/** AI助手确认策略选项 */
const executorConfirmOptions = computed(() => [
  { value: 'always_approve' as 'always_approve' | 'per_command', label: '自动批准(不弹窗,注入 YOLO 模式)' },
  { value: 'per_command' as 'always_approve' | 'per_command', label: '逐命令确认(危险命令弹窗批准)' },
])

/** 策略原始值(脏检查基准,hydrate 时写入) */
const originalPolicy = ref({
  agent2Enabled: DEFAULT_POLICY.agent2_enabled,
  allowVerify: DEFAULT_POLICY.allow_verify,
  allowReference: DEFAULT_POLICY.allow_reference_check,
  verifierAuthMode: DEFAULT_POLICY.verifier_auth_mode_default,
  executorCommandConfirm: DEFAULT_POLICY.executor_command_confirm_default,
})

/** agent 策略是否有未保存改动 */
const policyDirty = computed(() => {
  return (
    policyAgent2Enabled.value !== originalPolicy.value.agent2Enabled ||
    policyAllowVerify.value !== originalPolicy.value.allowVerify ||
    policyAllowReference.value !== originalPolicy.value.allowReference ||
    policyVerifierAuthMode.value !== originalPolicy.value.verifierAuthMode ||
    policyExecutorCommandConfirm.value !== originalPolicy.value.executorCommandConfirm
  )
})

// 未保存改动时切换路由弹窗提醒(保存并离开复用 handleSave)
useUnsavedGuard(policyDirty, () => handleSave())

/** 重置策略表单为系统默认值(不立即保存) */
function resetPolicyToDefault(): void {
  policyAgent2Enabled.value = DEFAULT_POLICY.agent2_enabled
  policyAllowVerify.value = DEFAULT_POLICY.allow_verify
  policyAllowReference.value = DEFAULT_POLICY.allow_reference_check
  policyVerifierAuthMode.value = DEFAULT_POLICY.verifier_auth_mode_default
  policyExecutorCommandConfirm.value = DEFAULT_POLICY.executor_command_confirm_default
}

// ============================================================
// 加载 / 保存
// ============================================================
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const updatedAt = ref<string | null>(null)

/** 顶部居中 toast */
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 5000)
}

async function loadPolicy(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const data = await getPreferences()
    updatedAt.value = data.updated_at ?? null
    const policy = data.agent_policy
    policyAgent2Enabled.value = policy?.agent2_enabled ?? DEFAULT_POLICY.agent2_enabled
    policyAllowVerify.value = policy?.allow_verify ?? DEFAULT_POLICY.allow_verify
    policyAllowReference.value = policy?.allow_reference_check ?? DEFAULT_POLICY.allow_reference_check
    policyVerifierAuthMode.value = policy?.verifier_auth_mode_default ?? DEFAULT_POLICY.verifier_auth_mode_default
    policyExecutorCommandConfirm.value = policy?.executor_command_confirm_default ?? DEFAULT_POLICY.executor_command_confirm_default
    // 同步原始值(脏检查基准)
    originalPolicy.value = {
      agent2Enabled: policyAgent2Enabled.value,
      allowVerify: policyAllowVerify.value,
      allowReference: policyAllowReference.value,
      verifierAuthMode: policyVerifierAuthMode.value,
      executorCommandConfirm: policyExecutorCommandConfirm.value,
    }
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

async function handleSave(): Promise<boolean> {
  if (!policyDirty.value || saving.value) return false
  saving.value = true
  try {
    const body: SaveAgentPolicyRequest = {
      agent2_enabled: policyAgent2Enabled.value,
      allow_verify: policyAllowVerify.value,
      allow_reference_check: policyAllowReference.value,
      verifier_auth_mode_default: policyVerifierAuthMode.value,
      executor_command_confirm_default: policyExecutorCommandConfirm.value,
    }
    const data = await saveAgentPolicy(body)
    updatedAt.value = data.updated_at ?? null
    // 重新 hydrate(后端可能规范化字段)
    const policy = data.agent_policy
    if (policy) {
      policyAgent2Enabled.value = policy.agent2_enabled
      policyAllowVerify.value = policy.allow_verify
      policyAllowReference.value = policy.allow_reference_check ?? DEFAULT_POLICY.allow_reference_check
      policyVerifierAuthMode.value = policy.verifier_auth_mode_default
      policyExecutorCommandConfirm.value = policy.executor_command_confirm_default
    }
    originalPolicy.value = {
      agent2Enabled: policyAgent2Enabled.value,
      allowVerify: policyAllowVerify.value,
      allowReference: policyAllowReference.value,
      verifierAuthMode: policyVerifierAuthMode.value,
      executorCommandConfirm: policyExecutorCommandConfirm.value,
    }
    showToast('智能体策略已保存', 'success')
    return true
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
    return false
  } finally {
    saving.value = false
  }
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '从未保存'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

// ============================================================
// 字段帮助气泡:圆圈问号按钮,点击显示说明,点击外部关闭
// ============================================================
/** 各字段帮助说明(点击问号按钮展示) */
const FIELD_HELP: Record<string, string> = {
  agent2_enabled:
    '开启后,检查助手参与协作(每轮执行后的质检评估、验证)。关闭后退化为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做质检评估、不验证。适合简单任务或用户完全信任 AI助手的场景。',
  allow_verify:
    '开启后,检查助手可自行调用工具验证 AI助手的产出。安全审计类任务会在未显式设置时自动开启(实际验证仍需任务配置测试环境 URL)。开启后对"疑似但不确定"的安全发现会优先发送 PoC 到测试环境确认。',
  allow_reference_check:
    '开启后,检查助手会抽查复核 AI助手结论中引用的外部依据链接(CVE / 安全公告 / 官方文档):链接是否存在、来源是否权威(域名分级参考信号)、内容是否与其说法相符。抓取在后端进行并有 SSRF 防护;无法访问时仅标注"复核无法完成",不会因此否定结论。',
  verifier_auth_mode:
    '仅在开启「自行验证」时生效。逐动作授权:每个验证动作(HTTP 请求 / PoC 脚本)执行前弹窗让用户确认;直接执行:验证动作自动执行不弹窗。此为用户级默认,任务创建或运行时可单独覆盖。',
  executor_command_confirm:
    '控制 AI助手(内置执行器 / Qoder / DeepSeek / Codex)执行危险命令时是否弹窗确认。自动批准:所有命令直接执行不弹窗(速度快,适合可信任务);逐命令确认:每个危险命令执行前弹窗让用户批准(更安全,防容器破坏/资源耗尽)。此为用户级默认,任务创建时可单独覆盖。注意:Codex CLI 受非交互模式限制,仅支持自动批准,选择「逐命令确认」时会降级并警告。',
}

/** 当前展开帮助气泡的字段 key(null=无展开) */
const openHelpKey = ref<string | null>(null)
/** 字段帮助气泡容器 DOM(按 key 索引,用于点击外部判断) */
const fieldHelpRefs = new Map<string, HTMLElement>()

/** 切换某字段帮助气泡:已展开则收起,未展开则展开(同时收起其他字段) */
function toggleFieldHelp(key: string): void {
  openHelpKey.value = openHelpKey.value === key ? null : key
}

/** 点击帮助容器外部时关闭气泡 */
function onDocClick(e: MouseEvent): void {
  if (openHelpKey.value) {
    const el = fieldHelpRefs.get(openHelpKey.value)
    if (!el || !el.contains(e.target as Node)) {
      openHelpKey.value = null
    }
  }
}

onMounted(() => {
  loadPolicy()
  document.addEventListener('click', onDocClick)
})

onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})
</script>

<template>
  <div class="panel">
    <!-- 页头 -->
    <div class="page-header">
      <div>
        <h1>智能体策略</h1>
        <p class="page-subtitle">
          检查助手与执行策略的用户级默认。任务创建时可单独覆盖。
        </p>
      </div>
      <div class="header-meta">
        <span class="meta-label">最后保存</span>
        <span class="meta-value">{{ loading ? '加载中…' : formatTime(updatedAt) }}</span>
      </div>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="loading-box">
      <span class="status-spinner" aria-label="加载中" />
      <span>正在加载策略配置…</span>
    </div>

    <!-- 加载失败 -->
    <div v-else-if="loadError" class="alert alert-error" role="alert">
      <span>加载失败:{{ loadError }}</span>
      <button class="btn-link" @click="loadPolicy">重试</button>
    </div>

    <!-- 策略表单(无卡片,平铺更简洁) -->
    <section v-else class="policy-form">
      <!-- 启用 agent2 开关(最核心,控制全局) -->
      <label class="policy-toggle-row policy-toggle-primary">
        <input v-model="policyAgent2Enabled" class="switch" type="checkbox" :disabled="saving" />
        <span>启用检查助手</span>
        <div
          :ref="(el) => { if (el) fieldHelpRefs.set('agent2_enabled', el as HTMLElement); else fieldHelpRefs.delete('agent2_enabled') }"
          class="field-help-wrap"
        >
          <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('agent2_enabled')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
          </button>
          <Transition name="help-fade">
            <div v-if="openHelpKey === 'agent2_enabled'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.agent2_enabled }}</div>
          </Transition>
        </div>
      </label>

      <!-- 单 agent 模式提示:agent2 关闭时说明下方依赖字段为何隐藏 -->
      <p v-if="!policyAgent2Enabled" class="policy-single-hint">
        当前为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做覆盖度评估与验证。
      </p>

      <!-- agent2 依赖字段:关闭时整组隐藏(v-show 保留值,保存 payload 不变) -->
      <Transition name="collapse">
        <div v-show="policyAgent2Enabled" class="policy-dependent">
      <label class="policy-toggle-row">
        <input v-model="policyAllowVerify" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
        <span>允许检查助手自行验证 <span class="policy-experimental">(实验性)</span></span>
        <div
          :ref="(el) => { if (el) fieldHelpRefs.set('allow_verify', el as HTMLElement); else fieldHelpRefs.delete('allow_verify') }"
          class="field-help-wrap"
        >
          <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('allow_verify')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
          </button>
          <Transition name="help-fade">
            <div v-if="openHelpKey === 'allow_verify'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.allow_verify }}</div>
          </Transition>
        </div>
      </label>

      <label class="policy-toggle-row">
        <input v-model="policyAllowReference" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
        <span>复核 AI助手引用的网址</span>
        <div
          :ref="(el) => { if (el) fieldHelpRefs.set('allow_reference_check', el as HTMLElement); else fieldHelpRefs.delete('allow_reference_check') }"
          class="field-help-wrap"
        >
          <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('allow_reference_check')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
          </button>
          <Transition name="help-fade">
            <div v-if="openHelpKey === 'allow_reference_check'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.allow_reference_check }}</div>
          </Transition>
        </div>
      </label>

      <Transition name="collapse">
        <div v-show="policyAllowVerify" class="verifier-config">
          <label class="policy-field">
            <div class="field-head">
              <span class="policy-label">验证授权模式</span>
              <div
                :ref="(el) => { if (el) fieldHelpRefs.set('verifier_auth_mode', el as HTMLElement); else fieldHelpRefs.delete('verifier_auth_mode') }"
                class="field-help-wrap"
              >
                <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('verifier_auth_mode')">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                </button>
                <Transition name="help-fade">
                  <div v-if="openHelpKey === 'verifier_auth_mode'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.verifier_auth_mode }}</div>
                </Transition>
              </div>
            </div>
            <BaseSelect
              v-model="policyVerifierAuthMode"
              :options="verifierAuthModeOptions"
              :disabled="saving"
              class="policy-select"
              aria-label="验证授权模式"
            />
          </label>
        </div>
      </Transition>
        </div>
      </Transition>

      <!-- AI助手确认策略(独立于 agent2,始终可用) -->
      <label class="policy-field policy-field-command-confirm">
        <div class="field-head">
          <span class="policy-label">AI助手确认策略</span>
          <div
            :ref="(el) => { if (el) fieldHelpRefs.set('executor_command_confirm', el as HTMLElement); else fieldHelpRefs.delete('executor_command_confirm') }"
            class="field-help-wrap"
          >
            <button type="button" class="field-help-btn" aria-label="查看说明" @click.stop="toggleFieldHelp('executor_command_confirm')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
            </button>
            <Transition name="help-fade">
              <div v-if="openHelpKey === 'executor_command_confirm'" class="field-help-popover" role="tooltip">{{ FIELD_HELP.executor_command_confirm }}</div>
            </Transition>
          </div>
        </div>
        <BaseSelect
          v-model="policyExecutorCommandConfirm"
          :options="executorConfirmOptions"
          :disabled="saving"
          class="policy-select"
          aria-label="AI助手确认策略"
        />
      </label>

      <!-- 操作区 -->
      <div class="policy-actions">
        <button
          type="button"
          class="btn btn-secondary"
          :disabled="saving || !policyDirty"
          @click="resetPolicyToDefault"
        >恢复默认</button>

        <button
          type="button"
          class="btn btn-primary"
          :disabled="saving || !policyDirty"
          @click="handleSave"
        >
          <span v-if="saving" class="btn-spinner" />
          {{ saving ? '保存中…' : '保存' }}
        </button>

        <span v-if="policyDirty" class="dirty-dot-hint">有未保存改动</span>
      </div>
    </section>

    <!-- 浮动提示弹窗 -->
    <Teleport to="body">
      <Transition name="toast-slide">
        <div
          v-if="toast"
          :class="['toast-popup', toast.type === 'error' ? 'toast-error' : 'toast-success']"
          role="status"
          aria-live="polite"
        >
          <span class="toast-icon" aria-hidden="true">
            <svg
              v-if="toast.type === 'success'"
              width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            >
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
            <svg
              v-else
              width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </span>
          <span class="toast-msg">{{ toast.msg }}</span>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.panel {
  max-width: 760px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5) var(--space-8);
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

@media (max-width: 640px) {
  .panel {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  .page-header {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin: 0;
}

.page-subtitle {
  margin: var(--space-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.header-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}

.meta-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.meta-value {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

/* ---- 加载/错误态 ---- */
.loading-box {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.alert {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ---- 策略表单 ---- */
.policy-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.policy-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.policy-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.policy-select {
  width: 100%;
}

.policy-select .base-select-trigger {
  width: 100%;
}

/* ---- 字段头部:标签 + 帮助按钮 ---- */
.field-head {
  display: flex;
  align-items: center;
  gap: var(--space-1);
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
  word-break: break-word;
}

.help-fade-enter-active,
.help-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.help-fade-enter-from,
.help-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.policy-toggle-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.policy-toggle-row:hover {
  background: var(--color-surface-alt);
}

.policy-toggle-row input {
  cursor: pointer;
}

.policy-toggle-row input:disabled {
  cursor: not-allowed;
}

.policy-toggle-primary {
  padding: var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  font-weight: var(--fw-medium);
}

.policy-single-hint {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.policy-dependent {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* ---- Switch 拨动开关 ---- */
input.switch {
  appearance: none;
  flex-shrink: 0;
  width: 36px;
  height: 20px;
  margin: 0;
  position: relative;
  border-radius: var(--radius-full);
  background: var(--color-border-strong);
  cursor: pointer;
  transition: background var(--transition-fast);
}

input.switch::before {
  content: '';
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--color-surface);
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.2);
  transition: transform var(--transition-fast);
}

input.switch:checked {
  background: var(--color-primary);
}

input.switch:checked::before {
  transform: translateX(16px);
}

input.switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

input.switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.policy-experimental {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-style: italic;
}

.verifier-config {
  margin-left: var(--space-5);
  padding: var(--space-2) 0;
}

.verifier-config .policy-field {
  max-width: 360px;
}

.policy-field-command-confirm {
  max-width: 360px;
  margin-bottom: var(--space-2);
}

/* ---- 操作区 ---- */
.policy-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
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

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

.dirty-dot-hint {
  font-size: var(--fs-xs);
  color: var(--color-primary);
  font-weight: var(--fw-medium);
}

.btn-link {
  background: none;
  border: none;
  padding: 2px 6px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.btn-link:hover {
  text-decoration: underline;
}

/* ---- collapse 过渡 ---- */
.collapse-enter-active,
.collapse-leave-active {
  transition: opacity var(--transition-fast), max-height var(--transition-fast);
  overflow: hidden;
}

.collapse-enter-from,
.collapse-leave-to {
  opacity: 0;
  max-height: 0;
}

.collapse-enter-to,
.collapse-leave-from {
  opacity: 1;
  max-height: 600px;
}

/* ---- toast ---- */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 280px;
  max-width: 420px;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  box-shadow: var(--shadow-lg);
  border: 1px solid transparent;
}

.toast-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border-color: #bbf7d0;
}

.toast-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border-color: #fecaca;
}

.toast-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  margin-top: 1px;
}

.toast-msg {
  flex: 1;
  word-break: break-word;
  white-space: pre-wrap;
}

.toast-slide-enter-active,
.toast-slide-leave-active {
  transition: opacity var(--transition-base), transform var(--transition-base);
}

.toast-slide-enter-from,
.toast-slide-leave-to {
  opacity: 0;
  transform: translate(-50%, -12px);
}
</style>
