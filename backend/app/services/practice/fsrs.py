"""FSRS-4.5 遗忘曲线调度器(公开默认参数,无需训练数据即可上线)

FSRS(Free Spaced Repetition Scheduler)比经典 SM-2 更贴合实测的遗忘规律:
- 用「稳定性 S(记忆保持强度)」与「难度 D(题目内在难度,1-10)」两个连续量建模,
  而非 SM-2 的 repetitions×EF;
- 可提取性 R 走幂律衰减(sm2.retrievability),复习时机由当前 R 反推;
- 质量分级用 FSRS 的 4 级 rating(1=Again,2=Hard,3=Good,4=Easy),
  由作答正确性 + 耗时派生(见 sm2.derive_rating)。

落地策略(与计划一致):
- 采用 FSRS-4.5 论文/开源实现公开的**默认权重**(DEFAULT_W),不做在线参数拟合;
  这样无需积累足够历史流水即可上线,行为确定、可单测。
- 后续可选:用 practice_attempts(latency_ms/is_correct)流水跑一次离线优化,
  把拟合出的权重替换 DEFAULT_W(见 docs 备注,列为后续项)。

输出契约:返回的 SM2State 仍写 interval_days / due_at(SM2State 兼容),
其中 interval_days 由 stability 反解(sm2.next_interval),因此下游
selector / stats / 看板只认 due_at,零破坏。SM-2 专有列 ease_factor 在
fsrs 路径下不更新(保留上次值,仅作展示兜底)。
"""
import math
from datetime import datetime, timedelta

from app.services.practice.sm2 import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    SM2State,
    next_interval,
    retrievability,
)

# FSRS-4.5 公开默认权重(17 个);索引语义见下方各公式注释。
# 来源:open-spaced-repetition(fsrs-rs / ts-fsrs)发布的 4.5 默认参数。
DEFAULT_W: tuple[float, ...] = (
    0.4, 0.6, 2.4, 5.8,   # w0..w3:各 rating 的初始稳定性 S0
    4.93, 0.94,            # w4,w5:初始难度 D0 的均值与随 rating 的衰减
    0.86,                  # w6:难度随 rating 的更新步长
    0.01,                  # w7:难度均值回归系数
    1.49,                  # w8:召回成功率稳定增长因子
    0.14,                  # w9:稳定性对旧稳定性的幂(s^-w9)
    0.94,                  # w10:可提取性 R 对成功增量影响
    2.18,                  # w11:失败遗忘稳定性的标度
    0.05,                  # w12:遗忘稳定性对难度 D 的幂
    0.34,                  # w13:遗忘稳定性对 (S+1) 的幂
    1.26,                  # w14:遗忘稳定性对 R 的指数
    0.29,                  # w15:Hard 惩罚系数
    2.61,                  # w16:Easy 奖励系数
)

DIFFICULTY_MIN = 1.0
DIFFICULTY_MAX = 10.0
MIN_STABILITY = 0.1


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _init_stability(rating: int, w=DEFAULT_W) -> float:
    """新卡首次复习各 rating 的初始稳定性 S0(天)"""
    return max(MIN_STABILITY, w[rating - 1])


def _init_difficulty(rating: int, w=DEFAULT_W) -> float:
    """新卡首次复习的初始难度 D0(1-10):rating 越高越觉得题简单"""
    d = w[4] - math.exp(w[5] * (rating - 1)) + 1.0
    return _clamp(d, DIFFICULTY_MIN, DIFFICULTY_MAX)


def _next_difficulty(d: float, rating: int, w=DEFAULT_W) -> float:
    """已有轨迹的难度更新:向 D0(Easy) 做均值回归,再按 rating 上下浮动。"""
    raw = d - w[6] * (rating - RATING_GOOD)
    # 均值回归目标取"答对且熟练"(Easy)对应的初始难度基准
    reversion_target = _init_difficulty(RATING_EASY, w)
    new_d = w[7] * reversion_target + (1 - w[7]) * raw
    return _clamp(new_d, DIFFICULTY_MIN, DIFFICULTY_MAX)


def _next_recall_stability(
    d: float, s: float, r: float, rating: int, w=DEFAULT_W
) -> float:
    """成功召回(rating≥2)后的新稳定性:按 S 幂、难度、当前可提取性放大。"""
    hard_penalty = w[15] if rating == RATING_HARD else 1.0
    easy_bonus = w[16] if rating == RATING_EASY else 1.0
    inner = (
        math.exp(w[8])
        * (11.0 - d)
        * (s ** (-w[9]))
        * (math.exp((1 - r) * w[10]) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return max(MIN_STABILITY, s * (inner + 1.0))


def _next_forget_stability(d: float, s: float, r: float, w=DEFAULT_W) -> float:
    """失败(Again)后重置的较小稳定性:遗忘曲线"打回重学"。"""
    new_s = (
        w[11]
        * (d ** (-w[12]))
        * (((s + 1.0) ** w[13]) - 1.0)
        * math.exp((1 - r) * w[14])
    )
    return _clamp(new_s, MIN_STABILITY, s)  # 失败后稳定性不超过原值


def apply_fsrs(
    state: SM2State,
    rating: int,
    elapsed_days: float,
    now: datetime,
    retention: float = 0.9,
    w=DEFAULT_W,
) -> SM2State:
    """应用一次作答推进 FSRS 状态,返回新 SM2State(不改入参)。

    rating:FSRS 4 级(1=Again,2=Hard,3=Good,4=Easy)。
    elapsed_days:距上次复习的真实天数(首次=0);用于算当前可提取性 R。
    retention:目标保留率,决定下次排期 interval=next_interval(S)。
    新卡的稳定性/难度走 init;已有轨迹走 recall/forget 公式。
    Again 视为打回重学(repetitions 归 0,间隔取学习下限 1 天)。
    """
    rating = int(_clamp(rating, RATING_AGAIN, RATING_EASY))
    s_prev = max(MIN_STABILITY, state.stability) if state.stability > 0 else 0.0
    d_prev = state.difficulty if state.difficulty > 0 else _init_difficulty(rating, w)
    is_new_card = state.stability <= 0 or state.repetitions == 0

    if is_new_card:
        new_s = _init_stability(rating, w)
        new_d = _init_difficulty(rating, w)
    else:
        r_now = retrievability(max(0.0, elapsed_days), s_prev)
        new_d = _next_difficulty(d_prev, rating, w)
        if rating == RATING_AGAIN:
            new_s = _next_forget_stability(d_prev, s_prev, r_now, w)
        else:
            new_s = _next_recall_stability(d_prev, s_prev, r_now, rating, w)

    if rating == RATING_AGAIN:
        repetitions = 0
        interval = 1.0  # 重学:次日必须再来(SM-2 一致的止血节奏)
    else:
        repetitions = state.repetitions + 1
        interval = next_interval(new_s, retention)

    return SM2State(
        ease_factor=state.ease_factor,  # fsrs 路径不使用 EF,原样带回
        interval_days=interval,
        repetitions=repetitions,
        due_at=now + timedelta(days=interval),
        attempts=state.attempts + 1,
        correct_count=state.correct_count + (1 if rating != RATING_AGAIN else 0),
        last_quality=rating,
        stability=new_s,
        difficulty=new_d,
    )
