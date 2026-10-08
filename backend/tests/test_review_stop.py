"""检查助手(agent2)终止检查:注册表 / agent2 检查点 / 流式收流 / 终止端点

背景:agent2 后台审查动辄数分钟(只读核查 / PoC / 引用复核多次 LLM 往返),
而有些对话并不需要检查 → 提供"终止检查"按钮。实现是协作式取消(不杀线程),
本文件覆盖它的四条契约:

1. app/review_stop.py 注册表:按轮登记、终止只作用于登记的那一轮、
   新一轮开始清掉遗留标志、无审查在跑时 request_stop 返回 None(路由据此就地收尾)
2. agent2.run_agent2:stop_check 命中时在循环边界退出,返回 stopped=True,
   且不再多付一次 LLM 往返
3. runtime.stream_llm + LLMClient.chat_stream:逐 chunk 的 stop_check 命中即
   关底层流并标 stopped(否则 16384-token 的调用要跑完才停,按钮像没生效)
4. POST /tasks/{id}/review/stop:仅 review_status=running 可停;
   无在跑审查(遗留角标)时就地写终态并推 review_done
"""
from unittest.mock import MagicMock

import pytest

import app.agents.agent2 as agent2
import app.review_stop as review_stop
from app.agents.runtime import llm_stream
from app.llm.client import LLMClient


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空注册表(进程级全局,避免用例间串味)"""
    review_stop._states.clear()
    yield
    review_stop._states.clear()


# ============================================================
# 1. 注册表:按轮登记的终止标志
# ============================================================


def test_stop_flag_scoped_to_registered_round():
    tid = "t-scope"
    review_stop.begin_review(tid, round_idx=1)
    assert review_stop.request_stop(tid) == 1
    assert review_stop.should_stop(tid, 1) is True
    # 别的轮次不受影响(追问开启的新审查必须正常跑)
    assert review_stop.should_stop(tid, 2) is False


def test_request_stop_without_active_review_returns_none():
    """没登记审查(线程未起/已死)→ None,且不落标志"""
    tid = "t-idle"
    assert review_stop.request_stop(tid) is None
    assert review_stop.should_stop(tid, 1) is False


def test_request_stop_is_idempotent():
    tid = "t-idem"
    review_stop.begin_review(tid, 3)
    assert review_stop.request_stop(tid) == 3
    assert review_stop.request_stop(tid) == 3
    assert review_stop.should_stop(tid, 3) is True


def test_new_round_clears_stale_stop_flag():
    """上一轮的终止请求不能泄漏到追问开启的新一轮(否则新审查一启动就被终止)"""
    tid = "t-stale"
    review_stop.begin_review(tid, 1)
    review_stop.request_stop(tid)
    review_stop.end_review(tid, 1)

    review_stop.begin_review(tid, 2)
    assert review_stop.should_stop(tid, 2) is False


def test_end_review_by_older_round_keeps_newer_registration():
    """并行收尾:老审查注销时不得清掉新轮刚登记的在跑审查"""
    tid = "t-parallel"
    review_stop.begin_review(tid, 1)
    review_stop.begin_review(tid, 2)          # 新轮接管
    review_stop.end_review(tid, 1)            # 老轮收尾(轮次不匹配 → 不清)
    assert review_stop.has_active_review(tid) is True
    assert review_stop.request_stop(tid) == 2  # 新轮仍可被终止
    review_stop.end_review(tid, 2)
    assert review_stop.has_active_review(tid) is False


def test_clear_review_stop_state_removes_entry():
    tid = "t-clear"
    review_stop.begin_review(tid, 1)
    review_stop.clear_review_stop_state(tid)
    assert review_stop.has_active_review(tid) is False


# ============================================================
# 2. agent2 工具循环:命中终止即退出,不再多付一次 LLM 往返
# ============================================================


def _final_json(content: str = "审查完成") -> str:
    return (
        '{"covered": [], "missing": [], "reasoning": %r, '
        '"suggestions": [], "results": [{"title": "k", "content": "c"}], '
        '"grouping": null}' % content
    )


def test_run_agent2_stops_at_loop_boundary(monkeypatch):
    """第一次流式返回后被终止 → stopped=True,且只发生一次 LLM 调用"""
    calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None,
                     stop_check=None):
        calls.append(1)
        # 模拟"思考结束后用户按下了终止"
        review_stop.begin_review(task_id, round_idx)
        review_stop.request_stop(task_id)
        return (_final_json(), [], "已思考一部分")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="t-agent2", round_idx=1, client=MagicMock(),
        stop_check=lambda: review_stop.should_stop("t-agent2", 1),
    )

    assert result["stopped"] is True
    assert result["results"] == []          # 部分输出不作审查结论
    assert len(calls) == 1


def test_run_agent2_stops_before_next_tool_batch(monkeypatch):
    """工具批次之间被终止 → 已请求的工具不再执行(读码核查白跑一次)"""
    read_calls = []
    monkeypatch.setattr(
        agent2, "_execute_read_tool",
        lambda *a, **k: read_calls.append(1) or "{}",
    )
    state = {"n": 0}

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None,
                     stop_check=None):
        state["n"] += 1
        if state["n"] == 1:
            # 首轮要求读码,但用户此刻按下终止
            review_stop.begin_review(task_id, round_idx)
            review_stop.request_stop(task_id)
            return ("", [{
                "id": "call_1", "index": 0, "name": "read_file",
                "arguments_str": '{"file_path": "a.py"}',
            }], "想读源码")
        return (_final_json(), [], "")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="t-agent2b", round_idx=1, client=MagicMock(),
        repo_path="/repo",
        stop_check=lambda: review_stop.should_stop("t-agent2b", 1),
    )

    assert result["stopped"] is True
    assert read_calls == []


def test_run_agent2_without_stop_check_is_unchanged(monkeypatch):
    """不传 stop_check(旧调用方):正常跑完,不会去碰注册表"""
    monkeypatch.setattr(
        agent2, "_stream_agent2_llm",
        lambda client, messages, *, task_id, round_idx, tools=None: (
            _final_json(), [], "思考"
        ),
    )
    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="t-none", round_idx=1, client=MagicMock(),
    )
    assert result.get("stopped") is not True
    assert result["results"]


# ============================================================
# 3. 流式层:逐 chunk 终止检查(命中即关流)
# ============================================================


class _FakeChunk:
    """LLMClient.chat_stream 产出的 StreamChunk 形状(供 stream_llm 层用例)"""

    def __init__(self, content=""):
        self.reasoning_delta = content
        self.content_delta = ""
        self.tool_call_deltas = []
        self.finish_reason = None


def _sdk_chunk(reasoning: str = ""):
    """OpenAI SDK chunk 形状(LLMClient.chat_stream 自己解析这个)"""
    from types import SimpleNamespace

    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content="", tool_calls=None, reasoning_content=reasoning),
        finish_reason=None,
    )])


class _FakeStream:
    """替身 OpenAI Stream:可迭代 + close()(chat_stream 命中终止时要关它)"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        self.closed = True


