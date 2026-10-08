"""agent2 后台审查流程测试(计划:agent2 后台审查重构)

覆盖新流程的关键契约:
- agent1 结束即任务完成(推 agent1_done,总线保持打开);done 在审查后
- 审查完成:本轮临时结果被重点与知识点替换(按轮追加),review_status=done
- 审查失败:任务仍 COMPLETED,临时结果保留,review_status=failed
- review_status 流转:running(审查开始)→ done/failed
- 事件活跃期(scope):done/finish 仅由最后活跃流收尾,并行流互不干扰
- 核查中追问:直接启动新一轮(不等老审查),老审查收尾不关总线
- 异常路径:error 事件先于总线关闭推送(否则前端收不到错误横幅)
- 单 agent 模式:无审查事件,review_status 不动
- suggestions 落库契约(前端解析 type=suggestions 渲染追问卡片)
- 临时结果助手 _replace_interim_results:按轮清理后落单条
- 用户终止检查:保留临时结果 + review_status=stopped + 跳过审查下游链
"""
import json
import uuid
from unittest.mock import MagicMock

import app.models.task_artifact  # noqa: F401  (mapper 依赖)
import app.models.user_git_binding  # noqa: F401

import app.agents.orchestrator as orchestrator
from app.models.task import Conversation, Result, TaskStatus
from app.user_messages import clear_user_messages, push_user_message


# ============================================================
# 测试环境构造(参考 test_agent2_stream_degrade 的屏蔽模式)
# ============================================================


def _mk_task():
    task = MagicMock()
    task.id = "task-br"
    task.status = TaskStatus.PENDING
    task.current_stage = ""
    task.error_message = None
    task.user_input = "审计这个仓库"
    task.scenario = "general"
    task.user_id = None
    task.llm_config_id = None
    task.react_llm_config_id = None
    task.params = {}
    task.allowed_skills = None
    task.executor = "builtin"
    task.review_status = None
    return task


def _mk_review_result(**overrides):
    result = {
        "covered": ["injection"],
        "missing": [],
        "reasoning": "审查通过",
        "suggestions": [],
        "results": [
            {"title": "知识点1", "content": "说明1",
             "metadata": {"learning_note": "值得学"}},
            {"title": "知识点2", "content": "说明2",
             "metadata": {"learning_note": "也值得学"}},
        ],
        "grouping": None,
    }
    result.update(overrides)
    return result


