"""Git 平台 HTTP 层加固回归测试(不连外网、不连 DB)

三条都来自实测:
1. 本机到 github.com 的 TLS 握手会被链路卡住(10s 无 ServerHello → 504),而同一
   进程里 Gitee 200ms 就走完 —— 域级链路问题,需要只给 Git 调用配代理。挂全局
   HTTPS_PROXY 会连带改变 LLM / 沙箱 / ACP 的出站路径,风险更大。
2. Gitee 的 /oauth/token 在 grant_type=refresh_token 时同样校验应用凭证,
   只传 refresh_token 会被回 401(报错文案笼统,看不出是缺参数)。
3. 换 token 要把平台算好的 redirect_uri 原样带上,与授权时不一致同样是 401。
"""
import httpx
import pytest

import app.git_provider as gp
from app.git_provider import GiteeProvider, OAuthTokenSet


# ============================================================
# GIT_OAUTH_PROXY:只作用于 Git 平台调用
# ============================================================


class _ClientSpy:
    """记录 httpx.Client 的构造参数,不真的建连接"""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def close(self):
        pass


@pytest.fixture()
def spy(monkeypatch):
    _ClientSpy.last_kwargs = {}
    monkeypatch.setattr(gp.httpx, "Client", _ClientSpy)
    return _ClientSpy


def test_configured_proxy_is_applied_with_trust_env_off(spy, monkeypatch):
    """显式配了代理:带上 proxy,并关掉 trust_env,免得系统 HTTP_PROXY/NO_PROXY 把它顶掉"""
    monkeypatch.setattr(gp.settings, "GIT_OAUTH_PROXY", "http://127.0.0.1:7890")
    gp._open_client(10.0)
    assert spy.last_kwargs["proxy"] == "http://127.0.0.1:7890"
    assert spy.last_kwargs["trust_env"] is False
    assert spy.last_kwargs["timeout"] == 10.0


def test_blank_proxy_keeps_client_defaults(spy, monkeypatch):
    """未配置代理:不传 proxy/trust_env,沿用 httpx 默认(仍读系统代理环境变量)"""
    monkeypatch.setattr(gp.settings, "GIT_OAUTH_PROXY", "")
    gp._open_client(10.0)
    assert "proxy" not in spy.last_kwargs
    assert "trust_env" not in spy.last_kwargs


def test_proxy_value_is_trimmed(spy, monkeypatch):
    """.env 里手写的值常带空格/换行,不能因为一个尾空格就配不上代理"""
    monkeypatch.setattr(gp.settings, "GIT_OAUTH_PROXY", "  http://127.0.0.1:7890  ")
    gp._open_client(5.0)
    assert spy.last_kwargs["proxy"] == "http://127.0.0.1:7890"


def test_installed_httpx_accepts_this_signature():
    """真客户端能按这个签名构造:防止只在 spy 上通过而线上 TypeError

    `proxy=` 是 httpx 0.26+ 的写法(旧版叫 proxies=),升级时这条会第一时间炸。
    """
    client = httpx.Client(timeout=5.0, proxy="http://127.0.0.1:7890", trust_env=False)
    try:
        assert client.timeout.read == 5.0
    finally:
        client.close()


# ============================================================
# Gitee token 端点:请求体必须带齐平台要求的字段
# ============================================================


class _RecordingClient:
    """记录 request 调用参数,固定返回一份可用的 token 响应"""

    def __init__(self, script: list, calls: list):
        self._script = script
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self._script[min(len(self.calls) - 1, len(self._script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture()
def gitee(monkeypatch):
    """Gitee 凭证就绪 + 平台调用被记录,默认回一份可用的 token 响应

    返回 calls 列表(重试次数设为 0,便于断言「只发了一次」)。
    """
    calls: list = []
    monkeypatch.setattr(gp.settings, "GITEE_OAUTH_CLIENT_ID", "gcid")
    monkeypatch.setattr(gp.settings, "GITEE_OAUTH_CLIENT_SECRET", "gsecret")
    monkeypatch.setattr(gp.settings, "GITEE_OAUTH_REDIRECT_URI", "http://localhost:5173/auth/gitee/callback")
    monkeypatch.setattr(gp.settings, "GIT_OAUTH_MAX_RETRIES", 0)
    monkeypatch.setattr(
        gp,
        "_open_client",
        lambda timeout: _RecordingClient(
            [httpx.Response(
                200,
                json={"access_token": "new-at", "refresh_token": "new-rt",
                      "expires_in": 86400},
                request=httpx.Request("POST", "https://gitee.com/oauth/token"),
            )],
            calls,
        ),
    )
    return calls


def test_gitee_refresh_carries_app_credentials(gitee):
    """刷新必须带 client_id/client_secret:少了 Gitee 回 401(实测同类端点行为)"""
    tokens = GiteeProvider().refresh_access_token("old-rt")

    sent = gitee[0][2]["data"]
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "old-rt"
    assert sent["client_id"] == "gcid"
    assert sent["client_secret"] == "gsecret"
    # 平台旋转后的新值要交回上层落库
    assert tokens == OAuthTokenSet(
        access_token="new-at", refresh_token="new-rt", expires_in=86400
    )


def test_gitee_exchange_carries_configured_redirect_uri(gitee):
    """换 token 的 redirect_uri 必须与授权时一致,否则同样被回 401"""
    GiteeProvider().exchange_code_for_token("fresh-code")

    sent = gitee[0][2]["data"]
    assert sent["redirect_uri"] == "http://localhost:5173/auth/gitee/callback"
    assert sent["client_id"] == "gcid" and sent["client_secret"] == "gsecret"
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "fresh-code"


def test_gitee_refresh_without_credentials_fails_before_http(gitee, monkeypatch):
    """配置缺失在调用平台前就拦下:不能发一个注定 401 的请求"""
    monkeypatch.setattr(gp.settings, "GITEE_OAUTH_CLIENT_SECRET", "")
    with pytest.raises(gp.GitProviderError) as ei:
        GiteeProvider().refresh_access_token("rt")
    assert "Gitee OAuth 未配置" in str(ei.value)
    assert gitee == []
