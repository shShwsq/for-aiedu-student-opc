"""SKILL 管理后台 API

鉴权与归属(阶段 6 起):
- 读操作(列表/详情):可选登录(get_optional_user)。匿名仅见内置 skill。
- 写操作(upload / upsert / delete / reload):必须登录(get_current_user)。
- 内置 skill(场景目录非 user_ 前缀):全局共享,只读,任何用户不可改删。
- 用户上传的 skill:落地到 <skills_root>/user_<uuid>/<skill_name>/,
  仅 owner 可见、可改、可删(用户隔离)。

路径安全(写操作必读):
- skill 名 / scenario_id 都会当作目录名拼路径,一律先过 loader 的形状校验
  (validate_skill_name / validate_scenario_id:拒 ".." 与路径分隔符、保留名等)。
- 上传的 skill 名来自 zip 内 frontmatter(攻击者可控),校验放在落盘之前;
  存储层 DirectorySkillStorage 另外做 resolve 收口,不论调用方是否校验过。
- 删除只针对"确在当前用户 user_<uid>/ 之下"的目录:内置 skill 的代码资产
  目录永远删不到。

路径设计:
    GET    /skills                                 列出当前用户可见的 skill
    GET    /skills/{scenario_id}                   列出某场景可见的 skill
    GET    /skills/{scenario_id}/{skill_name}       查看 skill 详情(含 body)
    GET    /skills/{scenario_id}/{skill_name}/files           列出 skill 目录文件
    GET    /skills/{scenario_id}/{skill_name}/files/{path}    读取 skill 单文件内容
    POST   /skills/upload                          上传 zip(仅登录用户)
    POST   /skills/{scenario_id}/{skill_name}      更新自己的 skill(仅 owner)
    DELETE /skills/{scenario_id}/{skill_name}      删除 skill(仅 owner)
    POST   /skills/reload                          重新扫描磁盘(登录用户)
"""
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.config import settings
from app.deps import get_current_user, get_optional_user
from app.models.user import User
from app.scenarios.base import resolve_scenario_id
from app.skills import loader as skill_loader
from app.skills.loader import (
    USER_SCENARIO_PREFIX,
    get_user_skills_root,
    reload_registry,
    scenario_owner_id,
    validate_scenario_id,
    validate_skill_name,
)
from app.skills.schema import ParsedSkill
from app.skills.storage import DirectorySkillStorage, SkillStorage
from app.skills.uploader import MAX_ZIP_SIZE, extract_skill_zip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


# 用户上传 skill 的存储后端(目录实现,根目录来自 USER_SKILLS_DIR)
# 未来切换数据库 / 对象存储时,替换此实例即可(实现同一 SkillStorage 接口)
user_skill_storage: SkillStorage = DirectorySkillStorage(get_user_skills_root())


# ============================================================
# 请求 / 响应模型
# ============================================================


class SkillCreateRequest(BaseModel):
    """更新 skill 的请求

    body 直接传 SKILL.md 全文(frontmatter + 正文)。
    仅允许更新自己上传的 skill(owner 校验)。
    """

    content: str


class SkillResponse(BaseModel):
    """skill 详情响应"""

    name: str
    description: str
    scenario_id: str
    body: str
    source_path: str
    owned: bool

    model_config = {"from_attributes": True}


class SkillSummaryResponse(BaseModel):
    """skill 概要响应(列表用)"""

    name: str
    description: str
    scenario_id: str
    owned: bool


class SkillUploadResponse(BaseModel):
    """zip 上传结果"""

    skill: SkillResponse
    replaced: bool  # True=覆盖了已存在的同名 skill


class SkillFileEntry(BaseModel):
    """skill 目录内的单个文件(列表用)"""

    path: str  # 相对 skill 目录的路径,'/' 分隔
    size: int  # 字节数


class SkillFileListResponse(BaseModel):
    """skill 文件列表响应"""

    files: list[SkillFileEntry]


