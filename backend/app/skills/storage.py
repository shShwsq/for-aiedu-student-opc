"""用户上传 skill 的存储后端抽象

职责:用户上传 skill 的持久化(保存 / 删除 / 存在性查询)。
内置 skill 是代码资产,始终由 loader 从 DEFAULT_SKILLS_ROOT 文件系统扫描,不经此接口。

当前实现:
- DirectorySkillStorage:文件系统目录存储,根目录来自 settings.USER_SKILLS_DIR

未来扩展:
- 数据库(PostgreSQL) / 对象存储(S3/MinIO)等,实现同一 SkillStorage 接口即可;
  loader 扫描用户 skill 时使用 loader.get_user_skills_root()(与 DirectorySkillStorage
  同源的 settings 路径),DB 实现落地时再扩展 loader 的枚举分支。

安全边界(目录实现自证,不依赖调用方校验):
- scenario_id / skill_name 先过形状校验(拒 ../ 与路径分隔符),再 resolve
  并断言结果仍在根目录内 —— 拼出来的路径逃不出 <root>
- 覆盖写"先拷临时目录、成功后再换名":copytree 失败不会毁掉已存在的 skill
- 单文件写(SKILL.md)与改名同样走暂存 + 换名:验证未通过的请求不应先碰磁盘
"""
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from app.skills.loader import validate_scenario_id, validate_skill_name


class SkillStorage(ABC):
    """用户上传 skill 的存储后端"""

    @abstractmethod
    def save(self, scenario_id: str, skill_name: str, src_dir: Path) -> bool:
        """保存 skill(src_dir 为含 SKILL.md + 资源文件的目录)

        返回 True 表示覆盖了已存在的同名 skill。

        抛出 ValueError:scenario_id / skill_name 非法或落地路径越出根目录
        """

    @abstractmethod
    def delete(self, scenario_id: str, skill_name: str) -> None:
        """删除 skill(幂等,不存在时静默)

        抛出 ValueError:scenario_id / skill_name 非法或落地路径越出根目录
        """

    @abstractmethod
    def write_skill_md(
        self,
        scenario_id: str,
        skill_name: str,
        content: str,
        *,
        move_from: Path | None = None,
    ) -> Path:
        """写入单个 skill 的 SKILL.md 全文(调用方应已在内存里校验过内容)

        skill_name 以 frontmatter.name 为准;move_from 非空时表示改名,一并把
        旧目录的资源文件带到新目录,并在落地成功后拆掉旧目录。

        返回最终 SKILL.md 路径。

        抛出:
            ValueError: scenario_id / skill_name 非法、落地路径越出根目录,
                或 move_from 不在根目录内
            FileExistsError: 改名的目标目录已存在
            FileNotFoundError: 改名的源目录不存在
        """

    @abstractmethod
    def contains(self, scenario_id: str, skill_name: str) -> bool:
        """是否已存在同名 skill(用于重名 / 覆盖判断)"""


