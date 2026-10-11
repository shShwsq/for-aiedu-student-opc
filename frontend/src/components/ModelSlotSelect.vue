<script setup lang="ts">
/**
 * 模型槽位下拉(共享件)
 *
 * 各设置面板里「某处任务默认用哪个模型」的下拉长得一样:一个空选项 + 已存配置列表 +
 * 「暂无配置时提示先去模型设置添加」。此前这段在练习设置 / 记忆设置 / 智能体策略里各抄一份,
 * 空选项文案与提示语容易漂,故收成一个组件。
 *
 * 只负责展示与回传选中的配置 id,保存时机由父面板决定(切即存 / 随表单一起存)。
 * `modelValue` 用空串表示「未指定(跟随默认)」,与后端 *_llm_config_id 的
 * 空串=清空约定对齐,父层直接把值塞进请求体即可。
 *
 * 无障碍标签走 `selectLabel` 而不是 `aria-label`:vue-tsc 不把模板上的
 * `aria-*` 当 prop 名做驼峰映射,会当成透传属性而报「缺必填 prop」。
 */
import { computed } from 'vue'

import { configOptionLabel } from '@/composables/useLlmConfigOptions'
import type { LLMConfigItemOut } from '@/types/model_configs'

const props = withDefaults(
  defineProps<{
    /** 选中的配置 id;空串=未指定(跟随默认) */
    modelValue: string
    /** 下拉选项来源(用户已保存的 LLM 配置) */
    configs: LLMConfigItemOut[]
    /** 空选项文案:各槽位回退目标不同(跟随系统默认 / 沿用出题模型 / 同评估模型) */
    emptyLabel: string
    /** 下拉的无障碍标签(绑到原生 select 的 aria-label) */
    selectLabel: string
    disabled?: boolean
    /** 无已存配置时的提示;传空串则不显示 */
    emptyHint?: string
  }>(),
  {
    disabled: false,
    emptyHint: '暂无已保存的模型配置,可先到「模型设置」中添加',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  /** 选中值(与 update:modelValue 同值):父面板用它触发「切即存」 */
  change: [value: string]
}>()

const noConfigs = computed(() => props.configs.length === 0)

function onSelect(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  emit('update:modelValue', value)
  emit('change', value)
}
</script>

<template>
  <div class="slot-select-wrap">
    <select
      class="slot-select"
      :value="props.modelValue"
      :aria-label="props.selectLabel"
      :disabled="props.disabled"
      @change="onSelect"
    >
      <option value="">{{ props.emptyLabel }}</option>
      <option v-for="cfg in props.configs" :key="cfg.id" :value="cfg.id">
        {{ configOptionLabel(cfg) }}
      </option>
    </select>
    <span v-if="noConfigs && props.emptyHint" class="slot-hint">{{ props.emptyHint }}</span>
  </div>
</template>

<style scoped>
.slot-select-wrap {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.slot-select {
  width: 100%;
  max-width: 420px;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-family: inherit;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.slot-select:hover:not(:disabled) {
  border-color: var(--color-border-strong);
}

.slot-select:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.slot-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}
</style>
