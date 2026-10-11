<script setup lang="ts">
/**
 * 技能管理页(上传 zip / 列表 / 右侧栏文件浏览与编辑 / 删除)
 *
 * 入口:主导航「技能管理」项(与记忆管理/智能体策略并列)。
 *
 * 布局:
 * - 中栏宽度与 CLI 设置页对齐(max-width 680px 居中):上传区 + 技能列表
 * - 上传区为常规文件上传样式:左侧选择框点击弹出文件对话框并回显文件名,右侧上传按钮
 * - 点击列表项打开右侧栏:左边技能文件列表,右边文件编辑器(与记忆管理同款)
 *   · .md 文件支持「编辑 / 预览」切换,预览用 marked 渲染 + DOMPurify 净化
 *   · 自己的 skill 的 SKILL.md 可编辑保存(后端 upsert 热刷新);其余只读
 *
 * 安全:zip 由后端校验(zip-slip / 大小 / frontmatter / 扩展名白名单);
 * 文件读取接口后端做了路径穿越防护与 UTF-8 校验。
 */
import { computed, onMounted, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import AppHeader from '@/components/AppHeader.vue'
import CodeMirrorEditor from '@/components/CodeMirrorEditor.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  deleteSkill,
  getSkillFileContent,
  getSkillFiles,
  getSkills,
  updateSkill,
  uploadSkill,
  type SkillFileEntry,
  type SkillSummary,
} from '@/api/skill'
import { useUnsavedGuard } from '@/composables/useUnsavedGuard'
import { extractErrorMessage } from '@/utils/error'

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
// 技能列表
// ============================================================
const skills = ref<SkillSummary[]>([])
const loading = ref(true)
const loadError = ref('')

async function loadSkills(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    skills.value = await getSkills()
  } catch (e) {
    loadError.value = extractErrorMessage(e)
  } finally {
    loading.value = false
  }
}

/** 是否为内置 skill(场景目录非 user_ 前缀) */
function isBuiltin(s: SkillSummary): boolean {
  return !s.scenario_id.startsWith('user_')
}

/** 技能名搜索关键词(纯前端过滤,大小写不敏感) */
const searchQuery = ref('')

const filteredSkills = computed<SkillSummary[]>(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return skills.value
  return skills.value.filter((s) => s.name.toLowerCase().includes(q))
})

/** 搜索时展示「匹配数 / 总数」 */
const listCountText = computed<string>(() => {
  if (!searchQuery.value.trim()) return `${skills.value.length} 个`
  return `${filteredSkills.value.length} / ${skills.value.length} 个`
})

// ============================================================
// 上传
// ============================================================
const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null)
const uploading = ref(false)
/** 同名覆盖确认弹窗是否打开 */
const overwriteConfirmOpen = ref(false)
/** 待确认覆盖的技能名(弹窗展示用) */
const pendingOverwriteName = ref('')

function triggerPick(): void {
  fileInputRef.value?.click()
}

function onPickFile(e: Event): void {
  const input = e.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
  // 清空 input,允许重复选择同一文件
  input.value = ''
}

async function handleUpload(): Promise<void> {
  if (!selectedFile.value) {
    showToast('请先选择 .zip 文件', 'error')
    return
  }
  await doUpload(false)
}

/**
 * 执行上传;409 同名冲突时区分两种情况:
 * - 自己的同名 skill(可覆盖)→ 弹窗让用户选择是否覆盖
 * - 全局冲突(内置/他人 skill 同名,不可覆盖)→ 直接报错
 */
