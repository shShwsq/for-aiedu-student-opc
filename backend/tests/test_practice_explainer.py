"""知识点讲解生成(explainer)单元测试

覆盖(不依赖数据库:mock session + fake LLM client):
- 上下文素材裁剪与 metadata 白名单(make_source_digest)
- 同知识点多条上下文的截断与展示名/主题更新(add_digest)
- 讲解输出解析:纯 JSON / 散文包 JSON 挽救 / 非法输出
- 覆盖规则:manual 永不覆盖,已有 auto 需 force,空讲解可直接写
- 批量调用次数按 ≤8 个知识点一批;单批失败不影响其它批
- 只写入本次请求的知识点,不动别的 KP;整批一次 commit
- 出题管线收尾:开关开启才汇素材并调用讲解生成,讲解异常不拖垮题目
- 讲解模型解析:explain_llm_config_id > 出题模型;配置失效回退
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  让 Task mapper 能解析 TaskArtifact 关联
import app.services.practice.explainer as ex
from app.models.practice import (
    EXPLANATION_SOURCE_AUTO,
    EXPLANATION_SOURCE_MANUAL,
    KnowledgePoint,
)
from app.models.task import Result
from app.services.practice.explainer import (
    _parse_explanations,
    _should_write,
    add_digest,
    build_digests_from_db,
    explain_knowledge_points,
    make_source_digest,
)


def _kp(key="CWE-89", name="SQL 注入", explanation=None, source="", model=""):
    return KnowledgePoint(
        user_id="u1", key=key, name=name, learning_topic="security",
        explanation=explanation, explanation_source=source, explanation_model=model,
    )


def _digest(key="CWE-89", topic="security", name="SQL 注入"):
    return {
        key: _digest_entry(key, topic=topic, name=name)
    }


def _digest_entry(key="CWE-89", topic="security", name="SQL 注入"):
    return {
        "knowledge_key": key,
        "knowledge_name": name,
        "learning_topic": topic,
        "sources": [make_source_digest(
            "SQL 注入风险", "cursor.execute(sql) 拼接输入",
            {"cwe": "CWE-89", "file_path": "src/a.py"},
            [{"path": "src/a.py", "lines": "40-46", "content": "40  cursor.execute(sql)"}],
            [{"stem": "存在哪种漏洞", "code_snippet": "cursor.execute(sql)",
              "options": ["SQL 注入", "XSS"], "answer_idx": 0,
              "explanation": "拼接导致注入", "origin": "repo",
              "source_file": "src/a.py", "source_lines": "40-46"}],
        )],
    }


class _FakeLLMClient:
    """按调用次序返回预设文本,并记录每次收到的 messages"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.model = "fake-model"

    def chat_stream(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        text = self.outputs.pop(0) if self.outputs else "[]"
        return iter([SimpleNamespace(content_delta=text, reasoning_delta="",
                                     tool_call_deltas=None, finish_reason="stop")])


# ============================================================
# 素材组装
# ============================================================


def test_make_source_digest_keeps_metadata_whitelist_and_clips():
    src = make_source_digest(
        "标题", "很长" * 2000,
        {"cwe": "CWE-89", "learning_note": "记住参数化", "内部字段": "_grouping",
         "severity": "high", "noise": "x" * 500},
        [{"path": "a.py", "lines": "1-2", "content": "y" * 5000}],
        [{"stem": "题", "code_snippet": "c", "options": ["甲", "乙"],
          "answer_idx": 1, "explanation": "解析", "origin": "repo",
          "source_file": "a.py", "source_lines": "1"}],
    )
    assert set(src["metadata"]) == {"cwe", "learning_note", "severity"}
    assert len(src["finding_content"]) <= ex._FINDING_CONTENT_CHARS
    assert len(src["materials"][0]["content"]) <= ex._MATERIAL_CHARS
    # 题目摘要用"正确项文本"而不是下标,模型不必再猜答案位置
    assert src["questions"][0]["answer"] == "乙"
    assert src["questions"][0]["source"] == "a.py 1"


def test_add_digest_caps_sources_and_refreshes_name_topic():
    digests = {}
    for i in range(ex.MAX_SOURCES_PER_KP + 3):
        add_digest(digests, "CWE-89", f"名字{i}", "security", {"n": i})
    entry = digests["CWE-89"]
    assert len(entry["sources"]) == ex.MAX_SOURCES_PER_KP
    # 超出上限时丢最旧的,保留最近的出题上下文
    assert entry["sources"][0]["n"] == 3
    assert entry["knowledge_name"] == f"名字{ex.MAX_SOURCES_PER_KP + 2}"
    add_digest(digests, "CWE-89", "", "architecture", {"n": 99})
    assert digests["CWE-89"]["learning_topic"] == "architecture"


def test_build_digests_from_db_groups_by_finding(monkeypatch):
    """DB 重建:题按知识点截断,同源题合成一份上下文,无题知识点被剔除"""
    kp1 = _kp("CWE-89", "SQL 注入")
    kp1.id = "k1"
    kp2 = _kp("CWE-79", "XSS")
    kp2.id = "k2"
    questions = [
        SimpleNamespace(
            id=f"q{i}", knowledge_point_id=kp1.id, source_result_id="r1",
            stem=f"题干{i}", options=["甲", "乙"], answer_idx=0,
            explanation="解析", code_snippet="cursor.execute(sql)",
            origin="repo", source_file="src/a.py", source_lines=f"{i}-{i}",
        )
        for i in range(ex.MAX_QUESTIONS_PER_KP + 2)
    ]
    questions.append(SimpleNamespace(
        id="qx", knowledge_point_id=kp2.id, source_result_id=None,
        stem="无来源题", options=["甲", "乙"], answer_idx=1,
        explanation="解析", code_snippet=None, origin="synthetic",
        source_file=None, source_lines=None,
    ))
    finding = SimpleNamespace(
        id="r1", title="SQL 注入风险", content="直接拼接用户输入",
        metadata_={"cwe": "CWE-89", "learning_note": "记住参数化"},
    )

    def _query(model):
        q = MagicMock()
        if model is KnowledgePoint:
            q.filter.return_value.all.return_value = [kp1, kp2]
        elif model is Result:
            q.filter.return_value.all.return_value = [finding]
        else:  # Question(多一段 order_by 链)
            q.filter.return_value.order_by.return_value.all.return_value = questions
        return q

    db = MagicMock()
    db.query.side_effect = _query
    digests = build_digests_from_db(db, "u1", ["CWE-89", "CWE-79", "不存在"])
    assert set(digests) == {"CWE-89", "CWE-79"}
    # 两个配额同时生效:每个知识点最多 MAX_QUESTIONS_PER_KP 道题,
    # 每份来源上下文最多 MAX_QUESTIONS_PER_SOURCE 道
    sources89 = digests["CWE-89"]["sources"]
    total_q = sum(len(s["questions"]) for s in sources89)
    assert total_q <= ex.MAX_QUESTIONS_PER_KP
    assert all(len(s["questions"]) <= ex.MAX_QUESTIONS_PER_SOURCE for s in sources89)
    # 同源题合成一份上下文,并带上当初的审计发现原文(不只题目本身)
    src89 = digests["CWE-89"]["sources"][0]
    assert src89["finding_title"] == "SQL 注入风险"
    assert src89["metadata"]["learning_note"] == "记住参数化"
    assert digests["CWE-79"]["sources"][0]["finding_title"] == ""


def test_build_digests_from_db_empty_keys():
    assert build_digests_from_db(MagicMock(), "u1", []) == {}


# ============================================================
# 输出解析与覆盖规则
# ============================================================


def test_parse_explanations_plain_and_prose_wrapped():
    body = json.dumps([{"knowledge_key": "CWE-89", "markdown": "### 是什么\n内容"}],
                      ensure_ascii=False)
    assert _parse_explanations(body)["CWE-89"].startswith("### 是什么")
    prose = "我先梳理一下:\n1. 参数化\n\n输出:\n" + body
    assert "CWE-89" in _parse_explanations(prose)
    assert _parse_explanations("") == {}
    assert _parse_explanations("完全不是 JSON") == {}
    # 空 markdown 视为该知识点放弃
    assert _parse_explanations('[{"knowledge_key":"CWE-89","markdown":"  "}]') == {}


def test_should_write_rules():
    assert _should_write(_kp(explanation=None, source=""), force=False) is True
    assert _should_write(_kp(explanation="旧讲解", source=EXPLANATION_SOURCE_AUTO), force=False) is False
    assert _should_write(_kp(explanation="旧讲解", source=EXPLANATION_SOURCE_AUTO), force=True) is True
    # 手工编辑永不被自动覆盖
    assert _should_write(_kp(explanation="我的总结", source=EXPLANATION_SOURCE_MANUAL), force=True) is False


# ============================================================
# 批量生成
# ============================================================


def _mock_db_with_kps(kps):
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.all.return_value = kps
    db.query.return_value = q
    return db


def test_explain_writes_only_targets_and_clips_markdown():
    kp = _kp()
    db = _mock_db_with_kps([kp])
    out = json.dumps(
        [{"knowledge_key": "CWE-89", "markdown": "### 是什么\n" + "长" * 9000}],
        ensure_ascii=False,
    )
    client = _FakeLLMClient([out])
    written = explain_knowledge_points(db, "u1", _digest(), client=client)
    assert written == 1
    assert len(kp.explanation) == ex.MAX_EXPLANATION_CHARS
    assert kp.explanation_source == EXPLANATION_SOURCE_AUTO
    assert kp.explanation_model == "fake-model"
    assert kp.explanation_updated_at is not None
    assert db.commit.call_count == 1


def test_explain_batches_at_most_eight_kp_per_call():
    """11 个知识点分 2 批(≤8/批),而不是 11 次往返;两批都写入"""
    keys = [f"CWE-{i}" for i in range(11)]
    kps = [_kp(key=k) for k in keys]
    digests = {k: _digest_entry(k) for k in keys}
    db = _mock_db_with_kps(kps)
    batches_seen = []

    def fake_stream(messages, **kw):
        payload = json.loads(_user_json(messages[1]["content"]))
        batches_seen.append([it["knowledge_key"] for it in payload])
        text = json.dumps(
            [{"knowledge_key": it["knowledge_key"], "markdown": "### 是什么\nx"}
             for it in payload],
            ensure_ascii=False,
        )
        return iter([SimpleNamespace(content_delta=text, reasoning_delta="",
                                     tool_call_deltas=None, finish_reason="stop")])

    client = _FakeLLMClient([])
    client.chat_stream = fake_stream
    written = explain_knowledge_points(db, "u1", digests, client=client)
    assert written == 11
    assert [len(b) for b in batches_seen] == [8, 3]
    assert all(kp.explanation_source == EXPLANATION_SOURCE_AUTO for kp in kps)


def _user_json(user_prompt: str) -> str:
    """从 build_explain_user_prompt 的文本里取出素材 JSON"""
    head = user_prompt.index("[")
    tail = user_prompt.rindex("]") + 1
    return user_prompt[head:tail]


def test_explain_skips_manual_and_missing_kp():
    manual = _kp("CWE-89", explanation="我的总结", source=EXPLANATION_SOURCE_MANUAL)
    db = _mock_db_with_kps([manual])
    client = _FakeLLMClient(["[]"])
    written = explain_knowledge_points(db, "u1", _digest(), client=client, force=True)
    assert written == 0
    assert manual.explanation == "我的总结"
    # 一次 LLM 调用都不该发(没有可写目标就早退)
    assert client.calls == []


def test_explain_batch_failure_does_not_break_caller():
    kp = _kp()
    db = _mock_db_with_kps([kp])

    client = _FakeLLMClient([])

    def boom(messages, **kw):
        raise RuntimeError("限流")

    client.chat_stream = boom
    # 单批异常被吞掉(只记日志),返回 0 而不是抛出
    assert explain_knowledge_points(db, "u1", _digest(), client=client) == 0


def test_explain_events_report_progress():
    kp = _kp()
    events = []
    client = _FakeLLMClient([json.dumps(
        [{"knowledge_key": "CWE-89", "markdown": "### 是什么\n内容"}], ensure_ascii=False,
    )])
    explain_knowledge_points(
        _mock_db_with_kps([kp]), "u1", _digest(), client=client,
        event_callback=lambda t, d: events.append((t, d)),
    )
    assert [e[0] for e in events] == ["explain", "explain"]
    assert events[0][1]["phase"] == "start" and events[0][1]["total"] == 1
    assert events[1][1]["phase"] == "done" and events[1][1]["written"] == 1


def test_explain_no_digest_is_noop():
    db = MagicMock()
    assert explain_knowledge_points(db, "u1", {}, client=_FakeLLMClient([])) == 0
    assert db.query.call_count == 0


# ============================================================
# 出题管线收尾 + 讲解模型解析
# ============================================================


def test_pipeline_tail_collects_generator_context(monkeypatch):
    """开关开启:讲解素材带着发现原文、预读材料与已生成题目(出题上下文)"""
    import app.services.practice.generator as gen

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    captured = {}

    def fake_explain(db, user_id, digests, **kw):
        captured["digests"] = digests
        captured["kw"] = kw
        return 1

    monkeypatch.setattr(gen, "explain_knowledge_points", fake_explain)
    monkeypatch.setattr(
        gen, "_call_llm",
        lambda *a, **k: json.dumps([{
            "qtype": "single_choice", "stem": "存在哪种漏洞",
            "code_snippet": "cursor.execute(sql)",
            "options": ["SQL 注入", "XSS"], "answer_idx": 0,
            "explanation": "拼接", "difficulty": 3,
            "knowledge_key": "CWE-89", "knowledge_name": "SQL 注入",
            "languages": ["python"], "origin": "repo",
            "source_file": "src/a.py", "source_lines": "40-46",
        }]),
    )
    findings = [SimpleNamespace(
        id="r0", title="SQL 注入风险", content="直接拼接用户输入",
        metadata_={"cwe": "CWE-89", "learning_note": "记住参数化查询"},
    )]
    settings_row = SimpleNamespace(
        restore_workspace_for_practice=False,
        thinking_mode_for_practice="follow",
        generate_explanation_with_questions=True,
        explain_llm_config_id=None,
        generate_concurrency=1,
    )
    db = _pipeline_db(settings_row, findings)
    created, _ = gen.generate_questions_for_task(
        db, _pipeline_task(), "u1", client=MagicMock(),
    )
    assert created
    digest = captured["digests"]["CWE-89"]
    source = digest["sources"][0]
    # 讲解输入必须带出题当时的上下文,而不是只有 key/name
    assert source["finding_title"] == "SQL 注入风险"
    assert source["metadata"]["learning_note"] == "记住参数化查询"
    assert source["questions"][0]["stem"] == "存在哪种漏洞"
    assert digest["learning_topic"] == "security"


def test_pipeline_skips_explain_when_switch_off(monkeypatch):
    """开关关闭:一次讲解调用都不发(默认零额外成本)"""
    import app.services.practice.generator as gen

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    called = []
    monkeypatch.setattr(
        gen, "explain_knowledge_points",
        lambda *a, **k: called.append(1) or 0,
    )
    monkeypatch.setattr(
        gen, "_call_llm", lambda *a, **k: json.dumps([{
            "qtype": "true_false", "stem": "该写法安全吗", "code_snippet": None,
            "options": ["正确", "错误"], "answer_idx": 1,
            "explanation": "拼接不安全", "difficulty": 2,
            "knowledge_key": "CWE-89", "knowledge_name": "SQL 注入", "languages": [],
        }]),
    )
    settings_row = SimpleNamespace(
        restore_workspace_for_practice=False,
        thinking_mode_for_practice="follow",
        generate_explanation_with_questions=False,
        explain_llm_config_id=None,
        generate_concurrency=1,
    )
    db = _pipeline_db(settings_row, [SimpleNamespace(
        id="r0", title="T", content="C", metadata_={"cwe": "CWE-89"},
    )])
    created, _ = gen.generate_questions_for_task(
        db, _pipeline_task(), "u1", client=MagicMock(),
    )
    assert created
    assert called == []


def test_pipeline_explain_failure_keeps_questions(monkeypatch):
    """讲解生成炸了只记日志:已生成的题目照常返回"""
    import app.services.practice.generator as gen

    monkeypatch.setattr(gen.sandbox_tools, "get_workspace_info", lambda tid: None)
    monkeypatch.setattr(
        gen, "explain_knowledge_points",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")),
    )
    monkeypatch.setattr(
        gen, "_call_llm", lambda *a, **k: json.dumps([{
            "qtype": "single_choice", "stem": "题干", "code_snippet": "x",
            "options": ["甲", "乙"], "answer_idx": 0, "explanation": "解析",
            "difficulty": 3, "knowledge_key": "CWE-89",
            "knowledge_name": "SQL 注入", "languages": ["python"],
        }]),
    )
    settings_row = SimpleNamespace(
        restore_workspace_for_practice=False, thinking_mode_for_practice="follow",
        generate_explanation_with_questions=True, explain_llm_config_id=None,
        generate_concurrency=1,
    )
    db = _pipeline_db(settings_row, [SimpleNamespace(
        id="r0", title="T", content="C", metadata_={"cwe": "CWE-89"},
    )])
    created, _ = gen.generate_questions_for_task(
        db, _pipeline_task(), "u1", client=MagicMock(),
    )
    assert len(created) == 1


def _pipeline_task():
    return SimpleNamespace(
        id="t1", params={"repo_url": "https://example.com/r.git"}, scenario=None,
    )


def _pipeline_db(settings_row, findings):
    """按查询目标返回不同链的 mock db(出题管线用)"""
    import app.services.practice.generator as gen
    from app.models.practice import LearningTopic

    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is gen.PracticeSettings:
            q.filter.return_value.first.return_value = settings_row
        elif model is gen.Result:
            q.filter.return_value.order_by.return_value.all.return_value = findings
        elif model is gen.KnowledgePoint:
            q.filter.return_value.first.return_value = None
        elif model is LearningTopic:
            # 全内置词表启用(含全部内置 key,不触发播种)
            q.filter.return_value.all.return_value = [
                LearningTopic(key=k, name=k, description="", sort_order=i,
                              is_builtin=True, enabled=True)
                for i, k in enumerate(("security", "architecture", "coding", "contract"))
            ]
        else:
            q.filter.return_value.all.return_value = []
        return q

    db.query.side_effect = _query
    return db


def test_resolve_explain_client_prefers_dedicated_config(monkeypatch):
    import app.services.practice.generator as gen

    monkeypatch.setattr(
        gen, "_client_from_config_id",
        lambda db, user_id, cid: SimpleNamespace(model=cid),
    )
    fallback = SimpleNamespace(model="出题模型")
    got = gen.resolve_explain_client(
        MagicMock(), "u1", SimpleNamespace(explain_llm_config_id="cfg-explain"),
        fallback,
    )
    assert got.model == "cfg-explain"
    # 未配置讲解模型 → 沿用出题模型
    same = gen.resolve_explain_client(
        MagicMock(), "u1", SimpleNamespace(explain_llm_config_id=None), fallback,
    )
    assert same is fallback
    # 配置失效(查不到)→ 静默回退
    monkeypatch.setattr(gen, "_client_from_config_id", lambda db, u, cid: None)
    back = gen.resolve_explain_client(
        MagicMock(), "u1", SimpleNamespace(explain_llm_config_id="gone"), fallback,
    )
    assert back is fallback


def test_resolve_explain_client_for_user_falls_back_to_env(monkeypatch):
    """按需路径没有 task:讲解专用 > 默认出题模型 > env 默认"""
    import app.services.practice.generator as gen

    seen = []
    monkeypatch.setattr(
        gen, "_client_from_config_id",
        lambda db, user_id, cid: seen.append(cid) or SimpleNamespace(model=cid),
    )
    got = gen.resolve_explain_client_for_user(
        MagicMock(), "u1",
        SimpleNamespace(explain_llm_config_id="cfg-x", default_llm_config_id="cfg-d"),
    )
    assert got.model == "cfg-x"
    assert seen == ["cfg-x"]

    got2 = gen.resolve_explain_client_for_user(
        MagicMock(), "u1",
        SimpleNamespace(explain_llm_config_id=None, default_llm_config_id="cfg-d"),
    )
    assert got2.model == "cfg-d"

    class _Env:
        def __init__(self, *a, **k):
            self.model = "env-default"

    monkeypatch.setattr(gen, "LLMClient", _Env)
    got3 = gen.resolve_explain_client_for_user(
        MagicMock(), "u1",
        SimpleNamespace(explain_llm_config_id=None, default_llm_config_id=None),
    )
    assert got3.model == "env-default"
