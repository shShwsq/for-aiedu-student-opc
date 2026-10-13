"""FSRS-4.5 调度器 + 质量分级 + 可提取性衰减 单元测试

覆盖:
- derive_rating:正确性 + 耗时 → 4 级 rating(含耗时缺失降级、难度放宽阈值)
- rating_to_quality:4 级映射回 SM-2 0-5 quality
- retrievability:幂律 R(0)=1、R(S)=目标保留率、随时间单调下降
- next_interval:retention=0.9 时 interval=stability
- apply_fsrs:新卡初始、稳定增长、Again 打回重学、不改入参
- advance_schedule:sm2 / fsrs 分派一致对外产出 due_at/interval
"""
from datetime import datetime, timedelta

from app.services.practice.fsrs import apply_fsrs
from app.services.practice.sm2 import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    SM2State,
    advance_schedule,
    derive_rating,
    next_interval,
    rating_to_quality,
    retrievability,
)

NOW = datetime(2026, 8, 16, 12, 0, 0)


# ============================================================
# derive_rating
# ============================================================


def test_rating_wrong_is_again():
    # 答错无论耗时都是 Again
    assert derive_rating(False, 500, 3.0) == RATING_AGAIN
    assert derive_rating(False, None, 3.0) == RATING_AGAIN


def test_rating_no_latency_degrades_to_good():
    # 耗时缺失(老客户端)答对 → Good,不惩罚不奖励
    assert derive_rating(True, None, 3.0) == RATING_GOOD


def test_rating_fast_correct_is_easy():
    # 秒对(低于快答阈值)→ Easy
    assert derive_rating(True, 800, 3.0) == RATING_EASY


def test_rating_slow_correct_is_hard():
    # 犹豫很久才对(超过 good 下限)→ Hard
    assert derive_rating(True, 60000, 3.0) == RATING_HARD


def test_rating_threshold_scales_with_difficulty():
    # 同一 3000ms:简单题算犹豫(Hard/Good 边界外),难题可能仍算熟练
    # 难度 1 的快答上限 = 1200+900*1=2100 → 3000>2100 不是 Easy
    assert derive_rating(True, 3000, 1.0) != RATING_EASY
    # 难度 5 的快答上限 = 1200+900*5=5700 → 3000<=5700 是 Easy
    assert derive_rating(True, 3000, 5.0) == RATING_EASY


def test_rating_to_quality_mapping():
    assert rating_to_quality(RATING_AGAIN) == 1
    assert rating_to_quality(RATING_HARD) == 3   # 答对最慢档仍 >=3(不清零)
    assert rating_to_quality(RATING_GOOD) == 4
    assert rating_to_quality(RATING_EASY) == 5
    # 越界钳制
    assert rating_to_quality(0) == 1
    assert rating_to_quality(9) == 5


# ============================================================
# retrievability / next_interval
# ============================================================


def test_retrievability_at_zero_is_one():
    assert abs(retrievability(0.0, 5.0) - 1.0) < 1e-9


def test_retrievability_at_stability_is_target():
    # R(S) = 目标保留率 0.9(stability 定义)
    assert abs(retrievability(5.0, 5.0) - 0.9) < 1e-6


def test_retrievability_monotonic_decay():
    s = 5.0
    assert retrievability(2.0, s) > retrievability(5.0, s) > retrievability(20.0, s)


def test_next_interval_retention_09_equals_stability():
    # retention=0.9 时 interval ≈ stability(四舍五入到整,至少 1)
    assert next_interval(5.0, 0.9) == 5
    assert next_interval(12.0, 0.9) == 12


def test_next_interval_higher_retention_shorter():
    # 要求更高保留率 → 排期更密(interval 更短)
    assert next_interval(10.0, 0.95) < next_interval(10.0, 0.85)


# ============================================================
# apply_fsrs
# ============================================================


def test_fsrs_new_card_initial_state():
    state = SM2State()  # stability=0, reps=0 → 新卡
    new = apply_fsrs(state, RATING_GOOD, elapsed_days=0.0, now=NOW)
    assert new.attempts == 1
    assert new.repetitions == 1
    assert new.stability > 0
    assert 1.0 <= new.difficulty <= 10.0
    assert new.due_at is not None and new.due_at > NOW
    # 答对计入 correct_count
    assert new.correct_count == 1


def test_fsrs_stability_grows_on_success():
    state = SM2State(stability=5.0, difficulty=5.0, repetitions=3,
                     interval_days=5.0, due_at=NOW - timedelta(days=5))
    new = apply_fsrs(state, RATING_GOOD, elapsed_days=5.0, now=NOW)
    # 按时复习且答对 → 稳定性提升
    assert new.stability > state.stability


def test_fsrs_again_resets_learning():
    state = SM2State(stability=20.0, difficulty=5.0, repetitions=5,
                     interval_days=20.0, due_at=NOW - timedelta(days=20))
    new = apply_fsrs(state, RATING_AGAIN, elapsed_days=20.0, now=NOW)
    assert new.repetitions == 0
    assert new.interval_days == 1.0
    assert new.stability < state.stability      # 遗忘后稳定性下调
    assert new.due_at == NOW + timedelta(days=1)
    assert new.correct_count == state.correct_count  # Again 不计正确


def test_fsrs_does_not_mutate_input():
    state = SM2State(stability=5.0, difficulty=5.0, repetitions=2)
    apply_fsrs(state, RATING_EASY, elapsed_days=1.0, now=NOW)
    assert state.stability == 5.0
    assert state.repetitions == 2


def test_fsrs_easy_beats_hard_interval():
    base = SM2State(stability=5.0, difficulty=5.0, repetitions=3,
                    interval_days=5.0, due_at=NOW)
    hard = apply_fsrs(base, RATING_HARD, elapsed_days=5.0, now=NOW)
    easy = apply_fsrs(base, RATING_EASY, elapsed_days=5.0, now=NOW)
    assert easy.stability > hard.stability
    assert easy.interval_days >= hard.interval_days


# ============================================================
# advance_schedule 分派
# ============================================================


def test_advance_schedule_sm2_path():
    state = SM2State()
    new = advance_schedule(state, RATING_GOOD, NOW, scheduler="sm2")
    # Good→quality4:SM-2 首次答对 interval=1 天,reps=1;不写 stability
    assert new.interval_days == 1.0
    assert new.repetitions == 1
    assert new.stability == 0.0


def test_advance_schedule_fsrs_path_sets_stability():
    state = SM2State()
    new = advance_schedule(state, RATING_GOOD, NOW, scheduler="fsrs")
    assert new.stability > 0
    assert new.difficulty > 0


def test_advance_schedule_illegal_scheduler_falls_back_sm2():
    state = SM2State()
    new = advance_schedule(state, RATING_GOOD, NOW,
                           scheduler="bogus")
    assert new.stability == 0.0
    assert new.interval_days == 1.0
