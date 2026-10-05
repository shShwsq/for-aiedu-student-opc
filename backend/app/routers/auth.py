"""认证授权路由

路由:
- POST   /auth/register              邮箱密码注册
- POST   /auth/verify-email          验证邮箱
- POST   /auth/resend-verification   重发验证邮件
- POST   /auth/login                邮箱密码登录
- POST   /auth/refresh               刷新 access token
- GET    /auth/me                    当前用户信息(需登录)
- POST   /auth/password/forgot       忘记密码(发重置邮件)
- POST   /auth/password/reset        重置密码
- POST   /auth/password/change       修改密码(需登录)
- POST   /auth/oauth/{provider}     Git 平台 OAuth 登录(github / gitee)
- DELETE /auth/account               删除账号(硬删除,连带 task+配置+token+绑定+磁盘 skill)
"""
import logging
import shutil
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.email_service import (
    create_email_verification_token,
    create_password_reset_token,
    mark_token_used,
    send_password_reset_email,
    send_verification_email,
    verify_token,
)
from app.git_errors import http_exc_for_git_error
from app.git_provider import GitProviderError, get_provider
from app.models.email_token import EmailTokenType
from app.models.user import User
from app.models.user_git_binding import UserGitBinding
from app.schemas.user import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    GitOAuthRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
)
from app.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_token_pair,
    create_access_token,
    extract_user_id_from_token,
    hash_password,
    verify_password,
)
from app.skills.loader import (
    DEFAULT_SKILLS_ROOT,
    get_user_skills_root,
    reload_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ============================================================
# 注册
# ============================================================


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """邮箱密码注册

    - 邮箱已存在 → 409
    - 注册成功 → 自动发验证邮件,返回 201
    """
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已注册",
        )

    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
    )
    db.add(user)
    db.flush()

    # 生成验证 token + 发邮件
    token_plain = create_email_verification_token(db, user.id)
    db.commit()

    # 发邮件(commit 后再发,避免事务失败但邮件已发)
    send_verification_email(user, token_plain)

    return MessageResponse(message="注册成功,请查收邮件验证邮箱")


# ============================================================
# 邮箱验证
# ============================================================


@router.post("/verify-email", response_model=MessageResponse)
def verify_email(req: VerifyEmailRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """验证邮箱(token 来自注册邮件)"""
    token_record = verify_token(db, req.token, EmailTokenType.VERIFY_EMAIL)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证链接无效或已过期,请重新申请",
        )

    user = db.query(User).filter(User.id == token_record.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户不存在",
        )

    if user.is_email_verified:
        # 已验证,标记 token 已用(幂等)
        mark_token_used(db, token_record)
        db.commit()
        return MessageResponse(message="邮箱已验证(此前已完成)")

    user.email_verified_at = datetime.now(timezone.utc)
    mark_token_used(db, token_record)
    db.commit()

    return MessageResponse(message="邮箱验证成功,现在可以登录")


