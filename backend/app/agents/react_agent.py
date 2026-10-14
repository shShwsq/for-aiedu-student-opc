"""react_agent:基于 ReAct 模式的 AI助手

阶段 4 重构:
- system prompt 从场景取,不再硬编码
- submit_findings → submit_results,字段通用化(title/content/metadata)
- 工具列表从场景白名单取
- 落库到 Result 表(带 round_idx)
- 不再管理 task 状态(由 orchestrator 控制),只负责跑一轮返回结果
- 不再关闭沙箱(由 orchestrator 控制,多轮复用)

阶段 7+:LLM 调用全部流式
- 通过 LLMClient.chat_stream() 拿 StreamChunk
- reasoning_delta / content_delta / tool_call_deltas 都通过 event_bus 推给前端
- 工具调用参数跨 chunk 累积,完整后才执行
"""
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task
from app.models.task_artifact import TaskArtifact
from app.pause_controller import wait_if_paused
from app.perf import perf_log
from app.tools.schema import execute_tool, get_all_tools, set_current_task
from app.user_messages import drain_user_messages, has_pending_messages

# 共享运行时原语(runtime 层,agent1 / agent2 / verifier_agent 复用):
# - _add_conversation:统一落库 + SSE 推送(含 tool_call_id 配对)
# - _build_tool_intent / _extract_text_tool_calls / _strip_tool_call_blocks:
#   工具意图生成 + Hermes 风格文本 tool_call 兜底(别名保持原模块名,
#   存量测试按模块属性导入/替换的兼容面不变)
# - MAX_HISTORY_MSG_CHARS:与 agent2 同源的截断常量
# - stream_llm:流式调用本体(下方 _stream_llm_response 是它的薄包装)
from app.agents.runtime.conversation import record_conversation as _add_conversation
from app.agents.runtime.constants import MAX_HISTORY_MSG_CHARS
from app.agents.runtime.llm_stream import (
    extract_text_tool_calls as _extract_text_tool_calls,
    strip_tool_call_blocks as _strip_tool_call_blocks,
    stream_llm,
)
from app.agents.runtime.plan import extract_plan as _extract_plan
from app.agents.runtime.tool_intent import build_tool_intent as _build_tool_intent

# LLM 文本资产(system prompt / 追问指引 / plan 提醒 / repo context 段落 /
# 历史压缩 prompt 等)集中管理于 app/prompts/executor.py,本模块只留执行逻辑
from app.prompts.executor import (
    ATTACHMENT_UNAVAILABLE_NOTE,
    DISPATCH_SUBTASKS_TOOL_DESCRIPTION_TEMPLATE,
    FOLLOWUP_GUIDANCE,
    HISTORY_COMPRESS_PROMPT,
    LOOP_BREAK_PROMPT,
    MAX_ITERATION_PROMPT,
    REACT_AGENT_SYSTEM_PROMPT,
    SYSTEM_INJECT_MARKER,
    TRUNCATION_CONTINUE_PROMPT,
    _has_creation_upload,
    build_first_round_question,
    build_history_compress_segments,
    build_repo_context_section,
    build_subtask_instruction,
    format_followup_attachment_note,
    format_followup_upload_warning,
    format_injected_user_messages,
    format_plan_reminder,
    round_compact_text,
)

logger = logging.getLogger(__name__)


# 最大迭代轮次,防止死循环
MAX_ITERATIONS = 30
# 单次输出被 max_tokens 截断且正文为空时,注入收尾提示重试的最大次数
# (思考型模型把输出预算烧在推理上会零产出;有界重试避免死循环)
MAX_TRUNCATION_RETRIES = 2
# 连续相同工具调用的容忍次数,超过就打破循环
MAX_SAME_CALLS = 3
# 修复 12:循环检测滑动窗口参数
# recent_calls 仅保留最近 MAX_RECENT_CALLS 条(避免无限增长 + 限制检测范围)
MAX_RECENT_CALLS = 10
# 滑动窗口大小:检查最近 WINDOW_SIZE 次调用是否构成循环
LOOP_WINDOW_SIZE = 6
# 窗口内不同 call_sig 少于等于此值 → 判定为循环(覆盖交替循环 A,B,A,B,A,B)
LOOP_MIN_DISTINCT = 2


# 跨轮记忆传递:从 Conversation 表加载之前轮次的对话,以结构化 messages
# 注入当前轮(保留 user/assistant/system 角色边界),用户追问原文作为
# 最后一条独立 user 消息 —— 对齐 Codex 的"历史 append-only + 用户消息零包装"。
# 单条历史消息(用户原话 / 执行总结 / 评审反馈)最大字符数,超出截断
# (常量本体已从 runtime/constants 导入,与 agent2 同源,消除手工同步)
# 历史记忆 token 预算(CJK 感知粗估,见 _estimate_tokens;
# 可用环境变量 HISTORY_TOKEN_BUDGET 覆盖,默认 8000)
MAX_HISTORY_TOKEN_BUDGET = int(os.getenv("HISTORY_TOKEN_BUDGET", "8000"))

# 系统注入消息的边界标记与追问轮指引模板:
# 集中管理于 app/prompts/executor.py(SYSTEM_INJECT_MARKER / FOLLOWUP_GUIDANCE),
# 此处经模块级 import 提供给本模块与(经 import 链)外部测试使用


