"""出题进度事件流测试(jobs 事件日志 + jobs 列表端点 + SSE 流)

覆盖:
- jobs.py:事件记录/序号/裁剪、recent_text 维护、list_jobs 隔离与排序、
  终态自动追加 done/error 事件、read_events 增量读取
- GET /practice/generate/jobs:列表与跨用户隔离
- GET /practice/generate/{job_id}/stream:snapshot 先行、事件重放、
  done 收尾、404/401 路径

数据库部分沿用 test_practice_api 的独立 schema + fake 生成器模式。
"""
import json
import os
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user, get_current_user_sse

import app.models.user  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_preference  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.practice  # noqa: F401

from app.models.practice import (
    KnowledgePoint,
    Question,
    QuestionStatus,
    QuestionType,
)
from app.models.task import Result, Task
from app.models.user import User
from app.routers import practice as practice_router
from app.services.practice import jobs as gen_jobs

# schema 名含 PID + 随机后缀:并发 pytest 进程各自独立建 schema,
# session 开始的 DROP SCHEMA 不会误删他人正在用的 schema(同 test_practice_api)
TEST_SCHEMA = f"pytest_practice_stream_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = (
    "practice_attempts", "practice_sessions", "practice_questions",
    "user_knowledge_states", "knowledge_points",
    "results", "conversations", "task_artifacts", "tasks", "user_preferences", "user_git_bindings", "users",
)


@pytest.fixture(scope="session")
def test_engine():
    try:
        maint = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    for i in range(5):
        try:
            with test_engine.begin() as conn:
                for t in _TABLES:
                    conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
            break
        except Exception:
            if i == 4:
                raise
            time.sleep(0.3)
    yield


# fake 生成器:每条 Result 出 1 题,并发出 finding/token 流式事件
def _fake_generate(db, task, user_id, max_findings=10, client=None,
                   progress_callback=None, event_callback=None,
                   force_regenerate=False):
    findings = db.query(Result).filter(Result.task_id == task.id).all()[:max_findings]
    created = []
    for i, r in enumerate(findings):
        if event_callback:
            event_callback("finding", {
                "index": i + 1, "total": len(findings), "title": r.title,
            })
            event_callback("token", {"delta": f"fake-output-{i}"})
        kp = KnowledgePoint(
            user_id=user_id, key=f"CWE-{89 + i}", name=f"知识点{i}", category="cwe"
        )
        db.add(kp)
        db.flush()
        q = Question(
            user_id=user_id,
            source_task_id=task.id,
            source_result_id=r.id,
            knowledge_point_id=kp.id,
            qtype=QuestionType.SINGLE_CHOICE,
            stem=f"题干{i}",
            options=["甲", "乙", "丙", "丁"],
            answer_idx=0,
            explanation="解析",
            difficulty=1.5,
            status=QuestionStatus.DRAFT,
            dedup_hash=uuid.uuid4().hex,
        )
        db.add(q)
        created.append(q)
        if progress_callback:
            progress_callback(i + 1, len(findings))
    db.commit()
    for q in created:
        db.refresh(q)
    return created, 0


