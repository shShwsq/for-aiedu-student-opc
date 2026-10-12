"""探索性追问推荐在 orchestrator 收尾链上的接线测试。

背景(2026-10 修复):推荐调用最初只接在「后台审查收尾」与「单 agent 初始轮」
两处,单 agent 的 resume 轮(_finish_resume)漏了 → 用户追问一次后,前端按
last-wins 取到的仍是上一轮的老建议,与当前对话脱节。

覆盖:
- _finish_resume 必须调用推荐,且**早于** _end_event_scope(推 done 关闭总线后,
  conversation 事件会被静默丢弃,实时到达就没了);
- 被新流取代(非最新世代)时跳过,不与并行新流重复产出;
- _followup_recommendations_safely 吞掉一切异常(可选轻调用不得影响任务收尾)。
"""
from unittest.mock import MagicMock

import pytest

from app.agents import orchestrator
from app.services import followup_recommender


@pytest.fixture
def order(monkeypatch):
    """桩掉 _finish_resume 的全部外部协作,只记录收尾链的调用顺序"""
    calls: list[str] = []

    monkeypatch.setattr(orchestrator, "_publish_status", lambda t: None)
    monkeypatch.setattr(orchestrator, "emit", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_auto_resume_leftover_messages", lambda *a: None)
    monkeypatch.setattr(orchestrator, "_precompress_history_safely", lambda *a: None)
    monkeypatch.setattr(
        "app.services.memory_summarize.summarize_and_save_memory", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.workspace_diff.save_workspace_diff_artifact", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.services.workspace_diff.save_repo_tree_artifact", lambda *a, **k: None,
    )

    def fake_end(task_id, gen, terminal=None):
        calls.append("end_event_scope")
        return True

    monkeypatch.setattr(orchestrator, "_end_event_scope", fake_end)
    return calls


def _run_resume(monkeypatch, order, *, latest: bool, round_idx: int = 2):
    monkeypatch.setattr(orchestrator, "_is_latest_generation", lambda tid, gen: latest)
    task = MagicMock()
    task.id = "t-resume"

    orchestrator._finish_resume(
        task, MagicMock(), [{"round": round_idx, "summary": "s"}],
        react_client=None, flow_gen=7, round_idx=round_idx,
    )
    return task


# ============================================================
# 单 agent resume 轮:推荐必须发生,且早于 done
# ============================================================


def test_finish_resume_recommends_before_done(monkeypatch, order):
    calls = order

    def fake_recommend(task, db, round_idx):
        calls.append(f"recommend:{round_idx}")

    monkeypatch.setattr(orchestrator, "_followup_recommendations_safely", fake_recommend)

    _run_resume(monkeypatch, order, latest=True, round_idx=2)

    # 顺序即契约:先落库/推 conversation 事件,再推 done 关总线
    assert calls == ["recommend:2", "end_event_scope"]


def test_finish_resume_skips_recommendation_when_superseded(monkeypatch, order):
    """已有更新的流接管:本流不产出,交给新轮(避免并行重复/留老建议占位)"""

    def fake_recommend(task, db, round_idx):
        order.append("recommend")

    monkeypatch.setattr(orchestrator, "_followup_recommendations_safely", fake_recommend)

    _run_resume(monkeypatch, order, latest=False)

    assert "recommend" not in order
    assert order == ["end_event_scope"]  # 收尾仍照常(总线不能悬挂)


# ============================================================
# 安全包装:失败静默
# ============================================================


def test_safely_wrapper_swallows_recommender_failure(monkeypatch):
    def boom(task, db, round_idx):
        raise RuntimeError("LLM 挂了")

    monkeypatch.setattr(followup_recommender, "recommend_followups", boom)

    # 不抛出即通过:一次可选的轻调用不得影响任务收尾
    orchestrator._followup_recommendations_safely(MagicMock(id="t-1"), MagicMock(), 1)


def test_safely_wrapper_calls_through_on_success(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        followup_recommender, "recommend_followups",
        lambda task, db, round_idx: seen.append((task, round_idx)),
    )

    task = MagicMock(id="t-2")
    orchestrator._followup_recommendations_safely(task, MagicMock(), 3)

    assert seen == [(task, 3)]
