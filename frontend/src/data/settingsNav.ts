/**
 * 设置页目录模型(一级 + 二级)
 *
 * 一级项与路由表(/settings/* 子路由)一一对应;二级项统一用 URL hash 表达
 * (`/settings/cli#qoder_cli`、`/settings/practice#learning-topics`),
 * 不新增路由记录,因此既有守卫(练习功能开关)与旧路径重定向(/cli 等)不受影响。
 *
 * 二级目录有两种语义(childMode):
 * - 'switch':子项 = 同页内视图切换(CLI 各 agent 面板 v-show 互切),不做滚动
 * - 'anchor':子项 = 同页锚点跳转 + scrollspy 高亮(练习设置三段)
 *
 * 展开规则(手风琴):子项只在 `route.path === item.path` 时渲染,
 * 展开态是路由的纯函数,没有本地展开状态,点已选中的一级项不会收起。
 */
import { practiceEnabled } from '@/composables/useFeatures'

/** 二级目录项:id 同时作为 URL hash 与页内元素 id */
export interface SettingsNavChild {
  /** 锚点 id / agent_type,写入 hash 与 DOM id */
  id: string
  /** 展示名(agent 子项取后端 display_name) */
  label: string
}

/** 一级目录项 */
export interface SettingsNavItem {
  /** 一级路由路径,如 '/settings/cli' */
  path: string
  /** 一级展示名 */
  label: string
  /**
   * 二级目录来源:
   * - 静态数组(练习设置三段)
   * - 'agent-types'(CLI 设置:由 GET /agents/types 动态解析,注册新 agent 自动多一项)
   * - 省略(无二级目录)
   */
  children?: SettingsNavChild[] | 'agent-types'
  /** 二级目录语义(有 children 时必须指定) */
  childMode?: 'switch' | 'anchor'
  /** 入口可见性(返回 false 时一级与其二级都不渲染) */
  enabled?: () => boolean
}

/** 练习设置二级分组 id(与 PracticeSettingsPanel 的 section id 必须一致) */
export const PRACTICE_SECTION_IDS = ['generation', 'learning-topics', 'data'] as const

/**
 * 设置页目录(顺序即渲染顺序)
 *
 * 账户/模型/协作策略是单表单或单列表,不拆二级;协作策略还有 dirty 离页确认,
 * 同路由 hash 切换不会触发弹窗,是保持一层结构的额外理由。
 */
export const SETTINGS_NAV: SettingsNavItem[] = [
  { path: '/settings/account', label: '账户设置' },
  { path: '/settings/models', label: '模型设置' },
  {
    path: '/settings/cli',
    label: 'CLI 设置',
    children: 'agent-types',
    childMode: 'switch',
  },
  { path: '/settings/policy', label: '协作策略' },
  {
    path: '/settings/practice',
    label: '练习设置',
    // 'learning-topics' 这个 id 是知识点看板「知识点主题设置」深链的目标,改名需同步迁移
    children: [
      { id: 'generation', label: '出题偏好' },
      { id: 'learning-topics', label: '学习主题' },
      { id: 'data', label: '数据管理' },
    ],
    childMode: 'anchor',
    enabled: () => practiceEnabled.value,
  },
]
