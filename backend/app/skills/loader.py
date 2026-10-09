"""SKILL 加载器

扫描磁盘上的 SKILL.md 文件,解析 frontmatter,注册到 SkillRegistry。

目录结构(与 scenarios 模块对齐):
    <skills_root>/
      <scenario_id>/
        <skill_name>/
          SKILL.md          # 必需
          <可选附加文件>      # 规则文件、示例等,skill body 可引用

进程级缓存:启动时扫描一次,管理员后台改完调 reload() 重新扫描。
"""
import logging
import re
import uuid
from pathlib import Path

import yaml

from app.config import settings
from app.skills.schema import ParsedSkill, SkillRegistry

logger = logging.getLogger(__name__)


# 内置 SKILL 根目录(backend/skills/,代码资产,随仓库版本化)
DEFAULT_SKILLS_ROOT = Path(__file__).parent.parent.parent / "skills"


# 进程级注册表(模块单例)
REGISTRY = SkillRegistry()


# ============================================================
# 用户上传 skill 根目录
# ============================================================


def get_user_skills_root() -> Path:
    """用户上传 skill 根目录(settings.USER_SKILLS_DIR,相对路径基于运行目录)

    与 storage.DirectorySkillStorage 同源:router 上传落地 / loader 扫描共用此目录。
    生产环境可经环境变量指向独立可写 volume。
    """
    p = Path(settings.USER_SKILLS_DIR)
    return p if p.is_absolute() else Path.cwd() / p


# ============================================================
# frontmatter 解析
# ============================================================

# SKILL.md 必须以 --- 开头的 frontmatter 块开始
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    re.DOTALL,
)


