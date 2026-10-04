"""ACP 会话恢复(session/load / session/resume)单测。

覆盖:
- runtime/acp_session:能力解析、记录读写(含截断轮不推进)、恢复前置条件判定(纯函数)
- acp_bridge:回放通知排空(_drain_queue)与协议版本常量一致性(bridge 脚本可独立导入)
- ACPClient.restore_session:RPC 载荷形状 + 通知丢弃(不接 on_event)
- 恢复成功后按"复用"口径走注入去重;last_accepted_round 落后时增量回放
- 恢复失败异常分类:业务错误清记录,传输层瞬时失败保留记录
"""
from unittest.mock import MagicMock

import httpx

import app.agents.acp_bridge as acp_bridge
import app.agents.acp_base as acp_base
from app.agents.acp_base import (
    ACPClient,
    _context_sections_hash,
    _resolve_injection_plan,
    _restore_or_new_session,
)
from app.agents.runtime.acp_session import (
    ACP_SESSION_KEY,
    BRIDGE_PROTOCOL_REPLAY_SAFE,
    build_session_record,
    clear_session_record,
    load_session_record,
    parse_restore_method,
    plan_session_open,
    save_session_record,
)

QODER_INIT = {
    "protocolVersion": 1,
    "agentCapabilities": {
        "loadSession": True,
        "sessionCapabilities": {"resume": {}, "list": {}, "close": {}},
    },
}
# dsh:不声明标准 loadSession,只有扩展 resume
DSH_INIT = {
    "agentCapabilities": {"sessionCapabilities": {"resume": {}, "close": {}}},
}
# codex_bridge:capabilities 为空,无恢复能力
CODEX_INIT = {"capabilities": {}}


# ============================================================
# parse_restore_method:只看 CLI 自己的能力声明
# ============================================================


def test_parse_restore_prefers_extension_resume():
    """Qoder 两者都支持 → 取 session/resume(与 dsh 统一走扩展方法)。"""
    assert parse_restore_method(QODER_INIT) == "session/resume"
    assert parse_restore_method(DSH_INIT) == "session/resume"


def test_parse_restore_falls_back_to_standard_load():
    """只有 loadSession:true 无 resume 扩展 → 用标准 session/load。"""
    assert parse_restore_method({"agentCapabilities": {"loadSession": True}}) == "session/load"


def test_parse_restore_none_when_unsupported_or_malformed():
    """无能力声明 / 显式关闭 / 响应异常 → None(绝不尝试恢复)。"""
    assert parse_restore_method(CODEX_INIT) is None
    assert parse_restore_method({}) is None
    assert parse_restore_method(None) is None
    # 显式声明关闭不算支持
    assert parse_restore_method({"agentCapabilities": {"loadSession": False}}) is None
    assert parse_restore_method(
        {"agentCapabilities": {"sessionCapabilities": {"resume": False}}}
    ) is None
    # 字符串 "true" 不等于 True,不据此启用
    assert parse_restore_method({"agentCapabilities": {"loadSession": "true"}}) is None
    # capabilities 非 dict 也不能抛
    assert parse_restore_method({"agentCapabilities": "weird"}) is None


def test_bridge_protocol_constants_match():
    """bridge 上报的版本必须等于后端启用恢复的门限值(两处不能各自漂移)。"""
    assert acp_bridge.BRIDGE_PROTOCOL == BRIDGE_PROTOCOL_REPLAY_SAFE
    assert set(acp_bridge._RESTORE_METHODS) == {"session/load", "session/resume"}


# ============================================================
# 会话记录读写(task.params 下的 _acp_session)
# ============================================================


def _mk_task(params=None):
    task = MagicMock()
    task.id = "t1"
    task.params = params if params is not None else {}
    return task


def test_record_roundtrip_and_agent_gate():
    """记录可写回并读回;换执行器后不认旧记录(sessionId 属于别的 CLI)。"""
    task = _mk_task()
    record = build_session_record(
        agent_type="qoder_cli", session_id="s-1", cwd="/home/user/repos/x",
        injected_context_hash={"repo": "R", "memory": "M"}, round_idx=2,
    )
    assert record["prompt_accepted"] is True
    assert record["round_idx"] == 2
    save_session_record(MagicMock(), task, record)
    assert task.params[ACP_SESSION_KEY]["session_id"] == "s-1"

    loaded = load_session_record(task.params, "qoder_cli")
    assert loaded and loaded["cwd"] == "/home/user/repos/x"
    assert load_session_record(task.params, "deepseek_cli") is None


