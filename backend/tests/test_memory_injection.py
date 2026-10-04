"""memory_injection 单元测试(用 MagicMock 模拟 DB Session,不连真实 DB)。

覆盖 build_react_agent_memory_section / build_agent2_memory_section /
build_global_memory_section。
"""
import os
from unittest.mock import MagicMock

from app.services.memory_injection import (
    MAX_PROJECT_MEM_CHARS,
    build_global_memory_section,
    build_react_agent_memory_section,
    build_agent2_memory_section,
    load_project_memory_brief,
    resolve_agent_memory_file_path,
)


def _mock_db(first_result=None):
    """构造 mock db:db.query(Model).filter(...).first() 统一返回 first_result。"""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = first_result
    return db


# ---------- build_react_agent_memory_section ----------

def test_react_section_empty_when_no_user():
    db = _mock_db()
    assert build_react_agent_memory_section(db, None, "https://github.com/a/b") == ""


def test_react_section_empty_when_no_repo():
    db = _mock_db()
    assert build_react_agent_memory_section(db, 1, "") == ""
    assert build_react_agent_memory_section(db, 1, None) == ""


def test_react_section_empty_when_no_project():
    db = _mock_db(first_result=None)  # Project 查不到
    assert build_react_agent_memory_section(db, 1, "https://github.com/a/b") == ""


def test_react_section_empty_when_no_memory_content():
    proj = MagicMock()
    proj.memory_content = ""
    proj.memory_summary = ""
    proj.alias = None
    db = _mock_db(first_result=proj)
    assert build_react_agent_memory_section(db, 1, "https://github.com/a/b") == ""


def test_react_section_uses_memory_summary():
    """memory_summary 非空 → 注入精简版(非 memory_content)+ 路径提示。"""
    proj = MagicMock()
    proj.memory_content = "## Hard Constraints\n- 这是很长的完整记忆不应出现"
    proj.memory_summary = "## Hard Constraints\n- 精简版摘要"
    proj.alias = "my-repo"
    db = _mock_db(first_result=proj)
    result = build_react_agent_memory_section(db, 1, "https://github.com/a/b")
    # 引导语开头(非 ## 标题,避免与内层 ## 类别层级冲突)
    assert result.startswith("The following is your known issues and historical memory")
    assert "Project alias: my-repo" in result
    # 注入的是精简版,不是完整 memory_content
    assert "## Hard Constraints\n- 精简版摘要" in result
    assert "这是很长的完整记忆不应出现" not in result
    # 末尾有完整记忆文件路径提示
    assert "/home/user/.agent_memory/project_memory.md" in result


def test_react_section_summary_empty_falls_back_to_content():
    """memory_summary 为空 → 回退用 memory_content(截断)+ 路径提示(兼容旧数据)。"""
    proj = MagicMock()
    proj.memory_content = "## Hard Constraints\n- rule A\n\n## Known Issues\n- issue B"
    proj.memory_summary = ""
    proj.alias = None
    db = _mock_db(first_result=proj)
    result = build_react_agent_memory_section(db, 1, "https://github.com/a/b")
    assert result.startswith("The following is your known issues and historical memory")
    # 回退用完整 memory_content
    assert "## Hard Constraints\n- rule A" in result
    assert "## Known Issues\n- issue B" in result
    # 末尾有路径提示
    assert "/home/user/.agent_memory/project_memory.md" in result


# ---------- load_project_memory_brief(单源数据加载,react / CLI 两侧共用) ----------

def test_brief_prefers_summary_over_content():
    """summary 非空 → 返回精简版 + alias(包装由各侧自行添加)。"""
    proj = MagicMock()
    proj.memory_content = "完整记忆原文"
    proj.memory_summary = "精简摘要"
    proj.alias = "my-repo"
    db = _mock_db(first_result=proj)
    text, alias = load_project_memory_brief(db, 1, "https://github.com/a/b")
    assert text == "精简摘要"
    assert alias == "my-repo"


def test_brief_falls_back_to_truncated_content():
    """summary 为空 → 回退 memory_content 截断(旧数据同样有值;
    CLI 侧经此获得此前缺失的回退能力)。"""
    long_content = "x" * (MAX_PROJECT_MEM_CHARS + 100)
    proj = MagicMock()
    proj.memory_content = long_content
    proj.memory_summary = ""
    proj.alias = None
    db = _mock_db(first_result=proj)
    text, alias = load_project_memory_brief(db, 1, "https://github.com/a/b")
    assert text == "x" * MAX_PROJECT_MEM_CHARS + "\n[...truncated...]"
    assert alias is None


def test_brief_empty_when_no_project():
    db = _mock_db(first_result=None)
    assert load_project_memory_brief(db, 1, "https://github.com/a/b") == ("", None)


