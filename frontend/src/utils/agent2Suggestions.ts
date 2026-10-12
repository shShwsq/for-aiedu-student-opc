/**
 * 建议追问方向展示派生的纯函数
 *
 * 两组建议来源不同、语义不同,均落库为 `role=agent2` 的一条 JSON 对话,仅按 type 分流:
 * - type=suggestions:检查助手(agent2)**经核查确认的真实缺口**;
 * - type=followup_suggestions:后台推荐器产出的**跨维度探索**("你可能还想深入了解")。
 *
 * 两个提取函数共用同一容错口径:取数组顺序里**最后一次出现**的同类型记录(整块覆盖
 * 语义),解析出可点击的追问文本清单。抽成纯函数便于单测(与 utils/reviewBucket.ts 同族)。
 */
import type { Conversation } from '@/types/task'

/** suggestions / followup_suggestions 对话记录的 content 形态:{"suggestions": [...]} */
interface SuggestionsPayload {
  suggestions?: unknown
}

/**
 * 建议类对话的 type 清单(唯一来源)。
 *
 * 它们都不是「核查过程」,而是给「概览」两组建议卡渲染的数据契约,因此
 * 除了本模块的提取函数,还有两处按这份清单判定:
 * - utils/agent2Timeline.ts:排除后不进核查时间轴(否则落进 `other` 兜底,
 *   把原始 JSON 直接铺给用户);
 * - views/TaskDetailView.vue:侧栏可见性与 rail 角标。
 * 新增建议类 type 只需改这里,不会再漏掉某一处。
 */
export const SUGGESTION_CONV_TYPES = ['suggestions', 'followup_suggestions'] as const

export type SuggestionConvType = (typeof SUGGESTION_CONV_TYPES)[number]

/** 是否建议类对话记录(时间轴排除 / 面板可见性 / 角标共用同一判定) */
export function isSuggestionConv(c: { role?: string; type?: string }): boolean {
  if (c.role !== 'agent2') return false
  return (SUGGESTION_CONV_TYPES as readonly string[]).includes(c.type ?? '')
}

/**
 * 从指定 type 的 agent2 记录里取最新一条,解析文本清单(0-N 条)。
 *
 * 容错口径(全部静默返回空数组,不打扰用户):
 * - 无匹配记录(单 agent 模式 / 尚未审查完成);
 * - content 不是合法 JSON(老数据 / 异常形态);
 * - suggestions 不是数组;
 * - 数组内的非字符串项与纯空白项被过滤。
 */
function pickLatestByType(
  conversations: Conversation[] | undefined | null,
  type: SuggestionConvType,
): string[] {
  let latest: Conversation | null = null
  for (const c of conversations ?? []) {
    if (c.role === 'agent2' && c.type === type) latest = c
  }
  if (!latest) return []
  try {
    const payload = JSON.parse(latest.content) as SuggestionsPayload
    if (!Array.isArray(payload.suggestions)) return []
    return payload.suggestions.filter(
      (s): s is string => typeof s === 'string' && !!s.trim(),
    )
  } catch {
    return []
  }
}

/** 检查助手发现的缺口(type=suggestions)。 */
export function extractSuggestions(
  conversations: Conversation[] | undefined | null,
): string[] {
  return pickLatestByType(conversations, 'suggestions')
}

/** 你可能还想深入了解:探索性追问(type=followup_suggestions)。 */
export function extractExploreSuggestions(
  conversations: Conversation[] | undefined | null,
): string[] {
  return pickLatestByType(conversations, 'followup_suggestions')
}
