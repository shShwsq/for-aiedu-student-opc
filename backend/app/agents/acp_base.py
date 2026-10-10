"""ACP 基础设施:共享的 ACP 客户端、bridge 管理、事件收集、凭证加载等

被 qoder_cli_agent.py / deepseek_cli_agent.py / codex_cli_agent.py 复用,避免重复实现。
所有函数均通过 agent_type 参数支持多种 CLI(qoder_cli / deepseek_cli / codex_cli),
各 CLI 的差异(启动参数、认证方式、模型选择方式)由 registry 配置 + wrapper 层回调处理。

工作流程(通用):
1. 复用 sandbox_tools 的沙箱会话(orchestrator 已预 clone 仓库)
2. 查本任务缓存的 bridge + ACP session(命中则直接跳到 7;bridge/CLI 进程驻留
   沙箱,会话上下文随 session 在轮次/追问间自然延续)
3. 未命中:从 user_agent_configs 加载用户凭证(加密存储),经 registry 映射为环境变量
4. 将 acp_bridge.py 写入沙箱/local 临时目录,后台启动
   (sandbox:沙箱内固定 ACP_BRIDGE_PORT;local:宿主机动态端口),凭证经 envs 注入
5. 通过 get_endpoint(port) 获取转发地址 + headers(local 直接 127.0.0.1:port)
6. ACP 客户端:initialize → 打开会话(优先恢复 CLI 自己落盘的会话
   session/resume / session/load,否则 session/new)→ [post_session_setup],随后写入缓存
7. session/prompt 流式接收 session/update 通知,翻译为 event_bus 事件
8. 收集最终 summary,提取 plan,返回 (results, summary, plan)

bridge 不在每轮结束时停止:进程随沙箱会话存活,供后续轮次/resume 追问复用
(省去 ensure_cli_env/start_bridge/initialize/new_session 合计 ~25s);
沙箱会话销毁(close_session)时经 stop_task_bridge 清缓存,容器销毁连带回收进程。

上下文注入三个约定(详见 _resolve_injection_plan / _load_history_replay / _session_has_context):
- 走全新链路(新 session)的追问轮会把之前轮次执行记录回放给 CLI
  —— 新 session 看不到上一轮的对话,否则只剩"基于之前的执行进度"无从续接
- 复用同一 session 时,未变化的注入段(仓库上下文/记忆段)不重发,避免重复占 token
- 只有 prompt 成功返回才记"该 session 已收下上下文";上一轮业务性失败留下的空
  session 会被当作新 session 全量注入 + 回放(否则历史永不补发)

会话恢复(保真度优于文本回放,两者互补,详见 runtime/acp_session.py):
- prompt 成功后把 sessionId/cwd/注入段指纹/round_idx 持久化到 task.params["_acp_session"]
- 重建链路时,若 CLI 在 initialize 里声明了恢复能力且 bridge 会排空回放通知
  (bridge_protocol >= 2),先恢复会话(历史由 CLI 从磁盘 transcript 复原);
  恢复时通知一律丢弃,尾巴由 bridge 排空,否则会污染本轮落库
- 恢复/复用的会话只覆盖到 last_accepted_round:其后失败轮的提问进了 DB 却
  没进 transcript(重试消息不复述原提问),按 last_accepted_round **增量回放**
  补发缺失轮次;截断兜底轮不推进记录(内容可能没落 CLI 磁盘)
- 恢复失败/能力不具备(如 codex_bridge)→ session/new + 全量文本回放,行为与原先一致;
  恢复仅因 JSON-RPC 业务错误清持久化记录,传输层瞬时失败保留记录下轮再试

各 wrapper 的差异通过回调/参数注入:
- post_session_setup(client, session_id, task):session/new 之后、prompt 之前执行
  (deepseek 用此回调调 set_config_option 设置模型/思考强度)
- credential_env_builder(credentials, task):动态构建凭证环境变量
  (deepseek 用此回调按命令确认模式注入 DSH_PERMISSION_MODE)
- test_acp_args:测试连接时额外的 CLI 参数
  (qoder 用 ["--model","DeepSeek-V4-Flash","--reasoning-effort","low"])
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from json_repair import repair_json
from sqlalchemy.orm import Session

from app.agents.registry import get_agent_meta, get_sandbox_config
# 对话落库 + SSE 推送:runtime 统一实现(与 react_agent / agent2 / verifier
# 同源;别名保持 _add_conversation 模块名,存量 monkeypatch 兼容面不变)
from app.agents.runtime.conversation import record_conversation as _add_conversation
# ACP 会话持久化与恢复决策(依赖 CLI 自己落盘的会话,见该模块 docstring)
from app.agents.runtime.acp_session import (
    build_session_record,
    clear_session_record,
    load_session_record,
    plan_session_open,
    save_session_record,
)
from app.config import settings
from app.event_bus import publish
from app.models.task import Conversation, Task
from app.models.user_agent_config import UserAgentConfig
from app.perf import perf_log
# CLI prompt 文案资产与上传检测(纯函数),集中管理于 app/prompts/
from app.prompts.executor import (
    FOLLOWUP_CORE_GUIDANCE,
    _has_creation_upload,
    build_cli_history_replay_section,
    build_cli_memory_section,
    build_cli_repo_context_section,
    build_cli_skills_section,
    build_first_round_question,
    format_plan_reminder,
)
from app.security import decrypt_secret
from app.tools import sandbox_tools
from app.tools.schema import set_current_task
from app.user_interaction import request_command_confirm, wait_for_command_confirm

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# ACP bridge 监听端口(沙箱内)
ACP_BRIDGE_PORT = 8088

# 沙箱内文件路径
BRIDGE_SCRIPT_PATH = "/home/user/.acp/acp_bridge.py"
BRIDGE_WORK_DIR = "/home/user"

# bridge 启动超时(秒):等待 CLI 进程就绪 + HTTP 服务监听
BRIDGE_STARTUP_TIMEOUT = 30
BRIDGE_HEALTH_INTERVAL = 1.0

# ACP 协议版本(数字 1,见 https://github.com/agentclientprotocol/agent-client-protocol
# "The current stable ACP protocol version is 1.")
ACP_PROTOCOL_VERSION = 1

# 本地 bridge 源文件路径(用于写入沙箱)
# 支持 per-agent 自定义 bridge:registry sandbox.bridge_script 指定使用哪个
_BRIDGE_SOURCES = {
    "acp_bridge": Path(__file__).parent / "acp_bridge.py",  # 通用 ACP stdio 桥接(qoder/deepseek)
    "codex_bridge": Path(__file__).parent / "codex_bridge.py",  # Codex 专用(codex exec --json → ACP 翻译)
}
# 默认 bridge(acp_bridge.py,通用 ACP stdio 桥接)
_DEFAULT_BRIDGE = "acp_bridge"

# 默认 ACP 日志目录:backend/logs/acp/
_ACP_LOG_DIR = Path(__file__).resolve().parents[2] / "logs" / "acp"

# idle 看门狗轮询间隔(秒):queue.get 超时粒度,也是 idle 检查粒度
_IDLE_POLL_SECONDS = 5.0

# local 模式 bridge 动态端口分配:宿主机端口空间全局共享,
# 固定 ACP_BRIDGE_PORT(8088)在并发 CLI 任务间会冲突。
# 已分配端口集合防同进程内两次探测撞车(bind(0) 关闭后可能返回同一端口);
# 进程生命周期内不回收(dev 场景任务量级小,重启即重置)
_used_bridge_ports: set[int] = set()
_used_ports_lock = threading.Lock()


# ============================================================
# 异步后台子 Agent(Qoder Agent 工具)提前收尾的结果回收
# ============================================================

# Agent 工具 completed 回执里表明"子 Agent 已后台派生、结果稍后回报"的标记
_ASYNC_AGENT_LAUNCH_RE = re.compile(
    r"Async agent launched|working in the background", re.IGNORECASE
)

# 本轮 final 文本"业务未完成、仍在等后台结果"的口吻(中英文)。命中且本轮有
# 异步派生 → 判为提前 end_turn,触发同 session 续轮回收。
_PENDING_ASYNC_REPORT_RE = re.compile(
    r"(等待|稍后|尚未|未收到|还没|之后.{0,8}(报告|结果|返回|给)"
    r"|will (deliver|report|give|provide|share)"
    r"|still waiting|waiting for|after they (finish|complete)"
    r"|later report|report after)",
    re.IGNORECASE,
)

# 续轮补发给 CLI 的指令:让其交付已完成子 Agent 的结果(不伪造内容)
_ASYNC_AGENT_CONTINUE_PROMPT = (
    "The background sub-agents you launched earlier should have finished by now. "
    "Output their COMBINED full findings now as your final answer. "
    "Do not launch new sub-agents. If a sub-agent is genuinely still running, "
    "report only what is ready and state which parts are still pending."
)


def _async_agent_autoccontinue_enabled(agent_type: str) -> bool:
    """本轮 executor 是否启用异步子 Agent 续轮回收(全局开关 + 类型白名单)。"""
    if not getattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", False):
        return False
    allowed = {
        t.strip()
        for t in (getattr(settings, "ACP_ASYNC_AGENT_TYPES", "") or "").split(",")
        if t.strip()
    }
    return agent_type in allowed


def _looks_like_pending_async_report(text: str) -> bool:
    """final 文本是否呈现"仍在等后台子 Agent 结果"的未完成口吻。"""
    if not text:
        return False
    return bool(_PENDING_ASYNC_REPORT_RE.search(text))


def _async_agent_collect_results(
    client: "ACPClient",
    acp_session_id: str,
    collector: "_ACPCollector",
    ctx: dict,
    *,
    task: Task,
    round_idx: int,
    agent_type: str,
) -> None:
    """在同一活跃 ACP session 上带退避地补发续轮 prompt,回收后台子 Agent 结果。

    - 复用同一 client + session_id + collector:续轮内容继续累积进 content_full
      并流式推前端,收尾时并入本轮 summary(不另起 conv/round)。
    - 停止条件:补发轮不再呈"在等结果"口吻(视为已回收)/ 达最大次数 /
      连接或 idle 兜底异常。
    - ctx["still_pending"]:循环结束时是否仍呈未完成口吻(供上层标注结果可能不全)。
    """
    max_continue = int(getattr(settings, "ACP_ASYNC_AGENT_MAX_CONTINUE", 3) or 0)
    cur_wait = float(getattr(settings, "ACP_ASYNC_AGENT_CONTINUE_WAIT_SECONDS", 20) or 0)
    factor = float(getattr(settings, "ACP_ASYNC_AGENT_CONTINUE_BACKOFF_FACTOR", 1.5) or 1.5)

    last_new_text = ""
    for attempt in range(1, max_continue + 1):
        # 给后台子 Agent 完成时间(auto_renew 后台线程期间持续续期沙箱 TTL)
        if cur_wait > 0:
            time.sleep(cur_wait)
        prev_len = len(collector.content_full)
        try:
            client.prompt(
                acp_session_id,
                [{"type": "text", "text": _ASYNC_AGENT_CONTINUE_PROMPT}],
                on_event=collector,
                idle_probe=lambda: collector.has_active_tools,
            )
        except (httpx.HTTPError, ConnectionError, PromptIdleTimeout, ACPStreamAborted) as e:
            logger.warning(
                f"[task={task.id}] {agent_type} 续轮回收第 {attempt} 次失败(停止续轮): {e}"
            )
            ctx["still_pending"] = True
            return

        new_text = collector.content_full[prev_len:]
        last_new_text = new_text
        # 本轮补发已产出实质内容且不再呈"在等结果"口吻 → 结果已回收完整
        if new_text.strip() and not _looks_like_pending_async_report(new_text):
            logger.info(
                f"[task={task.id}] {agent_type} 续轮回收第 {attempt} 次完成,"
                f"新增 {len(new_text)} 字符"
            )
            ctx["still_pending"] = False
            return
        logger.info(
            f"[task={task.id}] {agent_type} 续轮回收第 {attempt} 次后仍似未完成"
            f"(新增 {len(new_text)} 字符),继续等待"
        )
        cur_wait = cur_wait * factor

    # 耗尽最大补发次数
    ctx["still_pending"] = (
        _looks_like_pending_async_report(last_new_text) if last_new_text else True
    )
    logger.warning(
        f"[task={task.id}] {agent_type} 续轮回收已达上限 {max_continue} 次,"
        f"结果仍可能不完整: still_pending={ctx['still_pending']}"
    )



def _alloc_local_bridge_port() -> int:
    """local 模式:探测一个空闲 TCP 端口供 bridge 监听

    探测与 bridge 实际 bind 之间存在外部竞态(其他进程抢注端口),
    概率极小且 fail-loud:bind 失败 bridge 退出,健康检查超时报错可见。
    """
    with _used_ports_lock:
        for _ in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
                if port not in _used_bridge_ports:
                    _used_bridge_ports.add(port)
                    return int(port)
        raise RuntimeError("无法分配空闲 bridge 端口(连续 20 次探测冲突)")


class PromptIdleTimeout(Exception):
    """session/prompt idle 兜底:长时间无数据事件,CLI 疑似挂死

    由 _rpc 的 idle 看门狗抛出;prompt() 捕获后发 session/cancel 并把
    已累积输出留给调用方收尾(不 fail 任务)。超时阈值按事件状态分级:
    有活动工具(长命令执行中)用 ACP_IDLE_TIMEOUT_TOOL_SECONDS,
    无活动工具(等模型输出/最终响应)用 ACP_IDLE_TIMEOUT_OUTPUT_SECONDS。
    """

    def __init__(self, idle_secs: float, tool_active: bool):
        self.idle_secs = idle_secs
        self.tool_active = tool_active
        super().__init__(
            f"CLI 已 {int(idle_secs)}s 无数据事件"
            f"({'工具执行中' if tool_active else '无活动工具'},疑似挂死)"
        )


class ACPStreamAborted(RuntimeError):
    """SSE 流在收到 JSON-RPC 最终响应前异常结束(CLI 崩溃/连接中断)

    ACP 协议要求每个请求必须回最终响应(含错误响应),因此"流结束但未收到
    id 匹配的响应"必然是异常终止——此前 _rpc 静默返回 {} 会把 CLI 崩溃
    (如 Node OOM)当作正常收尾,残缺输出直接流入 agent2 审查。
    bridge 会在关流前推 event: stream_error 事件携带原因;未收到该事件
    (网络中断/旧版 bridge)时用通用描述兜底。
    继承 RuntimeError:new_session 的 bridge 日志增强路径(run_acp_agent)
    仍可捕获并附上 CLI stderr。prompt() 捕获后走截断兜底(同 idle 超时)。
    """


# ============================================================
# ACP HTTP 客户端
# ============================================================


class ACPClient:
    """ACP HTTP 客户端:通过 HTTP/SSE 与沙箱内的 acp_bridge 通信

    桥接服务将 HTTP 请求转换为 CLI 的 stdio ACP(JSON-RPC over
    newline-delimited JSON),响应以 SSE 流式返回。

    使用方式:
        client = ACPClient(endpoint_url, endpoint_headers)
        client.initialize()
        session_id = client.new_session(cwd="/home/user/repo")
        result = client.prompt(session_id, [{"type":"text","text":"hi"}], on_event=callback)
        client.close()
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        recorder: _ACPRecorder | None = None,
        permission_handler=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        # 可选的原始响应记录器:在 _rpc 的 SSE 循环里记录每一行原文,
        # 任何解析/过滤之前落盘。None 表示不记录(如 test_credential 流程)。
        self.recorder = recorder
        # 可选的命令确认处理器:CLI 发来 request_permission 时(经 bridge 转为
        # permission_request SSE 事件),调用此 handler 让用户确认。
        # 签名:permission_handler(payload: dict) -> dict
        # 返回:{"outcome": "selected", "option_id": "allow_once"} 或 {"outcome": "rejected"}
        # None 表示不处理(默认拒绝,兼容 always_approve 模式下 CLI 仍开 yolo 不会发请求的场景)
        self.permission_handler = permission_handler
        # 最近一次 prompt 是否被 idle 兜底提前终止(截断原因)。
        # 非 None 时调用方应用已累积输出收尾本轮,并在 summary 里标注。
        self.last_prompt_truncated: str | None = None
        # read timeout=None:session/prompt 可能长时间流式输出
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=30),
            headers=self.headers,
        )
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _rpc(
        self,
        request: dict,
        on_event=None,
        timeout: httpx.Timeout | float | None = None,
        idle_probe: Callable[[], bool] | None = None,
    ) -> dict:
        """发送 JSON-RPC 请求,可选流式处理通知,返回最终响应 result

        桥接服务对所有 POST /rpc 返回 SSE(text/event-stream):
        - 通知(method 字段,无 id):中间事件,通过 on_event 回调处理
        - 最终响应(有 id 匹配):流结束标志,返回其 result

        on_event: 接收通知 dict 的回调函数。None 表示不处理中间事件
        (用于 initialize / session/new 等快速调用)。
        timeout: 本次请求的超时(秒或 httpx.Timeout)。None 用 client 默认
        (read=None 无限等待)。测试场景应传有限值,避免模型无响应时卡死。
        idle_probe: 返回当前是否有活动工具的回调。非 None 时启用 idle
        看门狗(挂死兜底),超时抛 PromptIdleTimeout;None 不启用。
        """
        request_id = request.get("id")
        method = request.get("method", "?")

        # 记录请求开始元信息(便于事后按 JSONL 边界定位每个 RPC 调用)
        if self.recorder:
            self.recorder.record_raw(
                f"--> {method} id={request_id} params={json.dumps(request.get('params', {}), ensure_ascii=False)}",
                kind="meta",
            )

        with self._client.stream(
            "POST",
            f"{self.base_url}/rpc",
            json=request,
            timeout=timeout,
        ) as response:
            if response.status_code != 200:
                response.read()
                body = response.text
                # 完整记录 HTTP 错误响应体(不截断)
                if self.recorder:
                    self.recorder.record_raw(
                        f"HTTP {response.status_code}\n{body}",
                        kind="http_error",
                    )
                raise RuntimeError(
                    f"ACP 请求失败: HTTP {response.status_code}, body={body[:500]}"
                )

            final_result: dict | None = None
            # 跟踪当前 SSE 事件类型(从 event: 行读取)
            # bridge 推 event: permission_request 时,后续 data: 行是 permission 载荷,
            # 不是 ACP 通知,需要走 permission_handler 路径
            current_event_type: str | None = None
            # bridge 在异常关流前推的 stream_error 原因(人类可读,如
            # "CLI 进程在执行中退出(returncode=3)");None 表示未收到
            # (网络中断/旧版 bridge),流结束时用通用描述兜底
            stream_abort_msg: str | None = None

            # idle 看门狗:idle_probe 非 None 时把阻塞读挪到后台线程,
            # 主线程按 _IDLE_POLL_SECONDS 检查无数据 idle 时长(挂死兜底)
            lines = (
                self._iter_lines_with_watchdog(response, idle_probe)
                if idle_probe is not None
                else response.iter_lines()
            )

            for line in lines:
                # 最先记录原始行(不解析、不过滤、不截断),
                # 确保即使后续 JSONDecodeError 或分发逻辑跳过,
                # 原始 SSE 文本仍完整保留在 JSONL 中。
                if self.recorder:
                    self.recorder.record_raw(line, kind="line")

                line = line.strip()
                if not line:
                    # SSE 事件分隔符(空行),重置事件类型
                    current_event_type = None
                    continue

                # event: 行,记录事件类型(如 permission_request)
                if line.startswith("event:"):
                    current_event_type = line[6:].strip()
                    continue

                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue

                # permission_request 事件:CLI 检测到危险命令,经 bridge 转发,
                # 调 permission_handler 让用户确认,然后 POST /permission_response 给 bridge
                if current_event_type == "permission_request":
                    try:
                        perm_payload = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning(f"[acp] permission_request 载荷非 JSON: {data[:100]}")
                        continue
                    self._handle_permission_request(perm_payload)
                    continue  # 不走 on_event,继续读后续 SSE

                # stream_error 事件:bridge 即将因异常关闭连接(CLI 崩溃/
                # stdout EOF/读失败),记录原因,流结束后由 _rpc 抛
                # ACPStreamAborted(该事件是关流前最后一条,不在 on_event 分发)
                if current_event_type == "stream_error":
                    try:
                        err_payload = json.loads(data)
                    except json.JSONDecodeError:
                        err_payload = {"message": data[:200]}
                    if not isinstance(err_payload, dict):
                        err_payload = {"message": str(err_payload)[:200]}
                    stream_abort_msg = str(
                        err_payload.get("message")
                        or err_payload.get("reason")
                        or "未知原因"
                    )
                    continue

                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug(f"[acp] 非 JSON SSE 行,跳过: {data[:100]}")
                    continue

                # 错误响应(JSON-RPC error)
                if "error" in msg and msg.get("id") == request_id:
                    err = msg["error"]
                    code = err.get("code")
                    message = err.get("message", "")
                    data = err.get("data")
                    # data 字段可能含详细错误信息(如 acp 库的 -32603 Internal error
                    # 会在 data 里附带原始异常 traceback 字符串)
                    parts = [f"ACP 错误 {code}: {message}"]
                    if data:
                        data_str = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                        parts.append(f"详情: {data_str}")
                    raise RuntimeError("\n".join(parts))

                # 最终响应(id 匹配)
                if msg.get("id") == request_id:
                    final_result = msg.get("result") or {}
                    break

                # 通知(有 method,无 id 或 id 不匹配)
                if on_event and "method" in msg:
                    on_event(msg)

        if final_result is None:
            if request_id is not None:
                # 流结束但未收到 id 匹配的最终响应:CLI 崩溃/连接中断等
                # 异常终止(ACP 要求每个请求必须回最终响应,正常完成不可
                # 能走到这里)。此前静默返回 {} 会把崩溃当作正常收尾,
                # 残缺输出直接流入 agent2 审查,必须显式报错。
                msg = stream_abort_msg or (
                    "连接在收到 JSON-RPC 最终响应前中断(CLI 崩溃或网络异常)"
                )
                if self.recorder:
                    self.recorder.record_raw(f"stream aborted: {msg}", kind="meta")
                raise ACPStreamAborted(msg)
            final_result = {}
        return final_result

    def _iter_lines_with_watchdog(
        self,
        response,
        idle_probe: Callable[[], bool],
    ) -> Generator[str, None, None]:
        """带 idle 检测的 SSE 行读取(读线程 + queue,主线程定期检查)

        httpx 的 iter_lines 阻塞在 socket 读上,无法插入 idle 检查;
        这里把读取挪到守护线程,主线程用 queue.get(timeout) 周期性检查。

        活动判定:任何 data:/event: 行算活动(bridge 的 `: idle Ns` 心跳
        注释只证明 bridge 活着,不代表 CLI 有进展,不重置 idle)。
        阈值按事件状态分级:idle_probe()=True(有工具在跑,如 git clone/
        构建等长命令本就长时间无输出)用 ACP_IDLE_TIMEOUT_TOOL_SECONDS,
        否则用 ACP_IDLE_TIMEOUT_OUTPUT_SECONDS;对应配置为 0 则不检查。

        超时抛 PromptIdleTimeout(由 prompt() 捕获善后)。
        """
        line_q: queue.Queue = queue.Queue()

        def _pump() -> None:
            try:
                for ln in response.iter_lines():
                    line_q.put(("line", ln))
            except Exception as e:  # 读取层错误透传给主线程(与原同步行为一致)
                line_q.put(("error", e))
            finally:
                line_q.put(("end", None))

        threading.Thread(
            target=_pump, daemon=True, name="acp-sse-reader"
        ).start()

        last_activity = time.monotonic()
        while True:
            try:
                kind, payload = line_q.get(timeout=_IDLE_POLL_SECONDS)
            except queue.Empty:
                idle_secs = time.monotonic() - last_activity
                tool_active = bool(idle_probe())
                threshold = (
                    settings.ACP_IDLE_TIMEOUT_TOOL_SECONDS
                    if tool_active
                    else settings.ACP_IDLE_TIMEOUT_OUTPUT_SECONDS
                )
                if threshold > 0 and idle_secs >= threshold:
                    raise PromptIdleTimeout(idle_secs, tool_active)
                continue
            if kind == "end":
                return
            if kind == "error":
                raise payload
            line = payload
            stripped = line.strip()
            if stripped.startswith(("data:", "event:")):
                last_activity = time.monotonic()
            yield line

    def _handle_permission_request(self, payload: dict) -> None:
        """处理 bridge 发来的 permission_request SSE 事件。

        CLI 检测到危险命令 → 经 bridge 转为 permission_request SSE 事件 →
        调 permission_handler 让用户确认 → POST /permission_response 提交结果给 bridge →
        bridge 把结果作为 JSON-RPC 响应写回 CLI stdin。

        payload 结构:{
            "id": "<perm_id>",
            "command": "rm -rf /",
            "description": "...",
            "options": [{"option_id": "allow_once", ...}, ...]
        }
        """
        perm_id = payload.get("id", "")
        if not perm_id:
            logger.warning("[acp] permission_request 载荷无 id,跳过")
            return

        if self.permission_handler is None:
            # 无 handler,默认拒绝(不应发生:always_approve 模式下 CLI 开 yolo 不会发请求)
            logger.warning(f"[acp] 收到 permission_request 但无 handler,默认拒绝(perm_id={perm_id})")
            outcome = {"outcome": "rejected"}
        else:
            try:
                outcome = self.permission_handler(payload)
            except Exception as e:
                logger.warning(f"[acp] permission_handler 异常: {e}", exc_info=True)
                outcome = {"outcome": "rejected"}

        # POST /permission_response 提交结果给 bridge
        try:
            self._client.post(
                f"{self.base_url}/permission_response",
                json={"id": perm_id, "outcome": outcome},
                timeout=30,
            )
        except Exception as e:
            logger.warning(f"[acp] 提交 permission_response 失败(perm_id={perm_id}): {e}")

    def initialize(self, timeout: httpx.Timeout | float | None = 60) -> dict:
        """ACP 握手:交换协议版本和能力

        返回的 result 可能含 authMethods(若 Agent 要求认证),
        此时客户端须先调 authenticate(methodId) 才能创建 session。
        见 https://agentclientprotocol.com/protocol/authentication

        timeout: 超时(秒),默认 60s。握手是 bridge 就绪后的快速往返,
        传有限值让 bridge 异常时快速失败——client 默认 read=None 会无限
        等待,bridge 崩溃/CLI 挂死时前端会永远停在握手阶段。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "SecondLook", "version": "1.0.0"},
            },
            "id": self._next_id(),
        }, timeout=timeout)

    def authenticate(
        self,
        method_id: str,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict:
        """ACP 认证:用 initialize 返回的某个 authMethod id 完成认证

        Agent 在 initialize 响应中通过 authMethods 声明支持的认证方式,
        客户端选一个调本方法。凭证经环境变量注入到 bridge 进程,
        CLI 子进程继承后在此步骤完成服务端认证。

        认证成功后才能创建 session,否则会收到 -32000 Authentication required。
        timeout: 超时(秒),认证可能涉及网络往返验证,建议传有限值。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "authenticate",
            "params": {"methodId": method_id},
            "id": self._next_id(),
        }, timeout=timeout)

    def new_session(
        self,
        cwd: str | None = None,
        timeout: httpx.Timeout | float | None = 60,
    ) -> str:
        """创建 ACP 会话,返回 session_id

        params 按 ACP 规范必须含 mcpServers(可为空数组),cwd 为可选工作目录。
        见 https://agentclientprotocol.com/protocol/session-setup

        timeout: 超时(秒),默认 60s(与 initialize 同理,快速往返传有限值,
        bridge 异常时快速失败而非无限挂起)。
        """
        params: dict[str, Any] = {"mcpServers": []}
        if cwd:
            params["cwd"] = cwd
        result = self._rpc({
            "jsonrpc": "2.0",
            "method": "session/new",
            "params": params,
            "id": self._next_id(),
        }, timeout=timeout)
        session_id = result.get("sessionId") or result.get("session_id") or ""
        if not session_id:
            raise RuntimeError(f"ACP session/new 未返回 sessionId: {result}")
        return session_id

    def restore_session(
        self,
        method: str,
        session_id: str,
        cwd: str,
        timeout: httpx.Timeout | float | None = 120,
    ) -> dict:
        """按 sessionId 恢复 CLI 自己持久化的会话(session/load / session/resume)

        与 session/new 的区别:上下文由 CLI 从磁盘 transcript 复原(含工具调用与
        思考过程),比后端把历史渲染成文本回放保真度更高且不占 prompt token。
        params 按 ACP 必须含 sessionId/cwd/mcpServers(CLI 按 cwd 定位项目会话)。

        通知一律丢弃(不传 on_event):恢复时 CLI 会把历史以 session/update 回放,
        那些是"往轮"内容,已落库过一次,绝不能再进本轮 collector;回放拖到
        最终响应之后的尾巴由 bridge 排空(_drain_queue),因此调用方仅对
        bridge_protocol >= 2 的 bridge 启用本方法。
        失败(会话不存在/磁盘状态已丢/CLI 不支持)由调用方降级 session/new。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": method,
            "params": {"sessionId": session_id, "cwd": cwd, "mcpServers": []},
            "id": self._next_id(),
        }, timeout=timeout)

    def set_config_option(
        self, session_id: str, config_id: str, value: str
    ) -> dict:
        """设置会话配置项(ACP session/set_config_option)

        用于运行时切换模型 / 思考强度 / 模式等,无需重启 CLI。
        常见 configId(因 CLI 而异):
        - 'model':模型名(如 'deepseek-v4-pro')
        - 'reasoning_effort':思考强度(如 'low' / 'medium' / 'high',dsh)
        - 'thinking':思考强度(部分 CLI 的命名)
        - 'mode':值 'yolo' = 跳过权限确认(等价 --yolo)

        部分 CLI(如 dsh)的 ACP 模式无 --model / --reasoning-effort 启动参数,
        通过本方法在 session/new 后设置。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "session/set_config_option",
            "params": {
                "sessionId": session_id,
                "configId": config_id,
                "value": value,
            },
            "id": self._next_id(),
        })

    def prompt(
        self,
        session_id: str,
        prompt: list[dict],
        on_event=None,
        timeout: httpx.Timeout | float | None = None,
        idle_probe: Callable[[], bool] | None = None,
    ) -> dict:
        """发送 prompt,流式处理通知,返回最终结果

        prompt: ACP/MCP content 数组,如 [{"type":"text","text":"你好"}]
        (注意:不是 OpenAI 的 {"role","content"} 格式,ACP 用 MCP content 格式)
        on_event: 接收 session/update 通知的回调
        timeout: 本次请求超时(秒或 httpx.Timeout)。None 用 client 默认
        (read=None 无限等待)。测试场景应传有限值。
        idle_probe: 返回当前是否有活动工具的回调(如 collector.has_active_tools)。
        非 None 时启用挂死兜底:分级 idle 超时后发 session/cancel 并返回空结果,
        同时置 last_prompt_truncated(调用方用已累积输出收尾,不 fail 任务)。
        流异常终止(ACPStreamAborted,CLI 崩溃/连接中断)同样走截断兜底:
        cancel(CLI 已死时无害)→ 置 last_prompt_truncated → 返回空结果。
        """
        self.last_prompt_truncated = None
        try:
            return self._rpc({
                "jsonrpc": "2.0",
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": prompt,
                },
                "id": self._next_id(),
            }, on_event=on_event, timeout=timeout, idle_probe=idle_probe)
        except PromptIdleTimeout as e:
            # 挂死兜底:记录现场 → cancel CLI(避免沙箱内残留失控进程)→
            # 返回空结果,由调用方用 collector 已累积的输出收尾本轮
            logger.warning(f"[acp] prompt idle 兜底触发: {e}")
            if self.recorder:
                self.recorder.record_raw(f"idle timeout: {e}", kind="meta")
            self.cancel(session_id)
            self.last_prompt_truncated = str(e)
            return {}
        except ACPStreamAborted as e:
            # CLI 崩溃/连接中断兜底:与 idle 同款善后——cancel(CLI 已死时
            # bridge 返回 503,cancel 内部吞掉无害)→ 置截断标记 → 返回空
            # 结果,由调用方用已累积输出收尾本轮并标注"输出不完整",
            # 让 agent2 评估时知情(流结束原因已由 _rpc 记入 recorder)
            logger.warning(f"[acp] prompt 流异常终止兜底触发: {e}")
            self.cancel(session_id)
            self.last_prompt_truncated = str(e)
            return {}

    def cancel(self, session_id: str) -> None:
        """取消正在进行的 prompt"""
        try:
            self._rpc({
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
                "id": self._next_id(),
            })
        except Exception as e:
            logger.warning(f"[acp] cancel 失败(忽略): {e}")

    def health(self) -> bool:
        """健康检查:bridge + CLI 是否存活"""
        try:
            resp = self._client.get(f"{self.base_url}/health", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "ok"
            return False
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()


# ============================================================
# 凭证加载 + 环境变量映射
# ============================================================


def _load_credentials(db: Session, user_id, agent_type: str) -> dict[str, str]:
    """从 user_agent_configs 加载解密后的凭证 dict

    返回如 {"pat": "xxx"} 或 {"api_key": "sk-xxx", "base_url": "..."}。
    未配置或解密失败时抛错(CLI executor 需要凭证才能认证)。
    agent_type: agent 类型标识(如 "qoder_cli" / "deepseek_cli")。
    """
    if user_id is None:
        raise RuntimeError("外部 CLI 执行器需要登录用户(匿名任务不支持)")

    row = (
        db.query(UserAgentConfig)
        .filter(
            UserAgentConfig.user_id == user_id,
            UserAgentConfig.agent_type == agent_type,
        )
        .first()
    )
    if row is None or not row.credentials_encrypted:
        raise RuntimeError(
            f"未配置 {agent_type} 凭证。请在「智能体配置」中配置相应凭证。"
        )

    try:
        plaintext = decrypt_secret(row.credentials_encrypted)
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise ValueError("凭证格式错误(非 JSON 对象)")
        return data
    except Exception as e:
        raise RuntimeError(f"凭证解密失败: {e}") from e


def _build_credential_envs(credentials: dict[str, str], agent_type: str) -> dict[str, str]:
    """将凭证 dict 映射为环境变量 dict(按 registry 的 credential_env)

    registry 中 credential_env 形如 {"pat": "QODER_PERSONAL_ACCESS_TOKEN"},
    即凭证 key → 环境变量名。只注入有值的凭证。

    另外,若 registry 配了 credential_env_defaults(形如
    {"SOME_ENV": "默认值"}),则将这些默认值也注入,
    确保某些必须的环境变量即使用户未填也有默认值。
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    cred_env_map: dict[str, str] = sandbox_cfg.get("credential_env", {})
    envs: dict[str, str] = {}
    for cred_key, env_name in cred_env_map.items():
        val = credentials.get(cred_key)
        if val:
            envs[env_name] = val

    # 注入默认值(仅当该环境变量尚未由凭证设置时)
    for env_name, default_val in (sandbox_cfg.get("credential_env_defaults") or {}).items():
        if env_name not in envs and default_val:
            envs[env_name] = default_val

    return envs


def _credential_injection_missing(
    credential_envs: dict[str, str], pre_bridge_hook
) -> bool:
    """是否缺少任何凭证注入手段(据此在启动前拦截)。

    环境变量映射为空 且 无文件注入型 pre_bridge_hook 时,才算真正的配置缺失。
    典型:codex chatgpt 模式无 api_key → credential_envs={},但凭证经 pre_bridge_hook
    以 auth.json 文件注入沙箱,应放行(否则会在 bridge 启动前误报"凭证映射为空")。
    qoder/deepseek 无 pre_bridge_hook,仍要求 credential_envs 非空,行为不变。
    """
    return not credential_envs and pre_bridge_hook is None


# ============================================================
# 沙箱环境准备:bridge 脚本 + CLI 可用性检查
# ============================================================


def _get_bin(agent_type: str) -> str:
    """从 settings 读取 CLI 可执行文件名(经 registry 的 config key)"""
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    config_key = sandbox_cfg.get("bin_config_key", "")
    if config_key:
        val = getattr(settings, config_key, None)
        if val:
            return val
    return sandbox_cfg.get("bin_default", "")


def _get_install_cmd(agent_type: str) -> str:
    """从 settings 读取 CLI 安装命令(经 registry 的 config key)"""
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    config_key = sandbox_cfg.get("install_cmd_config_key", "")
    if config_key:
        val = getattr(settings, config_key, None)
        if val:
            return val
    return sandbox_cfg.get("install_cmd_default", "")


def _get_acp_args(
    task: Task | None = None,
    agent_type: str = "",
    extra_args: list[str] | None = None,
) -> list[str]:
    """从 registry 读取 ACP 启动参数,按需注入 task.params 中的模型配置

    支持的 task.params 字段(均为可选):
        model:            模型名
        reasoning_effort: 思考强度(low/medium/high/xhigh/max)
        context_window:   上下文窗口
        _executor_command_confirm: 命令确认模式("always_approve"/"per_command")
            per_command 时移除 --yolo(Qoder),让 CLI 发 request_permission 给前端确认

    若 registry 的 sandbox.inject_cli_model_args 为 False(如 dsh 的
    ACP 模式不支持 --model 等 CLI 参数),则不注入 task.params 模型配置
    —— 由 wrapper 层通过 set_config_option 在 session/new 后设置。

    extra_args: 额外追加的 CLI 参数(如测试时强制用特定模型),
    追加在 task.params 配置之后,会覆盖同名参数(CLI 以最后的为准)。
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    args = list(sandbox_cfg.get("acp_args", []))

    # 仅当 registry 声明支持 CLI 模型参数时才注入(qoder=True, dsh/codex=False)
    if sandbox_cfg.get("inject_cli_model_args", True) and task and task.params:
        model = task.params.get("model")
        if model:
            args.extend(["--model", str(model)])
        effort = task.params.get("reasoning_effort")
        if effort:
            args.extend(["--reasoning-effort", str(effort)])
        ctx = task.params.get("context_window")
        if ctx:
            args.extend(["--context-window", str(ctx)])

    # 命令确认模式:per_command 时移除 --yolo(Qoder 的 yolo 在 acp_args)
    # 让 CLI 进入 approval 模式,遇到危险命令发 request_permission 给前端确认
    # dsh 的权限在 wrapper 层经 DSH_PERMISSION_MODE 环境变量处理
    # Codex 用 --dangerously-bypass-approvals-and-sandbox,不支持 per_command,在 wrapper 降级
    if task and task.params:
        approval_mode = task.params.get("_executor_command_confirm", "always_approve")
        if approval_mode == "per_command":
            args = [a for a in args if a != "--yolo"]

    if extra_args:
        args.extend(extra_args)
    return args


def _write_bridge_script(session, agent_type: str = "") -> None:
    """将 bridge 脚本写入沙箱(从本地源文件读取)

    agent_type 决定使用哪个 bridge(从 registry sandbox.bridge_script 读取):
    - "acp_bridge"(默认):通用 ACP stdio 桥接,适用于原生支持 ACP 的 CLI(qoder/deepseek)
    - "codex_bridge":Codex 专用,将 codex exec --json JSONL 翻译为 ACP 通知
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    bridge_name = sandbox_cfg.get("bridge_script", _DEFAULT_BRIDGE)
    source = _BRIDGE_SOURCES.get(bridge_name)
    if source is None:
        raise RuntimeError(f"未知 bridge 脚本: {bridge_name}")
    if not source.exists():
        raise RuntimeError(f"bridge 源文件不存在: {source}")
    content = source.read_text(encoding="utf-8")
    session.write_file(BRIDGE_SCRIPT_PATH, content)


def _ensure_cli_env(session, agent_type: str) -> None:
    """准备 CLI 运行环境(sandbox:沙箱内 / local:宿主机)

    1. 写入 bridge 脚本(local 模式落 local_dir/.acp/,write_file 自动建父目录)
    2. 检查 CLI 是否可用:
       - sandbox:`command -v` / `which` shell 检查,未装则执行安装命令
       - local:Python shutil.which 检查(跨平台,可解析 Windows .cmd/.exe),
         未装直接报错要求预装 —— 不自动 npm install -g,避免修改宿主机全局环境
    """
    cli_bin = _get_bin(agent_type)
    is_local = getattr(session, "mode", "") == "local"

    if not is_local:
        # 创建脚本目录(local 模式 _local_write_file 自动建父目录,跳过)
        session.run_command(f"mkdir -p {Path(BRIDGE_SCRIPT_PATH).parent.as_posix()}")

    # 写入 bridge 脚本(per-agent,默认 acp_bridge.py)
    _write_bridge_script(session, agent_type)

    if is_local:
        # local 模式:宿主机 PATH 检查(Python 端,不走 shell)
        if not shutil.which(cli_bin):
            raise RuntimeError(
                f"宿主机未找到 {cli_bin}(local 模式 CLI 直接跑在宿主机)。"
                f"请先在宿主机预装(如 npm install -g),"
                f"或在配置中将对应 *_CLI_BIN 设为可执行文件绝对路径。"
            )
        logger.info(
            f"[{agent_type}] 环境就绪(local): {cli_bin} 可用,"
            f"bridge 脚本已写入 {session.local_dir / '.acp'}"
        )
        return

    # sandbox 模式:shell 检查 + 未装自动安装
    check_cmd = f"command -v {cli_bin} || which {cli_bin} 2>/dev/null"
    result = session.run_command(check_cmd, timeout=10)
    if not result.strip():
        # CLI 未安装,尝试安装
        install_cmd = _get_install_cmd(agent_type)
        if not install_cmd:
            raise RuntimeError(
                f"沙箱内未找到 {cli_bin},且安装命令为空。"
                f"请在沙箱镜像中预装 CLI,或在配置中设置安装命令。"
            )
        logger.info(f"[{agent_type}] {cli_bin} 未安装,执行: {install_cmd}")
        install_result = session.run_command(install_cmd, timeout=120, check=False)
        # 再次检查
        result = session.run_command(check_cmd, timeout=10)
        if not result.strip():
            raise RuntimeError(
                f"CLI 安装失败({install_cmd})。"
                f"安装日志: {install_result[:500]}"
            )

    logger.info(f"[{agent_type}] 环境就绪: {cli_bin} 可用,bridge 脚本已写入 {BRIDGE_SCRIPT_PATH}")


# ============================================================
# Git 凭证注入:credential helper cache(让 CLI 能克隆私有仓库)
# ============================================================


# credential cache 超时(秒):24h,覆盖任务最长执行时长
# cache 守护进程随沙箱容器销毁自动清理,无需手动退出
_GIT_CRED_CACHE_TIMEOUT = 86400


def _inject_git_credentials_to_cache(session, db: Session, task: Task | None) -> None:
    """把用户的 git token 注入沙箱的 git credential helper cache

    让 CLI 智能体在沙箱内能克隆/拉取私有仓库,同时避免 token 出现在:
    - 环境变量(printenv / env / os.environ 读不到)
    - 磁盘文件(cat ~/.git-credentials 读不到,文件不存在)
    - 命令行参数(ps / /proc/<pid>/cmdline 读不到,token 通过 write_file
      写临时文件再重定向,不经过 shell 解析)

    token 只存在于 git credential-cache 守护进程的内存中,
    仅 git 命令在 HTTPS 鉴权时自动取用。沙箱容器销毁时 cache 随进程退出清理。

    工作流程:
    1. 配置 git config --global credential.helper 'cache --timeout=86400'
    2. 对每个有 token 的 provider(GitHub / Gitee):
       a. write_file 写临时输入文件(含 protocol/host/username/password)
       b. chmod 600 限制文件权限
       c. git credential approve < 临时文件(注入到 cache 守护进程内存)
       d. rm -f 删除临时文件(token 只留在 cache 内存中)

    失败不阻断流程(降级为无 token,CLI 走 SSH / 匿名 HTTPS 回退)。
    task=None(测试连接场景)时跳过。

    安全性:
    - LLM 无法通过 env / printenv 读到 token(不在环境变量)
    - LLM 无法通过 cat ~/.git-credentials 读到(文件模式是 cache 不是 store)
    - LLM 无法通过 ~/.gitconfig 读到(只配置了 helper=cache,无 token)
    - LLM 只能通过主动构造 `git credential fill` 命令才能从 cache 取出 token,
      这种调用很显眼,容易被审计日志捕获
    """
    if task is None or task.user_id is None:
        return

    # local 模式:跳过注入(降级为无 token)
    # - `git config --global` 会写宿主机真实 ~/.gitconfig(污染用户环境)
    # - chmod / /tmp 路径 / git credential approve 均为 Unix 写法
    # - Windows git 不带 credential-cache(Unix socket 依赖)
    if getattr(session, "mode", "") == "local":
        logger.warning(
            f"[task={task.id}] local 模式跳过 git credential 注入"
            f"(避免污染宿主机 ~/.gitconfig):CLI 会话内克隆/拉取私有仓库将不可用,"
            f"预 clone 不受影响"
        )
        return

    # 延迟导入避免循环依赖
    from app.agents.orchestrator import _load_git_tokens
    from app.git_provider import PROVIDERS

    tokens = _load_git_tokens(db, task.user_id)
    if not tokens:
        return

    try:
        # 配置 git credential helper cache(timeout 覆盖任务最长时长)
        session.run_command(
            f"git config --global credential.helper 'cache --timeout={_GIT_CRED_CACHE_TIMEOUT}'",
            timeout=10,
        )

        # 对每个有 token 的 provider,注入到 credential cache
        injected: list[str] = []
        for provider_id, token in tokens.items():
            provider = PROVIDERS.get(provider_id)
            if not provider or not token:
                continue

            # 写临时输入文件(token 不走命令行,避免 ps / /proc 泄露 + shell 解析问题)
            cred_input = (
                f"protocol=https\n"
                f"host={provider.host}\n"
                f"username={provider.token_username}\n"
                f"password={token}\n"
                f"\n"  # 空行结束输入(git credential approve 读到空行表示输入结束)
            )
            cred_file = f"/tmp/.git_cred_{provider_id}_{int(time.time() * 1000)}.tmp"
            session.write_file(cred_file, cred_input)
            # 限制文件权限(仅 owner 可读,防止同容器其他用户读取)
            session.run_command(f"chmod 600 {cred_file}", timeout=5)

            # 注入到 credential cache(git credential approve 启动 cache 守护进程并存入 token)
            session.run_command(
                f"git credential approve < {cred_file}",
                timeout=10, check=False,
            )

            # 立即删除临时文件(token 只留在 cache 守护进程内存中)
            session.run_command(f"rm -f {cred_file}", timeout=5)
            injected.append(provider_id)

        if injected:
            logger.info(
                f"[task={task.id}] git credential cache 注入成功: "
                f"{injected}(沙箱内 git 可访问私有仓库)"
            )
    except Exception as e:
        # 降级:不阻断流程,CLI 走 SSH / 匿名 HTTPS 回退
        logger.warning(f"[task={task.id}] git credential cache 注入失败(降级为无 token): {e}")


# ============================================================
# ACP bridge 生命周期管理
# ============================================================


def _start_acp_bridge(
    session,
    credential_envs: dict[str, str],
    task: Task | None = None,
    agent_type: str = "",
    extra_acp_args: list[str] | None = None,
) -> tuple[str, int]:
    """后台启动 ACP bridge,返回 (execution_id, port)

    sandbox 模式:python3 + 沙箱内路径 + 固定 ACP_BRIDGE_PORT(每容器独立端口空间)
    local 模式:sys.executable + 宿主机本地路径 + 动态分配端口
              (宿主机端口空间全局共享,固定 8088 并发任务会冲突),
              argv 列表启动(不经 shell,绕开 Windows cmd.exe 的引号问题)

    bridge 启动命令:
        python acp_bridge.py --port {port} --bin {bin} --args '{json}'
    凭证经 envs 注入到 bridge 进程,bridge 子进程(CLI)继承这些环境变量,
    实现凭证不在命令行明文出现。

    task 参数用于从 task.params 读取模型/思考强度/上下文窗口配置(见 _get_acp_args)。
    extra_acp_args: 额外 CLI 参数(如测试时强制特定模型),透传给 _get_acp_args。
    """
    cli_bin = _get_bin(agent_type)
    acp_args = _get_acp_args(task, agent_type, extra_acp_args)
    args_json = json.dumps(acp_args, ensure_ascii=False)

    if getattr(session, "mode", "") == "local":
        # local 模式:bridge 脚本经 _write_bridge_script 写入
        # local_dir/.acp/acp_bridge.py(路径映射,与 sandbox 同名,含 codex_bridge)
        bridge_path = session.local_dir / ".acp" / "acp_bridge.py"
        port = _alloc_local_bridge_port()
        argv = [
            sys.executable, str(bridge_path),
            "--port", str(port),
            "--bin", cli_bin,
            # args_json 作为单个 argv 元素传递,JSON 中的引号不经 shell 解析
            "--args", args_json,
        ]
        execution_id = session.run_command_background(
            envs=credential_envs,  # 凭证注入 bridge 进程,继承给 CLI
            work_dir=str(session.local_dir),
            argv=argv,
        )
        logger.info(
            f"[{agent_type}] ACP bridge 后台启动(local): "
            f"execution_id={execution_id}, port={port}"
        )
        return execution_id, port

    # sandbox 模式:沙箱内 python3 + 固定端口
    # shell 中 JSON 数组含双引号,需单引号包裹
    cmd = (
        f"python3 {BRIDGE_SCRIPT_PATH}"
        f" --port {ACP_BRIDGE_PORT}"
        f" --bin {cli_bin}"
        f" --args '{args_json}'"
    )
    execution_id = session.run_command_background(
        cmd,
        envs=credential_envs,  # 凭证注入 bridge 进程,继承给 CLI
        work_dir=BRIDGE_WORK_DIR,
    )
    logger.info(f"[{agent_type}] ACP bridge 后台启动: execution_id={execution_id}")
    return execution_id, ACP_BRIDGE_PORT


def _wait_for_bridge_ready(
    session, execution_id: str, endpoint_url: str, endpoint_headers: dict[str, str],
    agent_type: str = "",
) -> dict[str, Any]:
    """等待 bridge HTTP 服务就绪(健康检查轮询),返回 /health payload

    payload 含 bridge_protocol(会话恢复排空能力的版本门控,见
    runtime/acp_session.BRIDGE_PROTOCOL_REPLAY_SAFE)。
    超时(BRIDGE_STARTUP_TIMEOUT 秒)未就绪时,读取 bridge 日志辅助排查并抛错。
    """
    deadline = time.time() + BRIDGE_STARTUP_TIMEOUT
    client = httpx.Client(headers=endpoint_headers, timeout=5)

    logger.info(
        f"[{agent_type}] 等待 bridge 就绪: endpoint={endpoint_url}, "
        f"timeout={BRIDGE_STARTUP_TIMEOUT}s"
    )

    last_logs_len = 0
    try:
        while time.time() < deadline:
            # 先检查后台进程是否还活着(避免 bridge 崩溃后空等)
            logs, _ = session.get_background_logs(execution_id)
            if "ACP CLI 启动失败" in (logs or ""):
                raise RuntimeError(
                    f"ACP bridge 启动失败:CLI 进程退出。日志:\n{logs[-1000:]}"
                )
            if logs and len(logs) > last_logs_len:
                new_part = logs[last_logs_len:]
                logger.debug(f"[{agent_type}] bridge 日志增量: {new_part.rstrip()}")
                last_logs_len = len(logs)

            # 健康检查
            try:
                resp = client.get(f"{endpoint_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[{agent_type}] ACP bridge 就绪")
                        return data
                    else:
                        logger.debug(f"[{agent_type}] health 200 但状态非 ok: {data}")
                else:
                    body = resp.text[:300]
                    logger.debug(f"[{agent_type}] health 返回 HTTP {resp.status_code}: {body}")
            except Exception as e:
                logger.debug(f"[{agent_type}] health 请求失败: {type(e).__name__}: {e}")

            time.sleep(BRIDGE_HEALTH_INTERVAL)

        # 超时:读取日志辅助排查
        logs, _ = session.get_background_logs(execution_id)
        raise RuntimeError(
            f"ACP bridge 启动超时({BRIDGE_STARTUP_TIMEOUT}s)。"
            f"日志:\n{(logs or '')[-1000:]}"
        )
    finally:
        client.close()


def _stop_acp_bridge(session, execution_id: str, agent_type: str = "") -> None:
    """停止 ACP bridge(中断后台命令)"""
    try:
        session.interrupt_command(execution_id)
        logger.info(f"[{agent_type}] ACP bridge 已停止: {execution_id}")
    except Exception as e:
        logger.warning(f"[{agent_type}] 停止 bridge 失败(忽略): {e}")


def _extract_bridge_error(session, execution_id: str, agent_type: str = "") -> str:
    """从 bridge 后台日志中提取 CLI 的错误/异常信息(CLI stderr 经 bridge 转发)

    bridge 的 _pump_stderr 把 CLI 的 stderr 每行加 "[cli stderr] " 前缀后
    输出到 bridge stderr,这些被沙箱后台进程日志捕获。本函数扫描日志中的
    Python traceback / Error / Exception 行,返回最后一段 traceback。

    用于 ACP -32603 Internal error 时获取真实异常(否则只有泛化的 "Internal error")。
    """
    try:
        logs, _ = session.get_background_logs(execution_id)
    except Exception:
        return ""
    if not logs:
        return ""

    lines = logs.splitlines()
    # 收集 traceback 相关行:Python 异常 traceback、Error/Exception 关键词行
    # 以及 bridge 转发的 [cli stderr] 行
    error_patterns = (
        "Traceback (most recent call last)",
        "Error:",
        "Exception:",
        "AuthError:",
        "ValueError:",
        "ImportError:",
        "ModuleNotFoundError:",
        "raise ",
        "[cli stderr]",
    )
    relevant: list[str] = []
    last_traceback_start = -1
    for i, line in enumerate(lines):
        if "Traceback (most recent call last)" in line:
            last_traceback_start = i
        if any(p in line for p in error_patterns):
            relevant.append(line)

    # 优先返回最后一个完整 traceback(从 Traceback 行到末尾)
    if last_traceback_start >= 0:
        tb_lines = lines[last_traceback_start:]
        # 限制长度,避免过长
        tb_text = "\n".join(tb_lines[:50])
        return tb_text

    # 回退:返回最后 20 行包含错误关键词的行
    if relevant:
        return "\n".join(relevant[-20:])

    return ""


# bridge 的 JSON-RPC 流水行:[bridge] <<< CLI stdout [N]: {...},
# 单条可达 500 字符,对用户无诊断价值,展示时过滤掉
_BRIDGE_JSONRPC_NOISE_RE = re.compile(r"\[bridge\] <<< CLI stdout \[\d+\]:")

# 静默失败场景下值得展示的错误关键词(HTTP 状态/限流/鉴权等)
_BRIDGE_LOG_ERROR_KEYWORDS = (
    "error", "exception", "failed", "traceback", "refused", "timeout",
    "unauthorized", "invalid", "denied",
    "400", "401", "402", "403", "429", "500",
    "错误", "失败", "超时", "拒绝", "配额", "余额", "异常", "警告", "退出",
)


def _extract_recent_bridge_logs(
    session, execution_id: str, agent_type: str = "", max_lines: int = 40
) -> str:
    """提取最近的 bridge 日志(过滤噪音,优先保留错误信息)

    用于"模型未响应"等静默失败场景:CLI 调用 LLM API 后未抛异常但返回空,
    真实的 HTTP 错误/拒绝信息可能在 CLI stderr 或 bridge 日志中。

    提取策略(逐级回退):
    1. [cli stderr] 转发行 + 含错误关键词的行(如 HTTP 401/429、超时等)
    2. 若无匹配,回退为最近 max_lines 行原始日志(仍过滤 JSON-RPC 流水噪音)
    """
    try:
        logs, _ = session.get_background_logs(execution_id)
    except Exception:
        return ""
    if not logs:
        return ""

    lines = logs.splitlines()
    if not lines:
        return ""

    def _clip(line: str, limit: int = 300) -> str:
        return line if len(line) <= limit else line[:limit] + " ..."

    def _is_noise(line: str) -> bool:
        return bool(_BRIDGE_JSONRPC_NOISE_RE.search(line))

    # 一级:[cli stderr] 转发行 + 错误关键词行
    error_lines = [
        _clip(line)
        for line in lines
        if "[cli stderr]" in line
        or (
            not _is_noise(line)
            and any(kw in line.lower() for kw in _BRIDGE_LOG_ERROR_KEYWORDS)
        )
    ]
    if error_lines:
        return "\n".join(error_lines[-max_lines:])

    # 二级:回退为最近原始日志(过滤 JSON-RPC 流水噪音)
    recent = [_clip(line) for line in lines[-max_lines * 2 :] if not _is_noise(line)]
    return "\n".join(recent[-max_lines:])


# ============================================================
# bridge/session 复用缓存(性能优化:省去每轮 ~25s 的 bridge 重建链路)
# ============================================================

# task_id -> bridge 状态。
# bridge(HTTP 服务)与 CLI 进程驻留沙箱内,ACP 会话状态在 CLI 进程内,
# 因此缓存 bridge_exec_id + acp_session_id 即可跨轮次/跨 resume 直接发 prompt,
# CLI 侧对话上下文随 session 自然延续。
_bridge_cache: dict[str, dict[str, Any]] = {}
_bridge_cache_lock = threading.Lock()


def _bridge_fingerprint(acp_args: list[str], credential_envs: dict[str, str]) -> str:
    """bridge 启动配置指纹:启动参数或凭证变化时缓存失效(需重建 bridge)

    全量环境变量参与指纹:任务级开关(如 dsh 的 DSH_PERMISSION_MODE)
    变化时应重建 bridge,不复用旧权限模式的进程。
    """
    keys = sorted(credential_envs)
    return json.dumps(
        [list(acp_args), [[k, credential_envs[k]] for k in keys]],
        ensure_ascii=False, sort_keys=True,
    )


def _bridge_health(endpoint_url: str, endpoint_headers: dict[str, str]) -> dict[str, Any]:
    """健康检查:返回 {"alive": bool, "protocol": int}

    protocol 取自 /health 的 bridge_protocol;无该字段(或请求失败)视为 1,
    即不会排空会话恢复的回放通知 → 不得尝试 session/load / session/resume
    (否则往轮历史会被当成本轮输出污染落库)。常量含义见
    runtime/acp_session.BRIDGE_PROTOCOL_REPLAY_SAFE。
    """
    try:
        with httpx.Client(headers=endpoint_headers, timeout=5) as hc:
            resp = hc.get(f"{endpoint_url}/health")
            if resp.status_code != 200:
                return {"alive": False, "protocol": 1}
            payload = resp.json() or {}
            alive = payload.get("status") == "ok"
            try:
                protocol = int(payload.get("bridge_protocol") or 1)
            except (TypeError, ValueError):
                protocol = 1
            return {"alive": alive, "protocol": protocol}
    except Exception:
        return {"alive": False, "protocol": 1}


def _bridge_alive(endpoint_url: str, endpoint_headers: dict[str, str]) -> bool:
    """健康检查:缓存的 bridge 及其 CLI 进程是否仍存活"""
    return _bridge_health(endpoint_url, endpoint_headers)["alive"]


def _try_reuse_bridge(
    task_id: str, session, agent_type: str, fingerprint: str,
) -> dict[str, Any] | None:
    """尝试复用本任务缓存的 bridge + ACP session

    可复用须同时满足:同一沙箱会话对象(容器未重建)、agent 类型一致、
    启动配置指纹一致、/health 通过。任一不满足则清缓存返回 None(走全新链路)。
    返回:{"bridge_exec_id", "endpoint_url", "endpoint_headers", "acp_session_id",
    "injected_context_hash"}(末项为本 session 已注入的系统段指纹,供注入去重)
    """
    with _bridge_cache_lock:
        entry = _bridge_cache.get(task_id)
        if not entry:
            return None
        if entry["session"] is not session:
            # 沙箱会话已重建,旧 bridge 随旧容器消亡,仅清缓存
            _bridge_cache.pop(task_id, None)
            return None
        if entry["agent_type"] != agent_type or entry["fingerprint"] != fingerprint:
            # 同沙箱内切换 agent 类型或启动参数/凭证变化:
            # 停旧 bridge(避免端口冲突)后重建
            _stop_acp_bridge(entry["session"], entry["bridge_exec_id"], entry["agent_type"])
            _bridge_cache.pop(task_id, None)
            return None
        reused = dict(entry)

    if not _bridge_alive(reused["endpoint_url"], reused["endpoint_headers"]):
        with _bridge_cache_lock:
            _bridge_cache.pop(task_id, None)
        logger.info(f"[task={task_id}] 缓存的 ACP bridge 已失活,重新初始化")
        return None
    return reused


def stop_task_bridge(task_id: str) -> None:
    """停止任务的 bridge 并清缓存(沙箱会话关闭/任务删除时调用)"""
    with _bridge_cache_lock:
        entry = _bridge_cache.pop(task_id, None)
    if not entry:
        return
    _stop_acp_bridge(entry["session"], entry["bridge_exec_id"], entry["agent_type"])


# ============================================================
# ACP 响应记录:完整 JSONL 落盘(供事后分析/回放)
# ============================================================


class _ACPRecorder:
    """把 ACP 通信的原始响应以 JSONL 形式落盘(不解析、不过滤)

    每个 task + round 一份文件,路径:
        {backend}/logs/acp/{task_id}_r{round_idx}_{YYYYmmdd_HHMMSS}.jsonl

    记录层级:在 ACPClient._rpc 的 SSE 循环里,每读到一行就 record_raw,
    在任何解析/过滤之前落盘。因此能完整保留:
    - 所有 data: 行(不论是否合法 JSON)
    - 非 data: 行(SSE 注释/event: 等)
    - HTTP 错误响应体(非 200 时)
    - 元信息(请求开始/结束标记)

    每行 JSONL 结构:
        {"seq": 1, "ts": "ISO 时间(本地时区)",
         "kind": "line" | "http_error" | "meta",
         "raw": "<原始文本,不截断>"}

    写入失败不影响主流程(只记 logger.warning)。
    """

    def __init__(self, task_id: str, round_idx: int, log_dir: Path | None = None):
        self.task_id = task_id
        self.round_idx = round_idx
        self._dir = log_dir or _ACP_LOG_DIR
        self._path: Path | None = None
        self._fh = None
        self._seq = 0
        self._open()

    def _open(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = f"{self.task_id}_r{self.round_idx}_{ts}.jsonl"
            self._path = self._dir / fname
            # 用 append 模式:同 task+round+秒级时间戳重启时追加,不覆盖
            self._fh = self._path.open("a", encoding="utf-8")
            logger.info(f"[acp_recorder] 记录到: {self._path}")
        except Exception as e:
            logger.warning(f"[acp_recorder] 打开文件失败(忽略): {e}")
            self._fh = None
            self._path = None

    def record_raw(self, raw: str, kind: str = "line") -> None:
        """记录一行原始文本(不解析、不截断、不过滤)

        kind:
            "line"       - SSE 流中的一行(data: / event: / 注释 / 空行等)
            "http_error" - HTTP 非 200 时的响应体
            "meta"       - 元信息(请求方法、开始/结束标记等)
        """
        if self._fh is None:
            return
        try:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "kind": kind,
                "raw": raw,
            }
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as e:
            logger.warning(f"[acp_recorder] 写入失败(忽略): {e}")

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            logger.info(
                f"[acp_recorder] 关闭: {self._path}, 共 {self._seq} 条事件"
            )


# ============================================================
# ACP 事件处理:翻译为 event_bus 事件 + 落库 Conversation
# ============================================================


class _ACPCollector:
    """收集 ACP 通知事件,翻译为 event_bus 事件并落库

    ACP session/update 通知结构(见 https://agentclientprotocol.com/protocol/prompt-turn):
        {
          "method": "session/update",
          "params": {
            "sessionId": "...",
            "update": {
              "sessionUpdate": "agent_message_chunk" | "plan" | "tool_call" | ...,
              "content": { "type": "text", "text": "..." },
              "entries": [...],  # plan
              "toolCallId": "...",  # tool_call / tool_call_update
              "status": "...",  # tool_call_update
              ...
            }
          }
        }

    字段映射(sessionUpdate → event_bus 事件):
    - agent_message_chunk → thinking_delta(phase=content)
    - thought_chunk       → thinking_delta(phase=reasoning)
    - tool_call           → conversation(type=tool_call)
    - tool_call_update    → conversation(type=tool_result) (status=completed 时)
    - plan                → event_bus plan 事件
    - error               → thinking_delta(phase=error)

    迭代切段(与 react_agent 对齐):
    ACP 一次 prompt 调用内部可能包含多次 ReAct 迭代
    (thought → message → tool_call → tool_result → thought → ...)。
    本类按 tool_call 切段:每遇到 tool_call 就结束当前思考迭代
    (推送 phase=end + 落库一条 thinking),并开启新迭代(新 conv_id),
    让前端以独立流式卡片展示每次思考。
    """

    def __init__(
        self,
        task: Task,
        db: Session,
        round_idx: int,
        *,
        agent_type: str = "",
    ):
        self.task = task
        self.db = db
        self.round_idx = round_idx
        # 全程累积(供调用方生成 summary / 提取 plan / 日志)
        self.reasoning_full = ""
        self.content_full = ""
        self.tool_call_count = 0
        # 本轮检测到的"异步后台子 Agent 派生"次数(Qoder Agent 工具回
        # "Async agent launched…background" 回执时 +1),供收尾判定是否续轮回收
        self.async_agent_launch_count = 0
        # 当前迭代状态
        self.iteration = 0
        self.current_conv_id = str(uuid.uuid4())
        self.reasoning_buf = ""
        self.content_buf = ""
        self._iter_started = False
        # tool_call 状态追踪:toolCallId -> {title, kind, tool_name, raw_input, input_text}
        # Qoder CN 的 rawInput 在 tool_call 事件里一次性给出;
        # Kimi(如 Agent 子任务)/Hermes 的参数经 tool_call_update(in_progress)增量构建,需累积 input_text。
        self._pending_tool_calls: dict[str, dict] = {}
        self._agent_type = agent_type
        # 最近一次 TodoList 工具(部分编码 CLI 的计划工具)解析出的计划清单,
        # 供收尾时续接 current_plan 链(content 里无 <plan> 时回退)
        self.last_todo_plan: list[dict] | None = None
        # [perf] 首个 ACP 事件到达时间(collector 创建 ≈ prompt 发送前一刻)
        self._perf_t0 = time.perf_counter()
        self._perf_first_logged = False

    def _ensure_iter_started(self) -> None:
        """懒启动:首个 delta 到达时推送 phase=start"""
        if self._iter_started:
            return
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "agent1",
            "phase": "start",
            "delta": "",
            "iteration": self.iteration,
        })
        self._iter_started = True

    def _flush_iteration(self) -> None:
        """结束当前迭代:推送 phase=end + 落库 thinking(若有内容)并推 conversation

        落库事件带 stream_conv_id(即本迭代的流式卡片 id):前端据此把实时
        流式卡片退役成只读历史卡片;中途离开详情页再回来的订阅者收不到
        thinking_delta 增量(总线不缓存),只能靠这条 conversation 事件补上。
        """
        if not self._iter_started:
            return
        stream_conv_id = self.current_conv_id
        publish(self.task.id, "thinking_delta", {
            "conv_id": stream_conv_id,
            "round_idx": self.round_idx,
            "role": "agent1",
            "phase": "end",
            "delta": "",
            "iteration": self.iteration,
        })
        if self.content_buf or self.reasoning_buf:
            _add_conversation(
                self.db, self.task,
                round_idx=self.round_idx,
                role="agent1", type="thinking",
                content=self.content_buf,
                reasoning=self.reasoning_buf,
                stream_conv_id=stream_conv_id,
            )
        self._iter_started = False

    def _start_new_iteration(self) -> None:
        """开新迭代(决策回合):iteration+1, 新 conv_id, 清空 buf
        (懒启动,不立即推 start)"""
        self.iteration += 1
        self.current_conv_id = str(uuid.uuid4())
        self.reasoning_buf = ""
        self.content_buf = ""
        self._iter_started = False

    def _start_new_iteration_if_needed(self) -> None:
        """决策回合锚点:一段思考/对话文本的开始 = 一次新迭代的开始。

        CLI 只暴露流式文本与工具事件,不暴露模型内部决策边界,以
        "思考/对话文本段"作为决策回合的可见标记:当前无活跃迭代时
        (上一段已在 tool_call 处 flush)开新迭代;同一段文本的连续
        chunk 直接续写,不重复开迭代。无文本的连续工具调用不产生
        新迭代,归入发起它们的决策回合(迭代=决策回合的统一口径)。
        """
        if self._iter_started:
            return
        self._start_new_iteration()

    def close(self) -> None:
        """prompt 调用结束:flush 最后一段迭代"""
        self._flush_iteration()

    @property
    def has_active_tools(self) -> bool:
        """是否有已发起但未结束的 tool_call(idle 超时分级判据)

        True 表示 CLI 在等工具(如 git clone/构建)返回,长时间无输出属正常,
        idle 兜底用宽松阈值;False 表示模型应在流式输出或已该回最终响应,
        用严格阈值。failed 状态在 __call__ 里与 completed 同样终结。
        """
        return bool(self._pending_tool_calls)

    def __call__(self, msg: dict) -> None:
        """处理一条 ACP 通知"""
        # [perf] 首个 ACP 事件 = CLI 侧首 token 到达(前端可见响应的起点)
        if not self._perf_first_logged:
            self._perf_first_logged = True
            perf_log(
                self.task.id, "acp_first_event",
                time.perf_counter() - self._perf_t0,
                round_idx=self.round_idx, agent_type=self._agent_type,
            )
        params_preview = msg.get("params") or {}
        update_preview = params_preview.get("update") or {}
        logger.debug(
            f"[acp] {msg.get('method', '')} / "
            f"{update_preview.get('sessionUpdate', '')}"
        )

        method = msg.get("method", "")
        if method != "session/update":
            return

        params = msg.get("params") or {}
        update = params.get("update") or {}
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", "")

        text = _extract_text(content)

        if update_type in (
            "thought_chunk", "thinking", "reasoning",
            "agent_thought_chunk",
        ):
            self._handle_thinking(text)
        elif update_type == "agent_message_chunk":
            self._handle_text(text)
        elif update_type == "tool_call":
            # 工具调用结束当前思考/对话段,但不开启新迭代:
            # 迭代=决策回合(以思考/对话文本段为锚点),无文本的连续
            # 工具调用归入发起它们的决策回合,避免按工具调用切迭代
            self._flush_iteration()
            self._handle_tool_call(update)
        elif update_type == "tool_call_update":
            tool_call_id = update.get("toolCallId", "")
            status = update.get("status", "")
            if status == "in_progress":
                # Kimi 的工具参数经 in_progress 增量构建,累积到 pending 缓存
                self._accumulate_tool_input(tool_call_id, text)
            elif status in ("completed", "failed"):
                # failed 同样是终态:落库工具结果(错误输出)并清理 pending,
                # 否则 pending 泄漏会让 has_active_tools 恒真(idle 兜底误用宽松阈值)
                self._handle_tool_result(tool_call_id, update)
        elif update_type == "plan":
            self._handle_plan(update.get("entries", []))
        elif update_type == "error":
            self._handle_error(text)
        else:
            logger.debug(f"[acp] 未知 update 类型: {update_type}, update={str(update)[:100]}")

    def _handle_thinking(self, delta: str) -> None:
        if not delta:
            return
        # 思考文本段的开始 = 新决策回合(新迭代)的开始
        self._start_new_iteration_if_needed()
        self._ensure_iter_started()
        self.reasoning_full += delta
        self.reasoning_buf += delta
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "agent1",
            "phase": "reasoning",
            "delta": delta,
            "iteration": self.iteration,
        })

    def _handle_text(self, delta: str) -> None:
        if not delta:
            return
        # 对话文本段的开始 = 新决策回合(新迭代)的开始
        self._start_new_iteration_if_needed()
        self._ensure_iter_started()
        self.content_full += delta
        self.content_buf += delta
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "agent1",
            "phase": "content",
            "delta": delta,
            "iteration": self.iteration,
        })

    def _handle_tool_call(self, update: dict) -> None:
        """记录工具调用(落库 conversation,推送 SSE)

        提取工具名、参数(rawInput / 增量 input_text),生成:
        - intent:人类可读一句话,末尾带 [tool_name] 标签(供前端提取)
        - detail:完整参数 JSON / 命令文本(前端等宽显示)

        content 格式:`{intent}\n{detail}`(前端 toolCallParts 按首行拆分)
        """
        self.tool_call_count += 1
        tool_call_id = update.get("toolCallId", "")
        title = update.get("title", "") or tool_call_id
        kind = update.get("kind", "other")
        raw_input = update.get("rawInput")  # Qoder 有,Kimi 无

        # 解析人类可读的工具名(优先 _meta.qoder.toolName,其次 kind 推断)
        meta = update.get("_meta") or {}
        qoder_meta = meta.get("qoder") or {}
        tool_name = qoder_meta.get("toolName", "") or self._infer_tool_name(title, kind)

        # Hermes 风格事件:无 rawInput,目标信息在 title 前缀与 locations 里
        # (read: /path、terminal: cmd、search: pattern),归一化成标准 rawInput,
        # 让 _build_tool_intent_detail 生成可读 intent(也避免 read: 被误判成 Bash)
        if not raw_input:
            locations = update.get("locations") or []
            loc_path = (
                locations[0].get("path", "")
                if locations and isinstance(locations[0], dict) else ""
            )
            if title.startswith("read: "):
                tool_name = "Read"
                raw_input = {"file_path": loc_path or title[6:].strip()}
            elif title.startswith("terminal: "):
                tool_name = "Bash"
                raw_input = {"command": title[10:].strip()}
            elif title.startswith("search: "):
                tool_name = "Grep"
                raw_input = {"pattern": title[8:].strip()}

        # 缓存,等 tool_call_update 累积输入 / completed 拿输出
        self._pending_tool_calls[tool_call_id] = {
            "title": title,
            "kind": kind,
            "tool_name": tool_name,
            "raw_input": raw_input or {},
            "input_text": "",  # Kimi 增量累积
            "conv_id": None,  # 落库后填充,completed 时用于更新
        }

        # 生成 intent + detail(Kimi 此时 input_text 为空,detail 可能为空)
        intent, detail = self._build_tool_intent_detail(tool_name, raw_input, "")

        # content: intent + "\n" + detail(前端按首行拆分)
        content = f"{intent}\n{detail}" if detail else intent

        conv = _add_conversation(
            self.db, self.task,
            round_idx=self.round_idx,
            role="agent1", type="tool_call",
            content=content,
        )
        self._pending_tool_calls[tool_call_id]["conv_id"] = conv.id

    def _accumulate_tool_input(self, tool_call_id: str, text: str) -> None:
        """记录 Kimi 的 tool_call_update(in_progress)累积参数

        Kimi 的工具参数不在 tool_call 事件的 rawInput 里(无此字段),
        而是通过 tool_call_update(status=in_progress)的 content 逐步构建。
        每次 in_progress 事件包含**完整累积文本**(非 delta),直接替换。

        节流推送 conversation_update,让前端实时看到参数构建过程
        (子智能体调用可能持续数分钟,否则用户只看到"启动子智能体"无详情)。
        """
        pending = self._pending_tool_calls.get(tool_call_id)
        if pending is None or not text:
            return
        # Kimi in_progress 每次是完整累积文本,直接替换(非 += 拼接)
        pending["input_text"] = text
        self._throttled_tool_call_update(tool_call_id, pending)

    def _throttled_tool_call_update(self, tool_call_id: str, pending: dict) -> None:
        """节流推送 tool_call conversation 更新(SSE only,不写 DB)

        Kimi 一次子智能体调用可能产生 100+ 个 in_progress 事件,
        全量推送会造成 SSE 风暴。按时间(0.5s)+ 长度增量(80 字符)节流。

        DB 不写:in_progress 期间内容是临时态,completed 时才落库最终内容。
        若用户在此期间刷新,从 DB 拿到的是上一版内容(可接受)。
        """
        conv_id = pending.get("conv_id")
        if not conv_id:
            return
        now = time.time()
        last_push = pending.get("last_push_time", 0.0)
        last_len = pending.get("last_push_len", 0)
        text_len = len(pending.get("input_text", ""))
        # 节流:距上次推送 < 0.5s 且长度增长 < 80 字符时跳过
        if now - last_push < 0.5 and text_len - last_len < 80:
            return
        pending["last_push_time"] = now
        pending["last_push_len"] = text_len

        intent, detail = self._build_tool_intent_detail(
            pending.get("tool_name", ""), None, pending.get("input_text", ""),
        )
        new_content = f"{intent}\n{detail}" if detail else intent
        publish(self.task.id, "conversation_update", {
            "id": str(conv_id),
            "content": new_content,
        })

    def _handle_tool_result(self, tool_call_id: str, update: dict) -> None:
        """记录工具结果(落库 conversation,推送 SSE)

        优先用 rawOutput(Qoder 和 Kimi 完成时都有,完整不截断),
        回退到 content 文本。

        对 Kimi(参数经 in_progress 增量构建):completed 时用累积的 input_text
        更新 tool_call conversation 的 content(补全 intent + detail),
        并推 SSE 让前端刷新显示。
        """
        raw_output = update.get("rawOutput", "")
        if not raw_output:
            raw_output = _extract_text(update.get("content"))
        if not raw_output:
            raw_output = "(无输出)"

        pending = self._pending_tool_calls.get(tool_call_id, {})
        tool_name = pending.get("tool_name", "工具")
        input_text = pending.get("input_text", "")
        conv_id = pending.get("conv_id")

        # Kimi:参数在 in_progress 增量构建,tool_call 落库时 detail 为空
        # 这里用累积的 input_text 补全 tool_call conversation 的 intent + detail
        if input_text and conv_id and not pending.get("raw_input"):
            intent, detail = self._build_tool_intent_detail(
                tool_name, None, input_text,
            )
            new_content = f"{intent}\n{detail}" if detail else intent
            # 更新已落库的 tool_call conversation
            self.db.query(Conversation).filter(
                Conversation.id == conv_id,
            ).update({"content": new_content})
            self.db.commit()
            # 推 SSE 让前端刷新(用 conversation_update 事件)
            publish(self.task.id, "conversation_update", {
                "id": str(conv_id),
                "content": new_content,
            })

        _add_conversation(
            self.db, self.task,
            round_idx=self.round_idx,
            role="agent1", type="tool_result",
            content=raw_output,
            # 关联对应 tool_call 会话记录(CLI 并行调用时 result 按完成顺序落库,
            # 前端靠它精确配对;conv_id 缺失时留空,前端回退相邻配对)
            tool_call_id=str(conv_id) if conv_id else None,
        )

        # TodoList 计划工具:completed 时携带完整计划清单({todos: [...]}),
        # 全量替换语义,解析后直接覆盖式推送 plan 事件(与 _handle_plan 同款,
        # 前端复用 react_agent 的计划清单卡片;部分 CLI 不发 ACP plan 通知,
        # 只能从该工具调用提取)
        if tool_name == "TodoList":
            todo_steps = self._parse_todo_plan(update.get("rawInput"), input_text)
            if todo_steps:
                self.last_todo_plan = todo_steps
                publish(self.task.id, "plan", {
                    "round_idx": self.round_idx,
                    "steps": todo_steps,
                })

        # 异步后台子 Agent 派生检测:Qoder 的 Agent 工具完成时回的是
        # "Async agent launched successfully…working in the background" 回执
        # (子 Agent 真正结果要到下一轮才回流),据此在收尾判定是否续轮回收。
        if tool_name == "Agent" and _ASYNC_AGENT_LAUNCH_RE.search(raw_output or ""):
            self.async_agent_launch_count += 1

        # 该 tool_call 已完成,清理 pending(避免累积 + 防止重复 completed 重复触发)
        self._pending_tool_calls.pop(tool_call_id, None)

    @staticmethod
    def _infer_tool_name(title: str, kind: str) -> str:
        """从 title/kind 推断工具名(无 _meta.qoder.toolName 时)"""
        if kind == "execute":
            return "Bash"
        if kind == "think":
            return "Agent"
        # title 本身就是工具名(如 "Agent")或命令文本
        if title and not title.startswith("/"):
            # 短 title 通常是工具名,长 title 通常是命令(取首词)
            if len(title) <= 30 and " " not in title:
                return title
            return "Bash"
        return "工具"

    @staticmethod
    def _build_tool_intent_detail(
        tool_name: str, raw_input: dict | None, input_text: str,
    ) -> tuple[str, str]:
        """生成人类可读的 intent + 完整参数 detail

        返回 (intent, detail):
        - intent:一句话描述,末尾带 [tool_name] 标签
        - detail:完整参数 JSON 或命令文本(前端等宽显示)

        各工具类型的 intent:
        - Agent(子智能体):"子任务: {description}" 或 "子任务: {prompt摘要}"
        - Bash(命令执行):"执行: {command摘要}"
        - Read/Grep/Glob(浏览型):"读取文件 {path}"/"搜索代码: {pattern}"/"查找文件: {pattern}"
        - 其他:"调用 {tool_name}"
        """
        raw_input = raw_input or {}
        detail = ""

        if tool_name == "Agent":
            # Qoder CN: rawInput 有 description / prompt / subagent_type
            desc = raw_input.get("description", "")
            prompt = raw_input.get("prompt", "")
            sub_type = raw_input.get("subagent_type", "")

            if desc:
                intent = f"子任务: {desc}"
            elif sub_type:
                intent = f"子任务: {sub_type}"
            elif prompt:
                intent = f"子任务: {prompt[:80]}"
            else:
                intent = "启动子智能体"

            # Kimi: 无 rawInput,参数在 input_text(JSON 字符串,可能不完整)
            if not raw_input and input_text:
                k_prompt = ""
                try:
                    params = json.loads(input_text)
                    k_prompt = params.get("prompt", "") or params.get("description", "")
                except json.JSONDecodeError:
                    # in_progress 期间 JSON 不完整(无闭合引号/大括号),
                    # 用 regex 提取 prompt 字段值,让前端实时看到正在构建的子任务描述
                    k_prompt = _extract_json_string_field(input_text, "prompt")
                if k_prompt:
                    intent = f"子任务: {k_prompt[:80]}"
                detail = input_text
            elif raw_input:
                detail = json.dumps(raw_input, ensure_ascii=False, indent=2)
            intent += " [Agent]"

        elif tool_name == "Bash":
            cmd = raw_input.get("command", "") or input_text
            desc = raw_input.get("description", "")
            if cmd:
                # intent 显示命令摘要(首行,最多 100 字符)
                cmd_first = cmd.split("\n")[0][:100]
                intent = f"执行: {cmd_first}" if not desc else f"执行: {desc}"
            else:
                intent = "执行命令"
            detail = cmd or (json.dumps(raw_input, ensure_ascii=False, indent=2) if raw_input else "")
            intent += " [Bash]"

        elif tool_name == "Read":
            fp = raw_input.get("file_path", "")
            intent = f"读取文件 {fp}" if fp else "读取文件"
            detail = json.dumps(raw_input, ensure_ascii=False, indent=2) if raw_input else input_text
            intent += " [Read]"

        elif tool_name in ("Grep", "Glob"):
            pattern = raw_input.get("pattern", "")
            verb = "搜索代码" if tool_name == "Grep" else "查找文件"
            intent = f"{verb}: {pattern[:60]}" if pattern else verb
            detail = json.dumps(raw_input, ensure_ascii=False, indent=2) if raw_input else input_text
            intent += f" [{tool_name}]"

        else:
            intent = f"调用 {tool_name}"
            if raw_input:
                detail = json.dumps(raw_input, ensure_ascii=False, indent=2)
            elif input_text:
                detail = input_text
            intent += f" [{tool_name}]"

        return intent, detail

    def _handle_plan(self, entries: list) -> None:
        """处理 plan 通知,推送 plan 事件

        ACP PlanEntry 状态为 pending/in_progress/completed,
        前端只认 pending/in_progress/done,completed 需映射为 done。
        """
        if not entries:
            return
        steps = []
        for i, e in enumerate(entries, 1):
            if not isinstance(e, dict):
                continue
            status = str(e.get("status") or "pending")
            status = _ACP_PLAN_STATUS_MAP.get(status, status)
            if status not in ("pending", "in_progress", "done"):
                status = "pending"
            steps.append({
                "id": i,
                "text": e.get("content", ""),
                "status": status,
            })
        if steps:
            publish(self.task.id, "plan", {
                "round_idx": self.round_idx,
                "steps": steps,
            })

    @staticmethod
    def _parse_todo_plan(raw_input: Any, input_text: str) -> list[dict] | None:
        """解析 TodoList 工具输入({todos: [{title, status}]})为 plan 步骤

        优先结构化 rawInput;回退解析 input_text(completed 时的累积文本,
        json_repair 容错)。查询模式(无 todos)/清空模式(空数组)返回 None。
        status 白名单归一(completed → done,非法值 → pending)。
        """
        todos: Any = None
        if isinstance(raw_input, dict):
            todos = raw_input.get("todos")
        if todos is None and input_text:
            try:
                parsed: Any = json.loads(input_text)
            except json.JSONDecodeError:
                try:
                    parsed = repair_json(input_text, return_objects=True)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict):
                todos = parsed.get("todos")
        if not isinstance(todos, list) or not todos:
            return None
        steps: list[dict] = []
        for item in todos:
            if not isinstance(item, dict):
                continue
            text = str(item.get("title") or "").strip()
            if not text:
                continue
            status = str(item.get("status") or "pending").strip()
            status = _ACP_PLAN_STATUS_MAP.get(status, status)
            if status not in ("pending", "in_progress", "done"):
                status = "pending"
            steps.append({"id": len(steps) + 1, "text": text, "status": status})
        return steps or None

    def _handle_error(self, text: str) -> None:
        if not text:
            text = "(未知错误)"
        self._ensure_iter_started()
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "agent1",
            "phase": "error",
            "delta": text,
            "iteration": self.iteration,
        })


def _extract_text(content: Any) -> str:
    """从 ACP content 字段提取文本

    content 可能是:
    - str:直接返回
    - dict:取 text / content / value 字段(即使值为空字符串也返回,
      表示"有该字段但内容为空",调用方据此过滤无内容的 chunk)
    - list:遍历取每项的 text 字段拼接

    注意:dict 含 text 字段但值为空时返回空字符串(而非 json.dumps 整个 dict)。
    Kimi 的 agent_thought_chunk 经常发送 {"type":"text","text":""} 空片段,
    若 fallback 到 json.dumps 会把 JSON 原文当文本堆积到 reasoning 里。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "value", "delta"):
            val = content.get(key)
            if isinstance(val, str):
                # 即使为空也返回:text 字段存在表示这是文本内容,空=无内容
                # (不要 fallback 到 json.dumps,否则空片段会变成 JSON 字符串堆积)
                return val
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _extract_text(item)
            if text:
                parts.append(text)
        return "".join(parts)
    return str(content)


# 从(可能不完整的)JSON 文本中提取字符串字段值
# 用于 Kimi in_progress 期间的增量参数,JSON 可能未闭合(无结束引号/大括号)
_INCOMPLETE_JSON_FIELD_RE = re.compile(
    r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)'
)


