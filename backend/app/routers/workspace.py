"""工作区浏览路由

让前端在任务详情页浏览 react_agent clone 的工作区文件结构、查看文件内容。

端点:
- GET /tasks/{task_id}/workspace          工作区信息(是否可用、repo_path、has_uploads)
- GET /tasks/{task_id}/workspace/files    列出目录(懒加载树,单层)
- GET /tasks/{task_id}/workspace/tree     整树快照(首屏一次拉取,带短 TTL 缓存)
- GET /tasks/{task_id}/workspace/file     读取文件内容(原始文本 + 分页,前端自行渲染行号)
- GET /tasks/{task_id}/workspace/uploads/tree 沙箱过期后回退浏览用户上传文件树
- GET /tasks/{task_id}/workspace/uploads/file 回退读取上传文件内容(同 workspace/file 形状)
- POST /tasks/{task_id}/workspace/restore 发起过期工作区重新 clone(后台执行,立即返回 job)
- GET  /tasks/{task_id}/workspace/restore/status 查询恢复进度(前端轮询)

session 生命周期:
- 任务运行中:clone 完成后即可浏览
- 任务完成后:session 保留 1 小时(TTL),供用户回看
- 超时后惰性清理(下次访问任意 workspace 端点时触发)

上传回退浏览(uploads/tree / uploads/file):
- 沙箱 session 过期后,仓库代码可 restore 重 clone,但用户上传是不可再生的
  用户资产 —— 回退端点直接从上传存储(local 目录 / S3)按 upload_layout
  布局拼出与沙箱树同构的文件树,不经过沙箱
- 内容可读期与上传保留策略一致(默认 30 天 GC,见 upload_gc.py);
  已被 GC 的上传在树中以"已清理"占位展示
- upload_id 只从 task.params 解析(不接受前端指定,防 IDOR)
"""
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_optional_user
from app.models.task import Task
from app.models.user import User
from app.services import workspace_restore
from app.services.upload_layout import (
    UploadSlot,
    compute_upload_layout,
    extract_creation_ids,
    extract_followup_ids,
    resolve_path,
)
from app.services.upload_storage import get_backend
from app.services.uploads import UploadError, load_upload_meta
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workspace"])

# 上传回退树缓存:task_id -> (写入时间戳, payload),短 TTL 缓解 S3 多页 list 压力
# (与 sandbox_tools._tree_cache 同模式;上传内容不可变,GC 后 30s 内可能读到陈旧树,可接受)
_UPLOADS_TREE_CACHE_TTL = 30.0
_uploads_tree_cache: dict[str, tuple[float, dict]] = {}

# 回退读取单文件大小上限:S3 需整对象下载,超限文件不提供在线查看
_UPLOADS_READ_MAX_BYTES = 5 * 1024 * 1024


