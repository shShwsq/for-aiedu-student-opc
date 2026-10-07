"""追问多文件上传单测(mock db / local 沙箱模式,不连真实 DB/沙箱 Server)

覆盖计划 §9 的追问链路:
- sandbox_tools 传输原语:_safe_dirname 清洗、add_uploads_to_workspace
  (不改 repo_path / 不清空既有内容 / 多文件各归子目录 / git exclude 幂等 /
  无工作根时按需建根)、transfer_uploads_to_workspace_root(创建多文件布局)
- 运行中追问 seam:push_user_message 携带 upload_ids → drain 取出;
  _format_injected_user_messages 拼接 attachment_note
- 完成后追问 seam:_restore_workspace_if_needed 重放 followup_upload_ids;
  路由 submit_task_message completed 分支把附件累积进 params 并透传给 resume
- 附件提示口径:路径取实际落位结果,传不成如实告知 + 落库 system/warning
  (旧行为:无仓库任务 repo_path 为空 → 抛错被吞 → 模型仍被告知文件已在工作区)
"""
import io
import uuid
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import app.agents.orchestrator as orchestrator
import app.models.task_artifact  # noqa: F401  单跑本文件时注册 TaskArtifact,Task mapper 才能初始化
import app.models.user_git_binding  # noqa: F401  单跑本文件时注册 UserGitBinding,User mapper 才能初始化
import app.routers.tasks as tasks_router
import app.tools.sandbox_tools as sandbox_tools
from app.prompts.executor import (
    ATTACHMENT_UNAVAILABLE_NOTE,
    format_followup_attachment_note,
    format_injected_user_messages as _format_injected_user_messages,
    format_resume_attachment_note,
)
from app.config import settings
from app.models.task import Task, TaskStatus
from app.schemas.task import SendMessageRequest
from app.services.uploads import save_upload
from app.tools.sandbox_tools import (
    UPLOAD_WORK_ROOT_NAME,
    _safe_dirname,
    add_uploads_to_workspace,
    transfer_uploads_to_workspace_root,
)
from app.user_messages import (
    clear_user_messages,
    drain_user_messages,
    has_pending_messages,
    push_user_message,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """按 {路径: 内容} 构造 zip 字节"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _uploads_dir(tmp_path, monkeypatch):
    """每个用例用独立上传目录,不污染真实数据目录"""
    monkeypatch.setattr(settings, "UPLOADS_DIR", str(tmp_path / "uploads_data"))


@pytest.fixture(autouse=True)
def _clean_sessions():
    """清空 sandbox_tools._sessions,避免跨用例串扰"""
    sandbox_tools._sessions.clear()
    yield
    sandbox_tools._sessions.clear()


def _mk_local_session(task_id: str, workspace: Path) -> dict:
    """预置一个 local 模式会话 ctx(绕过真实沙箱创建)

    local 模式下 _get_or_create_session 命中已存在的 ctx 即直接返回:探活只
    对 sandbox 模式生效(local 工作区是宿主临时目录,无容器可问),
    故无需真 session 对象参与传输。
    """
    workspace.mkdir(parents=True, exist_ok=True)
    ctx = {
        "session": MagicMock(),
        "mode": "local",
        "local_dir": str(workspace),
        "repo_path": str(workspace),
    }
    sandbox_tools._sessions[task_id] = ctx
    return ctx


# ============================================================
# _safe_dirname:文件名清洗为安全目录名
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("report.pdf", "report.pdf"),
    ("code.zip", "code.zip"),
    ("a/b/c.txt", "c.txt"),          # 带路径只取 basename
    ("../../evil", "evil"),          # 穿越被剥离
    ("hello world.zip", "hello_world.zip"),  # 空格 → _
    ("..", "upload"),                # 清洗后为空 → fallback
    ("", "upload"),
])
def test_safe_dirname(raw, expected):
    assert _safe_dirname(raw, fallback="upload") == expected


# ============================================================
# add_uploads_to_workspace:追问文件追加进现有工作区
# ============================================================

def test_add_uploads_preserves_repo_path_and_existing(tmp_path):
    """追问传输不重定向 repo_path、不清空既有 uploaded_files,多文件各归子目录"""
    task_id = "task-fu-1"
    workspace = tmp_path / "ws"
    ctx = _mk_local_session(task_id, workspace)
    # 既有工作区内容:创建时上传铺在 uploaded_files/
    existing = workspace / "uploaded_files"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("original")
    original_repo_path = ctx["repo_path"]

    u1 = save_upload(b"aaa", "report.pdf", "u1")["upload_id"]
    u2 = save_upload(_make_zip({"src/main.py": b"print(1)"}), "code.zip", "u1")["upload_id"]

    added = add_uploads_to_workspace(task_id, [u1, u2], "followup_uploads")

    # repo_path 未被重定向(agent 继续在原工作根作业)
    assert ctx["repo_path"] == original_repo_path
    # 既有内容未被清空
    assert (existing / "keep.txt").read_text() == "original"
    # 返回相对工作根的 posix 路径,各归 {i}-{清洗文件名}/ 子目录
    assert added == ["followup_uploads/0-report.pdf", "followup_uploads/1-code.zip"]
    # 文件确实落地到各自子目录
    assert (workspace / "followup_uploads" / "0-report.pdf" / "report.pdf").read_bytes() == b"aaa"
    assert (workspace / "followup_uploads" / "1-code.zip" / "src" / "main.py").read_text() == "print(1)"


def test_add_uploads_writes_git_exclude(tmp_path):
    """git 工作区:追问目录写入 .git/info/exclude,避免污染 diff"""
    task_id = "task-fu-git"
    workspace = tmp_path / "ws"
    _mk_local_session(task_id, workspace)
    (workspace / ".git").mkdir(parents=True)
    u1 = save_upload(b"x", "a.txt", "u1")["upload_id"]

    add_uploads_to_workspace(task_id, [u1], "followup_uploads")

    excl = workspace / ".git" / "info" / "exclude"
    assert excl.exists()
    assert "followup_uploads/" in excl.read_text(encoding="utf-8").splitlines()


def test_add_uploads_git_exclude_idempotent(tmp_path):
    """重复追问:exclude 行幂等(不重复追加)"""
    task_id = "task-fu-git2"
    workspace = tmp_path / "ws"
    _mk_local_session(task_id, workspace)
    (workspace / ".git").mkdir(parents=True)
    u1 = save_upload(b"x", "a.txt", "u1")["upload_id"]

    add_uploads_to_workspace(task_id, [u1], "followup_uploads")
    add_uploads_to_workspace(task_id, [u1], "followup_uploads")

    lines = (workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert lines.count("followup_uploads/") == 1


def test_add_uploads_non_git_workspace_skips_exclude(tmp_path):
    """非 git 工作区:不创建 .git,静默跳过 exclude"""
    task_id = "task-fu-nongit"
    workspace = tmp_path / "ws"
    _mk_local_session(task_id, workspace)
    u1 = save_upload(b"x", "a.txt", "u1")["upload_id"]

    added = add_uploads_to_workspace(task_id, [u1], "followup_uploads")

    assert added == ["followup_uploads/0-a.txt"]
    assert not (workspace / ".git").exists()


def test_add_uploads_empty_ids_returns_empty():
    """空 upload_ids → 直接返回空列表(不触碰会话)"""
    assert add_uploads_to_workspace("any-task", [], "followup_uploads") == []


def test_add_uploads_creates_local_root_when_repo_path_empty(tmp_path):
    """local 模式 + repo_path 为空(纯对话开场、无仓库无创建上传):按需建工作根

    旧行为是抛"工作区尚未就绪"让调用方吞掉 —— 用户的附件于是根本没进工作区,
    模型却照旧被告知"文件已在 followup_uploads/"。建根是这条链路的正解。
    """
    task_id = "task-fu-root-local"
    workspace = tmp_path / "ws"
    ctx = _mk_local_session(task_id, workspace)
    ctx["repo_path"] = ""
    u1 = save_upload(b"doc body", "lecture.pptx", "u1")["upload_id"]

    added = add_uploads_to_workspace(task_id, [u1], "followup_uploads")

    # 工作根 = {local_dir}/uploaded_files(与创建上传同一命名/同一口径)
    assert ctx["repo_path"] == str(workspace / UPLOAD_WORK_ROOT_NAME)
    assert added == ["followup_uploads/0-lecture.pptx"]
    assert (
        workspace / UPLOAD_WORK_ROOT_NAME / "followup_uploads" / "0-lecture.pptx"
        / "lecture.pptx"
    ).read_bytes() == b"doc body"


def test_add_uploads_creates_sandbox_root_on_demand(monkeypatch):
    """sandbox 模式:建 /home/user/repos/uploaded_files 根(mkdir -p 后 _set_repo_path)"""
    task_id = "task-fu-root-sandbox"
    session = MagicMock()
    ctx = {"session": session, "mode": "sandbox", "repo_path": ""}
    sandbox_tools._sessions[task_id] = ctx
    copied: list[str] = []
    # 沙箱 base64 传输链路由其它用例覆盖,这里只验建根与落点路径口径
    monkeypatch.setattr(
        sandbox_tools, "_copy_local_dir_into_workspace",
        lambda c, src_local, dest_path, clear_dest: copied.append(dest_path) or dest_path,
    )
    u1 = save_upload(b"x", "a.txt", "u1")["upload_id"]

    added = add_uploads_to_workspace(task_id, [u1], "followup_uploads")

    root = f"/home/user/repos/{UPLOAD_WORK_ROOT_NAME}"
    assert ctx["repo_path"] == root
    assert added == ["followup_uploads/0-a.txt"]
    assert copied == [f"{root}/followup_uploads/0-a.txt"]
    first_cmd = session.run_command.call_args_list[0].args[0]
    assert first_cmd.startswith(f"mkdir -p {root}")


def test_ensure_upload_work_root_keeps_later_clone_source_free():
    """建根不写 clone_source:之后真实 clone 同一仓库不会被当成已 clone 过而复用"""
    task_id = "task-fu-root-clone"
    session = MagicMock()
    ctx = {"session": session, "mode": "sandbox", "repo_path": ""}
    sandbox_tools._sessions[task_id] = ctx

    root = sandbox_tools._ensure_upload_work_root(ctx, task_id)

    assert root == f"/home/user/repos/{UPLOAD_WORK_ROOT_NAME}"
    assert "clone_source" not in sandbox_tools._sessions[task_id]
    # 幂等复用按 clone_source 判定:没 clone 过就绝不把上传根当仓库返回
    assert sandbox_tools._reuse_existing_clone(
        sandbox_tools._sessions[task_id], "https://github.com/a/b", None,
    ) is None


# ============================================================
# transfer_uploads_to_workspace_root:创建时多文件布局
# ============================================================

def test_transfer_uploads_root_layout(tmp_path):
    """创建多上传:工作根 = uploaded_files/,各上传进 {i}-{name}/ 子目录,repo_path 被设为根"""
    task_id = "task-create-multi"
    workspace = tmp_path / "ws"
    ctx = _mk_local_session(task_id, workspace)
    ctx["repo_path"] = ""  # 创建时尚未设

    u1 = save_upload(b"doc body", "doc.pdf", "u1")["upload_id"]
    u2 = save_upload(_make_zip({"a.py": b"1"}), "src.zip", "u1")["upload_id"]

    repo_path = transfer_uploads_to_workspace_root(task_id, [u1, u2])

    assert repo_path == str(workspace / "uploaded_files")
    assert ctx["repo_path"] == repo_path  # _set_repo_path 到工作根
    assert (Path(repo_path) / "0-doc.pdf" / "doc.pdf").read_bytes() == b"doc body"
    assert (Path(repo_path) / "1-src.zip" / "a.py").read_text() == "1"


def test_transfer_uploads_root_empty_ids_raises(tmp_path):
    """空 upload_ids → ValueError(创建流不允许空多上传)"""
    task_id = "task-create-empty"
    _mk_local_session(task_id, tmp_path / "ws")
    with pytest.raises(ValueError, match="为空"):
        transfer_uploads_to_workspace_root(task_id, [])


# ============================================================
# 运行中追问 seam:队列携带 upload_ids + 注入文本拼接
# ============================================================

def test_push_drain_carries_upload_ids():
    """push_user_message 携带 upload_ids → drain 原样取出"""
    tid = "task-queue-1"
    clear_user_messages(tid)
    try:
        push_user_message(
            tid, "看这个文件",
            message_id="m1", created_at="2024-01-01T00:00:00",
            upload_ids=["u1", "u2"],
        )
        msgs = drain_user_messages(tid)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "看这个文件"
        assert msgs[0]["upload_ids"] == ["u1", "u2"]
    finally:
        clear_user_messages(tid)


def test_push_without_upload_ids_defaults_empty():
    """未带 upload_ids → 队列元素 upload_ids 为空列表(而非 None)"""
    tid = "task-queue-2"
    clear_user_messages(tid)
    try:
        push_user_message(tid, "纯文字", message_id="m1", created_at="2024-01-01T00:00:00")
        msgs = drain_user_messages(tid)
        assert msgs[0]["upload_ids"] == []
    finally:
        clear_user_messages(tid)


def test_format_injected_appends_attachment_note():
    """注入文本末尾拼接 attachment_note(让模型感知新文件)"""
    note = "\n\n[用户本轮附带了新文件,已放入工作区 followup_uploads/ 目录]"
    text = _format_injected_user_messages(
        [{"content": "分析新文件", "upload_ids": ["u1"]}], attachment_note=note,
    )
    assert "分析新文件" in text
    assert text.endswith(note)


# ============================================================
# react_agent 循环出口守卫:最终答案期间到达的消息同轮继续处理
# ============================================================


def test_react_agent_exit_guard_continues_on_pending(monkeypatch):
    """窗口 1 修复:最终答案生成期间(本迭代 drain 之后)到达的用户消息
    → 循环不退出,下一迭代顶部 drain+注入,模型在同轮上下文里继续处理
    (而非遗留队列、被流结束清理静默丢弃)。"""
    import app.agents.react_agent as react_agent

    tid = "task-guard-1"
    task = MagicMock()
    task.id = tid
    task.params = {}
    task.user_id = None
    task.scenario = "general"
    task.user_input = "初始任务"
    db = MagicMock()
    # 幂等落库查询返回非 None → 跳过 question 重复落库
    db.query.return_value.filter.return_value.first.return_value = MagicMock()

    clear_user_messages(tid)
    llm_contexts: list[list] = []

    def _fake_stream(client, task, db, round_idx, iteration, messages, tools):
        llm_contexts.append(list(messages))
        if len(llm_contexts) == 1:
            # 第一次 LLM 调用期间:用户发来补充消息(drain 已过,留在队列)
            push_user_message(
                tid, "看这个新要求",
                message_id=str(uuid.uuid4()),
                created_at="2026-01-01T00:00:00",
            )
            return ("思考1", "第一个答案", [], "stop", "c1")
        return ("思考2", "第二个答案", [], "stop", "c2")

    monkeypatch.setattr(react_agent, "_stream_llm_response", _fake_stream)
    monkeypatch.setattr(react_agent, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "get_all_tools", lambda: [])
    monkeypatch.setattr(react_agent, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "perf_log", lambda *a, **k: None)
    published = []
    monkeypatch.setattr(
        react_agent, "publish",
        lambda t, etype, data=None: published.append((etype, data)),
    )
    monkeypatch.setattr(
        react_agent, "_add_conversation", lambda *a, **k: MagicMock(id="c"),
    )
    # drain 发布时按 message_id 回查 Conversation(mock 返回对应记录)
    drained_conv = MagicMock()
    drained_conv.id = "drained-conv-id"
    drained_conv.round_idx = 1
    drained_conv.role = "user"
    drained_conv.type = "message"
    drained_conv.content = "看这个新要求"
    drained_conv.reasoning = None
    drained_conv.attachments = None
    db.query.return_value.filter.return_value.all.return_value = [drained_conv]

    try:
        _results, summary, _plan = react_agent.run_react_agent(
            task, db, round_idx=1, followup_query=None,
            client=MagicMock(), repo_context=None, previous_plan=None,
        )

        # LLM 被调两次:第一次出最终答案 → 守卫发现有 pending → 继续;
        # 第二次处理用户消息后队列已空 → 正常退出
        assert len(llm_contexts) == 2
        # 第二次调用的上下文包含注入的用户消息(同轮继续,模型可见)
        injected = [
            m for m in llm_contexts[1]
            if isinstance(m, dict) and m.get("role") == "user"
            and "看这个新要求" in (m.get("content") or "")
        ]
        assert injected
        # 消费时刻补推 conversation 事件:消息在 agent 实际处理的位置入流
        # (发送时仅推 user_message_pending,由 API 端点负责)
        conv_events = [d for e, d in published if e == "conversation"]
        assert len(conv_events) == 1
        assert conv_events[0]["content"] == "看这个新要求"
        assert conv_events[0]["role"] == "user"
        # summary 为第二次的答案(含对用户消息的回应)
        assert summary == "第二个答案"
        # 队列已清空(无遗留)
        assert drain_user_messages(tid) == []
    finally:
        clear_user_messages(tid)


def test_react_agent_exit_guard_no_pending_exits(monkeypatch):
    """无遗留消息 → 守卫不干预,单次最终答案即正常退出。"""
    import app.agents.react_agent as react_agent

    tid = "task-guard-2"
    task = MagicMock()
    task.id = tid
    task.params = {}
    task.user_id = None
    task.scenario = "general"
    task.user_input = "初始任务"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()

    clear_user_messages(tid)
    calls = []

    def _fake_stream(client, task, db, round_idx, iteration, messages, tools):
        calls.append(iteration)
        return ("思考", "最终答案", [], "stop", "c1")

    monkeypatch.setattr(react_agent, "_stream_llm_response", _fake_stream)
    monkeypatch.setattr(react_agent, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "get_all_tools", lambda: [])
    monkeypatch.setattr(react_agent, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "publish", lambda *a, **k: None)
    monkeypatch.setattr(
        react_agent, "_add_conversation", lambda *a, **k: MagicMock(id="c"),
    )

    try:
        _results, summary, _plan = react_agent.run_react_agent(
            task, db, round_idx=1, followup_query=None,
            client=MagicMock(), repo_context=None, previous_plan=None,
        )
        assert calls == [1]  # 单次调用即退出(守卫不干预)
        assert summary == "最终答案"
    finally:
        clear_user_messages(tid)


def test_format_injected_without_note_has_no_followup_hint():
    """无附件时注入文本不含 followup 提示"""
    text = _format_injected_user_messages([{"content": "纯文字追问"}])
    assert "纯文字追问" in text
    assert "followup_uploads" not in text


def test_format_injected_empty_content_returns_empty():
    """content 全为空 → 返回空串(无可注入内容)"""
    assert _format_injected_user_messages([{"content": "   "}]) == ""


# ============================================================
# 运行中追问的附件落地:react_agent drain 段
# ============================================================

def _mk_react_env(monkeypatch, tid: str):
    """构造跑一回合 run_react_agent 的最小替身,返回 (task, db, llm_contexts)

    llm_contexts 收集每次 LLM 调用看到的 messages(包含 drain 注入的那条),
    据此断言附件提示到底注入了什么。
    """
    import app.agents.react_agent as react_agent

    task = MagicMock()
    task.id = tid
    task.params = {}
    task.user_id = None
    task.scenario = "general"
    task.user_input = "hi"
    db = MagicMock()
    # 幂等落库查询返回非 None → 跳过 question 重复落库
    db.query.return_value.filter.return_value.first.return_value = MagicMock()

    llm_contexts: list[list] = []

    def _fake_stream(client, task, db, round_idx, iteration, messages, tools):
        llm_contexts.append(list(messages))
        return ("思考", "答案", [], "stop", "c1")

    monkeypatch.setattr(react_agent, "_stream_llm_response", _fake_stream)
    monkeypatch.setattr(react_agent, "set_current_task", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "get_all_tools", lambda: [])
    monkeypatch.setattr(react_agent, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(react_agent, "publish", lambda *a, **k: None)
    drained_conv = MagicMock()
    drained_conv.id = "drained-conv-id"
    drained_conv.round_idx = 1
    drained_conv.role = "user"
    drained_conv.type = "message"
    drained_conv.content = "这是什么文件"
    drained_conv.reasoning = None
    drained_conv.attachments = None
    db.query.return_value.filter.return_value.all.return_value = [drained_conv]
    clear_user_messages(tid)
    return task, db, llm_contexts


def _injected_user_text(llm_contexts: list[list]) -> str:
    """取首次 LLM 调用里注入的那条用户补充消息正文"""
    for m in llm_contexts[0]:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        if "这是什么文件" in (m.get("content") or ""):
            return m["content"]
    return ""


def test_drain_attachment_success_injects_actual_paths(monkeypatch):
    """运行中追问附件传成功:提示里是实际落位路径 + 工作根,不是写死的目录名"""
    tid = "task-drain-ok"
    task, db, llm_contexts = _mk_react_env(monkeypatch, tid)
    monkeypatch.setattr(
        sandbox_tools, "add_uploads_to_workspace",
        lambda tids, ids, subdir: [f"{subdir}/0-a.txt"],
    )
    monkeypatch.setattr(
        sandbox_tools, "get_workspace_info",
        lambda tids: {"repo_path": "/home/user/repos/uploaded_files"},
    )
    push_user_message(
        tid, "这是什么文件",
        message_id=str(uuid.uuid4()), created_at="2026-01-01T00:00:00",
        upload_ids=["u1"],
    )
    try:
        import app.agents.react_agent as react_agent

        react_agent.run_react_agent(
            task, db, round_idx=1, followup_query=None,
            client=MagicMock(), repo_context=None, previous_plan=None,
        )

        injected = _injected_user_text(llm_contexts)
        assert "followup_uploads/0-a.txt" in injected
        assert "/home/user/repos/uploaded_files" in injected
        assert ATTACHMENT_UNAVAILABLE_NOTE not in injected
        # 累积列表先落库再传输:params 是重放/回退浏览的唯一真源
        assert task.params["followup_upload_ids"] == ["u1"]
    finally:
        clear_user_messages(tid)


def test_drain_attachment_failure_is_visible(monkeypatch):
    """运行中追问附件传不成:如实告知模型(防臆测)+ 落库 system/warning

    旧行为是 catch+log 后什么也不说,模型对着不存在的文件编答案。
    """
    tid = "task-drain-fail"
    task, db, llm_contexts = _mk_react_env(monkeypatch, tid)

    def _boom(*a, **k):
        raise RuntimeError("沙箱不可用")

    monkeypatch.setattr(sandbox_tools, "add_uploads_to_workspace", _boom)
    import app.agents.react_agent as react_agent

    warnings: list[dict] = []

    def _record(_db, _task, **kw):
        if kw.get("type") == "warning":
            warnings.append(kw)
        return MagicMock(id="c")

    monkeypatch.setattr(react_agent, "_add_conversation", _record)
    push_user_message(
        tid, "这是什么文件",
        message_id=str(uuid.uuid4()), created_at="2026-01-01T00:00:00",
        upload_ids=["u1"],
    )
    try:
        react_agent.run_react_agent(
            task, db, round_idx=1, followup_query=None,
            client=MagicMock(), repo_context=None, previous_plan=None,
        )

        injected = _injected_user_text(llm_contexts)
        # 文字消息照常规注入,但附件事实说清楚
        assert "这是什么文件" in injected
        assert ATTACHMENT_UNAVAILABLE_NOTE in injected
        assert "followup_uploads" not in injected
        assert len(warnings) == 1
        assert warnings[0]["role"] == "system"
        assert warnings[0]["round_idx"] == 1
        assert "沙箱不可用" in warnings[0]["content"]
    finally:
        clear_user_messages(tid)


# ============================================================
# 完成后追问 seam:恢复工作区时重放 followup_upload_ids
# ============================================================

def test_restore_replays_followup_uploads(monkeypatch):
    """会话回收 + params 有 followup_upload_ids → 恢复时重放到 followup_uploads/"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _t: None)
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context", MagicMock(return_value=(None, "")),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info", lambda _tid: None,
    )
    add_calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "add_uploads_to_workspace",
        lambda tid, ids, subdir: add_calls.append((tid, ids, subdir)),
    )
    task = MagicMock()
    task.id = "task-restore-fu"
    task.params = {
        "repo_url": "https://github.com/a/b",
        "followup_upload_ids": ["fu1", "fu2"],
    }
    task.current_stage = ""

    restored = orchestrator._restore_workspace_if_needed(
        task, MagicMock(), "task-restore-fu", {},
    )

    assert restored is True
    assert add_calls == [("task-restore-fu", ["fu1", "fu2"], "followup_uploads")]


