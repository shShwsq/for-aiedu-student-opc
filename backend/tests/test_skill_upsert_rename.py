"""upsert(在线编辑)的先验后写 / 改名语义 + 存储层单文件写测试

锚定 2026-10 安全审查的后续修复:旧 upsert 是"写盘 → reload → 查不到就 unlink",
而注册表按 frontmatter.name 注册,于是编辑器里改 name 会让 unlink 删掉原 skill 的
SKILL.md(实测静默丢数据)。现在:

- 内容校验(编码 / 大小 / frontmatter / name 形状)全部在落盘之前,失败不留任何磁盘痕迹
- 已存在的旧 skill 在校验失败时分毫不动(数据丢失回归)
- 改名 = 整个 skill 目录搬到新名字下,资源文件随行;目标撞内置/已有同名时 409/400
- 写入经 DirectorySkillStorage.write_skill_md:暂存 + 换名,并 resolve 收口
- 存储根目录改为运行时求值(与 loader 同源),DELETE 路由的真实删除链路可测
"""
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import skills as skills_api
from app.routers.skills import SkillCreateRequest, delete_skill, upsert_skill
from app.skills import loader as skill_loader
from app.skills.storage import DirectorySkillStorage


def _skill_md(name: str, description: str = "d", body: str = "body") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """两处根都指向 tmp,并保证每次调用都重新扫盘(不碰开发者本地数据)"""
    user_root = tmp_path / "user_skills"
    builtin_root = tmp_path / "builtin_skills"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))
    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", builtin_root)

    caller = uuid.uuid4()
    return SimpleNamespace(
        tmp_path=tmp_path,
        user_root=user_root,
        builtin_root=builtin_root,
        caller=caller,
        scenario_id=f"user_{caller}",
    )


def _seed_user_skill(env, name: str, *, resources: dict[str, str] | None = None) -> Path:
    """在用户根下放下一个合法 skill 并刷新注册表,返回其目录"""
    d = env.user_root / env.scenario_id / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
    for rel, content in (resources or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    skill_loader.reload_registry()
    return d


def _seed_builtin_skill(env, name: str) -> Path:
    d = env.builtin_root / "code_review" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_skill_md(name, description="builtin"), encoding="utf-8")
    skill_loader.reload_registry()
    return d


def _upsert(env, url_name: str, content: str):
    return upsert_skill(
        env.scenario_id, url_name,
        SkillCreateRequest(content=content),
        current_user=SimpleNamespace(id=env.caller),
    )


# ============================================================
# 数据丢失回归:校验失败不碰磁盘,旧 skill 完好
# ============================================================


def test_upsert_with_illegal_name_leaves_existing_skill_intact(env):
    """改名成非法名:400 且原 SKILL.md 内容 / 资源文件都在(旧实现会删文件)。"""
    d = _seed_user_skill(env, "my_skill", resources={"rules.md": "precious"})
    before = (d / "SKILL.md").read_text(encoding="utf-8")

    with pytest.raises(HTTPException) as ei:
        _upsert(env, "my_skill", _skill_md("中文名"))

    assert ei.value.status_code == 400
    assert "name 非法" in ei.value.detail
    assert (d / "SKILL.md").read_text(encoding="utf-8") == before
    assert (d / "rules.md").read_text(encoding="utf-8") == "precious"
    assert skill_loader.REGISTRY.get(env.scenario_id, "my_skill") is not None


def test_upsert_with_broken_frontmatter_leaves_no_disk_trace(env):
    """完全没有 frontmatter 的内容:400,不创建目录,也不动已存在的同名文件。"""
    d = _seed_user_skill(env, "my_skill")
    before = (d / "SKILL.md").read_text(encoding="utf-8")

    with pytest.raises(HTTPException) as ei:
        _upsert(env, "my_skill", "just body, no frontmatter\n")

    assert ei.value.status_code == 400
    assert "未做任何写入" in ei.value.detail
    assert (d / "SKILL.md").read_text(encoding="utf-8") == before