def _check_task_access(
    task_id: uuid.UUID,
    db: Session,
    current_user: User | None,
) -> Task:
    """加载任务并校验访问权限,返回 task 对象"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    # 权限:任务归属用户或匿名任务可访问(与 get_task 一致)
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")
    return task


def _task_upload_slots(task: Task) -> list[UploadSlot]:
    """从 task.params 解析上传槽位布局(回退树/读文件共用)

    meta 加载失败(被 GC)不影响布局:槽位名回退 uid 前缀,与传输侧
    meta 加载失败时的命名一致;是否存在由调用方经 backend.exists 判断。
    """
    creation_ids = extract_creation_ids(task.params)
    followup_ids = extract_followup_ids(task.params)
    metas: dict[str, dict] = {}
    for uid in creation_ids + followup_ids:
        try:
            metas[uid] = load_upload_meta(uid)
        except UploadError:
            metas[uid] = {}
    return compute_upload_layout(creation_ids, followup_ids, metas)


@router.get("/tasks/{task_id}/workspace")
def get_workspace(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """获取工作区信息

    返回:
    - available: 工作区是否可浏览(session 存在且已 clone)
    - repo_path: 工作区路径
    - completed: 任务是否已完成
    - mode: sandbox/local
    - has_uploads: 任务是否带用户上传(沙箱过期后前端回退浏览的依据;零存储访问)
    - can_restore: 任务是否带 repo_url(工作区过期后前端「重新克隆」按钮的显示依据;
      纯上传任务无仓库可恢复,不显示)
    """
    task = _check_task_access(task_id, db, current_user)

    # 惰性清理过期 session
    sandbox_tools.cleanup_expired_sessions_bg()

    has_uploads = bool(
        extract_creation_ids(task.params) or extract_followup_ids(task.params)
    )
    can_restore = bool((task.params or {}).get("repo_url"))

    info = sandbox_tools.get_workspace_info(str(task_id))
    if info is None:
        return {
            "available": False,
            "reason": "工作区不可用(任务未 clone 仓库或会话已过期)",
            "repo_path": "",
            "completed": False,
            "mode": "",
            "has_uploads": has_uploads,
            "can_restore": can_restore,
        }

    repo_path = info.get("repo_path", "")
    return {
        "available": bool(repo_path),
        "reason": None if repo_path else "尚未 clone 仓库,请等待 react_agent 执行 clone_repo",
        "repo_path": repo_path,
        "completed": info.get("completed", False),
        "mode": info.get("mode", ""),
        "has_uploads": has_uploads,
        "can_restore": can_restore,
    }


@router.get("/tasks/{task_id}/workspace/files")
def list_workspace_files(
    task_id: uuid.UUID,
    subdir: str = Query(default="", description="仓库内相对路径,默认根目录"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """列出工作区某目录下的文件和子目录(单层,懒加载树)

    返回结构与 list_files 工具一致:
    {
        "path": "src/",
        "entries": [{"name": "main.py", "type": "file", "size": 1024}, ...],
        "total": int,
        "truncated": bool,
    }
    """
    _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    try:
        return sandbox_tools.browse_files(str(task_id), subdir)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"[task={task_id}] 列出工作区文件失败: subdir={subdir}")
        raise HTTPException(status_code=500, detail=f"列出文件失败: {e}")


@router.get("/tasks/{task_id}/workspace/tree")
def get_workspace_tree(
    task_id: uuid.UUID,
    max_depth: int = Query(default=4, ge=1, le=8, description="快照最大深度"),
    max_entries: int = Query(default=3000, ge=100, le=10000, description="最大条目数"),
    refresh: bool = Query(default=False, description="绕过缓存强制重建快照"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """一次性返回整树快照(前端文件树首屏渲染,替代逐级懒加载)

    返回结构:
    {
        "entries": [{"path": "src/main.py", "type": "file"|"dir"}, ...],
        "truncated": bool,   # 超上限截断,前端未覆盖目录退回 /workspace/files 懒加载
        "max_depth": int,    # 快照实际覆盖深度
    }
    """
    _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    try:
        return sandbox_tools.browse_tree(str(task_id), max_depth, max_entries, refresh)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"[task={task_id}] 获取工作区树快照失败")
        raise HTTPException(status_code=500, detail=f"获取文件树失败: {e}")


@router.get("/tasks/{task_id}/workspace/file")
def read_workspace_file(
    task_id: uuid.UUID,
    path: str = Query(..., description="仓库内文件相对路径"),
    offset: int = Query(default=1, ge=1, description="起始行号(1-based)"),
    max_lines: int = Query(default=500, ge=1, le=2000, description="最多返回行数"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """读取工作区内文件内容(原始文本,支持分页)

    返回结构:
    {
        "path": str,
        "content": str,       # 原始文本(不带行号前缀,前端自行渲染行号列)
        "start_line": int,
        "end_line": int,
        "total_lines": int,
        "truncated": bool,
    }

    注意:与 LLM 工具 read_file 不同,此处 content 不带行号前缀。
    前端 WorkspaceSidebar 用 start_line + 行索引自行渲染行号,
    若后端再带行号会造成两列行号重复。
    """
    _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    try:
        return sandbox_tools.browse_read_file(str(task_id), path, offset, max_lines)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"[task={task_id}] 读取工作区文件失败: path={path}")
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")


@router.get("/tasks/{task_id}/workspace/uploads/tree")
def get_workspace_uploads_tree(
    task_id: uuid.UUID,
    max_entries: int = Query(default=3000, ge=100, le=10000, description="最大条目数"),
    refresh: bool = Query(default=False, description="绕过缓存强制重建"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """沙箱过期后回退浏览:用户上传文件树(不经过沙箱)

    路径布局与沙箱树同构(相对工作根,见 upload_layout):
    - 单创建上传 → 文件平铺根;多创建上传 → {i}-{name}/;追问 → followup_uploads/{i}-{name}/

    返回:
    {
        "entries": [{"path": "0-报告.zip/doc.md", "type": "file"|"dir"}, ...],
        "truncated": bool,
        "max_depth": int,   # 快照实际覆盖深度(前端建树用)
        "unavailable": ["1-旧附件.zip"],  # 已被 GC 清理的上传占位标签
    }
    """
    task = _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    cached = _uploads_tree_cache.get(str(task_id))
    if not refresh and cached is not None and time.time() - cached[0] < _UPLOADS_TREE_CACHE_TTL:
        return cached[1]

    try:
        payload = _build_uploads_tree(task, max_entries)
    except Exception as e:
        logger.exception(f"[task={task_id}] 获取上传文件树失败")
        raise HTTPException(status_code=500, detail=f"获取上传文件树失败: {e}")

    _uploads_tree_cache[str(task_id)] = (time.time(), payload)
    return payload


def _build_uploads_tree(task: Task, max_entries: int) -> dict:
    """按槽位布局拼接各上传的文件树(available 判定 + unavailable 占位)

    带前缀的槽位(多上传 {i}-{name}/、followup_uploads/{i}-{name}/)必须补齐
    前缀目录链条目:前端 buildUploadsTree 按父路径挂节点、跳过缺父目录的
    孤儿条目,不补链则整枝文件不显示(list_files 只含 files/ 内部相对条目)。
    """
    backend = get_backend()
    slots = _task_upload_slots(task)
    entries: list[dict] = []
    ensured_dirs: set[str] = set()
    unavailable: list[str] = []
    truncated = False
    max_depth = 1

    def _ensure_dir_chain(prefix: str) -> None:
        """补齐前缀目录链(跨槽位去重,如多个 followup 共享 followup_uploads)"""
        parts = prefix.split("/")
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in ensured_dirs:
                ensured_dirs.add(d)
                entries.append({"path": d, "type": "dir"})

    for slot in slots:
        try:
            exists = backend.exists(slot.upload_id)
        except UploadError as e:
            logger.warning(f"[task={task.id}] 检查上传失败(按已清理处理): {e}")
            exists = False
        if not exists:
            # 平铺根槽位(单创建上传)无前缀,占位标签用 uid 前缀
            unavailable.append(slot.prefix or f"上传文件({slot.upload_id[:12]})")
            continue
        try:
            files = backend.list_files(slot.upload_id)
        except UploadError as e:
            logger.warning(f"[task={task.id}] 列出上传文件失败(按已清理处理): {e}")
            unavailable.append(slot.prefix or f"上传文件({slot.upload_id[:12]})")
            continue
        if slot.prefix:
            _ensure_dir_chain(slot.prefix)
        for f in files:
            path = f"{slot.prefix}/{f['path']}" if slot.prefix else f["path"]
            entries.append({"path": path, "type": f["type"]})
            max_depth = max(max_depth, path.count("/") + 1)
            if len(entries) > max_entries:
                truncated = True
                break
        if truncated:
            break

    entries.sort(key=lambda e: e["path"])
    return {
        "entries": entries[:max_entries],
        "truncated": truncated,
        "max_depth": max_depth,
        "unavailable": unavailable,
    }


@router.get("/tasks/{task_id}/workspace/uploads/file")
def read_workspace_uploads_file(
    task_id: uuid.UUID,
    path: str = Query(..., description="工作根内上传文件相对路径"),
    offset: int = Query(default=1, ge=1, description="起始行号(1-based)"),
    max_lines: int = Query(default=500, ge=1, le=2000, description="最多返回行数"),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """沙箱过期后回退读取上传文件内容(原始文本 + 分页)

    响应形状与 /workspace/file 一致(content 无行号,前端自行渲染行号列);
    二进制文件返回占位文案;超过 5MB 的文件不提供在线查看(400)。
    """
    task = _check_task_access(task_id, db, current_user)
    sandbox_tools.cleanup_expired_sessions_bg()

    resolved = resolve_path(_task_upload_slots(task), path)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    upload_id, relpath = resolved

    backend = get_backend()
    try:
        size = backend.stat_file(upload_id, relpath)
        if size > _UPLOADS_READ_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"文件过大({size // (1024 * 1024)}MB),仅支持在线查看不超过 5MB 的文件",
            )
        data = backend.read_file(upload_id, relpath)
    except UploadError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    # 解码 + 分页语义与 sandbox_tools._read_file_local 一致
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": path,
            "content": "(二进制文件,无法显示)",
            "start_line": 0,
            "end_line": 0,
            "total_lines": 0,
            "truncated": False,
        }

    all_lines = text.splitlines()
    total_lines = len(all_lines)
    start_idx = max(0, min(offset - 1, total_lines))
    end_idx = min(start_idx + max_lines, total_lines)
    selected = all_lines[start_idx:end_idx]
    start_line = start_idx + 1
    end_line = start_idx + len(selected)

    return {
        "path": path,
        "content": "\n".join(selected),
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "truncated": end_line < total_lines,
    }


@router.post("/tasks/{task_id}/workspace/restore")
def restore_workspace(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """发起工作区恢复:后台重新 clone,立即返回 job 快照(进度轮询 status 端点)

    做题页右侧代码栏在沙箱 session 过期后展示「重新拉取代码」按钮调用。
    与出题时的自动恢复不同,此处为用户主动触发,不受
    restore_workspace_for_practice 开关限制。恢复的 session 属于已完成任务,
    后台线程会标记 completed 纳入 TTL 清理序列避免常驻泄漏。

    - session 仍存活且已 clone → 幂等返回 done(与旧版直接回工作区信息一致)
    - 任务无 repo_url → 400
    - 已在克隆 → 返回进行中的 job(不重复排队)

    不在本请求里等克隆完成:大仓库是分钟级操作,前端 axios 全局 30s 超时会把它
    误报成"网络错误"(而真实结果几分钟后才落,反而没人看)。
    两段式实现见 app/services/workspace_restore.py。
    """
    task = _check_task_access(task_id, db, current_user)

    info = sandbox_tools.get_workspace_info(str(task_id))
    if info and info.get("repo_path"):
        return {
            "state": "done", "percent": 100, "message": "工作区已就绪", "error": "",
            "available": True, "repo_path": info["repo_path"],
            "mode": info.get("mode", ""),
        }

    params = task.params or {}
    repo_url = params.get("repo_url")
    if not repo_url:
        raise HTTPException(status_code=400, detail="任务无仓库地址,无法恢复工作区")

    # 复用出题模块的 git token 解密逻辑(懒加载避免模块级循环引用)
    from app.services.practice.generator import _load_git_tokens

    logger.info("[task=%s] 用户请求恢复工作区,后台重新 clone", task_id)
    return workspace_restore.start(
        str(task_id), repo_url, params.get("branch"),
        _load_git_tokens(db, task.user_id),
    )


@router.get("/tasks/{task_id}/workspace/restore/status")
def restore_workspace_status(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict:
    """查询恢复 job 进度与终态(state: running / done / failed)

    无 job 时返回 idle:要么从未发起,要么后端重启把内存 job 表与沙箱 session
    一起清掉了(此时前端重新检查可用性即可,用户可再点一次按钮)。
    """
    _check_task_access(task_id, db, current_user)

    job = workspace_restore.status(str(task_id))
    if job is not None:
        return job
    info = sandbox_tools.get_workspace_info(str(task_id)) or {}
    return {
        "state": "idle", "percent": 0, "message": "", "error": "",
        "available": bool(info.get("repo_path")),
        "repo_path": info.get("repo_path", ""), "mode": info.get("mode", ""),
    }
