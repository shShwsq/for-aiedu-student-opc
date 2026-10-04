<script setup lang="ts">
/**
 * 设置页布局(嵌套路由父组件)
 *
 * 提供共享的页面外壳:AppHeader + WorkspaceSidebar + 左侧两级设置目录 + 右侧 RouterView。
 * 一级子路由:account / models / cli / policy / practice(练习功能开关关闭时隐藏入口)。
 *
 * 二级目录由 data/settingsNav 声明,统一用 URL hash 表达(不新增路由记录),
 * 按 childMode 分两种语义:
 * - CLI 设置(childMode='switch'):每个已注册 agent 一项,与面板内 tab 栏双向同步
 * - 练习设置(childMode='anchor'):出题偏好 / 学习主题 / 数据管理,同页锚点 + scrollspy
 *
 * 展开规则(手风琴):二级只在其一级项为当前路由时渲染——展开态是路由的纯函数,
 * 没有本地展开状态,点已选中的一级项不会收起(回到它的默认二级项)。
 *
 * 高亮一律走计算出的 is-active 而非 router-link-active:Vue Router 只比 path、
 * 忽略 hash,同一父项下的所有子链接会被同时判为 active,父项也跟着亮。
 */
import { computed, onMounted, provide, ref } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { agentTypes } from '@/composables/useAgentTypes'
import { ensureFeaturesLoaded } from '@/composables/useFeatures'
import { settingsScrollRootKey } from '@/composables/useSectionNav'
import { SETTINGS_NAV, type SettingsNavChild, type SettingsNavItem } from '@/data/settingsNav'
import { isNavItemActive, resolveActiveChildId, resolveNavChildren } from '@/utils/settingsNav'

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

const route = useRoute()

/** 右侧内容区(子路由面板)的滚动容器:交给 anchor 语义的二级目录做 scrollspy 根 */
const contentRef = ref<HTMLElement | null>(null)
provide(settingsScrollRootKey, contentRef)

// 练习功能开关:关闭时隐藏「练习设置」入口(含其二级目录;进页拉取一次并缓存)
onMounted(() => {
  ensureFeaturesLoaded()
})

/** 目录树的一级项视图(已解析二级项与当前高亮项) */
interface NavGroupView {
  item: SettingsNavItem
  children: SettingsNavChild[]
  /** 是否当前路由 */
  active: boolean
  /** 是否展开二级(手风琴:当前路由且确有二级项) */
  expanded: boolean
  /** 当前应高亮的二级项 id(空串=无二级) */
  activeChildId: string
}

const navViews = computed<NavGroupView[]>(() =>
  SETTINGS_NAV
    .filter((item) => !item.enabled || item.enabled())
    .map((item) => {
      const children = resolveNavChildren(item, agentTypes.value)
      const active = isNavItemActive(item, route.path)
      return {
        item,
        children,
        active,
        expanded: active && children.length > 0,
        activeChildId: resolveActiveChildId(item, children, route.path, route.hash),
      }
    }),
)

/** 是否有展开的二级(窄屏下给横向二级条预留高度) */
const hasExpandedSub = computed(() => navViews.value.some((view) => view.expanded))
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

      <!-- 导航 + 内容整体居中 -->
      <div class="settings-shell">
        <!-- 左侧设置目录(一级 + 二级) -->
        <nav
          class="settings-nav"
          :class="{ 'settings-nav--with-sub': hasExpandedSub }"
          aria-label="设置目录"
        >
          <div class="nav-list">
            <div v-for="(view, idx) in navViews" :key="view.item.path" class="nav-group">
              <RouterLink
                :to="view.item.path"
                :class="[
                  'nav-item',
                  {
                    'is-active': view.active,
                    'nav-item--first': idx === 0,
                    'nav-item--last': idx === navViews.length - 1,
                    'nav-item--has-sub': view.expanded,
                  },
                ]"
                :aria-expanded="view.item.children ? view.expanded : undefined"
              >
                <span class="nav-label">{{ view.item.label }}</span>
                <span class="nav-node" aria-hidden="true" />
              </RouterLink>

              <!-- 二级目录:仅当前一级项下渲染(手风琴) -->
              <div
                v-if="view.expanded"
                :class="['nav-sub', { 'nav-sub--last-group': idx === navViews.length - 1 }]"
                role="group"
                :aria-label="`${view.item.label}二级目录`"
              >
                <RouterLink
                  v-for="child in view.children"
                  :key="child.id"
                  :to="{ path: view.item.path, hash: `#${child.id}` }"
                  :class="['nav-sub-item', { 'is-active': child.id === view.activeChildId }]"
                  :aria-current="child.id === view.activeChildId ? 'page' : undefined"
                  :title="child.label"
                >
                  <span class="nav-sub-label">{{ child.label }}</span>
                </RouterLink>
              </div>
            </div>
          </div>
        </nav>

        <!-- 右侧子路由内容 -->
        <main ref="contentRef" class="settings-content">
          <RouterView />
        </main>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
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

/* ---- 导航 + 内容居中容器 ---- */
.settings-shell {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-width: 0;
  max-width: 1220px;
  margin: 0 auto;
  overflow: hidden;
}

/* ---- 左侧设置目录(无边框;一级与二级共用右侧竖线轨道 + 节点,二级靠缩进读层级) ---- */
.settings-nav {
  flex-shrink: 0;
  width: var(--settings-nav-w);
  padding: var(--space-6) var(--space-4);
  overflow-y: auto;
}

.nav-list {
  display: flex;
  flex-direction: column;
}

.nav-item {
  position: relative;
  display: block;
  padding: 10px 36px 10px var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-md);
  transition: color var(--transition-fast), background var(--transition-fast);
}

