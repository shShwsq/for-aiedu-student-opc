"""选题策略(selector)单元测试

覆盖:
- 打分:到期紧迫度 / 薄弱点 / 难度匹配 / 新题
- 约束:同知识点 ≤60%、复习题占比 ≥50%、冷启动低难度新题(无低难度题时退化兜底)
"""
import random
from datetime import datetime, timedelta

from app.services.practice.selector import (
    CandidateInfo,
    score_candidate,
    select_questions,
)

NOW = datetime(2026, 8, 16, 12, 0, 0)


def make_candidate(
    qid: str,
    kp: str = "kp1",
    difficulty: float = 3.0,
    attempts: int = 1,
    due_at: datetime | None = None,
    kp_attempts: int = 0,
    kp_correct: int = 0,
) -> CandidateInfo:
    return CandidateInfo(
        question=None,
        question_id=qid,
        kp_id=kp,
        difficulty=difficulty,
        question_attempts=attempts,
        kp_due_at=due_at,
        kp_attempts=kp_attempts,
        kp_correct_count=kp_correct,
    )


# ============================================================
# 打分
# ============================================================


def test_overdue_beats_weakness_and_novelty():
    overdue = make_candidate("q1", due_at=NOW - timedelta(days=3))
    fresh = make_candidate("q2", attempts=0, kp_attempts=0)
    s1 = score_candidate(overdue, ability=3.0, now=NOW, jitter=0.0)
    s2 = score_candidate(fresh, ability=3.0, now=NOW, jitter=0.0)
    assert s1 > s2


def test_urgency_capped():
    d2 = make_candidate("q1", due_at=NOW - timedelta(days=2))
    d30 = make_candidate("q2", due_at=NOW - timedelta(days=30))
    s1 = score_candidate(d2, ability=3.0, now=NOW, jitter=0.0)
    s2 = score_candidate(d30, ability=3.0, now=NOW, jitter=0.0)
    assert s2 > s1
    # 封顶:30 天与 100 天得分一致
    d100 = make_candidate("q3", due_at=NOW - timedelta(days=100))
    s3 = score_candidate(d100, ability=3.0, now=NOW, jitter=0.0)
    assert abs(s3 - s2) < 1e-9


def test_weakness_requires_min_samples():
    # 样本不足(<3)不加薄弱分
    few = make_candidate("q1", kp_attempts=2, kp_correct=0)
    none = make_candidate("q2", kp_attempts=0)
    assert (
        score_candidate(few, ability=3.0, now=NOW, jitter=0.0)
        == score_candidate(none, ability=3.0, now=NOW, jitter=0.0)
    )
    # 错误率 80% ≥ 3 样本 → 明显加分
    weak = make_candidate("q3", kp_attempts=5, kp_correct=1)
    strong = make_candidate("q4", kp_attempts=5, kp_correct=5)
    assert (
        score_candidate(weak, ability=3.0, now=NOW, jitter=0.0)
        > score_candidate(strong, ability=3.0, now=NOW, jitter=0.0)
    )


def test_difficulty_match_best_at_ability():
    matched = make_candidate("q1", difficulty=3.0)
    far = make_candidate("q2", difficulty=5.0)
    s1 = score_candidate(matched, ability=3.0, now=NOW, jitter=0.0)
    s2 = score_candidate(far, ability=3.0, now=NOW, jitter=0.0)
    assert s1 > s2
    # 差 2 以上 → 难度匹配分为 0
    extreme = make_candidate("q3", difficulty=1.0)
    s3 = score_candidate(extreme, ability=4.5, now=NOW, jitter=0.0)
    assert s3 < s2 or abs(s3 - s2) < 1e-9


def test_novelty_bonus():
    new_q = make_candidate("q1", attempts=0)
    old_q = make_candidate("q2", attempts=2)
    s1 = score_candidate(new_q, ability=3.0, now=NOW, jitter=0.0)
    s2 = score_candidate(old_q, ability=3.0, now=NOW, jitter=0.0)
    assert abs((s1 - s2) - 0.5) < 1e-9


# ============================================================
# 组卷约束
# ============================================================


def test_cold_start_only_easy_new():
    candidates = [
        make_candidate("q1", difficulty=1.0, attempts=0),
        make_candidate("q2", difficulty=2.0, attempts=0),
        make_candidate("q3", difficulty=4.0, attempts=0),  # 冷启动不出
    ]
    picked = select_questions(candidates, ability=2.5, count=5, now=NOW)
    assert [c.question_id for c in picked] == ["q1", "q2"]


def test_cold_start_falls_back_to_easiest_when_no_easy_question():
    # 冷启动且题库无 difficulty ≤ 2 的题(真实生成题默认 3、prompt 示例即 3):
    # 不应返回空(那会让「开始练习」报"没有可出的题目"),而应按难度升序取最易的 count 道
    candidates = [
        make_candidate("q1", difficulty=3.0, attempts=0, kp_attempts=0),
        make_candidate("q2", difficulty=5.0, attempts=0, kp_attempts=0),
        make_candidate("q3", difficulty=4.0, attempts=0, kp_attempts=0),
    ]
    picked = select_questions(candidates, ability=2.5, count=2, now=NOW)
    assert [c.question_id for c in picked] == ["q1", "q3"]


def test_same_kp_capped_at_60_percent():
    # kp1 有 6 道到期高分题,kp2 有 2 道;count=5 → kp1 最多 3 道
    candidates = [
        make_candidate(f"q{i}", kp="kp1", due_at=NOW - timedelta(days=i + 1))
        for i in range(6)
    ] + [
        make_candidate("r1", kp="kp2"),
        make_candidate("r2", kp="kp2"),
    ]
    random.seed(42)
    picked = select_questions(candidates, ability=3.0, count=5, now=NOW)
    kp1_count = sum(1 for c in picked if c.kp_id == "kp1")
    assert len(picked) == 5
    assert kp1_count <= 3  # floor(5*0.6)=3


def test_review_ratio_at_least_50_percent():
    # 3 道到期复习 + 5 道普通题,count=6 → 复习题至少 3 道
    candidates = [
        make_candidate(f"due{i}", kp=f"kp{i}", due_at=NOW - timedelta(days=1))
        for i in range(3)
    ] + [
        make_candidate(f"plain{i}", kp=f"p{i}", difficulty=3.0)
        for i in range(5)
    ]
    random.seed(7)
    picked = select_questions(candidates, ability=3.0, count=6, now=NOW)
    due_picked = [c for c in picked if c.kp_due_at is not None and c.kp_due_at <= NOW]
    assert len(picked) == 6
    assert len(due_picked) >= 3


def test_review_shortage_takes_all():
    # 只有 1 道复习题,不强制凑 50%
    candidates = [
        make_candidate("due1", kp="kp1", due_at=NOW - timedelta(days=1)),
        make_candidate("p1", kp="p1"),
        make_candidate("p2", kp="p2"),
    ]
    random.seed(1)
    picked = select_questions(candidates, ability=3.0, count=3, now=NOW)
    assert len(picked) == 3
    assert any(c.question_id == "due1" for c in picked)


def test_empty_pool_returns_empty():
    assert select_questions([], ability=2.5, count=8, now=NOW) == []


def test_select_fewer_when_pool_small():
    candidates = [make_candidate("q1", due_at=NOW - timedelta(days=1))]
    picked = select_questions(candidates, ability=3.0, count=8, now=NOW)
    assert len(picked) == 1
