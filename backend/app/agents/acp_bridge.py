#!/usr/bin/env python3
"""ACP Bridge:HTTP <-> stdio 桥接服务(运行在沙箱内)

将后端的 HTTP 请求桥接到任意支持 ACP(Agent Client Protocol)over stdio 的
外部 CLI agent(如 Qoder CLI),CLI 通过命令行参数启动,凭证经环境变量注入。

工作原理:
1. 启动时 spawn `<bin> <args...>` 子进程(如 `qodercli --acp --yolo`),
   持有 stdin/stdout pipe。凭证(PAT 等)由父进程(bridge)环境变量继承,
   无需命令行明文传递。
2. 监听 HTTP 端口(默认 8088)
3. POST /rpc:接收 JSON-RPC 请求,写入 CLI stdin,读取 stdout 响应
   - 响应通过 SSE(stream)返回:每行 stdout 作为一个 SSE event
   - 通知(notification,无 id)作为中间事件,最终响应(有 id)作为终止事件
4. GET /health:健康检查(CLI 进程存活返回 200)

ACP 协议(JSON-RPC 2.0 over stdio):
- 请求:{"jsonrpc":"2.0","method":"initialize","params":{...},"id":1}
- 响应:{"jsonrpc":"2.0","result":{...},"id":1}
- 通知:{"jsonrpc":"2.0","method":"progress","params":{...}}  (无 id)

使用方式(沙箱内):
    python acp_bridge.py --port 8088 --bin qodercli --args '["--acp","--yolo"]'

依赖:仅 Python 标准库(http.server, subprocess, json, threading)
"""
import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# bridge 能力版本:后端据此判断能否安全使用会话恢复。
# 2 = 会排空恢复回放通知(见 _drain_queue)。老 bridge 无此字段(视为 1),
# 后端遇到老 bridge 时不会尝试 session/load 与 session/resume。
# 常量与 backend/app/agents/runtime/acp_session.py 的
# BRIDGE_PROTOCOL_REPLAY_SAFE 需保持一致;本脚本独立跑在沙箱内,
# 不得依赖 app 包,因此就地定义而非导入。
BRIDGE_PROTOCOL = 2

# ACP 会话恢复方法:响应前先以 session/update 通知回放历史,响应后
# 仍可能残留尾巴行。这些历史属于"往轮",必须在本请求内排空,
# 否则会被下一个请求(session/prompt)当成本轮输出。
_RESTORE_METHODS = ("session/load", "session/resume")


def _drain_queue(q: "queue.Queue", *, quiet_seconds: float = 0.35, max_seconds: float = 5.0) -> int:
    """取空队列残留行并丢弃,返回丢弃条数

    持续取,直到连续 quiet_seconds 无新行或累计超过 max_seconds(两者取先到达
    者)——回放尾巴靠"静默窗口"界定,不用固定 sleep 赌时长。
    哨兵行(end/error)同样丢弃:它们属于已结束的上一请求。
    """
    dropped = 0
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        try:
            q.get(timeout=quiet_seconds)
        except queue.Empty:
            break
        dropped += 1
    return dropped


# ============================================================
# ACP CLI 进程管理(通用,不绑定具体 CLI)
# ============================================================


