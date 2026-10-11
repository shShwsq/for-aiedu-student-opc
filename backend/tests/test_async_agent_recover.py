"""异步后台子 Agent 提前收尾的续轮回收逻辑单元测试

覆盖:
- _looks_like_pending_async_report:命中"在等后台结果"口吻(中英),完整报告不误判
- _is_async_agent_launch:派生检测(工具名宽松 + 回执必须含异步标记)
- _needs_async_agent_recovery:末尾窗口口吻 / 文本没实质内容 → 需要回收;
  长完整报告(或正文中段带技术用词但末尾干净)→ 不回收
- _should_recover_async_agent:主判据是"本轮派生过后台子 Agent",且文本判据只看
  **最后一次派生回执之后**的收尾(派生之前写的正文不参与);首轮被兜底截断 /
  开关关闭 / executor 不在白名单都不触发
- _async_agent_autoccontinue_enabled:全局开关 + agent 类型白名单
- _async_agent_collect_results:交付 / 续等后交付 / 耗尽 / 连接异常,以及
  **被兜底截断**(prompt() 吞异常返回 {})与 stopReason 非 end_turn 都必须判
  "仍不完整"(回归:过去只看文本口吻,把半份报告标成"已回收完整报告")
- 报告堆叠防护:实质内容 + 仍带等待口吻 / 连续两次输出重复 → 停止补发
- _ACPCollector.publish_note:进度与结论提示的相位(别把成功提示渲染成错误)

背景:Qoder 的 Agent 工具 fire-and-forget,模型常在子 Agent 未回报时就 end_turn,
平台据此显"已完成";修复在同一活跃 session 上补发 prompt 回收结果。
"""
from types import SimpleNamespace

import pytest

from app.agents import acp_base as A
from app.config import settings


# ============================================================
# 待完成口吻判定
# ============================================================

def test_pending_report_matches_english_and_chinese():
    assert A._looks_like_pending_async_report(
        "INTERIM: launched 2 background sub-agents, will deliver combined report after they finish."
    )
    assert A._looks_like_pending_async_report(
        "已完成初步排查。等待 4 路并行审计代理返回详细结果后我会给完整报告。"
    )
    assert A._looks_like_pending_async_report("still waiting for the other sub-agents")


def test_pending_report_ignores_full_report():
    # 真实完整报告:不含"在等结果"口吻 → 不误判
    assert not A._looks_like_pending_async_report(
        "# 1. JWT 安全陷阱\n- alg:none 混淆\n# 2. SQL 注入\n- 使用参数化查询防止注入"
    )
    assert not A._looks_like_pending_async_report("")


# ============================================================
# Agent 异步派生检测
# ============================================================

_ASYNC_ACK = (
    "Async agent launched successfully.\n"
    "The agent is working in the background. You will be notified automatically "
    "when it completes."
)


def test_async_launch_regex_matches_qoder_ack():
    assert A._ASYNC_AGENT_LAUNCH_RE.search(_ASYNC_ACK)
    assert not A._ASYNC_AGENT_LAUNCH_RE.search("子任务完成,发现 3 处问题")


def test_is_async_agent_launch_loose_tool_name_but_strict_ack():
    # 工具名因 CLI 而异(Agent / Task / subagent),宽松匹配
    assert A._is_async_agent_launch("Agent", _ASYNC_ACK)
    assert A._is_async_agent_launch("Task", _ASYNC_ACK)
    assert A._is_async_agent_launch("  subagent ", _ASYNC_ACK)
    # 但名字对、回执不是异步派生 → 不算(否则同步子任务会白补发一轮)
    assert not A._is_async_agent_launch("Agent", "子任务完成,发现 3 处问题")
    # 回执像异步派生、名字不是子智能体工具 → 也不算
    assert not A._is_async_agent_launch("Bash", _ASYNC_ACK)
    assert not A._is_async_agent_launch("", _ASYNC_ACK)


# ============================================================
# 是否需要续轮回收
# ============================================================