def _patch_env(monkeypatch, executor, ua_side_effect):
    """屏蔽 dual 链路副作用,保留审查链真实逻辑(结果替换/事件时序)。"""
    monkeypatch.setattr(orchestrator, "resolve_agent_policy",
                        lambda *a, **k: {"agent2_enabled": True})
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_build_llm_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(orchestrator, "_build_react_llm_client",
                        lambda *a, **k: (MagicMock(), None))
    monkeypatch.setattr(orchestrator, "_load_git_tokens", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "set_current_git_tokens", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "get_executor", lambda *a, **k: executor)
    monkeypatch.setattr(orchestrator, "_prepare_repo_context",
                        lambda *a, **k: (None, ""))
    monkeypatch.setattr(orchestrator, "_publish_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "run_agent2", ua_side_effect)

    import app.services.memory_summarize as memory_summarize
    import app.services.workspace_diff as workspace_diff
    monkeypatch.setattr(memory_summarize, "summarize_and_save_memory",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_workspace_diff_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_repo_tree_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.sandbox_tools, "mark_task_completed",
                        lambda *a, **k: None)


class _EventRecorder:
    """记录 publish/finish_task 调用顺序(时序断言用)。"""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        monkeypatch.setattr(orchestrator, "publish", self._publish)
        monkeypatch.setattr(orchestrator, "finish_task", self._finish)

    def _publish(self, task_id, event, data=None):
        self.calls.append(("publish", event, data))

    def _finish(self, task_id):
        self.calls.append(("finish",))

    def events(self, name):
        return [c for c in self.calls if c[0] == "publish" and c[1] == name]

    def index(self, marker):
        for i, c in enumerate(self.calls):
            if c == marker or (c[0] == "publish" and c[1] == marker):
                return i
        return -1


# ============================================================
# 事件时序:agent1_done → (审查) → review_done → done → finish
# ============================================================


def test_agent1_done_before_done_and_bus_stays_open(monkeypatch):
    """agent1_done 在 done 之前推送;finish_task(总线关闭)发生在审查后。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    # agent1_done 早于 review_done,review_done 早于 done
    assert rec.index("agent1_done") != -1
    assert rec.index("agent1_done") < rec.index("review_done")
    assert rec.index("review_done") < rec.index("done")
    # finish_task(总线关闭)在 agent1_done 之后(审查期间总线保持打开)
    assert rec.index("agent1_done") < rec.index(("finish",))
    # 事件 data 契约
    assert rec.events("agent1_done")[0][2] == {"status": "completed"}
    assert rec.events("review_done")[0][2] == {"review_status": "done"}


def test_review_status_transitions_running_to_done(monkeypatch):
    """审查期间 review_status=running,完成后=done。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    observed = {}

    def _ua(*args, **kwargs):
        observed["review_status_at_review"] = task.review_status
        return _mk_review_result()

    _patch_env(monkeypatch, executor, _ua)
    _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    assert observed["review_status_at_review"] == "running"
    assert task.review_status == "done"
    assert task.status == TaskStatus.COMPLETED


# ============================================================
# 结果替换:临时结果 → 重点与知识点
# ============================================================


def test_review_replaces_interim_results_with_knowledge(monkeypatch):
    """审查完成:临时结果被 agent2 知识点整体替换(先删全部再落新)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    db = MagicMock()

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, db)

    results_added = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], Result)
    ]
    # 1 条临时(agent1 summary)+ 2 条知识点(agent2)
    assert len(results_added) == 3
    assert "检查助手整理中" in results_added[0].title
    assert [r.title for r in results_added[1:]] == ["知识点1", "知识点2"]
    assert results_added[1].metadata_ == {"learning_note": "值得学"}
    # 替换 = 两次全删(临时落库时 1 次 + 审查完成时 1 次)
    delete_calls = [
        c for c in db.query.call_args_list if c.args and c.args[0] is Result
    ]
    assert len(delete_calls) == 2


def test_review_failure_keeps_interim_results(monkeypatch):
    """审查降级:任务仍 COMPLETED,临时结果保留(不新增不删除),警告审查卡落库。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    db = MagicMock()
    degraded = _mk_review_result(
        results=[], degraded=True, degrade_reason="boom",
    )

    _patch_env(monkeypatch, executor, lambda *a, **k: degraded)
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, db)

    assert task.status == TaskStatus.COMPLETED  # 任务不因审查失败而失败
    assert task.review_status == "failed"
    # 临时结果保留:只有 agent1 summary 那 1 条,审查未增删
    results_added = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], Result)
    ]
    assert len(results_added) == 1
    assert "检查助手整理中" in results_added[0].title
    delete_calls = [
        c for c in db.query.call_args_list if c.args and c.args[0] is Result
    ]
    assert len(delete_calls) == 1  # 仅临时结果落库时的那次全删
    # review_done 仍推送(failed 语义),done 正常终止
    assert rec.events("review_done")[0][2] == {"review_status": "failed"}
    assert rec.index("review_done") < rec.index("done")


# ============================================================
# suggestions 落库契约(前端追问卡片的数据源)
# ============================================================


def test_record_agent2_review_persists_suggestions_json():
    """有建议 → type=suggestions 的 Conversation 落 JSON;无建议 → 不落。"""
    task = _mk_task()
    db = MagicMock()

    with_suggestions = _mk_review_result(
        suggestions=["追问依赖漏洞供应链", "复核第 2 条结论的误报"],
    )
    orchestrator._record_agent2_review(db, task, 1, with_suggestions)

    added = [c.args[0] for c in db.add.call_args_list]
    suggestions_convs = [c for c in added if isinstance(c, Conversation)
                         and c.type == "suggestions"]
    assert len(suggestions_convs) == 1
    payload = json.loads(suggestions_convs[0].content)
    assert payload["suggestions"] == ["追问依赖漏洞供应链", "复核第 2 条结论的误报"]
    # 审查结论卡 + 最终总结卡同轮落库
    assert [c.type for c in added if isinstance(c, Conversation)] == [
        "review", "suggestions", "summary",
    ]

    # 无建议:不落 suggestions 卡
    db2 = MagicMock()
    orchestrator._record_agent2_review(db2, task, 1, _mk_review_result())
    added2 = [c.args[0] for c in db2.add.call_args_list]
    assert not [
        c for c in added2 if isinstance(c, Conversation) and c.type == "suggestions"
    ]


