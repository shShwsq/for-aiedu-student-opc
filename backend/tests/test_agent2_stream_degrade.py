"""agent2 流式调用降级 + error_message 兜底增强测试

背景:用户发追问时,agent2 评估阶段的流式 LLM 调用失败会直接 raise,
导致整个任务 FAILED,前端只显示"未知错误"。修复:
1. agent2.py:流式调用失败重试一次,仍失败返回 degraded=true 降级结果
   (不抛异常杀死任务);orchestrator 检测到降级后标记审查失败,
   保留 agent1 临时结果;
2. orchestrator.py / tasks.py:error_message 兜底增强——异常消息为空时
   补异常类型名(_err_detail),杜绝 UI 显示"未知错误"字面。

另覆盖:resume 直传语义——用户追加消息不经 agent2 转述,原文直接
交给 agent1 执行(followup_query 逐字等于用户消息)。
"""
import uuid
from unittest.mock import MagicMock

import pytest

# 导入关系依赖模型,确保独立测试环境下 SQLAlchemy mapper 可完成配置
# (后台线程端点内创建真实 Conversation 等记录)
import app.models.task_artifact  # noqa: F401
import app.models.user_git_binding  # noqa: F401

import app.agents.orchestrator as orchestrator
import app.agents.agent2 as agent2
import app.database as app_database
from app.models.task import TaskStatus


# 本文件被测 task 登记表(按 task.id 键):供下方按轮会话替身取回同一个对象。
# orchestrator._run_agent1_rounds_with_auto_continue 每轮走 _round_session() 的独立
# 短会话(commit 87bebcc "DB 会话纪律"),它直连 SessionLocal:测试环境连不到库时
# 会一路等到 connect_timeout 才炸,流水线把任务判成 failed,resume/dual 两组断言
# 全部错位。跟 test_pause_resume_hardening 同样手法把 SessionLocal 换成替身工厂。
_TASK_UNDER_TEST: dict[str, object] = {}


def _register_task(task):
    _TASK_UNDER_TEST[str(task.id)] = task
    return task


def _task_for(task_id):
    """按 id 取回本用例的 task;取不到直接 fail(不返回 None 也不给假对象)。

    返回 None 会被流水线当成"任务已删"提前收尾,返回新 MagicMock 则把轮内写操作
    用到另一个对象上 —— 两者都是静默错测。pytest.fail 抛的 Failed 继承自
    BaseException,不会被 orchestrator 的 except Exception 吞掉,能原样报出来。
    """
    task = _TASK_UNDER_TEST.get(str(task_id))
    if task is None:
        pytest.fail(
            f"按轮会话替身取不到 task_id={task_id}:用例未用本文件的"
            " _mk_resume_task()/_mk_dual_task() 造 task"
        )
    return task


@pytest.fixture(autouse=True)
def _stub_round_session(monkeypatch):
    """按轮短会话不连真库:get(Task, id) 返回本用例正在断言的 task。

    _round_session() 本体不改,退出时的 close() 由 MagicMock 自接;用例自己
    setattr SessionLocal 时(如后台线程兜底用例)会覆盖本 fixture。
    """
    def _fake_session():
        round_db = MagicMock(name="round_db")
        round_db.get.side_effect = lambda model, pk, *a, **k: _task_for(pk)
        return round_db

    monkeypatch.setattr(orchestrator, "SessionLocal", _fake_session)


# ============================================================
# agent2 流式调用降级(重试一次 + 降级返回)
# ============================================================


def test_stream_fail_once_retry_success(monkeypatch):
    """第一次流式调用失败 → 重试一次成功,返回正常评估结果(无降级标记)。"""
    calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("网络抖动")
        return (
            '{"covered": [], "missing": [], "reasoning": "重试成功", '
            '"suggestions": [], "results": [], "grouping": null}',
            [], "思考链",
        )

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-1", round_idx=1, client=MagicMock(),
    )

    assert len(calls) == 2  # 失败后重试了一次
    assert result.get("degraded") is not True
    assert result.get("reasoning") == "重试成功"


