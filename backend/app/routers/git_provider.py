"""Git 平台绑定与仓库访问路由(统一 GitHub / Gitee)

已登录用户在设置页"绑定 GitHub/Gitee"后,后端存加密的 access_token(scope 含仓库),
任务执行时解密传给 clone_repo 工具,用于克隆私有仓库。

端点(带 {provider} 路径参数,provider ∈ github|gitee):
- POST   /git/{provider}/bind        用授权码换 token 并加密落库(绑定/升级 scope)
- GET    /git/{provider}/status      查看当前用户该平台绑定状态
- DELETE /git/{provider}/bind        解绑(清 token,保留 provider_user_id 关联)
- GET    /git/{provider}/repos       列出当前用户该平台仓库(含私有)
- PATCH  /git/{provider}/sync-email  将账号邮箱同步为平台邮箱(仅支持可验证邮箱的平台)
- POST   /git/{provider}/refresh     用 refresh_token 刷新 access_token(仅 Gitee 支持)

错误码约定(实现见 app/git_errors.py):
- 平台明确拒绝(授权码失效 / scope 不足 / 凭证错)→ 400,token 类接口用 401
- 链路或平台故障(连不上 / 超时 / 5xx / 响应损坏)→ 502/504,detail 提示稍后重试
两类都会在服务端记一条 warning(带根因与 user_id),避免只剩一行状态码无从排查。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.git_errors import http_exc_for_git_error
from app.git_provider import GitProviderError, GitProviderUnavailable, get_provider
from app.models.user import User
from app.models.user_git_binding import UserGitBinding
from app.security import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/git", tags=["git"])


# ============================================================
# 仓库列表内存缓存(30s TTL)
# ============================================================
# 背景:list_repos 每次都要调 GitHub/Gitee /user/repos(耗时 1-5s),
# 而仓库列表短时间内不会变化。加进程内缓存,30s 内同一用户同一 provider
# 直接返回上次结果。key=(user_id, provider),value=(fetched_at, repos)。
# bind/unbind 时会调 _invalidate_repos_cache 清除,避免换账号后返回旧仓库。
_REPOS_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_REPOS_CACHE_TTL = 30.0  # 秒


def _invalidate_repos_cache(user_id=None, provider=None) -> None:
    """清除仓库列表缓存

    无参 → 清全部(调试用);指定 user_id+provider → 清该用户该平台一条。
    bind/unbind 时调用,避免换账号后返回旧仓库(安全考虑)。
    """
    if user_id is None or provider is None:
        _REPOS_CACHE.clear()
        return
    _REPOS_CACHE.pop((user_id, provider), None)


# ============================================================
# 请求/响应模型
# ============================================================


class GitBindRequest(BaseModel):
    """绑定请求体:前端从 OAuth callback 拿到的 code"""

    code: str


class GitProviderStatusResponse(BaseModel):
    """绑定状态"""

    provider: str  # github / gitee
    bound: bool  # 是否已绑定(有 access_token)
    provider_user_id: str | None  # 平台用户 ID(可能登录时绑定但未授权 repo)
    provider_login: str | None  # 平台用户名(实时查 /user,失败则空)
    avatar_url: str | None  # 头像
    # 邮箱不一致提示(仅 bind 响应中可能为 true,status 查询恒为 false)
    email_mismatch: bool = False
    provider_email: str | None = None  # 平台可验证邮箱(不一致时用于弹窗展示)
    current_email: str | None = None  # 账号当前邮箱
    # token 有效期信息(refresh 机制用)
    token_expires_at: datetime | None = None  # None=不过期(GitHub)/老数据未记录
    token_expired: bool = False  # 是否已过期(后端基于 expires_at 判断)
    token_refreshable: bool = False  # 是否支持手动刷新(有 refresh_token)


class GitRepoItem(BaseModel):
    """仓库列表项(精简)"""

    full_name: str  # owner/repo
    name: str  # repo
    private: bool
    html_url: str
    clone_url: str
    default_branch: str


class GitReposResponse(BaseModel):
    repos: list[GitRepoItem]


class SyncEmailResponse(BaseModel):
    """邮箱同步结果"""

    email: str  # 更新后的邮箱
    email_verified: bool  # 是否已验证


# ============================================================
# 辅助
# ============================================================


def _resolve_provider(provider: str):
    """把路径参数转成 provider 实例,未知 id → 400"""
    try:
        return get_provider(provider)
    except GitProviderError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e


def _get_binding(db: Session, user_id, provider: str) -> UserGitBinding | None:
    return (
        db.query(UserGitBinding)
        .filter(
            UserGitBinding.user_id == user_id,
            UserGitBinding.provider == provider,
        )
        .first()
    )


# token 临近过期提前刷新的提前量(秒),避免请求途中边界过期
REFRESH_LEAD_SECONDS = 300


def _ensure_valid_token(db: Session, binding: UserGitBinding, p) -> str:
    """返回有效 access_token 明文;临近过期则用 refresh_token 刷新并落库

    - 不支持刷新(GitHub)或无 expires_at(老数据)→ 直接返回当前 token
    - 未临近过期(> REFRESH_LEAD_SECONDS)→ 直接返回当前 token
    - 临近过期/已过期且有 refresh_token → 刷新并更新 binding(access/refresh/expires_at)
    - 刷新失败 → 抛 GitProviderError(平台判定 refresh_token 失效 → 上层 401
      提示重新绑定)或 GitProviderUnavailable(链路故障 → 上层 502/504,
      不该让用户以为凭证失效)

    db.commit() 由本函数负责(刷新成功时);刷新失败时不提交,保持原 token 不变。
    """
    token = decrypt_secret(binding.access_token)
    if not p.supports_token_refresh or not binding.expires_at:
        return token
    if binding.expires_at > datetime.now(timezone.utc) + timedelta(
        seconds=REFRESH_LEAD_SECONDS
    ):
        return token
    if not binding.refresh_token:
        return token  # 无 refresh_token,退化为返回当前 token(让被动 401 处理)
    try:
        new_set = p.refresh_access_token(decrypt_secret(binding.refresh_token))
    except GitProviderError as e:
        logger.warning(
            "user %s 刷新 %s token 失败: %s",
            binding.user_id, p.display_name, e,
        )
        raise
    binding.access_token = encrypt_secret(new_set.access_token)
    binding.refresh_token = (
        encrypt_secret(new_set.refresh_token) if new_set.refresh_token else ""
    )
    binding.expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=new_set.expires_in)
        if new_set.expires_in else None
    )
    db.commit()
    logger.info(
        "user %s 的 %s token 已自动刷新", binding.user_id, p.display_name
    )
    return new_set.access_token


# ============================================================
# 端点
# ============================================================


@router.post("/{provider}/bind", response_model=GitProviderStatusResponse)
def bind_provider(
    provider: str,
    req: GitBindRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """绑定 Git 平台:用 code 换 token,加密落库,更新 provider_user_id

    场景:
    - 密码注册用户首次绑定:新建 binding(provider_user_id + access_token)
    - OAuth 登录用户升级权限:binding 已存在,只更新 access_token
    """
    p = _resolve_provider(provider)

    try:
        token_set = p.exchange_code_for_token(req.code)
        info = p.get_user_info(token_set.access_token)
    except GitProviderError as e:
        # 网络/平台故障给 502/504,真正的 OAuth 参数错才 400
        raise http_exc_for_git_error(
            e,
            action=f"绑定 {p.display_name}",
            provider_display=p.display_name,
            user_id=current_user.id,
        ) from e

    # provider_user_id 冲突检查:若已被其他用户占用,拒绝绑定
    if info.provider_user_id:
        existing = (
            db.query(UserGitBinding)
            .filter(
                UserGitBinding.provider == provider,
                UserGitBinding.provider_user_id == info.provider_user_id,
                UserGitBinding.user_id != current_user.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{p.display_name} 账号已被其他用户绑定",
            )

    # upsert:同一用户同一 provider 只有一行
    binding = _get_binding(db, current_user.id, provider)
    if binding is None:
        binding = UserGitBinding(
            user_id=current_user.id,
            provider=provider,
        )
        db.add(binding)
    # 统一更新 provider_user_id + token 三件套
    binding.provider_user_id = info.provider_user_id
    # 缓存 login / avatar,status 接口直接读这两列,不再每次调 /user
    binding.provider_login = info.login
    binding.avatar_url = info.avatar_url
    binding.access_token = encrypt_secret(token_set.access_token)
    binding.refresh_token = (
        encrypt_secret(token_set.refresh_token) if token_set.refresh_token else ""
    )
    binding.expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token_set.expires_in)
        if token_set.expires_in else None
    )
    db.commit()
    db.refresh(binding)
    # 绑定可能换账号,清除该用户该平台的仓库缓存
    _invalidate_repos_cache(current_user.id, provider)

    logger.info(
        "user %s 绑定 %s 成功,provider_user_id=%s",
        current_user.id,
        p.display_name,
        info.provider_user_id,
    )

    # 邮箱不一致检测:仅支持可验证邮箱的平台才做
    provider_email = info.email
    if not provider_email and p.supports_verified_email:
        try:
            emails = p.get_user_emails(token_set.access_token)
            if emails:
                provider_email = emails[0]
        except GitProviderError:
            pass  # 拿不到就不做对比,不阻塞绑定
    email_mismatch = bool(
        provider_email and provider_email.lower() != current_user.email.lower()
    )

    return GitProviderStatusResponse(
        provider=provider,
        bound=True,
        provider_user_id=binding.provider_user_id,
        provider_login=info.login,
        avatar_url=info.avatar_url,
        email_mismatch=email_mismatch,
        provider_email=provider_email,
        current_email=current_user.email,
        token_expires_at=binding.expires_at,
        token_refreshable=bool(binding.refresh_token),
    )


@router.get("/{provider}/status", response_model=GitProviderStatusResponse)
def get_provider_status(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """查看当前用户该平台绑定状态

    bound=true 表示有 access_token(可访问私有仓库);
    provider_user_id 非空但 bound=false 表示仅用 OAuth 登录过(scope 不含仓库),
    未授权仓库访问,需重新绑定升级 scope。
    """
    p = _resolve_provider(provider)
    binding = _get_binding(db, current_user.id, provider)

    bound = bool(binding and binding.access_token)
    # 直接读绑定时缓存的 login / avatar,避免每次打开页面都调外部 /user API
    provider_login = binding.provider_login if binding else None
    avatar_url = binding.avatar_url if binding else None

    # 老数据回填:绑定早于该字段上线的 binding,login/avatar 为 NULL。
    # 懒加载一次 /user 回填到 binding,之后 status 永远不再走外部网络。
    # 仅在 bound 时尝试(有 token 才能调 /user);失败则静默返回 NULL,不阻塞页面。
    if bound and provider_login is None and avatar_url is None:
        try:
            token = _ensure_valid_token(db, binding, p)
            info = p.get_user_info(token)
            provider_login = info.login
            avatar_url = info.avatar_url
            binding.provider_login = provider_login
            binding.avatar_url = avatar_url
            db.commit()
        except (GitProviderError, ValueError) as e:
            logger.warning(
                "user %s 的 %s 老数据回填 login/avatar 失败: %s",
                current_user.id,
                p.display_name,
                e,
            )

    return GitProviderStatusResponse(
        provider=provider,
        bound=bound,
        provider_user_id=binding.provider_user_id if binding else None,
        provider_login=provider_login,
        avatar_url=avatar_url,
        token_expires_at=binding.expires_at if binding else None,
        token_expired=bool(
            binding
            and binding.expires_at
            and binding.expires_at <= datetime.now(timezone.utc)
        ),
        token_refreshable=bool(binding and binding.refresh_token),
    )


@router.delete("/{provider}/bind", response_model=GitProviderStatusResponse)
def unbind_provider(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """解绑:清除 access_token(保留 provider_user_id 行,因为 OAuth 登录仍可能用到)

    若想彻底解除账号关联,需走账号设置里的"删除账号"(随用户 CASCADE 删除)。

    最佳实践:解绑时主动调平台 revoke API 把旧 token 在平台端也失效掉,
    否则平台端旧 token 仍有效 + 授权状态粘性会导致用户重新绑定时换 token 失败。
    revoke 失败不阻塞解绑(本地 token 照清),记 warning 日志,后续 API 调用
    收到 401 时会自动提示"token 失效,请重新绑定"作为兜底。
    """
    p = _resolve_provider(provider)
    binding = _get_binding(db, current_user.id, provider)
    if binding is not None:
        # 先在平台端 revoke 旧 token(用明文 token 调用),再清本地
        # 顺序很重要:先清本地就拿不到明文 token 了
        if binding.access_token:
            try:
                token = decrypt_secret(binding.access_token)
                p.revoke_token(token)
                logger.info(
                    "user %s 的 %s token 已在平台端 revoke",
                    current_user.id,
                    p.display_name,
                )
            except (GitProviderError, ValueError) as e:
                # revoke 失败不阻塞解绑:本地 token 照清,
                # 平台端旧 token 会自然过期或被下次绑定时的新 token 覆盖
                logger.warning(
                    "user %s 的 %s token revoke 失败(不阻塞解绑): %s",
                    current_user.id,
                    p.display_name,
                    e,
                )

        binding.access_token = ""
        # 清空 login / avatar 缓存(与 access_token 同生命周期)
        binding.provider_login = None
        binding.avatar_url = None
        db.commit()
        db.refresh(binding)
        # 解绑后仓库列表应清空,清除缓存
        _invalidate_repos_cache(current_user.id, provider)
        logger.info("user %s 解绑 %s(清除 access_token)", current_user.id, provider)

    return GitProviderStatusResponse(
        provider=provider,
        bound=False,
        provider_user_id=binding.provider_user_id if binding else None,
        provider_login=None,
        avatar_url=None,
    )


@router.patch("/{provider}/sync-email", response_model=SyncEmailResponse)
def sync_email(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SyncEmailResponse:
    """将账号邮箱同步为平台可验证邮箱

    仅支持 supports_verified_email 的平台(GitHub)。Gitee 无可验证邮箱语义,
    调用返回 400。

    安全考虑:
    - 未绑定该平台 → 403
    - 平台不支持可验证邮箱 → 400
    - 拿不到平台 email → 400
    - 平台邮箱已被其他账号占用 → 409(不自动更新,避免账号合并风险)
    """
    p = _resolve_provider(provider)
    if not p.supports_verified_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{p.display_name} 暂不支持邮箱同步",
        )

    binding = _get_binding(db, current_user.id, provider)
    if not binding or not binding.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"尚未绑定 {p.display_name},无法同步邮箱",
        )

    try:
        token = _ensure_valid_token(db, binding, p)
        info = p.get_user_info(token)
    except GitProviderError as e:
        raise http_exc_for_git_error(
            e,
            action=f"同步 {p.display_name} 邮箱(取用户信息)",
            provider_display=p.display_name,
            user_id=current_user.id,
        ) from e
    except ValueError as e:
        # decrypt_secret 失败:服务端密钥与库里密文不匹配,属服务端配置问题
        logger.error(
            "user %s 的 %s token 解密失败: %s", current_user.id, p.display_name, e
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="token 解密失败,请联系管理员",
        ) from e

    provider_email = info.email
    if not provider_email:
        try:
            emails = p.get_user_emails(token)
            if emails:
                provider_email = emails[0]
        except GitProviderError as e:
            raise http_exc_for_git_error(
                e,
                action=f"同步 {p.display_name} 邮箱(取邮箱列表)",
                provider_display=p.display_name,
                user_id=current_user.id,
            ) from e

    if not provider_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{p.display_name} 账号无可用 verified primary email",
        )

    # 邮箱一致,无需更新
    if provider_email.lower() == current_user.email.lower():
        return SyncEmailResponse(
            email=current_user.email,
            email_verified=current_user.is_email_verified,
        )

    # 查重:平台邮箱已被其他账号占用 → 拒绝,避免账号合并风险
    existing = (
        db.query(User)
        .filter(User.email == provider_email, User.id != current_user.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该 {p.display_name} 邮箱已被其他账号占用,无法同步",
        )

    old_email = current_user.email
    current_user.email = provider_email
    # 可验证邮箱视为已验证
    if not current_user.email_verified_at:
        current_user.email_verified_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(current_user)

    logger.info(
        "user %s 同步邮箱: %s -> %s",
        current_user.id,
        old_email,
        provider_email,
    )

    return SyncEmailResponse(
        email=current_user.email,
        email_verified=current_user.is_email_verified,
    )


@router.get("/{provider}/repos", response_model=GitReposResponse)
def list_repos(
    provider: str,
    refresh: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitReposResponse:
    """列出当前用户该平台仓库(含私有)

    用于任务创建页下拉选择私有仓库。返回按更新时间倒序的前 100 个。
    30s 内重复请求走内存缓存,避免每次都调外部 GitHub/Gitee API。

    refresh=true 时跳过缓存读(强制调外部 API 并更新缓存),
    用于仓库下拉框旁的「刷新」按钮:用户在平台上新建仓库后立即拉取。
    """
    p = _resolve_provider(provider)
    binding = _get_binding(db, current_user.id, provider)
    if not binding or not binding.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"尚未绑定 {p.display_name} 或未授权仓库访问,请先在设置页绑定",
        )

    # 命中缓存且未过期且非强制刷新 → 直接返回(不调外部 API)
    cache_key = (current_user.id, provider)
    if not refresh:
        cached = _REPOS_CACHE.get(cache_key)
        if cached is not None:
            fetched_at, raw = cached
            if time.monotonic() - fetched_at < _REPOS_CACHE_TTL:
                repos = [GitRepoItem(**item) for item in raw]
                return GitReposResponse(repos=repos)

    try:
        token = _ensure_valid_token(db, binding, p)
        raw = p.list_repos(token)
    except GitProviderUnavailable as e:
        # 链路/平台故障:token 其实没坏,提示"重新绑定"会误导用户
        raise http_exc_for_git_error(
            e,
            action=f"列出 {p.display_name} 仓库",
            provider_display=p.display_name,
            user_id=current_user.id,
        ) from e
    except GitProviderError as e:
        # 平台明确拒绝(4xx):token 可能已失效,提示用户重新绑定
        logger.warning(
            "user %s 的 %s 仓库列表被平台拒绝(→ 401): %s",
            current_user.id, p.display_name, e,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{p.display_name} token 失效,请重新绑定: {e}",
        ) from e
    except ValueError as e:
        logger.error("user %s 的 %s token 解密失败: %s", current_user.id, provider, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="token 解密失败,请联系管理员",
        ) from e

    # 仅缓存成功结果(401/403/500 不缓存,让下次请求能重试)
    _REPOS_CACHE[cache_key] = (time.monotonic(), raw)
    repos = [GitRepoItem(**item) for item in raw]
    return GitReposResponse(repos=repos)


@router.post("/{provider}/refresh", response_model=GitProviderStatusResponse)
def refresh_provider_token(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """手动用 refresh_token 刷新 access_token

    仅支持 supports_token_refresh 的平台(Gitee);GitHub 返回 400。
    用于设置页「刷新 token」按钮:token 过期或用户主动续期时调用。
    """
    p = _resolve_provider(provider)
    if not p.supports_token_refresh:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{p.display_name} token 不支持刷新",
        )

    binding = _get_binding(db, current_user.id, provider)
    if not binding or not binding.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"尚未绑定 {p.display_name}",
        )
    if not binding.refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无 refresh_token,请重新绑定",
        )

    try:
        new_set = p.refresh_access_token(decrypt_secret(binding.refresh_token))
    except GitProviderUnavailable as e:
        # 链路/平台故障时 refresh_token 并未被平台判定无效,
        # 提示"请重新绑定"会误导(而且重绑要重新走授权页)
        raise http_exc_for_git_error(
            e,
            action=f"刷新 {p.display_name} token",
            provider_display=p.display_name,
            user_id=current_user.id,
        ) from e
    except GitProviderError as e:
        logger.warning(
            "user %s 手动刷新 %s token 失败(→ 401): %s",
            current_user.id, p.display_name, e,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"刷新失败,请重新绑定: {e}",
        ) from e
    except ValueError as e:
        # decrypt_secret 失败:服务端密钥与库里密文不匹配
        logger.error(
            "user %s 的 %s refresh_token 解密失败: %s",
            current_user.id, p.display_name, e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="token 解密失败,请联系管理员",
        ) from e

    binding.access_token = encrypt_secret(new_set.access_token)
    binding.refresh_token = (
        encrypt_secret(new_set.refresh_token) if new_set.refresh_token else ""
    )
    binding.expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=new_set.expires_in)
        if new_set.expires_in else None
    )
    db.commit()
    db.refresh(binding)
    logger.info(
        "user %s 手动刷新 %s token 成功", current_user.id, p.display_name
    )

    return GitProviderStatusResponse(
        provider=provider,
        bound=True,
        provider_user_id=binding.provider_user_id,
        token_expires_at=binding.expires_at,
        token_expired=False,
        token_refreshable=bool(binding.refresh_token),
    )
