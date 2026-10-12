"""用户级「模型槽位」默认分配(per-user, 1:N)

一个「槽位(slot)」= 系统里一处会调 LLM 的任务(任务审查 / 出题 / 讲解 / 记忆归纳),
槽位行记录该任务默认用哪条 UserLLMConfig 配置(以及该任务的思考模式覆盖)。

为什么单独一张表:模型选择原本散在 practice_settings.default_llm_config_id /
explain_llm_config_id、memory_settings.curator_llm_config_id 三个业务表列上,
再加「任务默认审查模型」就得再开列;表驱动后新增一处任务只加一个 slot 常量。

设计:
- UNIQUE(user_id, slot):每用户每槽位一行,首次保存 get_or_create
- llm_config_id 存 UserLLMConfig.llm_configs 里某条配置的 id(UUID 字符串),
  **不加外键**:配置列表整体是 JSONB,删除配置时由解析链回退 env(见 model_slots)
- llm_config_id 三态(别混用,否则会掉进「清空后重启又长回来」的坑):
  非空字符串=生效的配置 id;''=用户显式清空过(解析链回退 env,迁移不覆盖);
  NULL=这行还没做过模型决定(只为思考模式建的行,或迁移还没跑)→ 读路径继续兜底旧列
- thinking_mode 为该槽位的思考模式覆盖(follow/on/off),默认 follow

迁移:老数据在 practice_settings / memory_settings 的列上,
migrate_model_slot_defaults() 启动时拷入本表(幂等,不删旧列);
旧列此后不再被写入,只作为「新表无行」时的读取兜底(详见 services/model_slots)。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 槽位标识(与 app/services/model_slots.py 的注册表同源,改名需同步迁移历史行)
SLOT_REVIEW_DEFAULT = "review_default"
SLOT_PRACTICE_GENERATE = "practice_generate"
SLOT_PRACTICE_EXPLAIN = "practice_explain"
SLOT_MEMORY_CURATOR = "memory_curator"

MODEL_SLOT_IDS = (
    SLOT_REVIEW_DEFAULT,
    SLOT_PRACTICE_GENERATE,
    SLOT_PRACTICE_EXPLAIN,
    SLOT_MEMORY_CURATOR,
)


class ModelSlotDefault(Base):
    __tablename__ = "model_slot_defaults"
    __table_args__ = (
        # 每用户每槽位只能有一行
        UniqueConstraint("user_id", "slot", name="uq_model_slot_user_slot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 注意:不用 unique=True(1:N 表),user_id 只建普通索引
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 槽位标识,取值见 SLOT_* 常量
    slot: Mapped[str] = mapped_column(String(48), nullable=False)
    # 该槽位默认使用的模型配置 id(UserLLMConfig.llm_configs 中某条)
    # 三态:非空=配置 id;''=已清空(回退 env 且不被迁移刷回);NULL=没做过决定(兜底旧列)
    llm_config_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    # 思考模式覆盖:follow=跟随模型配置自身开关,on/off=强制开/关(默认 follow)
    thinking_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="follow", default="follow",
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


def migrate_model_slot_defaults() -> None:
    """幂等迁移:把业务表上的模型列拷入 model_slot_defaults,旧列保留不删。

    背景:项目用 Base.metadata.create_all(无 Alembic),新表随 create_all 建好,
    但老库里「默认出题模型 / 讲解模型 / 记忆归纳模型」还存着
    practice_settings / memory_settings 的列上。

    - 新表不存在(理论不会发生,建表在前)或无候选行 → 直接返回
    - 逐行 INSERT ... ON CONFLICT (user_id, slot) DO UPDATE,且只在目标行
      llm_config_id IS NULL 时覆盖:重跑不会把用户在新表上的选择刷回旧值
    - 这里的 IS NULL 同时是「清空不会被复活」的保护:用户清空过的行存的是空串
      ``''``而不是 NULL,条件不成立,所以下次重启不会把旧列值填回去
    - 无模型且思考模式为默认 follow 的行不拷(等价于「未设置」,不产生空行)
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    # (源表, 模型列, 思考模式列, 目标 slot)
    sources = (
        ("practice_settings", "default_llm_config_id", "thinking_mode_for_practice",
         SLOT_PRACTICE_GENERATE),
        ("practice_settings", "explain_llm_config_id", None, SLOT_PRACTICE_EXPLAIN),
        ("memory_settings", "curator_llm_config_id", "thinking_mode", SLOT_MEMORY_CURATOR),
    )

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("model_slot_defaults"):
            return  # 新表未建(启动顺序异常),本次不迁,下次重启再试
        for table, llm_col, mode_col, slot in sources:
            if not insp.has_table(table):
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if llm_col not in cols:
                continue
            # 候选:设了模型,或(有思考列时)思考模式非默认 follow
            where = f"{llm_col} IS NOT NULL"
            if mode_col and mode_col in cols:
                where += f" OR {mode_col} <> 'follow'"
                select_mode = mode_col
            else:
                select_mode = "'follow'"
            rows = conn.execute(
                text(
                    f"SELECT user_id, {llm_col}, {select_mode} "
                    f"FROM {table} WHERE {where}"
                )
            ).fetchall()
            copied = 0
            for user_id, config_id, thinking_mode in rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO model_slot_defaults
                            (id, user_id, slot, llm_config_id, thinking_mode,
                             created_at, updated_at)
                        VALUES
                            (:id, :user_id, :slot, :llm_config_id, :thinking_mode,
                             now(), now())
                        ON CONFLICT (user_id, slot) DO UPDATE
                        SET llm_config_id = EXCLUDED.llm_config_id,
                            thinking_mode = EXCLUDED.thinking_mode,
                            updated_at = now()
                        WHERE model_slot_defaults.llm_config_id IS NULL
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "user_id": user_id,
                        "slot": slot,
                        "llm_config_id": config_id,
                        "thinking_mode": thinking_mode or "follow",
                    },
                )
                copied += 1
            if copied:
                log.info(
                    "model_slot_defaults 迁移: %s.%s → slot=%s(%d 行,旧列保留)",
                    table, llm_col, slot, copied,
                )
        conn.commit()
