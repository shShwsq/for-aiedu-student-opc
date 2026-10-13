/**
 * 迭代内工具项 → 渲染行(纯函数,便于单测)。
 *
 * 从 TaskDetailView 抽出:plan step 组与子任务块共用同一套工具渲染行构造,
 * 抽到 utils 后可在 vitest 里直接断言(仓库约定:视图不挂载,只测纯函数)。
 *
 * 四形态:compact(浏览型单行摘要)/ agent(子智能体卡片)/ toolpair(普通工具卡片)/ plain(兜底)。
 */
import {
  buildToolSegments,
  buildToolSummary,
  parseAgentTrace,
  toolFileTargetOf,
  type ToolItem,
} from './toolSummary'
import { renderMarkdown } from './markdown'

/** 工具渲染行所需的最小项结构(视图的 DisplayItem 满足;仅取用到的字段) */
export interface ToolRowItem extends ToolItem {
  /** 是否流式思考项(流式项的身份标在 streaming.facet 上) */
  is_streaming?: boolean
  /** 分面名(内置 react 子任务并行;落库行在顶层) */
  facet?: string
  /** 流式卡(仅实时流式项有) */
  streaming?: { facet?: string }
}

/** 工具渲染行:compact 单行摘要 / agent 子智能体卡片 / toolpair 普通工具卡片 / plain 兜底。
 * 字段扁平化避免模板内联合类型收窄 */
export interface ToolRenderRow<T extends ToolRowItem = ToolRowItem> {
  key: string
  kind: 'compact' | 'agent' | 'toolpair' | 'plain'
  /** compact/agent/toolpair:tool_call id(展开状态 key) */
  callId: string
  /** compact:单行摘要;agent/toolpair:卡片标题 */
  summary: string
  /** compact:可跳转的文件路径(工作区相对路径,无则空串) */
  filePath: string
  /** compact:摘要中展示的文件路径文本 */
  fileDisplay: string
  /** compact:摘要拆分前后缀(filePath 非空时,摘要 = prefix + fileDisplay + suffix) */
  summaryPrefix: string
  summarySuffix: string
  /** 是否已有结果(执行中为 false) */
  hasResult: boolean
  /** agent 卡片专用:只拿到"后台已启动"回执(结果尚未回报)。
   *  它不该按"已完成"折叠,也不能被当成报告正文渲染 */
  agentAsync: boolean
  /** 结果文本(compact 轻量展开 / toolpair 结果块) */
  resultContent: string
  /** agent/toolpair:调用参数 detail(展开后等宽块) */
  callDetail: string
  /** agent:内部思考(<think> 块内容) */
  agentThink: string
  /** agent:Markdown 渲染的报告正文 HTML */
  agentBodyHtml: string
  /** plain:该段内的工具项(兜底原渲染) */
  items: T[]
}

/** 子智能体块展开/切换所需的最小行信息(跨组件 emit 载荷,避开泛型摩擦) */
export interface AgentBlockRowLike {
  callId: string
  hasResult: boolean
  agentAsync: boolean
}

/** 取项的分面名(子任务 label):流式项读流式卡上的 facet,落库行读顶层 facet */
export function itemFacetOf(item: ToolRowItem): string | undefined {
  return item.is_streaming ? item.streaming?.facet : item.facet
}

/** tool_call content 拆分为 intent(首行,剥 [tool_name] 标签)与 detail(其后内容) */
export function callPartsOf(call: { content?: string }): { intent: string; detail: string } {
  const content = call.content || ''
  const idx = content.indexOf('\n')
  const rawIntent = idx < 0 ? content : content.slice(0, idx)
  return {
    intent: rawIntent.replace(/\s*\[\w+\]$/, ''),
    detail: idx < 0 ? '' : content.slice(idx + 1),
  }
}

/**
 * 迭代内工具项拆分为渲染行:
 * compact(浏览型单行摘要)、agent(子智能体卡片,Markdown 报告)、
 * toolpair(普通工具,调用+结果整体一张折叠卡)。
 */
export function toolRowsOf<T extends ToolRowItem>(
  iter: { id: string; toolItems: T[] },
): ToolRenderRow<T>[] {
  const empty = {
    resultContent: '',
    callDetail: '',
    agentThink: '',
    agentBodyHtml: '',
    agentAsync: false,
    filePath: '',
    fileDisplay: '',
    summaryPrefix: '',
    summarySuffix: '',
    items: [] as T[],
  }
  return buildToolSegments(iter.toolItems).map((seg, idx): ToolRenderRow<T> => {
    if (seg.kind === 'plain') {
      return {
        key: `${iter.id}-plain-${idx}`,
        kind: 'plain',
        callId: '',
        summary: '',
        hasResult: false,
        ...empty,
        items: seg.items,
      }
    }
    const callId = seg.call.id
    const parts = callPartsOf(seg.call)
    if (seg.kind === 'compact') {
      // 读文件工具提取跳转目标:摘要中路径文本包成链接,点击在工作区文件树打开
      const target = toolFileTargetOf(seg.call, seg.result)
      return {
        key: callId,
        kind: 'compact',
        callId,
        summary: buildToolSummary(seg.call, seg.result),
        hasResult: !!seg.result,
        ...empty,
        resultContent: seg.result?.content || '',
        filePath: target?.path || '',
        fileDisplay: target?.display || '',
        summaryPrefix: target?.prefix || '',
        summarySuffix: target?.suffix || '',
      }
    }
    if (seg.kind === 'agent') {
      const trace = seg.result ? parseAgentTrace(seg.result.content || '') : null
      // 异步派生回执:不是结论,必须标"后台未回报"(回归:旧版因 trace.status 为
      // null 而落到 else,给一张正文只是启动回执的卡片打上"已完成")
      const asyncPending = !!trace?.asyncPending
      // 标题:子任务意图 + 子智能体类型/状态后缀
      const tags: string[] = []
      if (trace?.subType) tags.push(trace.subType)
      if (!seg.result) tags.push('执行中…')
      else if (asyncPending) tags.push(trace?.agentId ? `后台未回报 ${trace.agentId}` : '后台未回报')
      else if (trace?.status && trace.status !== 'completed') tags.push(trace.status)
      else tags.push('已完成')
      return {
        key: callId,
        kind: 'agent',
        callId,
        summary: `🤖 ${parts.intent}${tags.length ? ' · ' + tags.join(' · ') : ''}`,
        hasResult: !!seg.result,
        ...empty,
        agentAsync: asyncPending,
        callDetail: parts.detail,
        agentThink: trace?.think || '',
        agentBodyHtml: trace ? renderMarkdown(trace.body) : '',
      }
    }
    // toolpair:普通工具(写操作/非只读命令等),调用+结果整体一张折叠卡
    return {
      key: callId,
      kind: 'toolpair',
      callId,
      summary: `🔧 ${parts.intent}`,
      hasResult: !!seg.result,
      ...empty,
      callDetail: parts.detail,
      resultContent: seg.result?.content || '',
    }
  })
}
