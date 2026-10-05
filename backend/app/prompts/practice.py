"""练习出题的提示词资产。

按学习主题切换出题视角(head 段)+ 共享的工具说明段与通用规则段,
外加主题分类器 system(按用户启用主题词表动态构建)、单条发现模板与质量反馈文案。
执行逻辑(工具循环、LLM 调用、解析落库)在 app/services/practice/generator.py。

本模块保持零 app.* 依赖:主题 key 使用与
app/models/practice.py LEARNING_TOPIC_* 一致的字符串字面量
("security" / "architecture" / "coding" / "contract",DB 存储的稳定枚举值)
+ 用户自定义主题的 "custom_*" 键,全部经参数传入(纯数据)。
"""
import json
from typing import Any

# ============================================================
# 提示词:按学习主题切换出题视角(通用规则段共享)
# ============================================================

_SECURITY_TOPIC_HEAD = """你是一名网络安全培训出题专家。基于给定的真实代码审计发现,改编出用于安全培训的客观题。

## 出题视角
- 漏洞识别:该代码片段存在哪种漏洞
- 成因判断:漏洞根因与触发条件
- 修复选择:正确的修复方式与安全编码实践
- knowledge_key 优先用 CWE 编号(如 "CWE-89");无对应 CWE 时用英文短标识(如 "hardcoded_secrets")"""

_ARCHITECTURE_TOPIC_HEAD = """你是一名软件架构培训出题专家。基于给定的真实代码分析发现,从架构设计视角改编出客观题。

## 出题视角
- 模块边界与职责划分、分层是否合理、依赖方向
- 设计模式的应用与误用、技术选型权衡(一致性/性能/可维护性)
- 耦合与内聚、扩展性、可测试性缺陷及其改进方案
- 即使发现本身是安全/质量问题,也应从架构成因或设计改进角度出题
- knowledge_key 用英文短标识(如 "layering"、"circular_dependency"、"observer_pattern")"""

_CODING_TOPIC_HEAD = """你是一名通用编码能力培训出题专家。基于给定的真实代码分析发现,改编出考察通用编码能力的客观题。

## 出题视角
- bug 识别与边界条件、异常与错误处理
- 代码坏味道与可读性、性能问题(如 N+1 查询、不必要的重复计算)
- 语言特性正确用法与工程最佳实践(测试、命名、API 设计)
- knowledge_key 用英文短标识(如 "null_safety"、"exception_handling"、"n_plus_one_query")"""

_CONTRACT_TOPIC_HEAD = """你是一名合同文书培训出题专家。基于给定的真实文书审查发现(合同/协议类条款),改编出考察条款判断力的客观题。

## 出题视角
- 条款不利识别:以下哪一句条款对你不利、哪条最值得警惕
- 权责对等判断:单方面义务、责任与权利是否失衡
- 付款与违约:付款条件模糊、违约金畸高或缺失的后果
- 知识产权归属:成果归属、署名权、竞业限制的影响
- 霸王条款识别:任意解除权、单方变更权、过度免责
- knowledge_key 用英文短标识(如 "payment_terms"、"ip_ownership"、"unfair_clause")

## 材料规则(文书题特有)
- code_snippet 字段放**条款原文**(不是代码),截取与发现相关的完整句子
- languages 固定给空数组 []
- source_file 为文书文件路径(工作区内相对路径),source_lines 不适用时给 null"""

# 自定义主题通用出题视角模板(视角由用户描述驱动,生成质量取决于描述具体程度)
_CUSTOM_TOPIC_HEAD_TEMPLATE = """你是一名「{name}」培训出题专家。基于给定的真实发现,从「{name}」的专业视角改编出客观题。

## 出题视角
{description}

- 出题必须落到材料的具体细节,考察需要「{name}」专业知识才能判断的问题
- knowledge_key 用英文短标识(如该领域常见概念的英文缩写或短语)"""


