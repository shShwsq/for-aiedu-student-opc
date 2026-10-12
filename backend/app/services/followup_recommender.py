"""探索性追问推荐("你可能还想深入了解"):审查收尾时的后台单轮轻调用。

与 agent2 的缺口建议分工:agent2 suggestions 是经核查的真实缺口;本模块只做
跨维度探索,仅读 **raw user + agent1 对话**(不读 agent2 结论),推测性、无需取证。

设计(与 prompt_polish / memory_summarize 同族的"轻量单次补全"范式):
- 优先 env 默认模型(LLMClient()),强制关闭思考以提速、拿干净正文,并给这次
  可选调用套**硬时限**(见 FOLLOWUP_TIMEOUT_SECONDS);
- 对话文本按预算装填:保留最新几轮回答与最新几条用户消息,装不下就丢最老的
  整块(见 _assemble_within_budget);
- 宽容解析 JSON,过滤非法项、截断到 MAX_FOLLOWUP_SUGGESTIONS;
- 非空才落库一条 Conversation(role=agent2, type=followup_suggestions),
  content 结构与既有 type=suggestions 一致({"suggestions":[...]}),前端复用
  同一解析器仅按 type 分流;
- **失败静默**:任何异常都 catch + log,绝不抛出,绝不影响任务完成。

调用点在 orchestrator 的三条收尾路径(启用检查助手的后台审查、单 agent 初始轮、
单 agent resume 轮),且必须在推 done 关闭事件总线之前——才能经实时 conversation
事件到达前端(落库同时保证刷新/快照也可见)。
"""
from __future__ import annotations

import json
import logging
import time

import httpx
from json_repair import repair_json
from sqlalchemy.orm import Session

from app.agents.runtime.conversation import record_conversation
from app.llm.client import LLMClient
from app.models.task import Conversation, Task
from app.prompts.followup import build_followup_messages

logger = logging.getLogger(__name__)

# 送进 LLM 的对话文本预算(超预算按语义优先级丢最老的整块,见 _assemble_within_budget)
MAX_FOLLOWUP_INPUT_CHARS = 8000
# 用户消息(含各轮追问)占的份额,其余给 AI 回答
USER_BUDGET_CHARS = 2000
# 单条用户消息 / 单轮 AI 回答的裁剪上限:单项上限低于各自预算 → 某一类只要有
# 内容就至少保留最新那一项,不会被整类清空
USER_LINE_CHARS = 1200
ANSWER_ROUND_CHARS = 3000
# 推荐输出上限(0-3 条短句足够)
FOLLOWUP_MAX_TOKENS = 512
# 最多保留的探索建议条数
MAX_FOLLOWUP_SUGGESTIONS = 3
# 本次轻调用的硬时限(秒):建连/读流/整段流式都受约束。
# LLMClient 沿用 openai SDK 默认(read 600s + 2 次重试),一次挂住的连接就能把
# done 事件与它后面的下游链(记忆归纳/自动出题)拖到分钟级 —— 探索推荐是可选
# 产出,宁缺勿滥,也不能拖住收尾。429 退避 sleep 仍按全局
# settings.LLM_RATE_LIMIT_MAX_RETRIES(封顶 60s/次):那是服务商侧节奏,不在本模块能收敛的范围。
FOLLOWUP_TIMEOUT_SECONDS = 20.0


