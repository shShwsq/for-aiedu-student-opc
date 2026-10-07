"""克隆凭证清理测试(不触真实 git / 沙箱)

覆盖:
- _looks_credentialed:HTTPS+token 命中,匿名 HTTPS / SSH / 路径里带 @ 的 URL 不误伤
- _scrub_url_userinfo:config / FETCH_HEAD / reflog 三种真实写法
- _scrub_clone_credentials local:改写三个文件 + set-url 用匿名 URL + argv 不含 token
- _scrub_clone_credentials sandbox:命令串里绝不出现 token 字面值(否则换个泄漏面)
- 清理异常不推翻克隆结果(只记 error)
"""
import uuid
from pathlib import Path

import pytest

from app.tools import sandbox_tools

TOKEN = "ghp_SECRETTOKEN1234567890"
CRED_URL = f"https://x-access-token:{TOKEN}@github.com/owner/repo.git"
ANON_URL = "https://github.com/owner/repo.git"
SHA = "1111111111111111111111111111111111111111"


class FakeSession:
    """假会话:记录 shell 命令与 argv 命令,可选抛错"""

    def __init__(self, outputs: list[str] | None = None, error: Exception | None = None):
        self.commands: list[str] = []
        self.argvs: list[list[str]] = []
        self.outputs = list(outputs or [])
        self.error = error

    def run_command(self, cmd: str, timeout: int = 60, check: bool = False) -> str:
        self.commands.append(cmd)
        if self.error is not None:
            raise self.error
        return self.outputs.pop(0) if self.outputs else ""

    def run_command_argv(self, argv, envs=None, timeout: int = 60, check: bool = False) -> str:
        self.argvs.append(list(argv))
        if self.error is not None:
            raise self.error
        return ""


def _make_git_dir(root: Path) -> None:
    """造出 git clone 后会记录 URL 的文件(config / FETCH_HEAD / HEAD 与分支 reflog)"""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(
        '[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n'
        f"\turl = {CRED_URL}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        encoding="utf-8",
    )
    (root / ".git" / "FETCH_HEAD").write_text(
        f"{SHA}\tbranch 'main' of {CRED_URL}\n", encoding="utf-8"
    )
    branch_logs = root / ".git" / "logs" / "refs" / "heads"
    branch_logs.mkdir(parents=True)
    reflog_line = (
        f"0000000000000000000000000000000000000000 {SHA} T <t@t> 1700000000 +0800"
        f"\tclone: from {CRED_URL}"
    )
    (root / ".git" / "logs" / "HEAD").write_text(reflog_line + "\n", encoding="utf-8")
    # clone 同时给被检出的分支 ref 写了 reflog —— 漏掉它 token 还在盘上
    (branch_logs / "main").write_text(reflog_line + "\n", encoding="utf-8")


def _all_recorded_texts(root: Path) -> dict[str, str]:
    """清洗前应覆盖到的全部文件(路径 → 文本)"""
    return {str(p): p.read_text(encoding="utf-8") for p in sandbox_tools._credential_record_files(str(root))}


# ============================================================
# 纯函数
# ============================================================


def test_looks_credentialed():
    assert sandbox_tools._looks_credentialed(CRED_URL) is True
    # Gitee 形态(oauth2:token)同样命中
    assert sandbox_tools._looks_credentialed(
        f"https://oauth2:{TOKEN}@gitee.com/o/r.git"
    ) is True


@pytest.mark.parametrize(
    "url",
    [
        ANON_URL,
        "git@github.com:owner/repo.git",          # SSH:冒号分隔的 user 不是凭证注入
        "https://github.com/owner/repo.git",
        "file:///tmp/cache/owner/repo.git",        # bare 缓存本地克隆路径
        "https://github.com/o/r@main",             # @ 在路径里,不是 userinfo
        "",
    ],
)
def test_not_credentialed(url):
    assert sandbox_tools._looks_credentialed(url) is False


def test_scrub_url_userinfo_keeps_host_and_path():
    out = sandbox_tools._scrub_url_userinfo(f"url = {CRED_URL}\n")
    assert TOKEN not in out
    assert "x-access-token" not in out
    assert out.strip() == f"url = {ANON_URL}"


def test_scrub_url_userinfo_multiple_occurrences():
    text = f"{CRED_URL} and {CRED_URL} tail"
    out = sandbox_tools._scrub_url_userinfo(text)
    assert out.count(ANON_URL) == 2
    assert TOKEN not in out


# ============================================================
# local 模式:真实文件清洗
# ============================================================


