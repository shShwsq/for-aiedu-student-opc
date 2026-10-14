"""verifier_agent(动态验证)回归测试

覆盖此前无测的三块:
1. 结论申报通道:`submit_verification` 的工具回执 + 结构化结论写进 outcome_out
   (未申报 → unknown,fail-closed 不抬「已动态验证」档);
2. per_action 授权:超时按"未授权"处理且**首次超时后本轮不再逐个弹窗**;
   用户已选"后续自动允许"时跳过弹窗直接放行;
3. agent2 侧接线:verify 台账带 outcome,定论才给 verified 档。

执行方式:monkeypatch 模块级 `_stream_verifier_llm`(替身按脚本返回
reasoning/content/tool_calls/finish_reason),不打真 LLM、不打真沙箱。
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest

# 注册 TaskArtifact,Task mapper 才能初始化(verifier 落库 Conversation 会触发配置)
import app.models.task_artifact  # noqa: F401
import app.agents.verifier_agent as verifier_agent
from app.agents.evidence import EvidenceRecorder, score_review_item
from app.agents.verification_outcome import (
    OUTCOME_CONFIRMED,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_NOT_AUTHORIZED,
    OUTCOME_UNKNOWN,
    aggregate_targets,
    normalize_outcome,
    parse_outcome_line,
)


def _mk_task(auth_mode: str = "direct") -> MagicMock:
    task = MagicMock()
    task.id = uuid.uuid4()
    task.test_env_url = "http://test.local"
    task.verifier_auth_mode = auth_mode
    task.verifier_auth_tokens = []
    return task


def _script(*steps: tuple[str, list[dict]]):
    """按顺序产出 (content, tool_calls);最后一步的 tool_calls 为空即"总结完成"。

    tool_calls 元素形如 {"name": ..., "args": {...}}(自动补 id/index/arguments_str)。
    """
    it = iter(steps)

    def fake(client, messages, *, task_id, round_idx, iteration):
        try:
            content, calls = next(it)
        except StopIteration:
            content, calls = "验证完成", []
        return (
            "",
            content,
            [
                {
                    "id": f"call_{i}",
                    "index": i,
                    "name": c["name"],
                    "arguments_str": json.dumps(c["args"], ensure_ascii=False),
                }
                for i, c in enumerate(calls)
            ],
            "stop",
        )

    return fake


# ============================================================
# 结论申报通道
# ============================================================

def test_submit_verification_sets_confirmed_outcome(monkeypatch):
    monkeypatch.setattr(
        verifier_agent, "_stream_verifier_llm",
        _script(
            ("先打个 PoC", [{"name": "http_request",
                            "args": {"method": "GET", "path": "/api/users?id=1' OR 1=1--"}}]),
            ("申报结论", [{"name": "submit_verification", "args": {"targets": [
                {"target": "users.py SQL 注入", "result": "confirmed", "evidence": "200 + 全量表"}
            ]}}]),
            ("PoC 成功:返回了全部用户", []),
        ),
    )
    executed: list[dict] = []
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda name, args, *a, **k: executed.append({"tool": name})
        or {"status_code": 200, "body": "all rows"},
    )

    outcome: dict = {}
    text = verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证 users.py 的 SQL 注入",
        client=MagicMock(), round_idx=1, outcome_out=outcome,
    )

    assert "PoC 成功" in text
    assert outcome["outcome"] == OUTCOME_CONFIRMED
    assert outcome["targets"][0]["result"] == "confirmed"
    assert outcome["targets"][0]["definitive"] is True
    assert executed and executed[0]["tool"] == "http_request"


def test_max_iterations_still_publishes_end(monkeypatch):
    # 退出路径都不许漏推 phase=end(迭代耗尽与中止同理),否则实时卡片永久"正在验证"
    ends: list[int] = []
    monkeypatch.setattr(verifier_agent, "_publish_verify_end", lambda tid, ri: ends.append(ri))
    monkeypatch.setattr(
        verifier_agent, "_stream_verifier_llm",
        _script(*[("还在探", [{"name": "http_request",
                            "args": {"method": "GET", "path": f"/x{i}"}}])
                   for i in range(verifier_agent.MAX_VERIFIER_ITERATIONS)]),
    )
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda *a, **k: {"status_code": 200, "body": "ok"},
    )
    outcome: dict = {}
    text = verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证", client=MagicMock(), outcome_out=outcome,
    )
    assert "最大迭代" in text
    assert ends == [1]
    assert outcome["outcome"] == OUTCOME_UNKNOWN  # 一直没申报 → 不抬档


def test_truncation_without_output_retries_then_finishes(monkeypatch):
    """finish=length 且正文为空:不当作"验证完成",注入收尾提示重试后正常收尾"""
    ends: list[int] = []
    monkeypatch.setattr(verifier_agent, "_publish_verify_end", lambda tid, ri: ends.append(ri))

    seen: list[list[dict]] = []

    def fake(client, messages, *, task_id, round_idx, iteration):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return ("被截断的长思考……", "", [], "length")
        return ("", "PoC 不成立,普通参数也能拿到数据", [], "stop")

    monkeypatch.setattr(verifier_agent, "_stream_verifier_llm", fake)

    outcome: dict = {}
    text = verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证 SQL 注入", client=MagicMock(),
        round_idx=1, outcome_out=outcome,
    )

    # 零产出后必须重试(共 2 次流式调用),而不是第一次就静默收尾
    assert len(seen) == 2
    assert seen[1][-1]["role"] == "user"
    assert seen[1][-1]["content"] == verifier_agent.TRUNCATION_CONTINUE_PROMPT
    assert "普通参数" in text
    assert ends == [1]


def test_truncation_retry_is_bounded(monkeypatch):
    """始终零产出:重试有界,之后降级收尾而非无限循环"""
    monkeypatch.setattr(verifier_agent, "_publish_verify_end", lambda tid, ri: None)
    calls = {"n": 0}

    def fake(client, messages, *, task_id, round_idx, iteration):
        calls["n"] += 1
        return ("一直截断", "", [], "length")

    monkeypatch.setattr(verifier_agent, "_stream_verifier_llm", fake)
    outcome: dict = {}
    verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证", client=MagicMock(),
        round_idx=1, outcome_out=outcome,
    )
    assert calls["n"] == 1 + verifier_agent._MAX_TRUNCATION_RETRIES


def test_submit_verification_not_called_fails_closed(monkeypatch):
    # 模型没申报 → unknown:宁可不抬档,也不让"跑过"冒充"验出来"
    monkeypatch.setattr(
        verifier_agent, "_stream_verifier_llm",
        _script(
            ("发请求", [{"name": "http_request", "args": {"method": "GET", "path": "/x"}}]),
            ("看起来能注入", []),
        ),
    )
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda *a, **k: {"status_code": 200, "body": "ok"},
    )
    outcome: dict = {}
    verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证注入", client=MagicMock(), outcome_out=outcome,
    )
    assert outcome["outcome"] == OUTCOME_UNKNOWN


def test_prose_outcome_line_is_fallback(monkeypatch):
    # 没用工具,但正文按契约写了申报行 → 仍然采信
    monkeypatch.setattr(
        verifier_agent, "_stream_verifier_llm",
        _script(
            ("发请求", [{"name": "http_request", "args": {"method": "GET", "path": "/x"}}]),
            ("注入不成立,普通参数也能拿到全量数据\n[验证结论] outcome=not_confirmed", []),
        ),
    )
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda *a, **k: {"status_code": 200, "body": "same rows"},
    )
    outcome: dict = {}
    verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证越权", client=MagicMock(), outcome_out=outcome,
    )
    assert outcome["outcome"] == "not_confirmed"


def test_inconclusive_report_never_marked_confirmed(monkeypatch):
    # 申报了"看不出差异":整体结论未定论(模型没填 confirmed 就不会替它坐实)
    monkeypatch.setattr(
        verifier_agent, "_stream_verifier_llm",
        _script(
            ("申报", [{"name": "submit_verification", "args": {"targets": [
                {"target": "SSRF", "result": "inconclusive"}]}}]),
            ("响应看不出差异", []),
        ),
    )
    outcome: dict = {}
    verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证 SSRF", client=MagicMock(), outcome_out=outcome,
    )
    assert outcome["outcome"] == OUTCOME_INCONCLUSIVE


# ============================================================
# per_action 授权:超时 / 自动允许 / 中止(走真 user_interaction 状态机)
# ============================================================

@pytest.fixture
def clean_verify_state():
    """user_interaction 是模块级全局 dict,用完必清,防串到其它用例"""
    used: list[str] = []
    yield used
    from app import user_interaction
    for tid in used:
        user_interaction.clear_pending_verify_action(tid)


def test_per_action_timeout_blocks_action_and_stops_asking(monkeypatch, clean_verify_state):
    """授权超时:动作不执行,且本轮后续动作不再逐个弹窗白等"""
    from app import user_interaction

    task = _mk_task(auth_mode="per_action")
    clean_verify_state.append(str(task.id))

    asked: list[dict] = []
    monkeypatch.setattr(
        user_interaction, "request_verify_authorization",
        lambda tid, desc: asked.append(desc),
    )
    executed: list[str] = []
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda name, *a, **k: executed.append(name) or {"status_code": 200},
    )
    # 授权时限压到 0:任何弹窗立即判超时(不真等几分钟)
    monkeypatch.setattr(verifier_agent, "_auth_wait_seconds", lambda _d: 0.0)
    monkeypatch.setattr(verifier_agent, "_stream_verifier_llm", _script(
        ("先探 A", [{"name": "http_request", "args": {"method": "GET", "path": "/a"}}]),
        ("再探 B", [{"name": "http_request", "args": {"method": "GET", "path": "/b"}}]),
        ("结束", []),
    ))

    outcome: dict = {}
    verifier_agent.run_verifier_agent(
        task, MagicMock(), "验证", client=MagicMock(), outcome_out=outcome,
    )

    assert executed == []                                # 两个动作都没跑
    assert len(asked) == 1                               # 首次超时后不再逐个弹窗
    assert outcome["outcome"] == OUTCOME_NOT_AUTHORIZED   # 一个没跑成 → 未授权,不是不成立


def test_per_action_auto_approve_skips_popup(monkeypatch, clean_verify_state):
    """用户选了"本次及后续":同一 task 之后的动作直接放行,不再弹窗"""
    from app import user_interaction

    task = _mk_task(auth_mode="per_action")
    clean_verify_state.append(str(task.id))

    # 先用真 API 建立一个待授权动作,并按"同意 + 记住"提交
    action = {"action_id": "a-setup", "type": "http_request", "method": "GET", "url": "http://test.local/a"}
    user_interaction.request_verify_authorization(str(task.id), action)
    assert user_interaction.submit_verify_authorization(
        str(task.id), "a-setup", True, remember=True
    ) is True

    def _boom(*_a, **_k):
        raise AssertionError("已自动允许时不该再弹窗")

    monkeypatch.setattr(user_interaction, "request_verify_authorization", _boom)
    executed: list[str] = []
    monkeypatch.setattr(
        verifier_agent, "_execute_verifier_tool",
        lambda name, *a, **k: executed.append(name) or {"status_code": 200, "body": "ok"},
    )
    monkeypatch.setattr(verifier_agent, "_stream_verifier_llm", _script(
        ("探测", [{"name": "http_request", "args": {"method": "GET", "path": "/a"}}]),
        ("完成", []),
    ))

    outcome: dict = {}
    verifier_agent.run_verifier_agent(
        task, MagicMock(), "验证", client=MagicMock(), outcome_out=outcome,
    )
    assert executed == ["http_request"]


def test_abort_check_ends_verification_early(monkeypatch):
    """审查被终止/被新一轮取代:verifier 立即收手,结论记 aborted"""
    monkeypatch.setattr(verifier_agent, "_stream_verifier_llm", _script(("不该走到这", [])))
    # 退出必须配套推 phase=end:开始事件已推过,不推就在前端留一张永不结束的"正在验证"卡
    ends: list[int] = []
    monkeypatch.setattr(verifier_agent, "_publish_verify_end", lambda tid, ri: ends.append(ri))
    outcome: dict = {}
    text = verifier_agent.run_verifier_agent(
        _mk_task(), MagicMock(), "验证", client=MagicMock(), round_idx=3,
        outcome_out=outcome, abort_check=lambda: True,
    )
    assert "验证中止" in text
    assert outcome["outcome"] == "aborted"
    assert ends == [3]


def test_wait_for_authorization_timeout_clears_pending(monkeypatch):
    """wait_for_authorization 自己:超时返回 timeout 并推 resolve 事件收起弹窗"""
    from app import user_interaction

    tid = str(uuid.uuid4())
    try:
        resolved: list[dict] = []
        monkeypatch.setattr(
            user_interaction, "_publish_verify_resolved",
            lambda t, aid, reason: resolved.append({"action_id": aid, "reason": reason}),
        )
        user_interaction.request_verify_authorization(tid, {"action_id": "a1", "type": "http_request"})
        assert user_interaction.wait_for_authorization(tid, "a1", timeout=0.05) == "timeout"
        assert resolved == [{"action_id": "a1", "reason": "timeout"}]
        # 超时后 pending 已清:前端不会再恢复一个没人答复的弹窗
        assert user_interaction.get_pending_verify_action(tid) is None
        # 事后补交的决议无人接收(幂等,不报错)
        assert user_interaction.submit_verify_authorization(tid, "a1", True) is False
    finally:
        user_interaction.clear_pending_verify_action(tid)


def test_wait_for_authorization_approve_path():
    from app import user_interaction

    tid = str(uuid.uuid4())
    try:
        user_interaction.request_verify_authorization(tid, {"action_id": "a2", "type": "http_request"})
        assert user_interaction.submit_verify_authorization(tid, "a2", True) is True
        assert user_interaction.wait_for_authorization(tid, "a2", timeout=1) == "approved"
        assert user_interaction.verify_auto_approved(tid) is False  # 未勾"后续"
    finally:
        user_interaction.clear_pending_verify_action(tid)


# ============================================================
# 纯函数口径
# ============================================================

def test_aggregate_targets_takes_weakest_link():
    """一批目标里只要有一条没定论,整批就不算"已动态验证"""
    # (引用号是"一次 verify"的粒度,审查项指不到那批里的某一条)
    assert aggregate_targets([
        {"result": "confirmed"}, {"result": "inconclusive"},
    ]) == OUTCOME_INCONCLUSIVE
    assert aggregate_targets([
        {"result": "confirmed"}, {"result": "not_confirmed"},
    ]) == OUTCOME_CONFIRMED
    assert aggregate_targets([
        {"result": "not_confirmed"}, {"result": "not_confirmed"},
    ]) == "not_confirmed"
    assert aggregate_targets([{"result": "unreachable"}]) == OUTCOME_INCONCLUSIVE
    assert aggregate_targets([]) == OUTCOME_UNKNOWN
    assert aggregate_targets([{"result": "大概能注入"}]) == OUTCOME_UNKNOWN