def test_record_agent2_review_failure_card_wording():
    """审查失败 → review 卡 content 提示保留执行结果。"""
    task = _mk_task()
    db = MagicMock()
    orchestrator._record_agent2_review(
        db, task, 1, _mk_review_result(results=[], degraded=True),
    )
    added = [c.args[0] for c in db.add.call_args_list]
    review_conv = next(c for c in added
                       if isinstance(c, Conversation) and c.type == "review")
    assert "审查未完成" in review_conv.content


def test_replace_interim_results_clears_and_writes_single_row():
    """_replace_interim_results:删全部旧 Result,落 1 条临时结果。"""
    task = _mk_task()
    db = MagicMock()

    count = orchestrator._replace_interim_results(db, task, 2, "本轮总结")

    assert count == 1
    db.query(Result).filter.assert_called_once()
    db.query(Result).filter.return_value.delete.assert_called_once()
    added = [c.args[0] for c in db.add.call_args_list]
    assert len(added) == 1
    assert added[0].round_idx == 2
    assert added[0].content == "本轮总结"
    assert "检查助手整理中" in added[0].title


# ============================================================
# 事件活跃期(scope):done/finish 仅由最后活跃流收尾
# ============================================================


def _clear_scopes(task_id: str) -> None:
    """清理 scope 注册表残留(测试隔离)"""
    with orchestrator._review_lock:
        orchestrator._event_scopes.pop(task_id, None)


def test_end_event_scope_solo_flow_pushes_done(monkeypatch):
    """唯一活跃流收尾:推 done + finish_task(与原单流行为一致)。"""
    task_id = "task-scope-solo"
    try:
        gen = orchestrator._begin_event_scope(task_id)
        rec = _EventRecorder(monkeypatch)

        ended = orchestrator._end_event_scope(
            task_id, gen, ("done", {"status": "completed"}),
        )

        assert ended is True
        assert len(rec.events("done")) == 1
        assert rec.index("done") < rec.index(("finish",))
        assert not orchestrator._has_active_scope(task_id)
    finally:
        _clear_scopes(task_id)


def test_end_event_scope_parallel_flow_keeps_bus_open(monkeypatch):
    """并行流在跑:先收尾的流不推 done、不 finish(总线保持打开)。"""
    task_id = "task-scope-parallel"
    try:
        gen_old = orchestrator._begin_event_scope(task_id)
        gen_new = orchestrator._begin_event_scope(task_id)
        assert gen_new > gen_old  # 世代号单调递增(最新流世代更大)

        rec = _EventRecorder(monkeypatch)
        # 老流(如老审查)先收尾:静默返回 False,不动总线
        ended_old = orchestrator._end_event_scope(
            task_id, gen_old, ("done", {"status": "completed"}),
        )
        assert ended_old is False
        assert not rec.events("done")
        assert rec.index(("finish",)) == -1
        assert orchestrator._has_active_scope(task_id)  # 新流仍活跃

        # 新流收尾:推 done + finish
        ended_new = orchestrator._end_event_scope(
            task_id, gen_new, ("done", {"status": "completed"}),
        )
        assert ended_new is True
        assert len(rec.events("done")) == 1
        assert rec.index("done") < rec.index(("finish",))
    finally:
        _clear_scopes(task_id)


def test_end_event_scope_idempotent_double_call(monkeypatch):
    """重复 end(收尾点 + finally 兜底):discard 幂等,不重复推 done。"""
    task_id = "task-scope-idem"
    try:
        gen = orchestrator._begin_event_scope(task_id)
        rec = _EventRecorder(monkeypatch)

        assert orchestrator._end_event_scope(
            task_id, gen, ("done", {"status": "completed"}),
        ) is True
        # finally 兜底的第二次 end:terminal=None,不重复推 done
        assert orchestrator._end_event_scope(task_id, gen) is True

        assert len(rec.events("done")) == 1
        assert not orchestrator._has_active_scope(task_id)
    finally:
        _clear_scopes(task_id)


