"""followup_recommender 单元测试(不连真实 DB)。

覆盖:
- _parse_suggestions:合法 / ```json``` 包裹 / 截断 3 条 / 过滤非法项 / 非 JSON → 空;
- recommend_followups:非空才落库、content 形状、失败静默、无对话不落库;
- _load_user_agent1_text:查询条件只读 user + agent1,绝不读 agent2;
- _assemble_within_budget:超长时留最新、整块丢最老,且不超预算;
- 硬时限:_chat 下发 stop_check(到点收流)、自建客户端套 timeout,
  传入的 client 不被修改。
"""
import json
import uuid
from unittest.mock import MagicMock

import pytest

from app.services import followup_recommender as fr


class _Chunk:
    def __init__(self, content_delta="", finish_reason=None):
        self.content_delta = content_delta
        self.finish_reason = finish_reason


class _FakeLLM:
    """按拼接文本分块产出;fail=True 时 chat_stream 抛异常。

    与真实实现对齐的两点:接受 stop_check 并在每个 chunk 边界尊重它(用于验证
    硬时限),记录收到的 kwargs(用于验证 max_tokens/stop_check 确实下发)。
    """

    def __init__(self, text="", fail=False):
        self._text = text
        self._fail = fail
        self.enable_thinking = True
        self.calls: list[dict] = []

    def chat_stream(self, messages, max_tokens=512, stop_check=None, **kwargs):
        self.calls.append({"max_tokens": max_tokens, "stop_check": stop_check})
        if self._fail:
            raise RuntimeError("LLM 调用失败(模拟)")
        # 模拟真实实现:到点即在 chunk 边界收流(已累积的输出交给调用方收尾)
        if stop_check is not None and stop_check():
            return
        yield _Chunk(content_delta=self._text)
        yield _Chunk(finish_reason="stop")


@pytest.fixture
def captured_records(monkeypatch):
    """替换 record_conversation,捕获落库参数。"""
    saved: list[dict] = []

    def fake_record(db, task, *, round_idx, role, type, content, **kw):
        conv = MagicMock()
        conv.id = f"conv-{uuid.uuid4()}"
        saved.append({"round_idx": round_idx, "role": role, "type": type, "content": content})
        return conv

    monkeypatch.setattr(fr, "record_conversation", fake_record)
    return saved


# ---------- _parse_suggestions ----------

def test_parse_suggestions_valid():
    got = fr._parse_suggestions('{"suggestions": ["查并发", "补错误处理"]}')
    assert got == ["查并发", "补错误处理"]


def test_parse_suggestions_fenced_block():
    got = fr._parse_suggestions('```json\n{"suggestions": ["看架构"]}\n```')
    assert got == ["看架构"]


def test_parse_suggestions_truncates_and_filters():
    got = fr._parse_suggestions(
        '{"suggestions": ["a", "b", "c", "d", "", "   ", 42, null]}'
    )
    assert got == ["a", "b", "c"]  # 截断到 3,过滤空白与非字符串


def test_parse_suggestions_non_json_returns_empty():
    assert fr._parse_suggestions("这不是 JSON") == []
    assert fr._parse_suggestions("") == []
    assert fr._parse_suggestions('{"suggestions": "非数组"}') == []
    assert fr._parse_suggestions('{"suggestions": []}') == []


# ---------- recommend_followups ----------

def test_recommend_records_on_valid(monkeypatch, captured_records):
    monkeypatch.setattr(fr, "_load_user_agent1_text", lambda db, tid: "用户: 只看代码质量\nAI: ...")
    llm = _FakeLLM('{"suggestions": ["再看架构分层", "再看安全性", "再看性能", "多余一条"]}')
    task = MagicMock()
    task.id = "t-1"
    task.user_input = "只看代码质量"

    fr.recommend_followups(task, MagicMock(), round_idx=1, client=llm)

    assert len(captured_records) == 1
    rec = captured_records[0]
    assert rec["role"] == "agent2"
    assert rec["type"] == "followup_suggestions"
    payload = json.loads(rec["content"])
    assert payload["source"] == "explore"
    assert len(payload["suggestions"]) == fr.MAX_FOLLOWUP_SUGGESTIONS  # 截断到 3