# key 与 app/models/practice.py 的 LEARNING_TOPIC_* 枚举值一致
_TOPIC_PROMPT_HEADS = {
    "security": _SECURITY_TOPIC_HEAD,
    "architecture": _ARCHITECTURE_TOPIC_HEAD,
    "coding": _CODING_TOPIC_HEAD,
    "contract": _CONTRACT_TOPIC_HEAD,
}


def build_custom_topic_head(topic: dict) -> str:
    """渲染自定义主题的出题视角 head(topic 为 {key,name,description})"""
    return _CUSTOM_TOPIC_HEAD_TEMPLATE.format(
        name=str(topic.get("name") or "自定义主题").strip() or "自定义主题",
        description=str(topic.get("description") or "").strip() or "考察该主题的核心概念、常见陷阱与最佳实践",
    )


def resolve_topic_head(topic: str, custom_topics: list[dict] | None = None) -> str:
    """取某学习主题的专业视角 head(内置用专有段,自定义用通用模板)

    出题 system prompt 与知识点讲解 system prompt 共用本函数:
    保证同一个主题的「出题口径」与「讲解口径」一致。
    无匹配回落 security(防御)。
    """
    head = _TOPIC_PROMPT_HEADS.get(topic)
    if head is None and custom_topics:
        for d in custom_topics:
            if d.get("key") == topic:
                return build_custom_topic_head(d)
    return head or _TOPIC_PROMPT_HEADS["security"]


# 工作区可用时注入的工具说明段(工作区存活且未预读材料时用:强制先调工具)
_TOOL_SECTION = """## 材料查阅工具(工作区已就绪,必须使用)
出题前必须先调工具查阅真实材料(源码或文书原文),禁止跳过直接出题:
- read_file(file_path, max_lines?, offset?):读工作区文件(带行号,分页)
- search_code(pattern, file_glob?, output_mode?):正则搜索文件内容;
  output_mode="files_with_matches" 可快速定位含关键词的文件
- find_files(pattern):按 glob 递归查文件路径(如 **/*.py 或 **/*.txt)
要求:
1. 每道题出题前至少 read_file 一次相关文件,确认材料真实存在
2. 真实材料题(origin=repo):题干与 code_snippet 必须引用读到的真实内容,
   不得虚构,并给出 source_file(工作区内相对路径)与
   source_lines(行区间如 "120-150" 或单行号 "42",取自 read_file 结果)
3. 改编题(origin=synthetic):先读原文件确认问题形态,再原创虚构材料,
   不给 source_file/source_lines
确实在工作区中找不到相关文件时,才退回基于发现描述出题(此时不给 source_file)。"""

# 材料已由服务端预读并附在发现后面时用(仍允许补读,但不再强制每题一次工具往返)
_TOOL_SECTION_WITH_MATERIAL = """## 材料查阅工具(相关材料已预先读好附在下面)
发现后面已给出按源码定位预先读出的真实材料片段(带行号),优先直接依据它出题;
只有当片段不足以支撑考察点(需要看上下游/别的文件)时,才调用工具补读:
- read_file(file_path, max_lines?, offset?) / search_code(...) / find_files(...)
要求:
1. 真实材料题(origin=repo):题干与 code_snippet 必须引用预读片段或补读的
   真实内容,不得虚构,并给出 source_file(工作区内相对路径)与
   source_lines(行区间如 "120-150" 或单行号 "42",取自片段行号)
2. 改编题(origin=synthetic):可先按需补读确认问题形态,再原创虚构材料,
   不给 source_file/source_lines
3. 片段已足够时不要为了调工具而调工具(每次工具往返都会拖慢出题)
片段为空或确实找不到相关材料时,才退回基于发现描述出题(此时不给 source_file)。"""

# 服务端预读材料段(接在单条发现模板之后)
_MATERIAL_SECTION_TEMPLATE = """

【已为你读好的真实材料】(按本发现的源码定位预先读取,带行号;path=工作区相对路径)
{materials}
"""