def test_is_latest_generation_tracks_newest_flow():
    """世代判定:后启动的流为最新;老流跳过重下游。"""
    task_id = "task-scope-gen"
    try:
        gen1 = orchestrator._begin_event_scope(task_id)
        assert orchestrator._is_latest_generation(task_id, gen1) is True

        gen2 = orchestrator._begin_event_scope(task_id)
        assert orchestrator._is_latest_generation(task_id, gen1) is False
        assert orchestrator._is_latest_generation(task_id, gen2) is True
    finally:
        _clear_scopes(task_id)


def test_force_cleanup_event_scopes_clears_leak(monkeypatch):
    """泄漏兜底:force_cleanup 清空全部 scope + 关总线(防 SSE 永久悬挂)。"""
    task_id = "task-scope-leak"
    try:
        orchestrator._begin_event_scope(task_id)
        orchestrator._begin_event_scope(task_id)  # 模拟两条泄漏的流
        rec = _EventRecorder(monkeypatch)

        orchestrator.force_cleanup_event_scopes(task_id)

        assert not orchestrator._has_active_scope(task_id)
        assert rec.index(("finish",)) != -1  # 总线兜底关闭
    finally:
        _clear_scopes(task_id)


def test_launch_resume_thread_registers_scope_synchronously(monkeypatch):
    """launch_resume_thread:scope 在线程启动前同步注册
    (老审查收尾若发生在端点返回后、线程体执行前,也能看到本流活跃,
    不会误推 done 关闭总线)。"""
    task_id = "task-scope-launch"
    monkeypatch.setattr(
        orchestrator, "_run_resume_in_background", lambda *a, **k: None,
    )
    try:
        orchestrator.launch_resume_thread(task_id, "追问消息")
        # 函数返回即断言(不等线程体):scope 已注册
        assert orchestrator._has_active_scope(task_id)
    finally:
        _clear_scopes(task_id)


def test_resume_wrapper_fallback_clears_scope(monkeypatch):
    """resume 前置段异常上抛 → 包装器兜底:标记失败 + error 先于总线关闭
    + scope 清理(防泄漏导致后续所有流被误判"并行中")。"""
    task_id = "11111111-1111-1111-1111-111111111111"  # 合法 UUID(包装器内会解析)
    task = _mk_task()
    task.status = TaskStatus.RUNNING
    db = MagicMock()
    db.get = lambda *a, **k: task
    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: db)

    def _raise(task, db, message, **kwargs):
        raise RuntimeError("前置段崩溃")

    monkeypatch.setattr(orchestrator, "resume_audit_with_message", _raise)
    rec = _EventRecorder(monkeypatch)

    try:
        orchestrator._run_resume_in_background(task_id, "消息")
    finally:
        _clear_scopes(task_id)

    assert task.status == TaskStatus.FAILED
    assert rec.events("error")  # 前端能收到失败通知
    assert rec.index("error") < rec.index(("finish",))  # 先推后关
    assert not orchestrator._has_active_scope(task_id)  # 无泄漏


# ============================================================
# 核查中追问:老审查与新轮并行(不等审查,总线不断)
# ============================================================