class SkillFileContentResponse(BaseModel):
    """skill 单文件内容响应"""

    path: str
    content: str
    size: int


# ============================================================
# 辅助
# ============================================================


def _user_scenario_id(user_id: uuid.UUID) -> str:
    """当前用户上传 skill 的场景 id(user_<uuid>)"""
    return f"user_{user_id}"


def _is_owned(skill: ParsedSkill, user: User | None) -> bool:
    """skill 是否归当前用户所有(可管理)"""
    if user is None:
        return False
    return scenario_owner_id(skill.scenario_id) == user.id


def _user_skill_path(scenario_id: str, skill_name: str) -> Path:
    """构造用户 skill 的 SKILL.md 路径(upsert 直写用),校验合法性"""
    try:
        safe_scenario = validate_scenario_id(scenario_id)
        safe_name = validate_skill_name(skill_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径参数: {e}") from e

    return get_user_skills_root() / safe_scenario / safe_name / "SKILL.md"


def _owned_skill_dirs_on_disk(skill: ParsedSkill, scenario_id: str) -> list[Path]:
    """列出该 skill 在两处根下的落地目录(逐个收口,只允许 <root>/user_<uid>/ 之内)

    删除动作既不能直接吃 URL 参数拼出的字符串(可含 ../),也不能无条件吃
    注册表里的 skill_dir(可能是旧版本遗留在内置根下的目录):每个候选路径
    resolve 后断言"仍在当前用户的 user_<uid>/ 之下",内置 skill 的代码资产
    目录因此永远删不到。目录名可能不等于 frontmatter name(旧数据手放),
    所以 skill_dir 与约定路径 <root>/<scenario>/<name> 都列进候选。
    """
    try:
        safe_scenario = validate_scenario_id(scenario_id)
        safe_name = validate_skill_name(skill.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径参数: {e}") from e

    bases: list[Path] = []
    candidates: list[Path] = [skill.skill_dir]
    # 经 loader 模块属性取内置根(与扫描侧同源,测试替换 skill_loader.DEFAULT_SKILLS_ROOT 生效)
    for root in (get_user_skills_root(), skill_loader.DEFAULT_SKILLS_ROOT):
        base = root / safe_scenario
        bases.append(base.resolve())
        candidates.append(base / safe_name)

    dirs: list[Path] = []
    for candidate in candidates:
        try:
            resolved = Path(candidate).resolve()
            if not resolved.is_dir():
                continue  # 只清单上真实存在的落地目录(调用方无需再容错)
        except OSError:
            continue
        # resolved != base:防止退化成"删掉整个用户目录"(那由注销账户流程负责)
        if any(resolved != base and resolved.is_relative_to(base) for base in bases):
            if resolved not in dirs:
                dirs.append(resolved)
    return dirs


def _to_summary(skill: ParsedSkill, user: User | None) -> SkillSummaryResponse:
    return SkillSummaryResponse(
        name=skill.name,
        description=skill.description,
        scenario_id=skill.scenario_id,
        owned=_is_owned(skill, user),
    )


def _to_detail(skill: ParsedSkill, user: User | None) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        scenario_id=skill.scenario_id,
        body=skill.body,
        source_path=str(skill.source_path),
        owned=_is_owned(skill, user),
    )


def _require_visible(skill: ParsedSkill, user: User | None) -> None:
    """可见性校验:他人上传的私有 skill 视同不存在(404,不泄露存在性)"""
    owner = scenario_owner_id(skill.scenario_id)
    if owner is not None and owner != (user.id if user else None):
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {skill.scenario_id}/{skill.name}",
        )


def _require_owned(skill: ParsedSkill, user: User) -> None:
    """owner 校验:仅用户上传的 skill 可改删,内置/他人 skill 一律 403"""
    if not _is_owned(skill, user):
        raise HTTPException(
            status_code=403,
            detail="仅 skill 的上传者可以修改/删除(内置 skill 只读)",
        )