def _extract_json_string_field(text: str, field: str) -> str:
    """从(可能不完整的)JSON 文本中提取指定字符串字段的值

    Kimi 的 tool_call_update(in_progress)每次包含完整累积文本,但 JSON 可能
    尚未闭合(如 `{"prompt": "审计 /home/user/...`)。本函数用 regex 提取
    指定字段的值,无需完整 JSON 解析。

    返回解码后的字符串值,未找到返回空字符串。
    """
    for m in _INCOMPLETE_JSON_FIELD_RE.finditer(text):
        if m.group(1) == field:
            raw_val = m.group(2)
            # 处理常见转义序列(\\n \\" \\\\ 等)
            return raw_val.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return ""


# ============================================================
# 对话落库辅助(_add_conversation 已收敛至 runtime/conversation,别名导入)
# ============================================================


# ============================================================
# Plan 提取(复用 <plan> 格式;解析实现收敛于 runtime/plan.py,
# 与 react_agent 共用同一实现,消除手抄副本)
# ============================================================


# ACP plan 通知状态 → 前端状态(completed → done)
_ACP_PLAN_STATUS_MAP = {"completed": "done"}


def _extract_plan(content: str) -> list[dict] | None:
    """从 content 提取 <plan>...</plan> 计划清单(委托 runtime/plan.py)

    优先 JSON 格式(对象数组),回退逐行格式([status] 文本)。
    无 plan 块时返回 None。
    """
    from app.agents.runtime.plan import extract_plan

    return extract_plan(content)


