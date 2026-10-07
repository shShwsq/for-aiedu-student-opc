"""工作区/上传文件下载端点测试(不连真实沙箱)

覆盖:
- sandbox_tools.browse_download:local 直读 / sandbox 走 SDK 字节流 / 路径穿越 / 会话缺失
- GET .../workspace/download:流式响应头(RFC 5987 中文名)、上限 413、凭证路径 403、
  不存在 404、他人任务 403
- GET .../workspace/uploads/download:字节往返、Content-Length、超限 413、GC 后 404
"""
import io
import uuid
import zipfile
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import app.routers.workspace as ws_router
import app.tools.sandbox_tools as sandbox_tools
from app.config import settings
from app.services.upload_storage import get_backend, reset_backend_cache
from app.services.uploads import save_upload


# ============================================================
# 通用 fake
# ============================================================


class FakeSandboxSession:
    """假沙箱会话:只实现下载用到的 stat_size / read_bytes_stream"""

    def __init__(self, size: int = 5, chunks: list[bytes] | None = None, error: Exception | None = None):
        self.size = size
        self.chunks = chunks if chunks is not None else [b"part1", b"part2"]
        self.error = error
        self.requested: list[str] = []

    def stat_size(self, path: str) -> int:
        if self.error is not None:
            raise self.error
        self.requested.append(path)
        return self.size

    def read_bytes_stream(self, path: str, chunk_size: int = 65536):
        self.requested.append(path)
        return iter(self.chunks)


def _register_sandbox_session(tid: str, session, repo_path: str = "/repo") -> None:
    sandbox_tools._sessions[tid] = {"session": session, "repo_path": repo_path, "mode": "sandbox"}


def _register_local_session(tid: str, repo_path: str) -> None:
    sandbox_tools._sessions[tid] = {
        "session": FakeSandboxSession(), "repo_path": repo_path, "mode": "local",
    }


def _cleanup(tid: str) -> None:
    sandbox_tools._sessions.pop(tid, None)


def _make_task(user_id, params):
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


def _download(task, path):
    return ws_router.download_workspace_file(
        task.id, path=path, db=_db(task), current_user=_user()
    )


def _uploads_download(task, path):
    return ws_router.download_workspace_uploads_file(
        task.id, path=path, db=_db(task), current_user=_user()
    )


# ============================================================
# sandbox_tools.browse_download
# ============================================================


def test_browse_download_local_reads_host_file(tmp_path):
    tid = f"t-{uuid.uuid4()}"
    (tmp_path / "doc.docx").write_bytes(b"PK\x03\x04binary-bytes")
    _register_local_session(tid, str(tmp_path))
    try:
        size, chunks = sandbox_tools.browse_download(tid, "doc.docx")
        assert size == 16
        assert b"".join(chunks) == b"PK\x03\x04binary-bytes"
    finally:
        _cleanup(tid)


def test_browse_download_local_traversal_rejected(tmp_path):
    tid = f"t-{uuid.uuid4()}"
    _register_local_session(tid, str(tmp_path))
    try:
        with pytest.raises(ValueError, match="非法路径"):
            sandbox_tools.browse_download(tid, "../escape.bin")
    finally:
        _cleanup(tid)


def test_browse_download_local_missing_file(tmp_path):
    tid = f"t-{uuid.uuid4()}"
    _register_local_session(tid, str(tmp_path))
    try:
        with pytest.raises(FileNotFoundError):
            sandbox_tools.browse_download(tid, "nope.docx")
        # 目录不可下载
        (tmp_path / "dir").mkdir()
        with pytest.raises(FileNotFoundError):
            sandbox_tools.browse_download(tid, "dir")
    finally:
        _cleanup(tid)


def test_browse_download_no_session():
    with pytest.raises(RuntimeError, match="工作区不可用"):
        sandbox_tools.browse_download(f"missing-{uuid.uuid4()}", "a.docx")


def test_browse_download_sandbox_uses_sdk_stream():
    tid = f"t-{uuid.uuid4()}"
    session = FakeSandboxSession(size=12, chunks=[b"abcdef", b"ghijkl"])
    _register_sandbox_session(tid, session)
    try:
        size, chunks = sandbox_tools.browse_download(tid, "sub/a.pdf")
        assert size == 12
        assert b"".join(chunks) == b"abcdefghijkl"
        # 绝对路径拼接与 read_file 同口径(repo_path + / + 去前导斜杠的相对路径)
        assert "/repo/sub/a.pdf" in session.requested
    finally:
        _cleanup(tid)


def test_browse_download_sandbox_missing_file(tmp_path):
    tid = f"t-{uuid.uuid4()}"
    session = FakeSandboxSession(error=FileNotFoundError("no such file"))
    _register_sandbox_session(tid, session)
    try:
        with pytest.raises(FileNotFoundError):
            sandbox_tools.browse_download(tid, "gone.docx")
    finally:
        _cleanup(tid)


# ============================================================
# GET .../workspace/download
# ============================================================


