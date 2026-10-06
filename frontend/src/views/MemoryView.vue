<script setup lang="ts">
/**
 * 记忆管理页(文件列表 + Markdown 编辑器)
 *
 * 三类记忆以「文件」隐喻呈现,每份文件即一篇 Markdown:
 * - User Profile(1):user_profile Markdown,注入 agent2
 * - 全局记忆(1):content Markdown,注入 agent2
 * - 项目记忆(N,按 repo_url 聚合):memory_content Markdown,注入 agent1;任务完成自动归纳
 *
 * 左侧文件列表,右侧编辑器(编辑/预览切换)。
 * 预览用 marked 渲染 + DOMPurify 净化(项目记忆可能由 agent 归纳自不可信仓库内容,防存储型 XSS)。
 *
 * 入口:主导航「记忆管理」项(与模型设置/CLI 设置/智能体策略并列)。
 * Agent 策略配置已迁移至 /agent-policy(AgentPolicyView),本页仅管理记忆文本。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import AppHeader from '@/components/AppHeader.vue'
import CodeMirrorEditor from '@/components/CodeMirrorEditor.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  deleteProject,
  getGlobalMemory,
  getPreferences,
  listProjects,
  saveGlobalMemory,
  savePreferences,
  saveProject,
} from '@/api/memory'
import { useUnsavedGuard } from '@/composables/useUnsavedGuard'
import { extractErrorMessage } from '@/utils/error'
import type { ProjectOut, UserPreferenceOut } from '@/types/memory'

const router = useRouter()

// ============================================================
// 默认模板(后端初始为空,前端在内容为空时预填模板作为引导)
// 保存后才会真正写入 DB 并注入 agent;纯模板未改动不计为"未保存"
// ============================================================

/** User Profile 默认模板(写入 user_profile,注入 agent2) */
const PREF_TEMPLATE = `# User Profile

## Academic Background
- 

## Current Focus Areas
- Security: hardcoded secrets, injection, privilege escalation, SSRF
- Performance: query optimization, caching, resource leaks
- Code quality: readability, separation of responsibilities

## Output Language
English

## Evaluation Style
Thorough; prioritize security and best practices over brevity. For critical issues, provide reproduction steps and fix examples.

## Additional Requirements
- 
`

/** 全局长期记忆默认模板(写入 content,注入 agent2) */
const GLOBAL_TEMPLATE = `# Global Memory

## General Conventions
- 

## Common Pitfalls
- 

## Reusable Lessons
- 
`

/** 按 文件类型 取对应模板(项目记忆无模板) */
function templateFor(kind: FileKind): string {
  if (kind === 'pref') return PREF_TEMPLATE
  if (kind === 'global') return GLOBAL_TEMPLATE
  return ''
}

/** 历史任务侧栏是否折叠 */
const workspaceCollapsed = ref(true)
function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// Toast
// ============================================================
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)
function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4000)
}

// ============================================================
// 文件模型
// ============================================================
type FileKind = 'pref' | 'global' | 'project'

interface FileEntry {
  /** 文件 id:pref / global / project:<uuid> */
  id: string
  kind: FileKind
  /** 显示名(项目=alias 或 repo_url,其余=固定名) */
  label: string
  /** 副标题(项目=repo_url_raw) */
  subtitle?: string
  /** 元信息(项目=上次归纳时间) */
  meta?: string | null
}

/** 项目原始数据(列表) */
const projects = ref<ProjectOut[]>([])

/** 文件列表(计算) */
const fileList = computed<FileEntry[]>(() => {
  const list: FileEntry[] = [
    { id: 'pref', kind: 'pref', label: '用户偏好' },
    { id: 'global', kind: 'global', label: '全局记忆' },
  ]
  for (const p of projects.value) {
    list.push({
      id: `project:${p.id}`,
      kind: 'project',
      label: p.alias || p.repo_url_raw,
      subtitle: p.repo_url_raw,
      meta: p.last_summary_at,
    })
  }
  return list
})

/** 当前选中文件 id */
const activeId = ref<string>('pref')
const activeEntry = computed<FileEntry | undefined>(() =>
  fileList.value.find((f) => f.id === activeId.value),
)

