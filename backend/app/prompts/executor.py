"""执行链(内置 react_agent + CLI/acp_base)的提示词与上下文段落。

集中管理所有送进 AI助手 LLM 的文本资产:
- system prompt 常量、追问指引、plan 提醒、循环/迭代兜底提示
- repo context / 上传文件 / 记忆段等上下文段落的纯文本包装函数

定位与边界:
- 数据加载(DB 查询、沙箱探测)不在本模块(memory_injection / 各执行器负责)
- 消息装配(哪段进 system 哪段进 user)由两侧薄装配层决定:
  内置 react_agent(结构化 messages)与 acp_base(单条 user 文本)
- 保持零 app.* 依赖(tests/test_prompts_purity.py 固化)
"""

from typing import Any

# ============================================================
# 首轮问题(react_agent 首轮 user 消息 + create_task 落库共用)
# ============================================================


def _has_creation_upload(params: dict | None) -> bool:
    """创建时是否带上传文件(兼容 legacy 单数 upload_id 与多文件 upload_ids)

    react_agent / acp_base 用它判定"上传任务"语境(选择 repo_context 的
    upload 包裹变体),与 orchestrator._creation_upload_ids 语义一致;
    此处内联避免跨模块循环导入。
    """
    p = params or {}
    return bool(p.get("upload_id") or p.get("upload_ids"))


def build_first_round_question(user_input: str, params: dict | None) -> str:
    """构造首轮"用户提问"对话的落库内容(user_input + 仓库/分支元信息)

    任务创建时(create_task)即以此内容落库 role=user / type=question 对话,
    保证前端首屏 getTask 快照立即显示用户提问,无需等待后台线程完成预 clone、
    react_agent 启动后才出现(此前问题气泡会晚于助手回答显示)。

    react_agent 首轮复用同一函数构造 user_msg,保证展示内容与创建时落库完全
    一致 —— 这是 (task, round_idx, user, question) 幂等去重成立的前提。

    注意:不含"预 clone 上下文段"与"跨轮历史记忆块",两者属系统编排信息,
    只进发送给 LLM 的内容,不落库展示。

    上传任务也不再附"用户上传的文件已放入任务工作区"行:该信息同属系统编排
    信息(工作区已就绪),统一由上传变体的 repo_context 段承载(只进发送内容),
    避免泄漏进前端用户提问气泡。
    """
    params = params or {}
    content = user_input
    if params.get("repo_url"):
        content += f"\n仓库地址: {params['repo_url']}"
    if params.get("branch"):
        content += f"\n分支: {params['branch']}"
    return content


# ============================================================
# 通用 system prompt(场景降级后,不再从场景读取)
# ============================================================

