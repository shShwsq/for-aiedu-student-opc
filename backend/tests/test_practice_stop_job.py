"""出题进度侧栏"停止出题 / 继续出题"后端契约

覆盖三条链:
1. services/practice/jobs.py:停止标志 + cancelled 终态
   (终态集合必须同时被"追加终止事件 / 忽略后续流式事件 / 读端收尾"三处认到,
   漏一处就让 SSE 客户端等不到终止事件永久挂着)
2. services/practice/generator.py:should_stop 检查点
   - 逐条 finding 之间停止:已生成的 draft **先 commit 再返回**(不白烧已付 token)
   - 并发路径停止:所有已跑完的 worker 照常落库(含刚触发检查点的那一条)
   - 停止落在最后一条之后:讲解照常收尾(与 job 层 is_cancelled 同口径)
   - 出题开始前(主题分类后 / 克隆阶段)停止:抛 PracticeGenerateCancelled
   - 克隆取消检查点透传到 sandbox_tools(否则停止按钮在克隆阶段完全不生效)
3. routers/practice.py:POST /practice/generate/{job_id}/stop 的 200/404/409
   (explain job 不查停止标志 → 409,不给假的 200)
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.models.task_artifact  # noqa: F401  Task mapper 要能解析 TaskArtifact 关联
import app.models.user_git_binding  # noqa: F401  User.git_bindings 反向关系
import app.services.practice.generator as gen
from app.routers.practice import stop_generate_job
from app.services.practice import jobs
from app.services.practice.generator import PracticeGenerateCancelled
from app.services.practice.jobs import create_job, snapshot, update_job


def _raw(**overrides):
    q = {
        "qtype": "single_choice",
        "stem": "该代码存在哪种漏洞?",
        "code_snippet": "cursor.execute(sql)",
        "options": ["SQL 注入", "XSS", "CSRF", "无漏洞"],
        "answer_idx": 0,
        "explanation": "拼接 SQL 导致注入",
        "difficulty": 3,
        "knowledge_key": "CWE-89",
        "knowledge_name": "SQL 注入",
    }
    q.update(overrides)
    return q


def _gen_db(finding_count=3, settings_row=None):
    """最小 mock db:Result 查询返回 N 条 finding,其余查询走空集

    settings_row 非空时作为 PracticeSettings 查询结果返回(要跑并发路径或
    开启收尾讲解时必须传,默认 None = 串行 + 无讲解开关)。
    """
    findings = [
        SimpleNamespace(
            id=f"r{i}",
            title=f"SQL 注入风险 {i}",
            content="cursor.execute(sql) 直接拼接用户输入构造查询",
            metadata_={"cwe": "CWE-89"},
        )
        for i in range(finding_count)
    ]
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is gen.PracticeSettings:
            q.filter.return_value.first.return_value = settings_row
        elif model is gen.Result:
            q.filter.return_value.order_by.return_value.all.return_value = findings
        elif model is gen.KnowledgePoint:
            q.filter.return_value.first.return_value = None
        elif getattr(model, "name", "") == "source_result_id":
            q.filter.return_value.all.return_value = []
        else:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def _gen_task():
    return SimpleNamespace(
        id="t1", params={"repo_url": "https://example.com/r.git"}, scenario=None,
    )


def _settings(concurrency=1, explain=False):
    """练习设置行(字段与真模同名称;并发路径需要 generate_concurrency>1)"""
    return SimpleNamespace(
        restore_workspace_for_practice=False,
        thinking_mode_for_practice="follow",
        generate_explanation_with_questions=explain,
        explain_llm_config_id=None,
        generate_concurrency=concurrency,
    )


# ============================================================
# 1. jobs:停止标志与 cancelled 终态
# ============================================================


def test_request_stop_sets_flag_and_marks_summary():
    job_id = create_job("u1", source="manual", task_id="t1")
    update_job(job_id, status="running")

    assert jobs.request_stop(job_id, "u1") == "running"
    assert jobs.is_stop_requested(job_id) is True
    # 尚未进终态:状态仍是 running,但 summary 带上"正在停止"供侧栏展示
    snap = snapshot(job_id, "u1")
    assert snap["status"] == "running"
    assert snap["stop_requested"] is True


def test_request_stop_is_idempotent_and_scoped_to_owner():
    job_id = create_job("u1")
    assert jobs.request_stop(job_id, "u1") is not None
    assert jobs.request_stop(job_id, "u1") is not None
    # 别人的 job:找不到(不泄漏存在性)
    assert jobs.request_stop(job_id, "u2") is None


def test_request_stop_on_terminal_job_returns_status_without_flag():
    """已结束的 job 不需要停止:返回当前终态,调用方据此回 409"""
    job_id = create_job("u1")
    update_job(job_id, status="done", created_count=1)
    assert jobs.request_stop(job_id, "u1") == "done"
    assert jobs.is_stop_requested(job_id) is False


def test_cancelled_terminal_appends_cancelled_event_with_counts():
    """cancelled 必须像 done 一样自动追加终止事件(载荷含已生成数与断点进度)"""
    job_id = create_job("u1")
    update_job(job_id, status="running", done=2, total=5)
    update_job(job_id, status="cancelled", created_count=3, skipped_findings=1)

    res = jobs.read_events(job_id, "u1", after_seq=0, timeout=0)
    last = res["events"][-1]
    assert last["type"] == "cancelled"
    assert last["data"]["created"] == 3
    assert last["data"]["skipped"] == 1
    assert last["data"]["done"] == 2 and last["data"]["total"] == 5
    assert res["job"]["status"] == "cancelled"
    assert jobs.is_terminal_status("cancelled") is True


def test_stream_events_ignored_after_cancelled():
    """终态后不再追加流式事件(SSE 收尾后不能被继续灌内容)"""
    job_id = create_job("u1")
    update_job(job_id, status="cancelled")
    before = [e["seq"] for e in jobs.read_events(job_id, "u1")["events"]]
    jobs.append_event(job_id, "token", {"delta": "late"})
    after = [e["seq"] for e in jobs.read_events(job_id, "u1")["events"]]
    assert after == before


def test_read_events_reports_finished_for_cancelled():
    """读端把 cancelled 当终态:否则 SSE 生成器永远等不到收尾,连接永久悬挂"""
    job_id = create_job("u1")
    update_job(job_id, status="cancelled")
    res = jobs.read_events(job_id, "u1", after_seq=10**6, timeout=0)
    assert res["events"] == []
    assert res["job"]["status"] == "cancelled"


def test_summary_carries_request_params_for_continue():
    """「继续出题」要沿用同一上限/重出开关:这两个字段必须在 summary 里"""
    job_id = create_job("u1", task_id="t1", max_findings=7, force_regenerate=True)
    snap = snapshot(job_id, "u1")
    assert snap["max_findings"] == 7
    assert snap["force_regenerate"] is True


def test_is_cancelled_distinguishes_mid_run_stop_from_after_finished():
    """请求落在最后一条之后(全部已处理)时不算取消:按 done 收口,不误报已停止"""
    stopped_early = create_job("u1")
    update_job(stopped_early, status="running", done=1, total=5)
    assert jobs.request_stop(stopped_early, "u1") == "running"
    assert jobs.is_cancelled(stopped_early, "u1") is True

    finished = create_job("u1")
    update_job(finished, status="running", done=5, total=5)
    jobs.request_stop(finished, "u1")
    assert jobs.is_stop_requested(finished) is True      # 标志确实设过
    assert jobs.is_cancelled(finished, "u1") is False    # 但没少跑活 → done
    # 跳用户查不到(不泄漏存在性)
    assert jobs.is_cancelled(finished, "u2") is False


# ============================================================
# 2. generator:停止检查点
# ============================================================


def test_stop_mid_loop_keeps_and_commits_generated_questions(monkeypatch):
    """逐条之间停止:第一条的题照常 commit 返回,第二条起不再付 LLM 成本"""
    calls = {"n": 0}

    def _fake_call_llm(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一条出完题,用户此刻按下停止
            stop["flag"] = True
        return json.dumps([_raw(stem=f"第{calls['n']}题")], ensure_ascii=False)

    stop = {"flag": False}
    monkeypatch.setattr(gen, "_call_llm", _fake_call_llm)
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    db = _gen_db(finding_count=3)

    created, skipped = gen.generate_questions_for_task(
        db, _gen_task(), "u1", client=MagicMock(),
        should_stop=lambda: stop["flag"],
    )

    assert calls["n"] == 1                 # 后面的 finding 一条都没再跑
    assert len(created) == 1               # 已生成的题保留
    assert db.commit.called                # 而且确实落了库(不是随 session 丢掉)


def test_concurrent_stop_keeps_future_that_triggered_checkpoint(monkeypatch):
    """并发停止:刚被 as_completed 产出的那条 future 已经跑完,必须照常落库

    旧写法用 `done_fut is not fut` 把它排除在落库之外 —— 每次停止恰好白丢
    一道已付过 LLM 成本的题,与两行之上的注释"不接收等于白烧"自相矛盾。

    两条 worker 都先睡再起(保证两条都已起跑、不会在起跑前被 cancel):
    谁先醒谁就拿到 n=1 并拉下停止标志,因而主线程第一次检查必然命中停止
    分支,且此时另一条要么仍在途(旧写法收到 0 条)要么刚跟上(旧写法只收到
    它这一条)——两种情况下"第1题"都会缺失。
    """
    import threading as _threading
    import time as _time

    lock = _threading.Lock()
    state = {"calls": 0, "flag": False}

    def _fake_call_llm(*args, **kwargs):
        _time.sleep(0.15)
        with lock:
            state["calls"] += 1
            n = state["calls"]
        if n == 1:
            state["flag"] = True          # 第一条跑完就按下停止
        return json.dumps([_raw(stem=f"第{n}题")], ensure_ascii=False)

    monkeypatch.setattr(gen, "_call_llm", _fake_call_llm)
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    db = _gen_db(finding_count=2, settings_row=_settings(concurrency=2))

    created, _skipped = gen.generate_questions_for_task(
        db, _gen_task(), "u1", client=MagicMock(),
        should_stop=lambda: state["flag"],
    )

    # 旧写法这里拿不到"第1题"(触发检查点的那条被排除)
    assert "第1题" in {q.stem for q in created}
    assert db.commit.called
    # 两条都真的进了 LLM:确认走的是并发分支且未被提前 cancel
    # (串行下第二条根本不会被调用)
    assert state["calls"] == 2


def test_stop_after_last_finding_keeps_explain_phase(monkeypatch):
    """停止落在最后一条之后:题已出齐,知识点讲解不能被静默跳过

    generator 的 stopped_early 与 job 层的 is_cancelled 必须同一口径(都要求
    "确实少跑了活")。旧写法只看标志:job 那边按 done 收口,这边的讲解却因
    stopped_early=True 被跳过 —— 用户看到"已完成"却少了知识点讲解。
    """
    state = {"flag": False}
    explained = []

    def _fake_call_llm(*args, **kwargs):
        state["flag"] = True              # 唯一一条正在跑时用户点了停止
        return json.dumps([_raw()], ensure_ascii=False)

    monkeypatch.setattr(gen, "_call_llm", _fake_call_llm)
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen, "resolve_explain_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        gen, "explain_knowledge_points",
        lambda db, uid, digests, **kw: explained.append(len(digests)) or len(digests),
    )
    db = _gen_db(finding_count=1, settings_row=_settings(explain=True))

    created, _skipped = gen.generate_questions_for_task(
        db, _gen_task(), "u1", client=MagicMock(),
        should_stop=lambda: state["flag"],
    )

    assert len(created) == 1
    assert explained, "全部 finding 已处理完时应照常收尾讲解"


def test_stop_after_topic_classification_raises_cancelled(monkeypatch):
    """出题开始前停止:没有题可保留 → 冒 PracticeGenerateCancelled(不是 error)"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen, "_call_llm", lambda *a, **k: json.dumps([_raw()]))

    with pytest.raises(PracticeGenerateCancelled):
        gen.generate_questions_for_task(
            db=_gen_db(), task=_gen_task(), user_id="u1", client=MagicMock(),
            should_stop=lambda: True,
        )


