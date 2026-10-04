"""acp_base._build_prompt_message 单元测试(纯函数,不连 DB / 不连沙箱)。

覆盖 CLI 智能体 prompt 中项目记忆精简版 + 全局记忆的注入与拼接顺序,
以及"纯指令(落库展示)与记忆注入段(只进发送内容)"的拆分。
"""
from unittest.mock import MagicMock

from app.agents.acp_base import (
    _build_base_prompt,
    _build_memory_section,
    _build_prompt_message,
    _build_repo_context_section,
    _compose_send_text,
)
from app.prompts.executor import SYSTEM_INJECT_MARKER


def _mk_task(user_input="审计这个仓库的安全问题", params=None):
    """构造最小 Task mock:_build_prompt_message 只用到 user_input / params。"""
    t = MagicMock()
    t.user_input = user_input
    t.params = params if params is not None else {"repo_url": "https://github.com/a/b"}
    return t


def test_first_round_includes_project_memory_and_file_hint():
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="## Hard Constraints\n- rule A",
    )
    assert "[项目记忆摘要]" in msg
    assert "## Hard Constraints\n- rule A" in msg
    assert "/home/user/.agent_memory/project_memory.md" in msg


def test_first_round_includes_global_memory():
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        global_memory="The following is general experience accumulated across tasks, organized by category (follow during execution):\n## Tech Stack\n- PG",
    )
    assert "The following is general experience accumulated across tasks" in msg
    assert "## Tech Stack\n- PG" in msg


def test_project_memory_before_global_memory():
    """项目记忆(具体)在前,全局记忆(通用)在后。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM_MARKER",
        global_memory="GLOBAL_MEM_MARKER",
    )
    assert msg.index("PROJECT_MEM_MARKER") < msg.index("GLOBAL_MEM_MARKER")


def test_empty_memories_not_appended():
    """memory_summary / global_memory 为空 → 不追加对应段。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="", global_memory="",
    )
    assert "[项目记忆摘要]" not in msg
    assert "项目记忆" not in msg


def test_followup_round_also_includes_memories():
    """追问轮同样注入记忆(与 react_agent system prompt 每轮都在一致)。"""
    task = _mk_task()
    msg = _build_prompt_message(
        task, 2, "请重点检查 SQL 注入", None, "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM",
        global_memory="GLOBAL_MEM",
    )
    # 追问段标签为中性措辞,不暴露内部角色,也不绑定审计/审查等场景词
    assert "[本轮补充要求]" in msg
    assert "agent2" not in msg.split("[本轮补充要求]")[0]
    assert "请重点检查 SQL 注入" in msg
    assert "PROJECT_MEM" in msg
    assert "GLOBAL_MEM" in msg
    # 无沙箱会话(工作区不可探测)时不得声称"已 clone",避免误导跳过 clone
    assert "已 clone" not in msg


