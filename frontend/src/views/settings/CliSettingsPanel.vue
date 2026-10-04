<script setup lang="ts">
/**
 * CLI 智能体设置面板(嵌套在 SettingsLayout 内)
 *
 * 顶部选项卡动态加载后端所有已注册 agent 类型(GET /agents/types,走共享缓存),
 * 下方平铺当前 agent 的内联配置表单(AgentConfigPanel)。
 *
 * 与设置页二级目录双向同步:侧栏子项与顶部 tab 都只改 URL hash(#agent_type),
 * 实际切换统一由 route.hash 的 watch 落到 activateTab——单向数据流,
 * 既保证两处高亮一致,也不会互相触发回环。tab 栏保留(不因为有了二级目录而移除)。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AgentConfigPanel from '@/components/AgentConfigPanel.vue'
import {
  deleteAgentConfig,
  getAgentConfig,
  saveAgentConfig,
  testAgentConfig,
} from '@/api/agent_configs'
import { agentTypes, ensureAgentTypesLoaded } from '@/composables/useAgentTypes'
import { extractErrorMessage } from '@/utils/error'
import type {
  AgentConfigDetailOut,
  AgentTestResult,
  AgentTypeMeta,
  CredentialValue,
} from '@/types/agent_configs'

// ============================================================
// Toast(顶部居中,5s 自动消失)
// ============================================================
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 5000)
}

// ============================================================
// agent 类型 + 按类型隔离的状态
// ============================================================
const route = useRoute()
const router = useRouter()

// agentTypes 为模块级共享缓存(useAgentTypes):设置目录的二级项读同一份,
// /agents/types 在「布局渲染二级目录 + 面板渲染表单」之间只请求一次
const agentTypesLoading = ref(true)
/** 当前激活的 agent_type */
const activeType = ref<string>('')
/** 已点开过的 agent(首次点开才挂载 Panel + 加载 detail) */
const activated = reactive(new Set<string>())

/** 单个 agent 的完整状态(隔离 detail / 流 / 测试结果) */
interface AgentState {
  detail: AgentConfigDetailOut | null
  detailLoaded: boolean
  detailLoading: boolean
  saving: boolean
  error: string
  testing: boolean
  testResult: AgentTestResult | null
  testStage: string
  testThinking: string
  testContent: string
}

function createAgentState(): AgentState {
  return {
    detail: null,
    detailLoaded: false,
    detailLoading: false,
    saving: false,
    error: '',
    testing: false,
    testResult: null,
    testStage: '',
    testThinking: '',
    testContent: '',
  }
}

/** agent_type → 独立状态(loadAgentTypes 时为每个 type 初始化) */
const states = reactive<Record<string, AgentState>>({})

/** 进行中的测试流 AbortController(离开页面时统一中止) */
const abortControllers = new Map<string, AbortController>()

/** tab 按钮 refs(切换时 scrollIntoView) */
const tabRefs = ref<HTMLElement[]>([])

/** 把 agentTypes 与 states zip 起来供模板遍历(state 一定已初始化) */
const panels = computed(() =>
  agentTypes.value.map((meta) => ({ meta, state: states[meta.agent_type]! })),
)

// ============================================================
// 加载
// ============================================================
onMounted(() => {
  loadAgentTypes()
})

onUnmounted(() => {
  // 中止所有进行中的测试流,避免后端沙箱空跑
  for (const controller of abortControllers.values()) {
    controller.abort()
  }
  abortControllers.clear()
})

async function loadAgentTypes(): Promise<void> {
  agentTypesLoading.value = true
  try {
    const types = await ensureAgentTypesLoaded()
    // 为每个 agent 预初始化独立状态(模板遍历时 state 一定存在)
    for (const meta of types) {
      if (!states[meta.agent_type]) {
        states[meta.agent_type] = createAgentState()
      }
    }
    // 默认激活:hash 命中的 agent(书签/深链),否则第一个;首屏不写 hash,保持 URL 干净
    const initial = pickAgentByHash(route.hash) ?? types[0]
    if (initial) {
      activateTab(initial)
    }
  } catch (err) {
    showToast(`加载 agent 类型失败: ${extractErrorMessage(err)}`, 'error')
  } finally {
    agentTypesLoading.value = false
  }
}

