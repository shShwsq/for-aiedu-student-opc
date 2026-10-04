"""长期记忆管理路由

用户可编辑三类记忆:
- User Profile (1:1,自由文本):影响 agent2 评判标准与审查维度
- 全局长期记忆(1:1,自由文本):跨项目通用经验,注入 agent2
- 分项目记忆(1:N,按 repo_url 聚合):注入 react_agent,影响审计方向

端点:
- GET/PUT /memory/preferences       User Profile
- GET/PUT /memory/global            全局长期记忆
- GET    /memory/projects           项目列表
- GET/PUT/DELETE /memory/projects/{project_id}  单个分项目记忆

鉴权:全部 Depends(get_current_user)。匿名用户无法访问(匿名任务不持久化记忆)。
跨用户隔离:所有查询/更新都带 user_id 过滤,确保用户 A 看不到/改不了用户 B 的数据。

注意:项目(repo_url)由 orchestrator 在任务完成时自动归纳创建(_get_or_create_project),
本路由不提供"新建项目"端点——用户只能编辑/删除已由 agent 自动归纳产生的项目记录。
若用户想手动为某仓库预置记忆,可在任务跑一次后编辑,或后续扩展 POST 端点。
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.agent_policy import AgentPolicy
from app.models.memory_settings import MemorySettings
from app.models.practice import (
    DEFAULT_THINKING_MODE,
    PracticeSettings,
)
from app.models.project import Project
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig
from app.models.user_memory import UserMemory
from app.models.user_preference import UserPreference
from app.prompts.memory_curator import (
    DEFAULT_GLOBAL_CATEGORY_DEFS,
    DEFAULT_PROJECT_CATEGORY_DEFS,
)
from app.schemas.memory import (
    MemoryCategoryDef,
    MemorySettingsOut,
    ProjectListResponse,
    ProjectOut,
    SaveAgentPolicyRequest,
    SaveMemorySettingsRequest,
    SavePracticeSettingsRequest,
    SaveProjectRequest,
    SaveUserMemoryRequest,
    SaveUserPreferenceRequest,
    StructureDefaultsOut,
    UserMemoryOut,
    UserPreferenceOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


# ============================================================
# User Profile (1:1)
# ============================================================


@router.get("/preferences", response_model=UserPreferenceOut)
def get_preferences(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """获取当前用户的偏好(未配置则返回空默认值)"""
    return _build_preference_out(db, current_user.id)


@router.put("/preferences", response_model=UserPreferenceOut)
def save_preferences(
    req: SaveUserPreferenceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新 User Profile (get_or_create)"""
    row = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = UserPreference(
            user_id=current_user.id,
            user_profile=req.user_profile,
        )
        db.add(row)
    else:
        row.user_profile = req.user_profile
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了偏好", current_user.id)
    return _build_preference_out(db, current_user.id, pref_row=row)