REACT_AGENT_SYSTEM_PROMPT = """你是 react_agent(AI助手),负责执行实际的分析任务(如代码审计、审查、质量分析等)。

## 你的职责
根据任务指令对目标仓库执行分析,发现并记录问题,最后用自然语言总结你的发现。

## 工作方式(ReAct 循环)
你通过"思考-行动-观察"循环工作:
1. **思考**:分析当前状态,决定下一步该做什么
2. **行动**:调用工具(clone_repo / list_files / find_files / read_file /
   search_code / run_semgrep / query_cve / list_dependencies / write_file /
   run_python_code / git_log / git_blame / git_diff / run_command / run_lint /
   run_coverage / str_replace_editor / list_skills / skill,以及按需派发的
   dispatch_review_subtasks 等)
3. **观察**:查看工具返回的结果
4. 重复以上步骤,直到完成分析

## 可用工具
- clone_repo:克隆 GitHub 仓库到沙箱(若系统已预克隆,无需调用)
- list_files:列出目录结构(单层,跳过 .git/node_modules 等噪声目录)
- find_files:按文件名 glob 模式递归查找文件(如 **/*.py、**/test_*.py),返回路径列表
- read_file:读取文件内容(带行号,支持 offset 翻页)
- search_code:正则搜索代码,支持 content/files_with_matches/count 三种输出模式
- run_semgrep:运行 Semgrep 静态分析(local 模式需宿主机已装 semgrep,sandbox 模式自动安装)
- query_cve:查询指定包+版本的已知 CVE 漏洞(OSV API,按依赖逐个查)
- list_dependencies:扫描仓库清单文件返回结构化依赖清单(依赖审计先调它拿清单,再逐个 query_cve)
- write_file:在工作区写产物(PoC 脚本、报告等);改仓库代码用 str_replace_editor
- run_python_code:在沙箱执行 Python 代码,验证 PoC / 跑分析脚本 / 执行测试
- run_command:在沙箱执行任意 shell 命令(构建/测试/脚本,如 pytest、npm test),与 CLI 的 bash 对齐
- run_lint:静态 lint 检查返回结构化问题清单(Python 走 ruff;JS 需仓库自带 eslint 配置)
- run_coverage:跑测试并解析覆盖率(总覆盖率 + 未覆盖 top 文件),测试覆盖度审查优先用它
- str_replace_editor:对仓库文件做精准编辑(create/str_replace/insert),就地改代码(git diff/checkout 可逆)
- git_log:查看仓库提交历史(默认 --oneline),理解代码演化、定位改动何时引入(需完整克隆,默认即完整)
- git_blame:追溯某文件每行的最后修改提交/作者,定位"这行是谁/哪次提交改的"(需完整克隆)
- git_diff:查看两个 ref 间的结构化 diff(增量审查;默认最近一次提交,大区间先用 stat_only 总览)
- list_skills / skill:查看并加载专家技能(获取 SKILL.md 指令后按其指引执行)
- dispatch_review_subtasks(按需派发工具):你为每个子任务自写要求/上下文并选定
  可见技能子集,派发并行只读审查子任务,调用会等待全部子任务完成后返回各任务
  结论(详见下方"子任务按需派发")

## 工作原则
- **自适应任务类型**:根据用户意图判断任务性质(安全审计/代码审查/质量分析/
  架构理解/功能梳理等),采用相应的分析方法。可调用 list_skills 查看是否有
  适用的专家技能。
- **系统性覆盖**:按指令指定的维度逐一分析,不遗漏。
- **证据导向**:每个结论都应有具体文件位置和代码证据,不臆测。
- **高效执行**:优先用 search_code 定位关键代码,再 read_file 确认细节,
  避免盲目遍历所有文件。
- **计划性**:复杂任务先输出 <plan> 步骤清单,逐步推进。

## 子任务按需派发(仅当工具列表里有 dispatch_review_subtasks 时适用)
并行审查子任务 = 每条独立的只读核查子循环,由你按需派发:每个子任务自带一段
instruction(你为该任务写的要求/上下文),可选地给它一个 skills 可见技能子集
(只能是下方列出的技能名的子集,可为空);技能正文不再预注入,子任务按需用 skill
工具自行读取。
**先计划后派发**:先看清任务与仓库结构,列出需要核查的维度和关键路径;
只把互不依赖的独立核查维度交给子任务,关键路径(定性判断、跨任务交叉验证、
产物输出)自己做。
**何时派**:全量/多维度审计 → 一次派发全部相关子任务;多维度定向核查 →
只派相关子任务。一次任务通常只派发 1 次。
**何时不派**:单一具体问题(自己用 read_file / search_code 查)、纯对话或
澄清(直接回答)、结论已在手(不重复派发)。
**派发后**:基于各任务结论集成、补缺、交叉验证,不重跑已完成的任务;
某任务失败或超时时,先判断能否自行补查,再如实汇报哪些维度未覆盖。
派发调用会等待全部子任务完成才返回,返回的就是各任务的结论(含失败/超时状态)。

## 计划格式(可选,复杂任务建议)
在思考内容中输出 <plan> 标签包裹的计划:
<plan>
[{"id": 1, "text": "步骤描述", "status": "pending"},
 {"id": 2, "text": "步骤描述", "status": "pending"}]
</plan>
status 可选:pending / in_progress / done。后端会解析并推送前端展示。

## 输出要求
- 每轮结束(不再调用工具时),用自然语言总结你的发现:
  - 发现了哪些问题/现象/结论(按任务性质组织,如漏洞/缺陷/风险/改进点/架构特点)
  - 具体文件位置和代码片段
  - 影响范围/严重程度(若适用)
  - 修复或改进建议(若适用)
- 总结要具体、有证据,**面向用户可直接理解与行动**(你的总结是展示给用户的
  核心回答;后台质检员会另行核查,不需要你面向评审组织语言)。
- 用户的问题需要明确结论时,在总结中直接给出结论。
- 不要在总结中编造未经验证的发现。
"""

# ============================================================
# 系统注入标记 + 追问轮编排指引(react 侧)
# ============================================================

# 系统注入消息的边界标记(对齐 Codex ContextualUserFragment 的 marker 思路):
# 所有编排注入(工具摘要/评审反馈/仓库路径/续跑指引)的消息内容必须以
# "[系统注入|来源]" 开头,让模型能区分"用户原话"与"系统注入",
# 防止注入内容被当成用户指令(间接注入面)
SYSTEM_INJECT_MARKER = "[系统注入|"