def run_react_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    client: LLMClient | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 react_agent

    参数:
        task: 任务对象
        db: 数据库会话
        round_idx: 当前协作轮次(1 开始)
        followup_query: 追问指令。None 表示第一轮(用 task.user_input)
        client: 可选的 LLMClient(阶段 6:从用户配置构造),None 时回退到 env 默认
        repo_context: 第 1 轮专用。orchestrator 主动 clone 后传入的仓库上下文
            (含 repo_path + 根目录结构)。非空时,第 1 轮 user_msg 会注入它并
            提示"仓库已 clone,不要调用 clone_repo,直接开始执行任务"。
            None 表示未主动 clone(走原流程,LLM 自主 clone)。
        previous_plan: 上一轮结束时的 plan 状态(修复 4)。None 或空表示第一轮
            或上轮无 plan。传入时,本轮启动即从该 plan 继续(避免跨轮重新规划
            已完成项),并在首轮 LLM 调用前作为 system 提醒注入。

    返回:(results 列表, summary 文本, final_plan)
        results: [{"title": str, "content": str, "metadata": dict}](始终为空,
            结构化结果(重点与知识点)由 agent2 后台审查完成时经 results 字段输出)
        summary: react_agent 的总结说明
        final_plan: 本轮结束时的 plan 状态(可能为空 list),供 orchestrator
            传给下一轮实现跨轮延续

    注意:本函数不管理 task 状态(不标记 COMPLETED),不关闭沙箱
    """
    # 设置当前任务上下文(供沙箱工具复用会话 + skill 工具按场景过滤)
    task_id_str = str(task.id)
    # 读命令确认模式:task.params._executor_command_confirm(由 agent_policy 回填默认值)
    # always_approve:危险命令直接执行;per_command:危险命令推前端 CommandConfirmDialog 弹窗确认
    # 仅影响内置 react_agent 的 run_command 工具;CLI 执行器走 ACP request_permission 独立机制
    executor_command_confirm = "always_approve"
    if task.params:
        executor_command_confirm = task.params.get("_executor_command_confirm", "always_approve")
    set_current_task(
        task_id_str,
        task.scenario,
        user_id=task.user_id,
        executor_command_confirm=executor_command_confirm,
    )

    # [perf] react_agent 进入锚点(与 executor_run / llm_ttft 串起时间线)
    perf_log(
        task.id, "react_agent_enter",
        round_idx=round_idx, followup=followup_query is not None,
    )

    # 场景降级后:用通用 prompt,工具全部开放(不再按场景过滤)
    system_prompt = REACT_AGENT_SYSTEM_PROMPT
    tools = get_all_tools()

    # 子任务按需派发:开关开即注册 dispatch_review_subtasks 工具 —— 子任务可纯
    # instruction、不再等同一个技能,可见技能数不该决定工具是否出现(是否派发
    # 始终由模型决定);否则(关)不注册 = 纯串行主循环。
    # visible_skill_names 供工具描述列出可用技能名,并供工具执行分支校验各子任务
    # skills ⊆ 可见集(与描述里列出的同一集),可为空。
    subtask_dispatch_enabled = bool(getattr(settings, "REACT_FACET_DISPATCH_ENABLED", True))
    if subtask_dispatch_enabled:
        visible_skill_names = _visible_skill_names(task)
        tools = tools + [_build_subtask_dispatch_tool(visible_skill_names)]
    else:
        visible_skill_names = []

    # 分项目记忆注入:基于 task.params.repo_url 查 Project.memory_content,
    # 追加到 system prompt 末尾,影响审计方向(优先检查已知问题)。
    # user_id 为 None(匿名任务)或无对应项目记忆时返回空串,不影响原 prompt。
    # 稳定性说明:记忆段追加在 base prompt 之后,provider 的 prompt 缓存
    # 按前缀匹配,base 部分跨轮字节级稳定、不受记忆更新影响;resume 轮
    # 重新读取记忆是有意行为(上一轮任务完成后归纳的新记忆应可用于下一轮),
    # 故不做"首轮注入后冻结"。
    _project_repo_url = (task.params or {}).get("repo_url")
    if _project_repo_url and task.user_id is not None:
        from app.services.memory_injection import build_react_agent_memory_section
        _project_mem = build_react_agent_memory_section(
            db, task.user_id, _project_repo_url,
        )
        if _project_mem:
            system_prompt = system_prompt + "\n\n" + _project_mem

    # 全局长期记忆注入:跨项目通用经验(Hard Constraints / Tech Stack / Lessons
    # Learned),影响执行方式。执行侧在沙箱里干活,这类"怎么做"的知识直接影响
    # 执行正确性。user_id 为 None 或无全局记忆时返回空串。
    if task.user_id is not None:
        from app.services.memory_injection import build_global_memory_section
        _global_mem = build_global_memory_section(db, task.user_id)
        if _global_mem:
            system_prompt = system_prompt + "\n\n" + _global_mem

    # 构造初始 user 消息
    # repo_ctx_section:预 clone 上下文段,只进发送内容不落库展示
    # (属系统编排信息,非用户原话,与 acp_base 记忆注入段同样处理)
    # history_messages / context_system_messages:追问轮的结构化历史与
    # 编排指引(独立 system 消息),同样不落库为用户消息
    repo_ctx_section = ""
    history_messages: list[dict[str, Any]] = []
    context_system_messages: list[dict[str, Any]] = []
    if followup_query is None:
        # 第一轮:用 task.user_input(+ 仓库/分支/上传来源提示)
        # 内容构造复用 build_first_round_question,与 create_task 落库的首轮
        # question 完全一致(幂等去重的前提)
        params = task.params or {}
        user_msg = build_first_round_question(task.user_input, params)

        # orchestrator 已准备好交付物(clone 或上传)时,注入上下文提示跳过 clone_repo
        if repo_context:
            # 上传任务:工作区里是用户上传的文件,不是 clone 的仓库
            variant = "upload" if _has_creation_upload(params) else "clone"
            repo_ctx_section = build_repo_context_section(repo_context, variant)
    else:
        # 追问轮:不重新 clone,基于已有仓库继续
        # 跨轮记忆以结构化 messages 注入(保留 user/assistant/system 角色边界,
        # 含每轮用户原话),用户追问原文作为最后一条独立 user 消息
        from app.tools import sandbox_tools

        ws_info = sandbox_tools.get_workspace_info(task_id_str)
        repo_path = ""
        # 只有工作区确实有文件才注入路径提示:预 clone/上传传输可能失败
        # 降级为空目录,此时提示"已就位"会误导 react_agent 跳过获取动作
        if (
            ws_info and ws_info.get("repo_path")
            and sandbox_tools.workspace_has_files(task_id_str)
        ):
            repo_path = ws_info["repo_path"]

        # 之前轮次的结构化对话记忆(用户原话 + 执行总结 + 评审反馈,
        # 三级压缩:Level 0(完整) → Level 1(丢工具摘要) → Level 2(LLM 压缩早期轮次))
        # client 提前构造,供 LLM 压缩使用(若传入的 client 为 None,临时构造一个)
        history_client = client or LLMClient()
        # [perf] 历史记忆构造(Level 2 会调 LLM 压缩,预压缩命中缓存时为纯读)
        _t0 = time.perf_counter()
        history_messages = _build_history_messages(
            db, task.id, round_idx, client=history_client,
        )
        perf_log(
            task.id, "build_history", time.perf_counter() - _t0,
            round_idx=round_idx, messages=len(history_messages),
        )

        # 用户追问原文:零包装独立成条(P0-2),落库与发送内容一致
        user_msg = followup_query

        # 编排指引 + 工作区路径提示:系统注入消息,与用户原话分离(P0-3)
        # 措辞中性:repo 任务该路径是克隆目录,纯上传任务是 uploaded_files/,
        # 统称为"工作区"避免误导(与 acp_base 只对真实仓库说"已 clone"口径一致)
        guidance_parts = [FOLLOWUP_GUIDANCE]
        if repo_path:
            guidance_parts.append(
                f"{SYSTEM_INJECT_MARKER}工作区路径]\n"
                f"任务工作区(文件已就位)在 {repo_path},直接用 read_file/"
                f"search_code/list_files 等工具访问该路径,不要重复 clone 或上传。"
            )
        context_system_messages = [
            {"role": "system", "content": "\n\n".join(guidance_parts)}
        ]

    # 记录 user 指令到对话:首轮为 build_first_round_question 构造的提问,
    # 追问轮为用户原话 —— 落库内容即用户可见内容;预 clone 上下文段、
    # 历史记忆与编排指引均为系统注入(独立 system 消息),不混入用户消息,
    # 落库与发送内容天然一致,无需事后剥离
    stored_msg = user_msg
    # 幂等落库:首轮提问已在任务创建时(create_task)落库,此处跳过避免重复记录;
    # 追问轮/续跑轮首次进入时该轮尚无 question,正常落库。
    # 按 (task_id, round_idx, role=user, type=question) 定位,与创建时一致。
    _existing_question = (
        db.query(Conversation.id)
        .filter(
            Conversation.task_id == task.id,
            Conversation.round_idx == round_idx,
            Conversation.role == "user",
            Conversation.type == "question",
        )
        .first()
    )
    if _existing_question is None:
        _add_conversation(
            db, task, round_idx=round_idx,
            role="user", type="question",
            content=stored_msg,
        )

    # 实际发送给 LLM 时拼上预 clone 上下文段
    if repo_ctx_section:
        user_msg += repo_ctx_section

    # 创建 LLM 客户端(优先用注入的,否则回退到 env 默认)
    client = client or LLMClient()

    # ReAct 循环的消息序列:
    # - 首轮:[system, user(任务指令+repo_ctx_section)]
    # - 追问轮:[system, *结构化历史(逐轮 user/assistant/system),
    #            编排指引(system), user(追问原文)]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]
    if followup_query is None:
        messages.append({"role": "user", "content": user_msg})
    else:
        messages.extend(history_messages)
        messages.extend(context_system_messages)
        messages.append({"role": "user", "content": user_msg})

    summary = ""
    recent_calls: list[str] = []

    # plan 状态(代码维护,参考 LangGraph Plan-and-Execute)
    # 修复 4:从上一轮的 plan 继续,避免跨轮重新规划已完成项
    # 初始为 previous_plan(深拷贝,避免修改入参);LLM 首次思考可输出 <plan> 更新
    current_plan: list[dict] = [dict(s) for s in (previous_plan or [])]

    # 跨轮 plan 续接:若有上轮 plan,作为 system 提醒注入首轮 messages,
    # 让 LLM 看到之前进度(已完成的步骤保持 done,只推进未完成项)
    if current_plan:
        initial_reminder = format_plan_reminder(current_plan)
        if initial_reminder:
            messages.append({"role": "system", "content": initial_reminder})
            # 推送 plan 事件让前端也同步显示跨轮 plan 状态
            publish(task.id, "plan", {
                "round_idx": round_idx,
                "steps": current_plan,
            })

    # ---- 子任务按需派发:不再在循环前无条件全量扇出,改由模型在主循环里
    #      按需调用 dispatch_review_subtasks 工具(见下方工具执行分支的特判)。
    #      派发结果作为 tool_result 回灌 messages,集成由模型在后续迭代自然完成。 ----
    loop_limit = MAX_ITERATIONS
    # 本轮因"截断且零产出"触发的收尾重试已用次数(见下方结束判断分支)
    truncation_retries = 0
    # 本轮已用派发次数(对标 Codex "避免对同一未决线程重复派发",额度见配置)
    subtask_dispatch_calls = 0
    subtask_dispatch_max_calls = max(
        0, int(getattr(settings, "REACT_FACET_DISPATCH_MAX_CALLS", 1) or 0)
    )

    for iteration in range(1, loop_limit + 1):
        logger.info(f"[task={task.id}] react_agent 第 {round_idx} 轮 / 迭代 {iteration}")

        # 暂停检查点 1:迭代边界(若用户已暂停,在此阻塞直到恢复)
        wait_if_paused(task.id, db)

        # 用户补充消息检查点:drain 队列,若有则注入到 LLM 上下文
        # 用户在对话界面输入框发的消息(运行中/暂停中场景),已由 API 端点
        # 落库为 Conversation(role=user, type=message)。发送时仅推
        # user_message_pending 事件(输入框上方待处理条目,TRAE 式),
        # 消费时刻在此补推 conversation 事件 —— 消息在 agent 实际处理的
        # 位置进入对话流,而非插在执行中的对话中间。
        # 多条消息合并为一条 user 消息(按时间顺序),避免上下文碎片化。
        pending_user_msgs = drain_user_messages(task.id)
        if pending_user_msgs:
            # 补推 conversation 事件(待处理条目 → 对话流;失败仅记录)
            try:
                _msg_ids = [
                    uuid.UUID(m["message_id"])
                    for m in pending_user_msgs if m.get("message_id")
                ]
                _drained_convs = (
                    db.query(Conversation)
                    .filter(Conversation.id.in_(_msg_ids))
                    .all()
                ) if _msg_ids else []
                for c in _drained_convs:
                    publish(task.id, "conversation", {
                        "id": str(c.id),
                        "round_idx": c.round_idx,
                        "role": c.role,
                        "type": c.type,
                        "content": c.content,
                        "reasoning": c.reasoning,
                        "attachments": c.attachments,
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                    })
            except Exception as _pub_err:
                logger.warning(
                    f"[task={task.id}] 消费消息入流事件推送失败(忽略): {_pub_err}"
                )
            # 本批消息附带的上传文件:传输入工区 followup_uploads/ 并把路径
            # 提示并入注入文本(累积 params / 传输容错 / 失败告警均由共享函数
            # apply_followup_attachments 负责,与续跑消费同构)。
            attachment_note = apply_followup_attachments(
                db, task, pending_user_msgs,
                round_idx=round_idx, task_id_str=task_id_str,
            )
            injected = format_injected_user_messages(
                pending_user_msgs, attachment_note=attachment_note
            )
            if injected:
                messages.append({"role": "user", "content": injected})
                logger.info(
                    f"[task={task.id}] react_agent 第 {round_idx} 轮 / 迭代 {iteration} "
                    f"注入 {len(pending_user_msgs)} 条用户补充消息"
                )

        # 流式调用 LLM,累积 reasoning / content / tool_calls
        # 同时通过 event_bus 实时推送 thinking_delta 给前端
        reasoning_full, content_full, tool_calls_full, finish_reason, conv_id = _stream_llm_response(
            client, task, db, round_idx, iteration, messages, tools
        )

        # 把 assistant 消息加进上下文(用于下一轮 LLM 调用)
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if content_full:
            assistant_msg["content"] = content_full
        if tool_calls_full:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments_str"],
                    },
                }
                for tc in tool_calls_full
            ]
        # 仅当确实有非空白正文或工具调用时才落这条 assistant 消息:二者皆空
        # 或正文仅空白(输出被 max_tokens 截断、零产出)时,空 assistant 会让
        # provider 报 400,且下方截断重试需要干净上下文继续补发收尾提示
        if content_full.strip() or tool_calls_full:
            messages.append(assistant_msg)

        # 落库思考(content + reasoning),供刷新页面后查看
        # 同时推 conversation 事件:流式卡片按 conv_id 退役为只读历史卡片,
        # 中途离开详情页再回来的订阅者(没收到这次的 thinking_delta 增量)
        # 也只有在这条事件到达时才能看到这段思考,否则要等下一次整页快照。
        # 正文仅空白且无 reasoning 时不落(零产出不算思考)
        if content_full.strip() or reasoning_full:
            _add_conversation(
                db, task, round_idx=round_idx,
                role="agent1", type="thinking",
                content=content_full,
                reasoning=reasoning_full,
                stream_conv_id=conv_id,
            )
            # 提取计划清单(复杂任务时 react_agent 会在 content 里输出 <plan>...</plan>)
            # 合并 LLM 显式更新到 current_plan(信任 LLM 的 done 标注),
            # 然后推送合并后的完整 plan(覆盖式更新,前端始终看到最新状态)
            llm_plan = _extract_plan(content_full)
            if llm_plan:
                current_plan = _merge_plan(current_plan, llm_plan)
                publish(task.id, "plan", {
                    "round_idx": round_idx,
                    "steps": current_plan,
                })

        # 兜底:结构化 tool_calls 为空但 content 里有 <tool_call> 文本块
        # (GLM/Qwen 等 Hermes 风格,在思考模式下可能把工具调用写在正文,
        # 而非走 OpenAI function calling 结构化通道)
        if not tool_calls_full and content_full:
            text_tool_calls = _extract_text_tool_calls(content_full)
            if text_tool_calls:
                logger.info(
                    f"[task={task.id}] 从 content 文本解析出 {len(text_tool_calls)} 个 tool_call"
                )
                tool_calls_full = text_tool_calls
                # 补回 assistant_msg 的 tool_calls(供下一轮 LLM 上下文)
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments_str"],
                        },
                    }
                    for tc in tool_calls_full
                ]
                # 从 messages 上下文里的 content 剥离 tool_call 文本块
                # (避免下一轮 LLM 重复看到工具调用文本;落库的 thinking 保留原文便于排查)
                cleaned = _strip_tool_call_blocks(content_full)
                if cleaned != content_full:
                    assistant_msg["content"] = cleaned

        # 结束判断:模型主动 stop 且无工具调用 → 真正结束
        # finish_reason=length 是被 max_tokens 截断,模型没说完,不算主动结束:
        #   - 有正文:用现有 content 作 summary 收尾(降级,记录 warning)
        #   - 正文为空/仅空白:输出预算全被思考吃掉、本轮零产出。直接收尾会让
        #     orchestrator 把任务误标为完成(静默丢结果),故注入收尾提示
        #     重试(有界);重试耗尽再降级结束
        if not tool_calls_full:
            # 遗留消息守卫:最终答案生成期间(本迭代 drain 之后)到达的用户
            # 消息 → 不结束,继续循环让下一迭代顶部 drain+注入 —— 模型在
            # 同轮上下文里立刻看到消息并继续处理(而非留到队列无人消费)
            if has_pending_messages(task.id):
                logger.info(
                    f"[task={task.id}] react_agent 最终答案后仍有用户补充消息,"
                    f"继续本轮处理(迭代 {iteration} → {iteration + 1})"
                )
                continue
            if finish_reason == "length" and not content_full.strip():
                if truncation_retries < MAX_TRUNCATION_RETRIES:
                    truncation_retries += 1
                    logger.warning(
                        f"[task={task.id}] react_agent 第 {round_idx} 轮/迭代 {iteration} "
                        f"输出被 max_tokens 截断且零产出"
                        f"(reasoning={len(reasoning_full)}字符),注入收尾提示重试"
                        f"({truncation_retries}/{MAX_TRUNCATION_RETRIES})"
                    )
                    messages.append({
                        "role": "user",
                        "content": TRUNCATION_CONTINUE_PROMPT,
                    })
                    continue
                logger.warning(
                    f"[task={task.id}] react_agent 第 {round_idx} 轮/迭代 {iteration} "
                    f"输出被 max_tokens 截断且零产出,收尾重试已达上限"
                    f"({MAX_TRUNCATION_RETRIES}),降级结束。"
                    f"reasoning={len(reasoning_full)}字符"
                )
            elif finish_reason == "length":
                logger.warning(
                    f"[task={task.id}] react_agent 第 {round_idx} 轮/迭代 {iteration} "
                    f"输出被 max_tokens 截断(finish=length),降级结束。"
                    f"reasoning={len(reasoning_full)}字符, content={len(content_full)}字符"
                )
            else:
                logger.info(
                    f"[task={task.id}] react_agent 结束"
                    f"(finish={finish_reason},无工具调用)"
                )
            if content_full.strip():
                summary = content_full
            break

        # 执行所有工具调用(按出现顺序)
        for tc in tool_calls_full:
            fn_name = tc["name"]
            try:
                fn_args = json.loads(tc["arguments_str"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            # 暂停检查点 2:工具调用前(细粒度,长工具链中也能及时响应暂停)
            wait_if_paused(task.id, db)

            # 记录工具调用签名(循环检测)
            call_sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)}"
            recent_calls.append(call_sig)
            # 修复 12:裁剪到滑动窗口范围,避免 recent_calls 无限增长
            # (仅保留最近 MAX_RECENT_CALLS 条,足够支撑连续检测 + 窗口检测)
            if len(recent_calls) > MAX_RECENT_CALLS:
                del recent_calls[: len(recent_calls) - MAX_RECENT_CALLS]

            # plan 状态推进:根据工具名推断当前在执行哪个 step,标 in_progress
            # 粗粒度(代码可判),done 标注交给 LLM 在下一轮思考时确认
            if current_plan:
                step_id = _infer_step_from_tool(fn_name, current_plan)
                if step_id is not None:
                    for s in current_plan:
                        if s["id"] == step_id:
                            if s["status"] == "pending":
                                s["status"] = "in_progress"
                                # 推送更新(状态刚变化)
                                publish(task.id, "plan", {
                                    "round_idx": round_idx,
                                    "steps": current_plan,
                                })
                            break

            # 工具意图:人类可读的一句话说明(首行) + 原始参数 JSON(后续行)
            # 格式向 qoder CLI agent 看齐:intent 首行人类可读,detail 是纯参数 JSON
            # (不含 task_id/git_tokens 等内部注入字段,只展示 LLM 传入的原始参数)
            # 前端 ConversationMessage 按 \n 拆分:首行高亮为标题,其余作为等宽详情
            intent = _build_tool_intent(fn_name, fn_args)
            call_detail = json.dumps(fn_args, ensure_ascii=False, indent=2)
            call_conv = _add_conversation(
                db, task, round_idx=round_idx,
                role="agent1", type="tool_call",
                content=f"{intent}\n{call_detail}",
            )

            # 子任务派发工具:循环内特判(需要 db/client/messages 上下文,不走
            # execute_tool 注册表)。同步派发—等待—回灌:扇出各子任务子循环,fan-in
            # 落库后把各任务结论当普通 tool_result 回灌 messages,集成由后续迭代自然完成。
            if fn_name == "dispatch_review_subtasks":
                if subtask_dispatch_calls >= subtask_dispatch_max_calls:
                    err_msg = (
                        f"本轮派发次数已达上限({subtask_dispatch_max_calls}),"
                        "请基于已有子任务结论集成,不要重复派发"
                    )
                    logger.warning(f"[task={task.id}] {err_msg}")
                    _add_conversation(
                        db, task, round_idx=round_idx,
                        role="agent1", type="tool_result",
                        content=err_msg, tool_call_id=str(call_conv.id),
                    )
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "content": err_msg,
                    })
                    continue
                subtask_dispatch_calls += 1
                try:
                    results, fan_in_summary = _dispatch_subtasks(
                        task, db, round_idx, client, tools, messages,
                        executor_command_confirm,
                        fn_args.get("subagents") or [],
                        visible_skill_names,
                    )
                    result_items = _subtask_result_items(results)
                    payload = {
                        "subagents": [
                            {"label": n, "status": s, "summary": t}
                            for n, s, t in result_items
                        ],
                        "summary": fan_in_summary,
                    }
                    result_str = json.dumps(payload, ensure_ascii=False, default=str)
                    _add_conversation(
                        db, task, round_idx=round_idx,
                        role="agent1", type="tool_result",
                        content=result_str, tool_call_id=str(call_conv.id),
                    )
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "content": result_str,
                    })
                except Exception as e:
                    err_msg = f"子任务派发失败: {e}"
                    logger.error(f"[task={task.id}] {err_msg}")
                    _add_conversation(
                        db, task, round_idx=round_idx,
                        role="agent1", type="tool_result",
                        content=err_msg, tool_call_id=str(call_conv.id),
                    )
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"], "content": err_msg,
                    })
                continue

            # 普通工具:执行(submit_results 已移除,react_agent 不再提交结构化结果)
            try:
                result = execute_tool(fn_name, fn_args)
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="agent1", type="tool_result",
                    content=result_str,
                    tool_call_id=str(call_conv.id),
                )
                # 完整结果传给 LLM(工具自身已控制返回量:
                # read_file 默认 200 行、search_code 默认 50 匹配等)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
            except Exception as e:
                err_msg = f"工具执行失败: {e}"
                logger.error(f"[task={task.id}] {err_msg}")
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="agent1", type="tool_result",
                    content=err_msg,
                    tool_call_id=str(call_conv.id),
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": err_msg,
                })

        # plan 提醒注入:把当前 plan 状态作为 system 消息加到 messages,
        # 让 LLM 在下一轮思考时看到进度,决定是否标 done(细粒度状态确认)
        # 用可替换的 system 消息(不累积,避免 messages 膨胀):
        # 找到上一轮注入的 plan 提醒就替换,否则追加新的
        if current_plan:
            reminder = format_plan_reminder(current_plan)
            if reminder:
                # 查找并替换已有的 plan 提醒消息(避免累积)
                replaced = False
                for i in range(len(messages) - 1, -1, -1):
                    msg = messages[i]
                    if msg.get("role") == "system" and msg.get("content", "").startswith(
                        "[系统提醒] 当前计划清单状态"
                    ):
                        msg["content"] = reminder
                        replaced = True
                        break
                if not replaced:
                    messages.append({"role": "system", "content": reminder})

        # 循环检测(修复 12:在连续相同检测基础上,增加滑动窗口检测)
        # 1) 连续 MAX_SAME_CALLS 次完全相同调用 → 死循环(A,A,A)
        # 2) 滑动窗口 LOOP_WINDOW_SIZE 内不同 call_sig ≤ LOOP_MIN_DISTINCT →
        #    交替循环(A,B,A,B,A,B 或 A,A,B,A,A,B 这类低多样性重复)
        is_loop = False
        loop_reason = ""
        if (
            len(recent_calls) >= MAX_SAME_CALLS
            and len(set(recent_calls[-MAX_SAME_CALLS:])) == 1
        ):
            is_loop = True
            loop_reason = f"连续 {MAX_SAME_CALLS} 次相同调用"
        elif len(recent_calls) >= LOOP_WINDOW_SIZE:
            window = recent_calls[-LOOP_WINDOW_SIZE:]
            distinct = len(set(window))
            if distinct <= LOOP_MIN_DISTINCT:
                is_loop = True
                loop_reason = (
                    f"最近 {LOOP_WINDOW_SIZE} 次调用仅 {distinct} 种不同签名"
                    f"(交替循环),如 {window[:3]}..."
                )

        if is_loop:
            logger.warning(f"[task={task.id}] 检测到循环({loop_reason}),打破")
            _add_conversation(
                db, task, round_idx=round_idx,
                role="agent1", type="thinking",
                content=f"检测到调用循环({loop_reason}),强制转入总结",
            )
            messages.append({
                "role": "user",
                "content": LOOP_BREAK_PROMPT,
            })
    else:
        # 循环跑满了,让 react_agent 输出自然语言总结
        # 上限用 loop_limit 而非常量:合成回合的预算比常态串行小,标签要如实
        logger.warning(
            f"[task={task.id}] react_agent 达到最大迭代次数({loop_limit})"
        )
        messages.append({
            "role": "user",
            "content": MAX_ITERATION_PROMPT,
        })
        try:
            reasoning_full, content_full, tool_calls_full, finish_reason, _conv_id = _stream_llm_response(
                client, task, db, round_idx, loop_limit, messages, tools
            )
            # content 作为 summary(自然语言总结);仅空白不落 summary
            if content_full.strip():
                summary = content_full
        except Exception as e:
            logger.error(f"[task={task.id}] 最终总结失败: {e}")

    # react_agent 不再落库 results(重点与知识点由 agent2 审查完成时
    # 经 results 字段输出并落库)
    if not summary:
        summary = "执行完成"

    # 修复 4:返回本轮结束时的 plan 状态,供 orchestrator 传给下一轮
    return [], summary, current_plan


# ============================================================
# 子任务按需派发(主循环内工具驱动扇出 / fan-in 主线程串行落库)
#
# 形状复用 practice/generator.py 已验证的 provider 闸门 + ThreadPoolExecutor,
# 但把并行边界收得更紧:worker 只跑 LLM + 只读工具 + 内存缓冲,绝不碰共享
# db session / publish / current_plan(全在 fan-in 主线程逐条做)。子任务
# 带原生身份(label,落库仍写 facet 列),规避 ACP 侧不带身份导致的并行 chunk 交织乱码。
# 派发时机由模型自己定(在主循环里按需调 dispatch_review_subtasks 工具),而非
# 编排器在循环前无差别全量扇出;开关关闭时不注册该工具(纯串行)。
# ============================================================

# 会改动工作区/沙箱物理状态的工具:并行子任务一律禁用(共享工作区不可并发写)。
# 审查子任务本就只需读取/静态分析。
_SUBTASK_MUTATING_TOOLS: frozenset[str] = frozenset({
    "clone_repo", "write_file", "run_python_code", "run_command", "str_replace_editor",
})

# 收尾轮(loop-break / max-iter / timeout)的墙钟宽限(秒):对已到期的
# deadline 再叠一段宽限,保证收尾这一发 LLM 能真正吏出总结而非被 stop_check 秒停。
_SUBTASK_FINALIZE_GRACE_SECONDS = 90

# 审查链路厂商级并发闸门(按 provider+model 归组,跨任务共享)
_review_provider_gates: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_review_provider_gates_lock = threading.Lock()


@contextmanager
def _review_provider_gate(client: LLMClient):
    """限制同一厂商同时进行中的审查请求数(镜像 practice 的 _provider_gate)。

    子任务并行会成倍放大并发 LLM 调用,而真正卡点是厂商配额/限流(本次真凶是
    403 free-quota)。闸门让全进程对该厂商的在途审查请求不超
    REACT_PROVIDER_MAX_CONCURRENCY;并发度=1 时本质是透传锁(不改行为)。
    """
    key = (
        f"{getattr(client, 'provider_id', '')}"
        f"|{getattr(client, 'model', '')}"
        f"|{getattr(client, 'base_url_override', '') or ''}"
    )
    limit = max(1, settings.REACT_PROVIDER_MAX_CONCURRENCY)
    with _review_provider_gates_lock:
        entry = _review_provider_gates.get(key)
        if entry is None or entry[0] != limit:
            entry = (limit, threading.BoundedSemaphore(limit))
            _review_provider_gates[key] = entry
    sem = entry[1]
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


# 按需取用的 skill 工具:子任务 skills 为空时从工具清单剔除(skill_tool 把
# allowed_skills=[] 当"全部可用",空列表无法表达"不可见",剔工具最干净)
_SKILL_TOOLS: frozenset[str] = frozenset({"list_skills", "skill"})

# 子任务子循环禁用集 = 会改动工作区的工具 ∪ 只在主循环有 handler 的派发工具。
# dispatch_review_subtasks 既不能被嵌套派发,留在子循环清单里只会让子模型误以为
# 可用(工具定义 + 共享 prompt 的"仅当工具列表里有…适用"同时成立),调用即「未知工具」。
_SUBTASK_FORBIDDEN_TOOLS: frozenset[str] = _SUBTASK_MUTATING_TOOLS | frozenset({
    "dispatch_review_subtasks",
})


def _subtask_readonly_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从全量工具定义里剔除子循环不可用的工具(改动类 + 嵌套派发),得到只读基集。"""
    return [
        t for t in tools
        if (t.get("function") or {}).get("name") not in _SUBTASK_FORBIDDEN_TOOLS
    ]