def _format_plan_reminder(plan_steps: list[dict]) -> str:
    """格式化 plan 状态,注入 prompt 让 CLI 续接进度

    文案实现收敛于 app/prompts/executor.py 的 format_plan_reminder(variant="cli",
    中立措辞兼容 CLI 原生 TodoList 等计划工具;与内置 react_agent 侧的
    react 变体同源集中管理,消除手抄副本)。
    """
    return format_plan_reminder(plan_steps, variant="cli")


# ============================================================
# 项目记忆精简版加载(注入 CLI prompt)
# ============================================================


def _load_project_memory_summary(db: Session, task: Task) -> str:
    """加载项目记忆精简版(注入 CLI prompt 用)。

    委托 memory_injection.load_project_memory_brief(与内置 react_agent
    共用同一数据源):memory_summary 优先,为空回退 memory_content 截断
    ——旧数据(未生成 summary)同样注入,消除两侧回退行为分叉。
    无 Project / 无 repo_url / 匿名任务 / 查询异常 → 返回 ""(不注入)。
    完整记忆已由 orchestrator 在 clone 后写入沙箱文件,这里只取精简版注入 prompt。
    """
    try:
        if task.user_id is None:
            return ""
        params = task.params or {}
        repo_url = params.get("repo_url")
        if not repo_url:
            return ""
        from app.services.memory_injection import load_project_memory_brief

        memory_text, _alias = load_project_memory_brief(db, task.user_id, repo_url)
        return memory_text
    except Exception as e:
        logger.warning(f"[task={task.id}] 加载项目记忆精简版失败(忽略): {e}")
        return ""