def test_normalize_outcome_rejects_garbage():
    assert normalize_outcome("已确认") == OUTCOME_CONFIRMED
    assert normalize_outcome("false_positive") == "not_confirmed"
    assert normalize_outcome("大概能注入") == OUTCOME_UNKNOWN
    assert parse_outcome_line("正文… [验证结论] outcome=confirmed") == OUTCOME_CONFIRMED
    assert parse_outcome_line("没有申报行") is None


def test_auth_wait_seconds_respects_review_deadline(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "VERIFY_AUTH_TIMEOUT_SECONDS", 300, raising=False)
    assert verifier_agent._auth_wait_seconds(None) == 300.0
    # 硬时限只剩 10s:不等满配置上限
    assert verifier_agent._auth_wait_seconds(_monotonic_now() + 10) == pytest.approx(10, abs=1)
    # 已到时限:0 秒(不弹一个没人看的窗)
    assert verifier_agent._auth_wait_seconds(_monotonic_now() - 1) == 0.0


def _monotonic_now() -> float:
    import time
    return time.monotonic()


# ============================================================
# agent2 接线:台账 outcome → 置信档
# ============================================================

def test_ledger_outcome_drives_verified_tier():
    rec = EvidenceRecorder()
    body = rec.record_verify("PoC 成功\n[验证结论] outcome=inconclusive",
                             verify_ok=True, outcome=OUTCOME_INCONCLUSIVE)
    assert "[evidence_ref=E1]" in body
    out = score_review_item(
        {"verification": {"verified": True, "call_ref": "E1"}},
        "confirmed", rec.ledger, has_repo=False, verify_available=True,
    )
    assert out["tier"] == "assertion_only"      # 未定论不抬档
    assert out["evidence_mismatch"] is False     # 也不是谎报引用

    rec2 = EvidenceRecorder()
    rec2.record_verify("PoC 成功", verify_ok=True, outcome=OUTCOME_CONFIRMED)
    out2 = score_review_item(
        {"verification": {"verified": True, "call_ref": "E1"}},
        "confirmed", rec2.ledger, has_repo=False, verify_available=True,
    )
    assert out2["tier"] == "verified"
