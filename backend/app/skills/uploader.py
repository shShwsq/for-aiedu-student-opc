"""SKILL zip 上传解析与校验

用户通过管理界面上传 zip 格式的 skill,本模块负责:
1. 解压到临时目录(防 zip-slip 路径穿越)
2. 结构校验(兼容两种布局,见下)
3. frontmatter 校验(复用 parse_skill_md)
4. 大小 / 文件数 / 扩展名白名单限制

zip 兼容结构(二选一):
    <skill_name>/SKILL.md   # Claude Code 标准,可携带附加资源
    SKILL.md                # 简化版,单文件

安全边界:
- zip 本体 ≤ MAX_ZIP_SIZE
- 解压后总大小 ≤ MAX_EXTRACT_SIZE,单文件 ≤ MAX_SINGLE_FILE_SIZE
- 条目数 ≤ MAX_FILES
- 附加文件仅允许白名单扩展名(防可执行文件/压缩包混入)
- 所有条目必须位于解压根目录内(拒绝 ../ 与绝对路径)
- frontmatter name 必须是可安全用作目录名的形状(parse_skill_md 内校验):
  返回给调用方的 skill.name 会被当作落盘目录名,越界名在这里就拒绝,不留到写盘
"""
import io
import logging
import zipfile
from pathlib import Path

from app.config import settings
from app.skills.loader import parse_skill_md
from app.skills.schema import ParsedSkill

logger = logging.getLogger(__name__)

# 安全边界(均可经环境变量覆盖,默认值见 app/config.py Settings)
# 保留模块级常量名供 routers/skills.py 与测试引用,值由配置注入
MAX_ZIP_SIZE = settings.SKILL_MAX_ZIP_SIZE_MB * 1024 * 1024
MAX_EXTRACT_SIZE = settings.SKILL_MAX_EXTRACT_SIZE_MB * 1024 * 1024
MAX_SINGLE_FILE_SIZE = settings.SKILL_MAX_SINGLE_FILE_SIZE_MB * 1024 * 1024
MAX_FILES = settings.SKILL_MAX_FILES

# 附加资源允许的扩展名白名单(SKILL.md 不受此限制)
# 文档/配置/脚本/常见源码,排除可执行文件、压缩包、二进制格式
_BASE_ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".xml", ".sql", ".py", ".sh", ".js", ".ts", ".html", ".css",
    ".rules", ".example", ".gitignore",
}


def _parse_extra_extensions(raw: str) -> set[str]:
    """解析逗号分隔的额外扩展名:去空白、转小写、补前导点"""
    result: set[str] = set()
    for item in raw.split(","):
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        result.add(ext)
    return result


# 最终白名单 = 内置基础白名单 ∪ 环境变量追加(默认含常见图片格式)
ALLOWED_EXTENSIONS = _BASE_ALLOWED_EXTENSIONS | _parse_extra_extensions(
    settings.SKILL_ALLOWED_EXTENSIONS_EXTRA
)

# Mac zip 常见噪音(打包时自动生成的资源分叉 / 元数据)
_SKIP_PREFIXES = ("__MACOSX/",)
_SKIP_NAMES = {".DS_Store"}


def _validate_entry_name(name: str) -> None:
    """校验 zip 条目名:拒绝绝对路径与 .. 穿越

    抛出 ValueError:非法条目名
    """
    if name.startswith("/") or "\\" in name:
        raise ValueError(f"zip 条目名不合法(绝对路径): {name}")
    # 用 pathlib 归一化后检查是否逃出根目录
    normalized = Path(name)
    if ".." in normalized.parts:
        raise ValueError(f"zip 条目名不合法(路径穿越): {name}")


def _is_skippable(name: str) -> bool:
    """Mac zip 噪音(资源分叉目录 / .DS_Store)跳过"""
    if name in _SKIP_NAMES:
        return True
    return any(name.startswith(p) for p in _SKIP_PREFIXES)


