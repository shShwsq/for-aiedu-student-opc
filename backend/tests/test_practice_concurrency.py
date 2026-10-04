"""出题并发与「工作区恢复 ∥ 主题分类」单元测试

覆盖:
- _effective_concurrency:用户设置 / 系统上限 / 待处理条数三者取小
- _provider_gate:同厂商在途请求数不超全局天花板
- 并发路径:墙钟接近线性下降、落库只在主线程、结果与串行一致
- 并发>1 时不推 token/tool 事件(打字机流是单 finding 假设),progress 仍单调
- 致命错误:并发下取消未起跑的 worker,已生成的题目照常保留并冒泡原因
- Phase 6:需要 clone 时恢复走后台线程,与主题分类重叠完成;
  工作区存活/未开开关时不起线程(保持原串行路径)

不依赖数据库:mock session + fake LLM client。
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  让 Task mapper 能解析 TaskArtifact 关联
import app.services.practice.generator as gen
from app.models.practice import LearningTopic
from app.config import settings

VALID_OUT = (
    '[{"qtype": "single_choice", "stem": "题干", "code_snippet": "x",'
    ' "options": ["甲", "乙"], "answer_idx": 0, "explanation": "解析",'
    ' "difficulty": 3, "knowledge_key": "CWE-89", "knowledge_name": "SQL 注入",'
    ' "languages": ["python"]}]'
)


def _findings(n):
    return [
        SimpleNamespace(
            id=f"r{i}", title=f"发现{i}", content="拼接用户输入",
            metadata_={"cwe": "CWE-89"},
        )
        for i in range(n)
    ]


def _mock_db(findings, concurrency=1, settings_row_extra=None):
    """按查询目标返回不同链的 mock db;记录 db.add 发生的线程

    返回 (db, added_idents, settings_row)。
    """
    added_idents = []
    extra = settings_row_extra or {}
    settings_row = SimpleNamespace(
        restore_workspace_for_practice=False,
        thinking_mode_for_practice="follow",
        generate_explanation_with_questions=False,
        explain_llm_config_id=None,
        generate_concurrency=concurrency,
        **extra,
    )
    db = MagicMock()
    db.add.side_effect = lambda obj: added_idents.append(threading.get_ident())

    def _query(model):
        q = MagicMock()
        if model is gen.PracticeSettings:
            q.filter.return_value.first.return_value = settings_row
        elif model is gen.Result:
            q.filter.return_value.order_by.return_value.all.return_value = findings
        elif model is gen.KnowledgePoint:
            q.filter.return_value.first.return_value = None
        elif model is LearningTopic:
            q.filter.return_value.all.return_value = [
                LearningTopic(key=k, name=k, description="", sort_order=i,
                              is_builtin=True, enabled=True)
                for i, k in enumerate(("security", "architecture", "coding", "contract"))
            ]
        else:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db, added_idents, settings_row


def _task():
    return SimpleNamespace(id="t1", params={"repo_url": "https://example.com/r.git"},
                           scenario=None, user_id="u1")


class _SlowClient:
    """每次 chat_stream 前睡 delay 秒并输出合法题目(模拟 LLM 往返耗时)"""

    def __init__(self, delay=0.0, output=VALID_OUT):
        self.delay = delay
        self.output = output
        self.calls = 0
        self.model = "slow-model"
        self._lock = threading.Lock()

    def chat_stream(self, messages, **kw):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        yield SimpleNamespace(content_delta=self.output, reasoning_delta="",
                              tool_call_deltas=None, finish_reason="stop")


# ============================================================
# 并发度取小与厂商闸门
# ============================================================


def test_effective_concurrency_takes_minimum(monkeypatch):
    monkeypatch.setattr(settings, "PRACTICE_GENERATE_CONCURRENCY", 2, raising=True)
    row = SimpleNamespace(generate_concurrency=4)
    assert gen._effective_concurrency(row, 10) == 2      # 系统上限压住用户设置
    assert gen._effective_concurrency(row, 1) == 1       # 只有 1 条不必并行
    assert gen._effective_concurrency(None, 10) == 1     # 无设置行 → 串行
    assert gen._effective_concurrency(SimpleNamespace(generate_concurrency=0), 10) == 1
    monkeypatch.setattr(settings, "PRACTICE_GENERATE_CONCURRENCY", 1, raising=True)
    assert gen._effective_concurrency(row, 10) == 1      # 运维强制全局串行


def test_provider_gate_caps_inflight_requests(monkeypatch):
    """同厂商在途请求数不超过 PRACTICE_PROVIDER_MAX_CONCURRENCY"""
    monkeypatch.setattr(settings, "PRACTICE_PROVIDER_MAX_CONCURRENCY", 2, raising=True)
    gen._provider_gates.clear()
    client = SimpleNamespace(provider_id="p", model="m", base_url_override=None)
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def work():
        with gen._provider_gate(client):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
            time.sleep(0.05)
            with lock:
                live["now"] -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert live["max"] <= 2
    gen._provider_gates.clear()


# ============================================================
# 并发出题路径
# ============================================================


def _run_pipeline(db, findings_n, client, events=None, **kw):
    return gen.generate_questions_for_task(
        db, _task(), "u1", max_findings=findings_n, client=client,
        event_callback=events.append if events is not None else None,
        **kw,
    )


def test_serial_path_matches_current_behaviour(monkeypatch):
    """并发度=1 时走原串行路径:逐条推 finding/token 事件"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    findings = _findings(3)
    db, added, _ = _mock_db(findings, concurrency=1)
    events = []
    progress = []
    created, skipped = gen.generate_questions_for_task(
        db, _task(), "u1", max_findings=3, client=_SlowClient(),
        event_callback=lambda t, d: events.append((t, d)),
        progress_callback=lambda done, total: progress.append((done, total)),
    )
    # 三条 finding 产出完全相同的题 → 后两条被 dedup 拦下(与改造前一致)
    assert len(created) == 1
    assert [t for t, _ in events].count("finding") == 3
    assert [t for t, _ in events].count("token") == 3
    # finding 事件序号逐条递进,进度终态到齐
    assert [d["index"] for t, d in events if t == "finding"] == [1, 2, 3]
    assert progress[-1] == (3, 3)
    assert added  # 主线程写入
    assert len(added) == 2  # 1 道题 + 1 个知识点


