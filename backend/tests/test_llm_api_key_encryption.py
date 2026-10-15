"""用户 LLM/Embedding api_key 字段级加密(enc:v1 信封)单元测试。

离线为主(不连真实 DB),覆盖本次改造的关键链路:
- security.encrypt_secret_envelope / decrypt_secret_envelope:往返 + 前缀 + 幂等 + 明文兼容
- llm.client.LLMClient.from_config_dict:密文透明解密 + 历史明文兼容 + 缺 key 报错
- models.user_llm_config.decrypt_config_api_keys:解密返回明文浅拷贝,不改原对象,混合列表
- models.user_llm_config._encrypt_keys_in_list:迁移核心,幂等 + 混合明文/密文
- routers.model_configs._merge_llm_configs / _merge_embedding_configs:新 key 加密,
  空串保留旧密文不二次加密
另附一个 @pytest.mark.db 用例,验证 migrate_encrypt_llm_api_keys 落库 + 幂等。

约定见 app/security.py 与 app/models/user_llm_config.py 模块文档。
"""
import os
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app import security
from app.llm.client import LLMClient
from app.models.user_llm_config import (
    _encrypt_keys_in_list,
    decrypt_config_api_keys,
)
from app.routers.model_configs import (
    _merge_embedding_configs,
    _merge_llm_configs,
)
from app.security import (
    decrypt_secret_envelope,
    encrypt_secret_envelope,
    is_enveloped,
)


# ============================================================
# fixtures:固定 Fernet 密钥,避免 .env 干扰
# ============================================================

@pytest.fixture
def fernet_key(monkeypatch):
    """固定 GITHUB_TOKEN_SECRET 为合法 Fernet key,并清单例缓存。"""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(security.settings, "GITHUB_TOKEN_SECRET", key)
    security._get_fernet.cache_clear()
    yield key
    security._get_fernet.cache_clear()


def _mk_item(**kw):
    """构造 _merge_* 需要的请求项对象(与 pydantic LLMConfigItem 字段对齐)。"""
    base = dict(
        id="c1", name="测试", provider="deepseek", api_key="sk-plain",
        model="deepseek-v4-pro", enable_thinking=True, base_url=None,
        max_output_tokens=None, dimension=1024,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ============================================================
# 信封加解密:往返 / 前缀 / 幂等 / 兼容
# ============================================================

def test_envelope_round_trip(fernet_key):
    plain = "sk-secret-value-123"
    enc = encrypt_secret_envelope(plain)
    assert enc.startswith("enc:v1:")
    assert enc != plain
    assert decrypt_secret_envelope(enc) == plain


def test_envelope_is_enveloped_detects_prefix(fernet_key):
    assert is_enveloped(encrypt_secret_envelope("x")) is True
    assert is_enveloped("sk-plaintext") is False
    assert is_enveloped("") is False


def test_envelope_encrypt_is_idempotent(fernet_key):
    """已带前缀的值再加密应原样返回(避免空串保留旧 key 时二次包裹)。"""
    once = encrypt_secret_envelope("sk-a")
    twice = encrypt_secret_envelope(once)
    assert twice == once
    assert decrypt_secret_envelope(twice) == "sk-a"


def test_envelope_encrypt_empty_returns_empty(fernet_key):
    assert encrypt_secret_envelope("") == ""


def test_envelope_decrypt_plaintext_passthrough(fernet_key):
    """无前缀的历史明文解密原样返回(兼容未迁移数据)。"""
    assert decrypt_secret_envelope("sk-legacy-plain") == "sk-legacy-plain"
    assert decrypt_secret_envelope("") == ""


def test_envelope_decrypt_with_wrong_key_raises(fernet_key, monkeypatch):
    """换 key 后解密 enc:v1 密文 → ValueError(密钥不匹配)。"""
    enc = encrypt_secret_envelope("sk-secret")
    monkeypatch.setattr(security.settings, "GITHUB_TOKEN_SECRET", Fernet.generate_key().decode())
    security._get_fernet.cache_clear()
    with pytest.raises(ValueError):
        decrypt_secret_envelope(enc)


# ============================================================
# from_config_dict:密文透明解密 / 明文兼容 / 缺 key
# ============================================================

def test_from_config_dict_decrypts_ciphertext(fernet_key):
    cfg = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "api_key": encrypt_secret_envelope("sk-real-key"),
    }
    client = LLMClient.from_config_dict(cfg)
    assert client.api_key == "sk-real-key"


def test_from_config_dict_accepts_legacy_plaintext(fernet_key):
    """历史明文(无前缀)仍可用,兼容迁移未跑完的数据。"""
    cfg = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "api_key": "sk-plain-old",
    }
    client = LLMClient.from_config_dict(cfg)
    assert client.api_key == "sk-plain-old"


def test_from_config_dict_missing_key_raises(fernet_key):
    with pytest.raises(ValueError, match="api_key"):
        LLMClient.from_config_dict({"provider": "deepseek", "model": "m", "api_key": ""})


# ============================================================
# decrypt_config_api_keys:明文浅拷贝,不改原对象
# ============================================================

