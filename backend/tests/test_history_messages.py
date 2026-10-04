"""react_agent 结构化历史注入 + orchestrator plan 持久化 单测(纯函数级)。

覆盖 Codex 对齐改造(P0-1/2/3、P1-6、P2-7/8)的核心纯函数:
- _estimate_tokens:CJK 感知 token 粗估
- _extract_round_data:单轮结构化提取(用户原话/工具摘要/总结/评审反馈/优先级)
- _rounds_to_messages:角色边界 + [系统注入|...] 标记
- _build_history_messages:三级压缩级别选择(Level 0/1/2)
- _truncate_messages:兜底截断保最近
- precompress_history_for_next_round:预压缩触发条件与幂等安全
- orchestrator._save_plan_to_task / _load_plan_from_task:plan 跨轮持久化
"""
from unittest.mock import MagicMock

import pytest

import app.agents.react_agent as react_agent
from app.agents.react_agent import (
    MAX_HISTORY_TOKEN_BUDGET,
    _estimate_tokens,
    _extract_round_data,
    _format_rounds_span,
    _rounds_to_messages,
    _round_token_cost,
    _truncate_messages,
    _build_history_messages,
    precompress_history_for_next_round,
)
from app.prompts.executor import SYSTEM_INJECT_MARKER
import app.agents.orchestrator as orchestrator
from app.agents.orchestrator import _load_plan_from_task, _save_plan_to_task


class _Conv:
    """最小化 Conversation 替身(只含 _extract_round_data 用到的字段)"""

    def __init__(self, round_idx, role, type, content="", reasoning=None):
        self.round_idx = round_idx
        self.role = role
        self.type = type
        self.content = content
        self.reasoning = reasoning


def _mk_round(ridx, *, question="", tool_calls=(), summary="", review="",
              review_reasoning=None, priority_review=None):
    """构造一轮 _extract_round_data 的输入(_Conv 列表)"""
    convs = []
    if question:
        convs.append(_Conv(ridx, "user", "question", content=question))
    for intent in tool_calls:
        convs.append(_Conv(ridx, "agent1", "tool_call", content=intent))
    if summary:
        convs.append(_Conv(ridx, "agent1", "thinking", content=summary))
    if review or review_reasoning:
        # review 走 agent2/type=review(后台审查);reasoning 优先
        convs.append(_Conv(
            ridx, "agent2", "review",
            content=review, reasoning=review_reasoning,
        ))
    return convs


# ============================================================
# _estimate_tokens:CJK 感知粗估
# ============================================================

def test_estimate_tokens_empty():
    assert _estimate_tokens("") == 0


def test_estimate_tokens_cjk_counts_per_char():
    # 10 个汉字 ≈ 10 token(每字 1 token)+ 常数项
    assert _estimate_tokens("一二三四五六七八九十") >= 10


def test_estimate_tokens_latin_one_quarter():
    # 40 个 ASCII ≈ 10 token + 常数项
    est = _estimate_tokens("a" * 40)
    assert 10 <= est <= 15


def test_estimate_tokens_mixed():
    cjk = "一二三"
    latin = "abcdef"
    est = _estimate_tokens(cjk + latin)
    assert est >= 3  # 至少 CJK 部分


# ============================================================
# _extract_round_data:单轮结构化提取
# ============================================================

def test_extract_round_data_full_round():
    """完整轮:question + tool + summary + review 全提取"""
    convs = _mk_round(
        2,
        question="继续检查注入类漏洞",
        tool_calls=["搜索代码: eval(\n{...}"],
        summary="发现 2 处注入风险",
        review_reasoning="已覆盖: ['注入']\n未覆盖: ['越权']\n判断: 需继续",
    )
    rd = _extract_round_data(convs, 2)
    assert rd is not None
    assert rd["ridx"] == 2
    assert rd["question"] == "继续检查注入类漏洞"
    assert "eval(" in rd["tool_summary"]
    assert rd["assistant_summary"] == "发现 2 处注入风险"
    assert "未覆盖" in rd["review"]
    # missing 非空 → priority=2
    assert rd["priority"] == 2


def test_extract_round_data_takes_last_question_and_thinking():
    """多条 question/thinking 时取最后一条(防重复落库的旧数据)"""
    convs = [
        _Conv(1, "user", "question", content="第一版问题"),
        _Conv(1, "agent1", "thinking", content="中间思考"),
        _Conv(1, "user", "question", content="第二版问题"),
        _Conv(1, "agent1", "thinking", content="最终总结"),
    ]
    rd = _extract_round_data(convs, 1)
    assert rd["question"] == "第二版问题"
    assert rd["assistant_summary"] == "最终总结"


def test_extract_round_data_no_content_returns_none():
    """全空轮(无 question/summary/review/tool)→ None"""
    assert _extract_round_data([], 3) is None
    convs = [_Conv(3, "agent1", "tool_result", content="x")]
    assert _extract_round_data(convs, 3) is None


