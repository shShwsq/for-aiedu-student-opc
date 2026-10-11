<script setup lang="ts">
/**
 * 任务模型默认(「模型设置 > 任务模型默认」二级项)
 *
 * 一处会调 LLM 的任务(槽位)默认用哪个模型,过去散在练习设置 / 记忆设置 /
 * 任务提交页三处,而且「任务默认审查模型」「AI 润色」根本没有入口。
 * 本页把这些去向**一次看全**:每行显示当前生效的模型与来源,编辑仍发生在
 * 归属面板(点行尾链接跳过去),因此这里只读 —— 同一个字段不留两个写入口。
 *
 * 值与来源都由后端 GET /models/defaults 算好(解析链只有一处实现),
 * 前端不再自己拼优先级,也不会出现「显示 A 实际用 B」。
 */
import { onMounted, ref } from 'vue'

import HelpPopover from '@/components/HelpPopover.vue'
import { getModelSlots } from '@/api/model_slots'
import { extractErrorMessage } from '@/utils/error'
import {
  editLabel,
  editTarget,
  effectiveLabel,
  thinkingModeLabel,
} from '@/utils/modelSlots'
import type { ModelSlot, ModelSlotInfoRow, ModelSlotsResponse } from '@/types/model_slots'

const loading = ref(true)
const loadError = ref('')
const slots = ref<ModelSlot[]>([])
const infoRows = ref<ModelSlotInfoRow[]>([])
const envModel = ref('')

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const data: ModelSlotsResponse = await getModelSlots()
    slots.value = data.slots
    infoRows.value = data.info_rows
    envModel.value = data.env_default_model
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="page-header">
      <div>
        <h1>任务模型默认</h1>
        <p class="page-subtitle">
          各处任务默认用哪个模型。本页只做汇总展示,修改请进对应设置页 ——
          同一项设置只有一个写入口,不会出现两处改了互相覆盖。
        </p>
      </div>
      <button class="btn-plain" :disabled="loading" @click="load">刷新</button>
    </div>

    <div v-if="loading" class="state-box">
      <span class="status-spinner" aria-label="加载中" />
      <span>正在读取模型分配…</span>
    </div>

    <div v-else-if="loadError" class="alert alert-error" role="alert">
      <span>加载失败:{{ loadError }}</span>
      <button class="btn-link" @click="load">重试</button>
    </div>

    <template v-else>
      <!-- ============ 槽位表 ============ -->
      <section class="slot-table" aria-label="任务模型分配">
        <div v-for="slot in slots" :key="slot.slot" class="slot-row">
          <div class="slot-main">
            <div class="slot-title-row">
              <span class="slot-title">{{ slot.label }}</span>
              <HelpPopover max-width="min(380px, 86vw)">{{ slot.description }}</HelpPopover>
              <span v-if="slot.task_override" class="slot-tag">任务自带模型优先</span>
            </div>
            <div class="slot-value-row">
              <span
                :class="['slot-value', slot.source === 'slot' ? 'is-set' : 'is-env']"
              >{{ effectiveLabel(slot, envModel) }}</span>
              <span v-if="slot.stale" class="slot-stale" role="status">
                原选配置已删除,当前回退环境默认,请重新选择
              </span>
              <span v-if="slot.has_thinking_mode" class="slot-thinking">
                思考模式:{{ thinkingModeLabel(slot.thinking_mode) }}
              </span>
            </div>
          </div>
          <RouterLink
            :to="editTarget(slot.edit_path)"
            class="slot-edit"
            :title="`${slot.label}在归属面板修改`"
          >{{ editLabel(slot.edit_path) }} →</RouterLink>
        </div>
      </section>

      <!-- ============ 不可配置的模型去向(说明用途,避免用户找不到) ============ -->
      <section class="info-section" aria-label="其他模型去向">
        <h2 class="section-title">其他模型去向</h2>
        <dl class="info-list">
          <template v-for="row in infoRows" :key="row.id">
            <dt class="info-label">{{ row.label }}</dt>
            <dd class="info-desc">{{ row.description }}</dd>
          </template>
        </dl>
      </section>
    </template>
  </div>
</template>

<style scoped>
.panel {
  max-width: 920px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-6) var(--space-12);
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
  max-width: 640px;
}

/* ---- 状态区 ---- */
.state-box {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
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

.btn-plain {
  flex-shrink: 0;
  height: 30px;
  padding: 0 var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.btn-plain:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-plain:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ---- 槽位表 ---- */
.slot-table {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.slot-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.slot-row:last-child {
  border-bottom: none;
}

.slot-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.slot-title-row {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

.slot-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.slot-tag {
  margin-left: var(--space-1);
  padding: 1px 7px;
  font-size: 10px;
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
  white-space: nowrap;
}

.slot-value-row {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.slot-value {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.slot-value.is-set {
  color: var(--color-text);
}

/* 没显式配置(回退环境默认)时降低对比度:让用户看出这是「未指定」而不是已选定 */
.slot-value.is-env {
  color: var(--color-text-muted);
  font-style: italic;
}

.slot-stale {
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

.slot-thinking {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
}

.slot-edit {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  text-decoration: none;
  white-space: nowrap;
}

.slot-edit:hover {
  text-decoration: underline;
}

/* ---- 说明区 ---- */
.info-section {
  margin-top: var(--space-6);
}

.section-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  margin: 0 0 var(--space-2);
}

.info-list {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: var(--space-1) var(--space-4);
  margin: 0;
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.info-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.info-desc {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
}

@media (max-width: 640px) {
  .panel {
    padding: var(--space-4) var(--space-3) var(--space-8);
  }

  .page-header {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-2);
  }

  .slot-row {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-2);
  }
}
</style>
