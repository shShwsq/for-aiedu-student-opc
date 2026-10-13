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
    AGENT2_PLAN_ITEM_FINALIZE_PROMPT,
    AGENT2_PLAN_PARALLEL_GUIDANCE,
    AGENT2_REVIEW_PROMPT,
    AGENT2_SYSTEM_PROMPT,
    _SUBMIT_REVIEW_PLAN_TOOL,
    _SUBMIT_REVIEW_ITEM_TOOL,
    _SUBMIT_KNOWLEDGE_POINT_TOOL,
    _SUBMIT_SUGGESTION_TOOL,
    build_agent2_plan_item_instruction,
    build_agent2_plan_result_messages,
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
    # 规划先行(origin 收敛为 agent1 的结论/改动产出两类)
    assert "规划先行" in AGENT2_REVIEW_PROMPT
    assert "agent1_claim" in AGENT2_REVIEW_PROMPT
    assert "agent1_action" in AGENT2_REVIEW_PROMPT
    # 置信后端派生,模型不自报
    assert "不要自报" in AGENT2_REVIEW_PROMPT
    assert "confidence" in AGENT2_REVIEW_PROMPT
    # 证据引用就近 call_ref
    assert "_evidence_ref" in AGENT2_REVIEW_PROMPT
    assert "call_ref" in AGENT2_REVIEW_PROMPT
    # 单一原语三分档合一 + review_target(待核实对象)必填可追溯
    assert "三分档合一" in AGENT2_REVIEW_PROMPT
    assert "待核实对象" in AGENT2_REVIEW_PROMPT
    assert "review_target" in AGENT2_REVIEW_PROMPT
    # 未核实的计划项不再被伪造成缺口审查项
    assert "不会" in AGENT2_REVIEW_PROMPT and "未核实" in AGENT2_REVIEW_PROMPT
    assert "回填为缺口" not in AGENT2_REVIEW_PROMPT


def test_review_prompt_has_user_readability_constraints():
    """用户可读性硬约束:正文不直铺内部标识与字段枚举(实测出现过
    「领域基线(历史记忆)」「E9」「(target 不存在)」「agent1 断言:…」直接面用户)"""
    assert "用户可读性硬约束" in AGENT2_REVIEW_PROMPT
    assert "禁写内部标识" in AGENT2_REVIEW_PROMPT
    # 取证引用号与枚举值不准进用户正文
    for banned in ("call_ref", "fail-closed", "领域基线", "历史记忆"):
        assert banned in AGENT2_REVIEW_PROMPT  # 作为"禁写"清单出现
    assert "禁以「agent1 断言:" in AGENT2_REVIEW_PROMPT
    # 三分档的用户可读名
    for label in ("已核实成立", "已核实不成立", "待核实"):
        assert label in AGENT2_REVIEW_PROMPT
    # 可追问内容 = 可执行的问句(不是"建议追问:…")
    assert "follow_up" in AGENT2_REVIEW_PROMPT
    assert "可追问内容" in AGENT2_REVIEW_PROMPT


def test_review_prompt_bans_agent_names_and_requires_first_person():
    """缺口建议/追问文本=点击即发送的用户消息:禁现 agent1/agent2 内部名,
    须写成用户第一人称祈使指令(不得"让 agent1 …"第三人称转述)。

    实测出现过的回归:面用户的建议写成「让 agent1 收回三个仍在运行的后台代理…」,
    而前端 handleSuggestionDig 把该文本原样 sendTaskMessage 发出 → 泄露内部名且语气不对。
    """
    # 可读性硬约束已把 suggestion 纳入受管字段,并把 agent1/agent2 列入禁写标识
    assert "title / conclusion / follow_up / suggestion" in AGENT2_REVIEW_PROMPT
    assert "`agent1`/`agent2`" in AGENT2_REVIEW_PROMPT
    # 追问/建议=点击即发送的用户消息,须第一人称祈使、禁第三人称转述
    assert "点击即发送" in AGENT2_REVIEW_PROMPT
    assert "第一人称" in AGENT2_REVIEW_PROMPT
    assert "第三人称转述" in AGENT2_REVIEW_PROMPT
    # submit_suggestion 工具描述同样要求可原样发送
    suggestion_desc = _SUBMIT_SUGGESTION_TOOL["function"]["description"]
    assert "原样作为用户消息" in suggestion_desc
    assert "第一人称" in suggestion_desc
    assert "祈使" in suggestion_desc


