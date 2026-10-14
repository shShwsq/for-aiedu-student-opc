"""邮件服务:验证邮箱 + 重置密码的 token 签发/校验 + 邮件发送抽象

设计要点:
- token 用 secrets.token_urlsafe(32) 生成,库里只存 sha256 哈希
- send_email 根据配置选择投递方式:
  * SMTP_HOST 留空 → 开发模式,仅日志打印邮件内容(不接 SMTP)
  * SMTP_HOST 已配 → 用标准库 smtplib 真实发信(STARTTLS/SSL/明文)
- 切换只需改配置,调用方(register / resend / forgot)无需变动
"""
import hashlib
import logging
import secrets
import smtplib
import ssl
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.orm import Session

from app.config import settings
from app.models.email_token import EmailToken, EmailTokenType
from app.models.user import User

# 独立 logger(不依赖 uvicorn 默认配置),确保开发期看到邮件内容
logger = logging.getLogger("app.email_service")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# token 有效期
VERIFY_EMAIL_TTL = timedelta(hours=24)
RESET_PASSWORD_TTL = timedelta(minutes=30)


# ============================================================
# token 生成与哈希
# ============================================================


def _generate_token() -> str:
    """生成 urlsafe 随机 token(48 字符)"""
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """sha256 哈希(存数据库,防泄露后被重放)"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ============================================================
# 创建 token 记录
# ============================================================


def create_email_verification_token(db: Session, user_id: uuid.UUID) -> str:
    """生成邮箱验证 token,返回原文(发给用户)"""
    return _create_token_record(db, user_id, EmailTokenType.VERIFY_EMAIL, VERIFY_EMAIL_TTL)


def create_password_reset_token(db: Session, user_id: uuid.UUID) -> str:
    """生成重置密码 token,返回原文(发给用户)"""
    return _create_token_record(db, user_id, EmailTokenType.RESET_PASSWORD, RESET_PASSWORD_TTL)


def _create_token_record(
    db: Session,
    user_id: uuid.UUID,
    token_type: EmailTokenType,
    ttl: timedelta,
) -> str:
    token_plain = _generate_token()
    token_hash = _hash_token(token_plain)
    expires_at = datetime.now(timezone.utc) + ttl

    record = EmailToken(
        user_id=user_id,
        type=token_type,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(record)
    db.flush()  # 拿到 id
    return token_plain


# ============================================================
# 校验 token
# ============================================================


def verify_token(
    db: Session,
    token_plain: str,
    expected_type: EmailTokenType,
) -> EmailToken | None:
    """校验 token,返回 EmailToken 记录(已校验有效且未使用)

    返回 None 表示 token 无效/过期/已使用
    """
    token_hash = _hash_token(token_plain)
    record = (
        db.query(EmailToken)
        .filter(
            EmailToken.token_hash == token_hash,
            EmailToken.type == expected_type,
            EmailToken.used_at.is_(None),
        )
        .first()
    )
    if not record:
        return None

    # 检查过期
    if datetime.now(timezone.utc) > record.expires_at:
        return None

    return record


def mark_token_used(db: Session, token_record: EmailToken) -> None:
    """标记 token 已使用"""
    token_record.used_at = datetime.now(timezone.utc)
    db.flush()


# ============================================================
# 邮件发送抽象
# ============================================================


def _smtp_host() -> str:
    """去空白后的 SMTP 主机名(空串表示未配置)

    .env 里写成 `SMTP_HOST= `(带空格)不能误判为已配置,否则会用带空白
    的非法 host 去连接、白吃一次超时后静默失败。
    """
    return (settings.SMTP_HOST or "").strip()


def _smtp_configured() -> bool:
    """是否配了真实 SMTP(未配则走日志降级);纯空白视为未配置"""
    return bool(_smtp_host())


def _sender_address() -> str:
    """发件人地址:优先 SMTP_FROM_EMAIL → SMTP_USERNAME → noreply@<host>

    末级回落是防免认证 relay(host 配了但用户名/发件人都空)时,
    `formataddr((name, ""))` 会生成 `SecondLook <>` 这种畸形 From 头被对端
    MTA 拒收。进入本函数时 host 必非空(send_email 已先判过)。
    """
    from_email = (settings.SMTP_FROM_EMAIL or "").strip()
    if from_email:
        return from_email
    username = (settings.SMTP_USERNAME or "").strip()
    if username:
        return username
    return f"noreply@{_smtp_host()}"


def _build_message(to: str, subject: str, body: str) -> EmailMessage:
    """构造 MIME 邮件(EmailMessage 自动处理中文主题的 RFC2047 编码)"""
    msg = EmailMessage()
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, _sender_address()))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def _smtp_deliver(msg: EmailMessage) -> None:
    """通过 smtplib 投递邮件(按 SMTP_SECURITY 选择加密方式)

    - ssl:SMTP_SSL 隐式 TLS(常见 465)
    - starttls:先明文连接再升级 TLS(常见 587)
    - none / 其他:不加密(仅本地 relay);非法值归一到 starttls 更安全
    连接均带 timeout,SMTP 不可达时快速抛错而非长时间阻塞调用线程。
    host / username 均取 trim 后的值(避免带空白误连);password 不 trim。
    """
    security = settings.SMTP_SECURITY.strip().lower()
    if security not in ("ssl", "starttls", "none"):
        security = "starttls"
    timeout = settings.SMTP_TIMEOUT_SECONDS
    host = _smtp_host()
    username = (settings.SMTP_USERNAME or "").strip()

    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            host,
            settings.SMTP_PORT,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        server = smtplib.SMTP(host, settings.SMTP_PORT, timeout=timeout)

    with server:
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if username:
            server.login(username, settings.SMTP_PASSWORD)
        server.send_message(msg)


def send_email(to: str, subject: str, body: str) -> None:
    """发送邮件

    - 未配 SMTP_HOST(开发模式):仅打印日志,不实际发信
    - 已配 SMTP:走 smtplib 投递;失败只记日志不向上抛

    发送失败不抛异常是有意为之:register / resend / forgot 都在发信前已
    commit,SMTP 抖动不应把已成功的注册变成 500;用户可通过重发验证邮件
    接口补发。日志只记 to / subject,不包含凭证(logger.exception 的 traceback
    不走脱敏,故不能把密码写进消息)。
    """
    if not _smtp_configured():
        logger.info(
            "[EMAIL] (SMTP 未配置,开发模式仅打印) to=%s subject=%s\n%s",
            to,
            subject,
            body,
        )
        return

    msg = _build_message(to, subject, body)
    try:
        _smtp_deliver(msg)
        logger.info("[EMAIL] 已发信 to=%s subject=%s", to, subject)
    except Exception:
        logger.exception("[EMAIL] 发信失败 to=%s subject=%s", to, subject)


def send_verification_email(user: User, token_plain: str) -> None:
    """发邮箱验证邮件"""
    verify_url = f"{settings.APP_BASE_URL}/auth/verify-email?token={token_plain}"
    send_email(
        to=user.email,
        subject="[SecondLook] 验证你的邮箱",
        body=f"请点击以下链接验证邮箱(24 小时内有效):\n\n{verify_url}\n\n如果不是你本人操作,请忽略此邮件。",
    )


def send_password_reset_email(user: User, token_plain: str) -> None:
    """发重置密码邮件"""
    reset_url = f"{settings.APP_BASE_URL}/auth/password/reset?token={token_plain}"
    send_email(
        to=user.email,
        subject="[SecondLook] 重置你的密码",
        body=f"请点击以下链接重置密码(30 分钟内有效):\n\n{reset_url}\n\n如果不是你本人操作,请忽略此邮件。",
    )
