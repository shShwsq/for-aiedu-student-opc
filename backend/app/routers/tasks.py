"""任务路由

阶段 4:双智能体协作(agent2 + react_agent)
阶段 7:异步化 + SSE 实时流

端点:
- GET /tasks  列出当前用户可见的任务(自己 + 匿名)
- POST /tasks  提交任务,立即返回 task_id,后台线程执行
- GET /tasks/{task_id}  查询任务状态与结果
- GET /tasks/{task_id}/stream  SSE 实时事件流(对话/状态/结果/完成)
- POST /tasks/{task_id}/retry  重试失败任务(断点续跑优先)
- GET /scenarios  列出可用场景
"""
import html
import json
import logging
import os
import threading
import uuid
from collections.abc import Generator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agents.orchestrator import (
    _err_detail,
    force_cleanup_event_scopes,
    launch_resume_thread,
    resume_audit_with_message,
    retry_failed_task,
    run_dual_agent_audit,
)
from app.database import SessionLocal, get_db
from app.deps import get_optional_user, get_optional_user_sse
from app.event_bus import (
    is_task_finished,
    publish,
    reset_task_bus,
    subscribe,
    unsubscribe,
)
from app.models.task import Conversation, Result, Task, TaskStatus
from app.models.task_artifact import TaskArtifact
from app.clone_skip import clear_skip_state, request_skip_clone
from app.prompts.executor import build_first_round_question
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.pause_controller import (
    clear_pause_state,
    pause_task,
    resume_task,
)
from app.perf import perf_log
from app.review_stop import (
    clear_review_stop_state,
    request_stop as request_review_stop,
)
from app.scenarios.base import list_scenarios
from app.schemas.task import (
    ScenarioInfo,
    SendMessageRequest,
    CommandConfirmRequest,
    CommandConfirmResponse,
    MessageWithdrawResponse,
    SendMessageResponse,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskListItem,
    TaskResponse,
    TaskTitleUpdateRequest,
    VerifyActionRequest,
    VerifyActionResponse,
    VerifyConfigUpdateRequest,
    RuntimeConfigUpdateRequest,
)
from app.schemas.task_artifact import TaskArtifactOut
from app.config import settings
from app.services.uploads import (
    UploadError,
    load_upload_meta,
    validate_upload_for_task,
)
from app.tools import sandbox_tools
from app.user_interaction import (
    clear_pending_verify_action,
    get_pending_command_confirm,
    get_pending_verify_action,
    submit_command_confirm,
    submit_verify_authorization,
)
from app.user_messages import push_user_message, remove_user_message

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"])


@router.get("/scenarios", response_model=list[ScenarioInfo])
def list_all_scenarios() -> list[dict[str, Any]]:
    """列出所有可用场景(含前端声明:表单字段/结果分组/meta字段/覆盖度看板)"""
    return list_scenarios()


