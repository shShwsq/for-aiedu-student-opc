/**
 * 建议追问方向(suggestions)展示派生的纯函数
 *
 * 从 agent2 落库的 `role=agent2 && type=suggestions` 对话里取**最新一条**
 * (按数组顺序的最后一次出现,旧版整块覆盖语义),解析出可点击的追问文本清单。
 *
 * 原先内联在 Agent2Panel,现随侧栏分段重构上移到视图层的「概览」段,
 * 抽成纯函数便于单测(与 utils/reviewBucket.ts 同族)。
 */
import type { Conversation } from '@/types/task'

/** suggestions 对话记录的 content 形态:{"suggestions": [...]} */
interface SuggestionsPayload {
  suggestions?: unknown
}

/**
 * 提取建议追问文本(0-N 条)。
 *
 * 容错口径(全部静默返回空数组,不打扰用户):
 * - 无 type=suggestions 记录(单 agent 模式 / 尚未审查完成);
 * - content 不是合法 JSON(老数据 / 异常形态);
 * - suggestions 不是数组;
 * - 数组内的非字符串项与纯空白项被过滤。
 */
export function extractSuggestions(
  conversations: Conversation[] | undefined | null,
): string[] {
  let latest: Conversation | null = null
  for (const c of conversations ?? []) {
    if (c.role === 'agent2' && c.type === 'suggestions') latest = c
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
