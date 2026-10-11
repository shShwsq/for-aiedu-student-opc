"""CLI 冷启动链路"砍死时间"改造的回归测试(PR-0)

覆盖:
- `_ensure_cli_env`:建目录 + 检查 CLI 合成一条命令(2 次往返);同一会话内容未变
  则整体短路(0 次);ctx 换新(容器重建)后必须重来 —— 否则新容器里没有 bridge 脚本
- `_inject_git_tokens_to_cache`:多 provider 合并为 write_file + 1 条命令;
  **token 绝不出现在命令串里**(只能经临时文件);`rm -f` 用 `;` 收尾,approve 失败也删
- `_wait_for_bridge_ready`:成功路径不取后台日志(省一次沙箱往返);health 不通才查
  日志,并保留 "ACP CLI 启动失败" 的快速失败口径
- `write_project_memory_file` / `write_skill_files`:同会话同内容跳过重写;
  skill 的父目录一次建齐(不再每文件一次 mkdir)

替身全部是鸭子类型假会话,不连真实沙箱(与 test_cli_skills / test_codex_chatgpt_auth 同范式)。
"""
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.tools.sandbox_tools as st
from app.agents import acp_base
from app.config import settings


class _FakeSession:
    """记录命令与写入的假沙箱会话;可按命令前缀预设输出"""

    mode = "sandbox"

    def __init__(self, outputs: dict[str, str] | None = None, raise_on: str = ""):
        self.commands: list[str] = []
        self.written: dict[str, str] = {}
        self._outputs = outputs or {}
        self._raise_on = raise_on

    def run_command(self, cmd, timeout=60, check=False):
        self.commands.append(cmd)
        for prefix, out in self._outputs.items():
            if cmd.startswith(prefix):
                return out
        return "/usr/local/bin/qodercli\n"

    def write_file(self, path, content):
        if self._raise_on and self._raise_on in path:
            raise RuntimeError("write failed")
        self.written[path] = content

    def read_file(self, path):
        return self.written.get(path, "")

    def get_background_logs(self, execution_id, cursor=None):
        return "", None


# ============================================================
# _ensure_cli_env:往返合并 + 会话级短路
# ============================================================


def test_ensure_cli_env_merges_mkdir_and_check(monkeypatch):
    session = _FakeSession()
    ctx: dict = {"mode": "sandbox"}
    monkeypatch.setattr(acp_base, "_get_bin", lambda _t: "qodercli")

    acp_base._ensure_cli_env(session, "qoder_cli", ctx)

    # 一条命令同时建目录 + 检查 CLI(原先两次往返),脚本单独一次 write_file
    assert len(session.commands) == 1, session.commands
    cmd = session.commands[0]
    assert cmd.startswith("mkdir -p ")
    assert "command -v qodercli" in cmd
    assert Path(acp_base.BRIDGE_SCRIPT_PATH).parent.as_posix() in cmd
    assert list(session.written) == [acp_base.BRIDGE_SCRIPT_PATH]
    # 就绪标记写在 ctx 上(容器重建 → ctx 换新 → 标记自然消失)
    assert ctx["_cli_env_memo"]


def test_ensure_cli_env_skips_within_same_session(monkeypatch):
    monkeypatch.setattr(acp_base, "_get_bin", lambda _t: "qodercli")
    session = _FakeSession()
    ctx: dict = {"mode": "sandbox"}

    acp_base._ensure_cli_env(session, "qoder_cli", ctx)
    n_cmd, n_write = len(session.commands), len(session.written)
    acp_base._ensure_cli_env(session, "qoder_cli", ctx)   # 同会话 → 0 次往返
    assert (len(session.commands), len(session.written)) == (n_cmd, n_write)

    # 换了 ctx(等价于沙箱容器被重建)必须重来,不能拿旧标记跳过
    fresh: dict = {"mode": "sandbox"}
    acp_base._ensure_cli_env(session, "qoder_cli", fresh)
    assert len(session.commands) == n_cmd * 2
    assert fresh["_cli_env_memo"] == ctx["_cli_env_memo"]


def test_ensure_cli_env_without_ctx_still_runs(monkeypatch):
    """不传 ctx(如 test_credential 流程)时行为不变:照常准备,只是不记标记"""
    monkeypatch.setattr(acp_base, "_get_bin", lambda _t: "qodercli")
    session = _FakeSession()
    acp_base._ensure_cli_env(session, "qoder_cli")
    assert len(session.commands) == 1
    assert list(session.written) == [acp_base.BRIDGE_SCRIPT_PATH]