def test_truncated_round_does_not_advance_record():
    """截断兜底轮(idle 挂死/流中断降级收尾)不生成记录:该轮内容可能没被
    CLI 写进磁盘 transcript,round_idx 留在上一次干净轮,恢复后的增量回放
    才能把它补发给 CLI。"""
    assert build_session_record(
        agent_type="qoder_cli", session_id="s-1", cwd="/x",
        injected_context_hash={}, round_idx=4, truncated=True,
    ) is None
    assert build_session_record(
        agent_type="qoder_cli", session_id="s-1", cwd="/x",
        injected_context_hash={}, round_idx=4, truncated=False,
    )["round_idx"] == 4


def test_load_ignores_unusable_records():
    """缺 sessionId / 未成功送达 / 非 dict 的记录一律视为无记录。"""
    assert load_session_record(
        {ACP_SESSION_KEY: {"agent_type": "qoder_cli", "prompt_accepted": True}}, "qoder_cli"
    ) is None
    assert load_session_record(
        {ACP_SESSION_KEY: {"agent_type": "qoder_cli", "session_id": "s", "prompt_accepted": False}},
        "qoder_cli",
    ) is None
    assert load_session_record({ACP_SESSION_KEY: "garbage"}, "qoder_cli") is None
    assert load_session_record(None, "qoder_cli") is None


def test_clear_session_record_scoped_by_agent():
    """clear 带 agent_type 时只删该 CLI 的记录,别的不误删。"""
    task = _mk_task({ACP_SESSION_KEY: {"agent_type": "qoder_cli", "session_id": "s"}})
    clear_session_record(MagicMock(), task, "deepseek_cli")
    assert ACP_SESSION_KEY in task.params
    clear_session_record(MagicMock(), task, "qoder_cli")
    assert ACP_SESSION_KEY not in task.params


def test_record_io_swallows_errors_and_rollbacks():
    """DB 异常只告警不抛,且 rollback 让本轮后续 DB 操作可用
    (不回滚的话 SQLAlchemy session 停在 pending-rollback,后续全抛
    PendingRollbackError,"不影响执行轮"的承诺被打破)。"""
    boom = MagicMock()
    boom.commit.side_effect = RuntimeError("db down")
    task = _mk_task()
    save_session_record(
        boom, task,
        build_session_record(
            agent_type="qoder_cli", session_id="s", cwd="/x",
            injected_context_hash=None, round_idx=1,
        ),
    )
    assert boom.rollback.called
    boom.rollback.reset_mock()
    clear_session_record(boom, task, "qoder_cli")
    assert boom.rollback.called


# ============================================================
# plan_session_open:恢复前置条件
# ============================================================


def _record(**over):
    base = {
        "agent_type": "qoder_cli",
        "session_id": "s-9",
        "cwd": "/home/user/repos/x",
        "injected_context_hash": {"repo": "RH", "memory": "MH"},
        "prompt_accepted": True,
        "round_idx": 3,
    }
    base.update(over)
    return base


def test_plan_restore_happy_path():
    """四条件齐备 → 恢复,并带出记录里的指纹与覆盖轮次(避免重复注入/回放)。"""
    decision = plan_session_open(
        init_result=QODER_INIT, record=_record(),
        bridge_protocol=BRIDGE_PROTOCOL_REPLAY_SAFE, cwd="/home/user/repos/x",
    )
    assert decision["method"] == "session/resume"
    assert decision["session_id"] == "s-9"
    assert decision["cwd"] == "/home/user/repos/x"
    assert decision["injected_context_hash"] == {"repo": "RH", "memory": "MH"}
    assert decision["round_idx"] == 3


