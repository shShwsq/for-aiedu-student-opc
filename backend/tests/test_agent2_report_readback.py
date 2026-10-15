"""agent2 读取 agent1 报告全文(read_agent1_report)测试。

背景:agent1 每轮 summary 回灌 agent2 时曾被 MAX_HISTORY_MSG_CHARS=3000 头部截断,
长报告(安全审查动辄上万字)只剩开头,agent2 把「自己没看到」误判成「agent1 未覆盖」
(假阴性)。改造后:回灌只给摘录 + 截断标注 + 指针,全文经 read_agent1_report 从
Conversation 表(已无截断落库)分页读取,且该工具不依赖 repo_path。

覆盖:
- 分页读取(多页续读、末页 has_more=False、单页不超 AGENT1_REPORT_PAGE_CHARS)
- 无 repo_path 时工具仍注册可用(与只读工具的「无工作区不可用」形成对比)
- 缺失轮次如实回告 ok=False,不抛异常
- 工具结果经 EvidenceRecorder 登记 _evidence_ref(供负向断言 call_ref 引用)
- 计划项并行 worker 无 db 会话时从预加载 reports_by_round 取全文并登记证据
- 纯分页函数 _build_report_page 的边界(offset 越界、limit 钳制)
"""
import json
import uuid
from collections import deque
from contextlib import nullcontext
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  注册 TaskArtifact,Task mapper 才初始化
import app.models.user_git_binding  # noqa: F401
import app.agents.agent2 as agent2
import app.agents.react_agent as react_agent
from app.agents.agent2 import (
    _build_report_page,
    _execute_read_agent1_report,
    run_agent2,
)
from app.agents.runtime.constants import (
    AGENT1_REPORT_EXCERPT_CHARS,
    AGENT1_REPORT_PAGE_CHARS,
)
from app.agents.runtime.llm_stream import StreamResult


# ============================================================
# 辅助
# ============================================================


def _mock_db_with_report(content: str, conv_id: str = "conv-1"):
    """构造一个 db 替身:query(Conversation).filter().order_by().first() 返回指定正文。"""
    db = MagicMock()
    conv = MagicMock()
    conv.content = content
    conv.id = conv_id
    (
        db.query.return_value
        .filter.return_value
        .order_by.return_value
        .first.return_value
    ) = conv
    return db


def _mock_db_no_report():
    db = MagicMock()
    (
        db.query.return_value
        .filter.return_value
        .order_by.return_value
        .first.return_value
    ) = None
    return db


def _tc(name, args, cid="call_1"):
    return {
        "id": cid, "index": 0, "name": name,
        "arguments_str": json.dumps(args, ensure_ascii=False),
    }


def _sr(content="", tool_calls=None, reasoning="", conv_id=None, stopped=False):
    return StreamResult(
        conv_id=conv_id or f"conv-{uuid.uuid4().hex[:8]}",
        reasoning=reasoning, content=content,
        tool_calls=tool_calls or [], finish_reason="stop", stopped=stopped,
    )


def _final_json():
    return json.dumps({
        "covered": [], "missing": [], "reasoning": "ok", "suggestions": [],
        "results": [], "grouping": None,
    }, ensure_ascii=False)


# ============================================================
# 1) 分页读取
# ============================================================


def test_read_agent1_report_paginates():
    """18558 字报告按 offset 续读:每页 ≤ 上限、has_more 正确、末页收尾。"""
    full = "报" * 18558
    db = _mock_db_with_report(full, conv_id="conv-full")

    # 第一页
    out = json.loads(_execute_read_agent1_report(
        {"round_idx": 1}, "task-1", db, task=None, round_idx=1,
    ))
    assert out["ok"] is True
    assert out["total_chars"] == 18558
    assert out["returned"] == AGENT1_REPORT_PAGE_CHARS
    assert out["returned"] <= AGENT1_REPORT_PAGE_CHARS
    assert out["has_more"] is True
    assert out["next_offset"] == AGENT1_REPORT_PAGE_CHARS
    assert out["conversation_id"] == "conv-full"
    assert out["content"] == full[:AGENT1_REPORT_PAGE_CHARS]

    # 续读到末页
    offset = out["next_offset"]
    pages = 1
    while True:
        page = json.loads(_execute_read_agent1_report(
            {"round_idx": 1, "offset": offset}, "task-1", db, task=None, round_idx=1,
        ))
        assert page["returned"] <= AGENT1_REPORT_PAGE_CHARS
        assert page["content"] == full[offset:offset + page["returned"]]
        pages += 1
        if not page["has_more"]:
            assert offset + page["returned"] == 18558
            break
        offset = page["next_offset"]
        assert pages < 100  # 防呆:分页应收敛

    # 全文可经分页完整重建(无截断丢失);页数由分页常量推导,env 调大则页数减少
    import math
    assert pages == math.ceil(18558 / AGENT1_REPORT_PAGE_CHARS)