def test_ensure_cli_env_installs_when_missing(monkeypatch):
    """检查输出为空 → 执行安装命令 → 复检仍为空则报错(口径与改造前一致)"""
    monkeypatch.setattr(acp_base, "_get_bin", lambda _t: "qodercli")
    monkeypatch.setattr(acp_base, "_get_install_cmd", lambda _t: "npm install -g x")
    session = _FakeSession(outputs={"mkdir -p": "", "command -v": ""})

    with pytest.raises(RuntimeError, match="CLI 安装失败"):
        acp_base._ensure_cli_env(session, "qoder_cli", {"mode": "sandbox"})


# ============================================================
# git 凭证注入:合并往返 + token 不上命令行
# ============================================================


@pytest.fixture
def two_providers(monkeypatch):
    import app.git_provider as gp

    providers = {
        "github": SimpleNamespace(host="github.com", token_username="x-access-token"),
        "gitee": SimpleNamespace(host="gitee.com", token_username="oauth2"),
    }
    monkeypatch.setattr(gp, "PROVIDERS", providers)
    return providers


def test_inject_git_tokens_uses_two_roundtrips(two_providers):
    session = _FakeSession()
    tokens = {"github": "ghp_SECRET_1", "gitee": "gitee_SECRET_2"}

    acp_base._inject_git_tokens_to_cache(session, str(uuid.uuid4()), tokens)

    assert len(session.written) == 1 and len(session.commands) == 1
    cred_file = next(iter(session.written))
    assert cred_file.startswith("/tmp/.git_cred_") and cred_file.endswith(".tmp")
    # 两条 credential record 都以"空行结尾"写进同一个输入文件
    body = session.written[cred_file]
    assert "host=github.com" in body and "password=ghp_SECRET_1" in body
    assert "host=gitee.com" in body and "password=gitee_SECRET_2" in body

    cmd = session.commands[0]
    # 顺序:config → chmod 600 → approve → rm(rm 用 `;` 收尾)
    assert (
        cmd.index("git config --global credential.helper")
        < cmd.index("chmod 600")
        < cmd.index("git credential approve")
        < cmd.index("rm -f")
    )
    assert f"; rm -f" in cmd, cmd
    assert "'cache --timeout=%d'" % acp_base._GIT_CRED_CACHE_TIMEOUT in cmd


def test_inject_git_tokens_never_puts_token_on_commandline(two_providers):
    """安全不变式:token 只进临时文件,命令串里出现 token = ps / /proc 泄露"""
    session = _FakeSession()
    tokens = {"github": "ghp_SECRET_1", "gitee": "gitee_SECRET_2"}

    acp_base._inject_git_tokens_to_cache(session, "t", tokens)

    joined = " ".join(session.commands)
    for secret in tokens.values():
        assert secret not in joined
    # 临时文件路径本身可以出现在命令行(它是重定向对象),但不能含 token
    assert "/tmp/.git_cred_" in joined


def test_inject_git_tokens_cleans_tmp_file_even_if_write_fails(two_providers):
    session = _FakeSession(raise_on=".git_cred_")
    # write_file 抛错不能冒泡(降级为无 token),但仍要补一次 rm 善后
    # —— 写可能只成功一半,含 token 的临时文件不能靠运气清理
    acp_base._inject_git_tokens_to_cache(
        session, "t", {"github": "ghp_SECRET_1"}
    )
    assert session.commands == [
        c for c in session.commands if c.startswith("rm -f ")
    ], session.commands
    assert len(session.commands) == 1


def test_inject_git_tokens_noop_without_valid_provider(monkeypatch):
    import app.git_provider as gp

    monkeypatch.setattr(gp, "PROVIDERS", {})
    session = _FakeSession()
    acp_base._inject_git_tokens_to_cache(session, "t", {"unknown": "tok"})
    assert session.commands == [] and session.written == {}


# ============================================================
# _wait_for_bridge_ready:成功路径不取日志
# ============================================================


class _HealthOK:
    def __init__(self, payload):
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


class _HealthFail:
    status_code = 503
    text = "cli_not_running"

    def json(self):
        raise ValueError("no body")


