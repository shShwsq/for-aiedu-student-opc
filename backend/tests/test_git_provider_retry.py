"""Git 平台调用的传输层重试与错误分类回归测试(不连外网、不连 DB)

背景(两条都是实测踩过的坑):
1. `except httpx.HTTPError` 把「连接被重置 / 读超时 / 平台 5xx」与
   「授权码失效 / 凭证错」混成同一个 GitProviderError,路由一律回 400,
   用户看到 "换取 access_token 失败: ..." 以为是自己 .env 配错了;
   而 400 在访问日志里只剩一行状态码,根因无从查起。
2. 平台 2xx 但响应体不是 JSON 时,`r.json()` 抛的 JSONDecodeError 不被捕获
   → 500,前端只显示 "请求失败(500)"。

本测试锁定 request_json 的重试口径与分类口径:
- 重试与否取决于「重复发送是否安全」:单次有效的授权码换 token 只在请求
  尚未发出的连接失败(ConnectError/ConnectTimeout)上重试;幂等 GET 全重试
- 传输层/5xx/非 JSON → GitProviderUnavailable(路由映射 502/504)
- 平台 4xx 与 200-带-error → GitProviderError(路由映射 400)
"""
import httpx
import pytest
from types import SimpleNamespace

import app.git_provider as gp
from app.git_errors import http_exc_for_git_error
from app.git_provider import (
    GitProviderError,
    GitProviderUnavailable,
    GitHubProvider,
    request_json,
)


# ============================================================
# 辅助:替掉 httpx.Client,按脚本依次抛错/返回响应
# ============================================================


class _FakeClient:
    """记录每次调用;脚本元素为 Exception(抛出)或 httpx.Response(返回)"""

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
def patched(monkeypatch):
    """默认重试 2 次;sleep 只记录不真等;GitHub OAuth 凭证非空(换 token 前置校验)"""
    sleeps: list[float] = []
    calls: list = []
    monkeypatch.setattr(gp.settings, "GIT_OAUTH_MAX_RETRIES", 2)
    monkeypatch.setattr(gp.time, "sleep", sleeps.append)
    monkeypatch.setattr(gp.settings, "GITHUB_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(gp.settings, "GITHUB_OAUTH_CLIENT_SECRET", "secret")

    def install(script: list):
        monkeypatch.setattr(
            gp, "_open_client", lambda timeout: _FakeClient(script, calls)
        )
        return script

    install([])  # 默认空脚本(不会被调用)
    return {"install": install, "calls": calls, "sleeps": sleeps}


def _resp(status: int = 200, body: dict | None = None, text: str | None = None):
    request = httpx.Request("POST", "https://example.test/x")
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, json=body if body is not None else {}, request=request)


def _call(method="GET", idempotent=True, **kw):
    """跑一次 request_json,断言用返回值的便捷包装"""
    return request_json(
        method, "https://example.test/x", label="调用失败", idempotent=idempotent, **kw
    )


# ============================================================
# 重试口径:幂等 GET vs 单次有效的 POST
# ============================================================


def test_connect_error_retried_even_for_single_use_code(patched):
    """连接阶段失败(请求未发出):即便换 token 也重试,授权码还有效"""
    patched["install"]([
        httpx.ConnectError("TLS EOF"),
        httpx.ConnectError("TLS EOF"),
        _resp(200, {"access_token": "ghu_ok"}),
    ])
    code, data = _call("POST", idempotent=False)
    assert code == 200 and data["access_token"] == "ghu_ok"
    assert len(patched["calls"]) == 3
    assert len(patched["sleeps"]) == 2


def test_read_timeout_not_retried_for_authorization_code(patched):
    """读超时不重发换 token:重发只会得到"码已失效",把网络问题伪装成参数错"""
    patched["install"]([httpx.ReadTimeout("timed out")])
    with pytest.raises(GitProviderUnavailable) as ei:
        _call("POST", idempotent=False)
    assert len(patched["calls"]) == 1
    assert patched["sleeps"] == []
    assert ei.value.timeout is True  # 让路由选 504


def test_read_timeout_retried_for_idempotent_get(patched):
    """幂等 GET:读超时重试后成功"""
    patched["install"]([httpx.ReadTimeout("timed out"), _resp(200, {"id": 7})])
    code, data = _call("GET", idempotent=True)
    assert (code, data) == (200, {"id": 7})
    assert len(patched["calls"]) == 2
    assert len(patched["sleeps"]) == 1


def test_platform_5xx_retried_on_get_then_unavailable(patched):
    """幂等 GET 撞平台 5xx:重试到上限后归为"不可用",不是 400"""
    patched["install"]([_resp(502, text="bad gateway")])
    with pytest.raises(GitProviderUnavailable) as ei:
        _call("GET", idempotent=True)
    assert len(patched["calls"]) == 3  # 1 + 2 次重试
    assert ei.value.timeout is False


