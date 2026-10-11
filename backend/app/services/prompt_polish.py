"""AI 润色调用编排:构建 LLM 客户端、单轮流式收集文本。

与记忆归纳一致,走「轻量单次补全」而非任务级客户端:优先 env 默认模型
(LLMClient()),不引入 DB 配置耦合。调用方(routers/tasks.py)负责鉴权、
输入校验与异常转 HTTP 状态码。提示词为纯文本资产,见 app/prompts/polish.py。
"""
from __future__ import annotations

import logging

from app.llm.client import LLMClient
from app.prompts.polish import build_polish_messages

logger = logging.getLogger(__name__)

# 送进 LLM 的原文上限(超长截断,避免无谓 token 消耗)
MAX_POLISH_INPUT_CHARS = 4000
# 润色输出上限(任务描述通常几百字足够)
POLISH_MAX_TOKENS = 1024


def polish_prompt(
    text: str,
    scenario_name: str | None = None,
    client: LLMClient | None = None,
) -> str:
    """把用户任务描述润色为更清晰的表述,返回纯文本。

    - 关闭思考开关(thinking)以提速并得到干净的正文输出,调用后恢复原值;
    - client 为 None 时回退 env 默认(与 react_agent/agent2 的 fallback 一致);
    - 任何 LLM 异常向上抛出,由调用方转成 5xx;空返回由调用方回退原文。
    """
    client = client or LLMClient()
    messages = build_polish_messages(text, scenario_name)
    collected: list[str] = []
    original_thinking = client.enable_thinking
    try:
        client.enable_thinking = False
        for chunk in client.chat_stream(messages, max_tokens=POLISH_MAX_TOKENS):
            if chunk.content_delta:
                collected.append(chunk.content_delta)
            if chunk.finish_reason in ("stop", "length"):
                break
    finally:
        client.enable_thinking = original_thinking
    return "".join(collected).strip()