# 追问消息处理指引的核心文本(内置 react_agent 与 CLI/acp_base 两侧共用,
# 各自的外层框架不同:react 加系统注入标记进独立 system 消息,
# acp 拼进 user 消息并用 [本轮补充要求] 包装追问原文)
FOLLOWUP_CORE_GUIDANCE = (
    "用户追问可直接回答,新需求/修正则执行对应工作,续跑则接着完成,"
    "均不要重做已完成的部分"
)

# 追问轮的编排指引模板(系统注入,与用户追问分离;react 侧)
FOLLOWUP_GUIDANCE = (
    f"{SYSTEM_INJECT_MARKER}续跑指引]\n"
    "本消息之前的历史对话是同一任务之前轮次的执行记录。"
    "请基于已有进度处理接下来的用户消息:"
    + FOLLOWUP_CORE_GUIDANCE
    + "。"
)


# ============================================================
# repo context 段落(预 clone / 上传文件的上下文包装)
# ============================================================


def build_repo_context_section(
    repo_context: str, variant: str = "clone",
) -> str:
    """把 repo_context 包上引导提示,拼进首轮 user 消息尾部(内置 react_agent 侧)

    variant:
    - "clone":orchestrator 已预 clone 仓库(提示跳过 clone_repo,并列出内置工具)
    - "upload":工作区是用户上传的文件,直接陈述位置,不提 clone
    """
    if variant == "upload":
        return (
            "\n\n[用户上传的文件已就绪]\n"
            + repo_context
            + "\n\n请直接基于上述路径开始执行任务(用 read_file / search_code / "
            "list_files 等工具)。"
        )
    return (
        "\n\n[仓库已预先 clone,无需你再调用 clone_repo]\n"
        + repo_context
        + "\n\n请直接基于上述仓库路径开始执行任务(用 read_file / search_code / "
        "list_files 等工具),不要再调用 clone_repo。"
    )


def build_cli_repo_context_section(
    repo_context: str | None, variant: str = "clone",
) -> str:
    """CLI 执行器侧的 repo context 包裹段(acp_base 消费)

    与内置侧差异:CLI 自带工具集(Read/Grep 等),结尾不列举内置工具名。
    上传任务此前被统一包上"仓库已预先 clone"与正文矛盾(已修复):
    variant 为 "upload" 时直接陈述用户上传文件,不提 clone。
    """
    if not repo_context:
        return ""
    if variant == "upload":
        return (
            "\n\n[用户上传的文件已就绪]\n"
            + repo_context
            + "\n\n请直接基于上述路径开始执行任务。"
        )
    return (
        "\n\n[仓库已预先 clone,无需你再调用 clone_repo]\n"
        + repo_context
        + "\n\n请直接基于上述仓库路径开始执行任务。"
    )


def format_repo_context_body(
    repo_url: str, repo_path: str, files_result: dict,
    header: str | None = None,
) -> str:
    """把 clone / 上传结果 + list_files 结果格式化成给 agent 看的上下文文本

    格式:
        仓库 <url> 已克隆到 <path>   (或 header 自定义首行,如上传文件)
        根目录结构(共 N 项):
          [目录] src
          [文件] README.md (1234 B)
          ...
    """
    entries = files_result.get("entries", [])
    total = files_result.get("total", 0)
    truncated = files_result.get("truncated", False)

    lines = [header or f"仓库 {repo_url} 已克隆到 {repo_path}"]
    trunc_hint = ", 已截断(仅显示部分)" if truncated else ""
    lines.append(f"根目录结构(共 {total} 项{trunc_hint}):")
    for e in entries:
        etype = e.get("type", "file")
        name = e.get("name", "?")
        if etype == "dir":
            lines.append(f"  [目录] {name}")
        else:
            size = e.get("size", 0)
            size_str = f" ({size} B)" if size else ""
            lines.append(f"  [文件] {name}{size_str}")

    return "\n".join(lines)


def build_upload_header(metas: list[dict], repo_path: str) -> str:
    """上传任务 repo_context 的首行文案(单上传/多上传两变体)

    措辞原则:直白陈述"用户上传的文件 + 位置",不使用内部术语,不提 clone。
    """
    if len(metas) == 1:
        meta = metas[0]
        filename = meta.get("filename") or "上传文件"
        file_count = meta.get("file_count") or "?"
        return (
            f"用户上传的文件({filename},共 {file_count} 个)"
            f"已放入 {repo_path},可直接开始处理"
        )
    names = "、".join((m.get("filename") or "上传文件") for m in metas)
    return (
        f"用户上传的 {len(metas)} 组文件({names})"
        f"已分别放入 {repo_path} 下的独立子目录,可直接开始处理"
    )


# ============================================================
# plan 状态提醒(react/cli 双变体;react 侧循环内按前缀查找替换更新)
# ============================================================


