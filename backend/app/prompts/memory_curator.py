"""记忆归纳/精简的提示词资产。

含任务完成后的记忆归纳模板(_SUMMARIZE_PROMPT)与
精简注入模板(_SUMMARIZE_INJECT_PROMPT),以及两类记忆的
默认结构化类别定义(标题 + 描述,作为用户自定义的预置默认)。
执行逻辑(解析 LLM 输出、合并、落库)在 app/services/memory_summarize.py。

结构化类别现为用户可自定义({title, description});下面的 DEFAULT_*_CATEGORY_DEFS
是开箱默认种子(用户可增删改),PROJECT/GLOBAL_CATEGORIES 为其标题列(仅供现有测试与回退)。
约定:类别按列表顺序代表优先级(越靠前越优先),最后一类作为杂项桶(非法/无法归类条目归入)。
"""

# 默认结构化类别定义(开箱种子;title 唯一、按顺序即优先级,末位为杂项桶)
DEFAULT_PROJECT_CATEGORY_DEFS = [
    {"title": "Hard Constraints", "description": "必须遵守的硬性约束(违反会破坏功能或安全)"},
    {"title": "Known Issues", "description": "已发现的缺陷/漏洞与复现要点"},
    {"title": "Audit Directions", "description": "值得优先审查的方向与关注点"},
    {"title": "Tech Stack", "description": "技术栈与关键依赖事实"},
    {"title": "Lessons Learned", "description": "经验教训与其它(杂项)"},
]
DEFAULT_GLOBAL_CATEGORY_DEFS = [
    {"title": "Hard Constraints", "description": "跨项目通用的硬性约束"},
    {"title": "Tech Stack", "description": "常用技术栈事实"},
    {"title": "Preferences", "description": "用户偏好与习惯"},
    {"title": "Lessons Learned", "description": "通用经验教训与其它(杂项)"},
]

# 标题列(由默认定义派生;保持与历史固定枚举一致,供现有测试与回退使用)
PROJECT_CATEGORIES = [d["title"] for d in DEFAULT_PROJECT_CATEGORY_DEFS]
GLOBAL_CATEGORIES = [d["title"] for d in DEFAULT_GLOBAL_CATEGORY_DEFS]


def render_categories_with_desc(cats: list[dict]) -> str:
    """把类别定义列表([{title, description}])渲染为提示词可读的条目文本。

    每行 "- Title: description"(description 为空时仅留 title)。
    """
    lines: list[str] = []
    for c in cats or []:
        if not isinstance(c, dict):
            continue
        title = (c.get("title") or "").strip()
        if not title:
            continue
        desc = (c.get("description") or "").strip()
        lines.append(f"- {title}: {desc}" if desc else f"- {title}")
    return "\n".join(lines)


def categories_titles(cats: list[dict]) -> list[str]:
    """从类别定义列表提取非空标题列(用于合并去重与注入模板)。"""
    out: list[str] = []
    for c in cats or []:
        if isinstance(c, dict):
            title = (c.get("title") or "").strip()
            if title:
                out.append(title)
    return out

# 归纳 prompt(要求输出严格 JSON,带类别结构)
_SUMMARIZE_PROMPT = """You are a memory curator. Based on the task execution records below, extract durable knowledge that will help future tasks of the same kind.

[Repository]
{repo_url}

[User intent]
{user_intent}

[react_agent per-round summaries]
{react_summaries}

[agent2 final evaluation]
{ua_reasoning}

Rules:
- Write in English. Preserve language-specific Chinese terms, user quotes, and UI strings verbatim (do NOT translate them).
- Each item must be a single concise line. No multi-paragraph prose.
- Only include genuinely reusable knowledge (constraints, known pitfalls, audit directions, tech stack facts, preferences, lessons). Skip one-off task details.

Categorize each item into exactly ONE of its allowed categories (listed as "Title: meaning").
[project_memory_update categories]
{project_categories}
[global_memory_update categories]
{global_categories}

Output STRICT JSON (no markdown fences; each "category" must be a title above). Use empty arrays if nothing new.
{{
  "project_memory_update": [
    {{"category": "Hard Constraints", "item": "..."}},
    {{"category": "Known Issues", "item": "..."}}
  ],
  "global_memory_update": [
    {{"category": "Preferences", "item": "..."}}
  ]
}}
"""

# 自由叙述归纳 prompt(freeform 模式:不分类,输出两段连贯纯文本)
_SUMMARIZE_PROMPT_FREEFORM = """You are a memory curator. Based on the task execution records below, write durable knowledge that will help future tasks of the same kind, as concise free-form notes (no categories, no bullet lists).

[Repository]
{repo_url}

[User intent]
{user_intent}

[react_agent per-round summaries]
{react_summaries}

[agent2 final evaluation]
{ua_reasoning}

Rules:
- Write in English. Preserve language-specific Chinese terms, user quotes, and UI strings verbatim (do NOT translate them).
- Only capture genuinely reusable knowledge (constraints, known pitfalls, tech stack facts, preferences, lessons). Skip one-off task details.
- Each of the two fields is a short plain-text paragraph (a few sentences); no markdown, no headers, no bullets.

Output STRICT JSON (no markdown fences). Use empty strings if nothing new:
{{
  "project_memory_update": "...",
  "global_memory_update": "..."
}}
"""

# 精简注入 prompt:把完整项目记忆压缩到 ≤MAX_PROJECT_MEM_INJECT 字符
# {inject_categories} 由调用方按 structure_mode 传入(与归纳阶段的类别枚举对齐)
_SUMMARIZE_INJECT_PROMPT = """You are condensing a project memory file for injection into an agent's system prompt (max {max_chars} chars).

[Full project memory]
{memory_content}

Rules:
- Write in English. Preserve language-specific Chinese terms, user quotes, and UI strings verbatim (do NOT translate them).
- Output ONLY the condensed memory as a flat list grouped by ## category headers, each item a single line starting with "- ".
- Use these categories in this order (skip empty ones): {inject_categories}.
- Treat earlier-listed categories as higher priority; drop items from later / redundant categories first to fit the limit.
- No preamble, no commentary, no markdown fences — only the condensed memory.
"""