def test_followup_during_review_runs_parallel(monkeypatch):
    """核查中用户追问已启动新流 → 老审查照常落库知识点并推 review_done,
    但收尾不推 done / 不 finish(总线保持打开,新轮事件无缝续达)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    # 在审查执行期间(run_agent2 回调)注册新流 scope,
    # 模拟"核查中用户追问 → 端点已同步启动新一轮 resume"
    def _ua(*args, **kwargs):
        orchestrator._begin_event_scope(task.id)
        return _mk_review_result()

    _patch_env(monkeypatch, executor, _ua)
    rec = _EventRecorder(monkeypatch)

    try:
        orchestrator.run_dual_agent_audit(task, MagicMock())

        # 老审查照常落库知识点,但被新流取代(世代门控)→ 不写 badge、
        # 不推 review_done(任务级字段归新流所有,防 badge 抖动)
        assert task.review_status == "running"  # 保持 agent1 完成时的值
        assert not rec.events("review_done")
        # 但不推 done / finish(新流活跃,总线打开)
        assert not rec.events("done")
        assert rec.index(("finish",)) == -1
        # 清理组跳过(新流仍在跑,不得清运行时状态)
        # (mark_task_completed 已被 _patch_env 屏蔽为 no-op,此处以 scope 状态佐证)
        assert orchestrator._has_active_scope(str(task.id))
    finally:
        _clear_scopes(str(task.id))


def test_no_parallel_flow_publishes_done(monkeypatch):
    """无并行流 → 审查结束推 done + finish_task(原行为不变)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    rec = _EventRecorder(monkeypatch)
    launched = []

    def _fake_launch(tid, msg, upload_ids=None):
        launched.append((tid, msg, upload_ids))

    monkeypatch.setattr(orchestrator, "launch_resume_thread", _fake_launch)

    try:
        orchestrator.run_dual_agent_audit(task, MagicMock())
    finally:
        clear_user_messages(str(task.id))

    assert rec.index("review_done") < rec.index("done")  # done 照常
    assert rec.index("done") < rec.index(("finish",))    # finish 照常
    assert launched == []  # 无遗留消息 → 不自动续轮


# ============================================================
# 遗留用户消息自动续轮:执行期间未被 drain 的消息不静默丢弃
# ============================================================


def test_leftover_messages_auto_resume_new_round(monkeypatch):
    """执行期间遗留(未被 drain)的用户消息 → 轮结束自动挪到新轮并
    launch_resume_thread(合并文本 + 去重附件 + 累积进 params);
    新流注册 scope 后审查收尾不推 done/不关总线(SSE 不断线)。"""
    task = _mk_task()
    task.params = {}
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    rec = _EventRecorder(monkeypatch)

    launched = []

    def _fake_launch(tid, msg, upload_ids=None):
        # 模拟真实行为:线程启动前同步注册 scope(新流活跃)
        orchestrator._begin_event_scope(tid)
        launched.append((tid, msg, upload_ids))

    monkeypatch.setattr(orchestrator, "launch_resume_thread", _fake_launch)

    # db mock:最新 Conversation round_idx=2 → 遗留消息挪到新轮 3
    db = MagicMock()
    latest = MagicMock()
    latest.round_idx = 2
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = latest
    # 挪轮后回查的遗留消息记录(接管时刻补推 conversation 入流)
    moved_a, moved_b = MagicMock(), MagicMock()
    moved_a.id, moved_b.id = "moved-a", "moved-b"
    moved_a.content, moved_b.content = "第一条遗留", "第二条遗留"
    for mv in (moved_a, moved_b):
        mv.round_idx = 3
        mv.role = "user"
        mv.type = "message"
        mv.reasoning = None
        mv.attachments = None
    db.query.return_value.filter.return_value.all.return_value = [moved_a, moved_b]

    m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        push_user_message(
            str(task.id), "第一条遗留",
            message_id=m1, created_at="t1", upload_ids=["u1", "u2"],
        )
        push_user_message(
            str(task.id), "第二条遗留",
            message_id=m2, created_at="t2", upload_ids=["u2", "u3"],
        )
        orchestrator.run_dual_agent_audit(task, db)

        # 自动续轮:合并文本(\n\n)+ 附件去重保序
        assert launched == [
            (str(task.id), "第一条遗留\n\n第二条遗留", ["u1", "u2", "u3"]),
        ]
        # 遗留消息的 Conversation 挪到新轮(round 2 → 3)
        update_mock = db.query.return_value.filter.return_value.update
        update_mock.assert_called_once()
        u_args, u_kwargs = update_mock.call_args
        assert u_args[0] == {"round_idx": 3}
        assert u_kwargs.get("synchronize_session") is False
        # 接管时刻补推 conversation 事件(消息进入新轮首的对话流,
        # 前端待处理条目随之清除;审查结论卡也走 conversation,按 role=user 过滤)
        user_conv_events = [
            c for c in rec.events("conversation") if c[2].get("role") == "user"
        ]
        assert [c[2]["content"] for c in user_conv_events] == ["第一条遗留", "第二条遗留"]
        # 附件累积进 params(沙箱回收后的重放依据)
        assert task.params["followup_upload_ids"] == ["u1", "u2", "u3"]
        # 审查照常落库知识点,但新流已接管(世代门控)→ 不写 badge、
        # 不推 review_done;也不推 done/finish(新流活跃,总线打开)
        assert task.review_status == "running"
        assert not rec.events("review_done")
        assert not rec.events("done")
        assert rec.index(("finish",)) == -1
    finally:
        _clear_scopes(str(task.id))
        clear_user_messages(str(task.id))


