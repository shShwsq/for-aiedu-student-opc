"""GET /tasks/{id}/workspace/uploads/{tree,file} 回退浏览端点测试

直接调用路由函数(mock db + monkeypatch sandbox_tools/存储目录),覆盖:
- 权限:任务不存在 404 / 他人任务 403 / 匿名任务可访问
- get_workspace.has_uploads:纯 params 推导,零存储访问
- 回退树形状:单上传平铺 / 多上传 {i}-{name}(含前缀目录链,防前端孤儿丢枝)
  / followup 布局 / GC 占位 unavailable / 条目截断
- 回退读文件:内容分页 / 二进制占位与 binary 标记 / 超限 400 / 路径反解失败 404 / 穿越 404
- 树缓存:TTL 内命中陈旧缓存,refresh 强制重建
- 存储后端异常(非 UploadError)→ 500
"""
import io
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import app.routers.workspace as ws_router
from app.config import settings
from app.services.upload_storage import get_backend, reset_backend_cache
from app.services.uploads import save_upload


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """按 {路径: 内容} 构造 zip 字节"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """独立上传目录 + local 后端 + 免清理线程 + 清空模块级树缓存"""
    monkeypatch.setattr(settings, "UPLOADS_DIR", str(tmp_path / "uploads_data"))
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    reset_backend_cache()
    monkeypatch.setattr(
        ws_router.sandbox_tools, "cleanup_expired_sessions_bg", lambda: None
    )
    ws_router._uploads_tree_cache.clear()
    yield
    reset_backend_cache()
    ws_router._uploads_tree_cache.clear()


def _task(user_id="u1", params=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.user_id = user_id
    t.params = params if params is not None else {}
    return t


def _db(task):
    db = MagicMock()
    db.get.side_effect = lambda model, tid: task
    return db


def _user(uid="u1"):
    u = MagicMock()
    u.id = uid
    return u


def _tree(task, **kw):
    # Query 参数须显式传值:直接调用路由函数时默认值是 Query 对象而非实参
    kw.setdefault("max_entries", 3000)
    kw.setdefault("refresh", False)
    return ws_router.get_workspace_uploads_tree(
        task.id, db=_db(task), current_user=_user(), **kw
    )


def _read(task, path, **kw):
    kw.setdefault("offset", 1)
    kw.setdefault("max_lines", 500)
    return ws_router.read_workspace_uploads_file(
        task.id, path=path, db=_db(task), current_user=_user(), **kw
    )


# ============================================================
# 权限
# ============================================================


def test_uploads_tree_task_not_found():
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(HTTPException) as ei:
        ws_router.get_workspace_uploads_tree(uuid.uuid4(), db=db, current_user=None)
    assert ei.value.status_code == 404


def test_uploads_tree_other_user_forbidden():
    task = _task(user_id="owner", params={"upload_ids": ["x"]})
    with pytest.raises(HTTPException) as ei:
        ws_router.get_workspace_uploads_tree(
            task.id, db=_db(task), current_user=_user("intruder")
        )
    assert ei.value.status_code == 403


def test_uploads_tree_anonymous_task_allowed():
    """匿名任务未登录可访问;上传全被 GC 时返回占位而非 403/500"""
    task = _task(user_id=None, params={"upload_ids": ["20990101000000-none"]})
    res = _tree(task)
    assert res["entries"] == []
    assert res["unavailable"] == ["上传文件(209901010000)"]


def test_uploads_file_other_user_forbidden():
    task = _task(user_id="owner", params={"upload_ids": ["x"]})
    with pytest.raises(HTTPException) as ei:
        ws_router.read_workspace_uploads_file(
            task.id, path="a.txt", db=_db(task), current_user=_user("intruder")
        )
    assert ei.value.status_code == 403


# ============================================================
# get_workspace.has_uploads(纯 params 推导,零存储访问)
# ============================================================


def test_get_workspace_has_uploads_without_storage(monkeypatch):
    """沙箱不可用 + 上传 id 不存在存储,has_uploads 仍按 params 为 True"""
    monkeypatch.setattr(ws_router.sandbox_tools, "get_workspace_info", lambda tid: None)
    task = _task(params={"upload_ids": ["20990101000000-none"]})
    res = ws_router.get_workspace(task.id, db=_db(task), current_user=_user())
    assert res["available"] is False
    assert res["has_uploads"] is True
    # legacy 单数字段也算
    task2 = _task(params={"upload_id": "legacy-id"})
    res2 = ws_router.get_workspace(task2.id, db=_db(task2), current_user=_user())
    assert res2["has_uploads"] is True


def test_get_workspace_no_uploads_false(monkeypatch):
    monkeypatch.setattr(
        ws_router.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/repo", "mode": "local", "completed": True},
    )
    task = _task(params={"repo_url": "https://example.com/r.git"})
    res = ws_router.get_workspace(task.id, db=_db(task), current_user=_user())
    assert res["available"] is True
    assert res["has_uploads"] is False


# ============================================================
# get_workspace.can_restore(纯 params 推导,「重新克隆」按钮显示依据)
# ============================================================


def test_get_workspace_can_restore_true_with_repo_url(monkeypatch):
    """带 repo_url 的任务(沙箱已过期):can_restore=True,前端显示重新克隆按钮"""
    monkeypatch.setattr(ws_router.sandbox_tools, "get_workspace_info", lambda tid: None)
    task = _task(params={"repo_url": "https://example.com/r.git"})
    res = ws_router.get_workspace(task.id, db=_db(task), current_user=_user())
    assert res["available"] is False
    assert res["can_restore"] is True


def test_get_workspace_can_restore_false_without_repo_url(monkeypatch):
    """纯上传任务无 repo_url:can_restore=False,不显示重新克隆按钮"""
    monkeypatch.setattr(ws_router.sandbox_tools, "get_workspace_info", lambda tid: None)
    task = _task(params={"upload_ids": ["20990101000000-none"]})
    res = ws_router.get_workspace(task.id, db=_db(task), current_user=_user())
    assert res["available"] is False
    assert res["can_restore"] is False


# ============================================================
# 回退树形状
# ============================================================


def test_tree_flat_single_upload():
    """单创建上传:文件平铺根(与沙箱树同构,无 {i}-{name} 前缀)"""
    meta = save_upload(
        _make_zip({"README.md": "hi", "src/main.py": "print(1)"}), "project.zip", "u1"
    )
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    res = _tree(task)
    assert res["entries"] == [
        {"path": "README.md", "type": "file"},
        {"path": "src", "type": "dir"},
        {"path": "src/main.py", "type": "file"},
    ]
    assert res["truncated"] is False
    assert res["max_depth"] == 2
    assert res["unavailable"] == []


def test_tree_multi_upload_with_dir_chain():
    """多上传:{i}-{name}/ 前缀目录链必须补齐(否则前端整枝丢孤儿条目)"""
    m1 = save_upload(_make_zip({"a.txt": "x"}), "first.zip", "u1")
    m2 = save_upload(_make_zip({"sub/b.txt": "y"}), "second.zip", "u1")
    task = _task(params={"upload_ids": [m1["upload_id"], m2["upload_id"]]})
    res = _tree(task)
    assert res["entries"] == [
        {"path": "0-first.zip", "type": "dir"},
        {"path": "0-first.zip/a.txt", "type": "file"},
        {"path": "1-second.zip", "type": "dir"},
        {"path": "1-second.zip/sub", "type": "dir"},
        {"path": "1-second.zip/sub/b.txt", "type": "file"},
    ]
    assert res["truncated"] is False
    assert res["max_depth"] == 3
    assert res["unavailable"] == []


def test_tree_followup_layout_with_dir_chain():
    """追问上传:followup_uploads/{i}-{name}/ 全局下标 + 目录链"""
    m1 = save_upload(_make_zip({"doc.md": "x"}), "base.zip", "u1")
    f1 = save_upload(b"note", "note.txt", "u1")
    task = _task(params={
        "upload_ids": [m1["upload_id"]],
        "followup_upload_ids": [f1["upload_id"]],
    })
    res = _tree(task)
    assert res["entries"] == [
        {"path": "doc.md", "type": "file"},
        {"path": "followup_uploads", "type": "dir"},
        {"path": "followup_uploads/0-note.txt", "type": "dir"},
        {"path": "followup_uploads/0-note.txt/note.txt", "type": "file"},
    ]
    assert res["unavailable"] == []


def test_tree_gc_upload_placeholder():
    """被 GC 的多上传槽位:不出现在树中,记入 unavailable(meta 已删,名回退 uid 前缀)"""
    m1 = save_upload(_make_zip({"a.txt": "x"}), "first.zip", "u1")
    m2 = save_upload(_make_zip({"b.txt": "y"}), "second.zip", "u1")
    get_backend().delete(m2["upload_id"])  # 模拟 30 天 GC
    task = _task(params={"upload_ids": [m1["upload_id"], m2["upload_id"]]})
    res = _tree(task)
    assert res["entries"] == [
        {"path": "0-first.zip", "type": "dir"},
        {"path": "0-first.zip/a.txt", "type": "file"},
    ]
    assert res["unavailable"] == [f"1-{m2['upload_id'][:12]}"]


def test_tree_no_uploads_task_empty():
    """非上传任务:空树,无占位"""
    task = _task(params={"repo_url": "https://example.com/r.git"})
    res = _tree(task)
    assert res["entries"] == []
    assert res["unavailable"] == []
    assert res["truncated"] is False


def test_tree_truncated_by_max_entries():
    """条目超上限截断:truncated 置位,返回条目数不超过上限,父目录不丢"""
    m1 = save_upload(_make_zip({"a.txt": "x"}), "first.zip", "u1")
    m2 = save_upload(_make_zip({"b.txt": "y"}), "second.zip", "u1")
    task = _task(params={"upload_ids": [m1["upload_id"], m2["upload_id"]]})
    res = _tree(task, max_entries=3)
    assert res["truncated"] is True
    assert len(res["entries"]) == 3
    # 排序后父目录恒在子条目前,截断不会产生孤儿
    paths = [e["path"] for e in res["entries"]]
    for p in paths:
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        assert parent == "" or parent in paths


def test_tree_storage_error_500(monkeypatch):
    """存储后端异常(非 UploadError)→ 500"""
    task = _task(params={"upload_ids": ["x"]})

    class Boom:
        def exists(self, uid):
            raise RuntimeError("storage down")

    monkeypatch.setattr(ws_router, "get_backend", lambda: Boom())
    with pytest.raises(HTTPException) as ei:
        _tree(task)
    assert ei.value.status_code == 500


# ============================================================
# 树缓存
# ============================================================


def test_tree_cache_ttl_and_refresh():
    """TTL 内命中陈旧缓存(GC 后占位不立刻出现),refresh 强制重建"""
    m1 = save_upload(_make_zip({"a.txt": "x"}), "first.zip", "u1")
    task = _task(params={"upload_ids": [m1["upload_id"]]})
    res1 = _tree(task)
    assert res1["unavailable"] == []

    get_backend().delete(m1["upload_id"])  # GC
    res2 = _tree(task)  # TTL 内:命中缓存,仍是旧树
    assert res2 is res1
    assert res2["unavailable"] == []

    res3 = _tree(task, refresh=True)  # 绕过缓存:重建出占位
    assert res3 is not res1
    assert res3["entries"] == []
    assert res3["unavailable"] == [f"上传文件({m1['upload_id'][:12]})"]


# ============================================================
# 回退读文件
# ============================================================


def test_read_file_flat_pagination():
    """平铺根文件:内容分页(1-based)与 truncated 语义"""
    lines = "\n".join(f"line{i}" for i in range(1, 11))  # 10 行
    meta = save_upload(lines.encode("utf-8"), "log.txt", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    res = _read(task, "log.txt", offset=3, max_lines=4)
    assert res["content"] == "line3\nline4\nline5\nline6"
    assert res["start_line"] == 3
    assert res["end_line"] == 6
    assert res["total_lines"] == 10
    assert res["truncated"] is True
    # 尾页
    res2 = _read(task, "log.txt", offset=9, max_lines=10)
    assert res2["content"] == "line9\nline10"
    assert res2["truncated"] is False


def test_read_file_followup_path():
    """追问槽位路径:剥前缀后按 files/ 内相对路径读取"""
    m1 = save_upload(_make_zip({"doc.md": "x"}), "base.zip", "u1")
    f1 = save_upload(b"note", "note.txt", "u1")
    task = _task(params={
        "upload_ids": [m1["upload_id"]],
        "followup_upload_ids": [f1["upload_id"]],
    })
    res = _read(task, "followup_uploads/0-note.txt/note.txt")
    assert res["content"] == "note"
    assert res["path"] == "followup_uploads/0-note.txt/note.txt"
    assert res["total_lines"] == 1


def test_read_file_binary_placeholder():
    """二进制:占位文案 + binary 标记 + 真实字节数(行号全 0)

    判定口径与 sandbox_tools 同源(后缀命中或含 NUL),不再按 UTF-8 解码失败判。
    """
    meta = save_upload(b"\xff\xfe\x00bin", "blob.bin", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    res = _read(task, "blob.bin")
    assert res["content"] == "(二进制文件,无法显示)"
    assert res["binary"] is True
    assert res["size"] == 6
    assert res["start_line"] == 0
    assert res["end_line"] == 0
    assert res["total_lines"] == 0
    assert res["truncated"] is False


def test_read_file_docx_binary_by_suffix():
    """docx 未含可判定的 NUL 也按后缀拦下(合同类交付物的常见形态)"""
    meta = save_upload(b"PK\x03\x04no-nul-here", "合同.docx", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    res = _read(task, "合同.docx")
    assert res["binary"] is True
    assert res["content"] == "(二进制文件,无法显示)"


def test_read_file_binary_flag_on_text():
    """文本态:binary=False,size 恒 0(与 workspace/file 契约一致)"""
    meta = save_upload(b"hello\nworld", "a.txt", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    res = _read(task, "a.txt")
    assert res["binary"] is False
    assert res["size"] == 0
    assert res["total_lines"] == 2


def test_read_file_oversize_400(monkeypatch):
    """超过大小上限:S3 需整对象下载,超限 400 友好提示"""
    monkeypatch.setattr(ws_router, "_UPLOADS_READ_MAX_BYTES", 4)
    meta = save_upload(b"12345678", "big.txt", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    with pytest.raises(HTTPException) as ei:
        _read(task, "big.txt")
    assert ei.value.status_code == 400
    assert "文件过大" in ei.value.detail


def test_read_file_unresolvable_path_404():
    """多上传任务(无平铺槽):不匹配任何前缀的路径 → 404"""
    m1 = save_upload(b"x", "a.txt", "u1")
    m2 = save_upload(b"y", "b.txt", "u1")
    task = _task(params={"upload_ids": [m1["upload_id"], m2["upload_id"]]})
    with pytest.raises(HTTPException) as ei:
        _read(task, "orphan.md")
    assert ei.value.status_code == 404


def test_read_file_missing_in_upload_404():
    """平铺槽路径反解成功但文件不存在 → 404"""
    meta = save_upload(b"x", "a.txt", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    with pytest.raises(HTTPException) as ei:
        _read(task, "nope.txt")
    assert ei.value.status_code == 404


def test_read_file_traversal_404():
    """.. 穿越:反解落到平铺槽后被存储层拒绝 → 404(读不到 files/ 外的 meta.json)"""
    meta = save_upload(b"x", "a.txt", "u1")
    task = _task(params={"upload_ids": [meta["upload_id"]]})
    with pytest.raises(HTTPException) as ei:
        _read(task, "../meta.json")
    assert ei.value.status_code == 404