def test_cancelled_is_not_a_fatal_error():
    """取消不能被 `except PracticeGenerateError` 当成失败(否则 job 会标 error)"""
    assert not issubclass(PracticeGenerateCancelled, gen.PracticeGenerateError)


def test_restore_phase_cancel_reaches_clone_checkpoint(monkeypatch):
    """工作区恢复把 cancel_check 传给克隆:否则大仓库克隆阶段停止完全不生效"""
    from app.tools.sandbox_tools import CloneCancelledError

    seen = {}

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None,
                   progress_callback=None, cancel_check=None):
        seen["cancel_check"] = cancel_check
        raise CloneCancelledError("调用方已取消克隆: r")

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", fake_clone)
    db = MagicMock()
    settings_row = SimpleNamespace(restore_workspace_for_practice=True)
    task = SimpleNamespace(id="t1", params={"repo_url": "https://x/r.git"}, user_id="u1")
    check = lambda: True  # noqa: E731

    with pytest.raises(PracticeGenerateCancelled):
        gen._ensure_workspace(db, task, settings_row, git_tokens={}, cancel_check=check)

    assert seen["cancel_check"] is check


def test_ensure_workspace_without_cancel_check_not_passed(monkeypatch):
    """不传 cancel_check 时不下传(存量克隆替身的兼容面)"""
    seen = {}

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None,
                   progress_callback=None):
        seen["ok"] = True
        raise RuntimeError("克隆失败也要走完调用")

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", fake_clone)
    settings_row = SimpleNamespace(restore_workspace_for_practice=True)
    task = SimpleNamespace(id="t2", params={"repo_url": "https://x/r.git"}, user_id="u1")

    info = gen._ensure_workspace(MagicMock(), task, settings_row, git_tokens={})

    assert info is None and seen.get("ok") is True