class ACPCLIProcess:
    """管理 ACP CLI 子进程的 stdin/stdout 通信

    通过 bin + args 启动子进程,子进程继承父进程环境变量
    (凭证经 envs 注入到 bridge,再继承给 CLI,避免命令行明文)。
    """

    def __init__(self, bin_name: str, args: list[str] | None = None):
        self.bin_name = bin_name
        self.args = args or []
        self.proc: subprocess.Popen | None = None
        # _rpc_lock:保护整个 send+collect 串行(ACP 协议是串行的)
        # _stdin_lock:保护 stdin 写入(短临界区,用于 request_permission 响应回写)
        # 拆分原因:POST /rpc 在 wait permission 响应期间持有 _rpc_lock,
        # 但 POST /permission_response 需要唤醒它,不能死锁;
        # 响应回写通过 _stdin_lock 与 /rpc 的请求写入互斥。
        self._rpc_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        # stdout 行队列:常驻读线程(_pump_stdout)推入,/rpc 消费。
        # 不用 select 轮询:Windows 的 select 只支持 socket,作用于管道 fd
        # 会抛 WinError 10038(local 模式 bridge 跑在宿主机 Windows Python 上,
        # 每个 /rpc 请求都会崩在 select 上,CLI 响应永远无法转发)。
        self._stdout_q: queue.Queue = queue.Queue()

    def start(self) -> None:
        """启动 ACP CLI 子进程"""
        # Windows:npm 全局产物是 xxx.cmd,Popen 列表模式不走 PATHEXT,
        # 必须 which 预解析为实际路径;非 Windows 下 which 失败则原样传回
        # (交由 Popen 按 PATH 解析,行为与原先一致)
        bin_path = shutil.which(self.bin_name) or self.bin_name
        cmd = [bin_path, *self.args]
        print(f"[bridge] 启动 ACP CLI: {' '.join(cmd)}", flush=True)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # ACP 是 newline-delimited JSON,CLI(Node 等)固定收发 UTF-8。
            # Windows 上 text=True 默认用 locale 编码(中文系统 GBK),
            # 遇到 UTF-8 多字节字符(如 em-dash 的续字节 0x94)会
            # UnicodeDecodeError 杀死读线程,响应流中断;stdin 同理会把
            # 中文 prompt 以 GBK 乱码发给 CLI。显式 UTF-8 + replace 容错。
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲
        )
        # 启动 stdout 读取线程(阻塞 readline 推入队列,跨平台替代 select)
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        # 启动 stderr 监控线程(把 CLI 的 stderr 转发到 bridge 的 stderr)
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        """常驻读线程:阻塞式逐行读取 CLI stdout,推入 _stdout_q 供 /rpc 消费

        行的语义分发(JSON-RPC 最终响应/通知/request_permission)由 /rpc 的
        SSE 循环处理,这里只负责搬运。EOF/读异常以哨兵入队,让消费方感知
        流结束(等价于原先 select + readline 的 EOF 检测)。
        """
        if not self.proc or not self.proc.stdout:
            return
        try:
            for line in self.proc.stdout:
                self._stdout_q.put(("line", line))
            self._stdout_q.put(("end", None))
        except Exception as e:
            self._stdout_q.put(("error", e))

    def _pump_stderr(self) -> None:
        """把 CLI 的 stderr 输出到 bridge 的 stderr(调试用)"""
        if not self.proc or not self.proc.stderr:
            return
        for line in self.proc.stderr:
            text = line.rstrip()
            try:
                print(f"[cli stderr] {text}", file=sys.stderr, flush=True)
            except UnicodeEncodeError:
                # bridge 自身 stderr 用宿主 locale 编码(Windows 如 GBK),
                # CLI stderr 中该编码表示不了的字符(emoji 等)降级替换,
                # 避免异常杀死本线程导致后续 stderr 转发中断
                enc = sys.stderr.encoding or "utf-8"
                safe = text.encode(enc, "replace").decode(enc)
                print(f"[cli stderr] {safe}", file=sys.stderr, flush=True)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write_stdin(self, line: str) -> None:
        """写一行到 CLI stdin(线程安全,用 _stdin_lock 保护)。

        供 /rpc 的请求写入和 request_permission 响应回写共用。
        """
        assert self.proc is not None
        assert self.proc.stdin is not None
        with self._stdin_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def stop(self) -> None:
        """停止 CLI 子进程"""
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None


# ============================================================
# HTTP 请求处理
# ============================================================

# 全局 CLI 进程实例(所有请求共享)
_cli: ACPCLIProcess | None = None

# Pending permission 请求管理(CLI 发来的 request_permission 请求,等待用户确认)
# 结构:{request_id: {"event": threading.Event, "result": dict | None}}
# 流程:POST /rpc 读到 request_permission → 存入 _pending_permissions →
#       推 SSE 事件给后端 → 阻塞 event.wait() →
#       POST /permission_response 设 result + event.set() →
#       POST /rpc 唤醒,把 result 写回 CLI stdin
_pending_permissions: dict[str, dict] = {}
_pending_lock = threading.Lock()