# 单文件内容读取上限(与上传单文件上限对齐,可经环境变量覆盖)
MAX_READ_SIZE = settings.SKILL_MAX_READ_SIZE_MB * 1024 * 1024
# 文件列表条目数上限(防御异常目录,可经环境变量覆盖)
MAX_LISTED_FILES = settings.SKILL_MAX_LISTED_FILES


def _list_skill_files(skill_dir: Path) -> list[SkillFileEntry]:
    """列出 skill 目录内的文件(递归,跳过隐藏文件)

    排序:SKILL.md 置顶,其余按相对路径字典序。超出条目上限时截断。
    """
    entries: list[SkillFileEntry] = []
    for p in skill_dir.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(skill_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        entries.append(SkillFileEntry(path="/".join(rel_parts), size=size))
    entries.sort(key=lambda e: (e.path != "SKILL.md", e.path))
    return entries[:MAX_LISTED_FILES]


def _read_skill_file(skill_dir: Path, file_path: str) -> SkillFileContentResponse:
    """读取 skill 目录内单个文件的 UTF-8 文本内容

    安全:resolve 后校验目标仍位于 skill_dir 内,防路径穿越。
    抛出 HTTPException:404 不存在/越界,400 超限或非 UTF-8 文本。
    """
    base = skill_dir.resolve()
    target = (base / file_path).resolve()
    if not target.is_file() or not target.is_relative_to(base):
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")

    size = target.stat().st_size
    if size > MAX_READ_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件超过读取上限 {MAX_READ_SIZE // 1024 // 1024}MB,无法预览",
        )
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail="文件不是 UTF-8 文本,无法预览",
        ) from e
    return SkillFileContentResponse(
        path=target.relative_to(base).as_posix(),
        content=content,
        size=size,
    )


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=list[SkillSummaryResponse])
def list_all_skills(
    current_user: User | None = Depends(get_optional_user),
) -> list[SkillSummaryResponse]:
    """列出当前用户可见的 skill(内置全局共享 + 自己上传的)"""
    skills = skill_loader.list_visible_skills(
        current_user.id if current_user else None
    )
    return [_to_summary(s, current_user) for s in skills]


@router.get("/{scenario_id}", response_model=list[SkillSummaryResponse])
def list_scenario_skills(
    scenario_id: str,
    current_user: User | None = Depends(get_optional_user),
) -> list[SkillSummaryResponse]:
    """列出某场景的 skill(他人的私有场景返回空列表,不泄露存在性)

    旧场景 id 经别名解析(如 code_security_audit → code_review),
    使老任务/旧链接仍能查到合并后的 skill。
    """
    scenario_id = resolve_scenario_id(scenario_id)
    owner = scenario_owner_id(scenario_id)
    if owner is not None and owner != (current_user.id if current_user else None):
        return []
    skills = skill_loader.REGISTRY.list_for_scenario(scenario_id)
    return [_to_summary(s, current_user) for s in skills]


@router.get("/{scenario_id}/{skill_name}", response_model=SkillResponse)
def get_skill_detail(
    scenario_id: str,
    skill_name: str,
    current_user: User | None = Depends(get_optional_user),
) -> SkillResponse:
    """查看 skill 详情(含 body)"""
    scenario_id = resolve_scenario_id(scenario_id)
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {scenario_id}/{skill_name}",
        )
    _require_visible(skill, current_user)
    return _to_detail(skill, current_user)


@router.get(
    "/{scenario_id}/{skill_name}/files",
    response_model=SkillFileListResponse,
)
def list_skill_files(
    scenario_id: str,
    skill_name: str,
    current_user: User | None = Depends(get_optional_user),
) -> SkillFileListResponse:
    """列出 skill 目录内的文件(供管理界面文件列表)"""
    scenario_id = resolve_scenario_id(scenario_id)
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {scenario_id}/{skill_name}",
        )
    _require_visible(skill, current_user)
    return SkillFileListResponse(files=_list_skill_files(skill.skill_dir))


