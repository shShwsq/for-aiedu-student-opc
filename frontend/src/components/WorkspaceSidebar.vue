<script setup lang="ts">
/**
 * 侧栏:历史任务列表 + 按需切换到某任务的工作区
 *
 * 双视图:
 * - view='tasks':显示历史任务列表,每项右侧有 📁 按钮,点击切换到该任务的工作区
 * - view='workspace':显示该任务的文件树(懒加载),顶部有返回按钮
 *
 * 文件树用懒加载:首次只加载根目录,点击文件夹时才加载子目录。
 * 树扁平化为带 depth 的一维列表渲染,避免递归组件。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  getWorkspaceInfo,
  getWorkspaceTree,
  getWorkspaceUploadsTree,
  listWorkspaceFiles,
  readWorkspaceFile,
  readWorkspaceUploadsFile,
} from '@/api/workspace'
import { useFileDownload } from '@/composables/useFileDownload'
import { useWorkspaceRestore } from '@/composables/useWorkspaceRestore'
import {
  deleteTask,
  listTasks,
  updateTaskTitle,
} from '@/api/task'
import FileBinaryCard from '@/components/FileBinaryCard.vue'
import { extractErrorMessage } from '@/utils/error'
import { isLikelyBinaryPath } from '@/utils/fileKind'
import { toWorkspaceRelative } from '@/utils/workspacePath'
import FileContentViewer from './FileContentViewer.vue'
import type { TaskListItem, TaskStatus } from '@/types/task'
import type {
  WorkspaceEntry,
  WorkspaceTreeResponse,
  WorkspaceUploadsTreeResponse,
} from '@/types/workspace'

const router = useRouter()
const route = useRoute()

// ============================================================
// 当前查看的任务(基于路由 /tasks/:id,用于侧栏 active 高亮)
// ============================================================

const activeTaskId = computed<string | null>(() =>
  route.name === 'task-detail' ? (route.params.id as string) ?? null : null,
)

// ============================================================
// 对外事件:任务被删除/标题被修改时通知父组件(父可据此跳转/同步状态)
// ============================================================

const emit = defineEmits<{
  (e: 'task-deleted', taskId: string): void
  (e: 'task-title-updated', taskId: string, title: string | null): void
  (e: 'open-diff-file', path: string): void
}>()

// ============================================================
// 外部输入:工作区不可用时的变更文件兜底列表
// ============================================================

// 父组件(TaskDetailView)从 git_diff artifact 解析传入;
// 仅在工作区不可用且任务非运行中时展示,点击跳转主区对应 diff 块。
// repoFiles 为 repo_tree 快照产物(逐行路径),无变更文件时的二级兜底
const props = defineProps<{ changedFiles?: string[]; repoFiles?: string[] }>()

/** 变更文件列表(空则不可用分支维持原文案) */
const changedFileList = computed(() => props.changedFiles ?? [])

/** 仓库文件清单快照(变更文件为空时的兜底展示,点击仅弹轻提示) */
const repoFileList = computed(() => props.repoFiles ?? [])

// ---- 仓库文件清单:按文件树方式渲染(从扁平路径本地构建嵌套树,无懒加载) ----

interface RepoTreeNode {
  name: string
  path: string
  type: 'dir' | 'file'
  expanded: boolean
  children: RepoTreeNode[]
}

const repoTreeRoot = ref<RepoTreeNode | null>(null)

/** 从扁平相对路径构建嵌套树(中间目录由路径段推导) */
function buildRepoTree(paths: string[]): RepoTreeNode {
  const root: RepoTreeNode = {
    name: '', path: '', type: 'dir', expanded: true, children: [],
  }
  for (const p of paths) {
    const parts = p.split('/').filter(Boolean)
    let node = root
    let acc = ''
    parts.forEach((seg, i) => {
      acc = acc ? `${acc}/${seg}` : seg
      let child = node.children.find((c) => c.name === seg)
      if (!child) {
        child = {
          name: seg,
          path: acc,
          type: i === parts.length - 1 ? 'file' : 'dir',
          expanded: false,
          children: [],
        }
        node.children.push(child)
      }
      node = child
    })
  }
  sortRepoTreeChildren(root)
  return root
}

/** 排序与文件树一致:目录在前、名称大小写不敏感 */
function sortRepoTreeChildren(node: RepoTreeNode): void {
  node.children.sort((a, b) =>
    a.type !== b.type
      ? (a.type === 'dir' ? -1 : 1)
      : a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
  )
  for (const c of node.children) {
    if (c.type === 'dir') sortRepoTreeChildren(c)
  }
}

// 清单变化时重建树(默认仅顶层可见、目录收起,避免大仓库一次性铺满)
watch(
  repoFileList,
  (paths) => {
    repoTreeRoot.value = paths.length ? buildRepoTree(paths) : null
  },
  { immediate: true },
)

/** 扁平化仓库快照树(仅展开的目录),复用文件树样式渲染 */
const flatRepoTree = computed(() => {
  const out: { node: RepoTreeNode; depth: number }[] = []
  const walk = (n: RepoTreeNode, depth: number) => {
    for (const child of n.children) {
      out.push({ node: child, depth })
      if (child.type === 'dir' && child.expanded) walk(child, depth + 1)
    }
  }
  if (repoTreeRoot.value) walk(repoTreeRoot.value, 0)
  return out
})

/** 目录展开/收起(纯前端状态,无请求) */
function toggleRepoDir(node: RepoTreeNode): void {
  node.expanded = !node.expanded
}

// ---- 轻提示(仓库文件行点击:工作区不可用;组件级 toast,同 TaskRuntimeSettings 模式) ----
const toastMsg = ref('')
let toastTimer: ReturnType<typeof setTimeout> | null = null

/** 弹轻提示,2s 后自动消失 */
function showUnavailableToast(): void {
  toastMsg.value = '工作区不可用，内容不可浏览'
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toastMsg.value = ''
    toastTimer = null
  }, 2000)
}

onUnmounted(() => {
  if (toastTimer) clearTimeout(toastTimer)
})

// ============================================================
// 视图状态
// ============================================================

type View = 'tasks' | 'workspace'
const view = ref<View>('tasks')

// ============================================================
// 任务列表
// ============================================================

const tasks = ref<TaskListItem[]>([])
const loadingTasks = ref(false)
const tasksError = ref('')

// ---- 全文搜索(任务标题 + 对话/结果内容) ----
/** 搜索输入框是否展开 */
const searchOpen = ref(false)
/** 搜索输入框当前值 */
const searchQuery = ref('')
/** 搜索输入框引用(展开时聚焦) */
const searchInputRef = ref<HTMLInputElement | null>(null)
/** 防抖计时器 */
let searchTimer: ReturnType<typeof setTimeout> | null = null
/** 是否处于搜索态(已输入关键词,列表展示的是搜索结果) */
const isSearching = computed(() => searchQuery.value.trim().length > 0)

/** 切换搜索框展开/收起;收起时清空查询并恢复原始列表 */
function toggleSearch(): void {
  if (searchOpen.value) {
    // 已展开:点击图标 = 收起 + 清空
    closeSearch()
  } else {
    searchOpen.value = true
    nextTick(() => searchInputRef.value?.focus())
  }
}

/** 收起搜索框并清空查询(若之前有查询则重新加载原始列表) */
function closeSearch(): void {
  searchOpen.value = false
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  if (searchQuery.value) {
    searchQuery.value = ''
    void loadTasks()
  }
}

/** 清空输入(不收起框),触发列表恢复 */
function clearSearchQuery(): void {
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  searchQuery.value = ''
  void loadTasks()
  nextTick(() => searchInputRef.value?.focus())
}

/** 输入时防抖触发搜索(空字符串则恢复原始列表) */
function onSearchInput(): void {
  if (searchTimer) {
    clearTimeout(searchTimer)
    searchTimer = null
  }
  searchTimer = setTimeout(() => {
    void loadTasks()
  }, 350)
}

/** 输入框键盘:Enter 立即搜索,Esc 收起 */
function onSearchKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter') {
    e.preventDefault()
    e.stopPropagation()
    if (searchTimer) {
      clearTimeout(searchTimer)
      searchTimer = null
    }
    void loadTasks()
  } else if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    closeSearch()
  }
}

async function loadTasks(): Promise<void> {
  loadingTasks.value = true
  tasksError.value = ''
  try {
    const q = searchQuery.value.trim()
    tasks.value = await listTasks({ limit: 50, ...(q ? { q } : {}) })
  } catch (e) {
    tasksError.value = extractErrorMessage(e)
  } finally {
    loadingTasks.value = false
  }
}

/** 选中的任务(用于工作区视图) */
const selectedTaskId = ref<string | null>(null)
const selectedTask = computed<TaskListItem | null>(() =>
  tasks.value.find((t) => t.id === selectedTaskId.value) ?? null,
)
const selectedTaskRunning = computed(
  () =>
    selectedTask.value?.status === 'pending' ||
    selectedTask.value?.status === 'running' ||
    selectedTask.value?.status === 'paused',
)

