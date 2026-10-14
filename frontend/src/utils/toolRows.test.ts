/**
 * toolRowsOf / itemFacetOf / callPartsOf 单元测试(纯函数,不挂载视图)。
 *
 * toolRows 从 TaskDetailView 抽出:plan step 组与「子任务」块共用同一套工具渲染行构造,
 * 这里直接断言四形态(compact / agent / toolpair / plain)的字段与 agent 卡片的
 * "后台未回报"诚实态 —— 该分支有历史回归(旧版把启动回执当已完成并自动折叠),
 * 视图层接线另见 taskDetailViewConclusionWiring.test.ts。
 */
import { describe, expect, it } from 'vitest'

import {
  callPartsOf,
  itemFacetOf,
  toolRowsOf,
  type ToolRowItem,
} from './toolRows'

function call(id: string, intent: string): ToolRowItem {
  return { id, type: 'tool_call', content: intent }
}

function result(id: string, content: string, toolCallId?: string): ToolRowItem {
  return { id, type: 'tool_result', content, tool_call_id: toolCallId ?? null }
}

/** 用工具项列表包一个最小迭代喂给 toolRowsOf */
function rowsOf(...toolItems: ToolRowItem[]) {
  return toolRowsOf({ id: 'it-1', toolItems })
}

describe('toolRowsOf:compact(浏览型单行摘要)', () => {
  it('read_file 单行摘要 + 文件跳转字段', () => {
    const rows = rowsOf(
      call('c1', '读取文件 src/main.py [read_file]'),
      result('r1', JSON.stringify({
        path: 'src/main.py', content: '...', total_lines: 356, truncated: false,
      })),
    )
    expect(rows).toHaveLength(1)
    const row = rows[0]
    expect(row.kind).toBe('compact')
    expect(row.callId).toBe('c1')
    expect(row.hasResult).toBe(true)
    expect(row.agentAsync).toBe(false)
    expect(row.summary).toBe('📖 阅读了 src/main.py · 356 行')
    expect(row.filePath).toBe('src/main.py')
    expect(row.fileDisplay).toBe('src/main.py')
    expect(row.summaryPrefix).toBe('📖 阅读了 ')
    expect(row.summarySuffix).toBe(' · 356 行')
    // 摘要 = prefix + display + suffix(compact 包链接时按段拼回)
    expect(row.summaryPrefix + row.fileDisplay + row.summarySuffix).toBe(row.summary)
    expect(row.resultContent).toContain('...')
  })

  it('result 未到达:hasResult=false(执行中),不折叠成空壳', () => {
    const rows = rowsOf(call('c1', '读取文件 a.py [read_file]'))
    expect(rows[0]).toMatchObject({ kind: 'compact', hasResult: false, agentAsync: false })
  })
})

describe('toolRowsOf:agent(子智能体卡片)的诚实态', () => {
  it('异步派生回执:agentAsync=true、hasResult=true、summary 含「后台未回报」', () => {
    const ack = [
      'Async agent launched successfully.',
      'agentId: ageneral-purpose-5f77f8122aee3358 (internal ID - do not mention to user.)',
      'The agent is working in the background. You will be notified automatically when it completes.',
    ].join('\n')
    const rows = rowsOf(
      call('c1', '子任务: 扫描硬编码密钥 [Agent]\n{"prompt": "扫描"}'),
      result('r1', ack, 'c1'),
    )
    expect(rows).toHaveLength(1)
    const row = rows[0]
    expect(row.kind).toBe('agent')
    expect(row.hasResult).toBe(true)
    // 关键:拿到启动回执不等于完成 —— 不能按已完成折叠
    expect(row.agentAsync).toBe(true)
    expect(row.summary).toContain('子任务: 扫描硬编码密钥')
    expect(row.summary).toContain('后台未回报')
    // 回执原文不作为报告正文渲染
    expect(row.agentBodyHtml).not.toContain('Async agent launched')
    expect(row.callDetail).toBe('{"prompt": "扫描"}')
  })

  it('同步完成报告:agentAsync=false、tag「已完成」、正文为 Markdown 报告', () => {
    const body = [
      'agent_id: agent-0',
      'actual_subagent_type: explore',
      'status: completed',
      '',
      '[summary]',
      '## 项目架构分析',
      '核心入口位于 `src/main.py`。',
    ].join('\n')
    const rows = rowsOf(
      call('c1', '子任务: 探索架构 [Agent]'),
      result('r1', body, 'c1'),
    )
    const row = rows[0]
    expect(row.kind).toBe('agent')
    expect(row.agentAsync).toBe(false)
    expect(row.hasResult).toBe(true)
    expect(row.summary).toContain('已完成')
    expect(row.agentBodyHtml).toContain('项目架构分析')
  })

  it('执行中(无 result):tag「执行中…」、hasResult=false', () => {
    const rows = rowsOf(call('c1', '子任务: 探索 [Agent]'))
    expect(rows[0]).toMatchObject({ kind: 'agent', hasResult: false, agentAsync: false })
    expect(rows[0].summary).toContain('执行中…')
  })
})