def format_plan_reminder(plan_steps: list[dict], variant: str = "react") -> str:
    """格式化 plan 状态提醒(内置 react_agent 注 system 消息 / CLI 拼 user 消息)

    variant:
    - "react":末行要求在回答开头 <plan> 回写完整清单(内置 plan 更新机制依赖);
    - "cli":首行中立措辞,兼容 CLI 原生 TodoList 等计划工具,无末行。

    两变体共享步骤行格式(○/◌/✓ 符号映射)与首行前缀
    "[系统提醒] 当前计划清单状态"——react_agent 循环内按该前缀查找替换
    更新提醒(不累积),改动前缀需同步替换逻辑。
    """
    if not plan_steps:
        return ""
    if variant == "cli":
        lines = [
            "[系统提醒] 当前计划清单状态(状态有变化时请用自己的方式更新完整清单——"
            "在 <plan> 里继续输出或更新你的 TodoList 等计划工具,完成的标 done):"
        ]
    else:
        lines = [
            "[系统提醒] 当前计划清单状态"
            "(已完成的请标记 [done],正在做的标 [in_progress]):"
        ]
    status_symbol = {"pending": "○", "in_progress": "◌", "done": "✓"}
    for s in plan_steps:
        sym = status_symbol.get(s["status"], "○")
        lines.append(f"{sym} [{s['status']}] {s['text']}")
    if variant != "cli":
        lines.append(
            "如果某个步骤状态有变化,请在回答开头的 <plan> 里输出更新后的完整清单"
            "(完成的标 done)。"
        )
    return "\n".join(lines)


# ============================================================
# 用户补充消息注入(运行中/暂停中场景)
# ============================================================


def format_injected_user_messages(
    messages: list[dict[str, Any]], attachment_note: str = "",
) -> str:
    """把 drain 出的用户补充消息格式化为一条 LLM user 消息文本

    多条消息按时间顺序合并为一条,加前缀说明这是用户在审计过程中追加的指令,
    引导模型理解为新的检查方向/补充要求,而非替换原始任务。

    attachment_note:本批消息附带文件已传输进工作区的提示(可空,路径按实际
    传输结果,见 format_followup_attachment_note),拼在正文后,让模型知道去
    哪个目录读新文件。

    返回空字符串表示无可注入内容(消息 content 全为空)。
    """
    parts: list[str] = []
    for msg in messages:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        parts.append(content)

    if not parts:
        return ""

    if len(parts) == 1:
        body = parts[0]
    else:
        body = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(parts))

    return (
        "[用户在审计过程中追加的消息]\n"
        "请把以下内容作为新的检查方向或补充要求纳入当前任务,"
        "结合已掌握的仓库信息继续执行(无需重新 clone):\n\n"
        f"{body}"
        f"{attachment_note}"
    )


# ============================================================
# 附带文件提示 + 轮次消息标记
# ============================================================

# 提示里最多列出的附件路径数(批量上传时正文会挤掉用户真正的那句话)
_ATTACHMENT_NOTE_MAX_PATHS = 20

# 附件未能写入工作区:如实告知。历史缺陷是无仓库任务 repo_path 为空 →
# 传输抛错被调用方吞掉,提示却照旧注入,模型对着不存在的文件只能编答案
# (或回答"工作区是空的"),用户完全看不出附件根本没送达。
ATTACHMENT_UNAVAILABLE_NOTE = (
    "\n\n[注:用户本轮附带的文件未能写入任务工作区,内容不可读。"
    "请如实告知用户附件未送达(建议重新提交任务并以上传文件作为交付物来源),"
    "不要根据文件名臆测内容]"
)


def format_attachment_note(
    header: str, added_paths: list[str], work_root: str = "",
) -> str:
    """附件已入工作区的提示正文(路径取**实际传输结果**,不写死目录名)

    header:提示首句(区分运行中追问 / resume 语境,由上层两个包装函数传入)
    added_paths:相对工作根的路径(与文件树、沙箱过期后的回退浏览同一口径),
        空列表时返回空串 —— 没传成就不该说"已放入"
    work_root:工作根绝对路径(可空),附上后模型不必自己猜路径
    """
    if not added_paths:
        return ""
    shown = added_paths[:_ATTACHMENT_NOTE_MAX_PATHS]
    lines = "\n".join(f"- {p}" for p in shown)
    if len(added_paths) > len(shown):
        lines += f"\n- ...(共 {len(added_paths)} 个,其余未列出)"
    tail = f"\n工作区根目录: {work_root}" if work_root else ""
    return (
        f"\n\n{header}\n{lines}{tail}\n"
        "用 list_files / read_file 查看(路径相对工作区根目录)"
    )