function openWorkspace(taskId: string): void {
  selectedTaskId.value = taskId
  view.value = 'workspace'
  resetFileTree()
  ensureInitialized()
}

function backToTasks(): void {
  view.value = 'tasks'
  stopPolling()
}

function goToTaskDetail(taskId: string): void {
  router.push(`/tasks/${taskId}`)
}

function goToNewTask(): void {
  router.push('/tasks/new')
}

// ============================================================
// 任务项"更多操作"菜单(三个点按钮)+ 修改标题 / 删除任务
// ============================================================

/** 当前展开菜单的任务 id(null=无菜单展开) */
const openMenuTaskId = ref<string | null>(null)
/** 菜单定位(基于按钮 boundingClientRect,固定到视口) */
const menuPos = reactive({ top: 0, right: 0 })

/** 当前展开菜单对应的任务对象(便于模板渲染菜单项) */
const openMenuTask = computed<TaskListItem | null>(() => {
  if (!openMenuTaskId.value) return null
  return tasks.value.find((t) => t.id === openMenuTaskId.value) ?? null
})

function toggleTaskMenu(task: TaskListItem, event: MouseEvent): void {
  event.stopPropagation()
  if (openMenuTaskId.value === task.id) {
    closeTaskMenu()
    return
  }
  const btn = event.currentTarget as HTMLElement
  const rect = btn.getBoundingClientRect()
  // 菜单宽度 ~160px,放在按钮左下方,右边缘对齐按钮右边缘
  menuPos.top = rect.bottom + 4
  menuPos.right = Math.max(8, window.innerWidth - rect.right)
  openMenuTaskId.value = task.id
}

function closeTaskMenu(): void {
  openMenuTaskId.value = null
}

// ---- 修改标题(就地内联编辑) ----

/** 当前正在编辑标题的任务 id(null=不在编辑) */
const editingTaskId = ref<string | null>(null)
/** 编辑中的标题草稿 */
const editingDraft = ref('')
/** 编辑中的原标题(用于比较是否变化、Esc 还原) */
const editingOriginal = ref('')
/** 编辑中是否正在提交(禁用输入框) */
const editingLoading = ref(false)
/** 输入框引用(用于打开编辑时聚焦 + 选中文本) */
const editInputRef = ref<HTMLInputElement | null>(null)

/** 进入编辑模式:点击"修改标题"菜单项后,任务项标题就地变输入框 */
function startEditTitle(task: TaskListItem): void {
  closeTaskMenu()
  editingTaskId.value = task.id
  editingOriginal.value = task.title ?? ''
  editingDraft.value = task.title ?? ''
  editingLoading.value = false
  // 下一个 tick 聚焦 + 选中文本,便于直接覆盖输入
  nextTick(() => {
    const el = editInputRef.value
    if (el) {
      el.focus()
      el.select()
    }
  })
}

function cancelEditTitle(): void {
  if (editingLoading.value) return
  editingTaskId.value = null
  editingDraft.value = ''
  editingOriginal.value = ''
}

async function commitEditTitle(): Promise<void> {
  if (!editingTaskId.value || editingLoading.value) return
  const taskId = editingTaskId.value
  const trimmed = editingDraft.value.trim()
  // 标题无变化直接退出编辑(不算失败)
  if (trimmed === editingOriginal.value.trim()) {
    editingTaskId.value = null
    return
  }
  editingLoading.value = true
  try {
    await updateTaskTitle(taskId, trimmed)
    // 同步本地列表
    const idx = tasks.value.findIndex((t) => t.id === taskId)
    if (idx >= 0) {
      tasks.value[idx] = {
        ...tasks.value[idx],
        title: trimmed || null,
      }
    }
    emit('task-title-updated', taskId, trimmed || null)
    editingTaskId.value = null
  } catch (e) {
    // 失败:保留编辑态,把光标还给输入框,让用户看到错误后修改
    editingLoading.value = false
    // 把错误信息写到任务列表的临时提示位(避免用 alert)
    const idx = tasks.value.findIndex((t) => t.id === taskId)
    if (idx >= 0) {
      ;(tasks.value[idx] as TaskListItem & { _editError?: string })._editError =
        extractErrorMessage(e)
    }
    nextTick(() => editInputRef.value?.focus())
  } finally {
    editingLoading.value = false
  }
}

/** 编辑中按键盘:Enter 提交,Esc 取消 */
function onEditKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter') {
    e.preventDefault()
    void commitEditTitle()
  } else if (e.key === 'Escape') {
    e.preventDefault()
    cancelEditTitle()
  }
}

// ---- 删除任务确认弹窗 ----

const deleteState = reactive({
  open: false,
  task: null as TaskListItem | null,
  loading: false,
  error: '' as string,
})

function openDeleteDialog(task: TaskListItem): void {
  closeTaskMenu()
  deleteState.task = task
  deleteState.error = ''
  deleteState.loading = false
  deleteState.open = true
}

function closeDeleteDialog(): void {
  if (deleteState.loading) return
  deleteState.open = false
  deleteState.task = null
}

/** 删除的任务是否处于运行/暂停状态(需额外警告) */
const deleteTargetRunning = computed(
  () =>
    deleteState.task?.status === 'running' ||
    deleteState.task?.status === 'paused' ||
    deleteState.task?.status === 'pending',
)

async function confirmDeleteTask(): Promise<void> {
  if (!deleteState.task) return
  const taskId = deleteState.task.id
  deleteState.loading = true
  deleteState.error = ''
  try {
    await deleteTask(taskId)
    // 从本地列表移除
    tasks.value = tasks.value.filter((t) => t.id !== taskId)
    // 若删除的是当前展开工作区的任务,返回任务列表视图
    if (selectedTaskId.value === taskId) {
      backToTasks()
    }
    emit('task-deleted', taskId)
    deleteState.open = false
    deleteState.task = null
  } catch (e) {
    deleteState.error = extractErrorMessage(e)
  } finally {
    deleteState.loading = false
  }
}

// ESC 关闭菜单(编辑态由输入框自己的 keydown 处理)
function onGlobalKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  if (deleteState.open) return // 删除弹窗自有取消按钮
  if (editingTaskId.value) return // 编辑态输入框已处理 Esc
  closeTaskMenu()
}

