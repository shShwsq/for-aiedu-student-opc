"""模型槽位(model slot):一处 LLM 任务 ↔ 一条默认模型配置

一处的模型选择就是一个「槽位」(任务审查 / 出题 / 讲解 / 记忆归纳),行存
model_slot_defaults(user_id, slot, llm_config_id, thinking_mode)。

本模块是槽位默认值的**唯一读写入口**,替掉此前散在 orchestrator /
practice.generator / memory_summarize 里的四份「按 config id 造 LLMClient,
否则 env 默认」实现:
- 配置 id 与思考模式的读取顺序在这里定(新表行 > 旧列兜底)
- 任何一级失配(配置被删 / 加载异常)一律回退下一级,最终 env 默认,不抛异常
- 命中来源(source)随返回值带出,供日志与「本次将使用:xxx」展示
- 任务级前置(task.llm_config_id / force_default_llm 反转)留在调用点组装:只有那里
  才拿得到任务对象与开关,本模块不猜优先级

写入约定(与既有设置端点一致):`None`=本次不修改,空串 `""`=清空(回退 env)。
清空在行上落成空串标记而不是 NULL(见 `CLEARED`):启动迁移只在目标行
`llm_config_id IS NULL` 时用旧列覆盖,写 NULL 会让每次重启把用户刚清掉的模型刷回来。
过渡期兜底:槽位行没做过模型决定(NULL)时才读旧列值(practice_settings /
memory_settings 上的模型列,已被 migrate_model_slot_defaults 拷入本表且不再被写入);
调用方手里已有那行时可用 legacy_value 省一次查询。旧列下线后 `legacy_for`
与相关分支一并删除。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.memory_settings import MemorySettings
from app.models.model_slot import (
    MODEL_SLOT_IDS,
    SLOT_MEMORY_CURATOR,
    SLOT_PRACTICE_EXPLAIN,
    SLOT_PRACTICE_GENERATE,
    SLOT_REVIEW_DEFAULT,
    ModelSlotDefault,
)
from app.models.practice import DEFAULT_THINKING_MODE, THINKING_MODES, PracticeSettings
from app.models.user_llm_config import UserLLMConfig

logger = logging.getLogger(__name__)

# 「未传」与「显式传 None」要区分开(前者表示让本模块自己去取旧列)
_MISSING = object()

# 槽位行上「用户显式清空过模型」的标记:llm_config_id 存空串而不是 NULL。
# 为什么不能用 NULL 表示清空:迁移的 `ON CONFLICT ... DO UPDATE ... WHERE
# llm_config_id IS NULL` 把 NULL 当成「这行还没从旧列交接完」的判据,于是下一次
# 重启又用旧列值把清空过的槽位填回去 —— 用户在 UI 上永远取消不掉这个设置。
# 空串两边都满足:不是 NULL(迁移不动它),也不是合法配置 id(读路径视为未设置)。
CLEARED = ""

# 解析来源标识(日志与前端展示共用口径)
SOURCE_SLOT = "slot"        # 命中用户级槽位默认
SOURCE_ENV = "env"          # 回退环境默认(.env LLM_*)


# ============================================================
# 槽位注册表(新增一处任务 = 加一条 SlotDef + 一个 slot 常量)
# ============================================================


@dataclass(frozen=True)
class SlotDef:
    """槽位元数据(前端「任务模型默认」页与写入白名单都从这里取)"""

    id: str
    label: str
    # 一句话说明「这里配的模型用在哪、优先级如何」,展示在 HelpPopover
    description: str
    # 编辑入口(路由 path,可带 hash);聚合页只读,编辑始终发生在归属面板
    edit_path: str
    # 是否允许 PUT /models/defaults 写本槽位(其余槽位由归属面板端点写穿)
    writable: bool = False
    # 解析链是否有任务级前置(任务自带模型 > 本槽位默认)
    task_override: bool = False
    # 是否有思考模式覆盖(出题/归纳有;讲解沿用其宿主设置)
    has_thinking_mode: bool = True


MODEL_SLOTS: dict[str, SlotDef] = {
    SLOT_REVIEW_DEFAULT: SlotDef(
        id=SLOT_REVIEW_DEFAULT,
        label="任务默认审查模型",
        description=(
            "提交任务时没选模型的情况下,检查助手(评估)与内置 AI 助手(执行)使用的默认模型;"
            "任务自带模型优先,未设置则用环境默认。外部 CLI 执行器不受影响(模型由 CLI 自管)。"
        ),
        edit_path="/settings/policy",
        writable=True,
        task_override=True,
        has_thinking_mode=False,
    ),
    SLOT_PRACTICE_GENERATE: SlotDef(
        id=SLOT_PRACTICE_GENERATE,
        label="默认出题模型",
        description=(
            "生成练习题默认用的模型(手动/自动出题都生效);"
            "开启「始终用默认出题模型」后连任务自带配置也让位给它。"
        ),
        edit_path="/settings/practice#generation",
        task_override=True,
    ),
    SLOT_PRACTICE_EXPLAIN: SlotDef(
        id=SLOT_PRACTICE_EXPLAIN,
        label="知识点讲解模型",
        description=(
            "看板按需生成知识点讲解用的模型;讲解是轻任务,可以挂比出题快/便宜的模型。"
            "未设置时沿用默认出题模型。"
        ),
        edit_path="/settings/practice#generation",
        has_thinking_mode=False,
    ),
    SLOT_MEMORY_CURATOR: SlotDef(
        id=SLOT_MEMORY_CURATOR,
        label="记忆归纳模型",
        description=(
            "任务完成后归纳/精简长期记忆用的模型,独立于任务自带模型"
            "(归纳是轻量任务,适合选便宜快速的模型)。"
        ),
        edit_path="/settings/memory",
    ),
}

# 展示顺序(聚合页按此顺序渲染)
SLOT_ORDER = (
    SLOT_REVIEW_DEFAULT,
    SLOT_PRACTICE_GENERATE,
    SLOT_PRACTICE_EXPLAIN,
    SLOT_MEMORY_CURATOR,
)


@dataclass(frozen=True)
class SlotInfoRow:
    """非槽位的模型去向说明(聚合页只读展示,不可配置)"""

    id: str
    label: str
    description: str


INFO_ROWS: tuple[SlotInfoRow, ...] = (
    SlotInfoRow(
        id="prompt_polish",
        label="AI 润色",
        description=(
            "任务描述润色是单轮轻调用,固定走环境默认模型并强制关闭思考;"
            "暂不支持按用户配置。"
        ),
    ),
    SlotInfoRow(
        id="embedding",
        label="Embedding 向量模型",
        description="当前仅保存与测试连通性,尚未接入向量检索流程,故无默认槽位可配。",
    ),
    SlotInfoRow(
        id="cli_agent",
        label="外部 CLI 执行器",
        description=(
            "Qoder / DeepSeek / Codex 等外部 CLI 的模型由各家 CLI 自管,"
            "在「CLI 设置」里配置,与这里的模型配置不是同一命名空间。"
        ),
    ),
)


def slot_def(slot: str) -> SlotDef | None:
    """按 id 取槽位定义(未知 id 返回 None,由调用方拒绝)"""
    return MODEL_SLOTS.get(slot)


def known_slots() -> tuple[str, ...]:
    return MODEL_SLOT_IDS


# ============================================================
# 读:槽位行 / 配置 id / 思考模式
# ============================================================


def get_slot_row(db: Session, user_id, slot: str) -> ModelSlotDefault | None:
    """取槽位行(无行/匿名/查询异常 → None,调用方回退下一级)"""
    if user_id is None or not slot:
        return None
    try:
        return (
            db.query(ModelSlotDefault)
            .filter(
                ModelSlotDefault.user_id == user_id,
                ModelSlotDefault.slot == slot,
            )
            .first()
        )
    except Exception as e:  # noqa: BLE001 - 读槽位失败不阻断业务
        logger.warning("[model_slot] 读取 slot=%s 失败(回退下一级): %s", slot, e)
        return None


def _clean_id(value) -> str | None:
    """配置 id 只接受非空字符串(容忍 mock/脏值,一律视为未设置)"""
    if isinstance(value, str) and value.strip():
        return value
    return None


def legacy_for(db: Session, user_id, slot: str) -> tuple[str | None, str | None]:
    """过渡期兜底:取该槽位在旧业务表上的 (配置 id, 思考模式)

    新表无行时才用到(见 get_slot_config_id / get_slot_thinking_mode)。
    """
    if user_id is None:
        return None, None
    if slot in (SLOT_PRACTICE_GENERATE, SLOT_PRACTICE_EXPLAIN):
        row = _legacy_row(db, PracticeSettings, user_id)
        if row is None:
            return None, None
        if slot == SLOT_PRACTICE_GENERATE:
            return row.default_llm_config_id, row.thinking_mode_for_practice
        return row.explain_llm_config_id, None
    if slot == SLOT_MEMORY_CURATOR:
        row = _legacy_row(db, MemorySettings, user_id)
        if row is not None:
            return row.curator_llm_config_id, row.thinking_mode
    return None, None


def _legacy_row(db: Session, model, user_id):
    try:
        return db.query(model).filter(model.user_id == user_id).first()
    except Exception as e:  # noqa: BLE001 - 兜底读失败不影响解析链
        logger.warning("[model_slot] 读取旧设置列失败(%s): %s", model.__tablename__, e)
        return None


def get_slot_config_id(
    db: Session, user_id, slot: str, *, legacy_value=_MISSING,
) -> str | None:
    """槽位配置的 id:新表行优先,行上没做过模型决定时才回退旧列值

    行上的三种值含义不同,不能混:
    - 非空字符串 = 用户选的或迁移拷来的配置 id → 以它为准
    - 空串 `CLEARED` = 用户显式清空过 → 不回退旧列,返回 None(解析链落到 env 默认)
    - NULL = 这行没做过模型决定(只为思考模式建的行 / 迁移还没跑)→ 继续回退旧列,
      保住「迁移没跑时用户旧设置仍生效」这条安全网

    legacy_value 由已持有归属表行的调用方传入,省一次查询;不传则本函数自取。
    """
    row = get_slot_row(db, user_id, slot)
    raw = getattr(row, "llm_config_id", None) if row is not None else None
    if raw == CLEARED:
        return None
    config_id = _clean_id(raw)
    if config_id:
        return config_id
    if legacy_value is not _MISSING:
        return _clean_id(legacy_value)
    return _clean_id(legacy_for(db, user_id, slot)[0])


def get_slot_thinking_mode(
    db: Session, user_id, slot: str, *, legacy_value=_MISSING,
) -> str:
    """槽位思考模式(follow/on/off):新表行优先,无行时回退旧列值"""
    row = get_slot_row(db, user_id, slot)
    mode = getattr(row, "thinking_mode", None) if row else None
    if mode is None or not isinstance(mode, str) or mode not in THINKING_MODES:
        if legacy_value is _MISSING:
            legacy_value = legacy_for(db, user_id, slot)[1]
        mode = legacy_value
    return mode if isinstance(mode, str) and mode in THINKING_MODES else DEFAULT_THINKING_MODE


def find_llm_config(db: Session, user_id, config_id: str) -> dict | None:
    """按 config id 在用户的 UserLLMConfig.llm_configs 里找配置 dict(找不到 None)"""
    if user_id is None or not config_id:
        return None
    try:
        cfg_row = (
            db.query(UserLLMConfig)
            .filter(UserLLMConfig.user_id == user_id)
            .first()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[model_slot] 加载用户模型配置失败(id=%s): %s", config_id, e)
        return None
    for c in ((cfg_row.llm_configs or []) if cfg_row else []):
        if isinstance(c, Mapping) and c.get("id") == config_id:
            return c
    return None


def user_llm_config_ids(db: Session, user_id) -> set[str]:
    """当前用户已保存的 LLM 配置 id 集合(写入端点做归属校验用)"""
    try:
        cfg_row = (
            db.query(UserLLMConfig)
            .filter(UserLLMConfig.user_id == user_id)
            .first()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[model_slot] 加载用户模型配置 id 失败: %s", e)
        return set()
    return {
        c.get("id") for c in ((cfg_row.llm_configs or []) if cfg_row else [])
        if isinstance(c, Mapping) and c.get("id")
    }


# ============================================================
# 解析:槽位 → LLMClient(带来源)
# ============================================================


def resolve_slot_client(
    db: Session, user_id, slot: str, *, legacy_value=_MISSING, label: str = "",
) -> tuple[LLMClient, str]:
    """解析槽位模型:槽位默认 > env 默认

    返回 (client, source),source ∈ {slot, env}。配置 id 失效(已被删)或构造
    失败只记日志并回退 env,绝不抛出 —— 模型选择是增益,不能拖垮主流程。

    任务级前置(如 task.llm_config_id / force_default_llm 反转)留在调用点组装
    (orchestrator._build_llm_client、generator.resolve_generate_client):那里才能拿到
    任务对象与出题开关,本函数不猜优先级。
    """
    config_id = get_slot_config_id(db, user_id, slot, legacy_value=legacy_value)
    if config_id and user_id is not None:
        cfg = find_llm_config(db, user_id, config_id)
        if cfg is not None:
            try:
                return LLMClient.from_config_dict(cfg), SOURCE_SLOT
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[model_slot] 构造客户端失败 slot=%s id=%s,回退 env 默认: %s",
                    slot, config_id, e,
                )
        else:
            logger.warning(
                "[model_slot] 未找到 slot=%s%s 的配置 id=%s,回退 env 默认",
                slot, f"({label})" if label else "", config_id,
            )
    return LLMClient(), SOURCE_ENV


# ============================================================
# 写:get_or_create 槽位行(调用方 commit)
# ============================================================


def set_slot(
    db: Session, user_id, slot: str, *,
    llm_config_id: str | None = None,
    thinking_mode: str | None = None,
) -> ModelSlotDefault | None:
    """写入槽位(None=本次不修改,空串=清空回退 env),返回行(可能为 None)

    不 commit:与既有设置端点一样由调用方在同一事务里提交。
    行不存在且两侧都不写时不凭空建行(返回 None)。
    清空写成 `CLEARED` 空串标记而不是 NULL(见常量注释),否则会被启动迁移刷回旧列。
    """
    if user_id is None:
        return None
    row = get_slot_row(db, user_id, slot)
    if row is None:
        if llm_config_id is None and thinking_mode is None:
            return None
        row = ModelSlotDefault(user_id=user_id, slot=slot)
        db.add(row)
        # autoflush 关闭:不 flush 的话同一事务内第二次 set_slot 同一槽位仍读不到这行,
        # 会再建一行并在 commit 时撞 uq_model_slot_user_slot。建行后立刻 flush 一次,
        # 把原先只写在调用点注释里的「一次调用写两侧」升级成代码保证。
        db.flush()
    if llm_config_id is not None:
        row.llm_config_id = llm_config_id or CLEARED
    if thinking_mode is not None and thinking_mode in THINKING_MODES:
        row.thinking_mode = thinking_mode
    return row


# ============================================================
# 展示:槽位当前生效的配置(聚合页与「本次将使用」用)
# ============================================================


def config_label(cfg: dict | None) -> str:
    """配置 dict → 展示名,口径与前端下拉一致:`名称(厂商 / 模型)`"""
    if not cfg:
        return ""
    name = (cfg.get("name") or "").strip()
    provider = (cfg.get("provider") or "").strip()
    model = (cfg.get("model") or "").strip()
    title = name or model or "未命名配置"
    return f"{title}({provider} / {model})" if (provider or model) else title


def env_default_model() -> str:
    """环境默认模型名(未配置槽位时的实际落点,展示用)"""
    from app.config import settings

    return getattr(settings, "LLM_MODEL", "") or ""


def slot_views(db: Session, user_id) -> list[dict]:
    """按注册表顺序组装全部槽位的展示数据(GET /models/defaults)

    - `source`: slot=已命中配置 / env=回退环境默认
    - `stale`: 配了 id 但配置已被删(悬空引用)——前端据此提示“当前实际用环境默认”,
      避免用户以为还在用那个模型(解析链本身就是静默回退)
    """
    views: list[dict] = []
    for slot_id in SLOT_ORDER:
        d = MODEL_SLOTS[slot_id]
        config_id = get_slot_config_id(db, user_id, slot_id)
        cfg = find_llm_config(db, user_id, config_id) if config_id else None
        views.append({
            "slot": d.id,
            "label": d.label,
            "description": d.description,
            "edit_path": d.edit_path,
            "writable": d.writable,
            "task_override": d.task_override,
            "has_thinking_mode": d.has_thinking_mode,
            "llm_config_id": config_id,
            "config_label": config_label(cfg),
            "thinking_mode": (
                get_slot_thinking_mode(db, user_id, slot_id)
                if d.has_thinking_mode else DEFAULT_THINKING_MODE
            ),
            "source": SOURCE_SLOT if cfg is not None else SOURCE_ENV,
            "stale": bool(config_id) and cfg is None,
        })
    return views