def format_followup_attachment_note(added_paths: list[str], work_root: str = "") -> str:
    """react_agent 循环内:用户在执行中追加消息且本轮附带了新上传文件"""
    return format_attachment_note(
        "[用户本轮附带了新文件,已放入工作区:]", added_paths, work_root,
    )


def format_followup_upload_warning(reason: str) -> str:
    """附件没进工作区的用户可见告警正文(落库 Conversation type=warning)

    运行中追问(react_agent drain)与完成后追问(orchestrator resume)两条链路
    共用,文案不再各写一份。reason 取异常正文或简要说明,截断后拼入。
    """
    return (
        "用户附带的文件未能写入任务工作区,智能体读不到它:"
        f"{(reason or '各附件均未能写入工作区')[:300]}\n"
        "可在「用户上传」区确认附件内容,或重新提交任务并以上传文件作为交付物来源。"
    )


def format_resume_attachment_note(added_paths: list[str], work_root: str = "") -> str:
    """resume 链路(orchestrator):用户追问消息附带新上传文件"""
    return format_attachment_note(
        "[本轮附带文件已放入工作区:]", added_paths, work_root,
    )


# 消息性质标记(拼到 user_intent 后供 agent2 审查理解语境)
RETRY_MSG_LABEL = "[重试续跑]"
USER_FOLLOWUP_MSG_LABEL = "[用户追加消息]"

# 暂停恢复后自动续跑的系统默认文案:仅当用户未在暂停期间排队真实补充
# 消息时使用(有排队消息则用用户原文,见 orchestrator._resolve_continuation_message)。
# 走同一活跃 ACP session,故不重述任务背景,只要求"从中断处接着做"。
AUTO_CONTINUE_FOLLOWUP = (
    "(系统)上轮执行已被用户暂停中断。请在当前会话上下文中从中断处继续完成"
    "尚未做完的任务,不要重复已完成的部分,直接接着往下推进。"
)


# ============================================================
# CLI 侧记忆注入段包装(acp_base 消费;数据加载在 memory_injection)
# ============================================================


def build_cli_memory_section(
    memory_summary: str = "", global_memory: str = "",
    *,
    project_file_path: str = "/home/user/.agent_memory/project_memory.md",
    global_file_path: str = "/home/user/.agent_memory/global_memory.md",
) -> str:
    """构造记忆注入段(拼在发送给 CLI 的 prompt 末尾,不落库不展示)

    - 项目记忆精简版 + 完整记忆文件路径提示
    - 全局长期记忆段(跨项目通用经验)+ 完整记忆文件路径提示

    两部分都为空时返回空串。每轮注入(与 react_agent system prompt 行为一致)。

    project_file_path/global_file_path 由调用方按运行模式传入(sandbox=/home/user;
    local=真实 <local_dir>/.agent_memory/...),措辞工具中立:外部 CLI 用各自的 Read
    工具、访问不到后端 read_file 对 /home/user 虚拟路径的映射,故不写 read_file。
    默认值保持 /home/user(行为中性 + 保护直接调用/现有测试)。
    """
    section = ""

    # 项目记忆精简版 + 完整记忆文件路径提示(每轮注入,与 react_agent system prompt 行为一致)
    summary = (memory_summary or "").strip()
    if summary:
        section += (
            "\n\n[项目记忆摘要]\n"
            + summary
            + f"\n\n完整项目记忆见文件 {project_file_path}(用你的文件读取工具查看)"
        )

    # 全局长期记忆(跨项目通用经验,影响执行方式;与 react_agent system prompt 行为一致)
    # 完整文件在任务启动时已写入沙箱/local 目录,超截断上限时 CLI 可用自身工具查全量
    gmem = (global_memory or "").strip()
    if gmem:
        section += (
            "\n\n" + gmem
            + f"\n\n完整全局记忆见文件 {global_file_path}(用你的文件读取工具查看)"
        )

    return section


# ============================================================
# CLI 侧可用技能(Skill)注入段(acp_base 消费;数据加载在调用侧)
# ============================================================