def test_decrypt_config_api_keys_returns_plaintext_copy_without_mutation(fernet_key):
    enc = encrypt_secret_envelope("sk-z")
    original = [{"id": "c1", "api_key": enc}]
    out = decrypt_config_api_keys(original)
    assert out[0]["api_key"] == "sk-z"
    # 原对象(ORM 列来源)保持密文,防止误 commit 把明文写回库
    assert original[0]["api_key"] == enc
    assert out[0] is not original[0]


def test_decrypt_config_api_keys_mixed_plaintext_and_ciphertext(fernet_key):
    enc = encrypt_secret_envelope("sk-enc")
    configs = [
        {"id": "a", "api_key": enc},
        {"id": "b", "api_key": "sk-plain"},
        {"id": "c"},  # 无 api_key
    ]
    out = decrypt_config_api_keys(configs)
    assert out[0]["api_key"] == "sk-enc"
    assert out[1]["api_key"] == "sk-plain"  # 明文透传
    assert "api_key" not in out[2]


def test_decrypt_config_api_keys_handles_none(fernet_key):
    assert decrypt_config_api_keys(None) == []


# ============================================================
# _encrypt_keys_in_list:迁移核心,幂等 + 混合
# ============================================================

def test_encrypt_keys_in_list_encrypts_plaintext(fernet_key):
    configs = [{"id": "a", "api_key": "sk-plain"}]
    out, count = _encrypt_keys_in_list(configs)
    assert count == 1
    assert is_enveloped(out[0]["api_key"])
    assert decrypt_secret_envelope(out[0]["api_key"]) == "sk-plain"
    # 不改原对象
    assert configs[0]["api_key"] == "sk-plain"


def test_encrypt_keys_in_list_is_idempotent(fernet_key):
    enc = encrypt_secret_envelope("sk-x")
    configs = [{"id": "a", "api_key": enc}]
    out, count = _encrypt_keys_in_list(configs)
    assert count == 0  # 已加密,跳过
    assert out[0]["api_key"] == enc  # 保持不变,不二次包裹


def test_encrypt_keys_in_list_mixed(fernet_key):
    enc = encrypt_secret_envelope("sk-enc")
    configs = [
        {"id": "a", "api_key": enc},       # 已加密
        {"id": "b", "api_key": "sk-plain"},  # 明文
        {"id": "c"},                          # 无 key
        "not-a-dict",                         # 脏数据透传
    ]
    out, count = _encrypt_keys_in_list(configs)
    assert count == 1  # 只加密 b
    assert out[0]["api_key"] == enc
    assert is_enveloped(out[1]["api_key"])
    assert "api_key" not in out[2]
    assert out[3] == "not-a-dict"


def test_encrypt_keys_in_list_non_list_passthrough(fernet_key):
    out, count = _encrypt_keys_in_list(None)
    assert out is None and count == 0


# ============================================================
# router 写入:_merge_llm_configs / _merge_embedding_configs
# ============================================================

def test_merge_llm_configs_encrypts_new_key(fernet_key):
    result = _merge_llm_configs(existing=[], new_items=[_mk_item(api_key="sk-new")])
    assert is_enveloped(result[0]["api_key"])
    assert decrypt_secret_envelope(result[0]["api_key"]) == "sk-new"


def test_merge_llm_configs_preserves_old_ciphertext_no_double_encrypt(fernet_key):
    """空串保留旧 key:旧值已是密文,幂等不应再包一层。"""
    old_enc = encrypt_secret_envelope("sk-old")
    existing = [{"id": "c1", "api_key": old_enc}]
    # 请求项 api_key 传空串 → 走保留旧值分支
    result = _merge_llm_configs(existing=existing, new_items=[_mk_item(api_key="")])
    assert result[0]["api_key"] == old_enc
    assert decrypt_secret_envelope(result[0]["api_key"]) == "sk-old"


def test_merge_llm_configs_requires_key_for_new_item(fernet_key):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _merge_llm_configs(existing=[], new_items=[_mk_item(id="brand-new", api_key="")])


def test_merge_embedding_configs_encrypts_new_key(fernet_key):
    result = _merge_embedding_configs(existing=[], new_items=[_mk_item(api_key="sk-emb")])
    assert is_enveloped(result[0]["api_key"])
    assert decrypt_secret_envelope(result[0]["api_key"]) == "sk-emb"


# ============================================================
# 空密钥防呆(P1):GITHUB_TOKEN_SECRET 留空时宁存明文不存死密文
# ============================================================

@pytest.fixture
def no_key(monkeypatch):
    """把 GITHUB_TOKEN_SECRET 置空(开发默认),并清单例缓存。"""
    monkeypatch.setattr(security.settings, "GITHUB_TOKEN_SECRET", "")
    security._get_fernet.cache_clear()
    yield
    security._get_fernet.cache_clear()


def test_encrypt_secret_envelope_empty_key_stores_plaintext(no_key):
    """无持久密钥时加密返回明文(无前缀),不会落重启即不可解的死密文。"""
    enc = encrypt_secret_envelope("sk-plain")
    assert enc == "sk-plain"
    assert is_enveloped(enc) is False
    assert decrypt_secret_envelope(enc) == "sk-plain"


