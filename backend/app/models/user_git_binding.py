"""用户 Git 平台绑定模型(per-user, per-provider)

把用户与 Git 托管平台(GitHub / Gitee)的关联统一收敛到一张表,取代原先散在
users 表上的 github_id / github_access_token 两列。每个用户每个 provider 最多
一行;每个平台账号(provider_user_id)全局只能绑一个本地用户。

语义(对齐原 GitHub 设计):
- access_token 为空串:仅用 OAuth 登录过(scope 不含仓库),未授权仓库访问
- access_token 非空:已显式绑定(scope 含仓库),任务执行可克隆私有仓库
  (status.bound = bool(access_token))

加密:access_token 用 app.security.encrypt_secret / decrypt_secret 加解密,
密钥来自 settings.GITHUB_TOKEN_SECRET(Fernet,与 provider 无关)。

级联删除:user_id 外键 ondelete=CASCADE,删用户时自动清掉绑定。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserGitBinding(Base):
    __tablename__ = "user_git_bindings"
    __table_args__ = (
        # 一个平台账号只能绑一个本地用户
        UniqueConstraint("provider", "provider_user_id", name="uq_provider_user_id"),
        # 一个用户每个 provider 只能绑一个
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "github" | "gitee"
    provider: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # 平台用户 ID(GitHub id / Gitee id),字符串
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 平台用户名(login),绑定时一次性写入并缓存,避免 status 接口每次都调 /user
    provider_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 平台头像 URL,绑定时一次性写入并缓存
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # access_token 加密密文(Fernet base64);空串表示仅登录未授权仓库
    access_token: Mapped[str] = mapped_column(
        String(2048), nullable=False, server_default=""
    )
    # refresh_token 加密密文(Fernet base64);空串表示不支持刷新(GitHub)或老数据
    refresh_token: Mapped[str] = mapped_column(
        String(2048), nullable=False, server_default=""
    )
    # access_token 过期时间;None 表示不过期(GitHub)或老数据未记录
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # 反向关系:与 User.git_bindings 成对(/auth/oauth/{provider} 登录用
    # binding.user 取回本地用户)。缺这一侧时访问 binding.user 直接抛
    # AttributeError → 接口 500,已有绑定行的用户永远登录不了。
    # 删除用户仍由 FK ondelete=CASCADE 在数据库层级联(passive_deletes 在父侧声明)。
    user: Mapped["User"] = relationship("User", back_populates="git_bindings")


def migrate_legacy_github_bindings() -> None:
    """一次性把旧 users.github_id / github_access_token 搬到 user_git_bindings

    背景:User 模型已移除 github_id / github_access_token 两列,但旧库物理列仍在
    (SQLAlchemy 不再映射)。启动时把仍存在的 GitHub 关联迁到新表,避免丢绑定。

    幂等:若 user_git_bindings 已有任意 github 行,直接返回(已迁过)。
    非破坏:不删除旧物理列(留作孤儿,避免 DB_REBUILD 丢数据)。
    旧库无 github_id 列(全新库)时直接返回。
    """
    import uuid as _uuid
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("users"):
            return
        users_cols = {c["name"] for c in insp.get_columns("users")}
        # 全新库或已无旧列 → 无需迁移
        if "github_id" not in users_cols:
            return
        # 已迁过(有任意 github binding)→ 跳过
        already = conn.execute(
            text("SELECT COUNT(*) FROM user_git_bindings WHERE provider = :p"),
            {"p": "github"},
        ).scalar()
        if already:
            return

        rows = conn.execute(
            text(
                "SELECT id, github_id, github_access_token FROM users "
                "WHERE github_id IS NOT NULL AND github_id <> ''"
            )
        ).fetchall()
        if not rows:
            return

        for uid, gh_id, gh_token in rows:
            # access_token 旧列可能为 NULL 或空串,统一成空串(仅登录未授权仓库)
            token = gh_token or ""
            conn.execute(
                text(
                    "INSERT INTO user_git_bindings (id, user_id, provider, provider_user_id, access_token) "
                    "VALUES (:id, :uid, :provider, :pid, :token)"
                ),
                {
                    "id": str(_uuid.uuid4()),
                    "uid": str(uid),
                    "provider": "github",
                    "pid": gh_id,
                    "token": token,
                },
            )
        conn.commit()
        log.info("迁移完成: %d 条旧 GitHub 绑定搬到 user_git_bindings", len(rows))


def add_refresh_token_columns() -> None:
    """幂等给 user_git_bindings 加 refresh_token / expires_at 两列

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列。
    启动时检查缺失列并 ALTER TABLE ADD COLUMN,保证老库平滑升级。
    全新库(create_all 已建好新列)或已迁过 → 直接返回。

    老数据 refresh_token="" / expires_at=NULL,退化为「不刷新」,
    行为与改动前一致(下游 _ensure_valid_token 遇到 None/空串直接返回原 token)。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_git_bindings"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("user_git_bindings")}
        add_clauses = []
        if "refresh_token" not in cols:
            add_clauses.append(
                "ADD COLUMN refresh_token VARCHAR(2048) NOT NULL DEFAULT ''"
            )
        if "expires_at" not in cols:
            add_clauses.append(
                "ADD COLUMN expires_at TIMESTAMP WITH TIME ZONE NULL"
            )
        if not add_clauses:
            return  # 已迁过
        # PG 要求多个 ALTER 动作用逗号分隔:ADD COLUMN ..., ADD COLUMN ...
        conn.execute(
            text(f"ALTER TABLE user_git_bindings {', '.join(add_clauses)}")
        )
        conn.commit()
        log.info("user_git_bindings 加列: %s", ", ".join(add_clauses))


def add_login_avatar_columns() -> None:
    """幂等给 user_git_bindings 加 provider_login / avatar_url 两列

    背景:status 接口原先每次都实时调 GitHub/Gitee /user API 拿 login+avatar,
    导致提交任务页加载耗时数秒(等外部网络)。改为绑定时一次性写入这两列,
    status 接口直接读缓存,不再走外部网络。老数据这两列为 NULL,
    status 接口会懒加载一次回填,之后不再调用 /user。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_git_bindings"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("user_git_bindings")}
        add_clauses = []
        if "provider_login" not in cols:
            add_clauses.append(
                "ADD COLUMN provider_login VARCHAR(255) NULL"
            )
        if "avatar_url" not in cols:
            add_clauses.append(
                "ADD COLUMN avatar_url VARCHAR(512) NULL"
            )
        if not add_clauses:
            return  # 已迁过
        conn.execute(
            text(f"ALTER TABLE user_git_bindings {', '.join(add_clauses)}")
        )
        conn.commit()
        log.info("user_git_bindings 加列: %s", ", ".join(add_clauses))