@router.get(
    "/{scenario_id}/{skill_name}/files/{file_path:path}",
    response_model=SkillFileContentResponse,
)
def read_skill_file(
    scenario_id: str,
    skill_name: str,
    file_path: str,
    current_user: User | None = Depends(get_optional_user),
) -> SkillFileContentResponse:
    """读取 skill 目录内单个文件的文本内容(供管理界面文件预览/编辑)"""
    scenario_id = resolve_scenario_id(scenario_id)
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {scenario_id}/{skill_name}",
        )
    _require_visible(skill, current_user)
    return _read_skill_file(skill.skill_dir, file_path)


def _check_upload_name_conflict(skill_name: str, user_id: uuid.UUID) -> ParsedSkill | None:
    """上传前重名检查

    只查两处(跳过其他用户的目录:运行时按用户隔离,同名不冲突):
    - 用户自己的场景目录 user_<uuid>:同名 → 返回该 skill,由调用方按 force 决定覆盖
    - 公共/内置场景(非 user_ 前缀):同名 → raise 409(全局共享,会与自己的 skill 去重歧义)
    无冲突 → 返回 None
    """
    # 1) 自己的目录:O(1) 直接命中
    existing = skill_loader.REGISTRY.get(_user_scenario_id(user_id), skill_name)

    # 2) 内置场景(非 user_ 前缀,数量极少):同名拒绝
    for sid in skill_loader.REGISTRY.list_scenarios():
        if sid.startswith(USER_SCENARIO_PREFIX):
            continue  # 用户目录:只需查自己的(上面已查);他人的隔离不冲突
        hit = skill_loader.REGISTRY.get(sid, skill_name)
        if hit:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"skill 名称与系统内置的「{hit.name}」冲突,无法覆盖"
                    f"(请换一个 skill 名称)"
                ),
            )
    return existing


