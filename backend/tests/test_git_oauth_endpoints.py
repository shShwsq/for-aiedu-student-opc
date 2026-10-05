"""Git 平台 OAuth 登录 / 绑定端点的行为回归测试(独立 PostgreSQL schema,不连外网)

覆盖三条真实故障(平台调用全部用假 provider 替掉,测试不发出任何外部请求):
1. `binding.user` 取本地用户:UserGitBinding 曾缺反向关系 → AttributeError →
   500,且 FastAPI 无 detail,前端只显示"请求失败(500)";已有绑定行的用户
   永远登录不了(模型层配对由 test_git_binding_relationship.py 锁,这里锁端点)
2. 链路/平台故障(GitProviderUnavailable)必须以 502/504 呈现并提示稍后重试,
   真正的 OAuth 参数错(授权码失效等)才是 400 —— 混成 400 会把用户引去改 .env
3. 邮箱已注册但该平台已绑另一个账号:再插一行会撞 uq_user_provider 唯一约束,
   裸 IntegrityError 是 500,现在明确 409

无建 schema 权限(数据库不可用)时整体跳过,同 test_practice_api。
"""
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.deps import get_current_user

# 确保相关模型全部注册到 Base.metadata(create_all 才能建全表;
# User.git_bindings 为 selectin 关系,读 user 时会触发,必须一并导入)
import app.models.email_token  # noqa: F401
import app.models.project  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.user  # noqa: F401
import app.models.user_agent_config  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_llm_config  # noqa: F401
import app.models.user_memory  # noqa: F401
import app.models.user_preference  # noqa: F401
from app.git_provider import (
    GitProviderError,
    GitProviderUnavailable,
    OAuthTokenSet,
    ProviderUserInfo,
)
from app.models.user import User
from app.models.user_git_binding import UserGitBinding
from app.routers import auth as auth_router
from app.routers import git_provider as git_router

# schema 名含 PID + 随机后缀:并发 pytest 进程各自独立建 schema
TEST_SCHEMA = f"pytest_git_oauth_{os.getpid()}_{uuid.uuid4().hex[:8]}"
_TABLES = ("user_git_bindings", "users")


