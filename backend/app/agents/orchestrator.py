"""双智能体编排器(agent2 后台审查版)

驱动 react_agent + agent2 协作(审查后台化后的新流程):
1. react_agent 按用户意图执行一轮(含 clone),输出自然语言总结
2. agent1 summary 作为临时结果落库,任务即标记 COMPLETED(推 agent1_done,
   事件总线保持打开)—— 用户感知的"任务完成"以 agent1 结束为准
3. agent2 在同一后台线程内做**后台审查**:只读核查/动态验证/引用复核,
   整理重点与知识点(results)替换临时结果,发现缺口输出"建议深挖方向"
   (suggestions,由用户决定是否让 agent1 继续深挖)
4. 审查完成推 review_done + done;练习题生成/记忆归纳在审查后链式触发

纯对话轮(本轮 agent1 无任何工具调用,回答完全来自历史上下文)在步骤 2
之前直接收尾:跳过审查/结果替换/练习题/记忆归纳,保留既有结果与审查状态
—— 交互对齐 Codex 式问答,纯追问即回答,不触发审查流水线。

resume(用户追加消息/点击建议深挖):
- 用户消息直接交给 agent1 跑一轮(不经 agent2 转述;agent1 跨轮历史
  由 react_agent._build_history_messages 以结构化 messages 注入,
  用户追问原文作为独立 user 消息),随后走后台审查
- 多轮完全由用户驱动(每次 resume = agent1 一轮 + 后台审查)
- plan 状态跨轮连续:每轮结束持久化到 task.params["_plan"],
  resume 时加载为 previous_plan 传入(对齐 Codex 的持久 plan 状态,
  追问/续跑不重规划已完成项)

单 agent 模式(agent2_enabled=false):react_agent 单轮 + summary 结果,
无审查,行为与旧版一致。

场景降级后的变更(沿承):
- 结果提取直接取 ua_result["results"];分组从 ua_result["grouping"] 读取
- allowed_skills 传给 react_agent(set_current_task),按用户选择过滤 skill
"""
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.executor_agent import get_executor
from app.agents.agent2 import run_agent2
# 对话落库 + SSE 推送:runtime 统一实现(与 react_agent / agent2 / acp_base
# 同源;别名保持 _add_conversation 模块名,存量 monkeypatch 兼容面不变)
from app.agents.runtime.conversation import record_conversation as _add_conversation
from app.clone_skip import clear_skip_state
from app.config import settings
from app.domain_events import (
    AGENT1_ROUND_COMPLETED,
    REVIEW_COMPLETED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_STARTED,
    emit,
)
from app.database import SessionLocal
from app.event_bus import finish_task, is_task_finished, publish, reset_task_bus
from app.llm.client import LLMClient
from app.models.task import Conversation, Result, Task, TaskStatus
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.pause_controller import clear_pause_state, wait_if_paused
from app.perf import perf_log
from app.prompts.executor import (
    RESUME_ATTACHMENT_NOTE,
    RETRY_MSG_LABEL,
    USER_FOLLOWUP_MSG_LABEL,
    build_first_round_question,
    build_retry_message,
    build_upload_header,
    format_repo_context_body,
)
from app.security import decrypt_secret
from app.tools import sandbox_tools
from app.tools.schema import set_current_git_tokens, set_current_task
from app.agent_policy import resolve_agent_policy
from app.user_messages import clear_user_messages, drain_user_messages
from app.user_interaction import clear_pending_command_confirm, clear_pending_verify_action

logger = logging.getLogger(__name__)


# ============================================================
# 事件活跃期(scope):执行流注册/注销,done/finish 仅由最后活跃流推送
# ============================================================

# task_id → 流世代计数(每次启动执行流 +1:初始运行/resume/重试)
_task_gens: dict[str, int] = {}
# task_id → 活跃事件期世代集合(执行流开始注册、事件收尾注销)。
# 非空 = 该任务仍有流在产出事件(agent1 执行中/审查中),
# 此时任何流的收尾都不得推 done/finish(否则并行流事件全被丢弃)
_event_scopes: dict[str, set[int]] = {}
_review_lock = threading.Lock()


def _begin_event_scope(task_id) -> int:
    """执行流启动:注册事件活跃期,返回本流世代号

    - 世代号单调递增:后启动的流世代更大,"最新流"负责重下游(记忆归纳/
      练习题/历史预压缩),老流跳过避免并行重复
    - 总线已结束(上一轮完全收尾)时兜底重置,让本流事件可推送
      (常规 immediate resume 由 API 端点先 reset,此处幂等兜底)
    """
    key = str(task_id)
    with _review_lock:
        _task_gens[key] = _task_gens.get(key, 0) + 1
        gen = _task_gens[key]
        _event_scopes.setdefault(key, set()).add(gen)
        finished = is_task_finished(key)
    if finished:
        reset_task_bus(key)
    return gen


def _end_event_scope(task_id, gen: int, terminal: tuple[str, dict] | None = None) -> bool:
    """执行流的事件活跃期结束(审查收尾/纯对话轮收尾/单 agent 收尾)

    - 仍有其他活跃 scope(并行流在跑):静默返回 False,不推终止事件、
      不关闭总线 —— 老审查与新一轮 agent1 并行时,老收尾不影响新流
    - 自己是最后一个:推 terminal 事件(如 done)并 finish_task 关闭总线,
      返回 True。异常路径 terminal=None(error 事件已由调用方推送,仅收尾)
    判定与推送在同一把锁内原子完成,消除"判定空闲与新一轮 begin 之间"
    的竞态窗口(begin 的总线重置也在同锁内检查,不会误重置已推 done 的总线)
    """
    key = str(task_id)
    with _review_lock:
        scopes = _event_scopes.get(key)
        if scopes:
            scopes.discard(gen)
            if not scopes:
                _event_scopes.pop(key, None)
        if _event_scopes.get(key):
            return False  # 并行流仍在产出事件
        if terminal:
            publish(task_id, terminal[0], terminal[1])
        finish_task(task_id)
        return True


def _has_active_scope(task_id) -> bool:
    """任务是否仍有活跃事件期(线程 finally 清理判定用)"""
    with _review_lock:
        return bool(_event_scopes.get(str(task_id)))


def _is_latest_generation(task_id, gen: int) -> bool:
    """本流是否为最新世代(重下游:记忆归纳/练习题/预压缩 仅最新流执行)"""
    with _review_lock:
        return _task_gens.get(str(task_id)) == gen


def force_cleanup_event_scopes(task_id) -> None:
    """线程包装器最后防线:强制清空任务全部事件活跃期并关闭总线

    仅在执行链在 scope 注册后意外崩溃、且包装器无法触达 gen 时使用
    (如 retry 链路中 resume 前置段异常上抛到 _run_retry_in_background)。
    正常流程的 scope 注销必须走 _end_event_scope(gen)。

    泄漏后果:该任务之后所有流被误判"并行中" → 永不推 done/finish、
    清理组永不执行、SSE 永久悬挂。retry 场景无并行流,强制清空安全。
    """
    key = str(task_id)
    with _review_lock:
        leaked = _event_scopes.pop(key, None)
    if leaked:
        logger.warning(
            f"[task={task_id}] 强制清空事件活跃期(执行链异常兜底,"
            f"leaked_gens={sorted(leaked)})"
        )
    finish_task(task_id)


def launch_resume_thread(task_id: str, user_message: str,
                         upload_ids: list[str] | None = None) -> None:
    """启动 resume 后台线程(用户追问驱动新一轮执行,不等老审查)

    追问直达 agent1:老审查(若有)继续在后台跑完,与新轮 agent1 并行;
    done/finish 由最后活跃流统一收尾(见 _end_event_scope)。

    scope 注册在启动线程**之前同步完成**:老审查收尾若发生在端点返回后、
    线程体执行前,也能看到本流活跃 → 不会误推 done 关闭总线。消除
    "端点置 RUNNING 与线程内注册 scope 之间"的窗口竞态。
    """
    flow_gen = _begin_event_scope(task_id)
    thread = threading.Thread(
        target=_run_resume_in_background,
        args=(task_id, user_message, upload_ids, flow_gen),
        daemon=True,
        name=f"task-{task_id}-resume",
    )
    thread.start()


def _run_resume_in_background(
    task_id: str, user_message: str,
    upload_ids: list[str] | None = None, flow_gen: int | None = None,
) -> None:
    """后台线程执行重启审计(与 _run_task_in_background 对齐)

    用独立的 DB session(线程安全),执行完毕后关闭。

    flow_gen 由 launch_resume_thread 同步注册并传入(端点→线程无注册窗口);
    直接调用(测试等)未传时自行注册。兜底 except 必须清 scope ——
    resume_audit_with_message 在注册与 try 之间仍有一段裸代码(状态翻转/
    LLM client 构建/工作区恢复),若抛异常逃逸到此处而 scope 未清,
    该任务之后所有流都会被误判"并行中":永不推 done、清理组永不执行、
    SSE 永久悬挂。
    """
    if flow_gen is None:
        flow_gen = _begin_event_scope(task_id)
    db = SessionLocal()
    try:
        task = db.get(Task, uuid.UUID(task_id))
        if not task:
            logger.error(f"重启任务:task {task_id} 不存在")
            _end_event_scope(task_id, flow_gen)  # 清 scope 防泄漏(幂等)
            return
        resume_audit_with_message(
            task, db, user_message, upload_ids=upload_ids, flow_gen=flow_gen,
        )
    except Exception as e:
        logger.exception(f"[task={task_id}] 重启后台执行失败")
        # 兜底:确保 task 状态被标记为失败
        try:
            task = db.get(Task, uuid.UUID(task_id))
            if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.status = TaskStatus.FAILED
                task.error_message = _err_detail(e)[:1000]
                task.current_stage = "重启执行失败"
                db.commit()
            # 兜底推送终止事件:防止 SSE 订阅者因线程在主 try 块前崩溃
            # 收不到 error 而永久挂起(并行流在跑时仅推事件,不动总线)
            publish(task_id, "error", {
                "status": "failed",
                "error_message": _err_detail(e)[:1000],
            })
        except Exception:
            pass
        finally:
            # error 已在上方推送(先推后关,防静默丢弃);清 scope 防泄漏
            _end_event_scope(task_id, flow_gen)
    finally:
        db.close()


