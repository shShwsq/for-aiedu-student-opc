"""共享工具意图生成(react_agent / agent2 / verifier_agent 三处合一)

纯模板映射,不调 LLM:工具名 + 参数 → 人类可读一句话(前端工具调用
卡片的标题行,让用户一眼看出"这个工具调用打算做什么")。末尾追加
[tool_name] 标签:前端用正则提取工具名,用于 plan step 归属推断。

此前三处各写一份映射(react_agent 全量表 / agent2 只读核查表 /
verifier 验证工具表),新增工具时容易漏改;收敛为单一注册表。

prefix:调用方语境前缀(如 agent2 的 "[agent2 质检]"),区分落库角色;
不影响 [tool_name] 标签位置(前端提取逻辑对前缀无感)。
"""
from __future__ import annotations

from typing import Any, Callable

IntentBuilder = Callable[[dict[str, Any]], str]


def _b_run_python_code(a: dict[str, Any]) -> str:
    code = str(a.get("code") or "").strip()
    if not code:
        return "执行 Python 代码"
    first_line = code.split("\n", 1)[0][:100]
    return f"执行 Python 代码: {first_line}"


def _b_git_log(a: dict[str, Any]) -> str:
    fp = a.get("file_path")
    return f"查看提交历史{f': {fp}' if fp else ''}"


def _b_git_diff(a: dict[str, Any]) -> str:
    base = a.get("base", "HEAD~1")
    head = a.get("head", "HEAD")
    return f"查看变更 diff: {base}..{head}"


def _b_run_command(a: dict[str, Any]) -> str:
    cmd = (a.get("command", "") or "")[:40]
    return f"执行命令: {cmd}" if cmd else "执行 shell 命令"


def _b_str_replace_editor(a: dict[str, Any]) -> str:
    sub = a.get("command", "?")
    fp = a.get("file_path", "?")
    label = {
        "create": "创建文件",
        "str_replace": "编辑文件",
        "insert": "插入内容",
    }.get(sub, "编辑文件")
    return f"{label}: {fp}"


def _b_write_file(a: dict[str, Any]) -> str:
    mode = a.get("mode", "write")
    return f"{mode == 'append' and '追加' or '写入'}文件 {a.get('file_path', '?')}"


def _b_check_reference(a: dict[str, Any]) -> str:
    return f"复核引用: {str(a.get('url', ''))[:120]}"


def _b_http_request(a: dict[str, Any]) -> str:
    method = str(a.get("method", "GET")).upper()
    return f"验证请求: {method} {a.get('path', '/')}"


def _b_dispatch_review_subtasks(a: dict[str, Any]) -> str:
    subs = [s for s in (a.get("subagents") or []) if isinstance(s, dict)]
    if not subs:
        return "派发并行审查子任务"
    # label 缺省时按序号回退「子任务 N」(与 react_agent._dispatch_subtasks 同一口径)
    named = [str(s.get("label") or "").strip() or f"子任务{i + 1}"
             for i, s in enumerate(subs)]
    return f"派发 {len(subs)} 个并行审查子任务:{'/'.join(named)}"


def _b_dispatch_review_subagents(a: dict[str, Any]) -> str:
    ids = [str(i).strip() for i in (a.get("plan_item_ids") or []) if str(i).strip()]
    if not ids:
        return "派发审查子智能体"
    return f"派发 {len(ids)} 个审查子智能体:{'/'.join(ids)}"


_TOOL_INTENT_BUILDERS: dict[str, IntentBuilder] = {
    "clone_repo": lambda a: f"克隆仓库 {a.get('repo_url', '?')}",
    "list_files": lambda a: f"查看目录结构: {a.get('subdir') or '根目录'}",
    "find_files": lambda a: f"查找文件: {a.get('pattern', '?')}",
    "read_file": lambda a: f"读取文件 {a.get('file_path', '?')}",
    "search_code": lambda a: f"搜索代码: {a.get('pattern', '?')}",
    "query_cve": lambda a: (
        f"查询 {a.get('package_name', '?')}@"
        f"{a.get('version', '?')} 的已知漏洞"
    ),
    "list_dependencies": lambda a: "解析依赖清单",
    "run_lint": lambda a: "运行 lint 静态检查",
    "run_coverage": lambda a: "运行测试并解析覆盖率",
    "write_file": _b_write_file,
    "run_python_code": _b_run_python_code,
    "run_semgrep": lambda a: "运行 Semgrep 静态分析",
    "git_log": _b_git_log,
    "git_blame": lambda a: f"追溯文件来源: {a.get('file_path', '?')}",
    "git_diff": _b_git_diff,
    "run_command": _b_run_command,
    "str_replace_editor": _b_str_replace_editor,
    "list_skills": lambda a: "查看可用技能列表",
    "skill": lambda a: f"获取技能指令: {a.get('skill_name', '?')}",
    # agent2 引用复核
    "check_reference": _b_check_reference,
    # agent2 读取 agent1 报告全文(负向断言取证)
    "read_agent1_report": lambda a: f"读取 AI助手第 {a.get('round_idx', '?')} 轮完整报告",
    # verifier 动态验证
    "http_request": _b_http_request,
    # agent1 子任务按需派发
    "dispatch_review_subtasks": _b_dispatch_review_subtasks,
    # agent2 审查子智能体按需派发
    "dispatch_review_subagents": _b_dispatch_review_subagents,
}


def build_tool_intent(
    fn_name: str,
    fn_args: dict[str, Any] | None,
    *,
    prefix: str = "",
) -> str:
    """生成工具调用的意图说明(未知工具回退到"调用 {fn_name}")

    返回格式:"{prefix} {意图正文} [{fn_name}]"(prefix 为空时省略前缀)。
    """
    builder = _TOOL_INTENT_BUILDERS.get(fn_name)
    body = builder(fn_args or {}) if builder else f"调用 {fn_name}"
    text = f"{prefix} {body}" if prefix else body
    return f"{text} [{fn_name}]"
