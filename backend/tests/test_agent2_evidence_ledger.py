"""agent2 证据台账集成测试(run_agent2 内取证调用 → E# 引用注入 + 台账旁路附带)

对应实施计划 Part A + Test Plan `test_agent2_evidence_ledger`:
- 替身触发一次 read_file → 台账含 E1、回灌给下一次 LLM 的 tool 结果带 _evidence_ref;
- verify 替身返回自然语言文本 → 末尾注入 [evidence_ref=E#] 且台账 kind=verify。
"""
import json
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  注册 TaskArtifact,Task mapper 才初始化
import app.agents.agent2 as agent2
import app.agents.verifier_agent as verifier_agent
from app.agents.agent2 import run_agent2


def _mk_task():
    task = MagicMock()
    task.id = "task-ledger"
    task.verifier_enabled = True
    task.test_env_url = "http://localhost/test"
    return task


def _final_json():
    return json.dumps({
        "covered": [], "missing": [], "reasoning": "ok", "suggestions": [],
        "results": [{"title": "k", "content": "c"}], "grouping": None,
    }, ensure_ascii=False)


def _read_tool_call():
    return [{
        "id": "call_1", "index": 0, "name": "read_file",
        "arguments_str": '{"file_path": "src/api/users.py"}',
    }]


def test_run_agent2_read_injects_evidence_ref(monkeypatch):
    monkeypatch.setattr(
        agent2, "_execute_read_tool",
        lambda *a, **k: json.dumps({"path": "src/api/users.py", "content": "1\tcursor.execute(sql)"}),
    )
    state = {"n": 0}
    seen_tool_contents: list[str] = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        state["n"] += 1
        if state["n"] >= 2:
            # 第二次调用:上一轮 read 工具结果已回灌,应带 _evidence_ref
            for m in messages:
                if m.get("role") == "tool":
                    seen_tool_contents.append(str(m.get("content")))
            return (_final_json(), [], "")
        return ("", _read_tool_call(), "想读源码")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "agent1 总结"}],
        task_id="task-ledger", db=None, round_idx=1,
        client=MagicMock(), task=_mk_task(), repo_path="/repo",
    )

    ledger = result.get("_evidence_ledger") or {}
    assert "E1" in ledger
    assert ledger["E1"]["kind"] == "read"
    # 回灌给下一次 LLM 的 tool 结果带 _evidence_ref 字段
    assert any('"_evidence_ref"' in c or "_evidence_ref" in c for c in seen_tool_contents)


def test_run_agent2_verify_injects_text_marker(monkeypatch):
    # verify 结果走 verifier_agent(自然语言文本),替身返回成功文本
    monkeypatch.setattr(
        verifier_agent, "run_verifier_agent",
        lambda *a, **k: "PoC 成功:确认 SQL 注入可利用",
    )
    state = {"n": 0}
    seen_tool_contents: list[str] = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        state["n"] += 1
        if state["n"] >= 2:
            for m in messages:
                if m.get("role") == "tool":
                    seen_tool_contents.append(str(m.get("content")))
            return (_final_json(), [], "")
        return ("", [{
            "id": "call_v", "index": 0, "name": "verify",
            "arguments_str": '{"verification_request": "验证 SQL 注入"}',
        }], "想验证")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "s"}],
        task_id="task-ledger", db=None, round_idx=1,
        client=MagicMock(), task=_mk_task(),
        agent_policy={"allow_verify": True},
    )

    ledger = result.get("_evidence_ledger") or {}
    assert ledger  # 台账非空
    verify_entries = [e for e in ledger.values() if e.get("kind") == "verify"]
    assert verify_entries and verify_entries[0]["verify_ok"] is True
    # 回灌文本末尾带 [evidence_ref=E#]
    assert any("[evidence_ref=E" in c for c in seen_tool_contents)