def _subtask_tools(
    readonly_tools: list[dict[str, Any]], skills: list[str],
) -> list[dict[str, Any]]:
    """按子任务的可见技能子集裁剪工具清单:skills 非空 → 保留 skill 工具(子任务
    按需用 skill 拉正文);skills 为空 → 剔除 list_skills/skill(纯 instruction 子任务
    不该看到技能工具)。"""
    if skills:
        return readonly_tools
    return [
        t for t in readonly_tools
        if (t.get("function") or {}).get("name") not in _SKILL_TOOLS
    ]


def _subtask_allowed_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    return {(t.get("function") or {}).get("name") for t in tools} - {None}


def apply_followup_attachments(
    db: Session,
    task: Task,
    pending_user_msgs: list[dict[str, Any]],
    *,
    round_idx: int,
    task_id_str: str,
) -> str:
    """把一批排队用户消息附带的上传文件传输入工区,返回并入注入文本的附件提示。

    drain 路径(react_agent 迭代边界)与续跑消费路径(orchestrator 自动续跑)
    共用同一处理(P1-2):
    - 按 params.followup_upload_ids 全量累积列表传输(全局下标),与沙箱回收后
      重放 / 工作区回退浏览同一布局;本轮新 ids 先并入 params 再传;
    - 传输失败 catch+log,不中断本轮(文字消息照常注入),同时落一条 system/warning
      + 如实告知模型(不能让模型读到不存在的文件);单个上传失败逐上传容错。
    返回注入用的 attachment_note(无附件时为空串)。
    """
    batch_upload_ids: list[str] = []
    for m in pending_user_msgs:
        for uid in (m.get("upload_ids") or []):
            if uid and uid not in batch_upload_ids:
                batch_upload_ids.append(uid)
    if not batch_upload_ids:
        return ""

    from app.services.upload_layout import extract_followup_ids
    from app.tools import sandbox_tools

    existing = extract_followup_ids(task.params)
    merged = existing + [u for u in batch_upload_ids if u not in existing]
    # 持久化累积(非 JSONB 突变):params 是重放与回退浏览的唯一真源,
    # 先落库再传输(传输失败也不丢重放依据)
    task.params = {**(task.params or {}), "followup_upload_ids": merged}
    try:
        db.commit()
    except Exception as e:
        logger.warning(f"[task={task.id}] 追问上传累积落库失败(忽略): {e}")
        db.rollback()
    added: list[str] = []
    fail_reason = ""
    try:
        added = list(
            sandbox_tools.add_uploads_to_workspace(
                task_id_str, merged, "followup_uploads"
            ) or []
        )
    except Exception as e:
        fail_reason = str(e)
        logger.warning(
            f"[task={task.id}] 追问上传文件传输失败(忽略,文字消息照常注入): {e}"
        )
    if added:
        ws_root = (
            sandbox_tools.get_workspace_info(task_id_str) or {}
        ).get("repo_path", "")
        return format_followup_attachment_note(added, ws_root)
    # 一个也没落地:告知模型实情(防臆测)+ 对话流里留一条可见告警
    try:
        _add_conversation(
            db, task, round_idx=round_idx,
            role="system", type="warning",
            content=format_followup_upload_warning(fail_reason),
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 追问附件告警落库失败(忽略): {e}")
    return ATTACHMENT_UNAVAILABLE_NOTE


def _subtask_stream_turn(
    client: LLMClient,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    *,
    task: Task,
    round_idx: int,
    label: str,
    iteration: int,
    deadline: float,
) -> tuple[str, str, list[dict[str, Any]], str | None, str]:
    """子任务实时流式调用:边收 chunk 边 publish 带 label 身份的 thinking_delta,
    累积 reasoning/content/tool_calls,不落库、不碰 current_plan。

    与主循环共用 stream_llm(同一条 thinking_delta 通道),但:
    ① extra 带 facet/parallel 身份(facet 列名不变,值写 label),前端按每次调用
       唯一的 conv_id 天然分桶,规避多路并行交织(ACP 侧正因不带身份才乱码);
       工具类行不实时、仍走 fan-in。
    ② 外包厂商闸门限并发,防并行放大把配额打爆;
    ③ deadline 逐 chunk 协作式收流(stop_check),超时返回部分输出。
    返回 (reasoning, content, tool_calls, finish_reason, conv_id)。
    """
    with _review_provider_gate(client):
        # max_tokens 同主循环:取模型解析后的输出上限,避免思考把 4K 预算
        # 烧光导致子任务零产出被截断(chat_stream 仍会向下钳制)
        res = stream_llm(
            client, messages,
            task_id=task.id, round_idx=round_idx, role="agent1",
            tools=tools, iteration=iteration,
            max_tokens=client.max_output_tokens,
            extra={"facet": label, "parallel": True},
            stop_check=lambda: time.monotonic() > deadline,
        )
    return res.reasoning, res.content, res.tool_calls, res.finish_reason, res.conv_id


@dataclass
class _SubtaskResult:
    """单个子任务子循环的产物(纯内存,由 fan-in 主线程负责落库)。

    pending_rows: 顺序的待落库对话行 {role,type,content,reasoning?},
        tool_call/tool_result 相邻成对(fan-in 按邻接补 tool_call_id);
        thinking 行额外带 stream_conv_id(实时流式卡片的 conv_id,fan-in
        据此把实时卡退役成历史卡)。
    label: 子任务身份标签(fan-in 落库写 facet 列,值即此 label)。
    timed_out: 本子任务是否因墙钟预算超时收尾(部分结论)。
    """
    label: str
    summary: str = ""
    pending_rows: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    timed_out: bool = False


def _run_subtask_pass(
    spec: dict[str, Any],
    base_messages: list[dict[str, Any]],
    readonly_tools: list[dict[str, Any]],
    client: LLMClient,
    task: Task,
    round_idx: int,
    max_iter: int,
    executor_command_confirm: str,
    deadline: float,
) -> _SubtaskResult:
    """跑一个子任务的有界只读 ReAct 子循环(在 worker 线程执行,全程无共享副作用)。

    与主循环同机制(LLM→只读工具→再思考直到 stop),但:独立 set_current_task
    (ContextVar per-thread)、只读工具、无 db、pending_rows 缓冲;thinking 实时
    流式(带 label 身份),工具类行仅缓存待 fan-in 落库。
    spec: 主 agent 为该子任务给的 {label, instruction, skills};技能正文不再预注入,
    skills 非空时保留 skill 工具供子任务按需拉正文,skills 为空时从工具清单剔除
    list_skills/skill(见 _subtask_tools)。
    deadline:本子任务的墙钟上界(time.monotonic() 基准),循环边界与收 chunk
    都协作式检查,超时转入总结收尾(部分结论)并置 timed_out,不拖垮 fan-in 整轮。
    """
    label = spec["label"]
    skills = spec.get("skills") or []
    result = _SubtaskResult(label=label)
    # 按本子任务的技能子集裁剪工具清单(skills 空则剔 skill 工具),allowed_names 现算
    sub_tools = _subtask_tools(readonly_tools, skills)
    allowed_names = _subtask_allowed_tool_names(sub_tools)
    try:
        # ContextVar 每个线程独立:ThreadPoolExecutor 不会继承主线程上下文,
        # 必须在 worker 里显式 set(skill 工具据此只看本子任务选定的技能子集)
        set_current_task(
            str(task.id), task.scenario,
            allowed_skills=skills or None,
            user_id=task.user_id,
            executor_command_confirm=executor_command_confirm,
        )
        messages: list[dict[str, Any]] = [dict(m) for m in base_messages]
        # 子任务角色/要求作为独立 system 指令层(不污染真实用户问题);技能正文
        # 不再预注入,子任务按需用 skill 工具自行读取
        messages.append({
            "role": "system",
            "content": build_subtask_instruction(label, spec.get("instruction", "")),
        })

        recent_calls: list[str] = []
        summary = ""
        # 零产出截断有界重试计数(镜像主循环/verifier,每子任务独立)
        truncation_retries = 0

        def _finalize_turn(prompt_text: str, notice: str | None, it: int) -> None:
            """收尾轮(loop-break / max-iter / timeout 共用)。

            可选先落一条 notice 思考行,注入收尾提示后再跑一次流式产出总结。
            用独立宽限 deadline(time.monotonic()+_SUBTASK_FINALIZE_GRACE_SECONDS)
            保证即便主预算已到期这一发也能真正吏出总结而非被 stop_check 秒停。
            """
            nonlocal summary
            if notice:
                result.pending_rows.append({
                    "role": "agent1", "type": "thinking",
                    "content": notice, "reasoning": "",
                })
            messages.append({"role": "user", "content": prompt_text})
            finalize_deadline = time.monotonic() + _SUBTASK_FINALIZE_GRACE_SECONDS
            try:
                _r, c, _tc, _f, cid = _subtask_stream_turn(
                    client, messages, sub_tools,
                    task=task, round_idx=round_idx, label=label,
                    iteration=it, deadline=finalize_deadline,
                )
            except Exception:
                return
            if c:
                summary = c
                result.pending_rows.append({
                    "role": "agent1", "type": "thinking",
                    "content": c, "reasoning": _r, "stream_conv_id": cid,
                })

        for _it in range(1, max_iter + 1):
            # 超时守卫:循环边界先查预算,命中即转总结收尾(不再开新回合)
            if time.monotonic() > deadline:
                result.timed_out = True
                _finalize_turn(
                    MAX_ITERATION_PROMPT,
                    "检测到达到时间预算,强制转入本子任务总结", _it,
                )
                break
            reasoning, content, tool_calls, _finish, conv_id = _subtask_stream_turn(
                client, messages, sub_tools,
                task=task, round_idx=round_idx, label=label,
                iteration=_it, deadline=deadline,
            )
            # Hermes 风格文本 tool_call 兜底(部分模型把调用写进正文)
            if not tool_calls and content:
                tool_calls = _extract_text_tool_calls(content)
                if tool_calls:
                    content = _strip_tool_call_blocks(content)
            result.pending_rows.append({
                "role": "agent1", "type": "thinking",
                "content": content, "reasoning": reasoning,
                "stream_conv_id": conv_id,
            })
            # 零产出截断重试(镜像主循环 L508-527 / verifier):finish=length 且正文空、
            # 无工具调用 = 本轮输出预算全被思考吃掉。直接 break 会让子任务静默产出空
            # 结论(fan-in 仅得占位)。注入收尾提示有界重试;此处不 append 空 assistant
            # (避免脏上下文),改而 append 一条 user 提示后 continue 开下一迭代。
            if not tool_calls and _finish == "length" and not content.strip():
                if truncation_retries < MAX_TRUNCATION_RETRIES:
                    truncation_retries += 1
                    logger.warning(
                        f"[task={task.id}] react_agent 子任务 {label} 第 {_it} 迭代"
                        f"输出被 max_tokens 截断且零产出"
                        f"(reasoning={len(reasoning)}字符),注入收尾提示重试"
                        f"({truncation_retries}/{MAX_TRUNCATION_RETRIES})"
                    )
                    messages.append({
                        "role": "user",
                        "content": TRUNCATION_CONTINUE_PROMPT,
                    })
                    continue
                logger.warning(
                    f"[task={task.id}] react_agent 子任务 {label} 输出被 max_tokens"
                    f"截断且零产出,收尾重试已达上限({MAX_TRUNCATION_RETRIES}),"
                    f"按空结论收尾"
                )
            # 镜像串行主循环(见上方 assistant_msg 构造):assistant 必须带 tool_calls,
            # 否则下一迭代序列里紧接的 {role:tool} 会违反 OpenAI 契约被拒 400
            # ("tool must be a response to a preceding message with tool_calls")。
            # 文本 tool_call 兜底路径产生的合成 id 与下方 tool 消息同源(tc["id"]),天然匹配。
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if content:
                assistant_msg["content"] = content
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments_str"],
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                if content:
                    summary = content
                break

            for tc in tool_calls:
                fn = tc["name"]
                try:
                    fn_args = json.loads(tc.get("arguments_str") or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                recent_calls.append(f"{fn}:{json.dumps(fn_args, sort_keys=True)}")
                intent = _build_tool_intent(fn, fn_args)
                result.pending_rows.append({
                    "role": "agent1", "type": "tool_call",
                    "content": f"{intent}\n{json.dumps(fn_args, ensure_ascii=False, indent=2)}",
                })
                # 只读白名单硬约束:模型若仍请求改动工具,拒绝并如实告知(不执行)
                if fn not in allowed_names:
                    blocked = f"并行子任务只允许只读工具,已禁用: {fn}"
                    result.pending_rows.append({"role": "agent1", "type": "tool_result", "content": blocked})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": blocked})
                    continue
                try:
                    tool_out = json.dumps(execute_tool(fn, fn_args), ensure_ascii=False, default=str)
                except Exception as e:
                    tool_out = f"工具执行失败: {e}"
                result.pending_rows.append({"role": "agent1", "type": "tool_result", "content": tool_out})
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_out})

            # 循环检测:连续 max_same 次相同调用则打破,转入总结
            if (
                len(recent_calls) >= MAX_SAME_CALLS
                and len(set(recent_calls[-MAX_SAME_CALLS:])) == 1
            ):
                _finalize_turn(
                    LOOP_BREAK_PROMPT, "检测到调用循环,强制转入本子任务总结", _it,
                )
                break
        else:
            # 跑满迭代:请模型就本子任务输出总结
            _finalize_turn(MAX_ITERATION_PROMPT, None, max_iter)
        result.summary = summary
    except Exception as e:  # 单个子任务失败不拖垮整轮
        logger.exception(f"[task={task.id}] 子任务 {label} 子循环失败")
        result.error = str(e)
    return result


