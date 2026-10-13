"""任务模型

通用任务模型,支持多场景。场景标识为字符串,可任意注册新场景。
"""
import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaskStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    # 用户暂停:后台线程在检查点阻塞,等待恢复
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewStatus(str, PyEnum):
    """后台审查状态(agent2 审查移到后台后的子状态)

    agent1 结束即任务 COMPLETED,agent2 的整个检查在此状态下后台执行:
    - running: 审查进行中(前端侧栏显示"检查中"角标,SSE 持续接收审查事件)
    - done:    审查完成(重点与知识点已替换临时结果)
    - failed:  审查失败/降级(保留 agent1 summary 临时结果,不影响任务状态)
    - stopped: 用户主动终止检查(保留临时结果,审查后下游链整体跳过)
    NULL:      未审查(单 agent 模式 / 老任务)
    """
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 阶段 0 暂不鉴权,user_id 可空
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )

    # 场景标识:字符串。场景已降级为"快捷模板"(预填提示词 + 推荐 skill),
    # 不再硬编码 checklist/prompt/工具白名单。默认 "general" 表示未选模板
    scenario: Mapped[str] = mapped_column(
        String(64), default="general", nullable=False
    )
    # 任务标题:用户可自定义,便于在历史列表中识别;为空时前端用 user_input 截断展示
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 用户原始输入(意图)。通用化:不再固定 repo_url,而是 user_input
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    # 可选的补充参数(如 repo_url、branch、scope 等),放 metadata
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 用户在创建任务时选择的允许调用的 skill 名称列表。
    # None 或空列表表示全部 skill 可用(默认);非空时 react_agent 的 skill 工具按此过滤
    allowed_skills: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False
    )
    # 当前阶段描述,展示给前端
    current_stage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 失败时的错误信息
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 用户提交任务时选择的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id)
    # 语义:agent2 评估使用的模型;为空表示用 env 默认配置或匿名任务
    llm_config_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 内置 react_agent 使用的 LLM 配置 id(仅 executor=builtin 时生效)。
    # 为空时回退到 llm_config_id(react_agent 与 agent2 共用同一模型)。
    # 外部 CLI 执行器忽略此字段(模型由 CLI 账号配额管理)。
    # 升级时需手动执行:ALTER TABLE tasks ADD COLUMN react_llm_config_id VARCHAR(64);
    react_llm_config_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 执行器选择:"builtin"(默认,内置 react_agent)或 registry 中已注册的 agent_type(如 "qoder_cli")
    # 决定 orchestrator 调用哪个 ExecutorAgent provider 执行 react 角色的任务
    # server_default 保证旧表新增列时已有行回填 "builtin"(create_all 不改已存在表,
    # 升级时需手动执行:ALTER TABLE tasks ADD COLUMN executor VARCHAR(32)
    #   NOT NULL DEFAULT 'builtin';)
    executor: Mapped[str] = mapped_column(
        String(32), default="builtin", server_default="builtin", nullable=False
    )

    # 后台审查状态(agent2 审查移到后台后的子状态,见 ReviewStatus)
    # agent1 结束即任务 COMPLETED,本字段表达"审查进行到哪一步":
    # running(审查中)/done(完成)/failed(失败,保留临时结果)/stopped(用户终止检查)
    # /NULL(单 agent 模式或老任务)
    # 升级时需手动执行:ALTER TABLE tasks ADD COLUMN review_status VARCHAR(16);
    # (幂等迁移见 migrate_task_add_review_status_column)
    # 列为自由字符串(非 DB 枚举),新增 stopped 取值无需迁移
    review_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default=None
    )

    # 执行被进程中断的持久标记:后端重启/崩溃导致执行线程丢失时写入当时时刻。
    # NULL = 未被中断(正常执行/正常失败)。前端据此把 FAILED 显示为"已中断(可继续)",
    # 与真正的失败区分开(见 migrate_stale_task_status 的启动对账)。
    # 升级时需手动执行:ALTER TABLE tasks ADD COLUMN interrupted_at TIMESTAMPTZ;
    # (幂等迁移见 migrate_task_add_interrupted_column)
    interrupted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 关联
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="task", cascade="all, delete-orphan",
        # 显式排序:按轮次 + 创建时间升序,保证刷新页面/任务完成后重载时
        # 对话顺序与 SSE 实时推送顺序一致。
        # 否则 PostgreSQL 在无 ORDER BY 时返回顺序未定义,会导致后写入的
        # agent2 总结等记录错位显示在 react_agent 工具调用之前。
        # 与后端内部查询(orchestrator/react_agent/agent2)的排序口径统一。
        order_by="Conversation.round_idx, Conversation.created_at",
    )
    results: Mapped[list["Result"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["TaskArtifact"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    # ============================================================
    # 验证器配置(存储在 params._verifier,免迁移;通过 property 读取)
    # ============================================================

    @property
    def test_env_url(self) -> str | None:
        """测试环境基址 URL(verifier_agent 的 http_request 目标)"""
        return (self.params or {}).get("_verifier", {}).get("test_env_url")

    @property
    def verifier_enabled(self) -> bool:
        """是否启用 verifier_agent(agent2 可自主调用)"""
        return (self.params or {}).get("_verifier", {}).get("enabled", False)

    @property
    def verifier_auth_mode(self) -> str:
        """验证授权模式:"direct"(直接执行) / "per_action"(每个动作弹窗授权)"""
        return (self.params or {}).get("_verifier", {}).get("auth_mode", "per_action")

    @property
    def verifier_auth_tokens(self) -> list[dict]:
        """登录凭证列表(供 verifier_agent 的 http_request 按身份注入请求头)

        每项:{"label": "管理员", "header_name": "Authorization", "header_value": "Bearer xxx"}
        LLM 调 http_request 时通过 auth_profile=label 选择身份,工具自动注入对应 header。
        """
        return (self.params or {}).get("_verifier", {}).get("auth_tokens", [])


class Conversation(Base):
    """对话记录:user / agent2 / react_agent 之间的所有消息

    每轮 agent2 的理解+提问、react_agent 的输出、最终总结,都存这里
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 协作轮次(第几轮,从 1 开始;存量数据可能含 round 0 的旧版初始评估)
    round_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 角色:user / agent2 / react_agent
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # 消息类型:
    #   question(用户提问) / message(用户追加消息)
    #   review(agent2 后台审查结论,详情存 reasoning)
    #   suggestions(agent2 建议追问方向,content 为 JSON)
    #   followup_suggestions(后台推荐"你可能还想深入了解",content 为 JSON)
    #   summary(agent2 最终总结)
    #   thinking(agent1 / agent2 思考)
    #   tool_call / tool_result
    #   evaluation / followup / answer(存量旧任务逐轮评估,新流程不再产出)
    #   error
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 思考链(reasoning_content):仅 type=thinking 有,模型一边想一边输出的临时过程
    # 落库以便刷新页面后仍可查看;其他 type 此字段为 None
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 仅 type=tool_result 有:对应 tool_call 会话记录的 id(字符串形式)。
    # 并行工具调用时 result 不再紧跟 call 落库,前端靠它精确配对 call/result;
    # 历史数据为 None,前端回退相邻配对
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 仅 user 追问消息有:本条消息附带的上传文件展示信息(刷新后气泡仍渲染 chip)。
    # 每项 {"upload_id", "filename", "size", "kind"};其他消息/历史数据为 None
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # 仅并行分面/计划项子循环的 fan-in 行有:所属分面名(如"check_ssrf"/"计划项 P1")。
    # 刷新/回放后前端据此给近似思考卡打"分面 · X"徽标(旧版只走瞬态 extra_payload,
    # 刷新后丢失)。历史数据为 NULL,前端不显示徽标。
    facet: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        # P2-6:PG now() 返回事务开始时间,会话复用长事务下落库时间戳会显著早于
        # 真实插入(实测偏差 14s),影响排序与审计。改用 Python 侧默认取插入时刻,
        # server_default 仍留作不经过 ORM 的直写(如原生 SQL)兜底。
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    task: Mapped[Task] = relationship(back_populates="conversations")


def migrate_conversation_tool_call_id() -> None:
    """幂等给 conversations 加 tool_call_id 列(tool_result 关联对应 tool_call)

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列。
    启动时检查缺失列并 ALTER TABLE ADD COLUMN,保证老库平滑升级。
    全新库(create_all 已建好新列)或已迁过 → 直接返回。

    老数据该列为 NULL,前端回退相邻配对,行为与改动前一致。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("conversations"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("conversations")}
        if "tool_call_id" in cols:
            return  # 已迁过
        conn.execute(
            text("ALTER TABLE conversations ADD COLUMN tool_call_id VARCHAR(64)")
        )
        conn.commit()
    log.info("conversations.tool_call_id 列迁移完成")


def migrate_conversation_add_attachments_column() -> None:
    """幂等给 conversations 加 attachments 列(追问消息附带的上传文件展示信息)

    背景同 migrate_conversation_tool_call_id:无 Alembic,老库需显式 ALTER。
    全新库(create_all 已建好)或已迁过 → 直接返回。老数据该列为 NULL,
    前端不渲染附件 chip,行为与改动前一致。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("conversations"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("conversations")}
        if "attachments" in cols:
            return  # 已迁过
        conn.execute(
            text("ALTER TABLE conversations ADD COLUMN attachments JSONB")
        )
        conn.commit()
    log.info("conversations.attachments 列迁移完成")


def migrate_conversation_add_facet_column() -> None:
    """幂等给 conversations 加 facet 列(并行分面/计划项子循环所属面名)

    背景同 migrate_conversation_tool_call_id:无 Alembic,老库需显式 ALTER。
    旧版 facet 只随 SSE 瞬态 extra_payload 下发,刷新/回放后徒标丢失;现在落列。
    全新库(create_all 已建好)或已迁过 → 直接返回。老数据该列为 NULL。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("conversations"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("conversations")}
        if "facet" in cols:
            return  # 已迁过
        conn.execute(
            text("ALTER TABLE conversations ADD COLUMN facet VARCHAR(64)")
        )
        conn.commit()
    log.info("conversations.facet 列迁移完成")


def migrate_task_drop_checklist_column() -> None:
    """幂等删除 tasks.checklist 旧列(覆盖度清单功能移除)

    背景:任务开始时的 agent2 初始评估(生成覆盖度清单 + 用户确认弹窗)
    已整体移除,task.checklist 不再有写入方与读取方。模型不再映射该列,
    create_all 不会删已存在的列,老库需显式 DROP。列已删(全新库)时直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("tasks"):
            return
        cols = {c["name"] for c in insp.get_columns("tasks")}
        if "checklist" not in cols:
            return  # 全新库或已迁过
        conn.execute(text("ALTER TABLE tasks DROP COLUMN checklist"))
        conn.commit()
    log.info("tasks.checklist 旧列已删除(覆盖度清单功能移除)")


def migrate_task_add_review_status_column() -> None:
    """幂等给 tasks 加 review_status 列(后台审查子状态)

    背景:agent2 审查移到后台执行,agent1 结束即任务 COMPLETED,
    需要独立子状态表达审查进度(running/done/failed)。
    项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列,
    启动时检查缺失列并 ALTER TABLE ADD COLUMN。老库已有行为 NULL(未审查),
    与"单 agent 模式 / 老任务"语义一致,无需回填。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("tasks"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("tasks")}
        if "review_status" in cols:
            return  # 已迁过
        conn.execute(
            text("ALTER TABLE tasks ADD COLUMN review_status VARCHAR(16)")
        )
        conn.commit()
    log.info("tasks.review_status 列迁移完成")


def migrate_stale_review_status() -> None:
    """启动时清理遗留的 running 审查状态 → failed

    背景:后台审查在 daemon 线程中执行,后端重启/崩溃后线程即死,
    review_status=running 的任务会永远卡在"检查中"(前端角标不消失)。
    启动时把所有 running 置为 failed(审查中断),前端侧栏显示"检查未完成"。
    幂等:无 running 记录时 UPDATE 0 行。
    """
    import logging

    from sqlalchemy import text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        result = conn.execute(
            text(
                "UPDATE tasks SET review_status = 'failed' "
                "WHERE review_status = 'running'"
            )
        )
        conn.commit()
    if result.rowcount:
        log.warning(
            f"启动清理: {result.rowcount} 个任务的遗留 running 审查状态已置为 failed"
        )


def migrate_task_add_interrupted_column() -> None:
    """幂等给 tasks 加 interrupted_at 列(执行被进程中断的持久标记)

    背景:后端重启/崩溃会丢失 in-memory 的暂停门控与执行线程,任务会永远卡在
    running/paused 的假死状态。新增可空列标记"被中断",使重启后仍可判定并让前端
    把这类 FAILED 显示为"已中断(可继续)"。
    项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列,
    启动时检查缺失列并 ALTER TABLE ADD COLUMN。全新库(create_all 已建好)或
    已迁过 → 直接返回;老库已有行为 NULL(未被中断),语义与"正常执行/真失败"一致。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("tasks"):
            return  # 全新库,create_all 会建好新列
        cols = {c["name"] for c in insp.get_columns("tasks")}
        if "interrupted_at" in cols:
            return  # 已迁过
        conn.execute(
            text("ALTER TABLE tasks ADD COLUMN interrupted_at TIMESTAMPTZ")
        )
        conn.commit()
    log.info("tasks.interrupted_at 列迁移完成")


def migrate_stale_task_status(bind=None) -> None:
    """启动对账:上一进程遗留的 running/paused 任务必为孤儿 → 标为可继续的中断失败

    原理:进程刚起、内存必空(pause_controller._states / 执行线程 /
    orchestrator._event_scopes 全为空),故 DB 中处于 running/paused 的任务,其执行
    线程一定已随上一进程消失。落地为 FAILED + interrupted_at,使前端出现"继续/重试"
    入口;真正续跑由 retry_failed_task 的断点续跑分支完成(有 agent1/agent2 对话 →
    不重做已完成部分)。

    用 ORM 读写(而非裸 SQL 的状态字面量):status 是 Enum(TaskStatus),
    SQLAlchemy 默认按成员 **名称**(大写 RUNNING/PAUSED/FAILED)落 PG 原生枚举,
    与 Python 侧小写 value 不一致,手写 SQL 字面量极易错配;交给 ORM 编码则无此歧义。

    幂等:无 running/paused 记录时查询为空,不写库。
    仅单 worker 部署成立:多实例下各 worker 各自对账会误杀其它 worker 在跑的任务,
    需改 DB 级租约/共享暂停状态(对齐 pause_controller 顶部既有的单机部署声明)。

    bind: 可选引擎/连接,默认用 app.database.engine(生产启动路径无需传参);
    测试可传入独立 schema 的 engine,避免触达开发/生产数据。
    """
    import logging
    from datetime import datetime, timezone

    from sqlalchemy.orm import Session

    from app.database import engine

    log = logging.getLogger(__name__)

    note = "服务重启,执行线程已丢失(可从断点继续)"
    with Session(bind or engine) as db:
        stale = (
            db.query(Task)
            .filter(Task.status.in_([TaskStatus.RUNNING, TaskStatus.PAUSED]))
            .all()
        )
        if not stale:
            return
        now = datetime.now(timezone.utc)
        for t in stale:
            # 保留既有失败原因(若有)再追加中断说明,供 retry_failed_task 拼续跑消息
            t.error_message = f"{t.error_message} | {note}" if t.error_message else note
            t.status = TaskStatus.FAILED
            t.interrupted_at = now
        db.commit()
    log.warning(
        f"启动对账: {len(stale)} 个遗留 running/paused 孤儿任务已置为 "
        f"FAILED + interrupted_at(可从断点继续)"
    )


class Result(Base):
    """任务结果项(通用)

    不再绑定安全审计语义。安全场景下 metadata 可放 cwe/severity/file_path/line_range 等;
    其他场景可自定义 metadata 结构。

    round_idx 记录由哪一轮 react_agent 产出,便于追溯
    """

    __tablename__ = "results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 由第几轮 react_agent 产出(从 1 开始)
    round_idx: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 通用字段
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 场景专用信息放 metadata(JSONB)
    # 安全场景示例:{"cwe": "CWE-89", "severity": "high", "file_path": "src/x.py", "line_range": "42"}
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    # 关联的练习域知识点(per-user 全局、按 key 去重)。出题收尾回写(见
    # services/practice/generator):任务级知识点(Result)与看板知识点
    # (KnowledgePoint)的形式化链接,供按知识点回溯任务上下文/多任务溯源。
    # 可空:未出题的知识点(仅展在侧栏)尚未落到看板时为 NULL。
    # 升级见 migrate_result_add_knowledge_point_id_column
    knowledge_point_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_points.id", ondelete="SET NULL"),
        nullable=True, default=None, index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="results")


def migrate_result_add_knowledge_point_id_column() -> None:
    """幂等给 results 加 knowledge_point_id 列(任务知识点 ↔ 看板知识点链接)

    背景同其他迁移:无 Alembic,已存在的表不会自动加列;全新库由 create_all 建好。
    FK 指向 knowledge_points(须晚于该表创建;create_all 已建)。老数据该列 NULL。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("results"):
            return  # 全新库由 create_all 建好
        cols = {c["name"] for c in insp.get_columns("results")}
        if "knowledge_point_id" in cols:
            return  # 已迁过
        conn.execute(text(
            "ALTER TABLE results ADD COLUMN knowledge_point_id "
            "UUID REFERENCES knowledge_points(id) ON DELETE SET NULL"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_results_knowledge_point_id "
            "ON results (knowledge_point_id)"
        ))
        conn.commit()
    log.info("results.knowledge_point_id 列迁移完成")
