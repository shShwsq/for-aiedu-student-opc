"""Git Provider 统一抽象层

把不同 Git 托管平台(GitHub / Gitee)的 OAuth 与 API 差异收敛到统一的
`GitProvider` 接口背后,上层路由 / 克隆工具只面对 provider id 与统一返回结构,
不再硬编码任一平台的 URL、scope 或 token 注入格式。

两套流程(沿用原 GitHub 设计):
- 登录流程(scope 仅用户信息):拿 provider_user_id + email,用于登录/创建账号
- 绑定流程(scope 额外含仓库):额外拿 access_token 落库,用于克隆私有仓库

平台差异要点:
- GitHub:token 注入 `https://x-access-token:{token}@github.com/...`;有
  /user/emails(verified primary)端点,支持邮箱同步。
- Gitee:token 注入 `https://oauth2:{token}@gitee.com/...`(用户名必须为字面量
  oauth2);无 verified-emails 端点,不支持邮箱同步;repos 接口无 clone_url,
  需由 full_name 构造。

错误分类(路由层据此定状态码,见 app/git_errors.py):
- GitProviderError:平台明确拒绝(授权码失效、凭证/scope 错、字段缺失)→ 400,
  重试不会改变结果。
- GitProviderUnavailable(子类):链路/平台故障(连接被重置、超时、5xx、
  响应非 JSON)→ 502/504,提示"稍后重试";把这类算成 400 会把用户
  引去改 .env(实测踩过)。
所有平台调用统一走 request_json:按"重复发送是否安全"决定是否退避重试。
"""
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.log_redaction import redact_secrets

logger = logging.getLogger(__name__)


class GitProviderError(Exception):
    """Git provider OAuth / API 错误(平台侧业务错误:授权码失效、4xx、缺字段)"""


class GitProviderUnavailable(GitProviderError):
    """平台不可达 / 无响应(连接被重置、超时、平台 5xx、响应体损坏)

    与「授权码无效」这类真正的 OAuth 参数错误分开:前者是**基础设施故障**,
    重试或稍后再试有可能成功,和用户的授权码、.env 配置都无关。混在
    GitProviderError 里会被映射成 400,把用户引去改配置(实测踩过这个坑),
    所以路由层据本类返回 502/504。

    timeout=True 表示读超时(请求可能已送达,结果未知);
    False 表示连接层失败 / 平台 5xx。
    """

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


@dataclass
class ProviderUserInfo:
    """统一用户信息(各平台 /user 接口归一化后)"""

    provider_user_id: str  # 平台用户 ID(GitHub id / Gitee id)
    email: str | None  # 可能为 None(邮箱私密且平台无可验证邮箱端点)
    login: str | None  # 用户名(如 octocat),一般有值
    name: str | None  # 显示名(很多人不填,可能为 None)
    avatar_url: str | None


@dataclass
class OAuthTokenSet:
    """OAuth 换 token 的完整结果

    - GitHub:token 默认不过期,无 refresh_token → refresh_token=None, expires_in=None
    - Gitee:access_token 有有效期(expires_in 秒),refresh_token 用于续期;
      刷新接口会旋转 refresh_token,必须用响应里的新值覆盖。
    """

    access_token: str
    refresh_token: str | None = None  # None=平台不支持刷新(GitHub)
    expires_in: int | None = None  # 秒;None=不过期(GitHub)或响应未带


# ============================================================
# 平台 HTTP 调用:传输层退避重试 + 错误分类
# ============================================================

# 退避口径与 llm 429 重试一致:base * 2^attempt 封顶 _BACKOFF_MAX,±25% 抖动
# (抖动避免多任务同时重试再次撞同一个坏链路)
_BACKOFF_BASE = 0.5
_BACKOFF_MAX = 4.0