/* 一级节点间的连接竖线(位于导航项右侧,与节点同心) */
.nav-item::before {
  content: '';
  position: absolute;
  right: 20px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--color-border);
}

/* 首尾项竖线各截去一半,使线只在节点之间延伸。
   用显式类而非 :first-child/:last-child:一级项展开后其二级块成为同级最后一个子节点,
   :last-child 就不再命中末个一级项(如展开的「练习设置」),截半会静默失效。 */
.nav-item--first::before {
  top: 50%;
}

.nav-item--last::before {
  bottom: 50%;
}

/* 展开二级的一级项:竖线不断到底,直接接入下面二级的轨道(同一条轨道) */
.nav-item--has-sub::before {
  bottom: 0;
}

/* 状态节点:空心圆,当前项填充主色并带光环 */
.nav-node {
  position: absolute;
  right: 16px;
  top: 50%;
  transform: translateY(-50%);
  box-sizing: border-box;
  width: 10px;
  height: 10px;
  border: 2px solid var(--color-border-strong);
  border-radius: 50%;
  background: var(--color-surface);
  transition: all var(--transition-fast);
}

.nav-item:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.nav-item.is-active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  font-weight: var(--fw-semibold);
}

.nav-item.is-active .nav-node {
  border-color: var(--color-primary);
  background: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

/* ---- 二级目录(与一级同轨:右侧竖线 + 空心小圆点,层级靠缩进) ---- */
.nav-sub {
  display: flex;
  flex-direction: column;
  /* 不留上下边距:轨道是一条连续线,间距全靠各项自身 padding */
  margin: 0;
}

.nav-sub-item {
  position: relative;
  display: block;
  padding: 6px 36px 6px var(--space-3);
  /* 缩进读层级;右缘与一级对齐,故轨道同心 */
  margin-left: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-sm);
  transition: color var(--transition-fast), background var(--transition-fast);
}

/* 二级轨道:与一级同一 x(右 20px),从一级项底边接着往下画 */
.nav-sub-item::before {
  content: '';
  position: absolute;
  right: 20px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--color-border);
}

/* 展开项是末个一级项时,整棵目录的末尾是它的最后一个子项:在那里收尾 */
.nav-sub--last-group .nav-sub-item:last-child::before {
  bottom: 50%;
}

/* 二级节点:8px 空心圆(圆心与一级节点同轴,均为右 21px),当前项填充主色 */
.nav-sub-item::after {
  content: '';
  position: absolute;
  right: 17px;
  top: 50%;
  transform: translateY(-50%);
  box-sizing: border-box;
  width: 8px;
  height: 8px;
  border: 2px solid var(--color-border-strong);
  border-radius: 50%;
  background: var(--color-surface);
  transition: all var(--transition-fast);
}

/* 后端 display_name 可能较长,省略号兜底(完整名走 title) */
.nav-sub-label {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nav-sub-item:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.nav-sub-item.is-active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  font-weight: var(--fw-semibold);
}

.nav-sub-item.is-active::after {
  border-color: var(--color-primary);
  background: var(--color-primary);
}

/* ---- 右侧内容区 ---- */
.settings-content {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

/* ---- 响应式:窄屏(手机) ---- */
@media (max-width: 768px) {
  .settings-nav {
    width: var(--settings-nav-w-sm);
    padding: var(--space-4) var(--space-2);
  }

  .nav-item {
    padding: 8px 36px 8px var(--space-2);
    font-size: var(--fs-xs);
  }

  .nav-sub-item {
    /* 缩进保持 8px(与宽屏一致),否则两级只差 4px 读不出层级 */
    padding: 6px 36px 6px var(--space-2);
    margin-left: var(--space-2);
  }
}

@media (max-width: 640px) {
  .settings-shell {
    flex-direction: column;
    max-width: none;
  }

  /* 一级横向 chip 条 + 二级固定在其下方一整条(靠手风琴保证同屏最多一组二级);
     二级绝对定位在 nav 自身(定位基准)的预留带内,避开 .nav-list 横向滚动的裁剪 */
  .settings-nav {
    position: relative;
    width: 100%;
    padding: var(--space-2) var(--space-3);
    overflow: visible;
  }

  /* 展开二级时给下面的横向条留位(28px = 二级 chip 高) */
  .settings-nav--with-sub {
    padding-bottom: calc(var(--space-2) + 28px);
  }

  .nav-list {
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    gap: var(--space-2);
    scrollbar-width: none;
  }

  .nav-list::-webkit-scrollbar {
    display: none;
  }

  .nav-group {
    flex-shrink: 0;
  }

  .nav-item {
    padding: var(--space-2) var(--space-3);
    white-space: nowrap;
  }

  /* 横向 chip 布局下隐藏轨道竖线与节点 */
  .nav-item::before,
  .nav-node {
    display: none;
  }

  .nav-sub {
    position: absolute;
    left: var(--space-3);
    right: var(--space-3);
    bottom: var(--space-2);
    flex-direction: row;
    margin: 0;
    overflow-x: auto;
    gap: var(--space-2);
    scrollbar-width: none;
  }

  .nav-sub::-webkit-scrollbar {
    display: none;
  }

  .nav-sub-item {
    flex-shrink: 0;
    margin-left: 0;
    padding: var(--space-1) var(--space-3);
    white-space: nowrap;
    border: 1px solid var(--color-border);
  }

  /* 横向 chip 布局下二级条也不画轨道与圆点 */
  .nav-sub-item::before,
  .nav-sub-item::after {
    display: none;
  }

  .nav-sub-label {
    overflow: visible;
  }
}
</style>
