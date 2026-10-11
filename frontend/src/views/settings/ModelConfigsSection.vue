<script setup lang="ts">
/**
 * 模型清单(「模型设置 > 模型清单」二级项,嵌在 ModelSettingsPanel 容器内)
 *
 * LLM 与 Embedding 配置合并为一张表格展示,通过「类型」列区分。
 * 每条配置有唯一 id 和自定义名称,任务提交时从列表中选择使用。
 *
 * 列表变化(增删改)后作废 useLlmConfigOptions 的共享缓存:否则各设置页的
 * 「默认模型」下拉会接着展示已被删除的配置(选中后后端按归属校验拒绝)。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'

import ModelConfigDialog from '@/components/ModelConfigDialog.vue'
import { getCatalog, getMyModels, saveModels, testEmbedding, testLLM } from '@/api/model_configs'
import { invalidateLlmConfigs } from '@/composables/useLlmConfigOptions'
import { extractErrorMessage } from '@/utils/error'
import type {
  EmbeddingConfigItem,
  EmbeddingConfigItemOut,
  LLMConfigItem,
  LLMConfigItemOut,
  ModelsCatalog,
} from '@/types/model_configs'

type Kind = 'llm' | 'embedding'

// ---- 厂商清单 ----
const catalog = ref<ModelsCatalog | null>(null)
const loadingCatalog = ref(true)

// ---- LLM 配置列表(带前端状态) ----
interface LLMConfigEditable extends LLMConfigItem {
  has_api_key: boolean
  testing: boolean
}
const llmConfigs = reactive<LLMConfigEditable[]>([])

// ---- Embedding 配置列表 ----
interface EmbeddingConfigEditable extends EmbeddingConfigItem {
  has_api_key: boolean
  testing: boolean
}
const embConfigs = reactive<EmbeddingConfigEditable[]>([])

// ---- 状态 ----
const loadingConfig = ref(true)
const saving = ref(false)
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

// ---- 弹窗状态 ----
const dialogOpen = ref(false)
const dialogMode = ref<'add' | 'edit'>('add')
const dialogKind = ref<Kind>('llm')
type LLMInitial = LLMConfigItem & { has_api_key: boolean }
type EmbInitial = EmbeddingConfigItem & { has_api_key: boolean }
const dialogInitial = ref<LLMInitial | EmbInitial | null>(null)

// ============================================================
// 统一表格行:合并两类配置用于展示,操作时按 kind 分发回原数组
// ============================================================

interface TableRow {
  kind: Kind
  id: string
  name: string
  provider: string
  model: string
  has_api_key: boolean
  testing: boolean
  /** LLM 是否启用深度思考;Embedding 恒为 null(显示斜杠占位) */
  enable_thinking: boolean | null
  cfg: LLMConfigEditable | EmbeddingConfigEditable
}

const tableRows = computed<TableRow[]>(() => [
  ...llmConfigs.map((c) => ({
    kind: 'llm' as const,
    id: c.id,
    name: c.name,
    provider: c.provider,
    model: c.model,
    has_api_key: c.has_api_key,
    testing: c.testing,
    enable_thinking: c.enable_thinking,
    cfg: c,
  })),
  ...embConfigs.map((c) => ({
    kind: 'embedding' as const,
    id: c.id,
    name: c.name,
    provider: c.provider,
    model: c.model,
    has_api_key: c.has_api_key,
    testing: c.testing,
    enable_thinking: null,
    cfg: c,
  })),
])

const hasRows = computed(() => tableRows.value.length > 0)

// ============================================================
// 加载:catalog + 用户已存配置
// ============================================================

onMounted(async () => {
  await Promise.all([loadCatalog(), loadConfig()])
})

async function loadCatalog(): Promise<void> {
  loadingCatalog.value = true
  try {
    catalog.value = await getCatalog()
  } catch (e) {
    showToast(`厂商清单加载失败: ${extractErrorMessage(e)}`, 'error')
  } finally {
    loadingCatalog.value = false
  }
}

async function loadConfig(): Promise<void> {
  loadingConfig.value = true
  try {
    const res = await getMyModels()
    llmConfigs.splice(0, llmConfigs.length, ...res.llm_configs.map(llmFromOut))
    embConfigs.splice(0, embConfigs.length, ...res.embedding_configs.map(embFromOut))
  } catch (e) {
    showToast(`配置加载失败: ${extractErrorMessage(e)}`, 'error')
  } finally {
    loadingConfig.value = false
  }
}

function llmFromOut(out: LLMConfigItemOut): LLMConfigEditable {
  return {
    id: out.id,
    name: out.name,
    provider: out.provider,
    api_key: '', // 空串=保留已存
    model: out.model,
    enable_thinking: out.enable_thinking,
    base_url: out.base_url,
    max_output_tokens: out.max_output_tokens,
    has_api_key: out.has_api_key,
    testing: false,
  }
}