def build_cli_skills_section(skills: list[dict[str, Any]]) -> str:
    """构造"可用技能"段(拼在发送给 CLI 的 prompt 中,不落库不展示)。

    背景:外部 CLI 执行器没有后端进程内的 skill 注册表,拿不到内置 react_agent 的
    list_skills/skill 工具;故 orchestrator 在任务启动时把"当前任务可见且允许"的
    skill 物化进容器(见 sandbox_tools.write_skill_files),这里只注入一段清单 +
    路径指针,引导 CLI 用自身 Read 工具按需查阅 SKILL.md 并按其指令执行。

    skills:调用侧预解析的列表,每项为
        {"name": str, "description": str, "file": str}
    其中 file 是 CLI 可直达的 SKILL.md 绝对路径(按运行模式算好,含物化子目录名);
    本函数零 app.* 依赖,不做任何路径/DB 计算(见 tests/test_prompts_purity.py)。

    空列表返回空串(无可用 skill 时不产生多余段落)。措辞工具中立(不写 read_file)。
    """
    items = [s for s in (skills or []) if (s.get("name") or "").strip()]
    if not items:
        return ""

    lines = [
        "- {}: {}(完整指令见文件 {},需要时先用你的文件读取工具查看再按其指引执行)".format(
            (s.get("name") or "").strip(),
            (s.get("description") or "").strip(),
            (s.get("file") or "").strip(),
        )
        for s in items
    ]
    return (
        "\n\n[可用技能(Skills)]\n"
        "以下是本任务可用的技能(预封装的审计/执行操作指引)。当某项与当前任务相关时,"
        "读取对应 SKILL.md 并按其指令调用你的工具执行:\n"
        + "\n".join(lines)
    )


# ============================================================
# CLI 侧后台子任务结论落盘段(acp_base 消费;目录由调用侧按运行模式算好)
# ============================================================


def build_cli_subagent_reports_section(reports_dir: str) -> str:
    """构造"后台子任务结论必须落盘"段(拼在发送给 CLI 的 prompt 中,不落库不展示)。

    背景:部分 CLI(Qoder 的 Agent 工具)以 fire-and-forget 方式派生后台子任务,
    主智能体常在子任务回报前就结束本轮;而沙箱 TTL 回收会把 CLI 自己的会话磁盘
    一起带走,下一轮 session/resume 失败时那些结果物理不可恢复。故这里要求把结论
    落到工作区之外的独立目录:主智能体据此汇总,后端在回合结束前回捞成持久 artifact。

    reports_dir:CLI 可直达的绝对路径(仅 sandbox 模式由调用侧给出;空串=不注入)。
    本函数零 app.* 依赖,不做任何路径/DB 计算(见 tests/test_prompts_purity.py);
    措辞工具中立(不写 read_file),并明确写在被审查仓库之外以免污染工作区 diff。
    """
    d = (reports_dir or "").strip()
    if not d:
        return ""
    d = d.rstrip("/")
    return (
        "\n\n[后台子任务结论落盘要求]\n"
        "如果你派生后台子任务(工具立刻返回\"已在后台运行\"之类的回执,结果稍后才回报),"
        "请在每个子任务的指令中明确要求它:\n"
        f"1. 把**完整结论**写入文件 {d}/<英文短名>.md(Markdown,含定位到的文件:行号、"
        "证据与修复建议),文件写完再结束子任务;\n"
        f"2. 只写进 {d}/ 这个目录,**不要**写进被审查的仓库目录"
        "(仓库内的新增文件会污染工作区变更);\n"
        f"3. 本轮收尾前若已能读到落盘结论,请**逐条汇总这些文件**并给出完整报告;"
        "读不到时如实说明哪些子任务仍未回报,不要把\"已派子任务\"当成结论。"
    )


# ============================================================
# CLI 侧禁止 fan-out 段(acp_base 消费;与"落盘要求"段互斥)
# ============================================================


def build_cli_no_subagent_section() -> str:
    """构造"不要派生子智能体"预防段(拼在首轮 prompt 中,不落库不展示)。

    背景:部分 CLI(Qoder 的 Agent 工具)以 fire-and-forget 方式派生后台子任务,
    而 ACP 事件不带子智能体身份、并行输出交织成乱码,续轮回收又不可靠,故这里
    预防性要求它在主循环串行完成审查。措辞工具中立(不写具体工具名),
    零 app.* 依赖(见 tests/test_prompts_purity.py)。本段无参数、内容恒定,
    由调用侧命中白名单时并入记忆桶参与去重。
    """
    return (
        "\n\n[子智能体使用限制]\n"
        "本次审查**不要派生任何后台/并行子智能体**(不要调用会启动子任务的工具,"
        "也不要在指令中要求子任务稍后回报)。请在**当前主循环里按审查子任务逐个串行完成**"
        "全部核查,并**一次性输出完整报告**(含定位到的文件:行号、证据与修复建议)。"
    )


# ============================================================
# CLI 侧跨轮历史回放段(acp_base 消费;结构化历史的加载与压缩在执行器侧)
# ============================================================

# 历史回放的角色标签:CLI 的 session/prompt 只有 text 通道,没有 user/
# assistant 角色通道,角色只能写进文本
_CLI_HISTORY_ROLE_LABELS = {
    "user": "用户",
    "assistant": "执行者",
}


