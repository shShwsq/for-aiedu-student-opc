"""create_task 请求归一化(_normalize_request)的空内容守卫单元测试。

回归点:创建链路此前用 Python 真值 `if req.user_input` 判空,纯空白 user_input
(如 "   ")会原样落库,build_first_round_question 返回近乎空的首轮 question,
agent1 如实回复"用户发的是空消息"。修复后判空用 strip 后真值(与 /messages
端点一致),空串/纯空白都走 repo_url 兜底或最终 422。

不连 DB,只测纯函数 _normalize_request 的分支。
"""
import pytest
from fastapi import HTTPException

from app.routers.tasks import _normalize_request
from app.schemas.task import TaskCreateRequest


def test_whitespace_only_input_without_repo_rejected():
    """纯空白 user_input 且无 repo_url → 422(改前会返回 ("   ", None))"""
    req = TaskCreateRequest(user_input="   ")
    with pytest.raises(HTTPException) as exc:
        _normalize_request(req)
    assert exc.value.status_code == 422


def test_whitespace_only_input_falls_back_to_repo_when_present():
    """纯空白 user_input 但有 repo_url → 兜底生成非空意图(改前会留空白串)"""
    req = TaskCreateRequest(user_input="   ", repo_url="https://github.com/foo/bar")
    user_input, params = _normalize_request(req)
    assert user_input.strip()  # 关键:落库内容非空
    assert "foo/bar" in user_input
    assert params.get("repo_url") == "https://github.com/foo/bar"


def test_valid_input_is_trimmed():
    """正常 user_input 前后空白被 strip 后落库(改前原样保留噪声空白)"""
    req = TaskCreateRequest(user_input="  审计这个仓库  ")
    user_input, _params = _normalize_request(req)
    assert user_input == "审计这个仓库"


def test_repo_only_still_generates_input():
    """兼容旧 API:仅 repo_url 生成通用意图(守卫不应影响该路径)"""
    req = TaskCreateRequest(user_input=None, repo_url="https://github.com/foo/bar")
    user_input, params = _normalize_request(req)
    assert user_input == "请处理这个仓库: https://github.com/foo/bar"
    assert params.get("repo_url") == "https://github.com/foo/bar"


def test_neither_input_nor_repo_rejected():
    """既无 user_input 又无 repo_url → 422"""
    req = TaskCreateRequest(user_input=None)
    with pytest.raises(HTTPException) as exc:
        _normalize_request(req)
    assert exc.value.status_code == 422
