"""AI 润色:把用户随手写的任务描述改写为更清晰、结构化的审查/分析请求。

纯文本层(零 app.* 依赖,受 tests/test_prompts_purity.py 固化)。
调用编排在路由层(app/routers/tasks.py 的 /tasks/polish_prompt)。

设计取舍(与"证据驱动可信审查"一致,克制不越权):
- 只**润色表达**,不改用户的原意与范围:不新增用户未提出的维度、不臆造具体结论;
- 保持用户输入的语言(中文输入 → 中文输出);
- 只返回润色后的纯文本,不带解释或前后缀,便于直接回填输入框。
"""
from typing import Any

# 润色 system prompt(约束模型只做表达优化,不做内容扩张)
POLISH_SYSTEM_PROMPT = """你是任务描述的润色助手。用户会给你一段写给「AI 审查/分析助手」的任务说明,可能是口语化、零散或含糊的。

你的职责:把它改写得更清晰、结构化、可直接执行,同时严格遵守:
- 保持用户的原意、范围与语言(中文输入用中文输出),不改变任务类型;
- 不新增用户未提出的审查维度或要求,不臆造任何具体结论、文件名或数字;
- 只做表达层面的整理:补全省略的主语/动作、理清要点、消除歧义、去除口头语;
- 若原文已足够清晰,只做最轻微的顺句,不要大幅改写或堆砌;
- 输出**仅**润色后的正文本身,不要任何解释、标题、引号或前后缀。"""


def build_polish_messages(text: str, scenario_name: str | None = None) -> list[dict[str, Any]]:
    """组装润色消息序列(纯文本,不做任何 IO)。

    scenario_name 仅作为轻量背景提示(如"代码审核"),帮助润色贴合场景用语,
    不据此扩展用户未提出的维度。
    """
    user_content = (text or "").strip()
    if scenario_name:
        user_content = f"[当前场景:{scenario_name}]\n{user_content}"
    return [
        {"role": "system", "content": POLISH_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