def run_dual_agent_audit(task: Task, db: Session) -> None:
    """执行双智能体协作审计"""
    task_id_str = str(task.id)

    # 先解析 agent 策略(agent2 启停、验证权限等):
    # 启动阶段文案必须在推送前由 agent2 启停决定,
    # 否则单 agent 模式会先闪现"双智能体协作启动"误导前端
    # 合并用户级默认(agent_policies 表)+ 任务级覆盖(task.params["_agent_policy"])
    agent_policy = resolve_agent_policy(task, db)
    logger.info(f"[task={task.id}] agent_policy: {agent_policy}")

    # agent2 启停(协作轮次设置已随后台审查移除:初始运行单轮,多轮由用户 resume 驱动)
    ua_enabled = bool(agent_policy.get("agent2_enabled", True))
    logger.info(f"[task={task.id}] agent2_enabled={ua_enabled}")
    # [perf] 任务启动锚点(含 ua 启停 + 执行器类型,供对照实验分组)
    perf_log(
        task.id, "task_start",
        ua_enabled=ua_enabled, executor=(task.executor or "builtin"),
    )

    task.status = TaskStatus.RUNNING
    task.current_stage = (
        "任务启动" if ua_enabled else "任务启动(单智能体模式)"
    )
    db.commit()
    _publish_status(task)
    # 领域事件:任务启动(订阅者:审计日志等,见 app/domain_events.py)
    emit(
        TASK_STARTED, task.id,
        ua_enabled=ua_enabled, executor=task.executor or "builtin",
        scenario=task.scenario,
    )

    scenario_id = task.scenario

    # 阶段 6:加载 LLM 配置
    # - llm_client:agent2 评估用(来自 task.llm_config_id)
    # - react_client:内置 react_agent 用(来自 task.react_llm_config_id,空时回退到 llm_config_id)
    #   外部 CLI 执行器忽略 react_client(模型由 CLI 自管)
    llm_client = _build_llm_client(db, task.user_id, task.llm_config_id)
    react_client, react_client_source = _build_react_llm_client(db, task)
    logger.info(
        f"[task={task.id}] LLM 配置:agent2=task.llm_config_id,"
        f"react_agent 来源={react_client_source}, executor={task.executor}"
    )

    # 加载用户的各 git provider access_token(解密),供 clone_repo 访问私有仓库
    # 空 dict 表示未绑定,clone_repo_with_fallback 会回退到 SSH/匿名 HTTPS
    git_tokens = _load_git_tokens(db, task.user_id)
    set_current_git_tokens(git_tokens)

    # 场景降级后:设置 task 上下文(含 allowed_skills,供 skill 工具按用户选择过滤)
    # allowed_skills 为 None/空 表示全部 skill 可用(默认)
    allowed_skills = task.allowed_skills
    set_current_task(task_id_str, scenario_id, allowed_skills)

    # 执行器选择:按 task.executor 拿到对应的 ExecutorAgent provider
    # (builtin → 内置 react_agent;registry 中的 agent_type → 外部 CLI via ACP)
    executor = get_executor(task)

    # agent_policy / ua_enabled 已在函数开头解析

    # 用户原始意图:复用 build_first_round_question(与 react_agent 首轮消息 /
    # create_task 落库同一拼装,消除逐字重复的双轨实现)
    user_intent = build_first_round_question(task.user_input, task.params)

    # react_agent 历轮结果摘要(给 agent2 评估用)
    react_summaries: list[dict] = []
    all_results_count = 0
    # 修复 4:跨轮 plan 状态(react_agent 之间传递,避免重新规划已完成项)
    current_plan: list[dict] = []
    # 本轮是否正常完成(内存标志,不受外部状态修改影响):
    # finally 兜底 error 推送必须用它判定,不能用 task.status ——
    # 任务完成后用户发追问会把状态改回 RUNNING,若按状态判定,
    # 已正常完成的任务会被误判为"未完成"而推送 error 并重新标记总线结束
    normal_completed = False
    # 注册本流事件活跃期:done/finish 仅由最后活跃流收尾(并行时老流
    # 提前收尾不关总线,见 _end_event_scope)
    flow_gen = _begin_event_scope(task.id)

    try:
        # ---------- 预处理:若用户选了仓库,主动 clone + list_files ----------
        # 把仓库结构和 repo_path 提前准备好:
        #   - 注入 react_agent 第 1 轮:跳过自主 clone,直接开始审计
        # clone 失败不再让整个任务 failed:降级返回 (None, ""),回到
        #   react_agent 自主 clone 路径(有 LLM 重试/自适应,成功率更高)
        _t0 = time.perf_counter()
        repo_path, repo_context = _prepare_repo_context(task, db, task_id_str, git_tokens)
        perf_log(
            task.id, "prepare_repo_context", time.perf_counter() - _t0,
            has_repo=bool((task.params or {}).get("repo_url")),
            cloned=bool(repo_path),
        )

        # ===== 单 agent 模式:agent2 已禁用,跳过评估/打断/验证 =====
        # react_agent 只跑 1 轮,用 summary 作为唯一结构化结果(无 covered/missing 提取)
        if not ua_enabled:
            logger.info(f"[task={task.id}] agent2 已禁用,单 agent 模式")
            task.current_stage = "react_agent 执行(单 agent 模式)"
            db.commit()
            _publish_status(task)

            _t0 = time.perf_counter()
            _results, summary, _plan = executor.run(
                task, db,
                round_idx=1,
                followup_query=None,
                client=react_client,
                repo_context=repo_context,
                previous_plan=None,
            )
            perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=1, executor=executor.name)
            emit(
                AGENT1_ROUND_COMPLETED, task.id,
                round_idx=1, executor=executor.name,
                results_count=len(_results),
            )
            react_summaries.append({"round": 1, "summary": summary})
            # plan 状态持久化(resume 跨轮续接)
            _save_plan_to_task(task, db, _plan)

            # 用 summary 作为唯一结构化结果(agent2 已禁用,不做结构化提取)
            structured_results = [{"title": "执行结果", "content": summary}]
            for r in structured_results:
                result = Result(
                    task_id=task.id,
                    round_idx=1,
                    title=r["title"],
                    content=r["content"],
                )
                db.add(result)
            db.commit()
            all_results_count = len(structured_results)

            # 标记完成
            task.status = TaskStatus.COMPLETED
            task.current_stage = (
                f"单 agent 执行完成,共 {len(react_summaries)} 轮,"
                f"共 {all_results_count} 个结果"
            )
            task.completed_at = datetime.now(timezone.utc)
            db.commit()
            _publish_status(task)

            # 单 agent 模式不写 agent2 总结对话(无评估可展示,避免误导)

            # 领域事件:任务完成(单 agent 路径)
            emit(
                TASK_COMPLETED, task.id,
                mode="single_agent", rounds=len(react_summaries),
                results_count=all_results_count,
            )

            # 遗留用户消息自动续轮(先于终止 end-scope:有遗留时新流注册
            # scope,下方 end-scope 看到活跃流不推 done,SSE 不断线)
            _auto_resume_leftover_messages(task, db, task_id_str)

            # 事件活跃期收尾:推 done + 关总线(仅当无并行流;
            # 判定与推送在 _end_event_scope 锁内原子完成,消除竞态)
            _end_event_scope(task.id, flow_gen, ("done", {"status": "completed"}))
            normal_completed = True

            # ---- 重下游(仅最新流执行:并行时老流跳过,避免与新流重复)----
            if not _is_latest_generation(task.id, flow_gen):
                return  # 已有更新的流接管(用户追问已启动新轮)

            # 预压缩早期历史:用户下一轮追问直接命中缓存(失败兜底)
            _precompress_history_safely(task, db, len(react_summaries), react_client)

            # 记忆归纳(失败兜底,不影响任务完成;模型/结构走用户「记忆设置」独立解析)
            try:
                from app.services.memory_summarize import summarize_and_save_memory
                summarize_and_save_memory(task, db)
            except Exception as mem_err:
                logger.warning(f"[task={task.id}] 归纳写入记忆失败(忽略): {mem_err}")

            # 自动生成练习题 draft(失败兜底,不影响任务完成;产出仍需用户确认)
            if settings.PRACTICE_ENABLED:
                try:
                    from app.services.practice.auto_generate import auto_generate_practice_for_task
                    auto_generate_practice_for_task(task, db)
                except Exception as practice_err:
                    logger.warning(f"[task={task.id}] 自动生成练习题失败(忽略): {practice_err}")

            # 捕获工作区 diff(失败兜底,不影响任务完成)
            try:
                from app.services.workspace_diff import (
                    save_repo_tree_artifact,
                    save_workspace_diff_artifact,
                )
                save_workspace_diff_artifact(task, db, task_id_str)
                # 树快照:更新为最终态(含新建文件),供不可用时兜底展示
                save_repo_tree_artifact(task, db, task_id_str)
            except Exception as diff_err:
                logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")

            return  # 单 agent 模式结束,finally 块仍会执行清理

        # ---------- 双 agent 模式:agent1 单轮 → 任务完成 → 后台审查 ----------
        # 初始运行只有 1 轮 agent1(协作循环已移除,多轮由用户 resume 驱动)
        # 暂停检查点:agent1 执行前(react_agent 内部还有细粒度检查点)
        wait_if_paused(task.id)

        task.current_stage = "AI助手执行"
        db.commit()
        _publish_status(task)

        _t0 = time.perf_counter()
        _results, summary, current_plan = executor.run(
            task, db,
            round_idx=1,
            followup_query=None,
            client=react_client,
            repo_context=repo_context,
            previous_plan=None,
        )
        perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=1, executor=executor.name)
        emit(
            AGENT1_ROUND_COMPLETED, task.id,
            round_idx=1, executor=executor.name,
            results_count=len(_results),
        )
        react_summaries.append({"round": 1, "summary": summary})
        # plan 状态持久化(resume 跨轮续接)
        _save_plan_to_task(task, db, current_plan)

        # ===== 轮次类型判定:纯对话轮(如问候/纯问答)跳过审查与重下游 =====
        # 本轮无任何工具调用 → agent1 未触碰工作区,无新证据可审;
        # 保留既有结果与审查状态(首轮即对话则任务无结果,回答在对话流)
        if not _round_has_tool_calls(db, task.id, 1):
            _finish_conversation_round(
                task, db, rounds=len(react_summaries), mode="dual_agent",
                flow_gen=flow_gen,
            )
            normal_completed = True  # 正常完成:finally 不再兑底推 error
            return  # finally 块仍会执行清理

        # agent1 summary 作为临时结果:审查完成前给前端可展示的结果
        # (审查完成后会被 agent2 的重点与知识点整体替换)
        all_results_count = _replace_interim_results(db, task, 1, summary)

        # ---------- agent1 结束即任务完成 ----------
        task.status = TaskStatus.COMPLETED
        task.current_stage = "任务完成,检查助手审查中"
        task.completed_at = datetime.now(timezone.utc)
        task.review_status = "running"
        db.commit()
        _publish_status(task)

        # 领域事件:任务完成(双 agent 路径,时刻=agent1 结束,不含审查时长)
        emit(
            TASK_COMPLETED, task.id,
            mode="dual_agent", rounds=len(react_summaries),
            results_count=all_results_count,
        )
        # [perf] agent1 完成锚点:用户感知的任务完成时刻(对照实验的完成时延以此为准)
        perf_log(task.id, "agent1_done", rounds=1, results_count=all_results_count)

        # 推送 agent1_done:主界面收尾(拉快照展示临时结果+完成态),
        # 事件总线保持打开 —— 后台审查的 conversation/thinking_delta 继续送达前端侧栏
        publish(task.id, "agent1_done", {"status": "completed"})

        # 捕获工作区 diff(agent2 只读审查不影响工作区,此时即可捕获;
        # 失败兜底,不影响后续审查)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")

        # 任务本体已完成:finally 不再兜底推 error
        # (审查失败属于 review_status=failed,不是任务失败)
        normal_completed = True

        # 遗留用户消息自动续轮(执行期间最后迭代之后到达、未被 drain 的):
        # 挪到新轮并立即启动新一轮,与本轮后台审查并行(不等审查)——
        # 新流先注册 scope,审查收尾的 end-scope 看到活跃流不推 done
        _auto_resume_leftover_messages(task, db, task_id_str)

        # ---------- 后台审查(同一线程;失败只影响 review_status) ----------
        # 审查期间用户追问可并行启动新一轮 resume(不等审查):
        # done/finish 由最后活跃流的 _end_event_scope 统一收尾。
        # _run_background_review 永不抛异常(内部全兜底),无需 try 包裹
        _run_background_review(
            task, db,
            user_intent=user_intent,
            react_summaries=react_summaries,
            scenario_id=scenario_id,
            llm_client=llm_client,
            agent_policy=agent_policy,
            task_id_str=task_id_str,
            react_client=react_client,
            flow_gen=flow_gen,
            round_idx=1,
        )

    except Exception as e:
        logger.exception(f"[task={task.id}] 双智能体协作失败")
        # 错误详情增强:消息为空时补异常类型名,避免 UI 显示"未知错误"
        err_detail = _err_detail(e)
        task.status = TaskStatus.FAILED
        task.error_message = err_detail[:1000]
        task.current_stage = "执行失败"
        db.commit()
        _publish_status(task)
        # 领域事件:任务失败(主流程 except 路径)
        emit(TASK_FAILED, task.id, error=err_detail[:500], stage="run")
        _add_conversation(
            db, task, round_idx=0,
            role="agent2", type="error",
            content=f"执行失败: {err_detail}",
        )
        # 失败也尽量捕获:工作区 diff + 仓库树快照(失败兜底;沙箱通常仍存活,
        # 会话已死则自然返回 None,不影响失败处理)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 失败时捕获工作区产物失败(忽略): {diff_err}")
        # 事件活跃期收尾移交 finally 统一处理:error 事件必须在
        # finish_task 之前推送(总线关闭后 publish 会被静默丢弃)
    finally:
        # 通知事件总线:任务结束。
        # done 事件已在各收尾点经 _end_event_scope 推送(正常路径)。
        # 此处仅兜底:异常路径(本轮未正常完成)推送 error 事件。
        # 注意:判定必须用本轮执行的内存标志 normal_completed,不能用 task.status ——
        # 任务完成后用户发追问会把状态改回 RUNNING(API 端点同步落库),若按状态判定,
        # 已正常完成的任务会被误判为"未完成"而推送 error(前端显示"未知错误"失败横幅)。
        if not normal_completed:
            # [诊断] error 事件推送日志:前端 onError 的唯一事件源,全量记录
            logger.warning(
                f"[task={task.id}] finally 兜底推送 error 事件 "
                f"(status={task.status.value}, error_message={task.error_message!r})"
            )
            # error 无条件推送(并行流失败也须通知前端);必须在 end-scope 之前
            # (总线关闭后 publish 会被静默丢弃,前端将永远收不到错误横幅)
            publish(task.id, "error", {
                "status": "failed",
                "error_message": task.error_message or "未知错误(无异常详情,请查看服务日志)",
            })
        # 无条件收尾(幂等):正常路径已在各收尾点 end 过(discard 无副作用),
        # 此处兜底 ① 异常路径的 scope 注销 ② "正常完成后仍抛异常"的边角泄漏。
        # 若此时尚有并行流在跑则静默返回,不动总线
        _end_event_scope(task.id, flow_gen)
        # ---- 状态清理组:仅当本任务无活跃流时执行 ----
        # (审查与新一轮 resume 并行时,老线程 finally 不得清掉新流正在用的
        #  运行时状态:用户消息队列/暂停标志/沙箱 completed 标记等。
        #  必须在 end-scope 之后:异常路径下自身 scope 尚未注销,
        #  先查会被误判"并行中"而永久跳过清理)
        if not _has_active_scope(task.id):
            for cleanup_fn, name in [
                (clear_pause_state, "暂停状态"),
                (clear_skip_state, "跳过预克隆标志"),
                (clear_user_messages, "用户消息队列"),
                (clear_pending_verify_action, "验证待授权状态"),
                (clear_pending_command_confirm, "命令待确认状态"),
                (sandbox_tools.mark_task_completed, "沙箱完成标记"),
            ]:
                try:
                    cleanup_fn(task_id_str)
                except Exception as cleanup_err:
                    logger.warning(f"[task={task.id}] 清理{name}失败: {cleanup_err}")
        else:
            logger.info(
                f"[task={task.id}] 并行流仍在运行,跳过状态清理"
                f"(gen={flow_gen})"
            )


