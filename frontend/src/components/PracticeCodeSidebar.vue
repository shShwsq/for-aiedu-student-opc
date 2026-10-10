<script setup lang="ts">
/**
 * 做题页右侧「源码查阅」栏
 *
 * 布局:文件树在左、文件内容在右(横向 IDE 分栏),栏左缘可拖拽调宽。
 * 按题目的 source_task_id 打开对应任务的工作区,浏览文件树与文件内容,
 * 供用户在答题时阅读真实源码。工作区过期清理后展示「重新拉取代码」按钮
 * (POST .../workspace/restore 发起后台克隆,再轮询 .../restore/status 看进度)。
 *
 * 树加载策略:优先整树快照(/workspace/tree);快照截断时退回逐级懒加载
 * (/workspace/files)。文件内容复用 /workspace/file(原始文本 + 分页),
 * 交给 FileContentViewer 以只读 CodeMirror 渲染(IDE 行号 + 语法高亮,
 * Markdown 文件可切「预览」)。二进制文件(docx/pdf/图片)后端不回内容,
 * 改渲染 FileBinaryCard(下载原件到本地看)。分页预览看不全的长文档由头部
 * 的下载按钮整份取回(仅文本文件;二进制用卡片里的大按钮,不重复设入口)。
 *
 * 定位:父组件传入 locateFile/locateLine(来自当前题的 source_file/source_lines),
 * 变化时自动展开对应目录、打开文件并滚动高亮。
 */
import { computed, nextTick, ref, watch } from 'vue'

import FileBinaryCard from './FileBinaryCard.vue'
import FileContentViewer from './FileContentViewer.vue'
import {
  getWorkspaceInfo,
  getWorkspaceTree,
  isWorkspaceExpiredError,
  listWorkspaceFiles,
  readWorkspaceFile,
} from '@/api/workspace'
import { useFileDownload } from '@/composables/useFileDownload'
import { useResizableSidebar } from '@/composables/useResizableSidebar'
import { useWorkspaceRestore } from '@/composables/useWorkspaceRestore'
import { extractErrorMessage } from '@/utils/error'
import { isLikelyBinaryPath } from '@/utils/fileKind'
import { toWorkspaceRelative } from '@/utils/workspacePath'

const props = defineProps<{
  /** 当前要浏览工作区的来源任务 id(null 时展示空态) */
  taskId: string | null
  /** 可切换的来源任务清单(一局混合多任务题目时展示下拉) */
  taskOptions: { id: string; label: string }[]
  /** 自动定位的文件(仓库内相对路径) */
  locateFile: string | null
  /** 自动定位的起始行号(1-based) */
  locateLine: number | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'switch-task', taskId: string): void
}>()

// ============================================================
// 栏宽:横向分栏需足够宽度,左缘拖拽调宽(窄屏改为覆盖抽屉,不启用)
// 指针跟手 / 钳位 / 窄屏判定交给 useResizableSidebar(与任务详情右侧栏共用一套);
// 这里不持久化 —— 每次进入按默认宽度起算
// ============================================================
const MIN_WIDTH = 480
const MAX_WIDTH = 1200
const DEFAULT_WIDTH = 760
const {
  resizing,
  /** 窄屏(<=640px):栏内回退为纵向堆叠,宽度交给 CSS,不应用内联宽度 */
  isNarrow,
  inlineWidth: sidebarStyle,
  startResize: onResizeStart,
  resetWidth: resetSidebarWidth,
  onResizeKeydown,
} = useResizableSidebar({
  min: MIN_WIDTH,
  max: MAX_WIDTH,
  defaultWidth: DEFAULT_WIDTH,
  // 与下面 @media (max-width: 640px) 的覆盖抽屉态一致
  narrowMax: 640,
})

// ============================================================
// 工作区可用性
// ============================================================
const loading = ref(false)
const available = ref(false)
const unavailableReason = ref('')
/** 后端返回的工作区根绝对路径(把模型给的绝对路径剥成仓库相对路径用) */
const repoPath = ref('')
const {
  restoring, restoreError, restorePercent, restoreMessage,
  run: runRestore, reset: resetRestore,
} = useWorkspaceRestore()

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

