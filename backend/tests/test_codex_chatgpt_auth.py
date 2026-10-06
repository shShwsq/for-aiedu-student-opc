"""Codex CLI ChatGPT 账户登录(离线注入 auth.json)单元测试。

覆盖本次改造的关键逻辑(见 .trae/documents/Codex-ChatGPT账户登录实施计划.md):
- registry:codex_cli 凭证字段包含 auth_mode/api_key/auth_json 及条件显隐 visible_when
- routers/agent_configs._merge_credentials:模式感知(切模式清除另一模式残留密钥、
  条件必填仅对可见字段生效)
- agents/codex_cli_agent._codex_pre_bridge_hook:chatgpt 分支写精简 config.toml + auth.json
- agents/codex_cli_agent._codex_post_bridge_hook:chatgpt 模式读回轮换后的 auth.json 并回写 DB
"""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agents.codex_cli_agent import (
    _codex_post_bridge_hook,
    _codex_pre_bridge_hook,
)
from app.agents.registry import get_credential_fields
from app.routers.agent_configs import _merge_credentials
from app.schemas.agent_configs import CredentialValue
from app.security import decrypt_secret, encrypt_secret


# ============================================================
# 测试替身
# ============================================================


class FakeSession:
    """记录写入文件 / 命令的假沙箱会话,read_file 从已写内容返回。"""

    def __init__(self, mode: str, local_dir: Path | None = None):
        self.mode = mode
        self.local_dir = local_dir
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def run_command(self, cmd: str, timeout: int = 60, check: bool = False) -> str:
        self.commands.append(cmd)
        return ""


class FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row


class FakeDb:
    """db.query(...).filter(...).first() 恒返回预设 row;queried=False 时视为未被调用。"""

    def __init__(self, row=None):
        self._row = row
        self.queried = False
        self.committed = False

    def query(self, model):
        self.queried = True
        return FakeQuery(self._row)

    def commit(self):
        self.committed = True


class FakeRow:
    def __init__(self, credentials_encrypted: str):
        self.credentials_encrypted = credentials_encrypted


# ============================================================
# registry 结构
# ============================================================


def test_codex_registry_has_chatgpt_fields():
    fields = {f["key"]: f for f in get_credential_fields("codex_cli")}
    assert "auth_mode" in fields
    assert "auth_json" in fields
    # auth_mode 是 select 且含 api_key / chatgpt 两个选项
    assert fields["auth_mode"]["type"] == "select"
    opts = {o["value"] for o in fields["auth_mode"]["options"]}
    assert {"api_key", "chatgpt"} <= opts
    # api_key 仅 api_key 模式可见,auth_json 仅 chatgpt 模式可见
    assert fields["api_key"].get("visible_when") == {"auth_mode": "api_key"}
    assert fields["auth_json"].get("visible_when") == {"auth_mode": "chatgpt"}
    # auth_json 用多行输入
    assert fields["auth_json"].get("multiline") is True


# ============================================================
# _merge_credentials 模式感知
# ============================================================


def _cv(key: str, value: str) -> CredentialValue:
    return CredentialValue(key=key, value=value)


def test_merge_drops_api_key_when_switching_to_chatgpt():
    old = {"auth_mode": "api_key", "api_key": "sk-old", "base_url": "https://x/v1"}
    new = [_cv("auth_mode", "chatgpt"), _cv("auth_json", '{"tokens":{"access_token":"a"}}')]
    result = _merge_credentials("codex_cli", old, new)
    assert result.get("auth_mode") == "chatgpt"
    assert "api_key" not in result  # 切模式清除残留密钥
    assert "base_url" not in result
    assert result["auth_json"] == '{"tokens":{"access_token":"a"}}'


def test_merge_drops_auth_json_when_switching_to_api_key():
    old = {"auth_mode": "chatgpt", "auth_json": '{"tokens":{"access_token":"a"}}'}
    new = [_cv("auth_mode", "api_key"), _cv("api_key", "sk-new")]
    result = _merge_credentials("codex_cli", old, new)
    assert "auth_json" not in result
    assert result["api_key"] == "sk-new"


def test_merge_chatgpt_first_save_requires_auth_json():
    # 切到 chatgpt 但没给 auth.json,且旧值也没有 → 必填报错
    with pytest.raises(HTTPException):
        _merge_credentials("codex_cli", {}, [_cv("auth_mode", "chatgpt")])


def test_merge_api_key_mode_does_not_require_auth_json():
    # api_key 模式:auth_json 不可见,即便没填也不应触发其必填校验
    old = {"auth_mode": "api_key", "api_key": "sk-keep"}
    new = [_cv("auth_mode", "api_key"), _cv("api_key", "")]  # 空 → 保留旧值
    result = _merge_credentials("codex_cli", old, new)
    assert result["api_key"] == "sk-keep"


# ============================================================
# _codex_pre_bridge_hook chatgpt 分支
# ============================================================


_VALID_AUTH = json.dumps(
    {"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": "r"}}
)


