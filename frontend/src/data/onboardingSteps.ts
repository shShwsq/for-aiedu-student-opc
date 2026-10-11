/**
 * 新手引导步骤文案与锚点配置(单一数据源)
 *
 * 设计:
 * - 所有引导文案集中在本文件,UI 组件不内嵌任何用户可见文字,便于统一审阅/翻译/调整。
 * - 锚点 selector 用 [data-onboarding="xxx"] 属性匹配,比 class 名稳定(不随样式重构漂移)。
 *   各视图只需在对应元素上添加 data-onboarding="xxx" 属性即可被引导定位。
 * - 路由驱动按需触发:每个步骤声明所属路由名(route),引导组件在路由变化时按需播放
 *   该路由对应的未读步骤子集,而非一次性播完整套(避免用户疲劳)。
 * - 跨页面步骤切换:同一路由内的步骤连续播放;路由切换后由新路由的 onboarding
 *   watcher 决定是否启动该路由的引导。
 * - 版本号 ONBOARDING_VERSION:引导文案/步骤结构有大改时递增,老用户的 completed 标记作废,
 *   下次登录会重新看到引导(只影响"已读过"状态,不影响其他数据)。
 */
import type { RouteLocationRaw } from 'vue-router'

import { practiceEnabled } from '@/composables/useFeatures'

/** 气泡相对目标元素的位置 */
export type OnboardingPlacement = 'top' | 'bottom' | 'left' | 'right'

/** 单个引导步骤定义 */
export interface OnboardingStep {
  /** 步骤唯一 id(也作为 localStorage 已读记录的 key 段) */
  id: string
  /** 锚点元素的 data-onboarding 属性值;空字符串表示居中浮层(无目标元素) */
  target: string
  /** 所属路由名(对应 router/index.ts 的 name);引导组件按路由分组播放 */
  route: string
  /** 气泡标题 */
  title: string
  /** 气泡正文(纯文本,不解析 HTML) */
  content: string
  /** 气泡相对目标元素的位置;默认 'bottom' */
  placement?: OnboardingPlacement
  /** 仅练习功能启用(PRACTICE_ENABLED=true)时包含此步骤 */
  requiresPractice?: boolean
  /** 练习功能启用时覆盖 content(同一锚点在两种部署下的不同文案) */
  contentWhenPractice?: string
  /** 引导定位前需要激活的侧栏标签页 key(如 'results');用于 tab 布局下目标元素隐藏时自动切换 */
  activateTab?: string
}

/**
 * 引导版本号。步骤结构或文案有大改时递增,使老用户重新看到引导。
 * - v1: 首版,覆盖 HomeView / TaskCreateView / TaskDetailView / AppHeader。
 * - v2: 主导航文案对齐实际 7 项(补「技能管理」),并补充主题切换入口说明。
 * - v3: 感知练习功能开关(PRACTICE_ENABLED):启用部署新增「自适应练习」介绍步骤,
 *       home-nav 文案补全该导航项;关闭部署步骤与文案保持 v2 不变。
 * - v4: 首页改为工作台布局(操作行 + 最近任务列表,欢迎卡/特性介绍区移除):
 *       欢迎步骤锚点从欢迎卡改到操作行,新增「最近任务」步骤。
 * - v5: 核查与结果侧栏改为分段标签页布局(概览/核查/结果),
 *       新增 activateTab 机制保证引导定位前自动切换 tab。
 */
export const ONBOARDING_VERSION = 5

/**
 * 全部引导步骤(按路由分组,组内按顺序播放)。
 *
 * 路由划分:
 * - home:        首页欢迎 + CTA + 最近任务 + 历史任务栏 + 主导航 + 账号按钮(6 步,练习启用时 +1)
 * - task-create: 场景 + 模型 + 执行器 + 输入 + 发送(5 步)
 * - task-detail: 结果清单 + 对话流 + 暂停 + 历史切换(4 步)
 * - settings:    账号设置入口说明(1 步,从 home 阶段末尾的齿轮步骤跳转进入)
 *
 * 注意:settings 路由的步骤仅用于用户主动进入 /settings 时播放;
 * 主流程中"齿轮按钮在哪里"的说明放在 home 路由最后一步,引导用户认识入口位置,
 * 而非真的跳进 /settings(那会离开当前上下文)。
 */
