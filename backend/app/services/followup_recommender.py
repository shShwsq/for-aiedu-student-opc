"""探索性追问推荐("你可能还想深入了解"):审查收尾时的后台单轮轻调用。

与 agent2 的缺口建议分工:agent2 suggestions 是经核查的真实缺口;本模块只做
跨维度探索,仅读 **raw user + agent1 对话**(不读 agent2 结论),推测性、无需取证。

设计(与 prompt_polish / memory_summarize 同族的"轻量单次补全"范式):
- 优先 env 默认模型(LLMClient()),强制关闭思考以提速、拿干净正文;
- 宽容解析 JSON,过滤非法项、截断到 MAX_FOLLOWUP_SUGGESTIONS;
- 非空才落库一条 Conversation(role=agent2, type=followup_suggestions),
  content 结构与既有 type=suggestions 一致({"suggestions":[...]}),前端复用
  同一解析器仅按 type 分流;
- **失败静默**:任何异常都 catch + log,绝不抛出,绝不影响任务完成。

调用点在 orchestrator 审查收尾(必须在推 done 关闭事件总线之前,才能经
实时 conversation 事件到达前端;落库同时保证刷新/快照也可见)。
"""
from __future__ import annotations

import json
import logging

from json_repair import repair_json
from sqlalchemy.orm import Session

from app.agents.runtime.conversation import record_conversation
from app.llm.client import LLMClient
from app.models.task import Conversation, Task
from app.prompts.followup import build_followup_messages

logger = logging.getLogger(__name__)

# 送进 LLM 的对话文本上限(超长截断,避免无谓 token 消耗)
MAX_FOLLOWUP_INPUT_CHARS = 8000
# 推荐输出上限(0-3 条短句足够)
FOLLOWUP_MAX_TOKENS = 512
# 最多保留的探索建议条数
MAX_FOLLOWUP_SUGGESTIONS = 3


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

        messages = build_followup_messages(
            task.user_input or "",
            conversation_text[:MAX_FOLLOWUP_INPUT_CHARS],
        )
        llm = client or LLMClient()
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


def _chat(llm: LLMClient, messages: list[dict]) -> str:
    """单轮流式收集正文;关闭思考以提速,调用后恢复原值。"""
    collected: list[str] = []
    original_thinking = llm.enable_thinking
    try:
        llm.enable_thinking = False
        for chunk in llm.chat_stream(messages, max_tokens=FOLLOWUP_MAX_TOKENS):
            if chunk.content_delta:
                collected.append(chunk.content_delta)
            if chunk.finish_reason in ("stop", "length"):
                break
    finally:
        llm.enable_thinking = original_thinking
    return "".join(collected).strip()


def _load_user_agent1_text(db: Session, task_id) -> str:
    """拼接 user 提问/追加消息 + agent1 每轮最终回答,按轮次时间序。

    仅读 role in (user, agent1),**不含 agent2**——探索推荐与可信审查解耦。
    agent1 每轮取最后一条 thinking(即用户看到的回答),与 memory_summarize
    的取数口径一致。
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

    parts: list[str] = []
    if user_lines:
        parts.append("【用户诉求】\n" + "\n".join(user_lines))
    if summaries_by_round:
        answer_blocks = [
            f"【AI助手回答·第 {ridx} 轮】\n{summaries_by_round[ridx]}"
            for ridx in sorted(summaries_by_round.keys())
        ]
        parts.append("\n\n".join(answer_blocks))
    return "\n\n".join(parts).strip()


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