function embFromOut(out: EmbeddingConfigItemOut): EmbeddingConfigEditable {
  return {
    id: out.id,
    name: out.name,
    provider: out.provider,
    api_key: '',
    model: out.model,
    base_url: out.base_url,
    dimension: out.dimension,
    has_api_key: out.has_api_key,
    testing: false,
  }
}

// ============================================================
// 弹窗:打开(新增 / 编辑)
// ============================================================

function openAddDialog(kind: Kind): void {
  dialogKind.value = kind
  dialogMode.value = 'add'
  dialogInitial.value = null
  dialogOpen.value = true
}

function openEditLlm(cfg: LLMConfigEditable): void {
  dialogKind.value = 'llm'
  dialogMode.value = 'edit'
  dialogInitial.value = {
    id: cfg.id,
    name: cfg.name,
    provider: cfg.provider,
    api_key: '',
    model: cfg.model,
    enable_thinking: cfg.enable_thinking,
    base_url: cfg.base_url,
    max_output_tokens: cfg.max_output_tokens,
    has_api_key: cfg.has_api_key,
  }
  dialogOpen.value = true
}

function openEditEmb(cfg: EmbeddingConfigEditable): void {
  dialogKind.value = 'embedding'
  dialogMode.value = 'edit'
  dialogInitial.value = {
    id: cfg.id,
    name: cfg.name,
    provider: cfg.provider,
    api_key: '',
    model: cfg.model,
    base_url: cfg.base_url,
    dimension: cfg.dimension,
    has_api_key: cfg.has_api_key,
  }
  dialogOpen.value = true
}

function openEditRow(row: TableRow): void {
  if (row.kind === 'llm') openEditLlm(row.cfg as LLMConfigEditable)
  else openEditEmb(row.cfg as EmbeddingConfigEditable)
}

// 弹窗确认:写回列表 → 立即保存 → 成功则关闭
async function handleDialogConfirm(payload: {
  kind: Kind
  config: LLMConfigItem | EmbeddingConfigItem
}): Promise<void> {
  const { kind, config } = payload
  if (kind === 'llm') {
    const c = config as LLMConfigItem
    const idx = llmConfigs.findIndex((x) => x.id === c.id)
    if (idx >= 0) {
      Object.assign(llmConfigs[idx], c)
    } else {
      llmConfigs.push({ ...c, has_api_key: false, testing: false })
    }
  } else {
    const c = config as EmbeddingConfigItem
    const idx = embConfigs.findIndex((x) => x.id === c.id)
    if (idx >= 0) {
      Object.assign(embConfigs[idx], c)
    } else {
      embConfigs.push({ ...c, has_api_key: false, testing: false })
    }
  }
  const ok = await handleSave()
  if (ok) dialogOpen.value = false
}

// ============================================================
// 删除(立即保存)
// ============================================================

async function removeRow(row: TableRow): Promise<void> {
  if (row.kind === 'llm') {
    const idx = llmConfigs.findIndex((x) => x.id === row.id)
    if (idx >= 0) llmConfigs.splice(idx, 1)
  } else {
    const idx = embConfigs.findIndex((x) => x.id === row.id)
    if (idx >= 0) embConfigs.splice(idx, 1)
  }
  await handleSave()
}

// ============================================================
// 保存(整体提交)
// ============================================================

async function handleSave(opts?: { silent?: boolean }): Promise<boolean> {
  saving.value = true
  toast.value = null
  try {
    for (const cfg of llmConfigs) {
      if (!cfg.provider || !cfg.model) {
        showToast(`LLM 配置"${cfg.name || '未命名'}"缺少厂商或模型`, 'error')
        return false
      }
    }
    for (const cfg of embConfigs) {
      if (!cfg.provider || !cfg.model) {
        showToast(`Embedding 配置"${cfg.name || '未命名'}"缺少厂商或模型`, 'error')
        return false
      }
    }

    const res = await saveModels({
      llm_configs: llmConfigs.map((c) => ({
        id: c.id,
        name: c.name,
        provider: c.provider,
        api_key: c.api_key,
        model: c.model,
        enable_thinking: c.enable_thinking,
        base_url: c.base_url,
        max_output_tokens: c.max_output_tokens,
      })),
      embedding_configs: embConfigs.map((c) => ({
        id: c.id,
        name: c.name,
        provider: c.provider,
        api_key: c.api_key,
        model: c.model,
        base_url: c.base_url,
        dimension: c.dimension,
      })),
    })
    // 更新现有对象字段（保留 testing 状态和引用）
    for (const out of res.llm_configs) {
      const existing = llmConfigs.find((c) => c.id === out.id)
      if (existing) {
        const testing = existing.testing
        Object.assign(existing, llmFromOut(out))
        existing.testing = testing
      }
    }
    for (const out of res.embedding_configs) {
      const existing = embConfigs.find((c) => c.id === out.id)
      if (existing) {
        const testing = existing.testing
        Object.assign(existing, embFromOut(out))
        existing.testing = testing
      }
    }
    if (!opts?.silent) showToast('设置已保存', 'success')
    // 列表是各设置页模型下拉的选项来源,改动后让共享缓存重新拉
    invalidateLlmConfigs()
    return true
  } catch (e) {
    showToast(`保存失败: ${extractErrorMessage(e)}`, 'error')
    return false
  } finally {
    saving.value = false
  }
}

