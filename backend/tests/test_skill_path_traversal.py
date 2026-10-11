"""skill 名/场景 id 的路径穿越防护回归测试

锚定 2026-10 安全审查发现的三条链路(上传名来自 zip frontmatter,攻击者可控):
- loader:parse_skill_md 在解析入口就拒掉非法形状(name 用作落盘目录名)
- uploader:extract_skill_zip 透传该拒绝,越界名不会走到任何磁盘写入
- storage:DirectorySkillStorage 的 save/delete/contains 自证安全(resolve 收口),
  并保证覆盖写失败不毁掉已有 skill
- routers/skills:_user_skill_path / delete 的拼路径入口全部先校验;
  删除范围收口在"当前用户的 user_<uid>/ 之下",内置 skill 目录删不到
"""
import io
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.skills import _owned_skill_dirs_on_disk, _user_skill_path
from app.skills import loader as skill_loader
from app.skills import storage as storage_mod
from app.skills.loader import (
    parse_skill_md,
    validate_scenario_id,
    validate_skill_name,
)
from app.skills.schema import ParsedSkill
from app.skills.storage import DirectorySkillStorage
from app.skills.uploader import extract_skill_zip


# 攻击者构造的穿越名:逃逸用户目录 / 落到他人场景目录 / 退化成删根目录
TRAVERSAL_NAMES = [
    "..",
    "../user_victim/evil",
    "..\\user_victim\\evil",
    "../../evil",
    "a/../../evil",
    ".",
    "..a..b..",
]


def _make_zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: d\n---\nbody\n"


def _write_src(tmp_path: Path, name: str = "src_skill") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_md("my_skill"), encoding="utf-8")
    return d


# ============================================================
# loader:名称形状校验
# ============================================================


def test_validate_rejects_traversal_and_keeps_legal_names():
    """穿越名/保留名/空名拒绝;内置与用户目录的常规名放行。"""
    for bad in TRAVERSAL_NAMES + ["CON", "nul.md", ".hidden", "中文名", "x" * 65, ""]:
        with pytest.raises(ValueError):
            validate_skill_name(bad)

    assert validate_skill_name("check_ssrf") == "check_ssrf"
    assert validate_skill_name(" review_test_quality ") == "review_test_quality"
    # 场景 id:user_<uuid>(含 -)与内置场景名合法
    uid = uuid.uuid4()
    assert validate_scenario_id(f"user_{uid}") == f"user_{uid}"
    assert validate_scenario_id("code_review") == "code_review"
    for bad in TRAVERSAL_NAMES:
        with pytest.raises(ValueError):
            validate_scenario_id(bad)


def test_parse_skill_md_rejects_traversal_name(tmp_path):
    """frontmatter name 越界 → parse 阶段拒绝(注册表永远拿不到穿越名)。"""
    md = tmp_path / "SKILL.md"
    for bad in ("../user_victim/evil", "..", "..\\..\\skills", "a/../../b"):
        # 单引号标量:YAML 不处理转义,确保走到形状校验这一步而不是先炸在 YAML
        md.write_text(f"---\nname: '{bad}'\ndescription: d\n---\nbody\n", encoding="utf-8")
        with pytest.raises(ValueError, match="name 非法"):
            parse_skill_md(md, scenario_id="code_review")


def test_scan_root_skips_illegal_scenario_dir(tmp_path):
    """场景目录名非法时跳过,不注册(不留可被删除逻辑消费的条目)。"""
    root = tmp_path / "skills"
    illegal = root / "a..b"
    (illegal / "some_skill").mkdir(parents=True)
    (illegal / "some_skill" / "SKILL.md").write_text(
        _skill_md("some_skill"), encoding="utf-8"
    )
    good = root / "code_review" / "ok_skill"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text(_skill_md("ok_skill"), encoding="utf-8")

    registry = skill_loader.discover_skills(root)

    assert registry.get("a..b", "some_skill") is None
    assert registry.get("code_review", "ok_skill") is not None


# ============================================================
# uploader:越界名在落盘前就被拒
# ============================================================


def test_extract_rejects_traversal_frontmatter_name(tmp_path):
    """zip 里 frontmatter name 写穿越串 → ValueError(路由据此回 400)。"""
    data = _make_zip({"whatever/SKILL.md": _skill_md("../user_victim/evil")})
    with pytest.raises(ValueError, match="name 非法"):
        extract_skill_zip(data, tmp_path)