# ============================================================
# 辅助:plan 状态持久化 / 轮次类型判定 / 记录 agent2 的对话 / 临时结果 / 后台审查
# ============================================================


def _save_plan_to_task(task: Task, db: Session, plan: list[dict]) -> None:
    """把本轮结束时的 plan 状态持久化到 task.params["_plan"]

    resume 时由 _load_plan_from_task 加载为 previous_plan 传给下一轮
    executor.run,保证跨轮 plan 连续(对齐 Codex 的持久 plan 状态:
    已完成项保持 done,只推进未完成项,不重新规划)。
    空 plan 也写入(清空上轮残留,防旧 plan 污染新任务语义)。
    """
    try:
        task.params = {**(task.params or {}), "_plan": plan or []}
        db.commit()
    except Exception as e:
        logger.warning(f"[task={task.id}] 保存 plan 状态失败(忽略): {e}")


def _load_plan_from_task(task: Task) -> list[dict]:
    """加载上次持久化的 plan 状态(resume 跨轮续接用)

    无记录/格式异常返回空 list(等同于无 plan,react_agent 正常重新规划)。
    """
    plan = (task.params or {}).get("_plan")
    if not isinstance(plan, list):
        return []
    # 只保留合法条目(id/text/status),脏数据不进执行链
    cleaned = [
        s for s in plan
        if isinstance(s, dict) and isinstance(s.get("text"), str) and s.get("text")
    ]
    return cleaned


def _precompress_history_safely(
    task: Task, db: Session, completed_round_idx: int, client: LLMClient | None,
) -> None:
    """轮次结束后预压缩早期历史(失败兜底,不影响任务生命周期)

    把 Level 2 历史压缩从用户追问的关键路径挪到后台:此处预写缓存,
    用户下一次追问直接命中,消除追问响应前的同步 LLM 压缩延迟。
    """
    try:
        from app.agents.react_agent import precompress_history_for_next_round
        precompress_history_for_next_round(db, task.id, completed_round_idx, client)
    except Exception as e:
        logger.warning(f"[task={task.id}] 预压缩历史失败(忽略): {e}")


def _round_has_tool_calls(db: Session, task_id, round_idx: int) -> bool:
    """判定某轮 agent1 是否有过工具调用

    builtin react_agent 与 CLI 执行器(acp_base)的工具调用均落库为
    Conversation(role=agent1, type=tool_call),统一按此判定。
    """
    return (
        db.query(Conversation.id)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx == round_idx,
            Conversation.role == "agent1",
            Conversation.type == "tool_call",
        )
        .first()
        is not None
    )


def _finish_conversation_round(
    task: Task, db: Session, *, rounds: int, mode: str, flow_gen: int,
) -> None:
    """纯对话轮收尾:跳过后台审查与重下游,直接完成任务

    纯对话轮 = 本轮 agent1 无任何工具调用(未触碰工作区,回答完全来自
    历史上下文/模型知识):无新证据可审,跳过 agent2 审查、结果替换、
    练习题生成与记忆归纳,保留既有结果与审查状态。交互对齐 Codex 式
    问答 —— 纯追问即回答,不为一次对话触发整条审查流水线。

    终止事件序列与单 agent 收尾同构:agent1_done → done → finish_task
    (经 _end_event_scope:老审查与新流并行时不关总线,由最后活跃流收尾)
    """
    task.status = TaskStatus.COMPLETED
    task.current_stage = "任务完成(本轮为纯对话,跳过审查)"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    _publish_status(task)
    # 领域事件:任务完成(对话轮,时刻=agent1 结束)
    emit(TASK_COMPLETED, task.id, mode=mode, rounds=rounds)
    perf_log(task.id, "agent1_done", rounds=rounds, conversation_round=True)
    publish(task.id, "agent1_done", {"status": "completed"})
    # 遗留用户消息自动续轮(先于终止 end-scope:有遗留时新流注册 scope,
    # 下方 end-scope 看到活跃流不推 done,SSE 不断线)
    _auto_resume_leftover_messages(task, db, str(task.id))
    _end_event_scope(task.id, flow_gen, ("done", {"status": "completed"}))


def _record_agent2_review(
    db: Session, task: Task, round_idx: int, ua_result: dict,
) -> None:
    """把 agent2 审查模式的输出记录到 Conversation 表(侧栏展示)

    - type="review":审查结论(不进主界面,主界面只放真追问 evaluation)
    - type="suggestions":建议深挖方向(JSON,前端渲染成卡片+深挖按钮)
    - type="summary":沿承旧版最终总结卡(侧栏 summaries 分组)
    """
    ua_result["_recorded"] = True

    covered = ua_result.get("covered", [])
    missing = ua_result.get("missing", [])
    reasoning_text = ua_result.get("reasoning", "")
    suggestions = ua_result.get("suggestions", [])
    review_failed = bool(ua_result.get("degraded")) or bool(ua_result.get("parse_failed"))

    # 审查结论卡(侧栏)
    if review_failed:
        content = "审查未完成:核查未产出,保留 AI助手执行结果"
    else:
        content = f"审查完成:{len(covered)} 个维度通过,{len(missing)} 个待改进"
    full_eval = (
        f"[agent2 审查结论(第 {round_idx} 轮后)]\n"
        f"已覆盖: {covered}\n"
        f"未覆盖: {missing}\n"
        f"判断: {reasoning_text}\n"
    )
    _add_conversation(
        db, task, round_idx=round_idx,
        role="agent2", type="review",
        content=content,
        reasoning=full_eval,
    )

    # 建议深挖卡(有建议才落库;JSON 内容由前端解析渲染)
    if suggestions:
        _add_conversation(
            db, task, round_idx=round_idx,
            role="agent2", type="suggestions",
            content=json.dumps(
                {"suggestions": suggestions}, ensure_ascii=False
            ),
        )

    # 最终总结卡(沿承旧版语义,侧栏 summaries 分组展示最终评估)
    _add_conversation(
        db, task, round_idx=round_idx,
        role="agent2", type="summary",
        content=reasoning_text or "(未给出审查结论)",
    )


