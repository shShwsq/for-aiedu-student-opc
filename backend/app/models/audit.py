"""审查项模型(ReviewItem)——证据驱动可信审查的单一原语

设计:
- 一张 `review_items` 表承载 agent2 的全部审查项,**三态合一**:
  ① 发现风险(verdict ∈ {confirmed, suspected});
  ② 已核查无问题 / 误报剔除(verdict ∈ {none, false_positive} 且 status=covered);
  ③ 缺口·待改进(status ∈ {missing, partial},常 verdict=pending)。
  判读口径集中在本模块的状态常量 + `ReviewItem` 只读属性(见三态判读),
  避免前端/导出/统计多处手写分桶逻辑漂移。
- 每条审查项由一次 `submit_review_item` 即时发射并落库,按轮(round_idx)追加。
- `evidence` 存就近指名的取证引用(source/analysis_basis/verification 三段,各带
  call_ref 指向本轮证据台账);`confidence` 由后端 `evidence.py` 依证据链派生
  (模型不自报数字),仅带证据者有。
- `agent1_ref` 指向被审的 agent1 `Conversation`(可追溯,可空;后端校验归属)。

`Result` 表语义收敛为"知识点",与审查项分两张实体;知识点用
`Result.metadata.source_review_item_id` 回指派生它的审查项(素材同源不焊死)。

表随 Base.metadata.create_all 建表(在 main.py lifespan 显式 import 注册)。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.task import Task


# ============================================================
# 三态判读口径(集中一处:常量 + 谓词,前端/导出/统计复用)
# ============================================================

# origin 来源(审查项派生自哪些输入,须含 requirement/baseline 否则漏审)
ORIGIN_AGENT1_CLAIM = "agent1_claim"
ORIGIN_AGENT1_ACTION = "agent1_action"
ORIGIN_USER_REQUIREMENT = "user_requirement"
ORIGIN_DOMAIN_BASELINE = "domain_baseline"
VALID_ORIGINS = frozenset({
    ORIGIN_AGENT1_CLAIM,
    ORIGIN_AGENT1_ACTION,
    ORIGIN_USER_REQUIREMENT,
    ORIGIN_DOMAIN_BASELINE,
})

# status 覆盖态
STATUS_COVERED = "covered"
STATUS_PARTIAL = "partial"
STATUS_MISSING = "missing"
VALID_STATUSES = frozenset({STATUS_COVERED, STATUS_PARTIAL, STATUS_MISSING})

# verdict 定性判断(agent2 主观结论,与后端派生的 confidence 分列)
VERDICT_CONFIRMED = "confirmed"
VERDICT_SUSPECTED = "suspected"
VERDICT_FALSE_POSITIVE = "false_positive"
VERDICT_NONE = "none"
VERDICT_PENDING = "pending"
VALID_VERDICTS = frozenset({
    VERDICT_CONFIRMED,
    VERDICT_SUSPECTED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_NONE,
    VERDICT_PENDING,
})

# severity(仅风险类有意义)
VALID_SEVERITIES = frozenset({"high", "medium", "low", "info"})

# 三态分桶标签(前端/导出/统计按此归类)
BUCKET_RISK = "risk"        # 发现风险
BUCKET_CLEARED = "cleared"  # 已核查无问题 / 误报剔除
BUCKET_GAP = "gap"          # 缺口·待改进


def classify_status(status: str | None, verdict: str | None) -> str:
    """把 (status, verdict) 组合判读为三态分桶之一。

    判读优先级:风险类 verdict 优先(即便 status 非 covered 也算发现),
    缺口类 status 次之(missing/partial),其余归已核查/清空(cleared)。
    集中于此,避免各处漂移。
    """
    if verdict in (VERDICT_CONFIRMED, VERDICT_SUSPECTED):
        return BUCKET_RISK
    if status in (STATUS_MISSING, STATUS_PARTIAL):
        return BUCKET_GAP
    # verdict ∈ {none, false_positive} 且 covered(或其他)→ 已核查/剔除误报
    return BUCKET_CLEARED


class ReviewItem(Base):
    """审查项(三态合一):agent2 每条审查结论挂可复核证据的载体"""

    __tablename__ = "review_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 由第几轮 agent2 审查产出(从 1 开始,与 Result 同构,按轮追加)
    round_idx: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 被核实的对象:agent1 的某条具体结论/动作,或用户的一条要求(必填)
    review_target: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 来源:agent1_claim / agent1_action / user_requirement / domain_baseline
    origin: Mapped[str] = mapped_column(String(24), nullable=False, default=ORIGIN_AGENT1_CLAIM)
    # 指向被审的 agent1 对话(可追溯,可空;后端校验属本任务 agent1 对话)
    agent1_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 分组标签(仅用于报告/分组,不再是组织单元)
    dimension: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=STATUS_COVERED)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # {source{file_path,line,quote,call_ref},
    #  analysis_basis{ref_url,ref_status,ref_authority,call_ref},
    #  verification{method,verified,poc_evidence,call_ref}};缺失段=不适用
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # {tier,label,score}(后端派生,非模型自报;仅带证据者有)
    confidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # call_ref 是否全部核验通过
    evidence_validated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 有引用但核验不通过(降置信)
    evidence_mismatch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # backref:Task.review_items(不改 task.py,靠 backref 挂上)
    task: Mapped[Task] = relationship(backref="review_items")

    # ---- 三态只读判读(复用集中口径 classify_status) ----

    @property
    def bucket(self) -> str:
        """三态分桶:risk / cleared / gap"""
        return classify_status(self.status, self.verdict)

    @property
    def is_risk(self) -> bool:
        return self.bucket == BUCKET_RISK

    @property
    def is_cleared(self) -> bool:
        return self.bucket == BUCKET_CLEARED

    @property
    def is_gap(self) -> bool:
        return self.bucket == BUCKET_GAP
