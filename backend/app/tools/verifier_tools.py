"""verifier_agent 专用工具:http_request + 结论申报工具定义

向任务配置的 test_env_url 发送 HTTP 请求,用于动态验证 react_agent 发现的安全问题。

设计:
- http_request 在沙箱里执行(非后端进程),用 Python 标准库 urllib 发起请求
  - 后端服务器 IP 不暴露给测试环境(防反向探测)
  - 沙箱是隔离边界,PoC 副作用不影响后端
  - 与 run_python_code 统一安全边界
- URL = test_env_url(base, 来自 task.params._verifier)+ path(LLM 提供,相对路径)
  base 固定不可由 LLM 篡改,防止 SSRF
- 支持自定义 method/headers/body,满足 PoC 需求
- 超时 30s,响应体截断到 50000 字符(防 LLM 上下文爆炸)
- 跳过 SSL 证书验证(测试环境可能自签)
- per_action 授权模式:每次调用前经 user_interaction 阻塞等用户确认

run_python_code 复用 sandbox_tools(与 react_agent 共享沙箱会话),
不在此文件重新实现,由 verifier_agent 直接从 sandbox_tools 导入;
`VERIFIER_EMIT_TOOL_DEFINITIONS` 里的 `submit_verification` 只申报结论(无副作用),
执行与校验在 verifier_agent 循环内完成。
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# HTTP 请求超时(秒)
_HTTP_TIMEOUT = 30.0
# 响应体最大字符数(截断防 LLM 上下文爆炸)
_MAX_BODY_CHARS = 50000


# ============================================================
# 沙箱内执行的 HTTP 请求脚本(用 urllib 标准库,不依赖第三方包)
# ============================================================
# 参数通过 __PARAMS__ 注入(json.dumps 输出,合法 Python 字面量,无注入风险)
# 脚本输出 JSON 到 stdout,外层解析
_HTTP_SANDBOX_SCRIPT = '''import json, urllib.request, urllib.error, ssl, time

params = __PARAMS__
method = params["method"]
url = params["url"]
headers = params.get("headers") or {}
body = params.get("body")
timeout = params.get("timeout", 30)
max_body = params.get("max_body", 50000)

# 不验证证书(测试环境可能自签)
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# 自定义重定向:支持所有 HTTP 方法的 307/308(默认只跟 GET/HEAD 的 301/302/303)
class AllMethodRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if code not in (301, 302, 303, 307, 308):
            return None
        m = req.get_method()
        if code in (301, 302, 303):
            m = "GET"
            data = None
        else:
            data = req.data
        new_headers = dict(req.header_items())
        return urllib.request.Request(newurl, method=m, data=data, headers=new_headers)

opener = urllib.request.build_opener(AllMethodRedirect, urllib.request.HTTPSHandler(context=ssl_ctx))

req = urllib.request.Request(
    url, method=method,
    data=body.encode("utf-8") if body else None,
    headers=headers,
)

start = time.time()
result = {"status_code": 0, "headers": {}, "body": "", "truncated": False}
raw = b""
try:
    resp = opener.open(req, timeout=timeout)
    result["status_code"] = resp.getcode()
    result["headers"] = dict(resp.headers)
    raw = resp.read()
except urllib.error.HTTPError as e:
    result["status_code"] = e.code
    result["headers"] = dict(e.headers) if e.headers else {}
    try:
        raw = e.read()
    except Exception:
        raw = b""
except Exception as e:
    result["body"] = "[请求失败: {}]".format(e)
    result["elapsed_ms"] = int((time.time() - start) * 1000)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0)

try:
    body_text = raw.decode("utf-8")
except UnicodeDecodeError:
    body_text = raw.decode("latin-1", errors="replace")

truncated = len(body_text) > max_body
if truncated:
    body_text = body_text[:max_body] + "\\n\\n[响应体已截断,原始长度 {} 字符]".format(len(body_text))

result["body"] = body_text
result["truncated"] = truncated
result["elapsed_ms"] = int((time.time() - start) * 1000)
print(json.dumps(result, ensure_ascii=False))
'''


def http_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    auth_profile: str | None = None,
    task_id: str = "",
    test_env_url: str = "",
    auth_tokens: list[dict] | None = None,
) -> dict[str, Any]:
    """向测试环境发送 HTTP 请求(在沙箱里执行,授权由 verifier_agent 统一拦截)

    参数:
        method: HTTP 方法(GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS)
        path: 相对路径(拼到 test_env_url 后面),如 "/api/users" 或 "/login?next=/"
        headers: 自定义请求头(可选)
        body: 请求体字符串(可选;POST/PUT 常用)
        auth_profile: 选择登录身份(可选,对应 auth_tokens 某项的 label);
            匹配则把该项 header_name: header_value 注入请求头;LLM 不接触 token 明文。
            None=不带认证;不存在的 label=返回错误,不发送请求。
        task_id: 当前任务 ID(自动注入)
        test_env_url: 测试环境基址(自动注入,来自 task.params._verifier)
        auth_tokens: 登录凭证列表(自动注入,来自 task.params._verifier.auth_tokens)

    返回:
        {"status_code": int, "headers": dict, "body": str, "elapsed_ms": int, "truncated": bool}
    """
    method = method.upper().strip()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return {"status_code": 0, "body": f"[不支持的方法: {method}]"}

    if not test_env_url:
        return {"status_code": 0, "body": "[未配置 test_env_url,无法发送请求]"}

    # 合并请求头:LLM 显式 headers + auth_profile 注入的凭证头
    # (显式 headers 优先级高,可覆盖凭证头;但通常不会冲突)
    final_headers: dict[str, str] = dict(headers or {})
    if auth_profile:
        tokens = auth_tokens or []
        matched = next(
            (t for t in tokens if t.get("label") == auth_profile),
            None,
        )
        if matched is None:
            available = ", ".join(t.get("label", "") for t in tokens) or "(无)"
            return {
                "status_code": 0,
                "body": f"[auth_profile='{auth_profile}' 不存在,可用身份: {available}]",
            }
        # 注入凭证头(显式 headers 优先,不覆盖 LLM 已设的值)
        h_name = matched.get("header_name", "")
        if h_name and h_name not in final_headers:
            final_headers[h_name] = matched.get("header_value", "")

    # 拼接完整 URL(base 去尾斜杠,path 补头斜杠)
    base = test_env_url.rstrip("/")
    p = path if path.startswith("/") else "/" + path
    full_url = base + p

    # 构造沙箱内执行的脚本(参数用 json.dumps 注入,合法 Python 字面量)
    params_json = json.dumps({
        "method": method,
        "url": full_url,
        "headers": final_headers,
        "body": body or "",
        "timeout": _HTTP_TIMEOUT,
        "max_body": _MAX_BODY_CHARS,
    })
    script = _HTTP_SANDBOX_SCRIPT.replace("__PARAMS__", params_json)

    # 在沙箱里执行 HTTP 请求脚本
    return _execute_in_sandbox(script, task_id, full_url, method)


def _execute_in_sandbox(
    script: str,
    task_id: str,
    full_url: str,
    method: str,
) -> dict[str, Any]:
    """在沙箱里执行 HTTP 请求脚本,解析 JSON 输出返回结果

    local 模式:本地 subprocess 执行(开发/调试用,脚本在宿主机直接运行,无隔离边界)
    sandbox 模式:沙箱容器内执行(生产用,隔离边界)
    """
    from app.tools.sandbox_tools import _get_or_create_session, _get_workspace_dir, _resolve_workspace_path, write_file

    ctx = _get_or_create_session(task_id)
    mode = ctx["mode"]
    ws_dir = _get_workspace_dir(ctx)

    # 脚本写到工作区临时文件(避免 shlex 转义复杂脚本出错)
    script_name = f"_http_{uuid.uuid4().hex[:8]}.py"
    write_file(script_name, script, mode="write", task_id=task_id)
    script_abs = _resolve_workspace_path(ws_dir, script_name)

    timeout_total = int(_HTTP_TIMEOUT) + 10
    start = time.time()

    if mode == "local":
        # local 模式:本地 subprocess 执行
        # 用 sys.executable 而不是写死 "python":写死会在 Windows 上打到
        # Microsoft Store 的 python 别名(弹商店/报退出码 9009),多解释器环境也会跑错版本
        try:
            result = subprocess.run(
                [sys.executable, script_abs],
                capture_output=True,
                text=True,
                timeout=timeout_total,
                cwd=ws_dir,
            )
            stdout = result.stdout
            exit_code = result.returncode
            stderr = result.stderr
        except subprocess.TimeoutExpired:
            logger.warning(f"[task={task_id}] HTTP {method} {full_url} 沙箱执行超时")
            return {
                "status_code": 0,
                "body": f"[请求超时({_HTTP_TIMEOUT}s)]",
                "elapsed_ms": int(_HTTP_TIMEOUT * 1000),
            }
    else:
        # sandbox 模式:沙箱容器内执行
        session = ctx["session"]
        cmd = (
            f"cd {shlex.quote(ws_dir)} && "
            f"timeout {timeout_total} python3 {shlex.quote(script_abs)} 2>&1; "
            f'echo "EXIT_CODE:$?"'
        )
        try:
            combined = session.run_command(cmd, timeout=timeout_total + 5)
            # 从末尾解析 EXIT_CODE 行
            m = None
            for line in reversed(combined.splitlines()):
                if line.startswith("EXIT_CODE:"):
                    m = line
                    break
            if m:
                code_str = m[len("EXIT_CODE:"):].strip()
                exit_code = int(code_str) if code_str.lstrip("-").isdigit() else -1
                stdout = combined.rsplit(m, 1)[0].rstrip("\n")
            else:
                stdout = combined
                exit_code = 0
            stderr = ""
        except Exception as e:
            logger.warning(f"[task={task_id}] HTTP {method} {full_url} 沙箱执行失败: {e}")
            return {
                "status_code": 0,
                "body": f"[沙箱执行失败: {e}]",
                "elapsed_ms": int((time.time() - start) * 1000),
            }

    elapsed_ms = int((time.time() - start) * 1000)

    if exit_code != 0:
        err_msg = stderr or stdout[:500]
        logger.warning(f"[task={task_id}] HTTP {method} {full_url} 脚本退出码 {exit_code}: {err_msg}")
        return {
            "status_code": 0,
            "body": f"[沙箱执行失败(exit={exit_code}): {err_msg}]",
            "elapsed_ms": elapsed_ms,
        }

    # 解析 JSON 输出
    try:
        http_result = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning(f"[task={task_id}] HTTP {method} {full_url} 输出解析失败: {stdout[:500]}")
        return {
            "status_code": 0,
            "body": f"[输出解析失败: {stdout[:500]}]",
            "elapsed_ms": elapsed_ms,
        }

    # 二次截断保险(沙箱脚本内已做一次)
    resp_body = http_result.get("body", "")
    truncated = http_result.get("truncated", False)
    if len(resp_body) > _MAX_BODY_CHARS:
        truncated = True
        resp_body = resp_body[:_MAX_BODY_CHARS] + f"\n\n[响应体已截断,原始长度 {len(resp_body)} 字符]"

    return {
        "status_code": http_result.get("status_code", 0),
        "headers": http_result.get("headers", {}),
        "body": resp_body,
        "elapsed_ms": http_result.get("elapsed_ms", elapsed_ms),
        "truncated": truncated,
    }


# ============================================================
# verifier_agent 工具定义(OpenAI function-calling 格式)
# ============================================================

VERIFIER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "向测试环境发送 HTTP 请求,验证 react_agent 发现的安全问题是否真实可利用。"
                "URL = 任务配置的 test_env_url + path(相对路径),base 不可篡改。"
                "请求在沙箱里执行(与 run_python_code 同等隔离边界),不经过后端服务器。"
                "用于:发送 PoC payload 验证注入、访问受保护端点验证认证绕过、"
                "提交恶意输入验证 XSS/SSRF 等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                        "description": "HTTP 方法",
                    },
                    "path": {
                        "type": "string",
                        "description": "相对路径(拼到 test_env_url 后面),如 /api/users / /login?next=/",
                    },
                    "headers": {
                        "type": "object",
                        "description": "自定义请求头(可选),如 {\"Content-Type\": \"application/json\"}。注意:认证头由 auth_profile 自动注入,不要在此手动填 Authorization/Cookie。",
                    },
                    "body": {
                        "type": "string",
                        "description": "请求体字符串(可选;POST/PUT 常用),如 JSON payload 或表单数据",
                    },
                    "auth_profile": {
                        "type": "string",
                        "description": (
                            "选择登录身份(可选,对应任务配置的凭证 label,如 '管理员'/'普通用户')。"
                            "工具自动把该身份的认证头注入请求,你不需要也不应该知道 token 明文。"
                            "用于越权测试:同一端点用不同身份访问,对比响应差异。"
                            "留空=不带认证(匿名访问);不存在的 label 会返回错误。"
                        ),
                    },
                },
                "required": ["method", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python_code",
            "description": (
                "在沙箱里执行 Python 代码,返回 stdout/stderr/exit_code。"
                "用于构造复杂 PoC 脚本(生成签名、编码 payload、解析响应),也用于"
                "需要多步状态的真发请求(登录态、签名序列、对比多个响应)——"
                "沙箱本就能访问测试环境(http_request 就是在同一沙箱里跑 Python urllib),"
                "自己写请求请用标准库 urllib(第三方包 requests/httpx 不保证预装)。"
                "单发的请求仍优先 http_request:它把 URL 锁定在 test_env_url、"
                "并自动注入登录身份凭证(你看不到 token 明文)。"
                "复用 react_agent 的沙箱会话,可 read_file 仓库代码辅助构造 PoC。"
                "单次超时 60s。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python 代码(字符串)"},
                    "timeout": {"type": "integer", "description": "超时秒数,默认 60,上限 120"},
                },
                "required": ["code"],
            },
        },
    },
]

# 结论申报工具(无副作用:不弹窗授权、不进沙箱,只在本轮内累结果)
# 为何需要:此前台账只记"验证跑没跑成",模型散文总结里的"无法确认/环境不可达"
# 也被当成成功条目,只要引用它就把审查项抬到「已动态验证」。
# 现在要拿到那一档,必须通过本工具申报过定论(confirmed / not_confirmed)。
VERIFIER_EMIT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "submit_verification",
            "description": (
                "申报验证结论(输出总结前必须调用一次)。"
                "每个验证目标给一条结论:confirmed=真的触发成功了;"
                "not_confirmed=试过且确认不成立/误报;inconclusive=发了请求但响应看不出差异;"
                "unreachable=环境/端点不可达;not_authorized=动作未获授权根本没执行。"
                "没把握就如实写未定论档位——不要猜。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "description": "逐个验证目标的结论(与传入的验证目标一一对应)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {
                                    "type": "string",
                                    "description": "验证目标描述(写具体位置/事,如 src/api/users.py 第 42 行 SQL 注入)",
                                },
                                "result": {
                                    "type": "string",
                                    "enum": [
                                        "confirmed", "not_confirmed",
                                        "inconclusive", "unreachable", "not_authorized",
                                    ],
                                    "description": "该目标的结论",
                                },
                                "evidence": {
                                    "type": "string",
                                    "description": "关键证据(HTTP 状态码 + 响应片段等,不超 300 字)",
                                },
                            },
                            "required": ["target", "result"],
                        },
                    },
                    "note": {"type": "string", "description": "整体补充说明(可选)"},
                },
                "required": ["targets"],
            },
        },
    },
]