async function loadDetail(type: string): Promise<void> {
  const s = states[type]
  if (!s) return
  s.detailLoading = true
  try {
    s.detail = await getAgentConfig(type)
  } catch (err) {
    // 未配置时后端可能 404,静默处理:detail 保持 null(按未配置渲染)
    console.warn('加载 agent 配置详情失败:', err)
  } finally {
    s.detailLoaded = true
    s.detailLoading = false
  }
}

// ============================================================
// 选项卡切换(hash 为唯一入口,tab 栏与侧栏二级目录共用)
// ============================================================
/** 取 hash('#agent_type') 命中的 agent;未知 hash 与空 hash 一样回退第一个,与目录高亮规则一致 */
function pickAgentByHash(hash: string | undefined): AgentTypeMeta | null {
  const id = (hash ?? '').startsWith('#') ? (hash as string).slice(1) : (hash ?? '')
  return agentTypes.value.find((meta) => meta.agent_type === id) ?? agentTypes.value[0] ?? null
}

function activateTab(meta: AgentTypeMeta): void {
  activeType.value = meta.agent_type
  activated.add(meta.agent_type)
  const s = states[meta.agent_type]
  if (s && !s.detailLoaded && !s.detailLoading) {
    loadDetail(meta.agent_type)
  }
  // active tab 滚入视野(窄屏 tab 栏横向滚动场景)
  nextTick(() => {
    const idx = agentTypes.value.findIndex((t) => t.agent_type === meta.agent_type)
    tabRefs.value[idx]?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  })
}

/** tab 栏点击:只负责把 hash 写进 URL,真正的切换交给下面的 route.hash watch */
function selectAgent(meta: AgentTypeMeta): void {
  if (meta.agent_type === activeType.value) return
  router.push({ hash: `#${meta.agent_type}` })
}

// 侧栏二级目录 / tab 栏 / 浏览器前进后退都只改 hash,统一落到 activateTab:
// 两处入口共用一条路径,既保持同步又不会回环(目标与当前一致时不动作)。
// 未知 hash 回退第一个 agent(空 hash 即「点已选中的一级项回默认子项」同样走此分支)。
watch(
  () => route.hash,
  (hash) => {
    // 只在 CLI 页响应(离开后 route 仍可能短暂持有旧 hash)
    if (!route.path.startsWith('/settings/cli')) return
    const meta = pickAgentByHash(hash)
    if (meta && meta.agent_type !== activeType.value) {
      activateTab(meta)
    }
  },
)

// ============================================================
// 保存 / 清除 / 测试(均操作 states[type],回调闭包绑定到对应 entry)
// ============================================================
function metaOf(type: string): AgentTypeMeta | undefined {
  return agentTypes.value.find((t) => t.agent_type === type)
}

async function handleSave(
  type: string,
  credentials: CredentialValue[],
  is_active: boolean,
): Promise<void> {
  const s = states[type]
  if (!s) return
  s.error = ''
  s.saving = true
  try {
    s.detail = await saveAgentConfig(type, { credentials, is_active })
    s.detailLoaded = true
    // 清空旧测试结果与流式状态(凭证可能已变)
    s.testResult = null
    s.testStage = ''
    s.testThinking = ''
    s.testContent = ''
    showToast(`${metaOf(type)?.display_name ?? type} 配置已保存`, 'success')
  } catch (err) {
    s.error = extractErrorMessage(err)
  } finally {
    s.saving = false
  }
}

async function handleClear(type: string): Promise<void> {
  const s = states[type]
  if (!s) return
  s.error = ''
  s.saving = true
  try {
    await deleteAgentConfig(type)
    s.detail = null
    s.testResult = null
    s.testStage = ''
    s.testThinking = ''
    s.testContent = ''
    showToast(`${metaOf(type)?.display_name ?? type} 配置已清除`, 'success')
  } catch (err) {
    s.error = extractErrorMessage(err)
  } finally {
    s.saving = false
  }
}

async function handleTest(type: string): Promise<void> {
  const s = states[type]
  if (!s) return
  s.error = ''
  s.testing = true
  s.testResult = null
  s.testStage = ''
  s.testThinking = ''
  s.testContent = ''
  const controller = new AbortController()
  abortControllers.set(type, controller)
  await testAgentConfig(
    type,
    {
      onStage: (_stage, message) => {
        s.testStage = message
      },
      onThinking: (delta) => {
        s.testThinking += delta
      },
      onContent: (delta) => {
        s.testContent += delta
      },
      onDone: (ok, message) => {
        s.testResult = { ok, message }
        s.testing = false
        abortControllers.delete(type)
      },
      onError: (message) => {
        s.testResult = { ok: false, message }
        s.testing = false
        abortControllers.delete(type)
      },
    },
    controller.signal,
  )
}
</script>