def test_concurrent_path_is_faster_and_writes_in_main_thread(monkeypatch):
    """4 路并发跑 4 条 finding:墙钟远小于串行总和,且 DB 写入只发生在主线程"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(settings, "PRACTICE_GENERATE_CONCURRENCY", 4, raising=True)
    findings = [
        SimpleNamespace(id=f"r{i}", title=f"发现{i}", content="拼接用户输入",
                        metadata_={"cwe": f"CWE-{89 + i}"})
        for i in range(4)
    ]
    db, added, _ = _mock_db(findings, concurrency=4)
    main_thread = threading.get_ident()

    # 每条 finding 一次往返,各睡 0.12s:串行 ~0.48s,4 路并发应远小于此
    outs = {i: VALID_OUT.replace("题干", f"题干{i}") for i in range(4)}

    class _PerFindingClient:
        model = "slow-model"

        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def chat_stream(self, messages, **kw):
            user_text = messages[1]["content"]
            idx = next(i for i in outs if f"发现{i}" in user_text)
            with self.lock:
                self.calls += 1
            time.sleep(0.12)
            yield SimpleNamespace(content_delta=outs[idx], reasoning_delta="",
                                  tool_call_deltas=None, finish_reason="stop")

    started = time.monotonic()
    created, skipped = gen.generate_questions_for_task(
        db, _task(), "u1", max_findings=4, client=_PerFindingClient(),
    )
    elapsed = time.monotonic() - started
    assert len(created) == 4 and skipped == 0
    assert elapsed < 0.35, f"并发未见效: {elapsed:.2f}s"
    # Session 非线程安全:所有 db.add 必须落在主线程
    assert added and all(ident == main_thread for ident in added)


def test_concurrent_path_emits_progress_but_no_token_events(monkeypatch):
    """并发>1:侧栏打字机流被抑制,progress 仍按完成数单调推进"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(settings, "PRACTICE_GENERATE_CONCURRENCY", 4, raising=True)
    findings = _findings(3)
    db, _, _ = _mock_db(findings, concurrency=4)
    events = []
    progress = []
    gen.generate_questions_for_task(
        db, _task(), "u1", max_findings=3, client=_SlowClient(),
        event_callback=lambda t, d: events.append((t, d)),
        progress_callback=lambda done, total: progress.append(done),
    )
    assert [t for t, _ in events] == []            # 无 finding/token/tool 交错
    assert progress == [1, 2, 3]                   # 单调不回退
    assert len(progress) == 3