@router.get("/tasks", response_model=list[TaskListItem])
def list_tasks(
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> list[Task]:
    """列出当前用户可见的任务(自己的 + 匿名的),按创建时间倒序

    用于侧栏历史任务列表。精简字段(不含对话/结果),降低传输成本。

    可选 q 参数:全文搜索(大小写不敏感)。匹配范围:
    - 任务标题(title)
    - 用户输入(user_input)
    - 对话内容(conversation.content / reasoning)
    - 结果内容(result.title / content)
    命中任一字段即返回该任务。
    """
    query = db.query(Task)
    if current_user:
        # 登录用户:返回自己的任务 + 匿名任务
        query = query.filter(
            (Task.user_id == current_user.id) | (Task.user_id.is_(None))
        )
    else:
        # 未登录:只返回匿名任务
        query = query.filter(Task.user_id.is_(None))

    # 全文搜索:用 ILIKE 做大小写不敏感匹配,通过 EXISTS 子查询避免 JOIN 产生重复行
    keyword = (q or "").strip()
    if keyword:
        kw = f"%{keyword}%"
        conv_match = select(Conversation.id).where(
            Conversation.task_id == Task.id,
            or_(
                Conversation.content.ilike(kw),
                Conversation.reasoning.ilike(kw),
            ),
        )
        result_match = select(Result.id).where(
            Result.task_id == Task.id,
            or_(
                Result.title.ilike(kw),
                Result.content.ilike(kw),
            ),
        )
        query = query.filter(
            or_(
                Task.title.ilike(kw),
                Task.user_input.ilike(kw),
                conv_match.exists(),
                result_match.exists(),
            )
        )

    return (
        query.order_by(Task.created_at.desc())
        .offset(max(0, offset))
        .limit(min(max(1, limit), 100))
        .all()
    )


@router.post("/tasks", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    req: TaskCreateRequest,
    current_user: User | None = Depends(get_optional_user),
) -> TaskCreateResponse:
    """提交任务

    阶段 7:异步化。立即创建 task 记录并返回 task_id,
    后台线程执行双智能体协作。前端通过 SSE 端点实时观看进度。

    支持两种提交方式:
    - 通用:scenario + user_input + params
    - 兼容旧 API:传 repo_url,自动转成 user_input + params
    """
    user_input, params = _normalize_request(req)

    # 上传交付物:创建前校验存在 + 归属 + 数量上限,快速失败(不落任务直接报错)
    creation_upload_ids = _merged_creation_upload_ids(req)
    if creation_upload_ids:
        if len(creation_upload_ids) > settings.UPLOAD_MAX_FILES_PER_MESSAGE:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"单个任务最多关联 {settings.UPLOAD_MAX_FILES_PER_MESSAGE} 个上传文件",
            )
        for uid in creation_upload_ids:
            try:
                validate_upload_for_task(
                    uid, current_user.id if current_user else None
                )
            except UploadError as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
                )

    # 用独立 session 创建 task(不依赖请求级 session,因为要立即返回)
    db = SessionLocal()
    try:
        # 标题:trim 后为空则存 None(前端按 None 回退到 user_input 截断展示)
        raw_title = (req.title or "").strip()
        task = Task(
            scenario=req.scenario,
            title=raw_title or None,
            user_input=user_input,
            params=params,
            user_id=current_user.id if current_user else None,
            llm_config_id=req.llm_config_id,
            react_llm_config_id=req.react_llm_config_id,
            executor=req.executor,
            status=TaskStatus.PENDING,
            current_stage="已提交,等待执行",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
        task_status = task.status

        # 用户提问即时落库(role=user, type=question, round_idx=1):
        # 保证前端首屏 getTask 快照即含用户提问,无需等后台线程完成预 clone、
        # react_agent 启动后才显示(此前问题气泡会晚于助手回答出现)。
        # react_agent / acp_base 首轮按 (task, round_idx, user, question) 幂等去重,
        # 不会重复落库。此处不 publish SSE:前端 getTask 先于 connectSSE,提问已
        # 在快照中;若再进总线历史会与快照重复(onConversation 无按 id 去重)。
        db.add(Conversation(
            task_id=task_id,
            round_idx=1,
            role="user",
            type="question",
            content=build_first_round_question(user_input, params),
        ))
        db.commit()
    finally:
        db.close()

    # 启动后台线程执行(用独立 DB session,线程安全)
    thread = threading.Thread(
        target=_run_task_in_background,
        args=(str(task_id),),
        daemon=True,
        name=f"task-{task_id}",
    )
    thread.start()

    return TaskCreateResponse(id=task_id, status=task_status)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Task:
    """查询任务详情,包含对话记录与结果

    阶段 6:若任务关联了 user_id,则只允许该用户访问;匿名任务任何人可访问
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 权限:任务归属用户或匿名任务可访问
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    # 过滤内部缓存记录(type=history_compress 是 LLM 压缩摘要,不展示给用户)
    task_resp = TaskResponse.model_validate(task)
    task_resp.conversations = [
        c for c in task_resp.conversations if c.type != "history_compress"
    ]
    return task_resp


# ============================================================
# 任务工作区产物(diff/patch)
# ============================================================


@router.get("/tasks/{task_id}/artifacts")
def list_task_artifacts(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """列出任务的工作区产物(diff/patch 等)

    任务完成时捕获的 git diff,持久化在 task_artifacts 表。
    鉴权:任务归属用户或匿名任务可访问(与 get_task 一致)。按 created_at 升序返回。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    artifacts = (
        db.query(TaskArtifact)
        .filter(TaskArtifact.task_id == task_id)
        .order_by(TaskArtifact.created_at.asc())
        .all()
    )
    return {"artifacts": [TaskArtifactOut.model_validate(a) for a in artifacts]}


@router.get("/tasks/{task_id}/artifacts/{artifact_id}", response_model=TaskArtifactOut)
def get_task_artifact(
    task_id: uuid.UUID,
    artifact_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> TaskArtifactOut:
    """查询单个工作区产物(含完整 content)

    鉴权:任务归属用户或匿名任务可访问(与 get_task 一致)。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    artifact = (
        db.query(TaskArtifact)
        .filter(
            TaskArtifact.id == artifact_id,
            TaskArtifact.task_id == task_id,
        )
        .first()
    )
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    return TaskArtifactOut.model_validate(artifact)


# ============================================================
# 验证器动作授权(verifier_agent per_action 模式:每个 HTTP/PoC 动作阻塞等用户确认)
# ============================================================


@router.get("/tasks/{task_id}/pending_verify_action")
def get_task_pending_verify_action(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any] | None:
    """查询任务当前待授权的验证动作(刷新页面后恢复弹窗用)

    无待授权动作返回 None。有则返回动作描述(action_id/type/method/url/code 等)。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    # 任务已结束,不恢复弹窗
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return None

    return get_pending_verify_action(task_id)


@router.post("/tasks/{task_id}/verify_action", response_model=VerifyActionResponse)
def submit_task_verify_action(
    task_id: uuid.UUID,
    req: VerifyActionRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> VerifyActionResponse:
    """提交用户对验证动作的授权决议(per_action 模式)

    唤醒阻塞等待的 verifier_agent 后台线程:
    - approved=true:继续执行该 HTTP/PoC 动作
    - approved=false:跳过该动作,verifier_agent 收到"用户拒绝"反馈

    返回 accepted=false 表示当前无待授权动作(可能已答复或任务已结束)。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
        return VerifyActionResponse(
            accepted=False,
            message=f"任务已结束({task.status.value}),无法提交授权",
        )

    ok = submit_verify_authorization(task_id, req.action_id, req.approved)
    if not ok:
        return VerifyActionResponse(
            accepted=False,
            message="当前没有待授权的验证动作(可能已答复或任务已结束)",
        )
    return VerifyActionResponse(
        accepted=True,
        message=f"已{'同意' if req.approved else '拒绝'}该验证动作",
    )


@router.get("/tasks/{task_id}/pending_command_confirm")
def get_task_pending_command_confirm(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any] | None:
    """查询任务当前待确认的危险命令(刷新页面后恢复弹窗用)

    无待确认命令返回 None。有则返回命令描述(command_id/command/tool/reason)。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return None

    return get_pending_command_confirm(task_id)


@router.post("/tasks/{task_id}/command_confirm", response_model=CommandConfirmResponse)
def submit_task_command_confirm(
    task_id: uuid.UUID,
    req: CommandConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> CommandConfirmResponse:
    """提交用户对危险命令的确认决议

    唤醒阻塞等待的后台线程:
    - approved=true:继续执行该命令
    - approved=false:跳过该命令,LLM 收到"用户拒绝"反馈

    返回 accepted=false 表示当前无待确认命令(可能已答复或任务已结束)。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
        return CommandConfirmResponse(
            accepted=False,
            message=f"任务已结束({task.status.value}),无法提交确认",
        )

    ok = submit_command_confirm(task_id, req.command_id, req.approved)
    if not ok:
        return CommandConfirmResponse(
            accepted=False,
            message="当前没有待确认的命令(可能已答复或任务已结束)",
        )
    return CommandConfirmResponse(
        accepted=True,
        message=f"已{'同意' if req.approved else '拒绝'}执行该命令",
    )


@router.patch("/tasks/{task_id}/verifier_config", response_model=TaskResponse)
def update_task_verifier_config(
    task_id: uuid.UUID,
    req: VerifyConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Task:
    """更新任务的验证器配置(运行时可调)

    允许在任务运行界面调整验证授权模式(direct/per_action)与开关。
    配置存储在 task.params._verifier,verifier_agent 每次调用时读取最新值。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    params = dict(task.params or {})
    verifier_cfg = dict(params.get("_verifier") or {})

    if req.verifier_enabled is not None:
        verifier_cfg["enabled"] = req.verifier_enabled
    if req.verifier_auth_mode is not None:
        verifier_cfg["auth_mode"] = req.verifier_auth_mode
    if req.test_env_url is not None:
        verifier_cfg["test_env_url"] = req.test_env_url
    # 登录凭证:None=不修改,空列表=清空,非空=整体覆盖
    if req.verifier_auth_tokens is not None:
        verifier_cfg["auth_tokens"] = [t.model_dump() for t in req.verifier_auth_tokens]

    # 若 enabled=false 或 test_env_url 为空,清除 _verifier 配置(禁用验证)
    if not verifier_cfg.get("enabled") or not verifier_cfg.get("test_env_url"):
        params.pop("_verifier", None)
    else:
        params["_verifier"] = verifier_cfg

    task.params = params
    db.commit()
    db.refresh(task)
    return task


@router.patch("/tasks/{task_id}/runtime_config", response_model=TaskResponse)
def update_task_runtime_config(
    task_id: uuid.UUID,
    req: RuntimeConfigUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Task:
    """更新任务运行时配置(模型选择)

    生效时机:running/paused 的当前执行线程使用启动时加载的配置,
    修改在下一轮执行(completed 后追加消息重启 / failed 重试)时生效。

    - 模型 id 需存在于任务归属用户的 LLM 配置列表,否则 400
    - react_llm_config_id 仅 executor=builtin 时可改(CLI 执行器模型自管)
    - 智能体策略项(max_rounds)已随后台审查重构移除,此处只更新模型配置
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    def _validate_config_id(config_id: str, label: str) -> None:
        """校验模型配置 id 属于任务归属用户(匿名任务不允许指定模型)"""
        if task.user_id is None:
            raise HTTPException(status_code=400, detail="匿名任务不支持指定模型配置")
        cfg = (
            db.query(UserLLMConfig)
            .filter(UserLLMConfig.user_id == task.user_id)
            .first()
        )
        ids = {c.get("id") for c in (cfg.llm_configs if cfg else [])}
        if config_id not in ids:
            raise HTTPException(status_code=400, detail=f"{label}不存在或不属于当前用户")

    if req.llm_config_id is not None:
        if req.llm_config_id:
            _validate_config_id(req.llm_config_id, "agent2 模型配置")
        task.llm_config_id = req.llm_config_id or None

    if req.react_llm_config_id is not None:
        if task.executor != "builtin":
            raise HTTPException(
                status_code=400,
                detail="当前任务使用外部 CLI 执行器,react 模型由 CLI 自管,不可修改",
            )
        if req.react_llm_config_id:
            _validate_config_id(req.react_llm_config_id, "react_agent 模型配置")
        task.react_llm_config_id = req.react_llm_config_id or None

    db.commit()
    db.refresh(task)
    logger.info(
        f"[task={task.id}] 运行时配置已更新: llm={task.llm_config_id}, "
        f"react_llm={task.react_llm_config_id}"
    )
    return task


# ============================================================
# 用户补充消息(对话界面下方输入框)
# ============================================================


@router.post("/tasks/{task_id}/messages", response_model=SendMessageResponse)
def submit_task_message(
    task_id: uuid.UUID,
    req: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> SendMessageResponse:
    """用户在对话界面下方输入框发送的补充消息

    按 task.status 分发:
    - running / paused:消息入队(user_messages.push_user_message),
      react_agent 在下一迭代边界 drain 出来注入 LLM 上下文
    - completed:立即启动新一轮执行(追问直达 agent1,不等老审查)——
      老审查(若仍在跑)与新轮 agent1 并行,各自落库自己轮次的知识点;
      done/finish 由最后活跃流统一收尾(orchestrator 事件活跃期机制)
    - pending / failed:拒绝(任务未启动或已失败)

    消息统一落库为 Conversation(role=user, type=message):
    - 运行中/暂停中:round_idx = 当前最大 round(react_agent 下一迭代边界注入);
      推送 user_message_pending 事件(输入框上方待处理条目,TRAE 式),
      消费时刻由 react_agent 补推 conversation 事件进入对话流
    - 完成后:round_idx = 当前最大 round + 1(归入即将开始的新轮,
      与 resume 的分析评估/首轮 react 执行共享轮号,消息位于轮首与回应连续展示);
      立即推送 conversation 事件进入对话流
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=422, detail="消息内容不能为空")

    # [perf] 用户发消息锚点(与 resume_start / llm_ttft / acp_first_event 配对算端到端延迟)
    perf_log(task_id, "user_message", status=task.status.value, msg_chars=len(content))

    # 拒绝 pending / failed 状态
    if task.status in (TaskStatus.PENDING, TaskStatus.FAILED):
        return SendMessageResponse(
            accepted=False,
            message=f"任务状态为 {task.status.value},无法接收消息",
        )

    # 追问附件:校验归属 + 数量上限,并构造落库展示信息(失败即拒绝,不落库)。
    # 复用 validate_upload_for_task 返回的 meta 构造 attachments,免二次读取。
    followup_ids: list[str] = []
    for uid in (req.upload_ids or []):
        if uid and uid not in followup_ids:
            followup_ids.append(uid)
    attachments: list[dict] = []
    if followup_ids:
        if len(followup_ids) > settings.UPLOAD_MAX_FILES_PER_MESSAGE:
            raise HTTPException(
                status_code=422,
                detail=f"单条消息最多附带 {settings.UPLOAD_MAX_FILES_PER_MESSAGE} 个文件",
            )
        for uid in followup_ids:
            try:
                meta = validate_upload_for_task(
                    uid, current_user.id if current_user else None
                )
            except UploadError as e:
                raise HTTPException(status_code=422, detail=str(e))
            attachments.append({
                "upload_id": uid,
                "filename": meta.get("filename") or "上传文件",
                "size": meta.get("size") or 0,
                "kind": meta.get("kind") or "file",
            })

    # 用户消息归 round:
    # - 运行中/暂停中:归当前 round(react_agent 迭代边界注入,即时介入)
    # - 完成后:归 max+1 的新轮(该轮即 resume 的分析评估 + 首轮 react 执行,
    #   消息位于轮首,与 agent2 的分析回应连续展示)
    latest_conv = (
        db.query(Conversation)
        .filter(Conversation.task_id == task_id)
        .order_by(Conversation.round_idx.desc())
        .first()
    )
    if task.status == TaskStatus.COMPLETED:
        msg_round_idx = (latest_conv.round_idx + 1) if latest_conv else 1
    else:
        msg_round_idx = latest_conv.round_idx if latest_conv else 0

    # 同步落库(确保刷新时数据库已有记录)
    conv = Conversation(
        task_id=task.id,
        round_idx=msg_round_idx,
        role="user",
        type="message",
        content=content,
        attachments=attachments or None,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    def _publish_user_message(event_type: str = "conversation") -> None:
        """推送用户消息事件(conversation=入流;user_message_pending=待处理)"""
        publish(task.id, event_type, {
            "id": str(conv.id),
            "round_idx": conv.round_idx,
            "role": conv.role,
            "type": conv.type,
            "content": conv.content,
            "reasoning": conv.reasoning,
            "attachments": conv.attachments,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        })

    def _accumulate_followup_uploads() -> None:
        """追问附件累积进 params.followup_upload_ids(非 JSONB 突变)"""
        if not followup_ids:
            return
        new_params = dict(task.params or {})
        existing = list(new_params.get("followup_upload_ids") or [])
        for uid in followup_ids:
            if uid not in existing:
                existing.append(uid)
        new_params["followup_upload_ids"] = existing
        task.params = new_params

    # 状态分发
    if task.status in (TaskStatus.RUNNING, TaskStatus.PAUSED):
        # 待处理事件(不入对话流):消息以 pending 状态展示在输入框上方
        # (TRAE 式),agent1 消费(drain)时才由 react_agent 推 conversation
        # 事件进入对话流 —— 避免插在 agent1 执行中的对话中间
        _publish_user_message(event_type="user_message_pending")
        # 入队,react_agent 下一迭代 drain
        push_user_message(
            task.id, content,
            message_id=str(conv.id),
            created_at=conv.created_at.isoformat() if conv.created_at else "",
            upload_ids=followup_ids or None,
        )
        return SendMessageResponse(
            accepted=True,
            message="消息已加入队列,智能体将在下一迭代处理",
        )

    if task.status == TaskStatus.COMPLETED:
        # 追问直达 agent1(不等老审查):老审查(若仍在跑)与新轮 agent1
        # 并行,各自落库自己轮次的知识点;done/finish 由最后活跃流收尾
        # (orchestrator._end_event_scope)。
        # 总线:上一轮已完全收尾(is_task_finished,含历史 done 事件)→
        # 重置,让新事件能推送、前端重连 SSE 不会因历史 done 立即关闭;
        # 老审查仍在跑(总线打开)→ 不重置,SSE 不断线、历史事件保留。
        if is_task_finished(task.id):
            reset_task_bus(task.id)
        _publish_user_message()

        # 同步将状态改为 RUNNING 落库后再启动后台线程:
        # ① 消除 SSE 端点快照读到 COMPLETED 的竞态窗口(否则前端重连时,
        #   stream_task_events 按旧快照直接推 done 关闭连接,需刷新才恢复)
        # ② 关闭双发竞态窗口:并发第二条消息看到 RUNNING → 走运行中入队,
        #   由新轮 agent1 在迭代边界消费(resume_audit_with_message 开头
        #   会再设置一次状态,幂等无冲突)
        task.status = TaskStatus.RUNNING
        task.current_stage = "用户追加消息,重启执行"
        _accumulate_followup_uploads()
        db.commit()

        # 启动新的协作 round(后台线程;scope 在线程启动前同步注册,
        # 老审查收尾不会误推 done 关总线)
        launch_resume_thread(str(task_id), content, upload_ids=followup_ids or None)
        return SendMessageResponse(
            accepted=True,
            message="已启动新一轮执行",
        )

    # 兜底(理论上不会到这,前面已覆盖所有可接收状态)
    return SendMessageResponse(
        accepted=False,
        message=f"任务状态 {task.status.value} 不支持发送消息",
    )


# ============================================================
# 撤回待处理消息(运行中发送、尚未被消费的补充消息)
# ============================================================


@router.delete("/tasks/{task_id}/messages/{message_id}", response_model=MessageWithdrawResponse)
def withdraw_task_message(
    task_id: uuid.UUID,
    message_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> MessageWithdrawResponse:
    """撤回运行中发送、尚未被 agent1 消费的待处理消息(TRAE 式)

    仅 running/paused 状态支持(此时消息在 in-memory 队列等待消费):
    - 仍在队列 → 移除 + 删除 Conversation 记录 + 推 user_message_withdrawn
      事件(前端移除待处理条目,多端同步;事件入总线历史,刷新后补播一致)
    - 已被消费(drain 过)→ 拒绝:消息已进入 agent 上下文,无法撤回
    - 附件说明:入队消息的附件在消费时才传输进沙箱,撤回无需清理工作区
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status not in (TaskStatus.RUNNING, TaskStatus.PAUSED):
        return MessageWithdrawResponse(
            success=False,
            message=f"任务状态 {task.status.value} 无待处理消息,不支持撤回",
        )

    removed = remove_user_message(task.id, str(message_id))
    if not removed:
        # 不在队列:已被消费,或记录不存在
        conv = db.get(Conversation, message_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        return MessageWithdrawResponse(
            success=False,
            message="消息已被智能体处理,无法撤回",
        )

    # 仍在队列:删除落库记录(对话流/刷新快照不再显示)+ 通知前端
    db.query(Conversation).filter(
        Conversation.id == message_id,
        Conversation.task_id == task.id,
    ).delete(synchronize_session=False)
    db.commit()
    publish(task.id, "user_message_withdrawn", {"id": str(message_id)})
    return MessageWithdrawResponse(success=True, message="消息已撤回")


# ============================================================
# 失败任务重试
# ============================================================


@router.post("/tasks/{task_id}/retry", response_model=SendMessageResponse)
def retry_failed_task_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> SendMessageResponse:
    """重试失败的任务(断点续跑优先)

    仅 failed 状态接受(其他状态返回 accepted=false,天然防重复点击)。
    后台线程调 retry_failed_task 按进度分流:
    - 无可续进度(早期失败):从头重跑 run_dual_agent_audit
    - 有进度(执行中途失败):复用 resume 链路断点续跑

    与 send_message 的 completed 分支同理,需先 reset_task_bus:
    失败任务的事件总线已被 finish_task 标记结束,后续 publish 会被静默丢弃。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status != TaskStatus.FAILED:
        # [诊断] 重试拒绝日志:与前端 client.log 的"点击重试"记录对拍,
        # 可定位"前端显示 failed 但后端 running"的状态不一致
        logger.info(
            f"[retry] task={task_id} 重试被拒:当前状态={task.status.value}"
            f"(仅 failed 可重试),error_message={task.error_message!r}"
        )
        return SendMessageResponse(
            accepted=False,
            message=f"任务状态为 {task.status.value},不支持重试",
        )

    perf_log(task_id, "user_retry")

    # 重置事件总线(清除 _finished + 旧历史),让新事件能推送、前端重连 SSE 可接收
    reset_task_bus(task.id)

    # 重试标记对话落库(对话流可见重试起点),role=system 不被重试进度判定计入
    latest_conv = (
        db.query(Conversation)
        .filter(Conversation.task_id == task_id)
        .order_by(Conversation.round_idx.desc())
        .first()
    )
    conv = Conversation(
        task_id=task.id,
        round_idx=latest_conv.round_idx if latest_conv else 0,
        role="system",
        type="info",
        content="发起失败任务重试(优先断点续跑)...",
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    })

    # 同步将状态改为 RUNNING 落库后再启动后台线程:
    # 与 completed 发消息同理,消除 SSE 端点快照读到 FAILED 的竞态窗口
    # (前端重连 SSE 时 stream_task_events 会按旧快照直接推 error 关闭连接)。
    # 注意:不清空 error_message —— retry_failed_task 需在进入时读取真实
    # 失败原因拼进续跑消息,清空由后台线程内部分流处理。
    task.status = TaskStatus.RUNNING
    task.current_stage = "重试失败任务,恢复执行"
    db.commit()

    thread = threading.Thread(
        target=_run_retry_in_background,
        args=(str(task_id),),
        daemon=True,
        name=f"task-{task_id}-retry",
    )
    thread.start()
    return SendMessageResponse(
        accepted=True,
        message="已开始重试",
    )


def _run_retry_in_background(task_id: str) -> None:
    """后台线程执行失败任务重试(与 _run_resume_in_background 对齐)

    用独立的 DB session(线程安全),执行完毕后关闭。
    retry_failed_task 内部从头重跑/断点续跑分支各自有异常兜底,
    这里仅兜底 DB 异常等极端情况。
    """
    db = SessionLocal()
    try:
        task = db.get(Task, uuid.UUID(task_id))
        if not task:
            logger.error(f"重试任务:task {task_id} 不存在")
            return
        retry_failed_task(task, db)
    except Exception as e:
        logger.exception(f"[task={task_id}] 重试后台执行失败")
        # 兜底:确保 task 状态被标记为失败
        try:
            task = db.get(Task, uuid.UUID(task_id))
            if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.status = TaskStatus.FAILED
                task.error_message = str(e)[:1000]
                task.current_stage = "重试执行失败"
                db.commit()
                # 兜底推送终止事件:防止 SSE 订阅者因线程在进入
                # resume 主 try 块前崩溃收不到 error 而永久挂起
                publish(task.id, "error", {
                    "status": "failed",
                    "error_message": str(e)[:1000],
                })
        except Exception:
            pass
        # 强制清空事件活跃期:retry 链路可能在崩溃前注册了 scope
        # (resume 前置段异常上抛)——泄漏会让该任务之后所有流被误判
        # "并行中"(永不推 done、SSE 永久悬挂)。先推 error 后清理。
        # retry 场景无并行流,强制清空安全(见函数注释)
        force_cleanup_event_scopes(task_id)
    finally:
        db.close()


# ============================================================
# 客户端诊断日志上报(前后端日志对拍)
# ============================================================


class ClientLogRequest(BaseModel):
    """前端诊断日志上报:定位"前端显示失败但后端 running"等状态不一致"""

    task_id: str = ""
    event: str = ""
    detail: dict[str, Any] | None = None
    ts: str = ""


@router.post("/debug/client-log")
def client_log_endpoint(
    req: ClientLogRequest,
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """接收前端诊断日志,追加写入 backend/logs/client.log(JSON 行)

    前端 fire-and-forget 上报,失败静默不阻塞业务。
    与后端 event_bus / SSE 埋点日志按时间 + task_id 对拍,
    可还原"未知失败"出现时的完整事件序列。
    """
    try:
        detail = dict(req.detail or {})
        # 截断过大的字段,防止日志膨胀(thinking 流式内容等)
        for k, v in detail.items():
            if isinstance(v, str) and len(v) > 200:
                detail[k] = v[:200] + f"...(截断,共{len(v)}字符)"
        line = json.dumps(
            {
                "ts": req.ts or datetime.now().isoformat(),
                "task_id": req.task_id,
                "event": req.event,
                "detail": detail,
                "user": str(current_user.id) if current_user else None,
            },
            ensure_ascii=False,
        )
        logs_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
        )
        os.makedirs(logs_dir, exist_ok=True)
        with open(
            os.path.join(logs_dir, "client.log"), "a", encoding="utf-8"
        ) as f:
            f.write(line + "\n")
    except Exception as e:
        logger.warning(f"客户端日志写入失败(忽略): {e}")
    return {"ok": True}


# ============================================================
# 任务暂停/恢复
# ============================================================


@router.post("/tasks/{task_id}/pause")
def pause_task_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """暂停正在运行的任务

    后台线程会在下一个检查点(迭代边界/工具调用前)阻塞。
    立即把 task.status 改为 PAUSED 并推送 status 事件,
    前端据此把"暂停"按钮变成"恢复"按钮。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status != TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status.value},仅 RUNNING 可暂停",
        )

    # 标记 in-memory 暂停门控(后台线程下一次检查时会阻塞)
    pause_task(task.id)
    # 持久化状态变更 + 推送事件(前端立即看到 UI 切换)
    task.status = TaskStatus.PAUSED
    task.current_stage = "已暂停(等待恢复)"
    db.commit()
    _publish_task_status(task)
    return {"status": task.status.value, "message": "任务已暂停"}


@router.post("/tasks/{task_id}/resume")
def resume_task_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """恢复已暂停的任务

    唤醒在检查点阻塞的后台线程,task.status 改回 RUNNING。
    current_stage 恢复到暂停前的描述不现实(已覆盖),改为通用提示。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status != TaskStatus.PAUSED:
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status.value},仅 PAUSED 可恢复",
        )

    # 唤醒后台线程(若已阻塞在 wait_if_paused)
    resume_task(task.id)
    task.status = TaskStatus.RUNNING
    task.current_stage = "已恢复,继续执行"
    db.commit()
    _publish_task_status(task)
    return {"status": task.status.value, "message": "任务已恢复"}


@router.post("/tasks/{task_id}/review/stop")
def stop_task_review_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """终止 agent2 后台检查(仅审查进行中可用)

    有些对话不需要检查:agent2 动辄数分钟的只读核查/PoC/引用复核可以直接停。
    不能用 /pause 走这条路:暂停只接受 RUNNING,而后台审查发生在任务已
    COMPLETED 之后;且把审查线程挂在检查点上只会白占 DB session 与事件活跃期。

    语义:按轮登记终止标志后立即返回,审查线程在下一个检查点(LLM 流 chunk
    边界 / 工具循环边界)协作式收尾,写 review_status=stopped + 推 review_done;
    本轮知识点不会被写入,保留 agent1 的执行结果。

    例外:没有审查线程在跑(后端重启后的遗留"检查中"角标)时无人会来收尾,
    就地写终态并推事件,否则前端永远卡在"检查中"。

    并发:落笔前复核一次 review_status —— 终止请求若落在审查收尾之后,不覆盖
    刚写入的 done/failed,而是把真实终态回给前端(它会据此拉快照收角标)。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.review_status != "running":
        raise HTTPException(
            status_code=409,
            detail=(
                f"检查状态为 {task.review_status or '未开始'},"
                "仅检查进行中可终止"
            ),
        )

    stopped_round = request_review_stop(task.id)
    # 写之前复核终态:上面的"仅 running 可终止"检查到这里之间存在窗口 —— 审查
    # 线程可能刚提交 done/failed(甚至已注销登记,使 stopped_round 为 None)。
    # 此时写 stopped 会把刚完成的审查成果标成"已终止"(知识点已落库、临时结果
    # 标题已替换),写 current_stage 则留下永不推进的"正在终止检查..."。
    # 直查列而不 db.refresh(task):行若已被删除只会拿到 None,不会抛异常。
    fresh_status = db.query(Task.review_status).filter(Task.id == task.id).scalar()
    if fresh_status != "running":
        logger.info(
            f"[task={task_id}] 终止请求落在审查收尾之后"
            f"(review_status={fresh_status or '未开始'}),不覆盖终态"
        )
        return {
            "review_status": fresh_status or "not_started",
            "message": "检查已结束,无需终止",
        }

    if stopped_round is None:
        # 无审查线程持有本轮:端点代它收尾(角标不能永久卡住)
        task.review_status = "stopped"
        task.current_stage = "任务完成(检查已终止,保留执行结果)"
        db.commit()
        _publish_task_status(task)
        publish(task.id, "review_done", {"review_status": "stopped"})
        logger.info(f"[task={task_id}] 无在跑审查,终止请求就地收尾")
        return {"review_status": "stopped", "message": "检查已终止"}

    # 标志已落:终态由审查线程写(它才持有本轮 round_idx 与事件活跃期),
    # 先把阶段文案改一下给前端即时反馈(终态随 review_done 到达)
    task.current_stage = "正在终止检查..."
    db.commit()
    _publish_task_status(task)
    logger.info(f"[task={task_id}] 已提交终止检查请求(round={stopped_round})")
    return {
        "review_status": "running",
        "message": "已提交终止请求,检查将在下一个检查点停止",
    }


@router.post("/tasks/{task_id}/skip_pre_clone")
def skip_pre_clone_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """请求跳过预克隆(仅运行中/暂停态任务有效)

    设置一次性跳过标志,克隆轮询循环在下一个检查点终止当前 clone,
    orchestrator 降级为 react_agent 自主克隆(与预克隆失败降级同路径)。
    幂等:重复请求无副作用;若点击时克隆恰好完成,标志不会被消费,
    任务结束时兜底清理。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status not in (TaskStatus.RUNNING, TaskStatus.PAUSED):
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status.value},仅运行中/暂停态可跳过预克隆",
        )

    request_skip_clone(task.id)

    # 暂停态:克隆循环阻塞在 wait_if_paused,检测不到跳过标志;
    # 先唤醒并把状态改回 RUNNING(用户意图是不再等待继续执行)
    if task.status == TaskStatus.PAUSED:
        resume_task(task.id)
        task.status = TaskStatus.RUNNING
        task.current_stage = "已恢复,正在跳过预克隆..."
        db.commit()
        _publish_task_status(task)
    return {"message": "已提交跳过请求"}


def _publish_task_status(task: Task) -> None:
    """推送任务状态变更事件(供 pause/resume 端点复用)"""
    publish(task.id, "status", {
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "current_stage": task.current_stage,
    })


# ============================================================
# 任务标题修改 / 任务删除
# ============================================================


@router.patch("/tasks/{task_id}/title", response_model=TaskResponse)
def update_task_title(
    task_id: uuid.UUID,
    req: TaskTitleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Task:
    """修改任务标题

    title 为空字符串(trim 后)等价于清除自定义标题,前端回退到 user_input 截断展示。
    权限:与查看一致,匿名任务任何人可改,归属任务仅 owner 可改。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    new_title = req.title.strip()
    task.title = new_title or None
    db.commit()
    db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Response:
    """删除任务

    级联删除 conversations / results(数据库层 ondelete=CASCADE)。
    同时清理 in-memory 暂停状态 + 沙箱 session(若存在),避免资源泄漏。

    注意:运行中的任务被删除时,后台线程可能在下次写库时报错并被自身 try/except 兜底,
    不会影响进程稳定性。前端可在此后引导用户离开详情页。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    # 先清理 in-memory 资源(暂停门控 + 跳过标志 + 审查终止标志 + 沙箱 session),
    # 再删数据库记录
    clear_pause_state(str(task_id))
    clear_skip_state(str(task_id))
    clear_review_stop_state(str(task_id))
    try:
        sandbox_tools.close_session(str(task_id))
    except Exception as e:
        logger.warning(f"[task={task_id}] 删除任务时关闭沙箱失败: {e}")

    db.delete(task)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _get_result_display_config(
    task: Task, results: list,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """获取结果展示配置(场景降级后:grouping 从 task.params._grouping 读取,
    meta_fields 从 results 的 metadata keys 动态推断)

    返回:(grouping, meta_fields)
      - grouping: agent2 done 时声明的分组配置,无则 None(平铺)
      - meta_fields: 从所有 results 的 metadata keys 汇总,每个 key 作为一个展示字段
    """
    # grouping:agent2 done 时存到 task.params["_grouping"]
    params = task.params or {}
    grouping = params.get("_grouping") if isinstance(params, dict) else None

    # meta_fields:从 results 的 metadata keys 动态推断
    # 收集所有 result 的 metadata keys(保留出现顺序)
    # practice_worthy 为布尔出题标记,报告无展示价值,跳过
    seen_keys: list[str] = []
    for r in results:
        meta = r.metadata_ or {}
        if isinstance(meta, dict):
            for k in meta.keys():
                if k not in seen_keys and not k.startswith("_") and k != "practice_worthy":
                    seen_keys.append(k)
    # file_path 类型的 key 标记为 file(可点击跳转),其余为 text
    meta_fields = []
    for k in seen_keys:
        field_type = "file" if k in ("file_path", "path", "file") else "text"
        meta_fields.append({"name": k, "label": k, "type": field_type})

    return grouping, meta_fields


# ============================================================
# 报告导出(Markdown / HTML)
# ============================================================


@router.get("/tasks/{task_id}/export")
def export_task_report(
    task_id: uuid.UUID,
    format: str = "markdown",
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Response:
    """导出任务报告

    format=markdown:返回 .md 附件下载
    format=html:返回打印友好的完整 HTML(前端用于新窗口打印为 PDF)
    """
    if format not in ("markdown", "html"):
        raise HTTPException(status_code=400, detail="format 仅支持 markdown / html")

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    if format == "markdown":
        body = _build_markdown_report(task, db)
        return Response(
            content=body.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="task-{task.id}.md"',
            },
        )
    # html
    body = _build_html_report(task, db)
    return Response(content=body.encode("utf-8"), media_type="text/html; charset=utf-8")


def _build_markdown_report(
    task: Task, db: Session,
) -> str:
    """生成 Markdown 报告:任务信息 + 重点与知识点(按 grouping 分组)+ 详细对话

    不设独立「用户意图」节:报告按轮次呈现多轮对话,用户每轮发言都在其中。
    """
    lines: list[str] = []
    lines.append("# 任务报告")
    lines.append("")
    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
    lines.append(f"- 任务 ID: `{task.id}`")
    lines.append(f"- 场景: {task.scenario}")
    lines.append(f"- 状态: {status_val}")
    lines.append(f"- 创建时间: {task.created_at}")
    if task.completed_at:
        lines.append(f"- 完成时间: {task.completed_at}")
    if task.current_stage:
        lines.append(f"- 当前阶段: {task.current_stage}")
    if task.error_message:
        lines.append(f"- 错误信息: {task.error_message}")
    lines.append("")

    # 重点与知识点(场景降级后:grouping 从 task.params._grouping 读取,
    # meta_fields 从 results 的 metadata keys 动态推断)
    results = list(task.results)
    if results:
        grouping, meta_fields = _get_result_display_config(task, results)
        lines.append("## 重点与知识点")
        lines.append("")
        if grouping:
            _append_grouped_results_md(lines, results, grouping, meta_fields)
        else:
            for r in results:
                _append_result_md(lines, r, meta_fields)

    # 详细对话(按轮组织的结论类对话,含 agent2 每轮审查结论/建议追问;
    # 跳过 thinking/tool_call/tool_result/history_compress 等过程类)
    trace = _collect_conversation_trace(task)
    if trace:
        lines.append("## 详细对话")
        lines.append("")
        _append_conversation_trace_md(lines, trace)

    return "\n".join(lines)


def _append_grouped_results_md(
    lines: list[str],
    results: list,
    grouping: dict[str, Any],
    meta_fields: list[dict[str, Any]],
) -> None:
    """按场景声明 result_grouping 分组追加结果到 Markdown"""
    buckets: dict[str, list] = {}
    field = grouping.get("field")
    for r in results:
        md = r.metadata_ or {}
        val = md.get(field) if field else None
        key = val if val else "__default__"
        buckets.setdefault(key, []).append(r)

    # type 默认 dynamic(LLM 输出可能不带 type/default_label,见 agent2 prompt 示例)
    default_label = grouping.get("default_label", "其他")
    if grouping.get("type") == "ordered":
        for v in sorted(grouping.get("values", []), key=lambda x: x.get("order", 0)):
            rs = buckets.get(v["value"], [])
            if rs:
                lines.append(f"### {v.get('label', v.get('value', '?'))} ({len(rs)})")
                lines.append("")
                for r in rs:
                    _append_result_md(lines, r, meta_fields)
        default_rs = buckets.get("__default__", [])
        if default_rs:
            lines.append(f"### {default_label} ({len(default_rs)})")
            lines.append("")
            for r in default_rs:
                _append_result_md(lines, r, meta_fields)
    else:
        for key, rs in buckets.items():
            label = default_label if key == "__default__" else key
            lines.append(f"### {label} ({len(rs)})")
            lines.append("")
            for r in rs:
                _append_result_md(lines, r, meta_fields)


def _append_result_md(
    lines: list[str], r, meta_fields: list[dict[str, Any]]
) -> None:
    """追加单个结果到 Markdown"""
    lines.append(f"#### {r.title}")
    lines.append("")
    lines.append(r.content or "(无内容)")
    lines.append("")
    md = r.metadata_ or {}
    if meta_fields and md:
        parts = []
        for f in meta_fields:
            v = md.get(f["name"])
            if v:
                parts.append(f"{f['label']}: {v}")
        if parts:
            lines.append("> " + " | ".join(parts))
            lines.append("")
    lines.append(f"_第 {r.round_idx} 轮产出_")
    lines.append("")


def _build_html_report(
    task: Task, db: Session,
) -> str:
    """生成打印友好的 HTML 报告(前端新窗口打印为 PDF)"""
    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append(f"<title>任务报告 - {task.id}</title>")
    parts.append("<style>")
    parts.append(
        "body{font-family:-apple-system,'Segoe UI',sans-serif;max-width:900px;"
        "margin:32px auto;padding:0 24px;color:#1a1a1a;line-height:1.6;}"
        "h1{font-size:24px;border-bottom:2px solid #2563eb;padding-bottom:8px;}"
        "h2{font-size:18px;margin-top:32px;border-left:4px solid #2563eb;padding-left:8px;}"
        "h3{font-size:15px;margin-top:20px;color:#374151;}"
        "h4{font-size:14px;margin:12px 0 4px;}"
        ".meta{color:#6b7280;font-size:13px;}"
        ".meta div{margin:2px 0;}"
        ".intent{background:#f9fafb;padding:12px 16px;border-radius:6px;"
        "white-space:pre-wrap;font-size:14px;}"
        ".result{margin:12px 0;padding:12px 16px;background:#fafafa;border-radius:6px;break-inside:avoid;}"
        ".result h4{margin:0 0 6px;}"
        ".result .rmeta{font-size:12px;color:#6b7280;margin:6px 0;}"
        ".result .round{font-size:11px;color:#9ca3af;}"
        ".conv{margin:10px 0;padding:10px 14px;background:#fafafa;border-radius:6px;"
        "border-left:3px solid #d1d5db;break-inside:avoid;}"
        ".conv.round-header{background:transparent;border-left:none;padding:4px 0;"
        "font-weight:600;color:#374151;}"
        ".conv-tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;"
        "border-radius:10px;margin-right:8px;color:#fff;background:#6b7280;}"
        ".conv-tag.question{background:#2563eb;}"
        ".conv-tag.answer{background:#0ea5e9;}"
        ".conv-tag.message{background:#0284c7;}"
        ".conv-tag.evaluation{background:#7c3aed;}"
        ".conv-tag.followup{background:#0891b2;}"
        ".conv-tag.submit{background:#16a34a;}"
        ".conv-tag.summary{background:#d97706;}"
        ".conv-tag.review{background:#7c3aed;}"
        ".conv-tag.suggestions{background:#0891b2;}"
        ".conv-tag.error{background:#dc2626;}"
        ".conv-role{font-size:12px;color:#6b7280;}"
        ".conv-content{margin-top:6px;white-space:pre-wrap;font-size:13px;}"
        "@media print{body{margin:0;max-width:none;}}"
    )
    parts.append("</style></head><body>")

    parts.append("<h1>任务报告</h1>")
    parts.append('<div class="meta">')
    parts.append(f"<div>任务 ID:<code>{task.id}</code></div>")
    parts.append(f"<div>场景:{html.escape(task.scenario)}</div>")
    parts.append(f"<div>状态:{html.escape(status_val)}</div>")
    parts.append(f"<div>创建时间:{task.created_at}</div>")
    if task.completed_at:
        parts.append(f"<div>完成时间:{task.completed_at}</div>")
    if task.current_stage:
        parts.append(f"<div>当前阶段:{html.escape(task.current_stage)}</div>")
    if task.error_message:
        parts.append(f"<div>错误信息:{html.escape(task.error_message)}</div>")
    parts.append("</div>")

    # 重点与知识点
    results = list(task.results)
    if results:
        grouping, meta_fields = _get_result_display_config(task, results)
        parts.append("<h2>重点与知识点</h2>")
        if grouping:
            _append_grouped_results_html(parts, results, grouping, meta_fields)
        else:
            for r in results:
                _append_result_html(parts, r, meta_fields)

    # 详细对话(按轮组织的结论类对话,含 agent2 每轮审查结论/建议追问)
    trace = _collect_conversation_trace(task)
    if trace:
        parts.append("<h2>详细对话</h2>")
        _append_conversation_trace_html(parts, trace)

    parts.append("</body></html>")
    return "\n".join(parts)


def _append_grouped_results_html(
    parts: list[str],
    results: list,
    grouping: dict[str, Any],
    meta_fields: list[dict[str, Any]],
) -> None:
    """按场景声明 result_grouping 分组追加结果到 HTML"""
    buckets: dict[str, list] = {}
    field = grouping.get("field")
    for r in results:
        md = r.metadata_ or {}
        val = md.get(field) if field else None
        key = val if val else "__default__"
        buckets.setdefault(key, []).append(r)

    def _emit_group(label: str, rs: list) -> None:
        parts.append(f"<h3>{html.escape(label)} ({len(rs)})</h3>")
        for r in rs:
            _append_result_html(parts, r, meta_fields)

    # type 默认 dynamic(LLM 输出可能不带 type/default_label,见 agent2 prompt 示例)
    default_label = grouping.get("default_label", "其他")
    if grouping.get("type") == "ordered":
        for v in sorted(grouping.get("values", []), key=lambda x: x.get("order", 0)):
            rs = buckets.get(v["value"], [])
            if rs:
                _emit_group(v.get("label", v.get("value", "?")), rs)
        default_rs = buckets.get("__default__", [])
        if default_rs:
            _emit_group(default_label, default_rs)
    else:
        for key, rs in buckets.items():
            label = default_label if key == "__default__" else key
            _emit_group(label, rs)


def _append_result_html(
    parts: list[str], r, meta_fields: list[dict[str, Any]]
) -> None:
    """追加单个结果到 HTML"""
    parts.append('<div class="result">')
    parts.append(f"<h4>{html.escape(r.title or '(无标题)')}</h4>")
    parts.append(f"<div>{html.escape(r.content or '(无内容)')}</div>")
    md = r.metadata_ or {}
    if meta_fields and md:
        mp = []
        for f in meta_fields:
            v = md.get(f["name"])
            if v:
                mp.append(f"{html.escape(f['label'])}: {html.escape(str(v))}")
        if mp:
            parts.append(f'<div class="rmeta">{" | ".join(mp)}</div>')
    parts.append(f'<div class="round">第 {r.round_idx} 轮产出</div>')
    parts.append("</div>")


# ============================================================
# 详细对话(报告导出用:按轮组织的结论类对话)
# ============================================================
#
# 与前端任务详情主对话流对齐:只摘「结论类」对话,跳过思考 / 工具调用 /
# history_compress 等过程性内容。
#
# 跳过规则(参考 frontend/src/views/TaskDetailView.vue 主对话流过滤):
# 1. thinking / tool_call / tool_result / history_compress —— 过程类,体积大
#    (thinking 含 reasoning_content 思考链,可能几 KB~几十 KB,塞进报告会让
#    .md / PDF 体积爆炸,浏览器打印会卡死)
#
# 结论类消息保留每轮脉络:用户发言 → agent1 提交结果 → agent2 审查/总结,
# 读者无需展开每个工具调用细节即可按轮通读重建协作脉络。
#
# 补充特殊处理:
# 2. agent1 每轮总结无独立落库类型,约定为该轮最后一条
#    role=agent1 type=thinking 的 content(与 orchestrator 落库约定一致),
#    报告侧按此约定合成「提交结果」条目
# 3. 存量数据里追问轮 question 可能整段落库了拼进提示词的
#    "[之前轮次的对话记忆]" 块(新数据已在落库侧拆分),报告侧裁剪兼容历史任务

_CONVERSATION_TRACE_TYPES = {
    "question",    # 用户某轮发言(首轮以 question 落库,后续轮为 message)
    "submit",      # agent1 提交结果(按轮从 thinking 合成)
    "review",      # agent2 后台审查结论(reasoning 存完整 covered/missing/判断)
    "suggestions", # agent2 建议追问方向(JSON,报告渲染为列表)
    "summary",     # agent2 最终总结
    "message",     # 用户追加消息(前端主对话流右对齐展示)
    "error",       # 错误(关键失败原因,属于结论而非过程)
}

# 跨轮历史记忆注入块标记(旧版 react_agent 把历史块拼进 question 落库)。
# 现行实现历史以结构化 messages 注入(不落库为 question),此标记仅用于
# 裁掉历史存量数据里追问轮 question 混入的记忆块
_HISTORY_MEMORY_MARKER = "[之前轮次的对话记忆]"

# 追问轮指令段标签(新旧兼容):记忆块之后紧跟的追问段起点,
# 裁剪记忆块时需保留该段(它是本轮真实指令)
_FOLLOWUP_SECTION_LABELS = (
    "[本轮补充要求]", "[本轮补充检查要求]",
    "[本轮 agent2 追问]", "[本轮追问]",
)

_CONVERSATION_TYPE_LABELS = {
    "question": "用户提问",
    "submit": "提交结果",
    "review": "审查结论",
    "suggestions": "建议追问方向",
    "summary": "总结",
    "message": "用户消息",
    "error": "错误",
}


def _strip_question_memory_block(content: str) -> str:
    """裁掉 question 里混入的跨轮历史记忆块(存量数据兼容)

    新数据已在 react_agent 落库侧拆分(历史记忆块只进发送内容不落库),
    此函数仅用于兼容修改前的历史任务数据。存量 content 结构:
    头部指令 + "[之前轮次的对话记忆]..." + 追问段(新旧标签见
    _FOLLOWUP_SECTION_LABELS),只移除中间的记忆块,保留追问部分
    (它是本轮真实指令)。
    """
    if not content:
        return content
    idx = content.find(_HISTORY_MEMORY_MARKER)
    if idx < 0:
        return content
    followup_idx = -1
    for label in _FOLLOWUP_SECTION_LABELS:
        followup_idx = content.find(label, idx)
        if followup_idx >= 0:
            break
    head = content[:idx].rstrip()
    if followup_idx < 0:
        # 没找到追问段标记,记忆块延伸到末尾,直接截断
        return head or content
    tail = content[followup_idx:]
    return f"{head}\n\n{tail}" if head else tail


def _format_suggestions(raw: str) -> str:
    """agent2 建议追问卡落库为 JSON {"suggestions": [...]},渲染为可读列表

    解析失败(老数据/异常形态)退回原文,避免丢信息。
    """
    try:
        payload = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        payload = None
    items = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return raw or "(无建议)"
    lines = [f"- {str(s).strip()}" for s in items if str(s).strip()]
    return "\n".join(lines) or "(无建议)"


def _trace_content(c) -> str:
    """结论类对话的展示正文(按 type 归一化)

    - question:裁掉存量数据里的跨轮历史记忆块
    - review:reasoning 存完整审查正文(covered/missing/判断),优先用它
    - suggestions:JSON 渲染为列表
    - 其他:content 原文
    """
    if c.type == "question":
        return _strip_question_memory_block(c.content)
    if c.type == "review":
        return (c.reasoning or c.content or "").strip() or "(无内容)"
    if c.type == "suggestions":
        return _format_suggestions(c.content)
    return c.content or "(无内容)"


def _collect_react_summaries(task: Task) -> list[dict[str, Any]]:
    """按轮提取 agent1 每轮最终总结,合成「提交结果」条目

    约定:agent1 每轮最终总结落库为该轮最后一条 role=agent1 type=thinking
    的 content,无独立 submit 类型。报告白名单跳过 thinking,故在此按约定合成,
    保证详细对话里能看到 agent1 每轮的结果。
    """
    last_by_round: dict[int, Any] = {}
    for c in task.conversations:
        if (
            c.role == "agent1"
            and c.type == "thinking"
            and c.content
        ):
            # task.conversations 顺序不保证,按 created_at 取每轮最后一条
            prev = last_by_round.get(c.round_idx)
            if prev is None or (
                c.created_at
                and (prev.created_at is None or c.created_at >= prev.created_at)
            ):
                last_by_round[c.round_idx] = c
    return [
        {
            "round_idx": c.round_idx,
            "role": "agent1",
            "type": "submit",
            "type_label": _CONVERSATION_TYPE_LABELS["submit"],
            "content": c.content,
            "created_at": c.created_at,
        }
        for _, c in sorted(last_by_round.items())
    ]


def _collect_conversation_trace(task: Task) -> list[dict[str, Any]]:
    """收集详细对话(仅结论类消息,按 round_idx + created_at 排序)

    返回结构:[{round_idx, role, type, type_label, content, created_at}, ...]
    依赖 task.conversations relationship(同一 session 内触发 lazy load)。

    报告按轮次呈现「多轮对话」:每轮 = 用户发言 + agent1 提交结果 + agent2
    审查结论/总结,顺时间序通读即可重建协作脉络,无须区分「原始意图」与追问。

    过滤规则(与前端任务详情主对话流对齐):
    - 仅保留 _CONVERSATION_TRACE_TYPES 中的类型
      (thinking/tool_call/tool_result/history_compress 不在白名单,天然跳过)
    - agent1 每轮总结按约定合成「提交结果」条目并入
    - question 内容裁掉存量数据里的历史记忆块
    - 追问轮去重:同轮已有干净的 user message(用户原话)时,该轮的编排
      样板 question(react/acp 注入的"基于之前的执行进度…仓库路径…[本轮补充要求]"
      包装体,与 message 内容重复且易误导)跳过,仅保留 message
    """
    convs = [
        c for c in task.conversations
        if c.type in _CONVERSATION_TRACE_TYPES
    ]
    submits = _collect_react_summaries(task)

    # 有干净 user message 的轮:该轮的编排样板 question 与之重复,跳过
    # (首轮只有 question、无 message,它就是本轮的用户发言,正常保留)
    message_rounds = {
        c.round_idx for c in convs
        if c.role == "user" and c.type == "message"
    }

    items: list[dict[str, Any]] = []
    for c in convs:
        if (
            c.role == "user" and c.type == "question"
            and c.round_idx in message_rounds
        ):
            continue  # 追问轮:编排样板 question 与干净 message 重复,保留 message
        items.append({
            "round_idx": c.round_idx,
            "role": c.role,
            "type": c.type,
            "type_label": _CONVERSATION_TYPE_LABELS.get(c.type, c.type),
            "content": _trace_content(c),
            "created_at": c.created_at,
        })
    items.extend(submits)
    items.sort(key=lambda it: (it["round_idx"], it["created_at"]))
    return items


def _append_conversation_trace_md(
    lines: list[str], trace: list[dict[str, Any]]
) -> None:
    """按协作轮次分组追加结论类对话到 Markdown"""
    cur_round: int | None = None
    for item in trace:
        if item["round_idx"] != cur_round:
            cur_round = item["round_idx"]
            lines.append(f"### 第 {cur_round} 轮")
            lines.append("")
        lines.append(f"**[{item['type_label']}] {item['role']}**")
        lines.append("")
        lines.append(item["content"] or "(无内容)")
        lines.append("")


def _append_conversation_trace_html(
    parts: list[str], trace: list[dict[str, Any]]
) -> None:
    """按协作轮次分组追加结论类对话到 HTML"""
    cur_round: int | None = None
    for item in trace:
        if item["round_idx"] != cur_round:
            cur_round = item["round_idx"]
            parts.append(
                f'<div class="conv round-header">第 {cur_round} 轮</div>'
            )
        parts.append(
            f'<div class="conv">'
            f'<span class="conv-tag {item["type"]}">'
            f'{html.escape(item["type_label"])}</span>'
            f'<span class="conv-role">{html.escape(item["role"])}</span>'
            f'<div class="conv-content">'
            f'{html.escape(item["content"] or "(无内容)")}'
            f'</div></div>'
        )


def _should_force_close_stream(
    initial_status: TaskStatus, task_id_str: str,
) -> bool:
    """SSE 连接建立时是否应直接推终止事件关闭连接

    快照为 COMPLETED/FAILED 且事件总线已标记结束 → 任务确实结束,关闭。
    快照为 COMPLETED/FAILED 但总线未标记结束 → resume/retry 已启动
    (API 端点 reset_task_bus 清除了标记),只是后台线程尚未更新 DB 状态,
    属时序竞态:若按快照关闭,前端刚重连的 SSE 会被误杀,后续
    conversation/status 等事件虽进历史缓存却无人接收(需刷新页面才恢复)。
    """
    if initial_status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return False
    return is_task_finished(task_id_str)


@router.get("/tasks/{task_id}/stream")
def stream_task_events(
    task_id: uuid.UUID,
    request: Request,
    current_user: User | None = Depends(get_optional_user_sse),
) -> StreamingResponse:
    """SSE 端点:实时推送任务事件

    事件类型:
    - conversation: 新对话消息(agent2/react_agent 的每一步)
    - status: 任务状态变更(进入新阶段)
    - thinking_delta: LLM 流式 token 增量(打字机效果)
    - done: 任务完成(终止事件)
    - error: 任务失败(终止事件)

    前端用 EventSource 连接,每条事件 data 字段是 JSON。

    注意:不用 Depends(get_db) —— 其会话要等流式响应完全结束才释放,
    而 SSE 连接贯穿整个任务生命周期(可达数十分钟),长占连接会耗尽
    连接池(pool_size=5 + overflow=10),导致其它请求(切路由加载列表等)
    全部排队卡死。这里改用短会话:鉴权/取状态快照后立即关闭。
    """
    # 鉴权 + 任务存在性检查(短会话,取完快照立即归还连接)
    db = SessionLocal()
    try:
        task = db.get(Task, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.user_id is not None:
            if current_user is None or current_user.id != task.user_id:
                raise HTTPException(status_code=403, detail="无权访问此任务")
        initial_status = task.status
        initial_stage = task.current_stage
    finally:
        db.close()

    task_id_str = str(task_id)
    # [诊断] SSE 连接建立日志:记录快照状态与总线结束标记,
    # 与 event_bus 订阅日志 / 前端 client.log 对拍
    logger.info(
        f"[sse] task={task_id_str} 连接建立 initial_status={initial_status.value} "
        f"initial_stage={initial_stage!r} is_task_finished={is_task_finished(task_id_str)}"
    )

    def event_generator() -> Generator[str, None, None]:
        """SSE 事件生成器(不碰数据库,只用上面的状态快照)"""
        q = subscribe(task_id_str)
        try:
            # 先推送一个 connected 事件(带当前状态,前端可据此判断是否已结束)
            connected_event = {
                "type": "connected",
                "task_id": task_id_str,
                "data": {
                    "status": initial_status.value if hasattr(initial_status, 'value') else str(initial_status),
                    "current_stage": initial_stage,
                },
                "timestamp": "",
            }
            yield _format_sse(connected_event)

            # 若任务已结束,直接推一个终止事件然后关闭
            # (竞态防御见 _should_force_close_stream:resume/retry 已启动时
            # 不按旧快照关闭,走正常订阅分支,历史缓存会补播已错过的事件)
            if _should_force_close_stream(initial_status, task_id_str):
                done_event = {
                    "type": "done" if initial_status == TaskStatus.COMPLETED else "error",
                    "task_id": task_id_str,
                    "data": {"status": initial_status.value},
                    "timestamp": "",
                }
                # [诊断] 快照终止事件:记录推了 done 还是 error(前端 onError 的唯一外部来源)
                logger.info(
                    f"[sse] task={task_id_str} 按旧快照推送终止事件 "
                    f"type={done_event['type']}(initial_status={initial_status.value})"
                )
                yield _format_sse(done_event)
                return

            # 阻塞读事件队列,直到收到 done/error
            while True:
                # 检查客户端是否断开
                if await_request_disconnect(request):
                    logger.info(f"[task={task_id_str}] SSE 客户端断开")
                    break

                try:
                    # 超时 15 秒无事件,发心跳保持连接
                    event = q.get(timeout=15)
                except Exception:
                    # queue.Empty 是正常的,发心跳
                    yield ": heartbeat\n\n"
                    continue

                yield _format_sse(event)

                # 终止事件:结束循环
                if event.get("type") in ("done", "error"):
                    break
        finally:
            unsubscribe(task_id_str, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx:禁用缓冲,确保实时推送
        },
    )


# ============================================================
# 后台任务执行(独立线程 + 独立 DB session)
# ============================================================


def _run_task_in_background(task_id: str) -> None:
    """后台线程执行双智能体协作

    用独立的 DB session(线程安全),执行完毕后关闭。
    """
    db = SessionLocal()
    try:
        task = db.get(Task, uuid.UUID(task_id))
        if not task:
            logger.error(f"后台任务:task {task_id} 不存在")
            return
        run_dual_agent_audit(task, db)
    except Exception as e:
        logger.exception(f"[task={task_id}] 后台执行失败")
        # 兜底:确保 task 状态被标记为失败
        try:
            task = db.get(Task, uuid.UUID(task_id))
            if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.status = TaskStatus.FAILED
                task.error_message = _err_detail(e)[:1000]
                task.current_stage = "执行失败"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ============================================================
# 辅助函数
# ============================================================


def _format_sse(event: dict) -> str:
    """格式化为 SSE 事件字符串

    格式:
    event: <type>
    data: <json>

    """
    event_type = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def await_request_disconnect(request: Request) -> bool:
    """检查请求是否已断开(客户端关闭连接)

    FastAPI/Starlette 的 Request.is_disconnected() 是 async 方法,
    但我们在同步生成器里,用 anyio.from_thread 桥接。
    """
    try:
        import anyio
        return anyio.from_thread.run(request.is_disconnected)
    except Exception:
        return False


def _merged_creation_upload_ids(req: TaskCreateRequest) -> list[str]:
    """合并 legacy 单数 upload_id + 多文件 upload_ids,去重保序

    旧客户端只传 upload_id;新客户端传 upload_ids;二者可共存(合并)。
    供 create_task 校验与 _normalize_request 写 params 复用,保证两处一致。
    """
    ids: list[str] = []
    if req.upload_id:
        ids.append(req.upload_id)
    for x in (req.upload_ids or []):
        if x and x not in ids:
            ids.append(x)
    return ids


def _normalize_request(req: TaskCreateRequest) -> tuple[str, dict | None]:
    """把请求归一化为 (user_input, params)

    - 通用方式:直接用 scenario + user_input + params
    - 兼容旧 API:传了 repo_url 但没传 user_input,自动生成
    - 验证器配置(test_env_url/verifier_enabled/verifier_auth_mode)存入 params._verifier
    """
    params = None
    user_input = None

    # 交付物来源互斥:Git 仓库与上传文件不可同时指定
    if (req.upload_id or req.upload_ids) and (
        req.repo_url or (req.params or {}).get("repo_url")
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="交付物来源只能二选一:Git 仓库或上传文件",
        )

    if req.user_input:
        # 通用方式:直接用
        user_input = req.user_input
        params = req.params
        # 若同时传了 repo_url 等,合并到 params
        if req.repo_url:
            params = dict(params or {})
            params["repo_url"] = str(req.repo_url)
            if req.branch:
                params["branch"] = req.branch
            if req.scope:
                params["scope"] = req.scope
    elif req.repo_url:
        # 兼容旧 API:只有 repo_url,生成通用 user_input(场景无关)
        user_input = f"请处理这个仓库: {req.repo_url}"
        params = dict(req.params or {})
        params["repo_url"] = str(req.repo_url)
        if req.branch:
            params["branch"] = req.branch
        if req.scope:
            params["scope"] = req.scope
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="必须提供 user_input 或 repo_url",
        )

    # 上传交付物:合并 legacy upload_id + upload_ids 写入 params.upload_ids,
    # orchestrator._creation_upload_ids 据此走上传分支(不 clone);旧任务读单数兼容
    creation_upload_ids = _merged_creation_upload_ids(req)
    if creation_upload_ids:
        params = dict(params or {})
        params["upload_ids"] = creation_upload_ids

    # 验证器配置存入 params._verifier(免迁移;Task 模型通过 @property 读取)
    if req.verifier_enabled and req.test_env_url:
        params = dict(params or {})
        params["_verifier"] = {
            "test_env_url": req.test_env_url,
            "enabled": True,
            "auth_mode": req.verifier_auth_mode,
            # 登录凭证:序列化为 plain dict 存入 params(避免 SQLAlchemy JSON 列存 Pydantic 模型)
            "auth_tokens": [t.model_dump() for t in req.verifier_auth_tokens],
        }

    return user_input, params