def _visible_skill_names(task: Task) -> list[str]:
    """当前任务可见技能名(用户隔离 ∩ allowed_skills),与内置 skill 工具同口径。

    仅用于:① 注入 dispatch_review_subtasks 工具描述的可用技能名清单;
    ② 校验每个子任务的 skills ⊆ 可见集。可为空(子任务可纯 instruction,技能数
    不再决定是否注册派发工具)。跨场景同名去重保序,与 skill_tool 口径一致。
    """
    from app.skills import loader as skill_loader
    names: list[str] = []
    seen: set[str] = set()
    for s in skill_loader.resolve_visible_skills(task.user_id, task.allowed_skills):
        n = getattr(s, "name", str(s))
        if n not in seen:
            seen.add(n)
            names.append(n)
    return names


def _build_subtask_dispatch_tool(skill_names: list[str]) -> dict[str, Any]:
    """构造 dispatch_review_subtasks 的 function-calling 定义。

    可用技能名动态注入描述与 subagents[].skills 参数说明(对标 Codex 把角色
    description 注入 spawn 工具供模型选角色):模型只能从这些名字里挑子集,写错直接
    报错。本工具不在子任务只读白名单里,子任务循环无法再派发(结构性保证派生深度=1)。
    """
    from app.prompts.executor import SUBTASK_INSTRUCTION_MAX_CHARS
    names_text = ", ".join(skill_names) if skill_names else "(无)"
    if skill_names:
        skills_desc = (
            f"该子任务可见/可参考的技能名(可选,只能是这些名字的子集,可为空"
            f"):{names_text}"
        )
    else:
        skills_desc = "该子任务可见的技能名(当前任务无可见技能,请留空)"
    return {
        "type": "function",
        "function": {
            "name": "dispatch_review_subtasks",
            "description": DISPATCH_SUBTASKS_TOOL_DESCRIPTION_TEMPLATE.format(
                skill_names=names_text
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subagents": {
                        "type": "array",
                        "description": "要派发的子任务列表(每项 = 一条独立只读核查子循环)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "instruction": {
                                    "type": "string",
                                    "description": (
                                        "你为该子任务写的要求/上下文(必填,"
                                        f"≤ {SUBTASK_INSTRUCTION_MAX_CHARS} 字)"
                                    ),
                                },
                                "skills": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": skills_desc,
                                },
                                "label": {
                                    "type": "string",
                                    "description": (
                                        "子任务身份标签(可选,缺省按序号生成「子任务 N」)"
                                    ),
                                },
                            },
                            "required": ["instruction"],
                        },
                    },
                },
                "required": ["subagents"],
            },
        },
    }


