"""用户模型

邮箱密码注册登录,Git 平台(GitHub / Gitee)关联后续作为可选绑定(见 spec 8.5),
关联数据存于 user_git_bindings 表(见 models/user_git_binding.py)。
阶段 6 实现:JWT 鉴权 + 邮箱验证 + 重置密码 + Git 平台 OAuth
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    # bcrypt 加盐哈希
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 邮箱验证时间(为空表示未验证,登录受限)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Git 平台绑定(GitHub / Gitee)已迁移到 user_git_bindings 表
    # (旧库可能仍残留 github_id / github_access_token 物理列,模型不再映射,
    #  由启动时 migrate_legacy_github_bindings 一次性搬到 user_git_bindings)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Git 平台绑定(GitHub / Gitee),一对多(每用户每 provider 一行)
    # 反向侧见 UserGitBinding.user(必须成对,否则 binding.user 取不到用户)
    # 级联删除由 FK ondelete=CASCADE 在数据库层完成;passive_deletes=True 让 ORM 不额外发 DELETE
    git_bindings: Mapped[list["UserGitBinding"]] = relationship(
        "UserGitBinding",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )

    # 便捷属性(非数据库字段)
    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None
