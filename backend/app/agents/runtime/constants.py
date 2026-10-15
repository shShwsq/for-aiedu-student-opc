"""跨智能体共享常量(单一事实源)

此前 react_agent 与 agent2 各自定义 MAX_HISTORY_MSG_CHARS /
MAX_HISTORY_TOTAL_CHARS,靠注释约定"与对方保持一致"——两处手工同步
容易漂移。提取到 runtime 后由导入共享,改一处即全局生效。
"""

from app.config import settings

# 跨轮记忆传递:单条历史消息(用户原话 / 执行总结 / 评审反馈 / 审查记录)
# 的最大字符数,超出截断。agent1 与 agent2 的历史注入共用同一上限
MAX_HISTORY_MSG_CHARS = 3000

# agent2 跨轮自记忆(agent2 自己之前各轮审查记录)的总字符上限。
# 注:agent1 侧历史压缩按 token 预算(MAX_HISTORY_TOKEN_BUDGET,
# 见 react_agent 模块内),策略不同,不在此列
MAX_HISTORY_TOTAL_CHARS = 12000

# agent1 → agent2 报告回灌:每轮摘录字符数(仅作概览,全文经
# read_agent1_report 工具按 round_idx 分页读取)。与 MAX_HISTORY_MSG_CHARS
# 分开:后者服务 agent1 自身历史压缩,调大会放大所有历史注入;本常量只
# 影响 agent2 prompt 里 agent1 总结的呈现形状(摘录 + 截断标注 + 指针)。
# 超出摘录上限时仍强制追加「已截断 + 全文指针」标注(见 agent2 rounds_text),
# 故调大只是减少工具往返、不会倒退成静默截断。走 env 便于按模型/部署调。
AGENT1_REPORT_EXCERPT_CHARS = int(getattr(settings, "AGENT2_REPORT_EXCERPT_CHARS", 4000))

# agent1 总结内联回灌的跨轮总字符上限(滑动窗口:超此值从最早轮丢弃,
# 被丢轮仍可经 read_agent1_report 按轮号读回)。与 MAX_HISTORY_TOTAL_CHARS
# 解耦:后者还管 agent2 自己的跨轮记忆注入,调大会连带放大那边。
# 摘录调大后总窗口需相应放宽,否则多轮场景早轮会被很快挤出内联。
AGENT1_REPORT_WINDOW_CHARS = int(
    getattr(settings, "AGENT2_REPORT_WINDOW_TOTAL_CHARS", 24000)
)

# read_agent1_report 单页返回字符数。分页长度由此常量自控,交付路径已
# 绕过 agent2._bound_for_llm(见 read_agent1_report 分发),故不再受
# _MAX_TOOL_RESULT_CHARS=3000 天花板约束。走 env(AGENT2_REPORT_PAGE_CHARS)
# 便于按部署/模型调大,减少分页轮次。
AGENT1_REPORT_PAGE_CHARS = int(getattr(settings, "AGENT2_REPORT_PAGE_CHARS", 6000))
