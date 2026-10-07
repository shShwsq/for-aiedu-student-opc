"""二进制文件读取拦截测试(不连真实沙箱)

覆盖:
- app.file_kinds:后缀判定 / NUL 判定 / 下载拒绝清单(按路径段,不误伤同名前缀)
- _read_file_sandbox:后缀短路(1 次 wc -c,不读内容)/ 浏览命令内 BINARY 首行 /
  MISSING 仍抛 FileNotFoundError / LLM 带行号分支同样拦截 / 文本路径不回归
- _read_file_local:后缀命中 / 无后缀但含 NUL / GBK 文本改判文本(替换字符)/ 空文件
- 响应契约:binary 与 size 字段
"""
import uuid

import pytest

from app.file_kinds import (
    BINARY_PLACEHOLDER,
    file_suffix,
    has_nul_bytes,
    is_denied_download_path,
    is_likely_binary,
)
from app.tools import sandbox_tools


class FakeSession:
    """假 SandboxSession:run_command 返回预设输出并记录命令"""

    def __init__(self, outputs: list[str] | None = None):
        self.outputs = list(outputs or [])
        self.commands: list[str] = []

    def run_command(self, cmd: str, timeout: int = 60, check: bool = False) -> str:
        self.commands.append(cmd)
        return self.outputs.pop(0) if self.outputs else ""


def _sandbox_ctx(session, repo_path: str = "/repo") -> dict:
    return {"session": session, "repo_path": repo_path, "mode": "sandbox"}


# ============================================================
# app.file_kinds
# ============================================================


@pytest.mark.parametrize(
    "path", ["合同.docx", "a/b/DECK.PPTX", "scan.pdf", "lib.so", "archive.zip", "x.png"]
)
def test_is_likely_binary_by_suffix(path):
    assert is_likely_binary(path) is True


@pytest.mark.parametrize("path", ["main.py", "README.md", "Dockerfile", ".gitignore", "notes"])
def test_not_likely_binary_by_suffix(path):
    assert is_likely_binary(path) is False


def test_file_suffix_case_normalized():
    assert file_suffix("A/B.TXT") == ".txt"
    assert file_suffix("archive.tar.gz") == ".gz"
    assert file_suffix("no-ext") == ""


def test_has_nul_bytes():
    assert has_nul_bytes(b"PK\x03\x04\x00abc") is True
    assert has_nul_bytes("中文文本".encode("utf-8")) is False
    assert has_nul_bytes(b"") is False
    # 只看头部窗口:窗口外的 NUL 不影响判定
    assert has_nul_bytes(b"x" * 10 + b"\x00", limit=10) is False


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "sub/.git/HEAD",
        ".GIT/config",       # local 模式在 NTFS 上会解析到真 .git(文件系统不区分大小写)
        ".Git/CONFIG",
        "id_rsa",
        "ID_RSA",
        ".netrc",
        "certs/a.pem",
        "certs/A.PEM",
        "k.pfx",
    ],
)
def test_denied_download_paths(path):
    assert is_denied_download_path(path) is True


@pytest.mark.parametrize("path", ["src/main.py", ".gitignore", "git/config", "my.git-hooks/x.txt"])
def test_allowed_download_paths(path):
    """按段比对而非子串:名字里带 git 的普通路径不该被拦"""
    assert is_denied_download_path(path) is False


# ============================================================
# _read_file_sandbox(浏览路径:单条合并命令)
# ============================================================


def test_sandbox_suffix_hit_skips_content_read():
    """已知二进制后缀:不跑读取命令,只查一次大小"""
    session = FakeSession(outputs=["245760\n"])
    res = sandbox_tools._read_file_sandbox(
        _sandbox_ctx(session), "/repo", "合同.docx", max_lines=500, offset=1,
        with_line_numbers=False,
    )
    assert res["binary"] is True
    assert res["content"] == BINARY_PLACEHOLDER
    assert res["size"] == 245760
    assert res["total_lines"] == 0
    # 只发生 1 次往返(wc -c),没有 awk 读取
    assert len(session.commands) == 1
    assert "awk" not in session.commands[0]


def test_sandbox_nul_probe_binary_first_line():
    """未知后缀但探测到 NUL:首行 BINARY → 占位响应"""
    session = FakeSession(outputs=["BINARY\n", "1024\n"])
    res = sandbox_tools._read_file_sandbox(
        _sandbox_ctx(session), "/repo", "blob", max_lines=500, offset=1,
        with_line_numbers=False,
    )
    assert res["binary"] is True
    assert res["size"] == 1024
    # 探测内联在同一条命令里(第 1 次往返),不额外加账
    assert "tr -dc" in session.commands[0]
    assert "BINARY" in session.commands[0]


def test_sandbox_missing_file_still_raises():
    session = FakeSession(outputs=["MISSING\n"])
    with pytest.raises(FileNotFoundError):
        sandbox_tools._read_file_sandbox(
            _sandbox_ctx(session), "/repo", "nope.py", max_lines=500, offset=1,
            with_line_numbers=False,
        )