def test_previous_plan_and_memories_coexist():
    """跨轮 plan 提醒 + 记忆段可共存,互不干扰。"""
    task = _mk_task()
    previous_plan = [{"text": "检查鉴权", "status": "done"}]
    msg = _build_prompt_message(
        task, 2, "继续检查", None, "/home/user/repos/r", previous_plan,
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert "PROJECT_MEM" in msg
    assert "GLOBAL_MEM" in msg


# ---------- 纯指令 / 记忆段拆分(落库不含记忆注入段) ----------

def test_base_prompt_excludes_memories():
    """_build_base_prompt 只含纯指令,不含项目/全局记忆段(落库展示用)。"""
    task = _mk_task()
    base = _build_base_prompt(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
    )
    assert task.user_input in base
    assert "[项目记忆摘要]" not in base
    assert "general experience accumulated" not in base


def test_base_prompt_excludes_repo_context():
    """_build_base_prompt 不含预 clone 上下文段(落库展示用,前端不展示)。"""
    task = _mk_task(
        params={"repo_url": "https://github.com/a/b", "branch": "main"},
    )
    base = _build_base_prompt(
        task, 1, None, "仓库 xxx 已克隆到 /home/user/repos/r", "/home/user/repos/r", None,
    )
    assert "仓库已预先 clone" not in base
    assert "已克隆到" not in base
    # 仓库地址/分支属用户选择的元信息,仍保留展示
    assert "仓库地址: https://github.com/a/b" in base
    assert "分支: main" in base


def test_base_prompt_first_round_matches_create_task_record():
    """非上传任务:CLI 首轮 base 与 create_task 落库拼装逐字一致(幂等去重前提)。"""
    from app.prompts.executor import build_first_round_question

    task = _mk_task(params={"repo_url": "https://github.com/a/b", "branch": "main"})
    base = _build_base_prompt(task, 1, None, "[repo context]", "/home/user/repos/r", None)
    assert base == build_first_round_question(task.user_input, task.params)


def test_base_prompt_upload_task_includes_upload_line():
    """上传任务:CLI 首轮带上"用户上传的文件已放入任务工作区"行(与落库一致)。"""
    task = _mk_task(params={"upload_id": "u1"})
    base = _build_base_prompt(task, 1, None, "[repo context]", "/ws/uploaded_files", None)
    assert "用户上传的文件已放入任务工作区" in base


def test_base_prompt_repo_path_fallback_kept():
    """未预 clone 但有路径:保留"仓库路径"兜底行(委托共享拼装后行为不变)。"""
    task = _mk_task(params={"repo_url": "https://github.com/a/b"})
    base = _build_base_prompt(task, 1, None, None, "/home/user/repos/r", None)
    assert "仓库路径: /home/user/repos/r" in base


def test_followup_repo_task_keeps_repo_path(monkeypatch):
    """真实仓库 + 工作区有文件:追问轮保留"仓库路径(已 clone)"行。"""
    import app.agents.acp_base as ab
    monkeypatch.setattr(ab.sandbox_tools, "workspace_has_files", lambda *a, **k: True)
    task = _mk_task(params={"repo_url": "https://github.com/a/b"})
    msg = _build_base_prompt(
        task, 2, "教学逻辑怎么样?", None, "/home/user/repos/r", None,
    )
    assert "仓库路径(已 clone,无需再 clone): /home/user/repos/r" in msg
    assert "[本轮补充要求]\n教学逻辑怎么样?" in msg


def test_followup_upload_task_omits_repo_path(monkeypatch):
    """纯上传任务(无 repo_url):追问轮不拼接误导性的"仓库路径"行。"""
    import app.agents.acp_base as ab
    monkeypatch.setattr(ab.sandbox_tools, "workspace_has_files", lambda *a, **k: True)
    task = _mk_task(params={"upload_id": "u1"})
    msg = _build_base_prompt(
        task, 2, "教学逻辑怎么样?", None, "/ws/uploaded_files", None,
    )
    assert "仓库路径" not in msg
    assert "[本轮补充要求]\n教学逻辑怎么样?" in msg


def test_repo_context_section_roundtrip():
    """_build_repo_context_section:有内容时包裹提示语,空时返回空串。"""
    assert _build_repo_context_section(None) == ""
    assert _build_repo_context_section("") == ""
    section = _build_repo_context_section("仓库 xxx 已克隆到 /home/user/repos/r")
    assert "仓库已预先 clone" in section
    assert "已克隆到 /home/user/repos/r" in section


def test_repo_context_section_upload_variant():
    """上传任务:upload 变体直接陈述用户上传文件,不再称"已预先 clone"(修复矛盾 bug)。"""
    section = _build_repo_context_section(
        "用户上传的文件(a.zip,共 3 个)已放入 /ws/uploaded_files",
        variant="upload",
    )
    assert "[用户上传的文件已就绪]" in section
    assert "仓库已预先 clone" not in section
    assert "clone_repo" not in section


def test_prompt_message_upload_task_uses_upload_variant():
    """_build_prompt_message 按 task.params 自动选 upload 变体。"""
    task = _mk_task(
        params={"repo_url": "https://github.com/a/b", "upload_id": "u1"},
    )
    msg = _build_prompt_message(
        task, 1, None,
        "用户上传的文件(a.zip,共 3 个)已放入 /ws/uploaded_files",
        "/ws/uploaded_files", None,
    )
    assert "[用户上传的文件已就绪]" in msg
    assert "仓库已预先 clone" not in msg


def test_load_project_memory_summary_falls_back_to_content():
    """CLI 侧项目记忆:summary 为空回退 memory_content(与内置 agent 对齐的增强)。

    数据源已收敛到 memory_injection.load_project_memory_brief 单一实现,
    旧数据(未生成 summary)此前 CLI 不注入,现在注入截断的 memory_content。
    """
    from app.agents.acp_base import _load_project_memory_summary

    proj = MagicMock()
    proj.memory_content = "## Known Issues\n- issue B"
    proj.memory_summary = ""
    proj.alias = None
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = proj
    task = MagicMock()
    task.user_id = 1
    task.params = {"repo_url": "https://github.com/a/b"}
    task.id = "t1"
    assert _load_project_memory_summary(db, task) == "## Known Issues\n- issue B"


def test_memory_section_contains_both_and_global_file_hint():
    """_build_memory_section 含项目记忆 + 全局记忆 + 全局记忆文件路径提示。"""
    section = _build_memory_section(
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert "PROJECT_MEM" in section
    assert "GLOBAL_MEM" in section
    assert "/home/user/.agent_memory/global_memory.md" in section
    assert "/home/user/.agent_memory/project_memory.md" in section


def test_memory_section_empty_when_no_memories():
    """两部分都为空 → 记忆段为空串。"""
    assert _build_memory_section("", "") == ""
    assert _build_memory_section("   ", "") == ""


def test_prompt_message_equals_sections_plus_base():
    """发送装配 == 预 clone 上下文段 + 记忆段 + 纯指令(发送完整,落库纯净)。

    本轮指令放在末尾(模型对末尾指令最敏感),前面的注入段在同一 session
    内保持逐字不变,便于提示词前缀缓存命中。
    """
    task = _mk_task()
    base = _build_base_prompt(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
    )
    repo_section = _build_repo_context_section("[repo context]")
    section = _build_memory_section("PROJECT_MEM", "GLOBAL_MEM")
    msg = _build_prompt_message(
        task, 1, None, "[repo context]", "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM", global_memory="GLOBAL_MEM",
    )
    assert msg == repo_section + section + "\n\n" + base


def test_prompt_message_history_replay_goes_first():
    """跨轮历史回放排最前(属"过去"的内容),本轮指令仍在末尾且与注入段有分隔。"""
    from app.prompts.executor import build_cli_history_replay_section

    task = _mk_task()
    base = _build_base_prompt(
        task, 2, "继续检查", None, "/home/user/repos/r", None,
    )
    section = _build_memory_section("PROJECT_MEM", "")
    replay = build_cli_history_replay_section([
        {"role": "user", "content": "首轮原话"},
        {"role": "assistant", "content": "首轮结论"},
    ])
    msg = _build_prompt_message(
        task, 2, "继续检查", None, "/home/user/repos/r", None,
        memory_summary="PROJECT_MEM",
        history_replay=replay,
    )
    assert msg.lstrip("\n").startswith(f"{SYSTEM_INJECT_MARKER}此前轮次执行记录]")
    assert msg.index("首轮原话") < msg.index("PROJECT_MEM") < msg.index(base)
    assert msg.endswith(base)
    # 指令与前方注入段之间保留空行分隔(不能粘连成一词)
    assert section.rstrip("\n") + "\n\n" + base in msg


def test_compose_send_text_skips_empty_sections():
    """空段不产生多余分隔;拼接顺序为历史回放 + 仓库上下文 + 记忆 + 指令。"""
    assert _compose_send_text("BASE") == "BASE"
    assert _compose_send_text("BASE", "", "", "") == "BASE"
    assert _compose_send_text("", "", "", "REPLAY") == "REPLAY"
    assert _compose_send_text("") == ""
    # 稳定段在前,本轮指令在后;无前置换行的段之间补 \n\n
    assert (
        _compose_send_text("BASE", "REPO", "MEM", "REPLAY")
        == "REPLAY\n\nREPO\n\nMEM\n\nBASE"
    )
    # 段自身已以换行开头时不重复加分隔
    assert _compose_send_text("BASE", "\n\nREPO", "\n\nMEM") == "\n\nREPO\n\nMEM\n\nBASE"