@router.post("/resend-verification", response_model=MessageResponse)
def resend_verification(
    req: ResendVerificationRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """重发验证邮件(若用户未验证)"""
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        # 不暴露邮箱是否注册,统一返回成功
        return MessageResponse(message="若该邮箱已注册且未验证,邮件已发送")
    if user.is_email_verified:
        return MessageResponse(message="若该邮箱已注册且未验证,邮件已发送")

    token_plain = create_email_verification_token(db, user.id)
    db.commit()
    send_verification_email(user, token_plain)

    return MessageResponse(message="若该邮箱已注册且未验证,邮件已发送")


# ============================================================
# 登录 / 刷新
# ============================================================


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """邮箱密码登录

    - 邮箱不存在 / 密码错 → 401(统一信息,防爆破枚举)
    - 未验证邮箱 → 403
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )

    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="邮箱未验证,请先查收验证邮件",
        )

    access, refresh = create_token_pair(user.id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=UserResponse.from_user(user),
    )


@router.post("/refresh", response_model=RefreshResponse)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)) -> RefreshResponse:
    """用 refresh token 换新的 access token"""
    try:
        user_id = extract_user_id_from_token(req.refresh_token, expected_type="refresh")
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 已过期,请重新登录",
        )
    except TokenInvalidError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"refresh token 无效: {e}",
        )

    # 校验用户仍存在
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )

    return RefreshResponse(access_token=create_access_token(user.id))


# ============================================================
# 当前用户
# ============================================================


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)) -> UserResponse:
    """获取当前登录用户信息"""
    return UserResponse.from_user(user)


# ============================================================
# 忘记密码 / 重置密码
# ============================================================


@router.post("/password/forgot", response_model=MessageResponse)
def forgot_password(
    req: ForgotPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """忘记密码(发重置邮件)

    - 邮箱不存在也返回成功(防爆破枚举)
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        return MessageResponse(message="若该邮箱已注册,重置邮件已发送")

    token_plain = create_password_reset_token(db, user.id)
    db.commit()
    send_password_reset_email(user, token_plain)

    return MessageResponse(message="若该邮箱已注册,重置邮件已发送")


@router.post("/password/reset", response_model=MessageResponse)
def reset_password(
    req: ResetPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """重置密码(token 来自重置邮件)"""
    token_record = verify_token(db, req.token, EmailTokenType.RESET_PASSWORD)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="重置链接无效或已过期,请重新申请",
        )

    user = db.query(User).filter(User.id == token_record.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户不存在",
        )

    user.password_hash = hash_password(req.new_password)
    mark_token_used(db, token_record)
    db.commit()

    return MessageResponse(message="密码重置成功,请用新密码登录")


@router.post("/password/change", response_model=MessageResponse)
def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """修改密码(已登录用户)

    - 已有密码的用户:必须传 current_password 且匹配
    - OAuth 用户(password_hash 为空):可跳过 current_password 直接设置
    - 新密码不能与当前密码相同(已有密码时)
    """
    has_password = bool(user.password_hash)

    if has_password:
        if not req.current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="请输入当前密码",
            )
        if not verify_password(req.current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="当前密码错误",
            )
        if verify_password(req.new_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="新密码不能与当前密码相同",
            )

    user.password_hash = hash_password(req.new_password)
    db.commit()

    return MessageResponse(message="密码修改成功" if has_password else "密码设置成功")


# ============================================================
# 删除账号
# ============================================================


def _cleanup_user_skills(user_id: uuid.UUID) -> None:
    """删除账户时清理磁盘上该用户上传的 skill

    skill 走文件系统存储(USER_SKILLS_DIR),无数据库外键,需手动清理:
    - 当前落地位置:<USER_SKILLS_DIR>/user_<uid>/(DirectorySkillStorage)
    - 旧版本遗留位置:backend/skills/user_<uid>/(与 skills.delete 同源清理,幂等)
    清理后刷新进程级注册表,避免残留条目。失败仅记日志,不阻断账号删除。
    """
    try:
        scenario_id = f"user_{user_id}"
        shutil.rmtree(get_user_skills_root() / scenario_id, ignore_errors=True)
        shutil.rmtree(DEFAULT_SKILLS_ROOT / scenario_id, ignore_errors=True)
        reload_registry()
        logger.info("用户 %s 的 skill 磁盘目录已清理", user_id)
    except Exception:
        logger.exception("清理用户 %s 的 skill 磁盘目录失败", user_id)


