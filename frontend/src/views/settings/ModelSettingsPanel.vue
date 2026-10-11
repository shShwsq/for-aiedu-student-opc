<script setup lang="ts">
/**
 * 模型设置面板(嵌套在 SettingsLayout 内,二级目录 childMode='switch')
 *
 * 同页两块视图,由 URL hash 决定显示哪一块:
 * - 无 hash / #configs → 模型清单(LLM / Embedding 配置的增删改与连通性测试)
 * - #defaults → 任务模型默认(各处任务默认用哪个模型的只读汇总)
 *
 * 无 hash 落在「模型清单」:`/models` 旧路径重定向、既有书签、以及各面板里
 * 「可先到「模型设置」中添加」的链接都只指一级路径,行为与拆二级之前一致
 * (回退第一个子项的规则在 utils/settingsNav.resolveActiveChildId)。
 *
 * 两块用 v-show 互切并保留各自状态(与 CLI 设置各 agent 面板同一做法),
 * 但**首次访问才挂载**:没进过 #defaults 就不会白拉一次 /models/defaults。
 * 侧栏子项与浏览器前进后退都只改 hash,统一由下面的 watch 落到 activeSection
 * (单向数据流,两处入口共用一条路径,不会互相触发回环)。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import ModelConfigsSection from './ModelConfigsSection.vue'
import ModelDefaultsSection from './ModelDefaultsSection.vue'
import { MODEL_SECTION_IDS } from '@/data/settingsNav'

type ModelSection = (typeof MODEL_SECTION_IDS)[number]

const route = useRoute()

function sectionFromHash(): ModelSection {
  const id = (route.hash ?? '').replace(/^#/, '')
  return (MODEL_SECTION_IDS as readonly string[]).includes(id)
    ? (id as ModelSection)
    : 'configs'
}

const activeSection = ref<ModelSection>(sectionFromHash())
/** 已访问过的视图(首次访问才挂载,之后 v-show 保留状态) */
const visited = ref<Set<ModelSection>>(new Set([activeSection.value]))

const showConfigs = computed(() => activeSection.value === 'configs')
const showDefaults = computed(() => activeSection.value === 'defaults')

function activate(section: ModelSection): void {
  activeSection.value = section
  if (!visited.value.has(section)) {
    visited.value = new Set(visited.value).add(section)
  }
}

// 进入时按 hash 定位(深链 /settings/models#defaults 直接显示汇总页)
onMounted(() => activate(sectionFromHash()))

// 侧栏二级项 / 前进后退都只改 hash
watch(
  () => route.hash,
  () => {
    // 只在模型页响应(离开后 route 仍可能短暂持有旧 hash)
    if (!route.path.startsWith('/settings/models')) return
    activate(sectionFromHash())
  },
)
</script>

<template>
  <div class="models-panel">
    <ModelConfigsSection v-if="visited.has('configs')" v-show="showConfigs" />
    <ModelDefaultsSection v-if="visited.has('defaults')" v-show="showDefaults" />
  </div>
</template>

<style scoped>
/* 容器不外加内边距:两块各自带 .panel 容器(与拆分前保持一致的排版) */
.models-panel {
  min-width: 0;
}
</style>