onMounted(() => {
  window.addEventListener('keydown', onGlobalKeydown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
})

// ============================================================
// 工作区可用性
// ============================================================

const available = ref(false)
const unavailableReason = ref('')
const checkingAvailable = ref(false)
/** 后端返回的工作区根绝对路径:跳转前用它把模型/CLI 给的路径剥成相对路径。
 *  local 模式下根目录是随机命名的宿主机临时目录,不能靠固定前缀猜 */
const repoPath = ref('')

// ---- 重新克隆(工作区过期后用户显式触发,复用 restore 接口) ----

/** 任务带 repo_url(纯上传任务无可恢复仓库,不显示按钮;由工作区信息返回) */
const canRestore = ref(false)
const {
  restoring, restoreError, restorePercent, restoreMessage,
  run: runRestore, reset: resetRestore,
} = useWorkspaceRestore()

/** 重新克隆工作区:后台 job + 轮询进度(大仓库分钟级,不能挂住请求等);
 * 成功后重置文件树并重新检查可用性(自动加载新树) */
async function handleRestore(): Promise<void> {
  const taskId = selectedTaskId.value
  if (!taskId) return
  await runRestore(taskId, async () => {
    // 恢复期间用户可能已切走:仅仍停留在该任务时刷新树
    if (selectedTaskId.value === taskId) {
      resetFileTree()
      await checkAvailable()
    }
  })
}

// ============================================================
// 用户上传回退浏览(沙箱过期后仍可浏览,独立于沙箱 TTL)
// ============================================================

/** 任务带用户上传(工作区信息返回;沙箱不可用时回退显示) */
const hasUploads = ref(false)
/** 上传回退树根节点(null = 未加载) */
const uploadsTreeRoot = ref<TreeNode | null>(null)
/** 已被 GC 清理的上传占位标签 */
const uploadsUnavailable = ref<string[]>([])
/** 上传树加载中 */
const loadingUploads = ref(false)
/** 上传树条目超上限截断 */
const uploadsTruncated = ref(false)
/** 上传树是否已拉取(每次进入工作区只拉一次) */
let uploadsTreeLoaded = false

/** 上传回退树已就绪 */
const uploadsTreeReady = computed(() => uploadsTreeRoot.value !== null)

/** 扁平化上传树(仅展开的目录),复用文件树样式渲染 */
const flatUploadsTree = computed<FlatNode[]>(() => {
  const root = uploadsTreeRoot.value
  if (!root) return []
  const out: FlatNode[] = []
  const walk = (n: TreeNode, depth: number) => {
    if (depth >= 0) out.push({ node: n, depth })
    if (n.type === 'dir' && n.expanded) {
      for (const child of n.children) walk(child, depth + 1)
    }
  }
  walk(root, -1)
  return out
})

// ============================================================
// 文件树
// ============================================================

interface TreeNode {
  name: string
  path: string // 相对仓库的路径(根节点为 "")
  type: 'dir' | 'file'
  expanded: boolean
  loaded: boolean
  loading: boolean
  children: TreeNode[]
}

const treeRoot = reactive<TreeNode>({
  name: '',
  path: '',
  type: 'dir',
  expanded: true,
  loaded: false,
  loading: false,
  children: [],
})

// ---- 当前选中文件 + 内容 ----
const selectedFilePath = ref<string | null>(null)
const fileContent = ref('')
const fileStartLine = ref(0)
const fileEndLine = ref(0)
const fileTotalLines = ref(0)
const fileTruncated = ref(false)
const loadingFile = ref(false)
const fileOffset = ref(1)
/** 当前文件是否二进制(后端 binary 标记或后缀预判):真则改渲染下载卡片 */
const fileBinary = ref(false)
/** 二进制文件字节数(仅卡片展示用;文本态后端回 0) */
const fileBinarySize = ref(0)
/** 高亮行号(由结果清单点击跳转设置,滚动定位后保留高亮) */
const highlightLine = ref<number | null>(null)
/** 文件查看面板是否被手动隐藏(点击隐藏按钮后为 true,重新选文件时重置) */
const filePanelHidden = ref(false)

/** FileContentViewer 暴露的定位接口(局部类型,避免依赖 SFC 实例类型推导) */
interface FileContentViewerHandle {
  focusRange: (start: number | null, end: number | null) => void
  clearHighlight: () => void
}
const fileViewerRef = ref<FileContentViewerHandle | null>(null)

// ---- 下载当前文件 ----
// 文本文件(尤其 md)原先只有二进制卡片带下载按钮,长文档分页看不全就没法整份取回;
// 这里给文本文件补上入口(二进制仍由 FileBinaryCard 的大按钮负责,头部按钮对二进制
// 隐藏,两个入口各持一份状态会打架)。动作本身与卡片共用 useFileDownload
const {
  downloading: fileDownloading,
  downloadError,
  run: runFileDownload,
  clearError: clearFileDownloadError,
} = useFileDownload()

// ---- 错误提示 ----
const errorMsg = ref('')

/** 整树快照被截断(条目超上限):提示用户,未覆盖目录展开时退回懒加载 */
const treeTruncated = ref(false)

function resetFileTree(): void {
  treeRoot.loaded = false
  treeRoot.loading = false
  treeRoot.children = []
  treeRoot.expanded = true
  selectedFilePath.value = null
  selectedFileSource.value = 'workspace'
  fileContent.value = ''
  fileBinary.value = false
  fileBinarySize.value = 0
  available.value = false
  unavailableReason.value = ''
  repoPath.value = ''
  errorMsg.value = ''
  treeTruncated.value = false
  initialized = false
  filePanelHidden.value = false
  // 重新克隆状态一并重置(切换任务时不残留上个任务的按钮/错误,并终止其轮询)
  canRestore.value = false
  resetRestore()
  // 上传回退状态一并重置(切换任务时不残留上个任务的上传树)
  hasUploads.value = false
  uploadsTreeRoot.value = null
  uploadsUnavailable.value = []
  uploadsTruncated.value = false
  loadingUploads.value = false
  uploadsTreeLoaded = false
  // 下载失败文案一并清掉(切任务不残留上个任务的报错;进行中的下载照常送达)
  clearFileDownloadError()
}

// ============================================================
// 扁平化树:把嵌套的 TreeNode 展开成带 depth 的一维列表(仅已展开的目录)
// ============================================================

interface FlatNode {
  node: TreeNode
  depth: number
}

const flatTree = computed<FlatNode[]>(() => {
  const result: FlatNode[] = []
  const walk = (n: TreeNode, depth: number) => {
    if (depth >= 0) result.push({ node: n, depth })
    if (n.type === 'dir' && n.expanded && n.loaded) {
      for (const child of n.children) {
        walk(child, depth + 1)
      }
    }
  }
  walk(treeRoot, -1) // 根节点不显示,从其子节点开始
  return result
})

// ============================================================
// 工作区初始化 + 轮询
// ============================================================

async function checkAvailable(): Promise<void> {
  if (!selectedTaskId.value) return
  checkingAvailable.value = true
  try {
    const info = await getWorkspaceInfo(selectedTaskId.value)
    available.value = info.available
    unavailableReason.value = info.reason ?? ''
    repoPath.value = info.repo_path ?? ''
    hasUploads.value = info.has_uploads ?? false
    canRestore.value = info.can_restore ?? false
    if (info.available && !treeRoot.loaded) {
      await loadTreeSnapshot()
    }
    // 上传树与工作区可用性无关:上传区独立展示,重新克隆后(仓库不含上传
    // 文件)仍要可见。运行中不加载(与其它兜底一致,完成后会再检查)
    if (hasUploads.value && !selectedTaskRunning.value) {
      await loadUploadsTree()
    }
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
  } finally {
    checkingAvailable.value = false
  }
}

/**
 * 拉整树快照一次建好文件树(首屏提速:1 次请求替代逐级懒加载)
 * 失败时降级为根目录懒加载,功能不受损
 */
async function loadTreeSnapshot(refresh: boolean = false): Promise<void> {
  if (!selectedTaskId.value) return
  try {
    const res = await getWorkspaceTree(selectedTaskId.value, refresh)
    buildTreeFromSnapshot(res)
  } catch {
    await loadDir(treeRoot)
  }
}

/** 收集当前处于展开态的目录路径(刷新快照时保持展开状态) */
function collectExpandedPaths(node: TreeNode, acc: Set<string>): void {
  for (const c of node.children) {
    if (c.type === 'dir' && c.expanded) {
      acc.add(c.path)
      collectExpandedPaths(c, acc)
    }
  }
}

/** 从整树快照(扁平 entries)构建嵌套树;截断/深度边界目录保持可懒加载 */
function buildTreeFromSnapshot(
  res: WorkspaceTreeResponse,
  expandedPaths: Set<string> = new Set(),
): void {
  treeRoot.children = []
  treeRoot.loaded = true
  treeRoot.expanded = true
  treeTruncated.value = res.truncated

  const byPath = new Map<string, TreeNode>()
  byPath.set('', treeRoot)

  for (const e of res.entries) {
    const parts = e.path.split('/').filter(Boolean)
    if (parts.length === 0) continue
    const parent = byPath.get(parts.slice(0, -1).join('/'))
    if (!parent) continue // 父目录被截断丢弃,跳过孤儿条目
    const node: TreeNode = {
      name: parts[parts.length - 1],
      path: e.path,
      type: e.type === 'dir' ? 'dir' : 'file',
      expanded: expandedPaths.has(e.path),
      loaded: false,
      loading: false,
      children: [],
    }
    // 目录仅在未截断且未达快照深度边界时子项才算完整,否则展开时重新拉
    if (node.type === 'dir') {
      node.loaded = !res.truncated && parts.length < res.max_depth
    }
    parent.children.push(node)
    byPath.set(e.path, node)
  }
  sortTreeChildren(treeRoot)
}

/** 排序与后端 listDir 一致:目录在前、文件在后,各自按名字大小写不敏感 */
function sortTreeChildren(node: TreeNode): void {
  node.children.sort((a, b) =>
    a.type !== b.type
      ? (a.type === 'dir' ? -1 : 1)
      : a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
  )
  for (const c of node.children) {
    if (c.type === 'dir') sortTreeChildren(c)
  }
}

/**
 * 沙箱不可用且有上传:拉上传文件树(独立于沙箱,保留期内内容可读)
 * 每次进入工作区只拉一次;失败提示错误但不影响其它兜底分区
 */
async function loadUploadsTree(): Promise<void> {
  if (!selectedTaskId.value || uploadsTreeLoaded) return
  loadingUploads.value = true
  try {
    const res = await getWorkspaceUploadsTree(selectedTaskId.value)
    uploadsTreeRoot.value = buildUploadsTree(res)
    uploadsUnavailable.value = res.unavailable
    uploadsTruncated.value = res.truncated
    uploadsTreeLoaded = true
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
  } finally {
    loadingUploads.value = false
  }
}

/** 从上传树快照构建嵌套树(整树一次拉全,目录均视为已加载,无懒加载) */
function buildUploadsTree(res: WorkspaceUploadsTreeResponse): TreeNode {
  const root: TreeNode = {
    name: '',
    path: '',
    type: 'dir',
    expanded: true,
    loaded: true,
    loading: false,
    children: [],
  }
  const byPath = new Map<string, TreeNode>()
  byPath.set('', root)
  for (const e of res.entries) {
    const parts = e.path.split('/').filter(Boolean)
    if (parts.length === 0) continue
    const parent = byPath.get(parts.slice(0, -1).join('/'))
    if (!parent) continue
    const node: TreeNode = {
      name: parts[parts.length - 1],
      path: e.path,
      type: e.type === 'dir' ? 'dir' : 'file',
      expanded: false,
      loaded: true,
      loading: false,
      children: [],
    }
    parent.children.push(node)
    byPath.set(e.path, node)
  }
  sortTreeChildren(root)
  return root
}

/** 上传树目录展开/收起(整树已拉全,无需懒加载) */
function toggleUploadDir(node: TreeNode): void {
  if (node.type !== 'dir') return
  node.expanded = !node.expanded
}

/** 在上传回退树中逐层查找节点(整树已加载,顺带展开途经目录) */
function findInUploadsTree(filePath: string): TreeNode | null {
  const parts = filePath.split('/').filter(Boolean)
  let current: TreeNode | undefined = uploadsTreeRoot.value ?? undefined
  for (const part of parts) {
    if (!current) return null
    if (current.type === 'dir' && !current.expanded) current.expanded = true
    current = current.children.find((c) => c.name === part)
  }
  return current ?? null
}

let pollTimer: ReturnType<typeof setInterval> | null = null

function startPolling(): void {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (available.value || !selectedTaskRunning.value) {
      stopPolling()
      return
    }
    await checkAvailable()
  }, 5000)
}