def _fan_in(
    task: Task,
    db: Session,
    round_idx: int,
    results: list[_SubtaskResult],
) -> str:
    """主线程串行 fan-in:按子任务顺序逐条落库 pending_rows(带 label 标签,落库仍
    写 facet 列)+ 拼接总结。tool_result 按邻接补 tool_call_id,与串行落库形状一致。
    """
    summaries: list[str] = []
    for fr in results:
        last_call_id: str | None = None
        for row in fr.pending_rows:
            tool_call_id = last_call_id if row["type"] == "tool_result" else None
            conv = _add_conversation(
                db, task, round_idx=round_idx,
                role=row["role"], type=row["type"],
                content=row.get("content") or "",
                reasoning=row.get("reasoning"),
                tool_call_id=tool_call_id,
                stream_conv_id=row.get("stream_conv_id"),
                facet=fr.label,
                extra_payload={"facet": fr.label, "parallel": True},
            )
            if row["type"] == "tool_call":
                last_call_id = str(conv.id)
        if fr.error:
            summaries.append(f"### {fr.label}\n(该子任务审查失败:{fr.error})")
        elif fr.timed_out:
            summaries.append(
                f"### {fr.label}\n{fr.summary or '(本子任务无明确结论)'}\n(部分结论,已超时收尾)"
            )
        else:
            summaries.append(f"### {fr.label}\n{fr.summary or '(本子任务无明确结论)'}")
    return "\n\n".join(summaries) if summaries else "执行完成"


def _subtask_result_items(results: list["_SubtaskResult"]) -> list[tuple[str, str, str]]:
    """把子任务结果映射成 (label, status, 已截断结论),供合成回合回灌装配。

    status 判定与 _fan_in 的小标题分支同源(error / timed_out / ok),两处不要写歪。
    结论按 REACT_FACET_SUMMARY_MAX_TOKENS 截断:并行放大的是"份数",每份必须有界,
    否则 N 份长结论会把父上下文撑爆。

    注入总量只记一条 info 日志,不设聚合闸门(P0 先取数,后续再定是否补上限)。
    """
    max_tokens = max(
        1, int(getattr(settings, "REACT_FACET_SUMMARY_MAX_TOKENS", 3000) or 3000)
    )
    items: list[tuple[str, str, str]] = []
    total_tokens = 0
    for fr in results:
        if fr.error:
            status, raw = "error", fr.error
        elif fr.timed_out:
            status, raw = "timed_out", fr.summary
        else:
            status, raw = "ok", fr.summary
        clipped = _clip_to_tokens(raw or "", max_tokens)
        total_tokens += _estimate_tokens(clipped)
        items.append((fr.label, status, clipped))
    logger.info(
        f"[子任务回灌] {len(items)} 份子任务结论合计粗估 {total_tokens} token"
        f"(单份上限 {max_tokens})"
    )
    return items


def _drop_inflight_assistant_tool_calls(
    messages: list[dict[str, Any]], *, task_id: Any = None,
) -> list[dict[str, Any]]:
    """剔除"本轮尚未收口"的 assistant tool_calls,给子任务一份语义干净的基座。

    主循环的顺序是"assistant(带 tool_calls)先入栈 → 逐个执行工具 → 回灌 tool
    结果",所以在 dispatch_review_subtasks 执行的此刻,messages 的最后一条就是刚入栈
    的 assistant,它的 tool 结果要等派发返回后才追加。把这份基座原样交给子任务子循环,
    子循环第一次 LLM 调用就会被 provider 拒 400("An assistant message with
    'tool_calls' must be followed by tool messages responding to each
    'tool_call_id'")—— 线上任务 2b38f1ef 即此因,两个子任务都在首调用就产出 error 结论。

    与发送口的 runtime.normalize_call_output_pairing(正确性兜底:给缺响应的 call 补
    合成响应)分工不同,这里按**语义**整条剔除:那条派发是父智能体的动作,而
    dispatch_review_subtasks 不在子任务只读白名单里,保留它(哪怕配合成响应)会让子任务
    误以为"我调用了派发工具",既归属误导又诱导它在思考里讨论派发。
    agent2 侧同类机制走"只取 system/user"的粗滤(见 _fan_out_plan_items);子任务要保留
    模型已积累的任务理解,故不能照搬,只能精确剔掉未收口的这一条。

    已收口的历史(各次工具调用/结果/思考)全部保留。返回新列表、不改入参:主循环
    随后照常把派发的 tool_result 回灌自己的 messages。
    """
    answered = {
        str(m["tool_call_id"]) for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    dropped_ids: set[str] = set()
    kept: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            call_ids = [
                str(tc["id"]) for tc in m["tool_calls"]
                if isinstance(tc, dict) and tc.get("id")
            ]
            if call_ids and not all(cid in answered for cid in call_ids):
                # 未收口:整条剔除(部分已应答的多调用也一并剔,避免留下半截配对)
                dropped_ids.update(call_ids)
                continue
        kept.append(dict(m))
    if dropped_ids:
        # 被剔 assistant 的已应答 tool 行随之成了无归属孤儿(provider 契约占位符),
        # 一并清掉;其余消息里同名 id 不存在(tool_call_id 全局唯一)
        kept = [
            m for m in kept
            if not (m.get("role") == "tool" and str(m.get("tool_call_id")) in dropped_ids)
        ]
        logger.info(
            f"[task={task_id}] react_agent 子循环基座剔除未收口的 "
            f"assistant tool_calls:{sorted(dropped_ids)}"
        )
    return kept


def _dispatch_subtasks(
    task: Task,
    db: Session,
    round_idx: int,
    client: LLMClient,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    executor_command_confirm: str,
    requested_subagents: list[dict[str, Any]],
    available_names: list[str],
) -> "tuple[list[_SubtaskResult], str]":
    """工具驱动的扇出/扇入(同步派发—等待—回灌)。

    子任务列表来自工具参数(每项 {instruction, skills, label}):
    - 每项 instruction strip 后非空;skills ⊆ 可见技能名(未知 → ValueError);
      项数 ≤ 上限;label 缺省按序号「子任务 N」;skills 去重保序。
    messages 基底 = 主循环当前 messages(模型已积累的任务理解随派发传递),再经
    _drop_inflight_assistant_tool_calls 剔掉本轮未收口的那条 assistant(此刻派发自己的
    tool_result 尚未回灌,原样传递会让子任务首调用被拒 400)。扇出主体(_run_subtask_pass
    的 deadline/厂商闸门/只读白名单/收尾宽限、wait_if_paused 检查点、fan-in 落库)原样保留。

    返回 (子任务结果列表, fan-in 拼接串);调用方把结果组装成 tool_result 回灌。
    校验失败(空列表/缺 instruction/未知技能/超上限)抛 ValueError,由调用方当工具失败处理。
    """
    if not requested_subagents:
        raise ValueError("subagents 不能为空")
    max_subtasks = int(getattr(settings, "REACT_FACET_DISPATCH_MAX_FACETS", 0) or 0)
    if max_subtasks and len(requested_subagents) > max_subtasks:
        raise ValueError(f"单次派发子任务数超过上限 {max_subtasks}")

    available_set = set(available_names or [])
    specs: list[dict[str, Any]] = []
    for i, raw in enumerate(requested_subagents):
        if not isinstance(raw, dict):
            raise ValueError(f"子任务 #{i + 1} 必须是对象")
        instruction = str(raw.get("instruction") or "").strip()
        if not instruction:
            raise ValueError(f"子任务 #{i + 1} 的 instruction 不能为空")
        # skills 去重保序 + 可见子集校验
        raw_skills = raw.get("skills") or []
        if not isinstance(raw_skills, list):
            raise ValueError(f"子任务 #{i + 1} 的 skills 必须是数组")
        skills: list[str] = []
        seen_s: set[str] = set()
        for s in raw_skills:
            name = str(s or "").strip()
            if not name:
                continue
            if name not in available_set:
                raise ValueError(
                    f"子任务 #{i + 1} 含未知技能: {name};可用技能: {sorted(available_set)}"
                )
            if name not in seen_s:
                seen_s.add(name)
                skills.append(name)
        label = str(raw.get("label") or "").strip() or f"子任务 {i + 1}"
        specs.append({"label": label, "instruction": instruction, "skills": skills})

    # 只读基集算一次;各子任务按自身 skills 在 worker 里裁剪 skill 工具(见 _run_subtask_pass)
    readonly_tools = _subtask_readonly_tools(tools)
    max_workers = min(len(specs), max(1, settings.REACT_PARALLEL_MAX_CONCURRENCY))
    max_iter = max(1, settings.REACT_FACET_MAX_ITERATIONS)
    # 统一算一次扇出基准 deadline(墙钟预算),贯穿本次派发的所有子任务
    deadline = time.monotonic() + max(1, settings.REACT_FACET_TIMEOUT_SECONDS)

    logger.info(
        f"[task={task.id}] react_agent 按需派发 {len(specs)} 个子任务:"
        f"{[s['label'] for s in specs]},max_workers={max_workers},"
        f"max_iter/子任务={max_iter},timeout/子任务={settings.REACT_FACET_TIMEOUT_SECONDS}s"
    )
    # 扇出前暂停检查点(长工具链中也能及时响应暂停)
    wait_if_paused(task.id, db)

    results: list[_SubtaskResult] = []
    # 子循环基座:L1 语义层清洗(见 _drop_inflight_assistant_tool_calls),
    # 发送口另有 runtime.normalize_call_output_pairing 作正确性兜底
    base_messages = _drop_inflight_assistant_tool_calls(messages, task_id=task.id)
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"react-subtask-{str(task.id)[:8]}",
    ) as pool:
        # 按子任务顺序提交并收集,保证 fan-in 落库/展示顺序确定(与完成顺序无关)
        futures = [
            pool.submit(
                _run_subtask_pass, spec, base_messages, readonly_tools,
                client, task, round_idx, max_iter, executor_command_confirm,
                deadline,
            )
            for spec in specs
        ]
        for fut in futures:
            results.append(fut.result())

    fan_in_summary = _fan_in(task, db, round_idx, results)
    # fan-in 后再检查一次暂停
    wait_if_paused(task.id, db)
    return results, fan_in_summary