# 各主题共享的输出规则段(代码/文书材料双轨)
_COMMON_RULES = """## 通用要求
1. 出 1~3 道题,题型限定:
   - single_choice(单选):如「该材料存在哪种问题」「正确的处理方式是」
   - true_false(判断):选项固定为 ["正确", "错误"],题干为对材料中某一
     专业结论或技术事实的真伪判断
2. 题目必须阅读材料才能作答:题干要落到具体材料细节(代码:函数名/调用关系/
   变量/分支逻辑;文书:具体条款原文的措辞与限定),禁止不看材料也能答对的
   通用概念题、常识题
3. 禁止答案泄露的出题套路(违者视为不合格):
   - 禁止「某同学/某人/某工程师做了某判断,该判断是否正确」式叙述题:
     题干必须直接呈现材料内容,让答题人对专业问题本身作判断,
     不引入虚构人物的行为故事
   - 禁止措辞即答案:题干出现「仅凭」「就想当然」「便断定」等字眼时,
     不看材料也能猜出答案,此类题一律不合格
4. 考察点必须有专业深度:出题内容应是漏洞模式、设计缺陷、条款风险、
   语言陷阱等需要专业知识才能判断的问题;文件扩展名、命名习惯、
   通用软件操作等日常常识不得作为考察点
5. 材料片段单独放 code_snippet,必须自包含:代码题含必要的函数签名、导入与
   上下文,文书题放相关条款的完整原句,脱离原文也能读懂;题干不得依赖
   特有路径、内部命名才可作答
6. 出题形式由你自主决定,鼓励真实材料题与改编题混合:
   - origin="repo"(真实材料题):基于发现中的真实材料出题
   - origin="synthetic"(改编题):原创一段含同类问题的完整虚构材料,
     业务场景与命名与原项目完全不同,题干不得提及原项目;考察用户把知识
     泛化应用到新材料的能力
7. 干扰项要有迷惑性但明确错误,正确答案唯一
8. explanation 讲清原理与改进要点,100 字以内
9. difficulty 评估难度(1-5 整数):1=概念识别,3=需理解材料逻辑,5=需深入细节
10. knowledge_name:知识点中文展示名(如 "SQL 注入"、"付款条件模糊")
11. languages:该题涉及的编程语言,小写短名数组(如 ["python", "sql"]);
   文书类题目固定给空数组 []
12. 以下情况不要出题,直接返回空数组 []:元信息标注 verified: false、
   判定为误报、或发现内容单薄到不足以支撑第 4 条专业深度(宁缺毋滥,
   不要为凑数降格出常识题)

只输出 JSON 数组,不要任何其他文字。每个元素结构:
{"qtype": "single_choice|true_false", "origin": "repo|synthetic",
 "stem": "...", "code_snippet": "...",
 "source_file": "工作区内相对路径"或null, "source_lines": "120-150"或null,
 "options": ["...", "..."], "answer_idx": 0, "explanation": "...",
 "difficulty": 3, "knowledge_key": "...", "knowledge_name": "...",
 "languages": ["python", "sql"]}"""


def build_system_prompt(
    topic: str, workspace_available: bool,
    custom_topics: list[dict] | None = None,
    material_prefetched: bool = False,
) -> str:
    """按学习主题拼出题 system prompt;工作区可用时附工具说明段

    内置主题用专有视角 head;自定义主题(custom_topics 传入 {key,name,
    description} 列表)用通用模板 + 用户描述渲染;无匹配回落 security(防御)。

    material_prefetched=True:相关源码片段已由服务端预读并附在发现后面,
    工具段换成「按需补读」措辞(保留工具能力,但不再要求每题必须先调一次,
    把每条发现的 LLM 往返从 2~4 次压到 1 次)。
    """
    sections = [resolve_topic_head(topic, custom_topics)]
    if workspace_available:
        sections.append(
            _TOOL_SECTION_WITH_MATERIAL if material_prefetched else _TOOL_SECTION
        )
    sections.append(_COMMON_RULES)
    return "\n\n".join(sections)