def build_cli_history_replay_section(messages: list[dict[str, Any]]) -> str:
    """把结构化历史消息渲染成"之前轮次执行记录"回放段(CLI/acp_base 消费)

    使用场景:ACP 侧新建 session(沙箱重建 / 后端重启 / 启动参数或凭证变化)
    时,CLI 看不到同一任务之前轮次做过什么,追问轮只剩一句"基于之前的执行
    进度"会退化成重做或答非所问,故把历史回放拼进本轮 prompt。

    渲染约定:
    - user → "用户:",assistant → "执行者:"(角色边界写进文本)
    - system 消息自带 [系统注入|来源|第 N 轮] 标记时原样保留(保住轮次边界),
      无标记的补上通用标记
    - 整段以 [系统注入|此前轮次执行记录] 开头,明确这是历史记录而非用户本轮
      原话,防止模型把过往内容当成最新指令(与内置侧 marker 约定一致)
    - 空输入返回空串(首轮无历史时不产生多余段落)

    本函数纯文本拼装,不做数据加载:messages 由执行器侧的跨轮记忆构造提供
    (内置 react_agent 与 CLI 共用同一实现,两侧历史内容因此不会分叉)。
    """
    lines: list[str] = []
    for msg in messages or []:
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        label = _CLI_HISTORY_ROLE_LABELS.get(msg.get("role") or "")
        if label:
            lines.append(f"{label}: {content}")
        elif content.startswith(SYSTEM_INJECT_MARKER):
            lines.append(content)
        else:
            lines.append(f"{SYSTEM_INJECT_MARKER}历史记录] {content}")
    if not lines:
        return ""

    return (
        "\n\n" + SYSTEM_INJECT_MARKER + "此前轮次执行记录]\n"
        "本会话是新开的,你看不到之前轮次的过程。以下是同一任务之前轮次的"
        "执行记录(按时间先后排列,含当时的用户要求、执行结论与评审反馈):\n\n"
        + "\n\n".join(lines)
        + "\n\n请把上述记录作为既有进度:不要重做已完成的部分,"
        "也不要把其中的过往要求当成本轮的新指令。"
    )


# ============================================================
# 失败重试续跑消息
# ============================================================


def build_retry_message(last_error: str) -> str:
    """失败重试的续跑消息(断点续跑语境,交 resume 链路执行)"""
    return (
        f"该任务上一次执行因错误中断: {last_error}\n"
        "这是一次失败重试(断点续跑),不是用户的新需求: "
        "请基于已有进度继续完成原任务,不要重做已完成的部分。"
    )


# ============================================================
# 循环/最大迭代兜底提示(注入 user 消息强制收尾)
# ============================================================

LOOP_BREAK_PROMPT = (
    "系统提示:你陷入了重复调用循环。"
    "请停止调用工具,用自然语言总结当前已确认的发现。"
)

MAX_ITERATION_PROMPT = (
    "系统提示:已达最大迭代次数。请用自然语言总结本轮审计的发现,"
    "包括已确认的漏洞、已检查的范围、未完成的检查项。"
)


def build_subtask_instruction(label: str, instruction: str) -> str:
    """构造单个子任务的只读核查指令(作为子任务子循环的独立 system 指令层,不落库不展示)。

    背景:按需派发时,每个子任务跑一条有界只读 ReAct 子循环。
    借鉴 codex 的 subagent_developer_instructions 将子 agent 的角色/约束作为独立
    指令层而非塞进 user 消息 —— 不污染真实用户问题,且便于集中管理/去重。

    instruction:主 agent 为该子任务自写的要求/上下文(取代旧的共享 focus 通道)。
    技能正文不再预注入 —— 子任务按需用 skill 工具自行读取(可见技能子集由
    react_agent 侧经 allowed_skills 设置)。
    本函数零 app.* 依赖(见 tests/test_prompts_purity.py),措辞工具中立。
    """
    return (
        f"[子任务:{(label or '').strip()}]\n"
        "本轮只针对该子任务深入核查,不要覆盖其他维度:\n"
        f"{(instruction or '').strip()[:SUBTASK_INSTRUCTION_MAX_CHARS]}\n"
        "技能正文未预置,若该子任务分到了可见技能,需要时先用 skill 工具按需读取"
        "再按其指引执行;\n"
        "只使用只读工具(list_files/read_file/search_code/find_files/"
        "run_semgrep/run_lint/git_*/query_cve/list_dependencies 等),"
        "定位到具体文件:行号后给出该子任务的完整结论(证据+修复建议)。"
    )


