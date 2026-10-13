"""app/prompts 纯文本资产包的零依赖约束测试。

app/prompts 定位为纯文本层(常量/模板/纯格式化函数),不得 import 任何
app.* 模块:数据加载在 memory_injection 等服务层,消息装配在各执行器。
本测试用 ast 静态扫描固化该约束,防止后续往 prompts 里塞执行逻辑
形成 import 环或隐式耦合。
"""
import ast
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "app" / "prompts"


def _collect_app_imports(path: Path) -> list[str]:
    """收集单个 py 文件里的 app.* import(含相对 import)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    offenders.append(f"{path.name}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                offenders.append(f"{path.name}: 相对 import(from {'.' * node.level}{node.module or ''})")
            elif (node.module or "").startswith("app.") or node.module == "app":
                offenders.append(f"{path.name}: from {node.module} import ...")
    return offenders


def test_prompts_package_has_no_app_imports():
    """app/prompts 下所有 .py 文件零 app.* import(纯文本层硬约束)。"""
    files = sorted(PROMPTS_DIR.glob("*.py"))
    assert files, "app/prompts 包应存在且至少含 __init__.py"
    offenders: list[str] = []
    for f in files:
        offenders.extend(_collect_app_imports(f))
    assert offenders == [], (
        "app/prompts 必须零 app.* 依赖(数据加载放服务层,装配放执行器): "
        + "; ".join(offenders)
    )


def test_prompts_modules_importable_and_nonempty():
    """各 prompt 模块可导入且非空(防搬家后出现空壳/拼写错误)。"""
    from app.prompts import (  # noqa: F401
        agent2, executor, followup, memory_curator, polish, practice, verifier,
    )

    assert agent2.AGENT2_REVIEW_PROMPT.strip()
    assert executor.REACT_AGENT_SYSTEM_PROMPT.strip()
    assert followup.FOLLOWUP_RECOMMEND_SYSTEM_PROMPT.strip()
    assert followup.build_followup_messages("只看代码质量", "助手回答…")[0]["content"].strip()
    assert memory_curator._SUMMARIZE_PROMPT.strip()
    assert practice.build_system_prompt("security", False).strip()
    assert verifier.VERIFIER_SYSTEM_PROMPT.strip()
    assert polish.POLISH_SYSTEM_PROMPT.strip()
    assert polish.build_polish_messages("你好")[0]["content"].strip()


def test_build_subtask_instruction_is_pure_and_tool_neutral():
    """子任务指令层纯函数:零 app.* 依赖(由上方 AST 扫描守门),措辞工具中立。

    技能正文不再预注入 —— 指令层只含主 agent 写的 instruction(取代旧 body/focus),
    引导子任务按需用 skill 工具读取。
    """
    from app.prompts import executor

    text = executor.build_subtask_instruction("鉴权核查", "重点核对鉴权与注入")
    assert "子任务" in text
    assert "鉴权核查" in text
    assert "重点核对鉴权与注入" in text
    # 只读约束明示,且不出现改动工具
    assert "只读工具" in text
    for mutating in ("write_file", "run_command", "clone_repo"):
        assert mutating not in text
    # 技能正文改为按需自取:指令里引导用 skill 工具按需读取(不写死 body)
    assert "skill" in text
    # 空入参不抛(健壮性)
    assert executor.build_subtask_instruction("", "").strip()


def test_build_subtask_instruction_clips_long_instruction():
    """instruction 超长按 SUBTASK_INSTRUCTION_MAX_CHARS 截断,不把子循环上下文撑胖。"""
    from app.prompts import executor

    long_instruction = "点" * (executor.SUBTASK_INSTRUCTION_MAX_CHARS + 500)
    clipped = executor.build_subtask_instruction("f", long_instruction)
    assert len(clipped) < len(long_instruction)


def test_dispatch_subtasks_tool_description_is_disciplined():
    """派发工具描述模板:含纪律文案与 {skill_names} 占位符,纯函数零依赖。"""
    from app.prompts import executor

    tmpl = executor.DISPATCH_SUBTASKS_TOOL_DESCRIPTION_TEMPLATE
    assert "{skill_names}" in tmpl
    # 纪律核心措辞(与 system prompt 派发段同一口径)
    assert "按需派发" in tmpl
    assert "不适合" in tmpl
    # 填充技能名后不残留占位符
    filled = tmpl.format(skill_names="check_ssrf, review_concurrency")
    assert "{skill_names}" not in filled
    assert "check_ssrf" in filled


def test_agent2_subagent_dispatch_guidance_present_and_drift_free():
    """agent2 审查子智能体按需派发的文本资产:纪律存在、已并入 system prompt,
    且不含已删除配置名与"系统会自动"旧措辞(防提示词与实现漂移)。"""
    from app.prompts import agent2 as a2

    # 纪律正文存在,并共享进 system prompt(单一来源)
    assert a2.AGENT2_SUBAGENT_DISPATCH_GUIDANCE.strip()
    assert "dispatch_review_subagents" in a2.AGENT2_SUBAGENT_DISPATCH_GUIDANCE
    assert a2.AGENT2_SUBAGENT_DISPATCH_GUIDANCE in a2.AGENT2_REVIEW_PROMPT
    # 工具描述与定义
    assert a2.DISPATCH_SUBAGENTS_TOOL_DESCRIPTION.strip()
    assert a2._DISPATCH_SUBAGENTS_TOOL["function"]["name"] == "dispatch_review_subagents"
    # 漂移护栏:不含已删除的自动扇出配置名 / 旧"系统会自动"措辞
    assert "AGENT2_PARALLEL_ENABLED" not in a2.AGENT2_REVIEW_PROMPT
    assert "系统会自动" not in a2.AGENT2_REVIEW_PROMPT
    assert "系统会自动" not in a2.AGENT2_PLAN_PARALLEL_GUIDANCE
