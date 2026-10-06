<script setup lang="ts">
/**
 * 提交任务页(对话式输入框)
 *
 * 布局类似常见大模型 Web 聊天输入框:
 * - 顶部:场景(mode)选择 + 使用模型选择
 * - 中部:大尺寸 textarea(用户主输入,提交时作为 user_input 拼到智能体上下文)
 * - 输入框底部:Git 仓库选择/输入(GitHub / Gitee)+ 分支 + 发送按钮
 *
 * 字段映射(对齐后端 params):
 * - userInput → user_input(用户主输入)
 * - repoUrl   → params.repo_url
 * - branch    → params.branch
 *
 * 提交后:后端立即返回 task_id(异步执行),前端跳转详情页通过 SSE 观看实时进度。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import ModelCombobox from '@/components/ModelCombobox.vue'
import BaseSelect from '@/components/BaseSelect.vue'
import BrandLogo from '@/components/BrandLogo.vue'
import { createTask, getScenarios } from '@/api/task'
import { uploadTaskFile, type UploadResult } from '@/api/uploads'
import { getMyModels } from '@/api/model_configs'
import { getAgentConfigs } from '@/api/agent_configs'
import {
  getGitProviderStatus,
  listGitProviderRepos,
} from '@/api/git_provider'
import { getSkills, type SkillSummary } from '@/api/skill'
import { getPreferences } from '@/api/memory'
import { extractErrorMessage } from '@/utils/error'
import type { Scenario } from '@/types/task'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { AgentConfigOut } from '@/types/agent_configs'
import type { GitProvider, GitRepoItem } from '@/types/git_provider'

const router = useRouter()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ---- 场景列表 ----

const scenarios = ref<Scenario[]>([])
const selectedScenario = ref('')

/**
 * 当前选中场景(用于读取 preset_prompt 等元信息)
 *
 * 场景已降级为模板:仅提供 preset_prompt 预填到输入框,
 * 不再声明 form_fields/result_grouping/coverage 等。
 */
const selectedScenarioDecl = computed<Scenario | null>(() =>
  scenarios.value.find((s) => s.id === selectedScenario.value) ?? null,
)

// ---- 模型列表 ----

const llmConfigs = ref<LLMConfigItemOut[]>([])
/** agent2 评估模型 */
const selectedLlmConfigId = ref('')
/**
 * 内置 agent1 模型(仅 builtin 模式生效)。
 * 空字符串 = 同评估模型(回退到 selectedLlmConfigId);
 * 非空 = 显式选另一个模型。
 */
const selectedReactLlmConfigId = ref('')
const loadingModels = ref(true)

// ---- 执行器选择(内置 + 用户已配置且启用的 agent) ----

/**
 * 执行器:决定 react 角色由哪个 agent 执行
 * - 'builtin':系统内置 agent1(使用上方选择的 LLM 配置)
 * - agent_type(如 'qoder_cli'):对应 agent CLI,react 角色模型由 CLI 自管;
 *   上方选择的 LLM 配置仅用于 agent2 评估
 *
 * 候选列表由后端 GET /agents/configs 动态返回(is_active=true 的)。
 */
const agentExecutors = ref<AgentConfigOut[]>([])
/** 当前选中执行器:'builtin' 或某个 agent_type */
const selectedExecutor = ref<string>('builtin')

/** 是否选中了非内置执行器(CLI 自管 react 模型,LLM 配置仅供 agent2) */
const useAgentExecutor = computed(() => selectedExecutor.value !== 'builtin')

/** 模型选择器在当前执行器下的语义标签
 * - builtin:模型同时用于内置 agent1 与 agent2 评估
 * - CLI:模型仅用于 agent2 评估(执行模型由 CLI 自管)
 */
const modelSelectLabel = computed(() =>
  useAgentExecutor.value ? '评估模型' : '使用模型',
)

// ---- Qoder CLI 模型配置(qoder_cli 执行器显示;deepseek_cli 见下方独立配置) ----
// 见 https://docs.qoder.cn/cli/model

/** Qoder CLI 模型选项(value 必须与 CLI --model 接受的名称严格大小写匹配,
 * 见 https://docs.qoder.cn/cli/model 及 ACP 错误返回的 Available models 列表) */
const qoderModelOptions: { value: string; label: string }[] = [
  { value: '', label: '默认(智能路由 Auto)' },
  { value: 'Auto', label: '智能路由 (Auto)' },
  { value: 'Qwen3.8-Max', label: 'Qwen3.8-Max' },
  { value: 'Qwen3.8-Flash', label: 'Qwen3.8-Flash' },
  { value: 'Qwen3.7-Max', label: 'Qwen3.7-Max' },
  { value: 'Qwen3.7-Plus', label: 'Qwen3.7-Plus' },
  { value: 'Qwen3.7-Flash', label: 'Qwen3.7-Flash' },
  { value: 'DeepSeek-V4-Pro', label: 'DeepSeek-V4-Pro' },
  { value: 'DeepSeek-V4-Flash', label: 'DeepSeek-V4-Flash' },
  { value: 'GLM-5.3', label: 'GLM-5.3' },
  { value: 'GLM-5.2', label: 'GLM-5.2' },
  { value: 'Kimi-K2.7-Code', label: 'Kimi-K2.7-Code' },
  { value: 'MiniMax-M2.7', label: 'MiniMax-M2.7' },
]

/** 思考强度选项 */
const qoderEffortOptions: { value: string; label: string }[] = [
  { value: '', label: '默认' },
  { value: 'low', label: 'Low(最快)' },
  { value: 'medium', label: 'Medium(适中)' },
  { value: 'high', label: 'High(深入)' },
  { value: 'xhigh', label: 'XHigh(深度分析)' },
  { value: 'max', label: 'Max(最大推理)' },
]

/** 上下文窗口选项 */
const qoderContextOptions: { value: number; label: string }[] = [
  { value: 0, label: '默认' },
  { value: 200000, label: '200K' },
  { value: 400000, label: '400K' },
  { value: 1000000, label: '1M' },
]

/** Qoder CLI 配置面板是否展开 */
const qoderConfigOpen = ref(false)
/** 选中的模型(空字符串=默认) */
const qoderModel = ref('')
/** 选中的思考强度(空字符串=默认) */
const qoderReasoningEffort = ref('')
/** 选中的上下文窗口(0=默认) */
const qoderContextWindow = ref(0)

// ---- DeepSeek CLI 模型配置(deepseek_cli 执行器显示) ----
// dsh ACP 模式不接受 --model CLI 参数,模型/思考强度经 session/set_config_option 设置;
// 模型见 dsh deepseek-official provider 目录:deepseek-v4-flash / deepseek-v4-pro

/** DeepSeek CLI 模型选项 */
const deepseekModelOptions: { value: string; label: string }[] = [
  { value: '', label: '默认(DeepSeek-V4-Flash)' },
  { value: 'deepseek-v4-flash', label: 'DeepSeek-V4-Flash(轻量)' },
  { value: 'deepseek-v4-pro', label: 'DeepSeek-V4-Pro' },
]

/** DeepSeek 思考强度选项(dsh 广播等级:off / low / high / max) */
const deepseekEffortOptions: { value: string; label: string }[] = [
  { value: '', label: '默认(Max)' },
  { value: 'off', label: 'Off(不思考)' },
  { value: 'low', label: 'Low(最快)' },
  { value: 'high', label: 'High(深入)' },
  { value: 'max', label: 'Max(最大推理)' },
]

/** 选中的 DeepSeek 模型(空字符串=默认) */
const deepseekModel = ref('')
/** 选中的 DeepSeek 思考强度(空字符串=默认) */
const deepseekReasoningEffort = ref('')

/** 模型配置面板是否适用于当前执行器(qoder_cli / deepseek_cli) */
const supportsModelConfig = computed(
  () =>
    selectedExecutor.value.startsWith('qoder_cli') || selectedExecutor.value === 'deepseek_cli',
)
/** 当前执行器是否为 DeepSeek CLI */
const isDeepseekCli = computed(() => selectedExecutor.value === 'deepseek_cli')
/** 模型下拉选项(按执行器切换) */
const modelOptions = computed(() =>
  isDeepseekCli.value ? deepseekModelOptions : qoderModelOptions,
)
/** 思考强度下拉选项(按执行器切换) */
const effortOptions = computed(() =>
  isDeepseekCli.value ? deepseekEffortOptions : qoderEffortOptions,
)
/** 当前选中的模型(读写代理,按执行器分发到对应 ref) */
const selectedModel = computed({
  get: () => (isDeepseekCli.value ? deepseekModel.value : qoderModel.value),
  set: (v: string) => {
    if (isDeepseekCli.value) deepseekModel.value = v
    else qoderModel.value = v
  },
})
/** 当前选中的思考强度(读写代理,按执行器分发到对应 ref) */
const selectedReasoningEffort = computed({
  get: () => (isDeepseekCli.value ? deepseekReasoningEffort.value : qoderReasoningEffort.value),
  set: (v: string) => {
    if (isDeepseekCli.value) deepseekReasoningEffort.value = v
    else qoderReasoningEffort.value = v
  },
})
/** 配置面板摘要行文案 */
const modelConfigSummary = computed(() => {
  if (isDeepseekCli.value) {
    return `${deepseekModel.value || 'deepseek-v4-flash'} · ${deepseekReasoningEffort.value || '默认'}`
  }
  return `${qoderModel.value || 'Auto'} · ${qoderReasoningEffort.value || '默认'} · ${
    qoderContextWindow.value
      ? qoderContextWindow.value >= 1000000
        ? '1M'
        : qoderContextWindow.value / 1000 + 'K'
      : '默认'
  }`
})

// ---- Skill 多选(高级选项) ----

