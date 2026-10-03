"""练习模块路由:题目生成 / 确认 / 组卷 / 答题 / 统计 / 题库管理

鉴权:全部 Depends(get_current_user),数据 per-user 隔离。
题目来源为审计任务的真实发现(Result),经 LLM 改编为客观题;
选题策略见 services/practice/selector.py(SM-2 到期复习 + 薄弱点 + 难度匹配)。
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from collections.abc import Generator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, cast, func as sa_func
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.deps import get_current_user, get_current_user_sse
from app.models.practice import (
    Attempt,
    KnowledgePoint,
    PracticeSession,
    Question,
    QuestionStatus,
    UserKnowledgeState,
)
from app.models.task import Result, Task
from app.models.user import User
from app.schemas.practice import (
    ActivateQuestionsRequest,
    ActivateQuestionsResponse,
    ClearRecordsResponse,
    ConfirmQuestionsRequest,
    ConfirmQuestionsResponse,
    DraftQuestionResponse,
    GenerateJobResponse,
    GenerateJobsResponse,
    GenerateJobStatusResponse,
    GenerateJobSummary,
    GenerateModelResponse,
    GenerateRequest,
    KnowledgeStateResponse,
    KnowledgePointCardItem,
    PracticeSummaryResponse,
    QuestionDetailResponse,
    QuestionListItem,
    SessionAttemptItem,
    SessionDetailResponse,
    SessionListItem,
    SessionQuestionResponse,
    StartSessionRequest,
    StartSessionResponse,
    StatsResponse,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
    TrendPoint,
    TrendResponse,
    WeakPointItem,
)
from app.services.practice import jobs as gen_jobs
from app.services.practice.difficulty import (
    ABILITY_WINDOW,
    adjust_question_difficulty,
    estimate_ability,
)
from app.services.practice.generator import (
    PracticeGenerateError,
    generate_questions_for_task,
    resolve_generate_model_info,
)
from app.services.practice.selector import (
    WEAKNESS_ERROR_RATE,
    WEAKNESS_MIN_ATTEMPTS,
    CandidateInfo,
    select_questions,
)
from app.services.practice.sm2 import (
    SM2State,
    apply_sm2,
    quality_from_correct,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/practice", tags=["practice"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_task_owned(db: Session, task_id: UUID, user_id: UUID) -> Task:
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在或无权访问")
    return task


def _estimate_user_ability(db: Session, user_id: UUID) -> float:
    """由最近答对题目的难度估计能力值(升序传入 estimate_ability)"""
    rows = (
        db.query(Question.difficulty)
        .join(Attempt, Attempt.question_id == Question.id)
        .filter(Attempt.user_id == user_id, Attempt.is_correct.is_(True))
        .order_by(Attempt.answered_at.desc())
        .limit(ABILITY_WINDOW)
        .all()
    )
    return estimate_ability([d for (d,) in reversed(rows)])


# ============================================================
# 题目生成 / 确认
# ============================================================


@router.get("/tasks/{task_id}/generate-model", response_model=GenerateModelResponse)
def get_task_generate_model(
    task_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GenerateModelResponse:
    """任务详情页展示「本次出题将使用」的模型(与真实出题同一解析逻辑)

    解析优先级与 POST /practice/generate 完全一致:任务自带配置 >
    练习设置默认出题模型 > env 默认;「始终用默认出题模型」开启时跳过任务级。
    前端在出题入口附近展示,避免用户困惑为什么没用默认模型。
    """
    task = _get_task_owned(db, task_id, current_user.id)
    info = resolve_generate_model_info(db, task)
    return GenerateModelResponse(**info)


@router.post("/generate", response_model=GenerateJobResponse)
def generate_questions(
    req: GenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GenerateJobResponse:
    """从审计任务的真实发现生成候选题(draft,异步)

    立即返回 job_id,后台线程逐条 finding 调 LLM 出题,
    前端轮询 GET /practice/generate/{job_id} 拿进度与结果。
    """
    task = _get_task_owned(db, req.task_id, current_user.id)
    result_count = (
        db.query(sa_func.count(Result.id))
        .filter(Result.task_id == task.id)
        .scalar()
    )
    if not result_count:
        raise HTTPException(status_code=400, detail="该任务还没有审计发现,无法生成练习题")

    job_id = gen_jobs.create_job(
        current_user.id,
        source="manual",
        task_id=str(task.id),
        task_title=task.title or task.user_input[:60],
    )
    gen_jobs.set_total(job_id, total=min(result_count, req.max_findings))
    thread = threading.Thread(
        target=_run_generate_job,
        args=(job_id, task.id, current_user.id, req.max_findings),
        daemon=True,
        name=f"practice-generate-{job_id[:8]}",
    )
    thread.start()
    return GenerateJobResponse(job_id=job_id)


def _run_generate_job(job_id: str, task_id: UUID, user_id: UUID, max_findings: int) -> None:
    """后台线程:独立 Session 执行生成,进度/结果写回 job,流式事件写事件日志"""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(
            Task.id == task_id, Task.user_id == user_id
        ).first()
        if not task:
            gen_jobs.update_job(job_id, status="error", error="任务不存在或无权访问")
            logger.warning("[practice] 手动出题 job=%s 任务不存在或无权访问 task=%s", job_id, task_id)
            return
        logger.info(
            "[practice] 手动出题开始 job=%s task=%s user=%s max_findings=%d",
            job_id, task_id, user_id, max_findings,
        )
        created, skipped = generate_questions_for_task(
            db, task, user_id, max_findings=max_findings,
            progress_callback=lambda done, total: gen_jobs.update_job(
                job_id, done=done, total=total
            ),
            event_callback=lambda etype, data: gen_jobs.append_event(
                job_id, etype, data
            ),
        )
        kp_by_id = {
            kp.id: kp
            for kp in db.query(KnowledgePoint).filter(
                KnowledgePoint.id.in_([q.knowledge_point_id for q in created])
            ).all()
        } if created else {}
        items = []
        for q in created:
            kp = kp_by_id.get(q.knowledge_point_id)
            items.append(DraftQuestionResponse(
                id=q.id,
                qtype=q.qtype.value,
                stem=q.stem,
                code_snippet=q.code_snippet,
                options=q.options,
                answer_idx=q.answer_idx,
                explanation=q.explanation,
                difficulty=q.difficulty,
                knowledge_key=kp.key if kp else None,
                knowledge_name=kp.name if kp else None,
                origin=q.origin or "repo",
                languages=(kp.languages if kp else None) or [],
                source_file=q.source_file,
                source_lines=q.source_lines,
            ))
        # done/total 由进度回调维护,此处不覆盖
        gen_jobs.update_job(
            job_id, status="done", questions=items, skipped_findings=skipped
        )
        logger.info(
            "[practice] 手动出题完成 job=%s task=%s: %d 题(%d 条 finding 未出题)",
            job_id, task_id, len(items), skipped,
        )
    except PracticeGenerateError as e:
        # 致命错误(额度/认证等):友好原因直接展示,无需堆栈
        logger.warning("[practice] 出题中止 job=%s: %s", job_id, e)
        gen_jobs.update_job(job_id, status="error", error=str(e)[:500])
    except Exception as e:
        logger.exception("[practice] 异步出题失败 job=%s", job_id)
        gen_jobs.update_job(job_id, status="error", error=str(e)[:500])
    finally:
        db.close()


@router.get("/generate/jobs", response_model=GenerateJobsResponse)
def list_generate_jobs(
    current_user: User = Depends(get_current_user),
) -> GenerateJobsResponse:
    """当前用户的出题 job 列表(运行中优先,限最近 10 条)

    练习页侧栏轮询发现正在运行的出题 job(手动与自动来源都含)。
    注意:必须注册在 /generate/{job_id} 之前,否则 "jobs" 会被当成 job_id。
    """
    return GenerateJobsResponse(
        jobs=[GenerateJobSummary(**s) for s in gen_jobs.list_jobs(current_user.id)],
    )


def _sse_pack(etype: str, data: dict) -> str:
    """打包 SSE 事件(data 直接是事件载荷,前端按 event 类型分发)"""
    return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _client_disconnected(request: Request) -> bool:
    """同步生成器内检查客户端是否断开(同 tasks.await_request_disconnect)"""
    try:
        import anyio
        return anyio.from_thread.run(request.is_disconnected)
    except Exception:
        return False


@router.get("/generate/{job_id}/stream")
def stream_generate_job(
    job_id: str,
    request: Request,
    current_user: User = Depends(get_current_user_sse),
) -> StreamingResponse:
    """SSE:实时推送出题进度与大模型流式输出

    事件类型:
    - snapshot: 连接建立的初始快照(含 recent_text,中途接入兜底)
    - finding: 开始处理某条发现 / token: LLM 输出增量 /
      tool: 工具调用记录 / progress: 进度计数
    - done / error: 终止事件

    鉴权:EventSource 不能自定义 header,用 ?token=XXX 查询参数。
    """
    if gen_jobs.get_job(job_id, current_user.id) is None:
        raise HTTPException(status_code=404, detail="生成任务不存在或已过期")

    # [诊断] SSE 连接生命周期:谁在什么时候订阅了哪个 job
    logger.info("[practice-sse] 建立连接 job=%s user=%s", job_id, current_user.id)

    user_id = current_user.id

    def event_generator() -> Generator[str, None, None]:
        _t0 = time.monotonic()
        _sent = 0
        _reason = "unknown"
        try:
            snap = gen_jobs.snapshot(job_id, user_id)
            if snap is None:
                _reason = "job 已被清理"
                return  # 连接建立瞬间 job 已被清理
            # [诊断] snapshot 时的 job 状态(决定后续是否需要重放事件)
            logger.info(
                "[practice-sse] snapshot job=%s status=%s", job_id, snap["status"],
            )
            yield _sse_pack("snapshot", snap)
            after_seq = 0
            while True:
                if _client_disconnected(request):
                    _reason = "客户端断开"
                    return
                res = gen_jobs.read_events(job_id, user_id, after_seq, timeout=15.0)
                if res is None:
                    _reason = "job 过期/被清理"
                    return  # job 过期/被清理
                for ev in res["events"]:
                    yield _sse_pack(ev["type"], ev["data"])
                    after_seq = ev["seq"]
                    _sent += 1
                    if ev["type"] in ("done", "error"):
                        _reason = f"终止事件 {ev['type']}"
                        return
                if not res["events"]:
                    if res["job"]["status"] in ("done", "error"):
                        _reason = "终态事件已送达"
                        return  # 已终态且事件已全部送达
                    yield ": keep-alive\n\n"
        finally:
            # [诊断] 连接结束原因 + 总推送事件数 + 存活时长;
            # 若冻结期间这里不再输出且请求堆积,说明后端卡死
            logger.info(
                "[practice-sse] 结束 job=%s 原因=%s 推送事件=%d 时长=%.1fs",
                job_id, _reason, _sent, time.monotonic() - _t0,
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx:禁用缓冲,确保实时推送
        },
    )


@router.get("/generate/{job_id}", response_model=GenerateJobStatusResponse)
def get_generate_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
) -> GenerateJobStatusResponse:
    """轮询出题进度与结果"""
    job = gen_jobs.get_job(job_id, current_user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="生成任务不存在或已过期")
    return GenerateJobStatusResponse(
        status=job["status"],
        done=job["done"],
        total=job["total"],
        error=job["error"],
        questions=job["questions"],
        skipped_findings=job["skipped_findings"],
    )


@router.get("/drafts", response_model=list[DraftQuestionResponse])
def list_drafts(
    task_id: UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DraftQuestionResponse]:
    """待确认候选题完整内容(预览勾选/确认入库用;可按来源任务过滤)"""
    query = db.query(Question).filter(
        Question.user_id == current_user.id,
        Question.status == QuestionStatus.DRAFT,
    )
    if task_id is not None:
        query = query.filter(Question.source_task_id == task_id)
    drafts = query.order_by(Question.created_at).all()
    kp_by_id = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.id.in_([q.knowledge_point_id for q in drafts])
        ).all()
    } if drafts else {}
    items = []
    for q in drafts:
        kp = kp_by_id.get(q.knowledge_point_id)
        items.append(DraftQuestionResponse(
            id=q.id,
            qtype=q.qtype.value,
            stem=q.stem,
            code_snippet=q.code_snippet,
            options=q.options,
            answer_idx=q.answer_idx,
            explanation=q.explanation,
            difficulty=q.difficulty,
            knowledge_key=kp.key if kp else None,
            knowledge_name=kp.name if kp else None,
            origin=q.origin or "repo",
            languages=(kp.languages if kp else None) or [],
            source_file=q.source_file,
            source_lines=q.source_lines,
        ))
    return items


@router.post("/questions/confirm", response_model=ConfirmQuestionsResponse)
def confirm_questions(
    req: ConfirmQuestionsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConfirmQuestionsResponse:
    """确认勾选的 draft 题目入库,丢弃同一任务的其余 draft"""
    drafts = (
        db.query(Question)
        .filter(
            Question.user_id == current_user.id,
            Question.source_task_id == req.task_id,
            Question.status == QuestionStatus.DRAFT,
        )
        .all()
    )
    keep_ids = set(req.question_ids)
    confirmed = 0
    discarded = 0
    for q in drafts:
        if q.id in keep_ids:
            q.status = QuestionStatus.ACTIVE
            confirmed += 1
        else:
            db.delete(q)
            discarded += 1
    db.commit()
    return ConfirmQuestionsResponse(confirmed=confirmed, discarded=discarded)


@router.post("/questions/activate", response_model=ActivateQuestionsResponse)
def activate_questions(
    req: ActivateQuestionsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ActivateQuestionsResponse:
    """只把传入 id 的 draft 转 active,不影响其余 draft(题库管理页逐条转正用)"""
    if not req.question_ids:
        raise HTTPException(status_code=400, detail="未选择任何题目")
    drafts = (
        db.query(Question)
        .filter(
            Question.user_id == current_user.id,
            Question.id.in_(req.question_ids),
            Question.status == QuestionStatus.DRAFT,
        )
        .all()
    )
    for q in drafts:
        q.status = QuestionStatus.ACTIVE
    db.commit()
    return ActivateQuestionsResponse(activated=len(drafts))


# ============================================================
# 练习会话(按需即时组卷)
# ============================================================


@router.post("/sessions", response_model=StartSessionResponse)
def start_session(
    req: StartSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StartSessionResponse:
    """按选题策略组卷(答案不下发)"""
    questions = db.query(Question).filter(
        Question.user_id == current_user.id,
        Question.status == QuestionStatus.ACTIVE,
    ).all()
    if not questions:
        raise HTTPException(
            status_code=400,
            detail="题库为空,请先在审计任务详情页生成并确认练习题",
        )

    kp_ids = list({q.knowledge_point_id for q in questions})
    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(KnowledgePoint.id.in_(kp_ids)).all()
    }
    if req.topic_filter:
        kps = {k: v for k, v in kps.items() if v.key == req.topic_filter}
        if not kps:
            raise HTTPException(status_code=404, detail=f"题库中没有知识点 {req.topic_filter} 的题目")
        questions = [q for q in questions if q.knowledge_point_id in kps]
    if req.learning_topic:
        # 主题级练习:按知识点所属学习主题过滤(内置/自定义 key 皆可,
        # key 存在性由 kp 匹配兜底——无匹配 404)
        kps = {k: v for k, v in kps.items() if v.learning_topic == req.learning_topic}
        if not kps:
            raise HTTPException(status_code=404, detail=f"题库中没有学习主题 {req.learning_topic} 的题目")
        questions = [q for q in questions if q.knowledge_point_id in kps]
    if req.question_ids:
        # 白名单组卷(错题重练):只从传入 id 中选题,跳过复习/多样性约束
        allow = set(req.question_ids)
        questions = [q for q in questions if q.id in allow]
        if not questions:
            raise HTTPException(status_code=400, detail="指定题目中没有可用的 active 题目")
        return _start_session_from_pool(db, current_user, questions, kps, req.count)

    states = {
        s.knowledge_point_id: s
        for s in db.query(UserKnowledgeState).filter(
            UserKnowledgeState.user_id == current_user.id,
            UserKnowledgeState.knowledge_point_id.in_(list(kps.keys())),
        ).all()
    }
    # 每题历史作答次数(判新题)
    attempt_counts = dict(
        db.query(Attempt.question_id, sa_func.count(Attempt.id))
        .filter(Attempt.user_id == current_user.id)
        .group_by(Attempt.question_id)
        .all()
    )

    now = _now()
    candidates = []
    for q in questions:
        st = states.get(q.knowledge_point_id)
        candidates.append(CandidateInfo(
            question=q,
            question_id=q.id,
            kp_id=q.knowledge_point_id,
            difficulty=q.difficulty,
            question_attempts=attempt_counts.get(q.id, 0),
            kp_due_at=st.due_at if st else None,
            kp_attempts=st.attempts if st else 0,
            kp_correct_count=st.correct_count if st else 0,
        ))

    ability = _estimate_user_ability(db, current_user.id)
    picked = select_questions(candidates, ability, req.count, now)
    if not picked:
        raise HTTPException(status_code=400, detail="当前筛选条件下没有可出的题目")

    session = PracticeSession(
        user_id=current_user.id,
        question_count=len(picked),
        question_ids=[str(c.question_id) for c in picked],
        stats={
            "ability": ability,
            "topic_filter": req.topic_filter,
            "learning_topic": req.learning_topic,
            "review_count": sum(
                1 for c in picked if c.kp_due_at is not None and c.kp_due_at <= now
            ),
        },
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return StartSessionResponse(
        session_id=session.id,
        questions=[
            SessionQuestionResponse(
                id=c.question.id,
                qtype=c.question.qtype.value,
                stem=c.question.stem,
                code_snippet=c.question.code_snippet,
                options=c.question.options,
                difficulty=c.question.difficulty,
                knowledge_name=kps[c.kp_id].name if c.kp_id in kps else None,
                languages=(kps[c.kp_id].languages if c.kp_id in kps else None) or [],
                origin=c.question.origin or "repo",
                source_task_id=c.question.source_task_id,
                source_file=c.question.source_file,
                source_lines=c.question.source_lines,
            )
            for c in picked
        ],
    )


def _start_session_from_pool(
    db: Session,
    current_user: User,
    questions: list[Question],
    kps: dict,
    count: int,
) -> StartSessionResponse:
    """白名单组卷:直接按创建顺序取前 count 题,不走选题策略(错题重练用)"""
    picked = questions[:count]
    session = PracticeSession(
        user_id=current_user.id,
        question_count=len(picked),
        question_ids=[str(q.id) for q in picked],
        stats={"mode": "question_ids"},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return StartSessionResponse(
        session_id=session.id,
        questions=[
            SessionQuestionResponse(
                id=q.id,
                qtype=q.qtype.value,
                stem=q.stem,
                code_snippet=q.code_snippet,
                options=q.options,
                difficulty=q.difficulty,
                knowledge_name=kps[q.knowledge_point_id].name
                if q.knowledge_point_id in kps else None,
                languages=(
                    kps[q.knowledge_point_id].languages
                    if q.knowledge_point_id in kps else None
                ) or [],
                origin=q.origin or "repo",
                source_task_id=q.source_task_id,
                source_file=q.source_file,
                source_lines=q.source_lines,
            )
            for q in picked
        ],
    )


# ============================================================
# 答题(判分 + SM-2 更新)
# ============================================================


@router.post("/sessions/{session_id}/answers", response_model=SubmitAnswerResponse)
def submit_answer(
    session_id: UUID,
    req: SubmitAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubmitAnswerResponse:
    session = db.query(PracticeSession).filter(
        PracticeSession.id == session_id,
        PracticeSession.user_id == current_user.id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="练习会话不存在")
    if str(req.question_id) not in (session.question_ids or []):
        raise HTTPException(status_code=400, detail="该题不在本次练习中")

    # 同一会话内不允许重复作答
    dup = db.query(Attempt).filter(
        Attempt.session_id == session.id,
        Attempt.question_id == req.question_id,
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="该题已作答")

    question = db.query(Question).filter(
        Question.id == req.question_id,
        Question.user_id == current_user.id,
    ).first()
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在")
    if req.chosen_idx >= len(question.options or []):
        raise HTTPException(status_code=400, detail="选项下标越界")

    now = _now()
    is_correct = req.chosen_idx == question.answer_idx

    # 本次作答前的会话进度与能力值(避免 autoflush 把本条计入)
    answered_before = (
        db.query(sa_func.count(Attempt.id))
        .filter(Attempt.session_id == session.id)
        .scalar()
        or 0
    )
    ability = _estimate_user_ability(db, current_user.id)

    # 1. 落答题流水
    attempt = Attempt(
        user_id=current_user.id,
        session_id=session.id,
        question_id=question.id,
        chosen_idx=req.chosen_idx,
        is_correct=is_correct,
    )
    db.add(attempt)

    # 2. SM-2 更新知识点记忆状态(get_or_create)
    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.id == question.knowledge_point_id
    ).first()
    state_row = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == current_user.id,
        UserKnowledgeState.knowledge_point_id == question.knowledge_point_id,
    ).first()
    if state_row is None:
        state_row = UserKnowledgeState(
            user_id=current_user.id,
            knowledge_point_id=question.knowledge_point_id,
        )
        db.add(state_row)
        db.flush()

    new_state = apply_sm2(
        SM2State(
            ease_factor=state_row.ease_factor,
            interval_days=state_row.interval_days,
            repetitions=state_row.repetitions,
            due_at=state_row.due_at,
            attempts=state_row.attempts,
            correct_count=state_row.correct_count,
            last_quality=state_row.last_quality,
        ),
        quality_from_correct(is_correct),
        now,
    )
    state_row.ease_factor = new_state.ease_factor
    state_row.interval_days = new_state.interval_days
    state_row.repetitions = new_state.repetitions
    state_row.due_at = new_state.due_at
    state_row.attempts = new_state.attempts
    state_row.correct_count = new_state.correct_count
    state_row.last_quality = new_state.last_quality

    # 3. 题目难度微调(能力值用本次作答前的估计)
    question.difficulty = adjust_question_difficulty(
        question.difficulty, ability, is_correct
    )

    # 4. 会话进度 / 收尾
    answered_count = answered_before + 1
    if answered_count >= session.question_count and session.finished_at is None:
        session.finished_at = now

    db.commit()

    accuracy = (
        new_state.correct_count / new_state.attempts if new_state.attempts else None
    )
    return SubmitAnswerResponse(
        is_correct=is_correct,
        correct_idx=question.answer_idx,
        explanation=question.explanation or "",
        state=KnowledgeStateResponse(
            knowledge_key=kp.key if kp else "general",
            knowledge_name=kp.name if kp else "未分类",
            languages=(kp.languages if kp else None) or [],
            ease_factor=new_state.ease_factor,
            interval_days=new_state.interval_days,
            repetitions=new_state.repetitions,
            due_at=new_state.due_at,
            attempts=new_state.attempts,
            correct_count=new_state.correct_count,
            accuracy=accuracy,
        ),
        answered_count=answered_count,
        total_count=session.question_count,
    )


# ============================================================
# 统计 / 题库管理
# ============================================================


@router.get("/summary", response_model=PracticeSummaryResponse)
def get_practice_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeSummaryResponse:
    """轻量汇总(导航徽章用):到期待复习知识点数 + 待确认 draft 数"""
    now = _now()
    due_count = (
        db.query(sa_func.count(UserKnowledgeState.id))
        .filter(
            UserKnowledgeState.user_id == current_user.id,
            UserKnowledgeState.due_at.isnot(None),
            UserKnowledgeState.due_at <= now,
        )
        .scalar()
        or 0
    )
    draft_count = (
        db.query(sa_func.count(Question.id))
        .filter(
            Question.user_id == current_user.id,
            Question.status == QuestionStatus.DRAFT,
        )
        .scalar()
        or 0
    )
    return PracticeSummaryResponse(due_count=due_count, draft_count=draft_count)


@router.get("/trend", response_model=TrendResponse)
def get_trend(
    weeks: int = Query(default=8, ge=1, le=26),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrendResponse:
    """按周聚合的作答趋势(旧到新,含无作答的零值周)"""
    now = _now()
    # 对齐到本周一 00:00 UTC,往前推 weeks-1 周作为首格
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = monday - timedelta(weeks=weeks - 1)
    rows = (
        db.query(Attempt.answered_at, Attempt.is_correct)
        .filter(Attempt.user_id == current_user.id, Attempt.answered_at >= start)
        .all()
    )
    buckets = {start + timedelta(weeks=i): [0, 0] for i in range(weeks)}
    for answered_at, is_correct in rows:
        offset = (answered_at - start).days // 7
        if 0 <= offset < weeks:
            ws = start + timedelta(weeks=offset)
            buckets[ws][0] += 1
            if is_correct:
                buckets[ws][1] += 1
    return TrendResponse(
        weeks=[
            TrendPoint(week_start=ws, attempts=n[0], correct=n[1])
            for ws, n in sorted(buckets.items())
        ]
    )


@router.get("/sessions", response_model=list[SessionListItem])
def list_sessions(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SessionListItem]:
    """历史练习会话(新到旧,含作答数/正确率)"""
    sessions = (
        db.query(PracticeSession)
        .filter(PracticeSession.user_id == current_user.id)
        .order_by(PracticeSession.started_at.desc())
        .limit(limit)
        .all()
    )
    if not sessions:
        return []
    stats = {
        sid: (cnt or 0, corr or 0)
        for sid, cnt, corr in db.query(
            Attempt.session_id,
            sa_func.count(Attempt.id),
            sa_func.sum(cast(Attempt.is_correct, Integer)),
        )
        .filter(Attempt.session_id.in_([s.id for s in sessions]))
        .group_by(Attempt.session_id)
        .all()
    }
    items = []
    for s in sessions:
        answered, correct = stats.get(s.id, (0, 0))
        items.append(SessionListItem(
            id=s.id,
            started_at=s.started_at,
            finished_at=s.finished_at,
            question_count=s.question_count,
            answered_count=answered or 0,
            correct_count=correct or 0,
            accuracy=(correct / answered) if answered else None,
        ))
    return items


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session_detail(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionDetailResponse:
    """会话逐题作答明细(错题回顾用)"""
    session = db.query(PracticeSession).filter(
        PracticeSession.id == session_id,
        PracticeSession.user_id == current_user.id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="练习会话不存在")
    attempts = (
        db.query(Attempt)
        .filter(Attempt.session_id == session.id)
        .order_by(Attempt.answered_at)
        .all()
    )
    questions = {
        q.id: q
        for q in db.query(Question).filter(
            Question.id.in_([a.question_id for a in attempts])
        ).all()
    } if attempts else {}
    kp_ids = list({q.knowledge_point_id for q in questions.values()})
    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(KnowledgePoint.id.in_(kp_ids)).all()
    } if kp_ids else {}
    items = []
    for a in attempts:
        q = questions.get(a.question_id)
        if q is None:
            continue  # 题目已被删除
        kp = kps.get(q.knowledge_point_id)
        items.append(SessionAttemptItem(
            question_id=q.id,
            stem=q.stem,
            qtype=q.qtype.value,
            knowledge_name=kp.name if kp else None,
            chosen_idx=a.chosen_idx,
            correct_idx=q.answer_idx,
            is_correct=a.is_correct,
            answered_at=a.answered_at,
        ))
    return SessionDetailResponse(
        id=session.id,
        started_at=session.started_at,
        finished_at=session.finished_at,
        question_count=session.question_count,
        attempts=items,
    )


@router.get("/stats", response_model=StatsResponse)
def get_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatsResponse:
    now = _now()
    states = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == current_user.id
    ).all()
    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == current_user.id
        ).all()
    }

    total_attempts = sum(s.attempts for s in states)
    total_correct = sum(s.correct_count for s in states)
    due_count = sum(1 for s in states if s.due_at is not None and s.due_at <= now)

    weak_points = []
    for s in states:
        if s.attempts <= 0:
            continue
        kp = kps.get(s.knowledge_point_id)
        weak_points.append(WeakPointItem(
            knowledge_key=kp.key if kp else "general",
            knowledge_name=kp.name if kp else "未分类",
            languages=(kp.languages if kp else None) or [],
            attempts=s.attempts,
            correct_count=s.correct_count,
            accuracy=round(1.0 - s.correct_count / s.attempts, 4),
            ease_factor=s.ease_factor,
            due_at=s.due_at,
        ))
    # 错误率高且样本多的排前面
    weak_points.sort(key=lambda w: (-w.accuracy, -w.attempts))

    active_count = (
        db.query(sa_func.count(Question.id))
        .filter(
            Question.user_id == current_user.id,
            Question.status == QuestionStatus.ACTIVE,
        )
        .scalar()
        or 0
    )
    draft_count = (
        db.query(sa_func.count(Question.id))
        .filter(
            Question.user_id == current_user.id,
            Question.status == QuestionStatus.DRAFT,
        )
        .scalar()
        or 0
    )

    return StatsResponse(
        ability=_estimate_user_ability(db, current_user.id),
        due_count=due_count,
        total_attempts=total_attempts,
        total_correct=total_correct,
        accuracy=(total_correct / total_attempts) if total_attempts else None,
        weak_points=weak_points[:10],
        active_question_count=active_count,
        draft_question_count=draft_count,
    )


@router.get("/knowledge-points", response_model=list[KnowledgePointCardItem])
def list_knowledge_points(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[KnowledgePointCardItem]:
    """知识点卡片列表(知识点看板视图):全量知识点 + SM-2 状态 + 题数 + 分栏状态

    分栏按优先级派生(与 selector 薄弱判定同一套常量):
    weak(错误率超阈值且样本足) > due(SM-2 到期) > mastered(连续答对已巩固)
    > learning(有作答记录) > fresh(从未作答)。
    排序:分栏优先,栏内薄弱按错误率、待复习按最急到期、其余按作答数。
    """
    now = _now()
    kps = (
        db.query(KnowledgePoint)
        .filter(KnowledgePoint.user_id == current_user.id)
        .all()
    )
    if not kps:
        return []
    kp_ids = [kp.id for kp in kps]
    states = {
        s.knowledge_point_id: s
        for s in db.query(UserKnowledgeState).filter(
            UserKnowledgeState.user_id == current_user.id,
            UserKnowledgeState.knowledge_point_id.in_(kp_ids),
        ).all()
    }
    q_counts = dict(
        db.query(Question.knowledge_point_id, sa_func.count(Question.id))
        .filter(
            Question.user_id == current_user.id,
            Question.status == QuestionStatus.ACTIVE,
            Question.knowledge_point_id.in_(kp_ids),
        )
        .group_by(Question.knowledge_point_id)
        .all()
    )

    # 已巩固判定:连续答对累计 3 次以上且正确率 >= 75%
    mastered_min_repetitions = 3
    mastered_min_accuracy = 0.75

    items: list[KnowledgePointCardItem] = []
    for kp in kps:
        st = states.get(kp.id)
        attempts = st.attempts if st else 0
        correct = st.correct_count if st else 0
        accuracy = (correct / attempts) if attempts else None
        repetitions = st.repetitions if st else 0
        due_at = st.due_at if st else None
        if (
            attempts >= WEAKNESS_MIN_ATTEMPTS
            and accuracy is not None
            and (1.0 - accuracy) > WEAKNESS_ERROR_RATE
        ):
            status = "weak"
        elif due_at is not None and due_at <= now:
            status = "due"
        elif (
            repetitions >= mastered_min_repetitions
            and accuracy is not None
            and accuracy >= mastered_min_accuracy
        ):
            status = "mastered"
        elif attempts > 0:
            status = "learning"
        else:
            status = "fresh"
        items.append(KnowledgePointCardItem(
            knowledge_key=kp.key,
            knowledge_name=kp.name,
            category=kp.category,
            languages=kp.languages or [],
            learning_topic=kp.learning_topic,
            attempts=attempts,
            correct_count=correct,
            accuracy=accuracy,
            repetitions=repetitions,
            interval_days=st.interval_days if st else 0.0,
            ease_factor=st.ease_factor if st else 2.5,
            due_at=due_at,
            question_count=q_counts.get(kp.id, 0) or 0,
            board_status=status,
        ))

    # 稳定排序:分栏优先,栏内薄弱按错误率降序、待复习按最急到期、
    # 其余按作答数降序(跨栏第一位的 rank 已分出大小,不会比较后续异构字段)
    rank = {"weak": 0, "due": 1, "mastered": 2, "learning": 3, "fresh": 4}

    def _sort_key(item: KnowledgePointCardItem):
        r = rank[item.board_status]
        if item.board_status == "weak":
            return (r, -(1.0 - (item.accuracy or 0.0)), -item.attempts, item.knowledge_key)
        if item.board_status == "due":
            return (r, item.due_at or now, item.knowledge_key)
        return (r, -item.attempts, -item.question_count, item.knowledge_key)

    items.sort(key=_sort_key)
    return items


@router.get("/questions", response_model=list[QuestionListItem])
def list_questions(
    status: str | None = Query(default=None, pattern="^(draft|active|archived)$"),
    knowledge_point: str | None = Query(default=None),
    mistake: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[QuestionListItem]:
    query = db.query(Question).filter(Question.user_id == current_user.id)
    if status:
        query = query.filter(Question.status == QuestionStatus(status))
    if mistake:
        # 错题本:答错过的 active 题(去重)
        wrong_ids = [
            qid for (qid,) in db.query(Attempt.question_id)
            .filter(
                Attempt.user_id == current_user.id,
                Attempt.is_correct.is_(False),
            )
            .distinct()
            .all()
        ]
        if not wrong_ids:
            return []
        query = query.filter(
            Question.status == QuestionStatus.ACTIVE,
            Question.id.in_(wrong_ids),
        )
    questions = query.order_by(Question.created_at.desc()).all()

    if knowledge_point:
        kp_ids = {
            kp.id
            for kp in db.query(KnowledgePoint).filter(
                KnowledgePoint.user_id == current_user.id,
                KnowledgePoint.key == knowledge_point,
            ).all()
        }
        questions = [q for q in questions if q.knowledge_point_id in kp_ids]

    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == current_user.id
        ).all()
    }
    attempt_stats = {
        qid: (cnt or 0, corr or 0)
        for qid, cnt, corr in db.query(
            Attempt.question_id,
            sa_func.count(Attempt.id),
            sa_func.sum(cast(Attempt.is_correct, Integer)),
        )
        .filter(Attempt.user_id == current_user.id)
        .group_by(Attempt.question_id)
        .all()
    } if questions else {}

    items = []
    for q in questions:
        attempts, correct = attempt_stats.get(q.id, (0, 0))
        kp = kps.get(q.knowledge_point_id)
        items.append(QuestionListItem(
            id=q.id,
            qtype=q.qtype.value,
            stem=q.stem,
            difficulty=q.difficulty,
            status=q.status.value,
            knowledge_name=kp.name if kp else None,
            languages=(kp.languages if kp else None) or [],
            origin=q.origin,
            attempts=attempts or 0,
            accuracy=(correct / attempts) if attempts else None,
            created_at=q.created_at,
        ))
    return items


@router.get("/questions/{question_id}", response_model=QuestionDetailResponse)
def get_question_detail(
    question_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QuestionDetailResponse:
    """题目完整信息(题库管理 / 错题回顾 / 历史明细点开单行时按需拉取)

    含 answer_idx 与 explanation,因此只用于首页复盘;组卷端点
    (POST /practice/sessions)仍走不下发答案的 SessionQuestionResponse。
    已归档题同样可读(归档只是退出组卷,题目内容仍需回看)。
    """
    question = db.query(Question).filter(
        Question.id == question_id,
        Question.user_id == current_user.id,
    ).first()
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在")

    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.id == question.knowledge_point_id
    ).first()
    attempts, correct = db.query(
        sa_func.count(Attempt.id),
        sa_func.sum(cast(Attempt.is_correct, Integer)),
    ).filter(
        Attempt.question_id == question.id,
        Attempt.user_id == current_user.id,
    ).one()
    attempts = attempts or 0
    correct = correct or 0

    return QuestionDetailResponse(
        id=question.id,
        qtype=question.qtype.value,
        stem=question.stem,
        code_snippet=question.code_snippet,
        options=question.options,
        answer_idx=question.answer_idx,
        explanation=question.explanation,
        difficulty=question.difficulty,
        knowledge_key=kp.key if kp else None,
        knowledge_name=kp.name if kp else None,
        origin=question.origin or "repo",
        languages=(kp.languages if kp else None) or [],
        source_file=question.source_file,
        source_lines=question.source_lines,
        status=question.status.value,
        category=kp.category if kp else None,
        learning_topic=question.learning_topic,
        source_task_id=question.source_task_id,
        attempts=attempts,
        correct_count=correct,
        accuracy=(correct / attempts) if attempts else None,
        created_at=question.created_at,
    )


@router.post("/questions/{question_id}/archive")
def archive_question(
    question_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """归档题目(不再参与组卷);已是 active/draft 均可归档"""
    question = db.query(Question).filter(
        Question.id == question_id,
        Question.user_id == current_user.id,
    ).first()
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在")
    question.status = QuestionStatus.ARCHIVED
    db.commit()
    return {"archived": True}


# ============================================================
# 清空练习记录(进度归零;可选连题库一并删除)
# ============================================================


@router.delete("/records", response_model=ClearRecordsResponse)
def clear_records(
    include_questions: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClearRecordsResponse:
    """清空当前用户的练习记录

    基础档:删除答题流水 / 会话 / SM-2 记忆状态,题库保留但难度重置回初值;
    include_questions=true 时连题目与知识点词典一并删除,回到完全空白。
    practice_settings 不受影响。
    """
    deleted_attempts = (
        db.query(Attempt).filter(Attempt.user_id == current_user.id).delete()
    )
    deleted_sessions = (
        db.query(PracticeSession)
        .filter(PracticeSession.user_id == current_user.id)
        .delete()
    )
    db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == current_user.id
    ).delete()

    deleted_questions = 0
    if include_questions:
        deleted_questions = (
            db.query(Question).filter(Question.user_id == current_user.id).delete()
        )
        db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == current_user.id
        ).delete()
    else:
        # 难度由作答动态调整过,进度归零时重置回默认初值
        db.query(Question).filter(Question.user_id == current_user.id).update(
            {"difficulty": 3.0}
        )
    db.commit()
    logger.info(
        "练习记录已清空: user=%s sessions=%s attempts=%s questions=%s",
        current_user.id, deleted_sessions, deleted_attempts, deleted_questions,
    )
    return ClearRecordsResponse(
        deleted_sessions=deleted_sessions,
        deleted_attempts=deleted_attempts,
        deleted_questions=deleted_questions,
    )
