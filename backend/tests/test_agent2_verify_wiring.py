"""agent2 × 动态验证接线回归测试(额度联动 + 结论落台账)

覆盖三件事:
1. `_verify_budget`:额度随审查计划里声明 planned_evidence=verify 的条目数联动,
   下限 3、上限 settings.AGENT2_MAX_VERIFY_CALLS(旧固定 3 让 8 项计划里只有 3 项
   能拿到定论,是"助手看着不肯核实"的真实成因);
2. verify 的结构化结论进证据台账(`outcome`),而不是只看"跑没跑成";
3. 超出本轮额度时静默跳过,并把实际额度回灌给模型(它得知道还能不能试)。
"""
import json
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  注册 TaskArtifact,Task mapper 才初始化
import app.agents.agent2 as agent2
import app.agents.verifier_agent as verifier_agent
from app.agents.agent2 import run_agent2
from app.config import settings

VERIFY_CALL = {
    "id": "call_v", "index": 0, "name": "verify",
    "arguments_str": '{"verification_request": "验证 SQL 注入"}',
}


def _mk_task():
    task = MagicMock()
    task.id = "task-verify-wiring"
    task.verifier_enabled = True
    task.test_env_url = "http://localhost/test"
    return task


def _final_json():
    return json.dumps({
        "covered": [], "missing": [], "reasoning": "ok", "suggestions": [],
        "results": [{"title": "k", "content": "c"}], "grouping": None,
    }, ensure_ascii=False)


def _plan_payload(n_verify: int) -> dict:
    return {
        "items": [
            {"target": f"疑点 {i}", "origin": "agent1_claim", "planned_evidence": "verify"}
            for i in range(n_verify)
        ]
    }


def _drive(monkeypatch, *, plan_items: int, verify_calls: int, outcome: str | None = None):
    """跑一次 run_agent2:先发计划(定额度),再连发 verify_calls 次 verify"""
    calls: list[str] = []
    seen_tool_contents: list[str] = []

    def _stub(task, db, request, client, round_idx, **kwargs):
        calls.append(request)
        box = kwargs.get("outcome_out")
        if outcome and isinstance(box, dict):
            box["outcome"] = outcome
            box["targets"] = [{"target": request, "result": outcome}]
        return "PoC 结果文本"

    monkeypatch.setattr(verifier_agent, "run_verifier_agent", _stub)

    state = {"n": 0}

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        state["n"] += 1
        if state["n"] == 1:
            return ("", [{
                "id": "call_p", "index": 0, "name": "submit_review_plan",
                "arguments_str": json.dumps(_plan_payload(plan_items), ensure_ascii=False),
            }], "先规划")
        if state["n"] <= 1 + verify_calls:
            return ("", [dict(VERIFY_CALL, id=f"call_v{state['n']}")], "想验证")
        # 收尾:这一轮把前面所有工具回灌正文一次取走(含拒收时的额度提示)
        for m in messages:
            if m.get("role") == "tool":
                seen_tool_contents.append(str(m.get("content")))
        return (_final_json(), [], "")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "s"}],
        task_id="task-verify-wiring", db=None, round_idx=1,
        client=MagicMock(), task=_mk_task(),
        agent_policy={"allow_verify": True},
    )
    return result, calls, seen_tool_contents


# ============================================================
# 额度联动
# ============================================================

def test_verify_budget_floor_and_cap(monkeypatch):
    monkeypatch.setattr(settings, "AGENT2_MAX_VERIFY_CALLS", 8, raising=False)
    assert agent2._verify_budget({}) == 3                      # 没计划:下限
    assert agent2._verify_budget({"plan": {"items": []}}) == 3
    assert agent2._verify_budget(
        {"plan": {"items": [{"planned_evidence": "verify"}] * 5}}
    ) == 6                                                     # 5 条计划 + 1 余量
    assert agent2._verify_budget(
        {"plan": {"items": [{"planned_evidence": "verify"}] * 30}}
    ) == 8                                                     # 封顶
    # 只读/引用类的计划项不占验证额度
    assert agent2._verify_budget(
        {"plan": {"items": [{"planned_evidence": "source"}] * 9}}
    ) == 3


def test_verify_budget_zero_cap_falls_back_to_floor(monkeypatch):
    # 配置成 0 = 回落到旧行为(固定 3 次),不是"一次都不许验"
    monkeypatch.setattr(settings, "AGENT2_MAX_VERIFY_CALLS", 0, raising=False)
    assert agent2._verify_budget(
        {"plan": {"items": [{"planned_evidence": "verify"}] * 9}}
    ) == 3


def test_run_agent2_honors_plan_scaled_budget(monkeypatch):
    # 计划声明 5 项要跑 PoC → 本轮真能连发 5 次 verify(旧固定 3 会在第 4 次就拒收)
    monkeypatch.setattr(settings, "AGENT2_MAX_VERIFY_CALLS", 8, raising=False)
    result, calls, contents = _drive(monkeypatch, plan_items=5, verify_calls=5)
    assert len(calls) == 5
    assert not any("已达验证次数额度" in c for c in contents)
    verify_entries = [e for e in (result.get("_evidence_ledger") or {}).values()
                      if e.get("kind") == "verify"]
    assert len(verify_entries) == 5


def test_run_agent2_skips_beyond_budget(monkeypatch):
    # 计划只声明 0 项 PoC(额度=3):第 4 次被拒,且把实际额度告诉模型
    monkeypatch.setattr(settings, "AGENT2_MAX_VERIFY_CALLS", 8, raising=False)
    _result, calls, contents = _drive(monkeypatch, plan_items=0, verify_calls=4)
    assert len(calls) == 3
    skipped = [c for c in contents if "已达验证次数额度" in c]
    assert skipped and "(3," in skipped[0]


def test_budget_exhausted_uses_effective_budget():
    # 对账口径要与循环里的实际额度一致:6 次触顶 = 额度用尽,不是"没排到"
    assert agent2._budget_exhausted(
        {"tool_counts": {"read": 0, "verify": 6, "reference": 0}, "verify_budget": 6}
    ) is True
    assert agent2._budget_exhausted(
        {"tool_counts": {"read": 0, "verify": 6, "reference": 0}, "verify_budget": 8}
    ) is False


# ============================================================
# 结论进台账
# ============================================================

def test_run_agent2_records_verifier_outcome(monkeypatch):
    monkeypatch.setattr(settings, "AGENT2_MAX_VERIFY_CALLS", 8, raising=False)
    result, _calls, contents = _drive(monkeypatch, plan_items=1, verify_calls=1,
                                      outcome="confirmed")
    entry = [e for e in (result.get("_evidence_ledger") or {}).values()
             if e.get("kind") == "verify"][0]
    assert entry["outcome"] == "confirmed"
    assert entry["verify_ok"] is True
    # 结论档位随文本回灌给模型(它据此决定要不要写 verified=true)
    assert any("outcome=confirmed" in c for c in contents)


def test_run_agent2_unreported_outcome_is_unknown(monkeypatch):
    # 替身不申报结论(老契约/不合规模型)→ unknown,不白送「已动态验证」
    monkeypatch.setattr(settings, "AGENT2_MAX_VERIFY_CALLS", 8, raising=False)
    result, _calls, _contents = _drive(monkeypatch, plan_items=1, verify_calls=1,
                                       outcome=None)
    entry = [e for e in (result.get("_evidence_ledger") or {}).values()
             if e.get("kind") == "verify"][0]
    assert entry["outcome"] == "unknown"
    assert entry["verify_ok"] is True