export const ONBOARDING_STEPS: readonly OnboardingStep[] = [
  // ---- 路由:home(首页) ----
  {
    id: 'home-welcome',
    target: 'home-actions',
    route: 'home',
    placement: 'bottom',
    title: '欢迎使用 SecondLook',
    content:
      '这是一个双智能体协作系统:AI助手 负责调用工具执行任务,检查助手 负责质检审查结果、必要时追问补全。下面用 30 秒带你认识主要入口。',
  },
  {
    id: 'home-cta',
    target: 'home-cta',
    route: 'home',
    placement: 'right',
    title: '提交新任务',
    content: '点这里进入任务提交页,描述你的需求并选择执行器。',
  },
  {
    id: 'home-recent-tasks',
    target: 'home-recent-tasks',
    route: 'home',
    placement: 'right',
    title: '最近任务',
    content:
      '首页直接展示最近的任务:点击任意一行进入任务详情,查看对话流与检查助手的质检结果;「展开全部任务」可打开左侧历史任务栏查找更早的任务。',
  },
  {
    id: 'home-workspace',
    target: 'home-workspace-toggle',
    route: 'home',
    placement: 'right',
    title: '历史任务',
    content: '左上角按钮可展开/折叠历史任务侧栏,方便随时切换之前的任务。',
  },
  {
    id: 'home-nav',
    target: 'app-header-nav',
    route: 'home',
    placement: 'bottom',
    title: '主导航',
    content:
      '顶栏可切换:模型设置(配置 LLM)、CLI 设置(外部 CLI 凭据)、智能体策略(检查助手启停 / 验证权限 / CLI 命令确认)、技能管理(上传自定义技能)、记忆管理(用户偏好 / 全局 / 项目记忆)。',
    contentWhenPractice:
      '顶栏可切换:模型设置(配置 LLM)、CLI 设置(外部 CLI 凭据)、智能体策略(检查助手启停 / 验证权限 / CLI 命令确认)、技能管理(上传自定义技能)、自适应练习(按掌握度出题复习)、记忆管理(用户偏好 / 全局 / 项目记忆)。',
  },
  {
    id: 'home-practice',
    target: 'app-header-nav-practice',
    route: 'home',
    placement: 'bottom',
    title: '自适应练习',
    content:
      '根据你的掌握度自动生成练习题,任务完成后可进入练习巩固知识。导航项右上角的红色数字表示到期待复习的题目数。',
    requiresPractice: true,
  },
  {
    id: 'home-settings',
    target: 'app-header-settings',
    route: 'home',
    placement: 'bottom',
    title: '账号与辅助按钮',
    content:
      '右侧依次为:问号(帮助文档弹窗)、主题切换(浅色 / 深色 / 跟随系统)、齿轮(账号设置:修改密码、GitHub/Gitee 绑定、删除账号)、登出。账号设置不在主导航中,只能从这里进入。',
  },

  // ---- 路由:task-create(提交任务) ----
  {
    id: 'create-scenario',
    target: 'create-scenario',
    route: 'task-create',
    placement: 'bottom',
    title: '选择场景',
    content: '场景提供预设 prompt 模板,选择后可自动填入输入框;也可自由输入覆盖模板。',
  },
  {
    id: 'create-react-executor',
    target: 'create-react-executor',
    route: 'task-create',
    placement: 'right',
    title: '执行器(AI助手)',
    content:
      '选择执行器:内置 AI助手可在此选模型(留空则用「高级设置」里的评估模型);若已配置外部 CLI(Qoder / DeepSeek / Codex),可在此切换,模型由 CLI 账号或环境变量管理。',
  },
  {
    id: 'create-user-model',
    target: 'create-user-model',
    route: 'task-create',
    placement: 'right',
    title: '高级设置',
    content:
      '点开可配置检查助手(启停 / 评估模型 / 允许自行验证)与执行命令确认模式。检查助手默认在后台质检 AI助手 的产出;关闭后进入单 agent 模式,AI助手 跑 1 轮直接产出结果。',
  },
  {
    id: 'create-input',
    target: 'create-input',
    route: 'task-create',
    placement: 'top',
    title: '任务描述',
    content: '在这里描述你的任务需求,尽量具体:目标、范围、约束、期望产物格式。',
  },
  {
    id: 'create-send',
    target: 'create-send',
    route: 'task-create',
    placement: 'left',
    title: '提交任务',
    content: '填好仓库与分支后点这里提交,后端会立即返回 task_id 并跳转到任务详情页观看实时进度。',
  },

  // ---- 路由:task-detail(任务详情) ----
  {
    id: 'detail-results',
    target: 'detail-results',
    route: 'task-detail',
    placement: 'right',
    activateTab: 'results',
    title: '结果清单',
    content:
      '任务执行完成后,产出的结果会按维度分组显示在这里(安全审计场景按严重程度,代码审查场景按类别)。当前任务还没结果,等你提交第一个任务后回到这里就能看到。',
  },
  {
    id: 'detail-conversation',
    target: 'detail-conversation',
    route: 'task-detail',
    placement: 'top',
    title: '协作对话流',
    content:
      '任务运行时,这里实时显示你和 AI助手的对话:正在跑的"执行过程"和 AI 思考会自动展开,结束后自动收起(点一下可以钉住你想看的状态),每轮最终回答则像聊天消息一样一直直接可见。',
  },
  {
    id: 'detail-pause',
    target: 'detail-pause',
    route: 'task-detail',
    placement: 'bottom',
    title: '暂停 / 恢复',
    content: '任务运行中可随时点这里暂停,补充信息后恢复执行;暂停期间可向智能体追加指令。',
  },
  {
    id: 'detail-workspace',
    target: 'detail-workspace-toggle',
    route: 'task-detail',
    placement: 'right',
    title: '历史任务切换',
    content: '左上角按钮展开侧栏,可在多个任务之间快速切换。',
  },
] as const

/**
 * 取指定路由下的所有引导步骤(按定义顺序)。
 * 引导组件在进入某路由时调用,若返回非空数组且该路由未标记完成,则启动播放。
 */
export function getStepsForRoute(routeName: string): OnboardingStep[] {
  return (
    ONBOARDING_STEPS
      // 练习开关感知:requiresPractice 步骤仅在练习启用部署中播放
      .filter((s) => s.route === routeName && (!s.requiresPractice || practiceEnabled.value))
      // 文案覆盖:练习启用时用 contentWhenPractice 替换 content(浅拷贝,不改原定义)
      .map((s) =>
        practiceEnabled.value && s.contentWhenPractice
          ? { ...s, content: s.contentWhenPractice }
          : s,
      )
  )
}

/**
 * 用于 router.push 的路由跳转目标(从步骤定义派生)。
 * 预留扩展:当前主流程不跨页面跳转(改为按路由分组播放),此函数保留供"重看引导"等场景使用。
 */
export function toRouteLocation(step: OnboardingStep): RouteLocationRaw {
  return { name: step.route }
}