def extract_skill_zip(zip_bytes: bytes, dest_root: Path) -> ParsedSkill:
    """解压并校验一个 skill zip 到 dest_root

    参数:
        zip_bytes: zip 文件完整字节
        dest_root: 解压目标根目录(调用方保证已存在)

    返回:
        解析后的 ParsedSkill(skill_dir 指向解压后的目录,scenario_id 为空占位,
        由调用方按实际归属决定;skill 名取自 frontmatter.name)

    抛出:
        ValueError: 结构不合法(frontmatter 缺失、SKILL.md 缺失/重复/层级错误、
            大小超限、非法条目、扩展名不在白名单等)
        zipfile.BadZipFile: 不是合法 zip
    """
    if len(zip_bytes) > MAX_ZIP_SIZE:
        raise ValueError(f"zip 文件超过大小上限 {MAX_ZIP_SIZE // 1024 // 1024}MB")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # 条目合法性预检:所有条目先过一遍,再决定是否解压
        entries: list[zipfile.ZipInfo] = []
        skill_md_entry: zipfile.ZipInfo | None = None
        total_size = 0
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or _is_skippable(name):
                continue
            try:
                # 非 UTF-8 文件名(PKZIP 旧编码)拒绝,避免乱码落盘
                name.encode("utf-8")
                _validate_entry_name(name)
            except UnicodeEncodeError:
                raise ValueError(f"zip 条目名不是 UTF-8 编码,拒绝: {name!r}")

            # 大小限制
            if info.file_size > MAX_SINGLE_FILE_SIZE:
                raise ValueError(f"zip 内单文件超过大小上限: {name} ({info.file_size} 字节)")
            total_size += info.file_size
            if total_size > MAX_EXTRACT_SIZE:
                raise ValueError(
                    f"解压后总大小超过上限 {MAX_EXTRACT_SIZE // 1024 // 1024}MB"
                )

            # 定位 SKILL.md:根目录或一级子目录
            parts = Path(name).parts
            is_skill_md = parts and parts[-1] == "SKILL.md"
            if is_skill_md:
                if len(parts) > 2:
                    raise ValueError(
                        f"SKILL.md 层级过深(仅支持根目录或 <skill_name>/SKILL.md): {name}"
                    )
                if skill_md_entry is not None:
                    raise ValueError(
                        f"zip 内含多个 SKILL.md(仅允许一个): {skill_md_entry.filename}, {name}"
                    )
                skill_md_entry = info
            else:
                # 附加资源:扩展名白名单
                suffix = Path(name).suffix.lower()
                if suffix not in ALLOWED_EXTENSIONS:
                    raise ValueError(
                        f"zip 内含不允许的文件类型: {name}"
                        f"(仅允许 {sorted(ALLOWED_EXTENSIONS)})"
                    )
            entries.append(info)

        if skill_md_entry is None:
            raise ValueError("zip 内缺少 SKILL.md(应在根目录或 <skill_name>/SKILL.md)")
        if not entries:
            raise ValueError("zip 为空")

        # 解压(目标路径已由 _validate_entry_name 保证在 dest_root 内)
        for info in entries:
            target = dest_root / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)

    # frontmatter 校验:name/description 必填,以文件内容为准
    skill_md_path = dest_root / skill_md_entry.filename
    try:
        skill = parse_skill_md(skill_md_path, scenario_id="")
    except ValueError as e:
        # 校验失败时清理已解压内容,避免污染临时目录
        import shutil

        shutil.rmtree(dest_root, ignore_errors=True)
        raise ValueError(f"SKILL.md 校验失败: {e}") from e

    # skill 目录 = SKILL.md 所在目录(简化版结构即解压根目录)
    skill_dir = skill_md_path.parent
    logger.info(f"skill zip 解析成功: name={skill.name}, dir={skill_dir}")
    return ParsedSkill(
        name=skill.name,
        description=skill.description,
        scenario_id="",  # 由调用方按归属设置
        skill_dir=skill_dir,
        body=skill.body,
        source_path=skill_md_path,
    )