def test_plan_refuses_restore_case_by_case():
    """逐项不满足 → session/new,并给出可排查的 reason。"""
    cwd = "/home/user/repos/x"
    cases = [
        ({"init_result": CODEX_INIT, "record": _record(), "bridge_protocol": 2, "cwd": cwd},
         "cli_advertises_no_restore"),
        ({"init_result": QODER_INIT, "record": None, "bridge_protocol": 2, "cwd": cwd},
         "no_persisted_session"),
        # 老 bridge 会回放通知串进下一个请求 → 禁用恢复
        ({"init_result": QODER_INIT, "record": _record(), "bridge_protocol": 1, "cwd": cwd},
         "bridge_cannot_drain_replay"),
        # 沙箱重建/重新 clone 导致工作目录变化:CLI 按 cwd 定位会话,磁盘状态不可靠
        ({"init_result": QODER_INIT, "record": _record(), "bridge_protocol": 2,
          "cwd": "/home/user/repos/y"},
         "cwd_changed"),
    ]
    for kwargs, expected_reason in cases:
        decision = plan_session_open(**kwargs)
        assert decision["method"] is None, expected_reason
        assert decision["reason"] == expected_reason
        assert decision["injected_context_hash"] == {}
        assert decision["round_idx"] == 0


# ============================================================
# bridge 回放排空
# ============================================================


def test_drain_queue_discards_leftover_lines():
    """恢复后残留在队列里的回放行被取空丢弃(含哨兵行)。"""
    import queue

    q = queue.Queue()
    for item in [("line", '{"jsonrpc":"2.0","method":"session/update"}'), ("end", None)]:
        q.put(item)
    dropped = acp_bridge._drain_queue(q, quiet_seconds=0.05, max_seconds=1.0)
    assert dropped == 2
    assert q.empty()


def test_drain_queue_returns_immediately_when_empty():
    """队列已空时快速返回,不给每次恢复加固定等待。"""
    import queue
    import time

    q = queue.Queue()
    started = time.monotonic()
    assert acp_bridge._drain_queue(q, quiet_seconds=0.05, max_seconds=5.0) == 0
    assert time.monotonic() - started < 1.0


# ============================================================
# ACPClient.restore_session
# ============================================================


def test_restore_session_payload_and_discards_notifications():
    """按 ACP 要求带 sessionId/cwd/mcpServers,且不接 on_event(回放通知必须丢弃)。"""
    captured = {}

    client = ACPClient("http://bridge:8088")

    def fake_rpc(request, on_event=None, timeout=None, idle_probe=None):
        captured["request"] = request
        captured["on_event"] = on_event
        return {}

    client._rpc = fake_rpc
    client.restore_session("session/resume", "s-9", "/home/user/repos/x")

    assert captured["request"]["method"] == "session/resume"
    assert captured["request"]["params"] == {
        "sessionId": "s-9", "cwd": "/home/user/repos/x", "mcpServers": [],
    }
    # 通知交给默认(None):往轮回放内容绝不能进本轮 collector
    assert captured["on_event"] is None
    client.close()


# ============================================================
# 恢复成功后的注入判定等同"复用已有上下文"
# ============================================================


def test_restored_session_skips_reinject_and_replay():
    """恢复的 session 已含既往注入段 → 未变化的段跳过;轮次已追平则不回放。"""
    # 上一轮实注入的是 repo="" + memory="M",恢复记录里就该是它们的指纹
    restored_state = {
        "injected_context_hash": _context_sections_hash("", "M"),
        "prompt_accepted": True,
        "last_accepted_round": 3,
    }
    repo, mem, replay_from, _ = _resolve_injection_plan(restored_state, "", "M", round_idx=4)
    assert (repo, mem, replay_from) == ("", "", None)

    # 记忆内容变化(新归纳) → 只重发记忆段
    _, mem2, replay_from2, _ = _resolve_injection_plan(restored_state, "", "M-NEW", round_idx=4)
    assert mem2 == "M-NEW" and replay_from2 is None

    # 记录轮次落后(其后有失败轮)→ 增量回放,注入段仍按指纹去重
    _, _, replay_from3, _ = _resolve_injection_plan(restored_state, "", "M", round_idx=5)
    assert replay_from3 == 3


# ============================================================
# _restore_or_new_session:恢复/新建与降级
# ============================================================


class _FakeClient:
    """只记录调用的 ACPClient 替身"""

    def __init__(self, *, restore_ok=True, restore_error=None, new_error=None):
        self.calls: list[tuple] = []
        self.restore_ok = restore_ok
        self.restore_error = restore_error
        self.new_error = new_error

    def restore_session(self, method, session_id, cwd):
        self.calls.append(("restore", method, session_id, cwd))
        if self.restore_error is not None:
            raise self.restore_error
        if not self.restore_ok:
            raise RuntimeError("ACP 错误 -32603: session not found")
        return {}

    def new_session(self, cwd=None):
        self.calls.append(("new", cwd))
        if self.new_error:
            raise self.new_error
        return "new-session-1"