async function doUpload(force: boolean): Promise<void> {
  if (!selectedFile.value) return
  uploading.value = true
  try {
    const result = await uploadSkill(selectedFile.value, force)
    showToast(
      result.replaced
        ? `已覆盖技能「${result.skill.name}」`
        : `已上传技能「${result.skill.name}」,AI助手立即可用`,
      'success',
    )
    selectedFile.value = null
    await loadSkills()
    // 上传成功后直接打开新技能的文件浏览
    openSkill({
      name: result.skill.name,
      description: result.skill.description,
      scenario_id: result.skill.scenario_id,
      owned: result.skill.owned,
    })
  } catch (e) {
    const msg = extractErrorMessage(e)
    // 全局冲突(内置/他人 skill 同名):不可覆盖,直接报错
    if (msg.includes('无法覆盖')) {
      showToast(msg, 'error')
      return
    }
    // 自己的同名 skill 冲突:弹窗让用户选择是否覆盖
    if (msg.includes('已存在')) {
      pendingOverwriteName.value = extractSkillNameFromMsg(msg)
      overwriteConfirmOpen.value = true
      return
    }
    showToast(msg, 'error')
  } finally {
    uploading.value = false
  }
}

/** 从后端 409 文案中提取技能名(形如 skill「xxx」已存在) */
function extractSkillNameFromMsg(msg: string): string {
  const m = msg.match(/「(.+?)」/)
  return m ? m[1] : ''
}

/** 用户确认覆盖:带 force=true 重新上传 */
function confirmOverwrite(): void {
  overwriteConfirmOpen.value = false
  void doUpload(true)
}

/** 用户取消覆盖 */
function cancelOverwrite(): void {
  overwriteConfirmOpen.value = false
}

// ============================================================
// 右侧栏:文件列表 + 文件编辑器
// ============================================================
const activeSkill = ref<SkillSummary | null>(null)
const detailFiles = ref<SkillFileEntry[]>([])
const filesLoading = ref(false)
const filesError = ref('')

const activePath = ref('')
const draft = ref('')
const original = ref('')
const fileLoading = ref(false)
const fileError = ref('')
const saving = ref(false)
const mode = ref<'edit' | 'preview'>('edit')

/** 防止快速切换 skill/文件时旧请求回写新状态
 * 列表与内容各自独立计数:loadFiles 内部会 await selectFile,
 * 若共用同一计数器,selectFile 的自增会让 loadFiles 的 finally
 * 判定过期,filesLoading 永远无法复位(列表一直转圈)。 */
let filesReqSeq = 0
let contentReqSeq = 0

function isMd(path: string): boolean {
  return path.toLowerCase().endsWith('.md')
}

const activeFile = computed<SkillFileEntry | undefined>(() =>
  detailFiles.value.find((f) => f.path === activePath.value),
)

/** 仅自己上传的 skill 的 SKILL.md 可编辑保存 */
const canEdit = computed(
  () => !!activeSkill.value?.owned && activePath.value === 'SKILL.md',
)

const isDirty = computed(() => draft.value !== original.value)
const canSave = computed(() => canEdit.value && isDirty.value && !saving.value)

// 未保存改动时切换路由弹窗提醒(保存并离开复用 handleSave)
useUnsavedGuard(isDirty, () => handleSave())

/** 文件树中处于展开态的文件夹路径(每次 loadFiles 默认全部展开) */
const expandedFolders = ref<Set<string>>(new Set())

/** 文件树行:扁平文件列表按 '/' 构建树后压平,同层文件夹在前、文件保持后端顺序(SKILL.md 置顶) */
interface FileTreeRow {
  /** 展示名(文件夹名 / 文件名) */
  name: string
  /** 完整相对路径(文件夹为各级拼接) */
  path: string
  /** 层级深度(根为 0) */
  depth: number
  kind: 'folder' | 'file'
  file?: SkillFileEntry
}

