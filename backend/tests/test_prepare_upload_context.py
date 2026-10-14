"""orchestrator._prepare_upload_context 单测(创建流多文件布局,mock 沙箱传输)

覆盖计划 §9 的"创建多文件":
- _creation_upload_ids 合并 legacy 单数 upload_id + upload_ids,去重保序
- 单个上传:走 transfer_upload_to_workspace(文件铺根,保持既有布局),header 含文件名
- 多个上传:走 transfer_uploads_to_workspace_root(各占子目录),header 含数量与文件名
- legacy upload_id 与 upload_ids 共存时合并去重
- 根目录为空 → 不列根目录结构,但仍返回上传 header 兜底(让 agent 知道是上传工作区)
- 无上传 id → RuntimeError(不降级)
"""
from unittest.mock import MagicMock

import pytest

import app.agents.orchestrator as orchestrator
from app.config import settings
from app.services.uploads import save_upload
from app.agents.orchestrator import _creation_upload_ids, _prepare_upload_context


@pytest.fixture(autouse=True)
def _uploads_dir(tmp_path, monkeypatch):
    """独立上传目录,不污染真实数据目录"""
    monkeypatch.setattr(settings, "UPLOADS_DIR", str(tmp_path / "uploads_data"))


def _patch_transfer(monkeypatch, entries=None):
    """屏蔽发布/传输/列表/树快照副作用,记录走的是单个还是多个传输路径"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _t: None)
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "list_files",
        lambda *a, **k: {
            "entries": (
                [{"name": "x", "type": "file", "size": 1}] if entries is None else entries
            ),
            "total": 1, "truncated": False,
        },
    )
    # save_repo_tree_artifact 在函数内 import,patch 源模块属性
    monkeypatch.setattr(
        "app.services.workspace_diff.save_repo_tree_artifact",
        lambda *a, **k: None,
        raising=False,
    )
    single_calls: list[tuple] = []
    multi_calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "transfer_upload_to_workspace",
        lambda tid, files_dir, dest_name="uploaded_files": (
            single_calls.append((tid, files_dir)) or "/ws/uploaded_files"
        ),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "transfer_uploads_to_workspace_root",
        lambda tid, ids, dest_name="uploaded_files": (
            multi_calls.append((tid, ids)) or "/ws/uploaded_files"
        ),
    )
    return single_calls, multi_calls


def _mk_task(params):
    task = MagicMock()
    task.id = "t1"
    task.params = params
    return task


# ============================================================
# _creation_upload_ids:合并 legacy + 新列表,去重保序
# ============================================================

def test_creation_upload_ids_merges_and_dedups():
    assert _creation_upload_ids({"upload_id": "a", "upload_ids": ["a", "b"]}) == ["a", "b"]


def test_creation_upload_ids_only_legacy():
    assert _creation_upload_ids({"upload_id": "legacy"}) == ["legacy"]


def test_creation_upload_ids_only_list():
    assert _creation_upload_ids({"upload_ids": ["x", "y"]}) == ["x", "y"]


def test_creation_upload_ids_filters_falsy():
    assert _creation_upload_ids({"upload_ids": ["", "x", None]}) == ["x"]


def test_creation_upload_ids_none_and_empty():
    assert _creation_upload_ids(None) == []
    assert _creation_upload_ids({}) == []


# ============================================================
# _prepare_upload_context:单个 vs 多个上传布局
# ============================================================

def test_single_upload_uses_transfer_to_workspace(monkeypatch):
    """单个上传:走 transfer_upload_to_workspace(根布局),header 直白陈述文件位置"""
    single_calls, multi_calls = _patch_transfer(monkeypatch)
    uid = save_upload(b"doc-bytes", "proj.txt", "u1")["upload_id"]
    task = _mk_task({"upload_id": uid})

    repo_path, ctx_text = _prepare_upload_context(task, MagicMock(), "t1")

    assert single_calls and not multi_calls
    assert single_calls[0][0] == "t1"
    assert repo_path == "/ws/uploaded_files"
    assert "proj.txt" in ctx_text
    # 直白措辞:陈述文件位置,不用内部术语,不提 clone
    assert "用户上传的文件" in ctx_text
    assert "已放入 /ws/uploaded_files" in ctx_text
    assert "可直接开始处理" in ctx_text


def test_single_upload_legacy_list_alone(monkeypatch):
    """upload_ids 仅一个元素也走单个路径(根布局不变)"""
    single_calls, multi_calls = _patch_transfer(monkeypatch)
    uid = save_upload(b"x", "only.txt", "u1")["upload_id"]
    task = _mk_task({"upload_ids": [uid]})

    _prepare_upload_context(task, MagicMock(), "t1")

    assert single_calls and not multi_calls


def test_multi_upload_uses_transfer_to_root(monkeypatch):
    """多个上传:走 transfer_uploads_to_workspace_root,header 含数量与各文件名 + 各占独立子目录"""
    single_calls, multi_calls = _patch_transfer(monkeypatch)
    u1 = save_upload(b"a", "one.txt", "u1")["upload_id"]
    u2 = save_upload(b"b", "two.txt", "u1")["upload_id"]
    task = _mk_task({"upload_ids": [u1, u2]})

    repo_path, ctx_text = _prepare_upload_context(task, MagicMock(), "t1")

    assert multi_calls and not single_calls
    assert multi_calls[0][1] == [u1, u2]
    assert repo_path == "/ws/uploaded_files"
    assert "2 组文件" in ctx_text
    assert "one.txt" in ctx_text and "two.txt" in ctx_text
    assert "独立子目录" in ctx_text


def test_multi_upload_merges_legacy_dedup(monkeypatch):
    """legacy upload_id 与 upload_ids 共存:合并去重后走多个路径"""
    _single, multi_calls = _patch_transfer(monkeypatch)
    u1 = save_upload(b"a", "a.txt", "u1")["upload_id"]
    u2 = save_upload(b"b", "b.txt", "u1")["upload_id"]
    task = _mk_task({"upload_id": u1, "upload_ids": [u1, u2]})

    _prepare_upload_context(task, MagicMock(), "t1")

    assert multi_calls[0][1] == [u1, u2]  # u1 去重,保序


def test_empty_root_returns_header_fallback(monkeypatch):
    """根目录为空 → 不列根目录结构,但仍返回上传 header 兜底(含路径),让 agent 知道是上传工作区"""
    _patch_transfer(monkeypatch, entries=[])
    uid = save_upload(b"x", "a.txt", "u1")["upload_id"]
    task = _mk_task({"upload_id": uid})

    repo_path, ctx_text = _prepare_upload_context(task, MagicMock(), "t1")

    assert repo_path == "/ws/uploaded_files"
    assert "用户上传的文件" in ctx_text
    assert "已放入 /ws/uploaded_files" in ctx_text
    assert "根目录结构" not in ctx_text  # 空目录不列结构,避免"共 0 项"误导


def test_no_upload_ids_raises(monkeypatch):
    """无上传 id → RuntimeError(上传流不降级)"""
    _patch_transfer(monkeypatch)
    task = _mk_task({})

    with pytest.raises(RuntimeError, match="缺少 upload_id"):
        _prepare_upload_context(task, MagicMock(), "t1")