// ============================================================
// 草稿与原始值(支持多文件切换不丢未保存编辑)
// ============================================================
/** 每份文件的 Markdown 草稿(键=文件 id) */
const drafts = reactive<Record<string, string>>({})
/** 每份文件的原始 Markdown(脏检查基准) */
const originals = reactive<Record<string, string>>({})
/** 项目 alias 草稿(键=project:<uuid>) */
const aliasDrafts = reactive<Record<string, string>>({})
/** 项目 alias 原始值 */
const originalAlias = reactive<Record<string, string>>({})
/** 该文件当前显示的是默认模板(DB 为空时预填,键=文件 id;仅 pref/global 会为 true) */
const isTemplateDefault = reactive<Record<string, boolean>>({})
/** 每份文件的最后更新时间(ISO 字符串,键=文件 id;null=从未保存) */
const updatedAt = reactive<Record<string, string | null>>({})

// ---- 透传存储(不在 UI 编辑,但保存时原样回传,避免数据丢失) ----
/** 项目 note(透传,键=project:<uuid>) */
const projectNotes = reactive<Record<string, string | null>>({})
/** 文件 id → 项目 uuid */
const projectIds = reactive<Record<string, string>>({})

// ============================================================
// 编辑/预览模式
// ============================================================
const mode = ref<'edit' | 'preview'>('edit')

// ============================================================
// 编辑器实例 + 撤回/恢复状态
// ============================================================
/** CodeMirror 编辑器实例引用(用于调用 undo/redo) */
const editorRef = ref<InstanceType<typeof CodeMirrorEditor> | null>(null)
/** 当前撤销栈是否有内容(由编辑器 historyChange 事件驱动) */
const editorCanUndo = ref(false)
/** 当前恢复栈是否有内容 */
const editorCanRedo = ref(false)

function onHistoryChange(payload: { canUndo: boolean; canRedo: boolean }): void {
  editorCanUndo.value = payload.canUndo
  editorCanRedo.value = payload.canRedo
}

/** 撤回按钮可用:编辑模式 + 非保存/删除中 + 有撤销栈 */
const canUndoNow = computed(
  () => mode.value === 'edit' && !saving.value && !deleting.value && editorCanUndo.value,
)
/** 恢复按钮可用:编辑模式 + 非保存/删除中 + 有恢复栈 */
const canRedoNow = computed(
  () => mode.value === 'edit' && !saving.value && !deleting.value && editorCanRedo.value,
)

function handleUndo(): void {
  if (!canUndoNow.value) return
  editorRef.value?.undo()
  editorRef.value?.focus()
}

function handleRedo(): void {
  if (!canRedoNow.value) return
  editorRef.value?.redo()
  editorRef.value?.focus()
}

/** 预览 HTML(marked 渲染 + DOMPurify 净化) */
const previewHtml = computed(() => {
  const md = drafts[activeId.value] ?? ''
  if (!md.trim()) return ''
  // marked.parse 在 async:false 下同步返回 string
  const raw = marked.parse(md, { async: false }) as string
  return DOMPurify.sanitize(raw)
})

// ============================================================
// 加载状态
// ============================================================
const loading = ref(true)
const loadError = ref('')

// 保存/删除状态(作用于当前文件)
const saving = ref(false)
const deleting = ref(false)
const actionError = ref('')

// ============================================================
// 字符上限(与后端 schema 对齐)
// ============================================================
const LIMITS: Record<FileKind, number> = {
  pref: 2000,
  global: 20000,
  project: 20000,
}
const activeLimit = computed(() => (activeEntry.value ? LIMITS[activeEntry.value.kind] : 20000))
const activeDraft = computed(() => drafts[activeId.value] ?? '')
const activeOverLimit = computed(() => activeDraft.value.length > activeLimit.value)

/** 草稿与原始值是否有任何差异(含 alias;不区分模板,用于判定"可保存") */
function hasDiff(fileId: string): boolean {
  if ((drafts[fileId] ?? '') !== (originals[fileId] ?? '')) return true
  if (fileId.startsWith('project:') && (aliasDrafts[fileId] ?? '') !== (originalAlias[fileId] ?? '')) return true
  return false
}