def _load_global_memory(db: Session, task: Task) -> str:
    """加载全局长期记忆段(跨项目通用经验,注入 CLI prompt)。

    委托 build_global_memory_section(与 react_agent 共用同一注入逻辑)。
    匿名任务 / 无全局记忆 / 查询异常 → 返回 ""(不注入)。

    show_pointer=False:外部 CLI 的"查全量"指针由 build_cli_memory_section 按运行模式
    统一产出(避免双指针 + local 模式失效的 /home/user 路径),此处只回纯内容。
    """
    try:
        if task.user_id is None:
            return ""
        from app.services.memory_injection import build_global_memory_section

        return build_global_memory_section(db, task.user_id, show_pointer=False)
    except Exception as e:
        logger.warning(f"[task={task.id}] 加载全局记忆失败(忽略): {e}")
        return ""


# ============================================================
# Prompt 消息构造
# ============================================================


def _build_prompt_message(
    task: Task,
    round_idx: int,
    followup_query: str | None,
    repo_context: str | None,
    repo_path: str,
    previous_plan: list[dict] | None,
    memory_summary: str = "",
    global_memory: str = "",
    history_replay: str = "",
    skills_section: str = "",
) -> str:
    """构造发给 CLI 的完整 prompt 消息(历史回放 + 预 clone 上下文 + 记忆注入段 + 纯指令)

    落库展示用 _build_base_prompt(纯指令);预 clone 上下文段、记忆段与
    跨轮历史回放都只进发送内容,不落库不展示。

    skills_section:CLI 可用技能注入段,与 run 链路一致并入记忆桶末尾(默认空,
    保持既有调用/单测行为不变)。

    run_acp_agent 不直接调本函数(它需要在同一 session 内跳过未变化的注入段),
    但两者均走 _compose_send_text,段落顺序因此一致。
    """
    variant = "upload" if _has_creation_upload(task.params) else "clone"
    return _compose_send_text(
        _build_base_prompt(
            task, round_idx, followup_query, repo_context, repo_path, previous_plan,
        ),
        _build_repo_context_section(
            repo_context if followup_query is None else None,
            variant,
        ),
        _build_memory_section(memory_summary, global_memory) + skills_section,
        history_replay,
    )


