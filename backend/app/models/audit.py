"""审查项模型(ReviewItem)——证据驱动可信审查的单一原语

设计:
- 一张 `review_items` 表承载 agent2 的全部审查项,**按"核实结果"三分档**
  (主语是"检查助手对这条待核实对象的核实结论",不再是"你的项目里有几类条目"):
  ① 已核实成立 established(verdict=confirmed);
  ② 已核实不成立 not_established(verdict ∈ {none, false_positive});
  ③ 待核实 pending_verify(suspected / pending / 未判定,或 status ∈ {partial, missing})。
  判读口径集中在本模块的状态常量 + `ReviewItem` 只读属性(见三分档判读),
  避免前端/导出/统计多处手写分桶逻辑漂移。
- `suspected`(存疑未取证)**不再算作成立**:置信由证据派生,没取到证就不能进结果档。
- 每条审查项由一次 `submit_review_item` 即时发射并落库,按轮(round_idx)追加。
- `evidence` 存就近指名的取证引用(source/analysis_basis/verification 三段,各带
  call_ref 指向本轮证据台账);`confidence` 由后端 `evidence.py` 依证据链派生
  (模型不自报数字),仅带证据者有。
- `plan_item_id` 指向它核实的是哪条审查计划项(`submit_review_plan.items[].id`,形如 P1),
  规划对账按 id 精确命中,不再靠 target 文本子串匹配(旧匹配全失败会伪造"缺口"条目)。
- `agent1_ref` 指向被审的 agent1 `Conversation`(可追溯,可空;后端校验归属)。
- 字段语义与命名(原 `description`/`suggestion` 已改名):
  `conclusion` = 审查结论(分析过程,可引用原始证据);`follow_up` = 可追问内容
  (一句可点给 agent1 执行的问句)。
  升级需手动执行(幂等迁移见 `migrate_review_item_columns`):
  ALTER TABLE review_items RENAME COLUMN description TO conclusion;
  ALTER TABLE review_items RENAME COLUMN suggestion TO follow_up;
  ALTER TABLE review_items ADD COLUMN plan_item_id VARCHAR(16);

`Result` 表语义收敛为"知识点",与审查项分两张实体;知识点用
`Result.metadata.source_review_item_id` 回指派生它的审查项(素材同源不焊死)。

表随 Base.metadata.create_all 建表(在 main.py lifespan 显式 import 注册)。
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship

from app.database import Base
from app.models.task import Task


# ============================================================
# 三分档判读口径(集中一处:常量 + 谓词,前端/导出/统计复用)
# ============================================================

# origin 来源:agent2 只复核 agent1 的产出——分"AI 的结论"(agent1_claim)与
# "AI 修改/生成的东西"(agent1_action:代码/文件/产物)两类。
# 旧版曾有 user_requirement/domain_baseline,用来逼 agent2 去补查 agent1 没碰的领域;
# 职责收敛后 agent1 未查的一律转追问、agent2 不再自查,故从可发射枚举中移除。
ORIGIN_AGENT1_CLAIM = "agent1_claim"
ORIGIN_AGENT1_ACTION = "agent1_action"
# legacy:agent2 不再产生,仅历史行仍可能带(不在 VALID_ORIGINS 里,新发射会被 clamp 掉)
ORIGIN_USER_REQUIREMENT = "user_requirement"
ORIGIN_DOMAIN_BASELINE = "domain_baseline"
VALID_ORIGINS = frozenset({
    ORIGIN_AGENT1_CLAIM,
    ORIGIN_AGENT1_ACTION,
})

# origin 的用户可读标签(报告导出/统计用;前端同名单份在 utils/reviewBucket)
# 后两项是 legacy:agent2 已不再产生,仅为历史行显示兜底(免让报告露出英文枚举)。
ORIGIN_LABELS: dict[str, str] = {
    ORIGIN_AGENT1_CLAIM: "AI助手 的结论",
    ORIGIN_AGENT1_ACTION: "AI助手 的改动/生成物",
    ORIGIN_USER_REQUIREMENT: "你的要求",
    ORIGIN_DOMAIN_BASELINE: "领域经验",
}

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

# severity(仅已核实成立类有意义)
VALID_SEVERITIES = frozenset({"high", "medium", "low", "info"})

# 三分档标签(前端/导出/统计按此归类)
BUCKET_ESTABLISHED = "established"            # 已核实成立
BUCKET_NOT_ESTABLISHED = "not_established"    # 已核实不成立
BUCKET_PENDING_VERIFY = "pending_verify"      # 待核实

BUCKET_LABELS: dict[str, str] = {
    BUCKET_ESTABLISHED: "已核实成立",
    BUCKET_NOT_ESTABLISHED: "已核实不成立",
    BUCKET_PENDING_VERIFY: "待核实",
}
# 展示顺序(前端分档标题行 / 报告导出小节 / 聚合计数)
BUCKET_ORDER: tuple[str, ...] = (
    BUCKET_ESTABLISHED, BUCKET_NOT_ESTABLISHED, BUCKET_PENDING_VERIFY,
)


def classify_status(status: str | None, verdict: str | None) -> str:
    """把 (status, verdict) 组合判读为三分档之一。

    判读优先级:
    ① `confirmed` 优先归"已核实成立"(即便 status 是 partial/missing——
      取到过证就不该被覆盖态降级掉,免得真问题从结果区消失);
    ② `none` / `false_positive` 归"已核实不成立"(核过了,指控不成立/是误报);
    ③ 其余一律"待核实"——含 `suspected`(存疑未取证)、`pending`、无 verdict,
      以及 missing/partial。这是与旧口径的关键差异:suspected 不再冒充成立。
    集中于此,避免各处漂移。
    """
    if verdict == VERDICT_CONFIRMED:
        return BUCKET_ESTABLISHED
    if verdict in (VERDICT_NONE, VERDICT_FALSE_POSITIVE):
        return BUCKET_NOT_ESTABLISHED
    return BUCKET_PENDING_VERIFY


class ReviewItem(Base):
    """审查项(三分档):agent2 每条审查结论挂可复核证据的载体"""

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
    # 审查结论(分析过程,可引用原始证据)——旧列名 description
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 待核实对象:agent1 的某条具体结论或它修改/生成的产物(必填)
    review_target: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 来源:agent1_claim(结论/发现) / agent1_action(改动/生成物)
    origin: Mapped[str] = mapped_column(String(24), nullable=False, default=ORIGIN_AGENT1_CLAIM)
    # 指向被审的 agent1 对话(可追溯,可空;后端校验属本任务 agent1 对话)
    agent1_ref: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 核实的是哪条审查计划项(submit_review_plan.items[].id,形如 P1;可空)
    # 规划对账靠它精确命中(旧版拿 target 文本做子串匹配,几乎全失配)
    plan_item_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 分组标签(仅用于报告/分组,不再是组织单元)
    dimension: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(12), nullable=False, default=STATUS_COVERED)
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # {source{file_path,line,quote,call_ref},
    #  analysis_basis{ref_url,ref_status,ref_authority,call_ref},
    #  verification{method,verified,poc_evidence,call_ref}};缺失段=不适用
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # {tier,label}(后端派生,非模型自报;仅带证据者有)
    confidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # call_ref 是否全部核验通过
    evidence_validated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 有引用但核验不通过(降置信)
    evidence_mismatch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # 可追问内容(一句可点给 agent1 执行的问句)——旧列名 suggestion
    follow_up: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # backref:Task.review_items(不改 task.py,靠 backref 挂上)
    # 删除语义必须显式声明:默认 cascade 仅 save-update/merge,删 Task 时 ORM 会
    # 抢先发 UPDATE review_items SET task_id=NULL 去"断开"子行,而 task_id 非空 →
    # NotNullViolation → 删除任务 500。FK 已带 ondelete=CASCADE,故用
    # passive_deletes=True 让 ORM 不碰子行、交数据库级联删除(与 user.git_bindings 同套路);
    # cascade=all,delete-orphan 与 Task 其它关系(Conversations/Results/Artifacts)口径一致。
    task: Mapped[Task] = relationship(
        backref=backref(
            "review_items", cascade="all, delete-orphan", passive_deletes=True,
        )
    )

    # ---- 三分档只读判读(复用集中口径 classify_status) ----

    @property
    def bucket(self) -> str:
        """三分档:established / not_established / pending_verify"""
        return classify_status(self.status, self.verdict)

    @property
    def bucket_label(self) -> str:
        return BUCKET_LABELS[self.bucket]

    @property
    def is_established(self) -> bool:
        return self.bucket == BUCKET_ESTABLISHED

    @property
    def is_not_established(self) -> bool:
        return self.bucket == BUCKET_NOT_ESTABLISHED

    @property
    def is_pending_verify(self) -> bool:
        return self.bucket == BUCKET_PENDING_VERIFY


# ============================================================
# 存量库升级迁移(无 Alembic:create_all 不改已存在表)
# ============================================================


def migrate_review_item_columns() -> None:
    """幂等改列名 + 补 plan_item_id 列(字段语义收敛,避免两套称呼漂移)

    背景同 `migrate_conversation_tool_call_id`:项目用 Base.metadata.create_all,
    已存在的表不会自动改名/加列。旧库:
    - `description` → `conclusion`(审查结论;旧标签"发现问题"与正文实际承载的分析不符)
    - `suggestion`  → `follow_up`(可追问内容)
    - 新增 `plan_item_id`(规划对账 id 链路)
    全新库(create_all 已按新形状建好)或已迁过 → 直接返回。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)
    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("review_items"):
            return  # 全新库由 create_all 按新形状建好
        cols = {c["name"] for c in insp.get_columns("review_items")}
        renamed: list[str] = []
        if "description" in cols and "conclusion" not in cols:
            conn.execute(text(
                "ALTER TABLE review_items RENAME COLUMN description TO conclusion"
            ))
            renamed.append("description→conclusion")
        if "suggestion" in cols and "follow_up" not in cols:
            conn.execute(text(
                "ALTER TABLE review_items RENAME COLUMN suggestion TO follow_up"
            ))
            renamed.append("suggestion→follow_up")
        if "plan_item_id" not in cols:
            conn.execute(text(
                "ALTER TABLE review_items ADD COLUMN plan_item_id VARCHAR(16)"
            ))
            renamed.append("+plan_item_id")
        if renamed:
            conn.commit()
            log.info("review_items 列迁移完成:%s", ", ".join(renamed))