const treeRoot = ref<TreeNode>({
  name: '',
  path: '',
  type: 'dir',
  expanded: true,
  loaded: false,
  loading: false,
  children: [],
})

/** 树是否处于懒加载模式(整树快照截断时退回) */
const lazyMode = ref(false)

function makeNode(name: string, path: string, type: 'dir' | 'file'): TreeNode {
  return { name, path, type, expanded: false, loaded: false, loading: false, children: [] }
}

function sortChildren(node: TreeNode): void {
  node.children.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

/** 从整树快照的扁平条目构建嵌套树 */
function buildTreeFromSnapshot(entries: { path: string; type: 'file' | 'dir' }[]): void {
  const root: TreeNode = {
    name: '', path: '', type: 'dir', expanded: true, loaded: true, loading: false, children: [],
  }
  const dirMap = new Map<string, TreeNode>([['', root]])
  const sorted = [...entries].sort((a, b) => a.path.localeCompare(b.path))
  for (const entry of sorted) {
    const parts = entry.path.split('/').filter(Boolean)
    if (!parts.length) continue
    // 逐级补齐祖先目录(快照可能先给文件后给目录)
    let parent = root
    let acc = ''
    for (let i = 0; i < parts.length - 1; i += 1) {
      acc = acc ? `${acc}/${parts[i]}` : parts[i]
      let dir = dirMap.get(acc)
      if (!dir) {
        dir = makeNode(parts[i], acc, 'dir')
        dirMap.set(acc, dir)
        parent.children.push(dir)
      }
      parent = dir
    }
    const leafName = parts[parts.length - 1]
    if (entry.type === 'dir') {
      const accPath = acc ? `${acc}/${leafName}` : leafName
      if (!dirMap.has(accPath)) {
        dirMap.set(accPath, makeNode(leafName, accPath, 'dir'))
        parent.children.push(dirMap.get(accPath)!)
      }
    } else {
      parent.children.push(makeNode(leafName, entry.path, 'file'))
    }
  }
  const stack = [root]
  while (stack.length) {
    const node = stack.pop()!
    sortChildren(node)
    // 快照已填充子节点,标记 loaded 避免展开时重复懒加载
    if (node.type === 'dir') node.loaded = true
    stack.push(...node.children)
  }
  treeRoot.value = root
}

/** 工作区已过期(HTTP 410):切到不可用态,让「重新拉取代码」按钮出现
 *
 * 沙箱容器被 Server 回收后后端会回 410(并已丢弃会话)。本组件的恢复入口只在
 * !available 时渲染,不接这一步就只会卡在"看着可用、什么也加载不出来"。
 * 返回 true 表示已按过期处理,调用方不该再走常规降级重试。
 */
function markExpiredUnavailable(err: unknown): boolean {
  if (!isWorkspaceExpiredError(err)) return false
  available.value = false
  unavailableReason.value = extractErrorMessage(err)
  return true
}

/** 懒加载某目录的子条目(单层) */
async function loadChildren(node: TreeNode): Promise<void> {
  if (!props.taskId || node.loaded || node.loading) return
  node.loading = true
  try {
    const res = await listWorkspaceFiles(props.taskId, node.path)
    node.children = res.entries.map((e) =>
      makeNode(e.name, node.path ? `${node.path}/${e.name}` : e.name, e.type),
    )
    sortChildren(node)
    node.loaded = true
    node.expanded = true
  } catch (e) {
    // 加载失败保持收起,允许重试;工作区已过期则改走重新拉取入口
    markExpiredUnavailable(e)
  } finally {
    node.loading = false
  }
}

/** 可见节点扁平列表(递归展开,支持任意深度) */
interface FlatNode {
  node: TreeNode
  depth: number
}

const visibleNodes = computed<FlatNode[]>(() => {
  const out: FlatNode[] = []
  const walk = (node: TreeNode, depth: number): void => {
    for (const child of node.children) {
      out.push({ node: child, depth })
      if (child.type === 'dir' && child.expanded) walk(child, depth + 1)
    }
  }
  walk(treeRoot.value, 0)
  return out
})

async function toggleNode(node: TreeNode): Promise<void> {
  if (node.type === 'file') {
    openFile(node.path, null, null)
    return
  }
  if (node.expanded) {
    node.expanded = false
    return
  }
  if (lazyMode.value || !node.loaded) {
    await loadChildren(node)
  } else {
    node.expanded = true
  }
}

async function loadTree(refresh = false): Promise<void> {
  if (!props.taskId) return
  lazyMode.value = false
  treeRoot.value = {
    name: '', path: '', type: 'dir', expanded: true, loaded: false, loading: false, children: [],
  }
  try {
    const res = await getWorkspaceTree(props.taskId, refresh)
    if (res.truncated) {
      // 快照截断:退回根目录逐级懒加载
      lazyMode.value = true
      await loadChildren(treeRoot.value)
      treeRoot.value.loaded = true
    } else {
      buildTreeFromSnapshot(res.entries)
    }
  } catch (e) {
    if (markExpiredUnavailable(e)) return
    // 快照失败(如 session 刚被清理):退回懒加载
    lazyMode.value = true
    await loadChildren(treeRoot.value)
    treeRoot.value.loaded = true
  }
}

// ============================================================
// 文件内容(行号 + 分页)
// ============================================================
const PAGE_LINES = 500

const selectedFile = ref<string | null>(null)
const fileContent = ref('')
const fileStartLine = ref(0)
const fileTotalLines = ref(0)
const fileTruncated = ref(false)
const loadingFile = ref(false)
/** 当前文件是否二进制(后缀预判或后端 binary 标记):真则改渲染下载卡片 */
const fileBinary = ref(false)
/** 二进制文件字节数(卡片展示用) */
const fileBinarySize = ref(0)
/** 高亮行号区间(题目 source_lines 定位用) */
const highlightStart = ref<number | null>(null)
const highlightEnd = ref<number | null>(null)

/** FileContentViewer 暴露的定位接口(局部类型,避免依赖 SFC 实例类型推导) */
interface FileContentViewerHandle {
  focusRange: (start: number | null, end: number | null) => void
  clearHighlight: () => void
}
const fileViewerRef = ref<FileContentViewerHandle | null>(null)

// ---- 下载当前文件 ----
// 文本文件(含 md)原先只有二进制卡片带下载按钮,而题目材料常是长文档:分页
// 预览看不全,得能整份取回。动作与 FileBinaryCard 共用 useFileDownload。
// 二进制隐藏头部按钮(卡片自带大按钮,两个入口各持一份 downloading 会状态打架)。
// 本栏只浏览工作区(无上传回退树),故 source 恒为 workspace
const {
  downloading: fileDownloading,
  downloadError,
  run: runFileDownload,
  clearError: clearFileDownloadError,
} = useFileDownload()

const fileLineCount = computed(() => (fileContent.value ? fileContent.value.split('\n').length : 0))
const pageEndLine = computed(() => fileStartLine.value + fileLineCount.value - 1)
const fileOffset = computed(() => fileStartLine.value)

async function openFile(
  path: string,
  targetLine: number | null,
  endLine: number | null,
): Promise<void> {
  if (!props.taskId) return
  selectedFile.value = path
  clearFileDownloadError() // 上个文件的下载失败文案不跟着串过来
  highlightStart.value = targetLine
  highlightEnd.value = endLine ?? targetLine
  // 后缀已知二进制:不发内容请求,直接给下载卡片(题目材料极少是二进制,
  // 真遇到时也比把乱码当源码展示好)
  if (isLikelyBinaryPath(path)) {
    fileBinary.value = true
    fileBinarySize.value = 0
    fileContent.value = ''
    fileStartLine.value = 0
    fileTotalLines.value = 0
    fileTruncated.value = false
    return
  }
  loadingFile.value = true
  const offset = targetLine ? Math.max(1, targetLine - 20) : 1
  try {
    const res = await readWorkspaceFile(props.taskId, path, offset, PAGE_LINES)
    fileBinary.value = res.binary === true
    fileBinarySize.value = res.binary ? res.size || 0 : 0
    fileContent.value = res.content
    fileStartLine.value = res.start_line
    fileTotalLines.value = res.total_lines
    fileTruncated.value = res.truncated
  } catch (err) {
    fileBinary.value = false
    fileBinarySize.value = 0
    fileContent.value = `读取失败: ${extractErrorMessage(err)}`
    fileStartLine.value = 0
    fileTotalLines.value = 0
  } finally {
    loadingFile.value = false
    // 成功与失败都重新定位:失败时定位行落在当前页外,applyFocus 会自动清高亮
    await nextTick()
    applyFocus()
  }
}

/** 翻页(上一/下一页,按 PAGE_LINES 步进) */
async function pageFile(delta: number): Promise<void> {
  if (!props.taskId || !selectedFile.value) return
  const next = fileStartLine.value + delta * PAGE_LINES
  if (next < 1 || next > fileTotalLines.value) return
  loadingFile.value = true
  try {
    const res = await readWorkspaceFile(props.taskId, selectedFile.value, next, PAGE_LINES)
    fileBinary.value = res.binary === true
    fileBinarySize.value = res.binary ? res.size || 0 : 0
    fileContent.value = res.content
    fileStartLine.value = res.start_line
    fileTotalLines.value = res.total_lines
    fileTruncated.value = res.truncated
    await nextTick()
    applyFocus()
  } catch {
    // 翻页失败保持当前内容
  } finally {
    loadingFile.value = false
  }
}

/** 下载当前选中的文件原文(容器被回收后后端回 410 → 重走 init 切到不可用态) */
async function downloadSelectedFile(): Promise<void> {
  await runFileDownload(
    props.taskId ?? '',
    selectedFile.value ?? '',
    'workspace',
    // 工作区已没:重新拉一次可用性(拿到后端的不可用文案 + 「重新拉取代码」入口),
    // 不在前端另写一份过期文案:那份只在后端一处,写两处必然漂
    () => init(false),
  )
}

/** 高亮当前题目定位行(若落在本页)并滚动到中间;否则清空高亮 */
function applyFocus(): void {
  const v = fileViewerRef.value
  if (!v) return
  const s = highlightStart.value
  if (s == null) {
    v.clearHighlight()
    return
  }
  const pageStart = fileStartLine.value
  const pageEnd = pageStart + fileLineCount.value - 1
  if (s >= pageStart && s <= pageEnd) v.focusRange(s, highlightEnd.value)
  else v.clearHighlight()
}

// ============================================================
// 定位(题目切换时自动展开目录 + 打开文件)
// ============================================================
async function locateInTree(path: string): Promise<void> {
  // 展开各级祖先目录(未加载的层级先懒加载)
  const parts = path.split('/').filter(Boolean)
  let node = treeRoot.value
  for (let i = 0; i < parts.length - 1; i += 1) {
    if (!node.loaded) {
      await loadChildren(node)
    }
    node.expanded = true
    const next = node.children.find((c) => c.type === 'dir' && c.name === parts[i])
    if (!next) return
    node = next
  }
}

// ============================================================
// 初始化与任务/题目切换
// ============================================================
async function init(refreshTree = false): Promise<void> {
  available.value = false
  unavailableReason.value = ''
  repoPath.value = ''
  resetRestore()
  clearFileDownloadError()
  selectedFile.value = null
  fileContent.value = ''
  highlightStart.value = null
  highlightEnd.value = null
  if (!props.taskId) return
  loading.value = true
  try {
    const info = await getWorkspaceInfo(props.taskId)
    available.value = info.available
    repoPath.value = info.repo_path ?? ''
    unavailableReason.value = info.available ? '' : (info.reason || '工作区不可用')
    if (info.available) {
      await loadTree(refreshTree)
      await applyLocate()
    }
  } catch (err) {
    available.value = false
    unavailableReason.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

/** 按当前 locateFile/locateLine 展开目录并打开文件
 *
 * locateFile 是模型输出的题目字段(source_file),常照抄它读到的工作区绝对路径
 * (local 模式下还是 Windows 反斜杠路径),不归一直接按段名在树里定位会落空。
 */
async function applyLocate(): Promise<void> {
  if (!props.locateFile) return
  const relPath = toWorkspaceRelative(props.locateFile, repoPath.value)
  await locateInTree(relPath)
  await openFile(relPath, props.locateLine, props.locateLine)
}

/** 重新拉取代码(工作区过期后用户显式触发):后台 job + 轮询进度
 *
 * 克隆分钟级,不能挂住请求同步等(旧版被 axios 30s 超时误报成"网络错误")。
 */
async function handleRestore(): Promise<void> {
  const taskId = props.taskId
  if (!taskId) return
  // 刚克隆完必须绕过整树快照缓存(refresh=true):否则 30s 内仍会拿到克隆前的空态快照,
  // 用户看到的就是"克隆完成但目录是空的"
  await runRestore(taskId, () => init(true))
}

// 任务切换:重新初始化
watch(() => props.taskId, () => {
  void init()
}, { immediate: true })

// 同任务内切题:仅重新定位(不重载树)
watch(
  () => [props.locateFile, props.locateLine],
  () => {
    if (available.value && props.locateFile) void applyLocate()
  },
)
</script>

<template>
  <aside
    class="code-sidebar"
    :class="{ 'cs-resizing': resizing }"
    :style="sidebarStyle"
    aria-label="源码查阅"
  >
    <!-- 桌面态左缘拖拽调宽手柄(窄屏隐藏) -->
    <div
      v-if="!isNarrow"
      class="cs-resize-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="调整源码查阅栏宽度"
      tabindex="0"
      title="拖动调整宽度,双击复位"
      @pointerdown="onResizeStart"
      @dblclick="resetSidebarWidth"
      @keydown="onResizeKeydown"
    />

    <div class="cs-head">
      <h3 class="cs-title">源码查阅</h3>
      <select
        v-if="taskOptions.length > 1"
        class="cs-task-select"
        title="切换题目来源任务"
        :value="taskId ?? ''"
        @change="emit('switch-task', ($event.target as HTMLSelectElement).value)"
      >
        <option v-for="t in taskOptions" :key="t.id" :value="t.id">
          {{ t.label }}
        </option>
      </select>
      <button class="cs-close-btn" title="收起源码查阅" @click="emit('close')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>

    <!-- 加载中 -->
    <div v-if="loading" class="cs-placeholder">
      <span class="cs-spinner" /> 加载工作区...
    </div>

    <!-- 不可用:展示原因 + 一键重新拉取 -->
    <div v-else-if="taskId && !available" class="cs-unavailable">
      <p class="cs-unavailable-text">
        {{ unavailableReason || '工作区不可用' }}
      </p>
      <p class="cs-unavailable-hint">
        沙箱工作区只保留一段时间,过期后仓库代码可重新拉取(不影响仓库本身)。
      </p>
      <button class="cs-restore-btn" :disabled="restoring" @click="handleRestore">
        {{ restoring
          ? (restorePercent > 0 ? `拉取中... ${restorePercent}%` : '正在发起克隆...')
          : '重新拉取代码' }}
      </button>
      <p v-if="restoring && restoreMessage" class="cs-unavailable-hint">{{ restoreMessage }}</p>
      <p v-if="restoreError" class="cs-restore-error">{{ restoreError }}</p>
    </div>

    <!-- 无来源任务(老题目) -->
    <div v-else-if="!taskId" class="cs-placeholder">
      该题目无来源任务信息,无法浏览代码
    </div>

    <!-- 可用:左树右内容(横向 IDE 分栏) -->
    <div v-else class="cs-body">
      <div class="cs-tree" role="tree" aria-label="工作区文件树">
        <template v-if="visibleNodes.length">
          <div
            v-for="item in visibleNodes"
            :key="item.node.path"
            class="cs-tree-node"
            role="treeitem"
            :aria-expanded="item.node.type === 'dir' ? item.node.expanded : undefined"
            :class="[
              item.node.type === 'dir' ? 'tree-dir' : 'tree-file',
              { 'cs-node-active': selectedFile === item.node.path },
            ]"
            :style="{ paddingLeft: `${item.depth * 14 + 8}px` }"
            @click="toggleNode(item.node)"
          >
            <span class="tree-icon">
              <!-- 文件夹:chevron(展开旋转 90°)+ 文件夹图标,与历史任务左侧栏一致 -->
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
          </div>
        </template>
        <div v-else-if="treeRoot.loading" class="cs-placeholder">
          <span class="cs-spinner" /> 加载文件树...
        </div>
        <div v-else class="cs-placeholder">仓库为空</div>
      </div>

      <!-- 文件内容区(只读 CodeMirror + Markdown 预览) -->
      <div class="cs-file">
        <template v-if="selectedFile">
          <div class="cs-file-head">
            <span class="cs-file-path" :title="selectedFile">
              {{ selectedFile }}
              <span v-if="loadingFile" class="cs-file-loading">读取中…</span>
            </span>
            <div v-if="!fileBinary" class="cs-file-pager">
              <button
                class="cs-pager-btn"
                :disabled="fileOffset <= 1 || loadingFile"
                title="上一页"
                @click="pageFile(-1)"
              >↑</button>
              <span class="cs-pager-info">
                {{ fileStartLine }}-{{ pageEndLine }}/{{ fileTotalLines }}
              </span>
              <button
                class="cs-pager-btn"
                :disabled="pageEndLine >= fileTotalLines || loadingFile"
                title="下一页"
                @click="pageFile(1)"
              >↓</button>
            </div>
            <!-- 下载当前文件:文本(含 md)分页预览看不全,得能整份取回。
                 二进制隐藏此按钮——FileBinaryCard 自带大按钮,两个入口各持一份
                 downloading 会状态打架(点一个另一个不禁用,还能并行下同一文件) -->
            <button
              v-if="!fileBinary"
              class="cs-file-dl-btn"
              :disabled="!taskId || !selectedFile || fileDownloading"
              :title="fileDownloading ? '下载中…' : '下载该文件'"
              :aria-label="fileDownloading ? '下载中' : '下载该文件'"
              @click="downloadSelectedFile"
            >
              <span v-if="fileDownloading" class="cs-spinner" aria-hidden="true" />
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
                aria-hidden="true"
              >
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            </button>
          </div>
          <!-- 下载失败原因(会话过期/超上限/凭证路径),换文件即清 -->
          <p v-if="downloadError" class="cs-file-error" role="alert">{{ downloadError }}</p>
          <FileBinaryCard
            v-if="fileBinary"
            :task-id="taskId ?? ''"
            :path="selectedFile"
            source="workspace"
            :size="fileBinarySize"
          />
          <FileContentViewer
            v-else
            ref="fileViewerRef"
            class="cs-viewer"
            :content="fileContent"
            :filename="selectedFile"
            :start-line="fileStartLine"
          />
        </template>
        <div v-else class="cs-placeholder cs-file-empty">
          点击左侧文件树查看源码<template v-if="locateFile">(题目引用:{{ locateFile }})</template>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.code-sidebar {
  position: relative;
  flex-shrink: 0;
  width: 760px;
  border-left: 1px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 拖拽调宽时禁用文本选中,避免拖动选到正文 */
.code-sidebar.cs-resizing {
  user-select: none;
  cursor: col-resize;
}

/* 左缘拖拽手柄(桌面态):悬停显主色 */
.cs-resize-handle {
  position: absolute;
  top: 0;
  left: 0;
  bottom: 0;
  width: 5px;
  z-index: 6;
  cursor: col-resize;
  /* 触屏:阻止浏览器把 pointermove 当滚动手势接管(641px+ 平板/触屏本仍可见手柄) */
  touch-action: none;
  background: transparent;
  transition: background var(--transition-fast);
}

.cs-resize-handle:hover,
.cs-resize-handle:focus-visible {
  background: var(--color-primary-light);
}

/* 手机窄屏:代码侧栏改为右侧覆盖式抽屉(定位基准为宿主 .page-body);
   横向分栏放不下,回退为纵向堆叠 */
@media (max-width: 640px) {
  .code-sidebar {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    width: min(420px, 92vw);
    box-shadow: var(--shadow-xl);
  }

  .cs-resize-handle {
    display: none;
  }

  .cs-body {
    flex-direction: column;
  }

  .cs-tree {
    width: auto;
    max-height: 42%;
    border-right: none;
    border-bottom: 1px solid var(--color-border);
  }
}

.cs-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.cs-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  white-space: nowrap;
}

.cs-task-select {
  flex: 1;
  min-width: 0;
  padding: 2px var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.cs-close-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.cs-close-btn:hover {
  color: var(--color-text);
  background: var(--color-bg-secondary);
}

.cs-placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.cs-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: cs-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes cs-spin {
  to { transform: rotate(360deg); }
}

/* ---- 不可用态 ---- */
.cs-unavailable {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.cs-unavailable-text {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-text);
  line-height: var(--lh-relaxed);
}

.cs-unavailable-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.cs-restore-btn {
  align-self: flex-start;
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

.cs-restore-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.cs-restore-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.cs-restore-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- 横向分栏容器:左树右内容 ---- */
.cs-body {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
}

/* ---- 文件树(左列):样式对齐历史任务左侧栏 WorkspaceSidebar ---- */
.cs-tree {
  flex-shrink: 0;
  width: 220px;
  overflow-y: auto;
  border-right: 1px solid var(--color-border);
  padding: var(--space-2);
}

.cs-tree-node {
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
  user-select: none;
}

.cs-tree-node:hover {
  background: var(--color-surface-alt);
}

.tree-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
}

/* 文件夹行:次级文字色 + muted 图标(hover 提亮) */
.tree-dir {
  color: var(--color-text-secondary);
}

.tree-dir .tree-icon {
  color: var(--color-text-muted);
}

.tree-dir:hover .tree-icon {
  color: var(--color-text);
}

.tree-dir .tree-name {
  font-weight: var(--fw-medium);
}

.tree-file .tree-icon {
  color: var(--color-text-muted);
}

.tree-chevron {
  flex-shrink: 0;
  color: var(--color-text-muted);
  transition: transform var(--transition-fast);
}

.tree-chevron.expanded {
  transform: rotate(90deg);
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

/* 选中行(当前打开的文件):置于末尾以保证主色优先级 */
.cs-node-active {
  background: var(--color-primary-light) !important;
  color: var(--color-primary);
  font-weight: var(--fw-semibold);
}

.cs-node-active .tree-icon {
  color: var(--color-primary);
}

/* ---- 文件内容(右列) ---- */
.cs-file {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.cs-file-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
}

.cs-file-path {
  flex: 1;
  min-width: 0;
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cs-file-loading {
  margin-left: var(--space-2);
  font-size: var(--fs-xs);
  font-family: var(--font-sans);
  color: var(--color-text-muted);
}

.cs-file-pager {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex-shrink: 0;
}

.cs-pager-btn {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.cs-pager-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.cs-pager-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 下载按钮:与翻页按钮同尺寸同描边,保持文件栏头部一条水平线 */
.cs-file-dl-btn {
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  flex-shrink: 0;
  transition: all var(--transition-fast);
}

.cs-file-dl-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.cs-file-dl-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 下载失败提示条:紧贴头部下方,不侵入内容区 */
.cs-file-error {
  flex-shrink: 0;
  margin: 0;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-danger);
  border-bottom: 1px solid var(--color-border);
  word-break: break-all;
}

.cs-pager-info {
  font-size: 11px;
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.cs-file-empty {
  flex: 1;
}

/* FileContentViewer 填充右列剩余高度 */
.cs-viewer {
  flex: 1;
  min-height: 0;
}
</style>
