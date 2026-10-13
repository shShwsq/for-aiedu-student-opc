"""SM-2 遗忘曲线调度(知识点记忆状态更新)

标准 SM-2 算法的简化落地:
- 作答质量映射:答对 quality=4,答错 quality=1(二值化,客观题无半对)
- quality<3 → 记忆重置:repetitions=0, interval=1 天(次日必须重学)
- quality>=3 → interval 序列 1 → 6 → 前值×EF
- EF' = EF + (0.1 - (5-q) * (0.08 + (5-q) * 0.02)),下限 1.3

状态直接体现在 UserKnowledgeState ORM 行上;纯函数 apply_sm2 接收
字段值返回新值,便于单测不依赖数据库。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

# SM-2 常量
DEFAULT_EASE_FACTOR = 2.5
MIN_EASE_FACTOR = 1.3
FIRST_INTERVAL_DAYS = 1.0
SECOND_INTERVAL_DAYS = 6.0

# 作答质量:答对 / 答错
QUALITY_CORRECT = 4
QUALITY_WRONG = 1

# ============================================================
# 作答质量分级(FSRS 4 级 rating):由正确性 + 耗时派生,替换旧二值 quality。
#   Again(1)=答错;Hard(2)=答对但犹豫;Good(3)=正常;Easy(4)=秒对。
# 耗时缺失(老客户端/手工导入)降级:答对一律 Good,不惩罚也不奖励。
# ============================================================
RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4

# 快答(Easy)阈值:难度越高允许越多思考仍算"掌握熟练"。
# easy_ceiling = EASY_BASE_MS + EASY_MS_PER_DIFFICULTY * difficulty(单位毫秒)
EASY_BASE_MS = 1200
EASY_MS_PER_DIFFICULTY = 900
# Hard 阈值:超过 good_floor 仍未答对到提交视为犹豫。同样随难度放宽。
GOOD_BASE_MS = 6000
GOOD_MS_PER_DIFFICULTY = 1500


def derive_rating(
    is_correct: bool,
    latency_ms: int | None,
    difficulty: float,
) -> int:
    """由正确性 + 作答耗时派生 FSRS 4 级 rating(难度用于放宽快答阈值)。

    - 答错 → Again(1)
    - 答对且耗时缺失 → Good(3)(优雅降级)
    - 答对且耗时 ≤ easy 阈值 → Easy(4)(秒对,熟练)
    - 答对且耗时 > good 阈值 → Hard(2)(答对但犹豫)
    - 介于两阈值之间 → Good(3)
    """
    if not is_correct:
        return RATING_AGAIN
    if latency_ms is None:
        return RATING_GOOD
    d = max(0.0, float(difficulty))
    easy_ceiling = EASY_BASE_MS + EASY_MS_PER_DIFFICULTY * d
    good_floor = GOOD_BASE_MS + GOOD_MS_PER_DIFFICULTY * d
    if latency_ms <= easy_ceiling:
        return RATING_EASY
    if latency_ms > good_floor:
        return RATING_HARD
    return RATING_GOOD


# rating → SM-2 quality(0-5):保留 SM-2 路径时把 4 级降级回连续质量。
# 答对的最慢档 Hard 也 >=3(不清零记忆轨迹,只是 EF 增益更小)。
_RATING_TO_QUALITY = {
    RATING_AGAIN: 1,
    RATING_HARD: 3,
    RATING_GOOD: 4,
    RATING_EASY: 5,
}


def rating_to_quality(rating: int) -> int:
    """FSRS 4 级 rating 映射回 SM-2 的 0-5 quality(越界按最近档钳制)"""
    r = max(RATING_AGAIN, min(RATING_EASY, int(rating)))
    return _RATING_TO_QUALITY[r]


@dataclass
class SM2State:
    """记忆状态快照(与 UserKnowledgeState 字段对应,解耦 ORM)

    SM-2 用 ease_factor/interval_days/repetitions;FSRS 额外用 stability/difficulty。
    两种调度器共用本结构便于上层统一读写。
    """

    ease_factor: float = DEFAULT_EASE_FACTOR
    interval_days: float = 0.0
    repetitions: int = 0
    due_at: datetime | None = None
    attempts: int = 0
    correct_count: int = 0
    last_quality: int | None = None
    # FSRS 记忆状态(sm2 路径不使用,默认 0)
    stability: float = 0.0
    difficulty: float = 0.0


def quality_from_correct(is_correct: bool) -> int:
    """客观题二值化质量映射"""
    return QUALITY_CORRECT if is_correct else QUALITY_WRONG


def apply_sm2(state: SM2State, quality: int, now: datetime) -> SM2State:
    """应用一次作答,返回更新后的状态(不修改入参)

    quality 取值 0-5;<3 视为失败,重置记忆轨迹。
    """
    quality = max(0, min(5, int(quality)))

    # EF 更新(无论成败都更新,连续失败会把 EF 压到下限)
    ef = state.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ef = max(MIN_EASE_FACTOR, ef)

    if quality < 3:
        repetitions = 0
        interval = FIRST_INTERVAL_DAYS
    else:
        repetitions = state.repetitions + 1
        if repetitions == 1:
            interval = FIRST_INTERVAL_DAYS
        elif repetitions == 2:
            interval = SECOND_INTERVAL_DAYS
        else:
            interval = max(1.0, round(state.interval_days * ef))

    return SM2State(
        ease_factor=ef,
        interval_days=interval,
        repetitions=repetitions,
        due_at=now + timedelta(days=interval),
        attempts=state.attempts + 1,
        correct_count=state.correct_count + (1 if quality >= 3 else 0),
        last_quality=quality,
    )


def is_overdue(state: SM2State, now: datetime) -> bool:
    """是否到期待复习(从未作答不算到期)"""
    return state.due_at is not None and state.due_at <= now


def overdue_days(state: SM2State, now: datetime) -> float:
    """过期天数(未到期/未作答返回 0)"""
    if state.due_at is None or state.due_at > now:
        return 0.0
    return (now - state.due_at).total_seconds() / 86400.0


# ============================================================
# 可提取性衰减(连续遗忘曲线,FSRS 幂律)
#   R(t,S) = (1 + FACTOR * t/S)^(-DECAY),归一化使 R(S)=目标保留率(0.9)
#   → 稳定性 S 的语义 = 记忆衰减到 90% 可提取所需天数。
# 组卷紧迫度用连续 (1-R) 代替旧的"过期天数封顶"线性量。
# ============================================================
DECAY = 0.5
REQUEST_RETENTION = 0.9
# FACTOR 使 R(S)=REQUEST_RETENTION: (1+FACTOR)^(-DECAY)=0.9
FACTOR = REQUEST_RETENTION ** (-1.0 / DECAY) - 1.0
MIN_STABILITY_DAYS = 0.1


def retrievability(t_days: float, stability: float) -> float:
    """经过 t_days 天后的可提取性 R∈(0,1](stability=衰减到 0.9 的天数)"""
    s = max(MIN_STABILITY_DAYS, float(stability))
    t = max(0.0, float(t_days))
    return (1.0 + FACTOR * t / s) ** (-DECAY)


def retrievability_from_due(
    due_at: datetime | None, interval_days: float, now: datetime
) -> float | None:
    """由到期时间与间隔推当前可提取性(无需额外存 last_review)。

    上次复习时刻 ≈ due_at - interval_days(调度时 now+interval 的逆推)。
    无到期(None)或间隔≤0(未建立轨迹) → 返回 None,调用方回退旧逻辑。
    """
    if due_at is None or interval_days <= 0:
        return None
    last_review = due_at - timedelta(days=interval_days)
    t_days = (now - last_review).total_seconds() / 86400.0
    return retrievability(t_days, interval_days)


def next_interval(stability: float, retention: float = REQUEST_RETENTION) -> float:
    """由 stability 反解下次复习间隔(天):令 R(interval)=retention。

    I = S/FACTOR * (retention^(-1/DECAY) - 1)。retention=0.9 时 I=S。
    """
    s = max(MIN_STABILITY_DAYS, float(stability))
    r = min(0.99, max(0.5, float(retention)))
    return max(1.0, round(s / FACTOR * (r ** (-1.0 / DECAY) - 1.0)))


def advance_schedule(
    state: SM2State,
    rating: int,
    now: datetime,
    elapsed_days: float = 0.0,
    scheduler: str | None = None,
    retention: float | None = None,
) -> SM2State:
    """统一调度入口:按配置分派 SM-2 或 FSRS,返回新状态(不改入参)。

    rating 为 FSRS 4 级(1-4):sm2 路径下经 rating_to_quality 映射回 0-5 quality;
    fsrs 路径直接用 rating。scheduler/retention 缺省读全局配置(非法调度器值回落 sm2)。
    惰性导入 fsrs 避免与本模块循环依赖。
    """
    if scheduler is None:
        from app.config import settings

        scheduler = getattr(settings, "PRACTICE_SCHEDULER", "sm2")
    if scheduler == "fsrs":
        from app.services.practice.fsrs import apply_fsrs

        if retention is None:
            from app.config import settings

            retention = getattr(settings, "PRACTICE_REQUEST_RETENTION", REQUEST_RETENTION)
        return apply_fsrs(state, rating, elapsed_days, now, retention=retention)
    # 默认 SM-2
    return apply_sm2(state, rating_to_quality(rating), now)