def test_scrub_local_rewrites_all_record_files(tmp_path):
    _make_git_dir(tmp_path)
    session = FakeSession()
    ctx = {"session": session, "mode": "local", "repo_path": str(tmp_path)}

    # 前置:待清洗清单确实覆盖了分支 reflog(漏了它等于白做)
    before = _all_recorded_texts(tmp_path)
    assert len(before) == 4, list(before)
    assert any(Path(p).name == "main" for p in before)

    sandbox_tools._scrub_clone_credentials(
        ctx, str(tmp_path), CRED_URL, ANON_URL, task_id="t1"
    )

    # set-url 走 argv(Windows cmd.exe 无 POSIX 引号规则),且用匿名 URL
    assert session.argvs == [
        ["git", "-C", str(tmp_path), "remote", "set-url", "origin", ANON_URL]
    ]
    for path, _ in before.items():
        text = Path(path).read_text(encoding="utf-8")
        assert TOKEN not in text, path
        assert ANON_URL in text, path
    # reflog 的说明文字与 FETCH_HEAD 的行结构不受影响(只剥 userinfo)
    assert "clone: from" in (tmp_path / ".git" / "logs" / "HEAD").read_text(encoding="utf-8")
    assert (tmp_path / ".git" / "FETCH_HEAD").read_text(encoding="utf-8").startswith(f"{SHA}\t")


def test_scrub_local_skips_anonymous_clone(tmp_path):
    """匿名/SSH 克隆不该被动过(既不跑 set-url 也不改文件)"""
    _make_git_dir(tmp_path)
    before = (tmp_path / ".git" / "config").read_text(encoding="utf-8")
    session = FakeSession()
    ctx = {"session": session, "mode": "local", "repo_path": str(tmp_path)}

    sandbox_tools._scrub_clone_credentials(ctx, str(tmp_path), ANON_URL, ANON_URL, task_id="t1")

    assert session.argvs == []
    assert (tmp_path / ".git" / "config").read_text(encoding="utf-8") == before


def test_scrub_local_tolerates_missing_files(tmp_path):
    """工作区可能没有 FETCH_HEAD/logs(如浅克隆后被清理):不应报错"""
    (tmp_path / ".git").mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {CRED_URL}\n', encoding="utf-8")
    session = FakeSession()
    ctx = {"session": session, "mode": "local", "repo_path": str(tmp_path)}

    sandbox_tools._scrub_clone_credentials(ctx, str(tmp_path), CRED_URL, ANON_URL, task_id="t1")

    assert TOKEN not in (tmp_path / ".git" / "config").read_text(encoding="utf-8")


def test_scrub_local_swallows_failure_keeps_workspace(tmp_path, monkeypatch):
    """清理失败不能推翻克隆结果(工作区可用是主目标),但要记 error 日志"""
    _make_git_dir(tmp_path)
    session = FakeSession(error=OSError("git not found"))
    ctx = {"session": session, "mode": "local", "repo_path": str(tmp_path)}
    logged = []
    monkeypatch.setattr(
        sandbox_tools.logger, "error", lambda msg, *a: logged.append(msg)
    )

    sandbox_tools._scrub_clone_credentials(
        ctx, str(tmp_path), CRED_URL, ANON_URL, task_id="t1"
    )  # 不抛

    assert logged and "克隆凭证清理失败" in logged[0]


# ============================================================
# sandbox 模式:命令里绝不出现 token 字面值
# ============================================================


def test_scrub_sandbox_commands_never_carry_token():
    session = FakeSession(outputs=["", ""])   # 清洗 + 复查:复查无输出 = 清理干净
    ctx = {"session": session, "mode": "sandbox", "repo_path": "/home/user/repos/repo"}

    sandbox_tools._scrub_clone_credentials(
        ctx, "/home/user/repos/repo", CRED_URL, ANON_URL, task_id="t1"
    )

    assert len(session.commands) == 2
    joined = "\n".join(session.commands)
    # 关键:按模式匹配剥离,token 从不进命令行(否则会落进 execd 记录的命令里)
    assert TOKEN not in joined
    assert "x-access-token" not in joined
    assert "remote set-url origin https://github.com/owner/repo.git" in joined
    assert "sed -i -E" in joined
    # reflog 用 find -exec 枚举:分支 ref 那份(logs/refs/heads/main)也在清洗范围内,
    # 且不能是 $(find …)(路径含空格会被拆坏)
    assert "find /home/user/repos/repo/.git/logs -type f -exec" in joined
    assert "$(find" not in joined
    for rel in ("config", "FETCH_HEAD"):
        assert f".git/{rel}" in joined


def test_scrub_sandbox_reports_residual_files(caplog):
    """复查仍命中 → 记 error(带残留文件路径),便于排查而非静默通过

    sandbox 分支两次 run_command:第 1 次清洗(无输出)、第 2 次复查残留。
    """
    session = FakeSession(
        outputs=["", "/home/user/repos/repo/.git/logs/refs/heads/main\n"]
    )
    ctx = {"session": session, "mode": "sandbox", "repo_path": "/home/user/repos/repo"}

    with caplog.at_level("ERROR"):
        sandbox_tools._scrub_clone_credentials(
            ctx, "/home/user/repos/repo", CRED_URL, ANON_URL, task_id="t1"
        )

    assert "克隆凭证清理未彻底" in caplog.text
    assert "logs/refs/heads/main" in caplog.text
