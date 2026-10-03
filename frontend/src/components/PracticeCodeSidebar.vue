<script setup lang="ts">
/**
 * 做题页右侧「源码查阅」栏
 *
 * 按题目的 source_task_id 打开对应任务的工作区,浏览文件树与文件内容,
 * 供用户在答题时阅读真实源码。工作区过期清理后展示「重新拉取代码」按钮
 * (POST .../workspace/restore 发起后台克隆,再轮询 .../restore/status 看进度)。
 *
 * 树加载策略:优先整树快照(/workspace/tree);快照截断时退回逐级懒加载
 * (/workspace/files)。文件内容复用 /workspace/file(原始文本 + 分页)。
 *
 * 定位:父组件传入 locateFile/locateLine(来自当前题的 source_file/source_lines),
 * 变化时自动展开对应目录、打开文件并滚动高亮。
 */
import { computed, nextTick, ref, watch } from 'vue'

import {
  getWorkspaceInfo,
  getWorkspaceTree,
  listWorkspaceFiles,
  readWorkspaceFile,
} from '@/api/workspace'
import { useWorkspaceRestore } from '@/composables/useWorkspaceRestore'
import { extractErrorMessage } from '@/utils/error'
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
  } catch {
    // 加载失败保持收起,允许重试
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

async function loadTree(): Promise<void> {
  if (!props.taskId) return
  lazyMode.value = false
  treeRoot.value = {
    name: '', path: '', type: 'dir', expanded: true, loaded: false, loading: false, children: [],
  }
  try {
    const res = await getWorkspaceTree(props.taskId)
    if (res.truncated) {
      // 快照截断:退回根目录逐级懒加载
      lazyMode.value = true
      await loadChildren(treeRoot.value)
      treeRoot.value.loaded = true
    } else {
      buildTreeFromSnapshot(res.entries)
    }
  } catch {
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
/** 高亮行号区间(题目 source_lines 定位用) */
const highlightStart = ref<number | null>(null)
const highlightEnd = ref<number | null>(null)
const fileContentEl = ref<HTMLElement | null>(null)

const fileLines = computed(() => fileContent.value.split('\n'))
const fileOffset = computed(() => fileStartLine.value)

async function openFile(
  path: string,
  targetLine: number | null,
  endLine: number | null,
): Promise<void> {
  if (!props.taskId) return
  loadingFile.value = true
  selectedFile.value = path
  highlightStart.value = targetLine
  highlightEnd.value = endLine ?? targetLine
  const offset = targetLine ? Math.max(1, targetLine - 20) : 1
  try {
    const res = await readWorkspaceFile(props.taskId, path, offset, PAGE_LINES)
    fileContent.value = res.content
    fileStartLine.value = res.start_line
    fileTotalLines.value = res.total_lines
    fileTruncated.value = res.truncated
    await nextTick()
    scrollToHighlight(targetLine)
  } catch (err) {
    fileContent.value = `读取失败: ${extractErrorMessage(err)}`
    fileStartLine.value = 0
    fileTotalLines.value = 0
  } finally {
    loadingFile.value = false
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
    fileContent.value = res.content
    fileStartLine.value = res.start_line
    fileTotalLines.value = res.total_lines
    fileTruncated.value = res.truncated
  } catch {
    // 翻页失败保持当前内容
  } finally {
    loadingFile.value = false
  }
}

function isHighlighted(lineNo: number): boolean {
  if (highlightStart.value === null) return false
  const end = highlightEnd.value ?? highlightStart.value
  return lineNo >= highlightStart.value && lineNo <= end
}

function scrollToHighlight(line: number | null): void {
  if (!line || !fileContentEl.value) return
  const idx = line - fileStartLine.value
  const target = fileContentEl.value.querySelector<HTMLElement>(`[data-line-idx="${Math.max(0, idx)}"]`)
  if (target) {
    target.scrollIntoView({ block: 'center' })
  } else if (line > fileStartLine.value + PAGE_LINES) {
    // 目标行不在当前页:加载目标行所在页
    openFile(selectedFile.value!, line, highlightEnd.value)
  }
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
async function init(): Promise<void> {
  available.value = false
  unavailableReason.value = ''
  repoPath.value = ''
  resetRestore()
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
      await loadTree()
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
  await runRestore(taskId, init)
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
  <aside class="code-sidebar" aria-label="源码查阅">
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
        沙箱默认保留 1 小时,过期后可重新拉取仓库代码。
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

    <!-- 可用:文件树 + 文件内容 -->
    <template v-else>
      <div class="cs-tree" role="tree" aria-label="工作区文件树">
        <template v-if="visibleNodes.length">
          <div
            v-for="item in visibleNodes"
            :key="item.node.path"
            class="cs-tree-node"
            role="treeitem"
            :aria-expanded="item.node.type === 'dir' ? item.node.expanded : undefined"
            :class="{ 'cs-node-active': selectedFile === item.node.path }"
            :style="{ paddingLeft: `${8 + item.depth * 14}px` }"
            @click="toggleNode(item.node)"
          >
            <span v-if="item.node.type === 'dir'" class="cs-node-arrow">
              {{ item.node.loading ? '…' : item.node.expanded ? '▾' : '▸' }}
            </span>
            <span v-else class="cs-node-arrow cs-node-arrow-empty" />
            <span class="cs-node-name">{{ item.node.name }}</span>
          </div>
        </template>
        <div v-else-if="treeRoot.loading" class="cs-placeholder">
          <span class="cs-spinner" /> 加载文件树...
        </div>
        <div v-else class="cs-placeholder">仓库为空</div>
      </div>

      <!-- 文件内容区 -->
      <div class="cs-file">
        <template v-if="selectedFile">
          <div class="cs-file-head">
            <span class="cs-file-path" :title="selectedFile">{{ selectedFile }}</span>
            <div class="cs-file-pager">
              <button
                class="cs-pager-btn"
                :disabled="fileOffset <= 1"
                title="上一页"
                @click="pageFile(-1)"
              >↑</button>
              <span class="cs-pager-info">
                {{ fileStartLine }}-{{ fileStartLine + fileLines.length - 1 }}/{{ fileTotalLines }}
              </span>
              <button
                class="cs-pager-btn"
                :disabled="fileStartLine + fileLines.length - 1 >= fileTotalLines"
                title="下一页"
                @click="pageFile(1)"
              >↓</button>
            </div>
          </div>
          <div ref="fileContentEl" class="cs-file-body">
            <div v-if="loadingFile" class="cs-placeholder">
              <span class="cs-spinner" /> 读取中...
            </div>
            <template v-else>
              <div
                v-for="(line, idx) in fileLines"
                :key="idx"
                class="cs-line"
                :class="{ 'cs-line-highlight': isHighlighted(fileStartLine + idx) }"
                :data-line-idx="idx"
              >
                <span class="cs-line-no">{{ fileStartLine + idx }}</span>
                <span class="cs-line-text">{{ line }}</span>
              </div>
            </template>
          </div>
        </template>
        <div v-else class="cs-placeholder cs-file-empty">
          点击左侧文件树查看源码<template v-if="locateFile">(题目引用:{{ locateFile }})</template>
        </div>
      </div>
    </template>
  </aside>
</template>

<style scoped>
.code-sidebar {
  flex-shrink: 0;
  width: 420px;
  border-left: 1px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 手机窄屏:代码侧栏改为右侧覆盖式抽屉(定位基准为宿主 .page-body) */
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

/* ---- 文件树 ---- */
.cs-tree {
  flex-shrink: 0;
  max-height: 42%;
  overflow-y: auto;
  border-bottom: 1px solid var(--color-border);
  padding: var(--space-2) 0;
}

.cs-tree-node {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: 2px var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  cursor: pointer;
  user-select: none;
}

.cs-tree-node:hover {
  background: var(--color-bg-secondary);
}

.cs-node-active {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.cs-node-arrow {
  flex-shrink: 0;
  width: 12px;
  font-size: 10px;
  text-align: center;
  color: var(--color-text-muted);
}

.cs-node-arrow-empty {
  visibility: hidden;
}

.cs-node-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 文件内容 ---- */
.cs-file {
  flex: 1;
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

.cs-pager-info {
  font-size: 11px;
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.cs-file-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--space-2) 0;
  background: var(--color-bg);
}

.cs-file-empty {
  flex: 1;
}

.cs-line {
  display: flex;
  gap: var(--space-2);
  padding: 0 var(--space-2);
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
}

.cs-line-highlight {
  background: var(--color-primary-light);
}

.cs-line-no {
  flex-shrink: 0;
  min-width: 32px;
  text-align: right;
  color: var(--color-text-muted);
  user-select: none;
}

.cs-line-text {
  white-space: pre;
  color: var(--color-text);
}
</style>
