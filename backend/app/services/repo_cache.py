"""bare 仓库缓存:同一仓库跨任务复用克隆,fetch --prune 增量更新。

设计(见 .trae/documents/仓库缓存与diff持久化实施计划.md):
- 首次访问:`git clone --bare` 全量入缓存(一次性成本,depth 恒为 0 换复用);
  成功后立即把 remote.origin.url 重写为匿名 URL —— token 绝不落盘(安全核心)。
- 再次访问:距上次 fetch 超 REPO_CACHE_FETCH_TTL 才 `git fetch --prune` 增量更新
  (fetch 用带 token 的一次性 URL 参数,不写入 config,失败仅 warning 用旧缓存)。
- 指定 branch 缓存中无该 ref 时无视 TTL 强制 fetch 一次,仍缺返回 None(降级)。
- 缓存损坏(rev-parse 校验失败)→ 删缓存重建。
- 任何异常返回 None,调用方降级原远程克隆链 —— 缓存永不阻塞任务。
- LRU:总大小超 REPO_CACHE_MAX_GB 按 last_used_at 淘汰最旧(1h 内用过的跳过,
  防与进行中克隆竞争),60s 节流后台线程执行。

缓存布局:{REPO_CACHE_DIR}/{key前16位}/{repo_name}.git(裸仓库)+ meta.json。
git 命令一律 argv 列表 + subprocess(不经 shell,规避 Windows cmd.exe 引号问题)。
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app.config import settings
from app.git_provider import get_provider_for_url

logger = logging.getLogger(__name__)

# 元数据文件名(存于条目目录,记录 url/created_at/last_fetch_at/last_used_at)
_META_NAME = "meta.json"

# per-repo 锁:串行化同一仓库的缓存构建/fetch(进程内;单 worker 部署假设)
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()

# LRU 淘汰节流
_EVICT_INTERVAL = 60.0
_last_evict = 0.0
_evict_lock = threading.Lock()
# 淘汰保护窗口:1h 内使用过的条目不淘汰(防与进行中的克隆/挂载竞争)
_EVICT_MIN_AGE = 3600.0

# 短 git 子命令超时(fetch 大仓库仍用 REPO_CLONE_TIMEOUT)
_GIT_SHORT_TIMEOUT = 30

# 剥 http(s) URL 的 userinfo(与 sandbox_tools._URL_USERINFO_RE 同口径):
# 用于 fetch 之后清洗 bare/FETCH_HEAD —— git 会把 "… of <带 token URL>" 写进去,
# 而 bare 目录在 REPO_CACHE_SANDBOX_ENABLED=True 时只读挂载进容器,agent cat 即得
_URL_USERINFO_RE = re.compile(r"(\bhttps?://)[^/\s@]+@", re.IGNORECASE)


def _strip_url_userinfo(text: str) -> str:
    """抹掉文本里所有 http(s) URL 的 userinfo(仅 bare 缓存清洗用)"""
    return _URL_USERINFO_RE.sub(r"\1", text)


def _scrub_bare_fetch_head(bare_dir: Path, task_id: str) -> None:
    """fetch 后清 bare 仓库根的 FETCH_HEAD 里的 token userinfo

    `git fetch <url>` 会写 FETCH_HEAD "… of <url>",带 token 的 fetch_url 因此落盘。
    初次 `_build_bare_cache` 用 `git clone --bare` 不写 FETCH_HEAD,只有增量 fetch
    才有这个残留。清洗失败仅 warning,不阻塞(缓存可用性优先,单条 fetch 泄漏面已知)。
    """
    fetch_head = bare_dir / "FETCH_HEAD"
    if not fetch_head.is_file():
        return
    try:
        text = fetch_head.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning(f"[repo_cache] task={task_id} 读 FETCH_HEAD 失败(跳过清洗): {e}")
        return
    cleaned = _strip_url_userinfo(text)
    if cleaned == text:
        return
    try:
        # newline="" 保 LF:Windows 默认翻译会把整个 FETCH_HEAD 改成 CRLF,
        # 破坏 git 侧的字节口径也污染下游 diff
        fetch_head.write_text(cleaned, encoding="utf-8", newline="")
    except OSError as e:
        logger.warning(f"[repo_cache] task={task_id} 写 FETCH_HEAD 失败(凭证残留): {e}")
        return
    # 复查:写不进去或只读属性(Windows pack)暴露在这
    try:
        residual = fetch_head.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if _URL_USERINFO_RE.search(residual):
        logger.error(f"[repo_cache] task={task_id} FETCH_HEAD 清洗后仍含凭证 userinfo")


def _run_git(argv: list[str], timeout: int | None = None) -> str:
    """宿主机执行 git(argv 列表,不经 shell),非零退出抛 RuntimeError"""
    try:
        proc = subprocess.run(
            ["git"] + argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or settings.REPO_CLONE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"git {' '.join(argv[:3])} 超时({timeout}s)") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-300:]
        raise RuntimeError(f"git 命令失败({argv[0]}): {err.strip()}")
    return proc.stdout or ""


def force_rmtree(path: Path | str, retries: int = 3) -> bool:
    """删除 git 仓库类目录:Windows 下 pack 文件只读,普通 rmtree 会静默残留。

    onexc 里 chmod 去只读后重试删除;瞬时锁(AV/索引器)按 retries 次退避重试。
    返回是否删除干净(目标不存在)。
    """
    import stat

    target = Path(path)
    if not target.exists():
        return True

    def _onexc(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass  # 瞬时锁:外层整体重试

    for _ in range(retries):
        try:
            shutil.rmtree(target, onexc=_onexc)
        except Exception:
            pass
        if not target.exists():
            return True
        time.sleep(0.2)
    return not target.exists()


def normalize_repo_url(repo_url: str) -> str:
    """归一化 URL 作为缓存键基础:统一 HTTPS 形态、去凭据/token、去 .git 后缀

    已知 provider(github/gitee)先转 HTTPS;未知主机仅去凭据与后缀。
    token URL 与匿名 URL 必须归一为同键(缓存以匿名形态存在)。
    """
    provider = get_provider_for_url(repo_url)
    url = provider.to_https_url(repo_url) if provider else repo_url
    # 去凭据 https://user:token@host/... -> https://host/...
    if "://" in url:
        scheme, rest = url.split("://", 1)
        authority = rest.split("/", 1)[0]
        if "@" in authority:
            url = f"{scheme}://{authority.split('@', 1)[1]}{rest[len(authority):]}"
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url


def cache_key(repo_url: str) -> str:
    """缓存键:归一化 URL 的 sha256 前 16 位(目录名)"""
    return hashlib.sha256(normalize_repo_url(repo_url).encode("utf-8")).hexdigest()[:16]


def _extract_repo_name(repo_url: str) -> str:
    """从 URL/路径提取仓库名(与 clone_repo_with_fallback 同规则,兼容 \\ 分隔的本地路径)"""
    match = re.search(r"[/\\]([^/\\]+?)(?:\.git)?$", repo_url)
    return match.group(1) if match else ""


def _cache_root() -> Path:
    root = Path(settings.REPO_CACHE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_paths(key: str, repo_name: str) -> tuple[Path, Path]:
    """返回 (bare 目录, meta 路径):{root}/{key}/{repo_name}.git + {root}/{key}/meta.json"""
    entry_dir = _cache_root() / key
    return entry_dir / f"{repo_name}.git", entry_dir / _META_NAME


def _repo_lock(key: str) -> threading.Lock:
    with _repo_locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[key] = lock
        return lock


def _read_meta(meta_path: Path) -> dict:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_meta(meta_path: Path, meta: dict) -> None:
    """原子写(temp + os.replace,防半成品)"""
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_path.with_name(_META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, meta_path)


def _candidate_urls(repo_url: str, git_tokens: dict) -> tuple[list[str], str]:
    """构造克隆候选链(HTTPS+token → SSH → 匿名,去重),返回 (候选列表, 匿名 URL)

    与 clone_repo_with_fallback 同规则;匿名 URL 用于缓存 remote.origin.url 重写。
    """
    provider = get_provider_for_url(repo_url)
    token = git_tokens.get(provider.id, "") if provider else ""
    if provider:
        https_anon = provider.to_https_url(repo_url)
        ssh_url = provider.to_ssh_url(repo_url)
        https_with_token = (
            provider.inject_token_in_https(https_anon, token) if token else ""
        )
    else:
        https_anon = repo_url
        ssh_url = repo_url
        https_with_token = ""
    candidates: list[str] = []
    for u in [https_with_token, ssh_url, https_anon]:
        if u and u not in candidates:
            candidates.append(u)
    return candidates, https_anon


def _safe_url(url: str) -> str:
    """日志脱敏:去 token"""
    return url.split("@")[-1] if "@" in url else url


def _build_bare_cache(
    repo_url: str,
    key: str,
    repo_name: str,
    git_tokens: dict,
    bare_dir: Path,
    meta_path: Path,
    task_id: str,
) -> None:
    """缓存未命中:候选链 git clone --bare 到临时目录,成功后 rename 入位

    安全核心:成功后立即把 remote.origin.url 重写为匿名 URL,并校验 config
    全文不含 token(token 绝不落盘)。
    """
    candidates, https_anon = _candidate_urls(repo_url, git_tokens)
    entry_dir = bare_dir.parent
    entry_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"bare_tmp_{key}_", dir=entry_dir))
    try:
        bare_tmp = tmp_dir / f"{repo_name}.git"
        errors: list[str] = []
        cloned = False
        for url in candidates:
            try:
                logger.info(
                    f"[repo_cache] task={task_id} bare 缓存未命中,"
                    f"全量克隆({_safe_url(url)})"
                )
                _run_git(["clone", "--bare", url, str(bare_tmp)])
                cloned = True
                break
            except Exception as e:
                errors.append(f"[{_safe_url(url)}] {str(e)[:200]}")
        if not cloned:
            raise RuntimeError("; ".join(errors))

        # token 绝不落盘:重写 remote.origin.url 为匿名 URL(先于任何后续操作)
        _run_git(
            ["-C", str(bare_tmp), "remote", "set-url", "origin", https_anon],
            timeout=_GIT_SHORT_TIMEOUT,
        )
        # 防御校验:config 全文不得含 token
        token_values = [
            v for v in git_tokens.values() if isinstance(v, str) and len(v) >= 8
        ]
        config_text = (bare_tmp / "config").read_text(encoding="utf-8", errors="replace")
        for tv in token_values:
            if tv in config_text:
                raise RuntimeError("安全检查失败:bare config 含 token,拒绝入缓存")

        # rename 入位(Windows:目标存在需先删——git pack 只读,用 force_rmtree;
        # 删不干净时抛错,ensure 捕获后降级远程克隆,不阻塞任务)
        if bare_dir.exists() and not force_rmtree(bare_dir):
            raise RuntimeError(f"旧缓存目录无法删除: {bare_dir}")
        os.replace(bare_tmp, bare_dir)

        now = time.time()
        _write_meta(
            meta_path,
            {
                "url": normalize_repo_url(repo_url),
                "created_at": now,
                "last_fetch_at": now,
                "last_used_at": now,
            },
        )
        logger.info(f"[repo_cache] task={task_id} bare 缓存已建立: {key}/{repo_name}.git")
    finally:
        force_rmtree(tmp_dir)


def _fetch_bare(
    bare_dir: Path,
    repo_url: str,
    git_tokens: dict,
    task_id: str,
) -> bool:
    """增量 fetch(--prune 清理已删远端分支)。显式 URL + refspec:
    - remote.origin.url 已重写为匿名,私有仓库必须用带 token 的一次性 URL fetch
      (URL 作参数不写入 config;但 git 会把 fetch URL 记进 FETCH_HEAD,详见下)
    - 显式 refspec 才会更新 refs/heads(fetch URL 时不自动用 remote 配置)

    成功后立即清洗 bare/FETCH_HEAD 的 userinfo —— 不洗的话在
    REPO_CACHE_SANDBOX_ENABLED=True 部署下,agent 在容器里 `cat <mount>/FETCH_HEAD`
    就能拿到 token。清洗失败也不回退 fetch 结果(缓存可用性优先)。

    失败仅 warning 返回 False,调用方继续用旧缓存(缓存永不阻塞任务)。
    """
    candidates, https_anon = _candidate_urls(repo_url, git_tokens)
    fetch_url = candidates[0] if candidates else https_anon
    try:
        _run_git(
            [
                "-C", str(bare_dir),
                "fetch", "--prune", fetch_url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ],
            timeout=settings.REPO_CLONE_TIMEOUT,
        )
        _scrub_bare_fetch_head(bare_dir, task_id)
        meta_path = bare_dir.parent / _META_NAME
        meta = _read_meta(meta_path)
        meta["last_fetch_at"] = time.time()
        _write_meta(meta_path, meta)
        return True
    except Exception as e:
        logger.warning(
            f"[repo_cache] task={task_id} fetch 增量更新失败,继续用旧缓存({_safe_url(str(e))[:200]})"
        )
        return False


def _has_ref(bare_dir: Path, ref: str) -> bool:
    """缓存中是否存在分支/tag(--branch 两者都接受)"""
    for ref_type in ("heads", "tags"):
        try:
            _run_git(
                ["-C", str(bare_dir), "rev-parse", "--verify", "--quiet", f"refs/{ref_type}/{ref}"],
                timeout=_GIT_SHORT_TIMEOUT,
            )
            return True
        except RuntimeError:
            continue
    return False


def _is_healthy_bare(bare_dir: Path) -> bool:
    """健康检查:是合法 bare 仓库(rev-parse --is-bare-repository 输出 true)

    不用 rev-parse HEAD:空仓库 HEAD 不可解析,会被误判为损坏导致无限重建。
    """
    try:
        out = _run_git(
            ["-C", str(bare_dir), "rev-parse", "--is-bare-repository"],
            timeout=_GIT_SHORT_TIMEOUT,
        ).strip()
        return out == "true"
    except Exception:
        return False


def ensure_bare_cache(
    repo_url: str,
    branch: str | None = None,
    git_tokens: dict | None = None,
    task_id: str = "",
) -> Path | None:
    """确保 bare 缓存就绪,返回 bare 目录路径;None = 缓存不可用(调用方降级原克隆链)

    逻辑(全程 per-repo 锁):
    1. 未命中 → _build_bare_cache 全量建立
    2. 命中但损坏 → 删缓存重建
    3. 命中且健康:
       - 指定 branch 且缓存无该 ref → 无视 TTL 强制 fetch,仍缺则返回 None
         (新推送分支场景;None 走原链的 branch_attempts 兜底)
       - 指定 branch 缓存已有,或未指定 branch,但距上次 fetch 超 TTL → 增量 fetch
         (失败继续用旧缓存)
    4. 更新 last_used_at(LRU 依据),节流触发 LRU 淘汰
    """
    git_tokens = git_tokens or {}
    try:
        key = cache_key(repo_url)
        repo_name = _extract_repo_name(repo_url)
        if not repo_name:
            return None
        bare_dir, meta_path = _cache_paths(key, repo_name)
        with _repo_lock(key):
            if not bare_dir.exists():
                _build_bare_cache(
                    repo_url, key, repo_name, git_tokens, bare_dir, meta_path, task_id
                )
            elif not _is_healthy_bare(bare_dir):
                logger.warning(
                    f"[repo_cache] task={task_id} 缓存损坏,重建: {key}/{repo_name}.git"
                )
                force_rmtree(bare_dir)
                _build_bare_cache(
                    repo_url, key, repo_name, git_tokens, bare_dir, meta_path, task_id
                )
            else:
                now = time.time()
                meta = _read_meta(meta_path)
                branch_missing = branch and not _has_ref(bare_dir, branch)
                if branch_missing:
                    # 新分支场景:强制 fetch 一次;仍缺说明远端真没有(或 fetch 失败)
                    if not _fetch_bare(bare_dir, repo_url, git_tokens, task_id):
                        return None
                    if not _has_ref(bare_dir, branch):
                        return None
                elif now - meta.get("last_fetch_at", 0.0) > settings.REPO_CACHE_FETCH_TTL:
                    _fetch_bare(bare_dir, repo_url, git_tokens, task_id)
            # 更新 last_used_at
            meta = _read_meta(meta_path)
            meta["last_used_at"] = time.time()
            _write_meta(meta_path, meta)
        _evict_lru_throttled()
        return bare_dir
    except Exception as e:
        logger.warning(
            f"[repo_cache] task={task_id} 缓存不可用,降级全量克隆: {str(e)[:300]}"
        )
        return None


def clone_from_cache(bare_path: Path | str, dest: Path | str, branch: str | None = None) -> Path:
    """从 bare 缓存本地克隆到 dest(秒级;主链路由 _clone_repo_local 复用,此函数供测试/独立场景)

    本地克隆 git 自动 hardlink 复用对象(跨卷自动降级复制);不用 --depth
    (本地克隆对 depth 仅告警且无意义)。
    """
    argv = ["clone"]
    if branch:
        argv += ["--branch", branch]
    argv += [str(bare_path), str(dest)]
    _run_git(argv, timeout=settings.REPO_CLONE_TIMEOUT)
    return Path(dest)


def sandbox_mount(repo_url: str) -> tuple[str, str] | None:
    """sandbox 模式挂载描述:(Server 宿主机 bare 路径, 容器内挂载点) | None

    安全设计:只挂载本任务仓库自己的 bare 子目录(非缓存根),readOnly ——
    容器读不到其他用户/其他仓库;bare remote URL 已是匿名形态,无 token 泄漏面。
    """
    if not (
        settings.REPO_CACHE_SANDBOX_ENABLED and settings.REPO_CACHE_SANDBOX_HOST_DIR
    ):
        return None
    key = cache_key(repo_url)
    repo_name = _extract_repo_name(repo_url)
    if not repo_name:
        return None
    host_dir = str(
        Path(settings.REPO_CACHE_SANDBOX_HOST_DIR) / key / f"{repo_name}.git"
    )
    mount_path = f"/home/user/repo-cache/{repo_name}.git"
    return host_dir, mount_path


# ============================================================
# LRU 淘汰
# ============================================================


def _evict_lru_throttled() -> None:
    """节流触发 LRU 淘汰(60s 最多一次),rmtree 大目录丢给后台线程不阻塞请求"""
    global _last_evict
    with _evict_lock:
        if time.time() - _last_evict < _EVICT_INTERVAL:
            return
        _last_evict = time.time()
    threading.Thread(target=_evict_lru, name="repo-cache-evict", daemon=True).start()


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _evict_lru() -> None:
    """按 last_used_at 淘汰最旧直至总大小 ≤ REPO_CACHE_MAX_GB

    跳过 _EVICT_MIN_AGE 内使用过的条目(防与进行中克隆/挂载竞争)。
    单条目删除失败跳过,不影响其余。
    """
    try:
        root = _cache_root()
        entries: list[tuple[Path, float, int]] = []
        total = 0
        for entry_dir in root.iterdir():
            if not entry_dir.is_dir():
                continue
            try:
                meta = _read_meta(entry_dir / _META_NAME)
                size = _dir_size(entry_dir)
            except Exception:
                continue
            total += size
            entries.append((entry_dir, meta.get("last_used_at", 0.0), size))
        limit = int(settings.REPO_CACHE_MAX_GB * (1024**3))
        if total <= limit:
            return
        now = time.time()
        for entry_dir, last_used, size in sorted(entries, key=lambda x: x[1]):
            if total <= limit:
                break
            if now - last_used < _EVICT_MIN_AGE:
                continue
            try:
                if not force_rmtree(entry_dir):
                    logger.warning(
                        f"[repo_cache] 淘汰 {entry_dir.name} 未删净(跳过)"
                    )
                    continue
                total -= size
                logger.info(f"[repo_cache] LRU 淘汰缓存条目: {entry_dir.name}")
            except Exception as e:
                logger.warning(
                    f"[repo_cache] 淘汰 {entry_dir.name} 失败(跳过): {e}"
                )
    except Exception as e:
        logger.warning(f"[repo_cache] LRU 淘汰执行失败(忽略): {e}")