def test_restore_no_followup_skips_add(monkeypatch):
    """会话回收但无 followup_upload_ids → 不调 add_uploads_to_workspace"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _t: None)
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context", MagicMock(return_value=(None, "")),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info", lambda _tid: None,
    )
    add_calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "add_uploads_to_workspace",
        lambda tid, ids, subdir: add_calls.append((tid, ids, subdir)),
    )
    task = MagicMock()
    task.id = "task-restore-nofu"
    task.params = {"upload_ids": ["c1"]}  # 仅创建上传,无追问上传
    task.current_stage = ""

    restored = orchestrator._restore_workspace_if_needed(
        task, MagicMock(), "task-restore-nofu", {},
    )

    assert restored is True
    assert add_calls == []


def test_followup_upload_ids_helper():
    """_followup_upload_ids 读 params.followup_upload_ids 并过滤空值"""
    assert orchestrator._followup_upload_ids(
        {"followup_upload_ids": ["f1", "f2"]}
    ) == ["f1", "f2"]
    assert orchestrator._followup_upload_ids({"followup_upload_ids": ["", "f1"]}) == ["f1"]
    assert orchestrator._followup_upload_ids({}) == []
    assert orchestrator._followup_upload_ids(None) == []


# ============================================================
# 附件提示口径:路径按实际落位,传不成如实告知
# ============================================================

def test_attachment_note_lists_actual_paths_and_work_root():
    """提示里是实际传成功的相对路径 + 工作根绝对路径(不写死目录名)"""
    note = format_followup_attachment_note(
        ["followup_uploads/0-report.pdf"], "/home/user/repos/uploaded_files",
    )
    assert "用户本轮附带了新文件" in note
    assert "followup_uploads/0-report.pdf" in note
    assert "/home/user/repos/uploaded_files" in note


def test_attachment_note_empty_paths_returns_empty():
    """一个也没落地 → 空串(没传成就不该说"已放入工作区")"""
    assert format_followup_attachment_note([], "/home/user/repos/x") == ""
    assert format_resume_attachment_note([], "") == ""


def test_attachment_note_truncates_long_lists():
    """附件超上限只列前 20 个,末尾报总数"""
    paths = [f"followup_uploads/{i}-f.txt" for i in range(25)]
    note = format_resume_attachment_note(paths, "")
    assert "共 25 个" in note
    assert "followup_uploads/24-f.txt" not in note


def test_followup_attachment_note_uses_transferred_paths(monkeypatch):
    """resume 提示:传成功后取实际落位路径 + 工作根"""
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "add_uploads_to_workspace",
        lambda tid, ids, subdir: [f"{subdir}/{i}-x" for i in range(len(ids))],
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/home/user/repos/uploaded_files"},
    )
    task = MagicMock()
    task.params = {"followup_upload_ids": ["u1", "u2"]}

    note = orchestrator._followup_attachment_note(
        task, MagicMock(), "t", ["u2"], restored=False, round_idx=3,
    )

    assert "followup_uploads/0-x" in note
    assert "followup_uploads/1-x" in note
    assert "/home/user/repos/uploaded_files" in note
    assert note != ATTACHMENT_UNAVAILABLE_NOTE


def test_followup_attachment_note_restored_skips_retransfer(monkeypatch):
    """restored=True:恢复已重放,本轮不再传一次,路径按同一布局算出"""
    calls: list[tuple] = []
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "add_uploads_to_workspace",
        lambda *a, **k: calls.append(a),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info",
        lambda tid: {"repo_path": "/home/user/repos/repo"},
    )
    task = MagicMock()
    task.params = {"repo_url": "https://github.com/a/b", "followup_upload_ids": ["fu1"]}

    note = orchestrator._followup_attachment_note(
        task, MagicMock(), "t", ["fu1"], restored=True, round_idx=2,
    )

    assert calls == []
    # meta 不存在(未真上传)→ 槽位名回退 uid 前缀,与传输侧口径一致
    assert "followup_uploads/0-fu1" in note


def test_followup_attachment_note_failure_is_honest(monkeypatch):
    """传不成:如实告知模型(防臆测)+ 落库一条 system/warning(不再静默)"""
    def _boom(*a, **k):
        raise RuntimeError("沙箱命令失败")

    monkeypatch.setattr(orchestrator.sandbox_tools, "add_uploads_to_workspace", _boom)
    recorded: list[dict] = []
    monkeypatch.setattr(
        orchestrator, "_add_conversation",
        lambda db, task, **kw: recorded.append(kw) or MagicMock(id="c"),
    )
    task = MagicMock()
    task.params = {"followup_upload_ids": ["u1"]}

    note = orchestrator._followup_attachment_note(
        task, MagicMock(), "t", ["u1"], restored=False, round_idx=4,
    )

    assert note == ATTACHMENT_UNAVAILABLE_NOTE
    assert len(recorded) == 1
    assert recorded[0]["role"] == "system"
    assert recorded[0]["type"] == "warning"
    assert recorded[0]["round_idx"] == 4
    assert "沙箱命令失败" in recorded[0]["content"]


def test_restore_followup_replay_failure_warns_with_round(monkeypatch):
    """恢复重放失败不阻断恢复(仍回 True),但传了 round_idx 就要落库告警"""
    monkeypatch.setattr(orchestrator, "_publish_status", lambda _t: None)
    monkeypatch.setattr(
        orchestrator, "_prepare_repo_context", MagicMock(return_value=(None, "")),
    )
    monkeypatch.setattr(
        orchestrator.sandbox_tools, "get_workspace_info", lambda _tid: None,
    )

    def _boom(*a, **k):
        raise RuntimeError("沙箱不可用")

    monkeypatch.setattr(orchestrator.sandbox_tools, "add_uploads_to_workspace", _boom)
    recorded: list[dict] = []
    monkeypatch.setattr(
        orchestrator, "_add_conversation",
        lambda db, task, **kw: recorded.append(kw) or MagicMock(id="c"),
    )
    task = MagicMock()
    task.id = "task-restore-fu-warn"
    task.params = {
        "repo_url": "https://github.com/a/b",
        "followup_upload_ids": ["fu1"],
    }
    task.current_stage = ""

    restored = orchestrator._restore_workspace_if_needed(
        task, MagicMock(), "task-restore-fu-warn", {}, round_idx=7,
    )

    assert restored is True
    assert recorded and recorded[0]["round_idx"] == 7
    assert recorded[0]["type"] == "warning"


# ============================================================
# 路由 completed 分支:附件累积进 params + 透传给 resume
# ============================================================

def _mk_completed_task(params=None):
    task = MagicMock()
    task.id = uuid.uuid4()
    task.user_id = None  # 匿名任务:跳过归属校验分支
    task.status = TaskStatus.COMPLETED
    task.params = params if params is not None else {}
    return task


def _patch_router(monkeypatch, max_files=10):
    """屏蔽路由副作用,记录 launch_resume_thread 调用"""
    monkeypatch.setattr(settings, "UPLOAD_MAX_FILES_PER_MESSAGE", max_files)
    monkeypatch.setattr(tasks_router, "perf_log", lambda *a, **k: None)
    monkeypatch.setattr(tasks_router, "publish", lambda *a, **k: None)
    monkeypatch.setattr(tasks_router, "reset_task_bus", lambda *a, **k: None)
    # 上一轮已收尾(mock task 无总线记录 → is_task_finished=True → 走重置+启动)
    monkeypatch.setattr(tasks_router, "is_task_finished", lambda tid: True)
    monkeypatch.setattr(
        tasks_router, "validate_upload_for_task",
        lambda uid, user_id: {
            "upload_id": uid, "filename": f"{uid}.txt", "size": 10, "kind": "file",
        },
    )
    launch_calls: list[tuple] = []
    monkeypatch.setattr(
        tasks_router, "launch_resume_thread",
        lambda tid, content, upload_ids=None: launch_calls.append((tid, content, upload_ids)),
    )
    return launch_calls


def _mk_db(task):
    db = MagicMock()
    db.get.return_value = task
    # latest_conv 查询返回 None(无历史对话)→ msg_round_idx = 1
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    return db


def test_submit_message_completed_accumulates_and_launches(monkeypatch):
    """completed 追问带附件:累积进 params.followup_upload_ids 并透传给 resume 线程"""
    launch_calls = _patch_router(monkeypatch)
    task = _mk_completed_task(params={"repo_url": "https://github.com/a/b"})
    req = SendMessageRequest(content="再看这个", upload_ids=["u1", "u2"])

    resp = tasks_router.submit_task_message(task.id, req, _mk_db(task), None)

    assert resp.accepted is True
    # 附件累积进 params(整体重赋值,非 JSONB 突变)
    assert task.params["followup_upload_ids"] == ["u1", "u2"]
    # 透传给 resume 线程
    assert launch_calls == [(str(task.id), "再看这个", ["u1", "u2"])]


def test_submit_message_completed_dedups_upload_ids(monkeypatch):
    """completed 追问附件去重保序"""
    _patch_router(monkeypatch)
    task = _mk_completed_task(params={"followup_upload_ids": ["u0"]})
    req = SendMessageRequest(content="继续", upload_ids=["u1", "u1", "u2"])

    tasks_router.submit_task_message(task.id, req, _mk_db(task), None)

    # 既有 u0 保留,新 id 去重追加
    assert task.params["followup_upload_ids"] == ["u0", "u1", "u2"]


def test_submit_message_completed_exceeds_limit_422(monkeypatch):
    """completed 追问附件超上限 → 422,不启动 resume"""
    launch_calls = _patch_router(monkeypatch, max_files=1)
    task = _mk_completed_task()
    req = SendMessageRequest(content="超量", upload_ids=["u1", "u2"])

    with pytest.raises(HTTPException) as ei:
        tasks_router.submit_task_message(task.id, req, _mk_db(task), None)

    assert ei.value.status_code == 422
    assert launch_calls == []


def test_submit_message_completed_no_attachments_keeps_params(monkeypatch):
    """completed 追问无附件:params 不新增 followup_upload_ids,resume upload_ids=None"""
    launch_calls = _patch_router(monkeypatch)
    task = _mk_completed_task(params={"repo_url": "https://github.com/a/b"})
    req = SendMessageRequest(content="纯文字追问")

    tasks_router.submit_task_message(task.id, req, _mk_db(task), None)

    assert "followup_upload_ids" not in task.params
    assert launch_calls == [(str(task.id), "纯文字追问", None)]


# ============================================================
# 撤回待处理消息(DELETE /tasks/{id}/messages/{message_id})
# ============================================================


def _mk_running_task():
    task = MagicMock()
    task.id = uuid.uuid4()
    task.user_id = None  # 匿名任务:跳过归属校验分支
    task.status = TaskStatus.RUNNING
    task.params = {}
    return task


def test_withdraw_pending_message_succeeds(monkeypatch):
    """撤回仍在队列的消息:移除 + 删 Conversation + 推 user_message_withdrawn 事件。"""
    task = _mk_running_task()
    db = MagicMock()
    db.get.return_value = task
    published = []
    monkeypatch.setattr(
        tasks_router, "publish",
        lambda tid, etype, data=None: published.append((etype, data)),
    )

    msg_id = str(uuid.uuid4())
    push_user_message(task.id, "待撤回的消息", message_id=msg_id, created_at="t")
    try:
        resp = tasks_router.withdraw_task_message(task.id, uuid.UUID(msg_id), db, None)

        assert resp.success is True
        assert not has_pending_messages(task.id)  # 已出队
        assert any(e == "user_message_withdrawn" for e, _ in published)
        # Conversation 记录被删除(刷新快照/对话流不再显示)
        db.query.return_value.filter.return_value.delete.assert_called_once()
    finally:
        clear_user_messages(task.id)


def test_withdraw_consumed_message_rejected(monkeypatch):
    """消息已被消费(不在队列但记录存在)→ 拒绝:已进入 agent 上下文。"""
    task = _mk_running_task()
    conv = MagicMock()
    db = MagicMock()
    db.get.side_effect = lambda cls, mid, *a, **k: task if cls is Task else conv
    monkeypatch.setattr(tasks_router, "publish", lambda *a, **k: None)

    resp = tasks_router.withdraw_task_message(task.id, uuid.uuid4(), db, None)

    assert resp.success is False
    assert "无法撤回" in resp.message


def test_withdraw_missing_message_404(monkeypatch):
    """记录不存在(从未发送过)→ 404。"""
    task = _mk_running_task()
    db = MagicMock()
    db.get.side_effect = lambda cls, mid, *a, **k: task if cls is Task else None
    monkeypatch.setattr(tasks_router, "publish", lambda *a, **k: None)

    with pytest.raises(HTTPException) as ei:
        tasks_router.withdraw_task_message(task.id, uuid.uuid4(), db, None)
    assert ei.value.status_code == 404


def test_withdraw_non_running_task_rejected(monkeypatch):
    """非 running/paused 状态(如 completed)→ 拒绝:无待处理消息。"""
    task = _mk_running_task()
    task.status = TaskStatus.COMPLETED
    db = MagicMock()
    db.get.return_value = task
    monkeypatch.setattr(tasks_router, "publish", lambda *a, **k: None)

    resp = tasks_router.withdraw_task_message(task.id, uuid.uuid4(), db, None)

    assert resp.success is False