// ============================================================
// 测试(先保存,再按 config_id 测试)
// ============================================================

async function handleTestRow(row: TableRow): Promise<void> {
  if (row.kind === 'llm') {
    const cfg = row.cfg as LLMConfigEditable
    cfg.testing = true
    toast.value = null
    try {
      const ok = await handleSave({ silent: true })
      if (!ok) return
      const res = await testLLM({ config_id: cfg.id })
      if (res.success) {
        const reply = res.reply ? `\n「${res.reply}」` : ''
        showToast(`"${cfg.name || cfg.model}" 测试成功 · ${res.latency_ms ?? '?'}ms${reply}`, 'success')
      } else {
        showToast(`"${cfg.name || cfg.model}" 测试失败: ${res.message}`, 'error')
      }
    } catch (e) {
      showToast(`测试异常: ${extractErrorMessage(e)}`, 'error')
    } finally {
      await stopTestingWithDelay(cfg)
    }
  } else {
    const cfg = row.cfg as EmbeddingConfigEditable
    cfg.testing = true
    toast.value = null
    try {
      const ok = await handleSave({ silent: true })
      if (!ok) return
      const res = await testEmbedding({ config_id: cfg.id })
      if (res.success) {
        showToast(
          `"${cfg.name || cfg.model}" 测试成功 · 维度 ${res.dimension ?? '?'} · ${res.latency_ms ?? '?'}ms`,
          'success',
        )
      } else {
        showToast(`"${cfg.name || cfg.model}" 测试失败: ${res.message}`, 'error')
      }
    } catch (e) {
      showToast(`测试异常: ${extractErrorMessage(e)}`, 'error')
    } finally {
      await stopTestingWithDelay(cfg)
    }
  }
}

/**
 * 延迟停止测试动画，确保 toast 弹窗已开始滑入动画后再停止旋转
 */
async function stopTestingWithDelay(cfg: LLMConfigEditable | EmbeddingConfigEditable): Promise<void> {
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 200))
  cfg.testing = false
}

// ============================================================
// 工具
// ============================================================

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 5000)
}

function configTitle(name: string, model: string): string {
  return name || model || '未命名配置'
}

function providerLabel(row: TableRow): string {
  return row.provider || '—'
}

function modelLabel(row: TableRow): string {
  return row.model || '—'
}
</script>

