"""CLI 执行器 skill 支持单元测试(物化 + prompt 注入,内置 react_agent 不受影响)。

覆盖:
- loader.skill_subdir_name:容器内安全子目录名
- loader.resolve_visible_skills:用户隔离 ∩ allowed_skills + 同名去重
- memory_injection.resolve_agent_skills_dir_path:local/sandbox 模式路径
- prompts.executor.build_cli_skills_section:空/非空(零 app.* 依赖,纯文本)
- sandbox_tools.write_skill_files:local 模式物化 + 资源文件 + 二进制跳过 + 清空残留
- acp_base._build_cli_skills_section:按可见集产出注入段
- acp_base._build_prompt_message:skills_section 并入记忆桶末尾(默认空保持兼容)
- orchestrator._write_skill_files_for_task:仅 CLI 执行器物化,builtin 跳过
"""
import uuid
from pathlib import Path
from types import SimpleNamespace

import app.tools.sandbox_tools as st
from app.agents import acp_base
from app.agents import orchestrator
from app.prompts.executor import build_cli_skills_section
from app.services.memory_injection import resolve_agent_skills_dir_path
from app.skills import loader
from app.skills.schema import ParsedSkill, SkillRegistry


def _skill(name: str, scenario_id: str, tmp: Path) -> ParsedSkill:
    d = tmp / scenario_id / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: d-{name}\n---\nbody-{name}\n", encoding="utf-8")
    return ParsedSkill(
        name=name, description=f"d-{name}", scenario_id=scenario_id,
        skill_dir=d, body=f"body-{name}", source_path=md,
    )


# ============================================================
# loader.skill_subdir_name
# ============================================================


def test_skill_subdir_name_sanitizes():
    assert loader.skill_subdir_name("check_sql_injection") == "check_sql_injection"
    # 去掉路径分隔/空段,防穿越;.. 被替换为 _
    assert loader.skill_subdir_name("a/b/c") == "abc"
    assert ".." not in loader.skill_subdir_name("../../x")
    assert loader.skill_subdir_name("") == "skill"
    assert loader.skill_subdir_name("   ") == "skill"


# ============================================================
# loader.resolve_visible_skills
# ============================================================


def test_resolve_visible_skills_isolation_and_filter(tmp_path, monkeypatch):
    owner = uuid.uuid4()
    other = uuid.uuid4()
    reg = SkillRegistry()
    reg.register(_skill("builtin_a", "code_review", tmp_path))
    reg.register(_skill("priv", f"user_{owner}", tmp_path))
    reg.register(_skill("foreign", f"user_{other}", tmp_path))
    monkeypatch.setattr(loader, "REGISTRY", reg)

    names = {s.name for s in loader.resolve_visible_skills(owner, None)}
    assert names == {"builtin_a", "priv"}  # 内置共享 + 自己上传,他人不可见

    anon = {s.name for s in loader.resolve_visible_skills(None, None)}
    assert anon == {"builtin_a"}  # 匿名只见内置

    allowed = {s.name for s in loader.resolve_visible_skills(owner, ["builtin_a"])}
    assert allowed == {"builtin_a"}  # allowed 过滤


def test_resolve_visible_skills_dedups_by_name(tmp_path, monkeypatch):
    reg = SkillRegistry()
    reg.register(_skill("dup", "code_review", tmp_path))
    reg.register(_skill("dup", "general", tmp_path))
    monkeypatch.setattr(loader, "REGISTRY", reg)
    out = loader.resolve_visible_skills(None, None)
    assert [s.name for s in out].count("dup") == 1


# ============================================================
# resolve_agent_skills_dir_path
# ============================================================


def test_resolve_agent_skills_dir_path_mode():
    import os

    assert resolve_agent_skills_dir_path("sandbox", None) == "/home/user/.agent_skills"
    assert resolve_agent_skills_dir_path("local", "/tmp/x") == os.path.join("/tmp/x", ".agent_skills")


# ============================================================
# build_cli_skills_section(纯文本)
# ============================================================


def test_build_cli_skills_section_empty():
    assert build_cli_skills_section([]) == ""


def test_build_cli_skills_section_lists_items():
    section = build_cli_skills_section([
        {"name": "check_sql_injection", "description": "SQL 注入",
         "file": "/home/user/.agent_skills/check_sql_injection/SKILL.md"},
    ])
    assert "[可用技能(Skills)]" in section
    assert "check_sql_injection" in section
    assert "SQL 注入" in section
    assert "/home/user/.agent_skills/check_sql_injection/SKILL.md" in section
    # 工具中立:不写后端 read_file
    assert "read_file" not in section


