"""CLI(ACP)执行器的跨轮上下文注入单测(纯函数,不连 DB / 不连沙箱)。

覆盖三项修复(对齐 Codex 的上下文装配思路):
- build_cli_history_replay_section:结构化历史消息 → CLI 单条 text 回放段
- _load_history_replay:走全新 session 时回放之前轮次(异常/首轮降级为空)
- _resolve_injection_plan + _remember_injected_context:同一 session 内
  未变化的系统注入段不重复发送(按段独立指纹)
"""
from unittest.mock import MagicMock

import app.agents.acp_base as acp_base
import app.agents.react_agent as react_agent
from app.agents.acp_base import (
    _context_sections_hash,
    _load_history_replay,
    _remember_injected_context,
    _resolve_injection_plan,
)
from app.prompts.executor import SYSTEM_INJECT_MARKER, build_cli_history_replay_section
from app.prompts.executor import build_cli_memory_section


# ============================================================
# build_cli_history_replay_section:结构化历史 → 文本回放段
# ============================================================


def test_replay_section_empty_messages_yield_empty():
    """无历史(首轮)→ 空串,不给 prompt 添空段。"""
    assert build_cli_history_replay_section([]) == ""
    assert build_cli_history_replay_section(None) == ""
    # 全为空白内容同样视为无历史
    assert build_cli_history_replay_section([{"role": "user", "content": "  "}]) == ""


def test_replay_section_keeps_role_boundaries():
    """user/assistant 角色写进文本(CLI 的 session/prompt 没有角色通道)。"""
    section = build_cli_history_replay_section([
        {"role": "user", "content": "审计登录模块"},
        {"role": "assistant", "content": "已发现 2 个越权问题"},
    ])
    assert section.startswith("\n\n" + SYSTEM_INJECT_MARKER)
    assert "此前轮次执行记录]" in section
    assert "用户: 审计登录模块" in section
    assert "执行者: 已发现 2 个越权问题" in section
    # 明示"这是历史不是新指令",防止模型把过往要求当成本轮任务
    assert "不要重做已完成的部分" in section


def test_replay_section_preserves_inject_markers():
    """system 消息自带的 [系统注入|来源|第 N 轮] 标记原样保留(保住轮次边界)。"""
    marked = f"{SYSTEM_INJECT_MARKER}评审反馈|第 1 轮]\n未覆盖: ['并发']"
    section = build_cli_history_replay_section([{"role": "system", "content": marked}])
    assert marked in section
    # 已带标记的不二次包裹
    assert section.count(SYSTEM_INJECT_MARKER) == 2  # 段首标记 + 该条标记


def test_replay_section_wraps_unmarked_system_message():
    """无标记的 system 内容补通用标记,不与用户原话混淆(如兜底截断标记)。"""
    section = build_cli_history_replay_section([
        {"role": "system", "content": "[...早期记忆已截断...]"},
    ])
    assert f"{SYSTEM_INJECT_MARKER}历史记录] [...早期记忆已截断...]" in section


def test_replay_section_order_follows_input():
    """按输入顺序渲染(历史本身已按轮次时间序)。"""
    section = build_cli_history_replay_section([
        {"role": "user", "content": "R1_Q"},
        {"role": "assistant", "content": "R1_A"},
        {"role": "user", "content": "R2_Q"},
    ])
    assert section.index("R1_Q") < section.index("R1_A") < section.index("R2_Q")


# ============================================================
# _load_history_replay:数据源与内置 react_agent 同源 + 降级安全
# ============================================================


def _mk_task(task_id="t1"):
    task = MagicMock()
    task.id = task_id
    return task


def test_load_history_replay_skips_first_round(monkeypatch):
    """第 1 轮无历史可回放:直接返回空串,不查 DB。"""
    def boom(*a, **kw):
        raise AssertionError("首轮不应构造历史")

    monkeypatch.setattr(react_agent, "_build_history_messages", boom)
    assert _load_history_replay(MagicMock(), _mk_task(), 1) == ""
    assert _load_history_replay(MagicMock(), _mk_task(), 0) == ""


def test_load_history_replay_renders_shared_history(monkeypatch):
    """复用内置侧同一份历史构造,渲染结果带注入标记。"""
    monkeypatch.setattr(acp_base, "perf_log", lambda *a, **kw: None)
    captured = {}

    def fake_build(db, task_id, round_idx, *a, **kw):
        captured["args"] = (task_id, round_idx)
        return [
            {"role": "user", "content": "第 1 轮的原话"},
            {"role": "assistant", "content": "第 1 轮的结论"},
        ]

    monkeypatch.setattr(react_agent, "_build_history_messages", fake_build)
    section = _load_history_replay(MagicMock(), _mk_task("task-9"), 3)
    # 按位置参数传入 (db, task.id, round_idx),不传 client(CLI 链不依赖后端 LLM)
    assert captured["args"] == ("task-9", 3)
    assert "第 1 轮的原话" in section and "第 1 轮的结论" in section
    assert section.startswith("\n\n" + SYSTEM_INJECT_MARKER)


def test_load_history_replay_degrades_to_empty_on_error(monkeypatch):
    """历史构造抛异常 → 返回空串(回放是增强项,不能拖垮执行轮)。"""
    monkeypatch.setattr(acp_base, "perf_log", lambda *a, **kw: None)

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(react_agent, "_build_history_messages", boom)
    assert _load_history_replay(MagicMock(), _mk_task(), 4) == ""


def test_load_history_replay_empty_history_returns_empty(monkeypatch):
    """历史为空(如仅有第 0 轮存量数据)→ 空串,不产生空回放段。"""
    monkeypatch.setattr(acp_base, "perf_log", lambda *a, **kw: None)
    monkeypatch.setattr(
        react_agent, "_build_history_messages", lambda *a, **kw: [],
    )
    assert _load_history_replay(MagicMock(), _mk_task(), 2) == ""


