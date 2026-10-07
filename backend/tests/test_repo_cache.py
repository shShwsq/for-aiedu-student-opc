"""repo_cache(bare 仓库缓存)单元测试:真实 git + tmp_path,不连 DB/网络。

覆盖:
- normalize_repo_url/cache_key:token URL 与匿名 URL 同键、.git 后缀归一
- ensure_bare_cache 首次:建立 bare + 安全断言(config 无 token、remote 为匿名)
- bare FETCH_HEAD 清洗:_strip_url_userinfo 口径 / _scrub_bare_fetch_head 幂等与
  保留 LF / _fetch_bare 成功后必调钩子
- fetch TTL:窗口内不 fetch,超窗 fetch
- 分支缺失:强制 fetch 后仍缺返回 None;新分支真实 fetch 后命中;tag 可命中
- 损坏重建:_is_healthy_bare 失败 → 删缓存重建
- clone_from_cache:分支正确检出
- LRU 淘汰:超限删最旧,1h 保护窗内跳过
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

import app.services.repo_cache as rc
from app.config import settings


# ============================================================
# 辅助:真实 git 操作
# ============================================================


def _git(cwd, *argv, check=True):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {argv} 失败: {proc.stderr}")
    return proc.stdout


def _make_origin(path: Path) -> Path:
    """建一个真实源仓库:main 分支一个提交(a.txt)"""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "tester")
    (path / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    cdir = tmp_path / "repo_cache"
    monkeypatch.setattr(settings, "REPO_CACHE_DIR", str(cdir))
    return cdir


# ============================================================
# 归一化 / 缓存键
# ============================================================


def test_normalize_token_url_same_key_as_anonymous():
    """token URL 与匿名 URL 归一后同键(缓存以匿名形态存在,跨用户复用)。"""
    token_url = "https://x-access-token:ghp_secret@github.com/foo/bar.git"
    anon_url = "https://github.com/foo/bar"
    assert rc.normalize_repo_url(token_url) == rc.normalize_repo_url(anon_url)
    assert rc.cache_key(token_url) == rc.cache_key(anon_url)
    # .git 后缀与尾斜杠归一
    assert rc.normalize_repo_url("https://github.com/foo/bar.git/") == "https://github.com/foo/bar"


def _url(path: Path) -> str:
    """测试统一用 posix 形式路径作 repo_url(与 URL 同为 / 分隔,缓存键一致)"""
    return path.as_posix()


# ============================================================
# ensure_bare_cache:建立 + token 安全
# ============================================================


class _FakeProvider:
    """把 URL 候选链导向本地真实仓库:token 候选用 token 命名的真实仓库目录,
    匿名候选用普通命名目录 —— 若"重写 remote.origin.url 为匿名"缺失,
    config 会含 token,安全断言即失败。"""

    id = "github"
    host = "github.com"
    token_username = "x-access-token"

    def __init__(self, anon: str, token_named: str):
        self._anon = anon
        self._token_named = token_named

    def to_https_url(self, repo_url):
        return self._anon

    def to_ssh_url(self, repo_url):
        return ""

    def inject_token_in_https(self, https_url, token):
        return self._token_named


def test_ensure_builds_bare_and_strips_token(tmp_path, cache_dir, monkeypatch):
    """首次 ensure:建立 bare;安全核心断言 —— config 全文不含 token,remote 为匿名 URL。"""
    token = "ghp_fake_secret_token_987654"
    origin = _make_origin(tmp_path / "origin")
    # token 候选:同名真实仓库(目录名含 token)——克隆会成功
    token_named = tmp_path / f"tokendir_{token}"
    _git(tmp_path, "clone", str(origin), str(token_named))

    monkeypatch.setattr(
        rc, "get_provider_for_url", lambda url: _FakeProvider(str(origin), str(token_named)),
    )
    bare = rc.ensure_bare_cache(
        "https://github.com/foo/bar", git_tokens={"github": token}, task_id="t1",
    )
    assert bare is not None and bare.exists()
    # 安全断言:config 全文不含 token,remote 指向匿名 URL
    config_text = (bare / "config").read_text(encoding="utf-8")
    assert token not in config_text, "token 泄漏进 bare config!"
    assert rc._run_git(["-C", str(bare), "remote", "get-url", "origin"]).strip() == str(origin)
    # meta 存在且记录匿名 URL
    meta = json.loads((bare.parent / rc._META_NAME).read_text(encoding="utf-8"))
    assert meta["url"] == rc.normalize_repo_url("https://github.com/foo/bar")


# ============================================================
# bare FETCH_HEAD 清洗(git fetch 会把 fetch URL 记进文件头)
# ============================================================


def test_strip_url_userinfo_variants():
    """与 sandbox_tools 同口径:只剥 http(s) URL 的 userinfo,不误伤 SSH/路径含 @"""
    cred = "abc\tbranch 'main' of https://x-access-token:ghpSECRET@git.example.com/o/r\n"
    anon = rc._strip_url_userinfo(cred)
    assert "ghpSECRET" not in anon
    assert "https://git.example.com/o/r" in anon
    # 大小写不敏感(HTTPS://)也剥掉
    assert "tok" not in rc._strip_url_userinfo("HTTPS://u:tok@host/p\n")
    # SSH 形态 git@host:path 不该被动
    ssh = "git@github.com:foo/bar.git\n"
    assert rc._strip_url_userinfo(ssh) == ssh


def test_scrub_bare_fetch_head_strips_token(tmp_path):
    """fetch 后写盘:含 userinfo 的行被清洗,原 LF 行尾与 SHA 结构保留"""
    bare = tmp_path / "repo.git"
    bare.mkdir()
    token_url = "https://x-access-token:ghpSECRET@github.com/foo/bar.git"
    fetch_head = bare / "FETCH_HEAD"
    fetch_head.write_text(
        f"{'1' * 40}\tbranch 'main' of {token_url}\n"
        f"{'2' * 40}\t\ttag 'v1' of {token_url}\n",
        encoding="utf-8", newline="",
    )

    rc._scrub_bare_fetch_head(bare, task_id="t1")

    raw = fetch_head.read_bytes()
    assert b"ghpSECRET" not in raw
    assert b"https://github.com/foo/bar.git" in raw
    # LF 保住,Windows 上默认换行翻译会污染整文件
    assert b"\r\n" not in raw
    # 前缀 SHA 与 tab 结构不变
    assert raw.startswith(b"11111111")
    assert raw.count(b"branch 'main' of") == 1
    assert raw.count(b"tag 'v1' of") == 1


def test_scrub_bare_fetch_head_noop_when_clean(tmp_path):
    """已匿形的 FETCH_HEAD 不动(幂等,避免每次 fetch 都触发 mtime 变化)"""
    bare = tmp_path / "repo.git"
    bare.mkdir()
    original = "abc123\tbranch 'main' of https://github.com/foo/bar.git\n"
    fetch_head = bare / "FETCH_HEAD"
    fetch_head.write_text(original, encoding="utf-8", newline="")
    before_stat = fetch_head.stat()

    rc._scrub_bare_fetch_head(bare, task_id="t1")

    assert fetch_head.read_text(encoding="utf-8") == original
    # 未清洗就不重写字节
    assert fetch_head.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_scrub_bare_fetch_head_missing_ok(tmp_path):
    """FETCH_HEAD 不存在(clone --bare 不会写它):不抛,静默返回"""
    bare = tmp_path / "repo.git"
    bare.mkdir()
    rc._scrub_bare_fetch_head(bare, task_id="t1")  # 不抛即为通过


def test_fetch_bare_invokes_scrub(tmp_path, cache_dir, monkeypatch):
    """_fetch_bare 成功后必须调清洗钩子(集成侧的锁定,不真跑 git)"""
    origin = _make_origin(tmp_path / "origin")
    bare = rc.ensure_bare_cache(_url(origin), task_id="t1")
    assert bare is not None

    calls: list[str] = []
    monkeypatch.setattr(rc, "_scrub_bare_fetch_head",
                         lambda d, tid: calls.append(str(d)))
    assert rc._fetch_bare(bare, _url(origin), git_tokens={}, task_id="t2") is True
    assert calls == [str(bare)]


# ============================================================
# fetch TTL
# ============================================================


def test_fetch_ttl_window_skips_fetch(tmp_path, cache_dir, monkeypatch):
    """命中后未超 TTL 不 fetch;meta 中 last_fetch_at 过期后 fetch 一次。"""
    origin = _make_origin(tmp_path / "origin")
    bare = rc.ensure_bare_cache(_url(origin), task_id="t1")
    assert bare is not None

    fetch_calls: list[int] = []
    monkeypatch.setattr(rc, "_fetch_bare", lambda *a, **kw: fetch_calls.append(1) or True)

    # TTL 内:不 fetch
    rc.ensure_bare_cache(_url(origin), task_id="t2")
    assert fetch_calls == []

    # 把 last_fetch_at 拨到过期 → fetch
    meta_path = bare.parent / rc._META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["last_fetch_at"] = time.time() - settings.REPO_CACHE_FETCH_TTL - 10
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    rc.ensure_bare_cache(_url(origin), task_id="t3")
    assert len(fetch_calls) == 1


# ============================================================
# 分支存在性
# ============================================================


def test_missing_branch_forces_fetch_then_none(tmp_path, cache_dir, monkeypatch):
    """缓存无该分支:强制 fetch;仍缺 → 返回 None(调用方降级原链)。"""
    origin = _make_origin(tmp_path / "origin")
    bare = rc.ensure_bare_cache(_url(origin), task_id="t1")
    assert bare is not None

    fetch_calls: list[int] = []
    monkeypatch.setattr(rc, "_fetch_bare", lambda *a, **kw: fetch_calls.append(1) or True)
    # fetch 假成功但 dev 分支确实不存在 → None
    assert rc.ensure_bare_cache(_url(origin), branch="dev", task_id="t2") is None
    assert len(fetch_calls) == 1  # 分支缺失触发了强制 fetch


def test_new_branch_real_fetch_hits(tmp_path, cache_dir):
    """远端新推分支:真实 fetch(本地路径可作 fetch 源)后命中。"""
    origin = _make_origin(tmp_path / "origin")
    # 先建缓存(此时 origin 只有 main)
    bare = rc.ensure_bare_cache(_url(origin), branch="main", task_id="t1")
    assert bare is not None
    # 之后"远端"新推 dev 分支
    _git(origin, "checkout", "-b", "dev")
    (origin / "dev.txt").write_text("dev\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "dev")
    # 缓存无 dev → 强制真实 fetch → 命中
    assert rc.ensure_bare_cache(_url(origin), branch="dev", task_id="t2") is not None
    # 真不存在的分支:fetch 后仍缺 → None
    assert rc.ensure_bare_cache(_url(origin), branch="no-such", task_id="t3") is None


def test_tag_branch_hits(tmp_path, cache_dir):
    """--branch 也接受 tag:bare 克隆自带 tags,ensure(tag) 不返回 None。"""
    origin = _make_origin(tmp_path / "origin")
    _git(origin, "tag", "v1.0")
    bare = rc.ensure_bare_cache(_url(origin), branch="v1.0", task_id="t1")
    assert bare is not None


# ============================================================
# 损坏重建
# ============================================================


def test_corrupted_cache_rebuilds(tmp_path, cache_dir, monkeypatch):
    """健康检查失败 → 删缓存重建(重建后 bare 可用)。"""
    origin = _make_origin(tmp_path / "origin")
    bare = rc.ensure_bare_cache(_url(origin), task_id="t1")
    assert bare is not None

    builds: list[int] = []
    orig_build = rc._build_bare_cache

    def _count_build(*a, **kw):
        builds.append(1)
        return orig_build(*a, **kw)

    monkeypatch.setattr(rc, "_build_bare_cache", _count_build)
    monkeypatch.setattr(rc, "_is_healthy_bare", lambda p: False)

    bare2 = rc.ensure_bare_cache(_url(origin), task_id="t2")
    assert len(builds) == 1  # 触发了一次重建
    assert bare2 is not None
    # 重建后健康检查恢复 → 正常命中(不再重建)
    monkeypatch.setattr(rc, "_is_healthy_bare", lambda p: True)
    assert rc.ensure_bare_cache(_url(origin), task_id="t3") is not None


# ============================================================
# clone_from_cache
# ============================================================


def test_clone_from_cache_checks_out_branch(tmp_path, cache_dir):
    """本地克隆:默认分支检出 a.txt;指定分支检出对应内容。"""
    origin = _make_origin(tmp_path / "origin")
    _git(origin, "checkout", "-b", "dev")
    (origin / "a.txt").write_text("dev-content\n", encoding="utf-8")
    _git(origin, "commit", "-am", "dev change")
    _git(origin, "checkout", "-")

    bare = rc.ensure_bare_cache(_url(origin), branch="main", task_id="t1")
    dest = tmp_path / "ws_main"
    rc.clone_from_cache(bare, dest)
    assert (dest / "a.txt").read_text(encoding="utf-8") == "hello\n"

    dest_dev = tmp_path / "ws_dev"
    rc.clone_from_cache(bare, dest_dev, branch="dev")
    assert (dest_dev / "a.txt").read_text(encoding="utf-8") == "dev-content\n"


# ============================================================
# LRU 淘汰
# ============================================================


def _mk_entry(root: Path, key: str, last_used: float, size_bytes: int) -> Path:
    entry = root / key
    entry.mkdir(parents=True)
    (entry / "data.bin").write_bytes(b"x" * size_bytes)
    (entry / rc._META_NAME).write_text(
        json.dumps({"url": key, "last_used_at": last_used}), encoding="utf-8",
    )
    return entry


def test_evict_lru_removes_oldest_keeps_recent(tmp_path, cache_dir, monkeypatch):
    """超限:按 last_used 淘汰最旧;1h 内用过的受保护跳过。"""
    root = cache_dir
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    old = _mk_entry(root, "old", now - 2 * 3600, 2000)
    recent = _mk_entry(root, "recent", now - 300, 2000)  # 5 分钟前用过,受保护
    # 上限 1e-6 GB ≈ 1073 字节:总量 4000 超限
    monkeypatch.setattr(settings, "REPO_CACHE_MAX_GB", 1e-6)

    rc._evict_lru()

    assert not old.exists(), "最旧条目应被淘汰"
    assert recent.exists(), "1h 内用过的条目受保护不被淘汰"