def test_platform_5xx_not_retried_on_non_idempotent_post(patched):
    """非幂等请求撞 5xx:不重发,直接上报不可用"""
    patched["install"]([_resp(503, text="slow down")])
    with pytest.raises(GitProviderUnavailable):
        _call("POST", idempotent=False)
    assert len(patched["calls"]) == 1


def test_4xx_is_business_error_and_never_retried(patched):
    """平台明确拒绝(4xx):GitProviderError 而非 Unavailable,重试无意义"""
    patched["install"]([_resp(401, text="unauthorized")])
    with pytest.raises(GitProviderError) as ei:
        _call("GET", idempotent=True)
    assert not isinstance(ei.value, GitProviderUnavailable)
    assert len(patched["calls"]) == 1
    assert "401" in str(ei.value)


def test_non_json_2xx_body_is_unavailable_not_crash(patched):
    """2xx 但正文不是 JSON(网关错误页):归 502,不再冒 JSONDecodeError → 500"""
    patched["install"]([_resp(200, text="<html>proxy error</html>")])
    with pytest.raises(GitProviderUnavailable) as ei:
        _call("GET", idempotent=True)
    assert "不是合法 JSON" in str(ei.value)


def test_max_retries_zero_disables_retry(patched, monkeypatch):
    """GIT_OAUTH_MAX_RETRIES=0:一次都不重试(排查时可关掉,行为回到改动前)"""
    monkeypatch.setattr(gp.settings, "GIT_OAUTH_MAX_RETRIES", 0)
    patched["install"]([httpx.ConnectError("down")])
    with pytest.raises(GitProviderUnavailable):
        _call("GET", idempotent=True)
    assert len(patched["calls"]) == 1


def test_backoff_is_bounded_and_grows(patched):
    """退避:指数增长且封顶 _BACKOFF_MAX(带抖动,只断言上下界)"""
    patched["install"]([httpx.ConnectError("down")])
    with pytest.raises(GitProviderUnavailable):
        _call("GET", idempotent=True)
    assert len(patched["sleeps"]) == 2
    assert all(0 < s <= gp._BACKOFF_MAX for s in patched["sleeps"])
    # 封顶前的窗口内后一次应大于前一次(0.5*0.75 < 1.0*1.25 成立)
    assert patched["sleeps"][1] > patched["sleeps"][0]


class _SlowClient:
    """每次尝试都"耗时" secs 秒(推进假时钟)后抛读超时"""

    def __init__(self, clock: dict, secs: float, calls: list):
        self._clock = clock
        self._secs = secs
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        self._clock["t"] += self._secs
        raise httpx.ReadTimeout("timed out")


@pytest.mark.parametrize(
    "attempt_secs,expect_calls",
    [
        # 10s 一次:重试一次后 20+ 秒已逼近预算 → 共 2 次
        (10.0, 2),
        # 15s 一次(仓库列表):再试一次就是 30s+,超过前端 axios 的 30s
        # 超时(用户只会看到"请求超时",拿不到 502/504 的 detail)→ 不重试
        (15.0, 1),
    ],
)
def test_retry_stops_when_budget_exhausted(patched, monkeypatch, attempt_secs, expect_calls):
    """重试受总时间预算约束:慢失败少试,快失败(连接被重置)才重试满"""
    clock = {"t": 0.0}
    sleeps: list[float] = []
    monkeypatch.setattr(gp, "time", SimpleNamespace(
        monotonic=lambda: clock["t"], sleep=sleeps.append
    ))
    calls: list = []
    monkeypatch.setattr(
        gp, "_open_client", lambda timeout: _SlowClient(clock, attempt_secs, calls)
    )

    with pytest.raises(GitProviderUnavailable) as ei:
        request_json(
            "GET",
            "https://example.test/x",
            label="调用失败",
            idempotent=True,
            timeout=attempt_secs,
        )
    assert len(calls) == expect_calls
    assert ei.value.timeout is True


def test_empty_body_returns_none_data(patched):
    """204 无正文:data 为 None(revoke 类接口用,不炸 json())"""
    patched["install"]([httpx.Response(
        204, request=httpx.Request("DELETE", "https://example.test/x")
    )])
    code, data = _call("DELETE", idempotent=True)
    assert (code, data) == (204, None)


def test_revoke_treats_404_as_already_revoked_without_parsing_body(patched):
    """ok_statuses 内的状态码视为成功;revoke 不看正文(404 纯文本也不误判为响应损坏)"""
    patched["install"]([_resp(404, text="no such token")])
    GitHubProvider().revoke_token("tok")  # 不抛异常
    assert len(patched["calls"]) == 1


def test_revoke_403_is_business_error(patched):
    """revoke 撞 403(client_secret 权限不足):归 400 类,由解绑逻辑降级为 warning"""
    patched["install"]([_resp(403, text="forbidden")])
    with pytest.raises(GitProviderError) as ei:
        GitHubProvider().revoke_token("tok")
    assert not isinstance(ei.value, GitProviderUnavailable)


# ============================================================
# provider 方法层:分类不能把真正的 OAuth 错误升格成"不可用"
# ============================================================


