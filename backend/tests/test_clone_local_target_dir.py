"""local 模式 clone 的目标目录处置:残留清理 / 幂等复用 / 避让 / 同任务串行

回归背景:git clone 要求目标目录不存在或为空,而 local 模式整个任务复用同一个
会话临时目录 —— 上一次失败(超时 kill)或"跳过预克隆"留下的半成品目录,会让下
一次尝试(换协议 / 换分支 / 降级为自主 clone / 工作区恢复)直接报
fatal: destination path '...' already exists and is not an empty directory,
把真实的首因错误淹没在级联错误里。旧实现只有 sandbox 模式在 _clone_repo_sandbox
里 rm -rf 兜底。

分工(职责不重叠,避免"猜目录"删错东西):
- _reuse_existing_clone:回退链入口按"归一化 URL + 分支"判断幂等复用,不动磁盘
- _resolve_local_repo_dir:按归属决定删除或避让换名,绝不碰非本会话 clone 的仓库
- _clone_repo_local 自身:失败/被跳过时清掉本次克隆的目录(只有它知道落在哪)
- Windows 只读 pack:委托 repo_cache.force_rmtree(chmod 后再删,重试只治瞬时锁)
"""
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

import pytest

import app.tools.sandbox_tools as st
from app.clone_skip import clear_skip_state, request_skip_clone

GITEE_URL = "https://gitee.com/shwsq/overleaf.git"
GITEE_SSH = "git@gitee.com:shwsq/overleaf.git"


def _ctx(tmp_path, repo_path="", clone_source=None):
    ctx = {"mode": "local", "local_dir": tmp_path, "repo_path": repo_path}
    if clone_source is not None:
        ctx["clone_source"] = clone_source
    return ctx


def _mk_junk(root: Path, name: str) -> Path:
    """无 .git 的残留目录(空壳/半成品工作树)"""
    d = root / name
    (d / "src").mkdir(parents=True)
    (d / "src" / "a.py").write_text("x", encoding="utf-8")
    return d


def _mk_repo(root: Path, name: str) -> Path:
    """含 .git 的目录(kill 半途与成功 clone 都是这个形态)"""
    d = root / name
    (d / ".git").mkdir(parents=True)
    (d / "README.md").write_text("half", encoding="utf-8")
    return d


# ============================================================
# 基础判定与删除(Windows 只读 pack)
# ============================================================


def test_dir_has_entries(tmp_path):
    """不存在/空目录 → False;非空目录/同名文件 → True(git 的判空口径)。"""
    assert st._dir_has_entries(tmp_path / "nope") is False
    (tmp_path / "empty").mkdir()
    assert st._dir_has_entries(tmp_path / "empty") is False
    assert st._dir_has_entries(_mk_junk(tmp_path, "full")) is True
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert st._dir_has_entries(tmp_path / "file.txt") is True


def test_remove_local_tree_handles_readonly_pack_files(tmp_path):
    """只读文件必须删得掉(git 在 Windows 把 pack/*.pack|*.idx 设为只读)。

    普通 shutil.rmtree 在 Windows 碰只读文件直接 WinError 5 且重试无效 —— 这是
    原始 bug 的删除失败机制,靠 repo_cache.force_rmtree 的 chmod onexc 解决。
    POSIX 下 unlink 权限看父目录,同一条断言也成立(不会误报)。
    """
    d = tmp_path / "overleaf"
    packs = d / ".git" / "objects" / "pack"
    packs.mkdir(parents=True)
    pack = packs / "pack-abc.idx"
    pack.write_text("readonly-sim", encoding="utf-8")
    os.chmod(pack, stat.S_IREAD)
    assert st._remove_local_tree(d) is True
    assert not d.exists()


def test_remove_local_tree_deletes_stray_file(tmp_path):
    """同名文件占用(非目录)走 unlink,不能因为 rmtree 只认目录就失败。"""
    f = tmp_path / "bar"
    f.write_text("x", encoding="utf-8")
    assert st._remove_local_tree(f) is True
    assert not f.exists()


def test_remove_local_tree_reports_failure_without_raising(tmp_path, monkeypatch):
    """删除失败返回 False(交调用方避让),不抛异常盖掉原始克隆错误。"""
    monkeypatch.setattr(st, "force_rmtree", lambda *a, **kw: False)
    assert st._remove_local_tree(_mk_junk(tmp_path, "bar")) is False