class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        # 覆盖默认日志,安全格式化(避免参数数量不匹配导致异常)
        try:
            msg = format % args if args else format
        except Exception:
            msg = f"{format} {args}"
        print(f"[bridge] {msg}", file=sys.stderr, flush=True)

    def do_GET(self):
        """GET /health:健康检查(附 bridge_protocol 供后端判定恢复能力)"""
        if self.path == "/health":
            alive = _cli is not None and _cli.alive
            status_code = 200 if alive else 503
            body = json.dumps({
                "status": "ok" if alive else "cli_not_running",
                "bridge_protocol": BRIDGE_PROTOCOL,
            })
            self._send_json(status_code, body)
        else:
            self._send_json(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        """POST /rpc:发送 JSON-RPC 请求,流式返回 SSE(每行 stdout 即时推送)
        POST /permission_response:提交用户对 request_permission 的确认结果

        /rpc 与批量收集模式不同,这里逐行读取 CLI stdout 并立即推送 SSE,
        让后端能实时收到 thinking/text/tool_call 增量,实现真正流式体验。

        线程安全:整个读取过程持有 _cli._rpc_lock,避免并发请求交叉。
        (ACP 是串行协议,同一时刻只处理一个请求,锁不影响吞吐)

        request_permission 处理:
        CLI 检测到危险命令时会发 JSON-RPC 请求(method=request_permission, 有 id),
        bridge 在 SSE 流里推 event: permission_request 事件给后端,后端问用户,
        用户确认后调 POST /permission_response,bridge 把结果写回 CLI stdin。

        stream_error 处理:
        CLI 进程退出/stdout EOF/读失败等异常关流时,bridge 在关闭连接前推
        event: stream_error 事件(含原因),后端 _rpc 据此抛 ACPStreamAborted,
        不会把流中断误当作正常完成(残缺输出流入 agent2 审查)。
        """
        if self.path == "/permission_response":
            self._handle_permission_response()
            return

        if self.path != "/rpc":
            self._send_json(404, json.dumps({"error": "not found"}))
            return

        if _cli is None or not _cli.alive:
            self._send_json(503, json.dumps({"error": "CLI process not running"}))
            return

        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            request = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"error": f"invalid JSON: {e}"}))
            return

        request_id = request.get("id")
        request_method = request.get("method", "")
        request_line = json.dumps(request, ensure_ascii=False)

        print(f"[bridge] >>> 发送到 CLI: method={request_method}, id={request_id}", file=sys.stderr, flush=True)

        # 发送请求 + 流式读取响应(持 _rpc_lock,串行)
        headers_sent = False
        try:
            with _cli._rpc_lock:
                if not _cli.alive:
                    raise RuntimeError("ACP CLI 进程已退出")

                # 写请求到 stdin(用 _write_stdin,内部持 _stdin_lock)
                _cli._write_stdin(request_line)

                # 先发 SSE 响应头
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                headers_sent = True

                # 逐行读取 stdout,立即推送 SSE
                # stdout 由常驻读线程(_pump_stdout)阻塞 readline 后推入
                # _stdout_q,这里 queue.get(timeout) 轮询:有行即转发,超时则
                # 检查进程存活 + 推 idle 心跳。
                # 不用 select:Windows 的 select 只支持 socket,作用于管道 fd
                # 会抛 WinError 10038(local 模式 bridge 跑在宿主机 Windows
                # Python 上,每个 /rpc 请求都会崩,CLI 响应永远无法转发)。
                # 不设硬性 deadline:CLI 可能在等异步子 agent(数十秒~数分钟无输出)。
                # 只在 CLI 进程退出或 stdout EOF 时结束,确保不丢失后续响应。
                line_count = 0
                idle_secs = 0.0
                last_idle_log = 0.0
                got_final = False

                while True:
                    # 从队列取一行(5s 超时,便于周期性检查进程存活)
                    try:
                        kind, payload = _cli._stdout_q.get(timeout=5.0)
                    except queue.Empty:
                        # 暂无数据,检查 CLI 进程是否还活着
                        if not _cli.alive:
                            rc = _cli.proc.poll() if _cli.proc else None
                            print(f"[bridge] CLI 进程在等待响应时退出(method={request_method}, returncode={rc})", file=sys.stderr, flush=True)
                            self._push_stream_error(
                                "cli_exit", f"CLI 进程在执行中退出(returncode={rc})",
                            )
                            break
                        # 推送 idle 心跳到 SSE(带 event: idle 标记,便于 recorder 记录)
                        # 每 5s 一次,让后端知道 bridge 还活着、CLI 还在跑
                        idle_secs += 5.0
                        # 每 30s 打一次日志(避免刷屏)
                        if idle_secs - last_idle_log >= 30.0:
                            print(f"[bridge] 等待 CLI 响应中(method={request_method}, idle={int(idle_secs)}s)", file=sys.stderr, flush=True)
                            last_idle_log = idle_secs
                        # 推送 SSE 注释行(: 开头是 SSE 注释,客户端会忽略,
                        # 但我们的 recorder 会记录原始行,便于事后分析 CLI 卡在哪)
                        try:
                            self.wfile.write(f": idle {int(idle_secs)}s\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except BrokenPipeError:
                            break  # 客户端断开连接
                        continue

                    if kind == "end":
                        # EOF,CLI 进程关闭了 stdout(读线程哨兵)
                        print(f"[bridge] CLI stdout EOF(method={request_method})", file=sys.stderr, flush=True)
                        self._push_stream_error(
                            "stdout_eof", "CLI 输出流关闭(EOF),疑似进程崩溃",
                        )
                        break
                    if kind == "error":
                        print(f"[bridge] 读 CLI stdout 失败(method={request_method}): {payload}", file=sys.stderr, flush=True)
                        self._push_stream_error(
                            "read_error", f"读取 CLI 输出失败: {payload}",
                        )
                        break

                    # 有数据,重置 idle 计数
                    idle_secs = 0.0
                    last_idle_log = 0.0

                    raw_line = payload
                    line = raw_line.strip()
                    if not line:
                        continue

                    line_count += 1
                    print(f"[bridge] <<< CLI stdout [{line_count}]: {line[:500]}", file=sys.stderr, flush=True)

                    # 检查是否是 CLI 发来的 request_permission 请求(JSON-RPC 请求,有 id)
                    # ACP 协议:CLI 检测危险命令 → 发 request_permission → bridge 转发后端 →
                    # 后端问用户 → POST /permission_response 提交结果 → bridge 写回 CLI stdin
                    try:
                        msg = json.loads(line)
                        if (
                            isinstance(msg, dict)
                            and msg.get("method") == "request_permission"
                            and msg.get("id") is not None
                        ):
                            self._handle_request_permission(msg)
                            continue  # 已处理,继续读 stdout(等待 CLI 后续响应)
                    except json.JSONDecodeError:
                        pass

                    sse_data = f"data: {line}\n\n"
                    try:
                        self.wfile.write(sse_data.encode("utf-8"))
                        self.wfile.flush()
                    except BrokenPipeError:
                        break  # 客户端断开连接

                    # 收到匹配 id 的最终响应 → 结束流
                    try:
                        msg = json.loads(line)
                        if request_id is not None and msg.get("id") == request_id:
                            print(f"[bridge] 收到匹配 id={request_id} 的最终响应,结束流", file=sys.stderr, flush=True)
                            got_final = True
                            break
                    except json.JSONDecodeError:
                        continue

                if request_method in _RESTORE_METHODS:
                    # 会话恢复(session/load / session/resume):CLI 会把历史以
                    # session/update 通知回放,尾巴可能拖到最终响应之后。无论请求
                    # 正常收尾还是异常断开(后端 120s 读超时先断 → BrokenPipe),
                    # 都在仍持有 _rpc_lock 时把残留行取空丢弃 —— 否则它们会涌进
                    # 下一个请求(session/prompt)的流里,往轮历史会被当成本轮
                    # 输出重复入库/推前端。
                    dropped = _drain_queue(_cli._stdout_q)
                    if dropped:
                        print(
                            f"[bridge] {request_method} 回放残留通知已排空({dropped} 行)",
                            file=sys.stderr, flush=True,
                        )

                if not got_final:
                    # 流在收到最终响应前结束(读失败/EOF/CLI 退出/客户端断开):
                    # 关闭连接让客户端立即收到 EOF 快速失败,而非等它自己的
                    # read 超时(keep-alive 下连接会一直开着)。
                    # 异常路径(CLI 退出/EOF/读失败)已先推 event: stream_error
                    # 告知原因,客户端 _rpc 据此抛 ACPStreamAborted 而非静默返回 {}
                    self.close_connection = True
                if line_count == 0:
                    print(f"[bridge] 警告:CLI 未输出任何响应行(method={request_method})", file=sys.stderr, flush=True)
        except Exception as e:
            if headers_sent:
                # SSE 响应头已发出,无法再返回 HTTP 错误状态;只能记录日志并
                # 强制关闭连接,让客户端的流式读取收到 EOF 而非无限等待
                # (写 500 会在同一响应里出现两个状态行,客户端也解析不出)
                print(f"[bridge] 流式响应异常: {e}", file=sys.stderr, flush=True)
                self.close_connection = True
            else:
                # 响应头未发,可正常返回 JSON 错误
                try:
                    self._send_json(500, json.dumps({"error": str(e)}))
                except Exception:
                    print(f"[bridge] 流式响应异常: {e}", file=sys.stderr, flush=True)

    def _push_stream_error(self, reason: str, message: str) -> None:
        """异常关流前推 event: stream_error 事件,告知后端终止原因

        仅在未收到最终响应的异常路径调用(CLI 进程退出/stdout EOF/读失败)。
        后端 ACPClient._rpc 收到后抛 ACPStreamAborted 并把 message 带进
        截断标注,让本轮以"输出不完整"收尾而非被当作正常完成
        (CLI 崩溃如 Node OOM 时,残缺输出不能直接流入 agent2 审查)。
        客户端已断开时(BrokenPipe/连接重置)静默跳过——无从告知。

        SSE 格式与 permission_request 一致(event: 行 + data: JSON 载荷),
        载荷:{"reason": "cli_exit|stdout_eof|read_error", "message": "人类可读描述"}
        """
        payload = {"reason": reason, "message": message}
        try:
            sse = f"event: stream_error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            self.wfile.write(sse.encode("utf-8"))
            self.wfile.flush()
            print(f"[bridge] 推送 stream_error 事件(reason={reason})", file=sys.stderr, flush=True)
        except (BrokenPipeError, ConnectionError, OSError):
            pass  # 客户端已断开,尽力而为

    def _handle_request_permission(self, msg: dict) -> None:
        """处理 CLI 发来的 request_permission JSON-RPC 请求。

        ACP 协议:CLI 检测到危险命令 → 发 request_permission 请求(有 id)→
        bridge 通过 SSE 流推 event: permission_request 给后端 →
        阻塞等待 POST /permission_response 提交结果 →
        把结果作为 JSON-RPC 响应写回 CLI stdin。

        msg 结构:{
            "jsonrpc": "2.0",
            "method": "request_permission",
            "id": <CLI 分配的 id>,
            "params": {
                "session_id": "...",
                "tool_call": {...},  # ToolCallUpdate,含命令/diff 等
                "options": [{"option_id": "allow_once", "kind": "allow_once", "name": "..."}, ...]
            }
        }
        """
        perm_id = str(msg.get("id"))
        params = msg.get("params", {})
        tool_call = params.get("tool_call", {})
        options = params.get("options", [])

        # 提取命令文本(tool_call.content[0].text 或 raw_input.command)
        command = ""
        description = ""
        raw_input = tool_call.get("raw_input", {})
        if isinstance(raw_input, dict):
            command = str(raw_input.get("command", ""))
            description = str(raw_input.get("description", "")) or command
        # 从 content 提取展示文本(备用)
        if not command:
            content_list = tool_call.get("content", [])
            if isinstance(content_list, list) and content_list:
                first_content = content_list[0]
                if isinstance(first_content, dict):
                    text_block = first_content.get("text", "")
                    if isinstance(text_block, str):
                        command = text_block

        # 构造 permission_request 事件载荷(后端用它推 command_confirm SSE 给前端)
        perm_payload = {
            "id": perm_id,
            "tool_call_id": tool_call.get("id", ""),
            "title": tool_call.get("title", ""),
            "kind": tool_call.get("kind", ""),
            "command": command,
            "description": description,
            "options": options,
        }

        # 推 SSE 事件给后端(event: permission_request,后端 ACPClient 识别此事件)
        sse_event = f"event: permission_request\ndata: {json.dumps(perm_payload, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(sse_event.encode("utf-8"))
            self.wfile.flush()
            print(f"[bridge] 推送 permission_request 事件(perm_id={perm_id}, command={command[:100]})", file=sys.stderr, flush=True)
        except BrokenPipeError:
            print(f"[bridge] 推送 permission_request 失败:客户端断开(perm_id={perm_id})", file=sys.stderr, flush=True)
            return

        # 注册 pending permission,阻塞等待 POST /permission_response 唤醒
        event = threading.Event()
        with _pending_lock:
            _pending_permissions[perm_id] = {"event": event, "result": None}

        # 阻塞等待(无超时,用户可能需要很久才确认;CLI 进程退出时会被 select 检测到)
        # 期间定期检查 CLI 是否还活着,避免 CLI 死了还在等
        while True:
            if event.wait(timeout=5.0):
                break
            if not _cli or not _cli.alive:
                print(f"[bridge] 等待 permission 响应时 CLI 退出(perm_id={perm_id})", file=sys.stderr, flush=True)
                break

        # 取出结果
        with _pending_lock:
            entry = _pending_permissions.pop(perm_id, None)
        result = entry["result"] if entry else None

        # 构造 JSON-RPC 响应写回 CLI stdin
        if result is None:
            # 超时/CLI 退出,默认拒绝
            outcome = {"outcome": "rejected"}
            print(f"[bridge] permission 无结果,默认拒绝(perm_id={perm_id})", file=sys.stderr, flush=True)
        else:
            outcome = result.get("outcome", {"outcome": "rejected"})

        response = {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"outcome": outcome},
        }
        response_line = json.dumps(response, ensure_ascii=False)
        _cli._write_stdin(response_line)
        print(f"[bridge] permission 响应已写回 CLI(perm_id={perm_id}, outcome={outcome})", file=sys.stderr, flush=True)

    def _handle_permission_response(self) -> None:
        """POST /permission_response:后端提交用户对 request_permission 的确认结果。

        请求体:{
            "id": "<perm_id>",  # 对应 request_permission 的 id
            "outcome": {"outcome": "selected", "option_id": "allow_once"}  # 或 {"outcome": "rejected"}
        }
        """
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_json(400, json.dumps({"error": f"invalid JSON: {e}"}))
            return

        perm_id = str(data.get("id", ""))
        outcome = data.get("outcome", {"outcome": "rejected"})

        with _pending_lock:
            entry = _pending_permissions.get(perm_id)
            if entry is None:
                self._send_json(404, json.dumps({"error": f"permission id not found: {perm_id}"}))
                return
            entry["result"] = {"outcome": outcome}
            entry["event"].set()

        print(f"[bridge] 收到 permission 响应(perm_id={perm_id}, outcome={outcome})", file=sys.stderr, flush=True)
        self._send_json(200, json.dumps({"status": "ok"}))

    def _send_json(self, status_code: int, body: str) -> None:
        """发送 JSON 响应"""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