const fileTreeRows = computed<FileTreeRow[]>(() => {
  interface Dir {
    dirs: Map<string, Dir>
    files: SkillFileEntry[]
  }
  const root: Dir = { dirs: new Map(), files: [] }
  for (const f of detailFiles.value) {
    const parts = f.path.split('/')
    let dir = root
    for (let i = 0; i < parts.length - 1; i++) {
      let next = dir.dirs.get(parts[i])
      if (!next) {
        next = { dirs: new Map(), files: [] }
        dir.dirs.set(parts[i], next)
      }
      dir = next
    }
    dir.files.push(f)
  }

  const rows: FileTreeRow[] = []
  const walk = (dir: Dir, prefix: string, depth: number): void => {
    const names = [...dir.dirs.keys()].sort((a, b) => a.localeCompare(b))
    for (const name of names) {
      const path = prefix ? `${prefix}/${name}` : name
      rows.push({ name, path, depth, kind: 'folder' })
      if (expandedFolders.value.has(path)) {
        walk(dir.dirs.get(name)!, path, depth + 1)
      }
    }
    // 文件保持后端顺序(SKILL.md 置顶),不按字母重排
    for (const f of dir.files) {
      rows.push({ name: fileName(f.path), path: f.path, depth, kind: 'file', file: f })
    }
  }
  walk(root, '', 0)
  return rows
})

/** 从扁平文件路径收集所有文件夹路径(用于默认全部展开) */
function collectFolderPaths(files: SkillFileEntry[]): Set<string> {
  const dirs = new Set<string>()
  for (const f of files) {
    const parts = f.path.split('/')
    for (let i = 1; i < parts.length; i++) {
      dirs.add(parts.slice(0, i).join('/'))
    }
  }
  return dirs
}