/** 所有可用 skill(从后端 GET /skills 加载) */
const allSkills = ref<SkillSummary[]>([])
/** skill 列表加载错误(静默失败,不阻塞提交) */
const skillsError = ref('')

// ============================================================
// 右侧设置抽屉(高级设置 / 技能两个分区;检查助手启停与测试环境设置并入「高级设置」)
// ============================================================

/** 抽屉分区标识 */
type DrawerSection = 'policy' | 'skills'

/** 抽屉是否打开 */
const drawerOpen = ref(false)
/** 当前展开的分区(同一时刻只展开一个) */
const drawerSection = ref<DrawerSection | null>(null)

/** 点击入口按钮:打开设置面板并展开对应分区;再次点击同一分区则收起面板 */
function openDrawer(section: DrawerSection): void {
  // 再次点击已展开的分区入口 → 收起面板
  if (drawerOpen.value && drawerSection.value === section) {
    closeDrawer()
    return
  }
  drawerOpen.value = true
  drawerSection.value = section
}

/** 入口按钮 title:展开中提示「再次点击收起」,收起时提示「点击打开设置」 */
function drawerTitle(section: DrawerSection, label: string): string {
  return drawerOpen.value && drawerSection.value === section
    ? '再次点击收起设置面板'
    : `点击打开${label}设置`
}

/** 关闭抽屉(遮罩点击 / × / Esc) */
function closeDrawer(): void {
  drawerOpen.value = false
}

/** Esc 关闭抽屉 */
function onDrawerKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && drawerOpen.value) closeDrawer()
}
/**
 * 当前选中的 skill name 集合
 *
 * 语义:
 * - 默认(无场景推荐 / general):全部勾选(等同于不限制,提交时不传 allowed_skills)
 * - 选场景后:勾选该场景 recommended_skills 中实际存在的 skill
 * - 用户可手动勾选/取消
 * - 提交时:若全部勾选 → 传 undefined(全部可用);否则传选中的数组
 */
const selectedSkillNames = ref<Set<string>>(new Set())

/** 是否全部 skill 都已选中 */
const allSkillsSelected = computed(
  () => allSkills.value.length > 0 && selectedSkillNames.value.size === allSkills.value.length,
)

/** 选中的 skill 数量(展示用) */
const selectedSkillCount = computed(() => selectedSkillNames.value.size)

/** 切换单个 skill 的选中状态 */
function toggleSkill(name: string): void {
  const next = new Set(selectedSkillNames.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  selectedSkillNames.value = next
}

/** 全选 / 全不选切换 */
function toggleAllSkills(): void {
  if (allSkillsSelected.value) {
    selectedSkillNames.value = new Set()
  } else {
    selectedSkillNames.value = new Set(allSkills.value.map((s) => s.name))
  }
}

/**
 * 根据场景的 recommended_skills 重置 skill 选中状态
 *
 * - recommended_skills 为空或 general 场景:全选(默认全部可用)
 * - recommended_skills 非空:只勾选推荐且实际存在的 skill
 */
function applyRecommendedSkills(): void {
  const recommended = selectedScenarioDecl.value?.recommended_skills ?? []
  if (recommended.length === 0) {
    // 无推荐 → 全选(等同于不限制)
    selectedSkillNames.value = new Set(allSkills.value.map((s) => s.name))
    return
  }
  // 只勾选推荐且实际存在的 skill
  const existingNames = new Set(allSkills.value.map((s) => s.name))
  const validRecommended = recommended.filter((name) => existingNames.has(name))
  selectedSkillNames.value = new Set(validRecommended)
}

// ---- 表单数据(扁平化,对应场景声明字段) ----

/** 用户主输入(对话式 textarea,对应 note 字段) */
const userInput = ref('')
/** 任务标题(可选,便于在历史列表识别;留空回退到 user_input 截断展示) */
const taskTitle = ref('')
/** GitHub 仓库地址(对应 repo_url 字段) */
const repoUrl = ref('')
/** 分支(对应 branch 字段) */
const branch = ref('')

// ---- 交付物来源:Git 仓库 / 上传 ZIP / 上传单文件(三选一) ----

type SourceMode = 'git' | 'zip' | 'file'
/** 交付物来源 Tab(默认 Git 仓库;zip/file 调 POST /uploads 拿 upload_id) */
const sourceMode = ref<SourceMode>('git')
/** 已上传的交付物列表(POST /uploads 返回;支持多选与多次追加) */
const uploadedDeliverables = ref<UploadResult[]>([])
const uploading = ref(false)
const uploadError = ref('')
const zipFileInputRef = ref<HTMLInputElement | null>(null)
const singleFileInputRef = ref<HTMLInputElement | null>(null)

/** 切换来源 Tab:离开上传 Tab 时清掉已选文件(避免"隐藏的上传"被误提交) */
function switchSourceMode(mode: SourceMode): void {
  if (sourceMode.value === mode) return
  sourceMode.value = mode
  uploadedDeliverables.value = []
  uploadError.value = ''
}

/** 打开文件选择框(按当前 Tab 路由到对应 input) */
function triggerFilePicker(): void {
  const el = sourceMode.value === 'zip' ? zipFileInputRef.value : singleFileInputRef.value
  el?.click()
}

/** 选择文件后上传(支持多选;expectZip=true 校验 .zip 后缀) */
async function onUploadFileChosen(e: Event, expectZip: boolean): Promise<void> {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files || [])
  // 清空 value 允许再次选择同一文件
  input.value = ''
  if (!files.length) return
  // ZIP Tab:任一文件后缀不符即拒绝整批
  if (expectZip && files.some((f) => !f.name.toLowerCase().endsWith('.zip'))) {
    uploadError.value = '请选择 .zip 压缩包'
    return
  }
  uploadError.value = ''
  uploading.value = true
  try {
    // 逐个上传并追加(多次选择可累积;按 upload_id 去重)
    for (const file of files) {
      try {
        const res = await uploadTaskFile(file)
        if (!uploadedDeliverables.value.some((d) => d.upload_id === res.upload_id)) {
          uploadedDeliverables.value.push(res)
        }
      } catch (err: unknown) {
        // 单个失败不阻断其余;保留错误提示供用户感知
        uploadError.value = extractErrorMessage(err)
      }
    }
  } finally {
    uploading.value = false
  }
}

/** 移除某个已上传的交付物 */
function removeDeliverable(uploadId: string): void {
  uploadedDeliverables.value = uploadedDeliverables.value.filter(
    (d) => d.upload_id !== uploadId,
  )
  uploadError.value = ''
}

/** 清空全部已上传交付物(切换场景/Tab 时) */
function clearUploadedDeliverables(): void {
  uploadedDeliverables.value = []
  uploadError.value = ''
}

