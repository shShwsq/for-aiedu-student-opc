"""场景模板(降级后:仅作快捷模板,不再硬编码 prompt/工具白名单)

场景降级后的职责:
- 提供预设提示词(preset_prompt):用户选场景后预填到输入框
- 推荐技能列表(recommended_skills):创建任务时默认勾选的 skill

不再承担:
- checklist(已随覆盖度清单功能移除,agent2 自行确定审查维度)
- agent2_prompt / react_agent_prompt(改为通用 prompt)
- enabled_tools(改为全部开放)
- extract_results / format_result(改为通用提取)
- form_fields / result_grouping / result_meta_fields / coverage(改为通用化/动态化)
"""
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ScenarioTemplate(Protocol):
    """场景模板协议:降级后的精简接口"""

    id: str
    name: str
    description: str

    @property
    def preset_prompt(self) -> str:
        """预设提示词:用户选此场景后预填到输入框

        示例(安全审计):"请审计这个仓库的安全漏洞,关注注入类、认证授权、
        反序列化、SSRF、配置泄露等类别,给出具体文件位置和修复建议。"
        """
        ...

    @property
    def recommended_skills(self) -> list[str]:
        """推荐的 skill 名称列表:创建任务时默认勾选

        空列表表示不特别推荐(用户自行选择)。skill 名称对应 skills/ 目录下的子目录名。
        """
        ...

    @property
    def prompt_templates(self) -> list[dict[str, str]]:
        """场景内可选的维度提示词模板(可选,默认无)

        一个场景仍可保留单一 preset_prompt 作默认,但额外提供若干「维度模板」
        供用户在创建页一点即预填输入框(如代码分代码质量/安全性/架构设计,
        文书分合同/商业计划书/需求文档)。每项为 {id, label, prompt}。
        模板只是快捷预填文案,不改变场景/智能体行为(场景保持降级)。
        """
        ...


# 场景别名:旧场景 id → 现行场景 id。
# 场景合并/更名后,老任务记录里仍存旧 id(agent_policy 场景匹配、
# 用户上传到旧场景目录的 skill 等),统一经 resolve_scenario_id 转换。
SCENARIO_ALIASES: dict[str, str] = {
    "code_security_audit": "code_review",  # 代码安全审计已并入代码审核
}


def resolve_scenario_id(scenario_id: str) -> str:
    """把旧场景 id 解析为现行 id(无别名时原样返回)"""
    return SCENARIO_ALIASES.get(scenario_id, scenario_id)


# 场景模板注册表
SCENARIOS: dict[str, "ScenarioTemplate"] = {}


def register_scenario(scenario: "ScenarioTemplate") -> None:
    """注册场景模板"""
    SCENARIOS[scenario.id] = scenario


def get_scenario(scenario_id: str) -> "ScenarioTemplate":
    """获取场景模板,不存在则报错(旧 id 经别名转换)"""
    scenario_id = resolve_scenario_id(scenario_id)
    if scenario_id not in SCENARIOS:
        raise ValueError(
            f"未知场景: {scenario_id},已注册: {list(SCENARIOS.keys())}"
        )
    return SCENARIOS[scenario_id]


def list_scenarios() -> list[dict[str, Any]]:
    """列出所有场景模板的精简声明(给前端展示用)

    降级后只返回 id/name/description/preset_prompt/recommended_skills。
    前端用 preset_prompt 预填输入框,用 recommended_skills 默认勾选 skill。
    """
    result: list[dict[str, Any]] = []
    for s in SCENARIOS.values():
        result.append({
            "id": s.id,
            "name": s.name,
            "description": _get_attr(s, "description", ""),
            "preset_prompt": _get_attr(s, "preset_prompt", ""),
            "recommended_skills": _get_attr(s, "recommended_skills", []),
            "prompt_templates": _get_attr(s, "prompt_templates", []),
        })
    return result


def _get_attr(scenario: "ScenarioTemplate", name: str, default: Any) -> Any:
    """安全获取场景的可选属性,缺失时返回默认值"""
    try:
        val = getattr(scenario, name)
        return val if val is not None else default
    except (AttributeError, NotImplementedError):
        return default