def test_submit_tool_definitions_shape():
    """四个发射工具的 function.name 与关键参数就位。"""
    assert _SUBMIT_REVIEW_PLAN_TOOL["function"]["name"] == "submit_review_plan"
    assert _SUBMIT_REVIEW_ITEM_TOOL["function"]["name"] == "submit_review_item"
    assert _SUBMIT_KNOWLEDGE_POINT_TOOL["function"]["name"] == "submit_knowledge_point"
    assert _SUBMIT_SUGGESTION_TOOL["function"]["name"] == "submit_suggestion"
    item_props = _SUBMIT_REVIEW_ITEM_TOOL["function"]["parameters"]["properties"]
    assert {
        "review_target", "origin", "status", "verdict", "evidence",
        "conclusion", "follow_up", "plan_item_id",
    } <= set(item_props)
    # 字段改名后的旧名不再是参数(suggestion 只作兼容别名留在 schema 里)
    assert "description" not in item_props
    # submit_review_item 不含 confidence(后端派生)
    assert "confidence" not in item_props
    # 计划项 id 由后端铸造并回灌(规划对账靠它,不再靠 target 子串匹配)
    plan_props = _SUBMIT_REVIEW_PLAN_TOOL["function"]["parameters"]["properties"]
    assert "items" in plan_props
    assert "plan_item_id" in _SUBMIT_REVIEW_PLAN_TOOL["function"]["description"]


def test_knowledge_point_content_adopts_explanation_contract():
    """知识点讲解统一:agent2 的 content 按练习侧四段式契约输出。

    回归点:任务侧 content 会被出题收尾**直接采用**为看板讲解(见
    explainer.sync_knowledge_explanations),两侧格式分家会让同一讲解
    出现两种排版 —— 故工具描述与审查 prompt 都锁定共享契约。
    """
    from app.prompts.practice import EXPLANATION_FORMAT_CONTRACT

    kp_props = _SUBMIT_KNOWLEDGE_POINT_TOOL["function"]["parameters"]["properties"]
    content_desc = kp_props["content"]["description"]
    # 工具描述内嵌共享契约(四段标题 + 不虚构路径行号)
    assert EXPLANATION_FORMAT_CONTRACT in content_desc
    assert "直接采用" in content_desc
    # 审查 prompt 的「提炼知识点」段同样要求四段式且不是结论复述
    assert "### 是什么 / ### 为什么会踩 / ### 怎么判断与修复 / ### 易错点" in AGENT2_REVIEW_PROMPT
    assert "不是审查结论复述" in AGENT2_REVIEW_PROMPT


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
    """建议追问契约:agent1 未覆盖/需补查闭环即转追问(不自行补查)、最多 3 条。"""
    assert "submit_suggestion" in AGENT2_REVIEW_PROMPT
    assert "最多 3 条" in AGENT2_REVIEW_PROMPT
    assert "需实际执行/补查才能闭环的真实缺口" in AGENT2_REVIEW_PROMPT
    assert "不要自拟成已核实审查项" in AGENT2_REVIEW_PROMPT


def test_review_prompt_role_is_review_only_gaps_route_back():
    """职责收敛:agent2 只复核 agent1 的产出与覆盖度,agent1 没查的不自行补查、转追问交回。

    回归点:实测核查计划出现"agent1 只审了后端→由 agent2 自拟前端已核实条目"这类
    越界补查;新规则要求覆盖度缺口交回 agent1(走 suggestions),agent2 不开新战场取证。
    """
    assert "你只复核 agent1 的产出与覆盖度" in AGENT2_REVIEW_PROMPT
    assert "不自行开新战场取证" in AGENT2_REVIEW_PROMPT
    # origin 收敛:来源只留 agent1 的结论/改动产出(旧的 requirement/baseline 驱动补查已移除)
    assert "user_requirement" not in AGENT2_REVIEW_PROMPT
    assert "domain_baseline" not in AGENT2_REVIEW_PROMPT
    assert "否则会漏掉 agent1 根本没碰的领域" not in AGENT2_REVIEW_PROMPT
    # 两个发射工具的 origin 枚举都收到 2 类
    plan_origin_enum = _SUBMIT_REVIEW_PLAN_TOOL["function"]["parameters"]["properties"]["items"]["items"]["properties"]["origin"]["enum"]
    item_origin_enum = _SUBMIT_REVIEW_ITEM_TOOL["function"]["parameters"]["properties"]["origin"]["enum"]
    assert set(plan_origin_enum) == {"agent1_claim", "agent1_action"}
    assert set(item_origin_enum) == {"agent1_claim", "agent1_action"}
    # submit_suggestion 描述放宽到"agent1 未覆盖"(不再限"无法自查")
    suggestion_desc = _SUBMIT_SUGGESTION_TOOL["function"]["description"]
    assert "agent1 未覆盖" in suggestion_desc
    assert "确属缺失且无法自查" not in suggestion_desc


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


