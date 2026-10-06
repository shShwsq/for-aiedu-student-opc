"""用户级 agent 智能体策略 (per-user, 1:1)

agent2(质检智能体)智能体策略的用户级默认(启停、协作轮次、验证权限等),
任务级可通过 task.params["_agent_policy"] 覆盖。
字段语义见 agent_policy.DEFAULT_AGENT_POLICY。

设计:
- 1:1 表(user_id unique),用户首次保存时 get_or_create
- 结构化列(不再用 JSONB):全部字段非空带 server_default(= DEFAULT_AGENT_POLICY)
- 保存接口(PUT /memory/preferences/agent_policy)总是全字段写入,无"部分保存"状态

迁移:老数据存于 user_preferences.agent_policy JSONB 列,
migrate_agent_policy_table() 启动时把数据拷入本表后删除旧列(幂等)。
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentPolicy(Base):
    __tablename__ = "agent_policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 是否启用 agent2(关闭=单 agent 模式,跳过评估/验证)
    agent2_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # agent2 是否能调用 verifier_agent 验证(需任务配了 test_env_url)
    allow_verify: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # agent2 是否能调用 check_reference 复核 agent1 引用的网址
    # (后端安全抓取,SSRF 防护;独立于 repo_path / test_env_url)
    allow_reference_check: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # 验证授权默认模式("direct" 直接执行 / "per_action" 逐动作授权)
    verifier_auth_mode_default: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="per_action", default="per_action"
    )
    # AI助手确认策略默认模式("always_approve" / "per_command")
    executor_command_confirm_default: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="always_approve",
        default="always_approve",
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

    def to_dict(self) -> dict[str, Any]:
        """转成与 DEFAULT_AGENT_POLICY 键对齐的 dict(resolve_agent_policy 合并用)"""
        return {
            "agent2_enabled": self.agent2_enabled,
            "allow_verify": self.allow_verify,
            "allow_reference_check": self.allow_reference_check,
            "verifier_auth_mode_default": self.verifier_auth_mode_default,
            "executor_command_confirm_default": self.executor_command_confirm_default,
        }


# ============================================================
# 迁移:老 JSONB 数据 → 独立表
# ============================================================


def normalize_policy_dict(
    raw: dict | None, defaults: dict[str, Any],
) -> dict[str, Any]:
    """把原始策略 dict(可能缺字段/类型错乱)规整为全字段 dict。

    迁移老 user_preferences.agent_policy JSONB 用:逐字段做类型防御,
    非法值回退 defaults 对应值。老数据里的检查点/打断/max_rounds 键直接丢弃
    (功能已移除)。
    """
    raw = raw if isinstance(raw, dict) else {}

    def _bool(key: str) -> bool:
        v = raw.get(key)
        return v if isinstance(v, bool) else defaults[key]

    def _enum(key: str, allowed: tuple[str, ...]) -> str:
        v = raw.get(key)
        return v if v in allowed else defaults[key]

    return {
        "agent2_enabled": _bool("agent2_enabled"),
        "allow_verify": _bool("allow_verify"),
        # 老数据无此键 → 回退 defaults(True)
        "allow_reference_check": _bool("allow_reference_check"),
        "verifier_auth_mode_default": _enum("verifier_auth_mode_default", ("direct", "per_action")),
        "executor_command_confirm_default": _enum(
            "executor_command_confirm_default", ("always_approve", "per_command")
        ),
    }


def migrate_agent_policy_table() -> None:
    """幂等迁移:把 user_preferences.agent_policy JSONB 拷入 agent_policies 表,
    完成后删除旧列。

    背景:项目用 Base.metadata.create_all(无 Alembic),新表随 create_all 建好,
    但老库的数据还在 user_preferences.agent_policy 列里。

    - user_preferences 不存在 / 无 agent_policy 列(全新库或已迁过)→ 直接返回
    - 拷贝用 INSERT ... ON CONFLICT (user_id) DO UPDATE,中途失败重跑安全
    - 老数据经 normalize_policy_dict 规整(缺字段回退 DEFAULT,与老解析语义等价:
      老 resolve 里缺失键也是回退 DEFAULT_AGENT_POLICY)
    """
    import logging

    from sqlalchemy import inspect, text

    from app.agent_policy import DEFAULT_AGENT_POLICY
    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_preferences"):
            return  # 全新库,无老数据
        cols = {c["name"] for c in insp.get_columns("user_preferences")}
        if "agent_policy" not in cols:
            return  # 全新库(新 model 无此列)或已迁过

        rows = conn.execute(
            text(
                "SELECT user_id, agent_policy FROM user_preferences "
                "WHERE agent_policy IS NOT NULL"
            )
        ).fetchall()
        for user_id, raw in rows:
            if not isinstance(raw, dict):
                continue  # 脏数据跳过(等价于老行为:非 dict 不参与合并)
            d = normalize_policy_dict(raw, DEFAULT_AGENT_POLICY)
            conn.execute(
                text(
                    """
                    INSERT INTO agent_policies (
                        id, user_id, agent2_enabled,
                        allow_verify, allow_reference_check,
                        verifier_auth_mode_default,
                        executor_command_confirm_default
                    ) VALUES (
                        :id, :user_id, :agent2_enabled,
                        :allow_verify, :allow_reference_check,
                        :verifier_auth_mode_default,
                        :executor_command_confirm_default
                    )
                    ON CONFLICT (user_id) DO UPDATE SET
                        agent2_enabled = EXCLUDED.agent2_enabled,
                        allow_verify = EXCLUDED.allow_verify,
                        allow_reference_check = EXCLUDED.allow_reference_check,
                        verifier_auth_mode_default = EXCLUDED.verifier_auth_mode_default,
                        executor_command_confirm_default = EXCLUDED.executor_command_confirm_default,
                        updated_at = now()
                    """
                ),
                {"id": str(uuid.uuid4()), "user_id": user_id, **d},
            )
        conn.execute(
            text("ALTER TABLE user_preferences DROP COLUMN agent_policy")
        )
        conn.commit()
        log.info(
            "agent_policy 迁移完成: %d 条记录拷入 agent_policies,旧列已删除",
            len(rows),
        )


def migrate_agent_policy_add_reference_check_column() -> None:
    """幂等迁移:agent_policies 表补 allow_reference_check 列

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会
    自动加新列。模型新增 allow_reference_check(引用复核开关,默认 true),
    老库需显式 ALTER 补列;不回写已有行数据(server_default 兜底 true)。

    - 表不存在(全新库,create_all 已带新列)→ 直接返回
    - 列已存在 → 直接返回(幂等)
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("agent_policies"):
            return
        cols = {c["name"] for c in insp.get_columns("agent_policies")}
        if "allow_reference_check" in cols:
            return
        conn.execute(text(
            "ALTER TABLE agent_policies ADD COLUMN allow_reference_check "
            "BOOLEAN NOT NULL DEFAULT true"
        ))
        conn.commit()
        log.info("agent_policies.allow_reference_check 列已补齐")