/** 字节数人性化显示(上传 chip 用) */
function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** 通用 URL 校验 */
const urlPattern = /^https?:\/\/[^\s/$.?#].[^\s]*$/

const loading = ref(false)
const error = ref('')

// ---- Agent 策略配置(任务级覆盖用户级默认,在右侧设置抽屉中编辑) ----

/** 是否启用 agent2(关闭=单 agent 模式,跳过评估/验证) */
const policyAgent2Enabled = ref(true)
/** agent2 是否能自己验证(实验性) */
const policyAllowVerify = ref(false)
/** AI助手确认策略(任务级 _executor_command_confirm 覆盖;builtin 与 CLI 执行器均生效) */
const policyExecutorCommandConfirm = ref<'always_approve' | 'per_command'>('always_approve')

/** AI助手确认策略选项(对齐 BaseSelect {value,label} 结构) */
const executorConfirmOptions = [
  { value: 'always_approve' as 'always_approve' | 'per_command', label: '自动批准' },
  { value: 'per_command' as 'always_approve' | 'per_command', label: '逐命令确认' },
]

/** 验证授权模式选项 */
const verifierAuthModeOptions = [
  { value: 'per_action' as 'direct' | 'per_action', label: '逐动作授权(每个请求前确认)' },
  { value: 'direct' as 'direct' | 'per_action', label: '直接执行(不弹窗)' },
]

/** 系统默认策略值(与后端 DEFAULT_AGENT_POLICY 对齐,作为未配置用户级默认时的兜底) */
const DEFAULT_POLICY = {
  agent2_enabled: true,
  allow_verify: false,
  executor_command_confirm_default: 'always_approve' as 'always_approve' | 'per_command',
}

/**
 * 用户级默认策略(比较基准,用于判断是否需要提交任务级覆盖)。
 * - 初始为系统默认值;onMounted 加载用户偏好(智能体策略设置页保存的)后替换为实际值。
 * - 加载失败/未配置时保持系统默认,与后端 resolve_agent_policy 的合并结果一致。
 */
const userPolicyDefaults = ref({
  agent2Enabled: DEFAULT_POLICY.agent2_enabled,
  allowVerify: DEFAULT_POLICY.allow_verify,
  verifierAuthMode: 'per_action' as 'direct' | 'per_action',
  executorCommandConfirm: DEFAULT_POLICY.executor_command_confirm_default,
})

// builtin + agent2 关闭:agent1 模型不再有「同评估模型」选项,
// react 模型为空时自动补齐(兼容「手动关闭」与「初始加载即默认关闭」两种时机)
watch(
  [policyAgent2Enabled, llmConfigs, selectedExecutor],
  () => {
    if (
      !policyAgent2Enabled.value &&
      !useAgentExecutor.value &&
      selectedReactLlmConfigId.value === ''
    ) {
      const fallback =
        selectedLlmConfigId.value ||
        (llmConfigs.value.find((c) => c.has_api_key) ?? llmConfigs.value[0])?.id ||
        ''
      if (fallback) selectedReactLlmConfigId.value = fallback
    }
  },
)

// ---- 测试环境 / 动态验证配置(高级设置抽屉内,开启「允许自行验证」后展示) ----
// agent2 可在已部署的测试环境动态验证 agent1 发现的安全问题。
// 对用户透明:不出现 verifier_agent 字样,只显示"正在验证"。
// 是否启用由 policyAllowVerify(允许检查助手自行验证)统一控制。
/** 测试环境 URL(已部署的应用地址,如 http://localhost:3000) */
const testEnvUrl = ref('')
/**
 * 验证授权模式:
 * - "direct":验证动作直接执行不弹窗
 * - "per_action":每个 HTTP 请求/PoC 运行前弹窗授权
 */
const verifierAuthMode = ref<'direct' | 'per_action'>('per_action')
/**
 * 登录凭证列表(可选):LLM 调 http_request 时按 auth_profile=label 注入对应请求头。
 * 用于越权测试:同一端点用不同身份访问,对比响应差异。
 * 每项 = { label, header_name, header_value };LLM 只看到 label,看不到 header_value。
 */
const verifierAuthTokens = ref<Array<{ label: string; header_name: string; header_value: string }>>([])

/** 添加一个空凭证行 */
function addAuthToken(): void {
  verifierAuthTokens.value.push({ label: '', header_name: 'Authorization', header_value: '' })
}

/** 删除指定索引的凭证行 */
function removeAuthToken(idx: number): void {
  verifierAuthTokens.value.splice(idx, 1)
}

// ============================================================
// Git 仓库选择(repo_url 字段专用增强,统一 GitHub / Gitee)
// ============================================================

/**
 * repo_url 字段支持两种输入方式:
 * - 'url':手动输入公开仓库地址(默认,场景无关)
 * - 'select':从已绑定的 Git 平台账号(GitHub / Gitee)仓库列表中选择(可含私有仓库)
 *
 * 仅当用户已绑定任一平台时显示下拉选择;未绑定时走普通 url 输入,不阻塞流程。
 * 多平台绑定时,两组仓库合并到同一个下拉,选项 label 带 provider 标记。
 */
/** 支持的平台列表 */
const PROVIDERS: GitProvider[] = ['github', 'gitee']

/** 各平台绑定状态 */
const providerStatus = ref<Record<GitProvider, { bound: boolean } | null>>({
  github: null,
  gitee: null,
})

/** 是否已绑定任一平台(决定走下拉选择还是纯输入框) */
const anyProviderBound = computed(() =>
  PROVIDERS.some((p) => providerStatus.value[p]?.bound),
)

/** 仓库项(带 provider 标记,用于下拉选项 label 与 default_branch 填充) */
interface RepoEntry extends GitRepoItem {
  provider: GitProvider
}

/** 合并后的所有仓库列表 */
const allRepos = ref<RepoEntry[]>([])
const reposLoaded = ref(false)
const reposLoading = ref(false)
const reposError = ref('')

/** 仓库提示弹窗(加载失败/无仓库/未绑定 等),用一个统一弹窗承载,避免行内提示抖动 */
const repoDialogOpen = ref(false)
/** 弹窗正文(空串=不显示) */
const repoDialogMessage = ref('')
/** 弹窗是否提供"前往设置绑定"链接 */
const repoDialogShowBindLink = ref(false)

/** 打开仓库提示弹窗 */
function showRepoDialog(message: string, showBindLink = false): void {
  repoDialogMessage.value = message
  repoDialogShowBindLink.value = showBindLink
  repoDialogOpen.value = true
}

function closeRepoDialog(): void {
  repoDialogOpen.value = false
}

/** 平台显示名(label 前缀用) */
function providerDisplayName(p: GitProvider): string {
  return p === 'gitee' ? 'Gitee' : 'GitHub'
}

/**
 * 并行加载所有已绑定平台的仓库列表,合并到 allRepos。
 * 任一平台失败不影响其他平台,仅记录到 reposError。
 *
 * @param force 强制刷新(跳过后端 30s 缓存,直接调平台 API),
 *              用于「刷新」按钮:用户在平台上新建仓库后立即拉取。
 *              force=true 时忽略 reposLoaded 一次性保护。
 */
async function loadAllRepos(force = false): Promise<void> {
  // force 模式跳过一次性保护,允许重复加载
  if (!force && (reposLoaded.value || reposLoading.value)) return
  if (reposLoading.value) return  // 正在加载中,避免并发
  reposLoading.value = true
  reposError.value = ''

  const boundProviders = PROVIDERS.filter((p) => providerStatus.value[p]?.bound)
  if (boundProviders.length === 0) {
    reposLoaded.value = true
    return
  }

  try {
    // 并行拉取所有已绑定平台的仓库;result 与 boundProviders 索引一一对应
    // force=true 时传 refresh=true 给后端,跳过 30s 缓存
    const results = await Promise.allSettled(
      boundProviders.map((p) => listGitProviderRepos(p, force)),
    )

    const merged: RepoEntry[] = []
    const errors: string[] = []
    results.forEach((r, idx) => {
      const p = boundProviders[idx]
      if (r.status === 'fulfilled') {
        for (const repo of r.value.repos) {
          merged.push({ ...repo, provider: p })
        }
      } else {
        errors.push(
          `${providerDisplayName(p)} 仓库加载失败: ${extractErrorMessage(r.reason)}`,
        )
      }
    })
    allRepos.value = merged
    reposLoaded.value = true

    if (merged.length === 0 && errors.length === 0) {
      showRepoDialog('你绑定的账号下暂无仓库')
    } else if (errors.length > 0) {
      // 部分成功时也提示失败的平台(不阻塞使用已加载的仓库)
      showRepoDialog(errors.join('\n'))
    }
  } catch (err) {
    reposError.value = extractErrorMessage(err)
    showRepoDialog(`加载仓库列表失败:${reposError.value}`)
  } finally {
    reposLoading.value = false
  }
}

/**
 * 刷新仓库列表(强制跳过缓存)。
 * 防抖 500ms:防止狂点按钮导致并发请求;
 * 保留当前选中的 repoUrl,刷新后若该仓库仍在列表中则不变,否则保持原值(用户可手动改)。
 */
let refreshReposTimer: ReturnType<typeof setTimeout> | null = null
async function refreshRepos(): Promise<void> {
  if (reposLoading.value) return  // 正在加载,忽略
  if (refreshReposTimer) {
    clearTimeout(refreshReposTimer)
  }
  // 防抖:500ms 内重复点击只执行最后一次
  await new Promise<void>((resolve) => {
    refreshReposTimer = setTimeout(() => resolve(), 500)
  })
  await loadAllRepos(true)
}

/**
 * 仓库下拉框选项(可输入下拉框)。
 * value=clone_url(选中后写入 repoUrl),
 * label=`[平台] owner/repo (私有)?`(带 provider 标记,多平台时便于区分)。
 */
const repoComboboxOptions = computed(() =>
  allRepos.value.map((r) => ({
    value: r.clone_url,
    label: `[${providerDisplayName(r.provider)}] ${r.full_name}${r.private ? ' (私有)' : ''}`,
  })),
)

/** 从下拉选中仓库时,若分支为空则自动填默认分支 */
watch(repoUrl, (url) => {
  if (!url) return
  const repo = allRepos.value.find((r) => r.clone_url === url)
  if (repo && !branch.value.trim()) {
    branch.value = repo.default_branch
  }
})

// 切换场景时:把场景的 preset_prompt 预填到 userInput(用户可自由编辑),
// 并重置其他字段(避免上一场景的选择残留),同时根据推荐重置 skill 选中状态
watch(selectedScenario, () => {
  userInput.value = selectedScenarioDecl.value?.preset_prompt ?? ''
  taskTitle.value = ''
  repoUrl.value = ''
  branch.value = ''
  // 交付物来源回退到 Git 仓库 Tab,清掉残留的上传
  sourceMode.value = 'git'
  clearUploadedDeliverables()
  // 根据场景推荐重置 skill 选中状态(skill 列表已加载时才生效)
  applyRecommendedSkills()
  nextTick(autoResize)
})

// ---- 校验 ----

const repoUrlError = computed(() => {
  const v = repoUrl.value.trim()
  if (!v) return '' // 仓库地址可选(用户可只输入文字说明)
  if (!urlPattern.test(v)) return '请输入有效的仓库地址'
  return ''
})

const canSubmit = computed(() => {
  if (loading.value || uploading.value) return false
  const hasInput = userInput.value.trim().length > 0
  // 上传模式:不传 repo_url(user_input 后端必填),必须有文字说明
  if (sourceMode.value !== 'git') return hasInput
  const hasRepo = repoUrl.value.trim().length > 0 && !repoUrlError.value
  return hasInput || hasRepo
})

/** textarea placeholder(固定文案;场景降级后不再从场景声明读取) */
const chatPlaceholder = '请输入任务说明,如:审计这个仓库的安全风险,或分析代码质量'

// ---- 自动调整 textarea 高度 ----

const textareaRef = ref<HTMLTextAreaElement | null>(null)

function autoResize(): void {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  // 限制最大高度 ~240px,超过则内部滚动
  el.style.height = Math.min(el.scrollHeight, 240) + 'px'
}

watch(userInput, () => {
  nextTick(autoResize)
})

function onTextareaKeydown(e: KeyboardEvent): void {
  // Enter 提交,Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (canSubmit.value) handleSubmit()
  }
}

// ---- 提交 ----