def test_review_prompt_has_architecture_but_delegates_scope_broadening():
    """代码审查维度仍含「架构设计」,但不再要求 agent2 主动拓宽过窄的用户范围。

    跨维度探索性追问已从 agent2 移出(改为后台推荐器单独产出 followup_suggestions),
    agent2 的 suggestions 只报 agent1 未覆盖、需补查才能闭环的真实缺口。
    """
    assert "架构设计" in AGENT2_REVIEW_PROMPT
    assert "主动拓宽过窄的用户范围" not in AGENT2_REVIEW_PROMPT
    assert "由后台推荐器" in AGENT2_REVIEW_PROMPT


# ============================================================
# 计划项并行核查文本资产(子循环指令 + fan-in 信封)
# ============================================================


def test_review_prompt_announces_parallel_plan_verification():
    """主 prompt 补段:告知模型计划后系统会自动并行核查,不要重跑。"""
    assert "并行只读核查" in AGENT2_REVIEW_PROMPT
    assert "不必重跑" in AGENT2_REVIEW_PROMPT


def test_plan_item_instruction_shape():
    """单项指令层:身份头 + target + 只读白名单措辞;reference 项附 check_reference。"""
    txt = build_agent2_plan_item_instruction({
        "id": "P2", "target": "登录接口未鉴权", "origin": "agent1_claim",
        "planned_evidence": "source",
    })
    assert "[审查计划项:P2]" in txt
    assert "登录接口未鉴权" in txt
    assert "只使用只读取证工具" in txt
    assert "不发射任何审查结论" in txt
    assert "证据引用号" in txt
    assert "check_reference" not in txt  # source 项不提引用复核
    ref_txt = build_agent2_plan_item_instruction({
        "id": "P3", "target": "t", "planned_evidence": "reference",
    })
    assert "check_reference" in ref_txt


def test_plan_result_messages_envelope_shape():
    """fan-in 信封:首条行动指引(整批一份)+ 每项一条 system 信封;
    三种状态措辞镜射分面版;空入参空列表。"""
    msgs = build_agent2_plan_result_messages([
        ("P1", "ok", "成立,见 E1"),
        ("P2", "error", "provider 500"),
        ("P3", "timed_out", "部分结论 E4"),
    ])
    assert len(msgs) == 4
    assert all(m["role"] == "system" for m in msgs)
    assert "[系统注入|并行核查]" in msgs[0]["content"]
    assert AGENT2_PLAN_PARALLEL_GUIDANCE in msgs[0]["content"]
    assert "不要重跑已完成的核查" in msgs[0]["content"]
    assert "[系统注入|计划项结论|P1]" in msgs[1]["content"] and "成立,见 E1" in msgs[1]["content"]
    assert "核查失败" in msgs[2]["content"] and "provider 500" in msgs[2]["content"]
    assert "部分结论,已超时收尾" in msgs[3]["content"]
    assert build_agent2_plan_result_messages([]) == []


def test_plan_item_finalize_prompt_bounds_scope():
    """收尾提示:停工具、输出部分结论、没取证不臆造。"""
    assert "停止调用工具" in AGENT2_PLAN_ITEM_FINALIZE_PROMPT
    assert "证据引用号" in AGENT2_PLAN_ITEM_FINALIZE_PROMPT
    assert "不要臆造结论" in AGENT2_PLAN_ITEM_FINALIZE_PROMPT