def test_recommend_no_record_when_empty_suggestions(monkeypatch, captured_records):
    monkeypatch.setattr(fr, "_load_user_agent1_text", lambda db, tid: "some conversation")
    fr.recommend_followups(MagicMock(id="t-2", user_input="x"), MagicMock(),
                           round_idx=1, client=_FakeLLM('{"suggestions": []}'))
    assert captured_records == []


def test_recommend_no_record_when_no_conversation(monkeypatch, captured_records):
    monkeypatch.setattr(fr, "_load_user_agent1_text", lambda db, tid: "")
    fr.recommend_followups(MagicMock(id="t-3", user_input="x"), MagicMock(),
                           round_idx=1, client=_FakeLLM('{"suggestions": ["不该被调用"]}'))
    assert captured_records == []


def test_recommend_fail_silent_on_llm_error(monkeypatch, captured_records):
    monkeypatch.setattr(fr, "_load_user_agent1_text", lambda db, tid: "conversation")
    # chat_stream 抛异常:recommend 不得抛出,且不落库
    fr.recommend_followups(MagicMock(id="t-4", user_input="x"), MagicMock(),
                           round_idx=1, client=_FakeLLM(fail=True))
    assert captured_records == []


def test_recommend_fail_silent_on_loader_error(monkeypatch, captured_records):
    def boom(db, tid):
        raise RuntimeError("DB 挂了")
    monkeypatch.setattr(fr, "_load_user_agent1_text", boom)
    # 不应抛出
    fr.recommend_followups(MagicMock(id="t-5", user_input="x"), MagicMock(),
                           round_idx=1, client=_FakeLLM('{"suggestions": ["a"]}'))
    assert captured_records == []


# ---------- 硬时限(不得拖住收尾) ----------

def test_chat_bounds_stream_with_stop_check(monkeypatch):
    """时限已过 → 一个 token 也不收,不落库半截建议"""
    monkeypatch.setattr(fr, "FOLLOWUP_TIMEOUT_SECONDS", -1.0)
    monkeypatch.setattr(fr, "_load_user_agent1_text", lambda db, tid: "conversation")
    fake = _FakeLLM('{"suggestions": ["不该落库"]}')
    captured: list = []

    def fake_record(db, task, **kw):
        captured.append(kw)
        return MagicMock()

    monkeypatch.setattr(fr, "record_conversation", fake_record)
    fr.recommend_followups(MagicMock(id="t-deadline", user_input="x"), MagicMock(),
                          round_idx=1, client=fake)

    assert fake.calls, "chat_stream 应被调用一次"
    assert callable(fake.calls[0]["stop_check"]), "硬时限靠 chat_stream 的 stop_check 生效"
    assert captured == []


def test_self_built_client_gets_hard_timeout(monkeypatch):
    """自建客户端必须套上 timeout + 不重试(默认值会把收尾拖到分钟级)"""
    inner = MagicMock(name="openai_client")
    wrapped = MagicMock(name="wrapped_client")
    inner.with_options.return_value = wrapped
    built = MagicMock(name="llm_client")
    built.client = inner
    monkeypatch.setattr(fr, "LLMClient", MagicMock(return_value=built))

    got = fr._new_llm_client()

    assert got is built
    assert got.client is wrapped, "with_options 的浅拷贝要回写,否则超时不生效"
    kwargs = inner.with_options.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"].read == pytest.approx(fr.FOLLOWUP_TIMEOUT_SECONDS)


def test_self_built_client_survives_timeout_wrap_failure(monkeypatch):
    """超时套不上(客户端形态变了)也应照常返回,不因止血手段失败而不推荐"""
    broken = MagicMock(name="llm_client")
    broken.client.with_options.side_effect = AttributeError("no with_options")
    monkeypatch.setattr(fr, "LLMClient", MagicMock(return_value=broken))

    assert fr._new_llm_client() is broken