def recommend_followups(
    task: Task, db: Session, round_idx: int,
    client: LLMClient | None = None,
) -> None:
    """基于 user + agent1 对话推荐 0-3 条探索性追问,非空才落库。

    永不抛异常:整段包 try/except,失败仅记日志(失败静默)。
    前置:任务已成功完成且未被新流取代(由调用方保证)。
    """
    try:
        conversation_text = _load_user_agent1_text(db, task.id)
        if not conversation_text:
            return

        messages = build_followup_messages(task.user_input or "", conversation_text)
        # 传入 client(测试替身/调用方指定模型)时不改其配置;自建才套硬超时
        llm = client if client is not None else _new_llm_client()
        raw = _chat(llm, messages)
        suggestions = _parse_suggestions(raw)
        if not suggestions:
            return

        record_conversation(
            db, task,
            round_idx=round_idx,
            role="agent2",
            type="followup_suggestions",
            content=json.dumps(
                {"suggestions": suggestions, "source": "explore"},
                ensure_ascii=False,
            ),
        )
        logger.info(
            f"[task={task.id}] 探索性追问推荐落库:{len(suggestions)} 条(第 {round_idx} 轮)"
        )
    except Exception as e:  # noqa: BLE001 - 失败静默,不影响任务完成
        logger.warning(f"[task={task.id}] 探索性追问推荐失败(忽略): {e}")


def _new_llm_client() -> LLMClient:
    """构造带硬超时的客户端(仅本模块自建的可选调用)。

    LLMClient 没有 per-call 超时入口,这里用 openai SDK 的 with_options 覆盖
    timeout/max_retries。**不改 LLMClient 默认值**:agent1/agent2 的长思考流
    依赖宽松的首 token 等待,全局收紧超时会误伤正常审查。
    套不上(替身客户端/属性变动)也照常推荐 —— 超时只是止血,不是前置条件。
    """
    llm = LLMClient()
    try:
        llm.client = llm.client.with_options(
            timeout=httpx.Timeout(FOLLOWUP_TIMEOUT_SECONDS, connect=5.0),
            max_retries=0,
        )
    except Exception as e:  # noqa: BLE001 - 超时没套上也照常尝试
        logger.warning(f"探索推荐客户端设超时失败(忽略,沿用默认): {e}")
    return llm


def _chat(llm: LLMClient, messages: list[dict]) -> str:
    """单轮流式收集正文;关闭思考以提速,调用后恢复原值,整段受硬时限约束。

    时限分两条链(缺一都会留出无上限的等待窗口):
    - 建连与两次 chunk 之间的空转:httpx 读超时(见 _new_llm_client);
    - 整段"细水长流"型响应:用 chat_stream 已有的 stop_check 在 chunk 边界到点
      收流(已累积的输出照常参与解析,拿不到完整 JSON 则由解析环节返回空)。
    """
    collected: list[str] = []
    original_thinking = llm.enable_thinking
    deadline = time.monotonic() + FOLLOWUP_TIMEOUT_SECONDS
    try:
        llm.enable_thinking = False
        for chunk in llm.chat_stream(
            messages,
            max_tokens=FOLLOWUP_MAX_TOKENS,
            stop_check=lambda: time.monotonic() >= deadline,
        ):
            if chunk.content_delta:
                collected.append(chunk.content_delta)
            if chunk.finish_reason in ("stop", "length"):
                break
    finally:
        llm.enable_thinking = original_thinking
    return "".join(collected).strip()


def _load_user_agent1_text(db: Session, task_id) -> str:
    """拼接 user 提问/追加消息 + agent1 每轮最终回答,按轮次时间序(已按预算装填)。

    仅读 role in (user, agent1),**不含 agent2**——探索推荐与可信审查解耦。
    agent1 每轮取最后一条 thinking(即用户看到的回答),与 memory_summarize
    的取数口径一致。超长时的取舍见 _assemble_within_budget。
    """
    # agent1 各轮总结:role=agent1, type=thinking,每轮取末条
    summaries_by_round: dict[int, str] = {}
    a1_convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "agent1",
            Conversation.type == "thinking",
        )
        .order_by(Conversation.round_idx.asc(), Conversation.created_at.asc())
        .all()
    )
    for c in a1_convs:
        if c.content:
            summaries_by_round[c.round_idx] = c.content

    # user 原始意图 + 追加消息,按轮次/时间序
    user_lines: list[str] = []
    user_convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "user",
            Conversation.type.in_(("question", "message")),
        )
        .order_by(Conversation.round_idx.asc(), Conversation.created_at.asc())
        .all()
    )
    for c in user_convs:
        text = (c.content or "").strip()
        if text:
            user_lines.append(text)

    return _assemble_within_budget(user_lines, summaries_by_round)