def test_upsert_rejects_oversized_content_without_writing(env, monkeypatch):
    """超大内容:400 且磁盘上什么都没有(zip 链路有大小上限,直写链路必须对齐)。"""
    monkeypatch.setattr(skills_api, "MAX_SKILL_MD_SIZE", 64)

    with pytest.raises(HTTPException) as ei:
        _upsert(env, "my_skill", _skill_md("my_skill", body="x" * 4096))

    assert ei.value.status_code == 400
    assert "大小上限" in ei.value.detail
    assert not (env.user_root / env.scenario_id / "my_skill" / "SKILL.md").exists()


def test_upsert_rejects_non_utf8_encodable_content(env):
    """孤立代理项无法编成 UTF-8:400,不写盘。"""
    with pytest.raises(HTTPException) as ei:
        _upsert(env, "my_skill", "\ud800")

    assert ei.value.status_code == 400
    assert "UTF-8" in ei.value.detail
    assert not env.user_root.exists()


# ============================================================
# 改名:目录随行
# ============================================================


def test_upsert_rename_moves_whole_dir_with_resources(env):
    """frontmatter 改 name → 目录搬到新名,资源随行,旧名不再注册。"""
    old = _seed_user_skill(env, "my_skill", resources={"rules.md": "r", "refs/n.md": "n"})

    result = _upsert(env, "my_skill", _skill_md("my_skill2", body="new body"))

    assert result.name == "my_skill2"
    new = env.user_root / env.scenario_id / "my_skill2"
    assert (new / "SKILL.md").read_text(encoding="utf-8").count("my_skill2") == 1
    assert "new body" in (new / "SKILL.md").read_text(encoding="utf-8")
    assert (new / "rules.md").read_text(encoding="utf-8") == "r"
    assert (new / "refs" / "n.md").read_text(encoding="utf-8") == "n"
    assert not old.exists()
    assert skill_loader.REGISTRY.get(env.scenario_id, "my_skill") is None
    assert skill_loader.REGISTRY.get(env.scenario_id, "my_skill2") is not None


def test_upsert_rename_rejects_conflict_with_existing_own_skill(env):
    """改名撞自己场景里的另一个 skill → 409,两个 skill 都保持原样。"""
    a = _seed_user_skill(env, "skill_a")
    b = _seed_user_skill(env, "skill_b")

    with pytest.raises(HTTPException) as ei:
        _upsert(env, "skill_a", _skill_md("skill_b"))

    assert ei.value.status_code == 409
    assert a.is_dir() and b.is_dir()
    assert skill_loader.REGISTRY.get(env.scenario_id, "skill_a") is not None


def test_upsert_rename_rejects_builtin_name(env):
    """改名撞内置 → 409(补齐上传链路的同名拒绝,不再只有 upsert 能绕过)。"""
    builtin = _seed_builtin_skill(env, "check_ssrf")
    mine = _seed_user_skill(env, "my_skill")

    with pytest.raises(HTTPException) as ei:
        _upsert(env, "my_skill", _skill_md("check_ssrf"))

    assert ei.value.status_code == 409
    assert builtin.is_dir()  # 内置代码资产不受影响
    assert mine.is_dir()


def test_upsert_rename_rejected_when_url_skill_does_not_exist(env):
    """新建时 name 与 URL 不一致 → 400(改名只对已存在的 skill 生效),不写盘。"""
    with pytest.raises(HTTPException) as ei:
        _upsert(env, "fresh_name", _skill_md("other_name"))

    assert ei.value.status_code == 400
    assert "改名只能针对已存在的 skill" in ei.value.detail
    assert not (env.user_root / env.scenario_id / "other_name").exists()
    assert not (env.user_root / env.scenario_id / "fresh_name").exists()