def test_stream_fail_twice_returns_degraded_review(monkeypatch):
    """审查:两次流式调用都失败 → 降级结果(无轮次语义,审查失败标记)。"""
    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent2, "_stream_agent2_llm", _fake_stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-1", round_idx=1, client=MagicMock(),
    )

    assert result["degraded"] is True
    assert result["degrade_reason"] == "boom"
    assert result["results"] == []          # 空 results → 调用方标记审查失败
    assert result["suggestions"] == []
    assert "降级" in result["reasoning"]
    # 无 followup_query/done 轮次语义(一次性完整审查)
    assert "followup_query" not in result
    assert "done" not in result


# ============================================================
# 并行降级:superseded_check(被新流取代时跳过 verify 动态验证)
# ============================================================


def _mk_verify_stream(scripted_rounds):
    """构造脚本化 LLM 流:按调用序返回 (content, tool_calls, reasoning)"""
    calls = []

    def _fake_stream(client, messages, *, task_id, round_idx, tools=None):
        calls.append([m for m in messages if m.get("role") == "tool"])
        if len(calls) <= len(scripted_rounds):
            return scripted_rounds[len(calls) - 1]
        return ('{"covered": [], "missing": [], "reasoning": "兜底", '
                '"suggestions": [], "results": [], "grouping": null}', [], "")

    _fake_stream.calls = calls
    return _fake_stream


def test_verify_skipped_when_superseded(monkeypatch):
    """并行降级:superseded_check=True → verify 被跳过(verifier 不执行,
    LLM 收到降级说明并继续只读评估)—— 与新轮共享沙箱不再跑 PoC。"""
    import app.agents.verifier_agent as verifier_agent

    verifier_calls = []
    monkeypatch.setattr(
        verifier_agent, "run_verifier_agent",
        lambda *a, **k: verifier_calls.append(1) or "不应执行",
    )
    stream = _mk_verify_stream([
        # 第一轮:LLM 要求动态验证
        ("", [{"id": "call_1", "index": 0, "name": "verify",
               "arguments_str": '{"verification_request": "发送 PoC 确认"}'}],
         "想验证一下"),
        # 第二轮:看到降级说明后输出最终评估
        ('{"covered": [], "missing": [], "reasoning": "只读评估完成", '
         '"suggestions": [], "results": [{"title": "知识点", "content": "说明"}], '
         '"grouping": null}', [], ""),
    ])
    monkeypatch.setattr(agent2, "_stream_agent2_llm", stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-sup", round_idx=1, client=MagicMock(),
        superseded_check=lambda: True,
    )

    assert verifier_calls == []  # PoC 验证被降级跳过
    assert result.get("reasoning") == "只读评估完成"  # 审查照常完成
    # LLM 收到降级说明(第二轮上下文的工具结果里)
    assert any(
        "已被新一轮执行取代" in m.get("content", "")
        for m in stream.calls[1]
    )


def test_verify_executed_when_not_superseded(monkeypatch):
    """未取代:superseded_check=False → verify 正常执行(降级不误伤)。"""
    import app.agents.verifier_agent as verifier_agent

    verifier_calls = []
    monkeypatch.setattr(
        verifier_agent, "run_verifier_agent",
        lambda *a, **k: verifier_calls.append(1) or "PoC 验证结果:确认存在",
    )
    stream = _mk_verify_stream([
        ("", [{"id": "call_1", "index": 0, "name": "verify",
               "arguments_str": '{"verification_request": "发送 PoC 确认"}'}],
         "想验证一下"),
        ('{"covered": [], "missing": [], "reasoning": "验证后评估", '
         '"suggestions": [], "results": [{"title": "知识点", "content": "说明"}], '
         '"grouping": null}', [], ""),
    ])
    monkeypatch.setattr(agent2, "_stream_agent2_llm", stream)

    result = agent2.run_agent2(
        "审计这个仓库", [], task_id="task-nosup", round_idx=1, client=MagicMock(),
        superseded_check=lambda: False,
    )

    assert verifier_calls == [1]  # 正常执行了一次 PoC 验证
    assert result.get("reasoning") == "验证后评估"


# ============================================================
# resume_audit_with_message:降级分流 + 错误兜底
# ============================================================


def _mk_resume_task():
    task = MagicMock()
    task.id = "task-1"
    task.status = TaskStatus.COMPLETED
    task.current_stage = ""
    task.error_message = None
    task.user_input = "审计这个仓库"
    task.scenario = "general"
    task.user_id = None
    task.llm_config_id = None
    task.params = {}
    task.allowed_skills = None
    task.verifier_enabled = False
    task.test_env_url = ""
    return _register_task(task)


