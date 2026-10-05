"""Git 平台异常的 HTTP 状态映射与服务端日志(供 /auth/oauth/* 与 /git/* 共用)

背景(两条都实测踩过):
1. 路由层把所有 GitProviderError 一律映射成 400,于是「github.com 连不上 /
   换 token 超时」这类链路故障会以 "换取 access_token 失败: ..." 的**参数错误**
   面目出现,把用户引去改 .env;而 400 在访问日志里只有一行状态码,
   事后无从判断原因(路由不记 detail)。
2. 平台 2xx 但响应体不是 JSON 时异常不被捕获 → 500,前端只显示
   "请求失败(500)"(无 detail 字段)。

本模块把「分类 + 状态码 + 日志」收敛到一处:
- GitProviderUnavailable → 504(读超时)/ 502(连接失败、平台 5xx、响应损坏),
  detail 明确提示"稍后重试",并说明与用户的授权码/配置无关
- 其余 GitProviderError → 400(detail 原样透出,文案与改动前一致)
两类都记 warning(带操作名与 user_id),控制台/日志里能直接看到根因。

放在 app 层而非某个 router 里,是因为 /auth 与 /git 两组端点都要用,
避免互相 import 路由模块。
"""
import logging
from typing import Any

from fastapi import HTTPException, status

from app.git_provider import GitProviderError, GitProviderUnavailable

logger = logging.getLogger(__name__)


def http_exc_for_git_error(
    exc: GitProviderError,
    *,
    action: str,
    provider_display: str = "",
    user_id: Any = None,
) -> HTTPException:
    """把 Git 平台异常转成 HTTPException,同时留一条带根因的 warning

    参数:
        exc:捕获到的 GitProviderError / GitProviderUnavailable
        action:操作描述(日志用),如 "绑定 GitHub" / "Gitee OAuth 登录"
        provider_display:平台展示名(文案用),如 "GitHub"
        user_id:当前用户(日志用,未登录路径可传 None)
    """
    detail = str(exc)
    target = provider_display or "Git 平台"

    if isinstance(exc, GitProviderUnavailable):
        code = (
            status.HTTP_504_GATEWAY_TIMEOUT
            if exc.timeout
            else status.HTTP_502_BAD_GATEWAY
        )
        logger.warning(
            "%s 失败(平台不可达 → HTTP %s): user=%s %s",
            action, code, user_id, detail,
        )
        hint = f"{target} 响应超时" if exc.timeout else f"无法连接 {target}"
        return HTTPException(
            status_code=code,
            # 明确"与授权码/配置无关":否则用户会以为要去改 .env 或重新授权
            detail=f"{hint},请稍后重试(与授权码和账号配置无关): {detail}",
        )

    logger.warning("%s 失败(→ HTTP 400): user=%s %s", action, user_id, detail)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