def _patch_httpx(monkeypatch, responses):
    """让 client.get 依次吐出给定响应;记录调用次数"""
    import httpx

    calls: list[str] = []

    class _Client:
        def __init__(self, headers=None, timeout=None):
            pass

        def get(self, url):
            calls.append(url)
            return responses[min(len(calls) - 1, len(responses) - 1)]

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(settings, "ACP_BRIDGE_HEALTH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(acp_base.time, "sleep", lambda *_a, **_kw: None)
    return calls


def test_wait_bridge_ready_success_does_not_fetch_logs(monkeypatch):
    """健康检查优先:一次通过就不取后台日志(取一次 = 一次沙箱往返 ≈0.7s)"""
    session = _FakeSession()
    logs_calls: list[str] = []

    def _logs(execution_id, cursor=None):
        logs_calls.append(execution_id)
        return "bridge log", 10

    session.get_background_logs = _logs
    _patch_httpx(monkeypatch, [_HealthOK({"status": "ok", "bridge_protocol": 2})])

    data = acp_base._wait_for_bridge_ready(
        session, "exec-1", "http://ep:8088", {"h": "1"}, "qoder_cli"
    )
    assert data["bridge_protocol"] == 2
    assert logs_calls == []


def test_wait_bridge_ready_keeps_fast_fail_on_cli_startup_error(monkeypatch):
    """bridge 起不来时仍要靠 "ACP CLI 启动失败" 日志快速抛错,不能空等到超时"""
    session = _FakeSession()
    session.get_background_logs = lambda execution_id, cursor=None: (
        "[bridge] ACP CLI 启动失败:FileNotFoundError,退出", None
    )
    _patch_httpx(monkeypatch, [_HealthFail()])

    with pytest.raises(RuntimeError, match="ACP bridge 启动失败"):
        acp_base._wait_for_bridge_ready(
            session, "exec-1", "http://ep:8088", {}, "qoder_cli"
        )


# ============================================================
# 记忆 / skill 文件:同会话内容未变跳过重写
# ============================================================


@pytest.fixture
def sandbox_ctx(monkeypatch):
    ctx = {"mode": "sandbox", "session": _FakeSession()}
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: ctx)
    return ctx


def test_write_project_memory_skips_unchanged(sandbox_ctx):
    session = sandbox_ctx["session"]
    mem_abs = f"{st._MEMORY_DIR_SANDBOX}/{st._MEMORY_FILE}"

    st.write_project_memory_file("t", "项目记忆 v1")
    first = (len(session.commands), len(session.written))
    assert first == (1, 1)
    assert session.written[mem_abs] == "项目记忆 v1"

    st.write_project_memory_file("t", "项目记忆 v1")   # 内容未变 → 不再发命令
    assert (len(session.commands), len(session.written)) == first

    st.write_project_memory_file("t", "项目记忆 v2")   # 内容变了 → 必须重写
    assert len(session.commands) == first[0] + 1
    assert session.written[mem_abs] == "项目记忆 v2"


def test_write_global_memory_fingerprint_reset_on_new_ctx(monkeypatch):
    """容器重建 → 新 ctx 没有指纹 → 必须重写(否则 agent 读不到记忆文件)"""
    ctx1 = {"mode": "sandbox", "session": _FakeSession()}
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: ctx1)
    st.write_global_memory_file("t", "同一份内容")

    ctx2 = {"mode": "sandbox", "session": _FakeSession()}
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: ctx2)
    st.write_global_memory_file("t", "同一份内容")
    assert len(ctx2["session"].commands) == 1


def test_write_skill_files_batches_mkdir_and_skips(tmp_path, sandbox_ctx):
    src = tmp_path / "sqli"
    src.mkdir()
    (src / "SKILL.md").write_text("body", encoding="utf-8")
    sub = src / "rules"
    sub.mkdir()
    (sub / "r.txt").write_text("rule", encoding="utf-8")
    skills = [SimpleNamespace(name="sqli", skill_dir=src)]

    st.write_skill_files("t", skills)
    session = sandbox_ctx["session"]
    # 1) 清目录+建根 合成一条;2) 父目录一次建齐(两个目录一条命令);之后只有 write_file
    assert len(session.commands) == 2, session.commands
    assert session.commands[0].startswith("rm -rf ") and "mkdir -p" in session.commands[0]
    assert session.commands[1].startswith("mkdir -p ")
    assert "sqli " in session.commands[1] or "sqli/" in session.commands[1]
    assert len(session.written) == 2

    st.write_skill_files("t", skills)   # 集合未变 → 0 次新命令
    assert len(session.commands) == 2


def test_write_skill_files_empty_set_still_clears(tmp_path, sandbox_ctx):
    src = tmp_path / "old"
    src.mkdir()
    (src / "SKILL.md").write_text("x", encoding="utf-8")

    st.write_skill_files("t", [SimpleNamespace(name="old", skill_dir=src)])
    st.write_skill_files("t", [])   # 空集合必须清残留(与改造前一致)
    session = sandbox_ctx["session"]
    assert session.commands[2].startswith("rm -rf ")
