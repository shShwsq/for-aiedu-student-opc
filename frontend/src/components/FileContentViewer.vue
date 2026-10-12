<script setup lang="ts">
/**
 * 只读文件内容查看器(IDE 观感)
 *
 * 两处升级自旧版「手写逐行渲染」:
 * - 只读 CodeMirror:行号槽(按页起始行偏移,显示真实文件行号)、
 *   软换行、按文件后缀经 @codemirror/language-data 惰性加载语法高亮。
 *   只读不可编辑、隐藏光标,纯浏览。
 * - Markdown 文件(.md/.markdown/.mdx)额外提供「源码 / 预览」切换,
 *   预览用 renderMarkdown(marked + DOMPurify 净化)渲染,与记忆管理页一致。
 *
 * 定位:父组件加载完某页内容后调用 focusRange(真实起止行),
 * 内部换算成当前页文档行号做区间高亮 + 滚动到中间;超出本页自动夹取。
 *
 * 分页仍由父组件负责(接口按 offset/maxLines 返回一页原文),本组件
 * 只渲染传入的这一页,行号槽据 startLine 偏移保证跨页连续。
 */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { Compartment, EditorState, RangeSet, StateEffect, StateField, type Extension, type Range } from '@codemirror/state'
import {
  Decoration,
  EditorView,
  lineNumbers,
  placeholder as cmPlaceholder,
} from '@codemirror/view'
import {
  LanguageDescription,
  defaultHighlightStyle,
  syntaxHighlighting,
} from '@codemirror/language'
import { languages } from '@codemirror/language-data'

import { renderMarkdown } from '@/utils/markdown'
import { classifyPreviewLink } from '@/utils/markdownLinks'

const props = withDefaults(
  defineProps<{
    /** 当前页原始文本内容 */
    content: string
    /** 文件名/路径(据后缀判定语法与是否 Markdown) */
    filename: string | null
    /** 本页首行对应的真实文件行号(1-based),行号槽按此偏移 */
    startLine: number
    /** 内容为空时占位文案 */
    placeholder?: string
  }>(),
  { placeholder: '' },
)

const emit = defineEmits<{
  /** 预览里点击了指向工作区其它文件的链接:path 为已解析的工作区相对路径,交父组件在应用内打开 */
  (e: 'open-link', path: string): void
}>()

const hostRef = ref<HTMLDivElement | null>(null)
const previewRef = ref<HTMLDivElement | null>(null)
const view = shallowRef<EditorView | null>(null)

// ============================================================
// Markdown 源码 / 预览切换
// ============================================================
const isMarkdown = ref(false)
const mode = ref<'code' | 'preview'>('code')

function detectMarkdown(filename: string | null): boolean {
  return /\.(md|markdown|mdx)$/i.test(filename ?? '')
}

/** 预览 HTML(renderMarkdown 已含 DOMPurify 净化) */
function previewHtmlOf(text: string): string {
  return renderMarkdown(text)
}

const previewHtml = ref('')

// ============================================================
// 行区间高亮:父组件加载完某页后调用 focusRange,把真实行号换算成当前页
// 文档行号并算出 Decoration 区间数组经 effect 传入 field;field 只负责存储
// + 跨文档改动映射(位置随文本位移)。
// ============================================================
const setHighlight = StateEffect.define<readonly Range<Decoration>[]>()

const highlightField = StateField.define<RangeSet<Decoration>>({
  create: () => RangeSet.empty,
  update(value, tr) {
    let next = value.map(tr.changes)
    for (const e of tr.effects) {
      if (e.is(setHighlight)) {
        next = e.value.length ? RangeSet.of(e.value, true) : RangeSet.empty
      }
    }
    return next
  },
  provide: (f) => EditorView.decorations.from(f),
})

// 行号槽起始偏移 & 语法 & 占位:均可动态重组
const gutterCompartment = new Compartment()
const languageCompartment = new Compartment()
const placeholderCompartment = new Compartment()

/** 行号格式化:页内第 lineNo 行 → 真实文件行号(lineNo + startLine - 1) */
function lineNumbersExt(startLine: number) {
  const offset = Math.max(0, startLine - 1)
  return lineNumbers({ formatNumber: (lineNo: number) => String(lineNo + offset) })
}

/** 按后缀惰性加载语法高亮(language-data 未收录时回退无高亮)
 *
 * 语言包为异步动态 import:连续切文件 A→B 时,A 的 load 可能晚于 B resolve,
 * 会把 A 的语法装到 B 的文档上。用 langToken 单调自增标记「当前文件」,
 * 回调里比对 token 确保只有最新一次切换才生效(view 实例跨文件复用不会变)。
 */