def test_sandbox_text_path_not_regression():
    """文本文件:命令输出解析与拦截前一致,且只 1 次往返"""
    session = FakeSession(outputs=["3\nline1\nline2\nline3\n"])
    res = sandbox_tools._read_file_sandbox(
        _sandbox_ctx(session), "/repo", "a.py", max_lines=500, offset=1,
        with_line_numbers=False,
    )
    assert res["binary"] is False
    assert res["content"] == "line1\nline2\nline3"
    assert res["total_lines"] == 3
    assert len(session.commands) == 1


def test_sandbox_llm_branch_blocks_binary():
    """LLM 带行号分支也必须拦截:否则乱码照样进模型上下文"""
    session = FakeSession(outputs=["4096\n"])
    res = sandbox_tools._read_file_sandbox(
        _sandbox_ctx(session), "/repo", "a.docx", max_lines=200, offset=1,
        with_line_numbers=True,
    )
    assert res["binary"] is True
    assert res["content"] == BINARY_PLACEHOLDER
    # 后缀已命中 → 只查一次大小,不跑探测/wc -l/awk
    assert len(session.commands) == 1
    assert "wc -c < /repo/a.docx" in session.commands[0]
    assert "awk" not in session.commands[0]


def test_sandbox_suffix_hit_missing_file_still_404():
    """不存在的 .docx 走后缀短路时也应报"文件不存在",而非 size=0 的二进制占位"""
    session = FakeSession(outputs=["MISSING\n"])
    with pytest.raises(FileNotFoundError):
        sandbox_tools._read_file_sandbox(
            _sandbox_ctx(session), "/repo", "gone.docx", max_lines=500, offset=1,
            with_line_numbers=False,
        )


def test_sandbox_llm_branch_text_flow():
    session = FakeSession(outputs=["OK\n", "2\n", "     1: x\n     2: y\n"])
    res = sandbox_tools._read_file_sandbox(
        _sandbox_ctx(session), "/repo", "a.py", max_lines=200, offset=1,
        with_line_numbers=True,
    )
    assert res["binary"] is False
    assert res["total_lines"] == 2
    assert len(session.commands) == 3


def test_sandbox_llm_branch_missing_file():
    session = FakeSession(outputs=["MISSING\n"])
    with pytest.raises(FileNotFoundError):
        sandbox_tools._read_file_sandbox(
            _sandbox_ctx(session), "/repo", "a.py", max_lines=200, offset=1,
            with_line_numbers=True,
        )


# ============================================================
# _read_file_local
# ============================================================


def test_local_binary_by_suffix(tmp_path):
    (tmp_path / "x.pdf").write_bytes(b"PK\x03\x04\x00junk")
    res = sandbox_tools._read_file_local(
        str(tmp_path), "x.pdf", max_lines=500, offset=1, with_line_numbers=False
    )
    assert res["binary"] is True
    assert res["size"] == (tmp_path / "x.pdf").stat().st_size


def test_local_binary_by_nul_without_known_suffix(tmp_path):
    (tmp_path / "blob").write_bytes(b"abcdef\x00\x01\x02")
    res = sandbox_tools._read_file_local(
        str(tmp_path), "blob", max_lines=500, offset=1, with_line_numbers=False
    )
    assert res["binary"] is True


def test_local_gbk_text_is_not_binary(tmp_path):
    """GBK 中文文本旧版按"UTF-8 解码失败"判二进制而完全不可读

    新口径只看后缀与 NUL:它算文本,内容以替换字符呈现(可读性优于整份拦掉,
    彻底修好需编码嗅探 —— 见计划遗留项)。
    """
    raw = "合同审查要点：违约责任条款。".encode("gbk")
    (tmp_path / "gbk.txt").write_bytes(raw)
    res = sandbox_tools._read_file_local(
        str(tmp_path), "gbk.txt", max_lines=500, offset=1, with_line_numbers=False
    )
    assert res["binary"] is False
    assert res["content"] != BINARY_PLACEHOLDER
    assert res["total_lines"] == 1


def test_local_empty_file_is_text(tmp_path):
    (tmp_path / "empty.txt").write_bytes(b"")
    res = sandbox_tools._read_file_local(
        str(tmp_path), "empty.txt", max_lines=500, offset=1, with_line_numbers=False
    )
    assert res["binary"] is False
    assert res["total_lines"] == 0


def test_local_missing_and_traversal(tmp_path):
    with pytest.raises(FileNotFoundError):
        sandbox_tools._read_file_local(str(tmp_path), "nope.txt", 500, 1)
    with pytest.raises(ValueError, match="非法路径"):
        sandbox_tools._read_file_local(str(tmp_path), "../out.txt", 500, 1)


# ============================================================
# browse_read_file 会话前置检查(与下载共用 _browse_repo_root)
# ============================================================


def test_browse_read_file_requires_session():
    with pytest.raises(RuntimeError, match="工作区不可用"):
        sandbox_tools.browse_read_file(f"none-{uuid.uuid4()}", "a.py")


def test_browse_read_file_requires_repo_path():
    tid = f"t-{uuid.uuid4()}"
    sandbox_tools._sessions[tid] = {"session": FakeSession(), "repo_path": "", "mode": "sandbox"}
    try:
        with pytest.raises(RuntimeError, match="尚未 clone"):
            sandbox_tools.browse_read_file(tid, "a.py")
    finally:
        sandbox_tools._sessions.pop(tid, None)