def _replace_interim_results(
    db: Session, task: Task, round_idx: int, summary: str,
) -> int:
    """用 agent1 本轮 summary 替换本轮的临时 Result(按轮追加模式)

    仅删除该任务**本轮 round_idx** 的旧 Result(同轮重跑幂等),再落本轮
    summary 为临时结果。跨轮 Result(上轮知识点)保留 —— 知识点按轮
    追加累积,并行审查(老审查写知识点与新轮临时结果共存)互不覆盖。
    """
    db.query(Result).filter(
        Result.task_id == task.id, Result.round_idx == round_idx,
    ).delete()
    interim = [
        Result(
            task_id=task.id,
            round_idx=round_idx,
            title="执行结果(检查助手整理中)",
            content=summary or "(无总结)",
        )
    ]
    for r in interim:
        db.add(r)
    db.commit()
    return len(interim)


def _auto_resume_leftover_messages(task: Task, db: Session, task_id_str: str) -> bool:
    """轮结束后自动续跑遗留的用户补充消息(未被 react_agent drain 的)

    遗留窗口:① 最后一次迭代 drain 之后到达(最终答案生成期间的消息由
    react_agent 循环出口守卫同轮消化,此处兜底收尾阶段的漏网);② CLI
    执行器(acp)本身无 drain 机制,执行期间的消息全部遗留 —— 若不在
    此处处理,会在流结束的清理组被 clear_user_messages 静默丢弃
    (前端已显示但 agent1 从未见过,即"发了没反应"缺陷)。

    处理:遗留消息的 Conversation 从当前轮挪到新轮(round_idx=max+1,
    避免与本轮临时结果/知识点撞轮号),多条合并(\n\n 连接、附件去重)
    后 launch_resume_thread 自动开启新一轮。

    调用时机:必须在流的终止 _end_event_scope 之前 —— 新流先注册
    scope,本流收尾看到活跃流 → 不推 done/不关总线,前端 SSE 不断线,
    新轮事件经现有连接无缝续达(与"核查中追问并行"同一语义)。

    返回 True 表示已启动新一轮(调用方的终止事件让位给新流收尾)。
    """
    leftover = drain_user_messages(task.id)
    if not leftover:
        return False
    try:
        # 挪轮:遗留消息落库在当前轮(运行中入队语义),归入新轮首
        # (与端点 completed 分支的 max+1 落轮规则一致)
        latest = (
            db.query(Conversation)
            .filter(Conversation.task_id == task.id)
            .order_by(Conversation.round_idx.desc())
            .first()
        )
        new_round = (latest.round_idx + 1) if latest else 1
        msg_uuids = [
            uuid.UUID(m["message_id"]) for m in leftover if m.get("message_id")
        ]
        if msg_uuids:
            db.query(Conversation).filter(
                Conversation.id.in_(msg_uuids)
            ).update({"round_idx": new_round}, synchronize_session=False)
            db.commit()
            # 补推 conversation 事件:发送时仅推了 user_message_pending
            # (输入框上方待处理条目),接管时刻让消息进入对话流(新轮首)
            # —— 前端待处理条目随之清除(失败仅记录)
            try:
                moved_convs = (
                    db.query(Conversation)
                    .filter(Conversation.id.in_(msg_uuids))
                    .all()
                )
                for c in moved_convs:
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
            except Exception as pub_err:
                logger.warning(
                    f"[task={task.id}] 遗留消息入流事件推送失败(忽略): {pub_err}"
                )
        # 附件累积进 params(沙箱回收后的重放依据,与端点立即路径一致)
        merged_uploads: list[str] = []
        for m in leftover:
            for uid in (m.get("upload_ids") or []):
                if uid and uid not in merged_uploads:
                    merged_uploads.append(uid)
        if merged_uploads:
            existing = list((task.params or {}).get("followup_upload_ids") or [])
            for uid in merged_uploads:
                if uid not in existing:
                    existing.append(uid)
            task.params = {**(task.params or {}), "followup_upload_ids": existing}
            db.commit()
        merged = "\n\n".join(m.get("content") or "" for m in leftover).strip()
        if not merged:
            return False
        logger.info(
            f"[task={task.id}] 遗留用户消息 {len(leftover)} 条,"
            f"自动开启新一轮处理(round={new_round})"
        )
        launch_resume_thread(
            task_id_str, merged,
            upload_ids=merged_uploads or None,
        )
        return True
    except Exception as e:
        # 兜底:自动续跑失败不阻塞本流收尾(与旧静默清理行为一致,仅记录)
        logger.warning(f"[task={task.id}] 遗留消息自动续跑失败(忽略): {e}")
        return False


def _run_background_review(
    task: Task, db: Session, *,
    user_intent: str,
    react_summaries: list[dict],
    scenario_id: str,
    llm_client: LLMClient | None,
    agent_policy: dict,
    task_id_str: str,
    react_client: LLMClient | None = None,
    flow_gen: int,
    round_idx: int,
) -> None:
    """后台审查:agent2 单次完整核查 + 知识点落库 + 终止判定 + 下游链

    在 agent1 完成后的同一后台线程内执行;**永不抛异常** ——
    所有失败都转为 review_status=failed(保留临时结果),任务保持 COMPLETED。
    审查结束(无论成败)推送 review_done;done/finish 由 _end_event_scope
    统一判定(并行流在跑时保持总线打开)。下游链(记忆/练习题/预压缩)
    仅最新世代流执行,避免并行重复。

    知识点按轮追加:只删本轮 round_idx 的临时 Result,跨轮知识点保留。
    round_idx 必须由调用方显式传入(= 本流 agent1 执行轮),不得用
    len(react_summaries) 推导 —— "某轮有对话却无 thinking 记录"(如
    executor 在首次思考前崩溃后重试)时二者会错位,导致删错轮、遗留陈旧 interim。

    并行世代门控:本流被更新世代取代时(用户追问/遗留续轮已启动新一轮)——
    ① verify/PoC 动态验证降级跳过(与新轮共享沙箱,防端口/进程争抢);
    ② 任务级字段(review_status/current_stage)与 review_done 事件归新流
    所有,本流跳过写入(防 badge 被老审查收尾值短暂覆盖);
    知识点落库与审查结论卡不受影响(按轮追加,历史完整)。
    """
    def _superseded() -> bool:
        return not _is_latest_generation(task_id_str, flow_gen)

    try:
        if _superseded():
            # 审查启动前已被新流取代(如遗留消息自动续轮先启动):
            # 纯只读降级,不动任务级字段(stage/badge 归新流所有)
            logger.info(
                f"[task={task.id}] 审查启动时已被新流取代,降级为只读核查"
                f"(gen={flow_gen})"
            )
        else:
            task.current_stage = "检查助手审查中"
            db.commit()
            _publish_status(task)

        # agent2 只读核查用的工作区路径:预克隆失败时 agent1 可能已自主
        # clone,从会话信息刷新(无会话/未 clone 时为 None,禁用只读工具)
        _ws_info = sandbox_tools.get_workspace_info(task_id_str)
        cur_repo_path = (_ws_info or {}).get("repo_path") or None

        _t0 = time.perf_counter()
        try:
            ua_result = run_agent2(
                user_intent, react_summaries,
                task_id=task.id, db=db, round_idx=round_idx,
                scenario_id=scenario_id, client=llm_client,
                user_id=task.user_id,
                repo_url=(task.params or {}).get("repo_url"),
                task=task, agent_policy=agent_policy,
                repo_path=cur_repo_path,
                superseded_check=_superseded,
            )
        except Exception as review_err:
            # run_agent2 内部已兜底降级,这里是最后防线(DB 异常等)
            logger.exception(f"[task={task.id}] 后台审查执行异常")
            ua_result = {
                "covered": [], "missing": [],
                "reasoning": f"审查执行异常: {review_err}",
                "suggestions": [], "results": [], "grouping": None,
                "degraded": True,
            }
        perf_log(
            task.id, "review_eval", time.perf_counter() - _t0,
            round_idx=round_idx,
        )
        _record_agent2_review(db, task, round_idx, ua_result)

        review_failed = (
            bool(ua_result.get("degraded"))
            or bool(ua_result.get("parse_failed"))
            or not ua_result.get("results")
        )

        if review_failed:
            # 审查失败:保留 agent1 summary 临时结果,只标记子状态
            # (被新流取代时跳过任务级字段:badge 归新流所有)
            if not _superseded():
                task.review_status = "failed"
                task.current_stage = "任务完成(检查未完成,已保留执行结果)"
            logger.warning(
                f"[task={task.id}] 后台审查未完成"
                f"(degraded={bool(ua_result.get('degraded'))},"
                f"parse_failed={bool(ua_result.get('parse_failed'))},"
                f"results={len(ua_result.get('results') or [])}),保留临时结果"
            )
        else:
            # 审查完成:本轮临时 Result 替换为重点与知识点(按轮追加:
            # 仅删本轮 round_idx 的临时结果,跨轮知识点保留不覆盖;
            # 知识点落库不受世代门控影响 —— 按轮产出,历史完整)
            structured_results = ua_result.get("results") or []
            grouping = ua_result.get("grouping")
            db.query(Result).filter(
                Result.task_id == task.id,
                Result.round_idx == round_idx,
            ).delete()
            for r in structured_results:
                db.add(Result(
                    task_id=task.id,
                    round_idx=round_idx,
                    title=r.get("title", "(无标题)"),
                    content=r.get("content", ""),
                    metadata_=r.get("metadata"),
                ))
            db.commit()
            # 把 grouping 存到 task.params 供前端读取(结果分组声明)
            if grouping:
                if task.params is not None:
                    task.params = {**(task.params or {}), "_grouping": grouping}
                else:
                    task.params = {"_grouping": grouping}
                db.commit()
            if not _superseded():
                task.review_status = "done"
                task.current_stage = (
                    f"任务完成,检查助手整理出 {len(structured_results)} 个重点与知识点"
                )
            logger.info(
                f"[task={task.id}] 后台审查完成,"
                f"整理 {len(structured_results)} 个结构化结果,"
                f"{len(ua_result.get('suggestions') or [])} 条建议"
            )

        # 世代门控:被新流取代时跳过 badge 更新与 review_done 事件
        # (review_status/current_stage 归新流所有,防老审查收尾值覆盖
        #  新轮的 running 造成侧栏 badge 短暂抖动)
        if _superseded():
            logger.info(
                f"[task={task.id}] 审查收尾时已被新流取代,跳过任务级字段"
                f"更新与 review_done 事件(gen={flow_gen})"
            )
        else:
            db.commit()
            _publish_status(task)

            # 领域事件 + perf 锚点:审查完成(含失败;对照实验的审查时延以此为准)
            emit(
                REVIEW_COMPLETED, task.id,
                review_status=task.review_status,
                results_count=len(ua_result.get("results") or []),
                suggestions_count=len(ua_result.get("suggestions") or []),
            )
            perf_log(task.id, "review_done", review_status=task.review_status)

            # 通知前端审查结束(侧栏 badge 更新 + 拉取最终结果)。
            publish(task.id, "review_done", {"review_status": task.review_status})
        # done/finish 由 _end_event_scope 统一判定:并行流(用户追问已启动
        # 新一轮 agent1/resume)仍在跑时保持总线打开,由最后活跃流收尾
        # (被取代也必须调:注销自身 scope,否则总线永久悬挂)
        _end_event_scope(task.id, flow_gen, ("done", {"status": "completed"}))

        # ---- 下游链(依赖最终结果,必须在审查后;仅最新流执行,避免并行重复)----
        if not _is_latest_generation(task.id, flow_gen):
            logger.info(
                f"[task={task.id}] 并行流已接管(gen={flow_gen}),跳过审查下游链"
            )
            return

        # 任务成功完成:自动归纳写入长期记忆(失败兜底,不影响任务完成;模型/结构走用户「记忆设置」)
        try:
            from app.services.memory_summarize import summarize_and_save_memory
            summarize_and_save_memory(task, db)
        except Exception as mem_err:
            logger.warning(f"[task={task.id}] 归纳写入记忆失败(忽略): {mem_err}")

        # 自动生成练习题 draft(失败兜底,不影响任务完成;产出仍需用户确认)
        if settings.PRACTICE_ENABLED:
            try:
                from app.services.practice.auto_generate import auto_generate_practice_for_task
                auto_generate_practice_for_task(task, db)
            except Exception as practice_err:
                logger.warning(f"[task={task.id}] 自动生成练习题失败(忽略): {practice_err}")

        # 预压缩早期历史:用户下一轮追问直接命中缓存,消除追问路径上的
        # 同步 LLM 压缩延迟(失败兜底,内部已捕获)
        _precompress_history_safely(task, db, round_idx, react_client)

    except Exception as e:
        # 最后防线:保证终止事件一定推送(SSE 不悬挂),任务保持 COMPLETED
        logger.exception(f"[task={task.id}] 后台审查收尾异常(强制终止总线)")
        try:
            if not _superseded():
                # 任务级字段与 review_done 归最新流所有(世代门控)
                task.review_status = "failed"
                task.current_stage = "任务完成(检查异常终止,已保留执行结果)"
                db.commit()
                publish(task.id, "review_done", {"review_status": "failed"})
            _end_event_scope(task.id, flow_gen, ("done", {"status": "completed"}))
        except Exception:
            _end_event_scope(task.id, flow_gen)