# ============================================================
# 流式 LLM 调用:边收 token 边推送 thinking_delta 给前端
# ============================================================


def _stream_llm_response(
    client: LLMClient,
    task: Task,
    db: Session,
    round_idx: int,
    iteration: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[str, str, list[dict[str, Any]], str, str]:
    """流式调用 LLM(runtime.stream_llm 的薄包装)

    保留模块级包装(而非主循环直连 runtime):签名与返回值保持不变,
    测试与扩展点仍可按模块属性替换。实际收 chunk / 推 thinking_delta /
    工具调用跨 chunk 累积 / perf 打点均由 runtime.stream_llm 统一实现。

    返回 (reasoning_full, content_full, tool_calls_full, finish_reason, conv_id)
        - reasoning_full: 完整思考链(落库供回看)
        - content_full: 完整回答内容
        - tool_calls_full: 完整工具调用列表
            [{"id": str, "name": str, "arguments_str": str, "index": int}]
        - finish_reason: 流结束原因('stop' / 'tool_calls' / 'length' 等),
            供调用方判断"模型是否主动结束"。None 表示异常中断。
        - conv_id: 这次 LLM 调用的流式卡片标识,落库 thinking 时作为
            stream_conv_id 一起推给前端,供实时卡片与历史记录对账退役
    """
    # 输出上限取模型解析后的能力值(用户设置 > catalog > 系统默认),
    # 而非 stream_llm 的 4096 默认值:思考型模型(reasoning 也占用输出预算)
    # 在长分析任务里很容易把 4K 预算全烧在思考上,导致零产出被截断。
    # chat_stream 仍会向下钳制到模型真实上限,故此处传入解析值安全。
    result = stream_llm(
        client, messages,
        task_id=task.id, round_idx=round_idx, role="agent1",
        tools=tools, iteration=iteration,
        max_tokens=client.max_output_tokens,
    )
    return (
        result.reasoning,
        result.content,
        result.tool_calls,
        result.finish_reason,
        result.conv_id,
    )


# ---- plan 状态维护(代码驱动 + LLM 显式更新合并) ----
#
# 设计思路(参考 LangGraph Plan-and-Execute):
# - 代码维护权威 current_plan,不依赖 LLM 每轮重写
# - 工具调用前:根据 tool_name 推断当前 step,标 in_progress(粗粒度,代码可判)
# - 工具调用后:把当前 plan 状态作为 system 提醒注入 messages,
#              让 LLM 在下一轮思考时决定标 done(细粒度,需语义判断)
# - LLM 在 thinking 里输出新 <plan> 时:合并到 current_plan,
#              信任 LLM 的 done 标注(它有 tool_result 上下文,判断更准)
# - 双向同步:代码推进 in_progress,LLM 确认 done

# 工具名 → plan 步骤关键词映射(用于推断当前在执行哪个 step)
# key 是工具名,value 是匹配 step.text 的关键词列表(任一命中即匹配)
_TOOL_STEP_KEYWORDS: dict[str, list[str]] = {
    "clone_repo":      ["克隆", "clone", "仓库"],
    "list_files":      ["结构", "目录", "查看", "list"],
    "find_files":      ["查找", "定位", "文件", "find"],
    "read_file":       ["读取", "依赖", "清单", "read"],
    "query_cve":       ["依赖", "cve", "漏洞"],
    "list_dependencies": ["依赖", "清单", "dependency", "锁文件", "lockfile"],
    "write_file":      ["写入", "补丁", "poc", "报告", "生成", "write"],
    "run_python_code": ["执行", "运行", "验证", "测试", "poc", "run"],
    "search_code":     ["注入", "密钥", "反序列化", "ssrf", "路径", "认证", "授权",
                         "审计", "代码审计", "search"],
    "run_semgrep":     ["semgrep", "sast", "静态分析"],
    "run_lint":        ["lint", "风格", "规范", "静态检查", "ruff", "eslint"],
    "run_coverage":    ["覆盖率", "覆盖", "测试", "coverage"],
    "git_log":         ["历史", "提交", "log", "演进"],
    "git_blame":       ["追溯", "blame", "来源", "谁改"],
    "git_diff":        ["diff", "变更", "增量", "改动", "对比"],
    "run_command":     ["执行", "运行", "跑", "测试", "构建", "build", "test", "run", "shell"],
    "str_replace_editor": ["编辑", "修改", "替换", "插入", "补丁", "patch", "edit", "create"],
    "list_skills":     ["skill", "技能"],
    "skill":           ["skill", "技能"],
}


def _infer_step_from_tool(tool_name: str, plan_steps: list[dict]) -> int | None:
    """根据工具名推断当前在执行哪个 plan step,返回 step id

    匹配规则:tool_name 对应的关键词与 step.text 命中(大小写不敏感)。
    优先匹配 status=pending 的 step(即将开始),其次 in_progress 的 step(正在做)。
    无匹配返回 None。
    """
    keywords = _TOOL_STEP_KEYWORDS.get(tool_name)
    if not keywords or not plan_steps:
        return None

    kw_lower = [k.lower() for k in keywords]
    # 先找 pending 中匹配的(说明进入了新步骤)
    for s in plan_steps:
        if s["status"] != "pending":
            continue
        text_lower = s["text"].lower()
        if any(k in text_lower for k in kw_lower):
            return s["id"]
    # 再找 in_progress 中匹配的(说明还在做同一步骤)
    for s in plan_steps:
        if s["status"] != "in_progress":
            continue
        text_lower = s["text"].lower()
        if any(k in text_lower for k in kw_lower):
            return s["id"]
    return None


def _merge_plan(current: list[dict], llm_update: list[dict]) -> list[dict]:
    """把 LLM 显式输出的 plan 状态合并到 current_plan

    合并策略(信任 LLM 的 done 标注,它有 tool_result 上下文):
    - 按 step.text 匹配(忽略 id,LLM 可能重新编号)
    - LLM 标 done → current 对应 step 标 done
    - LLM 标 in_progress → current 对应 step 标 in_progress
    - LLM 未提及的 step → 保持 current 原状态(代码已推进的 in_progress 不丢)
    - LLM 新增的 step → 追加到 current 末尾
    返回合并后的 plan(新 list,不修改入参)。
    """
    if not llm_update:
        return list(current)
    if not current:
        return [dict(s) for s in llm_update]

    # 用 text 做 key 建索引(忽略首尾空白和大小写差异)
    def _key(text: str) -> str:
        return text.strip().lower()

    current_by_text = {_key(s["text"]): s for s in current}
    llm_by_text = {_key(s["text"]): s for s in llm_update}

    merged: list[dict] = []
    next_id = 1
    # 1. 遍历 current,按 LLM 更新状态(若 LLM 提到)
    for s in current:
        new_s = dict(s)
        new_s["id"] = next_id
        next_id += 1
        k = _key(s["text"])
        if k in llm_by_text:
            # LLM 显式标注了,信任 LLM(尤其是 done)
            new_s["status"] = llm_by_text[k]["status"]
        merged.append(new_s)

    # 2. 追加 LLM 新增的 step(current 里没有的)
    for s in llm_update:
        k = _key(s["text"])
        if k not in current_by_text:
            new_s = dict(s)
            new_s["id"] = next_id
            next_id += 1
            merged.append(new_s)

    return merged


# ============================================================
# 跨轮记忆传递:结构化历史 + 三级压缩
# ============================================================

# 工具调用摘要单轮最大字符数(避免单轮工具调用过多撑爆 history)
MAX_TOOL_HISTORY_CHARS = 2000
# 后台子任务落盘报告单份进回放的字符上限(采集侧已按 MAX_CHARS 截断,这里再收一次
# 防单轮多份报告叠加把跨轮预算吃光)
MAX_AGENT_REPORT_HISTORY_CHARS = 6000
# Level 2 时保留最近几轮的完整 Level 1(不压缩)
HISTORY_KEEP_RECENT = 1


def _estimate_tokens(text: str) -> int:
    """粗估文本 token 数(CJK 感知)

    经验值:中日韩全角字符 ≈ 1 token/字,拉丁/符号 ≈ 4 字符/token。
    精度足以支撑历史压缩的预算判定(真值取决于 provider tokenizer,
    为此引入分词依赖不值得)。空串返回 0。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF       # CJK 统一表意
            or 0x3400 <= cp <= 0x4DBF    # CJK 扩展 A
            or 0x3000 <= cp <= 0x303F    # CJK 标点
            or 0xFF00 <= cp <= 0xFFEF    # 全角形式
        ):
            cjk += 1
    return cjk + (len(text) - cjk) // 4 + 1


def _clip_to_tokens(text: str, max_tokens: int) -> str:
    """按 CJK 感知粗估把文本裁到 max_tokens 以内(未超限原样返回)

    换算直接以 _estimate_tokens 为单一事实来源:先用整段的"字符/token"比例算出
    目标字符数,再逐步回退直到确实不超预算 —— 不在这里抄一遍 CJK 字符区间,
    否则两处口径会漂移。

    尾部的截断提示本身不计入预算(十几个 token,可接受)。
    """
    if not text:
        return ""
    if max_tokens <= 0 or _estimate_tokens(text) <= max_tokens:
        return text
    total = _estimate_tokens(text)
    chars_per_token = len(text) / max(1, total - 1)
    keep = max(1, int(max_tokens * chars_per_token))
    while keep > 1 and _estimate_tokens(text[:keep]) > max_tokens:
        keep = max(1, int(keep * 0.9))
    clipped = text[:keep].rstrip()
    return f"{clipped}\n…(结论过长已截断,原文 {len(text)} 字符)"


def _build_history_messages(
    db: Session, task_id, current_round_idx: int,
    client: LLMClient | None = None,
    since_round: int = 0,
) -> list[dict[str, Any]]:
    """构造之前轮次的结构化对话记忆(保留角色边界),三级压缩控制 token 成本

    同一任务内,每轮 react_agent 启动时 messages 是重新构造的,
    若不做记忆传递,LLM 看不到自己之前几轮做了什么、agent2 给过什么
    反馈、用户之前要求过什么。

    与旧版(历史拍平成文本前缀拼进 user_msg)的差异:
    - 每轮历史按角色拆条:user 原话 → assistant 执行总结 → system 评审
      反馈,模型能区分"谁说的";用户原话首次进入历史(旧版只有总结,
      原始任务指令在追问轮会丢失)
    - 系统注入内容(工具摘要/评审反馈)带 [系统注入|...] 边界标记

    since_round(仅 CLI 恢复链路使用,内置侧恒 0):只装载
    (since_round, current_round_idx) 开区间内的轮次——恢复出来的 CLI
    会话已自带 ≤ since_round 的 transcript,增量回放只补它没见过的轮次。

    三级压缩(按 _estimate_tokens 粗估预算):
    - Level 0(完整):用户原话 + 工具调用摘要 + 执行总结 + 评审反馈
    - Level 1(压缩):丢工具调用摘要
    - Level 2(LLM 压缩):保留最近 HISTORY_KEEP_RECENT 轮 Level 1,
      早期轮次 LLM 压缩成一段摘要(带缓存,增量压缩;后台预压缩见
      precompress_history_for_next_round)

    超限处理顺序:
    1. 先尝试全部 Level 0
    2. 超限 → 按优先级降级到 Level 1(优先级低的先降,同优先级 FIFO)
    3. 全部 Level 1 还超 → Level 2
    4. 无 client 或压缩失败 → 兜底强制截断

    优先级判定(决定哪些轮次保留完整信息最久):
    - 2:agent2 评估 missing 非空(还有未覆盖项,信息量大)
    - 1:done=false
    - 0:其他(done=true 等)

    返回 messages 列表(可能为空)。第 1 轮(current_round_idx=1)无历史。
    """
    rounds_data = _load_rounds_data(
        db, task_id, before_round=current_round_idx, since_round=since_round,
    )
    if not rounds_data:
        return []

    # ---- Level 0:全部含工具摘要 ----
    total = sum(_round_token_cost(r, include_tools=True) for r in rounds_data)
    include_tools_flags = [True] * len(rounds_data)
    if total <= MAX_HISTORY_TOKEN_BUDGET:
        return _rounds_to_messages(rounds_data, include_tools_flags)

    # ---- Level 1:按优先级丢工具摘要 ----
    while total > MAX_HISTORY_TOKEN_BUDGET:
        # 找最低优先级中最早且还含工具摘要的轮次降级
        target_idx = None
        min_pri = 999
        for i, r in enumerate(rounds_data):
            if not include_tools_flags[i]:
                continue
            if r["priority"] < min_pri:
                min_pri = r["priority"]
                target_idx = i
        if target_idx is None:
            break  # 全部已降级
        total -= (
            _round_token_cost(rounds_data[target_idx], include_tools=True)
            - _round_token_cost(rounds_data[target_idx], include_tools=False)
        )
        include_tools_flags[target_idx] = False

    if total <= MAX_HISTORY_TOKEN_BUDGET:
        return _rounds_to_messages(rounds_data, include_tools_flags)

    # ---- Level 2:LLM 压缩早期轮次 ----
    # 保留最近 HISTORY_KEEP_RECENT 轮的 Level 1,早期轮次调 LLM 压缩
    if len(rounds_data) <= HISTORY_KEEP_RECENT or client is None:
        # 无法压缩(轮次太少或无 client),兜底强制截断(全部 Level 1)
        return _truncate_messages(
            _rounds_to_messages(rounds_data, [False] * len(rounds_data)),
            MAX_HISTORY_TOKEN_BUDGET,
        )

    recent_rounds = rounds_data[-HISTORY_KEEP_RECENT:]
    old_rounds = rounds_data[:-HISTORY_KEEP_RECENT]

    # 查缓存或创建压缩摘要(预压缩命中缓存时为纯读,无 LLM 调用)
    compressed_text, compressed_rounds = _get_or_create_compressed(
        db, task_id, client, old_rounds, current_round_idx
    )

    # 拼接:压缩摘要(单条 system)+ 最近 N 轮 Level 1
    parts: list[dict[str, Any]] = []
    if compressed_text:
        parts.append({
            "role": "system",
            "content": (
                f"{SYSTEM_INJECT_MARKER}早期轮次压缩摘要|"
                f"覆盖{_format_rounds_span(compressed_rounds)}]\n{compressed_text}"
            ),
        })
    parts.extend(
        _rounds_to_messages(recent_rounds, [False] * len(recent_rounds))
    )

    # 如果拼接后还超(压缩摘要本身太长),强制截断
    if _messages_token_cost(parts) > MAX_HISTORY_TOKEN_BUDGET:
        return _truncate_messages(parts, MAX_HISTORY_TOKEN_BUDGET)
    return parts


def _load_rounds_data(
    db: Session, task_id, before_round: int, since_round: int = 0,
) -> list[dict[str, Any]]:
    """加载 (since_round, before_round) 开区间内各轮的结构化数据(历史注入与预压缩共用)

    每轮提取:question(用户原话)/ tool_summary(工具调用摘要)/
    assistant_summary(执行总结)/ review(评审反馈)/ priority(降级优先级)。
    区间为空(含 since_round 已追平 before_round)时返回 []。
    """
    if before_round <= 1 or since_round >= before_round - 1:
        return []

    # 查询目标轮次之前的所有对话(排除 history_compress 缓存记录)
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx < before_round,
            Conversation.round_idx > since_round,
            Conversation.type != "history_compress",
        )
        .order_by(Conversation.round_idx, Conversation.created_at)
        .all()
    )
    if not convs:
        return []

    # 按 round_idx 分组(只取 >= 1 的轮次;存量数据里第 0 轮是旧版
    # agent2 初始评估,内容已通过 task.user_input 传给第 1 轮 react_agent,
    # 这里不重复注入)
    by_round: dict[int, list[Conversation]] = {}
    for c in convs:
        if c.round_idx >= 1:
            by_round.setdefault(c.round_idx, []).append(c)

    rounds_data: list[dict[str, Any]] = []
    reports_per_round = _load_agent_reports_by_round(db, task_id)
    for ridx in sorted(by_round.keys()):
        rd = _extract_round_data(
            by_round[ridx], ridx, reports=reports_per_round.get(ridx, ""),
        )
        if rd is not None:
            rounds_data.append(rd)
    return rounds_data


def _load_agent_reports_by_round(db: Session, task_id) -> dict[int, str]:
    """读回本任务已回捞的后台子任务落盘报告,按轮次拼成文本(kind=agent_report)

    为何需要:部分 CLI 的 Agent 工具是后台派生,子任务结论常常没能回流到本轮总结,
    而沙箱回收/会话重建后那些结果再也拿不到。acp_base 已把它们回捞成持久
    artifact,跨轮回放直接读它,新轮就能合并旧结论而不必重跑子任务。

    一次查全部(不按轮循环查 N 次);单份截断由采集侧已完成(MAX_CHARS 上限),
    这里只拼接。任何异常降级为"无报告"(回放是增强项,不拖垮执行轮)。
    """
    try:
        rows = (
            db.query(TaskArtifact)
            .filter(
                TaskArtifact.task_id == task_id,
                TaskArtifact.kind == "agent_report",
            )
            .order_by(TaskArtifact.created_at)
            .all()
        )
    except Exception as e:
        logger.warning(f"[task={task_id}] 读取后台子任务落盘报告失败(忽略,不注入): {e}")
        return {}

    by_round: dict[int, list[str]] = {}
    for art in rows:
        meta = art.metadata_ or {}
        try:
            ridx = int(meta.get("round_idx") or 0)
        except (TypeError, ValueError):
            ridx = 0
        if ridx < 1 or not (art.content or "").strip():
            continue
        name = str(meta.get("filename") or meta.get("path") or "")
        body = art.content if len(art.content) <= MAX_AGENT_REPORT_HISTORY_CHARS else (
            art.content[:MAX_AGENT_REPORT_HISTORY_CHARS] + "\n…(已截断)"
        )
        by_round.setdefault(ridx, []).append(f"### {name}\n{body}" if name else body)
    return {ridx: "\n\n".join(parts) for ridx, parts in by_round.items()}


def _extract_round_data(
    round_convs: list[Conversation], ridx: int, reports: str = "",
) -> dict[str, Any] | None:
    """从单轮对话记录提取结构化数据,无有效内容时返回 None

    - question:用户当轮原话(role=user, type=question,取最后一条;
      首轮由 create_task 落库,追问/续跑轮由 react_agent 幂等落库)
    - tool_summary:工具调用摘要(意图首行 + 结果片段,Level 0 专用)
    - assistant_summary:react_agent 当轮最后一条 thinking(即最终总结)
    - review:agent2 当轮评估/审查(优先 reasoning;type 兼容
      evaluation=旧版协作评估 / review=后台审查 两代)
    - reports:当轮回捞到的后台子任务落盘报告(由 _load_agent_reports_by_round
      供给,写该轮总结时未回报的结果靠它补齐;空串=无)
    - priority:压缩降级优先级(2=missing 非空,1=未宣布完成,0=其他)
    """
    questions = [
        c for c in round_convs
        if c.role == "user" and c.type == "question" and c.content
    ]
    question = questions[-1].content if questions else ""

    # 工具调用摘要(intent + 结果片段)
    tool_calls = [
        c for c in round_convs
        if c.role == "agent1" and c.type == "tool_call" and c.content
    ]
    tool_results = [
        c for c in round_convs
        if c.role == "agent1" and c.type == "tool_result" and c.content
    ]
    tool_lines: list[str] = []
    for i, tc in enumerate(tool_calls):
        intent_line = tc.content.split("\n", 1)[0] if tc.content else ""
        result_snippet = ""
        if i < len(tool_results):
            result_snippet = tool_results[i].content[:200]
        if result_snippet:
            tool_lines.append(f"  - {intent_line} → {result_snippet}")
        else:
            tool_lines.append(f"  - {intent_line}")
    tool_summary = "\n".join(tool_lines)[:MAX_TOOL_HISTORY_CHARS]

    # react_agent 当轮最后一条 thinking(即最终总结)
    react_thinkings = [
        c for c in round_convs
        if c.role == "agent1" and c.type == "thinking" and c.content
    ]
    assistant_summary = react_thinkings[-1].content if react_thinkings else ""

    # agent2 当轮评估/审查
    ua_evals = [
        c for c in round_convs
        if c.role == "agent2" and c.type in ("evaluation", "review")
    ]
    ua_eval = ua_evals[-1] if ua_evals else None
    review = ""
    if ua_eval:
        review = ua_eval.reasoning or ua_eval.content or ""

    if not question and not assistant_summary and not review and not tool_summary and not reports:
        return None

    # 单条截断
    question = question[:MAX_HISTORY_MSG_CHARS] if question else ""
    assistant_summary = (
        assistant_summary[:MAX_HISTORY_MSG_CHARS] if assistant_summary else ""
    )
    review = review[:MAX_HISTORY_MSG_CHARS] if review else ""

    # 优先级判定(决定哪些轮次保留工具摘要最久)
    priority = 0
    if review:
        if "未覆盖:" in review:
            missing_part = review.split("未覆盖:")[1].split("\n")[0]
            if missing_part.strip() and missing_part.strip() != "[]":
                priority = 2
        if priority == 0 and "→ 宣布完成" not in review:
            priority = 1

    return {
        "ridx": ridx,
        "question": question,
        "tool_summary": tool_summary,
        "assistant_summary": assistant_summary,
        "review": review,
        "reports": reports or "",
        "priority": priority,
    }


def _round_token_cost(rd: dict[str, Any], include_tools: bool) -> int:
    """单轮历史 token 成本(CJK 感知粗估)

    reports 总是计入:后台子任务落盘报告与工具摘要不同桶,降级到 Level 1 也不会
    把它丢掉,不计入会让预算估偏小、实际超 budget。
    """
    parts = [rd["question"], rd["assistant_summary"], rd["review"], rd.get("reports") or ""]
    if include_tools:
        parts.append(rd["tool_summary"])
    return _estimate_tokens("".join(parts))


def _messages_token_cost(messages: list[dict[str, Any]]) -> int:
    """消息序列 token 成本(CJK 感知粗估,只算 content)"""
    return sum(_estimate_tokens(str(m.get("content") or "")) for m in messages)


def _rounds_to_messages(
    rounds_data: list[dict[str, Any]], include_tools_flags: list[bool],
) -> list[dict[str, Any]]:
    """把各轮结构化数据转成消息序列(按轮次时间序,保留角色边界)

    每轮产出(有内容才产出,保持对话自然流):
    - user:用户当轮原话(零包装)
    - system:[系统注入|工具调用摘要|第 N 轮](Level 0 且有摘要时)
    - assistant:react_agent 当轮执行总结
    - system:[系统注入|评审反馈|第 N 轮](agent2 审查反馈,
      段落标签用中性措辞,不向执行 agent 暴露 agent2 等内部角色)
    """
    messages: list[dict[str, Any]] = []
    for rd, include_tools in zip(rounds_data, include_tools_flags):
        if rd["question"]:
            messages.append({"role": "user", "content": rd["question"]})
        if include_tools and rd["tool_summary"]:
            messages.append({
                "role": "system",
                "content": (
                    f"{SYSTEM_INJECT_MARKER}工具调用摘要|第 {rd['ridx']} 轮]\n"
                    f"{rd['tool_summary']}"
                ),
            })
        if rd["assistant_summary"]:
            messages.append({
                "role": "assistant",
                "content": rd["assistant_summary"],
            })
        # 后台子任务落盘报告:单独一条 system 注入(不与本轮总结合并),因为它
        # 是"本轮总结当时没拿到"的补位证据,合并会让模型分不清哪些已交付
        reports = rd.get("reports") or ""
        if reports:
            messages.append({
                "role": "system",
                "content": (
                    f"{SYSTEM_INJECT_MARKER}后台子任务落盘报告|第 {rd['ridx']} 轮]\n"
                    "以下是该轮派生的后台子任务写进工作区的完整结论(本轮总结当时未能拿到它们),"
                    f"请作为已完成的证据直接沿用,不要重跑这些子任务:\n{reports}"
                ),
            })
        if rd["review"]:
            messages.append({
                "role": "system",
                "content": (
                    f"{SYSTEM_INJECT_MARKER}评审反馈|第 {rd['ridx']} 轮]\n"
                    f"{rd['review']}"
                ),
            })
    return messages


def _format_rounds_span(rounds: list[int]) -> str:
    """轮次列表 → 展示用区间文本,如 [1, 2, 3] → "第 1-3 轮\""""
    if not rounds:
        return ""
    if len(rounds) == 1:
        return f"第 {rounds[0]} 轮"
    return f"第 {min(rounds)}-{max(rounds)} 轮"


def _truncate_messages(
    messages: list[dict[str, Any]], max_tokens: int,
) -> list[dict[str, Any]]:
    """兜底截断:超预算时从最早消息开始裁剪,保留最近内容

    消息粒度截断(不切断单条内容,保持 role 结构完整):从最新消息向前
    累计 token,装不下即停,头部插入截断标记;至少保留最后一条消息。
    """
    if not messages or _messages_token_cost(messages) <= max_tokens:
        return messages
    marker = "[...早期记忆已截断...]"
    acc = 0
    kept_reversed: list[dict[str, Any]] = []
    for msg in reversed(messages):
        cost = _estimate_tokens(str(msg.get("content") or ""))
        if acc + cost > max_tokens and kept_reversed:
            break
        acc += cost
        kept_reversed.append(msg)
    kept_reversed.reverse()
    return [{"role": "system", "content": marker}] + kept_reversed


# ============================================================
# Level 2:LLM 压缩(带缓存 + 增量压缩)
# 压缩 prompt 与输入拼装段集中管理于 app/prompts/executor.py
# ============================================================


def _llm_compress_history(
    client: LLMClient,
    old_summary: str | None,
    new_segments: list[str],
) -> str:
    """调 LLM 压缩历史记忆段

    参数:
        old_summary: 之前的压缩摘要(增量压缩时传入,首次为 None)
        new_segments: 新增的需要压缩的记忆段列表

    返回压缩后的摘要文本。失败时返回拼接的原文(降级,不丢信息)。
    """
    history_text, old_hint = build_history_compress_segments(old_summary, new_segments)

    prompt = HISTORY_COMPRESS_PROMPT.format(
        old_hint=old_hint,
        history_text=history_text[:20000],  # 保护性截断,避免超长
    )

    try:
        # 关闭思考模式压缩更快(压缩是简单任务,不需要深度思考)
        original_thinking = client.enable_thinking
        client.enable_thinking = False
        try:
            collected: list[str] = []
            # [perf] Level 2 历史压缩的 LLM 调用(阻塞在首个可见响应之前)
            _t0 = time.perf_counter()
            for chunk in client.chat_stream(
                [{"role": "user", "content": prompt}],
                max_tokens=2048,
            ):
                if chunk.content_delta:
                    collected.append(chunk.content_delta)
                if chunk.finish_reason in ("stop", "length"):
                    break
            compressed = "".join(collected).strip()
            perf_log(
                "-", "history_compress", time.perf_counter() - _t0,
                incremental=bool(old_summary),
                input_chars=len(history_text), output_chars=len(compressed),
            )
            if compressed:
                return compressed
        finally:
            client.enable_thinking = original_thinking
    except Exception as e:
        logger.warning(f"LLM 压缩历史失败,降级用原文: {e}")

    # 降级:拼接原文(不丢信息,但可能超长,由调用方截断)
    if old_summary:
        return old_summary + "\n\n" + "\n\n".join(new_segments)
    return "\n\n".join(new_segments)


def _get_or_create_compressed(
    db: Session,
    task_id,
    client: LLMClient,
    old_rounds: list[dict[str, Any]],
    current_round_idx: int,
) -> tuple[str, list[int]]:
    """获取或创建早期轮次的 LLM 压缩摘要(带缓存 + 增量压缩)

    缓存策略:
    - 查 Conversation 表 type=history_compress 的最新记录
    - 若缓存覆盖的轮次 ⊇ 需要压缩的轮次,直接用缓存
    - 若部分覆盖(如缓存有 round 1-2,需要 round 1-3),增量压缩:旧摘要 + 新轮次
    - 若无缓存,压缩所有需要压缩的轮次
    - 压缩结果落库为新缓存记录(不删旧记录,便于回查)

    返回 (compressed_text, compressed_rounds)
    """
    need_ridxs = sorted(r["ridx"] for r in old_rounds)
    old_by_ridx = {r["ridx"]: r for r in old_rounds}

    # 查最新缓存
    cache = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.type == "history_compress",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )

    cached_rounds: list[int] = []
    cached_text = ""
    if cache:
        try:
            cache_data = json.loads(cache.reasoning or "{}")
            cached_rounds = cache_data.get("rounds", [])
            cached_text = cache.content or ""
        except (json.JSONDecodeError, TypeError):
            pass

    # 找出需要新增压缩的轮次(在 need_ridxs 但不在 cached_rounds)
    new_ridxs = [r for r in need_ridxs if r not in cached_rounds]

    if not new_ridxs and set(cached_rounds) >= set(need_ridxs):
        # 缓存完全覆盖,直接用
        return cached_text, need_ridxs

    if cached_text and new_ridxs:
        # 增量压缩:旧摘要 + 新轮次
        new_segments = [
            round_compact_text(old_by_ridx[r]) for r in new_ridxs if r in old_by_ridx
        ]
        if new_segments:
            compressed = _llm_compress_history(client, cached_text, new_segments)
            all_rounds = sorted(set(cached_rounds) | set(new_ridxs))
        else:
            return cached_text, need_ridxs
    elif new_ridxs:
        # 无缓存,压缩所有需要压缩的轮次
        new_segments = [
            round_compact_text(old_by_ridx[r]) for r in need_ridxs if r in old_by_ridx
        ]
        if not new_segments:
            return "", []
        compressed = _llm_compress_history(client, None, new_segments)
        all_rounds = need_ridxs
    else:
        # cached_rounds 超出 need(不该发生),用缓存
        return cached_text, need_ridxs

    # 落库新缓存
    try:
        conv = Conversation(
            task_id=task_id,
            round_idx=current_round_idx,
            role="system",
            type="history_compress",
            content=compressed,
            reasoning=json.dumps({"rounds": all_rounds}, ensure_ascii=False),
        )
        db.add(conv)
        db.commit()
    except Exception as e:
        logger.warning(f"[task={task_id}] 压缩缓存落库失败(不影响流程): {e}")

    return compressed, all_rounds