<template>
  <div class="panel">
    <!-- 加载中 -->
    <div v-if="loadingCatalog || loadingConfig" class="loading">
      <div class="spinner" />
      <span>加载中...</span>
    </div>

    <template v-else>
      <!-- 页头 + 添加按钮 -->
      <div class="page-header">
        <div>
          <h1>我的模型</h1>
        </div>
        <div class="header-actions">
          <button class="btn-add" :disabled="saving" @click="openAddDialog('llm')">
            + 添加 LLM
          </button>
          <button class="btn-add" :disabled="saving" @click="openAddDialog('embedding')">
            + 添加 Embedding
          </button>
        </div>
      </div>

      <!-- ============ 统一表格 ============ -->
      <section class="table-section">
        <div class="table-wrap">
          <table class="config-table">
            <thead>
              <tr>
                <th class="col-name">名称</th>
                <th class="col-type">类型</th>
                <th class="col-provider">厂商</th>
                <th class="col-model">模型</th>
                <th class="col-key">Key</th>
                <th class="col-thinking">思考</th>
                <th class="col-actions">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!hasRows" class="empty-row">
                <td colspan="7">
                  <div class="empty-hint">
                    尚未配置任何模型,点击右上角「+ 添加」新增
                  </div>
                </td>
              </tr>

              <tr
                v-for="row in tableRows"
                :key="`${row.kind}-${row.id}`"
                class="data-row"
                @click="openEditRow(row)"
              >
                <td class="col-name">
                  <span class="cell-title">{{ configTitle(row.name, row.model) }}</span>
                </td>
                <td class="col-type">
                  <span :class="['type-tag', row.kind === 'llm' ? 'tag-llm' : 'tag-emb']">
                    {{ row.kind === 'llm' ? 'LLM' : 'Embedding' }}
                  </span>
                </td>
                <td class="col-provider">
                  <span class="cell-mono">{{ providerLabel(row) }}</span>
                </td>
                <td class="col-model">
                  <span class="cell-mono">{{ modelLabel(row) }}</span>
                </td>
                <td class="col-key">
                  <span v-if="row.has_api_key" class="badge badge-ok">已配置</span>
                  <span v-else class="badge badge-warn">未配置</span>
                </td>
                <td class="col-thinking">
                  <span v-if="row.enable_thinking === null" class="thinking-na">—</span>
                  <span v-else-if="row.enable_thinking" class="badge badge-thinking-on">开启</span>
                  <span v-else class="badge badge-thinking-off">关闭</span>
                </td>
                <td class="col-actions" @click.stop>
                  <div class="row-actions">
                    <button
                      class="btn-icon"
                      :class="{ 'is-testing': row.testing }"
                      :title="row.testing ? '测试中...' : '测试连通性'"
                      :disabled="row.testing || saving"
                      @click="handleTestRow(row)"
                    >
                      <svg
                        class="icon-activity"
                        :class="{ 'icon-spin': row.testing }"
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
                        <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
                      </svg>
                    </button>
                    <button
                      class="btn-icon"
                      title="编辑"
                      :disabled="saving"
                      @click="openEditRow(row)"
                    >
                      <svg
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
                        <path d="M12 20h9" />
                        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
                      </svg>
                    </button>
                    <button
                      class="btn-icon btn-danger"
                      title="删除"
                      :disabled="saving"
                      @click="removeRow(row)"
                    >✕</button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- ============ 弹窗 ============ -->
      <ModelConfigDialog
        :open="dialogOpen"
        :kind="dialogKind"
        :mode="dialogMode"
        :initial="dialogInitial"
        :catalog="catalog"
        :saving="saving"
        @confirm="handleDialogConfirm"
        @cancel="dialogOpen = false"
      />
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
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
            <svg
              v-else
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
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
  max-width: 920px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-6) var(--space-12);
}

.loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
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

.icon-spin {
  animation: spin 0.9s linear infinite;
  transform-origin: center;
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-6);
}

@media (max-width: 640px) {
  .panel {
    padding: var(--space-4) var(--space-3) var(--space-8);
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

.header-actions {
  display: flex;
  gap: var(--space-2);
  flex-shrink: 0;
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

/* ---- 表格区 ---- */
.table-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.table-wrap {
  overflow-x: auto;
}

.config-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}

.config-table thead th {
  text-align: left;
  padding: var(--space-3) var(--space-4);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}

.col-name { width: 24%; }
.col-type { width: 100px; text-align: center; }
.col-provider { width: 15%; }
.col-model { width: 20%; }
.col-key { width: 80px; text-align: center; }
.col-thinking { width: 70px; text-align: center; }
.col-actions { width: 120px; text-align: right; }

.config-table thead th.col-type,
.config-table thead th.col-key,
.config-table thead th.col-thinking,
.config-table tbody td.col-type,
.config-table tbody td.col-key,
.config-table tbody td.col-thinking {
  text-align: center;
}

.config-table tbody td {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  vertical-align: middle;
}

.config-table tbody tr:last-child td {
  border-bottom: none;
}

.data-row {
  cursor: pointer;
  transition: background var(--transition-fast);
}

.data-row:hover {
  background: var(--color-surface-alt);
}

/* ---- 单元格内容 ---- */
.cell-title {
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-mono {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 类型标签 ---- */
.type-tag {
  display: inline-block;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  padding: 2px 8px;
  border-radius: var(--radius-full);
  letter-spacing: 0.02em;
}

.tag-llm {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid var(--color-primary-border);
}

.tag-emb {
  background: var(--color-info-light);
  color: var(--color-info);
  border: 1px solid #bfdbfe;
}

/* ---- 徽标 ---- */
.badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  font-weight: var(--fw-medium);
  white-space: nowrap;
}

.badge-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.badge-warn {
  background: #fef3c7;
  color: #92400e;
}

.badge-thinking-on {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.badge-thinking-off {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.thinking-na {
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

/* ---- 空状态 ---- */
.empty-row td {
  padding: 0;
}

.empty-hint {
  padding: var(--space-10);
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

/* ---- 操作列 ---- */
.row-actions {
  display: flex;
  gap: 4px;
  justify-content: flex-end;
}

/* ---- 按钮 ---- */
.btn-add {
  height: 32px;
  padding: 0 var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.btn-add:hover:not(:disabled) {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-add:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-icon {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 13px;
  color: var(--color-text-secondary);
  transition: all var(--transition-fast);
}

.btn-icon:hover:not(:disabled) {
  background: var(--color-surface);
  color: var(--color-text);
}

.btn-icon:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger-light);
  color: var(--color-danger);
}
</style>