def purge_legacy_plan_backfill_items() -> None:
    """一次性清掉旧版"规划对账伪造的缺口审查项"

    背景:旧对账用 target 文本子串匹配判断计划项是否被核实,两条独立成句的长中文
    几乎不可能互为子串 → 计划内条目被整批回填成 `status=missing` 的假缺口(实测一次
    审查 8 条计划全被误判,凭空多出 8 条"[计划回填]"条目,聚合里"核实 0")。
    现改为 plan_item_id 对账 + 未核实只进 `_review.plan.unverified`,不再落库。
    旧数据是 bug 产物(无证据/无结论/不可点开),删掉比留着诚实;幂等:0 行时不记日志。
    """
    import logging

    from sqlalchemy import inspect, text

    from app.database import engine

    log = logging.getLogger(__name__)
    with engine.connect() as conn:
        insp = inspect(conn)
        if not insp.has_table("review_items"):
            return
        cols = {c["name"] for c in insp.get_columns("review_items")}
        # 正文列名取决于迁移是否已跑过(本函数排在改名迁移之后)
        desc_col = "conclusion" if "conclusion" in cols else (
            "description" if "description" in cols else None
        )
        if desc_col is None:
            return
        result = conn.execute(
            text(
                "DELETE FROM review_items "
                "WHERE title LIKE :prefix AND status = 'missing' "
                f"AND {desc_col} = :placeholder"
            ),
            {"prefix": "[计划回填]%", "placeholder": "计划内未执行,由规划对账回填"},
        )
        conn.commit()
        if result.rowcount:
            log.info(
                "review_items 已清理旧版规划对账伪造条目 %s 条(分档改用 plan_item_id 对账)",
                result.rowcount,
            )