# 单次平台调用的总时间预算(含退避与最后一次尝试的超时)。
# 取 24s 的依据:前端 axios 全局超时 30s,超过它就只会显示"请求超时",
# 用户看不到我们给出的 502/504 detail;绑定/登录一次要串两个平台调用,
# 更要留出余量。因此只在"再试一次仍可能落在预算内"时才重试 ——
# 快速失败(连接被重置,毫秒级)可以重试满,慢超时(10s/15s)只补一次或不补。
_RETRY_BUDGET_SECONDS = 24.0


def _open_client(timeout: float) -> httpx.Client:
    """单独成函数便于测试替换(不在 httpx 模块层面打补丁)

    GIT_OAUTH_PROXY 只给 Git 平台调用加代理:实测 github.com 的 TLS 握手会
    被链路卡住(10s 无 ServerHello),而同一进程里 Gitee 200ms 就走完 —— 这是
    域级链路问题,挂全局 HTTPS_PROXY 会连带改变 LLM / 沙箱 / ACP 的出站路径,
    风险远大于收益。显式配了代理时关掉 trust_env,避免系统 HTTP_PROXY 与
    NO_PROXY 把这份配置悄悄顶掉。
    """
    proxy = (settings.GIT_OAUTH_PROXY or "").strip()
    if proxy:
        return httpx.Client(timeout=timeout, proxy=proxy, trust_env=False)
    return httpx.Client(timeout=timeout)


def _backoff_seconds(attempt: int) -> float:
    return min(_BACKOFF_BASE * (2**attempt), _BACKOFF_MAX) * (
        0.75 + random.random() * 0.5
    )


def _parse_json(response: httpx.Response, label: str) -> Any:
    """解析响应体

    平台返回 2xx 但正文不是 JSON(网关/代理的错误页等)时,原来会让
    json.JSONDecodeError 直接冒到路由 → 500「请求失败(500)」;
    这里归成平台不可用,让上层给出可重试的 502。
    """
    try:
        return response.json()
    except ValueError as e:
        raise GitProviderUnavailable(
            f"{label}: 平台响应不是合法 JSON({str(response.text)[:120]})",
            timeout=False,
        ) from e


# 4xx 正文里透出给用户的最大长度:平台错误页可能是整段 HTML,截断后再进 detail
_MAX_REASON_CHARS = 200

# 平台错误体里表示「原因」的字段(Gitee/GitHub 命名不统一,按信息量从高到低取第一个非空)
_REASON_KEYS = ("error_description", "message", "error", "description")


