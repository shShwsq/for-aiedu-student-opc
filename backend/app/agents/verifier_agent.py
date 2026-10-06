"""verifier_agent:动态验证智能体(对用户透明,不暴露此名称)

角色:agent2 调用此 agent 在已部署的测试环境动态验证 react_agent 发现的安全问题。
例如:react_agent 静态分析发现 SQL 注入疑似点,verifier_agent 发送 PoC HTTP 请求
验证是否真的能注入。

工具:
- http_request:向 test_env_url 发送 HTTP 请求(GET/POST/PUT/DELETE + headers + body)
  在沙箱里执行(用 urllib 标准库),后端服务器 IP 不暴露给测试环境
- run_python_code:在沙箱执行 Python(复用 react_agent 沙箱,可 read_file 仓库代码辅助构造 PoC)

授权:
- auth_mode="direct":所有动作直接执行
- auth_mode="per_action":每个 http_request / run_python_code 调用前弹窗让用户确认
  (通过 user_interaction.request_verify_authorization 阻塞等待)

与 react_agent 的关系:
- 独立 ReAct 循环(自己的 messages 列表 + 迭代)
- 复用 react_agent 的沙箱会话(run_python_code 在同一沙箱执行,可访问已 clone 的仓库)
- 独立 LLM 调用(用 agent2 的 LLMClient,因为 verifier 是 agent2 的工具)

调用方:agent2(通过 tool_call 机制)
返回:验证结果文本(供 agent2 下一轮 LLM 调用作为 tool_result 注入)
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

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
from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Task
from app.prompts.verifier import VERIFIER_SYSTEM_PROMPT, build_auth_section
from app.tools.verifier_tools import VERIFIER_TOOL_DEFINITIONS, http_request

logger = logging.getLogger(__name__)

# verifier_agent ReAct 循环最大迭代次数
MAX_VERIFIER_ITERATIONS = 10


def run_verifier_agent(
    task: Task,
    db: Session,
    verification_request: str,
    client: LLMClient | None = None,
    round_idx: int = 1,
) -> str:
    """执行一轮动态验证

    参数:
        task: 任务对象(含 test_env_url / verifier_auth_mode 配置)
        db: 数据库会话(落库 conversation)
        verification_request: agent2 传入的验证目标描述
            (如"验证 src/api/users.py 第 42 行的 SQL 注入是否可利用")
        client: LLMClient(用 agent2 的 client)
        round_idx: 当前协作轮次(用于落库 + 事件标注)

    返回:验证结果文本(供 agent2 作为 tool_result 注入下一轮 LLM 调用)
    """
    task_id = task.id
    task_id_str = str(task_id)
    test_env_url = task.test_env_url or ""
    auth_mode = task.verifier_auth_mode or "per_action"
    auth_tokens = task.verifier_auth_tokens or []

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

    for iteration in range(1, MAX_VERIFIER_ITERATIONS + 1):
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

        # 无工具调用 → 验证完成,content 是总结
        if not tool_calls_full:
            logger.info(
                f"[task={task_id}] verifier_agent 在第 {iteration} 轮完成验证,"
                f"content={len(content_full)}字符"
            )
            _record_verifier_thinking(db, task, round_idx, content_full, reasoning_full)
            _publish_verify_end(task_id, round_idx)
            return content_full or "(验证完成,无总结输出)"

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

            # per_action 授权:每个动作弹窗确认
            if auth_mode == "per_action":
                approved = _request_action_authorization(
                    task_id_str, tool_name, args, test_env_url, auth_tokens
                )
                if not approved:
                    tool_result = {"status_code": 0, "body": "[用户拒绝执行此动作]"}
                else:
                    tool_result = _execute_verifier_tool(
                        tool_name, args, task_id_str, test_env_url, auth_tokens
                    )
            else:
                tool_result = _execute_verifier_tool(
                    tool_name, args, task_id_str, test_env_url, auth_tokens
                )

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
    _record_verifier_thinking(
        db, task, round_idx,
        f"验证达到最大迭代次数({MAX_VERIFIER_ITERATIONS}),强制结束。已有结果见上方工具调用。",
        "",
    )
    _publish_verify_end(task_id, round_idx)
    return f"验证达到最大迭代次数({MAX_VERIFIER_ITERATIONS}),强制结束。"


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
    result = stream_llm(
        client, messages,
        task_id=task_id, round_idx=round_idx, role="agent2",
        tools=VERIFIER_TOOL_DEFINITIONS, max_tokens=4096,
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
) -> bool:
    """per_action 模式:弹窗让用户确认是否执行此动作

    返回 True=用户同意,False=用户拒绝
    """
    from app.user_interaction import request_verify_authorization, wait_for_authorization

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
    return wait_for_authorization(task_id_str, action_id)


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