def test_injected_client_is_not_mutated(monkeypatch):
    """传入的 client(测试替身/调用方指定模型)不得被套超时改写"""
    fake = _FakeLLM('{"suggestions": ["ok"]}')
    monkeypatch.setattr(fr, "_load_user_agent1_text", lambda db, tid: "conversation")
    monkeypatch.setattr(fr, "record_conversation", lambda db, task, **kw: MagicMock())
    constructor = MagicMock(name="LLMClient")
    monkeypatch.setattr(fr, "LLMClient", constructor)

    fr.recommend_followups(MagicMock(id="t-inject", user_input="x"), MagicMock(),
                          round_idx=1, client=fake)

    constructor.assert_not_called()
    assert fake.enable_thinking is True, "_chat 应在 finally 里恢复原值"


# ---------- _load_user_agent1_text 只读 user + agent1 ----------

def _collect_role_literals(captured_criteria):
    """从捕获的 filter 条件里抽出对 role 列取到的字符串字面量。"""
    values: set[str] = set()
    for crits in captured_criteria:
        for c in crits:
            left = getattr(c, "left", None)
            right = getattr(c, "right", None)
            if left is not None and getattr(left, "name", None) == "role":
                val = getattr(right, "value", None)
                if isinstance(val, str):
                    values.add(val)
    return values


def test_load_text_queries_only_user_and_agent1():
    captured: list[list] = []

    class _FakeQuery:
        def filter(self, *criteria):
            captured.append(list(criteria))
            return self

        def order_by(self, *args):
            return self

        def all(self):
            return []

    class _FakeDB:
        def query(self, model):
            return _FakeQuery()

    fr._load_user_agent1_text(_FakeDB(), "t-load")

    roles = _collect_role_literals(captured)
    assert roles <= {"user", "agent1"}, f"读取了越界的 role: {roles}"
    assert "agent2" not in roles
    # 两条查询:agent1 总结 + user 消息
    assert len(captured) == 2


# ---------- _assemble_within_budget:留最新,整块丢最老 ----------

def test_budget_keeps_newest_rounds_and_drops_oldest():
    """旧实现 conversation_text[:MAX] 留下的恰好是最老几轮(方向反了)"""
    long = "答" * fr.ANSWER_ROUND_CHARS
    text = fr._assemble_within_budget(["只看代码质量"], {1: long, 2: long, 3: long})

    assert "第 3 轮" in text
    assert "第 1 轮" not in text
    assert len(text) <= fr.MAX_FOLLOWUP_INPUT_CHARS


def test_budget_keeps_newest_user_lines():
    """用户消息也有自预算:从最新一条往前装(原始诉求另有 task.user_input 全文)"""
    lines = [f"第{i}条追问" + "问" * 400 for i in range(1, 12)]
    text = fr._assemble_within_budget(lines, {})

    assert "第11条追问" in text
    assert "第1条追问" not in text


def test_budget_output_stays_chronological():
    """装填按优先级倒序,输出仍按时间正序"""
    text = fr._assemble_within_budget(
        ["最初诉求", "中间追问", "最新追问"], {1: "r1", 2: "r2", 3: "r3"},
    )

    assert text.index("最初诉求") < text.index("最新追问")
    assert text.index("第 1 轮") < text.index("第 2 轮") < text.index("第 3 轮")


def test_budget_never_exceeds_cap():
    """预算含段头与段间分隔符:极端长对话也不超上限"""
    answers = {r: "x" * fr.ANSWER_ROUND_CHARS for r in range(1, 30)}
    lines = ["y" * fr.USER_LINE_CHARS for _ in range(30)]

    text = fr._assemble_within_budget(lines, answers)

    assert len(text) <= fr.MAX_FOLLOWUP_INPUT_CHARS


def test_short_conversation_is_not_rewritten():
    """不超预算时保持原口径(不丢内容、不加裁剪标记)"""
    text = fr._assemble_within_budget(["只看并发"], {1: "已回答并发与超时"})

    assert text == "【用户诉求】\n只看并发\n\n【AI助手回答·第 1 轮】\n已回答并发与超时"
