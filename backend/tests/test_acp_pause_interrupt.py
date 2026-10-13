"""CLI(ACP)暂停真中断单元测试(方案 B)

覆盖 agent1 走 Qoder/DeepSeek/Codex 等 CLI 执行器时,用户暂停能在流式循环内
真中断生成并冻结流水线:
- ACPClient.prompt:pause_probe 命中(ACPPromptPaused)→ cancel + 置 last_prompt_paused
  + 空结果;与 last_prompt_truncated 互斥(暂停不置截断)
- ACPClient.prompt:每次调用重置 last_prompt_paused(状态不跨轮串扰)
- ACPClient.prompt:把 pause_probe 透传给 _rpc
- ACPClient._rpc:on_event 分发前检查 pause_probe(活跃流下一条即停)
- ACPClient._iter_lines_with_watchdog:轮询空转检查 pause_probe(静默期响应)
- 未暂停时行为与现状一致(pause_probe 恒 False)
- orchestrator._consume_paused_interrupt_flag:读取并清除 _round_paused_interrupt
  (builtin 不写标志 → 恒 False,行为无变化)
- orchestrator._resolve_continuation_message:无排队消息用默认文案,有则用用户原文
- orchestrator._run_agent1_rounds_with_auto_continue:被中断轮→恢复后同会话自动
  续跑下一轮;开关关/达上限则对当前轮收尾
"""
import queue as queue_mod
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import (
    ACPPromptPaused,
    ACPClient,
    PromptIdleTimeout,
)
from app.agents.orchestrator import _consume_paused_interrupt_flag


# ============================================================
# 辅助:伪造 httpx streaming response(同 test_acp_stream_abort)
# ============================================================


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self.status_code = status_code
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


class _BlockingStreamResponse:
    """iter_lines 阻塞不返回(模拟 CLI 静默):供看门狗空转检查暂停"""

    def __init__(self, hold_secs: float = 2.0):
        self._hold = hold_secs

    def iter_lines(self):
        time.sleep(self._hold)
        return iter(())


class _StreamCM:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        return False


def _install_stream(monkeypatch, client: ACPClient, response) -> None:
    monkeypatch.setattr(client._client, "stream", lambda *a, **kw: _StreamCM(response))


# ============================================================
# ACPClient.prompt:暂停真中断善后
# ============================================================


def test_prompt_paused_cancels_and_sets_flag(monkeypatch):
    """pause_probe 命中:cancel + last_prompt_paused=True + 空结果;不置 truncated(互斥)"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(client, "_rpc", MagicMock(side_effect=ACPPromptPaused()))
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))

    result = client.prompt(
        "sess-1", [{"type": "text", "text": "hi"}],
        pause_probe=lambda: True,
    )

    assert result == {}
    assert cancelled == ["sess-1"]
    assert client.last_prompt_paused is True
    assert client.last_prompt_truncated is None  # 暂停 ≠ 截断,语义分流


def test_prompt_resets_paused_flag_each_call(monkeypatch):
    """每次 prompt 入口重置 last_prompt_paused:暂停后下一轮正常完成不残留"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(client, "_rpc", MagicMock(side_effect=ACPPromptPaused()))
    monkeypatch.setattr(client, "cancel", lambda sid: None)
    client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=lambda: True)
    assert client.last_prompt_paused is True

    monkeypatch.setattr(client, "_rpc", MagicMock(return_value={"stopReason": "end_turn"}))
    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=lambda: False)
    assert result == {"stopReason": "end_turn"}
    assert client.last_prompt_paused is False


def test_prompt_forwards_pause_probe_to_rpc(monkeypatch):
    """prompt 把 pause_probe 透传给 _rpc(探针不丢失)"""
    client = ACPClient("http://fake")
    captured: dict = {}

    def fake_rpc(req, on_event=None, timeout=None, idle_probe=None, pause_probe=None):
        captured["pause_probe"] = pause_probe
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(client, "_rpc", fake_rpc)
    probe = lambda: False  # noqa: E731
    client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=probe)
    assert captured["pause_probe"] is probe