def _wait_job(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/practice/generate/{job_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("生成 job 超时未完成")


class Ctx:
    """一个用户的测试上下文(app/client + 库内 user/task/results)"""

    def __init__(self, session_factory: sessionmaker, email: str, n_results: int):
        self.session_factory = session_factory
        self.app = FastAPI()
        self.app.include_router(practice_router.router)
        db = session_factory()
        self.user = User(email=email, password_hash="x")
        db.add(self.user)
        db.flush()
        self.task = Task(user_id=self.user.id, user_input="audit")
        db.add(self.task)
        db.flush()
        for i in range(n_results):
            db.add(Result(
                task_id=self.task.id, title=f"发现{i}", content="c",
                metadata_={"cwe": f"CWE-{89 + i}", "severity": "high"},
            ))
        db.commit()
        db.refresh(self.user)
        db.refresh(self.task)
        db.close()

        def override_get_db():
            s = session_factory()
            try:
                yield s
            finally:
                s.close()

        self.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(self.app)

    def login(self):
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        # SSE 端点用独立的必须登录依赖,一并覆盖
        self.app.dependency_overrides[get_current_user_sse] = lambda: self.user
        return self

    def logout(self):
        self.app.dependency_overrides.pop(get_current_user, None)
        self.app.dependency_overrides.pop(get_current_user_sse, None)
        return self


@pytest.fixture()
def ctx(test_engine, monkeypatch):
    factory = sessionmaker(bind=test_engine, autoflush=False)
    monkeypatch.setattr(practice_router, "SessionLocal", factory)
    monkeypatch.setattr(
        practice_router, "generate_questions_for_task", _fake_generate
    )
    return Ctx(factory, "stream-alice@test.local", n_results=3)


@pytest.fixture()
def ctx_b(test_engine):
    factory = sessionmaker(bind=test_engine, autoflush=False)
    return Ctx(factory, "stream-bob@test.local", n_results=1).login()


def _parse_sse(raw: str) -> list[tuple[str, dict | None]]:
    """解析 SSE 文本为 (event 类型, data) 列表(keep-alive 注释被跳过)"""
    events = []
    for block in raw.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        etype, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                etype = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if not data:
            continue
        events.append((etype, json.loads(data)))
    return events


# ============================================================
# jobs.py 单元行为(进程内事件日志)
# ============================================================


def test_append_event_seq_and_snapshot_fields():
    user_id = uuid.uuid4()
    job_id = gen_jobs.create_job(user_id, source="manual", task_title="t")
    try:
        gen_jobs.set_total(job_id, total=2)
        gen_jobs.append_event(job_id, "finding", {"index": 1, "total": 2, "title": "SQL注入"})
        gen_jobs.append_event(job_id, "token", {"delta": "hello "})
        gen_jobs.append_event(job_id, "token", {"delta": "world"})
        gen_jobs.append_event(job_id, "tool", {"name": "read_file", "summary": "read_file: a.py"})

        job = gen_jobs.get_job(job_id, user_id)
        types = [e["type"] for e in job["events"]]
        # set_total 也会产生 progress 事件
        assert types == ["progress", "finding", "token", "token", "tool"]
        seqs = [e["seq"] for e in job["events"]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        # snapshot 辅助字段
        snap = gen_jobs.snapshot(job_id, user_id)
        assert snap["current_finding"] == "SQL注入"
        assert snap["recent_text"] == "hello world"
        # 非 finding/token/tool 类型被拒绝
        gen_jobs.append_event(job_id, "bogus", {})
        assert gen_jobs.get_job(job_id, user_id)["events"][-1]["type"] == "tool"
    finally:
        with gen_jobs._LOCK:
            gen_jobs._JOBS.pop(job_id, None)


def test_append_event_restore_updates_snapshot():
    """restore 事件入事件流,并维护 snapshot 字段供中途接入兜底"""
    user_id = uuid.uuid4()
    job_id = gen_jobs.create_job(user_id)
    try:
        assert gen_jobs.snapshot(job_id, user_id)["restore"] is None
        gen_jobs.append_event(job_id, "restore", {"phase": "start"})
        gen_jobs.append_event(
            job_id, "restore", {"phase": "progress", "percent": 42, "message": "Receiving objects: 42%"},
        )
        snap = gen_jobs.snapshot(job_id, user_id)
        # snapshot 只保留最新一条 restore 状态
        assert snap["restore"] == {
            "phase": "progress", "percent": 42, "message": "Receiving objects: 42%",
        }
        # 事件流保留全部 restore 事件(供 SSE 重放)
        job = gen_jobs.get_job(job_id, user_id)
        assert [e["type"] for e in job["events"]] == ["restore", "restore"]
        # list_jobs 摘要同样携带 restore 字段(job 列表状态展示用)
        listed = [j for j in gen_jobs.list_jobs(user_id) if j["job_id"] == job_id]
        assert listed[0]["restore"]["phase"] == "progress"
    finally:
        with gen_jobs._LOCK:
            gen_jobs._JOBS.pop(job_id, None)


def test_recent_text_keeps_tail():
    user_id = uuid.uuid4()
    job_id = gen_jobs.create_job(user_id)
    try:
        gen_jobs.set_total(job_id, total=1)
        gen_jobs.append_event(job_id, "token", {"delta": "A" * 5000})
        gen_jobs.append_event(job_id, "token", {"delta": "B" * 100})
        snap = gen_jobs.snapshot(job_id, user_id)
        assert len(snap["recent_text"]) == 4000
        assert snap["recent_text"].endswith("B" * 100)
    finally:
        with gen_jobs._LOCK:
            gen_jobs._JOBS.pop(job_id, None)


def test_token_events_trimmed_over_cap():
    """超预算后从**左端**裁剪:留存事件仍是连续序号后缀

    旧行为是在锁内找第一个 token 事件再用 list.remove() 删(O(n),
    且中间挖空会让读端无法按后缀重放);现在统一从左侧丢最旧一条,
    代价是极长 job 可能连头部结构事件也被丢弃 —— 晚接入客户端本来
    就拿不到头部,由 snapshot(含 recent_text 尾部)兜底。
    开启 token 合并后单 job 事件数在百量级,该分支几乎不再触发。
    """
    user_id = uuid.uuid4()
    job_id = gen_jobs.create_job(user_id)
    try:
        gen_jobs.set_total(job_id, total=1)
        gen_jobs.append_event(job_id, "finding", {"index": 1, "total": 1, "title": "x"})
        # 灌入超过 64KB 的 token 增量
        for _ in range(70):
            gen_jobs.append_event(job_id, "token", {"delta": "T" * 1024})
        job = gen_jobs.get_job(job_id, user_id)
        token_chars = sum(
            len(e["data"]["delta"]) for e in job["events"] if e["type"] == "token"
        )
        assert token_chars <= 64 * 1024
        seqs = [e["seq"] for e in job["events"]]
        # 连续后缀:相邻序号差恒为 1,且最新的输出仍在
        assert all(b - a == 1 for a, b in zip(seqs, seqs[1:]))
        # 共发过 3 类 72 条事件:set_total 的 progress + 1 条 finding + 70 条 token
        assert seqs[-1] == 72
        assert job["events"][-1]["type"] == "token"
        # 读端仍可按 after_seq 增量重放(不会因为中间挖空而错位)
        res = gen_jobs.read_events(job_id, user_id, after_seq=seqs[0], timeout=0)
        assert [e["seq"] for e in res["events"]] == seqs[1:]
    finally:
        with gen_jobs._LOCK:
            gen_jobs._JOBS.pop(job_id, None)


def test_update_job_done_appends_terminal_event():
    user_id = uuid.uuid4()
    job_id = gen_jobs.create_job(user_id)
    try:
        gen_jobs.set_total(job_id, total=1)
        gen_jobs.update_job(job_id, status="done", created_count=2, skipped_findings=1)
        job = gen_jobs.get_job(job_id, user_id)
        assert job["events"][-1]["type"] == "done"
        assert job["events"][-1]["data"] == {"created": 2, "skipped": 1}
        # 终态后不再接受流式事件
        gen_jobs.append_event(job_id, "token", {"delta": "late"})
        assert gen_jobs.get_job(job_id, user_id)["events"][-1]["type"] == "done"
    finally:
        with gen_jobs._LOCK:
            gen_jobs._JOBS.pop(job_id, None)


def test_list_jobs_isolation_and_running_first():
    alice, bob = uuid.uuid4(), uuid.uuid4()
    a_running = gen_jobs.create_job(alice, source="auto")
    a_done = gen_jobs.create_job(alice, source="manual")
    b_job = gen_jobs.create_job(bob, source="manual")
    try:
        gen_jobs.set_total(a_done, total=1)
        gen_jobs.update_job(a_done, status="done")
        gen_jobs.set_total(b_job, total=1)

        jobs = gen_jobs.list_jobs(alice)
        assert [j["job_id"] for j in jobs] == [a_running, a_done]  # 运行中优先
        assert all(j["job_id"] != b_job for j in jobs)  # 跨用户隔离

        # read_events:终态 job 立即返回全部事件
        res = gen_jobs.read_events(a_done, alice, after_seq=0, timeout=0)
        assert res is not None
        assert res["events"][-1]["type"] == "done"
        # after_seq 过滤
        last_seq = res["events"][-1]["seq"]
        res2 = gen_jobs.read_events(a_done, alice, after_seq=last_seq, timeout=0)
        assert res2["events"] == []
        # 越权读取返回 None
        assert gen_jobs.read_events(a_done, bob, timeout=0) is None
        assert gen_jobs.snapshot(a_done, bob) is None
    finally:
        with gen_jobs._LOCK:
            for jid in (a_running, a_done, b_job):
                gen_jobs._JOBS.pop(jid, None)


# ============================================================
# API:jobs 列表端点 + SSE 流
# ============================================================


def test_jobs_endpoint_lists_and_isolates(ctx, ctx_b):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    job = _wait_job(ctx.client, job_id)
    assert job["status"] == "done"

    body = ctx.client.get("/practice/generate/jobs").json()
    assert len(body["jobs"]) == 1
    item = body["jobs"][0]
    assert item["job_id"] == job_id
    assert item["source"] == "manual"
    assert item["status"] == "done"
    assert item["created_count"] == 3
    assert item["task_title"] == "audit"  # 任务无 title 时回退 user_input
    assert item["task_id"] == str(ctx.task.id)

    # bob 看不到 alice 的 job
    assert ctx_b.client.get("/practice/generate/jobs").json()["jobs"] == []


def test_jobs_endpoint_requires_login(ctx):
    ctx.logout()
    assert ctx.client.get("/practice/generate/jobs").status_code == 401


def test_stream_snapshot_events_done(ctx):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job_id = r.json()["job_id"]
    _wait_job(ctx.client, job_id)

    with ctx.client.stream("GET", f"/practice/generate/{job_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())

    events = _parse_sse(raw)
    types = [t for t, _ in events]
    # snapshot 先行,done 收尾;含 finding/token/progress 事件
    assert types[0] == "snapshot"
    assert types[-1] == "done"
    assert "finding" in types and "token" in types and "progress" in types
    # snapshot 字段完整
    snap = events[0][1]
    assert snap["status"] == "done" and snap["total"] == 3
    assert snap["source"] == "manual"
    # fake 生成器每条 finding 发一个 token 事件,未被裁剪全部重放
    tokens = [d for t, d in events if t == "token"]
    assert [d["delta"] for d in tokens] == [
        "fake-output-0", "fake-output-1", "fake-output-2",
    ]
    # 终止事件携带计数
    assert events[-1][1] == {"created": 3, "skipped": 0}


def test_stream_unknown_job_404(ctx):
    ctx.login()
    r = ctx.client.get("/practice/generate/no-such-job/stream")
    assert r.status_code == 404


def test_stream_other_users_job_404(ctx, ctx_b):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job_id = r.json()["job_id"]
    _wait_job(ctx.client, job_id)
    # bob 访问 alice 的 job → 404(不泄露存在性)
    assert ctx_b.client.get(f"/practice/generate/{job_id}/stream").status_code == 404


def test_stream_requires_login(ctx):
    ctx.login()
    r = ctx.client.post("/practice/generate", json={"task_id": str(ctx.task.id)})
    job_id = r.json()["job_id"]
    _wait_job(ctx.client, job_id)
    ctx.logout()
    assert ctx.client.get(f"/practice/generate/{job_id}/stream").status_code == 401
