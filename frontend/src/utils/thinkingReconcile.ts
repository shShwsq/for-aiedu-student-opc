/**
 * 思考流式卡片 ↔ 落库 thinking 记录的对账(纯函数,无 Vue/DOM 依赖)
 *
 * 背景:一次 LLM 调用的思考过程有两条展示载体——
 * 1. 实时流式卡片:SSE thinking_delta 增量按 conv_id 累积(只活在前端内存);
 * 2. 历史记录:调用结束时落库的 Conversation(type=thinking, reasoning/content)。
 *
 * 后端过去为了"卡片已展示"不给第 2 条推 conversation 事件,于是中途离开
 * 任务详情页再回来的用户:增量收不到(thinking_delta 是高频瞬时事件,事件总线
 * 不缓存),落库记录也不推,只能等下一次整页快照 —— 表现就是"思考内容没保存"。
 * 现在后端落库即推 conversation 事件(带 stream_conv_id),本模块负责把
 * 对应的实时卡片退役掉,避免同一段思考在界面上出现两次。
 *
 * 匹配优先级:
 * 1. stream_conv_id 精确匹配(内置 react_agent / CLI 执行器都能给);
 * 2. 文本对账:同 role + 同 round、已结束流式(status!=='streaming'),
 *    且 reasoning 或 content 任一通道"相等或互相包含"(短于阈值的文本
 *    不足以做指纹,避免误伤相邻的相同短句);
 * 3. 都匹配不到则不退役(宁可短暂重复展示,也不吞掉用户能看到的思考)。
 *
 * 第 2 条兜底是必要的:agent2 审查/动态验证的思考链由 `_stream_*_llm` 内部
 * 生成 conv_id,落库侧拿不到它(改函数签名会波及多处测试替身),只能靠文本;
 * 动态验证落库时还会给 content 加 "[验证结果] " 前缀,故用包含而非相等。
 */

/** 流式卡片的最小结构形状(与 TaskDetailView 的 StreamingItem 兼容) */
export interface StreamingCardLike {
  conv_id: string
  round_idx: number
  role: string
  reasoning: string
  content: string
  /** 'streaming' 仍在收增量 / 'done' 已收到 end / 'error' 流式失败 */
  status: string
}

/** 落库 thinking 记录(conversation 事件 data)的最小结构形状 */
export interface ThinkingRecordLike {
  round_idx: number
  role: string
  type: string
  content?: string | null
  reasoning?: string | null
  /** 落库前那次流式调用的 conv_id(老数据/部分智能体没有) */
  stream_conv_id?: string | null
}

/**
 * 文本对账的最小长度:低于它不做包含判断(如"好的""继续"这类极短思考段,
 * 相邻卡片文本可能完全相同或互为子串,误退役会藏掉真实内容)。
 */
const MIN_FINGERPRINT_CHARS = 32

/** 通道文本归一:仅 trim(不折叠内部空白,代码/缩进要保留原样) */
function normalizeText(value?: string | null): string {
  return (value ?? '').trim()
}

/**
 * 两个通道的文本是否算"同一段思考":相等,或一方包含另一方
 * (包含用于后端加前缀/多段合并的场景;太短的文本只做相等判断)
 */
function sameThinkingText(
  cardText: string,
  recordText?: string | null,
): boolean {
  const a = normalizeText(cardText)
  const b = normalizeText(recordText)
  if (!a || !b) return false
  if (a === b) return true
  if (Math.min(a.length, b.length) < MIN_FINGERPRINT_CHARS) return false
  return a.includes(b) || b.includes(a)
}

/** 该 conversation 事件是否为思考落库记录 */
export function isThinkingRecord(record: ThinkingRecordLike): boolean {
  return record.type === 'thinking'
}

/**
 * 找出应随这条落库记录退役的实时卡片 conv_id;无需/无法退役时返回 null。
 *
 * @param cards 当前实时卡片(调用方自己的 Map 值集合,按插入顺序遍历)
 * @param record 刚到达的 type=thinking conversation 事件 data
 */
export function findRetiredCardConvId(
  cards: Iterable<StreamingCardLike>,
  record: ThinkingRecordLike,
): string | null {
  if (!isThinkingRecord(record)) return null

  const list = [...cards]

  // 1) 精确匹配:后端给了这次流式调用的 conv_id
  const exact = record.stream_conv_id
  if (exact && list.some((card) => card.conv_id === exact)) {
    return exact
  }

  // 2) 文本对账:取最后一个候选(同一段文本可能出现在多轮/多卡片里,
  //    最新那张才是刚落库的这段)
  let fallback: string | null = null
  for (const card of list) {
    // 仍在流式的卡片不可能已经落库,不能退役
    if (card.status === 'streaming') continue
    if (card.role !== record.role) continue
    if (card.round_idx !== record.round_idx) continue
    const reasoningHit = sameThinkingText(card.reasoning, record.reasoning)
    const contentHit = sameThinkingText(card.content, record.content)
    if (reasoningHit || contentHit) fallback = card.conv_id
  }
  return fallback
}