def _build_base_prompt(
    task: Task,
    round_idx: int,
    followup_query: str | None,
    repo_context: str | None,
    repo_path: str,
    previous_plan: list[dict] | None,
) -> str:
    """构造发给 CLI 的纯指令部分(不含预 clone 上下文与记忆注入段)

    - 第 1 轮:task.user_input + 仓库信息
    - 追问轮:基于已有仓库继续,注入跨轮记忆(plan 续接)

    这部分落库到 Conversation(前端展示);预 clone 上下文段与记忆段
    单独拼接只进发送内容(属系统编排信息,非用户原话)。
    """
    if followup_query is None:
        # 委托共享拼装(与 create_task 落库 / 内置 react_agent 首轮完全一致,
        # 修复此前 CLI 首轮与落库展示不同步的双轨不一致;上传任务会带上
        # "用户上传的文件已放入任务工作区"行)
        msg = build_first_round_question(task.user_input, task.params)

        # 预 clone 上下文见 _build_repo_context_section(只进发送内容);
        # 未预 clone 但有路径时,展示仓库路径供用户确认
        if not repo_context and repo_path:
            msg += f"\n仓库路径: {repo_path}"
    else:
        msg = (
            "基于之前的执行进度,请处理以下新消息"
            f"({FOLLOWUP_CORE_GUIDANCE})"
        )
        # 仅真实 git 仓库才提示"仓库路径(已 clone)":纯上传任务的 repo_path 指向
        # uploaded_files/,该措辞会误导(工作区文件路径已由 session cwd 提供);
        # 且只有工作区确实有文件才声称"已 clone"(预 clone 可能降级为空目录)
        if (
            (task.params or {}).get("repo_url")
            and repo_path
            and sandbox_tools.workspace_has_files(str(task.id))
        ):
            msg += f"\n仓库路径(已 clone,无需再 clone): {repo_path}"
        msg += f"\n\n[本轮补充要求]\n{followup_query}"

    if previous_plan:
        reminder = _format_plan_reminder(previous_plan)
        if reminder:
            msg += f"\n\n{reminder}"

    return msg