# ============================================================
# 注入段去重(按段独立指纹)
# ============================================================


def test_sections_hash_is_content_stable_and_distinguishes_change():
    """内容逐字一致 → 指纹一致;任一段变化 → 该段指纹变化。"""
    a = _context_sections_hash("REPO", "MEM")
    b = _context_sections_hash("REPO", "MEM")
    c = _context_sections_hash("REPO", "MEM2")
    assert a == b
    assert a["repo"] == c["repo"]
    assert a["memory"] != c["memory"]
    # 空内容也有稳定指纹(两段都空时无历史注入,不应误判为"变化")
    assert _context_sections_hash("", "") == _context_sections_hash("", "")


def test_new_session_injects_everything_and_requests_replay():
    """走全新链路:注入段全量发送 + 需要历史回放。"""
    repo, mem, need_replay, _ = _resolve_injection_plan(None, "REPO", "MEM")
    assert (repo, mem, need_replay) == ("REPO", "MEM", True)


def test_reused_session_skips_unchanged_sections():
    """复用 session 且注入段未变 → 两段都跳过,不再要求回放。"""
    _, _, _, ctx_hash = _resolve_injection_plan(None, "REPO", "MEM")
    reused = {"injected_context_hash": ctx_hash}
    repo, mem, need_replay, again = _resolve_injection_plan(reused, "REPO", "MEM")
    assert (repo, mem, need_replay) == ("", "", False)
    # 指纹按原内容计算(被裁剪的段不参与),下一轮仍可继续命中
    assert again == ctx_hash


def test_reused_session_reinjects_only_changed_section():
    """只有记忆段变化 → 记忆段重发,仓库上下文段仍跳过(分桶指纹)。"""
    _, _, _, first = _resolve_injection_plan(None, "REPO", "MEM1")
    reused = {"injected_context_hash": first}
    repo, mem, need_replay, _ = _resolve_injection_plan(reused, "REPO", "MEM2")
    assert repo == ""
    assert mem == "MEM2"
    assert need_replay is False


def test_reused_session_without_hash_record_injects_sections():
    """缓存里没有指纹(旧 entry / 首次记录前)→ 正常注入,不抛异常。"""
    repo, mem, need_replay, _ = _resolve_injection_plan(
        {"injected_context_hash": None}, "REPO", "MEM",
    )
    assert (repo, mem, need_replay) == ("REPO", "MEM", False)
    # 缺 key 同样安全
    repo, mem, _, _ = _resolve_injection_plan({}, "REPO", "MEM")
    assert (repo, mem) == ("REPO", "MEM")


def test_remember_injected_context_updates_entry(monkeypatch):
    """发送成功后把指纹写回 session 缓存,供下一轮去重。"""
    cache = {"t1": {"acp_session_id": "s1"}}
    monkeypatch.setattr(acp_base, "_bridge_cache", cache)
    ctx_hash = _context_sections_hash("REPO", "MEM")
    _remember_injected_context("t1", ctx_hash)
    assert cache["t1"]["injected_context_hash"] == ctx_hash


def test_remember_injected_context_tolerates_missing_entry(monkeypatch):
    """缓存已被清(连接层失败/sandbox 销毁)→ 静默跳过,不抛 KeyError。"""
    monkeypatch.setattr(acp_base, "_bridge_cache", {})
    _remember_injected_context("absent-task", _context_sections_hash("R", "M"))


def test_dedup_round_trip_over_two_rounds():
    """模拟同 session 连续两轮:第 2 轮不再重复发送记忆段。"""
    memory = build_cli_memory_section("PROJECT_MEM", "GLOBAL_MEM")
    # 第 1 轮:新 session,全量注入
    repo1, mem1, _, hash1 = _resolve_injection_plan(None, "REPO", memory)
    assert mem1 == memory
    reused = {"injected_context_hash": hash1}
    # 第 2 轮(追问轮:预 clone 段不再拼,记忆段逐字未变)
    repo2, mem2, need_replay, _ = _resolve_injection_plan(reused, "", memory)
    assert repo2 == "" and mem2 == "" and need_replay is False
    # 记忆内容发生变化(如归纳出新约束)→ 重新注入
    _, mem3, _, _ = _resolve_injection_plan(reused, "", memory + "\n新约束")
    assert "新约束" in mem3


def test_replay_is_one_shot_per_session(monkeypatch):
    """回放只在新 session 首轮发生:同 session 后续轮(need_replay=False)不重发。"""
    monkeypatch.setattr(acp_base, "perf_log", lambda *a, **kw: None)
    calls = []

    def fake_build(db, task_id, round_idx, *a, **kw):
        calls.append(round_idx)
        return [{"role": "user", "content": "旧轮原话"}]

    monkeypatch.setattr(react_agent, "_build_history_messages", fake_build)
    db, task = MagicMock(), _mk_task()
    # 轮 3 走全新链路 → 回放;轮 4 复用同一 session → 不回放
    _, _, need_replay3, _ = _resolve_injection_plan(None, "REPO", "MEM")
    if need_replay3:
        _load_history_replay(db, task, 3)
    _, _, need_replay4, _ = _resolve_injection_plan(
        {"injected_context_hash": _context_sections_hash("REPO", "MEM")}, "REPO", "MEM",
    )
    assert need_replay4 is False
    replay4 = _load_history_replay(db, task, 4) if need_replay4 else ""
    assert replay4 == ""
    assert calls == [3]  # 只有轮 3 真正构造过历史