function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

let initialized = false
async function ensureInitialized(): Promise<void> {
  if (initialized) return
  initialized = true
  await checkAvailable()
  if (!available.value && selectedTaskRunning.value) {
    startPolling()
  }
}

// 选中任务从运行变为完成时,再检查一次(确保拿到最终状态)
watch(
  () => selectedTaskRunning.value,
  (running, wasRunning) => {
    if (wasRunning && !running && !available.value) {
      checkAvailable()
    }
  },
)

onMounted(loadTasks)
onUnmounted(stopPolling)

// ============================================================
// 文件树操作
// ============================================================

async function loadDir(node: TreeNode): Promise<void> {
  if (!selectedTaskId.value || node.loaded || node.loading) return
  node.loading = true
  errorMsg.value = ''
  try {
    const res = await listWorkspaceFiles(selectedTaskId.value, node.path)
    node.children = res.entries.map((e: WorkspaceEntry) => ({
      name: e.name,
      path: node.path ? `${node.path}/${e.name}` : e.name,
      type: e.type,
      expanded: false,
      loaded: false,
      loading: false,
      children: [],
    }))
    node.loaded = true
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
  } finally {
    node.loading = false
  }
}

async function toggleDir(node: TreeNode): Promise<void> {
  if (node.type !== 'dir') return
  if (!node.loaded) {
    await loadDir(node)
  }
  node.expanded = !node.expanded
}

/** 选中文件来源:workspace=工作区树 / uploads=用户上传树(决定读文件走哪个端点)。
 *  不能按 available 分流:重新克隆后工作区可用,但上传文件不在新克隆的仓库里 */
const selectedFileSource = ref<'workspace' | 'uploads'>('workspace')

async function selectFile(
  node: TreeNode,
  source: 'workspace' | 'uploads' = 'workspace',
): Promise<void> {
  if (node.type !== 'file') return
  selectedFilePath.value = node.path
  selectedFileSource.value = source
  fileOffset.value = 1
  highlightLine.value = null // 手动选文件时清除高亮
  filePanelHidden.value = false // 重新选文件时恢复面板显示
  clearFileDownloadError() // 上个文件的下载失败文案不跟着串过来
  // 后缀已知二进制:不发内容请求(后端本来也会拦,但那一趟会把几 MB 占位/乱码
  // 拉过网络),直接进下载卡片;size 未知由卡片自身留空展示
  if (isLikelyBinaryPath(node.path)) {
    fileBinary.value = true
    fileBinarySize.value = 0
    fileContent.value = ''
    fileTotalLines.value = 0
    fileStartLine.value = 0
    fileEndLine.value = 0
    fileTruncated.value = false
    errorMsg.value = ''
    return
  }
  await loadFileContent()
}

/** 手动隐藏文件查看面板(保留选中状态,便于恢复) */
function hideFilePanel(): void {
  filePanelHidden.value = true
}

/** 恢复显示文件查看面板(已选中文件时使用) */
function showFilePanelAgain(): void {
  if (selectedFilePath.value) {
    filePanelHidden.value = false
  }
}

/** 下载当前选中的文件原文(按选中来源分流,与读内容同一口径) */
async function downloadSelectedFile(): Promise<void> {
  await runFileDownload(
    selectedTaskId.value ?? '',
    selectedFilePath.value ?? '',
    selectedFileSource.value,
  )
}

async function loadFileContent(): Promise<void> {
  if (!selectedTaskId.value || !selectedFilePath.value) return
  loadingFile.value = true
  errorMsg.value = ''
  try {
    // 按选中来源分流:工作区树文件走沙箱端点,上传树文件走上传读取端点
    // (上传区独立于工作区状态展示,重新克隆后仍可读上传文件)
    const res = selectedFileSource.value === 'uploads'
      ? await readWorkspaceUploadsFile(
          selectedTaskId.value,
          selectedFilePath.value,
          fileOffset.value,
          500,
        )
      : await readWorkspaceFile(
          selectedTaskId.value,
          selectedFilePath.value,
          fileOffset.value,
          500,
        )
    fileContent.value = res.content
    fileStartLine.value = res.start_line
    fileEndLine.value = res.end_line
    fileTotalLines.value = res.total_lines
    fileTruncated.value = res.truncated
    // 后缀表未覆盖的二进制(如未知后缀的 ELF)由后端标记兜住
    fileBinary.value = res.binary === true
    fileBinarySize.value = res.binary ? res.size || 0 : 0
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
    fileContent.value = ''
    fileBinary.value = false
    fileBinarySize.value = 0
  } finally {
    loadingFile.value = false
  }
  // 内容/分页刷新后按当前 highlightLine 重新定位(FileContentViewer 内部换算真实行→文档行)
  await nextTick()
  applyFileFocus()
}

/** 按 highlightLine 在文件查看器上高亮定位;落在当前页外则清除高亮 */
function applyFileFocus(): void {
  const v = fileViewerRef.value
  if (!v) return
  const line = highlightLine.value
  if (line != null && line >= fileStartLine.value && line <= fileEndLine.value) {
    v.focusRange(line, line)
  } else {
    v.clearHighlight()
  }
}

async function loadNextPage(): Promise<void> {
  if (!fileTruncated.value || loadingFile.value) return
  fileOffset.value = fileEndLine.value + 1
  await loadFileContent()
}

async function loadPrevPage(): Promise<void> {
  if (fileOffset.value <= 1 || loadingFile.value) return
  fileOffset.value = Math.max(1, fileOffset.value - 500)
  await loadFileContent()
}

async function refreshTree(): Promise<void> {
  errorMsg.value = ''
  // 保留当前展开状态,用 refresh=true 绕过后端缓存重建整树快照
  const expandedPaths = new Set<string>()
  collectExpandedPaths(treeRoot, expandedPaths)
  treeRoot.loaded = false
  treeRoot.children = []
  treeTruncated.value = false
  try {
    const res = await getWorkspaceTree(selectedTaskId.value!, true)
    buildTreeFromSnapshot(res, expandedPaths)
  } catch {
    // 快照失败降级:重新拉根目录(懒加载)
    treeRoot.loaded = false
    await loadDir(treeRoot)
  }
}

// ============================================================
// 计算属性
// ============================================================

const selectedFileName = computed(() => {
  if (!selectedFilePath.value) return ''
  const parts = selectedFilePath.value.split('/')
  return parts[parts.length - 1]
})

const hasPrevPage = computed(() => fileOffset.value > 1)
const hasNextPage = computed(() => fileTruncated.value)
const showFilePanel = computed(
  () =>
    view.value === 'workspace' &&
    selectedFilePath.value !== null &&
    !filePanelHidden.value,
)
/** 文件被选中但被手动隐藏时,文件树中显示恢复按钮 */
const showRestoreBtn = computed(
  () =>
    view.value === 'workspace' &&
    selectedFilePath.value !== null &&
    filePanelHidden.value,
)

// ============================================================
// 任务状态徽章
// ============================================================

const statusClassMap: Record<TaskStatus, string> = {
  pending: 'badge-pending',
  running: 'badge-running',
  paused: 'badge-paused',
  completed: 'badge-completed',
  failed: 'badge-failed',
}

const statusLabelMap: Record<TaskStatus, string> = {
  pending: '等待',
  running: '进行',
  paused: '暂停',
  completed: '完成',
  failed: '失败',
}

function statusClass(s: TaskStatus): string {
  return statusClassMap[s] ?? ''
}

function statusLabel(s: TaskStatus): string {
  return statusLabelMap[s] ?? s
}

// ============================================================
// 格式化
// ============================================================

function formatTaskTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function truncateInput(s: string, max = 40): string {
  if (s.length <= max) return s
  return s.slice(0, max) + '...'
}

// ============================================================
// 对外暴露:从结果清单点击跳转打开指定文件并定位行号(B1)
// ============================================================