@router.delete("/account", response_model=MessageResponse)
def delete_account(
    req: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """删除账号(硬删除,不可恢复)

    要求用户输入完整邮箱作为二次确认,后端校验匹配后执行硬删除:
    - 连带删除该用户的 task、user_llm_config、user_git_bindings(email_token 已 CASCADE)
    - 练习数据 / 项目记忆 / 偏好设置等均随外键 CASCADE 删除
    - 解除 Git 平台关联(provider_user_id 随绑定行释放,可被其他账号绑定)
    - 清理磁盘上该用户上传的 skill 目录

    安全考虑:
    - 邮箱不匹配 → 400,不执行删除
    - 只删除当前登录用户,不影响他人
    """
    # 二次确认:输入的邮箱必须与当前账号邮箱完全一致(忽略大小写)
    if req.email.strip().lower() != user.email.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="邮箱不匹配,无法删除账号",
        )

    # 删 user 即可,所有关联数据由数据库外键 ondelete=CASCADE 自动级联删除:
    # user → tasks → conversations/results, user → user_llm_config, user → email_token
    db.delete(user)
    db.commit()

    # skill 走文件系统存储,无数据库外键,需手动清理磁盘目录
    _cleanup_user_skills(user.id)

    logger.info("用户 %s (%s) 已删除账号", user.id, user.email)

    return MessageResponse(message="账号已删除")


# ============================================================
# Git 平台 OAuth 登录(GitHub / Gitee)
# ============================================================


@router.post("/oauth/{provider}", response_model=TokenResponse)
def git_oauth(
    provider: str,
    req: GitOAuthRequest,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Git 平台 OAuth 登录(github / gitee)

    - code 换 access_token 换用户信息
    - (provider, provider_user_id) 已绑定 → 登录
    - 未绑定,email 已注册 → 关联(建 binding,access_token="" 仅登录)
    - 未绑定,email 也未注册 → 自动创建账号(无密码)+ binding
    """
    try:
        p = get_provider(provider)
    except GitProviderError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    try:
        gh_user = p.oauth_login(req.code)
    except GitProviderError as e:
        # 区分链路故障与真正的 OAuth 错误:前者 502/504 提示稍后重试,
        # 一律报 400 会把用户引去改 .env(见 app/git_errors.py)
        raise http_exc_for_git_error(
            e, action="Git 平台 OAuth 登录", provider_display=p.display_name
        ) from e

    # 1. (provider, provider_user_id) 已绑定 → 登录该用户
    binding = (
        db.query(UserGitBinding)
        .filter(
            UserGitBinding.provider == provider,
            UserGitBinding.provider_user_id == gh_user.provider_user_id,
        )
        .first()
    )
    user = binding.user if binding else None

    if not user and gh_user.email:
        # 2. email 已注册,关联该平台(建仅登录 binding,access_token="")
        user = db.query(User).filter(User.email == gh_user.email).first()
        if user:
            # 该账号在这个平台已有绑定行(平台侧换过账号 / 同一邮箱挂到多个
            # 平台账号):再插一行会撞 uq_user_provider(user_id, provider)
            # 唯一约束 → 裸 IntegrityError 会变成没有 detail 的 500。
            # 这里不静默改写既有 provider_user_id(身份关联不能悄悄换人),
            # 明确 409 让用户知道该怎么解绑。
            if any(b.provider == provider for b in user.git_bindings):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"该邮箱已注册,且已绑定另一个 {p.display_name} 账号;"
                        "请先用原账号登录并解绑,或改用其他邮箱"
                    ),
                )
            db.add(
                UserGitBinding(
                    user_id=user.id,
                    provider=provider,
                    provider_user_id=gh_user.provider_user_id,
                    access_token="",
                )
            )

    if not user:
        # 3. 完全新用户,自动创建(无密码,只走 OAuth)
        if not gh_user.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{p.display_name} 账号无可用邮箱,无法自动注册",
            )
        user = User(
            email=gh_user.email,
            password_hash="",  # OAuth 用户无密码
            # OAuth 已隐含邮箱验证(平台已 verified)
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()
        db.add(
            UserGitBinding(
                user_id=user.id,
                provider=provider,
                provider_user_id=gh_user.provider_user_id,
                access_token="",
            )
        )

    db.commit()
    db.refresh(user)

    access, refresh = create_token_pair(user.id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=UserResponse.from_user(user),
    )
