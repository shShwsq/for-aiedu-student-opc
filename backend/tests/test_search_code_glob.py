"""search_code 的 file_glob 匹配口径回归测试(local 模式)

实测一次审查里 agent2 调 `search_code(pattern="JWT_SECRET|…", file_glob="backend/app/*.py")`
返回 0 匹配:local 模式只拿**文件名**去 fnmatch(`config.py` 永远不等于
`backend/app/*.py`),而 sandbox 模式的 `rg --glob` 能命中——两种运行模式口径不一致,
模型白烧一次取证额度还拿到空结果。现按"文件名 ∪ 仓库相对路径"匹配,并在
带路径的 glob 空结果时回一句自检提示。
"""
from app.tools.sandbox_tools import _empty_glob_hint, _glob_matches, _search_code_local


def _seed(tmp_path):
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "config.py").write_text(
        "JWT_SECRET = 'change_me'\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "tests" / "test_config.py").write_text(
        "JWT_SECRET = 'test'\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("JWT_SECRET in docs\n", encoding="utf-8")
    return str(tmp_path)


def test_glob_matches_basename_and_relative_path():
    assert _glob_matches("*.py", "config.py", "backend/app/config.py")
    # 带路径前缀:旧实现这条永远 False
    assert _glob_matches("backend/app/*.py", "config.py", "backend/app/config.py")
    assert not _glob_matches("backend/tests/*.py", "config.py", "backend/app/config.py")
    # Windows 相对路径分隔符也按 posix 口径比
    assert _glob_matches("backend/app/*.py", "config.py", "backend\\app\\config.py")


def test_local_search_with_path_prefixed_glob_finds_the_file(tmp_path):
    repo = _seed(tmp_path)
    res = _search_code_local(
        repo, "JWT_SECRET", "backend/app/*.py", False, 50, 0, "content", 0,
    )
    assert res["total_matches"] == 1
    assert res["matches"][0]["file"].replace("\\", "/") == "backend/app/config.py"
    assert "hint" not in res  # 命中了就不塞提示


def test_local_search_with_plain_glob_still_works(tmp_path):
    repo = _seed(tmp_path)
    res = _search_code_local(repo, "JWT_SECRET", "*.py", False, 50, 0, "content", 0)
    files = {m["file"].replace("\\", "/") for m in res["matches"]}
    assert files == {"backend/app/config.py", "backend/tests/test_config.py"}


def test_local_search_empty_path_glob_returns_self_check_hint(tmp_path):
    """glob 含路径却 0 匹配 → 明确告诉模型怎么改,别让一次取证无声失败"""
    repo = _seed(tmp_path)
    res = _search_code_local(repo, "JWT_SECRET", "src/*.py", False, 50, 0, "content", 0)
    assert res["total_matches"] == 0
    assert "hint" in res and "file_glob" in res["hint"]


def test_empty_glob_hint_is_quiet_when_it_should_be():
    assert _empty_glob_hint(None, 0) is None          # 没写 glob
    assert _empty_glob_hint("*.py", 0) is None         # 有匹配可能,不啰嗦
    assert _empty_glob_hint("a/b/*.py", 3) is None     # 命中了
    assert _empty_glob_hint("a/b/*.py", 0) is not None


def test_local_search_files_with_matches_mode_carries_hint(tmp_path):
    repo = _seed(tmp_path)
    res = _search_code_local(
        repo, "JWT_SECRET", "nope/*.py", False, 50, 0, "files_with_matches", 0,
    )
    assert res["files"] == []
    assert "hint" in res