/** 逐层展开文件树直到目标文件,返回文件节点(懒加载目录) */
async function expandToPath(filePath: string): Promise<TreeNode | null> {
  const parts = filePath.split('/').filter(Boolean)
  let current = treeRoot
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]
    const isLast = i === parts.length - 1
    if (!current.loaded) await loadDir(current)
    const child = current.children.find((c) => c.name === part)
    if (!child) return null
    if (isLast) return child
    // 中间目录:确保展开
    if (!current.expanded) current.expanded = true
    current = child
  }
  return null
}

/** 跳转到指定行:翻到该行所在分页,由查看器滚动 + 高亮 */
async function jumpToLine(line: number): Promise<void> {
  // 二进制文件没有行概念(结果清单极少指向它,但路径可能被 LLM 填成 docx)
  if (fileBinary.value) return
  highlightLine.value = line
  const pageSize = 500
  const targetOffset = Math.floor((line - 1) / pageSize) * pageSize + 1
  if (fileOffset.value !== targetOffset) {
    fileOffset.value = targetOffset
    await loadFileContent() // 分页变化:加载后内部会重新定位
  } else {
    await nextTick()
    applyFileFocus()
  }
}

/**
 * 打开指定任务的指定文件并定位行号(供 TaskDetailView 结果清单点击调用)
 *
 * 流程:切换/初始化任务工作区 → 归一跳转路径 → 逐层展开到目标文件 → 选中加载 → 定位行号
 *
 * filePath 允许是工作区相对路径或绝对路径(后者来自 CLI 工具/模型 metadata),
 * 绝对路径会被剥成相对路径后再查树——树条目路径恒为相对 repo_path 的正斜杠路径。
 */
async function openTaskFile(taskId: string, filePath: string, line?: number): Promise<void> {
  // 切换/初始化任务工作区
  if (selectedTaskId.value !== taskId) {
    selectedTaskId.value = taskId
    view.value = 'workspace'
    resetFileTree()
    await ensureInitialized()
  } else {
    view.value = 'workspace'
    if (!initialized) await ensureInitialized()
  }
  // 路径归一必须排在初始化之后:repoPath 要等工作区信息接口回来才拿得到
  const relPath = toWorkspaceRelative(filePath, repoPath.value)

  if (!available.value) {
    // 沙箱不可用:回退上传树(上传任务过期后结果清单仍可跳转)
    if (!uploadsTreeReady.value) return
    const node = findInUploadsTree(relPath)
    if (!node) return
    await selectFile(node, 'uploads')
    if (line && line > 0) {
      await jumpToLine(line)
    }
    return
  }

  // 展开到目标文件
  const node = await expandToPath(relPath)
  if (node) {
    await selectFile(node)
  } else {
    // 工作区树无此文件(如重新克隆后追问上传文件不在新仓库里):回退上传树
    if (!uploadsTreeReady.value) return
    const upNode = findInUploadsTree(relPath)
    if (!upNode) return
    await selectFile(upNode, 'uploads')
  }

  // 定位行号
  if (line && line > 0) {
    await jumpToLine(line)
  }
}

defineExpose({ openTaskFile })
</script>

