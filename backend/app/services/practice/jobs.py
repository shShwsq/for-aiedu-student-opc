"""异步出题任务的内存状态管理

POST /practice/generate 立即返回 job_id,后台线程执行生成,
前端轮询 GET /practice/generate/{job_id} 获取进度与结果;
实时进度与 LLM 流式输出经 GET /practice/generate/{job_id}/stream(SSE)推送。

设计:
- 进程内 dict + 全局锁/条件变量(单实例部署够用;多实例部署时各实例轮询自己的
  job,前端命中非执行实例会 404,属已知边界,后续可换 Redis)
- 每个 job 维护带序号的事件日志(finding/token/tool/restore/progress/done/error),
  SSE 端点按 after_seq 增量重放;事件队列用 deque 并从**左端裁剪**(超预算时
  丢弃最旧一条),保证留存事件始终是序号连续的后缀——
  旧写法在锁内用 next()+list.remove() 找第一个 token 事件再删,是 O(n) 扫描
  且可能删中间项破坏连续性;配合生成侧的 token 合并,单 job 事件数从近万降到
  百量级,读端每次轮询的线性扫不再成为拖慢出题的锁竞争源
- 中途接入的客户端靠 snapshot(含 recent_text 尾部文本)兜底
- job 完成/失败后保留 TTL 秒供轮询取结果,过期或超量时清理
- job 记录 user_id,读取时校验,防跨用户窥探
"""
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# [诊断] 锁等待超过该阈值(秒)时告警,定位全局锁竞争/后端卡死
_LOCK_WAIT_THRESHOLD = 0.25

# job 完成后保留时长(秒),供前端取结果
_JOB_TTL_SECONDS = 3600
# 最多保留的 job 数(超量时先清理最旧的已完成 job)
_MAX_JOBS = 200
# 单 job 事件流中 token 文本总量上限(超限从队列左端裁剪最旧事件)
_MAX_EVENT_TOKEN_CHARS = 64 * 1024
# snapshot 携带的 recent_text 尾部长度(中途接入客户端的可视兜底)
_RECENT_TEXT_CHARS = 4000

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
# 事件等待用条件变量(复用全局锁,SSE 读端可阻塞等待新事件)
_COND = threading.Condition(_LOCK)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_job(
    user_id,
    source: str = "manual",
    task_id: str | None = None,
    task_title: str = "",
) -> str:
    job_id = uuid.uuid4().hex
    with _COND:
        _prune_locked()
        _JOBS[job_id] = {
            "id": job_id,
            "user_id": user_id,
            "status": "pending",
            "done": 0,
            "total": 0,
            "error": "",
            "questions": [],
            "skipped_findings": 0,
            "created_count": 0,
            # 出题来源:manual(任务详情页手动) / auto(任务完成自动生成)
            "source": source,
            "task_id": task_id,
            "task_title": task_title or "",
            # 当前正在处理的 finding 标题(SSE snapshot 用)
            "current_finding": "",
            # 当前 finding 已累计的 LLM 输出尾部文本(中途接入 snapshot 用)
            "recent_text": "",
            # 工作区恢复最新状态(start/progress/done/failed,中途接入 snapshot 用)
            "restore": None,
            # 事件日志:deque,元素为 {seq, type, data}(只从左侧裁剪)
            "events": deque(),
            "event_seq": 0,
            # 事件流中 token 文本累计字符数(裁剪判定用)
            "event_token_chars": 0,
            "created_at": time.monotonic(),
            "started_at": _utc_now(),
            "finished_at": None,
        }
    return job_id


def get_job(job_id: str, user_id) -> dict | None:
    """按 id + user 读取 job(不存在或不属于该用户返回 None)"""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        # 拷贝一份,避免读时被 worker 线程修改
        return dict(job)


def update_job(job_id: str, **fields) -> None:
    with _COND:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        status = fields.get("status")
        if status in ("done", "error"):
            job["finished_at"] = time.monotonic()
            # 终态自动追加终止事件,SSE 客户端据此收尾并断流
            if status == "done":
                _append_locked(job, "done", {
                    "created": job["created_count"] or len(job["questions"]),
                    "skipped": job["skipped_findings"],
                })
            else:
                _append_locked(job, "error", {"message": job["error"]})
        elif "done" in fields or "total" in fields:
            _append_locked(job, "progress", {
                "done": job["done"], "total": job["total"],
            })
        _COND.notify_all()


def set_total(job_id: str, total: int) -> None:
    update_job(job_id, total=total, status="running")


def append_event(job_id: str, etype: str, data: dict) -> None:
    """追加流式事件(finding/token/tool/restore/explain),并维护 snapshot 辅助字段

    - finding:切换当前 finding,recent_text 清零
    - token:追加 LLM 输出增量,recent_text 只保留尾部
    - tool:工具调用记录(仅入事件流)
    - restore:出题前工作区恢复(start/progress/done/failed),同步记入
      job["restore"] 供中途接入的 snapshot 兜底
    - explain:收尾知识点讲解阶段(start/done/failed),仅入事件流
    """
    if etype not in ("finding", "token", "tool", "restore", "explain"):
        return
    # [诊断] 测量获取全局锁的等待耗时(不含锁内处理),超阈值告警
    _t = time.perf_counter()
    with _COND:
        _w = time.perf_counter() - _t
        job = _JOBS.get(job_id)
        if job is None or job["status"] in ("done", "error"):
            return
        if etype == "finding":
            job["current_finding"] = str(data.get("title") or "")
            job["recent_text"] = ""
        elif etype == "token":
            delta = str(data.get("delta") or "")
            job["recent_text"] = (job["recent_text"] + delta)[-(_RECENT_TEXT_CHARS):]
        elif etype == "restore":
            job["restore"] = dict(data)
        _append_locked(job, etype, data)
        _COND.notify_all()
    if _w > _LOCK_WAIT_THRESHOLD:
        logger.warning(
            "[gen-jobs] append_event(%s) 锁等待 %.3fs(job=%s)", etype, _w, job_id,
        )