async function handleSubmit(): Promise<void> {
  error.value = ''
  if (!canSubmit.value) return

  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    // 交付物来源:仅 Git Tab 采集仓库地址/分支(上传模式与 repo_url 后端互斥)
    const isGitMode = sourceMode.value === 'git'
    const repoUrlVal = isGitMode ? repoUrl.value.trim() : ''
    const branchVal = isGitMode ? branch.value.trim() : ''
    if (repoUrlVal) params.repo_url = repoUrlVal
    if (branchVal) params.branch = branchVal

    // CLI 模型配置(qoder_cli 写 model/reasoning_effort/context_window;
    // deepseek_cli 仅写 model/reasoning_effort,后端经 session/set_config_option 设置)
    if (selectedExecutor.value.startsWith('qoder_cli')) {
      if (qoderModel.value) params.model = qoderModel.value
      if (qoderReasoningEffort.value) params.reasoning_effort = qoderReasoningEffort.value
      if (qoderContextWindow.value) params.context_window = qoderContextWindow.value
    } else if (selectedExecutor.value === 'deepseek_cli') {
      if (deepseekModel.value) params.model = deepseekModel.value
      if (deepseekReasoningEffort.value) params.reasoning_effort = deepseekReasoningEffort.value
    }

    // Agent 策略配置(仅当用户改了用户级默认值时才提交,作为任务级覆盖)
    // 后端 resolve_agent_policy 会合并用户级默认 + 此任务级覆盖;
    // 比较基准 userPolicyDefaults 已在 onMounted 加载用户偏好,未配置时即系统默认
    const agentPolicy: Record<string, unknown> = {}
    if (policyAgent2Enabled.value !== userPolicyDefaults.value.agent2Enabled) {
      agentPolicy.agent2_enabled = policyAgent2Enabled.value
    }
    if (policyAllowVerify.value !== userPolicyDefaults.value.allowVerify) {
      agentPolicy.allow_verify = policyAllowVerify.value
    }
    if (Object.keys(agentPolicy).length > 0) {
      params._agent_policy = agentPolicy
    }

    // AI助手确认策略(任务级 _executor_command_confirm 覆盖)
    // 与 agent_policy 分离存储:后端 agent_policy.resolve_agent_policy 会把
    // executor_command_confirm_default 映射到 task.params._executor_command_confirm(若未显式设置);
    // 此处仅在用户改了用户级默认时显式提交,优先级最高。
    // builtin 与 CLI 执行器均生效:builtin 通过 ContextVar 注入到 run_command;CLI 走 ACP request_permission
    if (policyExecutorCommandConfirm.value !== userPolicyDefaults.value.executorCommandConfirm) {
      params._executor_command_confirm = policyExecutorCommandConfirm.value
    }

    // user_input 优先用用户主输入;若为空但仓库地址已填,自动兜底生成
    let finalUserInput = userInput.value.trim()
    if (!finalUserInput && repoUrlVal) {
      finalUserInput = `请处理这个仓库: ${repoUrlVal}`
    }
    if (!finalUserInput) {
      error.value = '请输入任务说明或仓库地址'
      return
    }

    // 后端立即返回 task_id(后台线程异步执行)
    // allowed_skills:全部勾选时不传(等同于全部可用);部分勾选时传选中数组
    const allowedSkillsPayload: string[] | undefined = allSkillsSelected.value
      ? undefined
      : Array.from(selectedSkillNames.value)

    // agent2 关闭(单 agent 模式)时验证相关字段一律不提交(验证是 agent2 的能力)
    const verifierOn = policyAllowVerify.value && policyAgent2Enabled.value

    const res = await createTask({
      scenario: selectedScenario.value,
      title: taskTitle.value.trim() || undefined,
      user_input: finalUserInput,
      // 上传交付物 ids:仅上传 Tab 且已上传时传(后端与 repo_url 互斥,422)
      upload_ids:
        sourceMode.value !== 'git' && uploadedDeliverables.value.length
          ? uploadedDeliverables.value.map((d) => d.upload_id)
          : undefined,
      // 评估模型是 agent2 的能力:agent2 关闭时不提交(builtin 下 react 模型已单独显式指定)
      llm_config_id: policyAgent2Enabled.value ? selectedLlmConfigId.value || undefined : undefined,
      // 仅 builtin 模式下传 react_llm_config_id;空串不传(后端回退到 llm_config_id)
      react_llm_config_id:
        selectedExecutor.value === 'builtin'
          ? (selectedReactLlmConfigId.value || undefined)
          : undefined,
      executor: selectedExecutor.value,
      allowed_skills: allowedSkillsPayload,
      // 测试环境 / 动态验证:仅当开启「允许自行验证」且 agent2 启用时提交(URL 留空则不传)
      test_env_url: verifierOn ? testEnvUrl.value.trim() || undefined : undefined,
      verifier_enabled: verifierOn,
      verifier_auth_mode: verifierOn ? verifierAuthMode.value : undefined,
      // 登录凭证:仅提交 label 和 header_value 都非空的项(过滤未填完的空行)
      verifier_auth_tokens: verifierOn
        ? verifierAuthTokens.value
            .filter((t) => t.label.trim() && t.header_value.trim())
            .map((t) => ({
              label: t.label.trim(),
              header_name: t.header_name.trim() || 'Authorization',
              header_value: t.header_value.trim(),
            }))
        : undefined,
      params,
    })

    // 立即跳转详情页,SSE 接收实时进度
    await router.push({ name: 'task-detail', params: { id: res.id } })
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

/** 模型选项的显示文本 */
function modelLabel(cfg: LLMConfigItemOut): string {
  const name = cfg.name || cfg.model
  return cfg.has_api_key ? name : `${name}(未配置 Key)`
}

// ---- BaseSelect 选项(computed,适配 {value,label} 结构) ----

/** agent2 评估模型选项 */
const llmConfigOptions = computed(() => [
  { value: '', label: useAgentExecutor.value ? '默认评估模型' : '默认模型' },
  ...llmConfigs.value.map((cfg) => ({ value: cfg.id, label: modelLabel(cfg) })),
])

/** agent1 模型选项(仅 builtin,空=同评估模型)。
 * agent2 关闭时评估模型不再存在,去掉「同评估模型」选项 */
const reactLlmConfigOptions = computed(() => [
  ...(policyAgent2Enabled.value ? [{ value: '', label: '同评估模型' }] : []),
  ...llmConfigs.value.map((cfg) => ({ value: cfg.id, label: modelLabel(cfg) })),
])

/** 执行器选项:内置 + 用户已配置且启用的 agent CLI */
const executorOptions = computed(() => [
  { value: 'builtin', label: '内置' },
  ...agentExecutors.value.map((a) => ({ value: a.agent_type, label: a.display_name })),
])

onMounted(async () => {
  document.addEventListener('keydown', onDrawerKeydown)
  try {
    // 并行拉取场景、模型、各 git provider 状态、技能、agent 配置
    // git provider 状态静默失败:未绑定不影响任务提交
    const [scenarioList, models, ghStatus, giteeStatus, skills, agentCfgs, prefs] = await Promise.all([
      getScenarios(),
      getMyModels().catch(() => null),
      getGitProviderStatus('github').catch(() => null),
      getGitProviderStatus('gitee').catch(() => null),
      getSkills().catch(() => null as SkillSummary[] | null), // 静默失败,无 skill 不阻塞提交
      getAgentConfigs().catch(() => null), // 静默失败,无 agent 配置不影响提交
      getPreferences().catch(() => null), // 静默失败:未配置/未登录时用系统默认策略
    ])
    // 用户级默认策略(智能体策略设置页保存的):填充为高级设置表单初始值,
    // 并同步为提交时的比较基准(未配置时表单保持系统默认,行为不变)
    if (prefs?.agent_policy) {
      const p = prefs.agent_policy
      policyAgent2Enabled.value = p.agent2_enabled
      policyAllowVerify.value = p.allow_verify
      // 测试环境授权模式默认值(任务级可单独覆盖)
      verifierAuthMode.value = p.verifier_auth_mode_default
      // CLI 命令确认模式默认值(任务级 _executor_command_confirm 可单独覆盖)
      policyExecutorCommandConfirm.value = p.executor_command_confirm_default ?? DEFAULT_POLICY.executor_command_confirm_default
      // 同步比较基准
      userPolicyDefaults.value = {
        agent2Enabled: policyAgent2Enabled.value,
        allowVerify: policyAllowVerify.value,
        verifierAuthMode: verifierAuthMode.value,
        executorCommandConfirm: policyExecutorCommandConfirm.value,
      }
    }
    scenarios.value = scenarioList
    if (scenarioList.length > 0) {
      selectedScenario.value = scenarioList[0].id
    }
    if (models && models.llm_configs.length > 0) {
      llmConfigs.value = models.llm_configs
      // 默认选第一个已配置 Key 的,否则选第一个
      const firstWithKey = models.llm_configs.find((c) => c.has_api_key)
      selectedLlmConfigId.value = (firstWithKey ?? models.llm_configs[0]).id
    }
    // 记录各 provider 绑定状态(用于决定走下拉选择还是纯输入框)
    if (ghStatus) providerStatus.value.github = { bound: ghStatus.bound }
    if (giteeStatus) providerStatus.value.gitee = { bound: giteeStatus.bound }
    // 已绑定任一平台:预加载仓库列表合并到下拉(失败由弹窗提示)
    if (anyProviderBound.value) loadAllRepos()
    // skill 列表加载成功后,默认全选;若已选场景则按推荐重置
    if (skills && skills.length > 0) {
      allSkills.value = skills
      selectedSkillNames.value = new Set(skills.map((s) => s.name))
      applyRecommendedSkills()
    }
    // 仅展示已启用且已配置凭据的 agent 作为可选执行器
    if (agentCfgs) {
      agentExecutors.value = agentCfgs.configs.filter((c) => c.is_active && c.has_credentials)
    }
  } catch {
    // 场景拉取失败兜底(不应发生,保留旧默认以便能提交)
    selectedScenario.value = 'general'
  } finally {
    loadingModels.value = false
  }
  nextTick(() => {
    autoResize()
    // 自动聚焦输入框
    textareaRef.value?.focus()
  })
})

onUnmounted(() => {
  document.removeEventListener('keydown', onDrawerKeydown)
})
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          @toggle="toggleWorkspace"
        />
      </template>
    </AppHeader>

    <div class="page-body" :class="{ 'panel-open': drawerOpen }">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <div class="main-col">
          <!-- 顶部配置区:三行布局(场景 / agent2 模型 / agent1 设置) -->
          <div class="topbar">
            <!-- 第 1 行:场景(无标签,直接靠左) -->
            <div class="config-row config-row-scenario">
              <div
                class="scenario-segmented"
                role="tablist"
                aria-label="场景选择"
                data-onboarding="create-scenario"
              >
                <button
                  v-for="s in scenarios"
                  :key="s.id"
                  type="button"
                  :class="['seg-btn', { active: selectedScenario === s.id }]"
                  role="tab"
                  :aria-selected="selectedScenario === s.id"
                  @click="selectedScenario = s.id"
                >{{ s.name }}</button>
                <span v-if="scenarios.length === 0" class="seg-loading">场景加载中...</span>
              </div>
            </div>
  
            <!-- 第 2 行:agent1(AI助手)设置(执行器 + CLI 模型配置 / 技能;检查助手已并入「高级设置」抽屉) -->
            <div class="config-row">
              <div class="config-label-group">
                <span class="agent-avatar avatar-agent1" aria-hidden="true">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                    <!-- 机器人头部 -->
                    <rect x="4" y="7" width="16" height="12" rx="3" />
                    <!-- 天线 -->
                    <line x1="12" y1="3" x2="12" y2="7" />
                    <circle cx="12" cy="3" r="1.2" fill="currentColor" stroke="none" />
                    <!-- 双眼 -->
                    <circle cx="9" cy="13" r="1.2" fill="currentColor" stroke="none" />
                    <circle cx="15" cy="13" r="1.2" fill="currentColor" stroke="none" />
                    <!-- 底部支架/底座 -->
                    <line x1="8" y1="19" x2="8" y2="21" />
                    <line x1="16" y1="19" x2="16" y2="21" />
                  </svg>
                </span>
                <span class="config-label">AI助手</span>
              </div>
              <div class="react-controls" data-onboarding="create-react-executor">
                <!-- 执行器选择(下拉框):内置 + 用户已配置且启用的 agent CLI -->
                <div class="executor-select">
                  <BaseSelect
                    v-model="selectedExecutor"
                    :options="executorOptions"
                    aria-label="执行器选择"
                  />
                </div>
  
              <!-- agent1 模型(仅 builtin:可与 agent2 用不同模型;空=同评估模型) -->
              <div v-if="!useAgentExecutor" class="model-select react-model-select">
                <BaseSelect
                  v-model="selectedReactLlmConfigId"
                  :options="reactLlmConfigOptions"
                  :disabled="loadingModels"
                  aria-label="AI助手模型"
                />
              </div>
  
                <!-- CLI 模型配置(qoder_cli / deepseek_cli 执行器显示) -->
                <div v-if="supportsModelConfig" class="qoder-config-panel">
                  <button
                    type="button"
                    class="qoder-config-toggle"
                    :aria-expanded="qoderConfigOpen"
                    @click="qoderConfigOpen = !qoderConfigOpen"
                  >
                    <svg
                      class="qoder-chevron"
                      :class="{ expanded: qoderConfigOpen }"
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <polyline points="9 18 15 12 9 6" />
                    </svg>
                    <span>{{ isDeepseekCli ? 'DeepSeek 模型' : 'Qoder 模型' }}</span>
                    <span class="qoder-config-summary">
                      {{ modelConfigSummary }}
                    </span>
                  </button>

                  <Transition name="collapse">
                    <div v-show="qoderConfigOpen" class="qoder-config-dropdown">
                      <div class="qoder-config-row">
                        <label class="qoder-config-label">模型</label>
                        <BaseSelect
                          v-model="selectedModel"
                          :options="modelOptions"
                          size="sm"
                          class="qoder-config-select"
                        />
                      </div>
                      <div class="qoder-config-row">
                        <label class="qoder-config-label">思考强度</label>
                        <BaseSelect
                          v-model="selectedReasoningEffort"
                          :options="effortOptions"
                          size="sm"
                          class="qoder-config-select"
                        />
                      </div>
                      <!-- 上下文窗口仅 Qoder 支持(dsh 无对应配置项) -->
                      <div v-if="!isDeepseekCli" class="qoder-config-row">
                        <label class="qoder-config-label">上下文窗口</label>
                        <BaseSelect
                          v-model.number="qoderContextWindow"
                          :options="qoderContextOptions"
                          size="sm"
                          class="qoder-config-select"
                        />
                      </div>
                    </div>
                  </Transition>
                </div>
  
                <!-- 技能设置入口(内容在右侧设置抽屉;仅内置执行器显示:CLI 用自身工具系统,本地 skill 无效) -->
                <button
                  v-if="!useAgentExecutor && allSkills.length > 0"
                  type="button"
                  class="drawer-toggle"
                  :aria-expanded="drawerOpen && drawerSection === 'skills'"
                  :title="drawerTitle('skills', '技能')"
                  @click="openDrawer('skills')"
                >
                  <svg
                    class="toggle-icon"
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
                    <!-- 星光(技能/专长) -->
                    <path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z" />
                  </svg>
                  <span>技能</span>
                  <span class="advanced-summary">
                    {{ selectedSkillCount }}/{{ allSkills.length }}
                  </span>
                  <svg
                    class="advanced-chevron"
                    :class="{ expanded: drawerOpen && drawerSection === 'skills' }"
                    width="12"
                    height="12"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                </button>

                <template v-if="!useAgentExecutor && allSkills.length > 0">
                  <Teleport defer to="#settings-drawer-body">
                    <div v-show="drawerOpen && drawerSection === 'skills'" class="drawer-section-body">
                    <div class="skill-header">
                      <label class="skill-select-all">
                        <input
                          type="checkbox"
                          :checked="allSkillsSelected"
                          @change="toggleAllSkills"
                        />
                        <span>全选 / 全不选</span>
                      </label>
                      <p class="skill-hint">
                        勾选的技能将作为 AI助手可调用的专家知识。
                        全选=不限制(默认);部分勾选=仅允许选中的;全不选=不启用任何技能。
                      </p>
                    </div>
  
                    <div class="skill-list">
                      <label
                        v-for="skill in allSkills"
                        :key="skill.name"
                        class="skill-item"
                        :class="{ checked: selectedSkillNames.has(skill.name) }"
                      >
                        <input
                          type="checkbox"
                          :checked="selectedSkillNames.has(skill.name)"
                          @change="toggleSkill(skill.name)"
                        />
                        <div class="skill-info">
                          <span class="skill-name">{{ skill.name }}</span>
                          <span class="skill-desc">{{ skill.description }}</span>
                        </div>
                      </label>
                    </div>
                    </div>
                  </Teleport>
                </template>
              </div>
            </div>
  
            <!-- 高级设置抽屉入口(合并原「检查助手」行 + 协作策略:agent2 启停/评估模型/验证/命令确认;始终展示) -->
            <div class="config-row config-row-scenario">
              <button
                type="button"
                class="drawer-toggle"
                :aria-expanded="drawerOpen && drawerSection === 'policy'"
                :title="drawerTitle('policy', '高级设置')"
                data-onboarding="create-user-model"
                @click="openDrawer('policy')"
              >
                <svg
                  class="toggle-icon"
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
                  <!-- 滑块(策略调节) -->
                  <line x1="21" y1="4" x2="14" y2="4" />
                  <line x1="10" y1="4" x2="3" y2="4" />
                  <line x1="21" y1="12" x2="12" y2="12" />
                  <line x1="8" y1="12" x2="3" y2="12" />
                  <line x1="21" y1="20" x2="16" y2="20" />
                  <line x1="12" y1="20" x2="3" y2="20" />
                  <line x1="14" y1="2" x2="14" y2="6" />
                  <line x1="8" y1="10" x2="8" y2="14" />
                  <line x1="16" y1="18" x2="16" y2="22" />
                </svg>
                <span>高级设置</span>
                <span class="advanced-summary">
                  {{ !policyAgent2Enabled ? '单智能体' : (policyAllowVerify ? '可自行验证' : '默认') }}
                </span>
                <svg
                  class="advanced-chevron"
                  :class="{ expanded: drawerOpen && drawerSection === 'policy' }"
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </button>
  
                <Teleport defer to="#settings-drawer-body">
                  <div v-show="drawerOpen && drawerSection === 'policy'" class="drawer-section-body">
                    <!-- 分组:检查助手(原 topbar 第 2 行整体移入) -->
                    <div class="adv-group">
                      <div class="adv-group-title">
                        <span class="agent-avatar avatar-agent2" aria-hidden="true">
                          <BrandLogo :size="20" variant="agent2" />
                        </span>
                        <span>检查助手</span>
                      </div>

                      <label
                        class="policy-toggle-row policy-toggle-primary"
                        :title="policyAgent2Enabled ? '检查助手后台质检 / 验证' : '单 agent 模式:AI助手 跑 1 轮直接产出结果'"
                      >
                        <input v-model="policyAgent2Enabled" class="switch" type="checkbox" />
                        <span>{{ policyAgent2Enabled ? '已启用' : '已停用' }}</span>
                      </label>

                      <label class="policy-field">
                        <span class="policy-label">{{ modelSelectLabel }}</span>
                        <BaseSelect
                          v-model="selectedLlmConfigId"
                          :options="llmConfigOptions"
                          :disabled="loadingModels || !policyAgent2Enabled"
                          class="policy-select"
                          :aria-label="modelSelectLabel"
                        />
                        <RouterLink
                          v-if="llmConfigs.length === 0 && !loadingModels"
                          to="/settings/models"
                          class="model-empty-link"
                        >配置 →</RouterLink>
                      </label>

                      <!-- 单 agent 模式提示:agent2 关闭时说明下方依赖字段为何隐藏 -->
                      <p v-if="!policyAgent2Enabled" class="policy-single-hint">
                        当前为单 agent 模式:AI助手 跑 1 轮直接产出结果,不做覆盖度评估与验证。
                      </p>

                    <!-- agent2 依赖字段:关闭时整组隐藏(v-show 保留值,提交 payload 不变) -->
                    <Transition name="collapse">
                      <div v-show="policyAgent2Enabled" class="policy-dependent">
                    <label class="policy-toggle-row">
                      <input v-model="policyAllowVerify" class="switch" type="checkbox" />
                      <span>允许检查助手自行验证 <span class="policy-experimental">(实验性)</span></span>
                    </label>

                    <!-- 测试环境设置:仅当开启「允许自行验证」时展开 -->
                    <Transition name="collapse">
                      <div v-show="policyAllowVerify" class="verifier-config">
                        <label class="policy-field">
                          <span class="policy-label">测试环境 URL</span>
                          <input
                            v-model.trim="testEnvUrl"
                            type="url"
                            class="policy-input"
                            placeholder="http://localhost:3000(已部署的应用地址)"
                          />
                          <span class="policy-hint">检查助手将在此环境动态验证安全发现</span>
                        </label>

                        <label class="policy-field">
                          <span class="policy-label">授权模式</span>
                          <BaseSelect
                            v-model="verifierAuthMode"
                            :options="verifierAuthModeOptions"
                            class="policy-select"
                            aria-label="授权模式"
                          />
                          <span class="policy-hint">控制验证动作执行前是否需要用户确认</span>
                        </label>

                        <!-- 登录凭证列表(可选):LLM 按 auth_profile=label 选择身份,
                             工具自动注入对应请求头。用于越权测试(同一端点不同身份访问)。
                             LLM 只看到 label,看不到 header_value(安全)。 -->
                        <div class="auth-tokens-section">
                          <div class="auth-tokens-header">
                            <span class="policy-label">登录凭证 <span class="policy-optional">(可选)</span></span>
                            <button type="button" class="auth-token-add-btn" @click="addAuthToken">
                              + 添加身份
                            </button>
                          </div>
                          <span class="policy-hint">
                            配置不同身份的认证头,LLM 验证越权时会按需选择(如:管理员 vs 普通用户访问同一端点)
                          </span>

                          <div
                            v-for="(token, idx) in verifierAuthTokens"
                            :key="idx"
                            class="auth-token-row"
                          >
                            <input
                              v-model.trim="token.label"
                              type="text"
                              class="auth-token-input auth-token-label"
                              placeholder="身份名(如 管理员)"
                            />
                            <input
                              v-model.trim="token.header_name"
                              type="text"
                              class="auth-token-input auth-token-header-name"
                              placeholder="Header 名"
                              list="auth-header-suggestions"
                            />
                            <input
                              v-model.trim="token.header_value"
                              type="text"
                              class="auth-token-input auth-token-header-value"
                              placeholder="Header 值(如 Bearer xxx)"
                            />
                            <button
                              type="button"
                              class="auth-token-remove-btn"
                              aria-label="删除"
                              @click="removeAuthToken(idx)"
                            >×</button>
                          </div>

                          <datalist id="auth-header-suggestions">
                            <option value="Authorization" />
                            <option value="Cookie" />
                            <option value="X-API-Key" />
                            <option value="X-Auth-Token" />
                          </datalist>
                        </div>
                      </div>
                    </Transition>
                      </div>
                    </Transition>
                    </div>
                    <!-- /分组:检查助手 -->

                    <!-- 分组:执行(独立于 agent2) -->
                    <div class="adv-group">
                      <div class="adv-group-title">执行</div>

                    <!-- AI助手确认策略(builtin 与 CLI 执行器均生效) -->
                    <label class="policy-field">
                      <span class="policy-label">AI助手确认策略</span>
                      <BaseSelect
                        v-model="policyExecutorCommandConfirm"
                        :options="executorConfirmOptions"
                        class="policy-select"
                        aria-label="AI助手确认策略"
                      />
                      <span class="policy-hint">控制 AI助手(内置 / CLI)执行危险命令时是否弹窗确认。CLI 中 Codex 受非交互模式限制,仅支持自动批准。</span>
                    </label>
                    </div>
                    <!-- /分组:执行 -->
                  </div>
                </Teleport>
            </div>
          </div>
  
          <!-- 错误提示 -->
          <Transition name="fade">
            <div v-if="error" class="alert alert-error" role="alert">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="8" x2="12" y2="12" />
                <line x1="12" y1="16" x2="12.01" y2="16" />
              </svg>
              <span>{{ error }}</span>
            </div>
          </Transition>
  
          <!-- 对话式输入框 -->
          <div class="chat-card">
            <!-- 任务标题(可选) -->
            <div class="title-row">
              <input
                v-model.trim="taskTitle"
                type="text"
                class="title-input"
                maxlength="255"
                placeholder="任务标题(可选,便于在历史列表识别)"
                aria-label="任务标题"
              />
            </div>
  
            <!-- 分隔线 -->
            <div class="chat-divider" />
  
            <!-- 主体:大 textarea -->
            <textarea
              ref="textareaRef"
              v-model="userInput"
              class="chat-input"
              rows="3"
              :placeholder="chatPlaceholder"
              data-onboarding="create-input"
              @keydown="onTextareaKeydown"
            />
  
            <!-- 分隔线 -->
            <div class="chat-divider" />
  
            <!-- 底部:仓库地址 + 分支 + 提交按钮 -->
            <div class="chat-footer">
              <!-- 交付物来源区:Git 仓库 / 上传 ZIP / 上传单文件 三选一 -->
              <div class="repo-area">
                <!-- 来源切换 Tab -->
                <div class="source-tabs" role="tablist" aria-label="交付物来源">
                  <button
                    type="button"
                    class="source-tab"
                    :class="{ active: sourceMode === 'git' }"
                    role="tab"
                    :aria-selected="sourceMode === 'git'"
                    @click="switchSourceMode('git')"
                  >
                    Git 仓库
                  </button>
                  <button
                    type="button"
                    class="source-tab"
                    :class="{ active: sourceMode === 'zip' }"
                    role="tab"
                    :aria-selected="sourceMode === 'zip'"
                    @click="switchSourceMode('zip')"
                  >
                    上传 ZIP
                  </button>
                  <button
                    type="button"
                    class="source-tab"
                    :class="{ active: sourceMode === 'file' }"
                    role="tab"
                    :aria-selected="sourceMode === 'file'"
                    @click="switchSourceMode('file')"
                  >
                    上传文件
                  </button>
                </div>

                <!-- Git Tab:仓库输入/选择区(可输入下拉框,已绑定任一平台时可从仓库列表选;否则纯输入) -->
                <div v-if="sourceMode === 'git'" class="repo-input-row">
                  <!-- 已绑定任一平台:可输入 + 下拉选择仓库(含 GitHub / Gitee 私有仓库) -->
                  <ModelCombobox
                    v-if="anyProviderBound"
                    :model-value="repoUrl"
                    :options="repoComboboxOptions"
                    :disabled="reposLoading"
                    :placeholder="reposLoading ? '加载仓库列表...' : '选择或输入仓库地址(GitHub / Gitee)'"
                    @update:model-value="repoUrl = $event"
                  />
                  <!-- 未绑定:纯输入框 -->
                  <input
                    v-else
                    v-model.trim="repoUrl"
                    type="url"
                    class="repo-input"
                    :class="{ invalid: repoUrlError }"
                    placeholder="https://github.com/owner/repo"
                    aria-label="仓库地址"
                  />
                  <!-- 刷新仓库列表按钮(强制跳过后端缓存,用于在平台上新建仓库后立即拉取) -->
                  <button
                    v-if="anyProviderBound"
                    type="button"
                    class="repo-refresh-btn"
                    :disabled="reposLoading"
                    :title="reposLoading ? '加载中...' : '刷新仓库列表'"
                    aria-label="刷新仓库列表"
                    @click="refreshRepos"
                  >
                    <!-- 旋转动画:loading 时加 .spinning class -->
                    <svg
                      class="refresh-icon"
                      :class="{ spinning: reposLoading }"
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                      <path d="M3 3v5h5" />
                      <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
                      <path d="M16 16h5v5" />
                    </svg>
                  </button>
                  <input
                    v-model.trim="branch"
                    type="text"
                    class="branch-input"
                    placeholder="默认分支"
                    aria-label="分支"
                  />
                </div>

                <!-- 上传 Tab:已上传展示 chip 列表,可随时追加更多 -->
                <template v-else>
                  <!-- 已上传列表:每项 文件名 + 大小(ZIP 含解压文件数)+ 移除按钮 -->
                  <div v-if="uploadedDeliverables.length" class="upload-chips">
                    <div
                      v-for="d in uploadedDeliverables"
                      :key="d.upload_id"
                      class="upload-chip"
                    >
                      <span class="upload-chip-name" :title="d.filename">
                        {{ d.filename }}
                      </span>
                      <span class="upload-chip-meta">
                        {{ formatBytes(d.size) }}
                        <template v-if="d.kind === 'zip'">
                          · {{ d.file_count }} 个文件
                        </template>
                      </span>
                      <button
                        type="button"
                        class="upload-chip-remove"
                        aria-label="移除已上传文件"
                        :title="'移除 ' + d.filename"
                        @click="removeDeliverable(d.upload_id)"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                  <!-- 选择/追加文件(多次选择可累积) -->
                  <button
                    type="button"
                    class="upload-btn"
                    :disabled="uploading"
                    @click="triggerFilePicker"
                  >
                    <span v-if="uploading" class="spinner" />
                    {{
                      uploading
                        ? '上传中...'
                        : uploadedDeliverables.length
                          ? '继续添加文件'
                          : sourceMode === 'zip'
                            ? '选择 .zip 压缩包'
                            : '选择要上传的文件'
                    }}
                  </button>
                  <p v-if="uploadError" class="upload-error">{{ uploadError }}</p>
                </template>

                <!-- 隐藏文件选择框(常驻渲染,多选;保证 triggerFilePicker 随时可点) -->
                <input
                  ref="zipFileInputRef"
                  type="file"
                  accept=".zip,application/zip"
                  multiple
                  class="hidden-file-input"
                  aria-hidden="true"
                  tabindex="-1"
                  @change="onUploadFileChosen($event, true)"
                />
                <input
                  ref="singleFileInputRef"
                  type="file"
                  multiple
                  class="hidden-file-input"
                  aria-hidden="true"
                  tabindex="-1"
                  @change="onUploadFileChosen($event, false)"
                />
              </div>
  
              <!-- 发送按钮 -->
              <button
                type="button"
                class="send-btn"
                :disabled="!canSubmit"
                :title="loading ? '处理中...' : '开始任务 (Enter)'"
                data-onboarding="create-send"
                aria-label="开始任务"
                @click="handleSubmit"
              >
                <span v-if="loading" class="spinner" />
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
                  <line x1="22" y1="2" x2="11" y2="13" />
                  <polygon points="22 2 15 22 11 13 2 9 22 2" />
                </svg>
              </button>
            </div>
          </div>
  
          <!-- 操作提示 -->
          <p class="chat-tip">
            <kbd>Enter</kbd> 发送 ·
            <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行
          </p>
  
          <!-- skill 加载错误提示(静默,不阻塞) -->
          <p v-if="skillsError" class="skill-load-error">
            技能列表加载失败:{{ skillsError }}(不影响任务提交)
          </p>
        </div>
      </main>

      <!-- 右侧设置面板:内嵌在页面布局中,展开时主内容区相应收窄往左移。
           当前分区表单由各入口处的 defer Teleport 注入(v-show 控制显隐) -->
      <aside v-show="drawerOpen" class="settings-panel" role="complementary" aria-label="任务设置">
        <div id="settings-drawer-body" class="panel-body" />
      </aside>
    </div>

    <!-- 仓库提示弹窗(加载失败/无仓库/未绑定) -->
    <Teleport to="body">
      <Transition name="dialog-fade">
        <div v-if="repoDialogOpen" class="repo-dialog-mask" @click.self="closeRepoDialog">
          <div class="repo-dialog-card" role="dialog" aria-modal="true">
            <header class="repo-dialog-header">
              <h3>提示</h3>
              <button
                class="repo-dialog-close"
                aria-label="关闭"
                @click="closeRepoDialog"
              >×</button>
            </header>
            <div class="repo-dialog-body">
              <p class="repo-dialog-message">{{ repoDialogMessage }}</p>
              <RouterLink
                v-if="repoDialogShowBindLink"
                to="/settings"
                class="repo-dialog-link"
                @click="closeRepoDialog"
              >前往设置绑定 →</RouterLink>
            </div>
            <footer class="repo-dialog-footer">
              <button class="btn btn-primary" @click="closeRepoDialog">知道了</button>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  /* 手机地址栏伸缩兜底 */
  height: 100dvh;
  overflow: hidden;
  background: var(--color-bg);
}