def test_prompt_idle_timeout_still_sets_truncated_not_paused(monkeypatch):
    """回归护栏:idle 超时仍走 truncated 路径,不误置 paused"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(client, "_rpc", MagicMock(side_effect=PromptIdleTimeout(60, False)))
    monkeypatch.setattr(client, "cancel", lambda sid: None)
    result = client.prompt("sess-1", [{"type": "text", "text": "hi"}], pause_probe=lambda: False)
    assert result == {}
    assert client.last_prompt_truncated is not None
    assert client.last_prompt_paused is False


# ============================================================
# ACPClient._rpc:on_event 分发前检查(活跃流路径)
# ============================================================


def test_rpc_raises_before_dispatching_notification(monkeypatch):
    """活跃流:命中暂停 → 分发通知前抛 ACPPromptPaused(on_event 不被调用)"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{"update":{}}}',
        "",
        'data: {"jsonrpc":"2.0","result":{"stopReason":"end_turn"},"id":3}',
        "",
    ]
    _install_stream(monkeypatch, client, _FakeStreamResponse(lines))
    events: list = []
    with pytest.raises(ACPPromptPaused):
        client._rpc(
            {"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3},
            on_event=events.append,
            pause_probe=lambda: True,
        )
    assert events == []  # 命中即停,不再转发


def test_rpc_no_pause_probe_behaves_normally(monkeypatch):
    """回归护栏:不传 pause_probe → 通知照常分发并返回最终结果"""
    client = ACPClient("http://fake")
    lines = [
        'data: {"jsonrpc":"2.0","method":"session/update","params":{"update":{}}}',
        "",
        'data: {"jsonrpc":"2.0","result":{"stopReason":"end_turn"},"id":3}',
        "",
    ]
    _install_stream(monkeypatch, client, _FakeStreamResponse(lines))
    events: list = []
    result = client._rpc(
        {"jsonrpc": "2.0", "method": "session/prompt", "params": {}, "id": 3},
        on_event=events.append,
        pause_probe=lambda: False,
    )
    assert result == {"stopReason": "end_turn"}
    assert len(events) == 1


# ============================================================
# 看门狗:静默期轮询空转检查暂停
# ============================================================


def test_watchdog_raises_on_pause_during_quiet(monkeypatch):
    """CLI 静默(无数据行):看门狗在 queue.Empty 分支命中暂停即抛"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.01)
    resp = _BlockingStreamResponse(hold_secs=2.0)
    # idle_probe 非 None 才启用看门狗;这里返回 False(无活动工具)
    gen = client._iter_lines_with_watchdog(resp, idle_probe=lambda: False, pause_probe=lambda: True)
    with pytest.raises(ACPPromptPaused):
        for _ in gen:
            pass


def test_watchdog_no_pause_when_probe_false(monkeypatch):
    """回归护栏:未暂停时看门狗不因暂停逻辑报错(CLI 结束后正常 return)"""
    client = ACPClient("http://fake")
    monkeypatch.setattr(acp_base, "_IDLE_POLL_SECONDS", 0.01)
    resp = _FakeStreamResponse([])  # 立即结束
    out = list(client._iter_lines_with_watchdog(resp, idle_probe=lambda: False, pause_probe=lambda: False))
    assert out == []


# ============================================================
# orchestrator._consume_paused_interrupt_flag(契约消费)
# ============================================================


class _FakeTask:
    def __init__(self, params):
        self.id = "task-1"
        self.params = params


class _FakeDb:
    def __init__(self, task=None):
        self.commits = 0
        self._task = task

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def get(self, *args, **kwargs):
        # P2-4:按轮会话不回退到外层游离 task——get 返回绑定的 task 对象,
        # 使 executor.run 对该 task.params 的中断改写可被同轮 _consume_paused_interrupt_flag 读到。
        # 未绑定(单元 consume 测试直接传 task)时返回 None。
        return self._task


def test_consume_paused_flag_absent_returns_false_no_commit():
    """builtin 任务不写标志 → 返回 False 且不落库(行为无变化)"""
    task = _FakeTask({"_plan": []})
    db = _FakeDb()
    assert _consume_paused_interrupt_flag(task, db) is False
    assert db.commits == 0


def test_consume_paused_flag_true_clears_and_commits():
    """命中标志 → 返回 True 并从 params 移除后落库(不影响下一轮)"""
    task = _FakeTask({"_plan": [], "_round_paused_interrupt": True})
    db = _FakeDb()
    assert _consume_paused_interrupt_flag(task, db) is True
    assert "_round_paused_interrupt" not in task.params
    assert task.params.get("_plan") == []  # 其它键保留
    assert db.commits == 1


def test_consume_paused_flag_commit_failure_returns_false():
    """P1-3:commit 抛错 → 返回 False(标志未从 DB 抹去,不假 park)+ rollback 归还会话"""
    class _BoomDb(_FakeDb):
        def commit(self):
            self.commits += 1
            raise RuntimeError("connection closed")

    task = _FakeTask({"_round_paused_interrupt": 3})
    db = _BoomDb()
    assert _consume_paused_interrupt_flag(task, db, 3) is False
    assert db.commits == 1


def test_consume_paused_flag_round_mismatch_is_ignored():
    """P1-3:标志轮号与当前轮不符(上一轮残留)→ 判未中断且不清除"""
    task = _FakeTask({"_round_paused_interrupt": 2})
    db = _FakeDb()
    assert _consume_paused_interrupt_flag(task, db, 3) is False
    assert task.params.get("_round_paused_interrupt") == 2  # 不属于本轮,不动
    assert db.commits == 0


def test_consume_paused_flag_round_match_consumes():
    """P1-3:标志轮号与当前轮一致 → 正常消费返回 True 并清除"""
    task = _FakeTask({"_plan": [], "_round_paused_interrupt": 3})
    db = _FakeDb()
    assert _consume_paused_interrupt_flag(task, db, 3) is True
    assert "_round_paused_interrupt" not in task.params
    assert db.commits == 1


# ============================================================
# orchestrator 恢复即自动续跑(CLI/ACP)
# ============================================================

import app.agents.orchestrator as orchestrator
from app.agents.orchestrator import (
    _resolve_continuation_message,
    _run_agent1_rounds_with_auto_continue,
)
from app.prompts.executor import AUTO_CONTINUE_FOLLOWUP


def _stub_loop_env(monkeypatch, *, auto_continue=True, max_rounds=8):
    """屏蔽续跑循环的外部副作用(埋点/落库/阻塞/按轮真实会话),保留循环本身逻辑。

    重构后循环每轮走真实 SessionLocal(_round_session)——测试环境无 DB,故用替身
    拦截:既避免连库挂起,又能记录"同时开启的会话数",供断言轮间 park 时不持有会话。
    返回 (parks, rec):parks 为 park 次数的 tids 列表;rec 携 open_at_park/会话计数。
    """
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "emit", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_save_plan_to_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "drain_user_messages", lambda tid: [])
    # 续轮前的工作区探活+重克隆:测试不涉仓库,直接返回 None(续轮 repo_ctx=None)
    monkeypatch.setattr(
        orchestrator, "_ensure_workspace_for_continuation", lambda *a, **k: None,
    )
    parks: list = []
    open_at_park: list[int] = []
    session_state = {"open": 0, "max_open": 0}
    # P2-4 后循环用 round_db.get(Task, id) 取本轮 task(不回退游离对象),
    # 故按轮会话需绑定一个跨轮持久的 task,executor 的中断改写才可见。
    shared_task = _FakeTask({})

    monkeypatch.setattr(
        orchestrator, "wait_if_paused",
        lambda tid, db=None: (
            parks.append(tid),
            open_at_park.append(session_state["open"]),
        ),
    )

    @contextmanager
    def _fake_round_session():
        session_state["open"] += 1
        session_state["max_open"] = max(
            session_state["max_open"], session_state["open"],
        )
        try:
            yield _FakeDb(shared_task)
        finally:
            session_state["open"] -= 1

    monkeypatch.setattr(orchestrator, "_round_session", _fake_round_session)
    monkeypatch.setattr(
        orchestrator.settings, "ACP_AUTO_CONTINUE_ON_RESUME", auto_continue,
    )
    monkeypatch.setattr(
        orchestrator.settings, "ACP_AUTO_CONTINUE_MAX_ROUNDS", max_rounds,
    )
    rec = {"open_at_park": open_at_park, "session": session_state}
    return parks, rec


class _RecordingExecutor:
    """按脚本驱动 executor.run:每次调用按 rounds 配置产出,并按需置中断标志。"""

    def __init__(self, interrupt_rounds: set[int]):
        self.name = "qoder_cli"
        self._interrupt = interrupt_rounds
        self.calls: list[dict] = []

    def run(self, task, db, *, round_idx, followup_query, client,
            repo_context, previous_plan):
        self.calls.append({"round_idx": round_idx, "followup_query": followup_query})
        if round_idx in self._interrupt:
            task.params = {**(task.params or {}), "_round_paused_interrupt": True}
        return ([], f"summary-r{round_idx}", [])


def test_auto_continue_interrupt_then_completes_next_round(monkeypatch):
    """第1轮被中断 → 恢复后同会话自动补发第2轮 → 第2轮正常完成才收尾。"""
    parks, rec = _stub_loop_env(monkeypatch)
    executor = _RecordingExecutor(interrupt_rounds={1})
    task = _FakeTask({})
    summaries: list[dict] = []

    results, summary, plan, final_round = _run_agent1_rounds_with_auto_continue(
        executor=executor, task=task, db=_FakeDb(), react_client=object(),
        start_round_idx=1, first_followup=None, first_repo_context=None,
        first_previous_plan=None, react_summaries=summaries,
    )

    assert final_round == 2
    assert summary == "summary-r2"
    assert [c["round_idx"] for c in executor.calls] == [1, 2]
    # 第2轮是续跑:同会话补发默认继续文案,且不重传仓库上下文
    assert executor.calls[1]["followup_query"] == AUTO_CONTINUE_FOLLOWUP
    assert len(summaries) == 2
    # 被中断轮阻塞过一次等恢复
    assert len(parks) == 1
    # Fix2.1 核心保证:轮间 wait_if_paused 时不持有任何会话(会话数=0)
    assert rec["open_at_park"] == [0]
    # 会话同时开启数始终≤1(每轮各开各关,无跨轮泄漏)
    assert rec["session"]["max_open"] == 1


def test_auto_continue_disabled_finalizes_partial(monkeypatch):
    """开关关闭:被中断 → 阻塞等恢复 → 退回旧行为,对半截当前轮收尾(不续跑)。"""
    parks, _ = _stub_loop_env(monkeypatch, auto_continue=False)
    executor = _RecordingExecutor(interrupt_rounds={1})
    task = _FakeTask({})

    _, summary, _, final_round = _run_agent1_rounds_with_auto_continue(
        executor=executor, task=task, db=_FakeDb(), react_client=object(),
        start_round_idx=1, first_followup=None, first_repo_context=None,
        first_previous_plan=None, react_summaries=[],
    )

    assert final_round == 1
    assert summary == "summary-r1"
    assert len(executor.calls) == 1
    assert len(parks) == 1  # 仍阻塞等恢复,只是不补发新轮


def test_builtin_never_continues(monkeypatch):
    """builtin(不写中断标志):首轮返回即收尾,不 park 不续跑(行为零变化)。"""
    parks, _ = _stub_loop_env(monkeypatch)
    executor = _RecordingExecutor(interrupt_rounds=set())
    task = _FakeTask({})

    _, summary, _, final_round = _run_agent1_rounds_with_auto_continue(
        executor=executor, task=task, db=_FakeDb(), react_client=object(),
        start_round_idx=1, first_followup=None, first_repo_context=None,
        first_previous_plan=None, react_summaries=[],
    )

    assert final_round == 1
    assert len(executor.calls) == 1
    assert len(parks) == 0


def test_auto_continue_respects_max_rounds(monkeypatch):
    """每轮都被中断:达续跑上限后停止补发,对最后一轮收尾(防无限起新轮)。"""
    _stub_loop_env(monkeypatch, auto_continue=True, max_rounds=2)
    # 1..9 都会中断,期望跑到 round_idx=1+2=3 因达上限而收尾
    executor = _RecordingExecutor(interrupt_rounds=set(range(1, 10)))
    task = _FakeTask({})

    _, summary, _, final_round = _run_agent1_rounds_with_auto_continue(
        executor=executor, task=task, db=_FakeDb(), react_client=object(),
        start_round_idx=1, first_followup=None, first_repo_context=None,
        first_previous_plan=None, react_summaries=[],
    )

    # start=1,续跑 cont_rounds = round_idx-1;上限2 → 最多补发到 round_idx=3
    assert final_round == 3
    assert summary == "summary-r3"
    assert [c["round_idx"] for c in executor.calls] == [1, 2, 3]


def test_resolve_continuation_message_default_when_no_pending(monkeypatch):
    """无排队消息(用户只点了恢复)→ 用系统默认继续文案。"""
    monkeypatch.setattr(orchestrator, "drain_user_messages", lambda tid: [])
    msg = _resolve_continuation_message(_FakeTask({}), _FakeDb(), round_idx=1)
    assert msg == AUTO_CONTINUE_FOLLOWUP


def test_resolve_continuation_message_uses_queued_user_text(monkeypatch):
    """有排队消息 → 用用户原文(而非默认),并补推入对话流。"""
    import uuid as _uuid

    mid = str(_uuid.uuid4())
    monkeypatch.setattr(
        orchestrator, "drain_user_messages",
        lambda tid: [{"content": "改成只审 src/", "message_id": mid}],
    )
    published = []
    monkeypatch.setattr(
        orchestrator, "publish",
        lambda tid, ev, data: published.append((ev, data)),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.update.return_value = 1
    db.query.return_value.filter.return_value.all.return_value = []

    msg = _resolve_continuation_message(_FakeTask({}), db, round_idx=2)
    assert msg == "改成只审 src/"
    # P2-3 摸轮:先 update 把排队消息挪到新轮首(一次写 + commit),
    # 再 all() 查回补推入对话流 → db.query 共调 2 次。
    assert db.query.call_count == 2
    db.query.return_value.filter.return_value.update.assert_called_once()


def test_run_had_tool_calls_any_across_range(monkeypatch):
    """整个 run 任一轮有工具调用就不当纯对话轮(防终轮纯文本误跳审查)。

    场景:第1轮(被中断)有工具,第2轮(终轮)纯文本 → 只看终轮会误跳。
    """
    from app.agents.orchestrator import _run_had_tool_calls

    def fake_round(_db, _tid, r):
        return r == 1  # 只有第1轮有工具

    monkeypatch.setattr(orchestrator, "_round_has_tool_calls", fake_round)
    # 区间 [1,2]:第1轮命中 → True(不跳过审查)
    assert _run_had_tool_calls(_FakeDb(), "t", 1, 2) is True
    # 单轮 [2,2]:第2轮无工具 → False(等价旧逐轮判定)
    assert _run_had_tool_calls(_FakeDb(), "t", 2, 2) is False
