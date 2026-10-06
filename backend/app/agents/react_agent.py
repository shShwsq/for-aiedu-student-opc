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
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task
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
    FOLLOWUP_ATTACHMENT_NOTE,
    FOLLOWUP_GUIDANCE,
    HISTORY_COMPRESS_PROMPT,
    LOOP_BREAK_PROMPT,
    MAX_ITERATION_PROMPT,
    REACT_AGENT_SYSTEM_PROMPT,
    SYSTEM_INJECT_MARKER,
    _has_creation_upload,
    build_first_round_question,
    build_history_compress_segments,
    build_repo_context_section,
    format_injected_user_messages,
    format_plan_reminder,
    round_compact_text,
)

logger = logging.getLogger(__name__)


# 最大迭代轮次,防止死循环
MAX_ITERATIONS = 30
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

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"[task={task.id}] react_agent 第 {round_idx} 轮 / 迭代 {iteration}")

        # 暂停检查点 1:迭代边界(若用户已暂停,在此阻塞直到恢复)
        wait_if_paused(task.id)

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
            # 本批消息附带的上传文件:传输进工作区 followup_uploads/(不重定向
            # repo_path),并把目录级提示并入注入文本,让模型感知新文件。
            # 布局约定:按 params.followup_upload_ids **全量累积列表**传输(全局
            # 下标),与沙箱回收后的重放 / 工作区回退浏览同一布局 —— 本轮新 ids
            # 先并入 params 再传,保证三处路径一致;单个上传失败由
            # add_uploads_to_workspace 逐上传容错(旧附件被 GC 不阻断新文件)。
            # 传输失败 catch+log,不中断本轮(文字消息照常注入)。
            attachment_note = ""
            batch_upload_ids: list[str] = []
            for m in pending_user_msgs:
                for uid in (m.get("upload_ids") or []):
                    if uid and uid not in batch_upload_ids:
                        batch_upload_ids.append(uid)
            if batch_upload_ids:
                from app.services.upload_layout import extract_followup_ids

                existing = extract_followup_ids(task.params)
                merged = existing + [u for u in batch_upload_ids if u not in existing]
                # 持久化累积(非 JSONB 突变):params 是重放与回退浏览的唯一真源,
                # 先落库再传输(传输失败也不丢重放依据)
                task.params = {**(task.params or {}), "followup_upload_ids": merged}
                try:
                    db.commit()
                except Exception as e:
                    logger.warning(
                        f"[task={task.id}] 追问上传累积落库失败(忽略): {e}"
                    )
                    db.rollback()
                try:
                    from app.tools import sandbox_tools
                    sandbox_tools.add_uploads_to_workspace(
                        task_id_str, merged, "followup_uploads"
                    )
                    attachment_note = FOLLOWUP_ATTACHMENT_NOTE
                except Exception as e:
                    logger.warning(
                        f"[task={task.id}] 追问上传文件传输失败(忽略,文字消息照常注入): {e}"
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
        messages.append(assistant_msg)

        # 落库思考(content + reasoning),供刷新页面后查看
        # 同时推 conversation 事件:流式卡片按 conv_id 退役为只读历史卡片,
        # 中途离开详情页再回来的订阅者(没收到这次的 thinking_delta 增量)
        # 也只有在这条事件到达时才能看到这段思考,否则要等下一次整页快照。
        if content_full or reasoning_full:
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
        # finish_reason=length 是被 max_tokens 截断,模型没说完,不算主动结束
        # (降级处理:用现有 content 作 summary,记录 warning)
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
            if finish_reason == "length":
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
            if content_full:
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
            wait_if_paused(task.id)

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
        logger.warning(f"[task={task.id}] react_agent 达到最大迭代次数")
        messages.append({
            "role": "user",
            "content": MAX_ITERATION_PROMPT,
        })
        try:
            reasoning_full, content_full, tool_calls_full, finish_reason, _conv_id = _stream_llm_response(
                client, task, db, round_idx, MAX_ITERATIONS, messages, tools
            )
            # content 作为 summary(自然语言总结)
            if content_full:
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
    result = stream_llm(
        client, messages,
        task_id=task.id, round_idx=round_idx, role="agent1",
        tools=tools, iteration=iteration,
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
    for ridx in sorted(by_round.keys()):
        rd = _extract_round_data(by_round[ridx], ridx)
        if rd is not None:
            rounds_data.append(rd)
    return rounds_data


def _extract_round_data(
    round_convs: list[Conversation], ridx: int,
) -> dict[str, Any] | None:
    """从单轮对话记录提取结构化数据,无有效内容时返回 None

    - question:用户当轮原话(role=user, type=question,取最后一条;
      首轮由 create_task 落库,追问/续跑轮由 react_agent 幂等落库)
    - tool_summary:工具调用摘要(意图首行 + 结果片段,Level 0 专用)
    - assistant_summary:react_agent 当轮最后一条 thinking(即最终总结)
    - review:agent2 当轮评估/审查(优先 reasoning;type 兼容
      evaluation=旧版协作评估 / review=后台审查 两代)
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

    if not question and not assistant_summary and not review and not tool_summary:
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
        "priority": priority,
    }


def _round_token_cost(rd: dict[str, Any], include_tools: bool) -> int:
    """单轮历史 token 成本(CJK 感知粗估)"""
    parts = [rd["question"], rd["assistant_summary"], rd["review"]]
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