# ============================================================
# sandbox_tools.write_skill_files(local 模式)
# ============================================================


def _fake_local_ctx(tmp_path):
    return {"mode": "local", "local_dir": tmp_path}


def test_write_skill_files_local_materializes_and_skips_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))

    src = tmp_path / "src" / "sqli"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: sqli\ndescription: d\n---\nbody\n", encoding="utf-8")
    (src / "rules.txt").write_text("rule text", encoding="utf-8")
    (src / "blob.bin").write_bytes(b"\xff\xfe\x00binary")  # 二进制应跳过

    skills = [SimpleNamespace(name="sqli", skill_dir=src)]
    st.write_skill_files("task-1", skills)

    root = tmp_path / ".agent_skills" / "sqli"
    assert (root / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (root / "rules.txt").read_text(encoding="utf-8") == "rule text"
    assert not (root / "blob.bin").exists()


def test_write_skill_files_local_clears_stale_on_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "_get_or_create_session", lambda _tid, **kw: _fake_local_ctx(tmp_path))
    # 先写一个,再用空集合覆盖 → 旧文件应被清掉
    src = tmp_path / "src2" / "old"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("x", encoding="utf-8")
    st.write_skill_files("task-2", [SimpleNamespace(name="old", skill_dir=src)])
    assert (tmp_path / ".agent_skills" / "old" / "SKILL.md").exists()

    st.write_skill_files("task-2", [])
    assert not (tmp_path / ".agent_skills" / "old").exists()


# ============================================================
# acp_base._build_cli_skills_section
# ============================================================


def test_acp_build_cli_skills_section(tmp_path, monkeypatch):
    owner = uuid.uuid4()
    reg = SkillRegistry()
    reg.register(_skill("check_ssrf", "code_review", tmp_path))
    monkeypatch.setattr(loader, "REGISTRY", reg)

    task = SimpleNamespace(user_id=owner, allowed_skills=None, id=uuid.uuid4())
    section = acp_base._build_cli_skills_section(task, "sandbox", None)
    assert "[可用技能(Skills)]" in section
    assert "check_ssrf" in section
    assert "/home/user/.agent_skills/check_ssrf/SKILL.md" in section


def test_acp_build_cli_skills_section_empty_when_none_visible(monkeypatch):
    monkeypatch.setattr(loader, "REGISTRY", SkillRegistry())
    task = SimpleNamespace(user_id=None, allowed_skills=None, id=uuid.uuid4())
    assert acp_base._build_cli_skills_section(task, "sandbox", None) == ""


def test_build_prompt_message_appends_skills_section():
    task = SimpleNamespace(
        user_input="检查仓库", params={"repo_url": "https://github.com/foo/bar"},
    )
    msg = acp_base._build_prompt_message(
        task, 1, None, None, "/home/user/repos/bar", None,
        skills_section="\n\n[可用技能(Skills)]\nmarker-skill",
    )
    assert "marker-skill" in msg
    # 默认不传 → 不产生 skill 段(保持既有行为)
    msg2 = acp_base._build_prompt_message(
        task, 1, None, None, "/home/user/repos/bar", None,
    )
    assert "[可用技能(Skills)]" not in msg2


# ============================================================
# orchestrator._write_skill_files_for_task 门控
# ============================================================


def test_write_skill_files_skipped_for_builtin(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "write_skill_files", lambda *a, **kw: calls.append(a))
    task = SimpleNamespace(executor="builtin", user_id=None, allowed_skills=None, id=uuid.uuid4())
    orchestrator._write_skill_files_for_task(task, "task-x")
    assert calls == []  # 内置 react_agent 走 skill 工具,不物化


def test_write_skill_files_runs_for_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "write_skill_files", lambda tid, skills: calls.append((tid, len(skills))))
    task = SimpleNamespace(executor="qoder_cli", user_id=None, allowed_skills=None, id=uuid.uuid4())
    orchestrator._write_skill_files_for_task(task, "task-y")
    assert calls and calls[0][0] == "task-y"


def test_write_skill_files_swallows_exception(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("sandbox down")
    monkeypatch.setattr(st, "write_skill_files", _boom)
    task = SimpleNamespace(executor="qoder_cli", user_id=None, allowed_skills=None, id=uuid.uuid4())
    # 不抛出(不阻塞任务启动)
    orchestrator._write_skill_files_for_task(task, "task-z")