describe('toolRowsOf:toolpair(普通工具卡片)', () => {
  it('写操作:调用参数 detail + 结果 content', () => {
    const rows = rowsOf(
      call('c1', '写入文件 b.py [write_file]\n{"path": "b.py", "content": "x"}'),
      result('r1', '写入成功', 'c1'),
    )
    const row = rows[0]
    expect(row.kind).toBe('toolpair')
    expect(row.summary).toBe('🔧 写入文件 b.py')
    expect(row.callDetail).toBe('{"path": "b.py", "content": "x"}')
    expect(row.resultContent).toBe('写入成功')
    expect(row.hasResult).toBe(true)
    expect(row.agentAsync).toBe(false)
  })
})

describe('toolRowsOf:plain(落单 result 兜底)', () => {
  it('孤儿 tool_result 归入 plain,原始项进 items', () => {
    const orphan = result('r1', '落单结果')
    const rows = rowsOf(orphan)
    expect(rows).toHaveLength(1)
    expect(rows[0].kind).toBe('plain')
    expect(rows[0].items).toHaveLength(1)
    expect(rows[0].items[0].id).toBe('r1')
  })
})

describe('itemFacetOf:子任务名读取(流式卡 vs 落库行)', () => {
  it('流式项读 streaming.facet', () => {
    const item: ToolRowItem = {
      id: 's1', type: 'thinking', is_streaming: true,
      streaming: { facet: '硬编码密钥扫描' },
    }
    expect(itemFacetOf(item)).toBe('硬编码密钥扫描')
  })

  it('落库行读顶层 facet', () => {
    const item: ToolRowItem = { id: 'h1', type: 'thinking', facet: 'SQL 注入检查' }
    expect(itemFacetOf(item)).toBe('SQL 注入检查')
  })

  it('两者皆无返回 undefined(串行/老任务)', () => {
    expect(itemFacetOf({ id: 'p1', type: 'thinking' })).toBeUndefined()
    // 流式项但 streaming.facet 缺失也回退 undefined(不读顶层 facet)
    expect(itemFacetOf({ id: 'p2', type: 'thinking', is_streaming: true, facet: '忽略' }))
      .toBeUndefined()
  })
})

describe('callPartsOf:首行剥 [tool_name] 标签 + 其后为 detail', () => {
  it('有换行:detail 为换行之后内容', () => {
    expect(callPartsOf({ content: '读取文件 a.py [read_file]\n{"file_path": "a.py"}' }))
      .toEqual({ intent: '读取文件 a.py', detail: '{"file_path": "a.py"}' })
  })

  it('无换行:detail 为空串', () => {
    expect(callPartsOf({ content: '子任务: 探索 [Agent]' }))
      .toEqual({ intent: '子任务: 探索', detail: '' })
  })

  it('无标签:原样保留 intent', () => {
    expect(callPartsOf({ content: '旧数据无标签' }))
      .toEqual({ intent: '旧数据无标签', detail: '' })
  })

  it('空内容:intent/detail 均为空串', () => {
    expect(callPartsOf({})).toEqual({ intent: '', detail: '' })
  })
})
