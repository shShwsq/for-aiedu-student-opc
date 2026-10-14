<script setup lang="ts">
/**
 * 智能体策略设置面板(嵌套在 SettingsLayout 内)
 *
 * 作为 agent2(检查助手)行为与执行命令策略的用户级默认配置:
 * - agent2 启停
 * - 验证权限:agent2 是否能自行调用工具验证(实验性)
 * - 验证授权模式:验证动作的默认授权模式(直接执行 / 逐动作授权)
 * - AI助手确认策略
 * - 任务默认审查模型:任务没选模型时检查助手/内置助手用的默认模型
 *   (存在模型槽位表,与上面的策略同一次点击一起保存)
 */
import { computed, onMounted, ref } from 'vue'

import BaseSelect from '@/components/BaseSelect.vue'
import HelpPopover from '@/components/HelpPopover.vue'
import ModelSlotSelect from '@/components/ModelSlotSelect.vue'
import { getPreferences, saveAgentPolicy } from '@/api/memory'
import { getModelSlots, saveModelSlots } from '@/api/model_slots'
import { ensureLlmConfigsLoaded, llmConfigs } from '@/composables/useLlmConfigOptions'
import { useUnsavedGuard } from '@/composables/useUnsavedGuard'
import { extractErrorMessage } from '@/utils/error'
import type { SaveAgentPolicyRequest } from '@/types/memory'

// ============================================================
// 默认策略值(与后端 DEFAULT_AGENT_POLICY 对齐)
// ============================================================

const DEFAULT_POLICY = {
  agent2_enabled: true,
  extract_knowledge_points: true,
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
/** agent2 是否提炼重点与知识点(关闭=只做核查) */
const policyExtractKp = ref(DEFAULT_POLICY.extract_knowledge_points)
/** agent2 是否能自己验证(实验性) */
const policyAllowVerify = ref(DEFAULT_POLICY.allow_verify)
/** agent2 是否能复核 AI助手引用的网址(后端安全抓取,结果仅供参考信号) */
const policyAllowReference = ref(DEFAULT_POLICY.allow_reference_check)
/** 验证授权默认模式(任务级可覆盖) */
const policyVerifierAuthMode = ref<'direct' | 'per_action'>(DEFAULT_POLICY.verifier_auth_mode_default)
/** AI助手确认策略默认模式(任务级 _executor_command_confirm 可覆盖) */
const policyExecutorCommandConfirm = ref<'always_approve' | 'per_command'>(DEFAULT_POLICY.executor_command_confirm_default)
/** 任务默认审查模型配置 id(空串=未指定,回退环境默认;存在模型槽位表) */
const policyReviewModelId = ref('')

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
  extractKp: DEFAULT_POLICY.extract_knowledge_points,
  allowVerify: DEFAULT_POLICY.allow_verify,
  allowReference: DEFAULT_POLICY.allow_reference_check,
  verifierAuthMode: DEFAULT_POLICY.verifier_auth_mode_default,
  executorCommandConfirm: DEFAULT_POLICY.executor_command_confirm_default,
  reviewModelId: '',
})

/** agent 策略是否有未保存改动 */
const policyDirty = computed(() => {
  return (
    policyAgent2Enabled.value !== originalPolicy.value.agent2Enabled ||
    policyExtractKp.value !== originalPolicy.value.extractKp ||
    policyAllowVerify.value !== originalPolicy.value.allowVerify ||
    policyAllowReference.value !== originalPolicy.value.allowReference ||
    policyVerifierAuthMode.value !== originalPolicy.value.verifierAuthMode ||
    policyExecutorCommandConfirm.value !== originalPolicy.value.executorCommandConfirm ||
    policyReviewModelId.value !== originalPolicy.value.reviewModelId
  )
})

// 未保存改动时切换路由弹窗提醒(保存并离开复用 handleSave)
useUnsavedGuard(policyDirty, () => handleSave())