def _mk_llm_client(chunks):
    """构造一个只替底层 SDK 的 LLMClient(chat_stream 走真实代码路径)"""
    client = LLMClient(provider_id="deepseek", api_key="sk-test", model="test-model")
    stream = _FakeStream(chunks)
    sdk = MagicMock()
    sdk.chat.completions.create.return_value = stream
    client.client = sdk
    return client, stream


def test_chat_stream_stop_check_closes_underlying_stream():
    """逐 chunk 检查命中 → 关掉底层流(不关会把剩余 token 读完)并提前结束"""
    client, stream = _mk_llm_client(
        [_sdk_chunk("a"), _sdk_chunk("b"), _sdk_chunk("c")]
    )
    seen = []

    def stop_after_two():
        return len(seen) >= 2

    for chunk in client.chat_stream(
        [{"role": "user", "content": "hi"}], stop_check=stop_after_two,
    ):
        seen.append(chunk)

    assert len(seen) == 2
    assert stream.closed is True


def test_chat_stream_without_stop_check_reads_to_end():
    """不传 stop_check 时行为不变(存量调用方兼容面)"""
    client, stream = _mk_llm_client([_sdk_chunk("a"), _sdk_chunk("b")])
    out = list(client.chat_stream([{"role": "user", "content": "hi"}]))
    assert len(out) == 2
    assert stream.closed is False