<template>
  <div class="workspace-container">
    <!-- 侧栏 -->
    <aside class="workspace-sidebar">
      <!-- 视图:历史任务列表 -->
      <template v-if="view === 'tasks'">
        <div class="sidebar-header">
          <span class="sidebar-title">历史任务</span>
          <div class="sidebar-actions">
            <button
              class="icon-btn"
              :class="{ 'icon-btn-active': searchOpen }"
              title="搜索任务"
              aria-label="搜索任务"
              @click="toggleSearch"
            >
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <circle cx="11" cy="11" r="8" />
                <line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
            </button>
            <button class="icon-btn" title="刷新任务列表" aria-label="刷新任务列表" @click="loadTasks">
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <polyline points="23 4 23 10 17 10" />
                <polyline points="1 20 1 14 7 14" />
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
              </svg>
            </button>
            <button class="icon-btn" title="提交新任务" aria-label="提交新任务" @click="goToNewTask">
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
            </button>
          </div>
        </div>

        <!-- 搜索输入框(展开时显示在 header 下方) -->
        <div v-if="searchOpen" class="sidebar-search">
          <span class="sidebar-search-icon" aria-hidden="true">
            <svg
              viewBox="0 0 24 24"
              width="12"
              height="12"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            >
              <circle cx="11" cy="11" r="8" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
          </span>
          <input
            ref="searchInputRef"
            v-model="searchQuery"
            class="sidebar-search-input"
            type="text"
            maxlength="200"
            placeholder="搜索标题或全文..."
            @input="onSearchInput"
            @keydown="onSearchKeydown"
          />
          <button
            v-if="searchQuery"
            class="sidebar-search-clear"
            title="清空"
            aria-label="清空"
            @click="clearSearchQuery"
          >×</button>
          <span v-else-if="loadingTasks" class="sidebar-search-spinner" aria-hidden="true">
            <span class="spinner-sm" />
          </span>
        </div>

        <div v-if="loadingTasks && tasks.length === 0" class="sidebar-status">
          <span class="spinner-sm" /> {{ isSearching ? '搜索中...' : '加载任务...' }}
        </div>
        <div v-else-if="tasksError" class="sidebar-status sidebar-status-muted">
          <p>{{ tasksError }}</p>
        </div>
        <div v-else-if="tasks.length === 0" class="empty-tree">
          {{ isSearching ? '无匹配任务' : '暂无任务' }}
        </div>
        <div v-else class="task-list">
          <div
            v-for="t in tasks"
            :key="t.id"
            class="task-item"
            :class="{ 'task-item-active': activeTaskId === t.id }"
            @click="goToTaskDetail(t.id)"
          >
            <div class="task-item-main">
              <div class="task-item-top">
                <span :class="['task-status-tag', statusClass(t.status)]">
                  {{ statusLabel(t.status) }}
                </span>
                <!-- 后台审查进行中(任务已完成但检查助手仍在核查) -->
                <span
                  v-if="t.review_status === 'running'"
                  class="task-review-tag"
                  title="检查助手正在后台核查"
                >检查中</span>
                <span class="task-time">{{ formatTaskTime(t.created_at) }}</span>
              </div>
              <!-- 标题:编辑态显示输入框,非编辑态显示文本 -->
              <input
                v-if="editingTaskId === t.id"
                ref="editInputRef"
                v-model="editingDraft"
                class="task-title-edit"
                type="text"
                maxlength="255"
                placeholder="输入标题(留空回退到任务输入)"
                :disabled="editingLoading"
                @click.stop
                @keydown="onEditKeydown"
                @blur="commitEditTitle"
              />
              <p
                v-else
                class="task-input"
                :title="t.title || t.user_input"
              >
                {{ truncateInput(t.title || t.user_input) }}
              </p>
            </div>
            <!-- 更多操作(三个点):修改标题 / 删除任务 -->
            <button
              class="more-btn"
              :class="{ active: openMenuTaskId === t.id }"
              title="更多操作"
              aria-label="更多操作"
              @click.stop="toggleTaskMenu(t, $event)"
            >
              <svg
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="currentColor"
                aria-hidden="true"
              >
                <circle cx="5" cy="12" r="2" />
                <circle cx="12" cy="12" r="2" />
                <circle cx="19" cy="12" r="2" />
              </svg>
            </button>
            <button
              class="workspace-btn"
              title="查看工作区"
              @click.stop="openWorkspace(t.id)"
            >
              <svg
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
              </svg>
            </button>
          </div>
        </div>
        <div v-if="tasksError" class="sidebar-error">{{ tasksError }}</div>
      </template>

      <!-- 视图:工作区文件树 -->
      <template v-else>
        <div class="sidebar-header">
          <button class="icon-btn back-btn" title="返回任务列表" aria-label="返回任务列表" @click="backToTasks">
            <svg
              viewBox="0 0 24 24"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <line x1="19" y1="12" x2="5" y2="12" />
              <polyline points="12 19 5 12 12 5" />
            </svg>
          </button>
          <span class="sidebar-title">文件树</span>
          <div class="sidebar-actions">
            <button
              class="icon-btn"
              title="刷新文件树"
              aria-label="刷新文件树"
              :disabled="!available"
              @click="refreshTree"
            >
              <svg
                viewBox="0 0 24 24"
                width="16"
                height="16"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <polyline points="23 4 23 10 17 10" />
                <polyline points="1 20 1 14 7 14" />
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
              </svg>
            </button>
          </div>
        </div>

        <!-- 检查中 -->
        <div v-if="checkingAvailable && !available" class="sidebar-status">
          <span class="spinner-sm" /> 检查工作区...
        </div>

        <!-- 不可用 -->
        <div v-else-if="!available" class="sidebar-status sidebar-status-muted">
          <p>{{ unavailableReason || '工作区不可用' }}</p>
          <p v-if="selectedTaskRunning" class="status-hint">等待 AI助手 clone 仓库...</p>
          <!-- 过期后重新克隆(纯上传任务无 repo_url 不显示;运行中等待自动 clone) -->
          <div v-if="!selectedTaskRunning && canRestore" class="restore-box">
            <button class="restore-btn" :disabled="restoring" @click="handleRestore">
              {{ restoring
                ? (restorePercent > 0 ? `重新克隆中... ${restorePercent}%` : '正在发起克隆...')
                : '重新克隆' }}
            </button>
            <p v-if="restoring && restoreMessage" class="status-hint">{{ restoreMessage }}</p>
            <p v-if="restoreError" class="restore-error">{{ restoreError }}</p>
          </div>
          <!-- 会话过期但有 diff 产物:展示变更文件列表(点击跳主区 diff,内容不可浏览) -->
          <div
            v-if="!selectedTaskRunning && changedFileList.length > 0"
            class="changed-files-fallback"
          >
            <div class="changed-files-title">变更文件（会话已过期，内容不可浏览）</div>
            <div class="changed-files-list">
              <div
                v-for="path in changedFileList"
                :key="path"
                class="tree-node tree-file changed-file-row"
                :title="path"
                @click="emit('open-diff-file', path)"
              >
                <span class="tree-icon">
                  <svg
                    viewBox="0 0 24 24"
                    width="13"
                    height="13"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  >
                    <path d="M14 3v4a1 1 0 0 0 1 1h4" />
                    <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" />
                  </svg>
                </span>
                <span class="tree-name">{{ path }}</span>
              </div>
            </div>
          </div>
          <!-- 无变更但有仓库树快照:按文件树方式渲染(目录展开/收起纯前端;文件点击弹轻提示) -->
          <div
            v-else-if="!selectedTaskRunning && repoTreeRoot"
            class="changed-files-fallback"
          >
            <div class="changed-files-title">仓库文件清单（快照，内容不可浏览）</div>
            <div class="changed-files-list">
              <div
                v-for="item in flatRepoTree"
                :key="item.node.path"
                class="tree-node"
                :class="`tree-${item.node.type}`"
                :style="{ paddingLeft: `${item.depth * 14 + 8}px` }"
                :title="item.node.path"
                @click="item.node.type === 'dir' ? toggleRepoDir(item.node) : showUnavailableToast()"
              >
                <span class="tree-icon">
                  <template v-if="item.node.type === 'dir'">
                    <svg
                      class="tree-chevron"
                      :class="{ expanded: item.node.expanded }"
                      viewBox="0 0 24 24"
                      width="10"
                      height="10"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <polyline points="9 18 15 12 9 6" />
                    </svg>
                    <svg
                      viewBox="0 0 24 24"
                      width="13"
                      height="13"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                    </svg>
                  </template>
                  <svg
                    v-else
                    viewBox="0 0 24 24"
                    width="13"
                    height="13"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  >
                    <path d="M14 3v4a1 1 0 0 0 1 1h4" />
                    <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" />
                  </svg>
                </span>
                <span class="tree-name">{{ item.node.name }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 文件树(扁平化渲染) -->
        <div v-else class="file-tree">
          <div v-if="treeTruncated" class="tree-truncated-hint">
            文件过多,列表已截断;未列出的目录展开时按需加载
          </div>
          <div
            v-for="item in flatTree"
            :key="item.node.path"
            class="tree-node"
            :class="[
              `tree-${item.node.type}`,
              { 'tree-selected': selectedFilePath === item.node.path },
            ]"
            :style="{ paddingLeft: `${item.depth * 14 + 8}px` }"
            @click="item.node.type === 'dir' ? toggleDir(item.node) : selectFile(item.node)"
          >
            <span class="tree-icon">
              <!-- 文件夹:chevron(展开旋转 90°)+ 文件夹图标,与技能管理文件列表风格一致 -->
              <template v-if="item.node.type === 'dir'">
                <svg
                  class="tree-chevron"
                  :class="{ expanded: item.node.expanded }"
                  viewBox="0 0 24 24"
                  width="10"
                  height="10"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2.5"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <polyline points="9 18 15 12 9 6" />
                </svg>
                <svg
                  viewBox="0 0 24 24"
                  width="13"
                  height="13"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                </svg>
              </template>
              <!-- 文件 -->
              <svg
                v-else
                viewBox="0 0 24 24"
                width="13"
                height="13"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M14 3v4a1 1 0 0 0 1 1h4" />
                <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" />
              </svg>
            </span>
            <span class="tree-name">{{ item.node.name }}</span>
            <span v-if="item.node.loading" class="tree-loading">...</span>
            <!-- 当前选中文件被手动隐藏时,显示恢复查看按钮 -->
            <button
              v-if="
                showRestoreBtn &&
                item.node.type === 'file' &&
                selectedFilePath === item.node.path
              "
              class="tree-restore-btn"
              title="显示文件查看面板"
              aria-label="显示文件查看面板"
              @click.stop="showFilePanelAgain"
            >
              <svg
                viewBox="0 0 24 24"
                width="12"
                height="12"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                <polyline points="15 3 21 3 21 9" />
                <line x1="10" y1="14" x2="21" y2="3" />
              </svg>
            </button>
          </div>
          <div v-if="treeRoot.loaded && treeRoot.children.length === 0" class="empty-tree">
            (空目录)
          </div>
        </div>

        <!-- 用户上传(独立兜底区:与工作区可用性无关,重新克隆后的仓库不含
             上传文件,仍在此常驻展示;保留期内内容可读) -->
        <div
          v-if="!selectedTaskRunning && (hasUploads || loadingUploads)"
          class="uploads-panel"
        >
          <div class="changed-files-title">
            用户上传{{ uploadsUnavailable.length > 0 ? '（部分已过期清理）' : '' }}
          </div>
          <div class="changed-files-list">
            <div v-if="loadingUploads" class="tree-loading">加载上传文件...</div>
            <template v-else-if="uploadsTreeReady">
              <div v-if="uploadsTruncated" class="tree-truncated-hint">
                文件过多,列表已截断
              </div>
              <!-- 已被 GC 清理的上传:占位展示,不可点击 -->
              <div
                v-for="label in uploadsUnavailable"
                :key="`gone-${label}`"
                class="tree-node tree-file uploads-gone"
                :title="`${label}（已过期清理）`"
              >
                <span class="tree-icon">
                  <svg
                    viewBox="0 0 24 24"
                    width="13"
                    height="13"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M14 3v4a1 1 0 0 0 1 1h4" />
                    <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" />
                  </svg>
                </span>
                <span class="tree-name">{{ label }}</span>
                <span class="uploads-gone-tag">已清理</span>
              </div>
              <!-- 上传文件树(整树已拉全,目录仅展开/收起;文件点击走上传读取端点) -->
              <div
                v-for="item in flatUploadsTree"
                :key="`up-${item.node.path}`"
                class="tree-node"
                :class="[
                  `tree-${item.node.type}`,
                  { 'tree-selected': selectedFilePath === item.node.path },
                ]"
                :style="{ paddingLeft: `${item.depth * 14 + 8}px` }"
                :title="item.node.path"
                @click="item.node.type === 'dir' ? toggleUploadDir(item.node) : selectFile(item.node, 'uploads')"
              >
                <span class="tree-icon">
                  <template v-if="item.node.type === 'dir'">
                    <svg
                      class="tree-chevron"
                      :class="{ expanded: item.node.expanded }"
                      viewBox="0 0 24 24"
                      width="10"
                      height="10"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <polyline points="9 18 15 12 9 6" />
                    </svg>
                    <svg
                      viewBox="0 0 24 24"
                      width="13"
                      height="13"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
                    </svg>
                  </template>
                  <svg
                    v-else
                    viewBox="0 0 24 24"
                    width="13"
                    height="13"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  >
                    <path d="M14 3v4a1 1 0 0 0 1 1h4" />
                    <path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z" />
                  </svg>
                </span>
                <span class="tree-name">{{ item.node.name }}</span>
              </div>
              <div
                v-if="flatUploadsTree.length === 0 && uploadsUnavailable.length === 0"
                class="empty-tree"
              >
                (空目录)
              </div>
            </template>
          </div>
        </div>

        <!-- 错误提示 -->
        <div v-if="errorMsg" class="sidebar-error">{{ errorMsg }}</div>
      </template>

      <!-- 轻提示(工作区不可用时点击仓库文件行;2s 自动消失) -->
      <div v-if="toastMsg" class="sidebar-toast">{{ toastMsg }}</div>
    </aside>

    <!-- 文件查看面板(右侧,仅工作区视图且选中文件且未手动隐藏时显示) -->
    <section v-if="showFilePanel" class="file-panel">
      <div class="file-panel-header">
        <span class="file-path" :title="selectedFilePath ?? undefined">{{ selectedFileName }}</span>
        <!-- 二进制文件没有行概念,分页条整体隐藏 -->
        <div v-if="!fileBinary" class="file-pagination">
          <button
            class="page-btn"
            title="上一页"
            aria-label="上一页"
            :disabled="!hasPrevPage || loadingFile"
            @click="loadPrevPage"
          >
            <svg
              viewBox="0 0 24 24"
              width="14"
              height="14"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <line x1="12" y1="19" x2="12" y2="5" />
              <polyline points="5 12 12 5 19 12" />
            </svg>
          </button>
          <span class="page-info">
            {{ fileStartLine }}-{{ fileEndLine }} / {{ fileTotalLines }}
          </span>
          <button
            class="page-btn"
            title="下一页"
            aria-label="下一页"
            :disabled="!hasNextPage || loadingFile"
            @click="loadNextPage"
          >
            <svg
              viewBox="0 0 24 24"
              width="14"
              height="14"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <line x1="12" y1="5" x2="12" y2="19" />
              <polyline points="19 12 12 19 5 12" />
            </svg>
          </button>
        </div>
        <!-- 下载当前文件:文本(含 md)分页预览看不全,长文档要能整份取回。
             二进制隐藏此按钮——FileBinaryCard 自带大按钮,两个入口各持一份
             downloading 会状态打架(点一个另一个不禁用,还能并行下同一文件) -->
        <button
          v-if="!fileBinary"
          class="icon-btn file-download-btn"
          :disabled="!selectedFilePath || !selectedTaskId || fileDownloading"
          :title="fileDownloading ? '下载中…' : '下载该文件'"
          :aria-label="fileDownloading ? '下载中' : '下载该文件'"
          @click="downloadSelectedFile"
        >
          <span v-if="fileDownloading" class="spinner-sm" aria-hidden="true" />
          <svg
            v-else
            viewBox="0 0 24 24"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
        </button>
        <button
          class="icon-btn file-close-btn"
          title="隐藏文件查看面板"
          aria-label="隐藏文件查看面板"
          @click="hideFilePanel"
        >
          <svg
            viewBox="0 0 24 24"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
      <!-- 下载失败原因(会话过期/超上限/凭证路径),换文件即清 -->
      <div v-if="downloadError" class="file-panel-error" role="alert">
        {{ downloadError }}
      </div>
      <div class="file-content">
        <div v-if="loadingFile" class="file-loading">
          <span class="spinner-sm" /> 加载中...
        </div>
        <!-- 二进制文件:不渲染源码,给"是什么 + 多大 + 下载" -->
        <FileBinaryCard
          v-else-if="fileBinary"
          :task-id="selectedTaskId ?? ''"
          :path="selectedFilePath"
          :source="selectedFileSource"
          :size="fileBinarySize"
        />
        <FileContentViewer
          v-else
          ref="fileViewerRef"
          class="file-viewer"
          :content="fileContent"
          :filename="selectedFilePath"
          :start-line="fileStartLine"
          placeholder="(空文件)"
        />
      </div>
    </section>

    <!-- 任务项"更多操作"下拉菜单(三个点按钮触发) -->
    <Teleport to="body">
      <div
        v-if="openMenuTask"
        class="task-menu-backdrop"
        @click="closeTaskMenu"
        @contextmenu.prevent="closeTaskMenu"
      >
        <div
          class="task-menu"
          :style="{ top: `${menuPos.top}px`, right: `${menuPos.right}px` }"
          @click.stop
        >
          <button class="task-menu-item" @click="startEditTitle(openMenuTask)">
            <svg
              viewBox="0 0 24 24"
              width="12"
              height="12"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
            </svg>
            <span>修改标题</span>
          </button>
          <button class="task-menu-item task-menu-danger" @click="openDeleteDialog(openMenuTask)">
            <svg
              viewBox="0 0 24 24"
              width="12"
              height="12"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
            </svg>
            <span>删除任务</span>
          </button>
        </div>
      </div>
    </Teleport>

    <!-- 删除任务确认弹窗 -->
    <Teleport to="body">
      <Transition name="dialog-fade">
        <div
          v-if="deleteState.open && deleteState.task"
          class="title-dialog-mask"
          @click.self="closeDeleteDialog"
        >
          <div class="title-dialog-card title-dialog-danger" role="dialog" aria-modal="true">
            <header class="title-dialog-header">
              <div class="title-dialog-title-row">
                <span class="title-dialog-danger-icon" aria-hidden="true">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                    <line x1="12" y1="9" x2="12" y2="13" />
                    <line x1="12" y1="17" x2="12.01" y2="17" />
                  </svg>
                </span>
                <h3>删除任务</h3>
              </div>
              <button
                class="title-dialog-close"
                :disabled="deleteState.loading"
                aria-label="关闭"
                @click="closeDeleteDialog"
              >×</button>
            </header>
            <div class="title-dialog-body">
              <div class="title-dialog-warning">
                <p class="warning-title">此操作不可恢复</p>
                <p class="warning-desc">
                  将永久删除该任务及其所有对话记录和结果。
                  <template v-if="deleteTargetRunning">
                    <strong>任务正在运行中,删除会同时终止后台执行。</strong>
                  </template>
                </p>
                <p class="warning-target" :title="deleteState.task.title || deleteState.task.user_input">
                  {{ truncateInput(deleteState.task.title || deleteState.task.user_input, 60) }}
                </p>
              </div>
              <p v-if="deleteState.error" class="title-dialog-error">
                {{ deleteState.error }}
              </p>
            </div>
            <footer class="title-dialog-footer">
              <button
                class="title-btn title-btn-secondary"
                :disabled="deleteState.loading"
                @click="closeDeleteDialog"
              >取消</button>
              <button
                class="title-btn title-btn-danger"
                :disabled="deleteState.loading"
                @click="confirmDeleteTask"
              >
                <span v-if="deleteState.loading" class="title-btn-spinner" />
                {{ deleteState.loading ? '删除中...' : '确认删除' }}
              </button>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.workspace-container {
  display: flex;
  flex-shrink: 0;
  height: 100%;
  overflow: hidden;
}