def test_extract_round_data_priority_logic():
    """优先级:review 有 未覆盖(非空)→2;未宣布完成→1;宣布完成→0"""
    # missing 非空 → 2
    rd = _extract_round_data(_mk_round(
        1, summary="s", review_reasoning="未覆盖: ['x']",
    ), 1)
    assert rd["priority"] == 2
    # missing 为空列表 → 降级判定;无"宣布完成" → 1
    rd = _extract_round_data(_mk_round(
        1, summary="s", review_reasoning="未覆盖: []\n判断: ok",
    ), 1)
    assert rd["priority"] == 1
    # 宣布完成 → 0
    rd = _extract_round_data(_mk_round(
        1, summary="s", review_reasoning="→ 宣布完成",
    ), 1)
    assert rd["priority"] == 0


# ============================================================
# _rounds_to_messages:角色边界 + 注入标记
# ============================================================

def _rd(ridx=1, question="q", tool_summary="", assistant_summary="a", review=""):
    return {
        "ridx": ridx, "question": question, "tool_summary": tool_summary,
        "assistant_summary": assistant_summary, "review": review, "priority": 0,
    }


def test_rounds_to_messages_role_structure():
    """每轮产出 user/assistant/system 三类消息,顺序保持对话自然流"""
    msgs = _rounds_to_messages(
        [_rd(1, question="任务指令", tool_summary="工具x",
             assistant_summary="总结", review="反馈")],
        [True],
    )
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "system", "assistant", "system"]
    # 用户原话零包装
    assert msgs[0]["content"] == "任务指令"
    assert msgs[2]["content"] == "总结"
    # 系统注入带边界标记
    assert msgs[1]["content"].startswith(f"{SYSTEM_INJECT_MARKER}工具调用摘要|第 1 轮]")
    assert msgs[3]["content"].startswith(f"{SYSTEM_INJECT_MARKER}评审反馈|第 1 轮]")


def test_rounds_to_messages_level1_drops_tool_summary():
    """include_tools=False 时工具摘要消息不产出(其余保留)"""
    msgs = _rounds_to_messages(
        [_rd(1, question="q", tool_summary="工具x", assistant_summary="a", review="r")],
        [False],
    )
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "system"]
    assert all("工具调用摘要" not in m["content"] for m in msgs)


def test_rounds_to_messages_skips_empty_parts():
    """无内容的部分不产出消息(如无 review 的轮)"""
    msgs = _rounds_to_messages([_rd(1, question="q", assistant_summary="a")], [True])
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_rounds_to_messages_user_content_never_marked():
    """用户消息内容不得带系统注入标记(边界完整性)"""
    msgs = _rounds_to_messages(
        [_rd(1, question="用户原话", tool_summary="t", assistant_summary="a", review="r")],
        [True],
    )
    for m in msgs:
        if m["role"] == "user":
            assert not m["content"].startswith(SYSTEM_INJECT_MARKER)


# ============================================================
# _build_history_messages:三级压缩级别选择
# ============================================================

def test_build_history_messages_empty(monkeypatch):
    """无历史轮 → 空列表"""
    monkeypatch.setattr(
        react_agent, "_load_rounds_data", lambda *a, **kw: [],
    )
    assert _build_history_messages(MagicMock(), "t", 5) == []


def test_build_history_messages_forwards_since_round(monkeypatch):
    """since_round(CLI 恢复链路的增量回放下界)透传给 _load_rounds_data;
    默认 0 保持内置侧行为不变。"""
    captured = {}

    def fake_load(db, task_id, before_round, since_round=0):
        captured["args"] = (before_round, since_round)
        return []

    monkeypatch.setattr(react_agent, "_load_rounds_data", fake_load)
    _build_history_messages(MagicMock(), "t", 5, since_round=3)
    assert captured["args"] == (5, 3)
    _build_history_messages(MagicMock(), "t", 5)
    assert captured["args"] == (5, 0)


def test_load_rounds_data_since_round_bounds():
    """since_round 已追平 before_round(-1)→ 开区间为空,不查库直接返回 []。"""
    from app.agents.react_agent import _load_rounds_data

    db = MagicMock()
    assert _load_rounds_data(db, "t", before_round=5, since_round=4) == []
    assert _load_rounds_data(db, "t", before_round=5, since_round=5) == []
    assert _load_rounds_data(db, "t", before_round=1, since_round=0) == []
    db.query.assert_not_called()