def test_stream_llm_marks_result_stopped(monkeypatch):
    """runtime.stream_llm:命中 stop_check 时结果标 stopped=True 并推 end 事件

    (调用方据此走终止分支;推 end 让前端的流式思考卡正常收尾)
    """
    events = []
    monkeypatch.setattr(
        llm_stream, "publish",
        lambda task_id, etype, data: events.append((etype, data.get("phase"))),
    )
    monkeypatch.setattr(llm_stream, "perf_log", lambda *a, **k: None)

    client = MagicMock()
    flags = {"stop": False}
    counter = {"n": 0}

    def _fake_chat_stream(messages, **kwargs):
        stop_check = kwargs.get("stop_check")

        def gen():
            for _ in range(3):
                counter["n"] += 1
                if counter["n"] == 2:
                    flags["stop"] = True
                if stop_check is not None and stop_check():
                    return
                yield _FakeChunk("x")

        return gen()

    client.chat_stream = _fake_chat_stream

    result = llm_stream.stream_llm(
        client, [{"role": "user", "content": "hi"}],
        task_id="t-stream", round_idx=1, role="agent2",
        stop_check=lambda: flags["stop"],
    )

    assert result.stopped is True
    assert ("thinking_delta", "end") in events


def test_stream_llm_without_stop_check_not_marked(monkeypatch):
    """不传 stop_check 时 stopped 恒为 False(react_agent / verifier 路径不受影响)"""
    monkeypatch.setattr(llm_stream, "publish", lambda *a, **k: None)
    monkeypatch.setattr(llm_stream, "perf_log", lambda *a, **k: None)

    client = MagicMock()
    client.chat_stream = lambda messages, **kwargs: iter([_FakeChunk("a")])

    result = llm_stream.stream_llm(
        client, [{"role": "user", "content": "hi"}],
        task_id="t-stream2", round_idx=1, role="agent2",
    )
    assert result.stopped is False


# ============================================================
# 4. 终止端点:仅审查中可停;无在跑审查时就地收尾
# ============================================================


def _mk_running_task(review_status="running"):
    task = MagicMock()
    task.id = "t-endpoint"
    task.user_id = None            # 匿名任务:不做归属校验
    task.status = "completed"      # 后台审查发生在任务完成之后
    task.review_status = review_status
    task.current_stage = "检查助手审查中"
    return task


def _call_stop_endpoint(task, db=None):
    from app.routers.tasks import stop_task_review_endpoint

    db = db or MagicMock()
    db.get.return_value = task
    return stop_task_review_endpoint(task.id, db=db, current_user=None)


def test_stop_endpoint_sets_flag_and_keeps_running_status(monkeypatch):
    """有审查在跑:只置标志,终态留给审查线程写(单一写者)"""
    task = _mk_running_task()
    review_stop.begin_review(task.id, 1)
    published = []
    monkeypatch.setattr(
        "app.routers.tasks._publish_task_status", lambda t: published.append(t),
    )

    resp = _call_stop_endpoint(task)

    assert resp["review_status"] == "running"
    assert review_stop.should_stop(task.id, 1) is True
    assert task.review_status == "running"   # 不抢先写终态
    assert "正在终止" in task.current_stage


def test_stop_endpoint_finalizes_when_no_review_thread(monkeypatch):
    """遗留"检查中"角标(线程已死):端点代它收尾,否则前端永远卡住"""
    task = _mk_running_task()
    events = []
    monkeypatch.setattr(
        "app.routers.tasks._publish_task_status", lambda t: None,
    )
    monkeypatch.setattr(
        "app.routers.tasks.publish",
        lambda task_id, etype, data: events.append((etype, data)),
    )

    resp = _call_stop_endpoint(task)

    assert task.review_status == "stopped"
    assert ("review_done", {"review_status": "stopped"}) in events
    assert resp["review_status"] == "stopped"


def test_stop_endpoint_rejects_when_not_running():
    """审查已完成/失败/未开始 → 409(按钮只在"检查中"出现,后端同样把门)"""
    import fastapi

    task = _mk_running_task(review_status="done")
    with pytest.raises(fastapi.HTTPException) as exc:
        _call_stop_endpoint(task)
    assert exc.value.status_code == 409


def test_stop_endpoint_rejects_other_users_task():
    """任务有归属而当前用户为空 → 403(与 pause/resume 同款鉴权口径)"""
    import fastapi

    from app.routers.tasks import stop_task_review_endpoint

    task = _mk_running_task()
    task.user_id = "owner-id"
    db = MagicMock()
    db.get.return_value = task
    with pytest.raises(fastapi.HTTPException) as exc:
        stop_task_review_endpoint(task.id, db=db, current_user=None)
    assert exc.value.status_code == 403