def _patch_resume_env(monkeypatch, executor, ua_side_effect):
    """屏蔽 resume 链路的副作用,只测降级分流。"""
    monkeypatch.setattr(
        orchestrator, "_resolve_llm_client",
        lambda *a, **k: (MagicMock(), orchestrator.CLIENT_FROM_TASK_CONFIG),
    )
    monkeypatch.setattr(
        orchestrator, "_build_react_llm_client",
        lambda *a, **k: (MagicMock(), None),
    )
    monkeypatch.setattr(orchestrator, "_load_git_tokens", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "set_current_git_tokens", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "get_executor", lambda *a, **k: executor)
    monkeypatch.setattr(
        orchestrator, "resolve_agent_policy",
        lambda *a, **k: {"agent2_enabled": True},
    )
    monkeypatch.setattr(orchestrator, "_publish_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_restore_workspace_if_needed",
                        lambda *a, **k: True)  # True → 跳过记忆文件补写
    monkeypatch.setattr(orchestrator, "_load_react_summaries", lambda *a, **k: [])
    monkeypatch.setattr(orchestrator, "_get_next_round_idx", lambda *a, **k: 2)
    monkeypatch.setattr(orchestrator, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "finish_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.sandbox_tools, "mark_task_completed",
                        lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "run_agent2", ua_side_effect)

    # _run_background_review 内部导入的归纳记忆/工作区 diff:屏蔽真实副作用
    import app.services.memory_summarize as memory_summarize
    import app.services.workspace_diff as workspace_diff
    monkeypatch.setattr(memory_summarize, "summarize_and_save_memory",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_workspace_diff_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_repo_tree_artifact",
                        lambda *a, **k: None)

    # publish 全量接管:丢弃所有事件(避免碰真实事件总线)
    monkeypatch.setattr(orchestrator, "publish", lambda *a, **k: None)


def test_resume_passes_user_message_directly(monkeypatch):
    """resume:用户消息原文直传 agent1(不经 agent2 转述),
    agent2 只在 agent1 结束后做一次后台审查。"""
    task = _mk_resume_task()
    review = {"covered": [], "missing": [], "reasoning": "审查完成",
              "suggestions": [], "results": [{"title": "发现", "content": "内容"}],
              "grouping": None}
    call_order = []  # 记录 agent1 执行与 agent2 审查的先后顺序

    def _ua(*args, **kwargs):
        call_order.append("agent2")
        return review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(
        side_effect=lambda *a, **k: call_order.append("agent1") or ([], "总结", []),
    )

    _patch_resume_env(monkeypatch, executor, _ua)

    orchestrator.resume_audit_with_message(
        task, MagicMock(), "再帮我查下依赖漏洞",
    )

    # 用户消息逐字直传 agent1,只跑一轮
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "再帮我查下依赖漏洞"
    assert task.status == TaskStatus.COMPLETED
    # agent2 仅一次调用,且发生在 agent1 执行之后(后台审查)
    assert call_order == ["agent1", "agent2"]
    assert task.review_status == "done"


def test_resume_collab_degraded_ends_round(monkeypatch):
    """resume 后台审查降级 → review_status=failed,任务仍 COMPLETED(保留临时结果)。"""
    task = _mk_resume_task()
    degraded_review = {"covered": [], "missing": [], "reasoning": "审查失败降级",
                       "suggestions": [], "results": [], "grouping": None,
                       "degraded": True, "degrade_reason": "boom"}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return degraded_review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_resume_env(monkeypatch, executor, _ua)

    orchestrator.resume_audit_with_message(task, MagicMock(), "再查依赖")

    # agent1 只跑一轮(用户消息直传);审查降级不触发再执行
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "再查依赖"
    assert task.status == TaskStatus.COMPLETED
    # 审查降级 → 子状态 failed(任务本身不失败)
    assert task.review_status == "failed"