# ============================================================
# 主题自动匹配:规则先行 + LLM 批量兜底(主题词表来自用户设置)
# ============================================================

# 内置主题的分类描述(与 _TOPIC_CLASSIFY_SYSTEM 历史文案一致)
_BUILTIN_CLASSIFY_LINES = (
    "- security:安全漏洞(注入、硬编码凭证、越权、SSRF、配置泄露等)",
    "- architecture:架构设计问题(分层、耦合、模块边界、设计模式、技术选型)",
    "- coding:通用代码质量问题(bug、边界条件、异常处理、性能、可读性、测试)",
    "- contract:合同文书问题(条款不利、权责失衡、付款违约、知识产权、霸王条款)",
)


def build_topic_classify_prompt(topics: list[dict]) -> str:
    """按用户启用主题词表构建分类器 system prompt

    topics 为 [{key,name,description}](仅启用主题):内置四类保留专有描述,
    自定义主题以「- {key}:{name}——{description}」列出;输出必须命中词表 key。
    """
    builtin_keys = set(_TOPIC_PROMPT_HEADS)
    lines: list[str] = []
    for d in topics:
        key = str(d.get("key") or "").strip()
        if not key:
            continue
        if key in builtin_keys:
            # 内置主题:保留专有分类描述(与历史文案一致)
            for line in _BUILTIN_CLASSIFY_LINES:
                if line.startswith(f"- {key}:"):
                    lines.append(line)
                    break
        else:
            name = str(d.get("name") or key).strip()
            desc = str(d.get("description") or "").strip()
            entry = f"- {key}:{name}"
            if desc:
                entry += f"——{desc}"
            lines.append(entry)
    return (
        "你是审查发现的主题分类器。对给定的每条发现,判断它最适合改编成哪种主题的练习题:\n"
        + "\n".join(lines)
        + "\n\ntopic 必须是上述列表中给定的 key 之一。\n"
        + "只输出 JSON 数组,不要任何其他文字。每个元素:\n"
        + '{"id": <发现序号,原样返回>, "topic": "<列表中的 key>", "reason": "一句话理由"}'
    )


# ============================================================
# 出题工具循环(只读三工具;repo_path/task_id 由宿主注入,LLM 不感知)
# ============================================================

_PRACTICE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内文件内容(带行号,支持 offset 翻页)。发现描述缺少具体代码时,先读相关源文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "仓库内相对路径"},
                    "max_lines": {"type": "integer", "description": "本次最多返回行数,默认 100"},
                    "offset": {"type": "integer", "description": "从第几行开始读(1-based),默认 1"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "正则搜索仓库代码,定位相关函数、字符串或调用点",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件名过滤 glob(可选),如 *.py"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches"],
                        "description": "content=匹配行+行号;files_with_matches=仅文件路径(快速定位)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按 glob 模式递归查找文件路径(不看内容),如 **/*.py",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ============================================================
# 单条发现的出题模板与质量反馈
# ============================================================

_FINDING_TEMPLATE = """以下是审查任务的一条真实发现(代码审计或文书审核):

【标题】{title}

【详细描述】
{content}

【元信息】{metadata}

请基于这条发现出题(1~3 道)。"""

# agent2 标记的学习点(practice_worthy=true)附加提示:引导出题
# 聚焦 agent2 认为值得学的点,而非泛泛复述发现本身
_LEARNING_NOTE_TEMPLATE = (
    "\n\n【学习价值提示】质检 agent 已判定这条发现对用户有学习价值:"
    "{note}\n请让题目围绕这个学习点展开考察。"
)