# ============================================================
# 目标目录处置:按归属删除或避让
# ============================================================


def test_resolve_target_absent_or_empty(tmp_path):
    """目录不存在或为空:直接用作克隆目标。"""
    assert st._resolve_local_repo_dir(_ctx(tmp_path), "bar") == tmp_path / "bar"
    (tmp_path / "bar").mkdir()
    assert st._resolve_local_repo_dir(_ctx(tmp_path), "bar") == tmp_path / "bar"


def test_resolve_target_cleans_junk(tmp_path):
    """无 .git 的残留 → 清掉后沿用原目录名。"""
    junk = _mk_junk(tmp_path, "bar")
    assert st._resolve_local_repo_dir(_ctx(tmp_path), "bar") == tmp_path / "bar"
    assert not junk.exists()


def test_resolve_target_recycles_own_clone(tmp_path):
    """本会话 clone 出来的工作区:走到了真克隆就删除让位(如换分支重试)。"""
    repo = _mk_repo(tmp_path, "bar")
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), "dev"),
    )
    assert st._resolve_local_repo_dir(ctx, "bar") == repo
    assert not repo.exists()


def test_resolve_target_never_deletes_upload_workspace(tmp_path):
    """只记了 repo_path 而无 clone_source 的目录(上传工作区)不能被当残留删掉。"""
    uploads = tmp_path / "uploaded_files"
    (uploads / "lecture.pptx").parent.mkdir()
    (uploads / "lecture.pptx").write_text("user data", encoding="utf-8")
    ctx = _ctx(tmp_path, repo_path=str(uploads))
    assert st._resolve_local_repo_dir(ctx, "uploaded_files") == tmp_path / "uploaded_files-2"
    assert (uploads / "lecture.pptx").exists()


def test_resolve_target_avoids_foreign_repo(tmp_path):
    """有 .git 但不是本会话 clone 的(执行 agent 自行克隆的成果)→ 避让换名,绝不删。"""
    repo = _mk_repo(tmp_path, "bar")
    assert st._resolve_local_repo_dir(_ctx(tmp_path), "bar") == tmp_path / "bar-2"
    assert repo.exists()
    assert (repo / "README.md").exists()


def test_resolve_target_avoids_when_removal_fails(tmp_path, monkeypatch):
    """残留删不掉(句柄被长期占用)→ 避让换名,克隆不因目录冲突而失败。"""
    monkeypatch.setattr(st, "force_rmtree", lambda *a, **kw: False)
    _mk_junk(tmp_path, "bar")
    assert st._resolve_local_repo_dir(_ctx(tmp_path), "bar") == tmp_path / "bar-2"


def test_resolve_target_avoids_when_own_clone_undeletable(tmp_path, monkeypatch):
    """自己的旧工作区删不掉也只能避让:带着残留让 git 报错是必然失败。"""
    repo = _mk_repo(tmp_path, "bar")
    monkeypatch.setattr(st, "force_rmtree", lambda *a, **kw: False)
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), "main"),
    )
    assert st._resolve_local_repo_dir(ctx, "bar") == tmp_path / "bar-2"


def test_unique_repo_dir_skips_taken_names(tmp_path):
    """非空候选名逐个避让(空目录 git 能写,不算占用),全被占时用随机后缀。"""
    taken = {f"bar-{i}" for i in range(2, 21)}
    for name in taken:
        d = tmp_path / name
        (d / ".git").mkdir(parents=True)
        (d / "f.txt").write_text("x", encoding="utf-8")
    target = st._unique_repo_dir(tmp_path, "bar")
    assert target.name not in taken
    assert re.fullmatch(r"bar-[0-9a-f]{6}", target.name)


def test_unique_repo_dir_reuses_empty_dir(tmp_path):
    """空的 bar-2 仍可直接用(git 对空目录不报错)。"""
    (tmp_path / "bar-2").mkdir()
    assert st._unique_repo_dir(tmp_path, "bar") == tmp_path / "bar-2"


# ============================================================
# 幂等复用:按归一化 URL + 分支判断,与目录名无关
# ============================================================


def test_reuse_existing_clone_same_source(tmp_path):
    """同一来源已 clone → 复用,且带 reused 标记与文件数。"""
    repo = _mk_repo(tmp_path, "bar")
    (repo / "a.py").write_text("x", encoding="utf-8")
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), "main"),
    )
    result = st._reuse_existing_clone(ctx, GITEE_URL, "main")
    assert result["path"] == str(repo)
    assert result["reused"] is True
    assert result["files_count"] == 2  # README.md + a.py(不计 .git)


