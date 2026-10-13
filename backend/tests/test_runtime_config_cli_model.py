"""PATCH /tasks/{id}/runtime_config 的 CLI 模型参数分支单测

直接调用端点函数(桩 db),覆盖回归点:
- qoder_cli / deepseek_cli:写入 task.params(model/reasoning_effort/context_window)
- context_window 仅 qoder_cli 落库(deepseek 忽略不报错)
- 空字符串 / 0 清除对应键
- 内置执行器与 codex_cli 传入 CLI 参数报 400
- 未触碰 CLI 参数时不动 task.params
"""
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.routers import tasks as tasks_router
from app.schemas.task import RuntimeConfigUpdateRequest


def _make_task(executor: str, params: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,            # 匿名任务:跳过归属校验与模型 id 校验
        executor=executor,
        params=params if params is not None else {},
        llm_config_id=None,
        react_llm_config_id=None,
    )


def _call(task, req):
    """用桩 db 调用端点函数;db.get 返回 task,commit/refresh 空操作"""
    db = MagicMock()
    db.get.return_value = task
    return tasks_router.update_task_runtime_config(task.id, req, db, None)


def test_qoder_cli_writes_all_model_params():
    task = _make_task("qoder_cli", {})
    _call(task, RuntimeConfigUpdateRequest(
        model="Qwen3.8-Max", reasoning_effort="high", context_window=200000,
    ))
    assert task.params["model"] == "Qwen3.8-Max"
    assert task.params["reasoning_effort"] == "high"
    assert task.params["context_window"] == 200000


def test_deepseek_cli_ignores_context_window():
    task = _make_task("deepseek_cli", {})
    _call(task, RuntimeConfigUpdateRequest(
        model="deepseek-v4-pro", reasoning_effort="max", context_window=200000,
    ))
    assert task.params["model"] == "deepseek-v4-pro"
    assert task.params["reasoning_effort"] == "max"
    # dsh 无上下文窗口概念:不落库,与创建页一致
    assert "context_window" not in task.params


def test_empty_string_clears_key():
    task = _make_task("qoder_cli", {"model": "Auto", "reasoning_effort": "low"})
    _call(task, RuntimeConfigUpdateRequest(model=""))
    assert "model" not in task.params
    # 未传的键保持不动
    assert task.params["reasoning_effort"] == "low"


def test_zero_context_window_clears_key():
    task = _make_task("qoder_cli", {"context_window": 400000})
    _call(task, RuntimeConfigUpdateRequest(context_window=0))
    assert "context_window" not in task.params


def test_builtin_rejects_cli_model_params():
    task = _make_task("builtin", {})
    with pytest.raises(HTTPException) as exc:
        _call(task, RuntimeConfigUpdateRequest(model="Qwen3.8-Max"))
    assert exc.value.status_code == 400


def test_codex_cli_rejects_cli_model_params():
    task = _make_task("codex_cli", {})
    with pytest.raises(HTTPException) as exc:
        _call(task, RuntimeConfigUpdateRequest(model="gpt-5"))
    assert exc.value.status_code == 400


def test_no_cli_params_leaves_task_params_untouched():
    task = _make_task("qoder_cli", {"repo_url": "https://x/y.git"})
    _call(task, RuntimeConfigUpdateRequest(llm_config_id=""))
    # 只清 agent2 模型,不新建 model/reasoning_effort 键
    assert task.params == {"repo_url": "https://x/y.git"}