def test_brief_empty_for_anonymous():
    db = _mock_db()
    assert load_project_memory_brief(db, None, "https://github.com/a/b") == ("", None)


# ---------- build_agent2_memory_section ----------

def test_agent2_section_empty_when_no_user():
    db = _mock_db()
    assert build_agent2_memory_section(db, None) == ""


def test_agent2_section_empty_when_no_pref_no_memory():
    # UserPreference 和 UserMemory 都查不到 → first 均返回 None
    db = _mock_db(first_result=None)
    assert build_agent2_memory_section(db, 1) == ""


def test_agent2_section_pref_only():
    """User Profile 段直接注入 user_profile 自由文本(不再读结构化 preferences)。"""
    pref = MagicMock()
    pref.user_profile = (
        "# User Profile\n\n## Output Language\n中文\n\n"
        "## Focus Areas\n- security\n- perf\n\n## Evaluation Style\nstrict"
    )
    # 先查 UserPreference(返回 pref),再查 UserMemory(返回 None)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, None]
    result = build_agent2_memory_section(db, 1)
    assert "## User Profile" in result
    # user_profile 原样整段注入(含其内部 Markdown 内容)
    assert "## Output Language" in result
    assert "中文" in result
    assert "security" in result
    assert "strict" in result
    # 无全局记忆段
    assert "long-term memory" not in result


def test_agent2_section_pref_empty_user_profile_not_injected():
    """pref 存在但 user_profile 为空白 → 不注入 User Profile 段。

    行为变化:不再读结构化 preferences 凑内容;user_profile 是唯一来源,空则不注入。
    """
    pref = MagicMock()
    pref.user_profile = "   "  # 空白
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PG"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, mem]
    result = build_agent2_memory_section(db, 1)
    assert "## User Profile" not in result
    assert "## Tech Stack\n- PG" in result


def test_agent2_section_global_memory_with_categories():
    mem = MagicMock()
    mem.content = "## Hard Constraints\n- rule A\n\n## Preferences\n- prefers English"
    # 无 pref,有 mem
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, mem]
    result = build_agent2_memory_section(db, 1)
    assert "long-term memory accumulated across tasks" in result
    assert "## Hard Constraints\n- rule A" in result
    assert "## Preferences\n- prefers English" in result


def test_agent2_section_both_pref_and_memory():
    """User Profile(user_profile 非空)+ 全局记忆两段共存。"""
    pref = MagicMock()
    pref.user_profile = "# User Profile\nEnglish output"
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PostgreSQL"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, mem]
    result = build_agent2_memory_section(db, 1)
    # 两段都有,用 \n\n 拼接
    assert "## User Profile" in result
    assert "English output" in result
    assert "long-term memory accumulated across tasks" in result
    assert "## Tech Stack\n- PostgreSQL" in result


def test_agent2_section_with_project_memory():
    """repo_url 非空 + 有 Project.memory_summary → 追加项目记忆精简版段。"""
    proj = MagicMock()
    proj.memory_summary = "## Hard Constraints\n- wire_api 必须 responses"
    proj.memory_content = "完整记忆不应被使用"
    # 无 pref,无 global mem,有 project(三次查询:Pref / UserMemory / Project)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, None, proj]
    result = build_agent2_memory_section(db, 1, "https://github.com/a/b")
    assert "summary of known issues and historical memory" in result
    assert "## Hard Constraints\n- wire_api 必须 responses" in result
    assert "完整记忆不应被使用" not in result  # 用 summary 不用 content


def test_agent2_section_project_falls_back_to_content():
    """memory_summary 为空 → 回退用 memory_content 截断(兼容旧数据)。"""
    proj = MagicMock()
    proj.memory_summary = ""
    proj.memory_content = "## Known Issues\n- issue X"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, None, proj]
    result = build_agent2_memory_section(db, 1, "https://github.com/a/b")
    assert "## Known Issues\n- issue X" in result


def test_agent2_section_no_project_when_no_repo_url():
    """repo_url 为 None → 不查 Project,无项目记忆段(向后兼容)。"""
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PG"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [None, mem]
    result = build_agent2_memory_section(db, 1)  # 不传 repo_url
    assert "long-term memory accumulated across tasks" in result
    assert "summary of known issues and historical memory" not in result


def test_agent2_section_pref_global_and_project_combined():
    """三段共存:User Profile + 全局记忆 + 项目记忆精简版。"""
    pref = MagicMock()
    pref.user_profile = "# User Profile\n中文输出"
    mem = MagicMock()
    mem.content = "## Tech Stack\n- PG"
    proj = MagicMock()
    proj.memory_summary = "## Hard Constraints\n- rule A"
    proj.memory_content = ""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [pref, mem, proj]
    result = build_agent2_memory_section(db, 1, "https://github.com/a/b")
    assert "## User Profile" in result
    assert "中文输出" in result
    assert "long-term memory accumulated across tasks" in result
    assert "summary of known issues and historical memory" in result
    assert "## Hard Constraints\n- rule A" in result