def test_build_history_messages_level0_when_under_budget(monkeypatch):
    """总量在预算内 → Level 0(含工具摘要)"""
    rounds = [
        _rd(1, question="问" * 10, tool_summary="工" * 10,
            assistant_summary="总" * 10, review="评" * 10),
    ]
    monkeypatch.setattr(react_agent, "_load_rounds_data", lambda *a, **kw: rounds)
    msgs = _build_history_messages(MagicMock(), "t", 2)
    assert any("工具调用摘要" in m["content"] for m in msgs)
    assert any(m["role"] == "user" for m in msgs)


def test_build_history_messages_level1_when_over_budget(monkeypatch):
    """Level 0 超预算 → 全部降级 Level 1(丢工具摘要,保角色结构)"""
    # 构造 3 轮:Level 0 超预算、Level 1 合计在预算内
    # (每轮 question+summary=2400 token,tool=500 → L0≈8706 > 8000,L1=7203 ≤ 8000)
    q_s = "字" * 1200
    rounds = [
        _rd(i, question=q_s, tool_summary="工" * 500,
            assistant_summary=q_s, review="")
        for i in range(1, 4)
    ]
    assert sum(_round_token_cost(r, True) for r in rounds) > MAX_HISTORY_TOKEN_BUDGET
    assert sum(_round_token_cost(r, False) for r in rounds) <= MAX_HISTORY_TOKEN_BUDGET
    monkeypatch.setattr(react_agent, "_load_rounds_data", lambda *a, **kw: rounds)
    msgs = _build_history_messages(MagicMock(), "t", 4)
    # 同优先级 FIFO 降级:最早两轮的工具摘要被丢,最近一轮(信息最新)保留
    tool_msgs = [m for m in msgs if "工具调用摘要" in m["content"]]
    assert len(tool_msgs) == 1
    assert "第 3 轮" in tool_msgs[0]["content"]
    # 用户原话与总结全部保留
    assert sum(1 for m in msgs if m["role"] == "user") == 3
    assert sum(1 for m in msgs if m["role"] == "assistant") == 3


def test_build_history_messages_level2_compresses_old_rounds(monkeypatch):
    """Level 1 仍超预算 → 早期轮压缩为单条 system 摘要,保留最近轮"""
    # 早期两轮超大(L1 总量 20003 > 8000),最近一轮小(可完整保留)
    big = "字" * 5000
    rounds = [
        _rd(1, question=big, assistant_summary=big),
        _rd(2, question=big, assistant_summary=big),
        _rd(3, question="最" * 100, assistant_summary="结" * 100),
    ]
    monkeypatch.setattr(react_agent, "_load_rounds_data", lambda *a, **kw: rounds)
    monkeypatch.setattr(
        react_agent, "_get_or_create_compressed",
        lambda *a, **kw: ("压缩摘要文本", [1, 2]),
    )
    msgs = _build_history_messages(MagicMock(), "t", 4, client=MagicMock())
    # 第一条是早期轮次压缩摘要(带标记与覆盖范围)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith(
        f"{SYSTEM_INJECT_MARKER}早期轮次压缩摘要|覆盖第 1-2 轮]"
    )
    # 保留最近 1 轮(HISTORY_KEEP_RECENT=1)的完整 Level 1
    assert msgs[-1]["role"] == "assistant"
    assert sum(1 for m in msgs if m["role"] == "user") == 1


def test_build_history_messages_level2_no_client_truncates(monkeypatch):
    """Level 1 超预算且无 client → 兜底截断(不抛异常,保最近)"""
    big = "字" * 6000
    rounds = [_rd(i, question=big, assistant_summary=big) for i in range(1, 4)]
    monkeypatch.setattr(react_agent, "_load_rounds_data", lambda *a, **kw: rounds)
    msgs = _build_history_messages(MagicMock(), "t", 4, client=None)
    assert len(msgs) < 6  # 3 轮 × 2 条 无法全保
    assert msgs[0]["content"].startswith("[...早期记忆已截断...]")


# ============================================================
# _truncate_messages:兜底截断
# ============================================================

def test_truncate_messages_under_budget_untouched():
    msgs = [{"role": "user", "content": "短"}]
    assert _truncate_messages(msgs, 100) == msgs


def test_truncate_messages_drops_oldest_keeps_recent():
    """超预算:丢最早消息,头部加标记,至少保留最后一条"""
    msgs = [
        {"role": "user", "content": "甲" * 500},
        {"role": "assistant", "content": "乙" * 500},
        {"role": "user", "content": "最近一条"},
    ]
    out = _truncate_messages(msgs, 600)
    assert out[0]["content"] == "[...早期记忆已截断...]"
    assert out[-1]["content"] == "最近一条"
    assert len(out) < len(msgs) + 1


def test_truncate_messages_single_huge_message_kept():
    """单条消息超预算也至少保留它(不返回空)"""
    msgs = [{"role": "user", "content": "巨" * 5000}]
    out = _truncate_messages(msgs, 10)
    assert len(out) >= 1
    assert out[-1]["content"] == msgs[0]["content"]


