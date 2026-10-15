"""用户模型配置(per-user)

阶段 6:用户可在前端配置多个 LLM / Embedding 模型,配置存于数据库,
任务执行时按 task.llm_config_id 选择对应配置,覆盖默认 env 配置。

设计:
- 一对一关联 User(每个用户一行,首次保存时 upsert)
- llm_configs / embedding_configs 用 JSONB 存列表,每个元素结构:
  { id, name, provider, api_key, model, enable_thinking, base_url, max_output_tokens }
- api_key 字段级加密:落库的 api_key 是 enc:v1 信封密文(见 app.security
  encrypt_secret_envelope),读取时由 decrypt_config_api_keys 解密为明文。历史明文
  行(无前缀)原样透传,兼容未迁移数据;密钥来自 settings.GITHUB_TOKEN_SECRET。
"""
import logging
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def decrypt_config_api_keys(configs: list) -> list[dict]:
    """返回 api_key 已解密为明文的浅拷贝列表(不改动原对象)。

    适用于 llm_configs / embedding_configs 两类列表(故名为 config 而非 llm)。
    为什么要拷贝而不是就地改:入参通常是 ORM JSONB 列加载出来的列表,就地
    把密文改成明文会被 SQLAlchemy 当变更跟踪,意外 commit 会把明文写回库。
    无前缀(历史明文/空值)的项原样返回。仅处理 dict 项,非 dict 项透传。
    """
    from app.security import decrypt_secret_envelope

    out: list[dict] = []
    for c in configs or []:
        if isinstance(c, dict) and c.get("api_key"):
            c = {**c, "api_key": decrypt_secret_envelope(c["api_key"])}
        out.append(c)
    return out


class UserLLMConfig(Base):
    __tablename__ = "user_llm_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 一对一关联用户
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    # LLM 配置列表(可多个)
    # 每个元素: { id, name, provider, api_key, model, enable_thinking, base_url,
    #            max_output_tokens(可选,单次输出上限) }
    # 为空列表表示未配置,任务执行时回退到 env 默认配置
    llm_configs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Embedding 配置列表(可多个)
    # 每个元素: { id, name, provider, api_key, model, base_url, dimension }
    # 当前阶段仅存储 + 测试连通性,尚未接入向量检索流程
    embedding_configs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def _encrypt_keys_in_list(configs):
    """对列表里每个明文 api_key 加密(enc:v1 信封)。

    返回 (新列表, 本次加密条数)。已带前缀的跳过(幂等);非 dict / 空 api_key 透传。
    """
    from app.security import encrypt_secret_envelope, is_enveloped

    if not isinstance(configs, list):
        return configs, 0
    count = 0
    out = []
    for c in configs:
        if isinstance(c, dict) and c.get("api_key") and not is_enveloped(c["api_key"]):
            c = {**c, "api_key": encrypt_secret_envelope(c["api_key"])}
            count += 1
        out.append(c)
    return out, count


def migrate_encrypt_llm_api_keys() -> None:
    """把 user_llm_configs 里 llm_configs/embedding_configs 的历史明文 api_key 就地加密。

    背景:api_key 早期明文存于 JSONB(与 password_hash 同表),现改为 enc:v1 信封
    密文落库。本函数一次性把存量明文加密;写入端(_merge_llm_configs)此后直接存密文。

    空密钥跳过:GITHUB_TOKEN_SECRET 留空时(开发默认),用临时随机 key 加密会得到
    「重启即不可解」的死密文,故此时直接跳过不落库,避免把存量明文毁成坏数据。

    幂等:已带 enc:v1: 前缀的值跳过(重跑不会二次加密);表不存在 / 无行直接返回。
    失败会导致启动失败(fail-fast,有意为之):lifespan 对迁移调用不做 try/except,
    抛异常即崩。带明文密钥长期运行比静默降级更危险,所以不吞异常。
    """
    import json

    from sqlalchemy import inspect, text

    from app.config import settings
    from app.database import engine

    log = logging.getLogger(__name__)

    if not settings.GITHUB_TOKEN_SECRET:
        # 宁存明文不存死密文:无持久密钥时不迁移,固定密钥后重启会自动补做
        log.warning(
            "GITHUB_TOKEN_SECRET 为空,跳过 api_key 加密迁移(临时密钥会把存量明文"
            "加密成重启即不可解的死密文)。请在 .env 固定 Fernet 密钥后重启。"
        )
        return

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_llm_configs"):
            return
        rows = conn.execute(
            text("SELECT id, llm_configs, embedding_configs FROM user_llm_configs")
        ).fetchall()
        migrated_rows = 0
        migrated_keys = 0
        for row_id, llm_cfgs, emb_cfgs in rows:
            new_llm, c1 = _encrypt_keys_in_list(llm_cfgs)
            new_emb, c2 = _encrypt_keys_in_list(emb_cfgs)
            if not (c1 or c2):
                continue  # 全已是密文或无 key,不写回
            conn.execute(
                text(
                    "UPDATE user_llm_configs "
                    "SET llm_configs = CAST(:llm AS jsonb), "
                    "embedding_configs = CAST(:emb AS jsonb), updated_at = now() "
                    "WHERE id = :id"
                ),
                {
                    "id": row_id,
                    "llm": json.dumps(new_llm if new_llm is not None else []),
                    "emb": json.dumps(new_emb if new_emb is not None else []),
                },
            )
            migrated_rows += 1
            migrated_keys += c1 + c2
        conn.commit()
        if migrated_keys:
            log.info(
                "user_llm_configs api_key 加密迁移: %d 行 / %d 个 key 已加密",
                migrated_rows, migrated_keys,
            )
