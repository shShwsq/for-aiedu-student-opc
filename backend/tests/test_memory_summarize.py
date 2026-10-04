"""memory_summarize 纯函数单元测试(不依赖 DB)。

覆盖 _merge_structured / _clean_items / _parse_summary_json / generate_memory_summary。
"""
from app.prompts.memory_curator import (
    DEFAULT_PROJECT_CATEGORY_DEFS,
    PROJECT_CATEGORIES,
    categories_titles,
    render_categories_with_desc,
)
from app.services.memory_summarize import (
    MAX_PROJECT_MEM_INJECT,
    MAX_PROJECT_MEM_STORE,
    _clean_items,
    _merge_freeform,
    _merge_structured,
    _parse_summary_json,
    generate_memory_summary,
)


# ============================================================
# generate_memory_summary 用到的 LLM mock
# ============================================================


class _MockChunk:
    """模拟 LLMClient.chat_stream 产出的 chunk"""

    def __init__(self, content_delta="", finish_reason=None):
        self.content_delta = content_delta
        self.finish_reason = finish_reason


class _MockLLM:
    """模拟 LLMClient:按预设 chunks 产出;fail=True 时 chat_stream 抛异常"""

    def __init__(self, chunks=None, fail=False):
        self._chunks = chunks or []
        self._fail = fail
        self.enable_thinking = True  # generate_memory_summary 会改它

    def chat_stream(self, messages, max_tokens=2048):
        if self._fail:
            raise RuntimeError("LLM 调用失败(模拟)")
        for c in self._chunks:
            yield c


# ---------- _merge_structured ----------