class DirectorySkillStorage(SkillStorage):
    """目录存储:<root>/<scenario_id>/<skill_name>/(SKILL.md + 资源文件)

    root 可直接给路径,也可给一个返回路径的可调用对象(如 loader.get_user_skills_root):
    后者每次操作时才求根,避免模块级实例在 import 时把路径冻结下来,
    与 loader 扫描侧的运行时求值保持同源(测试 monkeypatch settings 也能生效)。
    """

    def __init__(self, root: Path | str | Callable[[], Path]) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root() if callable(self._root) else Path(self._root)

    def _dest(self, scenario_id: str, skill_name: str) -> Path:
        """算出落地目录并断言仍在根目录内(存储层兜底,不论调用方是否校验过)

        抛出 ValueError:名称非法,或 resolve 后越出 / 撞上根目录本身
        """
        safe_scenario = validate_scenario_id(scenario_id)
        safe_name = validate_skill_name(skill_name)

        root = self.root.resolve()
        dest = (root / safe_scenario / safe_name).resolve()
        # 兜底:dest == root 会把整个用户根当 skill 目录删掉(校验已挡,这里再断言一次)
        if not dest.is_relative_to(root) or dest == root:
            raise ValueError(
                f"skill 落地路径越出存储根目录: {scenario_id!r}/{skill_name!r}"
            )
        return dest

    def save(self, scenario_id: str, skill_name: str, src_dir: Path) -> bool:
        dest = self._dest(scenario_id, skill_name)
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True)

        # 先完整拷到同级临时目录,再换名替换:真正的破坏动作(移走旧目录)
        # 只在拷贝成功后才发生。临时/备份目录名以 "." 开头,loader 扫描会跳过
        staged = parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
        try:
            shutil.copytree(src_dir, staged)
            replaced = dest.exists()
            if not replaced:
                os.replace(staged, dest)
                return False

            backup = parent / f".{dest.name}.old-{uuid.uuid4().hex}"
            os.replace(dest, backup)
            try:
                os.replace(staged, dest)
            except OSError:
                os.replace(backup, dest)  # 回滚:旧 skill 不丢
                raise
            shutil.rmtree(backup, ignore_errors=True)
            return True
        finally:
            shutil.rmtree(staged, ignore_errors=True)

    def write_skill_md(
        self,
        scenario_id: str,
        skill_name: str,
        content: str,
        *,
        move_from: Path | None = None,
    ) -> Path:
        dest = self._dest(scenario_id, skill_name)

        if move_from is not None and Path(move_from).resolve() != dest:
            # 改名分支自己负责建父目录:dest 必须保持不存在(_write_with_move 要拿它做断言)
            dest.parent.mkdir(parents=True, exist_ok=True)
            return self._write_with_move(dest, content, move_from)

        # 同名更新:先写同级暂存文件再换名覆盖。os.replace 对已存在的目标文件
        # 是原子的(POSIX rename / Windows MoveFileExW + REPLACE_EXISTING),
        # 写到一半崩溃也不会把原 SKILL.md 截断成半截
        dest.mkdir(parents=True, exist_ok=True)
        skill_md = dest / "SKILL.md"
        staged = dest.parent / f".SKILL.md.tmp-{uuid.uuid4().hex}"
        try:
            staged.write_text(content, encoding="utf-8")
            os.replace(staged, skill_md)
        finally:
            staged.unlink(missing_ok=True)  # 成功路径下已被换走
        return skill_md

    def _write_with_move(self, dest: Path, content: str, move_from: Path) -> Path:
        """改名:整个旧 skill 目录 → 新名字目录,SKILL.md 同时换成新内容

        先把旧目录完整拷到暂存目录并覆写 SKILL.md,一次换名落地,然后才拆旧目录:
        换名要求目标不存在(已断言),因此不存在"新目录没建、旧目录已删"的窗口。
        拆旧目录时先删 SKILL.md:即使删空壳失败,扫描侧也不会再注册一条旧名字的幽灵。
        """
        root = self.root.resolve()
        src = Path(move_from).resolve()
        if not src.is_relative_to(root) or src == root:
            raise ValueError(f"改名的源目录不在存储根目录内: {move_from}")
        if not src.is_dir():
            raise FileNotFoundError(f"改名的源 skill 不存在: {src}")
        if dest.exists():
            raise FileExistsError(f"改名目标目录已存在: {dest}")

        staged = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
        try:
            shutil.copytree(src, staged)
            (staged / "SKILL.md").write_text(content, encoding="utf-8")
            os.replace(staged, dest)
        finally:
            shutil.rmtree(staged, ignore_errors=True)  # 成功后已被换走

        (src / "SKILL.md").unlink(missing_ok=True)
        shutil.rmtree(src, ignore_errors=True)
        return dest / "SKILL.md"

    def delete(self, scenario_id: str, skill_name: str) -> None:
        shutil.rmtree(self._dest(scenario_id, skill_name), ignore_errors=True)

    def contains(self, scenario_id: str, skill_name: str) -> bool:
        return self._dest(scenario_id, skill_name).is_dir()