def _publish_status(task: Task) -> None:
    """推送任务状态变更事件"""
    publish(task.id, "status", {
        "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
        "current_stage": task.current_stage,
    })


def _build_llm_client(
    db: Session,
    user_id,
    llm_config_id: str | None = None,
    *,
    label: str = "llm_config_id",
) -> LLMClient | None:
    """按 user_id + llm_config_id 加载用户保存的 LLM 配置

    - user_id 为空(匿名任务)或 llm_config_id 为空 → 返回 None,agent 回退到 env 默认
    - 找到指定配置 → 返回 LLMClient.from_config_dict(...)
    - 找不到配置 id 或构造失败 → 记日志并回退到 None

    label 仅用于日志区分(agent2 / react_agent)。
    """
    if user_id is None or not llm_config_id:
        return None
    try:
        cfg = db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user_id).first()
        if cfg is None:
            return None
        # 从配置列表中按 id 查找
        target = None
        for c in cfg.llm_configs:
            if c.get("id") == llm_config_id:
                target = c
                break
        if target is None:
            logger.warning(
                f"[user={user_id}] 未找到 {label}={llm_config_id},回退到 env 默认"
            )
            return None
        return LLMClient.from_config_dict(target)
    except Exception as e:
        logger.warning(f"[user={user_id}] 加载用户 LLM 配置失败({label}),回退到 env 默认: {e}")
        return None


def _build_react_llm_client(
    db: Session,
    task: Task,
) -> tuple[LLMClient | None, str]:
    """构造内置 react_agent 使用的 LLMClient

    仅 executor=builtin 时调用方有意义(外部 CLI 忽略 client)。
    优先级:task.react_llm_config_id → task.llm_config_id(回退) → None(env 默认)

    返回 (client, source_label):
        source_label 取值 "react_llm_config_id" / "llm_config_id" / "env_default",
        仅供日志记录用。
    """
    react_id = task.react_llm_config_id
    if react_id:
        client = _build_llm_client(
            db, task.user_id, react_id, label="react_llm_config_id"
        )
        # 即使 client 为 None(未找到/加载失败)也认为用的是 react_llm_config_id 槽位
        return client, "react_llm_config_id"
    # 回退到 agent2 的配置
    client = _build_llm_client(
        db, task.user_id, task.llm_config_id, label="llm_config_id(fallback)"
    )
    return client, "llm_config_id"


def _load_git_tokens(db: Session, user_id) -> dict[str, str]:
    """加载用户所有 git provider 的 access_token(解密明文)

    返回 {provider: token},只含有 access_token 的 provider(已显式绑定仓库)。
    空 dict 表示未绑定任何平台或解密失败,clone_repo 会回退到 SSH/匿名 HTTPS。
    """
    if user_id is None:
        return {}
    try:
        from app.models.user_git_binding import UserGitBinding

        bindings = (
            db.query(UserGitBinding)
            .filter(
                UserGitBinding.user_id == user_id,
                UserGitBinding.access_token != "",
            )
            .all()
        )
        tokens: dict[str, str] = {}
        for b in bindings:
            try:
                tokens[b.provider] = decrypt_secret(b.access_token)
            except Exception as e:
                logger.warning(f"[user={user_id}] 解密 {b.provider} token 失败: {e}")
        return tokens
    except Exception as e:
        logger.warning(f"[user={user_id}] 加载 git tokens 失败: {e}")
        return {}


# ============================================================
# 预处理:主动 clone + list_files(把仓库结构提前准备好)
# ============================================================


def _prepare_repo_context(
    task: Task, db: Session, task_id_str: str, git_tokens: dict | None = None,
) -> tuple[str | None, str]:
    """若用户选了仓库,主动 clone + list_files,返回 (repo_path, repo_context 文本)

    - 无 repo_url:返回 (None, ""),走原流程(react_agent 自主 clone)
    - 有 repo_url:clone(HTTPS+token → SSH → HTTPS 匿名,含分支回退);
      失败不抛异常,降级返回 (None, "") → 回到 react_agent 自主 clone 路径
      (工具失败可被 LLM 重试/自适应,成功率高于预 clone 一次定生死);
      成功且仓库非空时 list_files 根目录,格式化成 repo_context 文本;
      仓库为空(或 list_files 失败无法确认非空)则降级返回 (repo_path, ""),
      不注入"已预先 clone"提示,避免误导 agent 在空仓库上直接开始审计

    git_tokens 用于访问私有仓库({provider: token},按 repo_url 主机匹配;
    无匹配则只试 SSH + 匿名 HTTPS)。

    repo_context 会注入:
      - react_agent 第 1 轮(传 repo_context 参数):跳过自主 clone 直接审计

    主动 clone 复用 sandbox_tools 的 session 管理,完成后 react_agent / workspace
    路由可通过 task_id 直接复用同一会话(前端工作区侧栏也立即可用)。
    """
    params = task.params or {}
    repo_url = params.get("repo_url")

    # sandbox 模式:先于记忆文件写入预建会话并挂载 bare 仓库缓存
    # (记忆文件写入也会创建会话但不带 repo_url,容器无法追加挂载,
    # 机会不可逆;幂等,已有会话直接复用)
    if repo_url:
        sandbox_tools.precreate_session_for_repo(
            task_id_str, repo_url,
            branch=params.get("branch"), git_tokens=git_tokens or {},
        )

    # 记忆文件提前写入(不依赖是否选仓库,供执行侧 read_file 查全量):
    # - 全局记忆文件:无条件写(无记忆则写空串清残留)
    # - 项目记忆文件:选了仓库且能匹配到 Project 时写,否则写空串清残留
    _write_memory_files_for_task(task, db, task_id_str, repo_url)

    # 上传交付物分支(优先于 clone;创建时已保证与 repo_url 互斥):
    # 把上传内容传输进沙箱工作区。上传文件 agent 无法自行重新获取,
    # 服务端文件缺失/传输失败都直接失败,不降级
    if _creation_upload_ids(params):
        return _prepare_upload_context(task, db, task_id_str)

    if not repo_url:
        return None, ""

    branch = params.get("branch")

    # 主动 clone(协议回退:HTTPS+token → SSH → HTTPS 匿名,含分支回退)
    task.current_stage = "正在克隆仓库(HTTPS+token → SSH → 匿名)..."
    db.commit()
    _publish_status(task)

    try:
        clone_result = sandbox_tools.clone_repo_with_fallback(
            repo_url, branch=branch, task_id=task_id_str, git_tokens=git_tokens or {},
            cancellable=True,
        )
    except sandbox_tools.CloneSkippedError:
        # 用户主动跳过预克隆:与失败降级同路径,改由 react_agent 自主克隆
        # (LLM 看报错重试/自适应,历史观察成功率更高)
        logger.info(
            f"[task={task.id}] 用户跳过预克隆,降级为 react_agent 自主克隆"
        )
        task.current_stage = "已跳过预克隆,改由执行阶段自主克隆..."
        db.commit()
        _publish_status(task)
        _add_conversation(
            db, task, round_idx=0,
            role="system", type="warning",
            content="用户已跳过预克隆,改由执行阶段自主克隆(可能多耗时几十秒)",
        )
        return None, ""
    except Exception as e:
        # 降级而非失败:预 clone 一次定生死太脆(网络抖动/分支不符/私有仓库
        # token 问题都会直接挂任务),改为回到 react_agent 自主 clone 路径,
        # 由 LLM 看报错重试/自适应(历史观察该路径成功率明显更高)
        logger.warning(
            f"[task={task.id}] 主动 clone 失败,降级为 react_agent 自主 clone: {e}"
        )
        task.current_stage = "预克隆失败,改由 react_agent 自主克隆..."
        db.commit()
        _publish_status(task)
        _add_conversation(
            db, task, round_idx=0,
            role="system", type="warning",
            content=(
                f"预先克隆仓库失败,已降级为执行阶段自主克隆:\n{str(e)[:500]}"
            ),
        )
        return None, ""
    repo_path = clone_result["path"]
    logger.info(
        f"[task={task.id}] 主动 clone 成功,path={repo_path},"
        f"files_count={clone_result.get('files_count')}"
    )

    # (记忆文件已在任务启动时写入,见 _write_memory_files_for_task)

    # 主动 list_files(根目录),把结构拼进上下文
    task.current_stage = "正在读取仓库根目录结构..."
    db.commit()
    _publish_status(task)

    try:
        files_result = sandbox_tools.list_files(repo_path, task_id=task_id_str)
    except Exception as e:
        # list_files 失败不应让整个任务失败(clone 已成功),降级为只给 repo_path
        logger.warning(f"[task={task.id}] list_files 失败,降级为仅 repo_path: {e}")
        files_result = {"entries": [], "total": 0, "truncated": False}

    # 仓库树快照保底:clone 成功、沙箱健康时立即捕获一份落库,
    # 后续任务失败/零改动/git 异常导致 diff 缺失时,侧栏仍能兜底展示文件清单
    # (任务结束段会再更新为最终态;此处失败不影响主流程)
    try:
        from app.services.workspace_diff import save_repo_tree_artifact
        save_repo_tree_artifact(task, db, task_id_str)
    except Exception as tree_err:
        logger.warning(f"[task={task.id}] 捕获仓库树快照失败(忽略): {tree_err}")

    # 仅当仓库非空才注入上下文:根目录无条目(空仓库/list_files 降级)时,
    # 告诉 agent "已 clone、直接开始审计"会误导,降级为仅 repo_path
    if not files_result.get("entries"):
        logger.info(
            f"[task={task.id}] clone 成功但根目录为空,"
            f"不注入 repo_context(降级为仅 repo_path={repo_path})"
        )
        return repo_path, ""

    repo_context = format_repo_context_body(repo_url, repo_path, files_result)
    return repo_path, repo_context


