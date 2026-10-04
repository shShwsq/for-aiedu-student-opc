"""用户级记忆生成设置 (per-user, 1:1)

控制「任务完成后自动归纳写入长期记忆」的行为:
- memory_enabled:是否启用自动归纳(关闭则任务完成不再写记忆;手改记忆不受影响)
- curator_llm_config_id:记忆归纳/精简专用模型(UserLLMConfig.llm_configs 中某条配置 id),
  None=未指定,回退 env 默认。独立于任务自带模型(不借用 task.llm_config_id)
- thinking_mode:归纳/精简的思考模式覆盖(follow=跟随模型配置/on/off=强制开/关),
  默认 follow
- structure_mode:结构化预设模式,见 MEMORY_STRUCTURE_MODES
- inject_max_chars:精简版记忆注入 system prompt 的字符上限(超出调 LLM 精简/截断)

设计:与 AgentPolicy / PracticeSettings 同构 —— 1:1 结构化列(不用 JSONB),
全部非空带 server_default(等于系统默认),保存接口总是全字段写入无"部分保存"状态。
全新表随 Base.metadata.create_all 建好;无历史数据,无需迁移函数。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 结构化预设模式(后端固定枚举):
# - structured:按用户自定义类别(标题+描述)分类,JSON 归纳 + 按类别去重合并(默认)
# - freeform:自由叙述纯文本,追加合并,不做类别去重
MEMORY_STRUCTURE_MODES = ("structured", "freeform")
DEFAULT_MEMORY_STRUCTURE_MODE = "structured"

# 精简版注入字符上限默认值(与 memory_summarize.MAX_PROJECT_MEM_INJECT 对齐)
DEFAULT_MEMORY_INJECT_MAX_CHARS = 2000
# 结构化类别数量上限(防提示词膨胀)
MAX_MEMORY_CATEGORIES = 12


class MemorySettings(Base):
    __tablename__ = "memory_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 是否启用任务完成后自动归纳写入记忆(默认开)
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # 记忆归纳/精简专用模型(UserLLMConfig.llm_configs 中某条配置 id;None=未指定,回退 env 默认)
    curator_llm_config_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    # 归纳/精简思考模式覆盖:follow=跟随模型配置,on/off=强制开/关(默认 follow)
    thinking_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="follow", default="follow",
    )
    # 结构化预设模式(见 MEMORY_STRUCTURE_MODES)
    structure_mode: Mapped[str] = mapped_column(
        String(16), nullable=False,
        server_default=DEFAULT_MEMORY_STRUCTURE_MODE,
        default=DEFAULT_MEMORY_STRUCTURE_MODE,
    )
    # 结构化类别定义(项目记忆):list[{title, description}],按顺序即优先级,末位为杂项桶。
    # 空列表时服务层回退内置默认(DEFAULT_PROJECT_CATEGORY_DEFS)。
    project_categories: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # 结构化类别定义(全局记忆):同上;空回退 DEFAULT_GLOBAL_CATEGORY_DEFS。
    global_categories: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # 精简版记忆注入 system prompt 的字符上限(超出调 LLM 精简,失败兜底硬截断)
    inject_max_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=str(DEFAULT_MEMORY_INJECT_MAX_CHARS),
        default=DEFAULT_MEMORY_INJECT_MAX_CHARS,
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


def migrate_memory_add_category_columns() -> None:
    """幂等给 memory_settings 补 project_categories / global_categories 列。

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列。
    若开发库已按旧版(无这两个 JSONB 列)建过 memory_settings,需显式 ALTER 补齐。
    全新库(create_all 已建好新列)或已迁过 → 直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("memory_settings"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("memory_settings")}
        if "project_categories" not in cols:
            conn.execute(text(
                "ALTER TABLE memory_settings ADD COLUMN project_categories "
                "JSONB NOT NULL DEFAULT '[]'"
            ))
            log.info("memory_settings.project_categories 列迁移完成")
        if "global_categories" not in cols:
            conn.execute(text(
                "ALTER TABLE memory_settings ADD COLUMN global_categories "
                "JSONB NOT NULL DEFAULT '[]'"
            ))
            log.info("memory_settings.global_categories 列迁移完成")
        conn.commit()