def test_merge_structured_new_category_empty_existing():
    """空 existing + 新条目 → 正确分组,按固定类别顺序输出。"""
    new_items = [
        {"category": "Known Issues", "item": "codex exec needs bypass flag"},
        {"category": "Hard Constraints", "item": "wire_api must be responses"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Hard Constraints\n- wire_api must be responses" in result
    assert "## Known Issues\n- codex exec needs bypass flag" in result
    assert result.index("## Hard Constraints") < result.index("## Known Issues")


def test_merge_structured_dedup_same_category():
    """同类别重复 item → 精确去重。"""
    existing = "## Hard Constraints\n- wire_api must be responses"
    new_items = [
        {"category": "Hard Constraints", "item": "wire_api must be responses"},  # 重复
        {"category": "Hard Constraints", "item": "approval_policy must be never"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result.count("wire_api must be responses") == 1
    assert "approval_policy must be never" in result


def test_merge_structured_preserves_free_text_no_headers():
    """无标题的 existing(用户自由笔记/旧格式残留)→ 作为 preamble 原样保留,新条目并入已知类别。"""
    existing = "用户手写的自由笔记,无标题\n第二行"
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result.startswith("用户手写的自由笔记,无标题\n第二行")
    assert "## Tech Stack\n- PostgreSQL" in result
    assert "## Legacy Notes" not in result  # 不再为旧格式专门建块


def test_merge_structured_preserves_unknown_category():
    """用户自定义的未知类别块 → 原样保留,新条目并入已知类别。"""
    existing = (
        "## Tech Stack\n- PostgreSQL\n\n"
        "## My Custom Notes\n用户自定义内容\n保留原样"
    )
    new_items = [
        {"category": "Tech Stack", "item": "Redis"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert "## Tech Stack\n- PostgreSQL\n- Redis" in result
    assert "## My Custom Notes\n用户自定义内容\n保留原样" in result
    assert result.index("## Tech Stack") < result.index("## My Custom Notes")


def test_merge_structured_preserves_non_list_lines_in_category():
    """已知类别块内用户写的非列表行 → 原样保留,新条目追加到块末尾。"""
    existing = "## Hard Constraints\n这是用户写的说明段落\n- rule A"
    new_items = [
        {"category": "Hard Constraints", "item": "rule B"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert "这是用户写的说明段落" in result
    assert "- rule A" in result
    assert "- rule B" in result


def test_merge_structured_invalid_category_fallback():
    """非法 category → 兜底归入 Lessons Learned。"""
    new_items = [
        {"category": "Nonexistent Category", "item": "some lesson"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Lessons Learned\n- some lesson" in result


def test_merge_structured_truncate_drops_preamble_first():
    """超长时先删 preamble(游离文本相对最旧),保留类别块。"""
    big_preamble = "X" * 5000
    existing = big_preamble  # 无标题,整体作为 preamble
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 1000)
    assert "XXXX" not in result
    assert "## Tech Stack\n- PostgreSQL" in result


def test_merge_structured_no_change_when_all_dup():
    """所有新条目均已存在 → merged == existing(调用方据此跳过写入)。"""
    existing = "## Hard Constraints\n- rule A"
    new_items = [{"category": "Hard Constraints", "item": "rule A"}]
    result = _merge_structured(existing, new_items, PROJECT_CATEGORIES, 8000)
    assert result == existing


def test_merge_structured_only_outputs_nonempty_categories():
    """空类别不输出,避免空标题。"""
    new_items = [
        {"category": "Tech Stack", "item": "PostgreSQL"},
    ]
    result = _merge_structured("", new_items, PROJECT_CATEGORIES, 8000)
    assert "## Hard Constraints" not in result
    assert "## Known Issues" not in result
    assert "## Tech Stack" in result


# ---------- _clean_items ----------

def test_clean_items_filters_invalid():
    raw = [
        {"category": "Hard Constraints", "item": "valid"},
        {"category": "Tech Stack", "item": ""},      # 空 item 丢弃
        {"category": "", "item": "no cat"},           # 空 category 丢弃
        "not a dict",                                  # 非 dict 丢弃
        {"item": "no cat field"},                      # 无 category 丢弃
        {"category": "Known Issues", "item": "  spaced  "},  # 保留并 strip
    ]
    result = _clean_items(raw)
    assert result == [
        {"category": "Hard Constraints", "item": "valid"},
        {"category": "Known Issues", "item": "spaced"},
    ]


def test_clean_items_non_list_returns_empty():
    assert _clean_items(None) == []
    assert _clean_items("not a list") == []
    assert _clean_items({"a": 1}) == []


# ---------- _parse_summary_json ----------

def test_parse_summary_json_new_format():
    content = (
        '{"project_memory_update": [{"category": "Hard Constraints", "item": "rule"}], '
        '"global_memory_update": []}'
    )
    result = _parse_summary_json(content)
    assert result is not None
    assert result["project_memory_update"] == [{"category": "Hard Constraints", "item": "rule"}]
    assert result["global_memory_update"] == []


def test_parse_summary_json_markdown_fence():
    content = (
        '```json\n{"project_memory_update": [], "global_memory_update": '
        '[{"category": "Preferences", "item": "prefers English"}]}\n```'
    )
    result = _parse_summary_json(content)
    assert result is not None
    assert result["global_memory_update"] == [{"category": "Preferences", "item": "prefers English"}]


def test_parse_summary_json_invalid_returns_none():
    assert _parse_summary_json("") is None
    assert _parse_summary_json("not json at all") is None
    assert _parse_summary_json("[]") is None  # 非 dict


# ---------- generate_memory_summary ----------

def test_max_project_mem_store_is_50000():
    """存储上限放宽到 50000(完整记忆写入沙箱文件,无字数限制)。"""
    assert MAX_PROJECT_MEM_STORE == 50000
    assert MAX_PROJECT_MEM_INJECT == 2000


def test_generate_memory_summary_empty_returns_empty():
    """空 memory_content → 返回空串(不调 LLM)。"""
    assert generate_memory_summary("", _MockLLM()) == ""
    assert generate_memory_summary("   \n  ", _MockLLM()) == ""


def test_generate_memory_summary_short_returns_as_is():
    """memory_content ≤ 注入上限 → 直接用完整内容,零成本不调 LLM。"""
    content = "## Hard Constraints\n- rule A\n- rule B"
    llm = _MockLLM(chunks=[_MockChunk("SHOULD NOT BE USED", "stop")])
    result = generate_memory_summary(content, llm)
    assert result == content  # 原样返回
    assert "SHOULD NOT BE USED" not in result  # LLM 未被调用


def test_generate_memory_summary_long_calls_llm():
    """memory_content > 注入上限 → 调 LLM 精简,返回 LLM 输出。"""
    content = "X" * (MAX_PROJECT_MEM_INJECT + 500)
    condensed = "## Hard Constraints\n- condensed rule"
    llm = _MockLLM(chunks=[
        _MockChunk("## Hard Constraints\n- condensed rule", "stop"),
    ])
    result = generate_memory_summary(content, llm)
    assert result == condensed
    # 精简过程会关 thinking(简单任务加速),结束后恢复
    assert llm.enable_thinking is True


def test_generate_memory_summary_llm_output_truncated_if_too_long():
    """LLM 返回超长 → 兜底截断到注入上限。"""
    content = "X" * (MAX_PROJECT_MEM_INJECT + 100)
    too_long = "Y" * (MAX_PROJECT_MEM_INJECT + 200)
    llm = _MockLLM(chunks=[_MockChunk(too_long, "stop")])
    result = generate_memory_summary(content, llm)
    assert len(result) == MAX_PROJECT_MEM_INJECT
    assert result == too_long[:MAX_PROJECT_MEM_INJECT]


def test_generate_memory_summary_llm_failure_fallback_truncate():
    """LLM 调用抛异常 → 兜底硬截断 memory_content 头部。"""
    content = "Z" * (MAX_PROJECT_MEM_INJECT + 100)
    llm = _MockLLM(fail=True)
    result = generate_memory_summary(content, llm)
    assert len(result) == MAX_PROJECT_MEM_INJECT
    assert result == content[:MAX_PROJECT_MEM_INJECT]


def test_generate_memory_summary_llm_empty_output_fallback_truncate():
    """LLM 返回空 → 兜底硬截断。"""
    content = "Z" * (MAX_PROJECT_MEM_INJECT + 100)
    llm = _MockLLM(chunks=[_MockChunk("", "stop")])
    result = generate_memory_summary(content, llm)
    assert result == content[:MAX_PROJECT_MEM_INJECT]


def test_generate_memory_summary_none_llm_truncates():
    """llm=None(如 PUT 路由未加载 LLM)→ 直接硬截断,不调 LLM。"""
    content = "Q" * (MAX_PROJECT_MEM_INJECT + 50)
    result = generate_memory_summary(content, None)
    assert len(result) == MAX_PROJECT_MEM_INJECT
    assert result == content[:MAX_PROJECT_MEM_INJECT]


def test_generate_memory_summary_custom_max_chars():
    """max_chars 参数生效:短于上限直接返回,长于上限截到 max_chars。"""
    short = "## Hard Constraints\n- rule"
    assert generate_memory_summary(short, _MockLLM(), max_chars=500) == short
    long = "X" * 600
    assert generate_memory_summary(long, None, max_chars=500) == long[:500]


def test_generate_memory_summary_freeform_skips_llm():
    """structured=False(freeform)→ 超长直接截断,不调 LLM。"""
    content = "prose " * 1000
    llm = _MockLLM(chunks=[_MockChunk("SHOULD NOT BE USED", "stop")])
    result = generate_memory_summary(content, llm, max_chars=500, structured=False)
    assert result == content.strip()[:500]
    assert "SHOULD NOT BE USED" not in result


# ---------- _merge_freeform ----------

def test_merge_freeform_append_new_after_existing():
    """旧内容 + 新段落拼接,旧内容在前不丢(含用户手写)。"""
    existing = "用户手写的旧笔记"
    result = _merge_freeform(existing, "新一轮归纳的散文", 8000)
    assert result.startswith("用户手写的旧笔记")
    assert "新一轮归纳的散文" in result


def test_merge_freeform_empty_new_returns_existing():
    assert _merge_freeform("old", "  ", 8000) == "old"


def test_merge_freeform_dedup_when_substring():
    """新内容已包含于旧内容 → 原样返回(近似去重,调用方据此跳过写入)。"""
    existing = "第一段经验\n第二段已知问题"
    assert _merge_freeform(existing, "第二段已知问题", 8000) == existing


def test_merge_freeform_truncate_keeps_tail():
    """超长时删头部旧内容,保留尾部新内容。"""
    existing = "X" * 5000
    result = _merge_freeform(existing, "NEW_TAIL", 1000)
    assert "NEW_TAIL" in result
    assert len(result) <= 1000


# ---------- 自定义结构化类别 ----------

def test_invalid_category_falls_back_to_last_category():
    """非法类别归入类别列表末位(自定义类别无 Lessons Learned 时的回退)。"""
    cats = ["Alpha", "Beta"]
    result = _merge_structured("", [{"category": "Nope", "item": "x"}], cats, 8000)
    assert "## Beta\n- x" in result


def test_categories_titles_and_render_desc():
    """categories_titles 提取非空标题;render 输出 "- title: desc" 行。"""
    defs = [
        {"title": "A", "description": "first"},
        {"title": "", "description": "skip"},
        {"title": "B", "description": ""},
    ]
    assert categories_titles(defs) == ["A", "B"]
    rendered = render_categories_with_desc(defs)
    assert "- A: first" in rendered
    assert "- B" in rendered


def test_project_categories_titles_derived_from_defaults():
    """PROJECT_CATEGORIES 默认标题列与 DEFAULT_PROJECT_CATEGORY_DEFS 一致。"""
    assert PROJECT_CATEGORIES == categories_titles(DEFAULT_PROJECT_CATEGORY_DEFS)
