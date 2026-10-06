"""练习模块的 Pydantic 模型(请求与响应)"""
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 学习主题 key 格式(与 learning_topics.key 一致:内置 security/architecture/
# coding/contract 或自定义 custom_xxxxxxxx)
_TOPIC_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")


# ============================================================
# 题目生成 / 确认
# ============================================================


class GenerateRequest(BaseModel):
    """从审计任务的真实发现生成练习题(POST /practice/generate)

    异步执行:立即返回 job_id,前端轮询 GET /practice/generate/{job_id}。
    """

    task_id: uuid.UUID
    # 参与生成的 finding 数上限(防 LLM 成本失控)
    max_findings: int = Field(default=10, ge=1, le=20)
    # 重出开关:默认 False,本用户已就该 finding 出过题的整条跳过(不再付 LLM 成本);
    # 任务详情页「重新出题」显式传 True 才允许对同一发现重出
    force_regenerate: bool = False


class GenerateJobResponse(BaseModel):
    """异步生成任务句柄(POST /practice/generate 立即返回)"""

    job_id: str


class GenerateModelResponse(BaseModel):
    """任务出题模型解析结果(GET /practice/tasks/{task_id}/generate-model)

    与真实出题的 resolve_llm_client 同一优先级解析,供任务详情页在
    出题入口附近展示「本次出题将使用:xxx」,避免用户困惑为什么
    没用练习设置里的默认模型。
    source: task=任务自带配置 / default=练习默认出题模型 / env=环境默认
    """

    model: str
    source: Literal["task", "default", "env"]


class GenerateJobStatusResponse(BaseModel):
    """生成进度与结果(GET /practice/generate/{job_id})

    status: pending(排队) / running(出题中) / done(完成) / error(失败)
    done/total: 已处理/总 finding 数;done 时 questions 为新 draft 列表。
    """

    status: str
    done: int = 0
    total: int = 0
    error: str = ""
    questions: list["DraftQuestionResponse"] = []
    skipped_findings: int = 0


class GenerateJobSummary(BaseModel):
    """出题 job 摘要(GET /practice/generate/jobs)

    练习页侧栏发现运行中 job 用;含 SSE snapshot 同构字段,
    已完成 job 的 recent_text 保留最后一批输出尾部文本。
    """

    job_id: str
    status: str
    done: int = 0
    total: int = 0
    error: str = ""
    # 出题来源:manual(任务详情页手动) / auto(任务完成自动生成)
    source: str = "manual"
    task_id: str | None = None
    task_title: str = ""
    current_finding: str = ""
    recent_text: str = ""
    skipped_findings: int = 0
    created_count: int = 0
    started_at: str | None = None


class GenerateJobsResponse(BaseModel):
    """当前用户的出题 job 列表(运行中优先,限最近 10 条)"""

    jobs: list[GenerateJobSummary] = []


class QuestionContent(BaseModel):
    """题目完整内容(题干 / 选项 / 正确答案 / 解析 / 代码 / 源码出处)

    草稿预览与题库详情共用同一套内容字段:两处都需要 answer_idx 与
    explanation 供用户校对题目质量或事后复盘。答题中的组卷下发用的是
    SessionQuestionResponse(不含本模型的答案字段),两者保持分离。
    """

    id: uuid.UUID
    qtype: str
    stem: str
    code_snippet: str | None = None
    options: list[str]
    answer_idx: int
    explanation: str
    difficulty: float
    knowledge_key: str | None = None
    knowledge_name: str | None = None
    # 出题形式:repo=基于真实源码,synthetic=改编题(虚构代码)
    origin: str = "repo"
    # 知识点编程语言标签(如 ["python", "sql"];来自知识点累积)
    languages: list[str] = []
    # 题目引用的源码定位(校对出处用;老题为 None)
    source_file: str | None = None
    source_lines: str | None = None

    model_config = {"from_attributes": True}


class DraftQuestionResponse(QuestionContent):
    """生成的候选题(draft 状态,待用户预览确认)

    预览阶段即下发 answer_idx 与 explanation,供用户校对题目质量。
    """


class GenerateResponse(BaseModel):
    """题目生成结果"""

    questions: list[DraftQuestionResponse] = []
    # 因解析失败/重复被丢弃的 finding 数(前端提示用)
    skipped_findings: int = 0


class ConfirmQuestionsRequest(BaseModel):
    """确认 draft 题目入库(POST /practice/questions/confirm)

    只传用户勾选保留的 id;同一来源任务的其余 draft 一并删除。
    """

    task_id: uuid.UUID
    question_ids: list[uuid.UUID] = []


class ConfirmQuestionsResponse(BaseModel):
    confirmed: int
    discarded: int = 0


class ActivateQuestionsRequest(BaseModel):
    """转正指定 draft 题目(POST /practice/questions/activate)

    与 confirm 不同:只把传入 id 转 active,不影响其余 draft(题库管理页逐条操作用)。
    """

    question_ids: list[uuid.UUID] = []


class ActivateQuestionsResponse(BaseModel):
    activated: int = 0


# ============================================================
# 练习会话(按需即时组卷)
# ============================================================


class StartSessionRequest(BaseModel):
    """开始练习(POST /practice/sessions)"""

    count: int = Field(default=8, ge=1, le=30)
    # 限定知识点 key(如只看 "CWE-89"),为空表示全部
    topic_filter: str | None = None
    # 限定学习主题(learning_topics.key,内置或自定义),为空表示全部;
    # 与 topic_filter 互斥(同传 422)。key 为动态值(用户自定义),
    # 仅做格式校验防注入,不存在的 key 由端点 404 兜底
    learning_topic: str | None = None
    # 限定题目白名单(如错题重练):非空时只从这些 active 题中组卷
    question_ids: list[uuid.UUID] | None = None

    @field_validator("learning_topic")
    @classmethod
    def _validate_learning_topic(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _TOPIC_KEY_RE.fullmatch(v):
            raise ValueError("learning_topic 格式非法(仅允许小写字母/数字/下划线,长度 1-64)")
        return v

    @model_validator(mode="after")
    def _check_topic_filters_exclusive(self) -> "StartSessionRequest":
        if self.topic_filter and self.learning_topic:
            raise ValueError("topic_filter 与 learning_topic 不能同时指定")
        return self


class SessionQuestionResponse(BaseModel):
    """组卷下发的题面(不含 answer_idx / explanation,防作弊)

    source_task_id/source_file/source_lines 供做题页右侧代码栏
    打开题目来源工作区并自动定位(不含答案,无作弊风险)。
    """

    id: uuid.UUID
    qtype: str
    stem: str
    code_snippet: str | None = None
    options: list[str]
    difficulty: float
    knowledge_name: str | None = None
    # 知识点编程语言标签(做题页标签展示用)
    languages: list[str] = []
    # 出题形式:repo=真实代码题,synthetic=改编题
    origin: str = "repo"
    source_task_id: uuid.UUID | None = None
    source_file: str | None = None
    source_lines: str | None = None

    model_config = {"from_attributes": True}


class StartSessionResponse(BaseModel):
    session_id: uuid.UUID
    questions: list[SessionQuestionResponse] = []
    # 选题池不足时的提示(如题库为空)
    message: str = ""


class SubmitAnswerRequest(BaseModel):
    """提交单题答案(POST /practice/sessions/{id}/answers)"""

    question_id: uuid.UUID
    chosen_idx: int = Field(ge=0)


class KnowledgeStateResponse(BaseModel):
    """知识点记忆状态(答题反馈与统计展示共用)"""

    knowledge_key: str
    knowledge_name: str
    # 知识点编程语言标签
    languages: list[str] = []
    ease_factor: float
    interval_days: float
    repetitions: int
    due_at: datetime | None = None
    attempts: int
    correct_count: int
    accuracy: float | None = None

    model_config = {"from_attributes": True}


class SubmitAnswerResponse(BaseModel):
    is_correct: bool
    correct_idx: int
    explanation: str
    # 该题知识点的最新记忆状态
    state: KnowledgeStateResponse | None = None
    # 本会话进度(answered / total)
    answered_count: int
    total_count: int
    # 答错时附回该知识点的完整讲解(Markdown;答对或尚未生成时为空串)
    # 只在错答下发:讲解正文平均 300 字,每题都带会把 payload 撑大
    knowledge_explanation: str = ""


# ============================================================
# 统计 / 题库管理
# ============================================================


class WeakPointItem(BaseModel):
    """薄弱点条目(按错误率排序)"""

    knowledge_key: str
    knowledge_name: str
    # 知识点编程语言标签
    languages: list[str] = []
    attempts: int
    correct_count: int
    accuracy: float
    ease_factor: float
    due_at: datetime | None = None


class KnowledgePointCardItem(BaseModel):
    """知识点卡片(知识点看板视图,GET /practice/knowledge-points)

    一张卡片 = 一个知识点:静态信息(key/name/languages) +
    SM-2 记忆状态(无作答记录时为默认值) + 题库题数 + 看板分栏状态 +
    知识点讲解(Markdown 正文与来源/更新时间)。
    """

    knowledge_key: str
    knowledge_name: str
    # 粗分类(如 cwe / general)
    category: str | None = None
    languages: list[str] = []
    # 所属学习主题 key(learning_topics.key,前端按主题分组展示)
    learning_topic: str = "security"
    # 作答统计(无作答记录为 0)
    attempts: int = 0
    correct_count: int = 0
    accuracy: float | None = None
    # SM-2 记忆参数(无作答记录为默认值)
    repetitions: int = 0
    interval_days: float = 0.0
    ease_factor: float = 2.5
    due_at: datetime | None = None
    # 题库中该知识点的 active 题数
    question_count: int = 0
    # 看板分栏(按优先级派生):weak=薄弱 / due=待复习 /
    # mastered=已巩固 / learning=学习中 / fresh=未开始
    board_status: Literal["weak", "due", "mastered", "learning", "fresh"] = "fresh"
    # ---- 知识点讲解 ----
    # 讲解正文(Markdown);空串 = 尚未生成(卡片展示「生成讲解」入口)
    explanation: str = ""
    # 来源:auto=模型生成 / manual=手工编辑(不会被自动覆盖)/ ""=未生成
    explanation_source: str = ""
    # 生成所用模型(手工编辑不记)
    explanation_model: str = ""
    # 最近一次更新讲解的时间(前端展示「更新于」)
    explanation_updated_at: datetime | None = None
    # 该知识点是否有可供模型出讲解的题素材(无题时前端不亮「生成讲解」)
    can_generate_explanation: bool = False


class ExplainKnowledgePointsRequest(BaseModel):
    """按需生成/更新知识点讲解(POST /practice/knowledge-points/explain)

    异步执行:立即返回 job_id,前端轮询 GET /practice/generate/{job_id}。
    knowledge_keys 上限 20(一次点击覆盖看板上当前的缺讲解项就够用了,
    再多应当走「出题收尾批量」路径顺便生成)。
    """

    knowledge_keys: list[str] = Field(default_factory=list, min_length=1, max_length=20)
    # force=True 时重写已有 auto 讲解;manual 讲解任何时候都不被自动覆盖
    force: bool = False


class ExplainKnowledgePointsResponse(BaseModel):
    """讲解 job 句柄与本次计划处理的知识点数"""

    job_id: str
    total: int


class SaveKnowledgeExplanationRequest(BaseModel):
    """手工编辑知识点讲解(PUT /practice/knowledge-points/{key}/explanation)

    正文按 Markdown 存储与展示;上限沿用 MAX_EXPLANATION_CHARS。
    写入后 explanation_source=manual,自动生成不再覆盖。
    """

    markdown: str = Field(default="", max_length=4000)


class KnowledgeExplanationOut(BaseModel):
    """知识点讲解字段回写视图(手工编辑后给前端拿最新来源与时间)"""

    knowledge_key: str
    explanation: str = ""
    explanation_source: str = ""
    explanation_model: str = ""
    explanation_updated_at: datetime | None = None


class SetKnowledgeTopicRequest(BaseModel):
    """手工修正知识点所属学习主题(PUT /practice/knowledge-points/{key}/topic)

    topic_key 必须是当前用户名下的主题(内置 security/architecture/coding/
    contract 或自定义 custom_*,含已停用主题);保存时级联更新该知识点下
    全部题目的 learning_topic,保持两处字段一致。
    """

    topic_key: str = Field(min_length=1, max_length=64)


class KnowledgeTopicOut(BaseModel):
    """知识点主题回写视图(改后给前端拿最新主题 key)"""

    knowledge_key: str
    learning_topic: str


class StatsResponse(BaseModel):
    """练习首页统计(GET /practice/stats)"""

    # 用户能力估计值(难度匹配基准)
    ability: float
    # 到期待复习的知识点数
    due_count: int
    total_attempts: int
    total_correct: int
    accuracy: float | None = None
    weak_points: list[WeakPointItem] = []
    # 题库规模
    active_question_count: int = 0
    draft_question_count: int = 0


class QuestionListItem(BaseModel):
    """题库列表项(GET /practice/questions)"""

    id: uuid.UUID
    qtype: str
    stem: str
    difficulty: float
    status: str
    knowledge_name: str | None = None
    # 知识点编程语言标签
    languages: list[str] = []
    # 出题形式:repo=真实代码题,synthetic=改编题(老题为 None)
    origin: str | None = None
    attempts: int = 0
    accuracy: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class QuestionDetailResponse(QuestionContent):
    """题目完整信息(GET /practice/questions/{question_id})

    列表项只带一行摘要(整库下发全量内容会让 payload 翻几倍),
    点开某题时再按需拉本题全量:内容字段同草稿预览,另附状态、
    知识点归类与该题作答统计,供题库管理与错题复盘使用。
    """

    # draft=待确认 / active=已入库 / archived=已归档
    status: str
    # 知识点粗分类(如 injection / auth;看板与筛选展示用)
    category: str | None = None
    # 出题时归属的学习主题(learning_topics.key;老题为 None)
    learning_topic: str | None = None
    # 来源任务(前端跳任务详情页查源码用;老题/手工导入为 None)
    source_task_id: uuid.UUID | None = None
    # 所属知识点的讲解正文(Markdown;空串=尚未生成)——错题复盘时直接在弹窗里看
    knowledge_explanation: str = ""
    # 讲解来源(auto/manual/""),前端据此判断能否就地编辑与是否会被自动覆盖
    knowledge_explanation_source: str = ""
    # 该题作答统计(无记录 attempts=0、accuracy=None)
    attempts: int = 0
    correct_count: int = 0
    accuracy: float | None = None
    created_at: datetime


# ============================================================
# 导航徽章 / 历史会话 / 趋势 / 错题
# ============================================================


class PracticeSummaryResponse(BaseModel):
    """轻量汇总(GET /practice/summary,导航徽章用)"""

    due_count: int = 0
    draft_count: int = 0


class SessionListItem(BaseModel):
    """历史练习会话列表项(GET /practice/sessions)"""

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    question_count: int
    answered_count: int = 0
    correct_count: int = 0
    accuracy: float | None = None


class SessionAttemptItem(BaseModel):
    """会话明细中的单次作答(GET /practice/sessions/{id})"""

    question_id: uuid.UUID
    stem: str
    qtype: str
    knowledge_name: str | None = None
    chosen_idx: int
    correct_idx: int
    is_correct: bool
    answered_at: datetime


class SessionDetailResponse(BaseModel):
    """历史会话明细"""

    id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    question_count: int
    attempts: list[SessionAttemptItem] = []


class TrendPoint(BaseModel):
    """按周聚合的学习趋势点(GET /practice/trend)"""

    week_start: datetime
    attempts: int = 0
    correct: int = 0


class TrendResponse(BaseModel):
    """最近 N 周作答趋势(旧到新)"""

    weeks: list[TrendPoint] = []


class ClearRecordsResponse(BaseModel):
    """清空练习记录的删除计数(DELETE /practice/records)"""

    deleted_sessions: int = 0
    deleted_attempts: int = 0
    deleted_questions: int = 0


# ============================================================
# 学习主题(用户可管理:内置 4 个 + 自定义,CRUD /practice/topics)
# ============================================================


class TopicOut(BaseModel):
    """学习主题条目(GET /practice/topics)"""

    id: uuid.UUID
    key: str
    name: str
    # 主题视角说明(出题视角 + 分类依据)
    description: str = ""
    is_builtin: bool = True
    # 出题开关:false 时该主题不再出新题(存量不动)
    enabled: bool = True
    sort_order: int = 0
    # 该主题下的知识点数(前端分组展示与删除保护提示用)
    kp_count: int = 0


class TopicCreateRequest(BaseModel):
    """新增自定义主题(POST /practice/topics)"""

    name: str = Field(min_length=1, max_length=64)
    # 主题视角描述(选填;出题质量取决于描述的具体程度)
    description: str = Field(default="", max_length=500)


class TopicUpdateRequest(BaseModel):
    """修改主题(PATCH /practice/topics/{id})

    内置主题仅接受 enabled;自定义主题可改 name/description/enabled。
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