def test_upsert_fresh_create_rejects_builtin_name(env):
    """新建(name 与 URL 一致但撞内置名)→ 409:与上传 zip 同口径,不再绕过。

    旧实现完全跳过重名检查:用户 skill 与内置同名时注册表两条,运行时
    resolve_visible_skills 同名去重"保留首个"(内置先注册)把用户那条整个遮蔽。
    """
    builtin = _seed_builtin_skill(env, "check_ssrf")

    with pytest.raises(HTTPException) as ei:
        _upsert(env, "check_ssrf", _skill_md("check_ssrf"))

    assert ei.value.status_code == 409
    assert "内置" in ei.value.detail
    assert builtin.is_dir()  # 内置代码资产不受影响
    assert not (env.user_root / env.scenario_id / "check_ssrf").exists()
    assert skill_loader.REGISTRY.get(env.scenario_id, "check_ssrf") is None


def test_upsert_fresh_create_with_clean_name_works(env):
    """全新名字的新建(无已有 skill、不撞内置):正常落盘并进注册表。"""
    result = _upsert(env, "brand_new", _skill_md("brand_new", body="hello"))

    assert result.name == "brand_new"
    d = env.user_root / env.scenario_id / "brand_new"
    assert "hello" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert skill_loader.REGISTRY.get(env.scenario_id, "brand_new") is not None