@router.put("/preferences/practice", response_model=UserPreferenceOut)
def save_practice_settings(
    req: SavePracticeSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新练习设置(自动生成开关 / 出题前恢复工作区 / 默认出题模型 / 思考模式)

    存于 practice_settings 独立表(1:1),get_or_create:无行时自动创建。
    restore_workspace_for_practice / default_llm_config_id /
    force_default_llm / thinking_mode_for_practice 可选:传 None 表示不修改;
    default_llm_config_id 传空串表示清空。
    (learning_topic 已移除:出题主题按发现内容自动匹配)
    """
    # 默认出题模型归属校验:必须是当前用户已保存的 LLM 配置
    if req.default_llm_config_id:
        cfg_row = (
            db.query(UserLLMConfig)
            .filter(UserLLMConfig.user_id == current_user.id)
            .first()
        )
        ids = {c.get("id") for c in (cfg_row.llm_configs or [])} if cfg_row else set()
        if req.default_llm_config_id not in ids:
            raise HTTPException(
                status_code=400,
                detail="出题模型配置不存在或不属于当前用户",
            )
    row = (
        db.query(PracticeSettings)
        .filter(PracticeSettings.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = PracticeSettings(
            user_id=current_user.id,
            auto_generate_practice=req.auto_generate_practice,
        )
        db.add(row)
    else:
        row.auto_generate_practice = req.auto_generate_practice
    if req.restore_workspace_for_practice is not None:
        row.restore_workspace_for_practice = req.restore_workspace_for_practice
    if req.default_llm_config_id is not None:
        row.default_llm_config_id = req.default_llm_config_id or None
    if req.force_default_llm is not None:
        row.force_default_llm = req.force_default_llm
    if req.thinking_mode_for_practice is not None:
        row.thinking_mode_for_practice = req.thinking_mode_for_practice
    db.commit()
    db.refresh(row)
    logger.info(
        "用户 %s 更新练习设置: auto_generate_practice=%s "
        "restore_workspace=%s default_llm_config_id=%s force_default_llm=%s thinking_mode=%s",
        current_user.id, req.auto_generate_practice,
        req.restore_workspace_for_practice,
        row.default_llm_config_id, row.force_default_llm,
        row.thinking_mode_for_practice,
    )
    return _build_preference_out(db, current_user.id, settings_row=row)


@router.put("/preferences/agent_policy", response_model=UserPreferenceOut)
def save_agent_policy(
    req: SaveAgentPolicyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新 agent 策略配置(agent2 启停、验证权限等)

    作为用户级默认值(存 agent_policies 独立表),
    任务级可通过 task.params["_agent_policy"] 覆盖。
    get_or_create:若用户无策略记录,自动创建。
    """
    policy_dict = req.model_dump()
    row = (
        db.query(AgentPolicy)
        .filter(AgentPolicy.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = AgentPolicy(user_id=current_user.id, **policy_dict)
        db.add(row)
    else:
        for key, value in policy_dict.items():
            setattr(row, key, value)
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了 agent_policy", current_user.id)
    return _build_preference_out(db, current_user.id)


@router.put("/preferences/memory_settings", response_model=UserPreferenceOut)
def save_memory_settings(
    req: SaveMemorySettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新记忆生成设置(总开关 / 归纳模型 / 结构模式 / 思考模式 / 注入上限)

    存于 memory_settings 独立表(1:1),get_or_create:无行时自动创建。
    curator_llm_config_id 传 None=不修改,空串=清空(回退 env 默认);传值需属于当前用户
    已保存的 LLM 配置。thinking_mode / structure_mode / inject_max_chars 传 None=不修改。
    """
    # 记忆模型归属校验:必须是当前用户已保存的 LLM 配置
    if req.curator_llm_config_id:
        cfg_row = (
            db.query(UserLLMConfig)
            .filter(UserLLMConfig.user_id == current_user.id)
            .first()
        )
        ids = {c.get("id") for c in (cfg_row.llm_configs or [])} if cfg_row else set()
        if req.curator_llm_config_id not in ids:
            raise HTTPException(
                status_code=400,
                detail="记忆模型配置不存在或不属于当前用户",
            )
    row = (
        db.query(MemorySettings)
        .filter(MemorySettings.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = MemorySettings(
            user_id=current_user.id,
            memory_enabled=req.memory_enabled,
            # 建行即播种结构化类别默认(避免开关型保存落出 [] 空列导致面板显空)
            project_categories=[dict(d) for d in DEFAULT_PROJECT_CATEGORY_DEFS],
            global_categories=[dict(d) for d in DEFAULT_GLOBAL_CATEGORY_DEFS],
        )
        db.add(row)
    else:
        row.memory_enabled = req.memory_enabled
    if req.curator_llm_config_id is not None:
        row.curator_llm_config_id = req.curator_llm_config_id or None
    if req.thinking_mode is not None:
        row.thinking_mode = req.thinking_mode
    if req.structure_mode is not None:
        row.structure_mode = req.structure_mode
    if req.inject_max_chars is not None:
        row.inject_max_chars = req.inject_max_chars
    if req.project_categories is not None:
        row.project_categories = _normalize_categories(
            req.project_categories, DEFAULT_PROJECT_CATEGORY_DEFS,
        )
    if req.global_categories is not None:
        row.global_categories = _normalize_categories(
            req.global_categories, DEFAULT_GLOBAL_CATEGORY_DEFS,
        )
    db.commit()
    db.refresh(row)
    logger.info(
        "用户 %s 更新记忆设置: enabled=%s curator_llm=%s structure=%s thinking=%s inject_max=%s",
        current_user.id, row.memory_enabled, row.curator_llm_config_id,
        row.structure_mode, row.thinking_mode, row.inject_max_chars,
    )
    return _build_preference_out(db, current_user.id)


@router.get("/preferences/structure_defaults", response_model=StructureDefaultsOut)
def get_structure_defaults(
    current_user: User = Depends(get_current_user),
) -> StructureDefaultsOut:
    """返回系统默认结构化类别(内置静态常量,全体用户一致)

    供记忆设置面板一次性拉取,用于展示对照与「恢复系统默认」;
    不塑进 /memory/preferences 响应。
    """
    return StructureDefaultsOut()


def _memory_settings_out(row: MemorySettings | None) -> MemorySettingsOut:
    """MemorySettings 行 → MemorySettingsOut。

    - 无行 → 全默认(类别为内置种子)。
    - 存量行但类别列为空(早期仅保存开关时落库的空列)→ 回退内置默认,
      保证前端始终能看到系统默认类别。
    - default_* 始终为内置种子(供前端对照与恢复)。
    """
    if row is None:
        return MemorySettingsOut()
    pcats = _cat_defs(row.project_categories, DEFAULT_PROJECT_CATEGORY_DEFS)
    gcats = _cat_defs(row.global_categories, DEFAULT_GLOBAL_CATEGORY_DEFS)
    return MemorySettingsOut(
        memory_enabled=row.memory_enabled,
        curator_llm_config_id=row.curator_llm_config_id,
        thinking_mode=row.thinking_mode,
        structure_mode=row.structure_mode,
        project_categories=pcats,
        global_categories=gcats,
        inject_max_chars=row.inject_max_chars,
    )


def _cat_defs(stored, defaults) -> list[MemoryCategoryDef]:
    """存量 JSONB 类别列 → MemoryCategoryDef 列表;为空回退内置默认。

    逐项取 title/description(容忍缺键),过滤无 title 的脏项。
    """
    out: list[MemoryCategoryDef] = []
    for c in stored or []:
        if not isinstance(c, dict):
            continue
        title = (c.get("title") or "").strip()
        if not title:
            continue
        out.append(MemoryCategoryDef(title=title, description=(c.get("description") or "").strip()))
    if out:
        return out
    return [MemoryCategoryDef(**d) for d in defaults]


def _normalize_categories(cats, defaults) -> list[dict]:
    """规范化结构化类别列表:去空标题/去重(保留首个),空结果回退内置默认。

    cats 为 list[MemoryCategoryDef];返回 [{title, description}] 供 JSONB 列存储。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for c in cats or []:
        title = (c.title or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        out.append({"title": title, "description": (c.description or "").strip()})
    return out or [dict(d) for d in defaults]


# ============================================================
# 全局长期记忆(1:1)
# ============================================================


@router.get("/global", response_model=UserMemoryOut)
def get_global_memory(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMemoryOut:
    """获取当前用户的全局长期记忆(未配置则返回空)"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        return UserMemoryOut()
    return UserMemoryOut.model_validate(row)


@router.put("/global", response_model=UserMemoryOut)
def save_global_memory(
    req: SaveUserMemoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMemoryOut:
    """保存/更新全局长期记忆(get_or_create)"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = UserMemory(user_id=current_user.id, content=req.content)
        db.add(row)
    else:
        row.content = req.content
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了全局长期记忆", current_user.id)
    return UserMemoryOut.model_validate(row)


# ============================================================
# 分项目记忆(1:N)
# ============================================================


@router.get("/projects", response_model=ProjectListResponse)
def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    """获取当前用户的所有项目记忆列表(按更新时间倒序)"""
    rows = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return ProjectListResponse(projects=[_project_to_out(r) for r in rows])


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """获取单个项目记忆详情"""
    row = _find_project(db, current_user.id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到项目: {project_id}")
    return _project_to_out(row)


@router.put("/projects/{project_id}", response_model=ProjectOut)
def save_project(
    project_id: str,
    req: SaveProjectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """更新项目记忆的 alias/note/memory_content(用户手动编辑)

    不更新 repo_url(归一化值是项目身份,不可改);
    不更新 last_summary_at(那是自动归纳的时间戳,手动编辑不改它)。

    memory_content 改动后同步重新生成 memory_summary(精简版,注入 system prompt 用):
    ≤2000 即时无 LLM;>2000 走 LLM(env 默认配置),失败兜底硬截断,不阻塞请求。
    """
    row = _find_project(db, current_user.id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到项目: {project_id}")
    row.alias = req.alias
    row.note = req.note
    row.memory_content = req.memory_content
    # 重新生成精简版(用户手改 memory_content 后,旧 summary 可能失效)
    row.memory_summary = _regen_memory_summary(db, current_user.id, req.memory_content)
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 手动更新了项目记忆 %s", current_user.id, project_id)
    return _project_to_out(row)


@router.delete("/projects/{project_id}", response_model=ProjectListResponse)
def delete_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    """删除项目记忆(整行删除,含 alias/note/memory_content)"""
    row = _find_project(db, current_user.id, project_id)
    if row is not None:
        db.delete(row)
        db.commit()
        logger.info("用户 %s 删除了项目记忆 %s", current_user.id, project_id)
    # 返回剩余列表
    rows = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return ProjectListResponse(projects=[_project_to_out(r) for r in rows])


# ============================================================
# 辅助函数
# ============================================================


def _build_preference_out(
    db: Session, user_id,
    pref_row: UserPreference | None = None,
    settings_row: PracticeSettings | None = None,
) -> UserPreferenceOut:
    """组装 UserPreferenceOut(数据跨三表:user_preferences + agent_policies + practice_settings)

    - user_profile 来自 user_preferences(可能无行)
    - agent_policy 来自 agent_policies 独立表(可能无行 → None,前端用系统默认)
    - auto_generate_practice / restore_workspace_for_practice /
      default_llm_config_id / force_default_llm / thinking_mode_for_practice
      来自 practice_settings 独立表(可能无行 → 用默认值)
    - updated_at 取各行中较新的(哪边最后保存,就算最后更新)
    """
    if pref_row is None:
        pref_row = (
            db.query(UserPreference)
            .filter(UserPreference.user_id == user_id)
            .first()
        )
    policy_row = (
        db.query(AgentPolicy)
        .filter(AgentPolicy.user_id == user_id)
        .first()
    )
    if settings_row is None:
        settings_row = (
            db.query(PracticeSettings)
            .filter(PracticeSettings.user_id == user_id)
            .first()
        )
    memory_row = (
        db.query(MemorySettings)
        .filter(MemorySettings.user_id == user_id)
        .first()
    )
    if pref_row is None and policy_row is None and settings_row is None and memory_row is None:
        return UserPreferenceOut()
    updated_at = None
    for r in (pref_row, policy_row, settings_row, memory_row):
        if r is not None and r.updated_at is not None:
            if updated_at is None or r.updated_at > updated_at:
                updated_at = r.updated_at
    return UserPreferenceOut(
        user_profile=pref_row.user_profile if pref_row else "",
        agent_policy=policy_row.to_dict() if policy_row else None,
        auto_generate_practice=settings_row.auto_generate_practice if settings_row else True,
        restore_workspace_for_practice=(
            settings_row.restore_workspace_for_practice if settings_row else False
        ),
        default_llm_config_id=(
            settings_row.default_llm_config_id if settings_row else None
        ),
        force_default_llm=(
            settings_row.force_default_llm if settings_row else False
        ),
        thinking_mode_for_practice=(
            settings_row.thinking_mode_for_practice
            if settings_row else DEFAULT_THINKING_MODE
        ),
        memory_settings=_memory_settings_out(memory_row),
        updated_at=updated_at,
    )


def _find_project(db: Session, user_id, project_id: str) -> Project | None:
    """按 project_id + user_id 查(确保跨用户隔离)

    project_id 是 UUID 字符串,解析失败返回 None(404)。
    """
    try:
        pid = UUID(project_id)
    except (ValueError, TypeError):
        return None
    return (
        db.query(Project)
        .filter(
            Project.id == pid,
            Project.user_id == user_id,
        )
        .first()
    )


def _project_to_out(row: Project) -> ProjectOut:
    """Project ORM → ProjectOut(把 uuid/datetime 序列化为字符串)"""
    return ProjectOut(
        id=str(row.id),
        repo_url_normalized=row.repo_url_normalized,
        repo_url_raw=row.repo_url_raw,
        alias=row.alias,
        note=row.note,
        memory_content=row.memory_content,
        memory_summary=row.memory_summary,
        last_summary_at=row.last_summary_at.isoformat() if row.last_summary_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _regen_memory_summary(db: Session, user_id, memory_content: str) -> str:
    """重新生成精简版项目记忆(PUT 手改后调用,与自动归纳同源)。

    尝试用 env 默认 LLM 生成(>2000 时);LLM 不可用或失败 → generate_memory_summary
    内部兜底硬截断。任何异常都不影响请求,最差返回硬截断串。
    """
    try:
        from app.services.memory_summarize import (
            generate_memory_summary,
            load_memory_settings,
            resolve_memory_llm_client,
        )

        settings_row = load_memory_settings(db, user_id)
        inject_max = (
            settings_row.inject_max_chars if settings_row and settings_row.inject_max_chars
            else 2000
        )
        structured = not (settings_row and settings_row.structure_mode == "freeform")
        thinking_mode = settings_row.thinking_mode if settings_row else "follow"
        try:
            llm, _src = resolve_memory_llm_client(db, user_id)
        except Exception:
            llm = None  # 未配置 env LLM → 走硬截断兜底
        return generate_memory_summary(
            memory_content, llm, max_chars=inject_max,
            thinking_mode=thinking_mode, structured=structured,
        )
    except Exception as e:
        logger.warning("重新生成精简记忆失败,回退硬截断: %s", e)
        # 兜底:直接硬截断
        content = (memory_content or "").strip()
        return content[:2000] if len(content) > 2000 else content
