"""OpenAI function-calling 工具定义

场景降级后:工具全部开放,不再按场景白名单过滤。
skill 过滤改为按 task.allowed_skills(用户创建任务时选择)。

- task_id 自动注入
- git_tokens 自动注入(用于 clone_repo 访问私有仓库,按 provider 提供对应 token)

并发安全:用 contextvars 替代全局变量,每个后台线程有独立上下文,
避免多任务并发执行时 task_id / allowed_skills / git_tokens 互相覆盖。
"""
import contextvars
import uuid
from typing import Any

from app.sandbox.client import SandboxGoneError
from app.tools import sandbox_tools
from app.tools.cve_tools import query_cve
from app.tools.dependency_tools import list_dependencies
from app.tools.quality_tools import run_coverage, run_lint
from app.tools.skill_tool import (
    list_available_skills,
    run_skill,
    set_current_allowed_skills,
    set_current_user_id,
)


# 当前任务的上下文(每个线程独立,避免并发任务互相覆盖)
_CURRENT_TASK_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_task_id", default=""
)
# 当前用户的各 git provider access_token(解密后的明文),{provider: token}
# clone_repo 工具按 repo_url 主机匹配 provider 取对应 token,执行完即用即弃不持久化
# 无默认值:读取处用 .get({}) 兜底(orchestrator 执行前会 set)
_CURRENT_GIT_TOKENS: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "current_git_tokens"
)
# 当前任务的 AI助手命令确认模式(从 task.params._executor_command_confirm 读取)
# "always_approve"(默认):危险命令直接执行不弹窗
# "per_command":危险命令执行前推前端 CommandConfirmDialog 弹窗确认
# 仅 run_command 工具读取此值;用于 sandbox 与 local 模式下统一控制危险命令确认行为
_CURRENT_EXECUTOR_COMMAND_CONFIRM: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_executor_command_confirm", default="always_approve"
)


def set_current_task(
    task_id: str,
    scenario_id: str = "general",
    allowed_skills: list[str] | None = None,
    user_id: uuid.UUID | None = None,
    executor_command_confirm: str = "always_approve",
) -> None:
    """react_agent 调用工具前,设置当前任务 ID 和允许的 skill 列表

    allowed_skills: 用户创建任务时选择的 skill 名称列表。
        None 或空列表表示全部 skill 可用(默认);
        非空时 skill 工具按此过滤。
    user_id: 任务所属用户 id。用于 skill 工具按用户过滤:
        内置 skill 全局共享,用户上传的 skill 仅 owner 可见。
        None 表示匿名任务(只看内置 skill)。
    executor_command_confirm: AI助手命令确认模式。
        "always_approve":危险命令直接执行不弹窗(默认)
        "per_command":危险命令推前端 CommandConfirmDialog 弹窗确认
        仅对内置 react_agent 的 run_command 工具生效;CLI 执行器走 ACP request_permission 机制独立处理。
    使用 ContextVar,每个后台线程的 set 只影响该线程自身。
    """
    _CURRENT_TASK_ID.set(task_id)
    set_current_allowed_skills(allowed_skills)
    set_current_user_id(user_id)
    if executor_command_confirm in ("always_approve", "per_command"):
        _CURRENT_EXECUTOR_COMMAND_CONFIRM.set(executor_command_confirm)
    else:
        _CURRENT_EXECUTOR_COMMAND_CONFIRM.set("always_approve")


def set_current_git_tokens(tokens: dict[str, str]) -> None:
    """orchestrator 在任务执行前设置当前用户的 git provider tokens(明文)

    {provider: token},只含有 access_token 的 provider。空 dict 表示未绑定任何平台,
    clone_repo 会回退到匿名 HTTPS/SSH。
    """
    _CURRENT_GIT_TOKENS.set(tokens)