def test_reuse_existing_clone_matches_ssh_form(tmp_path):
    """SSH / HTTPS 两种写法归一化后同键:不能因为调用方形态不同就重新克隆。"""
    repo = _mk_repo(tmp_path, "bar")
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), ""),
    )
    assert st._reuse_existing_clone(ctx, GITEE_SSH, None)["path"] == str(repo)


def test_reuse_existing_clone_finds_avoided_dir_name(tmp_path):
    """避让后工作区落在 bar-2:按目录名找不回来就会再克隆一份(bar-3、bar-4 累加)。"""
    repo = _mk_repo(tmp_path, "bar-2")
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), "main"),
    )
    assert st._reuse_existing_clone(ctx, GITEE_URL, "main")["path"] == str(repo)


def test_reuse_existing_clone_rejects_different_repo(tmp_path):
    """同名不同源(github 的 overleaf)不能复用成 gitee 那份,得真去克隆。"""
    repo = _mk_repo(tmp_path, "overleaf")
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), ""),
    )
    assert st._reuse_existing_clone(ctx, "https://github.com/overleaf/overleaf.git", None) is None


def test_reuse_existing_clone_branch_rules(tmp_path):
    """分支:任一侧未指定算不冲突(远端默认分支常与参数不符);两侧都指定且不同则重克隆。"""
    repo = _mk_repo(tmp_path, "bar")
    ctx = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), "dev"),
    )
    assert st._reuse_existing_clone(ctx, GITEE_URL, "main") is None
    assert st._reuse_existing_clone(ctx, GITEE_URL, None) is not None
    ctx2 = _ctx(
        tmp_path, repo_path=str(repo),
        clone_source=(st.normalize_repo_url(GITEE_URL), ""),
    )
    assert st._reuse_existing_clone(ctx2, GITEE_URL, "main") is not None


def test_reuse_existing_clone_requires_healthy_workspace(tmp_path):
    """未记录来源 / 目录已不在 / local 下缺 .git(残缺检出)都不算可复用。"""
    repo = _mk_repo(tmp_path, "bar")
    source = (st.normalize_repo_url(GITEE_URL), "")
    assert st._reuse_existing_clone(_ctx(tmp_path), GITEE_URL, None) is None
    assert st._reuse_existing_clone(
        _ctx(tmp_path, repo_path=str(tmp_path / "gone"), clone_source=source),
        GITEE_URL, None,
    ) is None
    shell = tmp_path / "shell"
    shell.mkdir()
    assert st._reuse_existing_clone(
        _ctx(tmp_path, repo_path=str(shell), clone_source=source), GITEE_URL, None,
    ) is None


def test_record_clone_source_key_and_branch(tmp_path):
    """记录的是"归一化 URL + 实际检出分支",分支为空时存空串。"""
    ctx = _ctx(tmp_path)
    st._record_clone_source(ctx, GITEE_SSH, None)
    assert ctx["clone_source"] == (st.normalize_repo_url(GITEE_URL), "")


# ============================================================
# _clone_repo_local:克隆前处置,失败/跳过时清理自己那份
# ============================================================


class _ExitProc:
    """伪造 Popen:立即退出,并按半成品形态造出目标目录(含 .git)"""

    def __init__(self, dest: str, rc: int = 0, stderr_lines=()):
        self.returncode = rc
        self.stderr = list(stderr_lines)
        d = Path(dest)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".git").mkdir(exist_ok=True)
        (d / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
        (d / "README.md").write_text("half", encoding="utf-8")

    def poll(self):
        return self.returncode

    def kill(self):
        pass


class _HangProc:
    """伪造 Popen:永不退出(供跳过/超时检查点触发),启动即留下半成品目录"""

    def __init__(self, dest: str):
        self.returncode = None
        self.stderr = iter([])
        d = Path(dest)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".git").mkdir(exist_ok=True)
        self.killed = False

    def poll(self):
        return None

    def kill(self):
        self.killed = True