/** 当前文件是否脏(有未保存改动;纯模板未改动不计脏,避免空文件初始就显示未保存,但仍可"采用"保存) */
function isDirty(fileId: string): boolean {
  if (!hasDiff(fileId)) return false
  const draft = drafts[fileId] ?? ''
  const orig = originals[fileId] ?? ''
  // 纯默认模板预填未改动:草稿===模板且 DB 原本为空 → 不算脏
  const onlyTemplateUnchanged =
    isTemplateDefault[fileId] && orig === '' && draft === templateFor(fileKind(fileId))
  if (onlyTemplateUnchanged) {
    return false
  }
  return true
}
/** 是否可保存:草稿与原始值有差异即可(含"采用默认模板":纯模板时 hasDiff 仍为 true) */
const activeCanSave = computed(() => hasDiff(activeId.value))

/** 当前文件是否处于"纯默认模板预填"状态(用于显示提示条) */
const activeIsTemplate = computed(() => {
  const entry = activeEntry.value
  if (!entry || entry.kind === 'project') return false
  return (
    isTemplateDefault[activeId.value] === true &&
    (drafts[activeId.value] ?? '') === templateFor(entry.kind)
  )
})

/** 按文件 id 取类型(fileList 反查) */
function fileKind(fileId: string): FileKind {
  if (fileId === 'pref') return 'pref'
  if (fileId === 'global') return 'global'
  return 'project'
}

/** 任一文件有未保存改动(用于离开页面前弹窗提醒,见底部 useUnsavedGuard) */
const hasAnyDirty = computed(() => fileList.value.some((f) => isDirty(f.id)))