@pytest.fixture(scope="session")
def test_engine():
    try:
        maint = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with maint.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
        maint.dispose()
        engine = create_engine(
            settings.DATABASE_URL,
            connect_args={"options": f"-c search_path={TEST_SCHEMA}"},
        )
        Base.metadata.create_all(engine)
    except Exception as e:
        pytest.skip(f"无法创建测试 schema(数据库不可用或权限不足): {e}")
    yield engine
    engine.dispose()
    try:
        cleanup = create_engine(settings.DATABASE_URL, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        cleanup.dispose()
    except Exception:
        pass  # 清理失败不影响测试结果


@pytest.fixture(autouse=True)
def _clean_tables(test_engine):
    with test_engine.begin() as conn:
        for t in _TABLES:
            conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
    yield


@pytest.fixture()
def factory(test_engine):
    return sessionmaker(bind=test_engine, autoflush=False)


@pytest.fixture()
def app(factory, monkeypatch):
    """只挂两个被测路由,db 指向测试 schema(不连真实平台)"""
    application = FastAPI()
    application.include_router(auth_router.router)
    application.include_router(git_router.router)

    def override_get_db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    application.dependency_overrides[get_db] = override_get_db
    return application


@pytest.fixture()
def client(app):
    return TestClient(app)


class FakeProvider:
    """鸭子类型假 provider:端点只用到下列成员,不发外部请求"""

    id = "gitee"
    display_name = "Gitee"
    host = "gitee.com"
    token_username = "oauth2"
    authorize_url = "https://gitee.com/oauth/authorize"
    scope_login = "user_info"
    scope_bind = "user_info projects"
    supports_verified_email = False
    supports_token_refresh = False

    def __init__(self, *, info=None, token_set=None, error=None):
        self._info = info
        self._token_set = token_set
        self._error = error

    def _maybe_raise(self):
        if self._error is not None:
            raise self._error

    def oauth_login(self, code):
        self._maybe_raise()
        return self._info

    def exchange_code_for_token(self, code):
        self._maybe_raise()
        return self._token_set or OAuthTokenSet(access_token="ghu_token")

    def get_user_info(self, access_token):
        self._maybe_raise()
        return self._info

    def get_user_emails(self, access_token):
        return []


def _info(pid="1001", email="gitee-user@example.com", login="shwsq"):
    return ProviderUserInfo(
        provider_user_id=pid,
        email=email,
        login=login,
        name=None,
        avatar_url="https://avatar/1.png",
    )


def _seed_user(factory, email, *, provider=None, pid=None, token=""):
    """建用户(可带一行该平台的绑定)"""
    db = factory()
    try:
        user = User(email=email, password_hash="x")
        db.add(user)
        db.flush()
        if provider:
            db.add(UserGitBinding(
                user_id=user.id,
                provider=provider,
                provider_user_id=pid,
                access_token=token,
            ))
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _use(monkeypatch, provider):
    """把 auth 与 git 两组路由的 get_provider 都换成假 provider"""
    monkeypatch.setattr(auth_router, "get_provider", lambda pid: provider)
    monkeypatch.setattr(git_router, "get_provider", lambda pid: provider)


def _login(client, code="the-code"):
    return client.post("/auth/oauth/gitee", json={"code": code})


# ============================================================
# 分支 1:已有绑定行 → 登录(曾经的 500 回归点)
# ============================================================


def test_login_with_existing_binding_returns_tokens(app, client, factory, monkeypatch):
    """binding.user 取回用户:这一步曾抛 AttributeError → 500"""
    _seed_user(factory, "owner@example.com", provider="gitee", pid="1001")
    _use(monkeypatch, FakeProvider(info=_info(pid="1001", email="other@example.com")))

    r = _login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    # 登录身份以绑定行为准,而不是平台返回的邮箱
    assert body["user"]["email"] == "owner@example.com"


# ============================================================
# 分支 2:邮箱已注册 → 关联
# ============================================================


def test_login_links_existing_email_with_no_binding(client, factory, app, monkeypatch):
    _seed_user(factory, "gitee-user@example.com")
    _use(monkeypatch, FakeProvider(info=_info(pid="2002")))

    r = _login(client)
    assert r.status_code == 200, r.text
    db = factory()
    try:
        binding = db.query(UserGitBinding).filter_by(provider="gitee").one()
        assert binding.provider_user_id == "2002"
        assert binding.access_token == ""  # 仅登录,未授权仓库
    finally:
        db.close()


def test_login_conflicts_when_email_already_bound_to_other_account(
    client, factory, app, monkeypatch
):
    """同邮箱用户已绑另一个平台账号 → 409(过去插第二行撞唯一约束 → 500)"""
    _seed_user(factory, "gitee-user@example.com", provider="gitee", pid="1001")
    _use(monkeypatch, FakeProvider(info=_info(pid="2002")))

    r = _login(client)
    assert r.status_code == 409, r.text
    assert "已绑定另一个" in r.json()["detail"]
    db = factory()
    try:
        # 既有绑定不被改写
        assert db.query(UserGitBinding).filter_by(provider="gitee").one().provider_user_id == "1001"
    finally:
        db.close()


# ============================================================
# 分支 3:全新用户 → 自动注册
# ============================================================


def test_login_auto_registers_new_user(client, factory, app, monkeypatch):
    _use(monkeypatch, FakeProvider(info=_info(pid="3003", email="brand-new@example.com")))

    r = _login(client)
    assert r.status_code == 200, r.text
    body = r.json()["user"]
    assert body["has_password"] is False  # OAuth 用户无密码
    assert body["git_providers"] == ["gitee"]
    db = factory()
    try:
        user = db.query(User).filter_by(email="brand-new@example.com").one()
        assert user.password_hash == ""  # OAuth 用户无密码
        assert user.is_email_verified  # OAuth 隐含邮箱已验证
        assert db.query(UserGitBinding).filter_by(user_id=user.id, provider="gitee").one()
    finally:
        db.close()


def test_login_without_provider_email_cannot_auto_register(client, app, monkeypatch):
    """平台不给邮箱(Gitee 私密邮箱 / GitHub 无 primary)且无绑定 → 400 说明原因"""
    _use(monkeypatch, FakeProvider(info=_info(pid="4004", email=None)))

    r = _login(client)
    assert r.status_code == 400, r.text
    assert "无可用邮箱" in r.json()["detail"]


# ============================================================
# 错误分类:链路故障 vs 真正的 OAuth 错误
# ============================================================


def test_login_read_timeout_maps_to_504(client, app, monkeypatch):
    _use(monkeypatch, FakeProvider(
        error=GitProviderUnavailable("换取 access_token 失败: timed out", timeout=True)
    ))
    r = _login(client)
    assert r.status_code == 504, r.text
    detail = r.json()["detail"]
    assert "响应超时" in detail and "稍后重试" in detail


def test_login_connect_error_maps_to_502(client, app, monkeypatch):
    _use(monkeypatch, FakeProvider(
        error=GitProviderUnavailable(
            "换取 access_token 失败: [SSL: UNEXPECTED_EOF_WHILE_READING]", timeout=False
        )
    ))
    r = _login(client)
    assert r.status_code == 502, r.text
    assert "无法连接 Gitee" in r.json()["detail"]


def test_login_expired_code_stays_400(client, app, monkeypatch):
    """授权码失效是真正的 OAuth 错误:400,detail 与改动前一致"""
    msg = "换取 access_token 失败: The code passed is incorrect or expired."
    _use(monkeypatch, FakeProvider(error=GitProviderError(msg)))

    r = _login(client)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == msg


def test_bind_read_timeout_maps_to_504(client, factory, app, monkeypatch):
    """绑定端点同样不再把网络故障说成配置错误"""
    user = _seed_user(factory, "owner@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(git_router, "get_provider", lambda pid: FakeProvider(
        error=GitProviderUnavailable("换取 access_token 失败: timed out", timeout=True)
    ))

    r = client.post("/git/gitee/bind", json={"code": "c"})
    assert r.status_code == 504, r.text
    assert "稍后重试" in r.json()["detail"]


def test_bind_success_writes_binding_and_flags_email_mismatch(
    client, factory, app, monkeypatch
):
    """绑定成功路径:token 落库(加密),邮箱不一致时给出提示字段"""
    user = _seed_user(factory, "local@example.com", provider="gitee", pid="1001")
    app.dependency_overrides[get_current_user] = lambda: user
    _use(monkeypatch, FakeProvider(info=_info(pid="1001", email="platform@example.com")))

    r = client.post("/git/gitee/bind", json={"code": "c"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound"] is True
    assert body["provider_login"] == "shwsq"
    assert body["email_mismatch"] is True
    assert body["provider_email"] == "platform@example.com"
    db = factory()
    try:
        binding = db.query(UserGitBinding).filter_by(user_id=user.id, provider="gitee").one()
        assert binding.access_token  # 密文非空
        assert binding.access_token != "ghu_token"  # 确实加密过
    finally:
        db.close()
