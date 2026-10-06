"""智能体配置路由

用户可配置多种外部 CLI agent(Qoder CLI 等),任务创建时选择一种作为执行器。

端点:
- GET    /agents/types                 已注册的 agent 类型清单(前端渲染配置表单用)
- GET    /agents/configs               当前用户已配置的 agent 列表(鉴权,不含凭据原文)
- GET    /agents/configs/{agent_type}  单个 agent 配置详情(含各字段填写状态)
- PUT    /agents/configs/{agent_type}  保存/更新某 agent 配置(凭证加密存储)
- POST   /agents/configs/{agent_type}/test  测试凭证连通性(SSE 流式推送进度+思考+回答)
- DELETE /agents/configs/{agent_type}  删除某 agent 配置

安全约定(与 model_configs.py 一致):
- 响应绝不回传凭据原文,只返回 has_credentials / credential_status 布尔
- 请求中 secret 字段传空串表示保留已存值,非空表示更新
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.registry import (
    AGENT_REGISTRY,
    get_credential_fields,
    is_registered,
)
from app.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.models.user_agent_config import UserAgentConfig
from app.schemas.agent_configs import (
    AgentConfigDetailOut,
    AgentConfigListResponse,
    AgentConfigOut,
    AgentTypeMeta,
    CredentialField,
    SaveAgentConfigRequest,
)
from app.security import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


# ============================================================
# Agent 类型清单
# ============================================================


@router.get("/types", response_model=list[AgentTypeMeta])
def get_agent_types() -> list[AgentTypeMeta]:
    """返回所有已注册的 agent 类型元数据(前端据此渲染配置表单)

    无需登录即可访问(类型清单不含敏感信息),便于任务创建页展示可选执行器。
    """
    result: list[AgentTypeMeta] = []
    for agent_type, meta in AGENT_REGISTRY.items():
        # 提取 help_url(取第一个 secret 字段的 help_url,或 None)
        help_url = None
        for f in meta.get("credential_fields", []):
            if f.get("help_url"):
                help_url = f["help_url"]
                break

        result.append(AgentTypeMeta(
            agent_type=agent_type,
            display_name=meta.get("display_name", agent_type),
            description=meta.get("description", ""),
            credential_fields=[
                CredentialField(**f) for f in meta.get("credential_fields", [])
            ],
            help_url=help_url,
        ))
    return result


# ============================================================
# 用户配置 CRUD
# ============================================================


@router.get("/configs", response_model=AgentConfigListResponse)
def list_my_configs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentConfigListResponse:
    """获取当前用户已配置的 agent 列表(不含凭据原文)"""
    rows = (
        db.query(UserAgentConfig)
        .filter(UserAgentConfig.user_id == current_user.id)
        .all()
    )
    configs = [_to_out(r) for r in rows]
    return AgentConfigListResponse(configs=configs)


@router.get("/configs/{agent_type}", response_model=AgentConfigDetailOut)
def get_my_config(
    agent_type: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentConfigDetailOut:
    """获取单个 agent 配置详情(含各凭证字段填写状态,不含原文)"""
    _require_registered(agent_type)

    row = _find_config(db, current_user.id, agent_type)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未配置 agent: {agent_type}")

    return _to_detail_out(row)


@router.put("/configs/{agent_type}", response_model=AgentConfigDetailOut)
def save_config(
    agent_type: str,
    req: SaveAgentConfigRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentConfigDetailOut:
    """保存/更新某 agent 配置

    凭证处理:
    - secret 字段:空串表示保留已存值,非空表示更新
    - text 字段:直接用新值(可为空)
    - 首次保存时,required 的 secret 字段必须非空

    凭证加密后整体存入 credentials_encrypted(JSON 密文)。
    """
    _require_registered(agent_type)

    row = _find_config(db, current_user.id, agent_type)
    if row is None:
        row = UserAgentConfig(
            user_id=current_user.id,
            agent_type=agent_type,
            credentials_encrypted="",
            is_active=True,
        )
        db.add(row)

    # 合并凭证
    old_creds = _decrypt_credentials(row.credentials_encrypted)
    new_creds = _merge_credentials(agent_type, old_creds, req.credentials)

    # 加密存储
    row.credentials_encrypted = _encrypt_credentials(new_creds)
    row.is_active = req.is_active

    db.commit()
    db.refresh(row)
    logger.info(
        "用户 %s 更新了 agent 配置 %s(active=%s)",
        current_user.id, agent_type, row.is_active,
    )
    return _to_detail_out(row)


@router.post("/configs/{agent_type}/test")
def test_config(
    agent_type: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """测试 agent 凭证连通性(SSE 流式推送进度 + 思考 + 回答)

    验证凭证有效性后立即销毁临时沙箱,不污染任务执行环境。
    耗时较长(约 10-60s,含沙箱启动 + ACP bridge 就绪 + 握手 + 模型响应),
    改用 SSE 流式推送各阶段进度、模型思考增量、模型回答增量,
    前端可实时显示测试过程,缓解等待焦虑。

    SSE 事件格式:
        event: stage     data: {"type":"stage","data":{"stage":"...","message":"..."}}
        event: thinking  data: {"type":"thinking","data":{"delta":"思考片段"}}
        event: content   data: {"type":"content","data":{"delta":"回答片段"}}
        event: done      data: {"type":"done","data":{"ok":bool,"message":"..."}}
        event: error     data: {"type":"error","data":{"ok":false,"message":"..."}}

    done/error 为终止事件。鉴权/配置检查在流开始前完成(可正常返回 4xx)。
    """
    _require_registered(agent_type)

    # 必须先保存配置才能测试(测试用的是已加密存储的凭证)
    row = _find_config(db, current_user.id, agent_type)
    if row is None or not row.credentials_encrypted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"未配置 {agent_type} 凭证,请先保存配置再测试",
        )

    # 按 agent_type 动态分派到对应的测试函数
    # 约定:流式测试函数名为 test_credential_streaming,与 registry.executor_module 同模块
    meta = AGENT_REGISTRY.get(agent_type) or {}
    module_path = meta.get("executor_module", "")
    if not module_path:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"agent 类型 {agent_type} 未配置 executor_module,暂不支持测试连接",
        )

    try:
        import importlib
        module = importlib.import_module(module_path)
        test_func = getattr(module, "test_credential_streaming", None)
    except ImportError as e:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"agent 类型 {agent_type} 的执行模块加载失败: {e}",
        )

    if test_func is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"agent 类型 {agent_type} 未实现 test_credential_streaming,暂不支持测试连接",
        )

    # 捕获 user_id 和 agent_type,在生成器闭包中使用(避免请求级 db session 复用问题)
    # 注意:db session 在请求结束后会关闭,但 test_credential_streaming 内部
    # 只在开头用 db 加载凭证(同步完成),后续沙箱操作不依赖 db,所以安全。
    user_id = current_user.id
    agent_type_capture = agent_type

    def event_generator():
        """SSE 事件生成器:消费 test_credential_streaming 的 dict 事件"""
        try:
            for event in test_func(db, user_id, agent_type_capture):
                yield _format_test_sse(event)
        except Exception as e:
            logger.exception("[agent_test] 流式测试生成器异常")
            yield _format_test_sse({
                "type": "error",
                "data": {"ok": False, "message": f"测试异常: {e}"},
            })

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx:禁用缓冲,确保实时推送
        },
    )


def _format_test_sse(event: dict) -> str:
    """格式化测试事件为 SSE 字符串

    格式:
        event: <type>
        data: <json>
    """
    event_type = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


@router.delete("/configs/{agent_type}", response_model=AgentConfigListResponse)
def delete_config(
    agent_type: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentConfigListResponse:
    """删除某 agent 配置(整行删除,含凭据)"""
    _require_registered(agent_type)

    row = _find_config(db, current_user.id, agent_type)
    if row is not None:
        db.delete(row)
        db.commit()
        logger.info("用户 %s 删除了 agent 配置 %s", current_user.id, agent_type)

    # 返回剩余列表
    rows = (
        db.query(UserAgentConfig)
        .filter(UserAgentConfig.user_id == current_user.id)
        .all()
    )
    return AgentConfigListResponse(configs=[_to_out(r) for r in rows])


# ============================================================
# 辅助函数
# ============================================================


def _require_registered(agent_type: str) -> None:
    """校验 agent 类型已注册,否则 404"""
    if not is_registered(agent_type):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知的 agent 类型: {agent_type}",
        )


def _find_config(db: Session, user_id, agent_type: str) -> UserAgentConfig | None:
    """按 user_id + agent_type 查配置"""
    return (
        db.query(UserAgentConfig)
        .filter(
            UserAgentConfig.user_id == user_id,
            UserAgentConfig.agent_type == agent_type,
        )
        .first()
    )


def _decrypt_credentials(encrypted: str) -> dict:
    """解密 credentials_encrypted,返回 dict

    空串/损坏返回空 dict(不抛错,让上层按"未配置"处理)
    """
    if not encrypted:
        return {}
    try:
        plaintext = decrypt_secret(encrypted)
        data = json.loads(plaintext)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"agent 凭证解密失败(按空处理): {e}")
        return {}


def _encrypt_credentials(creds: dict) -> str:
    """加密 dict,返回 base64 密文"""
    if not creds:
        return ""
    plaintext = json.dumps(creds, ensure_ascii=False)
    return encrypt_secret(plaintext)


def _is_field_visible(fdef: dict, effective: dict) -> bool:
    """判断字段在给定生效凭证下是否可见(基于 visible_when)

    visible_when 形如 {"auth_mode": "chatgpt"}:仅当 effective["auth_mode"]=="chatgpt" 时可见。
    无 visible_when 则始终可见。生效值取不到(字段缺失)按不满足处理。
    """
    cond = fdef.get("visible_when")
    if not cond:
        return True
    return all(effective.get(k) == v for k, v in cond.items())


def _merge_credentials(
    agent_type: str,
    old_creds: dict,
    new_values: list,
) -> dict:
    """合并凭证(策略同 model_configs._merge_llm_configs,并支持条件显隐字段)

    - 先算生效 auth_mode(本次提交优先,缺省回退旧值),用于判定字段可见性
    - 不可见字段:不写入结果(切模式即清除另一模式的残留密钥,如 chatgpt 模式丢弃 api_key)
    - secret 字段:空串 → 保留旧值;非空 → 更新;首次 required 空串 → 报错
    - text 字段:直接用新值(可为空)
    """
    field_defs = get_credential_fields(agent_type)
    field_map = {f["key"]: f for f in field_defs}

    # 构建 new_values 的 lookup
    new_map = {v.key: v.value for v in new_values}

    # 生效凭证(用于 visible_when 判定):auth_mode 属非 secret,本次提交优先,否则旧值
    effective: dict = dict(old_creds)
    am_def = field_map.get("auth_mode")
    if am_def is not None:
        submitted_am = new_map.get("auth_mode", "")
        effective["auth_mode"] = submitted_am or old_creds.get("auth_mode", "") or am_def.get("default", "")

    result: dict = {}
    for fdef in field_defs:
        key = fdef["key"]
        ftype = fdef.get("type", "secret")

        # 不可见字段直接跳过:既不写入新值,也不保留旧值(切模式即清除另一模式密钥)
        if not _is_field_visible(fdef, effective):
            continue

        new_val = new_map.get(key, "")

        if ftype == "secret":
            if new_val:
                # 非空:更新为新值
                result[key] = new_val
            else:
                # 空串:保留旧值
                old_val = old_creds.get(key, "")
                if old_val:
                    result[key] = old_val
                elif fdef.get("required"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"字段 '{fdef.get('label', key)}' 首次保存必须填写",
                    )
                # 非必填且空:不存
        else:
            # text 类型:直接用新值
            result[key] = new_val

    return result


def _to_out(row: UserAgentConfig) -> AgentConfigOut:
    """row → AgentConfigOut(不含凭据原文)"""
    creds = _decrypt_credentials(row.credentials_encrypted)
    has_creds = any(bool(v) for v in creds.values())
    meta = AGENT_REGISTRY.get(row.agent_type, {})
    return AgentConfigOut(
        agent_type=row.agent_type,
        display_name=meta.get("display_name", row.agent_type),
        is_active=row.is_active,
        has_credentials=has_creds,
    )


def _to_detail_out(row: UserAgentConfig) -> AgentConfigDetailOut:
    """row → AgentConfigDetailOut(含各字段填写状态 + 非 secret 字段回显值)"""
    creds = _decrypt_credentials(row.credentials_encrypted)
    meta = AGENT_REGISTRY.get(row.agent_type, {})
    field_defs = get_credential_fields(row.agent_type)

    # 各字段的填写状态 + 非 secret 字段回显值
    credential_status: dict[str, bool] = {}
    credential_values: dict[str, str] = {}
    has_any = False
    for fdef in field_defs:
        key = fdef["key"]
        ftype = fdef.get("type", "secret")
        val = creds.get(key, "")
        filled = bool(val)
        credential_status[key] = filled
        if filled:
            has_any = True
            # 非 secret 字段(text/select)回传已配置的值,供前端编辑时回显
            # secret 字段绝不回传
            if ftype != "secret":
                credential_values[key] = val

    return AgentConfigDetailOut(
        agent_type=row.agent_type,
        display_name=meta.get("display_name", row.agent_type),
        is_active=row.is_active,
        has_credentials=has_any,
        credential_status=credential_status,
        credential_values=credential_values,
    )