def test_read_agent1_report_limit_clamped():
    """limit 超单页上限被钳制到 AGENT1_REPORT_PAGE_CHARS(防一次吐爆上下文)。"""
    full = "x" * 9000
    db = _mock_db_with_report(full)
    out = json.loads(_execute_read_agent1_report(
        {"round_idx": 1, "limit": 99999}, "task-1", db, task=None, round_idx=1,
    ))
    assert out["returned"] == AGENT1_REPORT_PAGE_CHARS
    assert out["has_more"] is True


# ============================================================
# 2) 无 repo_path 仍可用
# ============================================================


def test_read_agent1_report_registered_without_repo_path(monkeypatch):
    """repo_path=None 时 read_agent1_report 仍注册(只读工具则不注册)。

    负向断言取证不该因任务没配仓库就被剥夺:本工具数据源是 Conversation,
    与被审工作区无关,须无条件可用。
    """
    captured_tools = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        captured_tools.append(tools)
        return (_final_json(), [], "")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "短总结"}],
        task_id="task-1", db=None, round_idx=1,
        client=MagicMock(), task=None, repo_path=None,
    )

    names = {(t.get("function") or {}).get("name") for t in (captured_tools[0] or [])}
    assert "read_agent1_report" in names
    # 对比:工作区只读工具无 repo_path 时不注册
    assert "read_file" not in names


def test_read_agent1_report_executes_without_repo_path():
    """无 repo_path 也能真正读到全文(执行不依赖工作区)。"""
    full = "y" * 5000
    db = _mock_db_with_report(full, conv_id="conv-norepo")
    out = json.loads(_execute_read_agent1_report(
        {"round_idx": 1}, "task-1", db, task=None, round_idx=1,
    ))
    assert out["ok"] is True
    assert out["total_chars"] == 5000
    assert out["content"] == full[:AGENT1_REPORT_PAGE_CHARS]


# ============================================================
# 3) 缺失轮次 / 非法参数
# ============================================================


def test_read_agent1_report_missing_round():
    """查询无记录 → ok=False + 原因,不抛异常。"""
    db = _mock_db_no_report()
    out = json.loads(_execute_read_agent1_report(
        {"round_idx": 3}, "task-1", db, task=None, round_idx=1,
    ))
    assert out["ok"] is False
    assert "未找到" in out["reason"]
    assert out["round_idx"] == 3


def test_read_agent1_report_invalid_round_idx():
    """round_idx 缺失/非正 → ok=False,提示必填。"""
    db = _mock_db_with_report("z" * 100)
    out = json.loads(_execute_read_agent1_report(
        {}, "task-1", db, task=None, round_idx=1,
    ))
    assert out["ok"] is False
    assert "round_idx" in out["reason"]


def test_read_agent1_report_no_db():
    """db=None(兜底场景)→ ok=False,不抛异常。"""
    out = json.loads(_execute_read_agent1_report(
        {"round_idx": 1}, "task-1", None, task=None, round_idx=1,
    ))
    assert out["ok"] is False


# ============================================================
# 4) 证据登记(_evidence_ref)
# ============================================================