def _clip(text: str, limit: int) -> str:
    """单项裁剪:超长截断并加标记(保留开头,够表意即可)"""
    s = (text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


SECTION_SEP = "\n\n"
USER_HEADER = "【用户诉求】\n"


def _fit_blocks(blocks: list[str], budget: int, sep: str) -> list[str]:
    """按预算从前往后装填文本块(blocks[0] 优先级最高,已包含段内分隔开销)。

    第一块无条件保留(各块上限都低于自己的预算,不会因此超纲),后面的
    装不下就整块丢弃 —— 不把最后一块砍半,宁可少一个完整轮次。
    """
    kept: list[str] = []
    used = 0
    for block in blocks:
        cost = len(block) + (len(sep) if kept else 0)
        if kept and used + cost > budget:
            break
        kept.append(block)
        used += cost
    return kept


def _assemble_within_budget(
    user_lines: list[str], answer_by_round: dict[int, str],
) -> str:
    """在 MAX_FOLLOWUP_INPUT_CHARS 预算内拼装对话文本:留最新,整块丢最老。

    旧实现是调用方整段 conversation_text[:MAX]:按时间序拼接后取前缀,等于
    **留下最老几轮、砍掉最新几轮**,与 prompt 里"别把已经答透的维度再列"的
    约束正相反(长对话里推荐器看不到刚发生的几轮)。

    两类内容也不能只靠"改取尾部"解决(那会把整段用户追问切掉,而"用户已问
    过什么"同样是硬约束的依据),故各给一份预算、各自从最新往前装:
    - AI 回答(主预算):判断"哪些维度已答透"的唯一依据,从最新一轮往前;
    - 用户消息(USER_BUDGET_CHARS,含段头):最初的诉求另有 task.user_input
      全文随 prompt 传入,这里丢掉的只是更老的追问。
    预算含段头与段间分隔符,返回值长度没有超纲的例外。
    """
    # 候选按"新旧优先级"倒序装填,输出时再反转回时间正序
    answer_blocks = [
        f"【AI助手回答·第 {ridx} 轮】\n{_clip(answer_by_round[ridx], ANSWER_ROUND_CHARS)}"
        for ridx in sorted(answer_by_round, reverse=True)
    ]
    user_blocks = [_clip(t, USER_LINE_CHARS) for t in reversed(user_lines)]

    answers = _fit_blocks(
        answer_blocks,
        MAX_FOLLOWUP_INPUT_CHARS - USER_BUDGET_CHARS - len(SECTION_SEP),
        SECTION_SEP,
    )
    users = _fit_blocks(
        user_blocks, USER_BUDGET_CHARS - len(USER_HEADER), "\n"
    )

    parts: list[str] = []
    if users:
        parts.append(USER_HEADER + "\n".join(reversed(users)))
    if answers:
        parts.append(SECTION_SEP.join(reversed(answers)))
    return SECTION_SEP.join(parts).strip()


def _parse_suggestions(content: str) -> list[str]:
    """宽容解析推荐 JSON:剥离 ```json``` 包裹 + json_repair 兜底,过滤非法项。

    期望结构:{"suggestions": ["…", "…"]}。解析失败 / 非数组 / 全空 → []。
    """
    text = (content or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(repair_json(text))
    except Exception as e:  # noqa: BLE001 - 解析失败静默返回空
        logger.warning(f"探索性追问推荐 JSON 解析失败(忽略): {e}, raw: {content[:200]}")
        return []

    raw_list = parsed.get("suggestions") if isinstance(parsed, dict) else None
    if not isinstance(raw_list, list):
        return []
    cleaned: list[str] = []
    for item in raw_list:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    return cleaned[:MAX_FOLLOWUP_SUGGESTIONS]
