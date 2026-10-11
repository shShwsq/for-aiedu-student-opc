"""沙箱会话并发验证冒烟脚本(连真实 OpenSandbox Server,不进 CI)

背景:`CLI冷启动提速实施计划.md` §1.5(PR-1.5)要把 `_ensure_cli_env` / 凭证注入也挪进
prewarm 线程,与主线程的 `clone_repo_with_fallback`(一条长沙箱命令)并发打同一个
`SandboxSession`。前提是「execd 的命令层支持跨线程并发」。这个前提没验证过,本脚本验证它。

验证五件事:
A. 长前台命令(`sleep N`)在跑时,其他线程仍能正常发前台命令 / 写文件 / 读文件
B. 前台命令与后台命令(`run_command_background` + `get_background_logs` 轮询)互不串话
   (各自的 marker 不混进对方的输出,顺序不乱)
C. 两条前台命令并发的墙钟时间 ≈ max(单条),而不是 ≈ 两条之和 → 证明没有排队串行
D. 长命令在跑时可拿 `get_endpoint` 并 HTTP 命中沙箱内端口(PR-1 HTTP-only 路径的旁证)
E. 并发中 `interrupt_command` 只中断目标后台命令,不误伤在跑的前台命令

用法(在有 SANDBOX_MODE=sandbox + SANDBOX_SERVER_URL 的环境里):
    python backend/scripts/smoke_sandbox_concurrency.py
    python backend/scripts/smoke_sandbox_concurrency.py --hold 25 --with-clone
退出码:全 PASS 为 0,任一 FAIL 为 1。脚本自己建沙箱、自己销毁,不碰 `_sessions`。
"""
import argparse
import sys
import threading
import time
from pathlib import Path

# 脚本在 backend/scripts/ 下,把 backend 根加入 sys.path 以便 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.sandbox.client import create_sandbox

FAILURES: list[str] = []
HTTP_PORT = 18088  # 避开 ACP bridge 的 8088(虽然每容器独立端口空间,仍留余量)


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        FAILURES.append(name)


def timed(fn):
    """包装执行并返回 (耗时, 结果);异常原样上抛"""
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