def _append_locked(job: dict, etype: str, data: dict) -> None:
    """追加事件;token 文本超上限时从**左端**丢弃最旧事件(调用方持锁)

    留存事件始终是序号连续的后缀,读端无需考虑中间挖空。
    被丢的可能是任何类型(含结构性事件):只发生在极长 job,
    晚接入客户端本来也拿不到头部事件,由 snapshot 兜底。
    """
    job["event_seq"] += 1
    events = job["events"]
    events.append({"seq": job["event_seq"], "type": etype, "data": data})
    if etype != "token":
        return
    delta = data.get("delta") or ""
    job["event_token_chars"] += len(delta)
    while job["event_token_chars"] > _MAX_EVENT_TOKEN_CHARS and events:
        victim = events.popleft()
        if victim["type"] == "token":
            job["event_token_chars"] -= len(str(victim["data"].get("delta") or ""))


def read_events(
    job_id: str, user_id, after_seq: int = 0, timeout: float = 0.0,
) -> dict | None:
    """读取 seq > after_seq 的事件,SSE 流式消费用

    - job 不存在/不属于该用户 → None
    - 有新事件或 job 已终态 → 立即返回;否则阻塞等待至多 timeout 秒
      (timeout<=0 不等待)
    返回 {"job": 摘要快照, "events": [{"seq","type","data"}]}。
    """
    # [诊断] SSE 读端锁等待耗时,超阈值说明与写端(token 洪流)严重竞争
    _t = time.perf_counter()
    with _COND:
        _w = time.perf_counter() - _t
        if _w > _LOCK_WAIT_THRESHOLD:
            logger.warning("[gen-jobs] read_events 锁等待 %.3fs(job=%s)", _w, job_id)
        job = _JOBS.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while True:
            new = [e for e in job["events"] if e["seq"] > after_seq]
            finished = job["status"] in ("done", "error")
            if new or finished or deadline is None:
                return {"job": _summary_locked(job), "events": new}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"job": _summary_locked(job), "events": []}
            _COND.wait(remaining)


def _summary_locked(job: dict) -> dict:
    """job 摘要(不含 events/questions/锁对象),供列表与 snapshot 使用"""
    return {
        "job_id": job["id"],
        "status": job["status"],
        "done": job["done"],
        "total": job["total"],
        "error": job["error"],
        "source": job["source"],
        "task_id": job["task_id"],
        "task_title": job["task_title"],
        "current_finding": job["current_finding"],
        "recent_text": job["recent_text"],
        "restore": job["restore"],
        "skipped_findings": job["skipped_findings"],
        "created_count": job["created_count"] or len(job["questions"]),
        "started_at": job["started_at"].isoformat(),
    }


def snapshot(job_id: str, user_id) -> dict | None:
    """SSE 连接建立时的初始快照(含 recent_text 供中途接入兜底)"""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job["user_id"] != user_id:
            return None
        return _summary_locked(job)


def list_jobs(
    user_id, limit: int = 10, sources: tuple[str, ...] | None = None,
) -> list[dict]:
    """该用户未过期的 job 摘要(运行中优先,其余按创建时间倒序,限 limit 条)

    sources 非空时只返回这些来源的 job(如练习页侧栏只要 manual/auto,
    把知识点讲解的 explain job 隔在外面,不干扰出题进度展示)。
    """
    with _LOCK:
        mine = [j for j in _JOBS.values() if j["user_id"] == user_id]
    if sources:
        wanted = set(sources)
        mine = [j for j in mine if j["source"] in wanted]
    running = [j for j in mine if j["status"] in ("pending", "running")]
    finished = [j for j in mine if j["status"] not in ("pending", "running")]
    running.sort(key=lambda j: j["created_at"], reverse=True)
    finished.sort(key=lambda j: j["created_at"], reverse=True)
    ordered = (running + finished)[:limit]
    return [_summary_locked(j) for j in ordered]


def _prune_locked() -> None:
    """清理过期/超量 job(调用方持锁)"""
    now = time.monotonic()
    expired = [
        jid for jid, j in _JOBS.items()
        if j["finished_at"] is not None and now - j["finished_at"] > _JOB_TTL_SECONDS
    ]
    for jid in expired:
        del _JOBS[jid]
    # 超量:按创建时间淘汰最旧的已完成 job
    if len(_JOBS) > _MAX_JOBS:
        finished = sorted(
            ((jid, j) for jid, j in _JOBS.items() if j["finished_at"] is not None),
            key=lambda kv: kv[1]["created_at"],
        )
        for jid, _ in finished[: len(_JOBS) - _MAX_JOBS]:
            del _JOBS[jid]
