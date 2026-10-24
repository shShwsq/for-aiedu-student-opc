"""acp_base 工具结果落库的多模态容错回归测试

背景(2026-10-23 生产事故):agent1 读取从 docx/pdf 抽出的图片时,Qoder 的
tool_call_update.rawOutput 返回的是多模态内容块列表而非字符串,直接写入
Conversation.content(Text 列)触发 psycopg "cannot adapt type 'dict'";
失败事务又未回滚,后续写库全部报 PendingRollbackError,任务被判 failed。

覆盖:
- _tool_result_text:图片块降级为占位文本,文本块保留
- _handle_tool_result:rawOutput 为多模态列表时落库内容仍是字符串
- 落库异常时 rollback,不向上抛(单条坏结果不拖垮整个任务)

数据取自真实事故日志(task=97dd61b1, r1_20261023_144100.jsonl)。
不连真实 DB:monkeypatch _add_conversation 与 publish。
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.agents import acp_base
from app.agents.acp_base import _ACPCollector, _tool_result_text

IMAGE_URL = (
    "https://qoder-vl-global.oss-ap-southeast-1.aliyuncs.com/cloud/"
    "rQSofS6RBYqHC7Z4MFzBE2aipe99HsUTPhl7JSXfZYEQWx67nSdfQgeSOUzZZ7b9/"
    "d91521c3-2b47-4f34-ad05-b380837144e8_1792766755875.png"
)

# 真实事故中 rawOutput 的形状
MULTIMODAL_RESULT = [
    {"type": "text", "text": "Binary content provided (1 item(s))."},
    {"type": "image", "source": {"type": "url", "url": IMAGE_URL}},
]


@pytest.fixture
def mock_task():
    task = MagicMock()
    task.id = f"test-task-{uuid.uuid4()}"
    return task


@pytest.fixture
def collector(mock_task, monkeypatch):
    """mock 掉落库与 SSE,捕获落库内容"""
    saved: list[dict] = []
    events: list[tuple] = []

    def fake_add_conversation(db, task, *, round_idx, role, type, content, **kw):
        saved.append({"type": type, "content": content})
        conv = MagicMock()
        conv.id = f"conv-{uuid.uuid4()}"
        return conv

    monkeypatch.setattr(acp_base, "_add_conversation", fake_add_conversation)
    monkeypatch.setattr(
        acp_base, "publish", lambda *a, **k: events.append((a, k)),
    )
    c = _ACPCollector(mock_task, MagicMock(), round_idx=1)
    return c, saved


def test_tool_result_text_flattens_image_blocks():
    """图片块降级为 [图片] + URL,不残留 dict/JSON 结构"""
    text = _tool_result_text(MULTIMODAL_RESULT)
    assert isinstance(text, str)
    assert "Binary content provided (1 item(s))." in text
    assert "[图片]" in text
    assert IMAGE_URL in text
    assert "source" not in text


def test_tool_result_text_passthrough_str():
    """普通字符串结果原样返回"""
    assert _tool_result_text("ok") == "ok"
    assert _tool_result_text(None) == ""


def test_handle_tool_result_with_multimodal_raw_output(collector):
    """rawOutput 为多模态列表时,落库 content 必须是字符串"""
    c, saved = collector
    c._handle_tool_result("call-image-1", {"rawOutput": MULTIMODAL_RESULT})

    assert len(saved) == 1
    assert saved[0]["type"] == "tool_result"
    assert isinstance(saved[0]["content"], str)
    assert "Binary content provided (1 item(s))." in saved[0]["content"]
    assert IMAGE_URL in saved[0]["content"]


def test_handle_tool_result_falls_back_to_content_blocks(collector):
    """rawOutput 为空、content 为多模态列表时,同样落库字符串"""
    c, saved = collector
    c._handle_tool_result("call-image-2", {"rawOutput": "", "content": MULTIMODAL_RESULT})

    assert len(saved) == 1
    assert isinstance(saved[0]["content"], str)


def test_add_conversation_failure_rolls_back(mock_task, monkeypatch):
    """落库抛错时回滚事务并吞掉异常,不向上传播"""
    def boom(*args, **kwargs):
        raise RuntimeError("cannot adapt type 'dict'")

    monkeypatch.setattr(acp_base, "_add_conversation", boom)
    monkeypatch.setattr(acp_base, "publish", lambda *a, **k: None)
    db = MagicMock()
    c = _ACPCollector(mock_task, db, round_idx=1)

    c._handle_tool_result("call-image-3", {"rawOutput": "ok"})

    db.rollback.assert_called_once()