def test_evidence_ref_recorded_for_report_read(monkeypatch):
    """read_agent1_report 结果经 EvidenceRecorder 登记 E#,供负向断言 call_ref 引用。"""
    monkeypatch.setattr(
        agent2, "_execute_read_agent1_report",
        lambda *a, **k: json.dumps({
            "ok": True, "round_idx": 1, "total_chars": 18558,
            "content": "1.1 [HIGH] local 模式 RCE ... 2.2 [HIGH] JWT 默认密钥 ...",
        }, ensure_ascii=False),
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
        return ("", [_tc("read_agent1_report", {"round_idx": 1}, cid="cR")], "想读全文")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = run_agent2(
        "审查这个仓库", [{"round": 1, "summary": "长" * (AGENT1_REPORT_EXCERPT_CHARS + 10)}],
        task_id="task-1", db=None, round_idx=1,
        client=MagicMock(), task=None, repo_path=None,
    )

    ledger = result.get("_evidence_ledger") or {}
    assert "E1" in ledger
    assert ledger["E1"]["kind"] == "read"
    # 回灌给下一次 LLM 的 tool 结果带 _evidence_ref(可被 call_ref 就近指名)
    assert any("_evidence_ref" in c for c in seen_tool_contents)


# ============================================================
# 5) 计划项并行 worker(无 db,从预加载 reports_by_round 取全文)
# ============================================================


def test_plan_item_worker_can_read_report(monkeypatch):
    """worker 无 db 会话:从 reports_by_round 取全文分页,登记证据,不被白名单拒绝。"""
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    full = "全文" * 3000  # 6000 字,跨多页
    reports_by_round = {1: ("conv-w1", full)}
    snapshots = []
    script = deque([
        _sr(tool_calls=[_tc("read_agent1_report", {"round_idx": 1}, cid="cW")]),
        _sr(content="结论:agent1 报告已覆盖 JWT 默认密钥(2.2 节)"),
    ])

    def _worker(client, messages, **kwargs):
        snapshots.append(list(messages))
        return script.popleft()

    monkeypatch.setattr(agent2, "stream_llm", _worker)

    result = agent2._run_plan_item_pass(
        {"id": "P1", "target": "JWT 默认密钥是否被报告覆盖", "planned_evidence": "source"},
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        MagicMock(), "task-w", 1, "",  # repo_path 为空:worker 读全文不依赖工作区
        read_cap=5, max_iter=8, item_timeout=60,
        reports_by_round=reports_by_round,
    )

    assert result.error == ""
    # 证据登记:read_agent1_report 经 worker_evidence.record_read 入台账
    assert result.evidence_entries, "worker 未登记读全文取证"
    # 工具未被白名单拒绝,全文分页结果已回灌
    tool_msgs = [m["content"] for seq in snapshots for m in seq if m.get("role") == "tool"]
    assert tool_msgs, "脚本未产生 tool 消息"
    fed = json.loads(tool_msgs[-1])
    assert fed["ok"] is True
    assert fed["total_chars"] == len(full)
    assert fed["_evidence_ref"]  # 证据引用号已注入


def test_plan_item_worker_report_missing_round(monkeypatch):
    """worker 请求不存在的轮次 → ok=False 如实回告,不抛异常、不登记伪证据。"""
    monkeypatch.setattr(react_agent, "_review_provider_gate", lambda client: nullcontext())
    reports_by_round = {1: ("conv-w1", "正文" * 100)}
    script = deque([
        _sr(tool_calls=[_tc("read_agent1_report", {"round_idx": 9}, cid="cM")]),
        _sr(content="结论:第 9 轮无报告"),
    ])

    def _worker(client, messages, **kwargs):
        return script.popleft()

    monkeypatch.setattr(agent2, "stream_llm", _worker)

    result = agent2._run_plan_item_pass(
        {"id": "P1", "target": "t", "planned_evidence": "source"},
        [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        MagicMock(), "task-w2", 1, "",
        read_cap=5, max_iter=8, item_timeout=60,
        reports_by_round=reports_by_round,
    )
    assert result.error == ""


# ============================================================
# 6) 纯分页函数边界
# ============================================================


def test_build_report_page_offset_beyond_total():
    """offset 越界 → 空切片、has_more=False、next_offset 回指 offset。"""
    page = _build_report_page("abc", "conv-x", 1, {"offset": 100})
    assert page["ok"] is True
    assert page["content"] == ""
    assert page["returned"] == 0
    assert page["has_more"] is False
    assert page["next_offset"] == 100
    assert page["total_chars"] == 3


def test_build_report_page_default_limit():
    """未给 limit → 默认单页上限;给定 limit 则按之(不超上限)。"""
    # full 严格大于单页上限,才能验证默认分页下 has_more=True
    # (分页常量走 env 可变,样本长度跟着常量走以免固定值跟常量相撞)
    full = "q" * (AGENT1_REPORT_PAGE_CHARS + 2000)
    default_page = _build_report_page(full, "conv-y", 1, {})
    assert default_page["returned"] == AGENT1_REPORT_PAGE_CHARS
    assert default_page["has_more"] is True

    small_page = _build_report_page(full, "conv-y", 1, {"limit": 800})
    assert small_page["returned"] == 800
    assert small_page["next_offset"] == 800
