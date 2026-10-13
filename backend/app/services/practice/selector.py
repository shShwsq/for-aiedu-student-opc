"""综合选题策略:薄弱点 + 难度匹配 + 记忆状态加权组卷

按需即时练习:用户点「开始练习」时一次组卷。每道候选题打分:

    score = 3.0 * overdue_urgency      # 到期复习优先级最高
          + 2.0 * weakness             # 知识点错误率强化
          + 1.5 * difficulty_match     # 与用户能力匹配
          + 0.5 * novelty              # 新题引入
          + random(0, 0.3)             # 抖动防固定套路

约束:
- 同一知识点最多占 60%
- 存在到期复习题时,复习题占比不低于 50%(不够则全选)
- 冷启动(无任何记忆状态):只取 difficulty ≤ 2 的新题

实现为纯函数(输入 CandidateInfo 列表),与 ORM 解耦便于单测。
"""
import math
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.practice.sm2 import retrievability_from_due

# 打分权重
W_OVERDUE = 3.0
W_WEAKNESS = 2.0
W_DIFFICULTY = 1.5
W_NOVELTY = 0.5
JITTER_MAX = 0.3

# 薄弱点判定阈值:错误率 > 40% 且样本 >= 3
WEAKNESS_ERROR_RATE = 0.4
WEAKNESS_MIN_ATTEMPTS = 3
# 过期紧迫度封顶天数(无稳定轨迹时的回退线性量)
OVERDUE_CAP_DAYS = 4.0
# 难度匹配高斯带宽 SIGMA:exp(-((d-a)^2)/(2*SIGMA^2)),越小越严格贴合能力值
DIFFICULTY_SIGMA = 1.0
# 题目新近度惩罚半衰期(天):上次做过越近,新鲜度越低
NOVELTY_HALF_LIFE_DAYS = 7.0
# 约束比例
MAX_SAME_KP_RATIO = 0.6
REVIEW_MIN_RATIO = 0.5
# 冷启动:无记忆状态时只出低难度新题
COLD_START_MAX_DIFFICULTY = 2.0


@dataclass
class CandidateInfo:
    """一道候选题及其知识点记忆上下文"""

    question: Any  # ORM Question(策略层不感知其字段,仅原样回传)
    question_id: Any
    kp_id: Any
    difficulty: float
    # 该题历史作答次数(0 = 新题)
    question_attempts: int
    # 知识点 SM-2 状态(从未作答过该知识点时为 None)
    kp_due_at: datetime | None
    kp_attempts: int
    kp_correct_count: int
    # 知识点当前记忆间隔(SM-2 interval_days;作可提取性衰减的稳定性代理)
    kp_interval_days: float = 0.0
    # 难度匹配用的分面能力维度(如 learning_topic key;None → 用全局能力值)
    facet: str | None = None
    # 该题上次被作答时刻(None → 无新近度惩罚,降级为旧"答过/没答过"二值)
    last_seen_at: datetime | None = None


def _error_rate(c: CandidateInfo) -> float:
    if c.kp_attempts <= 0:
        return 0.0
    return 1.0 - c.kp_correct_count / c.kp_attempts


def _overdue_urgency(c: CandidateInfo, now: datetime) -> float:
    """到期紧迫度 0-1:基于连续可提取性 R,越接近遗忘越急(1-R)。

    未到期 → 0.0(不占复习高权重)。无稳定轨迹(间隔≤0)时回退旧"过期天数
    封顶"线性量,保证升级前后行为一致。
    """
    if c.kp_due_at is None or c.kp_due_at > now:
        return 0.0
    r = retrievability_from_due(c.kp_due_at, c.kp_interval_days, now)
    if r is None:
        # 回退:无间隔轨迹时按过期天数线性
        days = overdue_days_raw(c.kp_due_at, now)
        return min(days + 1, OVERDUE_CAP_DAYS + 1) / (OVERDUE_CAP_DAYS + 1)
    # R=0.9(刚到期) → urgency≈0.1;R 越低(越久未复习)→ urgency 越接近 1
    return max(0.0, min(1.0, 1.0 - r))


def overdue_days_raw(due_at: datetime, now: datetime) -> float:
    return max(0.0, (now - due_at).total_seconds() / 86400.0)


def _weakness(c: CandidateInfo) -> float:
    if c.kp_attempts < WEAKNESS_MIN_ATTEMPTS:
        return 0.0
    rate = _error_rate(c)
    return rate if rate > WEAKNESS_ERROR_RATE else 0.0


def _difficulty_match(c: CandidateInfo, ability: float) -> float:
    """难度匹配 0-1:高斯峰在能力值处(比旧的线性衰减更平滑,贴合最近发展区)"""
    return math.exp(-((c.difficulty - ability) ** 2) / (2.0 * DIFFICULTY_SIGMA ** 2))


def _novelty(c: CandidateInfo, now: datetime) -> float:
    """新鲜度 0-1:新题=1.0;做过且无 last_seen_at → 0.0(旧二值行为);
    有 last_seen_at 则按指数衰减(刚做过降权,隔得越久越接近 1)。"""
    if c.question_attempts == 0:
        return 1.0
    if c.last_seen_at is None:
        return 0.0
    days = max(0.0, (now - c.last_seen_at).total_seconds() / 86400.0)
    return 1.0 - math.exp(-math.log(2.0) * days / NOVELTY_HALF_LIFE_DAYS)