# ---------- build_global_memory_section ----------

def test_global_section_empty_when_no_user():
    db = _mock_db(first_result=MagicMock())
    assert build_global_memory_section(db, None) == ""


def test_global_section_empty_when_no_memory():
    db = _mock_db(first_result=None)  # UserMemory 查不到
    assert build_global_memory_section(db, 1) == ""


def test_global_section_empty_when_blank_content():
    mem = MagicMock()
    mem.content = "   \n  "
    db = _mock_db(first_result=mem)
    assert build_global_memory_section(db, 1) == ""


def test_global_section_returns_content_with_header():
    mem = MagicMock()
    mem.content = "## Hard Constraints\n- wire_api 必须 responses\n\n## Lessons Learned\n- codex bridge 必须 SSE"
    db = _mock_db(first_result=mem)
    result = build_global_memory_section(db, 1)
    # 执行侧 header(区别于 agent2 的"长期记忆"措辞)
    assert result.startswith("The following is general experience accumulated across tasks, organized by category (follow during execution):")
    assert "## Hard Constraints\n- wire_api 必须 responses" in result
    assert "## Lessons Learned\n- codex bridge 必须 SSE" in result


def test_global_section_truncates_long_content():
    """超长全局记忆截断到注入上限 + 尾部截断标记(不调 LLM,直接截断)。"""
    from app.services.memory_injection import MAX_GLOBAL_MEM_CHARS

    mem = MagicMock()
    mem.content = "X" * (MAX_GLOBAL_MEM_CHARS + 500)
    db = _mock_db(first_result=mem)
    result = build_global_memory_section(db, 1)
    # header 后接截断内容
    body = result.split("\n", 1)[1]
    # 截断保留头部内容 + 加截断标记,整体比原文短
    assert body.startswith("X" * 100)
    assert "[...truncated...]" in body
    assert len(body) < len(mem.content)


def test_global_section_truncated_appends_file_hint():
    """被截断时附全局记忆文件路径提示(供 read_file 查全量);未截断不附。"""
    from app.services.memory_injection import MAX_GLOBAL_MEM_CHARS

    # 超长 → 截断 → 附提示
    mem = MagicMock()
    mem.content = "X" * (MAX_GLOBAL_MEM_CHARS + 500)
    result = build_global_memory_section(_mock_db(first_result=mem), 1)
    assert "/home/user/.agent_memory/global_memory.md" in result

    # 未超长 → 不附提示(内联已是全量)
    mem2 = MagicMock()
    mem2.content = "## Tech Stack\n- FastAPI"
    result2 = build_global_memory_section(_mock_db(first_result=mem2), 1)
    assert "global_memory.md" not in result2


def test_global_section_show_pointer_false_omits_hint():
    """show_pointer=False(外部 CLI 侧):即使截断也不内联 /home/user 文件提示。

    指针改由 build_cli_memory_section 按运行模式统一产出,避免双指针 + local
    模式下失效的 Linux 路径。
    """
    from app.services.memory_injection import MAX_GLOBAL_MEM_CHARS

    mem = MagicMock()
    mem.content = "X" * (MAX_GLOBAL_MEM_CHARS + 500)
    result = build_global_memory_section(
        _mock_db(first_result=mem), 1, show_pointer=False,
    )
    assert "The following is general experience" in result  # 内容仍在
    assert "global_memory.md" not in result                 # 但不含指针


def test_resolve_agent_memory_file_path_local_mode():
    """local 模式:返回 <local_dir>/.agent_memory/<file>,不含 /home/user。"""
    p = resolve_agent_memory_file_path(
        "local", "/tmp/sandbox_local_abc", "global_memory.md",
    )
    assert p == os.path.join("/tmp/sandbox_local_abc", ".agent_memory", "global_memory.md")
    assert "/home/user" not in p


def test_resolve_agent_memory_file_path_sandbox_mode():
    """sandbox / 其他模式:回落沙箱绝对路径 /home/user/.agent_memory/<file>。"""
    assert (
        resolve_agent_memory_file_path("sandbox", None, "global_memory.md")
        == "/home/user/.agent_memory/global_memory.md"
    )
    # 非 local(含空 mode / local 但无 local_dir)一律回落沙箱路径
    assert (
        resolve_agent_memory_file_path("", None, "project_memory.md")
        == "/home/user/.agent_memory/project_memory.md"
    )
    assert (
        resolve_agent_memory_file_path("local", "", "project_memory.md")
        == "/home/user/.agent_memory/project_memory.md"
    )