def _build_repo_context_section(
    repo_context: str | None, variant: str = "clone",
) -> str:
    """构造仓库/上传文件上下文段(拼在发送给 CLI 的 prompt 中,不落库不展示)

    文案实现收敛于 app/prompts/executor.py 的 build_cli_repo_context_section
    (与内置 react_agent 侧同源集中管理,消除双轨手抄)。

    variant:
    - "clone":orchestrator 主动 clone 成功后注入,提示 CLI 跳过 clone_repo。
      属系统编排信息而非用户原话,与记忆注入段同样处理:落库内容保持纯净。
    - "upload":工作区是用户上传的文件,直接陈述位置(不提 clone,
      修复此前上传任务被包"仓库已预先 clone"与正文矛盾的退化)。
    repo_context 为空时返回空串。
    """
    return build_cli_repo_context_section(repo_context, variant)


def _build_memory_section(
    memory_summary: str = "", global_memory: str = "",
    *,
    project_file_path: str = "/home/user/.agent_memory/project_memory.md",
    global_file_path: str = "/home/user/.agent_memory/global_memory.md",
) -> str:
    """构造记忆注入段(拼在发送给 CLI 的 prompt 末尾,不落库不展示)

    文案实现收敛于 app/prompts/executor.py 的 build_cli_memory_section
    (与内置 react_agent 侧的包装同源集中管理)。
    两部分都为空时返回空串。
    project_file_path/global_file_path 由调用处按运行模式传入(见 resolve_agent_memory_file_path)。
    """
    return build_cli_memory_section(
        memory_summary, global_memory,
        project_file_path=project_file_path, global_file_path=global_file_path,
    )


def _build_cli_skills_section(task: Task, mode: str, local_dir) -> str:
    """构造"可用技能"注入段(外部 CLI 执行器专用,拼进 prompt,不落库不展示)。

    orchestrator 已在任务启动时把可见且允许的 skill 物化进容器
    (_write_skill_files_for_task);这里按同一可见集算出每个 SKILL.md 的容器绝对
    路径(子目录名用 loader.skill_subdir_name,与物化写入同源)并拼清单+指针。
    无可用 skill / 异常 → 返回空串(不注入,不拖垮执行轮)。
    """
    try:
        from app.services.memory_injection import resolve_agent_skills_dir_path
        from app.skills import loader as skill_loader

        skills = skill_loader.resolve_visible_skills(task.user_id, task.allowed_skills)
        if not skills:
            return ""
        skills_dir = resolve_agent_skills_dir_path(mode, local_dir)
        items = [
            {
                "name": s.name,
                "description": s.description,
                "file": f"{skills_dir}/{skill_loader.skill_subdir_name(s.name)}/SKILL.md",
            }
            for s in skills
        ]
        return build_cli_skills_section(items)
    except Exception as e:
        logger.warning(f"[task={task.id}] 构造 CLI skill 注入段失败(忽略): {e}")
        return ""


# ============================================================
# 发送文本装配(稳定注入段在前,本轮指令置后)
# ============================================================


def _compose_send_text(
    base_msg: str,
    repo_ctx_section: str = "",
    memory_section: str = "",
    history_replay: str = "",
) -> str:
    """装配实际发给 CLI 的文本:稳定的系统注入段在前,本轮指令在后

    段落顺序(与此前"指令在最前 + 上下文在尾"相反):
    1. 跨轮历史回放(属"过去"的内容,也最稳定)
    2. 预 clone / 上传上下文
    3. 项目记忆 + 全局记忆
    4. 本轮指令(用户原话 / 追问原文 / plan 状态提醒)

    理由:本轮要求落在末尾(模型对末尾指令最敏感),而前几段在同一 session
    内保持逐字不变,便于 CLI 侧与模型服务端的提示词前缀缓存命中。
    空段直接跳过;段间分隔统一由本函数补(注入段自身已以 \n\n 开头时不重复加,
    纯指令不带前导空行,靠这里补上分隔)。
    """
    parts = [
        part for part in (history_replay, repo_ctx_section, memory_section, base_msg)
        if part
    ]
    if not parts:
        return ""
    text = parts[0]
    for part in parts[1:]:
        text += part if part.startswith("\n") else "\n\n" + part
    return text


