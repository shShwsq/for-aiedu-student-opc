"""共享流式 LLM 调用原语(react_agent / agent2 / verifier_agent 复用)

提取动机:此前三个智能体各自手抄一份"流式调用 + 跨 chunk 工具调用累积"
实现,修复互不传播——react_agent 为 GLM/Qwen 思考模式做的文本 tool_call
兜底解析,agent2 / verifier 没有跟上:这类模型把工具调用写在正文而非
结构化 tool_calls 通道,导致 agent2 审查 JSON 解析失败(审查标记失败)、
verifier 把工具调用文本当验证总结提前返回、PoC 根本没执行。

职责边界:只做无业务语义的运行时原语(收 chunk / 推 thinking_delta /
累积工具调用 / 文本 tool_call 兜底解析 / perf 打点)。循环策略(迭代
上限、工具配额、授权拦截、降级重试)归各智能体,不在此层。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.event_bus import publish
from app.perf import perf_log

logger = logging.getLogger(__name__)


# ============================================================
# 文本 tool_call 兜底解析(GLM/Qwen 等 Hermes 风格)
# ============================================================

# Hermes 风格文本工具调用:特殊标记包裹 JSON 体
# GLM/Qwen 等在思考模式下可能把工具调用写在正文(而非走 OpenAI
# function calling 结构化通道)。正则靠结尾标记锚定,非贪婪 .*?
# 可正确处理嵌套 JSON 对象
_TEXT_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)
_TEXT_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*.*?\s*</tool_call>", re.DOTALL)


def extract_text_tool_calls(content: str) -> list[dict[str, Any]]:
    """从 content 文本解析 Hermes 风格工具调用块,作为结构化 tool_calls 的兜底

    适配 GLM/Qwen 等模型在思考模式下把工具调用写在正文(而非走 OpenAI
    function calling 通道)的情况。每个块解析为一个工具调用,
    JSON 不合法的块跳过。

    返回 [{"id": str, "name": str, "arguments_str": str, "index": int}]
    无匹配返回空列表。
    """
    matches = _TEXT_TOOL_CALL_RE.findall(content)
    if not matches:
        return []

    result: list[dict[str, Any]] = []
    for i, json_str in enumerate(matches):
        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError:
            continue
        name = parsed.get("name")
        if not name:
            continue
        # arguments 字段(部分模型用 parameters)可能是 dict 或 str,统一成 str
        args = parsed.get("arguments", parsed.get("parameters", {}))
        if isinstance(args, dict):
            args_str = json.dumps(args, ensure_ascii=False)
        else:
            args_str = str(args)
        result.append({
            "id": f"text_tc_{i}_{uuid.uuid4().hex[:8]}",
            "name": name,
            "arguments_str": args_str,
            "index": i,
        })
    return result


def strip_tool_call_blocks(content: str) -> str:
    """从 content 剥离 Hermes 风格工具调用文本块

    兜底解析后用于清理 messages 上下文里的 content,避免下一轮 LLM
    重复看到工具调用文本。落库的 thinking 保留原文(便于排查)。
    """
    return _TEXT_TOOL_CALL_BLOCK_RE.sub("", content).strip()


# ============================================================
# 跨 chunk 工具调用累积
# ============================================================


class ToolCallAccumulator:
    """按 index 跨 chunk 累积工具调用(id/name 后续 chunk 补全,arguments 拼接)

    三处原实现各自内联同一段累积逻辑,收敛为一个可复用的小对象。
    """

    def __init__(self) -> None:
        self._acc: dict[int, dict[str, Any]] = {}

    def add(self, deltas: Any) -> None:
        """喂入一次 chat_stream chunk 的 tool_call_deltas(可空)"""
        for tc_delta in deltas or []:
            idx = tc_delta.index
            cur = self._acc.get(idx)
            if cur is None:
                self._acc[idx] = {
                    "id": tc_delta.id or "",
                    "name": tc_delta.name or "",
                    "arguments_str": "",
                    "index": idx,
                }
            else:
                # 后续 chunk 可能补 id / name(理论上第一个 chunk 就有,但保险)
                if tc_delta.id and not cur["id"]:
                    cur["id"] = tc_delta.id
                if tc_delta.name and not cur["name"]:
                    cur["name"] = tc_delta.name
            if tc_delta.arguments_fragment:
                self._acc[idx]["arguments_str"] += tc_delta.arguments_fragment

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """按 index 排序输出完整工具调用列表"""
        return [self._acc[i] for i in sorted(self._acc)]

    def __len__(self) -> int:
        return len(self._acc)


# ============================================================
# 统一流式调用
# ============================================================


@dataclass
class StreamResult:
    """一次流式 LLM 调用的完整结果

    - reasoning / content:完整累积文本
    - tool_calls:完整工具调用列表([{"id","name","arguments_str","index"}])
    - finish_reason:流结束原因('stop' / 'tool_calls' / 'length' 等),
      None 表示异常中断前的状态(异常时直接 raise)
    - conv_id:本次调用的流式卡片标识(前端按此 key 累积 thinking_delta)
    - stopped:调用方 stop_check 命中而提前收流(拿到的是部分输出,
      结果不可用,调用方据此走终止分支;不传 stop_check 时恒为 False)
    """
    conv_id: str
    reasoning: str
    content: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    stopped: bool = False


def stream_llm(
    client: Any,
    messages: list[dict[str, Any]],
    *,
    task_id: Any,
    round_idx: int,
    role: str,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    publish_content: bool = True,
    iteration: int | None = None,
    extra: dict[str, Any] | None = None,
    phase_start: bool = True,
    stop_check: Callable[[], bool] | None = None,
) -> StreamResult:
    """流式调用 LLM:边收 token 边推 thinking_delta 事件给前端

    参数:
        client: LLMClient(已按用户配置构造)
        messages: OpenAI 风格消息序列(system / user / assistant / tool)
        task_id / round_idx / role:事件标注(前端按 role + round 分流)
        tools: 工具定义(None 表示不启用 function calling)
        max_tokens: 单次输出上限(调用方策略不同:react_agent / verifier
            4096;agent2 评估 16384——大 JSON results 需要余量)
        publish_content: 是否把 content 增量推给前端。
            react_agent / verifier 展示正文 → True;
            agent2 的 content 是最终评估 JSON(侧栏只展示思考链)→ False
        iteration: ReAct 迭代号(react_agent / verifier 有多轮迭代;
            agent2 的工具循环每次都是新卡片 → None 不下发)
        extra: 附加到每条 thinking_delta 的字段(如 verifier 的 verify=True)
        phase_start: 是否推流开始事件(verifier 的 start 事件由
            run_verifier_agent 自行发布,避免重复 → False)
        stop_check: 可选的逐 chunk 终止检查(agent2 被用户终止时秒级收流);
            命中后正常收尾(推 end 事件关闭前端卡片)并标 stopped=True,
            已收到的 reasoning 照常落库。不传时对底层调用一字不改。

    事件序列:phase=start(可选) → reasoning / content 增量 →
    error(异常,先推事件再原样 raise)/ end。降级策略(重试/兜底)由
    调用方决定,本函数只负责透传异常。

    perf:打点 llm_ttft(首 token 延迟)与 llm_stream_total(整次流耗时),
    附 role 字段区分智能体;perf_log 内部吞异常,不影响主流程。
    """
    conv_id = str(uuid.uuid4())
    acc = ToolCallAccumulator()
    reasoning_full = ""
    content_full = ""
    finish_reason: str | None = None

    def _payload(phase: str, delta: str = "") -> dict[str, Any]:
        data: dict[str, Any] = {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": role,
            "phase": phase,
            "delta": delta,
        }
        if iteration is not None:
            data["iteration"] = iteration
        if extra:
            data.update(extra)
        return data

    if phase_start:
        publish(task_id, "thinking_delta", _payload("start"))

    try:
        _t0 = time.perf_counter()
        _first_chunk = True
        # 仅在调用方传了 stop_check 时才多传一个参数:存量调用方与按 kwargs
        # 断言的测试(替身 client)看到的调用形状完全不变
        stream_kwargs: dict[str, Any] = {
            "messages": messages, "tools": tools,
            "tool_choice": "auto", "max_tokens": max_tokens,
        }
        if stop_check is not None:
            stream_kwargs["stop_check"] = stop_check
        for chunk in client.chat_stream(**stream_kwargs):
            if _first_chunk:
                _first_chunk = False
                perf_log(
                    task_id, "llm_ttft", time.perf_counter() - _t0,
                    round_idx=round_idx, role=role,
                    iteration=iteration,
                    prompt_chars=sum(
                        len(str(m.get("content") or "")) for m in messages
                    ),
                    tools=len(tools) if tools else 0,
                )
            # 思考链增量
            if chunk.reasoning_delta:
                reasoning_full += chunk.reasoning_delta
                publish(
                    task_id, "thinking_delta",
                    _payload("reasoning", chunk.reasoning_delta),
                )
            # 正式回答增量
            if chunk.content_delta:
                content_full += chunk.content_delta
                if publish_content:
                    publish(
                        task_id, "thinking_delta",
                        _payload("content", chunk.content_delta),
                    )
            # 工具调用增量(跨 chunk 累积)
            if chunk.tool_call_deltas:
                acc.add(chunk.tool_call_deltas)
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
        perf_log(
            task_id, "llm_stream_total", time.perf_counter() - _t0,
            round_idx=round_idx, role=role, iteration=iteration,
            finish=finish_reason or "unknown",
        )
    except Exception as e:
        logger.exception(f"[task={task_id}] {role} 流式调用失败")
        # 推送错误 delta(前端流式卡片内联显示),随后原样抛出
        publish(task_id, "thinking_delta", _payload("error", f"[流式调用失败: {e}]"))
        raise

    publish(task_id, "thinking_delta", _payload("end"))
    # 终止判定放在收流之后:底层生成器命中 stop_check 时直接 return,
    # 这里再用同一条检查点把它显式标出来(也是"流自然结束但期间被终止"
    # 的情形 —— 调用方下一层的循环边界同样会命中,不会丢)
    stopped = stop_check is not None and bool(stop_check())
    logger.info(
        f"[task={task_id}] {role} 流式结束,finish={finish_reason}, "
        f"reasoning={len(reasoning_full)}字符, content={len(content_full)}字符, "
        f"tool_calls={len(acc)}"
        + (", 已被调用方终止" if stopped else "")
    )
    return StreamResult(
        conv_id=conv_id,
        reasoning=reasoning_full,
        content=content_full,
        tool_calls=acc.tool_calls,
        finish_reason=finish_reason,
        stopped=stopped,
    )