def precompress_history_for_next_round(
    db: Session, task_id, completed_round_idx: int,
    client: LLMClient | None,
) -> None:
    """轮次完成后预压缩早期历史(后台调用,永不抛异常)

    用户下一次追问(_build_history_messages)若需 Level 2 压缩,直接命中
    此处预写的缓存,消除追问关键路径上的同步 LLM 压缩延迟(旧版该压缩
    阻塞在用户追问响应之前,是 perf 里可观察的大耗时点)。

    以"下一轮"(current_round = completed_round_idx + 1)的视角判断:
    仅当历史 Level 1 总量已超预算(即下一轮真的会触发 Level 2)才调 LLM。
    缓存带增量合并(见 _get_or_create_compressed),重复调用幂等。
    """
    try:
        if client is None or completed_round_idx < 1:
            return
        rounds_data = _load_rounds_data(
            db, task_id, before_round=completed_round_idx + 1,
        )
        if len(rounds_data) <= HISTORY_KEEP_RECENT:
            return
        level1_total = sum(
            _round_token_cost(r, include_tools=False) for r in rounds_data
        )
        if level1_total <= MAX_HISTORY_TOKEN_BUDGET:
            return  # 下一轮 Level 1 即可放下,无需预压缩
        old_rounds = rounds_data[:-HISTORY_KEEP_RECENT]
        _t0 = time.perf_counter()
        _get_or_create_compressed(
            db, task_id, client, old_rounds, completed_round_idx + 1,
        )
        perf_log(
            task_id, "history_precompress", time.perf_counter() - _t0,
            rounds=len(old_rounds),
        )
    except Exception as e:
        logger.warning(f"[task={task_id}] 预压缩历史失败(忽略,追问时兜底压缩): {e}")