def test_pre_hook_chatgpt_writes_auth_json_and_slim_config(tmp_path):
    session = FakeSession(mode="local", local_dir=tmp_path)
    creds = {"auth_mode": "chatgpt", "auth_json": _VALID_AUTH, "model": "gpt-5"}
    envs = _codex_pre_bridge_hook(session, creds, "codex_cli", task=None)

    # local 模式返回 CODEX_HOME 指向临时 .codex
    assert envs == {"CODEX_HOME": str(tmp_path / ".codex")}
    # auth.json 已写入且为原文
    assert session.files[".codex/auth.json"] == _VALID_AUTH
    # 精简 config.toml:不含自定义 provider / env_key(否则会被迫走 API Key 路径)
    toml = session.files[".codex/config.toml"]
    assert 'model = "gpt-5"' in toml
    assert "model_provider" not in toml
    assert "env_key" not in toml
    assert "[model_providers" not in toml


def test_pre_hook_chatgpt_sandbox_mode(tmp_path):
    session = FakeSession(mode="sandbox")
    creds = {"auth_mode": "chatgpt", "auth_json": _VALID_AUTH, "model": ""}
    envs = _codex_pre_bridge_hook(session, creds, "codex_cli", task=None)

    assert envs is None
    assert "mkdir -p ~/.codex" in session.commands
    assert session.files["~/.codex/auth.json"] == _VALID_AUTH
    # model 留空回退默认 gpt-5
    assert 'model = "gpt-5"' in session.files["~/.codex/config.toml"]


def test_pre_hook_chatgpt_rejects_invalid_json():
    session = FakeSession(mode="local", local_dir=Path("/tmp/fake"))
    creds = {"auth_mode": "chatgpt", "auth_json": "not-json{"}
    with pytest.raises(RuntimeError):
        _codex_pre_bridge_hook(session, creds, "codex_cli", task=None)


def test_pre_hook_chatgpt_requires_auth_json():
    session = FakeSession(mode="local", local_dir=Path("/tmp/fake"))
    creds = {"auth_mode": "chatgpt", "auth_json": "  "}
    with pytest.raises(RuntimeError):
        _codex_pre_bridge_hook(session, creds, "codex_cli", task=None)


def test_pre_hook_api_key_keeps_provider_when_base_url(tmp_path):
    session = FakeSession(mode="local", local_dir=tmp_path)
    creds = {
        "auth_mode": "api_key",
        "api_key": "sk-1",
        "base_url": "https://relay.example/v1",
        "model": "gpt-5",
    }
    envs = _codex_pre_bridge_hook(session, creds, "codex_cli", task=None)
    toml = session.files[".codex/config.toml"]
    assert 'model_provider = "secondlook"' in toml
    assert 'env_key = "CODEX_API_KEY"' in toml
    # api_key 模式不写 auth.json
    assert ".codex/auth.json" not in session.files
    assert envs == {"CODEX_HOME": str(tmp_path / ".codex")}


# ============================================================
# _codex_post_bridge_hook 回写
# ============================================================


def test_post_hook_skips_non_chatgpt():
    db = FakeDb(row=None)
    session = FakeSession(mode="local", local_dir=Path("/tmp/fake"))
    _codex_post_bridge_hook(
        session, {"auth_mode": "api_key", "api_key": "sk-1"}, "codex_cli", db, "user-1"
    )
    assert db.queried is False  # 非 chatgpt 模式完全不碰 DB


def test_post_hook_writes_back_rotated_tokens():
    old_encrypted = encrypt_secret(
        json.dumps({"auth_mode": "chatgpt", "auth_json": _VALID_AUTH})
    )
    row = FakeRow(credentials_encrypted=old_encrypted)
    db = FakeDb(row=row)

    # 沙箱内 auth.json 已被 codex 轮换(新 access_token)
    rotated = json.dumps(
        {"auth_mode": "chatgpt", "tokens": {"access_token": "NEW", "refresh_token": "r2"}}
    )
    session = FakeSession(mode="local", local_dir=Path("/tmp/fake"))
    session.files[".codex/auth.json"] = rotated

    _codex_post_bridge_hook(session, {"auth_mode": "chatgpt"}, "codex_cli", db, "user-1")

    assert db.committed is True
    creds = json.loads(decrypt_secret(row.credentials_encrypted))
    assert json.loads(creds["auth_json"])["tokens"]["access_token"] == "NEW"


def test_post_hook_no_write_when_unchanged():
    same = json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "a", "refresh_token": "r"}})
    row = FakeRow(credentials_encrypted=encrypt_secret(json.dumps({"auth_json": same})))
    db = FakeDb(row=row)
    session = FakeSession(mode="local", local_dir=Path("/tmp/fake"))
    # 读回内容与已存 auth_json 规范化后一致 → 不重复写库
    session.files[".codex/auth.json"] = same

    _codex_post_bridge_hook(session, {"auth_mode": "chatgpt"}, "codex_cli", db, "user-1")
    assert db.committed is False


def test_post_hook_skips_when_no_tokens_in_env_auth():
    row = FakeRow(credentials_encrypted=encrypt_secret(json.dumps({"auth_json": _VALID_AUTH})))
    db = FakeDb(row=row)
    session = FakeSession(mode="local", local_dir=Path("/tmp/fake"))
    # 沙箱读回的内容没有 tokens(异常状态)→ 不回写,避免污染已存凭证
    session.files[".codex/auth.json"] = json.dumps({"garbage": True})

    _codex_post_bridge_hook(session, {"auth_mode": "chatgpt"}, "codex_cli", db, "user-1")
    assert db.committed is False