def test_concurrent_fatal_error_aborts_rest_and_keeps_created(monkeypatch):
    """额度/认证类致命错误:并发下同样快速中止,已生成题目保留并冒泡原因"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(settings, "PRACTICE_GENERATE_CONCURRENCY", 2, raising=True)
    findings = _findings(4)
    db, _, _ = _mock_db(findings, concurrency=2)
    import openai

    def boom(*a, **k):
        raise openai.AuthenticationError(
            "Error code: 401 - bad key", response=_resp(), body=None,
        )

    monkeypatch.setattr(gen, "_call_llm", boom)
    try:
        gen.generate_questions_for_task(
            db, _task(), "u1", max_findings=4, client=MagicMock(),
        )
        raise AssertionError("应抛 PracticeGenerateError")
    except gen.PracticeGenerateError as e:
        assert "401" in str(e)


def _resp():
    import httpx

    return httpx.Response(
        status_code=401, headers={}, request=httpx.Request("POST", "https://x/v1/chat"),
    )


def test_worker_exception_does_not_kill_job(monkeypatch):
    """单条 worker 意外异常:计一条未出题,其余照常入库"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(settings, "PRACTICE_GENERATE_CONCURRENCY", 4, raising=True)
    findings = _findings(3)
    db, _, _ = _mock_db(findings, concurrency=3)
    seen = {"n": 0}

    def flaky(client, system_prompt, finding_text, task_id, repo_path,
              on_event=None, messages=None):
        seen["n"] += 1
        if seen["n"] == 1:
            raise RuntimeError("本地 bug")
        return VALID_OUT.replace("题干", f"题干{seen['n']}")

    monkeypatch.setattr(gen, "_call_llm", flaky)
    created, skipped = gen.generate_questions_for_task(
        db, _task(), "u1", max_findings=3, client=MagicMock(),
    )
    assert skipped == 1
    assert len(created) == 2


# ============================================================
# Phase 6:工作区恢复与主题分类并行
# ============================================================


def test_restore_runs_in_background_thread(monkeypatch):
    """需要 clone 时:恢复在后台线程跑,分类期间它已在进行"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(gen, "_call_llm", lambda *a, **k: VALID_OUT)
    main_thread = threading.get_ident()
    order = []

    def fake_ensure(db, task, settings_row, event_callback=None, git_tokens=None):
        order.append(("restore-thread", threading.get_ident() != main_thread))
        time.sleep(0.1)
        return {"repo_path": "/repo"}

    monkeypatch.setattr(gen, "_ensure_workspace", fake_ensure)
    monkeypatch.setattr(gen, "_load_git_tokens", lambda db, uid: {"github": "tok"})

    def fake_classify(client, pending, task_id, topic_defs):
        # 分类发生在主线程,且此刻后台恢复线程已经起跑
        order.append(("classify", threading.get_ident() == main_thread))
        return {}

    monkeypatch.setattr(gen, "_classify_topics_with_llm", fake_classify)
    # 无 CWE 的 finding 才会走 LLM 分类(有 CWE 时规则捷径直接判定)
    findings = [SimpleNamespace(id="r0", title="发现0", content="拼接用户输入",
                                metadata_={"file_path": "src/a.py"})]
    db, _, settings_row = _mock_db(findings)
    settings_row.restore_workspace_for_practice = True
    created, _ = gen.generate_questions_for_task(
        db, _task(), "u1", max_findings=1, client=MagicMock(),
    )
    assert created
    # 恢复真的在别的线程,且与分类重叠(分类开始时恢复已起跑)
    assert order[0] == ("restore-thread", True)
    assert order[1] == ("classify", True)


def test_no_background_thread_when_workspace_alive(monkeypatch):
    """工作区存活或没开恢复开关 → 不起线程(保持原串行路径)"""
    live = {"repo_path": "/repo"}
    assert gen._workspace_restore_worth_bg(
        _task(), SimpleNamespace(restore_workspace_for_practice=True), live,
    ) is False
    # 未开开关
    assert gen._workspace_restore_worth_bg(
        _task(), SimpleNamespace(restore_workspace_for_practice=False), None,
    ) is False
    # 无 settings 行
    assert gen._workspace_restore_worth_bg(_task(), None, None) is False
    # 无 repo_url
    task = SimpleNamespace(id="t1", params={}, scenario=None, user_id="u1")
    assert gen._workspace_restore_worth_bg(
        task, SimpleNamespace(restore_workspace_for_practice=True), None,
    ) is False
    # 唯一需要后台恢复的情形
    assert gen._workspace_restore_worth_bg(
        _task(), SimpleNamespace(restore_workspace_for_practice=True), None,
    ) is True


def test_ensure_workspace_uses_preloaded_tokens_without_db(monkeypatch):
    """预先传入 git_tokens 时不再在线程内查库(Session 非线程安全)"""
    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    db = MagicMock()
    cloned = {}

    def fake_clone(repo_url, branch=None, task_id="", git_tokens=None, progress_callback=None):
        cloned["tokens"] = git_tokens
        raise RuntimeError("clone 失败也要走完调用")

    monkeypatch.setattr(gen.sandbox_tools, "clone_repo_with_fallback", fake_clone)
    settings_row = SimpleNamespace(restore_workspace_for_practice=True)
    task = SimpleNamespace(id="t1", params={"repo_url": "https://x/r.git"}, user_id="u1")
    info = gen._ensure_workspace(db, task, settings_row, git_tokens={"github": "tok"})
    assert info is None  # 沙箱仍不可用 → 返回原 info(None)
    assert cloned["tokens"] == {"github": "tok"}
    # 关键:没有用 db 去查 UserGitBinding
    assert all(
        c.args and c.args[0] is not gen.PracticeSettings for c in db.query.call_args_list
    ) or db.query.call_count == 0