/** 重置策略表单为系统默认值(不立即保存) */
function resetPolicyToDefault(): void {
  policyAgent2Enabled.value = DEFAULT_POLICY.agent2_enabled
  policyExtractKp.value = DEFAULT_POLICY.extract_knowledge_points
  policyAllowVerify.value = DEFAULT_POLICY.allow_verify
  policyAllowReference.value = DEFAULT_POLICY.allow_reference_check
  policyVerifierAuthMode.value = DEFAULT_POLICY.verifier_auth_mode_default
  policyExecutorCommandConfirm.value = DEFAULT_POLICY.executor_command_confirm_default
  // 审查模型的系统默认就是「未指定→用环境默认模型」
  policyReviewModelId.value = ''
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
  // 两个读取并行:策略在 /memory/preferences,模型分配在 /models/defaults(槽位表)
  const [prefRes, slotsRes] = await Promise.allSettled([
    getPreferences(),
    getModelSlots(),
    ensureLlmConfigsLoaded(),
  ] as const)
  if (prefRes.status === 'rejected') {
    loadError.value = extractErrorMessage(prefRes.reason)
    loading.value = false
    return
  }
  const data = prefRes.value
  // 槽位读取失败不阻断策略页:当下按「未指定」展示,不写回(保存时由差异判定决定发不发)
  const reviewSlot =
    slotsRes.status === 'fulfilled'
      ? slotsRes.value.slots.find((s) => s.slot === 'review_default')
      : undefined
  const reviewModelId = reviewSlot?.llm_config_id ?? ''
  updatedAt.value = data.updated_at ?? null
  const policy = data.agent_policy
  policyAgent2Enabled.value = policy?.agent2_enabled ?? DEFAULT_POLICY.agent2_enabled
  policyExtractKp.value = policy?.extract_knowledge_points ?? DEFAULT_POLICY.extract_knowledge_points
  policyAllowVerify.value = policy?.allow_verify ?? DEFAULT_POLICY.allow_verify
  policyAllowReference.value = policy?.allow_reference_check ?? DEFAULT_POLICY.allow_reference_check
  policyVerifierAuthMode.value = policy?.verifier_auth_mode_default ?? DEFAULT_POLICY.verifier_auth_mode_default
  policyExecutorCommandConfirm.value = policy?.executor_command_confirm_default ?? DEFAULT_POLICY.executor_command_confirm_default
  policyReviewModelId.value = reviewModelId
  // 同步原始值(脏检查基准)
  originalPolicy.value = snapshotPolicy()
  loading.value = false
}

/** 当前表单值快照(脏检查基准写入用) */
function snapshotPolicy() {
  return {
    agent2Enabled: policyAgent2Enabled.value,
    extractKp: policyExtractKp.value,
    allowVerify: policyAllowVerify.value,
    allowReference: policyAllowReference.value,
    verifierAuthMode: policyVerifierAuthMode.value,
    executorCommandConfirm: policyExecutorCommandConfirm.value,
    reviewModelId: policyReviewModelId.value,
  }
}