/* ---- 侧栏 ---- */
.workspace-sidebar {
  position: relative; /* 供底部轻提示绝对定位 */
  flex-shrink: 0;
  width: 280px;
  border-right: 1px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 手机窄屏:侧栏改为左侧覆盖式抽屉,避免挤压主内容;
   定位基准为宿主页 .page-body(global.css 统一 position:relative) */
@media (max-width: 640px) {
  .workspace-sidebar {
    position: absolute;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 30;
    width: min(300px, 85vw);
    box-shadow: var(--shadow-xl);
  }
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
  gap: var(--space-2);
}

.sidebar-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  flex: 1;
}

.sidebar-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.icon-btn {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 13px;
  line-height: 1;
  transition: all var(--transition-fast);
}

.icon-btn:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.back-btn {
  flex-shrink: 0;
}

/* 搜索按钮激活态:高亮提示当前处于搜索模式 */
.icon-btn-active {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.icon-btn-active:hover:not(:disabled) {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

/* ---- 搜索输入框 ---- */
.sidebar-search {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  flex-shrink: 0;
}

.sidebar-search-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
}

.sidebar-search-input {
  flex: 1;
  min-width: 0;
  height: 24px;
  padding: 0 2px;
  font-size: var(--fs-sm);
  font-family: var(--font-sans);
  color: var(--color-text);
  background: transparent;
  border: none;
  outline: none;
}

.sidebar-search-input::placeholder {
  color: var(--color-text-muted);
}

.sidebar-search-clear {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  line-height: 1;
  border: none;
  background: transparent;
  color: var(--color-text-muted);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.sidebar-search-clear:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.sidebar-search-spinner {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.sidebar-search-spinner .spinner-sm {
  width: 10px;
  height: 10px;
}

/* ---- 侧栏状态 ---- */
.sidebar-status {
  padding: var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.sidebar-status-muted {
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
}

.status-hint {
  color: var(--color-text-muted);
  font-size: 10px;
}

/* ---- 重新克隆(工作区过期后显示,与做题页「重新拉取代码」同风格) ---- */
.restore-box {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.restore-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.restore-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.restore-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.restore-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-danger);
  word-break: break-all;
}

/* ---- 变更文件兜底列表(工作区不可用且非运行中时展示) ---- */
.changed-files-fallback {
  width: 100%;
  margin-top: var(--space-2);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* ---- 用户上传独立兜底区(与工作区可用性无关,重新克隆后仍常驻显示) ---- */
.uploads-panel {
  flex-shrink: 0;
  max-height: 45%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-top: var(--space-1);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--color-border);
}

.uploads-panel .changed-files-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 var(--space-1);
}

.changed-files-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  padding: 0 var(--space-2);
}

.changed-files-list {
  overflow-y: auto;
  margin-top: var(--space-1);
}

.changed-file-row {
  padding-left: var(--space-2);
}

/* ---- 用户上传兜底:已被 GC 清理的上传占位行 ---- */
.uploads-gone {
  opacity: 0.55;
  cursor: default;
}

.uploads-gone:hover {
  background: transparent;
}

.uploads-gone-tag {
  margin-left: auto;
  flex-shrink: 0;
  font-size: var(--fs-xs, 10px);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border, currentColor);
  border-radius: var(--radius-sm);
  padding: 0 4px;
  line-height: 1.4;
}