@router.post("/upload", response_model=SkillUploadResponse)
async def upload_skill_zip(
    file: UploadFile = File(...),
    force: bool = Form(False),
    current_user: User = Depends(get_current_user),
) -> SkillUploadResponse:
    """上传 zip 格式的 skill

    zip 结构(二选一):
        <skill_name>/SKILL.md   # 标准结构,可携带附加资源
        SKILL.md                # 简化结构,单文件

    - skill 名以 SKILL.md frontmatter.name 为准
    - skill 名会被当作落盘目录名,非法形状(../ / 路径分隔符 / 保留名)在
      任何磁盘写入之前就拒绝(parse_skill_md 已挡,这里再显式过一遍)
    - 落地到 USER_SKILLS_DIR 下的 user_<uid>/<skill_name>/,仅上传者可见可用
    - 与全局(含内置/他人)skill 重名 → 409;与自己的 skill 重名时
      传 force=true 可覆盖,否则 409
    """
    zip_bytes = await file.read()
    if len(zip_bytes) > MAX_ZIP_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"zip 文件超过大小上限 {MAX_ZIP_SIZE // 1024 // 1024}MB",
        )

    scenario_id = _user_scenario_id(current_user.id)

    # 解压到临时目录并校验(结构 / frontmatter / 大小 / 扩展名白名单)
    tmp_root = Path(tempfile.mkdtemp(prefix="skill_upload_"))
    try:
        try:
            skill = extract_skill_zip(zip_bytes, tmp_root)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # 落地前锁死名字形状:此后的任何一步都不该再拿未校验的名字拼路径
        try:
            skill_name = validate_skill_name(skill.name)
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"SKILL.md frontmatter name 非法(不能用作目录名): {e}",
            ) from e

        # 重名检查(规则见 _check_upload_name_conflict):
        # 与内置同名拒绝;与他人同名允许(运行时隔离);与自己同名需 force
        existing = _check_upload_name_conflict(skill_name, current_user.id)
        if existing and not force:
            raise HTTPException(
                status_code=409,
                detail=f"skill「{skill_name}」已存在",
            )

        # 落地(目录实现:拷贝到 USER_SKILLS_DIR;覆盖时由存储后端清旧数据)
        # 存储层会再校验 + resolve 收口,越界名到这里只会是 400,不会写到别人目录
        try:
            replaced = user_skill_storage.save(scenario_id, skill_name, skill.skill_dir)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"skill 无法落地: {e}"
            ) from e
        logger.info(
            f"upload skill: user={current_user.id} name={skill_name} "
            f"replaced={replaced}"
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    # 重新扫描注册表,使新 skill 立即生效
    reload_registry()
    uploaded = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not uploaded:
        raise HTTPException(
            status_code=400,
            detail="SKILL.md frontmatter 解析失败,请检查 name/description 字段",
        )

    return SkillUploadResponse(
        skill=_to_detail(uploaded, current_user),
        replaced=replaced,
    )


@router.post("/{scenario_id}/{skill_name}", response_model=SkillResponse)
def upsert_skill(
    scenario_id: str,
    skill_name: str,
    req: SkillCreateRequest,
    current_user: User = Depends(get_current_user),
) -> SkillResponse:
    """更新自己的 skill(直接写 SKILL.md 全文)

    仅允许写入自己上传的 skill 空间(user_<uid> 场景);内置/他人 skill 不可改。
    写完后调用 reload_registry 刷新注册表。
    """
    if scenario_id != _user_scenario_id(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="仅允许更新自己上传的 skill",
        )
    skill_md = _user_skill_path(scenario_id, skill_name)
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(req.content, encoding="utf-8")

    # 立即重新扫描,校验 frontmatter 是否合法
    reload_registry()
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        # 重载后找不到:回读刚写的文件拿到具体原因,别只丢一句"解析失败"
        reason = ""
        try:
            skill_loader.parse_skill_md(skill_md, scenario_id)
        except ValueError as e:
            reason = str(e)
        except OSError:
            pass
        # 删掉刚写的文件,避免污染磁盘(名字非法时注册表永远没这条,留着也看不见)
        try:
            skill_md.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail=(
                "SKILL.md frontmatter 解析失败,请检查 name/description 字段"
                + (f":{reason}" if reason else "")
            ),
        )

    logger.info(f"upsert skill: {scenario_id}/{skill_name} ({skill_md})")
    return _to_detail(skill, current_user)


@router.delete("/{scenario_id}/{skill_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(
    scenario_id: str,
    skill_name: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """删除自己的 skill(删整个 skill 目录)"""
    # 先过形状校验:非法 scenario_id / skill_name(含 ../)直接 400,不进任何拼路径
    try:
        validate_scenario_id(scenario_id)
        validate_skill_name(skill_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径参数: {e}") from e

    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {scenario_id}/{skill_name}",
        )
    _require_owned(skill, current_user)

    # 删除:经存储后端(目录实现删 USER_SKILLS_DIR 下的落地位置)
    user_skill_storage.delete(scenario_id, skill_name)
    # 一并清掉注册表指向的目录(含旧版本遗留在 backend/skills/user_* 的位置),幂等。
    # 只删"确在当前用户 user_<uid>/ 之下"的目录,绝不在内置根的其他场景下拼路径
    for target in _owned_skill_dirs_on_disk(skill, scenario_id):
        shutil.rmtree(target, ignore_errors=True)
    reload_registry()
    logger.info(f"delete skill: {scenario_id}/{skill_name}")


@router.post("/reload", response_model=list[SkillSummaryResponse])
def reload_skills(
    current_user: User = Depends(get_current_user),
) -> list[SkillSummaryResponse]:
    """重新扫描磁盘,刷新注册表"""
    registry = reload_registry()
    result = []
    for scenario_id in registry.list_scenarios():
        for skill in registry.list_for_scenario(scenario_id):
            result.append(_to_summary(skill, current_user))
    return result
