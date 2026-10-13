"""代码审核场景模板(降级后:仅提供预设提示词 + 推荐 skill)

不再定义 checklist/prompt/工具白名单/结果 schema。
(原「代码安全审计 code_security_audit」已并入本场景,旧 id 经别名兼容)
"""
from app.scenarios.base import register_scenario


class CodeReviewTemplate:
    """代码审核场景模板(安全 + 隐性成本 + 代码质量/可维护性 + 架构设计 + 上线可行性)"""

    id = "code_review"
    name = "代码审核"
    description = "审查代码交付物:安全漏洞、代码质量、架构设计、隐性成本、能否上线"

    @property
    def preset_prompt(self) -> str:
        return (
            "请审查本仓库的代码交付物，评估其是否达到上线标准。重点关注："
            "安全漏洞(注入类、硬编码凭证、认证授权、SSRF、配置泄露)、"
            "架构设计(分层与模块边界、耦合与内聚、设计模式与技术选型权衡、扩展性与可测试性)、"
            "代码质量与可维护性(可读性、边界条件、异常处理、并发问题)、"
            "隐性成本(失控 API 调用、死循环烧钱逻辑、明显资源泄漏)、"
            "以及阻塞发布的关键缺陷，"
            "给出具体文件位置、问题类型和修复建议。"
        )

    @property
    def prompt_templates(self) -> list[dict[str, str]]:
        # 场景内维度模板:选中代码审核后,用户可点一下预填到输入框(仍可自由编辑)
        return [
            {
                "id": "code_quality",
                "label": "代码质量",
                "prompt": (
                    "请审查本仓库代码的质量与可维护性:可读性与命名、重复与过度复杂、"
                    "边界条件与空值处理、异常处理与错误传播、并发安全、测试覆盖与用例质量。"
                    "逐条给出具体文件位置、问题类型与改进建议。"
                ),
            },
            {
                "id": "code_security",
                "label": "安全性",
                "prompt": (
                    "请审查本仓库代码的安全漏洞:注入类(SQL/命令/模板注入)、硬编码凭证与密钥泄露、"
                    "认证与会权越权、SSRF、不安全反序列化、配置与依赖风险、敏感信息日志输出。"
                    "逐条给出具体文件位置、可利用性与修复建议。"
                ),
            },
            {
                "id": "code_architecture",
                "label": "架构设计",
                "prompt": (
                    "请审查本仓库的架构设计:分层与模块边界是否清晰、耦合与内聚、循环依赖、"
                    "设计模式的运用与误用、技术选型权衡、扩展性与可测试性、单点与跨服务边界风险。"
                    "指出结构性隐患并给出重构方向建议。"
                ),
            },
        ]

    @property
    def recommended_skills(self) -> list[str]:
        # 对应 skills/code_review/ 目录下实际存在的 skill name(来自 SKILL.md frontmatter)
        return [
            "check_hardcoded_secrets",
            "check_ssrf",
            "review_error_handling",
            "review_concurrency",
        ]


register_scenario(CodeReviewTemplate())
