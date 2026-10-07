<script setup lang="ts">
/**
 * 二进制文件的替代视图(取代源码面板)
 *
 * docx / pdf / 图片这类文件后端不再回传内容:sandbox 模式下读取走的是 execd 的
 * 文本通道,原始字节过来就是乱码,同一函数还会污染智能体上下文。这里只回答三个
 * 问题:这是哪个文件、多大、怎么拿走(下载)。
 *
 * source 决定调哪条端点:
 * - workspace:沙箱工作区文件,会话过期后下载会失败(提示走「重新克隆」)
 * - uploads:用户上传原件,不经沙箱,保留期内始终可取回
 *
 * 下载动作本身在 useFileDownload 里(文件面板头部的下载按钮走同一套)。
 */
import { computed, watch } from 'vue'

import { useFileDownload, type FileDownloadSource } from '@/composables/useFileDownload'
import { formatBytes } from '@/utils/bytes'
import { basenameOf } from '@/utils/download'
import { fileSuffix } from '@/utils/fileKind'

const props = withDefaults(
  defineProps<{
    taskId: string
    /** 工作根内相对路径 */
    path: string | null
    /** 文件来源(决定下载端点与过期文案) */
    source?: FileDownloadSource
    /** 后端 binary 响应带回的字节数;0 表示未提供 */
    size?: number
  }>(),
  { source: 'workspace', size: 0 },
)

const {
  downloading, downloadError: errorMsg, run: runDownload, reset: resetDownload,
} = useFileDownload()

// 卡片实例在切换文件时被复用(父级 v-else-if 不重建),上个文件的失败文案要清掉
watch(() => [props.taskId, props.path, props.source], resetDownload)

const filename = computed(() => basenameOf(props.path))
const kindLabel = computed(() => {
  const suffix = fileSuffix(props.path)
  return suffix ? suffix.slice(1).toUpperCase() : '二进制'
})
const sizeLabel = computed(() => (props.size > 0 ? formatBytes(props.size) : ''))
const hint = computed(() =>
  props.source === 'uploads'
    ? '上传原件不受沙箱过期影响,保留期内可随时取回。'
    : '工作区会话过期后需先「重新克隆」再下载。',
)

async function download(): Promise<void> {
  await runDownload(props.taskId, props.path ?? '', props.source)
}
</script>

<template>
  <div class="fbc">
    <div class="fbc-icon" aria-hidden="true">
      <svg
        viewBox="0 0 24 24"
        width="28"
        height="28"
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
    </div>

    <div class="fbc-title">{{ filename }}</div>

    <div class="fbc-meta">
      <span class="fbc-tag">{{ kindLabel }}</span>
      <span v-if="sizeLabel" class="fbc-size">{{ sizeLabel }}</span>
    </div>

    <p class="fbc-desc">该文件不支持在线预览,可下载后在本地查看。</p>

    <button
      type="button"
      class="fbc-btn"
      :disabled="!path || !taskId || downloading"
      @click="download"
    >
      <span v-if="downloading" class="fbc-spinner" aria-hidden="true" />
      <svg
        v-else
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
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
        <polyline points="7 10 12 15 17 10" />
        <line x1="12" y1="15" x2="12" y2="3" />
      </svg>
      {{ downloading ? '准备中…' : '下载文件' }}
    </button>

    <p class="fbc-hint">{{ hint }}</p>
    <p v-if="errorMsg" class="fbc-error" role="alert">{{ errorMsg }}</p>
  </div>
</template>

<style scoped>
.fbc {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-4);
  background: var(--color-surface);
  text-align: center;
}

.fbc-icon {
  color: var(--color-text-muted);
}

.fbc-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  word-break: break-all;
  max-width: 100%;
}

.fbc-meta {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.fbc-tag {
  padding: 1px var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.fbc-size {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.fbc-desc {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.fbc-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 30px;
  padding: 0 var(--space-4);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.fbc-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.fbc-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.fbc-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: fbc-spin 0.8s linear infinite;
}

@keyframes fbc-spin {
  to {
    transform: rotate(360deg);
  }
}

.fbc-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.fbc-error {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-danger);
}
</style>