def _sha1_text(text: str) -> str:
    """逐字指纹(空串也有稳定值,便于统一比较)"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _context_sections_hash(
    repo_ctx_section: str, memory_section: str,
) -> dict[str, str]:
    """两类注入段各自的指纹(内容逐字一致 → 指纹一致)

    分桶而非合并哈希:两类内容各自变化(首轮后预 clone 段就不再拼,而记忆段
    可能一直未变),合并指纹会让一段变化连带把另一段重发一遍。
    """
    return {
        "repo": _sha1_text(repo_ctx_section),
        "memory": _sha1_text(memory_section),
    }


def _record_prompt_accepted(
    task_id: str, ctx_hash: dict[str, str], round_idx: int,
) -> None:
    """prompt 成功送达后的善后:回写注入段指纹 + "已收下消息"标记 + 送达轮次

    三个事实一起记是有意为之:指纹只判"内容是否已给过这个 session",
    last_accepted_round 判"transcript 覆盖到哪一轮",而业务性失败轮不走到
    这里 → 一起保持在"尚未送达"状态,下一轮全量重发 + 回放历史。
    缓存不在(已清/已停)则静默跳过。
    """
    with _bridge_cache_lock:
        entry = _bridge_cache.get(task_id)
        if entry is not None:
            entry["injected_context_hash"] = ctx_hash
            entry["prompt_accepted"] = True
            entry["last_accepted_round"] = round_idx


def _session_has_context(reused: dict[str, Any]) -> bool:
    """该 session 是否真的已持有之前的对话上下文(至少成功收下过一次 prompt)

    为何需要这个标记:条目在 session/new 后、prompt 前就写入缓存,而 prompt 抛
    JSON-RPC 业务错误(RuntimeError)时保留缓存 —— 此时 CLI 进程仍存活、/health
    通过,下一轮会复用一个从未收到过消息的空 session。若仅凭 reused 非空就判定
    "上下文随 session 延续",跨轮历史回放永不执行(失忆窗口)。
    旧条目缺该 key 时视为已送达(写入时已显式置 False,只有历史遗留条目会走到
    默认分支 —— 它们旧版链路总是先写缓存紧接发 prompt,session 确实有上下文)。
    """
    return bool(reused.get("prompt_accepted", True))


def _resolve_injection_plan(
    reused: dict[str, Any] | None,
    repo_ctx_section: str,
    memory_section: str,
    round_idx: int = 0,
) -> tuple[str, str, int | None, dict[str, str]]:
    """决定本轮的系统注入与历史回放内容(注入策略集中在此,便于单测)

    返回 (实际注入的仓库上下文段, 实际注入的记忆段, 回放下界, 注入段指纹)。
    回放下界 replay_from:None=不回放;0=全量回放(轮次 < round_idx);
    k>0=增量回放(k < 轮次 < round_idx,session 已覆盖 ≤k 的轮次)。

    - 走全新链路(reused 为空,含沙箱重建/后端重启/启动参数或凭证变化)→
      session 对之前轮次一无所知,全量回放,且注入段全量重发
    - 复用 session 但上一轮 prompt 从未成功送达(缓存条目 prompt_accepted 为假,
      如 JSON-RPC 业务错误保留缓存而 CLI 仍存活)→ 同全新链路处理:注入段全量
      重发 + 全量回放(否则第 1~N-1 轮上下文会静默丢失)
    - 复用/恢复已送达过的 session,但 last_accepted_round 落后于 round_idx-1
      (恢复会话只覆盖到记录轮次,其后失败轮的提问从没进过 transcript;重试消息
      build_retry_message 不复述原提问)→ 对缺失轮次做**增量回放**;注入段仍按
      指纹去重(更早轮次给过的段不重发)
    - 复用已送达过的 session 且某注入段逐字未变 → 该段置空:上一轮已发过,
      内容已在 CLI 侧对话上下文里,重发只是重复占 token(对齐 Codex
      reference_context_item 的 diff 思路);另一段变化不影响本判定
    - 指纹永远按未裁剪的原内容计算:内容变化时指纹变化 → 下一轮重新注入
    - 旧格式条目缺 last_accepted_round 时视为"不缺轮次"(热更新遗留条目,
      与接入前行为一致,不凭空多回放)
    """
    ctx_hash = _context_sections_hash(repo_ctx_section, memory_section)
    if reused is None or not _session_has_context(reused):
        # 新 session / 复用一个实际空白的 session:全量注入 + 全量回放
        return repo_ctx_section, memory_section, 0, ctx_hash

    previous = reused.get("injected_context_hash") or {}
    repo_out = "" if previous.get("repo") == ctx_hash["repo"] else repo_ctx_section
    mem_out = "" if previous.get("memory") == ctx_hash["memory"] else memory_section

    last_round = reused.get("last_accepted_round")
    if isinstance(last_round, int) and round_idx >= 1 and last_round < round_idx - 1:
        # session 有上下文但缺 (last_round, round_idx) 的轮次:增量补发
        return repo_out, mem_out, last_round, ctx_hash
    return repo_out, mem_out, None, ctx_hash


def _restore_or_new_session(
    client: ACPClient,
    decision: dict[str, Any],
    *,
    db: Session,
    task: Task,
    agent_type: str,
    session,
    bridge_exec_id: str,
    cwd: str,
) -> tuple[str, bool, str]:
    """按恢复决策打开 ACP 会话,返回 (acp_session_id, 是否恢复成功, 结果标记)

    decision 来自 runtime.acp_session.plan_session_open;结果标记供日志/perf 定位
    本轮到底走了哪条链路("restored" / "new" / "restore_failed_new")。

    - decision 无 method → 直接 session/new(下游靠 _load_history_replay 文本回放)
    - 恢复失败(会话已被清理 / 磁盘状态随沙箱销毁 / CLI 实际不支持)→ 降级新建,
      并删除过期记录,避免每轮都撞一次;**仅 JSON-RPC 业务错误清记录**——
      传输层瞬时失败(httpx 超时/bridge 瞬断/ACPStreamAborted)磁盘会话多半仍
      完好,保留记录下轮再试,不清就永久退回低保真文本回放
    - session/new 失败时把 bridge 里的 CLI stderr 附进异常:-32603 Internal error
      的 JSON-RPC 响应只有泛化消息,真实异常在 CLI stderr(经 _pump_stderr 转发)
    """
    if decision.get("method"):
        try:
            client.restore_session(
                decision["method"], decision["session_id"], decision["cwd"],
            )
            return str(decision["session_id"]), True, "restored"
        except Exception as e:
            transient = isinstance(
                e, (httpx.HTTPError, ConnectionError, ACPStreamAborted)
            )
            logger.warning(
                f"[{agent_type}] 会话恢复失败({decision['method']}, "
                f"sessionId={decision['session_id']}, "
                f"{'传输层瞬时失败,保留记录下轮再试' if transient else '业务错误,清除记录'}),"
                f"降级 session/new: {e}"
            )
            if not transient:
                clear_session_record(db, task, agent_type)

    try:
        return client.new_session(cwd=cwd), False, (
            "restore_failed_new" if decision.get("method") else "new"
        )
    except RuntimeError as e:
        bridge_detail = _extract_bridge_error(session, bridge_exec_id, agent_type)
        if bridge_detail:
            raise RuntimeError(f"{e}\n\n[CLI 日志]\n{bridge_detail}") from e
        raise


def _load_history_replay(
    db: Session, task: Task, round_idx: int, since_round: int = 0,
) -> str:
    """构造"之前轮次执行记录"回放段(仅新建/恢复的 ACP session 需要)

    背景:ACP session 复用时,之前轮次的对话上下文留在 CLI 进程内自然延续;
    但沙箱重建 / 后端重启 / 启动参数或凭证变化都会走全新链路(session/new),
    新 session 对之前轮次一无所知,追问轮只剩"基于之前的执行进度"就无从续接。

    since_round:增量回放下界(= 恢复/复用会话已覆盖的最后一轮)。全量回放
    传 0(装载全部 < round_idx 的轮次);恢复的会话只缺最近失败轮时传
    last_accepted_round,只补它没见过的轮次。

    数据源与内置 react_agent 完全同源(_build_history_messages:同一张
    Conversation 表 + 三级压缩 + token 预算),此处只多一步"结构化消息 →
    单条文本"的渲染(CLI 的 session/prompt 只有 text 通道)。

    不传 client:CLI 执行链不依赖后端 LLM(模型配额在 CLI 账号侧,后端可能
    未配置 LLM key),因此不走 Level 2 的 LLM 压缩 —— 超预算时由兜底截断
    保留最近轮次(最近轮次对续接最有价值)。

    任何异常降级为"不注入":历史回放是增强项,不能拖垮正常执行轮。
    """
    if round_idx <= 1:
        return ""
    _t0 = time.perf_counter()
    try:
        # 函数级导入:CLI 侧不在模块加载期依赖内置 agent
        from app.agents.react_agent import _build_history_messages

        messages = _build_history_messages(
            db, task.id, round_idx, since_round=since_round,
        )
    except Exception as e:
        logger.warning(f"[task={task.id}] 构造跨轮历史回放失败(忽略,本轮不注入): {e}")
        return ""
    section = build_cli_history_replay_section(messages)
    # [perf] 跨轮历史回放构造(走新 session 的追问轮才执行,含 DB 查询)
    perf_log(
        task.id, "acp_history_replay", time.perf_counter() - _t0,
        round_idx=round_idx, since_round=since_round, replay_chars=len(section),
    )
    return section


# ============================================================
# 主入口:通用 ACP agent 运行流程
# ============================================================


def run_acp_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = "",
    *,
    post_session_setup: Callable[[ACPClient, str, Task], None] | None = None,
    credential_env_builder: Callable[[dict[str, str], Task | None], dict[str, str]] | None = None,
    pre_bridge_hook: Callable[[Any, dict[str, str], str, Task | None], None] | None = None,
    post_bridge_hook: Callable[[Any, dict[str, str], str, Session, Any], None] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 ACP CLI 执行器(通用流程)

    与 run_react_agent 签名对齐(不含 client 参数,外部 CLI 自带模型配置)。
    agent_type 决定使用哪个 CLI(qoder_cli / deepseek_cli / codex_cli)。

    post_session_setup: session/new 之后、prompt 之前执行的回调
        (client, session_id, task) -> None。
        deepseek 用此回调调 set_config_option 设置模型 / 思考强度;
        qoder 不需要(启动参数已含 --yolo)。

    credential_env_builder: 动态构建凭证环境变量的回调
        (credentials: dict[str, str], task: Task | None) -> dict[str, str]。
        若提供,替代默认的 _build_credential_envs 静态映射。
        deepseek 用此回调按 task.params._executor_command_confirm
        决定是否注入 DSH_PERMISSION_MODE(权限模式)。

    pre_bridge_hook: bridge 启动前的沙箱准备回调
        (session, credentials, agent_type, task) -> None。
        在 _ensure_cli_env 之后、_start_acp_bridge 之前执行,
        用于向沙箱写入 CLI 需要的配置文件。
        task 为 None 时(测试连接场景)默认 always_approve。

    post_bridge_hook: bridge 运行后的收尾回调
        (session, credentials, agent_type, db, user_id) -> None。
        置于外层 finally,成功/异常路径都会调用(尽力而为,自身 try/except 兜底),
        以便 codex chatgpt 模式即便本轮失败也能回捞已轮换的 auth.json 并回写用户配置。

    返回:(results, summary, final_plan)
        results: 始终为空 list(结构化结果由 agent2 在 done 时提取)
        summary: 本轮自然语言总结(CLI 的最终文本输出)
        final_plan: 本轮结束时的 plan 状态(从 content 提取 <plan>)
    """
    task_id_str = str(task.id)
    set_current_task(task_id_str, task.scenario)

    # [perf] CLI 执行器进入锚点
    perf_log(
        task.id, "acp_enter",
        agent_type=agent_type, round_idx=round_idx,
        followup=followup_query is not None,
    )

    # ---- 校验 agent 类型已注册 ----
    if get_agent_meta(agent_type) is None:
        raise RuntimeError(f"agent 类型未注册: {agent_type}")

    # ---- 检查沙箱模式 ----
    # local 模式:bridge/CLI 直接跑在宿主机真实环境(无隔离,仅开发/调试),
    # 由 SANDBOX_LOCAL_ALLOW_CLI 控制是否放行
    if settings.SANDBOX_MODE == "local" and not settings.SANDBOX_LOCAL_ALLOW_CLI:
        raise RuntimeError(
            "外部 CLI 执行器在 local 模式下被禁用(SANDBOX_LOCAL_ALLOW_CLI=false)。"
            "可设 SANDBOX_LOCAL_ALLOW_CLI=true 启用(bridge/CLI 将直接运行在宿主机),"
            "或使用 SANDBOX_MODE=sandbox。"
        )
    if settings.SANDBOX_MODE == "local":
        logger.warning(
            f"[task={task.id}] local 模式运行外部 CLI 执行器({agent_type}):"
            f"bridge/CLI 直接跑在宿主机真实环境,无隔离边界;"
            f"凭证经环境变量注入 CLI 进程,请勿用于生产"
        )

    # ---- 加载凭证 + 映射为环境变量 ----
    credentials = _load_credentials(db, task.user_id, agent_type)
    if credential_env_builder:
        # 动态构建(如 deepseek 按 _executor_command_confirm 注入 DSH_PERMISSION_MODE)
        credential_envs = credential_env_builder(credentials, task)
    else:
        credential_envs = _build_credential_envs(credentials, agent_type)
    # chatgpt 模式无环境变量但经 pre_bridge_hook 注入 auth.json,故放行(判断集中在助手函数)
    if _credential_injection_missing(credential_envs, pre_bridge_hook):
        raise RuntimeError("凭证映射为空,无法注入环境变量(请检查 registry 配置)")

    # ---- 获取/创建沙箱会话 ----
    ctx = sandbox_tools._get_or_create_session(task_id_str)
    session = ctx["session"]
    repo_path = ctx.get("repo_path", "")

    # 会话工作目录(新建/恢复都用同一个值,并随会话记录持久化):
    # CLI 按 cwd 定位自己的项目会话目录(如 ~/.qoder/projects/<cwd>/...),
    # 不一致时恢复必然失败,因此判定与持久化都用这一个口径。
    # local 模式 fallback 用 local_dir(宿主机上无 /home/user)
    if getattr(session, "mode", "") == "local":
        cwd = repo_path or str(session.local_dir)
    else:
        cwd = repo_path or BRIDGE_WORK_DIR

    # ---- 加载项目记忆精简版(注入 CLI prompt,完整记忆已在沙箱文件中) ----
    memory_summary = _load_project_memory_summary(db, task)
    # ---- 加载全局长期记忆(跨项目通用经验,影响执行方式) ----
    global_memory = _load_global_memory(db, task)

    # ---- 尝试复用缓存的 bridge + ACP session ----
    # 命中时跳过 ensure_cli_env/pre_bridge_hook/start_bridge/initialize/new_session
    # 合计 ~25s 的重建链路,直接发 prompt;CLI 侧会话上下文随 session 延续。
    fingerprint = _bridge_fingerprint(
        _get_acp_args(task, agent_type), credential_envs
    )
    _t0 = time.perf_counter()
    reused = _try_reuse_bridge(task_id_str, session, agent_type, fingerprint)
    perf_log(
        task.id, "acp_bridge_reuse", time.perf_counter() - _t0,
        agent_type=agent_type, hit=reused is not None,
    )

    if reused is None:
        # ---- 全新链路:准备 CLI 环境 ----
        _t0 = time.perf_counter()
        _ensure_cli_env(session, agent_type)
        perf_log(task.id, "acp_ensure_cli_env", time.perf_counter() - _t0, agent_type=agent_type)

        # ---- 注入 git token 到 credential helper cache ----
        # 让 CLI 在沙箱内能克隆/拉取私有仓库(token 不进环境变量/磁盘文件/命令行)
        _t0 = time.perf_counter()
        _inject_git_credentials_to_cache(session, db, task)
        perf_log(task.id, "acp_inject_git_cred", time.perf_counter() - _t0, agent_type=agent_type)

        # ---- wrapper 层钩子:bridge 启动前的沙箱文件准备 ----
        # codex 用此回调写入 ~/.codex/config.toml(按 task.params 决定 approval_policy)
        # deepseek 不需要(凭证/权限均经环境变量注入,无配置文件)
        # hook 可返回额外环境变量 dict(如 codex local 模式返回 CODEX_HOME),合并注入 bridge
        if pre_bridge_hook:
            _t0 = time.perf_counter()
            hook_envs = pre_bridge_hook(session, credentials, agent_type, task)
            if hook_envs:
                credential_envs = {**credential_envs, **hook_envs}
            perf_log(task.id, "acp_pre_bridge_hook", time.perf_counter() - _t0, agent_type=agent_type)

        # ---- 启动 ACP bridge ----
        _t0 = time.perf_counter()
        bridge_exec_id, bridge_port = _start_acp_bridge(session, credential_envs, task, agent_type=agent_type)
        perf_log(task.id, "acp_start_bridge", time.perf_counter() - _t0, agent_type=agent_type)
    else:
        bridge_exec_id = reused["bridge_exec_id"]

    if reused is not None:
        # 复用路径:缓存的 endpoint 已被 _bridge_alive 健康检查验证可用,
        # 跳过 get_endpoint(SDK 端口转发调用,resume 场景实测最长 ~70s)
        endpoint_url, endpoint_headers = reused["endpoint_url"], reused["endpoint_headers"]
    else:
        _t0 = time.perf_counter()
        # local 模式 bridge 用动态分配端口(_start_acp_bridge 返回),
        # sandbox 模式固定 ACP_BRIDGE_PORT
        endpoint_url, endpoint_headers = session.get_endpoint(bridge_port)
        perf_log(task.id, "acp_get_endpoint", time.perf_counter() - _t0, agent_type=agent_type)

    # 会话恢复相关状态(复用链路不恢复,保持默认值)
    session_restored = False
    restored_hash: dict[str, str] = {}
    restored_round = 0
    restore_method_used: str | None = None

    try:
        if reused is None:
            _t0 = time.perf_counter()
            bridge_health = _wait_for_bridge_ready(
                session, bridge_exec_id, endpoint_url, endpoint_headers, agent_type
            )
            perf_log(task.id, "acp_wait_bridge_ready", time.perf_counter() - _t0, agent_type=agent_type)
            try:
                bridge_protocol = int(bridge_health.get("bridge_protocol") or 1)
            except (TypeError, ValueError):
                bridge_protocol = 1

        # ---- ACP 通信 ----
        recorder = _ACPRecorder(task.id, round_idx)

        # permission_handler:CLI 发来 request_permission 时(危险命令确认),
        # 推 SSE 给前端 CommandConfirmDialog,阻塞等待用户确认。
        # 仅在 CLI 关闭 yolo 模式时才会被调用(always_approve 模式下 CLI 开 yolo 不会发请求)。
        def _permission_handler(payload: dict) -> dict:
            """处理 CLI 的 request_permission:推前端确认弹窗,阻塞等用户决议"""
            command = payload.get("command", "")
            description = payload.get("description", "") or command
            perm_id = payload.get("id", "")
            tool_call_kind = payload.get("kind", "")

            # 构造 command_confirm 事件载荷(与 local 模式一致的字段结构)
            command_desc = {
                "command_id": f"acp_{perm_id}",
                "command": command,
                "tool": f"cli:{agent_type}" + (f":{tool_call_kind}" if tool_call_kind else ""),
                "reason": description,
            }
            request_command_confirm(task.id, command_desc)
            approved = wait_for_command_confirm(task.id, command_desc["command_id"])
            if approved:
                # 用户同意:返回 allow_once(不记忆,下次再问)
                return {"outcome": "selected", "option_id": "allow_once"}
            else:
                return {"outcome": "rejected"}

        client = ACPClient(endpoint_url, endpoint_headers, recorder=recorder,
                           permission_handler=_permission_handler)
        try:
            if reused is not None:
                # 复用路径:bridge/CLI 已完成 initialize,session 存于 CLI 进程内,
                # 直接用缓存的 session_id 发 prompt(对话上下文随 session 延续)
                acp_session_id = reused["acp_session_id"]
            else:
                # 握手
                _t0 = time.perf_counter()
                init_result = client.initialize()
                perf_log(task.id, "acp_initialize", time.perf_counter() - _t0, agent_type=agent_type)

                # 跳过 authenticate,直接 session/new
                # 凭证经环境变量注入后内部已自动认证,session/new 可直接成功。
                # (authenticate 在沙箱无 TTY 环境下会静默挂起)
                auth_methods = init_result.get("authMethods", []) or []
                if auth_methods:
                    logger.info(
                        f"[{agent_type}] 跳过 authenticate(凭证经环境变量自动认证),"
                        f"authMethods={[m.get('id') for m in auth_methods]}"
                    )

                # 打开会话:优先让 CLI 恢复自己磁盘上持久化的会话(含工具调用与
                # 思考过程,保真度高于文本回放且不占 prompt token);判定不满足或
                # 恢复失败则新建 session(新建后由 _load_history_replay 文本回放兜底)
                restore = plan_session_open(
                    init_result=init_result,
                    record=load_session_record(task.params, agent_type),
                    bridge_protocol=bridge_protocol,
                    cwd=cwd,
                )
                _t0 = time.perf_counter()
                acp_session_id, session_restored, open_outcome = _restore_or_new_session(
                    client, restore,
                    db=db, task=task, agent_type=agent_type,
                    session=session, bridge_exec_id=bridge_exec_id, cwd=cwd,
                )
                if session_restored:
                    restored_hash = restore["injected_context_hash"]
                    restored_round = int(restore.get("round_idx") or 0)
                    restore_method_used = restore["method"]
                perf_log(
                    task.id, "acp_session_open", time.perf_counter() - _t0,
                    agent_type=agent_type, outcome=open_outcome,
                    decision=restore.get("method") or restore.get("reason") or "",
                )

                # ---- wrapper 层钩子:session/new 后的自定义设置 ----
                # deepseek 在此调 set_config_option(model/reasoning_effort) 等
                if post_session_setup:
                    _t0 = time.perf_counter()
                    post_session_setup(client, acp_session_id, task)
                    perf_log(task.id, "acp_post_session_setup", time.perf_counter() - _t0, agent_type=agent_type)

                # ---- 写入复用缓存(bridge 不再每轮停止,供后续轮次/resume 复用) ----
                with _bridge_cache_lock:
                    _bridge_cache[task_id_str] = {
                        "session": session,
                        "agent_type": agent_type,
                        "bridge_exec_id": bridge_exec_id,
                        "endpoint_url": endpoint_url,
                        "endpoint_headers": endpoint_headers,
                        "acp_session_id": acp_session_id,
                        "fingerprint": fingerprint,
                        # 本 session 已注入过的系统段指纹 + 是否至少成功收下过一次
                        # prompt(两者只在 prompt 成功后回写,见 _record_prompt_accepted)。
                        # 恢复成功的 session 例外:它已从磁盘复原了上下文与既往注入段,
                        # 因此初始就是“已送达”+记录里的指纹+记录覆盖到的轮次
                        "injected_context_hash": restored_hash,
                        "prompt_accepted": session_restored,
                        "last_accepted_round": restored_round,
                    }

            # ---- 构造 prompt 消息 ----
            # 落库只存纯指令(前端展示不含预 clone 上下文与记忆注入段);
            # 实际发送时再拼上系统注入段(不展示给用户,装配顺序见 _compose_send_text)
            base_msg = _build_base_prompt(
                task, round_idx, followup_query, repo_context, repo_path, previous_plan,
            )

            # 幂等落库:首轮提问已在任务创建时(create_task)落库,此处跳过避免
            # 重复记录;追问轮/续跑轮首次进入时该轮尚无 question,正常落库。
            _existing_question = (
                db.query(Conversation.id)
                .filter(
                    Conversation.task_id == task.id,
                    Conversation.round_idx == round_idx,
                    Conversation.role == "user",
                    Conversation.type == "question",
                )
                .first()
            )
            if _existing_question is None:
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="user", type="question",
                    content=base_msg,
                )

            # ---- 系统注入段(只进发送内容,不落库)----
            # 上传任务走 upload 包裹变体(不称"已预先 clone",与正文一致)
            _repo_ctx_variant = (
                "upload" if _has_creation_upload(task.params) else "clone"
            )
            raw_repo_ctx = _build_repo_context_section(
                repo_context if followup_query is None else None,
                _repo_ctx_variant,
            )
            # 记忆"查全量"指针按运行模式给出 CLI 可直达的绝对路径(后端 read_file 对
            # /home/user 的映射仅内置侧有效,外部 CLI 用各自的 Read 工具读不到)
            from app.services.memory_injection import resolve_agent_memory_file_path

            _mem_mode = getattr(session, "mode", "") or ctx.get("mode", "")
            _mem_local_dir = ctx.get("local_dir")
            _project_mem_path = resolve_agent_memory_file_path(
                _mem_mode, _mem_local_dir, sandbox_tools._MEMORY_FILE,
            )
            _global_mem_path = resolve_agent_memory_file_path(
                _mem_mode, _mem_local_dir, sandbox_tools._GLOBAL_MEMORY_FILE,
            )
            raw_memory = _build_memory_section(
                memory_summary, global_memory,
                project_file_path=_project_mem_path, global_file_path=_global_mem_path,
            )
            # skill 注入段并入记忆桶一起参与去重(skill 集在任务内恒定,合并无害);
            # 独立成第三桶需改 _resolve_injection_plan/_context_sections_hash 及其单测,
            # 收益(仅"skill 单独变化时不重发记忆")不足以覆盖改动面,故复用记忆桶。
            raw_skills = _build_cli_skills_section(task, _mem_mode, _mem_local_dir)
            if raw_skills:
                raw_memory = raw_memory + raw_skills
            # 判定用的 session 状态:
            # - 复用进程内 session → 直接用缓存条目
            # - 恢复成功的 session → CLI 已从磁盘复原上下文(含既往注入段),
            #   按"复用"口径处理:不重发未变化的注入段;transcript 只覆盖到
            #   记录的 round_idx,其后若有失败轮(提问进了 DB 但没进
            #   transcript),按 last_accepted_round 增量回放补齐
            # - 新建 session → None → 全量注入 + 全量文本回放
            injection_state = reused if reused is not None else (
                {
                    "injected_context_hash": restored_hash,
                    "prompt_accepted": True,
                    "last_accepted_round": restored_round,
                }
                if session_restored else None
            )
            repo_ctx_section, memory_section, replay_from, ctx_hash = _resolve_injection_plan(
                injection_state, raw_repo_ctx, raw_memory, round_idx=round_idx,
            )
            if (raw_repo_ctx and not repo_ctx_section) or (
                raw_memory and not memory_section
            ):
                logger.info(
                    f"[task={task.id}] {agent_type} 同一 ACP session 内注入段未变化,"
                    f"本轮不重复注入(仓库上下文/记忆段已在 CLI 上下文中):"
                    f"skipped_repo={bool(raw_repo_ctx and not repo_ctx_section)}, "
                    f"skipped_memory={bool(raw_memory and not memory_section)}"
                )
            history_replay = ""
            if replay_from is not None:
                history_replay = _load_history_replay(
                    db, task, round_idx, since_round=replay_from,
                )
                if replay_from > 0:
                    logger.info(
                        f"[task={task.id}] {agent_type} 会话上下文只覆盖到第 {replay_from} 轮,"
                        f"增量回放第 {replay_from + 1}~{round_idx - 1} 轮(补发失败轮的提问)"
                    )
            elif session_restored:
                logger.info(
                    f"[task={task.id}] {agent_type} 已恢复 CLI 持久化会话"
                    f"({restore_method_used}, sessionId={acp_session_id}),本轮不回放文本历史"
                )

            user_msg = _compose_send_text(
                base_msg, repo_ctx_section, memory_section, history_replay,
            )

            # ---- 流式发送 prompt ----
            collector = _ACPCollector(
                task, db, round_idx,
                agent_type=agent_type,
            )
            # 异步子 Agent 续轮回收状态(供收尾标注是否仍不完整)
            _async_ctx = {"continued": False, "still_pending": False}

            try:
                # auto_renew:prompt 期间 CLI 用自带 bash,不触发后端访问续期,
                # 单轮长执行可能拖过 TTL,后台线程周期性 renew 沙箱
                with session.auto_renew():
                    # [perf] prompt 发送锚点(CLI 侧 TTFT 由 acp_first_event 记录)
                    perf_log(task.id, "acp_prompt_send", round_idx=round_idx, msg_chars=len(user_msg))
                    result = client.prompt(
                        acp_session_id,
                        [{"type": "text", "text": user_msg}],
                        on_event=collector,
                        # 挂死兜底:按活动工具状态分级 idle 超时(见 PromptIdleTimeout)
                        idle_probe=lambda: collector.has_active_tools,
                    )
                    # 本轮 prompt 已送达:记录注入段指纹与"session 有上下文"标记,
                    # 供同 session 下一轮去重与回放判定(业务错误异常不会走到这里,
                    # 下一轮因此仍会全量注入 + 回放,不会复用空 session 而丢历史)
                    _record_prompt_accepted(task_id_str, ctx_hash, round_idx)
                    # 会话记录持久化:后端重启 / bridge 重建后仍可凭 sessionId 让
                    # CLI 恢复上下文(指纹一并存,恢复后才知道哪些注入段已给过)。
                    # 截断兜底轮(idle 挂死/流中断降级收尾)不推进记录:该轮内容
                    # 可能没被 CLI 写进磁盘 transcript,记录留在上一次干净轮,
                    # 恢复后的增量回放才能把它补上
                    _session_record = build_session_record(
                        agent_type=agent_type,
                        session_id=acp_session_id,
                        cwd=cwd,
                        injected_context_hash=ctx_hash,
                        round_idx=round_idx,
                        restore_method=restore_method_used,
                        truncated=bool(client.last_prompt_truncated),
                    )
                    if _session_record is not None:
                        save_session_record(db, task, _session_record)

                    # ---- 异步子 Agent 提前收尾:同一活跃 session 续轮回收 ----
                    # 签名:本轮派生过后台子 Agent + 未被 idle/流中断截断 +
                    # final 文本仍呈"在等结果"口吻 → 判为提前 end_turn。
                    # 实测(qwen3.8-flash):同 session 补发一条 session/prompt,
                    # CLI 会把已完成的后台子 Agent 结果注入下一轮并产出完整报告。
                    if (
                        collector.async_agent_launch_count > 0
                        and _async_agent_autoccontinue_enabled(agent_type)
                        and not client.last_prompt_truncated
                        and _looks_like_pending_async_report(collector.content_full)
                    ):
                        _async_ctx["continued"] = True
                        logger.warning(
                            f"[task={task.id}] {agent_type} 第 {round_idx} 轮检测到异步子 Agent "
                            f"提前 end_turn(派生 {collector.async_agent_launch_count} 个),"
                            f"启动同 session 续轮回收"
                        )
                        publish(task.id, "thinking_delta", {
                            "conv_id": collector.current_conv_id,
                            "round_idx": round_idx,
                            "role": "agent1",
                            "phase": "error",
                            "delta": "[检测到后台子任务未回报,正在续轮回收结果…]",
                            "iteration": collector.iteration,
                        })
                        try:
                            _async_agent_collect_results(
                                client, acp_session_id, collector, _async_ctx,
                                task=task, round_idx=round_idx, agent_type=agent_type,
                            )
                        except Exception as e:  # 续轮回收失败不拖垮本轮,保留已累积输出收尾
                            logger.warning(
                                f"[task={task.id}] {agent_type} 异步子 Agent 续轮回收异常(忽略,用已累积输出收尾): {e}"
                            )

            except Exception as e:
                logger.exception(f"[task={task.id}] ACP prompt 失败 ({agent_type})")
                # 连接层失败(bridge/CLI 已死)时清缓存,下次走全新链路;
                # ACP 业务错误保留缓存(session 仍有效)。
                if isinstance(e, (httpx.HTTPError, ConnectionError)):
                    with _bridge_cache_lock:
                        _bridge_cache.pop(task_id_str, None)
                publish(task.id, "thinking_delta", {
                    "conv_id": collector.current_conv_id,
                    "round_idx": round_idx,
                    "role": "agent1",
                    "phase": "error",
                    "delta": f"[CLI 调用失败: {e}]",
                    "iteration": collector.iteration,
                })
                raise
            finally:
                recorder.close()
                collector.close()

        finally:
            client.close()

    finally:
        # bridge 保持运行(写入/已在 _bridge_cache),供后续轮次/resume 复用;
        # 仅在初始化阶段失败且未入缓存时停掉,避免残留坏进程。
        with _bridge_cache_lock:
            _cached = _bridge_cache.get(task_id_str) is not None
        if not _cached:
            _stop_acp_bridge(session, bridge_exec_id, agent_type)

        # ---- wrapper 层钩子:bridge 运行后的收尾(置于 finally,异常路径也尽量回捞) ----
        # codex chatgpt 模式:即便本轮 prompt 失败/bridge 挂掉,codex 可能已轮换 auth.json
        # 中的 token,不回捞会让旧 refresh_token 逐步失效(正是此 hook 要解决的问题)。
        # 无变化不写库;自身 try/except 兜底,不会掩盖上方正在传播的异常。
        if post_bridge_hook:
            try:
                post_bridge_hook(session, credentials, agent_type, db, task.user_id)
            except Exception as e:
                logger.warning(f"[task={task.id}] {agent_type} post_bridge_hook 失败(忽略): {e}")

    # ---- 提取 summary 和 plan ----
    summary = collector.content_full or ""
    if not summary:
        summary = f"执行完成({agent_type},{collector.tool_call_count} 次工具调用)"

    # 兜底提前终止了 prompt(idle 挂死超时 / CLI 崩溃连接中断):在 summary 里
    # 标注截断,让 agent2 评估时知道本轮输出不完整(任务不 fail,照常评估/追问;
    # 前端同步推 error 提示)
    if client.last_prompt_truncated:
        trunc_reason = client.last_prompt_truncated
        logger.warning(
            f"[task={task.id}] {agent_type} 第 {round_idx} 轮被兜底提前终止: {trunc_reason},"
            f"用已累积输出({len(collector.content_full)}字符)收尾"
        )
        publish(task.id, "thinking_delta", {
            "conv_id": collector.current_conv_id,
            "round_idx": round_idx,
            "role": "agent1",
            "phase": "error",
            "delta": f"[本轮提前终止: {trunc_reason}]",
            "iteration": collector.iteration,
        })
        summary += (
            f"\n\n[系统注记:本轮执行提前终止({trunc_reason}),"
            "以上为终止前已输出的内容,可能不完整]"
        )

    # 异步子 Agent 续轮回收后的状态标注(C:状态诚实,避免裸显"已完成")
    if _async_ctx["continued"]:
        if _async_ctx["still_pending"]:
            logger.warning(
                f"[task={task.id}] {agent_type} 第 {round_idx} 轮后台子任务续轮后仍未全部回报"
            )
            publish(task.id, "thinking_delta", {
                "conv_id": collector.current_conv_id,
                "round_idx": round_idx,
                "role": "agent1",
                "phase": "error",
                "delta": "[后台子任务未全部回报,已尝试续轮回收但结果可能仍不完整]",
                "iteration": collector.iteration,
            })
            summary += (
                "\n\n[系统注记:本轮派生的后台子任务在续轮回收后仍未全部回报,"
                "以上结果可能不完整,可稍后追问以获取完整报告]"
            )
        else:
            publish(task.id, "thinking_delta", {
                "conv_id": collector.current_conv_id,
                "round_idx": round_idx,
                "role": "agent1",
                "phase": "error",
                "delta": "[已回收后台子任务结果并合并为完整报告]",
                "iteration": collector.iteration,
            })

    current_plan: list[dict] = [dict(s) for s in (previous_plan or [])]
    extracted = _extract_plan(collector.content_full)
    if extracted:
        current_plan = extracted
        publish(task.id, "plan", {
            "round_idx": round_idx,
            "steps": current_plan,
        })
    elif collector.last_todo_plan:
        # TodoList 计划已在 completed 时推送过 plan 事件,这里只续接跨轮链
        current_plan = [dict(s) for s in collector.last_todo_plan]

    logger.info(
        f"[task={task.id}] {agent_type} 第 {round_idx} 轮完成: "
        f"content={len(collector.content_full)}字符, "
        f"reasoning={len(collector.reasoning_full)}字符, "
        f"tool_calls={collector.tool_call_count}"
    )

    return [], summary, current_plan