def test_resume_except_typed_error_message(monkeypatch):
    """resume 链路异常消息为空 → error_message 补异常类型名,不再显示"未知错误"。"""
    task = _mk_resume_task()

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(side_effect=RuntimeError(""))
    _patch_resume_env(monkeypatch, executor, lambda *a, **k: None)

    orchestrator.resume_audit_with_message(task, MagicMock(), "消息")

    assert task.status == TaskStatus.FAILED
    assert task.error_message == "RuntimeError(无错误详情)"


# ============================================================
# run_pipeline_audit:直接执行 + 降级分流
# ============================================================


def _mk_dual_task():
    task = MagicMock()
    task.id = "task-d"
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
    return _register_task(task)


def _patch_dual_env(monkeypatch, executor, ua_side_effect):
    """屏蔽 dual 链路的副作用,只测执行与降级分流。"""
    monkeypatch.setattr(orchestrator, "resolve_agent_policy",
                        lambda *a, **k: {"agent2_enabled": True})
    monkeypatch.setattr(orchestrator, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(
        orchestrator, "_resolve_llm_client",
        lambda *a, **k: (MagicMock(), orchestrator.CLIENT_FROM_TASK_CONFIG),
    )
    monkeypatch.setattr(orchestrator, "_build_react_llm_client",
                        lambda *a, **k: (MagicMock(), None))
    monkeypatch.setattr(orchestrator, "_load_git_tokens", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator, "set_current_git_tokens", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "get_executor", lambda *a, **k: executor)
    monkeypatch.setattr(orchestrator, "_prepare_repo_context",
                        lambda *a, **k: (None, ""))  # 无仓库:跳过 clone
    monkeypatch.setattr(orchestrator, "_publish_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_record_agent2_review", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_replace_interim_results",
                        lambda *a, **k: 1)
    monkeypatch.setattr(orchestrator, "_add_conversation", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "finish_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.sandbox_tools, "mark_task_completed",
                        lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "run_agent2", ua_side_effect)

    import app.services.memory_summarize as memory_summarize
    import app.services.workspace_diff as workspace_diff
    monkeypatch.setattr(memory_summarize, "summarize_and_save_memory",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_workspace_diff_artifact",
                        lambda *a, **k: None)
    monkeypatch.setattr(workspace_diff, "save_repo_tree_artifact",
                        lambda *a, **k: None)

    monkeypatch.setattr(orchestrator, "publish", lambda *a, **k: None)


def test_dual_starts_with_react_agent_no_initial_eval(monkeypatch):
    """任务开始时无 agent2 初始评估:agent1 第 1 轮直接按用户意图执行,
    agent2 只在 agent1 结束后做一次后台审查。"""
    task = _mk_dual_task()
    review = {"covered": [], "missing": [], "reasoning": "审查完成",
              "suggestions": [], "results": [{"title": "发现", "content": "内容"}],
              "grouping": None}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_dual_env(monkeypatch, executor, _ua)

    orchestrator.run_pipeline_audit(task, MagicMock())

    # agent1 先执行:第 1 轮、followup_query=None(直接用用户意图),只此一轮
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["round_idx"] == 1
    assert executor.run.call_args.kwargs["followup_query"] is None
    # agent2 审查仅一次,发生在 agent1 执行之后(单次完整审查)
    assert len(ua_calls) == 1
    assert task.status == TaskStatus.COMPLETED
    assert task.review_status == "done"


def test_dual_collab_degraded_ends_round(monkeypatch):
    """dual 后台审查降级 → review_status=failed,任务仍 COMPLETED,
    agent1 只跑一轮(多轮由用户驱动,降级不触发再执行)。"""
    task = _mk_dual_task()
    degraded_review = {"covered": [], "missing": [], "reasoning": "审查失败降级",
                       "suggestions": [], "results": [], "grouping": None,
                       "degraded": True, "degrade_reason": "boom"}
    ua_calls = []

    def _ua(*args, **kwargs):
        ua_calls.append(kwargs)
        return degraded_review

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "总结", []))

    _patch_dual_env(monkeypatch, executor, _ua)

    orchestrator.run_pipeline_audit(task, MagicMock())

    # 初始运行只有 1 轮 agent1(协作循环已移除)
    assert executor.run.call_count == 1
    assert executor.run.call_args.kwargs["followup_query"] is None
    assert task.status == TaskStatus.COMPLETED
    # 审查降级 → 子状态 failed(任务本身不失败,保留临时结果)
    assert task.review_status == "failed"