def test_superseded_review_skips_badge_updates(monkeypatch):
    """审查期间被新流取代(用户追问启动新一轮)→ 老审查照常落库知识点,
    但跳过 review_status 覆盖与 review_done 事件(badge 归新流所有,
    不被老审查收尾值短暂覆盖成 done/failed)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    probe_results = []

    def _ua(*args, **kwargs):
        # 审查执行期间:用户追问启动了新一轮(注册新 scope)→ 本流被取代
        orchestrator._begin_event_scope(task.id)
        check = kwargs.get("superseded_check")
        probe_results.append(check() if callable(check) else "missing")
        return _mk_review_result()

    _patch_env(monkeypatch, executor, _ua)
    rec = _EventRecorder(monkeypatch)
    db = MagicMock()

    try:
        orchestrator.run_dual_agent_audit(task, db)

        # 降级探针被传入且取代后返回 True
        assert probe_results and probe_results[-1] is True
        # 知识点照常落库(interim 1 + knowledge 2,按轮产出不受门控影响)
        results_added = [
            c.args[0] for c in db.add.call_args_list
            if isinstance(c.args[0], Result)
        ]
        assert len(results_added) == 3
        # review_status 保持 agent1 完成时的 running(新轮语义),
        # 不被老审查的 done 覆盖;review_done 事件不推
        assert task.review_status == "running"
        assert not rec.events("review_done")
    finally:
        _clear_scopes(str(task.id))
        clear_user_messages(str(task.id))


def test_dual_audit_failure_error_before_finish(monkeypatch):
    """异常路径(P3 回归):error 事件必须先于 finish_task 推送
    (总线关闭后 publish 会被静默丢弃,前端将永远收不到错误横幅)。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(side_effect=RuntimeError("agent1 崩溃"))

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result())
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    assert task.status == TaskStatus.FAILED
    assert rec.events("error")
    assert rec.index("error") < rec.index(("finish",))
    # 异常路径 scope 也被 finally 兜底注销(无泄漏)
    assert not orchestrator._has_active_scope(str(task.id))


# ============================================================
# 单 agent 模式:无审查,行为不变
# ============================================================