# ============================================================
# 主入口
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="ACP Bridge: HTTP <-> stdio")
    parser.add_argument(
        "--port", type=int, default=8088,
        help="HTTP 监听端口(默认 8088)",
    )
    parser.add_argument(
        "--bin", type=str, default="qodercli",
        help="ACP CLI 可执行文件名/路径(默认 qodercli,从 PATH 查找或绝对路径)",
    )
    parser.add_argument(
        "--args", type=str, default='["--acp", "--permission-mode", "bypass_permissions"]',
        help='CLI 启动参数(JSON 数组,默认 \'["--acp", "--permission-mode", "bypass_permissions"]\')',
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="监听地址(默认 0.0.0.0,允许外部访问)",
    )
    args = parser.parse_args()

    # 解析 args JSON
    try:
        cli_args = json.loads(args.args)
        if not isinstance(cli_args, list):
            raise ValueError("args 必须是 JSON 数组")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[bridge] --args 解析失败: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    global _cli

    # 启动 CLI 子进程(继承当前环境变量,凭证经 envs 注入到 bridge 进程)
    _cli = ACPCLIProcess(bin_name=args.bin, args=cli_args)
    _cli.start()

    # 等待 CLI 就绪(短暂等待进程稳定)
    time.sleep(1)
    if not _cli.alive:
        print("[bridge] ACP CLI 启动失败,退出", file=sys.stderr, flush=True)
        sys.exit(1)

    # 启动 HTTP 服务器
    # Windows:SO_REUSEADDR 语义允许两个进程绑定同一端口(不是 POSIX 的
    # "重启复用"),并发任务的 bridge 健康检查可能打到别的实例,必须禁用
    class _BridgeServer(ThreadingHTTPServer):
        if sys.platform == "win32":
            allow_reuse_address = False

    server = _BridgeServer((args.host, args.port), BridgeHandler)
    print(f"[bridge] HTTP 服务监听 {args.host}:{args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[bridge] 正在关闭...", file=sys.stderr, flush=True)
        if _cli:
            _cli.stop()
        server.server_close()


if __name__ == "__main__":
    main()