@pytest.fixture()
def _min_chars_100(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS", 100)


def test_recovery_needed_for_pending_tone_tail(_min_chars_100):
    assert A._needs_async_agent_recovery(
        "已启动 3 个子任务,结果稍后汇总给你。"
    )


def test_recovery_needed_when_no_substance(_min_chars_100):
    # 提前 end_turn 常只留一句"子任务已启动",不含等待字样:靠篇幅兜住
    assert A._needs_async_agent_recovery("已并行启动 3 个审计子任务。")
    assert A._needs_async_agent_recovery("")


def test_recovery_skipped_for_delivered_full_report(_min_chars_100):
    # 实质内容 + 末尾无等待口吻 → 结果已给全,不再补发(避免报告堆叠)
    full = "# 审查报告\n" + "- 参数化查询避免注入;" * 30
    assert not A._needs_async_agent_recovery(full)


def test_recovery_skipped_when_tech_word_outside_tail_window(_min_chars_100):
    # "等待/waiting for"只出现在正文中段的技术表述里(超出末尾窗口),末尾是
    # 干净结论 → 不误判为"还在等子 Agent 回报"
    head = "并发代码里存在等待锁未释放的风险,以及 waiting for 队列的超时设置。"
    body = "- 建议:为每个锁设置超时时间; " * 120
    text = head + body + "\n综上,本轮共 2 处高危,均已给出修复方案。"
    assert len(text) > A._ASYNC_REPORT_TAIL_WINDOW
    assert not A._needs_async_agent_recovery(text)


def test_min_chars_zero_disables_substance_gate(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS", 0)
    # 阈值关掉后只按口吻判定:长报告无口吻 → 不回收;有口吻 → 回收
    assert not A._needs_async_agent_recovery("结论:发现 2 处高危。" + "- 说明;" * 40)
    assert A._needs_async_agent_recovery("结论:发现 2 处高危,其余等待子任务。")


class _GateCollector:
    def __init__(self, content_full="", launch_count=1, tail_start=0):
        self.content_full = content_full
        self.async_agent_launch_count = launch_count
        self.async_agent_launch_tail_start = tail_start


class _GateClient:
    def __init__(self, truncated=None):
        self.last_prompt_truncated = truncated


@pytest.fixture()
def _gate_on(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", True)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_TYPES", "qoder_cli")
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS", 100)


def test_should_recover_requires_launch(_gate_on):
    col = _GateCollector("结果稍后给你", launch_count=0)
    assert A._should_recover_async_agent(col, _GateClient(), "qoder_cli") is False


def test_should_recover_skips_truncated_first_round(_gate_on):
    # 首轮已被兜底截断:残留文本不可信,不再补发(由收尾统一标注不完整)
    col = _GateCollector("结果稍后给你")
    assert A._should_recover_async_agent(
        col, _GateClient(truncated="idle timeout"), "qoder_cli"
    ) is False


def test_should_recover_honors_executor_allowlist(_gate_on):
    col = _GateCollector("结果稍后给你")
    assert A._should_recover_async_agent(col, _GateClient(), "deepseek_cli") is False


def test_should_recover_for_early_end_turn(_gate_on):
    assert A._should_recover_async_agent(
        _GateCollector("已启动子任务,等待返回"), _GateClient(), "qoder_cli"
    ) is True


def test_should_recover_judges_only_text_after_launch_ack(_gate_on):
    # 派生回执之前写的正文不参与判定(哪怕里面有"等待"):派生后没再写字 → 需回收
    before = "先说明这里存在等待锁未释放的风险。" * 10
    col = _GateCollector(before, launch_count=1, tail_start=len(before))
    assert A._should_recover_async_agent(col, _GateClient(), "qoder_cli") is True
    # 派生后写出了实质结论且末尾无等待口吻 → 视为已交付,不补发(避免报告堆叠)
    after = "# 阶段性完整结论\n" + "- 已定位 3 处高危并给出修复方案;" * 20
    col2 = _GateCollector(before + after, launch_count=1, tail_start=len(before))
    assert A._should_recover_async_agent(col2, _GateClient(), "qoder_cli") is False


# ============================================================
# 启用门控
# ============================================================

def test_autoccontinue_gate_flag_and_type(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", True)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_TYPES", "qoder_cli,codex_cli")
    assert A._async_agent_autoccontinue_enabled("qoder_cli") is True
    assert A._async_agent_autoccontinue_enabled("deepseek_cli") is False

    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", False)
    assert A._async_agent_autoccontinue_enabled("qoder_cli") is False


# ============================================================
# 续轮回收循环
# ============================================================

class _StubCollector:
    def __init__(self, initial: str = ""):
        self.content_full = initial
        self.has_active_tools = False
        self.async_agent_launch_count = 2


class _Resp:
    """一次续轮 prompt 的结果,按 ACPClient.prompt() 的真实契约模拟:
    正常 → 返回 {"stopReason": ...};被兜底截断 → 置 last_prompt_truncated
    并返回 {}(截断前流出的文本仍留在 collector 里)。
    """

    def __init__(self, text="", *, truncated=None, stop_reason="end_turn"):
        self.text = text
        self.truncated = truncated
        self.stop_reason = stop_reason


class _StubClient:
    """每次 prompt 消费一条预设结果;遇 BaseException 则抛出(连接层异常路径)"""

    def __init__(self, collector, responses):
        self._collector = collector
        self._responses = responses
        self._i = 0
        self.calls = 0
        self.last_prompt_truncated = None

    def prompt(self, sid, msgs, on_event=None, idle_probe=None):
        item = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        # prompt() 每次调用都会重置截断标记,只反映本次调用的结果
        self.last_prompt_truncated = item.truncated
        self._collector.content_full += item.text
        if item.truncated:
            return {}
        return {"stopReason": item.stop_reason}


@pytest.fixture()
def _fast_settings(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_MAX_CONTINUE", 3)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_CONTINUE_WAIT_SECONDS", 0)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_CONTINUE_BACKOFF_FACTOR", 1.0)
    # 实质内容阈值:够小便于构造"长报告",够大以区分零碎回复
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_DELIVERED_MIN_CHARS", 100)


task = SimpleNamespace(id="t-verify")


def _collect(collector, client, ctx=None):
    ctx = ctx if ctx is not None else {"continued": True, "still_pending": True}
    A._async_agent_collect_results(
        client, "sess", collector, ctx, task=task, round_idx=1, agent_type="qoder_cli"
    )
    return ctx


def test_continue_delivers_on_first_followup(_fast_settings):
    col = _StubCollector("INTERIM: will deliver report after they finish.")
    client = _StubClient(col, [_Resp("# Complete combined report: JWT + SQL findings.")])
    ctx = _collect(col, client)
    assert client.calls == 1
    assert ctx["still_pending"] is False
    assert "Complete combined report" in col.content_full


def test_continue_loops_until_delivered(_fast_settings):
    col = _StubCollector("waiting for the background agents")
    client = _StubClient(col, [
        _Resp("still waiting for sub-agent B"),
        _Resp("# Final full report delivered now."),
    ])
    ctx = _collect(col, client)
    assert client.calls == 2
    assert ctx["still_pending"] is False


def test_continue_exhausts_max_attempts(_fast_settings):
    col = _StubCollector("will report after they finish")
    # 短文本 + 每次内容不同 → 既不达实质阈值也不判重复,一直补发到上限
    client = _StubClient(col, [
        _Resp("仍在等待子任务甲的结果"),
        _Resp("仍在等待子任务乙的结果"),
        _Resp("仍在等待子任务丙的结果"),
    ])
    ctx = _collect(col, client)
    assert client.calls == settings.ACP_ASYNC_AGENT_MAX_CONTINUE
    assert ctx["still_pending"] is True


def test_continue_stops_on_connection_error(_fast_settings):
    col = _StubCollector("waiting")
    client = _StubClient(col, [ConnectionError("bridge down")])
    ctx = _collect(col, client)
    assert client.calls == 1
    assert ctx["still_pending"] is True


def test_truncated_continuation_is_not_marked_complete(_fast_settings):
    """回归:prompt() 把 idle 挂死/流中断吞成"截断 + 返回 {}",残留的半份报告
    口吻上看着像成品 —— 只按文本判断会把不完整结果标成"已回收完整报告"。"""
    half = "# 审查报告(写到一半)\n- JWT 校验缺失\n- SQL 拼接"
    col = _StubCollector("等待子任务返回")
    client = _StubClient(col, [_Resp(half, truncated="idle timeout after 300s")])
    ctx = _collect(col, client)
    assert client.calls == 1
    assert ctx["still_pending"] is True


def test_non_end_turn_stop_reason_marks_pending(_fast_settings):
    col = _StubCollector("等待子任务返回")
    client = _StubClient(col, [_Resp("# 报告", stop_reason="max_tokens")])
    ctx = _collect(col, client)
    assert client.calls == 1
    assert ctx["still_pending"] is True


def test_substantive_output_with_pending_tone_stops_stacking(_fast_settings):
    """已拿到实质报告但末尾仍带"等待"字样:停止补发(再问一遍只会让模型把
    同一份报告重复输出一遍堆进 summary),同时保留"可能不完整"的诚实标注。"""
    report = "# 合并报告\n- JWT / SQL / SSRF 三类问题定位完毕。" * 6 + "\n仍有 1 路等待。"
    col = _StubCollector("等待子任务返回")
    client = _StubClient(col, [_Resp(report)])
    ctx = _collect(col, client)
    assert client.calls == 1
    assert ctx["still_pending"] is True


def test_identical_repeat_stops_loop(_fast_settings):
    col = _StubCollector("等待子任务返回")
    same = "仍在等待子任务返回结果"
    client = _StubClient(col, [_Resp(same)])
    ctx = _collect(col, client)
    # 阈值 100 > 文本长度 → 走"与上次重复"判据,第 2 次即停
    assert client.calls == 2
    assert ctx["still_pending"] is True


def test_empty_output_keeps_polling(_fast_settings):
    col = _StubCollector("等待子任务返回")
    client = _StubClient(col, [_Resp("")])
    ctx = _collect(col, client)
    assert client.calls == settings.ACP_ASYNC_AGENT_MAX_CONTINUE
    assert ctx["still_pending"] is True


def test_max_continue_zero_makes_no_prompt(_fast_settings, monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_MAX_CONTINUE", 0)
    col = _StubCollector("等待子任务返回")
    client = _StubClient(col, [_Resp("# 报告")])
    ctx = _collect(col, client)
    assert client.calls == 0
    assert ctx["still_pending"] is True


# ============================================================
# 系统提示相位(前端别把成功提示渲染成 [错误])
# ============================================================

class _NoteCollector:
    """只带 publish_note 需要的最小状态,并记录懒启动是否被调用"""

    publish_note = A._ACPCollector.publish_note

    def __init__(self):
        self.task = SimpleNamespace(id="t-verify")
        self.round_idx = 1
        self.iteration = 2
        self.current_conv_id = "conv-1"
        self.started = False

    def _ensure_iter_started(self):
        self.started = True


def _capture_publish(monkeypatch):
    events = []
    monkeypatch.setattr(A, "publish", lambda task_id, etype, data: events.append(data))
    return events


def test_publish_note_progress_writes_into_live_card(monkeypatch):
    events = _capture_publish(monkeypatch)
    col = _NoteCollector()
    col.publish_note("[正在续轮回收结果…]")
    assert events[0]["phase"] == "reasoning"
    # 整轮没有活跃文本卡时先补 start,否则提示挂在一个没收过 start 的 conv_id 上
    assert col.started is True


def test_publish_note_success_is_not_an_error(monkeypatch):
    events = _capture_publish(monkeypatch)
    _NoteCollector().publish_note("[已回收后台子任务结果并合并为完整报告]", terminal=True)
    assert events[0]["phase"] == "notice"


def test_publish_note_incomplete_stays_error(monkeypatch):
    events = _capture_publish(monkeypatch)
    _NoteCollector().publish_note(
        "[后台子任务未全部回报]", terminal=True, level="error",
    )
    assert events[0]["phase"] == "error"