def test_download_workspace_headers_rfc5987():
    tid = f"t-{uuid.uuid4()}"
    session = FakeSandboxSession(size=12, chunks=[b"123456789012"])
    _register_sandbox_session(tid, session, repo_path="/repo")
    task = _make_task("u1", {})
    task.id = tid
    try:
        resp = _download(task, "合同/附件.pdf")
        assert resp.status_code == 200
        cd = resp.headers["Content-Disposition"]
        # 中文名必须走 filename*=UTF-8''(filename= 只作 ASCII 回退)
        assert "filename*=UTF-8''%E9%99%84%E4%BB%B6.pdf" in cd
        assert 'filename=".pdf"' in cd
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.media_type == "application/pdf"
        # 流式响应不预设 Content-Length(stat 与实际流不一致会静默截断)
        assert "Content-Length" not in resp.headers
    finally:
        _cleanup(tid)


def test_download_workspace_over_size_limit_413(monkeypatch):
    tid = f"t-{uuid.uuid4()}"
    monkeypatch.setattr(settings, "WORKSPACE_DOWNLOAD_MAX_MB", 1)
    session = FakeSandboxSession(size=2 * 1024 * 1024)
    _register_sandbox_session(tid, session)
    task = _make_task("u1", {})
    task.id = tid
    try:
        with pytest.raises(HTTPException) as ei:
            _download(task, "big.iso")
        assert ei.value.status_code == 413
        assert "下载上限 1MB" in ei.value.detail
    finally:
        _cleanup(tid)


@pytest.mark.parametrize(
    "bad_path",
    [
        ".git/config",
        "sub/.git/config",
        ".GIT/config",   # 大小写变体:local 模式在 NTFS 上同样指向真 .git
        "id_rsa",
        "keys/server.pem",
        "cert.p12",
        ".netrc",
    ],
)
def test_download_workspace_denied_paths_403(bad_path):
    tid = f"t-{uuid.uuid4()}"
    _register_sandbox_session(tid, FakeSandboxSession(), repo_path="/repo")
    task = _make_task("u1", {})
    task.id = tid
    try:
        with pytest.raises(HTTPException) as ei:
            _download(task, bad_path)
        assert ei.value.status_code == 403
    finally:
        _cleanup(tid)


@pytest.mark.parametrize("bad_path", ["", "   ", "/etc/passwd", "../out.bin", "a/../../b"])
def test_download_workspace_bad_paths_404(bad_path):
    tid = f"t-{uuid.uuid4()}"
    _register_sandbox_session(tid, FakeSandboxSession(), repo_path="/repo")
    task = _make_task("u1", {})
    task.id = tid
    try:
        with pytest.raises(HTTPException) as ei:
            _download(task, bad_path)
        assert ei.value.status_code == 404
    finally:
        _cleanup(tid)


def test_download_workspace_session_expired_404():
    task = _make_task("u1", {})
    with pytest.raises(HTTPException) as ei:
        _download(task, "a.docx")
    assert ei.value.status_code == 404


def test_download_workspace_other_user_forbidden():
    task = _make_task("u1", {})
    db = _db(task)
    with pytest.raises(HTTPException) as ei:
        ws_router.download_workspace_file(
            task.id, path="a.docx", db=db, current_user=_user("u2")
        )
    assert ei.value.status_code == 403


def test_download_workspace_task_not_found():
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(HTTPException) as ei:
        ws_router.download_workspace_file(
            uuid.uuid4(), path="a.docx", db=db, current_user=_user()
        )
    assert ei.value.status_code == 404


# ============================================================
# GET .../workspace/uploads/download(沙箱过期后的取回路径)
# ============================================================


@pytest.fixture
def uploads_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOADS_DIR", str(tmp_path / "uploads_data"))
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    reset_backend_cache()
    yield
    reset_backend_cache()


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_uploads_download_roundtrip(uploads_env):
    data = _make_zip({"合同.docx": b"PK\x03\x04docx-bytes"})
    meta = save_upload(data, "contract.zip", "u1")
    task = _make_task("u1", {"upload_ids": [meta["upload_id"]]})

    resp = _uploads_download(task, "合同.docx")
    assert resp.status_code == 200
    assert resp.body == b"PK\x03\x04docx-bytes"
    assert resp.headers["Content-Length"] == str(len(b"PK\x03\x04docx-bytes"))
    assert "filename*=UTF-8''%E5%90%88%E5%90%8C.docx" in resp.headers["Content-Disposition"]
    assert resp.media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_uploads_download_followup_layout(uploads_env):
    """追问上传按累积下标布局取文件(与回退树同一路径口径)"""
    first = save_upload(b"note text", "note.txt", "u1")["upload_id"]
    second = save_upload(b"PK\x03\x04x", "附件.docx", "u1")["upload_id"]
    task = _make_task("u1", {"followup_upload_ids": [first, second]})

    resp = _uploads_download(task, "followup_uploads/1-附件.docx/附件.docx")
    assert resp.body == b"PK\x03\x04x"


