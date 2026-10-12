"""ReviewItem 三分档判读口径回归测试(集中在 audit.classify_status)

分档逻辑若散落在前端/导出/统计各写一份会漂移,故集中到此纯函数;本测试锚定
判读的优先级与边界,尤其锁住两条语义纠正:
- `suspected`(存疑未取证)**不再**算"已核实成立"(旧口径把它归进"发现风险",
  实测让三条没读到源码的结论顶着风险标题出现在结果区);
- `confirmed` 优先于覆盖态(取到过证就不该被 partial/missing 降级掉)。
"""
from app.models.audit import (
    BUCKET_ESTABLISHED,
    BUCKET_LABELS,
    BUCKET_NOT_ESTABLISHED,
    BUCKET_ORDER,
    BUCKET_PENDING_VERIFY,
    STATUS_COVERED,
    STATUS_MISSING,
    STATUS_PARTIAL,
    VERDICT_CONFIRMED,
    VERDICT_FALSE_POSITIVE,
    VERDICT_NONE,
    VERDICT_PENDING,
    VERDICT_SUSPECTED,
    ReviewItem,
    classify_status,
)


def test_confirmed_is_established_even_when_status_not_covered():
    assert classify_status(STATUS_COVERED, VERDICT_CONFIRMED) == BUCKET_ESTABLISHED
    # 取到过证就不该被覆盖态降级(真问题不能从结果区消失)
    assert classify_status(STATUS_PARTIAL, VERDICT_CONFIRMED) == BUCKET_ESTABLISHED
    assert classify_status(STATUS_MISSING, VERDICT_CONFIRMED) == BUCKET_ESTABLISHED


def test_none_and_false_positive_are_not_established():
    assert classify_status(STATUS_COVERED, VERDICT_NONE) == BUCKET_NOT_ESTABLISHED
    assert classify_status(
        STATUS_COVERED, VERDICT_FALSE_POSITIVE
    ) == BUCKET_NOT_ESTABLISHED


def test_suspected_is_pending_verify_not_established():
    """关键回归:没取到证的存疑结论不冒充「已核实成立」"""
    assert classify_status(STATUS_COVERED, VERDICT_SUSPECTED) == BUCKET_PENDING_VERIFY
    assert classify_status(STATUS_PARTIAL, VERDICT_SUSPECTED) == BUCKET_PENDING_VERIFY


def test_pending_and_missing_verdict_are_pending_verify():
    assert classify_status(STATUS_MISSING, VERDICT_PENDING) == BUCKET_PENDING_VERIFY
    assert classify_status(STATUS_PARTIAL, VERDICT_PENDING) == BUCKET_PENDING_VERIFY
    # 无 verdict 的 covered:agent2 没下判断,同样算待核实(旧口径默认归"已核查")
    assert classify_status(STATUS_COVERED, None) == BUCKET_PENDING_VERIFY
    assert classify_status(STATUS_MISSING, None) == BUCKET_PENDING_VERIFY


def test_bucket_order_and_labels_are_the_full_three_way():
    assert BUCKET_ORDER == (
        BUCKET_ESTABLISHED, BUCKET_NOT_ESTABLISHED, BUCKET_PENDING_VERIFY,
    )
    assert [BUCKET_LABELS[k] for k in BUCKET_ORDER] == [
        "已核实成立", "已核实不成立", "待核实",
    ]


def test_model_read_only_properties_reuse_central_classify():
    item = ReviewItem(
        task_id=None, round_idx=1, title="JWT 默认密钥",
        review_target="backend/app/config.py:34", origin="agent1_claim",
        status=STATUS_COVERED, verdict=VERDICT_CONFIRMED,
    )
    assert item.bucket == BUCKET_ESTABLISHED
    assert item.bucket_label == "已核实成立"
    assert item.is_established and not item.is_pending_verify

    pending = ReviewItem(
        task_id=None, round_idx=1, title="SSRF 面", review_target="base_url",
        origin="agent1_claim", status=STATUS_COVERED, verdict=VERDICT_SUSPECTED,
    )
    assert pending.is_pending_verify and not pending.is_established
    assert not pending.is_not_established