# ============================================================
# 纯对话轮:本轮无工具调用 → 跳过后台审查与重下游
# ============================================================


def _mk_no_tool_db():
    """db mock:_round_has_tool_calls 的 query→filter→first 链返回 None
    (即本轮没有任何 tool_call 记录)。"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def test_dual_conversation_round_skips_review(monkeypatch):
    """初始运行纯对话轮(如问候,无工具调用)→ agent2 不被调用,任务直接完成。"""
    task = _mk_dual_task()
    ua_calls = []

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "你好!很高兴帮你分析代码。", []))

    _patch_dual_env(monkeypatch, executor, lambda *a, **k: ua_calls.append(1))

    orchestrator.run_pipeline_audit(task, _mk_no_tool_db())

    executor.run.assert_called_once()
    assert not ua_calls  # 纯对话轮不触发 agent2 审查
    assert task.status == TaskStatus.COMPLETED
    assert "纯对话" in task.current_stage


def test_resume_conversation_round_skips_review(monkeypatch):
    """resume 纯对话轮(追问直接回答,无工具调用)→ 跳过审查/练习题/记忆,
    保留既有结果与上一轮审查状态。"""
    task = _mk_resume_task()
    task.review_status = "done"  # 上一轮(分析轮)的审查状态
    ua_calls = []

    executor = MagicMock()
    executor.name = "builtin"
    executor.run = MagicMock(return_value=([], "这个函数用于校验入参。", []))

    _patch_resume_env(monkeypatch, executor, lambda *a, **k: ua_calls.append(1))

    orchestrator.resume_audit_with_message(task, _mk_no_tool_db(), "这个函数是干嘛的?")

    # 用户消息仍逐字直传 agent1
    executor.run.assert_called_once()
    assert executor.run.call_args.kwargs["followup_query"] == "这个函数是干嘛的?"
    assert not ua_calls  # 纯对话轮不触发 agent2 审查
    assert task.status == TaskStatus.COMPLETED
    assert "纯对话" in task.current_stage
    # 既有审查状态保留(未被置为 running/failed)
    assert task.review_status == "done"


# ============================================================
# _err_detail:error_message 兜底增强
# ============================================================


def test_err_detail_empty_message():
    """异常消息为空 → 补异常类型名;有消息 → 原样返回。"""
    assert orchestrator._err_detail(RuntimeError("boom")) == "boom"
    assert orchestrator._err_detail(RuntimeError()) == "RuntimeError(无错误详情)"
    assert orchestrator._err_detail(ValueError("")) == "ValueError(无错误详情)"


def test_resume_background_except_typed_error_message(monkeypatch):
    """resume 后台线程 except:空消息异常 → error_message 补异常类型名。
    (resume 线程体已迁至 orchestrator._run_resume_in_background)"""
    task_id = str(uuid.uuid4())
    task = MagicMock()
    task.id = uuid.UUID(task_id)
    task.status = TaskStatus.RUNNING
    task.error_message = None
    db = MagicMock()
    db.get.return_value = task

    monkeypatch.setattr(orchestrator, "SessionLocal", lambda: db)
    # 终态兜底走 app.database.persist_task_failure_fresh 的全新独立会话(87bebcc):
    # 它从 app.database 命名空间取 SessionLocal,只换 orchestrator 里的同名引用拦不到,
    # 不补这一刀会真连库等到 connect_timeout,任务留在 RUNNING。
    monkeypatch.setattr(app_database, "SessionLocal", lambda: db)

    def _boom(*a, **k):
        raise RuntimeError("")

    monkeypatch.setattr(orchestrator, "resume_audit_with_message", _boom)
    error_events = []
    monkeypatch.setattr(
        orchestrator, "publish",
        lambda tid, event, data: error_events.append(data) if event == "error" else None,
    )
    monkeypatch.setattr(orchestrator, "finish_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "clear_pause_state", lambda *a, **k: None)

    orchestrator._run_resume_in_background(task_id, "消息")

    assert task.status == TaskStatus.FAILED
    assert task.error_message == "RuntimeError(无错误详情)"
    assert error_events
    assert error_events[0]["error_message"] == "RuntimeError(无错误详情)"
