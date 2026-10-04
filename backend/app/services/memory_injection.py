"""记忆注入服务:把 UserPreference / UserMemory / Project.memory_content
拼成 prompt 段,供 agent2 / react_agent 注入 system_prompt。

无状态函数,每次调用现查 DB。token 受控(各段上限 2000 字符,超出尾部截断)。

注入策略:
- agent2:User Profile + 全局长期记忆(影响评判标准与审查维度)
- react_agent:分项目记忆(影响审计方向,优先检查已知问题)

user_id 为 None(匿名任务)或无配置 → 返回空串(不注入),保证匿名任务不受影响。
"""
import os

from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.user_memory import UserMemory
from app.models.user_preference import UserPreference
from app.services.repo_url import normalize_repo_url

# 各段字符上限(超出尾部截断 + 加截断标记)
MAX_PREF_CHARS = 2000
MAX_GLOBAL_MEM_CHARS = 2000
MAX_PROJECT_MEM_CHARS = 2000


def _truncate(text: str, max_chars: int) -> str:
    """超长截断,尾部加截断标记"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...truncated...]"


def build_agent2_memory_section(
    db: Session, user_id, repo_url: str | None = None,
) -> str:
    """构造 agent2 的"User Profile + 全局长期记忆 + 项目记忆精简版"段。

    user_id 为 None(匿名任务)或无任何配置 → 返回空串(不注入)。
    repo_url 非空时追加当前项目的记忆精简版(影响审查维度与评估覆盖度)。
    注入到 agent2 system prompt 末尾,影响评判标准与审查维度。

    agent2 不在沙箱,无法 read_file 查阅完整记忆,故只注入精简版
    (memory_summary;为空回退 memory_content 截断)。
    """
    if user_id is None:
        return ""

    parts: list[str] = []

    # User Profile(自由文本 user_profile,由用户在记忆管理页编辑后整段注入)
    pref = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == user_id)
        .first()
    )
    if pref and pref.user_profile and pref.user_profile.strip():
        parts.append(
            "## User Profile (follow when evaluating and defining review dimensions)\n"
            + _truncate(pref.user_profile.strip(), MAX_PREF_CHARS)
        )

    # 全局长期记忆(内容已含 ## 类别 + - 条目结构,用引导语作外层避免层级冲突)
    mem = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
    if mem and mem.content and mem.content.strip():
        parts.append(
            "The following is long-term memory accumulated across tasks, "
            "organized by category (follow when evaluating and defining review dimensions):\n"
            + _truncate(mem.content.strip(), MAX_GLOBAL_MEM_CHARS)
        )

    # 项目记忆精简版(影响审查维度与评估覆盖度;agent2 不在沙箱,只注入精简版)
    if repo_url:
        norm = normalize_repo_url(repo_url)
        if norm:
            proj = (
                db.query(Project)
                .filter(
                    Project.user_id == user_id,
                    Project.repo_url_normalized == norm,
                )
                .first()
            )
            if proj:
                summary = (proj.memory_summary or "").strip()
                if not summary:  # 兼容未生成 summary 的旧数据
                    summary = _truncate(
                        (proj.memory_content or "").strip(), MAX_PROJECT_MEM_CHARS
                    )
                if summary:
                    parts.append(
                        "The following is a summary of known issues and historical "
                        "memory for this project, organized by category "
                        "(reference when evaluating and defining review dimensions):\n"
                        + summary
                    )

    if not parts:
        return ""
    return "\n\n".join(parts)


def load_project_memory_brief(
    db: Session, user_id, repo_url: str | None,
) -> tuple[str, str | None]:
    """加载项目记忆数据(单源,无包装):返回 (记忆原文, 项目 alias)。

    记忆原文优先取精简版 memory_summary(LLM 生成,≤注入上限);
    为空时回退 memory_content 截断(兼容未生成 summary 的旧数据)。

    内置 react_agent(build_react_agent_memory_section,英文 header + alias
    + read_file 提示包装)与 CLI 侧(acp_base,裸文本 + 中文路径提示包装)
    共用此数据源,消除两侧各自查询导致的回退行为分叉。

    user_id 为 None / repo_url 为空 / 无对应 Project / 记忆为空 → ("", None)。
    """
    if user_id is None or not repo_url:
        return "", None

    norm = normalize_repo_url(repo_url)
    if not norm:
        return "", None

    proj = (
        db.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.repo_url_normalized == norm,
        )
        .first()
    )
    if not proj:
        return "", None

    # 优先用精简版(已 ≤ MAX_PROJECT_MEM_CHARS,无需截断);为空回退完整内容截断
    summary = (proj.memory_summary or "").strip()
    if summary:
        memory_text = summary
    else:
        memory_text = _truncate(
            (proj.memory_content or "").strip(), MAX_PROJECT_MEM_CHARS
        )
    return memory_text, proj.alias


def build_react_agent_memory_section(
    db: Session, user_id, repo_url: str | None,
) -> str:
    """构造 react_agent 的"分项目记忆"段。

    数据经 load_project_memory_brief 单源加载(summary 优先,memory_content
    截断回退);此处只做 react 侧包装(英文 header + alias)。
    末尾附完整记忆文件路径提示,引导 agent 用 read_file 查阅突破字数限制。

    user_id 为 None / repo_url 为空 / 无对应 Project / 记忆为空 → 返回空串。
    注入到 react_agent system prompt 末尾,影响审计方向(优先检查已知问题)。
    """
    memory_text, alias = load_project_memory_brief(db, user_id, repo_url)
    if not memory_text:
        return ""

    # 结构化记忆(含 ## 类别头)用"优先检查 Hard Constraints/Known Issues"引导;
    # freeform 散文无类别头,改用中性文案避免误导 agent 去找不存在的分类。
    if "## " in memory_text:
        header = (
            "The following is your known issues and historical memory for this project, "
            "organized by category. Prioritize the earlier-listed categories:"
        )
    else:
        header = (
            "The following is your known issues and historical memory for this project:"
        )
    if alias:
        header += f"\nProject alias: {alias}"
    # 完整记忆已写入沙箱文件,提示 agent 可 read_file 查阅突破字数限制
    memory_text += (
        "\n\nFull memory available via read_file "
        "/home/user/.agent_memory/project_memory.md"
    )
    return header + "\n" + memory_text


def build_global_memory_section(
    db: Session, user_id, *, show_pointer: bool = True,
) -> str:
    """构造全局长期记忆段(注入 react_agent / CLI 执行侧)。

    跨项目通用经验(Hard Constraints / Tech Stack / Lessons Learned 等),
    影响执行方式。执行侧(react_agent / CLI)在沙箱里干活,这类"怎么做"的知识
    直接影响执行正确性,故注入执行侧而非仅 agent2。

    user_id 为 None(匿名任务)或无全局记忆 → 返回空串(不注入)。
    截断到 MAX_GLOBAL_MEM_CHARS;被截断时附完整记忆文件路径提示(任务启动时
    已写入沙箱 global_memory.md),引导 agent 用 read_file 查阅全量。

    show_pointer=False 时不内联该路径提示(供外部 CLI 侧使用:其"查全量"指针由
    build_cli_memory_section 按运行模式统一产出,避免双指针与失效的 /home/user 路径)。
    """
    if user_id is None:
        return ""
    mem = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
    if not mem or not mem.content or not mem.content.strip():
        return ""
    content = mem.content.strip()
    truncated = _truncate(content, MAX_GLOBAL_MEM_CHARS)
    section = (
        "The following is general experience accumulated across tasks, "
        "organized by category (follow during execution):\n"
        + truncated
    )
    # 被截断时提示可 read_file 查全量(文件在任务启动时写入沙箱)
    # show_pointer=False:外部 CLI 执行器用各自 Read 工具、访问不到后端对
    # /home/user 虚拟路径的映射,指针改由 build_cli_memory_section 按模式产出(去双指针)
    if show_pointer and truncated != content:
        section += (
            "\n\nFull memory available via read_file "
            "/home/user/.agent_memory/global_memory.md"
        )
    return section


def resolve_agent_memory_file_path(mode: str, local_dir, filename: str) -> str:
    """计算执行智能体可真正打开的记忆文件绝对路径(按运行模式)。

    - sandbox 模式:/home/user/.agent_memory/<filename> —— 容器内真实路径,
      外部 CLI 在其中运行,可直接读取,与后端虚拟路径恰好一致
    - local 模式:<local_dir>/.agent_memory/<filename> —— 宿主机上无 /home/user;
      write_*_memory_file 即写此处,而外部 CLI(Qoder/codex/dsh)用各自的 Read 工具,
      访问不到后端 read_file 对 /home/user 虚拟路径的映射,故必须给真实绝对路径

    仅外部 CLI 执行器需要本函数(其"查全量"指针按此模式感知生成);内置 react_agent
    走后端 read_file,继续用 /home/user 虚拟路径即可。
    """
    from app.tools import sandbox_tools

    if str(mode) == "local" and local_dir:
        return os.path.join(str(local_dir), ".agent_memory", filename)
    return f"{sandbox_tools._MEMORY_DIR_SANDBOX}/{filename}"