.page-body {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
  overflow: hidden;
  /* 窄屏时设置面板以本容器为定位基准覆盖显示 */
  position: relative;
}

.main {
  flex: 1;
  min-width: 0;
  /* 全宽滚动容器:垂直滚动条贴界面右边,内容在 main-col 内居中 */
  overflow-y: auto;
  /* flex 列容器:配合 main-col 的 margin auto 实现内容不满屏时垂直居中 */
  display: flex;
  flex-direction: column;
}

/* 内容列:在滚动容器内水平 + 垂直居中(与技能管理 / CLI 设置 / 智能体策略一致);
   内容超高时 margin auto 退化为 0,自动改为顶部对齐可滚动,不会被裁剪。
   width: 100% + max-width 保持原块级流的宽度行为(flex 下 auto 边距会取消拉伸) */
.main-col {
  width: 100%;
  max-width: 768px;
  margin: auto;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
  padding: var(--space-6) var(--space-6) var(--space-8);
}

/* ---- 顶部配置区:三行布局(场景 / agent2 模型 / agent1 设置) ---- */
.topbar {
  display: flex;
  flex-direction: column;
  margin-bottom: var(--space-4);
}

/* 单行配置:左侧标签 + 右侧控件 */
.config-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  padding: var(--space-2) 0;
}

