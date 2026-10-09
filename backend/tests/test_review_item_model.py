"""ReviewItem 三态判读口径回归测试(集中在 audit.classify_status)

风险项:分桶逻辑若散落在前端/导出/统计各写一份会漂移。集中到此纯函数,
本测试锚定三态判读的优先级与边界。
"""
from app.models.audit import (
    BUCKET_CLEARED,
    BUCKET_GAP,
    BUCKET_RISK,
    STATUS_COVERED,
    STATUS_MISSING,
    STATUS_PARTIAL,
    VERDICT_CONFIRMED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_NONE,
    VERDICT_PENDING,
    VERDICT_SUSPECTED,
    classify_status,
)


def test_risk_verdict_beats_other_states():
    # 风险类 verdict 优先归 risk(即便 status 非 covered)
    assert classify_status(STATUS_COVERED, VERDICT_CONFIRMED) == BUCKET_RISK
    assert classify_status(STATUS_PARTIAL, VERDICT_SUSPECTED) == BUCKET_RISK


def test_missing_or_partial_is_gap():
    assert classify_status(STATUS_MISSING, VERDICT_PENDING) == BUCKET_GAP
    assert classify_status(STATUS_PARTIAL, VERDICT_PENDING) == BUCKET_GAP
    # 无 verdict 的 missing 也是缺口
    assert classify_status(STATUS_MISSING, None) == BUCKET_GAP


def test_cleared_states():
    assert classify_status(STATUS_COVERED, VERDICT_NONE) == BUCKET_CLEARED
    assert classify_status(STATUS_COVERED, VERDICT_FALSE_POSITIVE) == BUCKET_CLEARED


def test_verdict_null_covered_defaults_cleared():
    assert classify_status(STATUS_COVERED, None) == BUCKET_CLEARED
