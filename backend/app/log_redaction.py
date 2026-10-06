"""凭证脱敏:避免平台 token / secret 随日志与错误文案外泄

背景(Gitee 实测泄漏):GitHub 的 /user 用 `Authorization: Bearer` 头,而 Gitee API v5
把 access_token 作为 **query 参数**传(见 `GiteeProvider.get_user_info`),httpx 在
INFO 级别会把完整 URL 打进日志:

    HTTP Request: GET https://gitee.com/api/v5/user?access_token=0776b8f2... "HTTP/1.1 200 OK"

生产默认就是 INFO(`APP_DEBUG=false`),而 Gitee 的 access_token 有效期长达数天,
于是一个长期可用的凭证会长期留在控制台、日志文件和日志采集链路里。Gitee 的 revoke
接口更是把 token 放在 **URL 路径**上(`/applications/{client_id}/tokens/{token}`)。

做法:在 handler 上挂一个 Filter,记录格式化后、落盘前把凭证形态替换成 `***`。
收口在 handler 而不是某个 logger,是因为泄漏点分散在 `httpx` / `httpcore` / `app.*`
多个 logger,一处兜底比逐个约束调用方可靠。

同一套 `redact_secrets` 也用于**透出给用户的错误正文**(`request_json` 的 4xx 分支):
平台可能把请求参数原样回显,不脱敏就会把 client_secret 带进前端 detail。

刻意**不**脱敏的形态:`client_id`(公开值,前端包里也有)、单次有效且几分钟就作废的
授权码 `code`(留着便于排查"同一个码提交了两次")。
"""
import logging
import re

# 键名以这些词结尾即视为凭证(access_token / refresh_token / client_secret / secret /
# token / password ...),允许带任意前缀(`x_token=` 也算),避免只匹配到整词而漏脱敏
_SECRET_KEY_SUFFIXES = (
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "secret",
    "token",
    "password",
    "passwd",
)

# query / form 参数:key=value,value 到分隔符为止
_PARAM_RE = re.compile(
    r"(?i)([A-Za-z0-9_]*(?:%s))=([^&?\s\"'#,;]+)" % "|".join(_SECRET_KEY_SUFFIXES)
)

# JSON 字段:"key": "value"
_JSON_RE = re.compile(
    r'(?i)("[A-Za-z0-9_]*(?:%s)"\s*:\s*")[^"]*(")' % "|".join(_SECRET_KEY_SUFFIXES)
)

# URL 路径段:Gitee revoke 的 /tokens/{access_token}
_PATH_RE = re.compile(r"(?i)(/tokens/)([^/?&#\s]+)")

# 认证头:Authorization: Bearer xxx / Basic xxx / token xxx
# 必须限定在 `Authorization:` 前缀之后:裸词 token/basic/bearer 在正文里太常见,
# 会把 "token revoke 失败""basic configuration" 这类正常日志一起遮掉,抹掉排障线索。
# 前缀与 scheme 之间允许引号,覆盖 {'Authorization': 'Bearer xxx'} 这类 dict 记法
_AUTH_RE = re.compile(
    r"""(?i)(Authorization['\"]?\s*[:=]\s*['\"]?(?:bearer|basic|token)\s+)([^\s,;'\"#]+)"""
)

# 克隆 URL 的 userinfo:https://oauth2:{token}@gitee.com/...
# 两组都排除 / 与空格,把匹配限制在 URL 的 authority 段内 —— 否则
# "https://host:8080/path 提到 user@example.com" 这种同行文本会被误伤
_USERINFO_RE = re.compile(r"(?i)(\bhttps?://[^\s/@:]+:)([^\s/@]+)(@)")


def redact_secrets(text: str) -> str:
    """把文本里各种形态的凭证值替换成 ***

    幂等:已脱敏的 `***` 再跑一次仍是 `***`,所以多个 handler 叠加过滤器不会
    把内容越改越乱。
    """
    if not text:
        return text
    out = _PARAM_RE.sub(r"\1=***", text)
    out = _JSON_RE.sub(r"\1***\2", out)
    out = _PATH_RE.sub(r"\1***", out)
    out = _USERINFO_RE.sub(r"\1***\3", out)
    out = _AUTH_RE.sub(r"\1***", out)
    return out


class SecretRedactingFilter(logging.Filter):
    """记录落盘前脱敏

    必须基于 `record.getMessage()`(把 `%s` 参数先格式进去)而不是只改
    `record.msg`:httpx 的日志是 `"HTTP Request: %s %s ..."` **带参数**记录的,
    凭证在 args 里,只处理 msg 会整条漏掉。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 消息本身格式有误:交回 logging 自行处理,不因脱敏再炸一次
            return True
        if message:
            record.msg = redact_secrets(message)
            record.args = ()
        return True


def install_log_redaction(*extra_handlers: logging.Handler) -> None:
    """给根 logger 的 handler(以及显式传入的 handler)挂脱敏过滤器

    `extra_handlers` 用于不挂根上的独立 handler(如出题滚动日志
    `logs/practice_generate.log`),重复调用不会叠加过滤器。
    """
    filt = SecretRedactingFilter()
    handlers = list(logging.getLogger().handlers) + list(extra_handlers)
    for handler in handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(filt)
