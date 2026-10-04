"""练习模块数据模型(题库 / 知识点 / 遗忘曲线状态 / 答题流水 / 用户级设置)

设计:
- 题目来源于审计任务的真实发现(Result),由 LLM 改编为客观题(draft),
  用户预览确认后转 active 进入题库
- 知识点按 CWE 编号归类(无 CWE 时回退分类),per-user 隔离
- user_knowledge_states 承载 SM-2 遗忘曲线调度状态(ease_factor / interval / due_at)
- attempts 记录每次作答,供薄弱点统计与能力值估计
- practice_settings 存用户级练习设置(1:1,如自动生成练习题开关)

题库类表全新,随 Base.metadata.create_all 自动建表;
practice_settings 由 user_preferences.auto_generate_practice 拆出,
老数据由 migrate_practice_settings_table() 启动时迁移。
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class QuestionType(str, PyEnum):
    SINGLE_CHOICE = "single_choice"
    TRUE_FALSE = "true_false"


# ============================================================
# 学习主题(用户级词表:内置 4 个 + 自定义,存 learning_topics 表)
# 出题提示词按主题切换;题目/知识点落库时记录所属主题 key,
# 便于按主题筛选/组卷。分类仍按 finding 内容自动匹配(规则先行 + LLM 兜底),
# 主题集合(含自定义)由用户在练习设置中管理。
# practice_settings.learning_topic 列保留但已不读写(历史废弃列)。
# ============================================================
LEARNING_TOPIC_SECURITY = "security"          # 网络安全
LEARNING_TOPIC_ARCHITECTURE = "architecture"  # 架构设计
LEARNING_TOPIC_CODING = "coding"              # 通用代码能力
LEARNING_TOPIC_CONTRACT = "contract"          # 合同文书
LEARNING_TOPICS = (
    LEARNING_TOPIC_SECURITY,
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
    LEARNING_TOPIC_CONTRACT,
)
DEFAULT_LEARNING_TOPIC = LEARNING_TOPIC_SECURITY

# 自定义主题 key 前缀(服务端生成,用户不接触 key,杜绝与内置键冲突)
CUSTOM_TOPIC_KEY_PREFIX = "custom_"
# 单用户自定义主题上限(防词表膨胀拖慢分类调用)
MAX_CUSTOM_TOPICS = 10

# 内置主题定义(ensure_user_topics 懒播种;description 供分类提示词与设置页展示)
BUILTIN_TOPIC_DEFS: tuple[dict, ...] = (
    {
        "key": LEARNING_TOPIC_SECURITY,
        "name": "安全",
        "description": "网络安全漏洞:注入、硬编码凭证、越权、SSRF、配置泄露、"
                       "不安全反序列化等漏洞模式与安全编码实践",
        "sort_order": 10,
    },
    {
        "key": LEARNING_TOPIC_ARCHITECTURE,
        "name": "架构",
        "description": "软件架构设计:分层与模块边界、耦合与内聚、设计模式应用与误用、"
                       "技术选型权衡、扩展性与可测试性缺陷",
        "sort_order": 20,
    },
    {
        "key": LEARNING_TOPIC_CODING,
        "name": "编码",
        "description": "通用代码质量:bug 与边界条件、异常与错误处理、性能问题(如 N+1 查询)、"
                       "代码坏味道、语言特性与工程最佳实践",
        "sort_order": 30,
    },
    {
        "key": LEARNING_TOPIC_CONTRACT,
        "name": "合同",
        "description": "合同文书审查:条款不利识别、权责对等、付款与违约、知识产权归属、"
                       "霸王条款(任意解除权、单方变更、过度免责)",
        "sort_order": 40,
    },
)


class LearningTopic(Base):
    """用户级学习主题词表(finding 分类与出题视角的定义源)

    内置 4 行由 ensure_user_topics 懒播种(is_builtin=true:
    不可删、不可改 key/name/description,仅可停用);
    自定义行(is_builtin=false)全字段可管理,上限 MAX_CUSTOM_TOPICS 个。
    enabled=false 的主题不再为新 finding 出题(存量题目/知识点不受影响)。
    """

    __tablename__ = "learning_topics"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_learning_topic_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 稳定标识:内置=security/architecture/coding/contract;自定义=custom_<8位随机>
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 展示名(内置:安全/架构/编码/合同)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 主题视角说明(出题视角 + 分类依据;内置行存内置文案,自定义行用户填写)
    description: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=""
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 出题开关:false 时该主题的 finding 不再出题(存量不动)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 排序:内置 10/20/30/40,自定义从 50 递增(按创建顺序)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )


def ensure_user_topics(db, user_id) -> list["LearningTopic"]:
    """确保用户已播种全部内置主题行(幂等),返回该用户全部主题(按 sort_order)

    只 flush 不 commit,事务边界由调用方决定。
    """
    existing = (
        db.query(LearningTopic)
        .filter(LearningTopic.user_id == user_id)
        .all()
    )
    existing_keys = {t.key for t in existing}
    missing = [d for d in BUILTIN_TOPIC_DEFS if d["key"] not in existing_keys]
    if missing:
        for d in missing:
            db.add(LearningTopic(
                user_id=user_id, key=d["key"], name=d["name"],
                description=d["description"], is_builtin=True,
                enabled=True, sort_order=d["sort_order"],
            ))
        db.flush()
        existing = (
            db.query(LearningTopic)
            .filter(LearningTopic.user_id == user_id)
            .all()
        )
    return sorted(existing, key=lambda t: (t.sort_order, t.created_at))


class QuestionStatus(str, PyEnum):
    # LLM 刚生成,待用户预览确认
    DRAFT = "draft"
    # 用户确认,进入选题池
    ACTIVE = "active"
    # 用户归档,不再参与选题
    ARCHIVED = "archived"


class KnowledgePoint(Base):
    """知识点(选题与遗忘曲线调度的最小单元)

    key 优先取 CWE 编号(如 "CWE-89"),来自 Result.metadata.cwe;
    无 CWE 时回退漏洞分类(如 "injection" / "auth" / "secrets")。
    per-user 唯一。

    讲解字段(explanation*)承载知识点看板上的「知识点讲解」正文:
    生成素材来自出题上下文(发现原文 + 预读到的真实材料 + 本次生成的题目),
    不是让模型凭空写概念简介;生成与覆盖规则见 services/practice/explainer.py。
    """

    __tablename__ = "knowledge_points"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_knowledge_point_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # 知识点唯一键:"CWE-89" / "injection" 等
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 展示名(如 "SQL 注入" / "CWE-89 SQL 注入")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 粗分类(前端分组展示用,如 injection / auth / crypto)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 编程语言标签(多值,如 ["python", "sql"]);出题时由 LLM 给出,
    # 同知识点多次出题做并集累积;老数据为空列表
    languages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 所属学习主题(learning_topics.key:内置 security/architecture/coding/contract
    # 或自定义 custom_*);出题时随知识点首次创建写入,已存在知识点不改(first-wins)
    learning_topic: Mapped[str] = mapped_column(
        String(64), nullable=False,
        default=DEFAULT_LEARNING_TOPIC, server_default=DEFAULT_LEARNING_TOPIC,
    )

    # ---- 知识点讲解(Markdown 正文,200~400 字)----
    # 讲解正文(None/空串 = 尚未生成)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # 讲解来源:auto=模型生成 / manual=用户编辑 / ""=尚未生成
    # manual 永不被自动生成覆盖(用户的自己的总结不会被刷掉)
    explanation_source: Mapped[str] = mapped_column(
        String(8), nullable=False, default="", server_default="",
    )
    # 生成所用模型名(排查质量用;手工编辑不记)
    explanation_model: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="",
    )
    # 最近一次更新讲解的时间(前端展示「更新于」与陈旧判定)
    explanation_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Question(Base):
    """练习题(客观题:单选 / 判断)

    由审计任务的 Result 经 LLM 改编生成;dedup_hash = sha256(stem + code_snippet),
    用于同用户下防重复生成。
    """

    __tablename__ = "practice_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # 来源追溯(可空:手工导入等未来扩展)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("results.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_points.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    qtype: Mapped[QuestionType] = mapped_column(Enum(QuestionType), nullable=False)
    # 题干
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    # 相关代码片段(可空)
    code_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 选项列表(判断题固定为 ["正确", "错误"])
    options: Mapped[list] = mapped_column(JSONB, nullable=False)
    # 正确选项下标
    answer_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # 答案解析
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # 难度 1-5(LLM 初评,作答后微调)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    # 出题时使用的学习主题(security/architecture/coding;老数据为 NULL)
    learning_topic: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 题目来源形式:repo=基于真实源码出题,synthetic=改编题
    # (LLM 原创含同类问题的虚构代码,脱离原仓库);老题为 NULL 视为 repo
    origin: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default="repo", server_default="repo"
    )
    # 题目引用的源码定位(仓库内相对路径 + 行区间,如 "120-150";老题为 NULL),
    # 做题页右侧代码栏据此自动打开并滚动定位
    source_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_lines: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[QuestionStatus] = mapped_column(
        Enum(QuestionStatus), default=QuestionStatus.DRAFT, nullable=False, index=True
    )
    # sha256(stem + code_snippet),同用户去重
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserKnowledgeState(Base):
    """用户对单个知识点的 SM-2 记忆状态(遗忘曲线核心)

    SM-2 参数:ease_factor(≥1.3) / interval_days / repetitions / due_at。
    首次作答该知识点时创建。
    """

    __tablename__ = "user_knowledge_states"
    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_point_id", name="uq_knowledge_state_user_kp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    knowledge_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_points.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    ease_factor: Mapped[float] = mapped_column(Float, nullable=False, default=2.5)
    interval_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下次到期复习时间(None = 尚未建立记忆轨迹)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 作答统计(薄弱点分析用)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最近一次作答质量(SM-2 quality:0-5)
    last_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PracticeSession(Base):
    """一次练习会话(按需即时组卷)

    question_ids 保存本次组卷选中的题目 id 列表(JSON 字符串数组),
    全部作答完成后写 finished_at。
    """

    __tablename__ = "practice_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 组卷选中的题目 id(str 列表)
    question_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 组卷时的策略快照(能力值 / 各知识点状态),供复盘
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Attempt(Base):
    """单次作答流水(薄弱点统计 / 能力值估计 / 错题回溯)"""

    __tablename__ = "practice_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practice_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practice_questions.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    chosen_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(nullable=False)

    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ============================================================
# 出题思考模式(用户级覆盖出题模型的思考开关)
# follow=跟随模型配置自身开关(默认);on/off=强制开/关。
# 仅支持思考模式的模型(catalog thinking=only)强制关会被忽略。
# ============================================================
THINKING_MODE_FOLLOW = "follow"
THINKING_MODE_ON = "on"
THINKING_MODE_OFF = "off"
THINKING_MODES = (THINKING_MODE_FOLLOW, THINKING_MODE_ON, THINKING_MODE_OFF)
DEFAULT_THINKING_MODE = THINKING_MODE_FOLLOW

# ============================================================
# 知识点讲解来源(knowledge_points.explanation_source)
# auto=出题收尾批量/按需生成写入;manual=用户在看板/弹窗手工编辑;
# 空串=尚未生成。自动生成永远不覆盖 manual(用户的总结不被刷掉)。
# ============================================================
EXPLANATION_SOURCE_AUTO = "auto"
EXPLANATION_SOURCE_MANUAL = "manual"
# 讲解正文长度上限(手工编辑同一限制;防贴入超长文章撑爆卡片展示)
MAX_EXPLANATION_CHARS = 4000

# ============================================================
# 出题并发度边界(practice_settings.generate_concurrency)
# 默认 1=串行;上限防把厂商 RPM/并发打死(429 会让整条 finding 白跑)
# ============================================================
DEFAULT_GENERATE_CONCURRENCY = 1
MAX_GENERATE_CONCURRENCY = 4


class PracticeSettings(Base):
    """用户级练习设置 (per-user, 1:1)

    - auto_generate_practice:任务完成后是否自动生成练习题 draft
      (默认开启;产出仍需用户预览确认才转 active)
    - learning_topic:(已废弃,列保留不迁移)出题主题现按 finding 自动匹配
    - restore_workspace_for_practice:出题前沙箱已清理时,
      是否重新 clone 仓库恢复工作区(供出题工具循环读源码)
    - default_llm_config_id:用户级默认出题模型(UserLLMConfig 中某条配置的 id),
      None=跟随任务级配置/env 默认
    - thinking_mode_for_practice:出题思考模式覆盖(follow/on/off),
      follow=跟随出题模型配置自身的思考开关

    迁移:老数据存于 user_preferences.auto_generate_practice 布尔列,
    migrate_practice_settings_table() 启动时把数据拷入本表后删除旧列(幂等);
    learning_topic / restore_workspace_for_practice / default_llm_config_id /
    thinking_mode_for_practice / force_default_llm 为后加列,由
    migrate_practice_learning_columns() 幂等补齐。
    """

    __tablename__ = "practice_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 任务完成后是否自动生成练习题 draft(默认开启)
    auto_generate_practice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # (已废弃,列保留不迁移)历史用户级学习主题;出题主题现按 finding 自动匹配
    learning_topic: Mapped[str] = mapped_column(
        String(32), nullable=False,
        server_default=DEFAULT_LEARNING_TOPIC, default=DEFAULT_LEARNING_TOPIC,
    )
    # 出题前沙箱已清理时是否重新 clone 恢复工作区(默认关,避免意外大仓库克隆)
    restore_workspace_for_practice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # 用户级默认出题模型(UserLLMConfig.llm_configs 中某条配置的 id;None=不指定)
    # 解析优先级:task.llm_config_id > 本字段 > env 默认
    default_llm_config_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    # 始终用默认出题模型(忽略任务自带模型配置;默认关)
    force_default_llm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # 出题思考模式覆盖:follow=跟随模型配置,on/off=强制开/关
    # (思考模式出题可能更慢但质量更高;部分模型思考模式下工具调用不稳定)
    thinking_mode_for_practice: Mapped[str] = mapped_column(
        String(16), nullable=False,
        server_default=DEFAULT_THINKING_MODE, default=DEFAULT_THINKING_MODE,
    )
    # 出题完成后是否顺带批量更新知识点讲解(默认关)
    # 开启时一次 job 只多 1~2 次轻任务调用(按 ≤8 个知识点一批)
    generate_explanation_with_questions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # 讲解专用模型(UserLLMConfig 中某条配置 id;None=沿用出题模型)
    # 讲解是轻任务,可以用比出题快/便宜的模型(思考类模型出题要 160s/条)
    explain_llm_config_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, default=None
    )
    # 出题并发度(1=串行,2/4=并行逐条 finding)
    # 受厂商组内并发上限约束,超限会触发 429 → 建议与厂商配额一致
    generate_concurrency: Mapped[int] = mapped_column(
        Integer, nullable=False,
        server_default=str(DEFAULT_GENERATE_CONCURRENCY),
        default=DEFAULT_GENERATE_CONCURRENCY,
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


# ============================================================
# 迁移:user_preferences.auto_generate_practice → 独立表
# ============================================================


def migrate_practice_settings_table() -> None:
    """幂等迁移:把 user_preferences.auto_generate_practice 拷入 practice_settings 表,
    完成后删除旧列。

    背景:项目用 Base.metadata.create_all(无 Alembic),新表随 create_all 建好,
    但老库的数据还在 user_preferences.auto_generate_practice 列里。

    - user_preferences 不存在 / 无该列(全新库或已迁过)→ 直接返回
    - 拷贝用 INSERT ... ON CONFLICT (user_id) DO UPDATE,中途失败重跑安全
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("user_preferences"):
            return  # 全新库,无老数据
        cols = {c["name"] for c in insp.get_columns("user_preferences")}
        if "auto_generate_practice" not in cols:
            return  # 全新库或已迁过
        conn.execute(
            text(
                """
                INSERT INTO practice_settings
                    (id, user_id, auto_generate_practice, created_at, updated_at)
                SELECT id, user_id, auto_generate_practice, now(), now()
                FROM user_preferences
                ON CONFLICT (user_id) DO UPDATE
                SET auto_generate_practice = EXCLUDED.auto_generate_practice,
                    updated_at = now()
                """
            )
        )
        conn.execute(
            text(
                "ALTER TABLE user_preferences "
                "DROP COLUMN auto_generate_practice"
            )
        )
        log.info(
            "practice_settings 迁移: user_preferences.auto_generate_practice "
            "→ practice_settings(旧列已删)"
        )
        conn.commit()


