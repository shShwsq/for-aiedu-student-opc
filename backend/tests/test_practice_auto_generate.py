"""自动生成练习题(auto_generate)守卫分支单元测试

不依赖数据库:mock Session 的 query 链,按调用顺序注入返回值,
覆盖 orchestrator 钩子里 auto_generate_practice_for_task 的全部守卫:
- 匿名任务(user_id 为空)跳过
- 开关关闭跳过(任务级 _practice.auto_generate > 用户级 > 默认开)
- 同一任务已有题目(任意状态)跳过
- 无结构化 finding(纯摘要/空)→降级为基于对话出题(conversation_fallback=True 传给生成器)
- 只有审查项、results 全无元信息(关闭知识点提炼的典型形状)→仍降级为对话出题
- metadata 含任意非空值(含非安全场景的通用键)→据结构化发现出题(不降级)
- 命中守卫后调 generator(带进度/事件回调),返回生成数
- 生成过程创建 source=auto 的 job 追踪(练习页侧栏可见)
"""
import uuid
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.practice import jobs as gen_jobs
from app.services.practice.auto_generate import auto_generate_practice_for_task


def _mock_db(pref_row=None, existing_question=None, result_metas=None,
             result_count=2):
    """按 auto_generate 内的 query 顺序注入:
    1) PracticeSettings 2) Question.id 3) Result.metadata_(_has_structured_findings)
    4) count(Result.id)(job 追踪的 set_total 用)
    """
    q_pref = MagicMock()
    q_pref.filter.return_value.first.return_value = pref_row
    q_question = MagicMock()
    q_question.filter.return_value.first.return_value = existing_question
    q_result = MagicMock()
    q_result.filter.return_value.all.return_value = [
        (m,) for m in (result_metas or [])
    ]
    q_count = MagicMock()
    q_count.filter.return_value.scalar.return_value = result_count
    db = MagicMock()
    db.query.side_effect = [q_pref, q_question, q_result, q_count]
    return db


def _task(user_id=None):
    t = MagicMock()
    t.id = uuid4()
    t.user_id = user_id or uuid4()
    t.title = "测试任务"
    return t


def test_skip_anonymous_task():
    db = MagicMock()
    task = MagicMock()
    task.user_id = None
    assert auto_generate_practice_for_task(task, db) == 0
    db.query.assert_not_called()


def test_skip_when_preference_off():
    pref = MagicMock()
    pref.auto_generate_practice = False
    db = _mock_db(pref_row=pref)
    with patch("app.services.practice.auto_generate.generate_questions_for_task") as gen:
        assert auto_generate_practice_for_task(_task(), db) == 0
    gen.assert_not_called()


def test_skip_when_task_level_auto_generate_off():
    """任务级 _practice.auto_generate=False → 立即跳过,不查 PracticeSettings(优先于用户级)。"""
    db = MagicMock()
    task = _task()
    task.params = {"_practice": {"auto_generate": False}}
    assert auto_generate_practice_for_task(task, db) == 0
    db.query.assert_not_called()


def test_task_level_auto_generate_on_skips_preference_lookup():
    """任务级显式开启时不回退用户级:即使用户关了,也不在守卫②因偏好关闭而跳过。"""
    task = _task()
    task.params = {"_practice": {"auto_generate": True}}
    # 守卫③命中已有题目(任意查询都返回有 existing),证明未走 PracticeSettings 分支
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = (uuid4(),)
    db.query.return_value = q
    with patch("app.services.practice.auto_generate.generate_questions_for_task") as gen:
        assert auto_generate_practice_for_task(task, db) == 0
    gen.assert_not_called()


def test_skip_when_questions_already_exist():
    db = _mock_db(pref_row=None, existing_question=(uuid4(),))
    with patch("app.services.practice.auto_generate.generate_questions_for_task") as gen:
        assert auto_generate_practice_for_task(_task(), db) == 0
    gen.assert_not_called()


def test_degrade_to_conversation_without_structured_findings():
    """无结构化 finding(纯摘要/空)→ 不跳过,降级为对话出题(conversation_fallback=True)。"""
    # 纯摘要 Result:metadata 为空/None/空 dict/全空白值
    for metas in ([], [None], [{}], [{"severity": "   "}]):
        db = _mock_db(pref_row=None, result_metas=metas)
        with patch(
            "app.services.practice.auto_generate.generate_questions_for_task",
            return_value=([], 0),
        ) as gen:
            assert auto_generate_practice_for_task(_task(), db) == 0
        gen.assert_called_once()
        assert gen.call_args.kwargs.get("conversation_fallback") is True


def test_generate_with_generic_meta():
    # 放宽后非安全场景的通用 metadata 键也视为结构化 finding
    for metas in ([{"title": "x"}], [{"category": "readability"}]):
        db = _mock_db(pref_row=None, result_metas=metas)
        with patch(
            "app.services.practice.auto_generate.generate_questions_for_task",
            return_value=([MagicMock()], 0),
        ) as gen:
            assert auto_generate_practice_for_task(_task(), db) == 1
        gen.assert_called_once()


