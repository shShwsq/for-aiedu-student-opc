"""SM-2 遗忘曲线调度 + 难度/能力评估单元测试

覆盖:
- SM-2 间隔序列 1 → 6 → 前值×EF、EF 更新公式与下限 1.3
- 答错重置(repetitions=0, interval=1)
- 到期判定与过期天数
- 题目难度微调规则与钳制
- 能力值冷启动 / 加权窗口
"""
from datetime import datetime, timedelta

from app.services.practice.difficulty import (
    COLD_START_ABILITY,
    adjust_question_difficulty,
    clamp_difficulty,
    estimate_ability,
)
from app.services.practice.sm2 import (
    DEFAULT_EASE_FACTOR,
    MIN_EASE_FACTOR,
    QUALITY_CORRECT,
    QUALITY_WRONG,
    SM2State,
    apply_sm2,
    is_overdue,
    overdue_days,
    quality_from_correct,
)

NOW = datetime(2026, 8, 16, 12, 0, 0)


# ============================================================
# SM-2
# ============================================================


def test_quality_mapping():
    assert quality_from_correct(True) == QUALITY_CORRECT
    assert quality_from_correct(False) == QUALITY_WRONG


def test_first_correct_interval_1_day():
    state = SM2State()
    new = apply_sm2(state, QUALITY_CORRECT, NOW)
    assert new.repetitions == 1
    assert new.interval_days == 1.0
    assert new.due_at == NOW + timedelta(days=1)
    assert new.attempts == 1
    assert new.correct_count == 1


def test_interval_sequence_1_6_then_ef():
    state = SM2State()
    state = apply_sm2(state, QUALITY_CORRECT, NOW)  # rep1 → 1 天
    assert state.interval_days == 1.0
    state = apply_sm2(state, QUALITY_CORRECT, NOW)  # rep2 → 6 天
    assert state.interval_days == 6.0
    state = apply_sm2(state, QUALITY_CORRECT, NOW)  # rep3 → round(6×EF)
    assert state.repetitions == 3
    assert state.interval_days == round(6.0 * state.ease_factor) or \
        state.interval_days == max(1.0, round(6.0 * 2.5))


def test_ef_formula_quality_4():
    state = SM2State(ease_factor=DEFAULT_EASE_FACTOR)
    new = apply_sm2(state, QUALITY_CORRECT, NOW)
    # EF' = 2.5 + (0.1 - 1*(0.08+0.02)) = 2.5
    assert abs(new.ease_factor - 2.5) < 1e-9


def test_ef_drops_on_wrong_and_floors_at_min():
    state = SM2State(ease_factor=DEFAULT_EASE_FACTOR)
    # quality=1:EF' = 2.5 + (0.1 - 4*(0.08+4*0.02)) = 2.5 + 0.1 - 0.64 = 1.96
    new = apply_sm2(state, QUALITY_WRONG, NOW)
    assert abs(new.ease_factor - 1.96) < 1e-9
    # 连续答错压到下限
    for _ in range(5):
        new = apply_sm2(new, QUALITY_WRONG, NOW)
    assert new.ease_factor == MIN_EASE_FACTOR


def test_wrong_answer_resets_repetitions():
    state = SM2State(ease_factor=2.5, interval_days=15.0, repetitions=4)
    new = apply_sm2(state, QUALITY_WRONG, NOW)
    assert new.repetitions == 0
    assert new.interval_days == 1.0
    assert new.due_at == NOW + timedelta(days=1)
    assert new.correct_count == 0  # 未增加


def test_input_state_not_mutated():
    state = SM2State()
    apply_sm2(state, QUALITY_CORRECT, NOW)
    assert state.repetitions == 0
    assert state.due_at is None


def test_quality_clamped_to_0_5():
    state = SM2State()
    new = apply_sm2(state, 9, NOW)
    assert new.last_quality == 5
    new = apply_sm2(state, -3, NOW)
    assert new.last_quality == 0


def test_is_overdue_and_days():
    state = SM2State()
    assert not is_overdue(state, NOW)  # 从未作答
    assert overdue_days(state, NOW) == 0.0

    state.due_at = NOW - timedelta(days=2, hours=12)
    assert is_overdue(state, NOW)
    assert abs(overdue_days(state, NOW) - 2.5) < 1e-6

    state.due_at = NOW + timedelta(days=1)
    assert not is_overdue(state, NOW)
    assert overdue_days(state, NOW) == 0.0


# ============================================================
# 难度 / 能力
# ============================================================


def test_clamp_difficulty():
    assert clamp_difficulty(0) == 1.0
    assert clamp_difficulty(9) == 5.0
    assert clamp_difficulty(3.5) == 3.5


def test_difficulty_up_when_strong_user_fails():
    # 能力 >= 难度却答错 → +0.5
    assert adjust_question_difficulty(3.0, ability=3.5, is_correct=False) == 3.5
    # 封顶
    assert adjust_question_difficulty(5.0, ability=5.0, is_correct=False) == 5.0


def test_difficulty_down_when_weak_user_succeeds():
    # 能力比难度低 1.5 以上却答对 → -0.5
    assert adjust_question_difficulty(4.0, ability=2.0, is_correct=True) == 3.5
    # 地板
    assert adjust_question_difficulty(1.0, ability=1.0, is_correct=True) == 1.0


def test_difficulty_unchanged_when_expected():
    assert adjust_question_difficulty(3.0, ability=3.0, is_correct=True) == 3.0
    assert adjust_question_difficulty(3.0, ability=2.0, is_correct=False) == 3.0


def test_ability_cold_start():
    assert estimate_ability([]) == COLD_START_ABILITY


def test_ability_weighted_recent():
    # 全答对 [(1,T), (5,T)]:权重 1,2 → (1*1 + 5*2)/3 = 11/3
    assert abs(estimate_ability([(1.0, True), (5.0, True)]) - 11 / 3) < 1e-9


def test_ability_wrong_pulls_down():
    # 单条答对难度 4 → 4.0;同难度答错 → 目标值 4-1=3.0(失败拉低)
    assert abs(estimate_ability([(4.0, True)]) - 4.0) < 1e-9
    assert abs(estimate_ability([(4.0, False)]) - 3.0) < 1e-9


def test_ability_window_limits_to_10():
    seq = [(1.0, True)] * 20  # 全 1,结果与窗口无关
    assert estimate_ability(seq) == 1.0
    # 前 15 条被丢弃:前面塞高难度不影响结果
    seq2 = [(5.0, True)] * 15 + [(2.0, True)] * 10
    assert estimate_ability(seq2) == 2.0
