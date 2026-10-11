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
"""
import os
import shutil
import uuid
from abc import ABC, abstractmethod
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
    def contains(self, scenario_id: str, skill_name: str) -> bool:
        """是否已存在同名 skill(用于重名 / 覆盖判断)"""


class DirectorySkillStorage(SkillStorage):
    """目录存储:<root>/<scenario_id>/<skill_name>/(SKILL.md + 资源文件)"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def _dest(self, scenario_id: str, skill_name: str) -> Path:
        """算出落地目录并断言仍在根目录内(存储层兜底,不论调用方是否校验过)

        抛出 ValueError:名称非法,或 resolve 后越出 / 撞上根目录本身
        """
        safe_scenario = validate_scenario_id(scenario_id)
        safe_name = validate_skill_name(skill_name)

        root = self._root.resolve()
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

    def delete(self, scenario_id: str, skill_name: str) -> None:
        shutil.rmtree(self._dest(scenario_id, skill_name), ignore_errors=True)

    def contains(self, scenario_id: str, skill_name: str) -> bool:
        return self._dest(scenario_id, skill_name).is_dir()
