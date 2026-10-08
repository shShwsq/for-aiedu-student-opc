"""任务完成后自动生成练习题 draft(题库供给自动化)

挂在 orchestrator 任务完成收尾处(与 memory_summarize 同模式):
- 只生成 draft,仍需用户在任务详情页预览确认后转 active
- 守卫条件:登录用户、开关开启、存在结构化 finding(metadata 非空);
  单 agent 模式的纯摘要 Result 无元信息,自动跳过避免低质量题
- 同一任务已生成过(任何状态的题目)则跳过,避免追问/重试后重复出题
- 生成过程创建 job 追踪(source=auto),练习页出题进度侧栏可实时查看
"""
import logging

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.models.practice import PracticeSettings, Question
from app.models.task import Result, Task
from app.services.practice import jobs as gen_jobs
from app.services.practice.generator import (
    PracticeGenerateCancelled,
    PracticeGenerateError,
    generate_questions_for_task,
)

logger = logging.getLogger(__name__)


def _has_structured_findings(db: Session, task_id) -> bool:
    """任务的 Results 中是否存在带元信息的结构化发现

    判定标准:metadata 为非空 dict(安全场景含 cwe/severity,
    代码审核等场景含 category/file_path 等,文书场景含条款定位;
    出题主题按发现内容自动匹配,不限定只认 cwe/severity)。
    """
    results = db.query(Result.metadata_).filter(Result.task_id == task_id).all()
    for (meta,) in results:
        if meta and isinstance(meta, dict) and any(
            str(v or "").strip() for v in meta.values()
        ):
            return True
    return False


def auto_generate_practice_for_task(task: Task, db: Session) -> int:
    """任务完成时为审计发现自动生成练习题 draft

    返回新生成的题目数;任一守卫条件不满足返回 0(调用方无需区分原因)。
    异常由调用方 try/except 兜底,不影响任务完成。
    """
    # 1) 匿名任务不支持(练习题 per-user 隔离)
    if task.user_id is None:
        logger.info("[task=%s] 匿名任务不支持自动生成练习题,跳过", task.id)
        return 0

    # 2) 用户开关(practice_settings 独立表,1:1)
    pref = db.query(PracticeSettings).filter(
        PracticeSettings.user_id == task.user_id
    ).first()
    if pref is not None and not pref.auto_generate_practice:
        logger.info("[task=%s] 用户关闭了自动生成练习题,跳过", task.id)
        return 0

    # 3) 已有题目(任意状态):追问/重试重新完成时不重复出题
    exists = db.query(Question.id).filter(
        Question.source_task_id == task.id
    ).first()
    if exists:
        logger.info("[task=%s] 该任务已有练习题,跳过自动生成", task.id)
        return 0

    # 4) 只对有结构化发现的任务出题(单 agent 纯摘要无元信息,质量差)
    if not _has_structured_findings(db, task.id):
        logger.info("[task=%s] 无结构化审计发现(元信息为空),跳过自动生成", task.id)
        return 0

    # 5) 创建 job 追踪(source=auto):练习页侧栏轮询 jobs 列表可发现,
    #    并可通过 SSE 实时看到进度与大模型流式输出
    result_count = (
        db.query(sa_func.count(Result.id))
        .filter(Result.task_id == task.id)
        .scalar()
        or 0
    )
    job_id = gen_jobs.create_job(
        task.user_id,
        source="auto",
        task_id=str(task.id),
        task_title=task.title or (task.user_input or "")[:60],
    )
    # generator 内部最多处理 10 条 finding,与默认 max_findings 对齐
    gen_jobs.set_total(job_id, total=min(result_count, 10))
    try:
        created, skipped = generate_questions_for_task(
            db, task, task.user_id,
            progress_callback=lambda done, total: gen_jobs.update_job(
                job_id, done=done, total=total,
            ),
            event_callback=lambda etype, data: gen_jobs.append_event(
                job_id, etype, data,
            ),
            should_stop=lambda: gen_jobs.is_stop_requested(job_id),
        )
    except PracticeGenerateCancelled as e:
        # 用户在出题前停掉了自动出题 job(克隆阶段):无题可保留,cancelled 不是 error
        logger.info("[task=%s] 自动出题在出题前被停止 job=%s: %s", task.id, job_id, e)
        gen_jobs.update_job(job_id, status="cancelled")
        return 0
    except PracticeGenerateError as e:
        # 致命错误(额度/认证等):友好原因置 job error 供侧栏展示,无需堆栈
        logger.warning("[task=%s] 自动出题中止 job=%s: %s", task.id, job_id, e)
        gen_jobs.update_job(job_id, status="error", error=str(e)[:500])
        raise  # 保持原有语义:由调用方 try/except 兜底,不影响任务完成
    except Exception as e:
        logger.exception("[task=%s] 自动生成练习题失败 job=%s", task.id, job_id)
        gen_jobs.update_job(job_id, status="error", error=str(e)[:500])
        raise  # 保持原有语义:由调用方 try/except 兜底,不影响任务完成
    if gen_jobs.is_cancelled(job_id, task.user_id):
        # 部分题目已生成后被停止:照常保留(仍是 draft,需用户确认)
        # 全部跑完才点到停止的请求走下面的 done,不误报"已停止"
        gen_jobs.update_job(
            job_id, status="cancelled",
            created_count=len(created), skipped_findings=skipped,
        )
        logger.info(
            "[task=%s] 自动出题已按请求停止 job=%s: %d 题已生成待确认",
            task.id, job_id, len(created),
        )
        return len(created)
    gen_jobs.update_job(
        job_id, status="done",
        created_count=len(created), skipped_findings=skipped,
    )
    logger.info(
        "[task=%s] 自动生成练习题 draft: %d 题(%d 条发现未能出题)",
        task.id, len(created), skipped,
    )
    return len(created)
