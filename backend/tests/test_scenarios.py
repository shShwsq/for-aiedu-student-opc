"""场景模板注册表测试:三场景齐全 + 旧 id 别名兼容。

场景降级为纯快捷模板(preset_prompt + recommended_skills)。本测试锚定:
- 注册表恰好含 general / code_review / document_review 三场景
- code_security_audit 经别名解析到 code_review(resolve_scenario_id / get_scenario)
- list_scenarios 输出结构供前端消费
- 代码审核推荐 4 个 skill;文书审核推荐 review_requirements;通用无推荐 skill
- 各场景 recommended_skills 与磁盘内置 skill 名对账(防推荐名漂移后静默降级)
"""
import pytest

# 导入触发注册(register_scenario 在模块加载时执行)
from app.scenarios import code_review, document_review, general  # noqa: F401
from app.scenarios.base import (
    SCENARIO_ALIASES,
    _get_attr,
    get_scenario,
    list_scenarios,
    resolve_scenario_id,
)


def test_three_scenarios_registered():
    """注册表恰好三场景:通用 / 代码审核 / 文书审核。"""
    ids = {s["id"] for s in list_scenarios()}
    assert ids == {"general", "code_review", "document_review"}


def test_old_security_audit_id_resolves_to_code_review():
    """旧 id code_security_audit 经别名解析到 code_review(老任务/skill 仍可达)。"""
    assert resolve_scenario_id("code_security_audit") == "code_review"
    assert SCENARIO_ALIASES["code_security_audit"] == "code_review"
    # get_scenario 对旧 id 同样命中,不抛未知场景
    assert get_scenario("code_security_audit").id == "code_review"


def test_resolve_unknown_id_passthrough():
    """无别名的 id 原样返回。"""
    assert resolve_scenario_id("general") == "general"


def test_unknown_scenario_raises():
    with pytest.raises(ValueError):
        get_scenario("not_a_scenario")


def test_code_review_recommended_skills_merged():
    """代码审核:合并安全审计后共 4 个 skill。"""
    s = get_scenario("code_review")
    assert s.name == "代码审核"
    assert set(s.recommended_skills) == {
        "check_hardcoded_secrets", "check_ssrf",
        "review_error_handling", "review_concurrency",
    }


def test_document_review_recommended_skills():
    """文书审核:推荐 review_requirements(需求文档审查),并有预设提示词。"""
    s = get_scenario("document_review")
    assert s.name == "文书审核"
    assert s.recommended_skills == ["review_requirements"]
    assert s.preset_prompt


def test_general_scenario_empty_preset():
    """通用场景:不预填提示词、无推荐 skill、无维度模板。"""
    s = get_scenario("general")
    assert s.preset_prompt == ""
    assert s.recommended_skills == []
    assert _get_attr(s, "prompt_templates", []) == []


def test_code_review_prompt_templates_have_architecture():
    """代码审核:提供代码质量/安全性/架构设计三个维度模板(老师反馈的架构维度落地)。"""
    s = get_scenario("code_review")
    tpl = s.prompt_templates
    assert {t["id"] for t in tpl} == {"code_quality", "code_security", "code_architecture"}
    assert "架构设计" in {t["label"] for t in tpl}
    for t in tpl:
        assert {"id", "label", "prompt"} <= set(t) and t["prompt"].strip()
    # 默认预设提示词也把架构纳入
    assert "架构" in s.preset_prompt


def test_document_review_prompt_templates():
    """文书审核:提供合同/商业计划书/需求文档三个模板。"""
    s = get_scenario("document_review")
    assert {t["id"] for t in s.prompt_templates} == {
        "doc_contract", "doc_business_plan", "doc_requirements",
    }


def test_list_scenarios_shape():
    """list_scenarios 每项含前端消费所需的固定字段(含 prompt_templates)。"""
    for item in list_scenarios():
        assert set(item) == {
            "id", "name", "description", "preset_prompt", "recommended_skills",
            "prompt_templates",
        }
        assert isinstance(item["prompt_templates"], list)


def test_recommended_skills_anchor_to_disk():
    """对账:各场景 recommended_skills 必须命中内置 skills 根目录里真实存在的 skill 名。

    推荐名一旦与磁盘 SKILL.md 的 frontmatter name 漂移,前端 applyRecommendedSkills
    会勾选出空集并提交 allowed_skills=[],后端按"全部可用"静默降级,链路无任何
    报错——只能靠本测试锚定。
    """
    from app.skills.loader import DEFAULT_SKILLS_ROOT, discover_skills

    registry = discover_skills(DEFAULT_SKILLS_ROOT)
    on_disk = {
        skill.name
        for bucket in registry.skills.values()
        for skill in bucket.values()
    }
    assert on_disk, "内置 skills 根目录为空,对账失去意义"
    for decl in list_scenarios():
        rec = set(decl["recommended_skills"])
        assert rec <= on_disk, (
            f"场景 {decl['id']} 推荐了磁盘上不存在的 skill: {sorted(rec - on_disk)}"
        )