def test_local_clone_cleans_leftover_before_git(tmp_path, monkeypatch):
    """残留目录在启动 git 前被清掉:目标名沿用 bar,不会撞 already exists。"""
    _mk_junk(tmp_path, "bar")
    recorded: dict[str, object] = {}

    def _fake_popen(cmd, *a, **kw):
        # 此处相当于 git 看到的现场:目标名必须可写(不存在或为空)
        recorded["dest"] = cmd[-1]
        recorded["dest_free"] = not st._dir_has_entries(Path(cmd[-1]))
        return _ExitProc(cmd[-1])

    monkeypatch.setattr(st.subprocess, "Popen", _fake_popen)
    result = st._clone_repo_local(_ctx(tmp_path), GITEE_URL, "bar", None)
    assert recorded["dest"] == str(tmp_path / "bar")
    assert recorded["dest_free"] is True
    assert result["files_count"] == 1  # README.md(不计 .git)


def test_local_clone_failure_removes_own_target_and_keeps_real_error(tmp_path, monkeypatch):
    """非零退出:清掉本次克隆目录,且错误信息里留下 fatal 而不是进度行。"""
    monkeypatch.setattr(
        st.subprocess, "Popen",
        lambda cmd, *a, **kw: _ExitProc(
            cmd[-1], rc=1,
            stderr_lines=[
                "Receiving objects: 100% (9/9), done.\n",
                "Updating files:  99% (6514/6579)\n",
                "Updating files: 100% (6579/6579)\n",
                "error: unable to create file a/deep/path.txt: Filename too long\n",
            ],
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        st._clone_repo_local(_ctx(tmp_path), GITEE_URL, "bar", None)
    assert "Filename too long" in str(exc.value)
    assert "Updating files" not in str(exc.value)
    assert not (tmp_path / "bar").exists()


def test_local_clone_skip_removes_own_target(tmp_path, monkeypatch):
    """克隆中被跳过(kill 半途):目录被自己清掉,异常继续向上传播。"""
    task_id = uuid.uuid4().hex
    procs: list[_HangProc] = []

    # 半成品在 Popen 时才出现(目标目录已过了归属判定,名字仍是 bar)
    def _fake_popen(cmd, *a, **kw):
        fake = _HangProc(cmd[-1])
        procs.append(fake)
        return fake

    monkeypatch.setattr(st.subprocess, "Popen", _fake_popen)
    request_skip_clone(task_id)
    with pytest.raises(st.CloneSkippedError):
        st._clone_repo_local(
            _ctx(tmp_path), GITEE_URL, "bar", None, task_id=task_id, cancellable=True,
        )
    assert procs[0].killed
    assert not (tmp_path / "bar").exists()
    clear_skip_state(task_id)


def test_local_clone_timeout_removes_own_target(tmp_path, monkeypatch):
    """超时 kill:同样清掉半成品(降级为自主 clone 时才不会撞 already exists)。"""
    monkeypatch.setattr(st.settings, "REPO_CLONE_TIMEOUT", 0)
    monkeypatch.setattr(
        st.subprocess, "Popen", lambda cmd, *a, **kw: _HangProc(str(tmp_path / "bar")),
    )
    with pytest.raises(RuntimeError, match="超时"):
        st._clone_repo_local(_ctx(tmp_path), GITEE_URL, "bar", None)
    assert not (tmp_path / "bar").exists()


def test_local_clone_failure_keeps_foreign_repo_untouched(tmp_path, monkeypatch):
    """避让出去的 clone 失败时,只清自己那份(bar-2),不碰受保护的外来仓库(bar)。

    旧的回退链按仓名猜目录清理,会把 _resolve_local_repo_dir 判定为"不可删"的
    外来工作区抹掉 —— 清理职责下沉到克隆者本身后不再有这个矛盾。
    """
    foreign = _mk_repo(tmp_path, "bar")
    monkeypatch.setattr(
        st.subprocess, "Popen", lambda cmd, *a, **kw: _ExitProc(cmd[-1], rc=1),
    )
    with pytest.raises(RuntimeError):
        st._clone_repo_local(_ctx(tmp_path), GITEE_URL, "bar", None)
    assert foreign.exists()
    assert (foreign / "README.md").exists()
    assert not (tmp_path / "bar-2").exists()


def test_git_error_tail_flags_progress_only_output(tmp_path):
    """全是进度行(被中途打断)时给出可读说明,而不是空字符串。"""
    out = st._git_error_tail(["Receiving objects: 45% (4/9)\n", "Updating files:  90% (1/9)\n"])
    assert "仅剩进度输出" in out


# ============================================================
# 回退链:复用优先、失败可重试、同任务串行
# ============================================================


def _offline_fallback(monkeypatch, tmp_path, task_id):
    """关仓库缓存 + 把假 ctx 注册进 _sessions(让 _set_repo_path 真的写回)"""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(st.settings, "REPO_CACHE_ENABLED", False)
    monkeypatch.setattr(
        st, "_get_or_create_session",
        lambda tid, **kw: st._sessions.setdefault(tid, ctx),
    )
    return ctx


def _teardown(task_id):
    st._sessions.pop(task_id, None)
    st._clone_locks.pop(task_id, None)


def test_fallback_reuses_existing_clone_without_second_git(monkeypatch, tmp_path):
    """同一来源再次 clone:复用既有工作区,绝不启动第二次 git(含避让后的目录名)。"""
    task_id = uuid.uuid4().hex
    ctx = _offline_fallback(monkeypatch, tmp_path, task_id)
    try:
        def _first_clone(_c, url, repo_name, branch, **kw):
            repo = tmp_path / repo_name
            (repo / ".git").mkdir(parents=True)
            (repo / "a.py").write_text("x", encoding="utf-8")
            return {"path": str(repo), "files_count": 1}

        monkeypatch.setattr(st, "_clone_repo_local", _first_clone)
        first = st.clone_repo_with_fallback(
            GITEE_URL, branch="main", task_id=task_id,
        )
        assert ctx["clone_source"] == (st.normalize_repo_url(GITEE_URL), "main")

        def _boom(*a, **kw):
            raise AssertionError("已 clone 过的同一来源不应再启动克隆")

        monkeypatch.setattr(st, "_clone_repo_local", _boom)
        again = st.clone_repo_with_fallback(GITEE_SSH, task_id=task_id)
        assert again["reused"] is True
        assert again["path"] == first["path"]
    finally:
        _teardown(task_id)


def test_fallback_next_protocol_sees_clean_target(monkeypatch, tmp_path):
    """第一种协议失败后,下一种协议看到的是空目标(端到端走真 _clone_repo_local)。

    不 patch 克隆函数,只 fake Popen:第一次非零退出留下的半成品必须被克隆者
    自己清掉,否则第二次尝试就是那句 already exists。
    """
    task_id = uuid.uuid4().hex
    _offline_fallback(monkeypatch, tmp_path, task_id)
    dests: list[str] = []
    free_at_start: list[bool] = []

    def _fake_popen(cmd, *a, **kw):
        # 此处相当于 git 看到的现场:每次尝试的目标名都必须可写
        dests.append(cmd[-1])
        free_at_start.append(not st._dir_has_entries(Path(cmd[-1])))
        rc = 128 if len(dests) == 1 else 0
        return _ExitProc(cmd[-1], rc=rc, stderr_lines=["fatal: early EOF\n"])

    monkeypatch.setattr(st.subprocess, "Popen", _fake_popen)
    try:
        result = st.clone_repo_with_fallback(GITEE_URL, task_id=task_id)
        # 无 token 的 gitee 仓库:SSH 与匿名 HTTPS 两种候选
        assert len(dests) == 2
        assert free_at_start == [True, True]
        assert result["path"] == dests[1]
        assert Path(result["path"]).is_dir()
    finally:
        _teardown(task_id)


def test_fallback_serializes_same_task(monkeypatch):
    """同 task_id 的并发克隆串行执行(同一时刻回退链内只有一个在跑)。"""
    task_id = uuid.uuid4().hex
    active: list[int] = []
    overlap: list[int] = []

    def _fake_impl(repo_url, **kw):
        active.append(1)
        if len(active) > 1:
            overlap.append(1)
        time.sleep(0.05)
        active.pop()
        return {"path": "/tmp/bar", "files_count": 0}

    monkeypatch.setattr(st, "_clone_repo_fallback", _fake_impl)
    threads = [
        threading.Thread(
            target=lambda: st.clone_repo_with_fallback(GITEE_URL, task_id=task_id),
        )
        for _ in range(3)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not overlap
    finally:
        st._clone_locks.pop(task_id, None)


def test_fallback_different_tasks_use_different_locks():
    """不同 task_id 不互相阻塞;登记可被清除。"""
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        assert st._get_clone_lock(a) is st._get_clone_lock(a)
        assert st._get_clone_lock(a) is not st._get_clone_lock(b)
    finally:
        st._clone_locks.pop(a, None)
        st._clone_locks.pop(b, None)
