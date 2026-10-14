"""verifier_agent:动态验证智能体(对用户透明,不暴露此名称)

角色:agent2 调用此 agent 在已部署的测试环境动态验证 react_agent 发现的安全问题。
例如:react_agent 静态分析发现 SQL 注入疑似点,verifier_agent 发送 PoC HTTP 请求
验证是否真的能注入。

工具:
- http_request:向 test_env_url 发送 HTTP 请求(GET/POST/PUT/DELETE + headers + body)
  在沙箱里执行(用 urllib 标准库),后端服务器 IP 不暴露给测试环境
- run_python_code:在沙箱执行 Python(复用 react_agent 沙箱,可 read_file 仓库代码辅助构造 PoC;
  需要多步状态/签名序列时它自己用 urllib 发请求也算正常取证手段)
- submit_verification:申报逐个目标的结论(无副作用,不授权)。结论只走这条结构化
  通道才能支撑「已动态验证」置信档,散文总结里的"应该能注入"不算定论

授权:
- auth_mode="direct":所有动作直接执行
- auth_mode="per_action":每个 http_request / run_python_code 调用前弹窗让用户确认
  (通过 user_interaction.request_verify_authorization 阻塞等待,但有上限:
  settings.VERIFY_AUTH_TIMEOUT_SECONDS 与本轮审查硬时限取小值;用户选"后续自动允许"
  后同任务不再弹窗)

与 react_agent 的关系:
- 独立 ReAct 循环(自己的 messages 列表 + 迭代)
- 复用 react_agent 的沙箱会话(run_python_code 在同一沙箱执行,可访问已 clone 的仓库)
- 独立 LLM 调用(用 agent2 的 LLMClient,因为 verifier 是 agent2 的工具)

调用方:agent2(通过 tool_call 机制)
返回:验证结果文本(供 agent2 下一轮 LLM 调用作为 tool_result 注入);
结构化结论写进调用方传入的 `outcome_out` 字典(保持返回类型是 str 以兼容现有调用/测试)。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable

from sqlalchemy.orm import Session

# 共享运行时原语(runtime 层):与 react_agent / agent2 同源,
# 含此前 verifier 缺失的 Hermes 风格文本 tool_call 兜底解析
from app.agents.runtime.conversation import record_conversation
from app.agents.runtime.llm_stream import (
    extract_text_tool_calls,
    stream_llm,
    strip_tool_call_blocks,
)
from app.agents.runtime.tool_intent import build_tool_intent as _build_tool_intent
from app.agents.verification_outcome import (
    OUTCOME_ABORTED,
    OUTCOME_NOT_AUTHORIZED,
    OUTCOME_UNKNOWN,
    aggregate_targets,
    normalize_outcome,
    parse_outcome_line,
)
from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Task
from app.prompts.executor import TRUNCATION_CONTINUE_PROMPT
from app.prompts.verifier import VERIFIER_SYSTEM_PROMPT, build_auth_section
from app.tools.verifier_tools import (
    VERIFIER_EMIT_TOOL_DEFINITIONS,
    VERIFIER_TOOL_DEFINITIONS,
    http_request,
)

logger = logging.getLogger(__name__)

# verifier_agent ReAct 循环最大迭代次数
MAX_VERIFIER_ITERATIONS = 10

# 输出被 max_tokens 截断且正文为空(思考吃光预算、零产出)时的收尾重试上限
_MAX_TRUNCATION_RETRIES = 2

# 结论申报工具名(无副作用:不授权、不进沙箱)
_SUBMIT_VERIFICATION_TOOL_NAME = "submit_verification"

# 传给 LLM 的完整工具面:取证工具 + 结论申报工具
_VERIFIER_TOOLS = VERIFIER_TOOL_DEFINITIONS + VERIFIER_EMIT_TOOL_DEFINITIONS


def _auth_wait_seconds(deadline_monotonic: float | None) -> float | None:
    """本次授权最多等多少秒( None=不限时,仅当配置与硬时限都没给时)"""
    from app.config import settings

    configured = int(getattr(settings, "VERIFY_AUTH_TIMEOUT_SECONDS", 0) or 0)
    limit = float(configured) if configured > 0 else None
    if deadline_monotonic is None:
        return limit
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        return 0.0  # 本轮审查已到时限:不再等授权,也不再发行动作
    return remaining if limit is None else min(limit, remaining)


def run_verifier_agent(
    task: Task,
    db: Session,
    verification_request: str,
    client: LLMClient | None = None,
    round_idx: int = 1,
    *,
    outcome_out: dict[str, Any] | None = None,
    abort_check: Callable[[], bool] | None = None,
    deadline_monotonic: float | None = None,
) -> str:
    """执行一轮动态验证

    参数:
        task: 任务对象(含 test_env_url / verifier_auth_mode 配置)
        db: 数据库会话(落库 conversation)
        verification_request: agent2 传入的验证目标描述
            (如"验证 src/api/users.py 第 42 行的 SQL 注入是否可利用")
        client: LLMClient(用 agent2 的 client)
        round_idx: 当前协作轮次(用于落库 + 事件标注)
        outcome_out: 可选输出参。结论写进这里:{"outcome": str, "targets": list},
            供调用方登记证据台账(台账只认"定论"给「已动态验证」档)
        abort_check: 可选回调() -> bool。返回 True 表示本轮审查已终止/被取代,
            立即收手(否则逐动作授权会把终止请求拖在后面出不来)
        deadline_monotonic: 本轮审查的挂钟硬时限(time.monotonic 基准),
            用于把单次授权等待上限压到"还剩多少时间"

    返回:验证结果文本(供 agent2 作为 tool_result 注入下一轮 LLM 调用)
    """
    task_id = task.id
    task_id_str = str(task_id)
    test_env_url = task.test_env_url or ""
    auth_mode = task.verifier_auth_mode or "per_action"
    auth_tokens = task.verifier_auth_tokens or []

    # 本轮验证的账:报了什么结论 / 真跑了多少动作 / 多少动作没获批
    reported_targets: list[dict[str, Any]] = []
    state = {"executed": 0, "blocked": 0, "auth_unavailable": False}

    def _finish(text: str, *, aborted: bool = False, reasoning: str = "") -> str:
        """收尾唯一出口:算结论 → 写 outcome_out → 落库思考 → 推 end 事件

        三条退出路径(总结完成 / 审查中止 / 迭代耗尽)都走这里:开始事件已经
        推过,不配套推 phase=end 就会在前端留一张永不结束的"正在验证"卡片。
        """
        outcome = _resolve_outcome(
            reported_targets, text,
            executed=state["executed"], blocked=state["blocked"], aborted=aborted,
        )
        if outcome_out is not None:
            outcome_out["outcome"] = outcome
            outcome_out["targets"] = list(reported_targets)
        # 结论档位一并落库(回灌给 agent2 的那行由调用方补),侧栏能看到
        # "验出了什么",不只是"跑了一堆请求"
        _record_verifier_thinking(
            db, task, round_idx,
            f"{text}\n[验证结论] outcome={outcome}", reasoning,
        )
        _publish_verify_end(task_id, round_idx)
        return text

    # 推送验证开始事件(前端显示"正在验证...")
    publish(task_id, "thinking_delta", {
        "conv_id": str(uuid.uuid4()),
        "round_idx": round_idx,
        "role": "agent2",  # 对用户透明:归到 agent2 名下
        "phase": "start",
        "delta": "",
        "verify": True,  # 前端据此显示"正在验证"而非"正在评估"
    })

    client = client or LLMClient()

    # 系统提示:动态注入可用登录身份(LLM 只看到 label,看不到 token 明文)
    system_prompt = VERIFIER_SYSTEM_PROMPT
    if auth_tokens:
        labels = ", ".join(t.get("label", "") for t in auth_tokens)
        system_prompt += build_auth_section(labels)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"请验证以下安全发现:\n\n{verification_request}\n\n"
            f"测试环境地址: {test_env_url}\n"
            f"请构造 PoC 验证每个问题是否真实可利用。"
        )},
    ]

    # 因"输出被截断且零产出"触发的收尾重试已用次数(见下方无工具调用分支)
    truncation_retries = 0

    for iteration in range(1, MAX_VERIFIER_ITERATIONS + 1):
        # 审查已终止/被新一轮取代:不再花 LLM 往返,也不等新授权
        if abort_check is not None and abort_check():
            logger.info(f"[task={task_id}] verifier_agent 在第 {iteration} 轮收到中止信号")
            return _finish(
                "[验证中止:本轮审查已终止或被新一轮执行取代,未继续发起验证动作。"
                "已有结果见上方工具调用。]",
                aborted=True,
            )

        # 流式调 LLM(runtime 统一流式实现,role 标为 agent2 + verify)
        reasoning_full, content_full, tool_calls_full, finish_reason = _stream_verifier_llm(
            client, messages, task_id=task_id, round_idx=round_idx, iteration=iteration
        )

        # 兜底:结构化 tool_calls 为空但 content 里有 Hermes 风格文本
        # 工具调用块(GLM/Qwen 思考模式把工具调用写在正文)。
        # verifier 此前缺失该兜底 → 工具调用文本被当成"验证总结"直接
        # 返回,PoC 根本没执行。与 react_agent 共用 runtime 的同一解析。
        if not tool_calls_full and content_full:
            text_tool_calls = extract_text_tool_calls(content_full)
            if text_tool_calls:
                logger.info(
                    f"[task={task_id}] verifier_agent 从 content 解析出 "
                    f"{len(text_tool_calls)} 个文本 tool_call(兜底)"
                )
                tool_calls_full = text_tool_calls
                content_full = strip_tool_call_blocks(content_full)

        # 无工具调用:模型主动 stop → 验证完成,content 是总结
        # 但 finish=length 且正文为空时,是思考吃光输出预算、本轮零产出——
        # 不能当"验证完成"静默收尾(会把截断伪装成完成)。注入收尾提示重试
        # (有界),耗尽再降级收尾
        if not tool_calls_full:
            if finish_reason == "length" and not content_full:
                if truncation_retries < _MAX_TRUNCATION_RETRIES:
                    truncation_retries += 1
                    logger.warning(
                        f"[task={task_id}] verifier_agent 第 {iteration} 轮输出被 "
                        f"max_tokens 截断且零产出(reasoning={len(reasoning_full)}字符),"
                        f"注入收尾提示重试({truncation_retries}/{_MAX_TRUNCATION_RETRIES})"
                    )
                    messages.append({
                        "role": "user",
                        "content": TRUNCATION_CONTINUE_PROMPT,
                    })
                    continue
                logger.warning(
                    f"[task={task_id}] verifier_agent 第 {iteration} 轮输出被 "
                    f"max_tokens 截断且零产出,收尾重试已达上限"
                    f"({_MAX_TRUNCATION_RETRIES}),降级收尾"
                )
            elif finish_reason == "length":
                logger.warning(
                    f"[task={task_id}] verifier_agent 第 {iteration} 轮输出被 "
                    f"max_tokens 截断(finish=length),以现有正文降级收尾。"
                    f"content={len(content_full)}字符"
                )
            else:
                logger.info(
                    f"[task={task_id}] verifier_agent 在第 {iteration} 轮完成验证,"
                    f"content={len(content_full)}字符"
                )
            return _finish(
                content_full or "(验证完成,无总结输出)",
                reasoning=reasoning_full,
            )

        # 把 assistant 消息(含 tool_calls)加回 messages
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content_full}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{tc['index']}",
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc["arguments_str"],
                },
            }
            for tc in tool_calls_full
        ]
        messages.append(assistant_msg)

        # 执行每个工具调用
        for tc in tool_calls_full:
            tool_name = tc["name"]
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}

            # 结论申报:纯累加,不动沙箱也不需授权(否则点"同意"的其实是结论)
            if tool_name == _SUBMIT_VERIFICATION_TOOL_NAME:
                receipt = _record_verification_report(args, reported_targets)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{tc['index']}",
                    "content": json.dumps(receipt, ensure_ascii=False, default=str),
                })
                continue

            # per_action 授权:每个动作弹窗确认(有上限,且可被"后续自动允许"跨过)
            decision = "approved"
            if auth_mode == "per_action":
                if state["auth_unavailable"]:
                    # 已经没人应答:不再逐动作白等一轮时限
                    decision = "unavailable"
                else:
                    decision = _request_action_authorization(
                        task_id_str, tool_name, args, test_env_url,
                        timeout=_auth_wait_seconds(deadline_monotonic),
                    )
                    if decision == "timeout":
                        # 用户不应答:本轮后续动作不再弹窗(整轮挂在这里
                        # 只会把审查硬时限和「终止检查」都拖到授权时限之后才生效)
                        state["auth_unavailable"] = True

            if decision != "approved":
                state["blocked"] += 1
                # .get 兼容:未列入的话术不能把整轮验证抛死
                tool_result = {
                    "status_code": 0,
                    "body": _UNAUTHORIZED_BODY.get(decision) or _UNAUTHORIZED_BODY["denied"],
                }
            else:
                tool_result = _execute_verifier_tool(
                    tool_name, args, task_id_str, test_env_url, auth_tokens
                )
                state["executed"] += 1

            # 落库工具调用 + 结果(对用户透明,role=agent2)
            _record_verifier_tool_call(
                db, task, round_idx, tool_name, args, tool_result
            )

            # tool_result 加回 messages
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{tc['index']}",
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            })

    # 达到最大迭代仍未完成
    logger.warning(f"[task={task_id}] verifier_agent 达到最大迭代 {MAX_VERIFIER_ITERATIONS}")
    return _finish(
        f"验证达到最大迭代次数({MAX_VERIFIER_ITERATIONS}),强制结束。"
        "已有结果见上方工具调用。"
    )


# 未获批动作回给模型的话术(区分"用户拒绝"与"没人应答",两者都不是漏洞不成立)
_UNAUTHORIZED_BODY: dict[str, str] = {
    "denied": "[用户拒绝执行此动作]",
    "timeout": "[用户未在时限内授权,动作未执行(这不代表问题不成立)]",
    "unavailable": "[本轮授权已超时未应答,此动作未执行(这不代表问题不成立)]",
}


def _resolve_outcome(
    reported: list[dict[str, Any]],
    prose: str,
    *,
    executed: int,
    blocked: int,
    aborted: bool = False,
) -> str:
    """算出本轮验证的整体结论(定论/未定论),供证据台账判给不给「已动态验证」档。

    优先级:申报过的目标聚合 > 审查中止 > 一个动作都没跑成(未授权) >
    正文里的 outcome= 申报行 > unknown。
    拿不到定论就 fail-closed:宁可在源码/引用档,也不白送置信。
    """
    if reported:
        return aggregate_targets(reported)
    if aborted:
        return OUTCOME_ABORTED
    if blocked and not executed:
        return OUTCOME_NOT_AUTHORIZED
    parsed = parse_outcome_line(prose)
    return normalize_outcome(parsed) if parsed else OUTCOME_UNKNOWN


def _record_verification_report(
    args: dict[str, Any], sink: list[dict[str, Any]],
) -> dict[str, Any]:
    """受理 submit_verification 申报:归一后累进 sink,返回回执(含整体结论)"""
    raw_targets = args.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        return {
            "ok": False,
            "error": "targets 必须是非空数组(每个验证目标一条结论)",
        }
    recorded: list[dict[str, Any]] = []
    for item in raw_targets[:20]:
        if not isinstance(item, dict):
            continue
        norm = normalize_outcome(item.get("result"))
        recorded.append({
            "target": str(item.get("target") or "")[:300],
            "result": norm,
            "definitive": norm in ("confirmed", "not_confirmed"),
            "evidence": str(item.get("evidence") or "")[:500],
        })
    if not recorded:
        return {"ok": False, "error": "targets 里没有任何合法条目(每项需 target + result)"}
    sink.extend(recorded)
    return {
        "ok": True,
        "recorded": recorded,
        "outcome": aggregate_targets(sink),
        "note": "结论已记录;若 result 为未定论档位,不要把它当成已动态验证",
    }


# ============================================================
# 流式 LLM 调用
# ============================================================


def _stream_verifier_llm(
    client: LLMClient,
    messages: list[dict[str, Any]],
    *,
    task_id: Any,
    round_idx: int,
    iteration: int,
) -> tuple[str, str, list[dict[str, Any]], str | None]:
    """流式调用 verifier_agent 的 LLM(runtime.stream_llm 的薄包装)

    保留模块级包装:签名与返回值保持不变。收 chunk / 推 thinking_delta /
    工具调用跨 chunk 累积 / perf 打点由 runtime.stream_llm 统一实现。

    verifier 特有行为经参数表达:
    - role="agent2" + extra={"verify": True}:对用户透明,前端显示
      "正在验证"而非"正在评估"
    - phase_start=False:流开始事件由 run_verifier_agent 自行发布
      (整个验证过程一张 start 卡,迭代间不重复推 start)

    返回 (reasoning_full, content_full, tool_calls_full, finish_reason)
    """
    # max_tokens 取模型解析后的输出上限:思考型模型把预算烧在推理上时,
    # 写死 4096 会导致零产出被截断(chat_stream 仍会向下钳制)
    result = stream_llm(
        client, messages,
        task_id=task_id, round_idx=round_idx, role="agent2",
        tools=_VERIFIER_TOOLS, max_tokens=client.max_output_tokens,
        iteration=iteration,
        extra={"verify": True},
        phase_start=False,
    )
    return (
        result.reasoning,
        result.content,
        result.tool_calls,
        result.finish_reason,
    )


# ============================================================
# 工具执行 + 授权拦截
# ============================================================


def _request_action_authorization(
    task_id_str: str,
    tool_name: str,
    args: dict[str, Any],
    test_env_url: str,
    auth_tokens: list[dict] | None = None,
    *,
    timeout: float | None = None,
) -> str:
    """per_action 模式:弹窗让用户确认是否执行此动作

    返回 "approved" / "denied" / "timeout"。用户已选"后续自动允许"时直接返回
    approved 不弹窗(动作照常经 _record_verifier_tool_call 落库,侧栏能看到跑了什么)。
    timeout 是本次等待上限(调用方按"配置上限 vs 本轮审查剩余硬时限"取小值);
    0 意味着已到时限,不再白等一个没人看的弹窗。
    """
    from app.user_interaction import (
        request_verify_authorization,
        verify_auto_approved,
        wait_for_authorization,
    )

    if verify_auto_approved(task_id_str):
        return "approved"

    action_id = str(uuid.uuid4())

    # 构造动作描述(前端弹窗展示)
    if tool_name == "http_request":
        method = args.get("method", "GET")
        path = args.get("path", "/")
        base = (test_env_url or "").rstrip("/")
        full_url = base + (path if path.startswith("/") else "/" + path)
        action_desc = {
            "action_id": action_id,
            "type": "http_request",
            "method": method.upper(),
            "url": full_url,
            "headers": args.get("headers", {}),
            "body": args.get("body", ""),
            # 显示登录身份(若有),便于用户判断该动作是否合理
            "auth_profile": args.get("auth_profile") or "",
        }
    elif tool_name == "run_python_code":
        code = args.get("code", "")
        action_desc = {
            "action_id": action_id,
            "type": "run_python_code",
            "code": code[:2000],  # 截断防前端弹窗过长
            "code_truncated": len(code) > 2000,
        }
    else:
        action_desc = {
            "action_id": action_id,
            "type": tool_name,
            "args": args,
        }

    request_verify_authorization(task_id_str, action_desc)
    return wait_for_authorization(task_id_str, action_id, timeout=timeout)


def _execute_verifier_tool(
    tool_name: str,
    args: dict[str, Any],
    task_id_str: str,
    test_env_url: str,
    auth_tokens: list[dict] | None = None,
) -> dict[str, Any]:
    """执行 verifier_agent 的工具调用(授权已通过,直接执行)"""
    if tool_name == "http_request":
        return http_request(
            method=args.get("method", "GET"),
            path=args.get("path", "/"),
            headers=args.get("headers"),
            body=args.get("body"),
            auth_profile=args.get("auth_profile"),
            task_id=task_id_str,
            test_env_url=test_env_url,
            auth_tokens=auth_tokens,
        )
    elif tool_name == "run_python_code":
        # 复用 react_agent 的沙箱(set_current_task 已由 orchestrator 设置)
        from app.tools.sandbox_tools import run_python_code
        return run_python_code(
            code=args.get("code", ""),
            timeout=args.get("timeout", 60),
            task_id=task_id_str,
        )
    else:
        return {"error": f"verifier_agent 不支持的工具: {tool_name}"}


# ============================================================
# 落库 + 事件
# ============================================================


def _record_verifier_thinking(
    db: Session,
    task: Task,
    round_idx: int,
    content: str,
    reasoning: str,
) -> None:
    """落库 verifier_agent 的思考/总结(runtime.record_conversation 统一实现)

    对用户透明(role=agent2, type=thinking)。推 conversation 事件:验证过程
    动辄数分钟,中途离开详情页再回来的订阅者收不到 thinking_delta 增量
    (总线不缓存),只能靠这条落库事件看到已完成的这段验证思考;前端按
    reasoning/content 文本对账退役对应的实时卡片,不会重复展示。
    """
    record_conversation(
        db, task, round_idx=round_idx,
        role="agent2", type="thinking",
        content=(
            f"[验证结果] {content}"
            if not content.startswith("[验证") else content
        ),
        reasoning=reasoning or None,
    )


def _record_verifier_tool_call(
    db: Session,
    task: Task,
    round_idx: int,
    tool_name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """落库 verifier_agent 的工具调用 + 结果(runtime.record_conversation 统一实现)

    对用户透明(role=agent2,事件附 verify=True)。修复:此前落库+推送
    不带 id / tool_call_id,前端无法把 result 与 call 配对展示;
    统一后与其他智能体的配对行为一致。
    """
    intent = _build_tool_intent(tool_name, args)
    call_conv = record_conversation(
        db, task, round_idx=round_idx,
        role="agent2", type="tool_call",
        content=intent,
        extra_payload={"verify": True},
    )

    # 工具结果记录(截断防过长),tool_call_id 配对 call
    result_str = json.dumps(result, ensure_ascii=False, default=str)
    if len(result_str) > 5000:
        result_str = result_str[:5000] + "...(已截断)"
    record_conversation(
        db, task, round_idx=round_idx,
        role="agent2", type="tool_result",
        content=result_str,
        tool_call_id=str(call_conv.id),
        extra_payload={"verify": True},
    )


def _publish_verify_end(task_id: Any, round_idx: int) -> None:
    """推送验证结束事件"""
    publish(task_id, "thinking_delta", {
        "conv_id": str(uuid.uuid4()),
        "round_idx": round_idx,
        "role": "agent2",
        "phase": "end",
        "delta": "",
        "verify": True,
    })