def test_clone_stop_reason_prefers_external_cancel():
    """外部取消优先于"跳过预克隆"一次性标志(不能被误消费)"""
    from app.tools.sandbox_tools import _clone_stop_reason
    import app.clone_skip as clone_skip

    tid = "t-clone-reason"
    clone_skip.clear_skip_state(tid)
    try:
        assert _clone_stop_reason(tid, True, None) == ""
        clone_skip.request_skip_clone(tid)
        # cancel_check 命中:返回 cancelled,且**不消费**跳过标志
        assert _clone_stop_reason(tid, False, lambda: True) == "cancelled"
        assert clone_skip.is_skip_requested(tid) is True
        # cancellable 路径消费跳过标志
        assert _clone_stop_reason(tid, True, lambda: False) == "skipped"
        assert clone_skip.is_skip_requested(tid) is False
    finally:
        clone_skip.clear_skip_state(tid)


# ============================================================
# 3. 路由:POST /practice/generate/{job_id}/stop
# ============================================================


def _user(uid="u-stop"):
    return SimpleNamespace(id=uid)


def test_stop_endpoint_accepts_running_job():
    job_id = create_job("u-stop", source="manual", task_id="t1")
    update_job(job_id, status="running")

    resp = stop_generate_job(job_id, current_user=_user())

    assert resp["status"] == "running"
    assert "停止" in resp["message"]
    assert jobs.is_stop_requested(job_id) is True


def test_stop_endpoint_rejects_unknown_job():
    import fastapi

    with pytest.raises(fastapi.HTTPException) as exc:
        stop_generate_job("no-such-job", current_user=_user())
    assert exc.value.status_code == 404


def test_stop_endpoint_rejects_finished_job():
    import fastapi

    job_id = create_job("u-stop")
    update_job(job_id, status="done", created_count=2)
    with pytest.raises(fastapi.HTTPException) as exc:
        stop_generate_job(job_id, current_user=_user())
    assert exc.value.status_code == 409


def test_stop_endpoint_rejects_explain_job():
    """知识点讲解 job:执行线程不查停止标志,接了只会回一个假的 200 → 409"""
    import fastapi

    job_id = create_job("u-stop", source="explain", task_title="知识点讲解")
    update_job(job_id, status="running")
    with pytest.raises(fastapi.HTTPException) as exc:
        stop_generate_job(job_id, current_user=_user())
    assert exc.value.status_code == 409
    # 标志不落:不能因为一次被拒的请求把后续同 id 的判定带偏
    assert jobs.is_stop_requested(job_id) is False
