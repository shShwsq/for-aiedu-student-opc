<script setup lang="ts">
/**
 * 记忆设置面板(嵌套在 SettingsLayout 内)
 *
 * 控制「任务完成后自动归纳写入长期记忆」的行为,各项切换/选中即保存、全局生效:
 * - 自动归纳开关:关闭后任务完成不再写记忆(用户手改记忆不受影响)
 * - 结构化预设模式:structured(自定义类别)/ freeform(自由叙述)
 * - 结构化类别(仅 structured):表格编辑项目/全局各自的类别标题+描述,可增删/上下移/恢复系统默认
 * - 记忆归纳模型:归纳/精简专用模型(独立于任务自带模型;未选则回退系统默认)
 * - 归纳思考模式:覆盖归纳/精简模型的思考开关(跟随配置/强制开/强制关)
 * - 注入精简上限:精简版记忆注入 system prompt 的字符上限(超出走 LLM 精简/截断)
 *
 * (复用练习设置面板同款开关/单选/下拉样式;数据链路走 PUT /memory/preferences/memory_settings)
 */
import { computed, onMounted, ref } from 'vue'

import { getPreferences, getStructureDefaults, saveMemorySettings } from '@/api/memory'
import { getMyModels } from '@/api/model_configs'
import { extractErrorMessage } from '@/utils/error'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type {
  MemoryCategoryDef,
  MemoryStructureMode,
  PracticeThinkingMode,
} from '@/types/memory'

// ============================================================
// 状态
// ============================================================
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const busy = computed(() => saving.value)

/** 自动归纳总开关 */
const memoryEnabled = ref(true)
/** 结构化预设模式 */
const structureMode = ref<MemoryStructureMode>('structured')
/** 归纳/精简思考模式 */
const thinkingMode = ref<PracticeThinkingMode>('follow')
/** 记忆归纳模型配置 id(空串=跟随系统默认) */
const curatorModelId = ref('')
/** 注入精简字符上限 */
const injectMaxChars = ref(2000)
/** 结构化类别(项目/全局各一套;本地编辑,点「保存类别」提交) */
const projectCats = ref<MemoryCategoryDef[]>([])
const globalCats = ref<MemoryCategoryDef[]>([])
/** 系统默认类别(一次性拉取;供对照展示与「恢复系统默认」) */
const defaultProjectCats = ref<MemoryCategoryDef[]>([])
const defaultGlobalCats = ref<MemoryCategoryDef[]>([])
/** 用户已保存的 LLM 配置列表(下拉选项来源) */
const llmConfigs = ref<LLMConfigItemOut[]>([])

/** 顶部居中 toast */
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)
function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4000)
}

const STRUCTURE_OPTIONS: Array<{ value: MemoryStructureMode; label: string; desc: string }> = [
  {
    value: 'structured',
    label: '结构化模式',
    desc: '按自定义类别(标题+描述)分类归纳、去重合并;类别按顺序即优先级',
  },
  {
    value: 'freeform',
    label: '自由叙述',
    desc: '归纳为一段连贯纯文本笔记,不做类别去重(多次归纳可能语义重复)',
  },
]

const THINKING_OPTIONS: Array<{ value: PracticeThinkingMode; label: string; desc: string }> = [
  { value: 'follow', label: '跟随模型配置', desc: '使用归纳模型配置自身的思考开关(默认)' },
  { value: 'on', label: '强制开启', desc: '归纳更慢,质量可能更高' },
  { value: 'off', label: '强制关闭', desc: '归纳更快(推荐:归纳是轻量任务)' },
]

// ============================================================
// 加载
// ============================================================
async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const pref = await getPreferences()
    const ms = pref.memory_settings
    memoryEnabled.value = ms.memory_enabled
    structureMode.value = ms.structure_mode
    thinkingMode.value = ms.thinking_mode
    curatorModelId.value = ms.curator_llm_config_id ?? ''
    injectMaxChars.value = ms.inject_max_chars
    projectCats.value = ms.project_categories.map((c) => ({ ...c }))
    globalCats.value = ms.global_categories.map((c) => ({ ...c }))
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
  try {
    const models = await getMyModels()
    llmConfigs.value = models.llm_configs
  } catch {
    // 静默失败,下拉只展示「跟随系统默认」
  }
  try {
    const defs = await getStructureDefaults()
    defaultProjectCats.value = defs.project_categories.map((c) => ({ ...c }))
    defaultGlobalCats.value = defs.global_categories.map((c) => ({ ...c }))
  } catch {
    // 静默失败：无默认数据时「恢复系统默认」不可用，不影响主流程
  }
}