def main() -> int:
    ap = argparse.ArgumentParser(description="沙箱会话并发验证(需真实 OpenSandbox Server)")
    ap.add_argument("--hold", type=int, default=20, help="长命令占用秒数(默认 20)")
    ap.add_argument("--with-clone", action="store_true",
                    help="额外用真实 git clone 当长命令(更接近预克隆现场,受网络影响)")
    ap.add_argument("--clone-url", default="https://github.com/facebook/react.git",
                    help="--with-clone 使用的匿名可克隆仓库地址(需几秒以上才能拉完,才能压到并发)")
    args = ap.parse_args()

    if settings.SANDBOX_MODE != "sandbox":
        print(f"[SKIP] SANDBOX_MODE={settings.SANDBOX_MODE},本脚本只验证真实沙箱。"
              f"请在 sandbox 模式的环境(如生产后端容器)里运行。")
        return 0

    print(f"[info] server={settings.SANDBOX_SERVER_URL} image={settings.SANDBOX_IMAGE} "
          f"hold={args.hold}s with_clone={args.with_clone}")
    t0 = time.perf_counter()
    session = create_sandbox()
    print(f"[info] 沙箱已创建,cost={time.perf_counter() - t0:.2f}s")

    # 结果槽:线程里只写自己的 key,主线程最后统一断言
    res: dict[str, object] = {}
    errs: dict[str, str] = {}
    # 每线程的实际起止时刻(monotonic):并发判据靠实测区间,不靠假设耗时
    spans: dict[str, tuple[float, float]] = {}

    def worker(key: str, fn) -> None:
        t_start = time.perf_counter()
        try:
            res[key] = fn()
            errs.setdefault(key, "")  # 成功也要占位,便于区分"崩了"与"没跑"
        except Exception as e:  # 记录而非抛出:一个线程崩了不能让整脚本没结论
            errs[key] = f"{type(e).__name__}: {e}"
        finally:
            spans[key] = (t_start, time.perf_counter())

    def overlap(a: str, b: str) -> float:
        """两个线程时间窗的重叠秒数(负数=完全错开)"""
        if a not in spans or b not in spans:
            return -1.0
        return min(spans[a][1], spans[b][1]) - max(spans[a][0], spans[b][0])

    long_cmd = (
        f"git clone --depth 1 {args.clone_url} /home/user/conc_clone >/dev/null 2>&1; "
        f"echo CLONE_EXIT=$?; rm -rf /home/user/conc_clone"
        if args.with_clone else
        f"echo LONG_START; sleep {args.hold}; echo LONG_DONE"
    )

    # ---- A/C:长命令线程(前台,占用 execd) ----
    def long_run():
        return timed(lambda: session.run_command(long_cmd, timeout=args.hold + 120))

    # ---- A:长命令期间的杂项前台命令 + 文件往返 ----
    def side_calls():
        outs = []
        for i in range(6):
            outs.append(session.run_command(f"echo SIDE-{i}", timeout=30).strip())
            session.write_file("/home/user/.conc_probe", f"payload-{i}")
            outs.append(session.read_file("/home/user/.conc_probe").strip())
            time.sleep(0.5)
        return outs

    # ---- B:后台命令启动 + 日志轮询 + 状态查询 ----
    def bg_run():
        cmd = 'i=1; while [ $i -le 10 ]; do echo "BG-$i"; i=$((i+1)); sleep 1; done'
        exec_id = session.run_command_background(cmd, work_dir="/home/user")
        marks: list[str] = []
        cursor = None
        deadline = time.time() + 40
        while time.time() < deadline:
            logs, cursor = session.get_background_logs(exec_id, cursor=cursor)
            for line in (logs or "").splitlines():
                line = line.strip()
                if line.startswith("BG-"):
                    marks.append(line)
            running, _ = session.get_command_status(exec_id)
            if not running and len(marks) >= 10:
                break
            time.sleep(0.5)
        return exec_id, marks

    # ---- D:长命令期间的端口转发 + HTTP ----
    def http_probe():
        srv_id = session.run_command_background(
            f"python3 -m http.server {HTTP_PORT}", work_dir="/home/user")
        time.sleep(2.0)
        url, headers = session.get_endpoint(HTTP_PORT)
        code = None
        try:
            import httpx
            with httpx.Client(headers=headers, timeout=10) as c:
                code = c.get(f"{url}/").status_code
        except Exception as e:
            errs["http_probe"] = f"{type(e).__name__}: {e}"
        finally:
            session.interrupt_command(srv_id)
        return code

    # ---- E:目标后台命令的中断 ----
    def interrupt_target():
        victim = session.run_command_background(
            "echo VICTIM_START; sleep 300; echo VICTIM_END", work_dir="/home/user")
        time.sleep(1.5)
        session.interrupt_command(victim)
        return victim

    threads = [
        threading.Thread(target=worker, args=("long", long_run), name="conc-long"),
        threading.Thread(target=worker, args=("side", side_calls), name="conc-side"),
        threading.Thread(target=worker, args=("bg", bg_run), name="conc-bg"),
        threading.Thread(target=worker, args=("http", http_probe), name="conc-http"),
        threading.Thread(target=worker, args=("victim", interrupt_target), name="conc-victim"),
    ]
    t_all = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=args.hold + 150)
    wall = time.perf_counter() - t_all
    long_cost = res["long"][0] if "long" in res else 0.0  # type: ignore[index]

    # ================= 断言 =================
    print()
    real_errs = {k: v for k, v in errs.items() if v}
    check("线程无异常", not real_errs, f"errors={real_errs}" if real_errs else "")

    out_long = res.get("long", (0, ""))[1]  # type: ignore[index]
    if args.with_clone:
        # 必须"真的克隆成功且够慢",否则长命令瞬间返回,并发压力等于没测
        check("A. 长命令(clone)成功退出", "CLONE_EXIT=0" in str(out_long),
              f"out={str(out_long)[:160]!r}")
    else:
        check("A. 长命令输出完整未截断",
              str(out_long).count("LONG_START") == 1 and str(out_long).count("LONG_DONE") == 1,
              f"out={str(out_long)[:120]!r}")

    side = res.get("side") or []
    # 同一线程内严格交错:SIDE-i 紧跟其后的 payload-i(read_file 回读 write_file)
    want_side = [v for i in range(6) for v in (f"SIDE-{i}", f"payload-{i}")]
    check("A. 并发前台命令/读写文件结果逐条正确", side == want_side,
          f"got_len={len(side)} 缺={sorted(set(want_side) - set(side))} "
          f"多余={sorted(set(side) - set(want_side))} got={side}")

    exec_id, marks = res.get("bg") or (None, [])
    check("B. 后台 marker 顺序完整且无串话",
          marks == [f"BG-{i}" for i in range(1, 11)],
          f"exec_id={str(exec_id)[:12]} got={marks}")
    check("B. 长命令与后台命令 id 不同",
          exec_id is not None and str(exec_id) != str(res.get("victim")),
          f"bg={str(exec_id)[:12]} victim={str(res.get('victim'))[:12]}")

    # 并发判据全部用实测区间,不假设单次往返耗时(那会随 Server 负载漂)
    side_cost = (spans["side"][1] - spans["side"][0]) if "side" in spans else 0.0
    serialized = long_cost + side_cost  # 真串行的下界:两条各自跑完
    ov_ls = overlap("long", "side")
    ov_lb = overlap("long", "bg")
    print(f"[info] wall={wall:.1f}s long={long_cost:.1f}s side={side_cost:.1f}s "
          f"重叠(long∩side)={ov_ls:.1f}s 重叠(long∩bg)={ov_lb:.1f}s "
          f"| 若 execd 串行,wall ≈ {serialized:.1f}s")
    check("C1. 长命令与杂项命令时间窗真重叠 ≥2s", ov_ls >= 2.0, f"overlap={ov_ls:.1f}s")
    check("C2. 墙钟省下≥可重叠部分的6成(未排队)",
          wall <= serialized - min(long_cost, side_cost) * 0.6,
          f"wall={wall:.1f}s ≤ {serialized - min(long_cost, side_cost) * 0.6:.1f}s")

    check("D. 长命令期间 HTTP 命中沙箱端口", res.get("http") == 200,
          f"status={res.get('http')}")

    victim_running, victim_exit = (True, None)
    try:
        victim_running, victim_exit = session.get_command_status(res["victim"])  # type: ignore[index]
    except Exception as e:
        errs["victim_status"] = f"{type(e).__name__}: {e}"
    check("E. interrupt 只杀目标后台命令", not victim_running and victim_exit is not None,
          f"running={victim_running} exit={victim_exit}")

    # 附带取样:单次命令往返延迟(未并发时),供"命令单价"口径复核
    timed(lambda: session.run_command("echo PING", timeout=30))
    rt = timed(lambda: session.run_command("echo PING", timeout=30))[0]
    print(f"[info] 空闲时单次 run_command 往返 ≈ {rt:.2f}s")

    print()
    print("=" * 62)
    if FAILURES:
        print(f"[RESULT] FAIL ({len(FAILURES)}): {', '.join(FAILURES)}")
        print("  → PR-1.5(整链路边克隆边热)前提不成立,预热线程保持 HTTP-only。")
        code = 1
    else:
        print("[RESULT] PASS")
        print("  → execd 命令层并发可用,PR-1.5 可排期;"
              "仍需保留会话级 try/except 与失败回落。")
        code = 0
    print("=" * 62)

    try:
        session.close()
        print("[info] 沙箱已销毁")
    except Exception as e:
        print(f"[warn] 销毁沙箱失败(请手工确认残留): {e}")
    return code


if __name__ == "__main__":
    sys.exit(main())
