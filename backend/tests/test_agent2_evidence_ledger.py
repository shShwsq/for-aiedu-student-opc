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


def test_long_non_range_result_ledger_keeps_full_body_while_llm_sees_truncated(monkeypatch):
    """回归:超长取证结果只截断回灌给 LLM 的内容,台账须存完整正文。

    覆盖**非 read_file 形状**(无 start_line 元数据,如 list_files/search_code 的
    超大结果)退回通用截断的兜底:回灌内容被 _truncate_for_llm 砍掉,台账仍存全文
    ——否则引用了截断点之后原文的 quote 会永远核验失败(被误判「证据未核验」)。
    带行范围元数据的 read_file 走按行裁剪,见
    test_long_read_llm_copy_is_line_bounded_and_honest。
    """
    long_content = "L1 " + ("x" * 5000)
    monkeypatch.setattr(
        agent2, "_execute_read_tool",
        lambda *a, **k: json.dumps({"path": "big.py", "content": long_content}),
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
        return ("", _read_tool_call(), "想读大文件")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "s"}],
        task_id="task-ledger", db=None, round_idx=1,
        client=MagicMock(), task=_mk_task(), repo_path="/repo",
    )

    # 台账正文完整(未被截断)
    body = result["_evidence_ledger"]["E1"]["body"]
    assert len(body) > 3000
    assert "工具结果过长已截断" not in body
    # 回灌 LLM 的内容被截断
    assert any("工具结果过长已截断" in c for c in seen_tool_contents)


def _real_shape_read_json() -> str:
    """sandbox_tools.read_file 的真实返回形状(带 start_line/end_line/total_lines)"""
    lines = [f"{i:>4}: def f_{i}(x):  # {'y' * 40}" for i in range(1, 501)]
    return json.dumps({
        "path": "big.py", "content": "\n".join(lines),
        "start_line": 1, "end_line": 500, "total_lines": 500,
        "truncated": False, "binary": False, "size": 0,
    }, ensure_ascii=False)


def test_long_read_llm_copy_is_line_bounded_and_honest(monkeypatch):
    """回归:read_file 超预算时,回灌给 LLM 的那份要裁到**完整行**并自陈真实范围。

    read_file 结果带 start_line/end_line/total_lines/truncated;直接 _truncate_for_llm
    会中途砍断,模型既看不懂区间尾部、又被标称的 end_line 误导。这里断言:
    - 模型看到的内容裁到整行(不留半个 line);
    - end_line/truncated 已修正为"真正看到的范围"(模型可据 offset=end_line+1 续读);
    - 台账仍存完整正文,元数据未被改动。
    """
    import re

    monkeypatch.setattr(agent2, "_execute_read_tool", lambda *a, **k: _real_shape_read_json())
    state = {"n": 0}
    seen_tool_contents: list[str] = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        state["n"] += 1
        if state["n"] >= 2:
            for m in messages:
                if m.get("role") == "tool":
                    seen_tool_contents.append(str(m.get("content")))
            return (_final_json(), [], "")
        return ("", _read_tool_call(), "想读大文件")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "s"}],
        task_id="task-ledger", db=None, round_idx=1,
        client=MagicMock(), task=_mk_task(), repo_path="/repo",
    )

    # 台账:完整正文(500 行),元数据原样
    body_obj = json.loads(result["_evidence_ledger"]["E1"]["body"])
    assert body_obj["end_line"] == 500 and body_obj["truncated"] is False
    assert body_obj["content"].count("\n") == 499

    # 回灌 LLM 的那份:引用号在最前、裁到整行、范围自陈
    fed = seen_tool_contents[-1]
    assert "工具结果过长已截断" not in fed
    fed_obj = json.loads(fed)
    assert fed_obj["_evidence_ref"] == "E1"
    assert fed_obj["end_line"] < 500
    assert fed_obj["truncated"] is True
    kept_lines = fed_obj["content"].splitlines()
    # 每个保留行都是完整的一行(带行号前缀),没有被切半个
    assert kept_lines and all(re.match(r"^\s*\d+:", ln) for ln in kept_lines)
    assert len(fed) <= agent2._MAX_TOOL_RESULT_CHARS
