"""共享对话落库 + SSE 推送(原四处各写一份,收敛为单一实现)

原状:react_agent / orchestrator / acp_base 各有一个 `_add_conversation`,
agent2 的只读/引用核查工具内联落库块(自带一份推送格式),verifier 又是
一份(且缺 id / tool_call_id 配对,前端无法把 result 与 call 配对展示)。
推送 payload 此前也有两种形状(带/不带 reasoning、带/不带 tool_call_id),
现统一为超集形状,前端兼容(缺省字段为 None)。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.models.task import Conversation, Task


def record_conversation(
    db: Session,
    task: Task,
    *,
    round_idx: int,
    role: str,
    type: str,
    content: str,
    reasoning: str | None = None,
    tool_call_id: str | None = None,
    publish_event: bool = True,
    extra_payload: dict[str, Any] | None = None,
    stream_conv_id: str | None = None,
) -> Conversation:
    """落库一条对话(带 round_idx),可选推送 conversation 事件给前端 SSE

    - publish_event=False:只落库不推事件。思考类不要用——曾经为了"流式卡片
      已展示"而省略推送,结果中途离开详情页再回来的订阅者既收不到增量
      (thinking_delta 不入总线历史)也收不到这条记录,只能等整页快照,
      看上去就像"思考没保存"。实时卡片改由前端按 stream_conv_id/文本对账退役。
    - tool_call_id:仅 type=tool_result 用,对应 tool_call 记录的 id,
      前端据此配对展示(并行调用时 result 按完成顺序落库,不紧跟 call)
    - extra_payload:随事件附加的字段(如 verifier 的 verify=True)
    - stream_conv_id:仅 type=thinking 用,落库前那次流式 thinking_delta 的
      conv_id(瞬态字段,不入 DB)。前端按它把"实时流式卡片"退役成只读历史
      卡片,避免同一思考在对话流里出现两次(卡片 + 落库记录)。
      落库时必须推 conversation 事件:thinking_delta 是高频瞬时事件,事件总线
      不缓存(见 event_bus.publish),刷新/中途离开页面再回来的订阅者只能靠
      本事件或 GET /tasks/{id} 快照拿到这条记录 —— 不推就会"看着没保存"。

    返回创建的 Conversation 对象(供调用方拿 id 做后续关联/更新)。
    """
    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role=role,
        type=type,
        content=content,
        reasoning=reasoning,
        tool_call_id=tool_call_id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    if publish_event:
        publish_conversation(
            task, conv,
            extra_payload=extra_payload, stream_conv_id=stream_conv_id,
        )
    return conv


def publish_conversation(
    task: Task,
    conv: Conversation,
    *,
    extra_payload: dict[str, Any] | None = None,
    stream_conv_id: str | None = None,
) -> None:
    """把已落库的对话记录推给前端 SSE(消费时刻入流 / 多端同步场景)"""
    data: dict[str, Any] = {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "tool_call_id": conv.tool_call_id,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "stream_conv_id": stream_conv_id,
    }
    if extra_payload:
        data.update(extra_payload)
    publish(task.id, "conversation", data)
