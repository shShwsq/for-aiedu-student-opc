<script setup lang="ts">
/**
 * 练习设置面板(嵌套在 SettingsLayout 内)
 *
 * 自适应练习的出题与复习偏好,四项设置均为全局生效、切换/选中即保存:
 * - 自动生成练习题开关(产出的候选题仍需在任务详情页预览确认才入库)
 * - 出题前恢复工作区:沙箱已清理时重新 clone 仓库,供出题时查阅源码
 * - 出题思考模式:覆盖出题模型的思考开关(跟随配置/强制开/强制关)
 * - 默认出题模型:用户级默认(任务级配置优先,未设置则回退 env 默认);
 *   可开「始终用默认出题模型」忽略任务级配置
 * - 学习主题管理:内置 4 个(安全/架构/编码/合同,可停用)+ 自定义增删改;
 *   停用的主题不再出新题(存量不动)
 * 另含危险操作:清空练习记录 / 清空全部数据(均二次确认,不可逆)。
 *
 * (由练习页右上角弹窗迁移而来;练习页入口改为跳转本面板)
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { getPreferences, savePracticeSettings } from '@/api/memory'
import { getMyModels } from '@/api/model_configs'
import {
  clearPracticeRecords,
  createLearningTopic,
  deleteLearningTopic,
  listLearningTopics,
  updateLearningTopic,
} from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { PracticeThinkingMode } from '@/types/memory'
import type { LearningTopicDef } from '@/types/practice'

// ============================================================
// 状态
// ============================================================
const route = useRoute()
const loading = ref(true)
const loadError = ref('')
/** 保存中(切换开关/思考模式/模型时) */
const saving = ref(false)
/** 清空中(危险操作) */
const clearing = ref(false)

/** 自动生成练习题开关 */
const autoGenerate = ref(true)
/** 出题前恢复工作区开关 */
const restoreWorkspace = ref(false)
/** 出题思考模式(follow=跟随模型配置/on=强制开/off=强制关) */
const thinkingMode = ref<PracticeThinkingMode>('follow')
/** 默认出题模型配置 id(空串=跟随系统默认) */
const defaultModelId = ref('')
/** 始终用默认出题模型(忽略任务自带模型配置) */
const forceDefaultLlm = ref(false)
/** 用户已保存的 LLM 配置列表(默认出题模型下拉选项来源) */
const llmConfigs = ref<LLMConfigItemOut[]>([])

/** 保存或清空中:统一禁用全部交互 */
const busy = computed(() => saving.value || clearing.value)

// ---- 危险操作:展开的确认态 ----
/** 当前展开的确认:none / 清空练习记录 / 清空全部数据 */
const confirmMode = ref<'none' | 'records' | 'all'>('none')
/** 清空全部数据的输入确认(输入「清空」后方可执行) */
const confirmText = ref('')

/** 顶部居中 toast */
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4000)
}

/** 思考模式选项(与后端 THINKING_MODES 对齐) */
const THINKING_OPTIONS: Array<{ value: PracticeThinkingMode; label: string; desc: string }> = [
  { value: 'follow', label: '跟随模型配置', desc: '使用出题模型配置自身的思考开关(默认)' },
  { value: 'on', label: '强制开启', desc: '出题更慢,题目质量可能更高' },
  { value: 'off', label: '强制关闭', desc: '出题更快;模型思考模式下工具调用异常导致出不出题时可尝试' },
]

// ============================================================
// 学习主题管理(内置 4 个 + 自定义,独立 CRUD,不走 preferences 链路)
// ============================================================
const topics = ref<LearningTopicDef[]>([])
/** 主题操作中(增删改/开关),禁用全部主题交互 */
const topicBusy = ref(false)
/** 新增表单展开态 */
const showCreateForm = ref(false)
const newName = ref('')
const newDesc = ref('')
/** 正在编辑的自定义主题 id(空串=无) */
const editingId = ref('')
const editName = ref('')
const editDesc = ref('')
/** 删除确认中的主题 id(空串=无) */
const deletingId = ref('')