def _truncate(text: str) -> str:
    """折叠空白 + 截断,避免把整段 HTML 错误页塞进一行日志/前端 detail"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _MAX_REASON_CHARS:
        return collapsed
    return collapsed[:_MAX_REASON_CHARS] + "…"


def _extract_error_reason(response: httpx.Response) -> str:
    """从 4xx 响应体里抽出平台给出的原因,拿不到则返回空串

    Gitee 的 /oauth/token 把「回调地址不匹配」「授权码已被用过」「Client Secret
    不对」收敛成同一个 401,区分信息只在正文里(error_description / message 字段)。
    丢掉正文就等于把三种成因完全不同的故障报成同一句话 —— 实测这次 401 只能靠
    curl 手工复现才知道原因。
    """
    body = (response.text or "").strip()
    if not body:
        return ""
    try:
        data = response.json()
    except ValueError:
        # 网关/代理返回的错误页:剥掉标签的截断正文仍比只报状态码有用
        return _truncate(re.sub(r"<[^>]+>", " ", body))
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            # 少数端点把原因嵌在 error 对象里
            nested = err.get("message") or err.get("code")
            if nested:
                return _truncate(str(nested))
        for key in _REASON_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate(value)
        # 结构不认识:退化为截断后的原文,至少比「什么都没有」更接近根因
    return _truncate(body)


def _describe_4xx(response: httpx.Response) -> str:
    """4xx 的可读描述:沿用 raise_for_status 的口径,再补上平台正文里的原因

    正文与 URL 都过一遍 redact_secrets:平台可能把请求参数原样回显,而 Gitee 的
    revoke URL 把 access_token 放在**路径**上,原样透出到前端 detail 不安全
    (见 app/log_redaction.py)。

    raise_for_status 的原文尾部是 "\nFor more information check:
    https://developer.mozilla.org/...",对排查无用还会把多行文本塞进前端 detail,
    这里只留第一行。
    """
    try:
        response.raise_for_status()
        base = f"HTTP {response.status_code}"  # 理论上到不了(4xx 必抛)
    except httpx.HTTPStatusError as e:
        base = str(e).split("\nFor more information check", 1)[0].strip()
    reason = _extract_error_reason(response)
    return redact_secrets(f"{base}(平台返回:{reason})" if reason else base)


def request_json(
    method: str,
    url: str,
    *,
    label: str,
    idempotent: bool,
    timeout: float = 10.0,
    ok_statuses: tuple[int, ...] = (),
    expect_json: bool = True,
    **kwargs,
) -> tuple[int, Any]:
    """调用 Git 平台接口,返回 (status_code, 解析后的 JSON)

    label:失败信息前缀(如 "换取 access_token 失败"),沿用既有文案。
    ok_statuses:视为成功的额外状态码(revoke 类接口用,如 404=已撤销)。
    expect_json:是否需要解析响应体。revoke 类接口不看正文,置 False 后
      平台回一句纯文本(如 404 "no such token")也不会被误判成"响应损坏"。

    是否重试取决于 **重复发送该请求是否安全**(idempotent):
    - 幂等(GET 用户信息/仓库列表、DELETE 撤销 token):连接失败、读超时、
      平台 5xx 都退避重试,最多 settings.GIT_OAUTH_MAX_RETRIES 次。
      本机实测到 github.com 偶发 TLS 握手重置、RTT 2-5s,一次抖动就报失败
      等于把可恢复的网络问题变成用户可见错误。
    - 不幂等(授权码换 token、refresh_token 刷新):授权码单次有效、
      refresh_token 每次被旋转。响应丢失后再发一次,平台只会回
      "code incorrect or expired",把网络问题伪装成"码失效",所以只在
      **请求尚未发出**的 ConnectError 上重试,其余传输层失败直接上报。
    - 4xx 一律不重试:授权码错/凭证错/权限不足,重试不会改变结果。

    错误分类:连接失败/超时/5xx/非 JSON → GitProviderUnavailable(502/504);
    平台 4xx 与字段缺失 → GitProviderError(400)。
    """
    max_retries = max(0, settings.GIT_OAUTH_MAX_RETRIES)
    started = time.monotonic()
    for attempt in range(max_retries + 1):
        failure: Exception | None = None
        is_timeout = False
        can_retry = False
        response: httpx.Response | None = None
        try:
            with _open_client(timeout) as client:
                response = client.request(method, url, **kwargs)
        except httpx.ConnectTimeout as e:
            # 建连阶段就超时(ConnectTimeout 同时是 TimeoutException 子类,
            # 必须先捕):请求未发出,授权码还有效,重试安全
            failure, is_timeout, can_retry = e, True, True
        except httpx.ConnectError as e:
            # 握手/连接阶段失败:请求未发出,重复发送安全
            failure, can_retry = e, True
        except httpx.TimeoutException as e:
            # 请求可能已发出并被平台处理,只是响应没回来
            failure, is_timeout, can_retry = e, True, idempotent
        except httpx.TransportError as e:
            # 读写中断、协议错、代理错:同样可能已把请求发出去
            failure, can_retry = e, idempotent

        if failure is not None:
            delay = _backoff_seconds(attempt)
            # 只有"退避 + 再来一次完整超时"仍在预算内才重试(见 _RETRY_BUDGET_SECONDS)
            within_budget = (
                time.monotonic() - started + delay + timeout <= _RETRY_BUDGET_SECONDS
            )
            if can_retry and attempt < max_retries and within_budget:
                logger.warning(
                    "%s: %s(%s),%.1fs 后重试(%d/%d)",
                    label, type(failure).__name__, failure, delay,
                    attempt + 1, max_retries,
                )
                time.sleep(delay)
                continue
            raise GitProviderUnavailable(
                f"{label}: {failure}", timeout=is_timeout
            ) from failure

        assert response is not None
        code = response.status_code
        if code >= 500 and code not in ok_statuses:
            # 平台侧故障:幂等请求重试,耗尽后归到"不可用"而非"参数错误"
            failure = httpx.HTTPStatusError(
                f"Server error '{code}' for url '{url}'",
                request=response.request,
                response=response,
            )
            delay = _backoff_seconds(attempt)
            within_budget = (
                time.monotonic() - started + delay + timeout <= _RETRY_BUDGET_SECONDS
            )
            if idempotent and attempt < max_retries and within_budget:
                logger.warning(
                    "%s: 平台 %s,%.1fs 后重试(%d/%d)",
                    label, code, delay, attempt + 1, max_retries,
                )
                time.sleep(delay)
                continue
            raise GitProviderUnavailable(
                f"{label}: {failure}", timeout=False
            ) from failure
        if 400 <= code < 500 and code not in ok_statuses:
            # 平台明确拒绝:授权码/凭证/scope 问题,重试不会改变结果。
            # 正文里的原因要带出来 —— Gitee 把「回调地址不匹配 / 授权码已用过 /
            # 凭证不对」全收敛成同一个 401,只报状态码等于没报(见 _describe_4xx)
            raise GitProviderError(f"{label}: {_describe_4xx(response)}")
        if response.content and expect_json:
            return code, _parse_json(response, label)
        return code, None


class GitProvider(ABC):
    """Git 托管平台抽象基类

    子类需实现平台相关的 OAuth 换 token、用户信息、仓库列表等。
    URL 转换(to_ssh_url / to_https_url / inject_token_in_https)基于 `host`
    与 `token_username` 做通用实现,子类只需声明这两个属性。
    """

    id: str  # "github" / "gitee"
    display_name: str  # "GitHub" / "Gitee"(前端展示)
    host: str  # "github.com" / "gitee.com"
    token_username: str  # HTTPS 克隆鉴权的用户名部分
    authorize_url: str  # OAuth 授权页 URL(前端拼接用,后端仅做元信息)
    token_url: str  # OAuth 换 token 端点
    user_url: str  # /user 接口
    repos_url: str  # /user/repos 接口
    scope_login: str  # 登录用 scope
    scope_bind: str  # 绑定用 scope(含仓库访问)

    # ---- OAuth / API(子类实现) ----

    @abstractmethod
    def exchange_code_for_token(self, code: str) -> OAuthTokenSet:
        """用授权码换 token,失败抛 GitProviderError

        返回 OAuthTokenSet(access_token + refresh_token + expires_in)。
        不支持刷新的平台(GitHub)refresh_token / expires_in 为 None。
        """

    @abstractmethod
    def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """用 refresh_token 换新的 access_token,失败抛 GitProviderError

        不支持刷新的平台(GitHub)直接抛 GitProviderError。
        支持的平台(Gitee)返回新的 OAuthTokenSet(refresh_token 可能被旋转)。
        """

    @abstractmethod
    def get_user_info(self, access_token: str) -> ProviderUserInfo:
        """用 access_token 调 /user 拿用户信息,失败抛 GitProviderError"""

    @abstractmethod
    def get_user_emails(self, access_token: str) -> list[str]:
        """拿可用的(可验证)邮箱列表,无则返回空"""

    @abstractmethod
    def list_repos(self, access_token: str) -> list[dict]:
        """列出当前用户仓库(含私有),返回统一字段:
        [{ "full_name", "name", "private", "html_url", "clone_url", "default_branch" }, ...]
        """

    @abstractmethod
    def revoke_token(self, access_token: str) -> None:
        """在平台端撤销 access_token,使其立即失效

        用于解绑场景:仅清本地 token 不够,平台端旧 token 仍有效会导致
        重新绑定时 OAuth 授权状态粘性(平台跳过授权页 → 换 token 失败)。
        失败应抛 GitProviderError,由上层捕获并降级(不阻塞解绑)。
        """

    @property
    @abstractmethod
    def supports_verified_email(self) -> bool:
        """是否支持可验证邮箱(决定能否做邮箱同步)"""

    @property
    def supports_token_refresh(self) -> bool:
        """是否支持用 refresh_token 续期 access_token

        默认 False(GitHub token 不过期,无需刷新);Gitee 重写为 True。
        """
        return False

    # ---- 通用流程 ----

    def oauth_login(self, code: str) -> ProviderUserInfo:
        """完整 OAuth 登录流程:code → access_token → /user,并补充邮箱

        失败抛 GitProviderError。登录流程不落库 token,仅用 access_token 拉用户信息。
        """
        token_set = self.exchange_code_for_token(code)
        info = self.get_user_info(token_set.access_token)

        # 若公开 email 为空,尝试拿可验证邮箱
        if not info.email:
            emails = self.get_user_emails(token_set.access_token)
            if emails:
                info.email = emails[0]

        return info

    # ---- URL 转换(基于 host / token_username 的通用实现) ----

    def to_ssh_url(self, repo_url: str) -> str:
        """把 HTTPS URL 转成 SSH URL(已是 SSH 则原样返回)"""
        if repo_url.startswith("git@"):
            return repo_url
        m = re.match(
            rf"^https?://{re.escape(self.host)}/(.+?)(?:\.git)?/?$", repo_url
        )
        if m:
            return f"git@{self.host}:{m.group(1)}.git"
        return repo_url

    def to_https_url(self, repo_url: str) -> str:
        """把 SSH URL 转成 HTTPS URL(已是 HTTPS 则原样返回)"""
        m = re.match(rf"^git@{re.escape(self.host)}:(.+?)(?:\.git)?$", repo_url)
        if m:
            return f"https://{self.host}/{m.group(1)}.git"
        return repo_url

    def inject_token_in_https(self, https_url: str, token: str) -> str:
        """把 HTTPS URL 注入 access_token,形成带认证的 clone URL

        GitHub: https://x-access-token:{token}@github.com/owner/repo.git
        Gitee:  https://oauth2:{token}@gitee.com/owner/repo.git

        若 URL 非本平台 HTTPS 或已含认证信息,原样返回。
        """
        if not token or not https_url.startswith(f"https://{self.host}/"):
            return https_url
        # 已含认证信息(user:pass@),不重复注入
        authority = https_url.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority:
            return https_url
        return https_url.replace(
            "https://",
            f"https://{self.token_username}:{token}@",
            1,
        )


# ============================================================
# GitHub
# ============================================================


class GitHubProvider(GitProvider):
    id = "github"
    display_name = "GitHub"
    host = "github.com"
    token_username = "x-access-token"
    authorize_url = "https://github.com/login/oauth/authorize"
    token_url = "https://github.com/login/oauth/access_token"
    user_url = "https://api.github.com/user"
    repos_url = "https://api.github.com/user/repos"
    scope_login = "user:email"
    scope_bind = "user:email repo"

    @property
    def supports_verified_email(self) -> bool:
        return True

    def exchange_code_for_token(self, code: str) -> OAuthTokenSet:
        if not settings.GITHUB_OAUTH_CLIENT_ID or not settings.GITHUB_OAUTH_CLIENT_SECRET:
            raise GitProviderError(
                "GitHub OAuth 未配置:请在 .env 设置 GITHUB_OAUTH_CLIENT_ID 和 GITHUB_OAUTH_CLIENT_SECRET"
            )
        payload = {
            "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
            "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
        }
        headers = {"Accept": "application/json"}
        # idempotent=False:授权码单次有效,响应丢失后重发只会得到"码已失效"
        _, data = request_json(
            "POST",
            self.token_url,
            label="换取 access_token 失败",
            idempotent=False,
            data=payload,
            headers=headers,
        )
        data = data or {}
        access_token = data.get("access_token")
        if not access_token:
            err = data.get("error_description") or data.get("error") or "未知错误"
            raise GitProviderError(f"换取 access_token 失败: {err}")
        # GitHub OAuth token 默认不过期,无 refresh_token
        return OAuthTokenSet(access_token=access_token)

    def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """GitHub token 不支持刷新(默认不过期,无需续期)"""
        raise GitProviderError("GitHub token 不支持刷新")

    def get_user_info(self, access_token: str) -> ProviderUserInfo:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        _, data = request_json(
            "GET",
            self.user_url,
            label="获取用户信息失败",
            idempotent=True,
            headers=headers,
        )
        data = data or {}
        provider_user_id = str(data.get("id") or "")
        if not provider_user_id:
            raise GitProviderError("GitHub 用户信息缺少 id 字段")
        return ProviderUserInfo(
            provider_user_id=provider_user_id,
            email=data.get("email"),
            login=data.get("login"),
            name=data.get("name"),
            avatar_url=data.get("avatar_url"),
        )

    def get_user_emails(self, access_token: str) -> list[str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        try:
            _, items = request_json(
                "GET",
                "https://api.github.com/user/emails",
                label="获取用户邮箱失败",
                idempotent=True,
                headers=headers,
            )
        except GitProviderError:
            # 邮箱只用于「不一致提示」,拿不到就跳过,不影响绑定/登录结果
            return []

        emails = []
        for item in items or []:
            if item.get("primary") and item.get("verified"):
                emails.append(item.get("email", ""))
        return [e for e in emails if e]

    def list_repos(self, access_token: str) -> list[dict]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        params = {
            "affiliation": "owner,collaborator",
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
        }
        _, items = request_json(
            "GET",
            self.repos_url,
            label="列出 GitHub 仓库失败",
            idempotent=True,
            timeout=15.0,
            headers=headers,
            params=params,
        )

        repos = []
        for item in items or []:
            repos.append({
                # 用 `or` 兜底:GitHub 空仓库 default_branch 也为 null,
                # dict.get 的 None 陷阱会导致下游 GitRepoItem Pydantic 校验失败 → 500
                "full_name": item.get("full_name") or "",
                "name": item.get("name") or "",
                "private": bool(item.get("private", False)),
                "html_url": item.get("html_url") or "",
                "clone_url": item.get("clone_url") or "",
                "default_branch": item.get("default_branch") or "main",
            })
        return repos

    def revoke_token(self, access_token: str) -> None:
        """撤销 GitHub access_token(DELETE /applications/{client_id}/token)

        用 client_id + client_secret 做 Basic Auth 鉴权(非用户 token 鉴权),
        撤销后该 token 立即失效,且会清除用户对该 OAuth App 的授权状态,
        下次绑定时 GitHub 会重新显示授权页(避免授权状态粘性导致换 token 失败)。

        参考:https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-token
        """
        if not settings.GITHUB_OAUTH_CLIENT_ID or not settings.GITHUB_OAUTH_CLIENT_SECRET:
            raise GitProviderError("GitHub OAuth 未配置,无法 revoke token")

        url = f"https://api.github.com/applications/{settings.GITHUB_OAUTH_CLIENT_ID}/token"
        # GitHub 成功返回 204 No Content;404 表示 token 已不存在(视为已撤销)
        # 撤销效果幂等(重复撤销同一 token 结果一致),传输层失败可安全重试
        request_json(
            "DELETE",
            url,
            label="撤销 GitHub token 失败",
            idempotent=True,
            ok_statuses=(204, 404),
            expect_json=False,
            auth=(settings.GITHUB_OAUTH_CLIENT_ID, settings.GITHUB_OAUTH_CLIENT_SECRET),
            headers={"Accept": "application/vnd.github+json"},
            json={"access_token": access_token},
        )


# ============================================================
# Gitee
# ============================================================


class GiteeProvider(GitProvider):
    id = "gitee"
    display_name = "Gitee"
    host = "gitee.com"
    token_username = "oauth2"  # Gitee HTTPS 克隆鉴权用户名必须为字面量 oauth2
    authorize_url = "https://gitee.com/oauth/authorize"
    token_url = "https://gitee.com/oauth/token"
    user_url = "https://gitee.com/api/v5/user"
    repos_url = "https://gitee.com/api/v5/user/repos"
    scope_login = "user_info"
    scope_bind = "user_info projects"  # 克隆私有仓库需 projects

    @property
    def supports_verified_email(self) -> bool:
        return False  # Gitee 无 GitHub 式 verified-emails 端点,不支持邮箱同步

    @property
    def supports_token_refresh(self) -> bool:
        return True  # Gitee access_token 有有效期,支持用 refresh_token 续期

    def exchange_code_for_token(self, code: str) -> OAuthTokenSet:
        if not settings.GITEE_OAUTH_CLIENT_ID or not settings.GITEE_OAUTH_CLIENT_SECRET:
            raise GitProviderError(
                "Gitee OAuth 未配置:请在 .env 设置 GITEE_OAUTH_CLIENT_ID 和 GITEE_OAUTH_CLIENT_SECRET"
            )
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.GITEE_OAUTH_CLIENT_ID,
            "client_secret": settings.GITEE_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.GITEE_OAUTH_REDIRECT_URI,
        }
        headers = {"Accept": "application/json"}
        # idempotent=False:授权码单次有效,响应丢失后重发只会得到"码已失效"
        _, data = request_json(
            "POST",
            self.token_url,
            label="换取 access_token 失败",
            idempotent=False,
            data=payload,
            headers=headers,
        )
        data = data or {}
        access_token = data.get("access_token")
        if not access_token:
            err = data.get("error_description") or data.get("error") or "未知错误"
            raise GitProviderError(f"换取 access_token 失败: {err}")
        # Gitee 返回 refresh_token + expires_in,用于续期
        refresh_token = data.get("refresh_token") or None
        expires_in = data.get("expires_in")
        try:
            expires_in = int(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires_in = None
        return OAuthTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )

    def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        """用 refresh_token 换新的 access_token

        Gitee 刷新接口会旋转 refresh_token,必须用响应里的新值覆盖落库。
        """
        if not settings.GITEE_OAUTH_CLIENT_ID or not settings.GITEE_OAUTH_CLIENT_SECRET:
            raise GitProviderError(
                "Gitee OAuth 未配置:请在 .env 设置 GITEE_OAUTH_CLIENT_ID 和 GITEE_OAUTH_CLIENT_SECRET"
            )
        # 应用凭证必须一起带上:Gitee 的 /oauth/token 在 grant_type=refresh_token 时
        # 同样校验 client_id/client_secret,只传 refresh_token 会被回 401
        # (与换 token 同一个端点,报错文案笼统,看不出是缺参数)
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.GITEE_OAUTH_CLIENT_ID,
            "client_secret": settings.GITEE_OAUTH_CLIENT_SECRET,
        }
        headers = {"Accept": "application/json"}
        # idempotent=False:刷新会旋转 refresh_token,响应丢失后重发用的还是旧
        # refresh_token,平台侧可能已轮换 → 只会得到"刷新失败"的误导结论
        _, data = request_json(
            "POST",
            self.token_url,
            label="刷新 access_token 失败",
            idempotent=False,
            data=payload,
            headers=headers,
        )
        data = data or {}
        new_access_token = data.get("access_token")
        if not new_access_token:
            err = data.get("error_description") or data.get("error") or "未知错误"
            raise GitProviderError(f"刷新 access_token 失败: {err}")
        new_refresh_token = data.get("refresh_token") or None
        expires_in = data.get("expires_in")
        try:
            expires_in = int(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires_in = None
        return OAuthTokenSet(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            expires_in=expires_in,
        )

    def get_user_info(self, access_token: str) -> ProviderUserInfo:
        _, data = request_json(
            "GET",
            self.user_url,
            label="获取用户信息失败",
            idempotent=True,
            params={"access_token": access_token},
        )
        data = data or {}
        provider_user_id = str(data.get("id") or "")
        if not provider_user_id:
            raise GitProviderError("Gitee 用户信息缺少 id 字段")
        # Gitee email 可能为空字符串,统一成 None
        email = data.get("email") or None
        return ProviderUserInfo(
            provider_user_id=provider_user_id,
            email=email,
            login=data.get("login"),
            name=data.get("name"),
            avatar_url=data.get("avatar_url"),
        )

    def get_user_emails(self, access_token: str) -> list[str]:
        """Gitee 无独立的可验证邮箱端点,/user 已返回 email,这里不再二次拉取"""
        return []

    def list_repos(self, access_token: str) -> list[dict]:
        params = {
            "access_token": access_token,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
        }
        _, items = request_json(
            "GET",
            self.repos_url,
            label="列出 Gitee 仓库失败",
            idempotent=True,
            timeout=15.0,
            params=params,
        )

        repos = []
        for item in items or []:
            full_name = item.get("full_name", "")
            # Gitee repos 接口无 clone_url,由 full_name 构造 HTTPS 克隆地址
            clone_url = f"https://{self.host}/{full_name}.git" if full_name else ""
            repos.append({
                # 用 `or` 兜底:Gitee 空仓库 default_branch 为 null,
                # dict.get(key, default) 在 key 存在但值为 None 时返回 None 而非 default,
                # 会导致下游 GitRepoItem(default_branch: str) Pydantic 校验失败 → 500
                "full_name": full_name or "",
                "name": item.get("name") or "",
                "private": bool(item.get("private", False)),
                "html_url": item.get("html_url") or "",
                "clone_url": clone_url or "",
                "default_branch": item.get("default_branch") or "master",
            })
        return repos

    def revoke_token(self, access_token: str) -> None:
        """撤销 Gitee access_token(DELETE /applications/{client_id}/tokens/{token})

        用 client_secret 作为 query 参数鉴权。撤销后 token 立即失效,
        用户对该 OAuth App 的授权状态也会清除,下次绑定时会重新显示授权页。

        参考:https://gitee.com/api/v5/swagger#/deleteApplicationToken
        """
        if not settings.GITEE_OAUTH_CLIENT_ID or not settings.GITEE_OAUTH_CLIENT_SECRET:
            raise GitProviderError("Gitee OAuth 未配置,无法 revoke token")

        url = (
            f"https://gitee.com/api/v5/applications/"
            f"{settings.GITEE_OAUTH_CLIENT_ID}/tokens/{access_token}"
        )
        # Gitee 成功返回 200/204;404 表示 token 已不存在(视为已撤销)
        # 撤销效果幂等,传输层失败可安全重试
        request_json(
            "DELETE",
            url,
            label="撤销 Gitee token 失败",
            idempotent=True,
            ok_statuses=(200, 204, 404),
            expect_json=False,
            params={"client_secret": settings.GITEE_OAUTH_CLIENT_SECRET},
        )


# ============================================================
# 注册表
# ============================================================


PROVIDERS: dict[str, GitProvider] = {
    "github": GitHubProvider(),
    "gitee": GiteeProvider(),
}


def get_provider(provider_id: str) -> GitProvider:
    """按 id 取 provider,未知 id 抛 GitProviderError"""
    p = PROVIDERS.get(provider_id)
    if p is None:
        raise GitProviderError(f"未知的 git provider: {provider_id}")
    return p


def get_provider_for_url(repo_url: str) -> GitProvider | None:
    """按 repo_url 的主机识别 provider,未知主机返回 None(走匿名/SSH 回退)"""
    for p in PROVIDERS.values():
        if f"://{p.host}/" in repo_url or f"@{p.host}:" in repo_url:
            return p
    return None