def test_upsert_same_name_update_keeps_resources(env):
    """原地更新(名字不变):换 SKILL.md 内容,资源文件保持不动。"""
    d = _seed_user_skill(env, "my_skill", resources={"rules.md": "keep me"})

    result = _upsert(env, "my_skill", _skill_md("my_skill", body="updated"))

    assert result.name == "my_skill"
    assert "updated" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert (d / "rules.md").read_text(encoding="utf-8") == "keep me"
    # 不留暂存文件
    leftovers = [p.name for p in d.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_upsert_allows_other_users_same_name(env):
    """他人场景同名不冲突(运行时按用户隔离),改名只动自己的目录。"""
    other = uuid.uuid4()
    theirs = env.user_root / f"user_{other}" / "shared_name"
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text(_skill_md("shared_name"), encoding="utf-8")
    mine = _seed_user_skill(env, "my_skill")
    skill_loader.reload_registry()

    result = _upsert(env, "my_skill", _skill_md("shared_name"))

    assert result.name == "shared_name"
    assert theirs.is_dir()
    assert not mine.exists()


def test_upsert_normalizes_legacy_dir_name_mismatch(env):
    """历史数据目录名 ≠ name:更新时把目录搬回约定位置,不留同名两份。"""
    legacy = env.user_root / env.scenario_id / "legacy_dir"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(_skill_md("my_skill", body="old"), encoding="utf-8")
    (legacy / "rules.md").write_text("keep", encoding="utf-8")
    skill_loader.reload_registry()
    assert skill_loader.REGISTRY.get(env.scenario_id, "my_skill") is not None

    result = _upsert(env, "my_skill", _skill_md("my_skill", body="new"))

    assert result.name == "my_skill"
    canon = env.user_root / env.scenario_id / "my_skill"
    assert "new" in (canon / "SKILL.md").read_text(encoding="utf-8")
    assert (canon / "rules.md").read_text(encoding="utf-8") == "keep"
    assert not legacy.exists()
    assert len(skill_loader.REGISTRY.list_for_scenario(env.scenario_id)) == 1


# ============================================================
# DELETE 路由的真实删除链路(此前只测了 400 分支)
# ============================================================


def test_delete_route_removes_dirs_from_both_roots(env):
    """删除同时清掉用户根与新版本目录 + 内置根下的旧遗留位置。"""
    own = env.user_root / env.scenario_id / "my_skill"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text(_skill_md("my_skill"), encoding="utf-8")
    legacy = env.builtin_root / env.scenario_id / "my_skill"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(_skill_md("my_skill"), encoding="utf-8")
    skill_loader.reload_registry()

    delete_skill(env.scenario_id, "my_skill", current_user=SimpleNamespace(id=env.caller))

    assert not own.exists()
    assert not legacy.exists()
    assert skill_loader.REGISTRY.get(env.scenario_id, "my_skill") is None


def test_delete_route_never_touches_builtin_asset_dir(env):
    """他人/内置同名 skill 的代码资产目录删不到(仅删自己 user_<uid>/ 之下)。"""
    builtin = _seed_builtin_skill(env, "check_ssrf")
    mine = _seed_user_skill(env, "my_skill")

    delete_skill(env.scenario_id, "my_skill", current_user=SimpleNamespace(id=env.caller))

    assert builtin.is_dir()
    assert not mine.exists()


# ============================================================
# 存储层:单文件写 + 改名收口
# ============================================================


def test_storage_write_skill_md_creates_dir_and_overwrites_atomically(tmp_path):
    """目标目录不存在时创建;重复写覆盖内容,同级不留暂存文件。"""
    storage = DirectorySkillStorage(tmp_path)
    md = storage.write_skill_md("user_a", "s1", _skill_md("s1", body="v1"))

    assert md == tmp_path / "user_a" / "s1" / "SKILL.md"
    assert "v1" in md.read_text(encoding="utf-8")

    storage.write_skill_md("user_a", "s1", _skill_md("s1", body="v2"))
    assert "v2" in md.read_text(encoding="utf-8")
    assert [p.name for p in (tmp_path / "user_a").iterdir()] == ["s1"]


def test_storage_write_skill_md_rejects_traversal(tmp_path):
    """与 save/delete 同一套收口:非法名 → ValueError,不建他人目录。"""
    storage = DirectorySkillStorage(tmp_path)

    with pytest.raises(ValueError):
        storage.write_skill_md("user_a", "../victim", _skill_md("x"))
    with pytest.raises(ValueError):
        storage.write_skill_md("../outside", "ok", _skill_md("x"))

    assert not (tmp_path / "victim").exists()
    assert not (tmp_path / "outside").exists()


def test_storage_move_rejects_source_outside_root(tmp_path):
    """改名的源目录必须落在存储根内:拒绝借 move_from 搬走根外目录。"""
    storage = DirectorySkillStorage(tmp_path)
    outside = tmp_path.parent / "elsewhere" / "secret"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text(_skill_md("secret"), encoding="utf-8")

    with pytest.raises(ValueError, match="不在存储根目录内"):
        storage.write_skill_md("user_a", "new_name", _skill_md("new_name"), move_from=outside)

    assert outside.is_dir()
    assert not (tmp_path / "user_a" / "new_name").exists()


def test_storage_move_requires_absent_destination(tmp_path):
    """改名目标已存在 → FileExistsError,源目录不受影响。"""
    storage = DirectorySkillStorage(tmp_path)
    src = storage.write_skill_md("user_a", "old", _skill_md("old")).parent
    (tmp_path / "user_a" / "new").mkdir()

    with pytest.raises(FileExistsError):
        storage.write_skill_md("user_a", "new", _skill_md("new"), move_from=src)

    assert src.is_dir()


def test_storage_root_is_resolved_per_call(tmp_path, monkeypatch):
    """传可调用对象时根目录每次调用才求值(monkeypatch settings 立即生效)。"""
    later = tmp_path / "later_root"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(later))
    storage = DirectorySkillStorage(skill_loader.get_user_skills_root)

    storage.write_skill_md("user_a", "s1", _skill_md("s1"))

    assert (later / "user_a" / "s1" / "SKILL.md").is_file()
    assert storage.contains("user_a", "s1") is True


def test_module_storage_follows_settings(tmp_path, monkeypatch):
    """路由模块级实例不再冻结 import 时的根目录:改 settings 后写入落到新根。"""
    user_root = tmp_path / "user_skills"
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))

    skills_api.user_skill_storage.write_skill_md("user_a", "s1", _skill_md("s1"))

    assert (user_root / "user_a" / "s1" / "SKILL.md").is_file()
