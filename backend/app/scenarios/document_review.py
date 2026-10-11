"""文书审核场景模板(降级后:仅提供预设提示词 + 推荐 skill)

覆盖合同/协议类文书的审查:权责对等、付款与违约、知识产权归属、霸王条款。
docx/pdf 不做正文解析:既不在线预览也不能被智能体阅读(read_file 会拦成占位文案),
只能在文件面板下载原件,因此 description 引导用户优先上传 txt/md。
无推荐 skill(前端语义为全部可用)。
"""
from app.scenarios.base import register_scenario


class DocumentReviewTemplate:
    """文书审核场景模板(合同/商业计划书/需求文档等纯文本类文书)"""

    id = "document_review"
    name = "文书审核"
    description = (
        "审核合同/商业计划书/需求文档等文书(请上传 txt/md 纯文本;"
        "docx/pdf 无法在线审阅,只能下载原件自行查看)"
    )

    @property
    def preset_prompt(self) -> str:
        return (
            "请审核这份合同/协议文书,关注以下维度:"
            "权责对等(单方面义务、责任不对等)、"
            "付款与违约(付款条件模糊、违约金畸高或缺失)、"
            "知识产权归属(成果归属、署名权、竞业限制)、"
            "常见霸王条款(任意解除权、单方变更、免责条款),"
            "逐条定位到具体条款,说明风险后果并给出修改建议。"
        )

    @property
    def prompt_templates(self) -> list[dict[str, str]]:
        # 场景内文书类型模板:选中文书审核后,用户可点一下预填到输入框(仍可自由编辑)
        return [
            {
                "id": "doc_contract",
                "label": "合同审核",
                "prompt": (
                    "请审核这份合同/协议文书:权责对等(单方面义务、责任不对等)、"
                    "付款与违约(付款条件模糊、违约金畸高或缺失)、知识产权归属、"
                    "常见霸王条款(任意解除权、单方变更、免责条款)。"
                    "逐条定位到具体条款,说明风险后果并给出修改建议。"
                ),
            },
            {
                "id": "doc_business_plan",
                "label": "商业计划书",
                "prompt": (
                    "请审核这份商业计划书:关键假设与数据来源是否可信、市场与竞品论证是否成立、"
                    "财务模型与单位经济(成本/毛利/现金流)是否自洽、风险与依赖是否充分披露、"
                    "里程碑与承诺是否可兑现。逐条指出可疑处并说明需补什么样的证据。"
                ),
            },
            {
                "id": "doc_requirements",
                "label": "需求文档",
                "prompt": (
                    "请审核这份需求文档:需求是否完整无遗漏、描述是否一致(无矛盾/歧义)、"
                    "是否可测试可验收、边界条件与前置假设是否明确、优先级是否清晰。"
                    "逐条列出缺失或含糊之处并给出补充建议。"
                ),
            },
        ]

    @property
    def recommended_skills(self) -> list[str]:
        return []  # 无推荐 → 前端全选(等同于不限制)


register_scenario(DocumentReviewTemplate())