# ============================================================
# storage:收口 + 覆盖原子化
# ============================================================


def test_storage_rejects_traversal_names(tmp_path):
    """save/delete/contains 拒绝越界的 scenario_id 与 skill_name。"""
    victim = tmp_path / "user_victim"
    storage = DirectorySkillStorage(tmp_path)
    src = _write_src(tmp_path)

    for bad in TRAVERSAL_NAMES:
        with pytest.raises(ValueError):
            storage.save("user_attacker", bad, src)
        with pytest.raises(ValueError):
            storage.save(bad, "evil", src)
        with pytest.raises(ValueError):
            storage.delete("user_attacker", bad)
        with pytest.raises(ValueError):
            storage.contains("user_attacker", bad)

    # 关键:他人场景目录未被创建(攻击链的另一半要求写进 victim 目录)
    assert not victim.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src_skill"]


def test_storage_save_survives_copytree_failure(tmp_path, monkeypatch):
    """覆盖写中途失败时,已存在的旧 skill 不被毁掉(旧实现先 rmtree 再拷)。"""
    storage = DirectorySkillStorage(tmp_path)
    src_v1 = _write_src(tmp_path / "v1", "v1")
    (src_v1 / "SKILL.md").write_text(_skill_md("my_skill"), encoding="utf-8")
    (src_v1 / "rules.md").write_text("old-rule", encoding="utf-8")
    storage.save("user_a", "my_skill", src_v1)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage_mod.shutil, "copytree", boom)
    with pytest.raises(OSError):
        storage.save("user_a", "my_skill", _write_src(tmp_path / "v2", "v2"))

    dest = tmp_path / "user_a" / "my_skill"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "rules.md").read_text(encoding="utf-8") == "old-rule"
    # 失败路径不留临时/备份目录
    leftovers = [p.name for p in (tmp_path / "user_a").iterdir() if "tmp-" in p.name or "old-" in p.name]
    assert leftovers == []


def test_storage_overwrite_leaves_no_hidden_staging(tmp_path):
    """成功覆盖后同级目录只剩 skill 目录("." 开头的暂存目录会被 loader 跳过)。"""
    storage = DirectorySkillStorage(tmp_path)
    src1 = _write_src(tmp_path / "s1", "s1")
    src2 = _write_src(tmp_path / "s2", "s2")
    (src2 / "rules.md").write_text("new-rule", encoding="utf-8")

    assert storage.save("user_a", "my_skill", src1) is False
    assert storage.save("user_a", "my_skill", src2) is True

    scenario_dir = tmp_path / "user_a"
    assert [p.name for p in scenario_dir.iterdir()] == ["my_skill"]
    assert (scenario_dir / "my_skill" / "rules.md").read_text() == "new-rule"
    assert storage.contains("user_a", "my_skill") is True

    storage.delete("user_a", "my_skill")
    assert storage.contains("user_a", "my_skill") is False
    storage.delete("user_a", "my_skill")  # 幂等


# ============================================================
# routers/skills:拼路径入口
# ============================================================


def test_user_skill_path_rejects_traversal():
    """upsert 的写路径入口:穿越名 → 400(不再只有零散的字符串包含判断)。"""
    for bad in TRAVERSAL_NAMES:
        with pytest.raises(HTTPException) as ei:
            _user_skill_path("user_a", bad)
        assert ei.value.status_code == 400
        with pytest.raises(HTTPException):
            _user_skill_path(bad, "my_skill")


def _skill(scenario_id: str, name: str, skill_dir: Path) -> ParsedSkill:
    return ParsedSkill(
        name=name,
        description="d",
        scenario_id=scenario_id,
        skill_dir=skill_dir,
        body="body\n",
        source_path=skill_dir / "SKILL.md",
    )