// ============================================================
// 即存即改:切换/选中立即调 API 持久化并 toast
// ============================================================
async function persist(payload: Record<string, unknown>, okMsg: string): Promise<boolean> {
  saving.value = true
  try {
    const latest = await saveMemorySettings({
      memory_enabled: memoryEnabled.value,
      ...payload,
    })
    const ms = latest.memory_settings
    if (ms) {
      memoryEnabled.value = ms.memory_enabled
      structureMode.value = ms.structure_mode
      thinkingMode.value = ms.thinking_mode
      curatorModelId.value = ms.curator_llm_config_id ?? ''
      injectMaxChars.value = ms.inject_max_chars
    }
    showToast(okMsg, 'success')
    return true
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
    return false
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(): Promise<void> {
  if (busy.value) return
  const next = !memoryEnabled.value
  memoryEnabled.value = next
  const ok = await persist(
    { memory_enabled: next },
    next ? '已开启任务完成后自动归纳记忆' : '已关闭自动归纳记忆(手改记忆不受影响)',
  )
  if (!ok) memoryEnabled.value = !next
}

async function selectStructure(mode: MemoryStructureMode): Promise<void> {
  if (busy.value || mode === structureMode.value) return
  const prev = structureMode.value
  structureMode.value = mode
  const label = STRUCTURE_OPTIONS.find((o) => o.value === mode)?.label ?? mode
  if (!(await persist({ structure_mode: mode }, `记忆结构化模式已切换为「${label}」`))) {
    structureMode.value = prev
  }
}

async function selectThinking(mode: PracticeThinkingMode): Promise<void> {
  if (busy.value || mode === thinkingMode.value) return
  const prev = thinkingMode.value
  thinkingMode.value = mode
  const label = THINKING_OPTIONS.find((o) => o.value === mode)?.label ?? mode
  if (!(await persist({ thinking_mode: mode }, `归纳思考模式已切换为「${label}」`))) {
    thinkingMode.value = prev
  }
}

async function selectModel(event: Event): Promise<void> {
  const value = (event.target as HTMLSelectElement).value
  if (busy.value || value === curatorModelId.value) return
  const prev = curatorModelId.value
  curatorModelId.value = value
  if (
    !(await persist(
      { curator_llm_config_id: value },
      value ? '记忆归纳模型已更新' : '记忆归纳模型已重置为跟随系统默认',
    ))
  ) {
    curatorModelId.value = prev
  }
}

async function commitInjectMax(event: Event): Promise<void> {
  const raw = Number((event.target as HTMLInputElement).value)
  if (busy.value || Number.isNaN(raw)) return
  const clamped = Math.min(8000, Math.max(200, Math.round(raw)))
  if (clamped === injectMaxChars.value) return
  const prev = injectMaxChars.value
  injectMaxChars.value = clamped
  if (!(await persist({ inject_max_chars: clamped }, `注入精简上限已设为 ${clamped} 字符`))) {
    injectMaxChars.value = prev
  }
}

function catList(scope: 'project' | 'global') {
  return scope === 'project' ? projectCats : globalCats
}
const MAX_CATS = 12

function addCat(scope: 'project' | 'global'): void {
  if (busy.value) return
  const list = catList(scope)
  if (list.value.length >= MAX_CATS) {
    showToast(`类别数量上限 ${MAX_CATS} 个`, 'error')
    return
  }
  list.value.push({ title: '', description: '' })
}

function removeCat(scope: 'project' | 'global', idx: number): void {
  if (busy.value) return
  catList(scope).value.splice(idx, 1)
}

function restoreCategories(scope: 'project' | 'global'): void {
  if (busy.value) return
  const defaults = scope === 'project' ? defaultProjectCats : defaultGlobalCats
  if (!defaults.value.length) {
    showToast('系统默认类别尚未加载', 'error')
    return
  }
  catList(scope).value = defaults.value.map((c) => ({ ...c }))
}

function moveCat(scope: 'project' | 'global', idx: number, dir: -1 | 1): void {
  if (busy.value) return
  const list = catList(scope)
  const j = idx + dir
  if (j < 0 || j >= list.value.length) return
  const arr = list.value.slice()
  const t = arr[idx]
  arr[idx] = arr[j]
  arr[j] = t
  list.value = arr
}

async function saveCategories(): Promise<void> {
  if (busy.value) return
  saving.value = true
  try {
    const latest = await saveMemorySettings({
      memory_enabled: memoryEnabled.value,
      project_categories: projectCats.value,
      global_categories: globalCats.value,
    })
    const ms = latest.memory_settings
    if (ms) {
      projectCats.value = ms.project_categories.map((c) => ({ ...c }))
      globalCats.value = ms.global_categories.map((c) => ({ ...c }))
    }
    showToast('结构化类别已保存', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="page-header">
      <div>
        <h1>记忆设置</h1>
        <p class="page-subtitle">
          控制任务完成后自动归纳写入长期记忆的行为。切换即保存,全局生效。
        </p>
      </div>
    </div>

    <div v-if="loading" class="loading-box">
      <span class="status-spinner" aria-label="加载中" />
      <span>正在加载记忆设置…</span>
    </div>

    <div v-else-if="loadError" class="alert alert-error" role="alert">
      <span>加载失败:{{ loadError }}</span>
      <button class="btn-link" @click="load">重试</button>
    </div>

    <section v-else class="memory-form">
      <!-- 自动归纳开关 -->
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-title">任务完成后自动归纳记忆</span>
          <span class="setting-desc">
            审计任务成功完成后,自动把可复用的经验归纳写入项目/全局长期记忆;
            关闭后仅停止自动写入,已保存记忆与手动编辑不受影响
          </span>
        </div>
        <button
          type="button"
          role="switch"
          :aria-checked="memoryEnabled"
          :class="['switch', { 'switch-on': memoryEnabled }]"
          :disabled="busy"
          @click="toggleEnabled"
        >
          <span class="switch-thumb" />
        </button>
      </div>

      <!-- 结构化模式 -->
      <div class="setting-block">
        <span class="setting-title">记忆结构化模式</span>
        <span class="setting-desc">
          决定归纳结果的形态。切换模式不会丢失历史记忆(旧内容原样保留)
        </span>
        <div class="option-list" role="radiogroup" aria-label="记忆结构化模式">
          <button
            v-for="opt in STRUCTURE_OPTIONS"
            :key="opt.value"
            type="button"
            role="radio"
            :aria-checked="structureMode === opt.value"
            :class="['option', { 'option-active': structureMode === opt.value }]"
            :disabled="busy"
            @click="selectStructure(opt.value)"
          >
            <span class="option-radio">
              <span v-if="structureMode === opt.value" class="option-radio-dot" />
            </span>
            <span class="option-text">
              <span class="option-label">{{ opt.label }}</span>
              <span class="option-desc">{{ opt.desc }}</span>
            </span>
          </button>
        </div>
      </div>

      <!-- 结构化类别编辑(仅结构化模式;表格展示标题/描述) -->
      <div v-if="structureMode === 'structured'" class="setting-block">
        <span class="setting-title">结构化类别</span>
        <span class="setting-desc">
          自定义分类标题与描述(描述用于指导归纳模型归类)。类别按表格行序即优先级，
          最后一行作为无法归类条目的杂项桶。修改后需点「保存类别」生效。
        </span>

        <div class="cat-groups">
          <div class="cat-group">
            <div class="cat-group-head">
              <span class="cat-group-title">项目记忆类别</span>
              <div class="cat-group-tools">
                <button class="btn-plain" :disabled="busy || !defaultProjectCats.length" @click="restoreCategories('project')">恢复系统默认</button>
                <button class="btn-plain" :disabled="busy" @click="addCat('project')">+ 添加</button>
              </div>
            </div>
            <table class="cat-table">
              <thead>
                <tr>
                  <th class="col-title">标题</th>
                  <th class="col-desc">描述</th>
                  <th class="col-ops">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(c, i) in projectCats" :key="'p' + i">
                  <td class="col-title">
                    <input v-model="c.title" class="cat-input" type="text" maxlength="64" placeholder="标题" :disabled="busy">
                  </td>
                  <td class="col-desc">
                    <input v-model="c.description" class="cat-input" type="text" maxlength="300" placeholder="描述(选填)" :disabled="busy">
                  </td>
                  <td class="col-ops">
                    <div class="cat-actions">
                      <button class="btn-plain" :disabled="busy || i === 0" title="上移" @click="moveCat('project', i, -1)">↑</button>
                      <button class="btn-plain" :disabled="busy || i === projectCats.length - 1" title="下移" @click="moveCat('project', i, 1)">↓</button>
                      <button class="btn-danger" :disabled="busy" title="删除" @click="removeCat('project', i)">✕</button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!projectCats.length" class="cat-empty">无类别，保存时会自动回退内置默认</p>
          </div>

          <div class="cat-group">
            <div class="cat-group-head">
              <span class="cat-group-title">全局记忆类别</span>
              <div class="cat-group-tools">
                <button class="btn-plain" :disabled="busy || !defaultGlobalCats.length" @click="restoreCategories('global')">恢复系统默认</button>
                <button class="btn-plain" :disabled="busy" @click="addCat('global')">+ 添加</button>
              </div>
            </div>
            <table class="cat-table">
              <thead>
                <tr>
                  <th class="col-title">标题</th>
                  <th class="col-desc">描述</th>
                  <th class="col-ops">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(c, i) in globalCats" :key="'g' + i">
                  <td class="col-title">
                    <input v-model="c.title" class="cat-input" type="text" maxlength="64" placeholder="标题" :disabled="busy">
                  </td>
                  <td class="col-desc">
                    <input v-model="c.description" class="cat-input" type="text" maxlength="300" placeholder="描述(选填)" :disabled="busy">
                  </td>
                  <td class="col-ops">
                    <div class="cat-actions">
                      <button class="btn-plain" :disabled="busy || i === 0" title="上移" @click="moveCat('global', i, -1)">↑</button>
                      <button class="btn-plain" :disabled="busy || i === globalCats.length - 1" title="下移" @click="moveCat('global', i, 1)">↓</button>
                      <button class="btn-danger" :disabled="busy" title="删除" @click="removeCat('global', i)">✕</button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!globalCats.length" class="cat-empty">无类别，保存时会自动回退内置默认</p>
          </div>
        </div>

        <button class="btn-primary cat-save-btn" :disabled="busy" @click="saveCategories">保存类别</button>
      </div>

      <!-- 归纳模型 -->
      <div class="setting-block">
        <span class="setting-title">记忆归纳模型</span>
        <span class="setting-desc">
          归纳与精简记忆时使用的专用模型,独立于任务自带模型(归纳是轻量任务,可选便宜快速的模型);
          选「跟随系统默认」则用环境配置的模型
        </span>
        <select
          class="model-select"
          aria-label="记忆归纳模型"
          :value="curatorModelId"
          :disabled="busy"
          @change="selectModel"
        >
          <option value="">跟随系统默认</option>
          <option v-for="cfg in llmConfigs" :key="cfg.id" :value="cfg.id">
            {{ cfg.name }}({{ cfg.provider }} / {{ cfg.model }})
          </option>
        </select>
        <span v-if="!llmConfigs.length" class="model-hint">
          暂无已保存的模型配置,可先到「模型设置」中添加
        </span>
      </div>

      <!-- 思考模式 -->
      <div class="setting-block">
        <span class="setting-title">归纳思考模式</span>
        <span class="setting-desc">
          是否让归纳模型开启思考(慢想)模式;关闭更快,开启质量可能更高
        </span>
        <div class="option-list" role="radiogroup" aria-label="归纳思考模式">
          <button
            v-for="opt in THINKING_OPTIONS"
            :key="opt.value"
            type="button"
            role="radio"
            :aria-checked="thinkingMode === opt.value"
            :class="['option', { 'option-active': thinkingMode === opt.value }]"
            :disabled="busy"
            @click="selectThinking(opt.value)"
          >
            <span class="option-radio">
              <span v-if="thinkingMode === opt.value" class="option-radio-dot" />
            </span>
            <span class="option-text">
              <span class="option-label">{{ opt.label }}</span>
              <span class="option-desc">{{ opt.desc }}</span>
            </span>
          </button>
        </div>
      </div>

      <!-- 注入精简上限 -->
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-title">注入精简上限(字符)</span>
          <span class="setting-desc">
            精简版项目记忆注入 system prompt 的字符上限,超出则调用模型精简(200–8000)
          </span>
        </div>
        <input
          class="number-input"
          type="number"
          min="200"
          max="8000"
          step="100"
          :value="injectMaxChars"
          :disabled="busy"
          aria-label="注入精简上限字符"
          @change="commitInjectMax"
        >
      </div>

      <p v-if="saving" class="saving-hint"><span class="btn-spinner" /> 保存中...</p>
    </section>

    <Teleport to="body">
      <Transition name="toast-slide">
        <div
          v-if="toast"
          :class="['toast-popup', toast.type === 'error' ? 'toast-error' : 'toast-success']"
          role="status"
          aria-live="polite"
        >
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

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
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

.memory-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.setting-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.setting-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.setting-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.setting-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.setting-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

/* ---- 单选列表 ---- */
.option-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.option {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}

.option:hover:not(:disabled) {
  border-color: var(--color-border-strong);
}

.option:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.option-active {
  border-color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}

.option-radio {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  border: 2px solid var(--color-border-strong);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.option-active .option-radio {
  border-color: var(--color-primary);
}

.option-radio-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
}

.option-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.option-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.option-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

/* ---- 模型下拉 ---- */
.model-select {
  width: 100%;
  height: 38px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.model-select:hover:not(:disabled) {
  border-color: var(--color-border-strong);
}

.model-select:focus {
  outline: none;
  border-color: var(--color-primary);
}

.model-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.model-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ---- 数字输入 ---- */
.number-input {
  flex-shrink: 0;
  width: 108px;
  height: 34px;
  padding: 0 var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  outline: none;
  transition: border-color var(--transition-fast);
}

.number-input:focus {
  border-color: var(--color-primary);
}

.number-input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* ---- 开关 ---- */
.switch {
  flex-shrink: 0;
  position: relative;
  width: 40px;
  height: 22px;
  margin-top: 2px;
  border: none;
  border-radius: var(--radius-full);
  background: var(--color-border-strong);
  cursor: pointer;
  padding: 0;
  transition: background var(--transition-fast);
}

.switch-on {
  background: var(--color-primary);
}

.switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.switch-thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: transform var(--transition-fast);
}

.switch-on .switch-thumb {
  transform: translateX(18px);
}

.saving-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
  border-top-color: currentColor;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
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

/* ---- 结构化类别编辑 ---- */
.cat-groups {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.cat-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.cat-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.cat-group-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.cat-group-tools {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.cat-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}

.cat-table th {
  text-align: left;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  border-bottom: 1px solid var(--color-border);
}

.cat-table td {
  padding: var(--space-1) var(--space-2);
  vertical-align: middle;
  border-bottom: 1px solid var(--color-border);
}

.cat-table .col-title {
  width: 26%;
}

.cat-table .col-desc {
  width: auto;
}

.cat-table .col-ops {
  width: 96px;
  text-align: right;
}

.cat-table .col-ops .cat-actions {
  justify-content: flex-end;
}

.cat-input {
  width: 100%;
  height: 32px;
  padding: 0 var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  outline: none;
  transition: border-color var(--transition-fast);
}

.cat-input:focus {
  border-color: var(--color-primary);
}

.cat-input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.cat-actions {
  display: flex;
  gap: var(--space-1);
}

.cat-empty {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.cat-save-btn {
  align-self: flex-start;
  margin-top: var(--space-1);
}

.btn-primary {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: #fff;
  background: var(--color-primary);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: opacity var(--transition-fast);
}

.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-plain {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-plain:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-plain:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-danger {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-danger);
  background: transparent;
  border: 1px solid color-mix(in srgb, var(--color-danger) 50%, transparent);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--color-danger) 10%, transparent);
}

.btn-danger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 640px) {
  .panel {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  .setting-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
