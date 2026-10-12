"""followup_recommender 单元测试(不连真实 DB)。

覆盖:
- _parse_suggestions:合法 / ```json``` 包裹 / 截断 3 条 / 过滤非法项 / 非 JSON → 空;
- recommend_followups:非空才落库、content 形状、失败静默、无对话不落库;
- _load_user_agent1_text:查询条件只读 user + agent1,绝不读 agent2。

monkeypatch record_conversation 与 _load_user_agent1_text,用假 LLM client。
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
    """按拼接文本分块产出;fail=True 时 chat_stream 抛异常。"""

    def __init__(self, text="", fail=False):
        self._text = text
        self._fail = fail
        self.enable_thinking = True

    def chat_stream(self, messages, max_tokens=512):
        if self._fail:
            raise RuntimeError("LLM 调用失败(模拟)")
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