def test_github_expired_code_stays_business_error(patched):
    """GitHub 用 200 + error 表达"授权码失效":必须是 400 类,不能被当成网络故障重试"""
    patched["install"]([_resp(200, {
        "error": "bad_verification_code",
        "error_description": "The code passed is incorrect or expired.",
    })])
    with pytest.raises(GitProviderError) as ei:
        GitHubProvider().exchange_code_for_token("stale-code")
    assert not isinstance(ei.value, GitProviderUnavailable)
    assert "incorrect or expired" in str(ei.value)
    assert len(patched["calls"]) == 1  # 非幂等 + 未失败,不重试


def test_github_network_failure_is_unavailable(patched):
    """同一方法的网络故障路径:升格为 Unavailable,路由才能给 502/504"""
    patched["install"]([httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]")])
    with pytest.raises(GitProviderUnavailable) as ei:
        GitHubProvider().exchange_code_for_token("code")
    # ConnectError 属"请求未发出",重试到上限(2 次)后放弃
    assert len(patched["calls"]) == 3
    assert "UNEXPECTED_EOF" in str(ei.value)


# ============================================================
# 4xx detail:平台正文里的原因必须带出来(Gitee 把多种成因收敛成同一个 401)
# ============================================================


def test_4xx_json_body_reason_is_appended(patched):
    """实测:Gitee 的 401 只报状态码等于没报,原因在正文 error_description 里"""
    patched["install"]([_resp(401, {
        "error": "invalid_request",
        "error_description": "授权方式无效，或者登录回调地址无效、过期或已被撤销",
    })])
    with pytest.raises(GitProviderError) as ei:
        _call("POST", idempotent=False)
    msg = str(ei.value)
    assert "401" in msg
    assert "回调地址无效" in msg


def test_4xx_html_body_is_stripped_and_truncated(patched):
    """网关/代理的错误页:剥掉标签后截断进 detail,不把整页 HTML 塞给前端"""
    patched["install"]([_resp(400, text="<html><body>" + "redirect_uri mismatch " * 50 + "</body></html>")])
    with pytest.raises(GitProviderError) as ei:
        _call("POST", idempotent=False)
    msg = str(ei.value)
    assert "redirect_uri mismatch" in msg
    assert "<html>" not in msg
    assert len(msg) < 500


def test_4xx_detail_drops_mdn_boilerplate(patched):
    """raise_for_status 尾部的 MDN 链接对排查无益,还会让 detail 变成多行"""
    patched["install"]([_resp(401, text="unauthorized")])
    with pytest.raises(GitProviderError) as ei:
        _call("POST", idempotent=False)
    assert "developer.mozilla.org" not in str(ei.value)
    assert "\n" not in str(ei.value)


def test_4xx_detail_redacts_credentials_echoed_by_platform(patched):
    """detail 会透出到前端:平台回显请求参数时不能把 client_secret 一起带出去"""
    patched["install"]([_resp(401, {
        "error_description": "client_secret=supersecretvalue 校验不通过",
    })])
    with pytest.raises(GitProviderError) as ei:
        _call("POST", idempotent=False)
    msg = str(ei.value)
    assert "supersecretvalue" not in msg
    assert "client_secret=***" in msg


def test_4xx_empty_body_keeps_status_only(patched):
    """平台 4xx 无正文:不编造原因,只给 raise_for_status 的口径"""
    patched["install"]([httpx.Response(
        404, request=httpx.Request("POST", "https://example.test/x")
    )])
    with pytest.raises(GitProviderError) as ei:
        _call("POST", idempotent=False)
    assert "平台返回" not in str(ei.value)
    assert "404" in str(ei.value)


# ============================================================
# 路由层映射:GitProviderError → 状态码
# ============================================================


def test_unavailable_timeout_maps_to_504():
    exc = http_exc_for_git_error(
        GitProviderUnavailable("换取 access_token 失败: timed out", timeout=True),
        action="绑定 GitHub",
        provider_display="GitHub",
        user_id=1,
    )
    assert exc.status_code == 504
    assert "GitHub 响应超时" in exc.detail
    assert "稍后重试" in exc.detail


def test_unavailable_connect_error_maps_to_502():
    exc = http_exc_for_git_error(
        GitProviderUnavailable("换取 access_token 失败: TLS EOF", timeout=False),
        action="绑定 GitHub",
        provider_display="GitHub",
    )
    assert exc.status_code == 502
    assert "无法连接 GitHub" in exc.detail


def test_business_error_maps_to_400_with_original_detail():
    """400 的 detail 与改动前完全一致(前端文案不变),只是多了服务端 warning"""
    exc = http_exc_for_git_error(
        GitProviderError("换取 access_token 失败: The code passed is incorrect or expired."),
        action="绑定 GitHub",
        provider_display="GitHub",
    )
    assert exc.status_code == 400
    assert exc.detail == (
        "换取 access_token 失败: The code passed is incorrect or expired."
    )
