"""agent2 审查 prompt 输出契约锚点测试

agent2 职责收敛为后台审查(单 prompt):
- AGENT2_REVIEW_PROMPT:agent1 结束后的单次完整后台审查,
  输出 covered/missing/reasoning/suggestions/results/grouping,
  无 followup_query/done 轮次语义;缺口降级为"建议追问方向"。
  resume 时用户消息直接交给 agent1,不经 agent2 分析转述。

AGENT2_SYSTEM_PROMPT 为审查模式的兼容别名。

本测试锚定关键契约关键词,防止后续误改导致契约回归。
"""
import pytest

from app.prompts.agent2 import (
    AGENT2_REVIEW_PROMPT,
    AGENT2_SYSTEM_PROMPT,
    _SUBMIT_REVIEW_PLAN_TOOL,
    _SUBMIT_REVIEW_ITEM_TOOL,
    _SUBMIT_KNOWLEDGE_POINT_TOOL,
    _SUBMIT_SUGGESTION_TOOL,
)


def test_system_prompt_alias_points_to_review():
    """兼容别名指向审查模式 prompt(旧引用不破坏)。"""
    assert AGENT2_SYSTEM_PROMPT is AGENT2_REVIEW_PROMPT



# ============================================================
# 审查模式契约
# ============================================================


def test_review_prompt_contains_knowledge_results_contract():
    """results 契约:重点与知识点形态 + learning_note + practice_worthy。"""
    assert "重点与知识点" in AGENT2_REVIEW_PROMPT
    assert "3-8" in AGENT2_REVIEW_PROMPT
    assert "learning_note" in AGENT2_REVIEW_PROMPT
    assert "practice_worthy" in AGENT2_REVIEW_PROMPT
    # 不是全量发现清单的语义
    assert "不是全量发现清单" in AGENT2_REVIEW_PROMPT


def test_review_prompt_grouping_is_backend_derived():
    """grouping/covered/missing/reasoning 由后端聚合派生(不再是模型输出契约)。"""
    assert "由后端" in AGENT2_REVIEW_PROMPT
    assert "聚合派生" in AGENT2_REVIEW_PROMPT


def test_review_prompt_has_four_emit_tools_and_planning():
    """工具发射契约:四发射工具 + 规划先行 + 不产 confidence + call_ref 证据台账。"""
    for name in (
        "submit_review_plan", "submit_review_item",
        "submit_knowledge_point", "submit_suggestion",
    ):
        assert name in AGENT2_REVIEW_PROMPT
    # 规划先行(必须含 requirement/baseline,否则漏审)
    assert "规划先行" in AGENT2_REVIEW_PROMPT
    assert "user_requirement" in AGENT2_REVIEW_PROMPT
    assert "domain_baseline" in AGENT2_REVIEW_PROMPT
    # 置信后端派生,模型不自报
    assert "不要自报" in AGENT2_REVIEW_PROMPT
    assert "confidence" in AGENT2_REVIEW_PROMPT
    # 证据引用就近 call_ref
    assert "_evidence_ref" in AGENT2_REVIEW_PROMPT
    assert "call_ref" in AGENT2_REVIEW_PROMPT
    # 单一原语三态合一 + review_target 必填可追溯
    assert "三态合一" in AGENT2_REVIEW_PROMPT
    assert "review_target" in AGENT2_REVIEW_PROMPT


def test_submit_tool_definitions_shape():
    """四个发射工具的 function.name 与关键参数就位。"""
    assert _SUBMIT_REVIEW_PLAN_TOOL["function"]["name"] == "submit_review_plan"
    assert _SUBMIT_REVIEW_ITEM_TOOL["function"]["name"] == "submit_review_item"
    assert _SUBMIT_KNOWLEDGE_POINT_TOOL["function"]["name"] == "submit_knowledge_point"
    assert _SUBMIT_SUGGESTION_TOOL["function"]["name"] == "submit_suggestion"
    item_props = _SUBMIT_REVIEW_ITEM_TOOL["function"]["parameters"]["properties"]
    assert {"review_target", "origin", "status", "verdict", "evidence"} <= set(item_props)
    # submit_review_item 不含 confidence(后端派生)
    assert "confidence" not in item_props


def test_review_prompt_ship_conclusion_is_conditional():
    """落地决策结论:仅当用户意图涉及采用/发布/签署等决策时才附,且措辞场景中立。

    agent2 是全场景共用的验证者,决策措辞不绑定 code-centric 的「敢不敢上线」;
    “能否上线”只是代码任务下的一个实例,由 reasoning 说明按任务类型自行选择。
    """
    assert "当用户意图" in AGENT2_REVIEW_PROMPT
    # 泛化为场景中立措辞:不硬编码「敢不敢上线/敢不敢用」
    assert "敢不敢上线" not in AGENT2_REVIEW_PROMPT
    # 旧的硬性/绑码表述均已移除
    assert "必须给出「敢不敢上线" not in AGENT2_REVIEW_PROMPT


def test_review_prompt_has_suggestions_contract():
    """建议追问契约:具体可执行、最后手段(能自查的不列建议)。"""
    assert "submit_suggestion" in AGENT2_REVIEW_PROMPT
    assert "最多 3 条" in AGENT2_REVIEW_PROMPT
    assert "最后手段" in AGENT2_REVIEW_PROMPT
    assert "你能自己核查确认的,一律不列建议" in AGENT2_REVIEW_PROMPT


def test_review_prompt_no_round_or_followup_semantics():
    """审查模式无轮次/追问语义:只审不改,多轮由用户驱动。"""
    assert "只审不改" in AGENT2_REVIEW_PROMPT
    assert "followup_query" not in AGENT2_REVIEW_PROMPT


def test_review_prompt_behind_scenes_positioning():
    """幕后审查定位:agent1 是台前回答者,agent2 过程经侧栏呈现。"""
    assert "幕后审查者" in AGENT2_REVIEW_PROMPT
    assert "台前回答者" in AGENT2_REVIEW_PROMPT
    assert "侧栏" in AGENT2_REVIEW_PROMPT


def test_review_prompt_reference_and_verify_sections_kept():
    """引用复核与动态验证章节保留(核查手段不变)。"""
    assert "引用复核" in AGENT2_REVIEW_PROMPT
    assert "动态验证" in AGENT2_REVIEW_PROMPT
    assert "check_reference" in AGENT2_REVIEW_PROMPT


def test_review_prompt_material_wording_generalized():
    """去代码化措辞:核实依据/只读核查/定位泛化为“源码或文书原文”,
    审查维度与新场景命名(代码审核/文书审核)对齐。"""
    assert "真实依据(源码或原文引用)" in AGENT2_REVIEW_PROMPT
    assert "核对工作区文件(源码或文书原文)" in AGENT2_REVIEW_PROMPT
    assert "代码审核任务" in AGENT2_REVIEW_PROMPT
    assert "文书审核任务" in AGENT2_REVIEW_PROMPT
    # 旧的纯代码措辞已移除
    assert "真实源码依据" not in AGENT2_REVIEW_PROMPT
