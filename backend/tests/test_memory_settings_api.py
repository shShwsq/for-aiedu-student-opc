"""记忆设置路由的纯逻辑回归测试(不依赖 DB)。

锁定「结构化类别为空」bug 修复链路上的关键不变量:
- _cat_defs:存量空列表回退内置默认(保证 GET 不再返回空、面板不再显空)
- _normalize_categories:保存时空/脏项回退默认,去重去空标题
- 建行播种的默认列表与 DEFAULT_*_CATEGORY_DEFS 一致
- MemorySettingsOut 出厂即带默认类别(前端无行也能渲染)
"""
from app.models.memory_settings import (
    DEFAULT_MEMORY_INJECT_MAX_CHARS,
    DEFAULT_MEMORY_STRUCTURE_MODE,
)
from app.prompts.memory_curator import (
    DEFAULT_GLOBAL_CATEGORY_DEFS,
    DEFAULT_PROJECT_CATEGORY_DEFS,
    categories_titles,
)
from app.routers.memory import _cat_defs, _normalize_categories
from app.schemas.memory import MemorySettingsOut, MemoryCategoryDef


# ---------- _cat_defs:空回退默认 ----------

def test_cat_defs_empty_stored_falls_back_to_defaults():
    """存量 [] → 返回内置默认(这是面板显空的根因修复)。"""
    out = _cat_defs([], DEFAULT_PROJECT_CATEGORY_DEFS)
    assert categories_titles([c.model_dump() for c in out]) == categories_titles(
        DEFAULT_PROJECT_CATEGORY_DEFS
    )
    assert out  # 非空


def test_cat_defs_none_falls_back_to_defaults():
    assert _cat_defs(None, DEFAULT_GLOBAL_CATEGORY_DEFS)


def test_cat_defs_maps_valid_stored():
    stored = [
        {"title": "MyCat", "description": "d1"},
        {"title": "", "description": "skip"},   # 空标题过滤
        {"title": "MyCat", "description": "dup"},  # 不去重(读侧不处理,保存侧才去重)
    ]
    out = _cat_defs(stored, DEFAULT_PROJECT_CATEGORY_DEFS)
    titles = [c.title for c in out]
    assert titles == ["MyCat", "MyCat"]  # 保留了非空标题,过滤了空标题
    assert out[0].description == "d1"


def test_cat_defs_all_blank_falls_back():
    """全空标题的脏数据 → 回退默认。"""
    out = _cat_defs([{"title": "  ", "description": "x"}], DEFAULT_PROJECT_CATEGORY_DEFS)
    assert categories_titles([c.model_dump() for c in out]) == categories_titles(
        DEFAULT_PROJECT_CATEGORY_DEFS
    )


# ---------- _normalize_categories:保存规范化 ----------

def test_normalize_dedup_and_drop_blank_titles():
    cats = [
        MemoryCategoryDef(title="A", description="a"),
        MemoryCategoryDef(title="", description="drop"),
        MemoryCategoryDef(title="A", description="dup"),
    ]
    out = _normalize_categories(cats, DEFAULT_PROJECT_CATEGORY_DEFS)
    assert [c["title"] for c in out] == ["A"]
    assert out[0] == {"title": "A", "description": "a"}


def test_normalize_empty_falls_back_to_defaults():
    out = _normalize_categories([], DEFAULT_PROJECT_CATEGORY_DEFS)
    assert out == [dict(d) for d in DEFAULT_PROJECT_CATEGORY_DEFS]


def test_normalize_empty_defaults_is_safe_copy():
    """返回的默认项是新字典,不共享 DEFAULT_* 里的原对象(避免调用方误改全局)。"""
    out = _normalize_categories([], DEFAULT_PROJECT_CATEGORY_DEFS)
    out[0]["title"] = "MUTATED"
    assert DEFAULT_PROJECT_CATEGORY_DEFS[0]["title"] != "MUTATED"


# ---------- 建行播种默认(端点 create 分支的取值来源) ----------

def test_seed_defaults_are_two_distinct_sets():
    """项目/全局默认类别各自独立(用户选定的'各一套')。"""
    p = [dict(d) for d in DEFAULT_PROJECT_CATEGORY_DEFS]
    g = [dict(d) for d in DEFAULT_GLOBAL_CATEGORY_DEFS]
    assert p and g
    assert p[0]["title"] and g[0]["title"]


# ---------- MemorySettingsOut 出厂即带默认类别 ----------

def test_memory_settings_out_default_has_categories_and_defaults():
    """无行时的默认出参应携带内置默认类别(前端据此渲染),且带系统默认字段模式等。"""
    out = MemorySettingsOut()
    assert out.memory_enabled is True
    assert out.structure_mode == DEFAULT_MEMORY_STRUCTURE_MODE
    assert out.inject_max_chars == DEFAULT_MEMORY_INJECT_MAX_CHARS
    assert categories_titles([c.model_dump() for c in out.project_categories]) == (
        categories_titles(DEFAULT_PROJECT_CATEGORY_DEFS)
    )
    assert categories_titles([c.model_dump() for c in out.global_categories]) == (
        categories_titles(DEFAULT_GLOBAL_CATEGORY_DEFS)
    )