/* ---- 侧栏轻提示(工作区不可用时点击仓库文件行) ---- */
.sidebar-toast {
  position: absolute;
  bottom: var(--space-4);
  left: 50%;
  transform: translateX(-50%);
  padding: var(--space-2) var(--space-3);
  max-width: 90%;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  white-space: nowrap;
  z-index: 10;
}

/* ---- 任务列表 ---- */
.task-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-2);
}

.task-item {
  position: relative;
  display: flex;
  align-items: stretch;
  gap: 0;
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  /* 建立 stacking context,让 ::before 背景层位于内容之下但不穿透到侧栏背景 */
  isolation: isolate;
}

/* 圆角背景块(hover/active 共用,整行铺满,与技能管理文件树行风格一致) */
.task-item::before {
  content: '';
  position: absolute;
  inset: 0;
  background: transparent;
  border-radius: var(--radius-md);
  z-index: -1;
  transition: background var(--transition-fast);
  pointer-events: none;
}

.task-item:hover::before {
  background: var(--color-surface-alt);
}

/* ---- 正在查看的任务 active 态:圆角主色背景块 ---- */
.task-item-active::before,
.task-item-active:hover::before {
  background: var(--color-primary-light);
}

/* active 态:标题文字变主色 + 加粗(与文件树选中态一致) */
.task-item-active .task-input {
  color: var(--color-primary);
  font-weight: var(--fw-semibold);
}

.task-item-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.task-item-top {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.task-status-tag {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  border-radius: var(--radius-full);
  flex-shrink: 0;
}

.task-status-tag.badge-pending {
  background: var(--color-surface-alt);
  color: var(--color-text-secondary);
}
.task-status-tag.badge-running {
  background: var(--color-info-light);
  color: var(--color-info);
}
.task-status-tag.badge-paused {
  background: var(--color-warning-light);
  color: var(--color-warning);
}
.task-status-tag.badge-completed {
  background: var(--color-success-light);
  color: var(--color-success);
}
.task-status-tag.badge-failed {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

.task-time {
  font-size: 10px;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
}

.task-input {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.4;
  font-weight: var(--fw-medium);
}

/* 任务标题就地编辑输入框(替换 .task-input 文本) */
.task-title-edit {
  width: 100%;
  height: 24px;
  padding: 0 4px;
  margin: -1px -4px; /* 抵消 padding,与文本基线对齐 */
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  font-family: var(--font-sans);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-sm);
  outline: none;
  box-shadow: 0 0 0 2px var(--color-primary-light);
  /* 防止长文本撑开布局 */
  min-width: 0;
}

.task-title-edit::placeholder {
  color: var(--color-text-muted);
}

.task-title-edit:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
  opacity: 0.7;
}

/* 三个点"更多操作"按钮(查看工作区按钮左侧) */
.more-btn {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--color-text-muted);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  margin-left: var(--space-2);
}

.more-btn:hover:not(.active),
.more-btn.active {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.workspace-btn {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--color-text-muted);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  margin-left: 2px;
}

.workspace-btn:hover {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

/* ---- 文件树 ---- */
.file-tree {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-2);
}

.empty-tree {
  padding: var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  text-align: center;
}

.tree-truncated-hint {
  padding: var(--space-1) var(--space-3);
  margin-bottom: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-warning, #b45309);
  background: var(--color-warning-bg, rgba(245, 158, 11, 0.1));
  border-radius: var(--radius-sm);
  line-height: 1.5;
}

.tree-node {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  font-size: var(--fs-sm);
  color: var(--color-text);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
  white-space: nowrap;
  overflow: hidden;
  line-height: 1.6;
}

.tree-node:hover {
  background: var(--color-surface-alt);
}

.tree-selected {
  background: var(--color-primary-light) !important;
  color: var(--color-primary);
  font-weight: var(--fw-semibold);
}

.tree-selected .tree-icon {
  color: var(--color-primary);
}

.tree-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
}

/* 文件夹行:次级文字色 + muted 图标(hover 提亮),与技能管理一致 */
.tree-dir {
  color: var(--color-text-secondary);
}

.tree-dir .tree-icon {
  color: var(--color-text-muted);
}

.tree-dir:hover .tree-icon {
  color: var(--color-text);
}

.tree-chevron {
  flex-shrink: 0;
  color: var(--color-text-muted);
  transition: transform var(--transition-fast);
}

.tree-chevron.expanded {
  transform: rotate(90deg);
}

.tree-file .tree-icon {
  color: var(--color-text-muted);
}

.tree-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tree-loading {
  color: var(--color-text-muted);
  font-size: 10px;
}

.tree-dir .tree-name {
  font-weight: var(--fw-medium);
}

/* 文件被选中但查看面板被手动隐藏时,文件树中显示的恢复按钮 */
.tree-restore-btn {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  line-height: 1;
  border: none;
  background: transparent;
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.tree-restore-btn:hover {
  background: var(--color-surface-alt);
  color: var(--color-primary-dark, var(--color-primary));
}

/* ---- 文件查看面板 ---- */
.file-panel {
  width: 480px;
  flex-shrink: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
  border-right: 1px solid var(--color-border);
}

.file-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  flex-shrink: 0;
}

.file-path {
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.file-pagination {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}

.page-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.page-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.page-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.page-info {
  font-size: 10px;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  white-space: nowrap;
}

/* 文件查看面板头部的隐藏按钮 */
.file-close-btn {
  flex-shrink: 0;
}

.file-close-btn:hover {
  color: var(--color-danger);
}

.file-download-btn {
  flex-shrink: 0;
}

/* 下载失败提示条:紧贴头部下方,不侵入内容区 */
.file-panel-error {
  flex-shrink: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-danger);
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  word-break: break-all;
}

/* 内容容器:定位基准供加载浮层;查看器自带滚动 */
.file-content {
  flex: 1;
  min-height: 0;
  position: relative;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 只读 CodeMirror 查看器填充容器 */
.file-viewer {
  flex: 1;
  min-height: 0;
}

/* 加载浮层:不拆掉查看器(避免分页时反复重挂 CodeMirror) */
.file-loading {
  position: absolute;
  top: var(--space-2);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
}

.spinner-sm {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.sidebar-error {
  padding: var(--space-2) var(--space-3);
  font-size: 10px;
  color: var(--color-danger);
  background: var(--color-danger-light);
  border-top: 1px solid #fecaca;
  flex-shrink: 0;
}

/* ============================================================
 * 任务项"更多操作"下拉菜单 + 修改标题/删除任务弹窗
 * (元素经 Teleport 渲染到 body,样式仍属本组件 scoped 范围)
 * ============================================================ */

/* ---- 下拉菜单 ---- */
.task-menu-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1100;
  /* 透明背景:仅用于捕获外部点击以关闭菜单 */
  background: transparent;
}

.task-menu {
  position: fixed;
  z-index: 1101;
  /* 宽度自适应内容,避免短文本被 min-width 撑出右边空白 */
  width: max-content;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  padding: 3px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.task-menu-item {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-2);
  border: none;
  background: transparent;
  color: var(--color-text);
  font-size: var(--fs-xs);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast), color var(--transition-fast);
  text-align: left;
  white-space: nowrap;
}

.task-menu-item:hover {
  background: var(--color-surface-alt);
}

.task-menu-item svg {
  flex-shrink: 0;
  color: var(--color-text-secondary);
}

.task-menu-danger {
  color: var(--color-danger);
}

.task-menu-danger:hover {
  background: var(--color-danger-light);
}

.task-menu-danger svg {
  color: var(--color-danger);
}

/* ---- 弹窗通用(mask + card) ---- */
.title-dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1200;
  padding: var(--space-4);
}

.title-dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 440px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.title-dialog-danger {
  border: 1px solid var(--color-danger);
}

.title-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.title-dialog-title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.title-dialog-danger-icon {
  display: inline-flex;
  color: var(--color-danger);
}

.title-dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.title-dialog-close {
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

.title-dialog-close:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.title-dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.title-dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
}

.title-dialog-error {
  margin: var(--space-3) 0 0;
  font-size: var(--fs-sm);
  color: var(--color-danger);
}

/* ---- 删除确认:警告横幅 ---- */
.title-dialog-warning {
  background: var(--color-danger-light);
  border: 1px solid #fecaca;
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
}

.title-dialog-warning .warning-title {
  font-weight: var(--fw-semibold);
  color: var(--color-danger);
  margin: 0 0 var(--space-1);
  font-size: var(--fs-sm);
}

.title-dialog-warning .warning-desc {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0 0 var(--space-2);
  line-height: var(--lh-relaxed);
}

.title-dialog-warning .warning-target {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
}

/* ---- 弹窗 footer ---- */
.title-dialog-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.title-btn {
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

.title-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.title-btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.title-btn-secondary:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.title-btn-danger {
  background: var(--color-danger);
  color: var(--color-text-inverse);
}

.title-btn-danger:hover:not(:disabled) {
  background: var(--color-danger-hover);
}

.title-btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: title-btn-spin 0.8s linear infinite;
}

@keyframes title-btn-spin {
  to { transform: rotate(360deg); }
}

/* ---- 弹窗过渡 ---- */
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
