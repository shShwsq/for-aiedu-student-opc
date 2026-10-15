"""安全工具:密码哈希 + JWT 签发/校验 + 对称加密

双 token 设计:
- access_token: 短期(15 分钟),用于 API 鉴权
- refresh_token: 长期(7 天),仅用于刷新 access_token

JWT payload:
- sub: 用户 ID(str)
- type: "access" | "refresh"
- iat: 签发时间
- exp: 过期时间

对称加密(Fernet):用于加密存储第三方 OAuth token(如 GitHub / Gitee access_token),
避免凭据明文落库。密钥来自 settings.GITHUB_TOKEN_SECRET(与 provider 无关,所有
git provider token 共用同一把 Fernet 密钥),留空则启动时随机生成。

该密钥实际保护三类数据(名字已偏窄,只管 GitHub 是误解):
- git OAuth token(user_git_bindings);
- 智能体凭证(user_agent_configs.credentials_encrypted);
- 用户 LLM/Embedding api_key(user_llm_configs 的 enc:v1 信封字段级加密)。
留空时后两者会与 OAuth token 一样面临「重启换随机 key → 旧密文永久不可解」,
故 encrypt_secret_envelope / 相关迁移对空密钥做了防呆(宁存明文不存死密文)。
"""
import functools
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt as pyjwt
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


# ============================================================
# 密码哈希
# ============================================================


def hash_password(password: str) -> str:
    """bcrypt 加盐哈希

    返回值含 salt 和 hash,可直接存数据库
    """
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码与哈希是否匹配"""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# ============================================================
# JWT 签发
# ============================================================


def _create_token(
    user_id: uuid.UUID,
    token_type: str,
    expires_delta: timedelta,
) -> str:
    """内部:签发单个 JWT"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: uuid.UUID) -> str:
    """签发 access token(短期)"""
    return _create_token(user_id, "access", timedelta(minutes=15))


def create_refresh_token(user_id: uuid.UUID) -> str:
    """签发 refresh token(长期 7 天)"""
    return _create_token(user_id, "refresh", timedelta(days=7))


def create_token_pair(user_id: uuid.UUID) -> tuple[str, str]:
    """签发 access + refresh 一对 token"""
    return create_access_token(user_id), create_refresh_token(user_id)


# ============================================================
# JWT 校验
# ============================================================


class TokenError(Exception):
    """token 校验失败基类"""


class TokenExpiredError(TokenError):
    """token 已过期"""


class TokenInvalidError(TokenError):
    """token 无效(签名错/格式错/类型错)"""


def decode_token(token: str, expected_type: str | None = None) -> dict:
    """解码并校验 JWT

    参数:
        token: JWT 字符串
        expected_type: 期望类型("access" / "refresh"),None 不校验

    抛出:
        TokenExpiredError: token 已过期
        TokenInvalidError: token 无效(签名错/格式错/类型错)

    返回:payload dict,含 sub / type / iat / exp
    """
    try:
        payload = pyjwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except pyjwt.ExpiredSignatureError as e:
        raise TokenExpiredError("token 已过期") from e
    except pyjwt.InvalidTokenError as e:
        raise TokenInvalidError(f"token 无效: {e}") from e

    if expected_type and payload.get("type") != expected_type:
        raise TokenInvalidError(
            f"token 类型错误:期望 {expected_type},实际 {payload.get('type')}"
        )

    return payload


def extract_user_id_from_token(
    token: str, expected_type: str | None = None
) -> uuid.UUID:
    """从 token 中提取用户 ID"""
    payload = decode_token(token, expected_type)
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise TokenInvalidError(f"token payload 缺少 sub 或格式错: {e}") from e


# ============================================================
# 对称加密(Fernet)——用于加密存储第三方 OAuth token
# ============================================================


@functools.lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    """获取 Fernet 实例(模块级单例,首次调用后缓存)

    密钥来源:settings.GITHUB_TOKEN_SECRET(必须为 Fernet 兼容的 base64 串);
    该密钥与 provider 无关,GitHub / Gitee 等 git provider 的 access_token 共用。
    留空则启动时随机生成(开发期方便,生产环境必须固定,否则重启后旧密文无法解密)。

    用 lru_cache 缓存:同一进程内只构造一次 Fernet,避免每次解密都重建实例。
    开发期若 GITHUB_TOKEN_SECRET 为空,首次调用生成随机 key 后固定,
    不再出现「每次调用随机生成 → 旧密文解密失败」的问题。
    """
    key = settings.GITHUB_TOKEN_SECRET
    if not key:
        # 开发期自动生成,生产环境应在 .env 固定 GITHUB_TOKEN_SECRET
        key = Fernet.generate_key().decode("utf-8")
    return Fernet(key.encode("utf-8"))


def encrypt_secret(plaintext: str) -> str:
    """加密字符串,返回 base64 密文(可安全落库)"""
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """解密 base64 密文,返回原文

    抛出 InvalidToken 表示密文损坏或密钥不匹配
    """
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(f"密文解密失败(密钥不匹配或数据损坏): {e}") from e


# ============================================================
# 信封加密(enc:v1)——用于 JSONB 内字段级密钥(如用户 LLM/Embedding api_key)
# ============================================================

# 密文前缀标记。存进 JSONB 的值形如 "enc:v1:gAAAA...(Fernet base64)"。
# 前缀让读取端能区分「已加密」与「历史明文」,保证:
# - 加密幂等:对已带前缀的值不再二次加密(避免「空串保留旧 key」时把密文再包一层)
# - 平滑兼容:迁移未跑完 / 新老数据共存时,无前缀的明文原样透传,不误解密
_ENVELOPE_PREFIX = "enc:v1:"


def is_enveloped(value: str) -> bool:
    """判断值是否为 enc:v1 信封密文"""
    return bool(value) and value.startswith(_ENVELOPE_PREFIX)


def encrypt_secret_envelope(plaintext: str) -> str:
    """加密并加 enc:v1 前缀;空值或已带前缀(已加密)的值原样返回(幂等)。

    与 encrypt_secret 的区别:带前缀便于读取端识别密文/明文,支持字段级、
    可重入的加密(同一值多次调用结果稳定,不会双重包裹)。

    空密钥防呆:GITHUB_TOKEN_SECRET 留空时 _get_fernet 每进程随机生成,用它加密
    会得到「重启即不可解」的死密文。此时宁存明文(无前缀,decrypt 原样透传)也不
    落死密文——与改造前的明文存储行为一致。生产环境必须固定该密钥才能真加密。
    """
    if not plaintext or is_enveloped(plaintext):
        return plaintext
    if not settings.GITHUB_TOKEN_SECRET:
        return plaintext
    return _ENVELOPE_PREFIX + encrypt_secret(plaintext)


def decrypt_secret_envelope(value: str) -> str:
    """解密 enc:v1 信封密文;空值或无前缀(历史明文)原样返回。

    抛出 ValueError(密钥不匹配/密文损坏)由调用方决定回退策略。
    """
    if not value or not is_enveloped(value):
        return value
    return decrypt_secret(value[len(_ENVELOPE_PREFIX):])