def test_uploads_download_over_limit_413(uploads_env, monkeypatch):
    """超限时先 stat 后读:大文件不该被整份拉进内存"""
    monkeypatch.setattr(settings, "WORKSPACE_DOWNLOAD_MAX_MB", 0)
    uid = save_upload(b"x" * 64, "big.bin", "u1")["upload_id"]
    task = _make_task("u1", {"upload_ids": [uid]})

    backend = get_backend()
    real_read = backend.read_file
    reads: list[str] = []

    def spy_read(upload_id, relpath):
        reads.append(relpath)
        return real_read(upload_id, relpath)

    monkeypatch.setattr(backend, "read_file", spy_read)
    with pytest.raises(HTTPException) as ei:
        _uploads_download(task, "big.bin")
    assert ei.value.status_code == 413
    assert reads == []


def test_uploads_download_gc_cleaned_404(uploads_env):
    task = _make_task("u1", {"upload_ids": ["00000000000000000000000000000000"]})
    with pytest.raises(HTTPException) as ei:
        _uploads_download(task, "gone.docx")
    assert ei.value.status_code == 404


def test_uploads_download_denied_path_403(uploads_env):
    """拒绝清单在路径解析之前生效(不依赖上传里真存在该文件)"""
    uid = save_upload(b"docx bytes", "合同.docx", "u1")["upload_id"]
    task = _make_task("u1", {"upload_ids": [uid]})
    for bad in (".git/config", "server.pem"):
        with pytest.raises(HTTPException) as ei:
            _uploads_download(task, bad)
        assert ei.value.status_code == 403


# ============================================================
# 回归:stat_size 归一 SDK 异常(未归一时下载不存在文件 → 500)
# ============================================================


class _FakeApiException(Exception):
    """模拟 SandboxApiException:纯 Exception 子类(非 FileNotFoundError),带 status_code"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _sandbox_session_get_file_info(side_effect: Exception | None = None, ret=None):
    from app.sandbox.client import SandboxSession

    mock_sb = MagicMock()
    if side_effect is not None:
        mock_sb.files.get_file_info.side_effect = side_effect
    else:
        mock_sb.files.get_file_info.return_value = ret
    return SandboxSession(mode="sandbox", sandbox=mock_sb)


def test_stat_size_returns_size_on_success():
    s = _sandbox_session_get_file_info(ret={"/repo/a.pdf": MagicMock(size=42)})
    assert s.stat_size("/repo/a.pdf") == 42


def test_stat_size_normalizes_404_status_to_file_not_found():
    s = _sandbox_session_get_file_info(
        _FakeApiException("Get file info failed: HTTP 404", status_code=404)
    )
    with pytest.raises(FileNotFoundError):
        s.stat_size("/repo/gone.docx")


def test_stat_size_normalizes_not_exist_message():
    """无 status_code 属性时按消息文本兜底归一(对齐 list_directory 口径)"""
    s = _sandbox_session_get_file_info(Exception("path does not exist"))
    with pytest.raises(FileNotFoundError):
        s.stat_size("/repo/gone.docx")


def test_stat_size_reraises_non_not_found():
    """非"不存在"类 SDK 异常(如 500)原样抛出,不误判成 FileNotFoundError"""
    s = _sandbox_session_get_file_info(
        _FakeApiException("Get file info failed: HTTP 500", status_code=500)
    )
    with pytest.raises(_FakeApiException):
        s.stat_size("/repo/x.docx")


# ============================================================
# 回归:_download_headers 剔除文件名中的控制字符(CR/LF 泄漏 → h11 断连)
# ============================================================


def test_download_headers_strip_control_chars():
    cd = ws_router._download_headers("evil\r\nX-Injected: 1.pdf")["Content-Disposition"]
    # 响应头里绝不能残留原始 CR/LF(否则整条头被判非法)
    assert "\r" not in cd and "\n" not in cd
    # 内容仍在(只是并进 filename 值里,不再是独立头行);filename* 走百分号编码
    assert "filename=\"evilX-Injected: 1.pdf\"" in cd
    assert "%0D%0A" in cd


# ============================================================
# 回归:预览端点套用凭证拒绝清单(与下载同口径)
# ============================================================


def _preview(task, path):
    return ws_router.read_workspace_file(
        task.id, path=path, db=_db(task), current_user=_user()
    )


@pytest.mark.parametrize(
    "bad_path",
    [".git/config", "sub/.git/config", ".GIT/config", "id_rsa", "keys/server.pem"],
)
def test_read_workspace_file_denied_paths_403(bad_path):
    tid = f"t-{uuid.uuid4()}"
    _register_sandbox_session(tid, FakeSandboxSession(), repo_path="/repo")
    task = _make_task("u1", {})
    task.id = tid
    try:
        with pytest.raises(HTTPException) as ei:
            _preview(task, bad_path)
        assert ei.value.status_code == 403
    finally:
        _cleanup(tid)