// ============================================================
// 加载
// ============================================================
async function loadAll(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [prefData, globalData, projectsData] = await Promise.all([
      getPreferences(),
      getGlobalMemory(),
      listProjects(),
    ])
    hydratePref(prefData)
    hydrateGlobal(globalData)
    hydrateProjects(projectsData.projects || [])
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

function hydratePref(data: UserPreferenceOut): void {
  const md = data.user_profile || ''
  if (md.trim() === '') {
    // DB 为空:预填默认模板作为引导(不算"已保存"内容,originals 仍为空)
    drafts['pref'] = PREF_TEMPLATE
    originals['pref'] = ''
    isTemplateDefault['pref'] = true
  } else {
    drafts['pref'] = md
    originals['pref'] = md
    isTemplateDefault['pref'] = false
  }
  updatedAt['pref'] = data.updated_at ?? null
}

function hydrateGlobal(data: { content: string; updated_at?: string | null }): void {
  const content = data.content || ''
  if (content.trim() === '') {
    drafts['global'] = GLOBAL_TEMPLATE
    originals['global'] = ''
    isTemplateDefault['global'] = true
  } else {
    drafts['global'] = content
    originals['global'] = content
    isTemplateDefault['global'] = false
  }
  updatedAt['global'] = data.updated_at ?? null
}

function hydrateProjects(list: ProjectOut[]): void {
  projects.value = list
  for (const p of list) {
    const fid = `project:${p.id}`
    drafts[fid] = p.memory_content || ''
    originals[fid] = p.memory_content || ''
    aliasDrafts[fid] = p.alias || ''
    originalAlias[fid] = p.alias || ''
    projectNotes[fid] = p.note ?? null
    projectIds[fid] = p.id
    updatedAt[fid] = p.updated_at ?? null
  }
}

// ============================================================
// 选择文件 / 切换模式
// ============================================================
function selectFile(id: string): void {
  activeId.value = id
  mode.value = 'edit'
  actionError.value = ''
}

// ============================================================
// 保存当前文件
// ============================================================

/** 保存单份文件(不含 loading/toast,供 handleSave 与 saveAllDirty 复用) */
async function saveFile(entry: FileEntry): Promise<void> {
  if (entry.kind === 'pref') {
    // pref 文件:仅保存 user_profile 文本
    // (agent 策略已迁移至 /agent-policy 独立页面保存)
    const latest = await savePreferences({ user_profile: drafts['pref'] })
    hydratePref(latest)
  } else if (entry.kind === 'global') {
    const data = await saveGlobalMemory({ content: drafts['global'] })
    hydrateGlobal(data)
  } else {
    const fid = entry.id
    const pid = projectIds[fid]
    const alias = (aliasDrafts[fid] || '').trim()
    const data = await saveProject(pid, {
      alias: alias || null,
      note: projectNotes[fid] ?? null,
      memory_content: drafts[fid],
    })
    // 就地更新项目列表 + 重水化草稿
    const idx = projects.value.findIndex((p) => p.id === pid)
    if (idx >= 0) projects.value[idx] = data
    drafts[fid] = data.memory_content || ''
    originals[fid] = data.memory_content || ''
    aliasDrafts[fid] = data.alias || ''
    originalAlias[fid] = data.alias || ''
    projectNotes[fid] = data.note ?? null
    updatedAt[fid] = data.updated_at ?? null
  }
}

async function handleSave(): Promise<void> {
  const entry = activeEntry.value
  if (!entry || !activeCanSave.value || activeOverLimit.value || saving.value) return
  saving.value = true
  actionError.value = ''
  try {
    await saveFile(entry)
    showToast('已保存', 'success')
  } catch (err) {
    actionError.value = extractErrorMessage(err)
  } finally {
    saving.value = false
  }
}

/** 保存所有未保存文件(离开页面前"保存并离开"用;任一失败返回 false) */
async function saveAllDirty(): Promise<boolean> {
  const dirtyEntries = fileList.value.filter((f) => isDirty(f.id))
  if (dirtyEntries.length === 0) return true
  saving.value = true
  actionError.value = ''
  try {
    for (const entry of dirtyEntries) {
      await saveFile(entry)
    }
    showToast('未保存改动已全部保存', 'success')
    return true
  } catch (err) {
    actionError.value = extractErrorMessage(err)
    return false
  } finally {
    saving.value = false
  }
}

// 未保存改动时切换路由弹窗提醒
useUnsavedGuard(hasAnyDirty, saveAllDirty)

// ============================================================
// 删除当前项目文件
// ============================================================
async function handleDelete(): Promise<void> {
  const entry = activeEntry.value
  if (!entry || entry.kind !== 'project' || deleting.value) return
  const fid = entry.id
  const pid = projectIds[fid]
  if (!pid) return
  if (!window.confirm('确定删除此项目记忆?该仓库的记忆将被清空,无法恢复。')) return
  deleting.value = true
  actionError.value = ''
  try {
    const data = await deleteProject(pid)
    projects.value = data.projects || []
    // 清理草稿
    delete drafts[fid]
    delete originals[fid]
    delete aliasDrafts[fid]
    delete originalAlias[fid]
    delete projectNotes[fid]
    delete projectIds[fid]
    delete updatedAt[fid]
    activeId.value = 'pref'
    mode.value = 'edit'
    showToast('项目记忆已删除', 'success')
  } catch (err) {
    actionError.value = extractErrorMessage(err)
  } finally {
    deleting.value = false
  }
}

// ============================================================
// 工具
// ============================================================
function formatTime(iso: string | null | undefined): string {
  if (!iso) return '从未归纳'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

/** 相对时间(用于侧栏"最后更新"):刚刚 / X 分钟前 / X 小时前 / X 天前 / YYYY-MM-DD */
function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '从未保存'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    const diffMs = Date.now() - d.getTime()
    const sec = Math.floor(diffMs / 1000)
    if (sec < 60) return '刚刚'
    const min = Math.floor(sec / 60)
    if (min < 60) return `${min} 分钟前`
    const hr = Math.floor(min / 60)
    if (hr < 24) return `${hr} 小时前`
    const day = Math.floor(hr / 24)
    if (day < 7) return `${day} 天前`
    return d.toLocaleDateString('zh-CN')
  } catch {
    return iso
  }
}

// ============================================================
// 编辑器:CodeMirror 6 封装组件(行号、Markdown 高亮、Tab 缩进、软换行对齐)
// ============================================================
// 由 CodeMirrorEditor.vue 内部维护编辑器实例,本页只负责 v-model 双向绑定。
// 切换文件时 v-model 变化 → 组件内同步到 doc;编辑时 doc 变化 → 回写 drafts。

