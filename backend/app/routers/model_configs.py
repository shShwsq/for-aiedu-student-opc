"""模型设置路由(列表式)

用户可配置多个 LLM / Embedding 模型,任务提交时选择一个使用。

端点:
- GET  /models/catalog        厂商与模型清单(无需登录,前端选厂商用)
- GET  /models/configs        当前用户已保存的配置列表(鉴权)
- PUT  /models/configs        保存配置列表(整体替换,鉴权)
- GET  /models/defaults       各任务(模型槽位)的默认模型分配(鉴权,只读聚合)
- PUT  /models/defaults       写入可配槽位(仅注册表 writable 项,鉴权)
- POST /models/llm/test       测试指定 LLM 配置连通性(按 config_id)
- POST /models/embedding/test 测试指定 Embedding 配置连通性(按 config_id)

安全约定见 schemas/settings.py 模块文档。
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.llm.client import LLMClient, _load_catalog
from app.llm.embedding import EmbeddingClient
from app.models.user import User
from app.models.user_llm_config import UserLLMConfig, decrypt_config_api_keys
from app.schemas.model_configs import (
    EmbeddingConfigItemOut,
    LLMConfigItemOut,
    SaveModelsRequest,
    TestRequest,
    TestResponse,
    UserModelsResponse,
)
from app.schemas.model_slots import (
    ModelSlotInfoRowOut,
    ModelSlotOut,
    ModelSlotsResponse,
    SaveModelSlotsRequest,
)
from app.services.model_slots import (
    INFO_ROWS,
    env_default_model,
    set_slot,
    slot_def,
    slot_views,
    user_llm_config_ids,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/models", tags=["models"])


@router.get("/catalog")
def get_catalog() -> dict:
    """厂商与模型清单(前端用于填充厂商下拉、模型下拉)

    返回 models_catalog.json 原文,含 llmProviders 与 embeddingProviders。
    """
    return _load_catalog()


@router.get("/configs", response_model=UserModelsResponse)
def get_my_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserModelsResponse:
    """获取当前用户已保存的模型配置列表(不返回 api_key 原文)"""
    cfg = _get_or_none(db, current_user.id)
    if cfg is None:
        return UserModelsResponse()

    llm_out = [_to_llm_out(c) for c in cfg.llm_configs]
    emb_out = [_to_embedding_out(c) for c in cfg.embedding_configs]
    return UserModelsResponse(llm_configs=llm_out, embedding_configs=emb_out)


@router.put("/configs", response_model=UserModelsResponse)
def save_models(
    req: SaveModelsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserModelsResponse:
    """保存模型配置(整体替换列表)

    api_key 约定:空串 "" 表示保留已存的 key(按 config_id 匹配旧配置),
    非空串表示更新为新 key。新配置项(config_id 不匹配任何旧项)首次保存必须填 api_key。
    """
    cfg = _get_or_none(db, current_user.id)
    if cfg is None:
        cfg = UserLLMConfig(user_id=current_user.id, llm_configs=[], embedding_configs=[])
        db.add(cfg)

    if req.llm_configs is not None:
        cfg.llm_configs = _merge_llm_configs(cfg.llm_configs, req.llm_configs)
    if req.embedding_configs is not None:
        cfg.embedding_configs = _merge_embedding_configs(cfg.embedding_configs, req.embedding_configs)

    db.commit()
    db.refresh(cfg)

    llm_out = [_to_llm_out(c) for c in cfg.llm_configs]
    emb_out = [_to_embedding_out(c) for c in cfg.embedding_configs]
    return UserModelsResponse(llm_configs=llm_out, embedding_configs=emb_out)


# ============================================================
# 任务模型默认(模型槽位聚合页:只读展示 + 可配槽位写入)
# ============================================================


@router.get("/defaults", response_model=ModelSlotsResponse)
def get_model_defaults(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelSlotsResponse:
    """各任务的默认模型分配(一次返回全部槽位 + 只读说明行)

    聚合页本身只读:编辑发生在归属面板(练习设置 / 记忆设置 / 智能体策略),
    那些端点写穿到同一张表,因此这里看到的是同一个真相。
    """
    return _slots_response(db, current_user.id)


@router.put("/defaults", response_model=ModelSlotsResponse)
def save_model_defaults(
    req: SaveModelSlotsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ModelSlotsResponse:
    """写入可配槽位(部分写:`None`=不修改,空串=清空回退环境默认)

    白名单:只有注册表标 `writable` 的槽位能走这里,其余槽位归归属面板端点
    (否则同一个字段会出现两个写入口,谁后写谁赢)。
    归属校验:`llm_config_id` 必须是当前用户已保存的 LLM 配置(与练习/记忆设置同源)。
    """
    owned = user_llm_config_ids(db, current_user.id)
    for slot_id, item in req.slots.items():
        d = slot_def(slot_id)
        if d is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"未知模型槽位: {slot_id}",
            )
        if not d.writable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"「{d.label}」请在 {d.edit_path} 修改",
            )
        if item.thinking_mode is not None and not d.has_thinking_mode:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"「{d.label}」不支持思考模式设置",
            )
        if item.llm_config_id and item.llm_config_id not in owned:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"「{d.label}」模型配置不存在或不属于当前用户",
            )
        # 一次调用写两侧:set_slot 内部 get_or_create,分两次会因 autoflush 关闭而建出重复行
        set_slot(
            db, current_user.id, slot_id,
            llm_config_id=item.llm_config_id,
            thinking_mode=item.thinking_mode,
        )
    db.commit()
    logger.info("用户 %s 更新模型槽位默认: %s", current_user.id, list(req.slots))
    return _slots_response(db, current_user.id)


@router.post("/llm/test", response_model=TestResponse)
def test_llm(
    req: TestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TestResponse:
    """测试指定 LLM 配置连通性(按 config_id,使用已保存的配置)"""
    cfg = _get_or_none(db, current_user.id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="未找到配置")

    # 先解密再查配:密文损坏/密钥轮换时 decrypt 会抛 ValueError,不能冒到 500
    try:
        llm_configs = decrypt_config_api_keys(cfg.llm_configs)
    except ValueError:
        return TestResponse(
            success=False,
            message="api_key 解密失败(加密密钥已轮换或密文损坏),请重新填写并保存 api_key",
        )

    target = _find_config(llm_configs, req.config_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"未找到 LLM 配置: {req.config_id}")

    if not target.get("api_key"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该配置未保存 api_key,请先填写并保存",
        )

    try:
        client = LLMClient.from_config_dict(target)
    except Exception as e:
        return TestResponse(success=False, message=f"客户端构造失败: {e}")

    result = client.test()
    return TestResponse(
        success=result["success"],
        message=result["message"],
        latency_ms=result.get("latency_ms"),
        reply=result.get("reply"),
    )


@router.post("/embedding/test", response_model=TestResponse)
def test_embedding(
    req: TestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TestResponse:
    """测试指定 Embedding 配置连通性(按 config_id,使用已保存的配置)"""
    cfg = _get_or_none(db, current_user.id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="未找到配置")

    # 同 test_llm:解密失败转友好 TestResponse，不冒 500
    try:
        emb_configs = decrypt_config_api_keys(cfg.embedding_configs)
    except ValueError:
        return TestResponse(
            success=False,
            message="api_key 解密失败(加密密钥已轮换或密文损坏),请重新填写并保存 api_key",
        )

    target = _find_config(emb_configs, req.config_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"未找到 Embedding 配置: {req.config_id}")

    if not target.get("api_key"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该配置未保存 api_key,请先填写并保存",
        )

    try:
        client = EmbeddingClient(
            provider_id=target.get("provider", ""),
            api_key=target.get("api_key", ""),
            model=target.get("model", ""),
            base_url=target.get("base_url"),
        )
    except Exception as e:
        return TestResponse(success=False, message=f"客户端构造失败: {e}")

    result = client.test()
    return TestResponse(
        success=result["success"],
        message=result["message"],
        latency_ms=result.get("latency_ms"),
        dimension=result.get("dimension"),
    )


# ============================================================
# 辅助函数
# ============================================================


def _get_or_none(db: Session, user_id) -> UserLLMConfig | None:
    """按 user_id 查 UserLLMConfig(可能不存在)"""
    return db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user_id).first()


def _slots_response(db: Session, user_id) -> ModelSlotsResponse:
    """槽位视图 → API 响应(GET/PUT /models/defaults 共用一份组装)"""
    return ModelSlotsResponse(
        slots=[ModelSlotOut(**v) for v in slot_views(db, user_id)],
        info_rows=[
            ModelSlotInfoRowOut(id=r.id, label=r.label, description=r.description)
            for r in INFO_ROWS
        ],
        env_default_model=env_default_model(),
    )


def _find_config(configs: list[dict], config_id: str) -> dict | None:
    """从配置列表中按 id 查找"""
    for c in configs:
        if c.get("id") == config_id:
            return c
    return None


def _merge_llm_configs(existing: list[dict], new_items) -> list[dict]:
    """合并 LLM 配置列表

    策略:整体替换,但 api_key 为空串时从旧配置(按 id 匹配)保留。
    新项(config_id 不匹配任何旧项)且 api_key 为空 → 报错。
    落库的 api_key 经 enc:v1 信封加密;保留的旧值已是密文,幂等不会二次加密。
    """
    from app.security import encrypt_secret_envelope

    old_map = {c["id"]: c for c in existing if "id" in c}
    result: list[dict] = []
    for item in new_items:
        api_key = item.api_key
        if not api_key:
            old = old_map.get(item.id)
            if old and old.get("api_key"):
                api_key = old["api_key"]
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"配置 '{item.name or item.id}' 缺少 api_key,首次保存必须填写",
                )
        result.append({
            # 防御性兜底:前端在非 Secure Context(http://服务器IP,如 Docker 部署)下
            # crypto.randomUUID 不可用,可能传空串;此处补 UUID 避免落库 id 为空。
            "id": item.id or str(uuid.uuid4()),
            "name": item.name,
            "provider": item.provider,
            "api_key": encrypt_secret_envelope(api_key),
            "model": item.model,
            "enable_thinking": item.enable_thinking,
            "base_url": item.base_url,
            "max_output_tokens": item.max_output_tokens,
        })
    return result


def _merge_embedding_configs(existing: list[dict], new_items) -> list[dict]:
    """合并 Embedding 配置列表(策略同 _merge_llm_configs,api_key 同样 enc:v1 加密)"""
    from app.security import encrypt_secret_envelope

    old_map = {c["id"]: c for c in existing if "id" in c}
    result: list[dict] = []
    for item in new_items:
        api_key = item.api_key
        if not api_key:
            old = old_map.get(item.id)
            if old and old.get("api_key"):
                api_key = old["api_key"]
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"配置 '{item.name or item.id}' 缺少 api_key,首次保存必须填写",
                )
        result.append({
            "id": item.id or str(uuid.uuid4()),
            "name": item.name,
            "provider": item.provider,
            "api_key": encrypt_secret_envelope(api_key),
            "model": item.model,
            "base_url": item.base_url,
            "dimension": item.dimension,
        })
    return result


def _to_llm_out(cfg: dict) -> LLMConfigItemOut:
    """dict → LLMConfigItemOut(剔除 api_key,转 has_api_key)"""
    return LLMConfigItemOut(
        id=cfg.get("id", ""),
        name=cfg.get("name", ""),
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
        enable_thinking=cfg.get("enable_thinking", True),
        base_url=cfg.get("base_url"),
        max_output_tokens=cfg.get("max_output_tokens"),
        has_api_key=bool(cfg.get("api_key")),
    )


def _to_embedding_out(cfg: dict) -> EmbeddingConfigItemOut:
    """dict → EmbeddingConfigItemOut(剔除 api_key,转 has_api_key)"""
    return EmbeddingConfigItemOut(
        id=cfg.get("id", ""),
        name=cfg.get("name", ""),
        provider=cfg.get("provider", ""),
        model=cfg.get("model", ""),
        base_url=cfg.get("base_url"),
        dimension=cfg.get("dimension", 1024),
        has_api_key=bool(cfg.get("api_key")),
    )
