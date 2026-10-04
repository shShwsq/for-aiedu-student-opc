"""出题 job 事件流(deque 左端裁剪 + 增量重放)单元测试

覆盖:
- 事件序号连续递增,read_events 按 after_seq 增量返回
- token 文本超上限时从队列左端裁剪,留存事件仍是连续后缀
  (旧写法在锁内 next()+list.remove() 找第一个 token 事件再删,
  O(n) 扫描且会挖空中间项)
- 非 token 事件不计入 token 字符预算
- job 终态自动追加 done/error 事件,等待中的读端被唤醒
"""
from app.services.practice import jobs


def _seqs(job_id, user_id="u1"):
    snap = jobs.read_events(job_id, user_id, after_seq=0)
    return [e["seq"] for e in snap["events"]]


def test_events_are_sequential_and_replayed_incrementally():
    job_id = jobs.create_job("u1")
    jobs.append_event(job_id, "finding", {"index": 1, "total": 3, "title": "T"})
    jobs.append_event(job_id, "token", {"delta": "hello "})
    jobs.append_event(job_id, "tool", {"name": "read_file", "summary": "read_file: a.py"})

    first = jobs.read_events(job_id, "u1", after_seq=0)
    assert [e["type"] for e in first["events"]] == ["finding", "token", "tool"]
    last_seq = first["events"][-1]["seq"]

    # 已读过的序号之后再无事件
    jobs.append_event(job_id, "token", {"delta": "world"})
    second = jobs.read_events(job_id, "u1", after_seq=last_seq)
    assert [e["type"] for e in second["events"]] == ["token"]
    assert second["events"][0]["seq"] == last_seq + 1


def test_token_trim_keeps_contiguous_suffix():
    """超预算后从左端裁剪:留存事件必须仍是连续序号(读端按后缀重放)"""
    job_id = jobs.create_job("u1")
    jobs.append_event(job_id, "finding", {"index": 1, "total": 1, "title": "T"})
    # 每块 20k 字符,推 5 块 → 100k > 64k 预算,必然触发裁剪
    for _ in range(5):
        jobs.append_event(job_id, "token", {"delta": "x" * 20000})

    seqs = _seqs(job_id)
    assert seqs, "裁剪不应清空事件流"
    # 连续后缀:相邻序号差恒为 1
    assert all(b - a == 1 for a, b in zip(seqs, seqs[1:]))
    assert seqs[-1] == 6  # 1 条 finding + 5 条 token 的最大序号仍在
    snap = jobs.snapshot(job_id, "u1")
    assert snap["recent_text"].endswith("x" * 100)


def test_structural_events_do_not_consume_token_budget():
    """progress/done 等结构事件不进 token 字符预算"""
    job_id = jobs.create_job("u1")
    for _ in range(50):
        jobs.update_job(job_id, done=1, total=3)  # 每次追加一条 progress 事件
    with jobs._LOCK:
        job = jobs._JOBS[job_id]
        assert job["event_token_chars"] == 0
        assert len(job["events"]) == 50


def test_terminal_status_appends_done_event_and_wakes_reader():
    job_id = jobs.create_job("u1")
    jobs.update_job(job_id, status="done", created_count=2, skipped_findings=1)
    res = jobs.read_events(job_id, "u1", after_seq=0, timeout=0)
    assert res["events"][-1]["type"] == "done"
    assert res["job"]["status"] == "done"

    err_job = jobs.create_job("u1")
    jobs.update_job(err_job, status="error", error="额度耗尽")
    res2 = jobs.read_events(err_job, "u1", after_seq=0, timeout=0)
    assert res2["events"][-1]["type"] == "error"
    assert res2["events"][-1]["data"]["message"] == "额度耗尽"


def test_events_after_terminal_are_ignored():
    """终态后不再追加流式事件(避免 SSE 收尾后又被灌内容)"""
    job_id = jobs.create_job("u1")
    jobs.update_job(job_id, status="done")
    before = _seqs(job_id)
    jobs.append_event(job_id, "token", {"delta": "late"})
    assert _seqs(job_id) == before


def test_read_events_isolates_users():
    job_id = jobs.create_job("u1")
    jobs.append_event(job_id, "token", {"delta": "secret"})
    assert jobs.read_events(job_id, "u2", after_seq=0) is None
    assert jobs.snapshot(job_id, "u2") is None