let langToken = 0
function applyLanguage(filename: string | null): void {
  const v = view.value
  if (!v) return
  const token = ++langToken
  const desc = filename ? LanguageDescription.matchFilename(languages, filename) : null
  if (!desc) {
    v.dispatch({ effects: languageCompartment.reconfigure([]) })
    return
  }
  const target = v
  desc.load().then((support) => {
    // 仍是最新一次切换(token 未被后续切换作废)且编辑器未销毁时才装配语法
    if (token === langToken && view.value === target) {
      target.dispatch({ effects: languageCompartment.reconfigure(support) })
    }
  }).catch(() => {
    /* 语言包加载失败:保持纯文本高亮,不影响浏览 */
  })
}

const editorTheme = EditorView.theme({
  '&': { backgroundColor: 'transparent', color: 'var(--color-text)', height: '100%', fontSize: '12px' },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': {
    fontFamily: 'var(--font-mono)',
    lineHeight: '1.6',
    overflow: 'auto',
  },
  '.cm-content': { padding: '6px 0', caretColor: 'transparent' },
  '.cm-line': { padding: '0 8px' },
  '.cm-gutters': {
    backgroundColor: 'var(--color-surface-alt)',
    color: 'var(--color-text-muted)',
    border: 'none',
    borderRight: '1px solid var(--color-border)',
    fontFamily: 'var(--font-mono)',
  },
  '.cm-lineNumbers .cm-gutterElement': { padding: '0 8px 0 12px', minWidth: '34px' },
  '.cm-cursor': { display: 'none' },
  // 高亮区间(题目源码定位):整行淡主色底 + 左侧主色条
  '.cm-focus-line': {
    backgroundColor: 'var(--color-primary-light)',
    boxShadow: 'inset 2px 0 0 var(--color-primary)',
  },
})

function buildExtensions(): Extension {
  return [
    highlightField,
    EditorState.readOnly.of(true),
    EditorView.editable.of(false),
    EditorView.lineWrapping,
    gutterCompartment.of(lineNumbersExt(props.startLine)),
    languageCompartment.of([]),
    placeholderCompartment.of(cmPlaceholder(props.placeholder)),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    editorTheme,
  ]
}

onMounted(() => {
  if (!hostRef.value) return
  view.value = new EditorView({
    state: EditorState.create({ doc: props.content ?? '', extensions: buildExtensions() }),
    parent: hostRef.value,
  })
  isMarkdown.value = detectMarkdown(props.filename)
  applyLanguage(props.filename)
})

onBeforeUnmount(() => {
  view.value?.destroy()
  view.value = null
})

// 内容 / 页起始行变化:整页替换文档 + 重算行号偏移 + 重新语法/预览
watch(
  () => [props.content, props.startLine] as const,
  ([content, startLine]) => {
    const v = view.value
    if (!v) return
    const doc = v.state.doc
    v.dispatch({
      changes: { from: 0, to: doc.length, insert: content ?? '' },
      effects: [gutterCompartment.reconfigure(lineNumbersExt(startLine))],
    })
    refreshPreview()
  },
)

// 文件切换:语法 + Markdown 判定 + 重置为源码视图(避免带着上一篇的预览)
watch(
  () => props.filename,
  (filename) => {
    isMarkdown.value = detectMarkdown(filename)
    mode.value = 'code'
    applyLanguage(filename)
    refreshPreview()
  },
)

watch(() => props.placeholder, (p) => {
  const v = view.value
  if (v) v.dispatch({ effects: placeholderCompartment.reconfigure(cmPlaceholder(p)) })
})

function refreshPreview(): void {
  previewHtml.value = isMarkdown.value ? previewHtmlOf(props.content) : ''
}

/** 切换源码 / 预览;切回源码时让 CodeMirror 重新测量(它可能被 display:none 隐藏过) */
function switchMode(next: 'code' | 'preview'): void {
  if (mode.value === next) return
  mode.value = next
  if (next === 'preview') {
    previewHtml.value = previewHtmlOf(props.content)
  } else {
    view.value?.requestMeasure()
  }
}

// ============================================================
// 预览区链接点击:拦下浏览器默认跳转,分类处置
// ------------------------------------------------------------
// Markdown 相对链接会被渲染成 <a href="...">;若放任默认行为,浏览器按当前 SPA URL
// (/tasks/{uuid})解析它并整页跳转,落进 /tasks/:id 用非 UUID 的 id 请求后端。
// 故一律 preventDefault,再按 classifyPreviewLink 的判定分流(逻辑集中在 utils/markdownLinks)。
// ============================================================
/** 预览正文点击委托:命中 <a> 即拦下默认跳转(含 ctrl/中键,否则新标签会复现同样坏 URL) */
function onPreviewClick(e: MouseEvent): void {
  const anchor = (e.target as HTMLElement | null)?.closest?.('a') as HTMLAnchorElement | null
  if (!anchor) return
  const href = anchor.getAttribute('href') ?? ''
  e.preventDefault()
  const action = classifyPreviewLink(props.filename ?? '', href)
  switch (action.kind) {
    case 'external':
      // 绝对外链:新标签打开,不离开当前工作树
      window.open(action.url, '_blank', 'noopener,noreferrer')
      break
    case 'anchor': {
      // 页内锚点:在预览容器内平滑滚动(marked 未生成标题 id 时静默无操作)
      const el = previewRef.value?.querySelector(`#${CSS.escape(action.id)}`)
      el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      break
    }
    case 'workspace':
      // 工作区相对链接:交父组件在应用内打开
      emit('open-link', action.path)
      break
    case 'ignore':
    default:
      break
  }
}

