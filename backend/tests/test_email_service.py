"""email_service.send_email 的 SMTP 投递测试

覆盖:
- 未配 SMTP_HOST → 开发模式,只打印日志,不碰 smtplib
- 配了 SMTP → 按 SMTP_SECURITY 选择 SMTP / SMTP_SSL,带 timeout,starttls 升级 + 登录 + 投递
- 中文主题走 EmailMessage(RFC2047 编码)不报错
- 发信失败只记日志,不向上抛(注册/重发已 commit,SMTP 抖动不应 500)
- 发件人地址回落 SMTP_USERNAME
"""
from __future__ import annotations

import pytest

from app import email_service
from app.config import settings


class _FakeSMTP:
    """记录调用参数的假 SMTP 客户端(支持 with 上下文)"""

    init_kwargs: dict = {}
    calls: list[str] = []

    def __init__(self, host, port, **kwargs):
        _FakeSMTP.init_kwargs = {"host": host, "port": port, **kwargs}
        _FakeSMTP.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        _FakeSMTP.calls.append("starttls")

    def login(self, user, password):
        _FakeSMTP.calls.append(("login", user, password))

    def send_message(self, msg):
        _FakeSMTP.calls.append(("send_message", msg))


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeSMTP.init_kwargs = {}
    _FakeSMTP.calls = []
    yield


def _patch_smtp(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", _FakeSMTP)


def test_dev_mode_logs_without_smtp(monkeypatch):
    """SMTP_HOST 留空 → 不调用 smtplib,走日志降级"""
    monkeypatch.setattr(settings, "SMTP_HOST", "", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("a@example.com", "主题", "正文")

    assert _FakeSMTP.calls == []  # 完全没碰 SMTP


def test_starttls_connects_upgrades_and_logs_in(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_PORT", 587, raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "starttls", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "noreply@example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "secret", raising=False)
    monkeypatch.setattr(settings, "SMTP_TIMEOUT_SECONDS", 7, raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "[SecondLook] 验证你的邮箱", "点链接")

    assert _FakeSMTP.init_kwargs["host"] == "smtp.example.com"
    assert _FakeSMTP.init_kwargs["port"] == 587
    assert _FakeSMTP.init_kwargs["timeout"] == 7
    # starttls 升级 + 登录 + 投递
    kinds = [c if isinstance(c, str) else c[0] for c in _FakeSMTP.calls]
    assert kinds == ["starttls", "login", "send_message"]
    login = next(c for c in _FakeSMTP.calls if isinstance(c, tuple) and c[0] == "login")
    assert login == ("login", "noreply@example.com", "secret")


def test_ssl_uses_implicit_tls_without_starttls(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_PORT", 465, raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "ssl", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "主题", "正文")

    kinds = [c if isinstance(c, str) else c[0] for c in _FakeSMTP.calls]
    # ssl 不做 starttls;username 空则不 login
    assert kinds == ["send_message"]


def test_invalid_security_falls_back_to_starttls(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "garbage", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "主题", "正文")

    kinds = [c if isinstance(c, str) else c[0] for c in _FakeSMTP.calls]
    assert "starttls" in kinds


def test_message_headers_and_from_fallback(monkeypatch):
    """From 未显式配置时回落 SMTP_USERNAME;中文主题可正常构造"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "none", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "noreply@example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "", raising=False)
    monkeypatch.setattr(settings, "SMTP_FROM_NAME", "SecondLook", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "[SecondLook] 验证你的邮箱", "正文")

    msg = next(c for c in _FakeSMTP.calls if isinstance(c, tuple) and c[0] == "send_message")[1]
    assert msg["To"] == "user@example.com"
    assert "noreply@example.com" in msg["From"]
    assert "SecondLook" in msg["From"]
    # 主题解码回来应是原文(EmailMessage 处理 RFC2047)
    assert "验证你的邮箱" in str(msg["Subject"]) or msg["Subject"] == "[SecondLook] 验证你的邮箱"


def test_delivery_failure_is_swallowed(monkeypatch):
    """SMTP 抛错时 send_email 不向上抛(只记日志)"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "none", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "", raising=False)

    class _Boom(_FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection reset")

    monkeypatch.setattr(email_service.smtplib, "SMTP", _Boom)
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", _Boom)

    # 不应抛异常
    email_service.send_email("user@example.com", "主题", "正文")


# ============================================================
# 边界加固(第三方 review 提出的 strip / 空 From / 失败吞掉)
# ============================================================


def test_whitespace_host_falls_back_to_dev_mode(monkeypatch):
    """SMTP_HOST 为纯空白 → 视为未配置,不连 SMTP(bool(' ') 的坑)"""
    monkeypatch.setattr(settings, "SMTP_HOST", "   ", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "主题", "正文")

    assert _FakeSMTP.calls == []  # 未进入真实发信


def test_host_is_trimmed_before_connect(monkeypatch):
    """SMTP_HOST 两端带空白时,连接用 trim 后的 host"""
    monkeypatch.setattr(settings, "SMTP_HOST", "  smtp.example.com  ", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "none", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "主题", "正文")

    assert _FakeSMTP.init_kwargs["host"] == "smtp.example.com"


def test_from_email_priority_over_username(monkeypatch):
    """SMTP_FROM_EMAIL 非空时优先于 SMTP_USERNAME 作为发件地址"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "none", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "login@example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "noreply@brand.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_FROM_NAME", "SecondLook", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "主题", "正文")

    msg = next(c for c in _FakeSMTP.calls if isinstance(c, tuple) and c[0] == "send_message")[1]
    assert "noreply@brand.com" in msg["From"]
    assert "login@example.com" not in msg["From"]


def test_empty_sender_falls_back_to_noreply_host(monkeypatch):
    """免认证 relay(FROM_EMAIL/USERNAME 都空)时 From 回落 noreply@<host>,
    不能是畸形的 `SecondLook <>`"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "none", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "", raising=False)
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "", raising=False)
    monkeypatch.setattr(settings, "SMTP_FROM_NAME", "SecondLook", raising=False)
    _patch_smtp(monkeypatch)

    email_service.send_email("user@example.com", "主题", "正文")

    msg = next(c for c in _FakeSMTP.calls if isinstance(c, tuple) and c[0] == "send_message")[1]
    assert "noreply@smtp.example.com" in msg["From"]
    assert "<>" not in msg["From"]


def test_login_failure_is_swallowed(monkeypatch):
    """认证失败(login 抛错)时 send_email 不向上抛"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "starttls", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "u@example.com", raising=False)

    class _LoginBoom(_FakeSMTP):
        def login(self, user, password):
            raise email_service.smtplib.SMTPAuthenticationError(535, b"auth failed")

    monkeypatch.setattr(email_service.smtplib, "SMTP", _LoginBoom)
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", _LoginBoom)

    email_service.send_email("user@example.com", "主题", "正文")


def test_starttls_failure_is_swallowed(monkeypatch):
    """STARTTLS 升级失败时 send_email 不向上抛"""
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "SMTP_SECURITY", "starttls", raising=False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "", raising=False)

    class _StarttlsBoom(_FakeSMTP):
        def starttls(self, context=None):
            raise OSError("starttls exploded")

    monkeypatch.setattr(email_service.smtplib, "SMTP", _StarttlsBoom)
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", _StarttlsBoom)

    email_service.send_email("user@example.com", "主题", "正文")