onMounted(() => {
  loadAll()
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

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="memory-main">
        <!-- ============ 左侧文件列表 ============ -->
        <aside class="file-sidebar">
          <!-- 加载中 -->
          <div v-if="loading" class="sidebar-placeholder">
            <span class="status-spinner" /> 加载中...
          </div>
          <!-- 加载失败 -->
          <div v-else-if="loadError" class="sidebar-placeholder error-text">
            加载失败: {{ loadError }}
            <button class="btn-link" @click="loadAll">重试</button>
          </div>
          <!-- 文件列表 -->
          <ul v-else class="file-list">
            <li>
              <button
                class="file-item"
                :class="{ active: activeId === 'pref' }"
                @click="selectFile('pref')"
              >
                <svg class="file-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="file-text">
                  <span class="file-label">用户偏好</span>
                  <span class="file-meta">{{ formatRelative(updatedAt['pref']) }}</span>
                </span>
                <span v-if="isDirty('pref')" class="dirty-dot" title="有未保存改动" />
              </button>
            </li>
            <li>
              <button
                class="file-item"
                :class="{ active: activeId === 'global' }"
                @click="selectFile('global')"
              >
                <svg class="file-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="file-text">
                  <span class="file-label">全局记忆</span>
                  <span class="file-meta">{{ formatRelative(updatedAt['global']) }}</span>
                </span>
                <span v-if="isDirty('global')" class="dirty-dot" title="有未保存改动" />
              </button>
            </li>

            <li class="group-label">项目记忆</li>
            <li v-if="projects.length === 0" class="group-empty">
              暂无项目记忆,任务完成后自动归纳生成
            </li>
            <li v-for="p in projects" :key="p.id">
              <button
                class="file-item project"
                :class="{ active: activeId === `project:${p.id}` }"
                @click="selectFile(`project:${p.id}`)"
              >
                <svg class="file-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 3h18v18H3z" />
                  <path d="M3 9h18M9 21V9" />
                </svg>
                <span class="file-text">
                  <span class="file-label" :title="p.alias || p.repo_url_raw">
                    {{ p.alias || p.repo_url_raw }}
                  </span>
                  <span class="file-meta">{{ formatRelative(updatedAt[`project:${p.id}`]) }}</span>
                </span>
                <span v-if="isDirty(`project:${p.id}`)" class="dirty-dot" title="有未保存改动" />
              </button>
            </li>
          </ul>

          <!-- 底栏:未保存提示 + 记忆设置 + 刷新 -->
          <div class="sidebar-footer-hint">
            <template v-if="hasAnyDirty">
              <span class="dirty-dot" /> <span>有未保存改动</span>
            </template>
            <button
              v-if="!loading && !loadError"
              class="btn-icon footer-settings"
              title="记忆设置(归纳模型 / 结构化类别 / 自动开关)"
              @click="router.push('/settings/memory')"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
            </button>
            <button
              v-if="!loading && !loadError"
              class="btn-icon footer-refresh"
              title="刷新"
              @click="loadAll"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="23 4 23 10 17 10" />
                <polyline points="1 20 1 14 7 14" />
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
              </svg>
            </button>
          </div>
        </aside>

        <!-- ============ 右侧编辑器 ============ -->
        <section class="editor-panel">
          <template v-if="activeEntry">
            <!-- 工具栏 -->
            <header class="editor-toolbar">
              <div class="toolbar-left">
                <!-- 项目:alias 可编辑标题 -->
                <input
                  v-if="activeEntry.kind === 'project'"
                  v-model="aliasDrafts[activeEntry.id]"
                  class="title-input"
                  type="text"
                  placeholder="项目别名(可选)"
                  :disabled="saving || deleting"
                  :maxlength="255"
                />
                <h2 v-else class="title-static">{{ activeEntry.label }}</h2>

                <div v-if="activeEntry.kind === 'project'" class="subtitle-row">
                  <span class="subtitle mono" :title="activeEntry.subtitle">{{ activeEntry.subtitle }}</span>
                  <span class="meta-sep">·</span>
                  <span class="subtitle">上次归纳: {{ formatTime(activeEntry.meta) }}</span>
                </div>
                <p v-if="actionError" class="action-error">{{ actionError }}</p>
              </div>

              <div class="toolbar-right">
                <span class="char-count" :class="{ over: activeOverLimit }">
                  {{ activeDraft.length }} / {{ activeLimit }}
                </span>
                <div class="mode-switch" role="group" aria-label="编辑/预览">
                  <button
                    :class="['mode-btn', { active: mode === 'edit' }]"
                    :disabled="saving || deleting"
                    @click="mode = 'edit'"
                  >编辑</button>
                  <button
                    :class="['mode-btn', { active: mode === 'preview' }]"
                    :disabled="saving || deleting"
                    @click="mode = 'preview'"
                  >预览</button>
                </div>
                <button
                  v-if="activeEntry.kind === 'project'"
                  class="btn btn-danger"
                  :disabled="saving || deleting"
                  @click="handleDelete"
                >
                  <span v-if="deleting" class="btn-spinner danger" />
                  {{ deleting ? '删除中...' : '删除' }}
                </button>
                <button
                  class="btn-icon toolbar-icon-btn"
                  type="button"
                  title="撤回 (Ctrl+Z)"
                  :disabled="!canUndoNow"
                  @click="handleUndo"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="1 4 1 10 7 10" />
                    <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
                  </svg>
                </button>
                <button
                  class="btn-icon toolbar-icon-btn"
                  type="button"
                  title="恢复 (Ctrl+Shift+Z)"
                  :disabled="!canRedoNow"
                  @click="handleRedo"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="23 4 23 10 17 10" />
                    <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
                  </svg>
                </button>
                <button
                  class="btn btn-primary"
                  :disabled="!activeCanSave || saving || deleting || activeOverLimit"
                  @click="handleSave"
                >
                  <span v-if="saving" class="btn-spinner" />
                  {{ saving ? '保存中...' : (activeCanSave ? '保存' : '已保存') }}
                </button>
              </div>
            </header>

            <!-- 模板提示条(当前显示默认模板、未改动时) -->
            <div v-if="activeIsTemplate" class="template-hint">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="16" x2="12" y2="12" />
                <line x1="12" y1="8" x2="12.01" y2="8" />
              </svg>
              <span>当前显示默认模板 — 保存后才会写入并注入 agent。可按需编辑,或直接点「保存」采用此模板。</span>
            </div>

            <!-- 内容区 -->
            <div class="editor-content">
              <!-- 加载占位 -->
              <div v-if="loading" class="content-placeholder">
                <span class="status-spinner" /> 加载中...
              </div>
              <!-- 编辑模式:CodeMirror 编辑器(行号、Markdown 高亮、软换行对齐)
                   :key 绑定文件 id → 切换文件时强制重建编辑器实例,清空撤销栈,
                   避免撤回跨文件污染(undo 把上一文件内容写回当前 draft) -->
              <CodeMirrorEditor
                v-else-if="mode === 'edit'"
                :key="activeEntry.id"
                ref="editorRef"
                v-model="drafts[activeEntry.id]"
                class="code-area"
                :class="{ invalid: activeOverLimit }"
                placeholder="用 Markdown 编写。支持标题、列表、代码块等。"
                :disabled="saving || deleting"
                @history-change="onHistoryChange"
              />
              <!-- 预览模式 -->
              <div v-else class="md-preview">
                <div v-if="previewHtml" class="markdown-body" v-html="previewHtml" />
                <div v-else class="preview-empty">暂无内容</div>
              </div>
            </div>

          </template>

          <!-- 无选中文件兜底 -->
          <div v-else class="editor-empty">
            <p>请从左侧选择一份记忆文件</p>
          </div>
        </section>
      </main>
    </div>

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
}

.memory-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}