# ============================================================
# 通用流式凭证测试
# ============================================================


def test_credential_streaming(
    db: Session,
    user_id,
    agent_type: str,
    *,
    post_session_setup: Callable[[ACPClient, str, Task | None], None] | None = None,
    test_acp_args: list[str] | None = None,
    credential_env_builder: Callable[[dict[str, str], Task | None], dict[str, str]] | None = None,
    pre_bridge_hook: Callable[[Any, dict[str, str], str, Task | None], None] | None = None,
    post_bridge_hook: Callable[[Any, dict[str, str], str, Session, Any], None] | None = None,
) -> Generator[dict, None, None]:
    """流式版测试凭证:yield SSE 事件 dict(供路由层格式化为 SSE)

    与各 wrapper 的 test_credential 验证流程一致,但把各阶段进度、思考增量、
    回答增量实时 yield 出去,前端可流式显示。

    事件类型(yield 的 dict):
        {"type": "stage",    "data": {"stage": "...", "message": "..."}}
        {"type": "thinking", "data": {"delta": "思考片段"}}
        {"type": "content",  "data": {"delta": "回答片段"}}
        {"type": "done",     "data": {"ok": bool, "message": "..."}}
        {"type": "error",    "data": {"ok": False, "message": "..."}}

    post_session_setup: 测试场景的 session/new 后回调
        (deepseek 用此设置模型 / 思考强度)
    test_acp_args: 测试时额外的 CLI 参数(qoder 用 ["--model","DeepSeek-V4-Flash",...])
    credential_env_builder: 动态构建凭证环境变量的回调
        (deepseek 用此注入 DSH_PERMISSION_MODE);
        task=None(测试场景),wrapper 应默认 always_approve。
    pre_bridge_hook: bridge 启动前的沙箱准备回调(codex 用此写 ~/.codex/config.toml);
        task=None(测试场景),wrapper 应默认 always_approve。
    post_bridge_hook: bridge 运行后的收尾回调(session, credentials, agent_type, db, user_id);
        在沙箱关闭前调用(尽力而为),codex chatgpt 模式用此回写轮换后的 auth.json。

    done/error 为终止事件,生成器在此后结束。
    """
    def stage(stage_id: str, message: str) -> dict:
        return {"type": "stage", "data": {"stage": stage_id, "message": message}}

    def done(ok: bool, message: str) -> dict:
        return {"type": "done", "data": {"ok": ok, "message": message}}

    # ---- 加载凭证 ----
    try:
        credentials = _load_credentials(db, user_id, agent_type)
    except RuntimeError as e:
        yield done(False, f"凭证加载失败: {e}")
        return

    if credential_env_builder:
        credential_envs = credential_env_builder(credentials, None)
    else:
        credential_envs = _build_credential_envs(credentials, agent_type)
    # 与 run_acp_agent 一致:无环境变量但有 pre_bridge_hook(文件注入)时放行
    if _credential_injection_missing(credential_envs, pre_bridge_hook):
        yield done(False, "凭证映射为空(请检查 registry 配置)")
        return

    # ---- 模式检查 ----
    # local 模式:测试连接直接在宿主机跑 bridge + CLI(临时目录承载 bridge 脚本),
    # 由 SANDBOX_LOCAL_ALLOW_CLI 控制是否放行
    if settings.SANDBOX_MODE == "local":
        if not settings.SANDBOX_LOCAL_ALLOW_CLI:
            yield done(
                False,
                "测试连接在 local 模式下被禁用(SANDBOX_LOCAL_ALLOW_CLI=false)。"
                "可设 SANDBOX_LOCAL_ALLOW_CLI=true 启用(需宿主机预装 CLI),"
                "或使用 SANDBOX_MODE=sandbox。",
            )
            return
        logger.warning(
            f"[{agent_type}_test] local 模式测试连接:bridge/CLI 直接跑在宿主机,"
            f"需宿主机预装 CLI(bridge 脚本/临时文件在本地临时目录,不碰宿主机 home)"
        )

    # ---- 创建临时沙箱 ----
    from app.sandbox.client import create_sandbox

    yield stage("creating_sandbox", "创建临时沙箱...")
    logger.info(f"[{agent_type}_test] 开始流式测试:创建临时沙箱")
    session = create_sandbox()
    bridge_exec_id: str | None = None
    is_local = settings.SANDBOX_MODE == "local"

    try:
        # ---- 准备 CLI 环境 ----
        yield stage("cli_env", "准备 CLI 环境(写入 bridge 脚本 + 检查 CLI)...")
        try:
            _ensure_cli_env(session, agent_type)
            logger.info(f"[{agent_type}_test] CLI 环境就绪")
        except RuntimeError as e:
            yield done(False, f"CLI 环境准备失败: {e}")
            return

        # 清理可能残留的旧登录态
        # local 模式跳过:~ 是宿主机真实 home,rm -rf 会删掉用户真实 CLI 登录态
        if not is_local:
            session.run_command("rm -rf ~/.qoder ~/.dsh ~/.codex 2>/dev/null", timeout=5)

        # ---- wrapper 层钩子:bridge 启动前的沙箱文件准备 ----
        # codex 用此回调写入 ~/.codex/config.toml(模型/审批策略配置)
        # deepseek 不需要(凭证/权限均经环境变量注入,无配置文件)
        # task=None(测试场景):wrapper 默认 always_approve
        # hook 可返回额外环境变量 dict(如 codex local 模式返回 CODEX_HOME)
        if pre_bridge_hook:
            try:
                hook_envs = pre_bridge_hook(session, credentials, agent_type, None)
                if hook_envs:
                    credential_envs = {**credential_envs, **hook_envs}
            except Exception as e:
                yield done(False, f"沙箱配置文件准备失败: {e}")
                return

        # ---- 启动 ACP bridge ----
        yield stage("bridge_start", "启动 ACP bridge(注入凭证,等待就绪)...")
        try:
            bridge_exec_id, bridge_port = _start_acp_bridge(
                session, credential_envs,
                agent_type=agent_type,
                extra_acp_args=test_acp_args,
            )
            endpoint_url, endpoint_headers = session.get_endpoint(bridge_port)
            _wait_for_bridge_ready(
                session, bridge_exec_id, endpoint_url, endpoint_headers, agent_type
            )
            yield stage("bridge_ready", "ACP bridge 就绪")
        except RuntimeError as e:
            err_msg = str(e)
            if any(kw in err_msg.lower() for kw in ("auth", "unauthorized", "token", "401", "credential")):
                yield done(False, f"凭证认证失败: {err_msg}")
            else:
                yield done(False, f"ACP bridge 启动失败: {err_msg}")
            return

        # ---- ACP 握手 ----
        yield stage("acp_init", "ACP 握手(initialize)...")
        client = ACPClient(endpoint_url, endpoint_headers)
        try:
            result = client.initialize()
            protocol_version = result.get("protocolVersion", "?")
            auth_methods = result.get("authMethods", []) or []
            logger.info(
                f"[{agent_type}_test] ACP 握手成功: protocolVersion={protocol_version}, "
                f"authMethods={[m.get('id') for m in auth_methods]}"
            )
            if auth_methods:
                logger.info(
                    f"[{agent_type}_test] 跳过 authenticate(凭证经环境变量自动认证),"
                    f"authMethods={[m.get('id') for m in auth_methods]}"
                )

            # ---- 创建会话 ----
            yield stage("session_new", "创建 ACP 会话...")
            # local 模式宿主机无 /tmp(Windows),用平台临时目录
            test_cwd = tempfile.gettempdir() if is_local else "/tmp"
            try:
                acp_session_id = client.new_session(cwd=test_cwd)
                logger.info(
                    f"[{agent_type}_test] session/new 返回: sessionId={acp_session_id}"
                )
            except httpx.TimeoutException:
                yield done(False, "创建会话超时(请检查网络或凭证有效性)")
                return
            except RuntimeError as e:
                err_msg = str(e)
                low = err_msg.lower()
                # 提取 bridge 日志中的 CLI stderr(含 Python traceback),
                # -32603 Internal error 时 JSON-RPC 响应只有泛化消息,
                # 真实异常在 CLI 的 stderr 里(经 bridge _pump_stderr 转发)
                bridge_detail = ""
                if bridge_exec_id:
                    bridge_detail = _extract_bridge_error(
                        session, bridge_exec_id, agent_type
                    )
                if bridge_detail:
                    err_msg = f"{err_msg}\n\n[CLI 日志]\n{bridge_detail}"

                if "auth" in low or "authentication required" in low:
                    yield done(False, f"session/new 失败:CLI 要求 authenticate 但不支持非交互式认证。错误: {err_msg}")
                elif any(kw in low for kw in ("unauthorized", "token", "401")):
                    yield done(False, f"凭证认证失败: {err_msg}")
                else:
                    yield done(False, f"创建会话失败: {err_msg}")
                return

            # ---- wrapper 层钩子:session/new 后的自定义设置 ----
            if post_session_setup:
                try:
                    post_session_setup(client, acp_session_id, None)
                except Exception as e:
                    yield done(False, f"会话配置失败: {e}")
                    return

            # ---- 发送测试 prompt + 流式接收 ----
            yield stage("prompt", "发送测试 prompt「你好」,等待模型响应(流式)...")
            test_prompt = [{"type": "text", "text": "你好"}]
            logger.info(
                f"[{agent_type}_test] 发送 session/prompt: "
                f"sessionId={acp_session_id}, prompt={json.dumps(test_prompt, ensure_ascii=False)}"
            )

            content_full: list[str] = []
            # MiniMax 等厂商 thinking=only / reasoningSplit 时,模型回复全部走
            # reasoning_content,Kimi CLI 转成 thought_chunk/reasoning 通知发出,
            # 永远不发 agent_message_chunk。这里累积 thinking,reply 为空时回退使用,
            # 避免被误判为"模型未响应"。
            reasoning_full: list[str] = []
            event_q: queue.Queue = queue.Queue()
            _SENTINEL = object()

            def _streaming_on_event(msg: dict) -> None:
                """on_event 回调:把 ACP 通知增量放入 queue 供生成器消费"""
                if msg.get("method") != "session/update":
                    return
                params = msg.get("params") or {}
                update = params.get("update") or {}
                update_type = update.get("sessionUpdate", "")
                content = update.get("content", "")
                text = _extract_text(content)
                if not text:
                    return
                if update_type in ("thought_chunk", "thinking", "reasoning"):
                    event_q.put(("thinking", text))
                    reasoning_full.append(text)
                elif update_type == "agent_message_chunk":
                    event_q.put(("content", text))
                    content_full.append(text)
                elif update_type == "error":
                    event_q.put(("error", text))

            prompt_error: list = []

            def _run_prompt() -> None:
                try:
                    client.prompt(
                        acp_session_id,
                        test_prompt,
                        on_event=_streaming_on_event,
                        timeout=60,
                    )
                except Exception as e:
                    prompt_error.append(e)
                finally:
                    event_q.put(_SENTINEL)

            prompt_thread = threading.Thread(
                target=_run_prompt, name=f"acp-test-{agent_type}", daemon=True
            )
            prompt_thread.start()

            while True:
                try:
                    item = event_q.get(timeout=120)
                except queue.Empty:
                    yield done(False, "模型响应超时(120s,请检查网络或配额)")
                    return
                if item is _SENTINEL:
                    break
                evt_type, text = item
                if evt_type == "thinking":
                    yield {"type": "thinking", "data": {"delta": text}}
                elif evt_type == "content":
                    yield {"type": "content", "data": {"delta": text}}
                elif evt_type == "error":
                    yield done(False, f"模型返回错误: {text}")
                    return

            if prompt_error:
                e = prompt_error[0]
                err_msg = str(e)
                low = err_msg.lower()
                # 提取 bridge 日志,补充真实错误详情(ACP 异常消息常为泛化描述)
                bridge_logs = ""
                if bridge_exec_id:
                    bridge_logs = _extract_recent_bridge_logs(
                        session, bridge_exec_id, agent_type
                    )
                if any(kw in low for kw in ("quota", "credit", "limit", "余额", "配额",
                                            "pricing", "pricingurl")):
                    msg = f"账户配额不足,请前往充值后重试。错误详情: {err_msg}"
                elif any(kw in low for kw in ("auth", "unauthorized", "token", "401")):
                    msg = f"凭证认证失败: {err_msg}"
                elif "timeout" in low:
                    msg = "模型响应超时(60s,请检查网络或配额)"
                else:
                    msg = f"模型响应测试失败: {err_msg}"
                if bridge_logs:
                    msg += f"\n\n[CLI 日志]\n{bridge_logs}"
                    logger.info(f"[{agent_type}_test] prompt 异常,bridge 日志:\n{bridge_logs}")
                yield done(False, msg)
                return

            # 自动拆分 <think>...</think>:某些模型/端点(Kimi CLI openai provider
            # 转发的 MiniMax、DeepSeek-R1 开源版等)把思考内嵌在 content 里,
            # 而非 reasoning_content 字段。Kimi CLI 不解析该标签,原样转发为
            # agent_message_chunk。这里在最终回复里拆分:标签内 → reasoning,
            # 标签外 → content,让前端看到干净的回复而非带标签的原文。
            content_joined = "".join(content_full)
            think_matches = re.findall(r"<think>(.*?)</think>", content_joined, re.DOTALL)
            if think_matches:
                extracted_reasoning = "".join(think_matches).strip()
                extracted_content = re.sub(
                    r"<think>.*?</think>", "", content_joined, flags=re.DOTALL
                ).strip()
                if extracted_reasoning:
                    reasoning_full.append(extracted_reasoning)
                reply = extracted_content
            else:
                reply = content_joined.strip()

            reasoning_text = "".join(reasoning_full).strip()

            if reply:
                # 有正式回复:正常成功
                preview = reply[:80] + ("..." if len(reply) > 80 else "")
                logger.info(f"[{agent_type}_test] 模型响应: {preview}")
                yield done(
                    True,
                    f"连接成功(ACP 协议版本 {protocol_version}),模型响应: {preview}",
                )
            elif reasoning_text:
                # 无正式回复但有思考内容:模型确实响应了(思考已在流式过程展示),
                # 判为成功,但 reply 为空,不把思考混入回复。
                logger.info(f"[{agent_type}_test] 模型仅返回思考内容(reply 为空)")
                yield done(
                    True,
                    f"连接成功(ACP 协议版本 {protocol_version}),模型仅返回思考内容,"
                    "未给出正式回复(思考过程已在上方展示)。",
                )
            else:
                # 既无回复也无思考:真正未响应
                # 提取 bridge 日志,展示真实的 API 错误(如 HTTP 401/400/配额不足等)
                # CLI 调 LLM 后未抛异常但返回空,真实错误可能在 stdout 或 stderr 中
                bridge_logs = ""
                if bridge_exec_id:
                    bridge_logs = _extract_recent_bridge_logs(
                        session, bridge_exec_id, agent_type
                    )
                msg = (
                    "session/new 成功,但模型未响应(请检查配额或网络)。"
                    "若使用 MiniMax/DeepSeek/阿里云等非 Moonshot 端点,"
                    "请在「智能体配置」中将「供应商协议类型」改为 openai 后重试。"
                )
                if bridge_logs:
                    msg += f"\n\n[CLI 日志]\n{bridge_logs}"
                    logger.info(f"[{agent_type}_test] 模型未响应,bridge 日志:\n{bridge_logs}")
                yield done(False, msg)

        except RuntimeError as e:
            err_msg = str(e)
            if any(kw in err_msg.lower() for kw in ("auth", "unauthorized", "token", "401")):
                yield done(False, f"凭证认证失败: {err_msg}")
            else:
                yield done(False, f"ACP 握手失败: {err_msg}")
            return
        finally:
            client.close()

    except Exception as e:
        logger.exception(f"[{agent_type}_test] 流式测试过程异常")
        yield {"type": "error", "data": {"ok": False, "message": f"测试异常: {e}"}}
    finally:
        # 沙箱关闭前:先执行 post hook(codex chatgpt 回写轮换后的 auth.json,尽力而为)
        if post_bridge_hook:
            try:
                post_bridge_hook(session, credentials, agent_type, db, user_id)
            except Exception as e:
                logger.warning(f"[{agent_type}_test] post_bridge_hook 失败(忽略): {e}")
        if bridge_exec_id:
            _stop_acp_bridge(session, bridge_exec_id, agent_type)
        try:
            session.close()
        except Exception as e:
            logger.info(f"[{agent_type}_test] 关闭沙箱失败(忽略): {e}")