# 工具名 → 函数的映射(通用工具,所有场景共用池)
TOOL_FUNCTIONS: dict[str, Any] = {
    "clone_repo": sandbox_tools.clone_repo,
    "list_files": sandbox_tools.list_files,
    "read_file": sandbox_tools.read_file,
    "search_code": sandbox_tools.search_code,
    "run_semgrep": sandbox_tools.run_semgrep,
    "find_files": sandbox_tools.find_files,
    "write_file": sandbox_tools.write_file,
    "run_python_code": sandbox_tools.run_python_code,
    "git_log": sandbox_tools.git_log,
    "git_blame": sandbox_tools.git_blame,
    "git_diff": sandbox_tools.git_diff,
    "run_command": sandbox_tools.run_command,
    "str_replace_editor": sandbox_tools.str_replace_editor,
    "query_cve": query_cve,
    "list_dependencies": list_dependencies,
    "run_lint": run_lint,
    "run_coverage": run_coverage,
    "list_skills": list_available_skills,
    "skill": run_skill,
}

# 通用工具的 OpenAI function-calling 定义
_ALL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "clone_repo",
            "description": "克隆 GitHub 仓库到沙箱,返回沙箱内的路径。任务开始时若需要代码,先调用此工具",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": "GitHub 仓库 URL",
                    },
                    "branch": {
                        "type": "string",
                        "description": "分支名,可选",
                    },
                },
                "required": ["repo_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取仓库内文件内容,返回带行号的内容(cat -n 格式),支持分页。"
                "首次读前 200 行,需要看后续内容时用 offset 翻页。"
                "行号可用于在结果中精确引用位置。"
                "也可读取记忆文件:/home/user/.agent_memory/project_memory.md(完整项目记忆)、"
                "/home/user/.agent_memory/global_memory.md(完整全局记忆),"
                "传绝对路径,不受 repo_path 限制。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "仓库内相对路径;或记忆文件绝对路径 "
                            "/home/user/.agent_memory/project_memory.md 或 "
                            "/home/user/.agent_memory/global_memory.md"
                        ),
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "本次最多返回行数,默认 200",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "从第几行开始读(1-based),默认 1。配合 max_lines 翻页",
                    },
                },
                "required": ["repo_path", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "列出仓库内某目录下的文件和子目录(单层,不递归)。"
                "clone 后先调用此工具查看仓库结构,再决定读哪些文件。"
                "看子目录后可再次调用进入子目录查看。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "subdir": {
                        "type": "string",
                        "description": "仓库内相对路径,默认根目录。如 'src'、'tests/unit'",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "最大返回条目数,默认 200",
                    },
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "在仓库内用正则搜索代码。支持三种输出模式: "
                "content(默认,返回匹配行+行号+可选上下文)、"
                "files_with_matches(只返回含匹配的文件路径)、"
                "count(返回每个文件的匹配数)。"
                "安全审计建议用 context_lines=3-5 看上下文,便于判断是否为漏洞。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件过滤 glob,如 '*.py'"},
                    "case_sensitive": {"type": "boolean", "description": "是否大小写敏感,默认 false"},
                    "max_matches": {"type": "integer", "description": "本次最多返回匹配数,默认 50"},
                    "context_lines": {
                        "type": "integer",
                        "description": "匹配行前后各显示 N 行上下文(仅 content 模式),默认 0",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "输出模式,默认 content",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "分页偏移,跳过前 N 个匹配,默认 0",
                    },
                },
                "required": ["repo_path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "按文件名 glob 模式递归查找仓库内文件,返回匹配的文件路径列表(不看内容)。"
                "适用于知道文件名或扩展名但不知道具体位置的场景,"
                "如找所有测试文件、配置文件、特定命名的模块。"
                "pattern 示例:**/*.py(所有 Python 文件)、**/test_*.py(测试文件)、"
                "src/**/*.ts(src 下的 TS)、**/*.{js,ts}(JS 和 TS)。"
                "与 list_files 区别:list_files 列单层目录看结构;find_files 按 pattern 递归定位文件。"
                "与 search_code 区别:search_code 按内容正则搜索;find_files 按文件名 pattern 搜索。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "glob 模式,支持 *、**、?、{a,b}。"
                            "如 **/*.py、src/**/*.ts、**/test_*.py、**/*.{js,ts}"
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回文件数,默认 100",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "分页偏移,跳过前 N 个结果,默认 0",
                    },
                },
                "required": ["repo_path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "在工作区写入文件(不影响原仓库)。工作区独立于 clone 的仓库,"
                "用于写 PoC 脚本、修复补丁、分析报告等产物。"
                "原仓库保持只读,保证审计可追溯。"
                "写入后可用 run_python_code 执行脚本,或再次 write_file 追加内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": (
                            "工作区内相对路径,如 'poc/sqli_test.py'、'patches/fix.diff'。"
                            "不能含 .. 或绝对路径(防路径穿越)。"
                            "父目录会自动创建。"
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容(文本)。上限 200000 字符",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["write", "append"],
                        "description": "写入模式:write 覆盖(默认),append 追加",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python_code",
            "description": (
                "在沙箱里执行 Python 代码,返回 stdout/stderr/exit_code。"
                "用于验证漏洞 PoC(触发 SQL 注入、跑反序列化 payload)、"
                "跑分析脚本(解析依赖树、调用图)、执行测试验证假设。"
                "工作目录是工作区根,write_file 写的脚本可直接执行。"
                "网络访问依赖沙箱配置(默认禁外网)。单次执行超时 60s。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python 代码(字符串)。多行直接写,无需转义",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "执行超时秒数,默认 60,上限 120",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": (
                "查看仓库提交历史(默认 --oneline 一行一提交)。"
                "用于理解代码演化、定位某次改动何时引入、结合 git_blame 追溯责任提交。"
                "需要完整克隆(默认即完整克隆);浅克隆下仅 1 条 commit。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "max_count": {
                        "type": "integer",
                        "description": "最多返回提交数,默认 20,上限 200",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "可选,只看某文件的历史(仓库内相对路径)",
                    },
                    "oneline": {
                        "type": "boolean",
                        "description": "True=--oneline 紧凑输出(默认);False=含作者/日期/正文",
                    },
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_blame",
            "description": (
                "追溯某文件每行的最后修改提交/作者/时间,定位'这行代码是谁/哪次提交改的'。"
                "需要完整克隆(默认即完整克隆);浅克隆下 blame 信息受限。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "file_path": {
                        "type": "string",
                        "description": "仓库内相对路径(必填)",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号(1-based,可选)",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号(1-based,可选;只传一个行号时按单行区间处理)",
                    },
                },
                "required": ["repo_path", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "查看两个 ref(提交/分支/标签)之间的结构化 diff(增量审查/演化分析用)。"
                "默认看最近一次提交的变更(base=HEAD~1 head=HEAD);"
                "也可比较分支(如 base='main' head='feature')。"
                "大区间先用 stat_only=true 看每文件增删行数总览,再针对重点文件拉全量 diff。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "base": {
                        "type": "string",
                        "description": "起始 ref,默认 HEAD~1。可传提交哈希/分支名/标签",
                    },
                    "head": {
                        "type": "string",
                        "description": "结束 ref,默认 HEAD",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "可选,只看某文件的 diff(仓库内相对路径)",
                    },
                    "stat_only": {
                        "type": "boolean",
                        "description": "True=只返回每文件增删行数(不含 patch),默认 False",
                    },
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "在沙箱里执行任意 shell 命令(与 CLI 的 bash 工具对齐)。"
                "用于跑构建/测试/脚本,如 ./build.sh、pytest -x、npm test、pip show pkg。"
                "沙箱内执行,网络按沙箱配置(默认禁外网)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "shell 命令字符串"},
                    "repo_path": {
                        "type": "string",
                        "description": "可选,clone_repo 返回的 path。提供则在仓库目录下执行",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时秒,默认 60,上限 300",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace_editor",
            "description": (
                "对仓库文件做外科手术式编辑(对齐 CLI 的 str_replace_editor),就地改仓库代码。"
                "可逆:git diff 回看、git checkout 回退。create=建新文件(不可覆盖已有),"
                "str_replace=精确替换(old_str 须唯一匹配或 replace_all),insert=在指定行后插入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["create", "str_replace", "insert"],
                        "description": "编辑命令",
                    },
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "file_path": {"type": "string", "description": "仓库内相对路径"},
                    "file_text": {"type": "string", "description": "create: 新文件完整内容"},
                    "old_str": {"type": "string", "description": "str_replace: 被替换的精确字符串"},
                    "new_str": {
                        "type": "string",
                        "description": "str_replace: 替换为 / insert: 要插入的文本",
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "insert: 在此行(1-based)之后插入;0=末尾追加",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "str_replace: True=替换所有匹配(默认 False,须唯一匹配)",
                    },
                },
                "required": ["command", "repo_path", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_cve",
            "description": (
                "查询指定包+版本的已知 CVE 漏洞(用 OSV API)。"
                "对每个依赖调一次"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_name": {"type": "string", "description": "包名"},
                    "version": {"type": "string", "description": "版本号"},
                    "ecosystem": {"type": "string", "description": "包管理系统,默认 python"},
                },
                "required": ["package_name", "version"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dependencies",
            "description": (
                "扫描仓库清单文件(requirements*.txt/pyproject.toml/package.json/"
                "package-lock.json/go.mod/pom.xml),返回结构化依赖清单。"
                "依赖漏洞审计时先调它拿清单,再对 version 非空的依赖逐个调 query_cve。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_lint",
            "description": (
                "运行静态 lint 检查,返回结构化问题清单(file/line/code/message)。"
                "Python 走 ruff(local 模式需宿主机已装,sandbox 模式自动安装);"
                "JS/TS 仅在仓库自带 eslint 配置时尝试,否则建议改用 run_semgrep。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "language": {
                        "type": "string",
                        "description": "auto(默认,按清单文件探测)/ python / javascript",
                    },
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_coverage",
            "description": (
                "跑测试并解析覆盖率,返回总覆盖率 + 未覆盖行数 top 文件。"
                "Python 项目走 pytest --cov(沙箱自动安装);检测到 vitest 的 JS 项目尝试 vitest。"
                "测试覆盖度审查时优先用它,拿不到结果再降级 run_command 手跑。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_semgrep",
            "description": "运行 Semgrep 静态分析。local 模式需宿主机已安装 semgrep(否则返回不可用提示),sandbox 模式自动安装",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "config": {"type": "string", "description": "semgrep 配置,默认 auto"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": (
                "列出当前场景所有可用的 skill(技能)。"
                "skill 是预封装的多步审计操作指令,clone 完仓库后建议先调用此工具查看可用技能。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": (
                "获取指定 skill 的详细指令(SKILL.md 内容)。"
                "拿到指令后,按其指引自行调用 search_code/read_file/run_semgrep 等底层工具执行。"
                "先调 list_skills 查可用 skill 名称。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "skill 名称,如 check_sql_injection",
                    },
                },
                "required": ["skill_name"],
            },
        },
    },
]


def get_all_tools() -> list[dict[str, Any]]:
    """返回全部工具定义(场景降级后:工具不再按场景白名单过滤,全部开放)

    react_agent 自主判断哪些工具适用当前任务。
    """
    return list(_ALL_TOOL_DEFINITIONS)


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """执行通用工具调用

    自动从 ContextVar 注入 task_id 和 git_tokens,LLM 看不到这两个参数。
    git_tokens 只对 clone_repo 注入(其他工具不接受该参数,避免误传)。
    command_confirm_mode 只对 run_command 注入(控制危险命令是否弹窗确认)。

    沙箱容器被 Server 回收(SandboxGoneError)时顺手丢掉本地会话:下一个工具调用
    会重建一个新容器。不等探活节流(最长 60s)才处理,是因为一次过期会让整轮工具
    调用全部砸在同一个死会话上,模型只会反复看到同一个错;先清干净,重试时
    至多就是"工作区空了,得重新 clone"。
    """
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具: {tool_name}")
    func = TOOL_FUNCTIONS[tool_name]
    task_id = _CURRENT_TASK_ID.get()
    arguments["task_id"] = task_id
    if tool_name == "clone_repo":
        arguments["git_tokens"] = _CURRENT_GIT_TOKENS.get()
    if tool_name == "run_command":
        arguments["command_confirm_mode"] = _CURRENT_EXECUTOR_COMMAND_CONFIRM.get()
    try:
        return func(**arguments)
    except SandboxGoneError:
        if task_id:
            sandbox_tools.drop_gone_session(str(task_id))
        raise
