"""凭证脱敏回归测试(log_redaction.redact_secrets + handler 过滤器)

背景(Gitee 实测):Gitee API v5 把 access_token 放在 URL query 上,httpx 在
INFO 级别会打出完整 URL;生产默认就是 INFO,于是数天有效的 token 长期落在
控制台/日志文件/采集链路里。Gitee 的 revoke 接口更是把 token 放在 URL 路径上。
另外 4xx 的 detail 会透出给用户,平台回显请求参数时同样不能带出凭证。

这里锁定三件事:
- 各种凭证形态(query/form、JSON 字段、URL 路径段、Authorization 头、克隆 URL
  的 userinfo)都被替换成 ***
- 非凭证信息保留(client_id、redirect_uri、error_description、per_page ...),
  否则脱敏会把排查需要的线索一起抹掉
- 幂等 + 过滤器覆盖「格式串 + args」的记法(httpx 就是这么记日志的)
"""
import io
import logging

from app.log_redaction import (
    SecretRedactingFilter,
    install_log_redaction,
    redact_secrets,
)


# ============================================================
# redact_secrets:该遮的遮掉
# ============================================================


def test_redacts_query_param_access_token():
    """实测泄漏的那条 httpx INFO 日志"""
    out = redact_secrets(
        'HTTP Request: GET https://gitee.com/api/v5/user'
        '?access_token=0776b8f28a8abe67b6e380930847875d "HTTP/1.1 200 OK"'
    )
    assert "0776b8f28a8abe67b6e380930847875d" not in out
    assert "access_token=***" in out
    # 状态码与端点信息保留:网络故障排查仍然看得见
    assert "gitee.com/api/v5/user" in out and "200 OK" in out


def test_redacts_each_credential_shaped_param():
    out = redact_secrets(
        "https://x/y?refresh_token=rt_abcdefgh&client_secret=cs_ijklmnop"
        "&password=pw_qrst&token=tk_uvwx&per_page=100"
    )
    for leaked in ("rt_abcdefgh", "cs_ijklmnop", "pw_qrst", "tk_uvwx"):
        assert leaked not in out
    assert "per_page=100" in out  # 非凭证参数不动


def test_redacts_json_field_value():
    out = redact_secrets('{"access_token": "ghu_secretvalue", "login": "octocat"}')
    assert "ghu_secretvalue" not in out
    assert '"login": "octocat"' in out  # 正文里的业务字段保留


def test_redacts_token_in_url_path_for_gitee_revoke():
    """Gitee revoke 把 token 放在路径上,同时 query 里带 client_secret"""
    out = redact_secrets(
        "https://gitee.com/api/v5/applications/giteeCid/tokens/tok_0123456789"
        "?client_secret=sec_0123456789"
    )
    assert "tok_0123456789" not in out and "sec_0123456789" not in out
    assert "/tokens/***" in out
    # client_id 是公开值(前端包里也有),遮了反而挡住排查
    assert "applications/giteeCid" in out


def test_redacts_authorization_header_value():
    assert "gho_secrettoken" not in redact_secrets("Authorization: Bearer gho_secrettoken")
    assert "Basic" in redact_secrets("Authorization: Basic dXNlcjpwYXNz")
    # GitHub 旧式 `Authorization: token <凭证>` 也在认证头语境里
    assert "ghu_leaktoken" not in redact_secrets("Authorization: token ghu_leaktoken")
    # dict 风格的 header 写法同样要命中
    assert "gho_secrettoken" not in redact_secrets("{'Authorization': 'Bearer gho_secrettoken'}")


def test_redacts_token_in_clone_url_userinfo():
    """注入 token 的克隆 URL 会出现在任务/克隆日志里"""
    out = redact_secrets("git clone https://oauth2:tok_abc123@gitee.com/o/r.git")
    assert "tok_abc123" not in out
    assert "https://oauth2:***@gitee.com/o/r.git" in out


def test_keeps_authorization_code_for_double_submit_tracing():
    """授权码单次有效且几分钟作废:保留它才能看出「同一个码提交了两次」"""
    assert "code=dQdpwqHFas" in redact_secrets("/auth/gitee/callback?code=dQdpwqHFas")


def test_keeps_error_description_text():
    """4xx 的原因正文是排查关键,不能被当成凭证吃掉"""
    text = '{"error_description": "授权方式无效，或者登录回调地址无效、过期或已被撤销"}'
    assert "回调地址无效" in redact_secrets(text)


def test_untouched_lines_stay_identical():
    """不误伤常规日志:没有凭证形态时逐字返回"""
    for text in (
        "user 7 刷新 Gitee token 失败: 无 refresh_token",
        "GET /git/gitee/status 200 OK in 12ms",
        "https://host:8080/path 与 user@example.com 同行出现",
        "",
    ):
        assert redact_secrets(text) == text


def test_prose_with_scheme_words_stays_identical():
    """裸词 token/basic/bearer 后面接普通词时不是凭证,逐字保留排障信息

    回归:_AUTH_RE 曾经匹配任意 `token <词>` 组合,把解绑流程的
    "token revoke 失败" 打码成 "token *** 失败",抹掉了失败发生在哪一步。
    """
    for text in (
        "user 7 的 Gitee token revoke 失败(不阻塞解绑): HTTP 403",
        "reset token expired please re-bind",
        "The basic configuration loaded",
        "bearer capability not supported by this model",
    ):
        assert redact_secrets(text) == text


def test_redaction_is_idempotent():
    once = redact_secrets("https://x/y?access_token=abc123def")
    assert redact_secrets(once) == once


# ============================================================
# handler 过滤器:落到日志前的那一道闸
# ============================================================


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """建一个只挂「带脱敏过滤器的 StreamHandler」的独立 logger"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    install_log_redaction(handler)
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_filter_masks_credentials_passed_as_args():
    """httpx 的记法是「格式串 + args」,凭证在 args 里 —— 只处理 record.msg 会整条漏掉"""
    logger, stream = _capture_logger("test.redaction.args")
    logger.info(
        "HTTP Request: %s %s \"%s %s\"",
        "GET", "https://gitee.com/api/v5/user?access_token=leakme123456", "200", "OK",
    )
    out = stream.getvalue()
    assert "leakme123456" not in out
    assert "access_token=***" in out


def test_filter_masks_plain_message():
    logger, stream = _capture_logger("test.redaction.plain")
    logger.info("克隆失败:https://oauth2:leakme999@gitee.com/o/r.git")
    out = stream.getvalue()
    assert "leakme999" not in out and "oauth2:***@" in out


def test_filter_keeps_record_levels_and_does_not_swallow():
    logger, stream = _capture_logger("test.redaction.pass")
    logger.warning("普通告警,不含凭证")
    assert "普通告警" in stream.getvalue()


def test_install_is_not_stacked_on_same_handler():
    """重复安装(如 uvicorn reload / 多次 import)不应叠加过滤器"""
    handler = logging.StreamHandler(io.StringIO())
    install_log_redaction(handler)
    install_log_redaction(handler)
    assert sum(1 for f in handler.filters if isinstance(f, SecretRedactingFilter)) == 1
