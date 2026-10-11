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
# 名称形状校验(标识符 == 路径片段,必须在解析入口锁死)
# ============================================================

# skill 名 / 场景 id 既是注册表 key,又是落盘目录名(<root>/<scenario>/<name>/),
# 所以非法形状必须在 parse_skill_md 这一层就拒掉:越界的名字根本进不了注册表,
# 也就无法借 URL 参数把 "../" 回传给删除逻辑(详见 routers/skills.py)。
NAME_MAX_LEN = 64

# 首字符须为字母或数字(挡前导 "." / "-"),其后允许 . _ -
_NAME_SHAPE_RE = re.compile(rf"\A[A-Za-z0-9][A-Za-z0-9._-]{{0,{NAME_MAX_LEN - 1}}}\Z")

# Windows 保留设备名(CON/NUL/AUX/COM1-9/LPT1-9,含带扩展名形式):
# 命中会让目录建得出、删不掉或干脆建不出
_WIN_RESERVED_RE = re.compile(
    r"\A(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?\Z", re.IGNORECASE
)


def _validate_path_segment(value: str, kind: str) -> str:
    """校验"用作单层目录名"的字符串,返回规范化(strip)后的值

    抛出 ValueError:空值 / 含路径分隔符 / "." 或 ".." / 首尾非法字符 /
        Windows 保留名 / 超长
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError(f"非法 {kind}(为空)")
    if len(cleaned) > NAME_MAX_LEN:
        raise ValueError(f"非法 {kind}(超过 {NAME_MAX_LEN} 字符): {value!r}")
    if not _NAME_SHAPE_RE.match(cleaned):
        raise ValueError(
            f"非法 {kind}(须以字母或数字开头,仅允许字母、数字与 . _ -): {value!r}"
        )
    # 形状正则已挡掉分隔符与前导 ".",这里再显式盯住 ".." 段(错误信息更好定位)
    if ".." in cleaned:
        raise ValueError(f"非法 {kind}(含 .. 穿越段): {value!r}")
    if cleaned.endswith("."):
        # 结尾的 "." 在 Windows 上会被静默截断,导致落盘目录名与注册表 key 不一致
        raise ValueError(f"非法 {kind}(不得以 . 收尾): {value!r}")
    if _WIN_RESERVED_RE.match(cleaned):
        raise ValueError(f"非法 {kind}(Windows 保留设备名): {value!r}")
    return cleaned


def validate_skill_name(name: str) -> str:
    """校验 skill 名可安全用作路径片段,返回规范化后的名字(抛 ValueError 表示非法)"""
    return _validate_path_segment(name, "skill 名")


def validate_scenario_id(scenario_id: str) -> str:
    """校验场景 id 可安全用作路径片段,返回规范化后的值(抛 ValueError 表示非法)"""
    return _validate_path_segment(scenario_id, "scenario_id")


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
        ValueError: frontmatter 缺失或必填字段不完整,或 name 形状非法
            (不能安全用作目录名)

    注:scenario_id 不在此校验——上传解析时它还是空占位(由调用方按归属填),
        扫描侧由 _scan_root 校验目录名,落地侧由 storage 收口。
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

    # name 会被当作落盘目录名,形状非法(../ 或分隔符)直接拒绝:
    # 越界名进不了注册表,删除/物化路径也就拿不到穿越段
    try:
        safe_name = validate_skill_name(name)
    except ValueError as e:
        raise ValueError(f"SKILL.md frontmatter name 非法: {path}: {e}") from e

    return ParsedSkill(
        name=safe_name,
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
        # 目录名即 scenario_id,它会参与后续的拼路径;非法名跳过(留 warning)
        try:
            scenario_id = validate_scenario_id(scenario_dir.name)
        except ValueError as e:
            logger.warning(f"场景目录名非法,跳过: {scenario_dir}: {e}")
            continue

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

    注:name 已在 parse_skill_md 过 validate_skill_name(合法名到这里是恒等变换);
    这里保留兜底,是为了让"注册表里出现历史脏数据"也不会变成容器内的穿越路径。
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