def migrate_practice_learning_columns() -> None:
    """幂等给 practice_settings / knowledge_points / practice_questions 补新列

    背景:项目用 Base.metadata.create_all(无 Alembic),已存在的表不会自动加新列。
    - practice_settings 加 learning_topic / restore_workspace_for_practice /
      default_llm_config_id / thinking_mode_for_practice / force_default_llm /
      generate_explanation_with_questions / explain_llm_config_id /
      generate_concurrency
    - knowledge_points 加 languages / learning_topic(加列时一次性回填:
      取该 KP 题目中最常见的非空 learning_topic,无题保持默认 'security')
      与讲解四列 explanation / explanation_source / explanation_model /
      explanation_updated_at(存量知识点讲解为空,由看板「生成讲解」按需回填)
    - practice_questions 加 learning_topic(可空,老题不补)与
      source_file / source_lines(源码定位,可空)
    全新库(create_all 已建好新列)或已迁过 → 直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)

    with engine.connect() as conn:
        insp = inspect(conn)
        if insp.has_table("practice_settings"):
            cols = {c["name"] for c in insp.get_columns("practice_settings")}
            if "learning_topic" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN learning_topic "
                    "VARCHAR(32) NOT NULL DEFAULT 'security'"
                ))
                log.info("practice_settings.learning_topic 列迁移完成")
            if "restore_workspace_for_practice" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "restore_workspace_for_practice BOOLEAN NOT NULL DEFAULT false"
                ))
                log.info("practice_settings.restore_workspace_for_practice 列迁移完成")
            if "default_llm_config_id" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "default_llm_config_id VARCHAR(36)"
                ))
                log.info("practice_settings.default_llm_config_id 列迁移完成")
            if "thinking_mode_for_practice" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "thinking_mode_for_practice VARCHAR(16) NOT NULL DEFAULT 'follow'"
                ))
                log.info("practice_settings.thinking_mode_for_practice 列迁移完成")
            if "force_default_llm" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "force_default_llm BOOLEAN NOT NULL DEFAULT false"
                ))
                log.info("practice_settings.force_default_llm 列迁移完成")
            if "generate_explanation_with_questions" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "generate_explanation_with_questions BOOLEAN NOT NULL DEFAULT false"
                ))
                log.info("practice_settings.generate_explanation_with_questions 列迁移完成")
            if "explain_llm_config_id" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "explain_llm_config_id VARCHAR(36)"
                ))
                log.info("practice_settings.explain_llm_config_id 列迁移完成")
            if "generate_concurrency" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_settings ADD COLUMN "
                    "generate_concurrency INTEGER NOT NULL DEFAULT 1"
                ))
                log.info("practice_settings.generate_concurrency 列迁移完成")
        kp_topic_added = False
        if insp.has_table("knowledge_points"):
            cols = {c["name"] for c in insp.get_columns("knowledge_points")}
            if "languages" not in cols:
                conn.execute(text(
                    "ALTER TABLE knowledge_points ADD COLUMN languages "
                    "JSONB NOT NULL DEFAULT '[]'"
                ))
                log.info("knowledge_points.languages 列迁移完成")
            if "learning_topic" not in cols:
                conn.execute(text(
                    "ALTER TABLE knowledge_points ADD COLUMN learning_topic "
                    "VARCHAR(64) NOT NULL DEFAULT 'security'"
                ))
                kp_topic_added = True
                log.info("knowledge_points.learning_topic 列迁移完成")
            # 知识点讲解四列(存量知识点默认为「尚未生成」,不回填正文)
            for col_name, ddl in (
                ("explanation", "ADD COLUMN explanation TEXT"),
                ("explanation_source", "ADD COLUMN explanation_source VARCHAR(8) "
                                      "NOT NULL DEFAULT ''"),
                ("explanation_model", "ADD COLUMN explanation_model VARCHAR(64) "
                                      "NOT NULL DEFAULT ''"),
                ("explanation_updated_at", "ADD COLUMN explanation_updated_at "
                                           "TIMESTAMP WITH TIME ZONE"),
            ):
                if col_name not in cols:
                    conn.execute(text(f"ALTER TABLE knowledge_points {ddl}"))
                    log.info(f"knowledge_points.{col_name} 列迁移完成")
        if insp.has_table("practice_questions"):
            cols = {c["name"] for c in insp.get_columns("practice_questions")}
            if "learning_topic" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN "
                    "learning_topic VARCHAR(32)"
                ))
                log.info("practice_questions.learning_topic 列迁移完成")
            if "source_file" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN "
                    "source_file VARCHAR(512)"
                ))
                log.info("practice_questions.source_file 列迁移完成")
            if "source_lines" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN "
                    "source_lines VARCHAR(32)"
                ))
                log.info("practice_questions.source_lines 列迁移完成")
            if "origin" not in cols:
                conn.execute(text(
                    "ALTER TABLE practice_questions ADD COLUMN origin "
                    "VARCHAR(16) DEFAULT 'repo'"
                ))
                log.info("practice_questions.origin 列迁移完成")
        # KP 主题回填:仅在本轮刚加列时执行一次(幂等)。
        # 必须放在 practice_questions 分支之后(回填依赖题表 learning_topic 列已存在),
        # 取该 KP 题目中最常见的非空 learning_topic(并列按主题名排序保证确定性),
        # 无带主题题目的 KP 保持默认 'security'。
        if kp_topic_added and insp.has_table("practice_questions"):
            conn.execute(text("""
                UPDATE knowledge_points kp SET learning_topic = sub.topic
                FROM (SELECT q.knowledge_point_id AS kpid, q.learning_topic AS topic,
                             ROW_NUMBER() OVER (PARTITION BY q.knowledge_point_id
                               ORDER BY COUNT(*) DESC, q.learning_topic) AS rn
                      FROM practice_questions q WHERE q.learning_topic IS NOT NULL
                      GROUP BY q.knowledge_point_id, q.learning_topic) sub
                WHERE sub.rn = 1 AND kp.id = sub.kpid
            """))
            log.info("knowledge_points.learning_topic 存量回填完成(取题目主题众数)")
        conn.commit()