def _creation_upload_ids(params: dict | None) -> list[str]:
    """创建时上传的 upload_id 列表(委托 upload_layout,单一来源)

    旧任务只写单数 upload_id;新任务写 upload_ids 列表。沙箱拷贝侧与
    工作区回退浏览侧共用同一解析,保证布局一致。
    """
    from app.services.upload_layout import extract_creation_ids

    return extract_creation_ids(params)


def _followup_upload_ids(params: dict | None) -> list[str]:
    """追问累积上传的 upload_id 列表(委托 upload_layout,单一来源)

    沙箱回收后 _restore_workspace_if_needed 需重放这些文件到 followup_uploads/,
    保证追问上传与创建上传一样可在恢复后重现;工作区回退浏览也按此列表
    重建 followup_uploads/{i}-{name} 布局(i 为列表下标,顺序即追加顺序)。
    """
    from app.services.upload_layout import extract_followup_ids

    return extract_followup_ids(params)


def _prepare_upload_context(
    task: Task, db: Session, task_id_str: str,
) -> tuple[str, str]:
    """上传交付物:把上传内容传输进任务沙箱工作区

    与 clone 分支的差异:
    - 上传文件 agent 无法自主重新获取(clone 失败可降级为 react_agent 自主
      clone 重试,上传没有对应手段),故服务端文件缺失 / 传输失败都直接
      抛异常让任务失败,不降级——降级只会让 agent 对着不存在的仓库盲跑
    - repo_context 的 header 明确"文件已就绪",提示 agent1 跳过 clone
      直接开始处理

    支持创建时多文件(upload_ids):
    - 单个上传:文件直接铺在工作根 uploaded_files/(保持既有布局)
    - 多个上传:工作根 = uploaded_files/,各上传各占 {i}-{清洗文件名}/ 子目录防碰撞
    """
    from app.services.uploads import (
        UploadError,
        load_upload_meta,
        materialize_upload_files,
    )

    creation_ids = _creation_upload_ids(task.params)
    if not creation_ids:
        raise RuntimeError("上传交付物不可用:缺少 upload_id")

    # 预加载各上传 meta(供 header 文案;缺失即失败,不降级)
    metas: list[dict] = []
    for uid in creation_ids:
        try:
            metas.append(load_upload_meta(uid))
        except UploadError as e:
            raise RuntimeError(f"上传内容不可用: {e}") from e

    task.current_stage = "正在把上传文件传输进任务工作区..."
    db.commit()
    _publish_status(task)

    # materialize:local 后端直接给真源目录(退出不删);s3 后端下载到临时目录,
    # with 退出即清理——故传输进沙箱必须在 with 块内完成。
    try:
        if len(creation_ids) == 1:
            # 单个上传:保持既有根布局(文件直接铺在 uploaded_files/)
            with materialize_upload_files(creation_ids[0]) as files_dir:
                repo_path = sandbox_tools.transfer_upload_to_workspace(
                    task_id_str, str(files_dir)
                )
        else:
            # 多个上传:工作根 = uploaded_files/,各上传进 {i}-{name}/ 子目录
            repo_path = sandbox_tools.transfer_uploads_to_workspace_root(
                task_id_str, creation_ids
            )
    except UploadError as e:
        raise RuntimeError(f"上传内容不可用: {e}") from e
    logger.info(
        f"[task={task.id}] 上传交付物传输完成,path={repo_path},"
        f"count={len(creation_ids)}"
    )

    # 根目录列表 → repo_context(同 clone 分支;list_files 复用同一会话)
    try:
        files_result = sandbox_tools.list_files(repo_path, task_id=task_id_str)
    except Exception as e:
        logger.warning(f"[task={task.id}] list_files 失败,降级为仅 repo_path: {e}")
        files_result = {"entries": [], "total": 0, "truncated": False}

    # 仓库树快照保底(同 clone 分支,任务失败/零改动时侧栏兜底展示)
    try:
        from app.services.workspace_diff import save_repo_tree_artifact
        save_repo_tree_artifact(task, db, task_id_str)
    except Exception as tree_err:
        logger.warning(f"[task={task.id}] 捕获仓库树快照失败(忽略): {tree_err}")

    # 空目录不注入 repo_context(同 clone 分支,避免误导 agent)
    if not files_result.get("entries"):
        logger.info(
            f"[task={task.id}] 上传内容根目录为空,不注入 repo_context"
            f"(降级为仅 repo_path={repo_path})"
        )
        return repo_path, ""

    header = build_upload_header(metas, repo_path)
    repo_context = format_repo_context_body("", repo_path, files_result, header=header)
    return repo_path, repo_context