# instruction 入指令层前的字符上限:它是主 agent 为该子任务写的要求/上下文摘要,
# 过长会把每份子任务子循环的上下文都撑胖(工具定义侧同步告知模型该约束)
SUBTASK_INSTRUCTION_MAX_CHARS = 2000


# dispatch_review_subtasks 工具描述的模板(派发纪律写进工具本体,对标 Codex
# multi_agents_spec.rs 把纪律放在 spawn_agent 描述里)。{skill_names} 由调用侧
# 按当前可见技能名填(信息链路已有:list_skills 给 name+description,模型据此为
# 每个子任务挑可见技能子集)。纪律正文与 system prompt 里的"子任务按需派发"段
# 同一口径,两处改动要同步。
DISPATCH_SUBTASKS_TOOL_DESCRIPTION_TEMPLATE = (
    "按需派发并行只读审查子任务(每个子任务 = 一条独立只读核查子循环)。\n"
    "你为每个子任务自写 instruction(要求/上下文),可选地给它一个 skills 可见技能"
    "子集(只能是下方技能名的子集,可为空);技能正文不预注入,子任务按需用 skill "
    "工具自行读取。\n"
    "适合:全量/多维度代码审计(一次派发全部相关子任务)、多个独立维度的定向核查。\n"
    "不适合:单一具体问题(自己用 read_file/search_code 查)、纯对话或澄清"
    "(直接回答)、结论已在手(不重复派发)。\n"
    "先形成高层计划再派发:只把互不依赖的独立核查维度交给子任务,关键路径自己做。\n"
    "一次任务通常只派发 1 次;调用会等待全部子任务完成后返回各任务结论"
    "(含失败/超时状态),拿到后请集成与交叉验证,不要重跑已完成的子任务。\n"
    "可用技能名(skills 只能从这里面选,可为空):{skill_names}。"
)


# ============================================================
# 跨轮历史记忆:Level 2 LLM 压缩
# ============================================================

# LLM 压缩 prompt
HISTORY_COMPRESS_PROMPT = """你是审计历史压缩助手。以下是之前几轮「AI助手 执行 + 检查助手 后台审查」的对话记忆,
请压缩成一段简洁的摘要,必须保留:
- 每轮用户的原始要求/追问方向(用户当轮原话的要点)
- 每轮 react_agent 的关键发现(漏洞/问题/已确认的结论)
- agent2 标记的已覆盖维度(covered)和未覆盖维度(missing)
- agent2 审查指出的待改进方向与建议追问的检查项

丢弃冗余的工具调用细节、重复信息和无关叙述。输出纯文本摘要(不要 JSON,不要 markdown 标题),
按轮次顺序组织,每轮用"第 N 轮:"开头。

{old_hint}

[待压缩的历史记忆]
{history_text}
"""


def round_compact_text(rd: dict[str, Any]) -> str:
    """单轮压缩输入文本(Level 2 增量压缩的缓存单位)

    包含用户要求:用户原话是压缩摘要必须保留的信息(任务的原始指令
    与历次追问方向),丢失会导致早期轮次"为什么这么做"的语义断链。

    包含后台子任务落盘报告(rd["reports"],用 .get 兼容旧调用方构造的缺 key):
    那是"本轮总结当时没拿到"的结论,压缩后若不保留就彻底丢失。
    """
    segs = [f"=== 第 {rd['ridx']} 轮 ==="]
    if rd["question"]:
        segs.append(f"[用户要求]\n{rd['question']}")
    if rd["assistant_summary"]:
        segs.append(f"[执行总结]\n{rd['assistant_summary']}")
    reports = rd.get("reports") or ""
    if reports:
        segs.append(f"[后台子任务结论]\n{reports}")
    if rd["review"]:
        segs.append(f"[评审反馈]\n{rd['review']}")
    return "\n".join(segs)


def build_history_compress_segments(
    old_summary: str | None, new_segments: list[str],
) -> tuple[str, str]:
    """拼装 LLM 压缩的输入文本与增量提示

    返回 (history_text, old_hint):
    - old_summary 非空(增量压缩):history_text = 已有摘要 + 新增轮次,
      old_hint 引导 LLM 把两者合并成一段新摘要
    - old_summary 为空(首次压缩):history_text = 新增轮次拼接,old_hint 为空串
    """
    if old_summary:
        history_text = f"[已有摘要]\n{old_summary}\n\n[新增轮次]\n" + "\n\n".join(new_segments)
        old_hint = "已有摘要是之前压缩的结果,请把它和新增轮次合并成一段新的摘要。"
    else:
        history_text = "\n\n".join(new_segments)
        old_hint = ""
    return history_text, old_hint
