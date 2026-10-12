"""后台子任务结论落盘:目录/注入段/回捞成 artifact(纯单测,不连 DB / 不连沙箱)。

覆盖:
- _async_agent_reports_dir:sandbox / local 两种路径,local 缺 local_dir → 空串
- prompts.executor.build_cli_subagent_reports_section:空目录不产段;文案含"仓库外落盘"要点
- _context_sections_hash 新增 subagent 桶(旧调用两参兼容)
- _resolve_subagent_injection:旧记录缺该桶按"未送达"补发一次(与 repo/memory 相反)
- _compose_send_text:落盘段位于记忆段之后、本轮指令之前
- _collect_async_agent_reports:回捞成功/每轮先删后写/截断/开关关闭/命令异常
  /非法文件名过滤,且异常绝不上抛
"""
from types import SimpleNamespace

import pytest

# 注册全部 mapper(否则实例化任一模型会因关系目标未解析而报错,
# 与 tests/test_git_binding_relationship.py 同法)
import app.models.email_token  # noqa: F401
import app.models.project  # noqa: F401
import app.models.task  # noqa: F401
import app.models.task_artifact  # noqa: F401
import app.models.user  # noqa: F401
import app.models.user_agent_config  # noqa: F401
import app.models.user_git_binding  # noqa: F401
import app.models.user_llm_config  # noqa: F401
import app.models.user_memory  # noqa: F401
import app.models.user_preference  # noqa: F401
from app.agents import acp_base as A
from app.config import settings
from app.models.task_artifact import TaskArtifact
from app.prompts.executor import build_cli_subagent_reports_section

TASK = SimpleNamespace(id="11111111-2222-3333-4444-555555555555")


# ============================================================
# 落盘目录
# ============================================================

def test_reports_dir_sandbox_and_local():
    assert A._async_agent_reports_dir(TASK.id, 3, "sandbox", None) == (
        "/home/user/.agent_reports/11111111/r3"
    )
    assert A._async_agent_reports_dir(TASK.id, 1, "local", "/tmp/njwjx-work") == (
        "/tmp/njwjx-work/.agent_reports/11111111/r1"
    )


def test_reports_dir_local_without_local_dir_is_empty():
    """local 模式拿不到任务临时目录时返回空串:不注入、不回捞,
    绝不能退化到 /home/user 那种宿主机上不存在的路径。"""
    assert A._async_agent_reports_dir(TASK.id, 1, "local", None) == ""


# ============================================================
# 注入段文案与装配
# ============================================================

def test_reports_section_empty_when_no_dir():
    assert build_cli_subagent_reports_section("") == ""
    assert build_cli_subagent_reports_section("   ") == ""


def test_reports_section_mentions_dir_outside_repo_and_neutral_tools():
    sec = build_cli_subagent_reports_section("/home/user/.agent_reports/abc/r1")
    assert "[后台子任务结论落盘要求]" in sec
    assert "/home/user/.agent_reports/abc/r1/" in sec
    # 关键要点:落在仓库之外(否则会污染工作区 diff)+ 收尾前汇总落盘文件
    assert "不要" in sec and "被审查的仓库目录" in sec
    assert "逐条汇总这些文件" in sec
    # 措辞工具中立:不出现后端专属的 read_file
    assert "read_file" not in sec


def test_context_sections_hash_has_subagent_bucket_and_keeps_two_arg_call():
    two = A._context_sections_hash("R", "M")
    three = A._context_sections_hash("R", "M", "S")
    assert set(two) == {"repo", "memory", "subagent"}
    assert two["subagent"] == A._sha1_text("")
    assert three["subagent"] == A._sha1_text("S")
    # 分桶不互扰:落盘段变化不影响 repo/memory 指纹
    assert three["repo"] == two["repo"] and three["memory"] == two["memory"]


def test_compose_send_text_places_reports_section_before_this_round_instruction():
    msg = A._compose_send_text("BASE", "REPO", "MEM", "HISTORY", "SUBAGENT")
    assert msg.index("HISTORY") < msg.index("REPO") < msg.index("MEM") < msg.index("SUBAGENT") < msg.index("BASE")
    # 空段不产生多余分隔
    assert A._compose_send_text("BASE", "", "", "", "") == "BASE"


# ============================================================
# 落盘段注入去重(新桶缺失 = 未送达)
# ============================================================

def test_subagent_injection_full_for_new_session():
    assert A._resolve_subagent_injection(None, "SEC") == "SEC"
    # 复用一个从未成功收下 prompt 的空 session → 同样全量注入
    assert A._resolve_subagent_injection({"prompt_accepted": False}, "SEC") == "SEC"


