"""执行链 prompt 共享资产的契约与锚点测试(app/prompts/executor.py)。

覆盖:
- build_first_round_question golden 快照(护 create_task 落库幂等去重:
  create_task 与 CLI 首轮共用此拼装,字节漂移会造成落库/展示不同步;
  上传任务不再附"用户上传的文件已放入任务工作区"行,该信息改由 upload
  变体 repo_context 段承载)
- 上传场景文案直白化锚点(wrapper / header):
  不用"交付物"内部术语、不加"无需 clone"否定指令、直接陈述文件位置
- followup 核心指引文本 react/cli 两侧共享
- plan reminder 双 variant 的前缀锚点(react 循环内按前缀查找替换)
"""
from unittest.mock import MagicMock

from app.prompts.executor import (
    FOLLOWUP_CORE_GUIDANCE,
    FOLLOWUP_GUIDANCE,
    build_first_round_question,
    build_repo_context_section,
    build_upload_header,
    format_plan_reminder,
)


# ============================================================
# build_first_round_question:golden 快照(护落库幂等)
# ============================================================

def test_golden_basic_repo():
    """基础仓库任务:user_input + 仓库地址 + 分支,逐字固定。"""
    q = build_first_round_question(
        "审计这个仓库的安全问题",
        {"repo_url": "https://github.com/a/b", "branch": "main"},
    )
    assert q == (
        "审计这个仓库的安全问题\n"
        "仓库地址: https://github.com/a/b\n"
        "分支: main"
    )


def test_golden_upload():
    """上传任务:落库/展示提问保持纯净,不带"用户上传的文件已放入任务工作区"行。"""
    q = build_first_round_question("审查这份合同", {"upload_ids": ["u1", "u2"]})
    assert q == "审查这份合同"
    assert "用户上传的文件已放入任务工作区" not in q


def test_golden_legacy_single_upload_id():
    """legacy 单数 upload_id 同样不附上传行(落库提问与无参时一致)。"""
    q = build_first_round_question("审查合同", {"upload_id": "u1"})
    assert q == "审查合同"
    assert "用户上传的文件已放入任务工作区" not in q


def test_golden_no_params():
    """无参数:仅 user_input 原文。"""
    assert build_first_round_question("直接分析", None) == "直接分析"


# ============================================================
# 上传场景文案直白化锚点
# ============================================================

def test_upload_wrapper_direct_wording():
    """upload 变体 wrapper:陈述文件位置,无"交付物"、无 clone 提示。"""
    section = build_repo_context_section(
        "用户上传的文件(a.zip,共 3 个)已放入 /ws/uploaded_files",
        variant="upload",
    )
    assert section.startswith("\n\n[用户上传的文件已就绪]\n")
    assert "交付物" not in section
    assert "clone" not in section


def test_clone_wrapper_keeps_clone_hint():
    """clone 变体保留"已预先 clone"提示(仅 clone 场景才提 clone)。"""
    section = build_repo_context_section("仓库 xxx 已克隆到 /r", variant="clone")
    assert "[仓库已预先 clone,无需你再调用 clone_repo]" in section
    assert "不要再调用 clone_repo" in section


def test_upload_header_single():
    """单上传 header:文件名 + 位置 + 数量,不提 clone。"""
    header = build_upload_header(
        [{"filename": "proj.zip", "file_count": 12}], "/ws/uploaded_files",
    )
    assert header == (
        "用户上传的文件(proj.zip,共 12 个)"
        "已放入 /ws/uploaded_files,可直接开始处理"
    )


def test_upload_header_multi():
    """多上传 header:组数 + 各文件名 + 独立子目录,不提 clone。"""
    header = build_upload_header(
        [{"filename": "a.zip", "file_count": 2}, {"filename": "b.zip", "file_count": 3}],
        "/ws/uploaded_files",
    )
    assert header == (
        "用户上传的 2 组文件(a.zip、b.zip)"
        "已分别放入 /ws/uploaded_files 下的独立子目录,可直接开始处理"
    )


# ============================================================
# followup 核心指引共享
# ============================================================

def test_followup_core_included_in_react_guidance():
    """react 侧编排指引包含共享核心文本。"""
    assert FOLLOWUP_CORE_GUIDANCE in FOLLOWUP_GUIDANCE


def test_followup_core_shared_by_acp_base():
    """acp 侧追问轮 prompt 拼入同一核心文本(两侧行为同源)。"""
    from app.agents.acp_base import _build_base_prompt

    task = MagicMock()
    task.user_input = "审计"
    task.params = {"repo_url": "https://github.com/a/b"}
    task.id = "t1"
    msg = _build_base_prompt(
        task, 2, "请重点检查 SQL 注入", None, "/home/user/repos/r", None,
    )
    assert FOLLOWUP_CORE_GUIDANCE in msg


# ============================================================
# plan reminder 双 variant 锚点
# ============================================================

_STEPS = [
    {"id": 1, "text": "步骤一", "status": "done"},
    {"id": 2, "text": "步骤二", "status": "pending"},
]


def test_plan_reminder_react_variant_shape():
    """react 变体:强 <plan> 回写末行 + ○/◌/✓ 步骤行。"""
    reminder = format_plan_reminder(_STEPS)
    assert reminder.startswith("[系统提醒] 当前计划清单状态")
    assert "✓ [done] 步骤一" in reminder
    assert "○ [pending] 步骤二" in reminder
    assert "<plan>" in reminder  # 末行回写指令


def test_plan_reminder_cli_variant_neutral():
    """cli 变体:中立措辞(TodoList 兼容),无 react 专属末行。"""
    reminder = format_plan_reminder(_STEPS, variant="cli")
    assert reminder.startswith("[系统提醒] 当前计划清单状态")
    assert "TodoList" in reminder
    assert "如果某个步骤状态有变化" not in reminder  # 无 react 末行
    # 步骤行与 react 变体同源
    assert "✓ [done] 步骤一" in reminder


def test_plan_reminder_prefix_is_replacement_anchor():
    """两变体首行前缀一致:react 循环内按此前缀查找替换(锚点不可漂移)。"""
    react_head = format_plan_reminder(_STEPS).splitlines()[0]
    cli_head = format_plan_reminder(_STEPS, variant="cli").splitlines()[0]
    assert react_head.startswith("[系统提醒] 当前计划清单状态")
    assert cli_head.startswith("[系统提醒] 当前计划清单状态")