# ============================================================
# _format_rounds_span
# ============================================================

def test_format_rounds_span():
    assert _format_rounds_span([]) == ""
    assert _format_rounds_span([3]) == "第 3 轮"
    assert _format_rounds_span([1, 2, 3]) == "第 1-3 轮"


# ============================================================
# precompress_history_for_next_round:预压缩触发条件
# ============================================================

def test_precompress_under_budget_skips_llm(monkeypatch):
    """下一轮 Level 1 可放下 → 不触发压缩 LLM"""
    called = []
    rounds = [_rd(i, question="问", assistant_summary="总") for i in range(1, 4)]
    monkeypatch.setattr(react_agent, "_load_rounds_data", lambda *a, **kw: rounds)
    monkeypatch.setattr(
        react_agent, "_get_or_create_compressed",
        lambda *a, **kw: called.append(1) or ("", []),
    )
    precompress_history_for_next_round(MagicMock(), "t", 3, MagicMock())
    assert called == []


def test_precompress_over_budget_compresses_old_rounds(monkeypatch):
    """下一轮会触发 Level 2 → 预压缩除最近轮外的早期轮次"""
    captured = {}

    def fake_compress(db, task_id, client, old_rounds, current_round_idx):
        captured["old_rounds"] = [r["ridx"] for r in old_rounds]
        captured["current"] = current_round_idx
        return "摘要", [r["ridx"] for r in old_rounds]

    big = "字" * 6000
    rounds = [_rd(i, question=big, assistant_summary=big) for i in range(1, 4)]
    monkeypatch.setattr(react_agent, "_load_rounds_data", lambda *a, **kw: rounds)
    monkeypatch.setattr(
        react_agent, "_get_or_create_compressed", fake_compress,
    )
    precompress_history_for_next_round(MagicMock(), "t", 3, MagicMock())
    # 3 轮完成 → 下一轮视角保留最近 1 轮(第 3 轮),压缩第 1-2 轮
    assert captured["old_rounds"] == [1, 2]
    assert captured["current"] == 4


def test_precompress_never_raises(monkeypatch):
    """内部异常被吞(预压缩是纯优化,失败不影响任务)"""
    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(react_agent, "_load_rounds_data", boom)
    # 不抛异常即通过
    precompress_history_for_next_round(MagicMock(), "t", 3, MagicMock())


def test_precompress_no_client_noop():
    """无 client → 直接返回(无可用的压缩模型)"""
    # _load_rounds_data 未 mock,真 db 是 MagicMock:
    # client=None 早返回,不会触达查询
    precompress_history_for_next_round(MagicMock(), "t", 3, None)


# ============================================================
# orchestrator:plan 持久化(P1-6)
# ============================================================

def test_save_and_load_plan_roundtrip():
    task = MagicMock()
    task.id = "t1"
    task.params = {"repo_url": "x"}
    db = MagicMock()
    plan = [
        {"id": 1, "text": "克隆仓库", "status": "done"},
        {"id": 2, "text": "审计依赖", "status": "in_progress"},
    ]
    _save_plan_to_task(task, db, plan)
    # params 被写入 _plan(保留原有键)
    assert task.params["_plan"] == plan
    assert task.params["repo_url"] == "x"
    db.commit.assert_called_once()

    # 回读:mock task.params 已是写入后的 dict
    assert _load_plan_from_task(task) == plan


def test_load_plan_empty_and_dirty_data():
    """无 _plan / 非法结构 / 脏条目 → 安全清洗为合法列表"""
    task = MagicMock()
    task.params = None
    assert _load_plan_from_task(task) == []

    task.params = {"_plan": "not-a-list"}
    assert _load_plan_from_task(task) == []

    task.params = {"_plan": [
        {"id": 1, "text": "合法", "status": "done"},
        {"id": 2, "text": "", "status": "pending"},   # 空 text → 剔除
        "garbage",                                     # 非 dict → 剔除
        {"id": 3, "status": "pending"},                # 缺 text → 剔除
    ]}
    loaded = _load_plan_from_task(task)
    assert loaded == [{"id": 1, "text": "合法", "status": "done"}]


def test_save_plan_failure_does_not_raise():
    """db.commit 抛异常 → 只记日志不外抛(不影响任务生命周期)"""
    task = MagicMock()
    task.id = "t1"
    db = MagicMock()
    db.commit.side_effect = RuntimeError("db down")
    _save_plan_to_task(task, db, [{"text": "x", "status": "pending"}])
    # 到这里没抛异常即通过


def test_save_plan_empty_clears_stale():
    """空 plan 也写入(清空上轮残留)"""
    task = MagicMock()
    task.id = "t1"
    task.params = {"_plan": [{"text": "旧", "status": "done"}]}
    db = MagicMock()
    _save_plan_to_task(task, db, [])
    assert task.params["_plan"] == []