def migrate_agent_policy_rename_columns() -> None:
    """幂等重命名 agent_policies 旧列,对齐 model(修复 /memory/preferences 500)

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会改列名。
    老库 agent_policies 建表时启用开关列名为 user_agent_enabled(旧命名),
    后来模型统一改为 agent2_enabled(与 DEFAULT_AGENT_POLICY / API 契约对齐),
    ORM SELECT 找不到列 → GET/PUT /memory/preferences 500,智能体策略页加载失败。

    - user_agent_enabled 存在且 agent2_enabled 不存在 → RENAME(保留数据)
    - 全新库(已是新列名)或已迁过 → 直接返回
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("agent_policies"):
            return
        cols = {c["name"] for c in insp.get_columns("agent_policies")}
        if "user_agent_enabled" in cols and "agent2_enabled" not in cols:
            conn.execute(text(
                "ALTER TABLE agent_policies RENAME COLUMN user_agent_enabled "
                "TO agent2_enabled"
            ))
            log.info("agent_policies.user_agent_enabled → agent2_enabled 列重命名完成")
        conn.commit()


def migrate_agent_policy_drop_max_rounds_column() -> None:
    """幂等删除 agent_policies 的 max_rounds 旧列

    背景:agent2 审查移到后台执行后,初始运行只有 1 轮 agent1、多轮由用户
    驱动(resume),"协作总轮次"设置不再有生效方,模型不再映射该列。
    create_all 不会删已存在的列,老库需显式 DROP。列已删(全新库)时直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("agent_policies"):
            return
        cols = {c["name"] for c in insp.get_columns("agent_policies")}
        if "max_rounds" not in cols:
            return  # 全新库或已迁过
        conn.execute(text("ALTER TABLE agent_policies DROP COLUMN max_rounds"))
        conn.commit()
    log.info("agent_policies.max_rounds 旧列已删除(协作总轮次设置移除)")


# 检查点/打断功能移除后要删除的旧列(存在才删,幂等)
_DROPPED_CHECKPOINT_COLUMNS = (
    "checkpoint_interval",
    "checkpoint_interval_builtin",
    "checkpoint_interval_cli",
    "allow_interrupt",
    "max_interrupts_per_round",
)


def migrate_agent_policy_drop_checkpoint_columns() -> None:
    """幂等删除 agent_policies 的检查点/打断旧列

    背景:检查点评估与打断功能已整体移除,模型不再映射这 5 列;
    create_all 不会删已存在的列,老库需显式 DROP。列数据随功能废弃,
    无保留价值。列已删(全新库)时直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("agent_policies"):
            return
        cols = {c["name"] for c in insp.get_columns("agent_policies")}
        dropped = [c for c in _DROPPED_CHECKPOINT_COLUMNS if c in cols]
        for col in dropped:
            conn.execute(text(f"ALTER TABLE agent_policies DROP COLUMN {col}"))
        if dropped:
            conn.commit()
            log.info(f"agent_policies 检查点/打断旧列已删除: {', '.join(dropped)}")


def migrate_conversations_drop_checkpoint_records() -> None:
    """幂等删除 conversations 里的检查点评估/中断历史记录

    背景:检查点评估与打断功能已移除,前端聚合侧栏与报告过滤同步删除。
    历史记录是 agent2 过程性评估内容(机器生成,非用户数据),保留只会让
    前端主对话流出现无法路由的孤儿消息。按 content 前缀匹配删除:
    - "[检查点评估" —— 检查点评估记录(type=evaluation)
    - "[检查点中断" —— CLI 软中断生效时落库的追问卡片(type=evaluation)

    无匹配记录时 DELETE 0 行,天然幂等。
    """
    import logging

    from sqlalchemy import text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        result = conn.execute(text(
            "DELETE FROM conversations "
            "WHERE role = 'agent2' AND type = 'evaluation' "
            "AND (content LIKE '[检查点评估%' OR content LIKE '[检查点中断%')"
        ))
        conn.commit()
        if result.rowcount:
            log.info(
                f"conversations 检查点评估/中断历史记录已清理: {result.rowcount} 条"
            )
