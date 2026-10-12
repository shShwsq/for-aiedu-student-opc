"""探索性追问推荐("你可能还想深入了解")的纯文本提示词。

与 agent2 的「建议追问方向(缺口)」分工:
- agent2 suggestions = 经核查确认的**真实缺口**(权威,证据驱动);
- 本模块 = 后台单轮轻调用产出的**跨维度探索建议**,仅基于用户意图 +
  agent1 对话,推测性、无需取证,故与可信审查解耦。

纯文本层(零 app.* 依赖,受 tests/test_prompts_purity.py 固化)。
调用编排在服务层(app/services/followup_recommender.py)。

设计取舍:
- 只做"维度发现",不臆造具体结论/文件名/数字;
- 不重复 agent1 已充分覆盖的维度(避免与缺口组大量重叠);
- 保持用户输入的语言;
- 输出严格 JSON,便于服务层解析,失败由服务层静默兜底。
"""
from typing import Any

# 探索性追问推荐 system prompt(定位:跨维度发现,非权威)
FOLLOWUP_RECOMMEND_SYSTEM_PROMPT = """你是"探索性追问推荐"助手。给你一段用户与 AI 助手的对话,用户已就某个交付物(代码仓库、文书等)提出了审查/分析需求。

你的任务:站在用户角度,提出 0-3 条**用户尚未问及、但该交付物类型值得进一步了解**的维度方向,供用户一键追问 AI 助手。

维度方向参考(按交付物类型取其宜,不是逐条硬塞):
- 代码类:架构设计与模块边界、安全性、性能与隐性成本、可维护性/测试、能否上线交付;
- 文书/合同类:权责对等、付款与违约、知识产权、免责/霸王条款、可执行性;
- 其它交付物:按其语义自适应生成关键风险维度。

严格遵守:
- 只推荐用户**当前问题未覆盖、且对话中 AI 助手也未充分展开**的维度,别把已经答透的再列;
- 每条**具体可执行**,是交给 AI 助手的一句指令(如"再评估整体架构的分层与耦合风险",而非空泛的"看看架构");
- 不臆造任何具体结论、文件名、行号或数字——你只在推荐"该问什么",不在回答;
- 宁缺毋滥:没有值得补充的方向就返回空数组;最多 3 条;
- 保持与用户输入相同的语言(中文输入用中文)。

输出**仅**一个 JSON 对象,不要任何解释、标题或代码块围栏之外的文字:
{"suggestions": ["…", "…"]}
无可推荐时:{"suggestions": []}"""


def build_followup_messages(
    user_intent: str, conversation_text: str,
) -> list[dict[str, Any]]:
    """组装探索性追问推荐消息序列(纯文本,不做任何 IO)。

    - user_intent:用户原始意图(task.user_input);
    - conversation_text:已拼好的 user + agent1 对话文本(不含 agent2)。
    """
    user_content = (
        f"用户原始意图:\n{(user_intent or '').strip()}\n\n"
        f"AI 助手对话内容:\n{(conversation_text or '').strip()}"
    )
    return [
        {"role": "system", "content": FOLLOWUP_RECOMMEND_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