def _db_dispatch_by_target(result_metas, review_items=True):
    """按查询目标分发(不依赖调用顺序)的会话替身:库里有审查项但 results 无元信息。

    为什么不用顺序 side_effect:新门控根本不查 review_items,把审查项返回值按顺序
    注进去会错喂给下一条查询,反而测不出「审查项被错误地当作出题素材判据」。
    分发键用真实表达式算出来(实测 str(ReviewItem.id) 是 "ReviewItem.id" 而不是
    表名,写死字面量会默默落进兜底分支,变成假阳性通过)。
    """
    from app.models.audit import ReviewItem
    from app.models.practice import Question
    from app.models.task import Result

    kinds = {
        str(ReviewItem.id): "review_item",
        str(Result.metadata_): "result_meta",
        str(Question.id): "question",
    }
    review_item_target = str(ReviewItem.id)

    def _query(target):
        q = MagicMock()
        kind = kinds.get(str(target))
        if kind == "review_item":
            q.filter.return_value.first.return_value = (uuid4(),) if review_items else None
        elif kind == "result_meta":
            q.filter.return_value.all.return_value = [(m,) for m in result_metas]
        elif str(target).startswith("count("):
            q.filter.return_value.scalar.return_value = 2
        else:  # PracticeSettings / Question.id
            q.filter.return_value.first.return_value = None
        return q

    db = MagicMock()
    db.query.side_effect = _query
    db.review_item_target = review_item_target
    return db


def test_has_structured_findings_ignores_review_items():
    """回归:审查项不能再当成「有结构化素材」的判据。

    关闭「提炼知识点」时 agent2 仍照常发射 submit_review_item(只剔除了
    submit_knowledge_point),旧门控因此恒为真,对话降级分支永远走不到。"""
    from app.models.audit import ReviewItem
    from app.services.practice.auto_generate import _has_structured_findings

    db = _db_dispatch_by_target([None, {"severity": "   "}], review_items=True)
    # 前提自检:替身确实喂得出「库里有审查项」(否则旧实现也会判 False,本用例假阳性通过)
    assert db.query(ReviewItem.id).filter.return_value.first.return_value is not None
    db.query.reset_mock()  # 抹掉这次探针,下面的「没查过审查项表」断言才干净

    assert _has_structured_findings(db, uuid4()) is False
    # 根本没查过审查项表(而不是查了但判错)
    queried = [str(c.args[0]) for c in db.query.call_args_list]
    assert db.review_item_target not in queried, queried


def test_degrade_when_only_review_items_exist():
    """端到端:有审查项 + Results 全无元信息 → conversation_fallback=True 传到生成器。"""
    db = _db_dispatch_by_target([None, {}], review_items=True)
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()], 0),
    ) as gen:
        assert auto_generate_practice_for_task(_task(), db) == 1
    gen.assert_called_once()
    assert gen.call_args.kwargs.get("conversation_fallback") is True


def test_generate_with_cwe_meta():
    db = _mock_db(pref_row=None, result_metas=[{"cwe": "CWE-89"}])
    task = _task()
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()] * 3, 1),
    ) as gen:
        assert auto_generate_practice_for_task(task, db) == 3
    gen.assert_called_once()
    # 传入位置参数不变,额外携带进度/事件回调(job 追踪用)
    args, kwargs = gen.call_args
    assert args == (db, task, task.user_id)
    assert callable(kwargs["progress_callback"])
    assert callable(kwargs["event_callback"])


def test_generate_creates_auto_job():
    """自动出题创建 source=auto 的 job,完成后状态 done 且计数正确"""
    user_id = uuid.uuid4()
    db = _mock_db(pref_row=None, result_metas=[{"cwe": "CWE-89"}], result_count=5)
    task = _task(user_id=user_id)
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()] * 3, 1),
    ):
        assert auto_generate_practice_for_task(task, db) == 3
    jobs = gen_jobs.list_jobs(user_id)
    try:
        assert len(jobs) == 1
        job = jobs[0]
        assert job["source"] == "auto"
        assert job["status"] == "done"
        assert job["total"] == 5  # 发现数未超上限,不裁剪
        assert job["created_count"] == 3
        assert job["skipped_findings"] == 1
        assert job["task_title"] == "测试任务"
        # 终止事件已追加(含 done 事件,SSE 客户端据此收尾)
        raw = gen_jobs.get_job(job["job_id"], user_id)
        assert raw["events"][-1]["type"] == "done"
        assert raw["events"][-1]["data"] == {"created": 3, "skipped": 1}
    finally:
        # 清理全局 job 表,避免污染其它用例
        with gen_jobs._LOCK:
            gen_jobs._JOBS.pop(job["job_id"], None)


def test_generate_with_severity_meta():
    db = _mock_db(pref_row=None, result_metas=[{}, {"severity": "high"}])
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()], 0),
    ) as gen:
        assert auto_generate_practice_for_task(_task(), db) == 1
    gen.assert_called_once()


def test_resolve_source_review_item_id():
    """出题溯源:从知识点 metadata 解析真实存在的审查项 id(悬空/无效→None)"""
    from uuid import UUID
    from app.services.practice.generator import _resolve_source_review_item_id

    rid = uuid4()
    # 非 dict / 缺键 / 非法 uuid → None
    assert _resolve_source_review_item_id(MagicMock(), None) is None
    assert _resolve_source_review_item_id(MagicMock(), {}) is None
    assert _resolve_source_review_item_id(MagicMock(), {"source_review_item_id": "not-uuid"}) is None

    # 存在审查项 → 返回 UUID
    db_ok = MagicMock()
    db_ok.query.return_value.filter.return_value.first.return_value = (rid,)
    meta = {"source_review_item_id": str(rid)}
    got = _resolve_source_review_item_id(db_ok, meta)
    assert isinstance(got, UUID) and got == rid

    # 悬空(查不到)→ None
    db_missing = MagicMock()
    db_missing.query.return_value.filter.return_value.first.return_value = None
    assert _resolve_source_review_item_id(db_missing, meta) is None



def test_preference_on_does_not_block():
    pref = MagicMock()
    pref.auto_generate_practice = True
    db = _mock_db(pref_row=pref, result_metas=[{"cwe": "79"}])
    with patch(
        "app.services.practice.auto_generate.generate_questions_for_task",
        return_value=([MagicMock()], 0),
    ) as gen:
        assert auto_generate_practice_for_task(_task(), db) == 1
