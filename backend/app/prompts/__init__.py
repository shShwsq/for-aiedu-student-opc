"""LLM 提示词与上下文段落资产(集中管理)。

按角色分模块存放所有送进 LLM 的文本资产:常量 system prompt、
模板、工具描述、纯格式化函数。本包为纯文本层:

- 不 import 任何 app.* 模块(零依赖,由 tests/test_prompts_purity.py 固化)
- 不含 DB / 沙箱 / LLM 调用等执行逻辑,执行逻辑留在各 agents/ 模块
- 上下文段落的"数据加载"在 services/memory_injection 等处,
  本包只负责"文本包装/格式化"

模块导览:
- executor:   执行链(内置 react_agent + CLI/acp_base 共享段落)
- agent2:     质检审查智能体(后台审查者)
- followup:   探索性追问推荐("你可能还想深入了解",后台轻调用)
- verifier:   动态验证智能体
- memory_curator: 记忆归纳/精简模板
- practice:   练习出题
"""