<template>
  <div class="panel">
    <!-- 加载中 -->
    <div v-if="agentTypesLoading" class="loading">
      <div class="spinner" />
      <span>加载中...</span>
    </div>

    <!-- 空状态:无可用 agent 类型或加载失败 -->
    <div v-else-if="agentTypes.length === 0" class="empty">
      <p class="empty-title">暂无可用的 CLI 智能体类型</p>
      <p class="empty-desc">请检查后端 agent 注册表或稍后重试</p>
    </div>

    <template v-else>
      <!-- 固定头部(居中,不随面板滚动) -->
      <div class="panel-inner">
        <!-- 页头 -->
        <div class="page-header">
          <h1>CLI 智能体设置</h1>
        </div>

        <!-- ============ 选项卡 ============ -->
        <div class="tab-bar" role="tablist">
          <button
            v-for="(meta, idx) in agentTypes"
            :key="meta.agent_type"
            :ref="(el) => { if (el) tabRefs[idx] = el as HTMLElement }"
            class="tab"
            :class="{ 'tab-active': activeType === meta.agent_type }"
            role="tab"
            :aria-selected="activeType === meta.agent_type"
            :tabindex="activeType === meta.agent_type ? 0 : -1"
            @click="selectAgent(meta)"
          >
            {{ meta.display_name }}
          </button>
        </div>
      </div>

      <!-- ============ 面板滚动区 ============ -->
      <div class="panels-scroll">
        <div class="panels-col">
          <div
            v-for="p in panels"
            :key="p.meta.agent_type"
            role="tabpanel"
          >
            <AgentConfigPanel
              v-if="activated.has(p.meta.agent_type)"
              v-show="activeType === p.meta.agent_type"
              :meta="p.meta"
              :detail="p.state.detail"
              :saving="p.state.saving || p.state.detailLoading"
              :error="p.state.error"
              :testing="p.state.testing"
              :test-result="p.state.testResult"
              :test-stage="p.state.testStage"
              :test-thinking="p.state.testThinking"
              :test-content="p.state.testContent"
              @save="(creds, active) => handleSave(p.meta.agent_type, creds, active)"
              @clear="handleClear(p.meta.agent_type)"
              @test="handleTest(p.meta.agent_type)"
            />
          </div>
        </div>
      </div>
    </template>

    <!-- ============ 浮动提示弹窗 ============ -->
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
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

/* 固定头部(页头 + 选项卡):全宽容器内居中 */
.panel-inner {
  flex-shrink: 0;
  width: 100%;
  max-width: 680px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5) 0;
}

.loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  flex: 1;
  padding: var(--space-16);
  color: var(--color-text-secondary);
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- 空状态 ---- */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  padding: var(--space-4);
  text-align: center;
}

.empty-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  margin: 0 0 var(--space-2);
}

.empty-desc {
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  margin: 0;
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
  flex-shrink: 0;
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin: 0;
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 640px) {
  .panel-inner {
    padding: var(--space-4) var(--space-3) 0;
  }

  .page-header {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }

  .tab-bar {
    overflow-x: auto;
    scrollbar-width: none;
  }

  .tab-bar::-webkit-scrollbar {
    display: none;
  }

  .panels-col {
    padding: 0 var(--space-3) var(--space-6);
  }
}

/* ---- 选项卡栏 ---- */
.tab-bar {
  display: flex;
  gap: var(--space-1);
  border-bottom: 1px solid var(--color-border);
  margin-bottom: var(--space-5);
  flex-shrink: 0;
}

/* ---- 面板滚动区 ---- */
.panels-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.panels-col {
  max-width: 680px;
  margin: 0 auto;
  padding: 0 var(--space-5) var(--space-8);
}

.tab {
  padding: var(--space-3) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
  white-space: nowrap;
  flex-shrink: 0;
}

.tab:hover {
  color: var(--color-text);
}

.tab-active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
  font-weight: var(--fw-semibold);
}

/* ---- 浮动提示弹窗 ---- */
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