def _write_memory_files_for_task(
    task: Task, db: Session, task_id_str: str, repo_url: str | None,
) -> None:
    """把记忆文件写入沙箱固定路径(供 react_agent / CLI 随时 read_file 查阅)

    - 全局记忆文件:写入 UserMemory.content(无则写空串清残留),无条件写
    - 项目记忆文件:按 user_id + repo_url 归一化查 Project,取 memory_content 写入;
      无 Project/无记忆则写空串(清空上一个项目残留,避免看到无关记忆)

    任何异常都 catch + log,不阻塞任务启动。
    """
    try:
        from app.models.user_memory import UserMemory

        global_content = ""
        if task.user_id is not None:
            mem = (
                db.query(UserMemory)
                .filter(UserMemory.user_id == task.user_id)
                .first()
            )
            if mem and mem.content:
                global_content = mem.content.strip()
        sandbox_tools.write_global_memory_file(task_id_str, global_content)
        logger.info(
            f"[task={task.id}] 已写入全局记忆文件 "
            f"(/home/user/.agent_memory/global_memory.md, {len(global_content)} 字符)"
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 写入全局记忆文件失败(忽略): {e}")

    try:
        from app.models.project import Project
        from app.services.repo_url import normalize_repo_url

        memory_content = ""
        norm = normalize_repo_url(repo_url) if repo_url else ""
        if norm and task.user_id is not None:
            proj = (
                db.query(Project)
                .filter(
                    Project.user_id == task.user_id,
                    Project.repo_url_normalized == norm,
                )
                .first()
            )
            if proj:
                memory_content = proj.memory_content or ""
        sandbox_tools.write_project_memory_file(task_id_str, memory_content)
        logger.info(
            f"[task={task.id}] 已写入项目记忆文件 "
            f"(/home/user/.agent_memory/project_memory.md, {len(memory_content)} 字符)"
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 写入项目记忆文件失败(忽略): {e}")


def _restore_workspace_if_needed(
    task: Task, db: Session, task_id_str: str, git_tokens: dict | None = None,
) -> bool:
    """沙箱会话已被回收且任务配了仓库 → 重新克隆恢复工作区

    判断与 retry_failed_task 一致:无 repo_url 或会话存活时跳过。
    用户追问(resume)与失败重试共用本助手,消除两条链路在
    工作区恢复上的不对称——否则追问轮只能新建空沙箱,
    react_agent 拿不到 repo_path 提示,行为不确定。

    _prepare_repo_context 内部已写记忆文件,且 clone 失败时降级
    不抛异常(续跑时 react_agent 可自主克隆),故返回 True 后
    调用方无需再写记忆文件。返回是否执行了恢复动作。

    上传任务(upload_id)同样适用:沙箱回收后重新传输上传内容
    (agent 无法自行重新获取,必须由服务端恢复)。
    """
    params = task.params or {}
    repo_url = params.get("repo_url")
    creation_ids = _creation_upload_ids(params)
    followup_ids = _followup_upload_ids(params)
    if (
        not (repo_url or creation_ids)
        or sandbox_tools.get_workspace_info(task_id_str) is not None
    ):
        return False
    is_upload_task = bool(creation_ids)
    logger.info(
        f"[task={task.id}] 沙箱会话已回收,"
        + ("重新传输上传文件恢复工作区" if is_upload_task else "重新克隆仓库恢复工作区")
    )
    task.current_stage = (
        "工作区已被回收,重新传输上传文件恢复..."
        if is_upload_task
        else "工作区已被回收,正在重新克隆恢复..."
    )
    db.commit()
    _publish_status(task)
    _prepare_repo_context(task, db, task_id_str, git_tokens)
    # 重新 clone 出来是干净仓库:把重启前已保存的 git_diff artifact 补回
    # (B3:重启前后工作成果不丢;截断的 patch 不可 apply 会自动跳过;
    # 失败仅 warning,不阻断恢复)
    try:
        from app.services.workspace_diff import reapply_workspace_diff

        reapply_workspace_diff(task, db, task_id_str)
    except Exception as e:
        logger.warning(f"[task={task.id}] 恢复补回 diff 失败(忽略): {e}")
    # 追问上传同样需重放(沙箱回收后追问文件与创建文件一起丢失):
    # 传输到 followup_uploads/,与运行中追问落地位置一致(agent 目录级感知)。
    # 失败不阻断恢复(创建内容已就位,追问文件缺失仅影响该部分上下文)。
    if followup_ids:
        try:
            sandbox_tools.add_uploads_to_workspace(
                task_id_str, followup_ids, "followup_uploads"
            )
        except Exception as e:
            logger.warning(
                f"[task={task.id}] 恢复追问上传文件失败(忽略): {e}"
            )
    return True


# ============================================================
# 完成后重启:用户在 task 完成后追加消息,触发新一轮协作
# ============================================================


def resume_audit_with_message(
    task: Task, db: Session, user_message: str, retry: bool = False,
    upload_ids: list[str] | None = None, flow_gen: int | None = None,
) -> None:
    """用户在任务完成后追加消息,重启执行(后台审查版)

    retry=True 时表示失败任务重试(断点续跑),消息措辞与阶段文案
    改为重试语境,其余流程一致。

    流程(每次 resume = agent1 一轮 + 后台审查,多轮由用户驱动):
    1. task.status: COMPLETED/FAILED → RUNNING(不等老审查:老审查
       继续在后台跑完,与本轮 agent1 并行,见"并行语义"注释)
    2. 加载历史上下文(react_summaries / LLM 配置),恢复工作区
    3. 起始 round_idx:用户追加消息时复用消息所在轮(消息与首轮 react 执行
       同轮,不隔轮);失败重试时从 max+1 续接新轮
    4. agent1 直接执行用户消息(原文直传,不经 agent2 转述;
       跨轮历史由 react_agent._build_history_messages 以结构化 messages
       注入,用户追问原文作为独立 user 消息;plan 状态从 task.params["_plan"]
       跨轮续接,已完成项不重规划)
    5. 轮次类型判定:纯对话轮(本轮无工具调用)直接收尾,保留既有结果
       与审查状态,跳过审查/练习题/记忆归纳
    6. agent1 轮结束:summary 落临时结果,任务 COMPLETED(推 agent1_done)
    7. 后台审查(同初始运行):知识点按轮追加,推 review_done;done/finish
       由最后活跃流统一收尾(_end_event_scope)

    并行语义(事件活跃期 scope):追问可在老审查未结束时直接启动本函数,
    老审查线程与本线程并行 —— 老审查照常落库自己轮次的知识点/结论,
    本流照常执行;done/finish 仅由最后活跃流推送,互不干扰。

    用户消息本身已由 API 端点落库为 Conversation(role=user, type=message),
    本函数不重复落库。
    """
    task_id_str = str(task.id)

    # 注册本流事件活跃期(先于状态翻转:确保老流收尾判定能看到本流)。
    # launch_resume_thread 已同步注册并传入 gen(消除端点→线程注册窗口);
    # 直接调用方(retry 链路/测试)未传时在此自行注册
    if flow_gen is None:
        flow_gen = _begin_event_scope(task.id)

    task.status = TaskStatus.RUNNING
    task.current_stage = (
        "重试失败任务,恢复执行" if retry else "用户追加消息,重启执行"
    )
    task.error_message = None  # 清除之前的错误信息(若有)
    db.commit()
    _publish_status(task)

    # 加载上下文(与 run_dual_agent_audit 一致)
    # llm_client:agent2 评估;react_client:内置 react_agent(空时回退到 llm_config_id)
    llm_client = _build_llm_client(db, task.user_id, task.llm_config_id)
    react_client, _ = _build_react_llm_client(db, task)
    git_tokens = _load_git_tokens(db, task.user_id)
    set_current_git_tokens(git_tokens)
    allowed_skills = task.allowed_skills
    set_current_task(task_id_str, task.scenario, allowed_skills)

    # 执行器选择:按 task.executor 拿到对应的 ExecutorAgent provider
    executor = get_executor(task)

    # 加载 agent 策略(agent2 启停、验证权限等)
    # 合并用户级默认(agent_policies 表)+ 任务级覆盖(task.params["_agent_policy"])
    agent_policy = resolve_agent_policy(task, db)
    logger.info(f"[task={task.id}] resume agent_policy: {agent_policy}")

    # agent2 启停(重启场景)
    ua_enabled = bool(agent_policy.get("agent2_enabled", True))
    logger.info(
        f"[task={task.id}] resume agent2_enabled={ua_enabled}"
    )

    react_summaries = _load_react_summaries(db, task.id)
    # plan 跨轮连续:加载上次持久化的 plan 作为本轮起点,
    # 已完成项保持 done,只推进未完成项;追问若改变方向,LLM 可在
    # <plan> 更新中新增/调整步骤(_merge_plan 按 text 匹配合并)
    previous_plan = _load_plan_from_task(task)

    # 起始轮:用户追加消息时复用消息所在轮(消息已由 API 端点落库为最新轮,
    # react 执行与该消息同轮——用户消息 → 执行 → 审查构成一轮完整闭环,
    # 不隔轮);失败重试时无新消息,从 max+1 续接新轮。
    start_round_idx = _get_next_round_idx(db, task.id, retry=retry)
    # [perf] resume 锚点(用户追加消息后重启;与 user_message 锚点配对算总延迟)
    perf_log(
        task.id, "resume_start",
        ua_enabled=ua_enabled, executor=executor.name,
        start_round=start_round_idx,
    )

    # 会话已被回收且配了仓库 → 与重试链路一致,重新克隆恢复工作区
    # (_prepare_repo_context 内部已写记忆文件);否则幂等补写记忆文件
    # (会话存活时覆盖同内容,保证执行侧 read_file 能查到全量记忆)
    # 重试链路进入本函数前已自行恢复过工作区,此处会话存活会自然跳过,不会双重 clone
    _t0 = time.perf_counter()
    repo_url = (task.params or {}).get("repo_url")
    restored = _restore_workspace_if_needed(task, db, task_id_str, git_tokens)
    if not restored:
        _write_memory_files_for_task(task, db, task_id_str, repo_url)
    perf_log(
        task.id,
        "restore_workspace" if restored else "write_memory_files",
        time.perf_counter() - _t0,
    )

    # 本轮追问附带的文件:传输进工作区 followup_uploads/(不重定向 repo_path)。
    # restored=True 时 _restore_workspace_if_needed 已按 params.followup_upload_ids
    # 重放(API 端点在调用前已把本轮新 ids 写入 params),此处跳过避免重复传输;
    # restored=False(会话存活)时传**全量累积列表**而非仅本轮新 ids ——
    # 全局下标与重放/工作区回退浏览一致,避免每轮下标从 0 重启导致的路径
    # 漂移与同名清洗目录被 clear_dest 覆盖丢文件(旧附件已被 GC 时由
    # add_uploads_to_workspace 逐上传容错跳过)。
    attachment_note = ""
    if upload_ids:
        if not restored:
            merged_ids = _followup_upload_ids(task.params)
            for uid in upload_ids:
                if uid and uid not in merged_ids:
                    merged_ids.append(uid)  # 防御:params 未含本轮 ids 的边缘情况
            try:
                sandbox_tools.add_uploads_to_workspace(
                    task_id_str, merged_ids, "followup_uploads"
                )
            except Exception as e:
                logger.warning(
                    f"[task={task.id}] 追问上传文件传输失败(忽略,文字消息照常处理): {e}"
                )
        attachment_note = RESUME_ATTACHMENT_NOTE

    # agent1 本轮执行的追问文本(含附件提示,让 agent 感知新文件)
    followup_text = user_message + attachment_note

    # 把用户消息拼到 user_intent 后面,供 agent1 结束后的后台审查参考
    # (审查需知道完整意图,含追问/重试语境);重试场景用专门标记,
    # 避免审查把续跑当成用户新增需求
    msg_label = RETRY_MSG_LABEL if retry else USER_FOLLOWUP_MSG_LABEL
    effective_intent = task.user_input + f"\n\n{msg_label}\n{followup_text}"

    # 本轮是否正常完成(内存标志):与 run_dual_agent_audit 同理,
    # finally 兑底 error 推送必须用它判定,不能用 task.status ——
    # 本轮完成后用户可能又发新追问(状态被改回 RUNNING),旧线程 finally
    # 按状态判定会误推 error 并重新标记总线结束,导致新 resume 线程事件全丢。
    normal_completed = False

    try:
        # ===== 用户消息直接交给 agent1(不经 agent2 转述) =====
        # agent2 职责收敛为后台审查:追问/重试消息原文直传 agent1,
        # 跨轮历史由 react_agent._build_history_messages 注入
        wait_if_paused(task.id)
        task.current_stage = "AI助手执行" if ua_enabled else "AI助手执行(单 agent)"
        db.commit()
        _publish_status(task)

        _t0 = time.perf_counter()
        _results, summary, current_plan = executor.run(
            task, db,
            round_idx=start_round_idx,
            followup_query=followup_text,
            client=react_client,
            repo_context=None,  # 重启不传 repo_context(仓库已 clone,react_agent 自行从 sandbox 取)
            previous_plan=previous_plan,  # plan 跨轮续接(见上方加载注释)
        )
        perf_log(task.id, "executor_run", time.perf_counter() - _t0, round_idx=start_round_idx, executor=executor.name)
        emit(
            AGENT1_ROUND_COMPLETED, task.id,
            round_idx=start_round_idx, executor=executor.name,
            results_count=len(_results),
        )
        react_summaries.append({"round": start_round_idx, "summary": summary})
        # plan 状态持久化(供下一次 resume 续接)
        _save_plan_to_task(task, db, current_plan)

        # ===== 轮次类型判定:纯对话轮(仅双 agent 模式)跳过审查与重下游 =====
        # 本轮无任何工具调用 → agent1 回答完全来自历史上下文,无新证据可审;
        # 保留既有整理结果与审查状态,不触发练习题/记忆归纳。
        # 单 agent 模式本就无审查链,保持原行为(结果替换为 summary)
        if ua_enabled and not _round_has_tool_calls(db, task.id, start_round_idx):
            _finish_conversation_round(
                task, db, rounds=len(react_summaries), mode="resume",
                flow_gen=flow_gen,
            )
            normal_completed = True  # 正常完成:finally 不再兑底推 error
            return  # finally 块仍会执行清理

        # 本轮 summary 落临时结果(按轮追加:仅替换本轮,跨轮知识点保留)
        _replace_interim_results(db, task, start_round_idx, summary)

        # ===== 单 agent 模式:agent2 已禁用,执行完直接收尾(无审查) =====
        if not ua_enabled:
            logger.info(f"[task={task.id}] resume 单 agent 模式(agent2 已禁用)")
            _finish_resume(
                task, db, react_summaries,
                react_client=react_client, flow_gen=flow_gen,
                round_idx=start_round_idx,
            )
            normal_completed = True  # 正常完成:finally 不再兑底推 error(见 finally 注释)
            return  # finally 块仍会执行清理

        # ---------- agent1 结束即任务完成(同初始运行) ----------
        task.status = TaskStatus.COMPLETED
        task.current_stage = "任务完成,检查助手审查中"
        task.completed_at = datetime.now(timezone.utc)
        task.review_status = "running"
        db.commit()
        _publish_status(task)

        # 领域事件:任务完成(resume 路径,时刻=agent1 结束)
        emit(
            TASK_COMPLETED, task.id,
            mode="resume", rounds=len(react_summaries),
        )
        perf_log(task.id, "agent1_done", rounds=len(react_summaries))

        # 推送 agent1_done:主界面收尾,总线保持打开供后台审查事件
        publish(task.id, "agent1_done", {"status": "completed"})

        # 捕获工作区 diff(agent2 只读审查不影响工作区;失败兜底)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")

        # 任务本体已完成:finally 不再兜底推 error(审查失败≠任务失败)
        normal_completed = True

        # 遗留用户消息自动续轮(执行期间最后迭代之后到达、未被 drain 的):
        # 挪到新轮并立即启动新一轮,与本轮后台审查并行(不等审查)——
        # 新流先注册 scope,审查收尾的 end-scope 看到活跃流不推 done
        _auto_resume_leftover_messages(task, db, task_id_str)

        # ---------- 后台审查(同一线程;失败只影响 review_status) ----------
        # 老审查(若未结束)与本流并行:各自落库自己轮次,done/finish 由
        # 最后活跃流的 _end_event_scope 统一收尾。
        # _run_background_review 永不抛异常(内部全兜底),无需 try 包裹
        _run_background_review(
            task, db,
            user_intent=effective_intent,
            react_summaries=react_summaries,
            scenario_id=task.scenario,
            llm_client=llm_client,
            agent_policy=agent_policy,
            task_id_str=task_id_str,
            react_client=react_client,
            flow_gen=flow_gen,
            round_idx=start_round_idx,
        )

    except Exception as e:
        err_stage = "重试执行失败" if retry else "重启执行失败"
        logger.exception(f"[task={task.id}] {err_stage}")
        # 错误详情增强:消息为空时补异常类型名,避免 UI 显示"未知错误"
        err_detail = _err_detail(e)
        task.status = TaskStatus.FAILED
        task.error_message = err_detail[:1000]
        task.current_stage = err_stage
        db.commit()
        _publish_status(task)
        # 领域事件:任务失败(resume/重试续跑 except 路径)
        emit(TASK_FAILED, task.id, error=err_detail[:500], stage=err_stage)
        _add_conversation(
            db, task, round_idx=0,
            role="agent2", type="error",
            content=f"{err_stage}: {err_detail}",
        )
        # 失败也尽量捕获:工作区 diff + 仓库树快照(失败兜底;沙箱通常仍存活,
        # 会话已死则自然返回 None,不影响失败处理)
        try:
            from app.services.workspace_diff import (
                save_repo_tree_artifact,
                save_workspace_diff_artifact,
            )
            save_workspace_diff_artifact(task, db, task_id_str)
            save_repo_tree_artifact(task, db, task_id_str)
        except Exception as diff_err:
            logger.warning(f"[task={task.id}] 失败时捕获工作区产物失败(忽略): {diff_err}")
        # 事件活跃期收尾移交 finally 统一处理:error 事件必须在
        # finish_task 之前推送(总线关闭后 publish 会被静默丢弃)
    finally:
        # 推送终止事件
        # done 事件已在各收尾点经 _end_event_scope 推送(正常路径)。
        # 此处仅兑底:异常路径(本轮未正常完成)推送 error 事件。
        # 判定必须用内存标志 normal_completed,不能用 task.status ——
        # 本轮完成后用户又发新追问会把状态改回 RUNNING,按状态判定会误推 error
        # 并重新标记总线结束,导致新 resume 线程事件全被静默丢弃。
        if not normal_completed:
            # [诊断] error 事件推送日志:前端 onError 的唯一事件源,全量记录
            logger.warning(
                f"[task={task.id}] resume finally 兑底推送 error 事件 "
                f"(status={task.status.value}, error_message={task.error_message!r})"
            )
            # error 无条件推送(并行流失败也须通知前端);必须在 end-scope 之前
            # (总线关闭后 publish 会被静默丢弃,前端将永远收不到错误横幅)
            publish(task.id, "error", {
                "status": "failed",
                "error_message": task.error_message or "未知错误(无异常详情,请查看服务日志)",
            })
        # 无条件收尾(幂等):正常路径已在各收尾点 end 过(discard 无副作用),
        # 此处兜底 ① 异常路径的 scope 注销 ② "正常完成后仍抛异常"的边角泄漏。
        # 若此时尚有并行流在跑则静默返回,不动总线
        _end_event_scope(task.id, flow_gen)
        # ---- 状态清理组:仅当本任务无活跃流时执行 ----
        # (老审查与本流并行时,不得清掉并行流正在用的运行时状态。
        #  必须在 end-scope 之后:异常路径下自身 scope 尚未注销,
        #  先查会被误判"并行中"而永久跳过清理)
        if not _has_active_scope(task.id):
            for cleanup_fn, name in [
                (clear_pause_state, "暂停状态"),
                (clear_skip_state, "跳过预克隆标志"),
                (clear_user_messages, "用户消息队列"),
                (clear_pending_verify_action, "验证待授权状态"),
                (clear_pending_command_confirm, "命令待确认状态"),
                (sandbox_tools.mark_task_completed, "沙箱完成标记"),
            ]:
                try:
                    cleanup_fn(task_id_str)
                except Exception as cleanup_err:
                    logger.warning(f"[task={task.id}] 清理{name}失败: {cleanup_err}")


# ============================================================
# 失败任务重试(断点续跑优先,无进度时从头重跑)
# ============================================================


def retry_failed_task(task: Task, db: Session) -> None:
    """失败任务重试入口:断点续跑优先,无可续进度时从头重跑

    判定依据:是否已有 agent2 / react_agent 的对话落库
    - 无(预克隆/沙箱/LLM 配置等早期失败,执行未真正开始):
      没有可续内容,直接重跑 run_dual_agent_audit
    - 有(执行中途失败):复用 resume_audit_with_message 断点续跑,
      以重试续跑消息直接驱动 agent1 基于已有进度接续未完成工作
      (round_idx 由 _get_next_round_idx 自动续接,不产生重复轮次)

    续跑前的沙箱探测:失败 finally 已 mark_task_completed,session
    超 1 小时 TTL 被回收(或后端重启)后已 clone 的仓库丢失;若 session
    不在且任务配了 repo_url,重新 clone 恢复工作区,避免续跑时执行器
    找不到仓库。

    注意:本函数由 API 端点在独立后台线程中调用(与 resume 一致),
    重试时原后台线程已因异常退出,互斥无竞态。
    """
    task_id_str = str(task.id)
    # 先捕获失败原因(后续会清 error_message),拼进续跑消息供 agent1 参考
    # (error_message 已由失败路径增强为非空,这里仅兜底旧数据)
    last_error = task.error_message or "未知错误(无异常详情)"
    perf_log(task.id, "retry_start", last_error_chars=len(last_error))

    has_progress = (
        db.query(Conversation.id)
        .filter(
            Conversation.task_id == task.id,
            Conversation.role.in_(("agent1", "agent2")),
        )
        .first()
        is not None
    )

    if not has_progress:
        # 早期失败:无可续内容,从头重跑(状态流转/清理由其内部处理)
        logger.info(f"[task={task.id}] 重试:无可续进度,从头重跑")
        task.error_message = None
        db.commit()
        run_dual_agent_audit(task, db)
        return

    # 执行中途失败:沙箱会话已被回收时,重新 clone 恢复工作区
    repo_url = (task.params or {}).get("repo_url")
    if repo_url and sandbox_tools.get_workspace_info(task_id_str) is None:
        logger.info(
            f"[task={task.id}] 重试:沙箱会话已回收,重新克隆仓库恢复工作区"
        )
        task.status = TaskStatus.RUNNING
        task.current_stage = "重试失败任务,正在恢复工作区..."
        task.error_message = None
        db.commit()
        _publish_status(task)
        git_tokens = _load_git_tokens(db, task.user_id)
        set_current_git_tokens(git_tokens)
        # clone 失败时 _prepare_repo_context 已降级不抛异常
        # (续跑时 react_agent 可自主克隆,不阻塞重试)
        _prepare_repo_context(task, db, task_id_str, git_tokens)

    # 以重试续跑消息恢复执行(状态流转/记忆文件重建/轮次续接由 resume 链路处理)
    retry_message = build_retry_message(last_error)
    resume_audit_with_message(task, db, retry_message, retry=True)


def _err_detail(e: Exception) -> str:
    """提取人类可读的错误详情:异常消息为空时补异常类型名,杜绝"未知错误"字面"""
    text = str(e)
    return text if text else f"{type(e).__name__}(无错误详情)"


def _finish_resume(
    task: Task, db: Session, react_summaries: list[dict],
    react_client: LLMClient | None = None, *, flow_gen: int, round_idx: int,
) -> None:
    """resume 收尾(单 agent 模式):标记状态 + 终止事件

    仅单 agent 模式(agent2 已禁用)使用:无 agent2 评估可展示,
    不写总结对话。该路径结果已由调用方落库(_replace_interim_results),
    后续后台审查路径不走此函数(由 _run_background_review 负责收尾)。
    done/finish 经 _end_event_scope 统一判定(并行流在跑时不关总线)。
    round_idx = 本流 agent1 执行轮(预压缩轮次,显式传入防 len 推导错位)。
    """
    task.status = TaskStatus.COMPLETED
    task.current_stage = "重启执行完成"
    task.completed_at = datetime.now(timezone.utc)
    db.commit()
    _publish_status(task)
    # 领域事件:任务完成(resume/重试续跑路径)
    emit(TASK_COMPLETED, task.id, mode="resume", rounds=len(react_summaries))

    # 遗留用户消息自动续轮(先于终止 end-scope:有遗留时新流注册 scope,
    # 下方 end-scope 看到活跃流不推 done,SSE 不断线)
    _auto_resume_leftover_messages(task, db, str(task.id))
    # 事件活跃期收尾:推 done + 关总线(仅当无并行流)
    _end_event_scope(task.id, flow_gen, ("done", {"status": "completed"}))

    # 预压缩早期历史:用户下一轮追问直接命中缓存(失败兜底)
    _precompress_history_safely(task, db, round_idx, react_client)

    # 重启完成:自动归纳写入长期记忆(失败兑底,不影响;模型/结构走用户「记忆设置」独立解析)
    try:
        from app.services.memory_summarize import summarize_and_save_memory
        summarize_and_save_memory(task, db)
    except Exception as mem_err:
        logger.warning(f"[task={task.id}] 归纳写入记忆失败(忽略): {mem_err}")

    # 捕获工作区 diff(失败兜底,不影响任务完成;容器仍存活)
    try:
        from app.services.workspace_diff import (
            save_repo_tree_artifact,
            save_workspace_diff_artifact,
        )
        save_workspace_diff_artifact(task, db, str(task.id))
        # 树快照:更新为最终态(含新建文件),供不可用时兜底展示
        save_repo_tree_artifact(task, db, str(task.id))
    except Exception as diff_err:
        logger.warning(f"[task={task.id}] 捕获工作区 diff 失败(忽略): {diff_err}")


def _load_react_summaries(db: Session, task_id) -> list[dict]:
    """从 Conversation 表加载历史 react_agent 总结(供 agent2 评估)

    按 round_idx 升序,取每个 round 的最后一条 thinking content 作为该轮 summary。
    (react_agent 内部把每轮最终思考落库为 type=thinking,content 即为 summary)
    """
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "agent1",
            Conversation.type == "thinking",
        )
        .order_by(Conversation.round_idx.asc(), Conversation.created_at.asc())
        .all()
    )
    summaries_by_round: dict[int, str] = {}
    for c in convs:
        if c.content:
            summaries_by_round[c.round_idx] = c.content
    return [
        {"round": r, "summary": s}
        for r, s in sorted(summaries_by_round.items())
    ]


def _get_next_round_idx(db: Session, task_id, retry: bool = False) -> int:
    """resume 起始轮计算:

    - 用户追加消息(retry=False):消息已由 API 端点落库到最新轮
      (round = max(Conversation.round_idx)),此处复用该轮号——
      分析评估与首轮 react 执行与用户消息同轮,消息位于轮首,不隔轮
    - 失败重试(retry=True):无新用户消息落库,从 max+1 续接新轮

    无对话记录时返回 1(理论上不会发生,因为已完成的任务一定有对话)。
    """
    latest = (
        db.query(Conversation)
        .filter(Conversation.task_id == task_id)
        .order_by(Conversation.round_idx.desc())
        .first()
    )
    if not latest:
        return 1
    return latest.round_idx if not retry else latest.round_idx + 1