def is_review_due(c: CandidateInfo, now: datetime) -> bool:
    return c.kp_due_at is not None and c.kp_due_at <= now


def score_candidate(
    c: CandidateInfo,
    ability: float,
    now: datetime,
    jitter: float | None = None,
    ability_map: dict[str, float] | None = None,
) -> float:
    """单题综合得分(jitter 可注入固定值,便于测试)。

    ability_map 非空且 c.facet 命中时,难度匹配用该分面能力值(如该学习主题的
    能力),否则回退全局 ability —— 避免用单一标量能力值套到所有主题。
    """
    if jitter is None:
        jitter = random.uniform(0.0, JITTER_MAX)
    eff_ability = ability
    if ability_map and c.facet is not None and c.facet in ability_map:
        eff_ability = ability_map[c.facet]
    return (
        W_OVERDUE * _overdue_urgency(c, now)
        + W_WEAKNESS * _weakness(c)
        + W_DIFFICULTY * _difficulty_match(c, eff_ability)
        + W_NOVELTY * _novelty(c, now)
        + jitter
    )


def select_questions(
    candidates: list[CandidateInfo],
    ability: float,
    count: int,
    now: datetime,
    ability_map: dict[str, float] | None = None,
) -> list[CandidateInfo]:
    """组卷:打分 + 知识点多样性约束 + 复习占比约束

    返回按出题顺序排列的候选题(复习题优先,末尾按知识点交错重排)。
    ability_map 非空时按候选 facet 取分面能力值做难度匹配(见 score_candidate)。
    """
    if not candidates or count <= 0:
        return []

    # 冷启动:完全没有记忆状态 → 只出低难度新题
    has_memory = any(c.kp_due_at is not None for c in candidates)
    if not has_memory:
        pool = [c for c in candidates if c.difficulty <= COLD_START_MAX_DIFFICULTY]
        pool.sort(key=lambda c: c.difficulty)
        return pool[:count]

    # 打分(含随机抖动,防固定套路)
    scored = [
        (score_candidate(c, ability, now, ability_map=ability_map), c)
        for c in candidates
    ]
    scored.sort(key=lambda t: t[0], reverse=True)

    max_per_kp = max(1, math.floor(count * MAX_SAME_KP_RATIO))
    review_candidates = [c for c in candidates if is_review_due(c, now)]
    review_required = (
        math.ceil(count * REVIEW_MIN_RATIO) if review_candidates else 0
    )
    # 复习题不够时全选
    review_required = min(review_required, len(review_candidates))

    picked: list[CandidateInfo] = []
    picked_ids: set = set()
    kp_counter: dict = {}

    def _try_pick(c: CandidateInfo) -> bool:
        if c.question_id in picked_ids:
            return False
        if kp_counter.get(c.kp_id, 0) >= max_per_kp:
            return False
        picked.append(c)
        picked_ids.add(c.question_id)
        kp_counter[c.kp_id] = kp_counter.get(c.kp_id, 0) + 1
        return True

    # 第一轮:复习题优先,保证占比
    if review_required:
        due_scored = [
            (s, c) for s, c in scored if is_review_due(c, now)
        ]
        for _, c in due_scored:
            if len([x for x in picked if is_review_due(x, now)]) >= review_required:
                break
            _try_pick(c)

    # 第二轮:按总分补足
    for _, c in scored:
        if len(picked) >= count:
            break
        _try_pick(c)

    # 第三轮:多样性约束过紧导致短填时,逐轮放宽 max_per_kp(×1.5)直到填满、
    # 某一轮无新增(候选真耗尽)或达到硬上限 —— 避免给用户一个比预期短的会话
    hard_cap = max(count, max_per_kp)
    while len(picked) < count and max_per_kp < hard_cap:
        before = len(picked)
        max_per_kp = min(hard_cap, max(max_per_kp + 1, math.ceil(max_per_kp * 1.5)))
        for _, c in scored:
            if len(picked) >= count:
                break
            _try_pick(c)
        if len(picked) == before:
            break  # 本轮无任何新增,后续放宽也无济于事

    return _interleave(picked[:count])


def _interleave(picked: list[CandidateInfo]) -> list[CandidateInfo]:
    """按知识点轮转重排,避免同一知识点连续出现(spacing/interleaving 效应)。

    保持原分数高者先出的大致次序:按入参顺序分桶到各 kp,再轮流从每个桶取 1 题。
    单知识点或空输入原样返回。
    """
    if len(picked) <= 1:
        return picked
    from collections import OrderedDict

    buckets: "OrderedDict[Any, list[CandidateInfo]]" = OrderedDict()
    for c in picked:
        buckets.setdefault(c.kp_id, []).append(c)
    if len(buckets) <= 1:
        return picked
    out: list[CandidateInfo] = []
    remaining = True
    while remaining:
        remaining = False
        for bucket in buckets.values():
            if bucket:
                out.append(bucket.pop(0))
                remaining = True
    return out