# 工作区可用但生成的题目全部缺材料上下文时,追加到 user prompt 重试的质量反馈
_NO_CODE_FEEDBACK = (
    "\n\n【质量反馈】上一轮生成的题目缺少真实材料上下文,不看材料也能作答,不合格。"
    "请先用材料查阅工具(read_file 等)读取相关文件,再基于实际材料重新出题:"
    "题干必须落到具体材料细节,每题必须带 code_snippet,并给出 source_file 与 source_lines。"
)

# 生成题全部为退化题(叙述式判断题/答案泄露措辞)时,追加到 user prompt 重试的质量反馈
_DEGEN_FEEDBACK = (
    "\n\n【质量反馈】上一轮生成的题目属于不合格的出题套路:叙述式判断题"
    "(某同学/某人做了某判断,该判断是否正确)或题干措辞本身泄露答案"
    "(如出现「仅凭」「就想当然」)。请重新出题:题干直接呈现材料内容,"
    "让答题人对专业问题(漏洞模式/设计缺陷/条款风险/语言陷阱)本身作判断,"
    "确保不看材料、不懂专业知识就无法确定答案;材料支撑不起专业考察点"
    "则直接返回空数组 []。"
)


# ============================================================
# 知识点讲解:按出题上下文批量写讲解(口径与出题同一主题视角)
# ============================================================

# 讲解输出契约与质量要求(各主题共享,接在各主题视角 head 之后)
_EXPLAIN_RULES = """## 知识点讲解要求
你现在不是出题,而是给刚做错这些题的学习者写知识点讲解。
每个知识点附了它的**出题上下文**(审计发现原文、实际读到的真实材料片段、
围绕它生成的题目与解析),讲解必须从这些素材里归纳,不得凭空背概念:
1. 四段结构,每段一个 ### 小标题:
   ### 是什么 / ### 为什么会踩 / ### 怎么判断与修复 / ### 易错点
2. 必须落到素材里的具体特征(函数名/调用方式/条款措辞/代码结构),
   可以用行内代码或短列表;**不得虚构素材里没出现过的文件路径与行号**
3. 同一知识点有多条素材时,归纳它们共同指向的那个易错模式,
   不要逐题复述题目与答案
4. 每个知识点单条讲解 200~400 字(中文),硬上限 600 字;整批按数组输出,
   逐条自拼,不共享总字数配额;不要外层 ``` 围栏
5. 素材确实撑不起专业讲解时,该知识点返回空字符串 markdown(宁缺毋滥)

只输出 JSON 数组,不要任何其他文字。每个元素结构:
{"knowledge_key": "原样返回", "markdown": "讲解正文(Markdown)"}"""


def build_explain_system_prompt(topics: list[str], custom_defs: list[dict] | None = None) -> str:
    """知识点讲解 system prompt

    topics 为本批涉及的出题主题 key 列表(去重后按序);每个主题复用出题
    同一份专业视角 head,保证「怎么出题」与「怎么讲解」口径一致。
    custom_defs 为自定义主题词表({key,name,description}),用通用模板渲染。
    """
    heads: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)
        heads.append(resolve_topic_head(topic, custom_defs))
    if not heads:
        heads.append(resolve_topic_head("security"))
    return "\n\n".join(heads) + "\n\n" + _EXPLAIN_RULES


def build_explain_user_prompt(items: list[dict]) -> str:
    """把逐知识点的出题上下文打包成 user prompt(一次批量出多个讲解)

    items 为 explainer 汇好的素材:[{knowledge_key, knowledge_name,
    learning_topic, sources: [...]}];一次调用覆盖多个知识点,
    避免每个知识点一次往返(那会把收尾批量变成 N 次)。
    """
    return (
        "以下是需要写讲解的知识点及其出题上下文(JSON 数组,一条 = 一个知识点):\n"
        + json.dumps(items, ensure_ascii=False)
        + "\n请逐个 knowledge_key 写讲解,按系统要求的格式输出。"
    )