// ============================================================
// 定位(供父组件加载完内容后调用)
// ============================================================
/** 把真实行区间高亮并滚动到中间;行号换算成当前页文档行号,越界自动夹取 */
function focusRange(realStart: number | null, realEnd: number | null): void {
  const v = view.value
  if (!v) return
  // 预览模式下无行概念:先切回源码
  if (mode.value === 'preview') mode.value = 'code'
  const doc = v.state.doc
  if (!realStart) {
    v.dispatch({ effects: setHighlight.of([]) })
    return
  }
  const offset = Math.max(0, props.startLine - 1)
  const docStart = Math.max(1, Math.min(realStart - offset, doc.lines))
  const docEnd = Math.max(docStart, Math.min((realEnd ?? realStart) - offset, doc.lines))
  const ranges: Range<Decoration>[] = []
  for (let l = docStart; l <= docEnd; l += 1) {
    ranges.push(Decoration.line({ class: 'cm-focus-line' }).range(doc.line(l).from))
  }
  v.dispatch({
    effects: [
      setHighlight.of(ranges),
      EditorView.scrollIntoView(doc.line(docStart).from, { y: 'center' }),
    ],
  })
}

function clearHighlight(): void {
  const v = view.value
  if (v) v.dispatch({ effects: setHighlight.of([]) })
}

defineExpose({ focusRange, clearHighlight })
</script>

<template>
  <div class="fcv">
    <!-- Markdown:源码 / 预览切换条 -->
    <div v-if="isMarkdown" class="fcv-mode-switch" role="group" aria-label="源码/预览">
      <button
        type="button"
        :class="['fcv-mode-btn', { active: mode === 'code' }]"
        @click="switchMode('code')"
      >源码</button>
      <button
        type="button"
        :class="['fcv-mode-btn', { active: mode === 'preview' }]"
        @click="switchMode('preview')"
      >预览</button>
    </div>

    <!-- 源码视图:只读 CodeMirror(始终在 DOM,v-show 控制显隐以保留状态) -->
    <div v-show="mode === 'code'" ref="hostRef" class="fcv-cm" />

    <!-- Markdown 预览视图(链接点击由 onPreviewClick 拦下,避免相对链接整页跳出 SPA) -->
    <div v-if="mode === 'preview'" ref="previewRef" class="fcv-preview" @click="onPreviewClick">
      <div v-if="previewHtml" class="markdown-body" v-html="previewHtml" />
      <div v-else class="fcv-preview-empty">暂无内容</div>
    </div>
  </div>
</template>

<style scoped>
.fcv {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
}

.fcv-mode-switch {
  position: absolute;
  top: var(--space-2);
  right: var(--space-3);
  z-index: 2;
  display: inline-flex;
  padding: 2px;
  gap: 2px;
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.fcv-mode-btn {
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.fcv-mode-btn.active {
  color: var(--color-text);
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}

.fcv-cm {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--color-bg);
}

.fcv-cm :deep(.cm-editor) {
  height: 100%;
}

.fcv-preview {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface);
}

.fcv-preview-empty {
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
  text-align: center;
  padding: var(--space-6) 0;
}

/* Markdown 渲染样式(与记忆管理页对齐;作用于 v-html 容器) */
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

.markdown-body :deep(h1) { font-size: var(--fs-lg); }
.markdown-body :deep(h2) { font-size: var(--fs-base); }
.markdown-body :deep(h3) { font-size: var(--fs-sm); }

.markdown-body :deep(p) { margin: 0 0 var(--space-3); }

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-5);
}

.markdown-body :deep(li) { margin: var(--space-1) 0; }

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

.markdown-body :deep(pre code) { padding: 0; background: transparent; }

.markdown-body :deep(blockquote) {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-4);
  border-left: 3px solid var(--color-primary);
  background: var(--color-surface-alt);
  color: var(--color-text-secondary);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.markdown-body :deep(a) { color: var(--color-primary); text-decoration: none; }
.markdown-body :deep(a:hover) { text-decoration: underline; }

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
</style>