def test_merge_llm_configs_empty_key_stores_plaintext(no_key):
    result = _merge_llm_configs(existing=[], new_items=[_mk_item(api_key="sk-new")])
    assert result[0]["api_key"] == "sk-new"  # 未加密,维持改造前行为


def test_migrate_skips_when_key_empty(no_key, monkeypatch):
    """空密钥时迁移入口早退,绝不连接 DB(否则临时 key 会毁掉存量明文)。"""
    import app.database as database_module
    from app.models.user_llm_config import migrate_encrypt_llm_api_keys

    calls = {"connect": False}

    class _Boom:
        def connect(self):
            calls["connect"] = True
            raise AssertionError("空密钥下不应连接数据库")

    monkeypatch.setattr(database_module, "engine", _Boom())
    migrate_encrypt_llm_api_keys()  # 不应抛异常
    assert calls["connect"] is False


# ============================================================
# test 端点解密失败(P3):返回 success=False 而非裸 500
# ============================================================

class _FakeQuery:
    def __init__(self, cfg):
        self._cfg = cfg

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._cfg


class _FakeDB:
    def __init__(self, cfg):
        self._cfg = cfg

    def query(self, model):
        return _FakeQuery(self._cfg)


def _dead_ciphertext():
    """用另一把 Fernet 密钥生成的合法密文:当前密钥解不开 → ValueError。"""
    other = Fernet(Fernet.generate_key())
    return "enc:v1:" + other.encrypt(b"stale").decode()


def test_test_llm_returns_failure_on_decrypt_error(fernet_key):
    from app.routers import model_configs as mc
    from app.schemas.model_configs import TestRequest

    cfg = SimpleNamespace(
        llm_configs=[{
            "id": "c1", "provider": "deepseek", "model": "deepseek-v4-pro",
            "api_key": _dead_ciphertext(),
        }],
        embedding_configs=[],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    resp = mc.test_llm(TestRequest(config_id="c1"), user, _FakeDB(cfg))
    assert resp.success is False
    assert "解密失败" in resp.message


def test_test_embedding_returns_failure_on_decrypt_error(fernet_key):
    from app.routers import model_configs as mc
    from app.schemas.model_configs import TestRequest

    cfg = SimpleNamespace(
        llm_configs=[],
        embedding_configs=[{
            "id": "e1", "provider": "dashscope", "model": "text-embedding-v2",
            "api_key": _dead_ciphertext(), "dimension": 1024,
        }],
    )
    user = SimpleNamespace(id=uuid.uuid4())
    resp = mc.test_embedding(TestRequest(config_id="e1"), user, _FakeDB(cfg))
    assert resp.success is False
    assert "解密失败" in resp.message


# ============================================================
# 迁移落库(DB,库不可达时 conftest 统一跳过)
# ============================================================

TEST_SCHEMA = f"pytest_llmenc_{os.getpid()}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def test_engine():
    """隔离 schema 内建全量表;库不可达则 skip。"""
    from sqlalchemy import create_engine, text

    from app.config import settings
    from app.database import Base

    try:
        maint = create_engine(
            settings.DATABASE_URL, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"connect_timeout": 5, "options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(
            settings.DATABASE_URL, isolation_level="AUTOCOMMIT",
            connect_args={"connect_timeout": 5},
        )
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.db
def test_migrate_encrypt_llm_api_keys_encrypts_and_is_idempotent(test_engine, fernet_key, monkeypatch):
    """迁移把存量明文 api_key 就地加密;重跑不二次加密,解密仍得原值。"""
    from sqlalchemy.orm import sessionmaker

    from app.models.user import User
    from app.models.user_llm_config import UserLLMConfig, migrate_encrypt_llm_api_keys
    import app.database as database_module

    # migrate 内部延迟 import app.database.engine,这里替换为测试引擎
    monkeypatch.setattr(database_module, "engine", test_engine)

    factory = sessionmaker(bind=test_engine)
    db = factory()
    user = User(id=uuid.uuid4(), email="enc@example.com", password_hash="x")
    db.add(user)
    db.add(UserLLMConfig(
        user_id=user.id,
        llm_configs=[{"id": "a", "provider": "deepseek", "model": "m", "api_key": "sk-plain-a"}],
        embedding_configs=[{"id": "b", "provider": "dashscope", "model": "e", "api_key": "sk-plain-b"}],
    ))
    db.commit()
    db.close()

    migrate_encrypt_llm_api_keys()

    db = factory()
    row = db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user.id).first()
    llm_key = row.llm_configs[0]["api_key"]
    emb_key = row.embedding_configs[0]["api_key"]
    assert is_enveloped(llm_key) and is_enveloped(emb_key)
    assert decrypt_secret_envelope(llm_key) == "sk-plain-a"
    assert decrypt_secret_envelope(emb_key) == "sk-plain-b"
    db.close()

    # 再跑一次:密文保持不变(幂等)
    migrate_encrypt_llm_api_keys()
    db = factory()
    row = db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user.id).first()
    assert row.llm_configs[0]["api_key"] == llm_key
    assert row.embedding_configs[0]["api_key"] == emb_key
    db.close()