async function handleSave(): Promise<boolean> {
  if (!policyDirty.value || saving.value) return false
  saving.value = true
  const reviewChanged = policyReviewModelId.value !== originalPolicy.value.reviewModelId
  try {
    const body: SaveAgentPolicyRequest = {
      agent2_enabled: policyAgent2Enabled.value,
      extract_knowledge_points: policyExtractKp.value,
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
      policyExtractKp.value = policy.extract_knowledge_points ?? DEFAULT_POLICY.extract_knowledge_points
      policyAllowVerify.value = policy.allow_verify
      policyAllowReference.value = policy.allow_reference_check ?? DEFAULT_POLICY.allow_reference_check
      policyVerifierAuthMode.value = policy.verifier_auth_mode_default
      policyExecutorCommandConfirm.value = policy.executor_command_confirm_default
    }
    // 任务默认审查模型落在模型槽位表(另一个端点),同一次点击一起提交;
    // 未改动则不发请求(不会把读取失败时的空值当成「用户选了跟随默认」刷掉真值)
    if (reviewChanged) {
      const slots = await saveModelSlots({
        slots: { review_default: { llm_config_id: policyReviewModelId.value } },
      })
      const saved = slots.slots.find((s) => s.slot === 'review_default')?.llm_config_id ?? ''
      policyReviewModelId.value = saved
    }
    originalPolicy.value = snapshotPolicy()
    showToast('智能体策略已保存', 'success')
    return true
  } catch (err) {
    // 任一步失败都保留脏态(不更新 originalPolicy),用户可直接重试
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
    '开启后,AI助手 产出结果后由检查助手在后台独立复核(核查与验证)。关闭后退化为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做复核、不验证。适合简单任务或用户完全信任 AI助手的场景。',
  extract_knowledge_points:
    '开启后,检查助手除核查外还会从全程产出提炼「重点与知识点」(供侧栏展示、驱动出题与知识点看板)。关闭时只做核查/验证/引用复核,不产出知识点;自动出题可降级为直接基于对话出题。',
  allow_verify:
    '开启后,检查助手可自行调用工具验证 AI助手的产出。安全审计类任务会在未显式设置时自动开启(实际验证仍需任务配置测试环境 URL)。开启后对"疑似但不确定"的安全发现会优先发送 PoC 到测试环境确认。',
  allow_reference_check:
    '开启后,检查助手会抽查复核 AI助手结论中引用的外部依据链接(CVE / 安全公告 / 官方文档):链接是否存在、来源是否权威(域名分级参考信号)、内容是否与其说法相符。抓取在后端进行并有 SSRF 防护;无法访问时仅标注"复核无法完成",不会因此否定结论。',
  verifier_auth_mode:
    '仅在开启「自行验证」时生效。逐动作授权:每个验证动作(HTTP 请求 / PoC 脚本)执行前弹窗让用户确认;直接执行:验证动作自动执行不弹窗。此为用户级默认,任务创建或运行时可单独覆盖。',
  executor_command_confirm:
    '控制 AI助手(内置执行器 / Qoder / DeepSeek / Codex)执行危险命令时是否弹窗确认。自动批准:所有命令直接执行不弹窗(速度快,适合可信任务);逐命令确认:每个危险命令执行前弹窗让用户批准(更安全,防容器破坏/资源耗尽)。此为用户级默认,任务创建时可单独覆盖。注意:Codex CLI 受非交互模式限制,仅支持自动批准,选择「逐命令确认」时会降级并警告。',
  review_model:
    '提交任务时没选模型的情况下,检查助手(评估)与内置 AI助手(执行)默认用哪个模型;任务自带模型仍然优先。选「跟随系统默认」则用环境变量里配的模型。外部 CLI 执行器(Qoder / DeepSeek / Codex)不受这里影响,它们的模型在「CLI 设置」里配。各处任务的默认模型汇总见「模型设置 > 任务模型默认」。',
}

// 字段帮助气泡:统一走共享组件 HelpPopover(正文由下方各字段的插槽供给)
// 全局互斥与「点外部 / Esc 收起」都在组件内,这里不再维护 openKey / DOM 索引
onMounted(loadPolicy)
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
        <HelpPopover>{{ FIELD_HELP.agent2_enabled }}</HelpPopover>
      </label>

      <!-- 单 agent 模式提示:agent2 关闭时说明下方依赖字段为何隐藏 -->
      <p v-if="!policyAgent2Enabled" class="policy-single-hint">
        当前为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做覆盖度评估与验证。
      </p>

      <!-- agent2 依赖字段:关闭时整组隐藏(v-show 保留值,保存 payload 不变) -->
      <Transition name="collapse">
        <div v-show="policyAgent2Enabled" class="policy-dependent">
      <label v-show="policyAgent2Enabled" class="policy-toggle-row">
        <input v-model="policyExtractKp" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
        <span>提炼重点与知识点</span>
        <HelpPopover>{{ FIELD_HELP.extract_knowledge_points }}</HelpPopover>
      </label>
      <label class="policy-toggle-row">
        <input v-model="policyAllowVerify" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
        <span>允许检查助手自行验证 <span class="policy-experimental">(实验性)</span></span>
        <HelpPopover>{{ FIELD_HELP.allow_verify }}</HelpPopover>
      </label>

      <label class="policy-toggle-row">
        <input v-model="policyAllowReference" class="switch" type="checkbox" :disabled="saving || !policyAgent2Enabled" />
        <span>复核 AI助手引用的网址</span>
        <HelpPopover>{{ FIELD_HELP.allow_reference_check }}</HelpPopover>
      </label>

      <Transition name="collapse">
        <div v-show="policyAllowVerify" class="verifier-config">
          <label class="policy-field">
            <div class="field-head">
              <span class="policy-label">验证授权模式</span>
              <HelpPopover>{{ FIELD_HELP.verifier_auth_mode }}</HelpPopover>
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
          <HelpPopover>{{ FIELD_HELP.executor_command_confirm }}</HelpPopover>
        </div>
        <BaseSelect
          v-model="policyExecutorCommandConfirm"
          :options="executorConfirmOptions"
          :disabled="saving"
          class="policy-select"
          aria-label="AI助手确认策略"
        />
      </label>

      <!-- 任务默认审查模型(存 model_slot_defaults,随本表单一起保存) -->
      <div class="policy-field policy-field-review-model">
        <div class="field-head">
          <span class="policy-label">任务默认审查模型</span>
          <HelpPopover max-width="min(380px, 86vw)">{{ FIELD_HELP.review_model }}</HelpPopover>
        </div>
        <ModelSlotSelect
          :model-value="policyReviewModelId"
          :configs="llmConfigs"
          empty-label="跟随系统默认"
          select-label="任务默认审查模型"
          :disabled="saving"
          @update:model-value="policyReviewModelId = $event"
        />
      </div>

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

/* 字段级帮助气泡的按钮与正文样式均由共享组件 HelpPopover 提供 */

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

/* 任务默认审查模型:同样不给整幅宽度(避免下拉占满一行后看不出是个选项) */
.policy-field-review-model {
  max-width: 420px;
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
