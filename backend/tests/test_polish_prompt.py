"""AI 润色(POST /tasks/polish_prompt)服务单测 + 端点测试。

服务层:注入假 client,验证文本收集、思考开关恢复、异常向上抛。
端点层:monkeypatch 服务函数,验证 200/422/502 三条路径。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import tasks as tasks_router
from app.services import prompt_polish


class _FakeChunk:
    def __init__(self, content_delta=None, finish_reason=None):
        self.content_delta = content_delta
        self.finish_reason = finish_reason


class _FakeClient:
    """最小 LLMClient 替身:只提供 enable_thinking 与 chat_stream。"""

    def __init__(self, text="润色后的描述", raise_exc=False):
        self.enable_thinking = True
        self._text = text
        self._raise = raise_exc
        self.last_messages = None

    def chat_stream(self, messages, *, max_tokens=None, tools=None):
        self.last_messages = messages
        if self._raise:
            raise RuntimeError("llm down")
        yield _FakeChunk(content_delta=self._text, finish_reason="stop")


def test_polish_prompt_service_collects_text_and_restores_thinking():
    c = _FakeClient(text="请审查本仓库的安全漏洞,给出文件位置。")
    out = prompt_polish.polish_prompt("审下安全问题", "代码审核", client=c)
    assert out == "请审查本仓库的安全漏洞,给出文件位置。"
    # 调用结束后恢复原思考开关(初值 True)
    assert c.enable_thinking is True
    # 场景名作为背景进入 user 消息(不新增维度)
    assert "代码审核" in c.last_messages[1]["content"]


def test_polish_prompt_service_propagates_error():
    with pytest.raises(RuntimeError):
        prompt_polish.polish_prompt("x", client=_FakeClient(raise_exc=True))


@pytest.fixture()
def api_client():
    app = FastAPI()
    app.include_router(tasks_router.router)
    return TestClient(app)


def test_polish_endpoint_returns_polished(api_client, monkeypatch):
    monkeypatch.setattr(
        tasks_router, "run_prompt_polish", lambda text, name: f"[润色]{text}"
    )
    r = api_client.post(
        "/tasks/polish_prompt",
        json={"text": "看看有没有sql注入", "scenario": "code_review"},
    )
    assert r.status_code == 200
    assert r.json()["polished"].startswith("[润色]")


def test_polish_endpoint_rejects_empty(api_client):
    r = api_client.post("/tasks/polish_prompt", json={"text": "   "})
    assert r.status_code == 422


def test_polish_endpoint_502_on_llm_error(api_client, monkeypatch):
    def boom(text, name):
        raise RuntimeError("no llm")

    monkeypatch.setattr(tasks_router, "run_prompt_polish", boom)
    r = api_client.post("/tasks/polish_prompt", json={"text": "任意内容"})
    assert r.status_code == 502