/* 第 1 行场景:无标签,直接靠左 */
.config-row-scenario {
  padding-left: 0;
}

.config-label-group {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
  /* 固定宽度,确保三行控件起点严格对齐(容纳头像 + 标签文字) */
  width: 116px;
}

.config-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* agent 头像:圆形徽标 + 白色图标 */
.agent-avatar {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}

.avatar-agent2 {
  background: transparent;
  border-radius: 0;
}

.avatar-agent1 {
  background: #7c3aed;
}

/* 第 3 行 agent1 控件容器:横向排列执行器 + CLI 配置 / 技能 */
.react-controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.scenario-segmented {
  display: inline-flex;
  align-items: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  padding: 3px;
  gap: 2px;
}

.seg-btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.seg-btn:hover {
  color: var(--color-text);
}

.seg-btn.active {
  color: var(--color-text-inverse);
  background: var(--color-primary);
}

.seg-loading {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
}

.model-select {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* BaseSelect 宽度约束(高度/边框/内边距由组件内部 size="md" 处理) */
.model-select .base-select {
  max-width: 200px;
}

/* ---- 执行器选择(下拉框) ---- */
.executor-select {
  display: inline-flex;
}

.executor-select .base-select {
  min-width: 120px;
}

.model-empty-link {
  font-size: var(--fs-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.model-empty-link:hover {
  text-decoration: underline;
}

/* ---- 错误提示 ---- */
.alert {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert svg {
  flex-shrink: 0;
  margin-top: 2px;
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid var(--color-alert-error-border);
}

/* ---- 对话式输入框 ---- */
.chat-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.chat-card:focus-within {
  border-color: var(--color-primary-border);
  box-shadow: 0 0 0 3px var(--color-primary-light), var(--shadow-md);
}

.chat-input {
  width: 100%;
  min-height: 96px;
  max-height: 240px;
  padding: 0;
  font-size: var(--fs-base);
  font-family: var(--font-sans);
  color: var(--color-text);
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  line-height: var(--lh-relaxed);
  overflow-y: auto;
}

.chat-input::placeholder {
  color: var(--color-text-muted);
}

/* ---- 任务标题输入(可选) ---- */
.title-row {
  display: flex;
}

.title-input {
  width: 100%;
  height: 32px;
  padding: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  font-family: var(--font-sans);
  color: var(--color-text);
  background: transparent;
  border: none;
  outline: none;
  transition: color var(--transition-fast);
}

.title-input::placeholder {
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
}

.chat-divider {
  height: 1px;
  background: var(--color-border);
  margin: 0 calc(-1 * var(--space-4));
}

.chat-footer {
  display: flex;
  align-items: flex-end;
  gap: var(--space-3);
}

/* ---- 仓库输入区 ---- */
.repo-area {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.repo-input-row {
  display: flex;
  gap: var(--space-2);
}

/* repo 下拉框撑满(已绑定时);纯输入框同样撑满 */
.repo-input-row .combobox {
  flex: 1;
  min-width: 0;
}

/* ---- 交付物来源 Tab ---- */

.source-tabs {
  display: flex;
  gap: var(--space-1);
}

.source-tab {
  padding: 1px 10px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast),
    border-color var(--transition-fast);
}

.source-tab:hover {
  color: var(--color-text);
}

.source-tab.active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

/* ---- 上传交付物 ---- */

/* 未上传:虚线选择按钮 */
.upload-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  width: 100%;
  height: 36px;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition:
    border-color var(--transition-fast),
    color var(--transition-fast);
}

.upload-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.upload-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* spinner 默认配色面向深色 send 按钮,浅色上传按钮内需换深色 */
.upload-btn .spinner {
  border-color: color-mix(in srgb, var(--color-text-secondary) 30%, transparent);
  border-top-color: var(--color-text-secondary);
}

/* 已上传 chip 列表(多文件纵向堆叠) */
.upload-chips {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* 已上传:文件信息 chip */
.upload-chip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 36px;
  padding: 0 var(--space-2) 0 var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.upload-chip-name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
  color: var(--color-text);
}

.upload-chip-meta {
  flex: 1;
  text-align: right;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.upload-chip-remove {
  flex: none;
  width: 22px;
  height: 22px;
  font-size: var(--fs-sm);
  line-height: 1;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast);
}

.upload-chip-remove:hover {
  color: var(--color-danger);
  background: var(--color-danger-light);
}

.upload-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* 隐藏文件输入框(视觉隐藏但保持可编程点击) */
.hidden-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  overflow: hidden;
}

.repo-input,
.branch-input {
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  outline: none;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.repo-input {
  flex: 1;
  min-width: 0;
}

.branch-input {
  flex: 0 0 120px;
}

.repo-input:focus,
.branch-input:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.repo-input.invalid {
  border-color: var(--color-danger);
  box-shadow: 0 0 0 3px var(--color-danger-light);
}

/* 刷新仓库列表按钮 */
.repo-refresh-btn {
  flex: 0 0 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  outline: none;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}

.repo-refresh-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.repo-refresh-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* loading 时图标持续旋转 */
.refresh-icon.spinning {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* ---- 仓库提示弹窗 ---- */
.repo-dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-4);
}

.repo-dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.15);
  width: 100%;
  max-width: 420px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.repo-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.repo-dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.repo-dialog-close {
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

.repo-dialog-close:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.repo-dialog-body {
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.repo-dialog-message {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0;
  line-height: 1.6;
}

.repo-dialog-link {
  font-size: var(--fs-sm);
  color: var(--color-primary);
  text-decoration: none;
  font-weight: var(--fw-medium);
}

.repo-dialog-link:hover {
  text-decoration: underline;
}

.repo-dialog-footer {
  display: flex;
  justify-content: flex-end;
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.repo-dialog-footer .btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
}

.repo-dialog-footer .btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.repo-dialog-footer .btn-primary:hover {
  filter: brightness(1.05);
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

/* ---- 发送按钮 ---- */
.send-btn {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.send-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  background: var(--color-text-muted);
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 提示 ---- */
.chat-tip {
  margin: var(--space-3) 0 0;
  text-align: center;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.chat-tip kbd {
  display: inline-block;
  padding: 1px 6px;
  font-size: var(--fs-xs);
  font-family: var(--font-sans);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: 0 1px 0 var(--color-border-strong);
}

/* ---- 过渡 ---- */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-base);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* 面板展开时:中间内容不再绝对居中,与设置面板保持 space-5 + main-col 右内边距
   的间距,两侧留白视觉上对称等宽 */
.page-body.panel-open .main-col {
  margin-left: auto;
  margin-right: var(--space-5);
  /* 宽度先扣掉右侧留白,与原块级流行为一致,避免窄屏时水平溢出 */
  width: calc(100% - var(--space-5));
}

/* ---- 小屏适配 ---- */
/* 窄屏:主区已不够宽,设置面板改为覆盖在内容上方 */
@media (max-width: 1100px) {
  .settings-panel {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 901;
    width: min(400px, 100%);
    box-shadow: var(--shadow-xl);
  }
}

@media (max-width: 640px) {
  /* 内容列内边距收紧 */
  .main-col {
    padding: var(--space-4) var(--space-3) var(--space-6);
  }

  /* 窄屏每行标签与控件上下排列 */
  .config-row {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--space-1);
  }

  .config-label-group {
    min-width: 0;
  }

  .model-select {
    flex: 1;
    width: 100%;
  }

  .model-select .base-select {
    max-width: 100%;
    flex: 1;
  }

  .branch-input {
    flex: 0 0 96px;
  }

  .qoder-config-dropdown {
    min-width: 0;
    max-width: calc(100vw - var(--space-6) * 2);
  }
}

/* ---- Qoder CLI 模型配置(下拉浮层,与高级选项同模式) ---- */
.qoder-config-panel {
  position: relative;
}

.qoder-config-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.qoder-config-toggle:hover {
  color: var(--color-text);
  border-color: var(--color-primary-border);
}

.qoder-config-toggle[aria-expanded="true"] {
  color: var(--color-primary);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.qoder-chevron {
  flex-shrink: 0;
  transition: transform var(--transition-fast);
  color: var(--color-text-muted);
}

.qoder-chevron.expanded {
  transform: rotate(90deg);
}

.qoder-config-summary {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
}

.qoder-config-dropdown {
  position: absolute;
  top: calc(100% + var(--space-2));
  right: 0;
  z-index: var(--z-dropdown, 100);
  min-width: 320px;
  max-width: min(80vw, 480px);
  padding: var(--space-3) var(--space-4) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg, 0 10px 25px rgba(0, 0, 0, 0.12));
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.qoder-config-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.qoder-config-label {
  flex-shrink: 0;
  width: 80px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
}

/* BaseSelect 根容器:仅需撑满 flex 行;高度/边框/内边距由组件内部 size="sm" 处理 */
.qoder-config-select {
  flex: 1;
}

/* ---- 设置抽屉入口按钮(高级设置 / 技能) ---- */
.drawer-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.drawer-toggle:hover {
  color: var(--color-text);
  border-color: var(--color-primary-border);
}

.drawer-toggle[aria-expanded="true"] {
  color: var(--color-primary);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

/* 分区专属图标(高级设置=滑块 / 技能=星光),强化「点击可设置」的入口感 */
.toggle-icon {
  flex-shrink: 0;
  color: var(--color-text-muted);
  transition: color var(--transition-fast);
}

/* 展开中:图标与箭头跟随主题色,与按钮高亮态一致 */
.drawer-toggle[aria-expanded="true"] .toggle-icon,
.drawer-toggle[aria-expanded="true"] .advanced-chevron {
  color: var(--color-primary);
}

.advanced-chevron {
  flex-shrink: 0;
  /* 箭头统一推到按钮右端:三个入口的状态指示位置对齐 */
  margin-left: auto;
  transition: transform var(--transition-fast), color var(--transition-fast);
  color: var(--color-text-muted);
}

/* 展开态:箭头转向(右→左),暗示「再次点击收起」 */
.advanced-chevron.expanded {
  transform: rotate(180deg);
}

.advanced-summary {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
  /* 摘要过长时省略,不挤掉右端箭头 */
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- 右侧内嵌设置面板 ---- */
.settings-panel {
  position: relative;
  width: 400px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--color-bg);
}

.panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  /* 左右留白 = space-5 + space-6,与中间内容 ↔ 面板的间距等宽 */
  padding: var(--space-5) calc(var(--space-5) + var(--space-6));
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* 当前分区表单(由各入口处的 Teleport 注入):无边框卡片,垂直居中;
   内容超高时 margin auto 退化为 0,自动改为顶部对齐可滚动,不会被裁剪 */
.drawer-section-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  width: 100%;
  margin: auto 0;
}

/* 单 agent 模式提示(agent2 关闭时展示) */
.policy-single-hint {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

/* agent2 依赖字段容器:作为 drawer-section-body 的单个 flex 项,需恢复内部字段间距 */
.policy-dependent {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* 高级设置抽屉内分组(检查助手 / 执行) */
.adv-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.adv-group + .adv-group {
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.adv-group-title {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
}

/* 检查助手启停(高级设置抽屉主开关) */
.policy-toggle-primary {
  padding: var(--space-2) var(--space-3);
  margin: 0;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-weight: var(--fw-medium);
}

/* 设置面板内技能列表改单列,卡片更舒展 */
.drawer-section-body .skill-list {
  grid-template-columns: 1fr;
}

/* 设置面板内登录凭证行:空间不足时换行(身份名+删除一行,header 名/值一行) */
.drawer-section-body .auth-token-row {
  flex-wrap: wrap;
}

.drawer-section-body .auth-token-label {
  flex: 1 1 160px;
}

.drawer-section-body .auth-token-header-name {
  flex: 1 1 45%;
}

.drawer-section-body .auth-token-header-value {
  flex: 1 1 45%;
}

/* ---- Agent 策略配置面板(设置面板内分区内容) ---- */
.policy-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
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

/* 字段头部:标签 + 帮助按钮(问号) */
.field-head {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

/* 字段级帮助按钮(问号)+ 说明气泡 */
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

.policy-input {
  width: 100%;
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.policy-input:hover:not(:disabled):not(:focus) {
  border-color: var(--color-primary-border);
}

.policy-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

/* BaseSelect 撑满字段宽度(CLI 命令确认 / 授权模式) */
.policy-select {
  width: 100%;
}

.policy-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.policy-toggle-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-2);
  margin: 0 calc(-1 * var(--space-2));
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

/* ---- Switch 拨动开关(替代原生 checkbox 外观) ---- */
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

/* ---- 测试环境 / 动态验证配置面板 ---- */
.verifier-config {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
}

.verifier-config .policy-field {
  gap: var(--space-1);
}

/* ---- 登录凭证列表(verifier_auth_tokens)---- */
.auth-tokens-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.auth-tokens-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.policy-optional {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
}

.auth-token-add-btn {
  padding: 2px 10px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.auth-token-add-btn:hover {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.auth-token-row {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.auth-token-input {
  height: 32px;
  padding: 0 var(--space-2);
  font-size: var(--fs-xs);
  font-family: inherit;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.auth-token-input:hover:not(:focus) {
  border-color: var(--color-border-strong);
}

.auth-token-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.auth-token-label {
  flex: 0 0 110px;
}

.auth-token-header-name {
  flex: 0 0 120px;
}

.auth-token-header-value {
  flex: 1;
  min-width: 0;
}

.auth-token-remove-btn {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  line-height: 1;
  color: var(--color-text-muted);
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.auth-token-remove-btn:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

.skill-header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.skill-select-all {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  cursor: pointer;
  white-space: nowrap;
}

.skill-select-all input {
  cursor: pointer;
}

.skill-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
  flex: 1;
  min-width: 200px;
}

.skill-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-2);
}

.skill-item {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.skill-item:hover {
  border-color: var(--color-primary-border);
  background: var(--color-surface-alt);
}

.skill-item.checked {
  border-color: var(--color-primary-border);
  background: var(--color-primary-light);
}

.skill-item input {
  margin-top: 2px;
  cursor: pointer;
  flex-shrink: 0;
}

.skill-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.skill-name {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  word-break: break-all;
}

.skill-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-snug);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.skill-load-error {
  margin-top: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  text-align: center;
}

/* ---- 折叠过渡 ---- */
.collapse-enter-active,
.collapse-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.collapse-enter-from,
.collapse-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