def parse_skill_md(path: Path, scenario_id: str) -> ParsedSkill:
    """解析一个 SKILL.md 文件

    参数:
        path: SKILL.md 绝对路径
        scenario_id: 所属场景(从目录路径推断)

    抛出:
        ValueError: frontmatter 缺失或必填字段不完整
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(
            f"SKILL.md 缺少 frontmatter 或格式错误(应以 --- 开头): {path}"
        )

    frontmatter_text, body = m.group(1), m.group(2)
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"SKILL.md frontmatter YAML 解析失败: {path}: {e}") from e

    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not name or not description:
        raise ValueError(
            f"SKILL.md frontmatter 缺少必填字段 name/description: {path}"
        )

    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError(
            f"SKILL.md frontmatter name/description 必须是字符串: {path}"
        )

    return ParsedSkill(
        name=name.strip(),
        description=description.strip(),
        scenario_id=scenario_id,
        skill_dir=path.parent,
        body=body.rstrip() + "\n",
        source_path=path,
    )


# ============================================================
# 扫描与注册
# ============================================================


def discover_skills(skills_root: Path | None = None) -> SkillRegistry:
    """扫描 SKILL.md,返回新的 SkillRegistry

    扫描规则(每个根):
        <skills_root>/<scenario_id>/<skill_name>/SKILL.md

    - skills_root 显式传入时仅扫该根(供测试 / 单根场景)
    - 默认扫内置根(DEFAULT_SKILLS_ROOT)+ 用户根(get_user_skills_root(),目录存在时)
    - 用户根与内置根共用同一注册表;内置根中历史遗留的 user_* 目录仍会被扫到
      (兼容旧版本落地位置),用户根后扫、同名覆盖
    - 单个 SKILL.md 解析失败不阻断其他 skill,只记录 warning
    """
    if skills_root is not None:
        registry = SkillRegistry()
        _scan_root(skills_root, registry)
        return registry

    registry = SkillRegistry()
    _scan_root(DEFAULT_SKILLS_ROOT, registry)

    user_root = get_user_skills_root()
    if user_root.is_dir():
        _scan_root(user_root, registry)
    else:
        logger.debug(f"用户 skill 目录不存在,跳过: {user_root}")
    return registry


def _scan_root(root: Path, registry: SkillRegistry) -> None:
    """扫描单个根目录下所有 SKILL.md 并注册到 registry"""
    if not root.is_dir():
        logger.warning(f"skills 目录不存在: {root},返回空注册表")
        return

    # 遍历 <root>/<scenario_id>/<skill_name>/SKILL.md
    for scenario_dir in sorted(root.iterdir()):
        if not scenario_dir.is_dir() or scenario_dir.name.startswith("."):
            continue
        scenario_id = scenario_dir.name

        for skill_dir in sorted(scenario_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue

            try:
                skill = parse_skill_md(skill_md, scenario_id)
            except Exception as e:
                logger.warning(f"解析 SKILL.md 失败,跳过: {skill_md}: {e}")
                continue

            # 同一 scenario 下重名,后注册的覆盖(以扫描顺序为准)
            if registry.get(scenario_id, skill.name):
                logger.warning(
                    f"skill 重名覆盖: scenario={scenario_id} name={skill.name} "
                    f"(新文件: {skill_md})"
                )
            registry.register(skill)
            logger.info(f"已加载 skill: {scenario_id}/{skill.name} ({skill_md})")


def reload_registry(skills_root: Path | None = None) -> SkillRegistry:
    """重新扫描磁盘,刷新进程级注册表

    管理员后台改完 SKILL.md 后调用此函数
    """
    global REGISTRY
    REGISTRY = discover_skills(skills_root)
    return REGISTRY


# ============================================================
# 查询接口(供 skill_tool / routers/skills 使用)
# ============================================================

# 用户上传 skill 的场景前缀:user_<uuid>。内置 skill 的场景目录无此前缀。
USER_SCENARIO_PREFIX = "user_"


def scenario_owner_id(scenario_id: str) -> uuid.UUID | None:
    """解析场景目录的 owner

    用户上传的 skill 落在 <root>/user_<uuid>/<skill_name>/,scenario_id 即 user_<uuid>。
    返回 owner 的 UUID;非 user_ 前缀或后缀不是合法 UUID 时返回 None(视为内置 skill)。
    """
    if not scenario_id.startswith(USER_SCENARIO_PREFIX):
        return None
    try:
        return uuid.UUID(scenario_id[len(USER_SCENARIO_PREFIX):])
    except ValueError:
        return None


def list_visible_skills(user_id: uuid.UUID | None) -> list[ParsedSkill]:
    """列出某用户可见的 skill

    可见性规则:
    - 内置 skill(场景目录非 user_ 前缀):全局共享,所有用户可见
    - 用户上传的 skill:仅 owner 可见(用户隔离)

    返回跨场景平铺的列表(与 skill_tool 的全局视角一致,同名去重由调用方处理)。
    """
    result: list[ParsedSkill] = []
    for scenario_id in REGISTRY.list_scenarios():
        owner = scenario_owner_id(scenario_id)
        if owner is not None and owner != user_id:
            continue  # 他人的 skill,不可见
        result.extend(REGISTRY.list_for_scenario(scenario_id))
    return result


def skill_subdir_name(name: str) -> str:
    """skill 名 → 容器内物化子目录名(物化写入与 prompt 指针共用,保证两侧一致)

    skill.name 正常是标识符;兜一层防路径穿越/非法字符:去掉路径分隔符与空段/ ".",
    把 ".." 换成 "_",空/异常回退为 "skill"。
    """
    cleaned = "".join(
        p for p in (name or "").strip().replace("\\", "/").split("/")
        if p not in ("", ".")
    )
    cleaned = cleaned.replace("..", "_")
    return cleaned or "skill"


def resolve_visible_skills(
    user_id: uuid.UUID | None, allowed_skills: list[str] | None,
) -> list[ParsedSkill]:
    """解析某任务实际可用的 skill(可见集 ∩ 允许集),供 CLI 执行侧物化/注入用。

    与内置 react_agent 的 skill 工具口径一致(skill_tool._get_all_skills + allowed
    过滤),只是这里显式接收参数而非读 ContextVar,便于 orchestrator/acp_base 调用:
    - 可见:user_id 的内置 skill(全局共享)+ 自己上传的 skill(用户隔离)
    - 允许:allowed_skills 非空时按名称过滤;None/空表示全部可用(默认)
    - 同名跨场景去重(保留首个,与 react 侧一致)

    返回平铺列表;无匹配时返回空列表。
    """
    seen_names: set[str] = set()
    deduped: list[ParsedSkill] = []
    for skill in list_visible_skills(user_id):
        if skill.name in seen_names:
            continue
        seen_names.add(skill.name)
        deduped.append(skill)

    if allowed_skills:
        allowed_set = set(allowed_skills)
        return [s for s in deduped if s.name in allowed_set]
    return deduped


def get_skill(scenario_id: str, skill_name: str) -> ParsedSkill | None:
    """按 (scenario, name) 查找 skill"""
    return REGISTRY.get(scenario_id, skill_name)


def list_skills(scenario_id: str) -> list[ParsedSkill]:
    """列出某场景的所有 skill"""
    return REGISTRY.list_for_scenario(scenario_id)