/** 启用主题数(最后一个启用的主题不可停/删,前端预判 + 后端兜底 400) */
const enabledCount = computed(() => topics.value.filter((t) => t.enabled).length)

async function loadTopics(): Promise<void> {
  topicBusy.value = true
  try {
    topics.value = await listLearningTopics()
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    topicBusy.value = false
  }
}

async function toggleTopic(t: LearningTopicDef): Promise<void> {
  if (topicBusy.value) return
  if (t.enabled && enabledCount.value <= 1) {
    showToast('至少需保留一个启用的学习主题', 'error')
    return
  }
  topicBusy.value = true
  try {
    const latest = await updateLearningTopic(t.id, { enabled: !t.enabled })
    Object.assign(t, latest)
    showToast(
      latest.enabled
        ? `已启用「${latest.name}」,新任务的发现将参与该主题出题`
        : `已停用「${latest.name}」,不再出新题(已有题目不受影响)`,
      'success',
    )
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    topicBusy.value = false
  }
}

async function handleCreateTopic(): Promise<void> {
  const name = newName.value.trim()
  if (!name || topicBusy.value) return
  topicBusy.value = true
  try {
    await createLearningTopic({ name, description: newDesc.value.trim() })
    newName.value = ''
    newDesc.value = ''
    showCreateForm.value = false
    await loadTopics()
    showToast('自定义主题已添加,新任务的发现会自动参与该主题分类与出题', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    topicBusy.value = false
  }
}

function startEdit(t: LearningTopicDef): void {
  editingId.value = t.id
  deletingId.value = ''
  editName.value = t.name
  editDesc.value = t.description
}

async function saveEdit(t: LearningTopicDef): Promise<void> {
  const name = editName.value.trim()
  if (!name || topicBusy.value) return
  topicBusy.value = true
  try {
    const latest = await updateLearningTopic(t.id, {
      name,
      description: editDesc.value.trim(),
    })
    Object.assign(t, latest)
    editingId.value = ''
    showToast('主题已更新', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    topicBusy.value = false
  }
}

async function handleDeleteTopic(t: LearningTopicDef): Promise<void> {
  if (topicBusy.value) return
  topicBusy.value = true
  try {
    await deleteLearningTopic(t.id)
    topics.value = topics.value.filter((x) => x.id !== t.id)
    deletingId.value = ''
    showToast(`已删除「${t.name}」`, 'success')
  } catch (err) {
    // 400 详情(如该主题下还有知识点)直接展示
    showToast(extractErrorMessage(err), 'error')
  } finally {
    topicBusy.value = false
  }
}

// ============================================================
// 加载
// ============================================================
async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const pref = await getPreferences()
    autoGenerate.value = pref.auto_generate_practice
    restoreWorkspace.value = pref.restore_workspace_for_practice
    thinkingMode.value = pref.thinking_mode_for_practice
    defaultModelId.value = pref.default_llm_config_id ?? ''
    forceDefaultLlm.value = pref.force_default_llm
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
  loadTopics()
}

// ============================================================
// 即存即改:每项切换/选中立即调 API 持久化并 toast
// ============================================================
async function toggleAuto(): Promise<void> {
  if (busy.value) return
  saving.value = true
  const next = !autoGenerate.value
  try {
    const latest = await savePracticeSettings({ auto_generate_practice: next })
    autoGenerate.value = latest.auto_generate_practice
    showToast(next ? '已开启自动生成练习题' : '已关闭自动生成练习题', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

async function toggleRestore(): Promise<void> {
  if (busy.value) return
  saving.value = true
  const next = !restoreWorkspace.value
  try {
    const latest = await savePracticeSettings({
      auto_generate_practice: autoGenerate.value,
      restore_workspace_for_practice: next,
    })
    restoreWorkspace.value = latest.restore_workspace_for_practice
    showToast(next ? '已开启出题前恢复工作区' : '已关闭出题前恢复工作区', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

async function selectThinking(mode: PracticeThinkingMode): Promise<void> {
  if (busy.value || mode === thinkingMode.value) return
  saving.value = true
  try {
    const latest = await savePracticeSettings({
      auto_generate_practice: autoGenerate.value,
      thinking_mode_for_practice: mode,
    })
    thinkingMode.value = latest.thinking_mode_for_practice
    const label = mode === 'follow' ? '跟随模型配置' : mode === 'on' ? '强制开启' : '强制关闭'
    showToast(`出题思考模式已切换为「${label}」`, 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

async function toggleForceDefault(): Promise<void> {
  if (busy.value) return
  saving.value = true
  const next = !forceDefaultLlm.value
  try {
    const latest = await savePracticeSettings({
      auto_generate_practice: autoGenerate.value,
      force_default_llm: next,
    })
    forceDefaultLlm.value = latest.force_default_llm
    showToast(next ? '已开启「始终用默认出题模型」' : '已关闭「始终用默认出题模型」', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

async function selectModel(event: Event): Promise<void> {
  const value = (event.target as HTMLSelectElement).value
  if (busy.value || value === defaultModelId.value) return
  saving.value = true
  try {
    const latest = await savePracticeSettings({
      auto_generate_practice: autoGenerate.value,
      default_llm_config_id: value,
    })
    defaultModelId.value = latest.default_llm_config_id ?? ''
    showToast(value ? '默认出题模型已更新' : '默认出题模型已重置为跟随系统默认', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

/**
 * 清空练习数据
 *
 * includeQuestions=false:进度归零,保留题库;
 * includeQuestions=true:连题库一并删除。
 */
async function handleClear(includeQuestions: boolean): Promise<void> {
  if (busy.value) return
  clearing.value = true
  try {
    await clearPracticeRecords(includeQuestions)
    confirmMode.value = 'none'
    confirmText.value = ''
    showToast(includeQuestions ? '已清空全部练习数据' : '已清空练习记录', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    clearing.value = false
  }
}

onMounted(async () => {
  await load()
  // 知识点看板「知识点主题设置」深链进入:待表单渲染后滚动到学习主题区块
  if (route.hash) {
    await nextTick()
    document
      .getElementById(route.hash.slice(1))
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
})
</script>

<template>
  <div class="panel">
    <!-- 页头 -->
    <div class="page-header">
      <div>
        <h1>练习设置</h1>
        <p class="page-subtitle">
          自适应练习的出题与复习偏好。切换即保存,全局生效。
        </p>
      </div>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="loading-box">
      <span class="status-spinner" aria-label="加载中" />
      <span>正在加载练习设置…</span>
    </div>

    <!-- 加载失败 -->
    <div v-else-if="loadError" class="alert alert-error" role="alert">
      <span>加载失败:{{ loadError }}</span>
      <button class="btn-link" @click="load">重试</button>
    </div>

    <!-- 设置表单 -->
    <section v-else class="practice-form">
      <!-- 自动生成练习题 -->
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-title">自动生成练习题</span>
          <span class="setting-desc">
            审计任务完成后自动生成练习题候选题(全局生效,仍需预览确认才入库)
          </span>
        </div>
        <button
          type="button"
          role="switch"
          :aria-checked="autoGenerate"
          :class="['switch', { 'switch-on': autoGenerate }]"
          :disabled="busy"
          @click="toggleAuto"
        >
          <span class="switch-thumb" />
        </button>
      </div>

      <!-- 出题前恢复工作区 -->
      <div class="setting-row">
        <div class="setting-info">
          <span class="setting-title">出题前恢复工作区</span>
          <span class="setting-desc">
            出题时沙箱已清理(任务完成超过 1 小时)则重新克隆仓库,
            让出题过程能查阅真实源码提高题目质量(会消耗克隆时间)
          </span>
        </div>
        <button
          type="button"
          role="switch"
          :aria-checked="restoreWorkspace"
          :class="['switch', { 'switch-on': restoreWorkspace }]"
          :disabled="busy"
          @click="toggleRestore"
        >
          <span class="switch-thumb" />
        </button>
      </div>

      <!-- 出题思考模式 -->
      <div class="setting-block">
        <span class="setting-title">出题思考模式</span>
        <span class="setting-desc">
          是否让出题模型开启思考(慢想)模式;开启后出题更慢但题目质量可能更高,
          关闭则生成更快——若某模型思考模式下反复读代码却出不出题,可强制关闭
        </span>
        <div class="topic-list" role="radiogroup" aria-label="出题思考模式">
          <button
            v-for="opt in THINKING_OPTIONS"
            :key="opt.value"
            type="button"
            role="radio"
            :aria-checked="thinkingMode === opt.value"
            :class="['topic-option', { 'topic-active': thinkingMode === opt.value }]"
            :disabled="busy"
            @click="selectThinking(opt.value)"
          >
            <span class="topic-radio">
              <span v-if="thinkingMode === opt.value" class="topic-radio-dot" />
            </span>
            <span class="topic-text">
              <span class="topic-label">{{ opt.label }}</span>
              <span class="topic-desc">{{ opt.desc }}</span>
            </span>
          </button>
        </div>
      </div>

      <!-- 默认出题模型 -->
      <div class="setting-block">
        <span class="setting-title">默认出题模型</span>
        <span class="setting-desc">
          生成练习题时默认使用的模型(手动出题与自动出题均生效);
          任务自带模型配置时优先用任务配置,选「跟随系统默认」则用环境配置
        </span>
        <select
          class="model-select"
          aria-label="默认出题模型"
          :value="defaultModelId"
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

        <!-- 始终用默认出题模型 -->
        <div class="force-default-row">
          <div class="setting-info">
            <span class="setting-title force-title">始终用默认出题模型</span>
            <span class="setting-desc">
              忽略任务自带的模型配置,所有任务出题统一用上面的默认模型
            </span>
          </div>
          <button
            type="button"
            role="switch"
            :aria-checked="forceDefaultLlm"
            :class="['switch', { 'switch-on': forceDefaultLlm }]"
            :disabled="busy"
            @click="toggleForceDefault"
          >
            <span class="switch-thumb" />
          </button>
        </div>
      </div>

      <!-- 学习主题管理:内置 4 个(可停用)+ 自定义增删改 -->
      <div id="learning-topics" class="setting-block">
        <span class="setting-title">学习主题</span>
        <span class="setting-desc">
          出题视角与自动分类的主题词表。停用的主题不再出新题,已有题目不受影响;
          自定义主题的出题质量取决于描述的具体程度
        </span>

        <div v-if="topicBusy" class="loading-box"><span class="btn-spinner" /> 加载中…</div>
        <div v-else class="topic-manage-list">
          <div
            v-for="t in topics"
            :key="t.id"
            :class="['topic-manage-row', { 'topic-row-disabled': !t.enabled }]"
          >
            <!-- 编辑态(仅自定义行) -->
            <template v-if="editingId === t.id">
              <div class="topic-edit-form">
                <input
                  v-model="editName"
                  class="topic-input"
                  type="text"
                  maxlength="64"
                  placeholder="主题名称"
                >
                <textarea
                  v-model="editDesc"
                  class="topic-textarea"
                  rows="2"
                  maxlength="500"
                  placeholder="主题视角描述(选填)"
                />
                <div class="topic-edit-actions">
                  <button class="btn-plain" :disabled="!editName.trim()" @click="saveEdit(t)">保存</button>
                  <button class="btn-plain" @click="editingId = ''">取消</button>
                </div>
              </div>
            </template>

            <!-- 展示态 -->
            <template v-else>
              <div class="topic-manage-info">
                <span class="topic-manage-name">
                  {{ t.name }}
                  <span v-if="t.is_builtin" class="topic-badge" title="内置主题:不可删除,仅可启用/停用">内置</span>
                </span>
                <span class="topic-manage-desc">{{ t.description || '暂无视角描述' }}</span>
                <span class="topic-manage-meta">{{ t.kp_count }} 个知识点</span>
              </div>
              <div class="topic-manage-side">
                <button
                  type="button"
                  role="switch"
                  :aria-checked="t.enabled"
                  :aria-label="`${t.enabled ? '停用' : '启用'}主题「${t.name}」`"
                  :class="['switch', { 'switch-on': t.enabled }]"
                  :disabled="topicBusy || (t.enabled && enabledCount <= 1)"
                  :title="t.enabled && enabledCount <= 1 ? '至少需保留一个启用的学习主题' : (t.enabled ? '停用后不再出新题,已有题目不受影响' : '启用后新任务的发现将参与该主题出题')"
                  @click="toggleTopic(t)"
                >
                  <span class="switch-thumb" />
                </button>
                <div v-if="!t.is_builtin" class="topic-manage-actions">
                  <button class="btn-plain" :disabled="topicBusy" @click="startEdit(t)">编辑</button>
                  <template v-if="deletingId === t.id">
                    <button class="btn-danger" :disabled="topicBusy" @click="handleDeleteTopic(t)">确认删除</button>
                    <button class="btn-plain" :disabled="topicBusy" @click="deletingId = ''">取消</button>
                  </template>
                  <button
                    v-else
                    class="btn-plain"
                    :disabled="topicBusy"
                    title="有关联知识点的主题无法删除,可改为停用"
                    @click="deletingId = t.id"
                  >删除</button>
                </div>
              </div>
            </template>
          </div>
        </div>

        <!-- 新增自定义主题 -->
        <div v-if="!showCreateForm" class="topic-add-row">
          <button class="btn-plain" :disabled="topicBusy" @click="showCreateForm = true">+ 添加自定义主题</button>
        </div>
        <div v-else class="topic-create-form">
          <input
            v-model="newName"
            class="topic-input"
            type="text"
            maxlength="64"
            placeholder="主题名称(如:算法与数据结构)"
          >
          <textarea
            v-model="newDesc"
            class="topic-textarea"
            rows="2"
            maxlength="500"
            placeholder="例如:算法与数据结构——考察复杂度分析、边界条件、正确性证明"
          />
          <div class="topic-edit-actions">
            <button class="btn-plain" :disabled="topicBusy || !newName.trim()" @click="handleCreateTopic">添加</button>
            <button class="btn-plain" :disabled="topicBusy" @click="showCreateForm = false">取消</button>
          </div>
        </div>
      </div>

      <!-- 危险操作:数据清空不可逆,均需二次确认 -->
      <div class="danger-block">
        <span class="setting-title danger-title">危险操作</span>

        <!-- 清空练习记录 -->
        <div class="danger-row">
          <div class="danger-info">
            <span class="danger-name">清空练习记录</span>
            <span class="danger-desc">
              清除历史会话与作答记录,重置知识点掌握度与复习计划;题库保留,题目难度重置
            </span>
          </div>
          <button
            v-if="confirmMode !== 'records'"
            class="btn-danger"
            :disabled="busy"
            @click="confirmMode = 'records'"
          >清空</button>
          <div v-else class="danger-confirm">
            <span class="danger-confirm-text">不可恢复,确认清空?</span>
            <div class="danger-confirm-actions">
              <button class="btn-danger" :disabled="busy" @click="handleClear(false)">确认清空</button>
              <button class="btn-plain" :disabled="busy" @click="confirmMode = 'none'">取消</button>
            </div>
          </div>
        </div>

        <!-- 清空全部数据 -->
        <div class="danger-row">
          <div class="danger-info">
            <span class="danger-name">清空全部数据</span>
            <span class="danger-desc">
              在「清空练习记录」基础上,连题库(含待确认候选题)与知识点一并删除,回到全新状态
            </span>
          </div>
          <button
            v-if="confirmMode !== 'all'"
            class="btn-danger"
            :disabled="busy"
            @click="confirmMode = 'all'"
          >清空全部</button>
          <div v-else class="danger-all-confirm">
            <span class="danger-confirm-text">输入「清空」确认删除:</span>
            <div class="danger-all-actions">
              <input
                v-model="confirmText"
                class="danger-input"
                type="text"
                placeholder="清空"
                :disabled="busy"
                @keyup.enter="confirmText === '清空' && handleClear(true)"
              >
              <button
                class="btn-danger"
                :disabled="busy || confirmText !== '清空'"
                @click="handleClear(true)"
              >确认删除</button>
              <button class="btn-plain" :disabled="busy" @click="confirmMode = 'none'">取消</button>
            </div>
          </div>
        </div>
      </div>

      <p v-if="saving" class="saving-hint"><span class="btn-spinner" /> 保存中...</p>
      <p v-else-if="clearing" class="saving-hint"><span class="btn-spinner" /> 清空中...</p>
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

/* ---- 表单 ---- */
.practice-form {
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

/* ---- 思考模式单选 ---- */
.topic-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.topic-option {
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

.topic-option:hover:not(:disabled) {
  border-color: var(--color-border-strong);
}

.topic-option:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.topic-active {
  border-color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}

.topic-radio {
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

.topic-active .topic-radio {
  border-color: var(--color-primary);
}

.topic-radio-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
}

.topic-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.topic-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.topic-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

/* ---- 默认出题模型 ---- */
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

.force-default-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-top: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

.force-title {
  font-size: var(--fs-sm);
}

/* ---- 学习主题管理 ---- */
/* 知识点看板深链定位锚点:留出呼吸,避免区块贴住滚动容器顶 */
#learning-topics {
  scroll-margin-top: var(--space-4);
}

.topic-manage-list {
  display: flex;
  flex-direction: column;
}

.topic-manage-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--color-border);
}

.topic-manage-row:first-child {
  padding-top: var(--space-1);
}

.topic-manage-row:last-of-type {
  border-bottom: none;
}

.topic-row-disabled .topic-manage-name,
.topic-row-disabled .topic-manage-desc,
.topic-row-disabled .topic-manage-meta {
  color: var(--color-text-muted);
}

.topic-manage-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.topic-manage-name {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.topic-badge {
  display: inline-flex;
  align-items: center;
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 999px;
}

.topic-manage-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.topic-manage-meta {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.topic-manage-side {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.topic-manage-actions {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.topic-add-row {
  margin-top: var(--space-2);
}

.topic-create-form,
.topic-edit-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: 100%;
  margin-top: var(--space-1);
}

.topic-edit-form {
  padding: var(--space-2) 0;
}

.topic-input,
.topic-textarea {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-family: inherit;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  outline: none;
  transition: border-color var(--transition-fast);
}

.topic-textarea {
  resize: vertical;
  line-height: var(--lh-relaxed);
}

.topic-input:focus,
.topic-textarea:focus {
  border-color: var(--color-primary);
}

.topic-edit-actions {
  display: flex;
  gap: var(--space-2);
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

/* ---- 危险操作区 ---- */
.danger-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-4);
  border: 1px solid color-mix(in srgb, var(--color-danger) 40%, var(--color-border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--color-danger) 3%, transparent);
}

.danger-title {
  color: var(--color-danger);
}

.danger-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.danger-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.danger-name {
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.danger-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.danger-confirm {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-1);
}

.danger-confirm-text {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
}

.danger-confirm-actions {
  display: flex;
  gap: var(--space-2);
}

.danger-all-confirm {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-2);
}

.danger-all-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.danger-input {
  width: 88px;
  height: 30px;
  padding: 0 var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  outline: none;
}

.danger-input:focus {
  border-color: var(--color-danger);
}

.danger-input:disabled {
  opacity: 0.5;
}

.btn-danger {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
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

.btn-plain {
  flex-shrink: 0;
  padding: var(--space-1) var(--space-3);
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

/* ---- 响应式 ---- */
@media (max-width: 640px) {
  .panel {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  .setting-row,
  .danger-row {
    flex-direction: column;
    align-items: stretch;
  }

  .danger-confirm,
  .danger-all-confirm {
    align-items: flex-start;
  }
}
</style>

