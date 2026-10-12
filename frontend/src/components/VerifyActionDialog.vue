<script setup lang="ts">
/**
 * 验证动作授权弹窗
 *
 * verifier_agent 在 per_action 模式下,每次执行 http_request / run_python_code 前
 * 推送 verify_action 事件,前端用此组件弹出对话框让用户确认或拒绝。
 *
 * 对用户透明:不出现 verifier_agent 字样,只显示"验证动作需要授权"。
 *
 * 用户选择后通过 @approve / @approve-all / @reject 事件通知父组件,由父组件调 API 提交决议。
 * approve-all = "本次同意且本任务后续动作自动允许":一次 PoC 序列动辄七八个动作,
 * 逐个确认会把人逼向完全不弹窗的 direct 模式(那才真的没有人工把关);自动放行的
 * 动作仍逐条落库进侧栏,事后看得见跑了什么。
 */
import { computed } from 'vue'
import type { VerifyActionEventData } from '@/types/task'

const props = defineProps<{
  /** 是否显示弹窗 */
  open: boolean
  /** 待授权的验证动作详情 */
  action: VerifyActionEventData | null
  /** 提交中状态(禁用按钮) */
  submitting?: boolean
}>()

const emit = defineEmits<{
  /** 用户同意执行该动作 */
  (e: 'approve', actionId: string): void
  /** 用户同意该动作,且本任务后续验证动作不再弹窗 */
  (e: 'approve-all', actionId: string): void
  /** 用户拒绝执行该动作 */
  (e: 'reject', actionId: string): void
}>()

/** 是否为 HTTP 请求动作 */
const isHttpRequest = computed(() => props.action?.type === 'http_request')

/** 是否为运行代码动作 */
const isRunCode = computed(() => props.action?.type === 'run_python_code')

/** 动作类型显示名 */
const actionTypeLabel = computed(() => {
  if (isHttpRequest.value) return 'HTTP 请求'
  if (isRunCode.value) return '运行 PoC 脚本'
  return props.action?.type ?? '未知动作'
})

/** 请求头格式化展示 */
const formattedHeaders = computed(() => {
  const headers = props.action?.headers
  if (!headers || Object.keys(headers).length === 0) return null
  return Object.entries(headers).map(([k, v]) => `${k}: ${v}`)
})

function handleApprove(): void {
  if (!props.action?.action_id || props.submitting) return
  emit('approve', props.action.action_id)
}

function handleApproveAll(): void {
  if (!props.action?.action_id || props.submitting) return
  emit('approve-all', props.action.action_id)
}

function handleReject(): void {
  if (!props.action?.action_id || props.submitting) return
  emit('reject', props.action.action_id)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open && action" class="dialog-mask" @click.self="handleReject">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>验证动作需要授权</h3>
            <button
              class="dialog-close"
              :disabled="submitting"
              aria-label="关闭"
              @click="handleReject"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="dialog-intro">
              智能体想执行以下验证动作,请确认是否允许。
            </p>

            <!-- HTTP 请求动作 -->
            <div v-if="isHttpRequest" class="action-detail">
              <div class="detail-row">
                <span class="detail-label">类型</span>
                <span class="detail-value">
                  <span class="method-badge" :class="`method-${(action.method || 'GET').toLowerCase()}`">
                    {{ action.method || 'GET' }}
                  </span>
                  HTTP 请求
                </span>
              </div>
              <div v-if="action.auth_profile" class="detail-row">
                <span class="detail-label">登录身份</span>
                <span class="detail-value">
                  <span class="auth-profile-badge">{{ action.auth_profile }}</span>
                </span>
              </div>
              <div class="detail-row">
                <span class="detail-label">URL</span>
                <code class="detail-value detail-url">{{ action.url || '(未指定)' }}</code>
              </div>
              <div v-if="formattedHeaders" class="detail-row">
                <span class="detail-label">请求头</span>
                <div class="detail-value detail-headers">
                  <code v-for="h in formattedHeaders" :key="h">{{ h }}</code>
                </div>
              </div>
              <div v-if="action.body" class="detail-row">
                <span class="detail-label">请求体</span>
                <pre class="detail-value detail-body">{{ action.body }}</pre>
              </div>
            </div>

            <!-- 运行代码动作 -->
            <div v-else-if="isRunCode" class="action-detail">
              <div class="detail-row">
                <span class="detail-label">类型</span>
                <span class="detail-value">运行 PoC 脚本</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">代码</span>
                <pre class="detail-value detail-code">{{ action.code || '(空)' }}</pre>
              </div>
              <p v-if="action.code_truncated" class="truncate-hint">
                代码已截断展示(实际长度超过 2000 字符)
              </p>
            </div>

            <!-- 其他类型 -->
            <div v-else class="action-detail">
              <div class="detail-row">
                <span class="detail-label">类型</span>
                <span class="detail-value">{{ actionTypeLabel }}</span>
              </div>
              <div v-if="action.args" class="detail-row">
                <span class="detail-label">参数</span>
                <pre class="detail-value detail-code">{{ JSON.stringify(action.args, null, 2) }}</pre>
              </div>
            </div>
          </div>

          <footer class="dialog-footer">
            <span class="footer-hint">拒绝后智能体会收到反馈并跳过此动作;授权等待有时限,超时按未执行处理</span>
            <div class="footer-actions">
              <button
                class="btn btn-secondary"
                :disabled="submitting"
                @click="handleReject"
              >拒绝</button>
              <button
                class="btn btn-ghost"
                :disabled="submitting"
                title="本任务后续验证动作不再弹窗(仍会逐条落库,侧栏可回看)"
                @click="handleApproveAll"
              >同意,后续自动允许</button>
              <button
                class="btn btn-primary"
                :disabled="submitting"
                @click="handleApprove"
              >
                <span v-if="submitting" class="btn-spinner" />
                {{ submitting ? '提交中...' : '仅同意这一次' }}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl, 0 20px 40px rgba(0, 0, 0, 0.15));
  width: 100%;
  max-width: 600px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.dialog-close {
  background: none;
  border: none;
  font-size: 24px;
  line-height: 1;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.dialog-close:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.dialog-intro {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  margin: 0;
}

.action-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  background: var(--color-surface-alt);
  padding: var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
}

.detail-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.detail-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.detail-value {
  font-size: var(--fs-sm);
  color: var(--color-text);
  word-break: break-all;
}

.detail-url {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: var(--fs-xs);
  background: var(--color-surface);
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
}

.detail-headers {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.detail-headers code {
  font-size: var(--fs-xs);
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  color: var(--color-text-secondary);
}

.detail-body,
.detail-code {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: var(--fs-xs);
  background: var(--color-surface);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  color: var(--color-text);
}

.method-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  margin-right: var(--space-2);
}

.method-get { background: #dbeafe; color: #1e40af; }
.method-post { background: #dcfce7; color: #166534; }
.method-put { background: #fef3c7; color: #92400e; }
.method-delete { background: #fee2e2; color: #991b1b; }
.method-patch { background: #e9d5ff; color: #6b21a8; }

/* 登录身份徽章(展示 auth_profile) */
.auth-profile-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.truncate-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-style: italic;
  margin: 0;
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.footer-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  flex: 1;
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
}

.btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
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
  filter: brightness(1.05);
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

/* 第三个选项:"同意且后续自动允许"——不抢主按钮的视觉权重,但要比次按钮可读 */
.btn-ghost {
  background: transparent;
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-ghost:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.btn-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

/* 弹窗淡入淡出 */
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