def test_subagent_injection_missing_key_means_not_delivered():
    """改造前上线的 session 记录里没有 subagent 桶:按"未送达"补发一次。

    repo/memory 两桶缺 key 的既有默认是"已送达",这里方向相反 —— 沿用默认会让
    这些 session 永远收不到落盘要求。
    """
    legacy = {"prompt_accepted": True, "injected_context_hash": A._context_sections_hash("R", "M")}
    # 旧记录(只有 repo/memory 两桶)的 subagent 指纹必然不是空串的哈希
    legacy["injected_context_hash"].pop("subagent")
    assert A._resolve_subagent_injection(legacy, "SEC") == "SEC"


def test_subagent_injection_skipped_when_unchanged_in_same_session():
    state = {
        "prompt_accepted": True,
        "injected_context_hash": A._context_sections_hash("", "", "SEC"),
    }
    assert A._resolve_subagent_injection(state, "SEC") == ""
    # 内容变了(换了目录口径)→ 重新注入
    assert A._resolve_subagent_injection(state, "SEC2") == "SEC2"
    # 空段永远返回空
    assert A._resolve_subagent_injection(state, "") == ""


# ============================================================
# 回捞落盘报告
# ============================================================

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):  # 过滤条件由被测代码在 Python 侧判断
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, existing=None):
        self.added = []
        self.deleted = []
        self.commits = 0
        self.rollbacks = 0
        self._existing = list(existing or [])

    def query(self, model):
        return _FakeQuery(self._existing)

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeSession:
    """run_command 按预设返回;calls 记录命令序列以核对 quoting"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def run_command(self, cmd, timeout=None, check=False):
        self.calls.append(cmd)
        item = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _collect_on(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_REPORTS_COLLECT", True)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_REPORTS_MAX_CHARS", 20000)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_REPORTS_MAX_FILES", 8)


def _artifact_rows(db):
    return db.added


def test_collect_writes_artifacts_with_metadata():
    db = _FakeDB()
    session = _FakeSession(["a.md\nb.md\n", "报告A", "报告B"])
    saved = A._collect_async_agent_reports(
        TASK, db, session, "/home/user/.agent_reports/11111111/r1", 1
    )
    assert saved == 2
    rows = _artifact_rows(db)
    assert [r.kind for r in rows] == ["agent_report", "agent_report"]
    assert all(isinstance(r, TaskArtifact) for r in rows)
    assert [r.metadata_["filename"] for r in rows] == ["a.md", "b.md"]
    assert [r.metadata_["round_idx"] for r in rows] == [1, 1]
    assert rows[0].content == "报告A"
    assert rows[0].metadata_["truncated"] is False
    # 目录/文件名分别独立成命令,且路径经 shlex.quote(此例无需加引号,但不能裸拼接)
    assert session.calls[0].startswith("ls -1 /home/user/.agent_reports")
    assert session.calls[0].endswith("r1 2>/dev/null")
    assert "cat " in session.calls[1]


def test_collect_deletes_same_round_before_writing():
    old_same_round = TaskArtifact(
        task_id=TASK.id, kind="agent_report", content="旧",
        metadata_={"round_idx": 1, "filename": "a.md"},
    )
    old_other_round = TaskArtifact(
        task_id=TASK.id, kind="agent_report", content="上一轮",
        metadata_={"round_idx": 2, "filename": "z.md"},
    )
    db = _FakeDB(existing=[old_same_round, old_other_round])
    A._collect_async_agent_reports(TASK, db, _FakeSession(["a.md\n", "新内容"]), "/r", 1)
    assert db.deleted == [old_same_round]        # 只删同轮(resume/重试幂等,不堆叠)
    assert len(db.added) == 1


def test_collect_truncates_oversized_report(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_REPORTS_MAX_CHARS", 10)
    db = _FakeDB()
    A._collect_async_agent_reports(TASK, db, _FakeSession(["a.md\n", "0123456789ABCDEFGHIJ"]), "/r", 1)
    row = db.added[0]
    assert row.content == "0123456789"
    assert row.metadata_["truncated"] is True
    assert row.metadata_["char_count"] == 10


def test_collect_respects_max_files(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_REPORTS_MAX_FILES", 2)
    db = _FakeDB()
    saved = A._collect_async_agent_reports(
        TASK, db, _FakeSession(["c.md\na.md\nb.md\nd.md\n", "x"]), "/r", 1
    )
    # 按文件名排序取前 2
    assert saved == 2
    assert [r.metadata_["filename"] for r in db.added] == ["a.md", "b.md"]


def test_collect_ignores_illegal_entries():
    """目录清单里的子目录/穿越项一律不收(回捞路径必须落在 reports 目录内)。"""
    db = _FakeDB()
    saved = A._collect_async_agent_reports(
        TASK, db, _FakeSession(["ok.md\nsub/dir.md\n../escape.md\nnotes.txt\n", "内容"]), "/r", 1
    )
    assert saved == 1
    assert db.added[0].metadata_["filename"] == "ok.md"


def test_collect_skips_blank_listing_and_empty_content():
    assert A._collect_async_agent_reports(TASK, _FakeDB(), _FakeSession([""]), "/r", 1) == 0
    db = _FakeDB()
    assert A._collect_async_agent_reports(
        TASK, db, _FakeSession(["a.md\n", "   \n"]), "/r", 1
    ) == 0
    assert db.added == []


def test_collect_skips_when_reports_dir_empty():
    """无目录(如 local 模式拿不到 local_dir)→ 不执行任何命令也不写库。"""
    db = _FakeDB()
    session = _FakeSession(["a.md\n", "x"])
    assert A._collect_async_agent_reports(TASK, db, session, "", 1) == 0
    assert session.calls == []
    assert db.added == []


def test_collect_skips_when_switch_off(monkeypatch):
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_REPORTS_COLLECT", False)
    db = _FakeDB()
    session = _FakeSession(["a.md\n", "x"])
    assert A._collect_async_agent_reports(TASK, db, session, "/r", 1) == 0
    assert session.calls == []
    assert db.added == []


def test_collect_is_silent_on_command_failure():
    """目录不可读/单文件失败都只记日志:绝不拖垮收尾,也不抛出。"""
    db = _FakeDB()
    boom = A._collect_async_agent_reports(
        TASK, db, _FakeSession([RuntimeError("sandbox gone")]), "/r", 1
    )
    assert boom == 0 and db.added == []

    # 列清单成功但 cat 失败 → 跳过该份,不抛
    db2 = _FakeDB()
    got = A._collect_async_agent_reports(
        TASK, db2, _FakeSession(["a.md\nb.md\n", OSError("read fail"), "B 内容"]), "/r", 1
    )
    assert got == 1
    assert db2.added[0].metadata_["filename"] == "b.md"


def test_collect_commit_failure_rolls_back_and_returns_zero():
    class _BoomDB(_FakeDB):
        def commit(self):
            raise RuntimeError("db down")

    db = _BoomDB()
    got = A._collect_async_agent_reports(TASK, db, _FakeSession(["a.md\n", "x"]), "/r", 1)
    assert got == 0
    assert db.rollbacks >= 1


# ============================================================
# 跨轮回放:已回捞的落盘报告要能重新喂给下一轮
# ============================================================

class _ReportRow:
    """TaskArtifact 的轻量替身(只带回放读取的字段)"""

    def __init__(self, content, metadata):
        self.content = content
        self.metadata_ = metadata
        self.created_at = 0


class _ListQueryDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, model):
        rows = self._rows

        class _Q:
            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def all(self):
                return list(rows)

        return _Q()


def test_load_agent_reports_groups_by_round():
    from app.agents.react_agent import _load_agent_reports_by_round

    db = _ListQueryDB([
        _ReportRow("甲报告", {"round_idx": 1, "filename": "a.md"}),
        _ReportRow("乙报告", {"round_idx": 1, "filename": "b.md"}),
        _ReportRow("上一轮报告", {"round_idx": 2, "filename": "c.md"}),
        _ReportRow("轮次缺失", {}),                # 无 round_idx → 不注入
        _ReportRow("   ", {"round_idx": 1}),       # 空内容 → 不注入
    ])
    by_round = _load_agent_reports_by_round(db, TASK.id)
    assert sorted(by_round) == [1, 2]
    assert "甲报告" in by_round[1] and "乙报告" in by_round[1]
    assert "a.md" in by_round[1]                    # 带文件名便于模型归因
    assert by_round[2] == "### c.md\n上一轮报告"


def test_load_agent_reports_truncates_oversized_single_report(monkeypatch):
    from app.agents import react_agent as R

    monkeypatch.setattr(R, "MAX_AGENT_REPORT_HISTORY_CHARS", 5)
    db = _ListQueryDB([_ReportRow("0123456789", {"round_idx": 1, "filename": "a.md"})])
    text = R._load_agent_reports_by_round(db, TASK.id)[1]
    assert "01234\n…(已截断)" in text
    assert "012345" not in text        # 只留前 5 字符,超出部分已丢


def test_load_agent_reports_swallows_db_failure():
    """查不到就当"无报告":回放是增强项,不能拖垮执行轮。"""
    from app.agents.react_agent import _load_agent_reports_by_round

    class _Boom:
        def query(self, model):
            raise RuntimeError("db down")

    assert _load_agent_reports_by_round(_Boom(), TASK.id) == {}