function toggleFolder(path: string): void {
  const next = new Set(expandedFolders.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  expandedFolders.value = next
}

/** 预览 HTML(marked 渲染 + DOMPurify 净化,防存储型 XSS) */
const previewHtml = computed(() => {
  if (!isMd(activePath.value) || !draft.value.trim()) return ''
  const raw = marked.parse(draft.value, { async: false }) as string
  return DOMPurify.sanitize(raw)
})

async function loadFiles(s: SkillSummary): Promise<void> {
  const seq = ++filesReqSeq
  filesLoading.value = true
  filesError.value = ''
  try {
    const files = await getSkillFiles(s.scenario_id, s.name)
    if (seq !== filesReqSeq) return
    detailFiles.value = files
    expandedFolders.value = collectFolderPaths(files)
    // 列表已就绪,先复位 loading 再联动选中文件(selectFile 会自增 contentReqSeq)
    filesLoading.value = false
    // 默认选中 SKILL.md(后端已置顶),否则选第一个文件
    const first = files.find((f) => f.path === 'SKILL.md') ?? files[0]
    if (first) {
      await selectFile(first.path)
    } else {
      activePath.value = ''
      draft.value = ''
      original.value = ''
    }
  } catch (e) {
    if (seq !== filesReqSeq) return
    filesError.value = extractErrorMessage(e)
  } finally {
    if (seq === filesReqSeq) filesLoading.value = false
  }
}

async function selectFile(path: string): Promise<void> {
  const s = activeSkill.value
  if (!s) return
  const seq = ++contentReqSeq
  activePath.value = path
  fileLoading.value = true
  fileError.value = ''
  draft.value = ''
  original.value = ''
  // md 文件:只读时默认预览,可编辑时默认编辑;其余文件默认源码视图
  const editable = s.owned && path === 'SKILL.md'
  mode.value = isMd(path) && !editable ? 'preview' : 'edit'
  try {
    const data = await getSkillFileContent(s.scenario_id, s.name, path)
    if (seq !== contentReqSeq) return
    draft.value = data.content
    original.value = data.content
  } catch (e) {
    if (seq !== contentReqSeq) return
    fileError.value = extractErrorMessage(e)
  } finally {
    if (seq === contentReqSeq) fileLoading.value = false
  }
}

function openSkill(s: SkillSummary): void {
  activeSkill.value = s
  detailFiles.value = []
  activePath.value = ''
  draft.value = ''
  original.value = ''
  filesError.value = ''
  fileError.value = ''
  loadFiles(s)
}

function closeSkill(): void {
  filesReqSeq++
  contentReqSeq++
  activeSkill.value = null
  detailFiles.value = []
  activePath.value = ''
  draft.value = ''
  original.value = ''
}

/** 保存 SKILL.md(直写全文,含 frontmatter;后端热刷新注册表);返回是否保存成功 */
async function handleSave(): Promise<boolean> {
  const s = activeSkill.value
  if (!s || !canSave.value) return false
  saving.value = true
  try {
    const result = await updateSkill(s.scenario_id, s.name, draft.value)
    original.value = draft.value
    showToast(`技能「${result.name}」已保存`, 'success')
    // frontmatter 改名/改描述时同步选中项与列表
    activeSkill.value = {
      name: result.name,
      description: result.description,
      scenario_id: result.scenario_id,
      owned: result.owned,
    }
    await loadSkills()
    return true
  } catch (e) {
    showToast(extractErrorMessage(e), 'error')
    await loadSkills()
    return false
  } finally {
    saving.value = false
  }
}

// ============================================================
// 删除
// ============================================================
async function handleDelete(s: SkillSummary): Promise<void> {
  if (!window.confirm(`确定删除技能「${s.name}」吗?删除后不可恢复。`)) return
  try {
    await deleteSkill(s.scenario_id, s.name)
    showToast(`已删除技能「${s.name}」`, 'success')
    if (
      activeSkill.value &&
      activeSkill.value.scenario_id === s.scenario_id &&
      activeSkill.value.name === s.name
    ) {
      closeSkill()
    }
    await loadSkills()
  } catch (e) {
    showToast(extractErrorMessage(e), 'error')
  }
}

// ============================================================
// 工具
// ============================================================
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 文件名(相对路径的最后一段,显示用) */
function fileName(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

onMounted(loadSkills)
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

      <!-- ============ 中栏(宽度与 CLI 设置页对齐) ============ -->
      <div class="skill-center">
        <main class="main-col">
          <!-- ============ 上传区 ============ -->
          <section class="upload-card">
            <div class="upload-header">
              <h2 class="section-title">上传技能</h2>
              <p class="section-desc">
                上传 zip 格式的 skill(内含 SKILL.md,支持
                <code>&lt;skill_name&gt;/SKILL.md</code> 或根目录直接放
                <code>SKILL.md</code>),上传后 AI助手的 list_skills / skill
                工具立即可用。技能仅自己可见,他人无法查看或使用。zip 最大 50MB。
                <br />
                SKILL.md 的 <code>name</code> 会作为存储目录名,需以字母或数字开头,
                仅含字母、数字与 <code>.</code> <code>_</code> <code>-</code>,
                不含 <code>..</code>,长度不超过 64(如
                <code>my_api_checker</code>)。
              </p>
            </div>
            <div class="upload-row">
              <!-- 左侧选择框:点击弹出文件对话框,选中后回显文件名 -->
              <input
                ref="fileInputRef"
                type="file"
                accept=".zip,application/zip"
                class="file-input-hidden"
                @change="onPickFile"
              />
              <button
                type="button"
                class="file-picker"
                :class="{ 'has-file': !!selectedFile }"
                @click="triggerPick"
              >
                <svg class="picker-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="picker-text" :title="selectedFile?.name ?? ''">
                  {{ selectedFile ? selectedFile.name : '点击选择 .zip 文件' }}
                </span>
              </button>
              <!-- 右侧上传按钮 -->
              <button
                class="btn-upload"
                :disabled="uploading || !selectedFile"
                @click="handleUpload"
              >
                <span v-if="uploading" class="btn-spinner" />
                {{ uploading ? '上传中...' : '上传' }}
              </button>
            </div>
          </section>

          <!-- ============ 技能列表 ============ -->
          <section class="list-card">
            <div class="list-header">
              <h2 class="section-title">技能列表</h2>
              <span class="list-count">{{ listCountText }}</span>
            </div>

            <!-- 技能名搜索框 -->
            <div v-if="skills.length > 0" class="list-search">
              <svg class="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="11" cy="11" r="8" />
                <line x1="21" x2="16.65" y1="21" y2="16.65" />
              </svg>
              <input
                v-model="searchQuery"
                type="text"
                class="search-input"
                placeholder="搜索技能名..."
                aria-label="搜索技能名"
              />
              <button
                v-if="searchQuery"
                class="search-clear"
                aria-label="清空搜索"
                title="清空搜索"
                @click="searchQuery = ''"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" x2="6" y1="6" y2="18" /><line x1="6" x2="18" y1="6" y2="18" />
                </svg>
              </button>
            </div>

            <div v-if="loading" class="list-placeholder">
              <span class="status-spinner" /> 加载中...
            </div>
            <div v-else-if="loadError" class="list-placeholder error-text">
              加载失败: {{ loadError }}
              <button class="btn-link" @click="loadSkills">重试</button>
            </div>
            <div v-else-if="filteredSkills.length === 0" class="list-placeholder">
              {{ skills.length === 0 ? '暂无技能,上传一个 zip 开始' : '没有匹配的技能' }}
            </div>

            <ul v-else class="skill-list">
              <li
                v-for="s in filteredSkills"
                :key="`${s.scenario_id}/${s.name}`"
                class="skill-item"
                :class="{ active: activeSkill && activeSkill.scenario_id === s.scenario_id && activeSkill.name === s.name }"
              >
                <button class="skill-main-btn" @click="openSkill(s)">
                  <span class="skill-name">{{ s.name }}</span>
                  <span class="skill-desc">{{ s.description }}</span>
                </button>
                <span class="skill-tag" :class="isBuiltin(s) ? 'tag-builtin' : 'tag-mine'">
                  {{ isBuiltin(s) ? '系统内置' : '我的' }}
                </span>
                <button
                  v-if="s.owned"
                  class="btn-delete"
                  title="删除技能"
                  aria-label="删除技能"
                  @click="handleDelete(s)"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6h14" />
                  </svg>
                </button>
              </li>
            </ul>
          </section>
        </main>
      </div>

      <!-- ============ 右侧栏:文件列表 + 文件编辑器 ============ -->
      <aside v-if="activeSkill" class="detail-panel">
        <header class="panel-header">
          <div class="panel-title-col">
            <div class="panel-title-row">
              <h2 class="panel-title">{{ activeSkill.name }}</h2>
              <span class="skill-tag" :class="isBuiltin(activeSkill) ? 'tag-builtin' : 'tag-mine'">
                {{ isBuiltin(activeSkill) ? '系统内置' : '我的' }}
              </span>
            </div>
            <p class="panel-desc">{{ activeSkill.description }}</p>
          </div>
          <button class="panel-close" aria-label="关闭" title="关闭" @click="closeSkill">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" x2="6" y1="6" y2="18" /><line x1="6" x2="18" y1="6" y2="18" />
            </svg>
          </button>
        </header>

        <div class="panel-body">
          <!-- 文件列表 -->
          <div class="file-sidebar">
            <div v-if="filesLoading" class="sidebar-placeholder">
              <span class="status-spinner" /> 加载中...
            </div>
            <div v-else-if="filesError" class="sidebar-placeholder error-text">
              加载失败
              <button class="btn-link" @click="loadFiles(activeSkill)">重试</button>
            </div>
            <ul v-else class="file-list">
              <li v-for="row in fileTreeRows" :key="row.kind + ':' + row.path">
                <button
                  v-if="row.kind === 'folder'"
                  class="file-item folder-item"
                  :style="{ paddingLeft: (8 + row.depth * 14) + 'px' }"
                  :title="row.path"
                  :aria-expanded="expandedFolders.has(row.path)"
                  @click="toggleFolder(row.path)"
                >
                  <svg
                    class="chevron"
                    :class="{ expanded: expandedFolders.has(row.path) }"
                    width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                  <svg class="folder-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                  </svg>
                  <span class="file-label">{{ row.name }}</span>
                </button>
                <button
                  v-else
                  class="file-item"
                  :class="{ active: activePath === row.path }"
                  :style="{ paddingLeft: (8 + row.depth * 14) + 'px' }"
                  :title="row.path"
                  @click="selectFile(row.path)"
                >
                  <svg class="file-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                  </svg>
                  <span class="file-label">{{ row.name }}</span>
                </button>
              </li>
            </ul>
          </div>

          <!-- 文件编辑器 -->
          <section class="editor-panel">
            <template v-if="activePath">
              <header class="editor-toolbar">
                <div class="toolbar-left">
                  <span class="editor-file-name" :title="activePath">{{ activePath }}</span>
                  <span v-if="activeFile" class="editor-file-size">{{ formatSize(activeFile.size) }}</span>
                  <span v-if="!canEdit" class="readonly-tag">只读</span>
                  <span v-else-if="isDirty" class="dirty-tag">未保存</span>
                </div>
                <div class="toolbar-right">
                  <!-- md 文件:编辑/预览切换 -->
                  <div v-if="isMd(activePath)" class="mode-switch" role="group" aria-label="编辑/预览">
                    <button
                      :class="['mode-btn', { active: mode === 'edit' }]"
                      :disabled="fileLoading"
                      @click="mode = 'edit'"
                    >编辑</button>
                    <button
                      :class="['mode-btn', { active: mode === 'preview' }]"
                      :disabled="fileLoading"
                      @click="mode = 'preview'"
                    >预览</button>
                  </div>
                  <button
                    v-if="canEdit"
                    class="btn-save"
                    :disabled="!canSave"
                    @click="handleSave"
                  >
                    <span v-if="saving" class="btn-spinner" />
                    {{ saving ? '保存中...' : (isDirty ? '保存' : '已保存') }}
                  </button>
                </div>
              </header>

              <div class="editor-content">
                <div v-if="fileLoading" class="content-placeholder">
                  <span class="status-spinner" /> 加载中...
                </div>
                <div v-else-if="fileError" class="content-placeholder error-text">
                  {{ fileError }}
                  <button class="btn-link" @click="selectFile(activePath)">重试</button>
                </div>
                <!-- 编辑模式:CodeMirror 编辑器(:key 切换文件时重建,清空撤销栈) -->
                <CodeMirrorEditor
                  v-else-if="mode === 'edit'"
                  :key="`${activeSkill.scenario_id}/${activeSkill.name}/${activePath}`"
                  v-model="draft"
                  class="code-area"
                  placeholder="暂无内容"
                  :disabled="!canEdit || saving"
                />
                <!-- 预览模式:Markdown 渲染 -->
                <div v-else class="md-preview">
                  <div v-if="previewHtml" class="markdown-body" v-html="previewHtml" />
                  <div v-else class="preview-empty">暂无内容</div>
                </div>
              </div>
            </template>

            <div v-else class="editor-empty">
              <p v-if="filesLoading"><span class="status-spinner" /> 加载中...</p>
              <p v-else>请从左侧选择一个文件</p>
            </div>
          </section>
        </div>
      </aside>
    </div>

    <!-- 同名覆盖确认弹窗 -->
    <Transition name="dialog-fade">
      <div
        v-if="overwriteConfirmOpen"
        class="dialog-mask"
        @click.self="cancelOverwrite"
      >
        <div
          class="dialog-card"
          role="dialog"
          aria-modal="true"
          aria-labelledby="overwrite-dialog-title"
        >
          <header class="dialog-header">
            <h3 id="overwrite-dialog-title">技能已存在</h3>
            <button class="dialog-close" aria-label="关闭" @click="cancelOverwrite">×</button>
          </header>
          <div class="dialog-body">
            <p>
              已存在同名技能<span v-if="pendingOverwriteName">「{{ pendingOverwriteName }}」</span>,是否覆盖?覆盖后原技能将被替换。
            </p>
          </div>
          <footer class="dialog-footer">
            <button class="dialog-btn dialog-btn-cancel" @click="cancelOverwrite">取消</button>
            <button class="dialog-btn dialog-btn-confirm" @click="confirmOverwrite">覆盖</button>
          </footer>
        </div>
      </div>
    </Transition>

    <!-- Toast -->
    <div v-if="toast" class="toast" :class="`toast-${toast.type}`">
      {{ toast.msg }}
    </div>
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

/* ============ 中栏(宽度与 CLI 设置页一致) ============ */
.skill-center {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

.main-col {
  max-width: 680px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5) var(--space-8);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

/* ============ 分区(无卡片包裹:平铺在页面上,分区之间用分隔线) ============ */
.upload-card,
.list-card {
  padding-bottom: var(--space-6);
  border-bottom: 1px solid var(--color-border);
}

.list-card {
  padding-bottom: 0;
  border-bottom: none;
}

.section-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.section-desc {
  margin-top: var(--space-1);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.section-desc code {
  font-family: var(--font-mono, monospace);
  font-size: 0.9em;
  background: var(--color-surface-alt);
  padding: 1px 5px;
  border-radius: var(--radius-sm);
}

/* ============ 上传区 ============ */
.upload-row {
  margin-top: var(--space-4);
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.file-input-hidden {
  display: none;
}

/* 左侧选择框:点击选择文件,回显文件名 */
.file-picker {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 40px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  border: 1px dashed var(--color-border-strong, var(--color-border));
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast), color var(--transition-fast);
}

.file-picker:hover {
  border-color: var(--color-primary);
  color: var(--color-text-secondary);
}

.file-picker.has-file {
  border-style: solid;
  border-color: var(--color-border);
  color: var(--color-text);
}

.picker-icon {
  flex-shrink: 0;
  color: var(--color-text-muted);
}

.file-picker.has-file .picker-icon {
  color: var(--color-primary);
}

.picker-text {
  flex: 1;
  min-width: 0;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 右侧上传按钮 */
.btn-upload {
  flex-shrink: 0;
  height: 40px;
  padding: 0 var(--space-5);
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-upload:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-upload:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

/* ============ 列表 ============ */
.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.list-count {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.list-search {
  position: relative;
  display: flex;
  align-items: center;
  margin-top: var(--space-3);
}

.search-icon {
  position: absolute;
  left: var(--space-3);
  color: var(--color-text-muted);
  pointer-events: none;
}

.search-input {
  width: 100%;
  height: 34px;
  padding: 0 var(--space-8) 0 34px;
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  outline: none;
  transition: border-color var(--transition-fast);
}

.search-input:focus {
  border-color: var(--color-primary);
}

.search-input::placeholder {
  color: var(--color-text-muted);
}

.search-clear {
  position: absolute;
  right: var(--space-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-1);
  color: var(--color-text-muted);
  background: none;
  border: none;
  border-radius: var(--radius-sm, var(--radius-md));
  cursor: pointer;
  transition: color var(--transition-fast);
}

.search-clear:hover {
  color: var(--color-text);
}

.list-placeholder {
  padding: var(--space-8) 0;
  text-align: center;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.error-text {
  color: var(--color-danger);
}

.btn-link {
  color: var(--color-primary);
  background: none;
  border: none;
  cursor: pointer;
  font-size: var(--fs-sm);
}

.skill-list {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.skill-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast);
}

.skill-item:hover {
  border-color: var(--color-border);
}

.skill-item.active {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.skill-main-btn {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  text-align: left;
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}

.skill-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
}

.skill-desc {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.skill-tag {
  flex-shrink: 0;
  padding: 2px 8px;
  font-size: 12px;
  border-radius: var(--radius-full);
}

.tag-builtin {
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.tag-mine {
  color: var(--color-primary);
  background: var(--color-primary-light, rgba(99, 102, 241, 0.12));
}

.btn-delete {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-delete:hover {
  color: var(--color-danger);
  border-color: var(--color-danger);
  background: var(--color-danger-light);
}

/* ============ 右侧栏 ============ */
.detail-panel {
  width: clamp(520px, 48vw, 700px);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border-left: 1px solid var(--color-border);
  overflow: hidden;
}

/* ---- 响应式:窄屏 ---- */
/* 中宽屏:右栏改为覆盖式抽屉(定位基准为 .page-body) */
@media (max-width: 1024px) {
  .detail-panel {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    width: min(700px, 85vw);
    box-shadow: var(--shadow-xl);
  }
}

/* 手机档:抽屉近全屏 + 中栏内边距收紧 + 上传行堆叠 */
@media (max-width: 640px) {
  .detail-panel {
    width: 100vw;
  }

  .main-col {
    padding: var(--space-4) var(--space-3) var(--space-6);
    gap: var(--space-4);
  }

  .upload-row {
    flex-direction: column;
    align-items: stretch;
  }

  /* 列表项删除按钮触控目标增大 */
  .btn-delete {
    width: 36px;
    height: 36px;
  }
}

.panel-header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.panel-title-col {
  flex: 1;
  min-width: 0;
}

.panel-title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.panel-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel-desc {
  margin: 2px 0 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
}

.panel-close:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
}

/* ---- 文件列表 ---- */
.file-sidebar {
  width: 190px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.sidebar-placeholder {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.file-list {
  list-style: none;
  margin: 0;
  padding: var(--space-2);
  flex: 1;
  overflow-y: auto;
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
}

.file-item:hover {
  background: var(--color-surface-alt);
}

.file-item.active {
  background: var(--color-primary-light);
  color: var(--color-primary-hover);
  font-weight: var(--fw-semibold);
}

.file-icon {
  color: var(--color-text-muted);
  flex-shrink: 0;
}

.file-item.active .file-icon {
  color: var(--color-primary);
}

.file-label {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---- 文件树:文件夹行 ---- */
.folder-item {
  color: var(--color-text-secondary);
}

.chevron {
  color: var(--color-text-muted);
  flex-shrink: 0;
  transition: transform var(--transition-fast);
}

.chevron.expanded {
  transform: rotate(90deg);
}

.folder-icon {
  color: var(--color-text-muted);
  flex-shrink: 0;
}

.folder-item:hover .folder-icon,
.folder-item:hover .chevron {
  color: var(--color-text);
}

/* ---- 文件编辑器 ---- */
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
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  flex-shrink: 0;
}

.toolbar-left {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.editor-file-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-file-size {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.readonly-tag,
.dirty-tag {
  flex-shrink: 0;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: var(--radius-full);
}

.readonly-tag {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
}

.dirty-tag {
  color: var(--color-warning, #f59e0b);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-warning, #f59e0b);
}

.toolbar-right {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

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

.btn-save {
  height: 30px;
  padding: 0 var(--space-3);
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-save:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

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
  padding: var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.code-area {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--color-surface);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

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

.editor-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
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

/* ============ Toast ============ */
.toast {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 60;
  padding: var(--space-3) var(--space-5);
  font-size: var(--fs-sm);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  max-width: 80vw;
}

.toast-success {
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-primary);
}

.toast-error {
  color: var(--color-danger);
  background: var(--color-danger-light);
  border: 1px solid var(--color-danger);
}

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: -2px;
  margin-right: var(--space-2);
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ============ 同名覆盖确认弹窗 ============ */
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
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.15);
  width: 100%;
  max-width: 420px;
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

.dialog-close:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-body {
  padding: var(--space-5);
  font-size: var(--fs-sm);
  color: var(--color-text);
  line-height: 1.6;
}

.dialog-body p {
  margin: 0;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.dialog-btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  border: 1px solid transparent;
  transition: all var(--transition-fast);
}

.dialog-btn-cancel {
  background: var(--color-surface-alt);
  color: var(--color-text);
  border-color: var(--color-border);
}

.dialog-btn-cancel:hover {
  background: var(--color-border);
}

.dialog-btn-confirm {
  background: var(--color-danger);
  color: var(--color-text-inverse);
}

.dialog-btn-confirm:hover {
  filter: brightness(1.08);
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