def test_single_agent_mode_no_review_events(monkeypatch):
    """agent2 禁用 → 无 agent1_done/review_done,review_status 保持 None。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    ua_called = []

    _patch_env(monkeypatch, executor, lambda *a, **k: ua_called.append(1))
    monkeypatch.setattr(
        orchestrator, "resolve_agent_policy",
        lambda *a, **k: {"agent2_enabled": False},
    )
    rec = _EventRecorder(monkeypatch)

    orchestrator.run_dual_agent_audit(task, MagicMock())

    assert task.status == TaskStatus.COMPLETED
    assert task.review_status is None          # 不进入审查状态机
    assert not ua_called                        # agent2 全程未被调用
    assert not rec.events("agent1_done")
    assert not rec.events("review_done")
    assert len(rec.events("done")) == 1         # 单 agent:done 直接终止
    assert rec.index("done") < rec.index(("finish",))


# ============================================================
# 用户终止检查(review_status=stopped)
# ============================================================


def test_review_stopped_keeps_interim_and_skips_downstream(monkeypatch):
    """终止检查:保留临时结果(不改知识点不删行)+ 把"整理中"标题改正
    + review_status=stopped + 推 review_done(stopped) + 下游链整体跳过。"""
    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    db = MagicMock()
    stopped = _mk_review_result(
        results=[], reasoning="检查已由用户终止,未得出审查结论。",
        stopped=True,
    )

    _patch_env(monkeypatch, executor, lambda *a, **k: stopped)
    rec = _EventRecorder(monkeypatch)
    import app.services.memory_summarize as memory_summarize
    import app.services.practice.auto_generate as auto_generate
    downstream = []
    monkeypatch.setattr(
        memory_summarize, "summarize_and_save_memory",
        lambda *a, **k: downstream.append("memory"),
    )
    monkeypatch.setattr(
        auto_generate, "auto_generate_practice_for_task",
        lambda *a, **k: downstream.append("practice"),
    )

    orchestrator.run_dual_agent_audit(task, db)

    assert task.status == TaskStatus.COMPLETED   # 终止不是任务失败
    assert task.review_status == "stopped"
    assert rec.events("review_done")[0][2] == {"review_status": "stopped"}
    assert rec.index("review_done") < rec.index("done")
    # 临时结果保留:只有 agent1 summary 那 1 条,未新增知识点
    results_added = [
        c.args[0] for c in db.add.call_args_list
        if isinstance(c.args[0], Result)
    ]
    # 也没有为替换知识点而再删一轮:全链路只删过一次(临时结果落库前的本轮清理)
    assert db.query.return_value.filter.return_value.delete.call_count == 1
    # 本轮知识点未写入:db.add 只出现过 1 次 Result(上面那条临时结果)
    assert len(results_added) == 1
    # "整理中"标题已改写(否则结果卡永远写着还在等待审查)
    updates = db.query.return_value.filter.return_value.update.call_args_list
    assert any(
        (call.args[0] or {}).get("title") == "执行结果(检查已终止)"
        for call in updates
    ), updates
    # 终止后下游链整体不跑(记忆归纳 + 自动出题)
    assert downstream == []


def test_background_review_registers_round_and_stop_check(monkeypatch):
    """审查按轮登记供终止端点定位;传入 agent2 的 stop_check 实时反映请求,
    审查收尾(无论成败)都注销登记。"""
    import app.review_stop as review_stop

    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    probe = {}

    def _ua(*args, **kwargs):
        check = kwargs.get("stop_check")
        probe["registered"] = review_stop.has_active_review(str(task.id))
        probe["callable"] = callable(check)
        probe["before_request"] = check() if callable(check) else "missing"
        review_stop.request_stop(task.id)
        probe["after_request"] = check() if callable(check) else "missing"
        return _mk_review_result(results=[], stopped=True)

    _patch_env(monkeypatch, executor, _ua)
    _EventRecorder(monkeypatch)

    try:
        orchestrator.run_dual_agent_audit(task, MagicMock())

        assert probe["registered"] is True
        assert probe["callable"] is True
        assert probe["before_request"] is False
        assert probe["after_request"] is True
        # 收尾已注销登记(finally 路径),注册表不随任务数增长
        assert review_stop.has_active_review(str(task.id)) is False
    finally:
        _clear_scopes(str(task.id))
        clear_user_messages(str(task.id))
        review_stop.clear_review_stop_state(str(task.id))


def test_review_round_registered_before_review_thread_starts(monkeypatch):
    """回归:agent1 完成时就登记审查轮,而不是等到审查真正开工。

    工作区 diff 捕获(大仓库可数秒)落在 review_status=running 与审查开工之间;
    这段窗口里点「终止检查」若因注册表为空而被判为"无审查在跑",端点会就地写
    stopped,随后审查照常跑完把终态改成 done —— 用户看到角标闪回"检查完成"。
    """
    import app.review_stop as review_stop

    task = _mk_task()
    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))
    probe = {}

    def _leftover(*args, **kwargs):
        # 这就是 diff 捕获之后、审查开工之前的那个窗口
        probe["registered"] = review_stop.has_active_review(str(task.id))
        probe["round"] = review_stop.request_stop(task.id)
        return False

    _patch_env(monkeypatch, executor, lambda *a, **k: _mk_review_result(stopped=True))
    monkeypatch.setattr(
        orchestrator, "_auto_resume_leftover_messages", _leftover,
    )
    _EventRecorder(monkeypatch)

    try:
        orchestrator.run_dual_agent_audit(task, MagicMock())

        assert probe["registered"] is True
        assert probe["round"] == 1            # 终止落在本轮审查上
        assert task.review_status == "stopped"  # 审查收尾尊重终止请求
    finally:
        _clear_scopes(str(task.id))
        clear_user_messages(str(task.id))
        review_stop.clear_review_stop_state(str(task.id))