/* ============ 左侧文件列表 ============ */
.file-sidebar {
  width: 248px;
  flex-shrink: 0;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.btn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-icon:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.sidebar-placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.error-text {
  color: var(--color-danger);
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}

.file-list {
  list-style: none;
  margin: 0;
  padding: var(--space-2);
  flex: 1;
  overflow-y: auto;
}

.file-list > li {
  margin: 0;
}

.group-label {
  padding: var(--space-3) var(--space-3) var(--space-1);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.group-empty {
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

.file-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  text-align: left;
  transition: all var(--transition-fast);
  position: relative;
}

.file-item:hover {
  background: var(--color-surface-alt);
}

.file-item.active {
  background: var(--color-primary-light);
  color: var(--color-primary-hover);
  font-weight: var(--fw-semibold);
}

.file-item.project {
  padding-left: var(--space-5);
}

.file-icon {
  color: var(--color-text-muted);
  flex-shrink: 0;
  align-self: flex-start;
  margin-top: 2px;
}

.file-item.active .file-icon {
  color: var(--color-primary);
}

/* 文本列:标签 + 最后更新时间(两行) */
.file-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
  line-height: var(--lh-tight);
}

.file-label {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-meta {
  font-size: var(--fs-xs);
  font-weight: 400;
  color: var(--color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-item.active .file-meta {
  color: var(--color-primary);
  opacity: 0.75;
}

.dirty-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-warning, #f59e0b);
  flex-shrink: 0;
  align-self: flex-start;
  margin-top: 6px;
}

.sidebar-footer-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-top: 1px solid var(--color-border);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.sidebar-footer-hint .footer-settings {
  margin-left: auto;
  width: 22px;
  height: 22px;
}

.sidebar-footer-hint .footer-refresh {
  width: 22px;
  height: 22px;
}

/* ============ 右侧编辑器 ============ */
.editor-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.editor-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 640px) {
  .editor-toolbar {
    flex-wrap: wrap;
    padding: var(--space-2) var(--space-3);
    gap: var(--space-2);
  }

  .toolbar-right {
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  /* 副标题等宽截断宽度改为相对上限,避免窄屏溢出 */
  .subtitle.mono {
    max-width: 100%;
  }

  .title-input {
    max-width: 100%;
  }
}

/* 模板提示条:当前显示默认模板(未改动)时出现 */
.template-hint {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-5);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-bottom: 1px solid var(--color-border);
  line-height: var(--lh-relaxed);
}

.template-hint svg {
  color: var(--color-primary);
  flex-shrink: 0;
  margin-top: 2px;
}

.toolbar-left {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.title-static {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.title-input {
  width: 100%;
  max-width: 480px;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.title-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.title-input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.subtitle-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.subtitle {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.subtitle.mono {
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 360px;
}

.meta-sep {
  color: var(--color-text-muted);
}

.toolbar-right {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: nowrap;
}

/* 模式切换 */
.mode-switch {
  display: inline-flex;
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.mode-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.mode-btn:hover:not(:disabled):not(.active) {
  color: var(--color-text);
}

.mode-btn.active {
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: var(--shadow-sm);
}

.mode-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 工具栏撤回/恢复图标按钮(与 .btn 高度对齐,32px 方便点击) */
.toolbar-icon-btn {
  width: 32px;
  height: 32px;
}

/* 禁用态:预览模式 / 保存中 / 撤销栈空时灰显,否则视觉上和可用态无差异 */
.toolbar-icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.toolbar-icon-btn:disabled:hover {
  background: transparent;
  color: var(--color-text-muted);
}

/* 内容区(全屏铺满,代码编辑器风格) */
.editor-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.content-placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4) var(--space-5);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

/* 代码编辑区:CodeMirror 宿主容器(内部布局由组件接管) */
.code-area {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--color-surface);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

.code-area.invalid {
  /* 超限时给整个编辑区一个左边框警示色 */
  box-shadow: inset 2px 0 0 var(--color-danger);
}

/* 预览(全屏铺满,自带内边距) */
.md-preview {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
  background: var(--color-surface);
}

.preview-empty {
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
  text-align: center;
  padding: var(--space-8) 0;
}

/* Markdown 渲染样式(作用于 v-html 容器) */
.markdown-body {
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: var(--space-4) 0 var(--space-2);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
}

.markdown-body :deep(h1) { font-size: var(--fs-xl); }
.markdown-body :deep(h2) { font-size: var(--fs-lg); }
.markdown-body :deep(h3) { font-size: var(--fs-base); }

.markdown-body :deep(p) {
  margin: 0 0 var(--space-3);
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-5);
}

.markdown-body :deep(li) {
  margin: var(--space-1) 0;
}

.markdown-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.875em;
  padding: 2px 6px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.markdown-body :deep(pre) {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  overflow-x: auto;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
}

.markdown-body :deep(blockquote) {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-4);
  border-left: 3px solid var(--color-primary);
  background: var(--color-surface-alt);
  color: var(--color-text-secondary);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.markdown-body :deep(a) {
  color: var(--color-primary);
  text-decoration: none;
}

.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

.markdown-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 var(--space-3);
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  text-align: left;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: var(--space-4) 0;
}

.char-count {
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.char-count.over {
  color: var(--color-danger);
  font-weight: var(--fw-medium);
}

.action-error {
  font-size: var(--fs-xs);
  color: var(--color-danger);
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.editor-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

/* ============ 按钮 ============ */
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
  background: var(--color-surface);
  color: var(--color-danger);
  border-color: var(--color-border);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger-light);
  border-color: var(--color-danger);
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
  border-color: rgba(239, 68, 68, 0.3);
  border-top-color: var(--color-danger);
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

.btn-link {
  background: none;
  border: none;
  padding: 4px 8px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.btn-link:hover {
  text-decoration: underline;
}

/* spinner */
.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ============ Toast ============ */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 240px;
  max-width: 380px;
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
