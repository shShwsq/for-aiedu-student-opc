"""模型槽位默认值相关的 Pydantic 模型

对应 /models/defaults 端点(见 app/routers/model_configs.py):
- GET  /models/defaults   全部槽位当前分配 + 只读说明行
- PUT  /models/defaults   部分写(仅注册表中标了 writable 的槽位)

写入语义与既有设置端点一致:字段为 None 表示「本次不修改」,
空串表示「清空」(回退环境默认模型)。
"""
from typing import Literal

from pydantic import BaseModel, Field

from app.models.practice import (
    THINKING_MODE_FOLLOW,
    THINKING_MODE_OFF,
    THINKING_MODE_ON,
)


class ModelSlotOut(BaseModel):
    """一个槽位的展示数据(只读:编辑始终发生在归属面板)"""

    slot: str
    label: str
    # 一句话说明「这里的模型用在哪、优先级如何」
    description: str
    # 编辑入口深链(路由 path,可带 hash)
    edit_path: str
    # 是否允许 PUT /models/defaults 写本槽位
    writable: bool = False
    # 解析链是否有任务级前置(任务自带模型 > 本槽位默认)
    task_override: bool = False
    has_thinking_mode: bool = True
    # 当前分配的模型配置 id(None=未指定,回退环境默认)
    llm_config_id: str | None = None
    # 命中配置的展示名 `名称(厂商 / 模型)`;空串表示未命中
    config_label: str = ""
    thinking_mode: str = THINKING_MODE_FOLLOW
    # slot=命中用户级默认 / env=回退环境默认
    source: str = "env"
    # 配了 id 但配置已被删除(悬空引用):实际生效的是环境默认
    stale: bool = False


class ModelSlotInfoRowOut(BaseModel):
    """非槽位的模型去向说明(AI 润色 / Embedding / 外部 CLI),只读不可配"""

    id: str
    label: str
    description: str


class ModelSlotsResponse(BaseModel):
    """GET /models/defaults 响应"""

    slots: list[ModelSlotOut] = Field(default_factory=list)
    info_rows: list[ModelSlotInfoRowOut] = Field(default_factory=list)
    # 未配置槽位时实际使用的模型名(env 默认),供「环境默认」行显示具体模型
    env_default_model: str = ""


class SaveModelSlotItem(BaseModel):
    """单个槽位的写入项(None=不修改,空串=清空)"""

    llm_config_id: str | None = Field(default=None, max_length=36)
    thinking_mode: Literal[
        THINKING_MODE_FOLLOW, THINKING_MODE_ON, THINKING_MODE_OFF,
    ] | None = None


class SaveModelSlotsRequest(BaseModel):
    """PUT /models/defaults 请求

    slots: {slot_id: 写入项};未列出的槽位不动。
    非 writable 的槽位 id 直接 400(归属面板端点才是它们的写入口)。
    """

    slots: dict[str, SaveModelSlotItem] = Field(default_factory=dict)