def test_owned_skill_dirs_only_under_own_user_dir(tmp_path, monkeypatch):
    """删除范围:只认 <两处根>/user_<caller>/ 之下的目录;内置与他人目录一律不收。"""
    caller = uuid.uuid4()
    victim = uuid.uuid4()
    user_root = tmp_path / "user_skills"
    builtin_root = tmp_path / "builtin_skills"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))
    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", builtin_root)

    scenario_id = f"user_{caller}"
    own = user_root / scenario_id / "my_skill"
    own.mkdir(parents=True)
    dirs = _owned_skill_dirs_on_disk(_skill(scenario_id, "my_skill", own), scenario_id)
    assert dirs == [own.resolve()]

    # 遗留在内置根下的旧位置:一并收录(仍在 caller 的用户目录内)
    legacy = builtin_root / scenario_id / "my_skill"
    legacy.mkdir(parents=True)
    assert legacy.resolve() in _owned_skill_dirs_on_disk(
        _skill(scenario_id, "my_skill", legacy), scenario_id
    )

    # 注册表被伪造成指向他人目录 → 他人目录绝不进清单(自己的约定路径照常清掉)
    alien = user_root / f"user_{victim}" / "evil"
    alien.mkdir(parents=True)
    dirs = _owned_skill_dirs_on_disk(_skill(scenario_id, "my_skill", alien), scenario_id)
    assert alien.resolve() not in dirs
    assert own.resolve() in dirs

    # 内置 skill 的代码资产目录 → 永远删不到
    builtin = builtin_root / "code_review" / "check_ssrf"
    builtin.mkdir(parents=True)
    dirs = _owned_skill_dirs_on_disk(_skill("code_review", "check_ssrf", builtin), scenario_id)
    assert builtin.resolve() not in dirs
    assert all("code_review" not in p.parts for p in dirs)


def test_upsert_route_rejects_traversal_name(tmp_path, monkeypatch):
    """upsert 的写路径入口:穿越名 → 400,且不在盘上留下目录。"""
    from types import SimpleNamespace

    from app.routers.skills import SkillCreateRequest, upsert_skill

    user_root = tmp_path / "user_skills"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))
    caller = uuid.uuid4()

    for bad in ("..", "../user_victim/evil", "..\\skills"):
        with pytest.raises(HTTPException) as ei:
            upsert_skill(
                f"user_{caller}", bad,
                SkillCreateRequest(content=_skill_md(bad)),
                current_user=SimpleNamespace(id=caller),
            )
        assert ei.value.status_code == 400
    assert not user_root.exists()


def test_upsert_route_surfaces_illegal_frontmatter_name(tmp_path, monkeypatch):
    """在线编辑把 name 改成非法形状 → 400 带具体原因,且不落残留文件。"""
    from types import SimpleNamespace

    from app.routers.skills import SkillCreateRequest, upsert_skill

    user_root = tmp_path / "user_skills"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))
    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", tmp_path / "builtin")
    # 免掉真实全盘扫描:这里只关心"非法 name 不落库 + 报错带原因"
    monkeypatch.setattr("app.routers.skills.reload_registry", lambda *a, **k: None)
    caller = uuid.uuid4()

    with pytest.raises(HTTPException) as ei:
        upsert_skill(
            f"user_{caller}", "my_skill",
            SkillCreateRequest(content=_skill_md("中文名")),
            current_user=SimpleNamespace(id=caller),
        )
    assert ei.value.status_code == 400
    assert "name 非法" in ei.value.detail
    # 写完即回滚:目录可能已建,但不该留下 SKILL.md
    stale = user_root / f"user_{caller}" / "my_skill" / "SKILL.md"
    assert not stale.exists()


def test_delete_route_rejects_traversal_before_touching_disk(tmp_path, monkeypatch):
    """DELETE 的 scenario_id / skill_name 非法 → 400,且不做任何删除。"""
    from types import SimpleNamespace

    from app.routers.skills import delete_skill

    user_root = tmp_path / "user_skills"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))
    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", tmp_path / "builtin")
    victim_dir = user_root / f"user_{uuid.uuid4()}" / "keep_me"
    victim_dir.mkdir(parents=True)
    (victim_dir / "SKILL.md").write_text(_skill_md("keep_me"), encoding="utf-8")

    caller = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(HTTPException) as ei:
        delete_skill("user_x", "../keep_me", current_user=caller)
    assert ei.value.status_code == 400
    assert victim_dir.is_dir()

    # 场景 id 侧同样拒绝(不进入 REGISTRY 查询与任何拼路径)
    with pytest.raises(HTTPException) as ei:
        delete_skill("../user_x", "keep_me", current_user=caller)
    assert ei.value.status_code == 400
    assert victim_dir.is_dir()