def _open(client, decision, **over):
    kwargs = dict(db=MagicMock(), task=_mk_task(), agent_type="qoder_cli",
                  session=MagicMock(), bridge_exec_id="exec-1", cwd="/home/user/repos/x")
    kwargs.update(over)
    return _restore_or_new_session(client, decision, **kwargs)


def test_restore_success_skips_new_session():
    """恢复成功 → 不再新建 session(上下文由 CLI 从磁盘复原)。"""
    client = _FakeClient()
    decision = {"method": "session/resume", "session_id": "s-9", "cwd": "/home/user/repos/x"}
    session_id, restored, outcome = _open(client, decision)
    assert (session_id, restored, outcome) == ("s-9", True, "restored")
    assert [c[0] for c in client.calls] == ["restore"]


def test_restore_failure_degrades_to_new_and_clears_record(monkeypatch):
    """恢复业务性失败(会话不存在等 JSON-RPC 错误)→ 降级 session/new 并清记录。"""
    cleared = []
    monkeypatch.setattr(
        acp_base, "clear_session_record",
        lambda db, task, agent_type=None: cleared.append(agent_type),
    )
    client = _FakeClient(restore_ok=False)
    decision = {"method": "session/load", "session_id": "gone", "cwd": "/home/user/repos/x"}
    session_id, restored, outcome = _open(client, decision)
    assert (session_id, restored, outcome) == ("new-session-1", False, "restore_failed_new")
    assert cleared == ["qoder_cli"]
    assert client.calls[0][0] == "restore" and client.calls[1][0] == "new"


def test_restore_transient_failure_keeps_record(monkeypatch):
    """恢复传输层瞬时失败(120s 读超时/bridge 瞬断/流中断)→ 降级新建但**保留
    记录**:磁盘上的会话多半仍完好,下轮再试恢复,不清就永久退回文本回放。"""
    cleared = []
    monkeypatch.setattr(
        acp_base, "clear_session_record",
        lambda db, task, agent_type=None: cleared.append(agent_type),
    )
    for transient_error in (
        httpx.ConnectError("bridge 瞬断"),
        httpx.ReadTimeout("恢复超大 transcript 超 120s"),
        acp_base.ACPStreamAborted("CLI 崩溃"),
    ):
        client = _FakeClient(restore_error=transient_error)
        decision = {"method": "session/resume", "session_id": "s-9", "cwd": "/home/user/repos/x"}
        session_id, restored, outcome = _open(client, decision)
        assert (session_id, restored, outcome) == ("new-session-1", False, "restore_failed_new")
        assert cleared == [], f"{type(transient_error).__name__} 不应清除记录"
        assert client.calls[0][0] == "restore" and client.calls[1][0] == "new"


def test_no_restore_method_goes_straight_to_new_session(monkeypatch):
    """无恢复能力/无记录 → 直接新建,不碰 restore_session(与旧行为一致)。"""
    boom = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不该尝试恢复"))
    monkeypatch.setattr(acp_base, "clear_session_record", boom)
    client = _FakeClient()
    session_id, restored, outcome = _open(client, {"method": None, "reason": "no_persisted_session"})
    assert (session_id, restored, outcome) == ("new-session-1", False, "new")
    assert [c[0] for c in client.calls] == ["new"]


def test_new_session_failure_appends_cli_stderr(monkeypatch):
    """新建失败时把 bridge 里的 CLI stderr 附进异常(-32603 只有泛化消息)。"""
    monkeypatch.setattr(
        acp_base, "_extract_bridge_error",
        lambda session, exec_id, agent_type: "Traceback: boom",
    )
    client = _FakeClient(new_error=RuntimeError("ACP 错误 -32603: Internal error"))
    try:
        _open(client, {"method": None})
        raise AssertionError("应抛出 RuntimeError")
    except RuntimeError as e:
        assert "Internal error" in str(e)
        assert "[CLI 日志]" in str(e) and "Traceback: boom" in str(e)
