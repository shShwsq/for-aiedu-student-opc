/**
 * resolveNavChildren / resolveActiveChildId / isNavItemActive 单元测试
 *
 * 覆盖设置页二级目录的判定逻辑:
 * 1. 子项解析:静态数组 / 'agent-types' 动态映射 / 无 children
 * 2. 当前子项:hash 命中、无 hash 回退第一项、未知 hash 回退第一项、非当前路由返回空
 * 3. 目录数据自检:CLI 走动态子项,练习三段 id 与深链锚点约定一致
 */
import { describe, expect, it } from 'vitest'

import {
  MODEL_SECTION_IDS,
  PRACTICE_SECTION_IDS,
  SETTINGS_NAV,
} from '@/data/settingsNav'
import type { AgentTypeMeta } from '@/types/agent_configs'
import { isNavItemActive, resolveActiveChildId, resolveNavChildren } from './settingsNav'
import { editTarget } from './modelSlots'

function agentMeta(agent_type: string, display_name: string): AgentTypeMeta {
  return { agent_type, display_name } as AgentTypeMeta
}

const cliItem = SETTINGS_NAV.find((item) => item.path === '/settings/cli')!
const practiceItem = SETTINGS_NAV.find((item) => item.path === '/settings/practice')!
const accountItem = SETTINGS_NAV.find((item) => item.path === '/settings/account')!
const modelsItem = SETTINGS_NAV.find((item) => item.path === '/settings/models')!

const AGENTS = [
  agentMeta('qoder_cli', 'Qoder CLI'),
  agentMeta('deepseek_cli', 'DeepSeek CLI'),
  agentMeta('codex_cli', 'Codex CLI'),
]

describe('resolveNavChildren', () => {
  it("'agent-types' 按后端注册表映射为子项", () => {
    expect(resolveNavChildren(cliItem, AGENTS)).toEqual([
      { id: 'qoder_cli', label: 'Qoder CLI' },
      { id: 'deepseek_cli', label: 'DeepSeek CLI' },
      { id: 'codex_cli', label: 'Codex CLI' },
    ])
  })

  it('注册新 agent 时子项自动增加(前端无需改动)', () => {
    const withNew = [...AGENTS, agentMeta('aider_cli', 'Aider CLI')]
    expect(resolveNavChildren(cliItem, withNew)).toHaveLength(4)
  })

  it('类型列表尚未加载时返回空数组', () => {
    expect(resolveNavChildren(cliItem, [])).toEqual([])
  })

  it('静态 children 原样返回,未声明时返回空数组', () => {
    expect(resolveNavChildren(practiceItem, [])).toHaveLength(3)
    expect(resolveNavChildren(accountItem, AGENTS)).toEqual([])
  })
})

describe('resolveActiveChildId', () => {
  const children = resolveNavChildren(cliItem, AGENTS)

  it('hash 命中子项时返回该子项', () => {
    expect(resolveActiveChildId(cliItem, children, '/settings/cli', '#codex_cli'))
      .toBe('codex_cli')
  })

  it('无 hash 回退第一个子项(进入一级页即点亮默认项)', () => {
    expect(resolveActiveChildId(cliItem, children, '/settings/cli', '')).toBe('qoder_cli')
    expect(resolveActiveChildId(cliItem, children, '/settings/cli', undefined)).toBe('qoder_cli')
  })

  it('未知 hash(书签失效/类型下线)回退第一个子项', () => {
    expect(resolveActiveChildId(cliItem, children, '/settings/cli', '#foo')).toBe('qoder_cli')
  })

  it('非当前路由返回空串(折叠项不参与高亮)', () => {
    expect(resolveActiveChildId(cliItem, children, '/settings/account', '#codex_cli')).toBe('')
  })

  it('无二级目录的一级项返回空串', () => {
    expect(resolveActiveChildId(accountItem, [], '/settings/account', '')).toBe('')
  })
})

describe('isNavItemActive', () => {
  it('只在 path 完全一致时为真(hash 不参与判断)', () => {
    expect(isNavItemActive(cliItem, '/settings/cli')).toBe(true)
    expect(isNavItemActive(cliItem, '/settings/practice')).toBe(false)
  })
})

describe('SETTINGS_NAV 数据自检', () => {
  it('练习三段 id 与面板 section id(含知识点看板深链锚点)保持一致', () => {
    const children = resolveNavChildren(practiceItem, [])
    expect(children.map((child) => child.id)).toEqual([...PRACTICE_SECTION_IDS])
    expect(children.map((child) => child.id)).toContain('learning-topics')
  })

  it('模型设置两段 id 与容器里 v-show 的两块一致,且默认落在模型清单', () => {
    const children = resolveNavChildren(modelsItem, [])
    expect(children.map((child) => child.id)).toEqual([...MODEL_SECTION_IDS])
    expect(children.map((child) => child.label)).toEqual(['模型清单', '任务模型默认'])
    // 无 hash(旧书签与 /models 重定向)仍进模型清单:子项须把 configs 排首位
    expect(resolveActiveChildId(modelsItem, children, '/settings/models', undefined))
      .toBe('configs')
    expect(resolveActiveChildId(modelsItem, children, '/settings/models', '#defaults'))
      .toBe('defaults')
  })

  it('模型槽位的编辑深链落在存在的一级项上', () => {
    // 后端 model_slots 回传的 edit_path 指错会静默跳到设置页首页,这里把住一级路由
    const paths = SETTINGS_NAV.map((item) => item.path)
    const targets = [
      '/settings/practice#generation',
      '/settings/memory',
      '/settings/policy',
    ]
    for (const target of targets) {
      expect(paths).toContain(editTarget(target).path)
    }
  })

  it('每个有二级目录的一级项都声明了 childMode', () => {
    for (const item of SETTINGS_NAV) {
      if (item.children) expect(item.childMode).toMatch(/^(switch|anchor)$/)
    }
  })

  it('练习设置入口受功能开关控制', () => {
    expect(practiceItem.enabled).toBeTypeOf('function')
    expect(accountItem.enabled).toBeUndefined()
  })
})
