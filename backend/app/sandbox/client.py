"""沙箱客户端封装

使用 OpenSandbox 官方同步 API(SandboxSync / ConnectionConfigSync),
对齐 react_agent 的同步循环(基于 OpenAI SDK 同步调用),无需 asyncio 包装。

两种模式:
- sandbox:连真实 OpenSandbox Server(部署在 Linux 服务器上),走 SandboxSync
- local:本地模式,不用沙箱,在宿主机文件系统直接执行,供开发/调试使用
  (LLM 生成的代码/命令在宿主机直接运行,无隔离边界,请勿用于生产)

对外接口(同步):
- create_sandbox() -> SandboxSession
- SandboxSession.run_command(cmd) -> str   返回 stdout
- SandboxSession.write_file(path, content)
- SandboxSession.read_file(path) -> str
- SandboxSession.close()

参考:https://github.com/alibaba/OpenSandbox
"""
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import settings

logger = logging.getLogger(__name__)


def _kill_bg_proc_tree(proc: subprocess.Popen) -> None:
    """终止后台进程及其整个子进程树(local 模式宿主机上运行时使用)

    Windows:taskkill /PID {pid} /T /F(terminate 只杀父进程,
    bridge spawn 的 CLI 子进程会成孤儿)
    POSIX:优先 killpg 杀进程组(配合 Popen 的 start_new_session),
    失败回退 terminate
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                if proc.poll() is None:
                    proc.terminate()
    except Exception:
        # 兜底:直接 terminate(可能留孤儿,但避免清理流程本身失败)
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass


# ============================================================
# 沙箱会话抽象
# ============================================================


class SandboxSession:
    """沙箱会话,封装对单个沙箱实例的操作

    所有方法都是同步的:sandbox 模式走 SandboxSync 同步 API,local 模式走本地文件系统
    """

    def __init__(self, mode: str, sandbox: Any = None, work_dir: str = "/home/user"):
        self.mode = mode
        self.sandbox = sandbox  # SandboxSync 对象(sandbox 模式)
        self.work_dir = work_dir
        # local 模式下的本地临时目录(单一临时目录,由 session 统一持有,
        # sandbox_tools 的文件/clone/workspace 操作都复用此目录,避免双份临时目录)
        self._local_dir: Path | None = None
        if mode == "local":
            self._local_dir = Path(tempfile.mkdtemp(prefix="sandbox_local_"))
            # local 模式下 work_dir 映射到本地目录
            self.work_dir = str(self._local_dir)
        self._closed = False
        # local 模式:后台进程跟踪 {execution_id: (Popen, [stdout_lines])}
        self._local_bg_procs: dict[str, tuple[subprocess.Popen, list[str]]] = {}
        # local 模式:平台原生沙箱工具("sandbox-exec" / "bwrap" / None)
        self._native_sandbox: str | None = None
        if mode == "local" and settings.SANDBOX_LOCAL_NATIVE_ISOLATION:
            if sys.platform == "darwin":
                if shutil.which("sandbox-exec"):
                    self._native_sandbox = "sandbox-exec"
                    logger.info("[sandbox] macOS 原生隔离已启用(sandbox-exec)")
                else:
                    logger.warning("[sandbox] macOS 未找到 sandbox-exec,跳过原生隔离")
            elif sys.platform.startswith("linux"):
                if shutil.which("bwrap"):
                    self._native_sandbox = "bwrap"
                    logger.info("[sandbox] Linux 原生隔离已启用(bubblewrap)")
                else:
                    logger.warning("[sandbox] Linux 未找到 bwrap,跳过原生隔离(可 apt install bubblewrap)")

    @property
    def local_dir(self) -> Path:
        """local 模式下的本地临时目录(sandbox_tools 复用,避免重复 mkdtemp)

        sandbox 模式访问会抛 RuntimeError。
        """
        if self._local_dir is None:
            raise RuntimeError("当前模式无 local_dir(sandbox 模式使用沙箱内路径)")
        return self._local_dir

    # ---------- 通用 ----------

    def run_command(self, cmd: str, timeout: int = 60, check: bool = False) -> str:
        """执行 shell 命令,返回 stdout

        sandbox 模式:在沙箱里执行(SandboxSync.commands.run)
        local 模式:在本地临时目录里执行(用 subprocess)

        check=True 时,退出码非零抛 RuntimeError(含 stderr),用于 git clone 等必须成功的命令。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._sandbox_run_command(cmd, timeout, check=check)
        else:
            return self._local_run_command(cmd, timeout, check=check)

    def run_command_argv(
        self,
        argv: list[str],
        envs: dict[str, str] | None = None,
        timeout: int = 60,
        check: bool = False,
    ) -> str:
        """以 argv 列表执行命令(不经 shell),返回 stdout

        local 模式:subprocess.run(argv)(绕开 shell 引号/单引号在
        Windows cmd.exe 不可用的问题;envs 合并进 os.environ 注入)
        sandbox 模式:退化为 export 前缀 + shlex.join 拼成 cmd 走沙箱 shell

        供 ACP PAT 快速诊断等需要精确 argv + 环境变量注入的调用使用。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            cmd = shlex.join(argv)
            if envs:
                exports = " ".join(f'{k}="{v}"' for k, v in envs.items())
                cmd = f"export {exports} && {cmd}"
            return self._sandbox_run_command(cmd, timeout, check=check)
        else:
            assert self._local_dir is not None
            merged_env = {**os.environ, **(envs or {})}
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=self._local_dir,
                timeout=timeout,
                env=merged_env,
            )
            if check and result.returncode != 0:
                raise RuntimeError(
                    f"命令退出码 {result.returncode}: {' '.join(argv)}"
                    f"\nstdout: {result.stdout}\nstderr: {result.stderr[:500]}"
                )
            return result.stdout

    def write_file(self, path: str, content: str) -> None:
        """写入文件"""
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            self._sandbox_write_file(path, content)
        else:
            self._local_write_file(path, content)

    def read_file(self, path: str) -> str:
        """读取文件"""
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            return self._sandbox_read_file(path)
        else:
            return self._local_read_file(path)

    def stat_size(self, path: str) -> int:
        """取沙箱内单文件的字节数(SDK 原生文件系统 API,供下载前限流检查)

        仅 sandbox 模式可用(local 模式调用方直接用 Path.stat,与列目录同惯例)。
        文件不存在抛 FileNotFoundError。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")
        if self.mode != "sandbox":
            raise RuntimeError("stat_size 仅 sandbox 模式可用")

        infos = self.sandbox.files.get_file_info([path])
        info = infos.get(path) if isinstance(infos, dict) else None
        if info is None:
            raise FileNotFoundError(f"文件不存在: {path}")
        return int(getattr(info, "size", 0) or 0)

    def read_bytes_stream(self, path: str, chunk_size: int = 65536) -> Generator[bytes, None, None]:
        """按块读取沙箱内文件的原始字节(下载用,不走 shell 文本通道)

        仅 sandbox 模式可用:local 模式的下载在 sandbox_tools 里直接 open 本地文件,
        而 `_local_resolve_path` 会把绝对宿主机路径往临时目录里映射,借道反而出错。

        SDK 侧为惰性生成器:调用方在消费时才发起 HTTP,超大文件不会整份进内存。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")
        if self.mode != "sandbox":
            raise RuntimeError("read_bytes_stream 仅 sandbox 模式可用")

        return self.sandbox.files.read_bytes_stream(path, chunk_size=chunk_size)

    def list_directory(self, path: str, depth: int | None = None) -> list[dict]:
        """列出目录内容(SDK 原生文件系统 API,单次 HTTP,无需起 shell 进程)

        仅 sandbox 模式可用(local 模式调用方直接用 Python 列目录)。
        depth=None 单层;depth=N 递归到 N 层(由 SDK DirectoryListEntry 支持)。

        返回归一化条目列表:[{"name": str, "is_dir": bool, "size": int, "path": str}]
        path 为 SDK 返回的沙箱内绝对路径。

        目录不存在时抛 FileNotFoundError;其他 SDK 异常原样抛出(调用方可回退 shell)。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")
        if self.mode != "sandbox":
            raise RuntimeError("list_directory 仅 sandbox 模式可用")

        from opensandbox.models.filesystem import DirectoryListEntry

        try:
            infos = self.sandbox.files.list_directory(
                DirectoryListEntry(path=path, depth=depth)
            )
        except FileNotFoundError:
            raise
        except Exception as e:
            # 目录不存在的 SDK 异常形态多样(404 / not exist 等),归一为 FileNotFoundError
            msg = str(e).lower()
            if "not exist" in msg or "no such" in msg or "404" in msg:
                raise FileNotFoundError(f"目录不存在: {path}") from e
            raise

        entries = []
        for info in infos:
            abs_path = getattr(info, "path", "") or ""
            name = abs_path.rstrip("/").rsplit("/", 1)[-1]
            if not name:
                continue
            mode_val = getattr(info, "mode", 0) or 0
            # 目录判定:优先 mode 的 S_IFDIR 位(比 entry_type 字符串更可靠)
            is_dir = (mode_val & 0o170000) == 0o040000
            if not is_dir:
                entry_type = str(getattr(info, "entry_type", "") or "").lower()
                is_dir = entry_type in ("directory", "dir")
            entries.append({
                "name": name,
                "is_dir": is_dir,
                "size": int(getattr(info, "size", 0) or 0),
                "path": abs_path,
            })
        return entries

    def get_endpoint(self, port: int) -> tuple[str, dict[str, str]]:
        """获取沙箱内端口的外部访问端点(端口转发)

        sandbox 模式:通过 SDK 的 get_endpoint(port) 获取转发 URL + 必需 headers
        local 模式:返回 localhost:port(local 模式下进程直接跑在宿主机,
                  若 agent 通过 run_command_background 起了监听该端口的服务,可直接访问)

        返回 (endpoint_url, headers):
            - endpoint_url:可直接 HTTP 请求的完整 URL(含 scheme)
            - headers:请求时必须携带的 headers(server proxy 路由/鉴权用)
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            ep = self.sandbox.get_endpoint(port)
            url = ep.endpoint
            # SDK 返回的 endpoint 可能不含 scheme(如 "host:port/path"),
            # httpx 要求完整 URL,补上 http://
            if not url.startswith(("http://", "https://")):
                url = f"http://{url}"
            return url, dict(ep.headers or {})
        else:
            # local 模式:进程在宿主机上,直接用 localhost
            return f"http://127.0.0.1:{port}", {}

    def run_command_background(
        self,
        cmd: str = "",
        envs: dict[str, str] | None = None,
        work_dir: str | None = None,
        argv: list[str] | None = None,
    ) -> str:
        """后台启动命令(非阻塞),返回 execution_id 供后续查询日志/中断

        用于启动 ACP bridge 等长驻服务。命令在沙箱内 detached 运行,
        本方法立即返回,不等待命令结束。

        envs:注入命令进程的环境变量(如 QODER_PERSONAL_ACCESS_TOKEN)
        work_dir:工作目录(沙箱内绝对路径)
        argv:以参数列表启动(不经 shell)。local 模式下用 Popen(list)
             绕开 Windows cmd.exe 的引号/单引号问题;sandbox 模式退化为
             shlex.join 拼成 cmd 字符串。传 argv 时 cmd 被忽略。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            if argv is not None:
                cmd = shlex.join(argv)
            return self._sandbox_run_background(cmd, envs, work_dir)
        else:
            return self._local_run_background(cmd, envs, work_dir, argv=argv)

    def get_background_logs(self, execution_id: str, cursor: int | None = None) -> tuple[str, int | None]:
        """获取后台命令的累积日志

        返回 (logs_text, next_cursor)。next_cursor 为 None 表示无更多日志。
        local 模式返回 (stdout_so_far, None)。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            logs = self.sandbox.commands.get_background_command_logs(
                execution_id, cursor=cursor
            )
            return logs.content, logs.cursor
        else:
            return self._local_get_background_logs(execution_id)

    def get_command_status(self, execution_id: str) -> tuple[bool, int | None]:
        """查询命令运行状态,返回 (running, exit_code)

        running=True 表示还在跑(exit_code 为 None);
        结束后 exit_code 为实际退出码。
        local 模式:进程已退出返回 (False, returncode),否则 (True, None)。
        """
        if self._closed:
            raise RuntimeError("沙箱已关闭")

        if self.mode == "sandbox":
            st = self.sandbox.commands.get_command_status(execution_id)
            return bool(st.running), st.exit_code
        else:
            entry = self._local_bg_procs.get(execution_id)
            if entry is None:
                return False, None
            proc, _ = entry
            ret = proc.poll()
            return (ret is None), ret

    def interrupt_command(self, execution_id: str) -> None:
        """中断后台命令"""
        if self._closed:
            return

        if self.mode == "sandbox":
            self.sandbox.commands.interrupt(execution_id)
        else:
            entry = self._local_bg_procs.pop(execution_id, None)
            if entry:
                _kill_bg_proc_tree(entry[0])

    def renew(self, timeout_minutes: int | None = None) -> bool:
        """续期沙箱 TTL:新过期时间 = 当前时间 + timeout(SDK renew 语义)

        长任务(多轮协作/用户等待确认/CLI 长时间执行)可能拖过创建时的
        TTL 被 Server 自动回收,回收后任何 run_command 都会 404。
        调用方应在会话被活跃使用时周期性续期(sandbox_tools 的访问续期
        + acp_base 的 prompt 期间后台续期)。

        timeout_minutes:续期时长,默认用 SANDBOX_TIMEOUT_MINUTES。
        返回是否成功。失败不抛异常(可能已过期,由后续命令自行报错);
        local 模式无 TTL 概念,直接返回 True。
        """
        if self._closed:
            return False
        if self.mode != "sandbox" or self.sandbox is None:
            return True

        minutes = timeout_minutes or settings.SANDBOX_TIMEOUT_MINUTES
        try:
            resp = self.sandbox.renew(timedelta(minutes=minutes))
            expires = getattr(resp, "expires_at", None) or getattr(resp, "expiration_time", None)
            logger.info(f"[sandbox] TTL 已续期 +{minutes} 分钟(过期时间: {expires})")
            return True
        except Exception as e:
            logger.warning(f"[sandbox] TTL 续期失败(沙箱可能已被回收): {e}")
            return False

    @contextmanager
    def auto_renew(self, interval_minutes: float | None = None) -> Generator[None, None, None]:
        """后台周期性续期上下文管理器(供 CLI prompt 等长时间阻塞段使用)

        prompt 期间 CLI 自带 bash 执行命令,不会触发后端 sandbox_tools 的
        访问续期;若单轮执行超过 TTL,沙箱会被中途回收。用此上下文包裹
        长阻塞段,后台线程按 interval 周期性 renew。

        interval_minutes:续期间隔,默认 SANDBOX_RENEW_INTERVAL_MINUTES。
        local 模式/已关闭会话:不开线程,直接 yield(no-op)。
        """
        if self._closed or self.mode != "sandbox" or self.sandbox is None:
            yield
            return

        interval = (interval_minutes or settings.SANDBOX_RENEW_INTERVAL_MINUTES) * 60
        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.wait(interval):
                if self._closed:
                    break
                self.renew()

        worker = threading.Thread(target=_loop, daemon=True, name="sandbox-auto-renew")
        worker.start()
        try:
            yield
        finally:
            stop_event.set()

    def close(self) -> None:
        """关闭沙箱,释放资源

        sandbox 模式用 destroy()(kill + close 本地资源,避免 httpx 连接泄漏);
        destroy 不可用时回退到 kill()。
        local 模式清理临时目录 + 终止后台进程(单一临时目录,含 workspace/clone/memory)。
        """
        if self._closed:
            return
        self._closed = True

        # local 模式:终止所有后台进程(含子进程树,防 CLI 孤儿)
        if self._local_bg_procs:
            for proc, _ in self._local_bg_procs.values():
                _kill_bg_proc_tree(proc)
            self._local_bg_procs.clear()

        if self.mode == "sandbox" and self.sandbox:
            try:
                destroy = getattr(self.sandbox, "destroy", None)
                if destroy is not None:
                    destroy()
                else:
                    self.sandbox.kill()
            except Exception as e:
                logger.warning(f"关闭沙箱失败: {e}")
        elif self._local_dir:
            # local 模式:清理临时目录(含 clone / workspace / memory 等全部子目录)。
            # 用 repo_cache.force_rmtree 而非 rmtree(ignore_errors=True):git 把
            # .git/objects/pack/*.pack|*.idx 设为只读,普通 rmtree 在 Windows 上删
            # 不动这种目录且默不作声 —— 临时目录会一直在 %TEMP% 里积下来
            # 延迟导入避免模块级循环依赖(sandbox_tools / acp_base 都依赖本模块)
            from app.services.repo_cache import force_rmtree

            if not force_rmtree(self._local_dir):
                logger.warning(f"本地临时目录未能删净,残留: {self._local_dir}")

    # ---------- sandbox 模式实现(SandboxSync 同步) ----------

    def _sandbox_run_command(self, cmd: str, timeout: int, *, check: bool = False) -> str:
        """在真实沙箱里执行命令(SandboxSync.commands.run 同步调用)

        SDK 的 run() 不接受 timeout kwarg,超时通过 RunCommandOpts(timeout=timedelta) 传入。
        check=True 时,退出码非零抛 RuntimeError(含 stderr)。
        """
        from opensandbox.models.execd import RunCommandOpts

        opts = RunCommandOpts(timeout=timedelta(seconds=timeout))
        execution = self.sandbox.commands.run(cmd, opts=opts)

        # SDK 的 Execution.text 属性已按 \n 正确拼接 stdout(每条 OutputMessage 是一行)
        stdout = execution.text or ""
        if check:
            exit_code = getattr(execution, "exit_code", None)
            if exit_code not in (None, 0):
                stderr = "\n".join(
                    msg.text.rstrip("\n") for msg in (execution.logs.stderr or [])
                )
                raise RuntimeError(
                    f"命令退出码 {exit_code}: {cmd}\nstdout: {stdout}\nstderr: {stderr}"
                )
        return stdout

    def _sandbox_write_file(self, path: str, content: str) -> None:
        """在真实沙箱里写文件"""
        from opensandbox.models.filesystem import WriteEntry

        self.sandbox.files.write_files([
            WriteEntry(path=path, data=content, mode=644)
        ])

    def _sandbox_read_file(self, path: str) -> str:
        """在真实沙箱里读文件"""
        content = self.sandbox.files.read_file(path)
        return content

    def _sandbox_run_background(
        self,
        cmd: str,
        envs: dict[str, str] | None,
        work_dir: str | None,
    ) -> str:
        """sandbox 模式:后台启动命令(SandboxSync.commands.run + background=True)

        返回 execution_id(str),供 get_background_logs / interrupt_command 使用。
        """
        from opensandbox.models.execd import RunCommandOpts

        opts = RunCommandOpts(
            background=True,
            working_directory=work_dir,
            envs=envs,
        )
        execution = self.sandbox.commands.run(cmd, opts=opts)
        if not execution.id:
            raise RuntimeError(f"后台命令启动失败,无 execution_id: {cmd}")
        logger.info(f"[sandbox] 后台命令已启动: execution_id={execution.id}, cmd={cmd[:100]}")
        return execution.id

    # ---------- local 模式实现(本地文件系统) ----------

    def _local_resolve_path(self, path: str) -> Path:
        """把传入路径映射到本地临时目录内,并做路径穿越防护

        path 可能是绝对路径(/home/user/xxx)或相对路径,统一映射到 _local_dir 下。
        解析后不得逃出 _local_dir(防 ../../etc/passwd 之类逃逸)。
        """
        assert self._local_dir is not None
        rel = path.lstrip("/")
        if rel.startswith("home/user/"):
            rel = rel[len("home/user/"):]
        target = (self._local_dir / rel).resolve()
        if not target.is_relative_to(self._local_dir.resolve()):
            raise ValueError(f"非法路径:不能超出本地工作目录({path})")
        return target

    def _local_check_write(self, target: Path, original_path: str) -> None:
        """写操作权限检查:.git 目录保护 + 配置的只读路径保护(对齐 TRAE 路径策略)

        target: 已 resolve 的目标路径
        original_path: 原始传入路径(用于错误信息)
        """
        check_local_write_permission(target, self._local_dir, original_path)

    def _wrap_native_sandbox(self, cmd: str) -> str:
        """用平台原生沙箱包装命令(macOS: sandbox-exec / Linux: bwrap)

        系统目录只读,工作区 + 临时目录读写,禁止 sudo/su。
        未检测到工具或已禁用时返回原始命令。
        """
        if not self._native_sandbox or self._local_dir is None:
            return cmd
        if self._native_sandbox == "sandbox-exec":
            return self._wrap_macos_sandbox_exec(cmd)
        elif self._native_sandbox == "bwrap":
            return self._wrap_linux_bwrap(cmd)
        return cmd

    def _wrap_macos_sandbox_exec(self, cmd: str) -> str:
        """macOS:用 sandbox-exec 包装命令,系统目录只读

        profile 策略(对齐 TRAE 路径策略):
        - 默认允许(网络/进程/文件读)
        - 系统目录写保护(/etc /usr /bin /sbin /System /Library)
        - 工作区 + 临时目录显式允许写
        - 禁止执行 sudo/su
        """
        assert self._local_dir is not None
        work_dir = str(self._local_dir.resolve())
        tmp_dir = tempfile.gettempdir()
        profile = (
            "(version 1)\n"
            "(allow default)\n"
            "(deny file-write*\n"
            '    (subpath "/etc")\n'
            '    (subpath "/usr")\n'
            '    (subpath "/bin")\n'
            '    (subpath "/sbin")\n'
            '    (subpath "/System")\n'
            '    (subpath "/Library")\n'
            '    (subpath "/private/etc")\n'
            ")\n"
            "(deny process-exec\n"
            '    (path "/usr/bin/sudo")\n'
            '    (path "/usr/bin/su")\n'
            '    (path "/bin/su")\n'
            ")\n"
            f'(allow file-write* (subpath "{work_dir}"))\n'
            f'(allow file-write* (subpath "{tmp_dir}"))\n'
        )
        # profile 写到临时文件(避免 -p 参数的引号转义问题)
        profile_path = self._local_dir / ".sandbox_profile.sb"
        profile_path.write_text(profile, encoding="utf-8")
        escaped_cmd = cmd.replace("'", "'\\''")
        return f"sandbox-exec -f {shlex.quote(str(profile_path))} sh -c '{escaped_cmd}'"

    def _wrap_linux_bwrap(self, cmd: str) -> str:
        """Linux:用 bwrap(bubblewrap)包装命令,系统目录只读

        策略(对齐 TRAE 路径策略):
        - 根目录只读挂载(--ro-bind / /)
        - 工作区 + 临时目录读写挂载
        - 独立的 /dev /proc(隔离设备/进程视图)
        - 不共享网络命名空间(local 模式需 git clone/pip install)
        """
        assert self._local_dir is not None
        work_dir = str(self._local_dir.resolve())
        tmp_dir = tempfile.gettempdir()
        escaped_cmd = cmd.replace("'", "'\\''")
        parts = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--bind", work_dir, work_dir,
            "--bind", tmp_dir, tmp_dir,
            "--dev", "/dev",
            "--proc", "/proc",
            "sh", "-c", f"'{escaped_cmd}'",
        ]
        return " ".join(parts)

    def _local_run_command(self, cmd: str, timeout: int, *, check: bool = False) -> str:
        """local 模式:用本地 subprocess 执行,把 work_dir 当作沙箱根

        macOS/Linux 下自动用平台原生沙箱(sandbox-exec/bwrap)包装命令:
        系统目录只读,工作区读写,禁止 sudo/su。
        Windows 无原生沙箱,直接执行(shell=True 走 cmd.exe,Unix 命令可能失败)。
        """
        assert self._local_dir is not None
        wrapped_cmd = self._wrap_native_sandbox(cmd)
        result = subprocess.run(
            wrapped_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=self._local_dir,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"命令退出码 {result.returncode}: {cmd}\nstdout: {result.stdout}\nstderr: {result.stderr[:500]}"
            )
        return result.stdout

    def _local_write_file(self, path: str, content: str) -> None:
        """local 模式:直接在本地临时目录写文件(带路径穿越防护 + 写权限检查)"""
        target = self._local_resolve_path(path)
        self._local_check_write(target, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _local_read_file(self, path: str) -> str:
        """local 模式:直接读本地临时目录(带路径穿越防护)"""
        target = self._local_resolve_path(path)
        return target.read_text(encoding="utf-8")

    def _local_run_background(
        self,
        cmd: str,
        envs: dict[str, str] | None,
        work_dir: str | None,
        argv: list[str] | None = None,
    ) -> str:
        """local 模式:用 subprocess.Popen 后台启动,跟踪进程

        argv 模式:不经 shell 直接以参数列表启动(Windows cmd.exe 无单引号/
        引号转义规则不同,JSON 参数经 shell 会损坏);POSIX 下 start_new_session
        建立进程组,配合 _kill_bg_proc_tree 的 killpg 杀整组。
        """
        assert self._local_dir is not None
        exec_id = f"local_bg_{uuid.uuid4().hex[:8]}"
        merged_env = {**os.environ, **(envs or {})}
        cwd = work_dir or str(self._local_dir)
        # local 模式下 work_dir 可能是 /home/user/xxx,映射到本地
        if cwd.startswith("/home/user"):
            cwd = str(self._local_dir / cwd[len("/home/user/"):])
        popen_kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd if Path(cwd).exists() else str(self._local_dir),
            env=merged_env,
        )
        if argv is not None:
            proc = subprocess.Popen(
                argv, shell=False,
                start_new_session=(os.name == "posix"),
                **popen_kwargs,
            )
        else:
            proc = subprocess.Popen(cmd, shell=True, **popen_kwargs)
        self._local_bg_procs[exec_id] = (proc, [])
        # 启动后台线程持续读取 stdout(避免 pipe 满死锁)
        def _drain():
            try:
                for line in proc.stdout:
                    self._local_bg_procs[exec_id][1].append(line)
            except Exception:
                pass
        threading.Thread(target=_drain, daemon=True).start()
        return exec_id

    def _local_get_background_logs(self, execution_id: str) -> tuple[str, int | None]:
        """local 模式:返回已累积的 stdout 行"""
        entry = self._local_bg_procs.get(execution_id)
        if entry is None:
            return "", None
        _proc, lines = entry
        return "".join(lines), None


# ============================================================
# 沙箱工厂
# ============================================================


def check_local_write_permission(target: Path, base_dir: Path, original_path: str) -> None:
    """写操作权限检查(模块级函数,供 client.py 和 sandbox_tools.py 共用)

    对齐 TRAE 沙箱路径策略:
    - .git 目录写保护(防 LLM 篡改 git 历史)
    - 配置的只读路径(.vscode / .trae / .idea 等)写保护

    target: 已 resolve 的目标路径
    base_dir: 工作区根目录(local_dir 或 repo_path)
    original_path: 原始传入路径(用于错误信息)
    """
    if not settings.SANDBOX_LOCAL_PROTECT_GIT:
        return
    # 计算相对于 base_dir 的相对路径,提取路径组件
    try:
        rel = target.relative_to(base_dir.resolve())
    except ValueError:
        return  # 不在 base_dir 内,由调用方的逃逸检查处理
    parts = rel.parts
    # .git 目录保护
    if ".git" in parts:
        raise ValueError(f"非法路径:.git 目录受保护,禁止写入({original_path})")
    # 配置的只读路径保护
    readonly = settings.SANDBOX_LOCAL_READONLY_PATHS
    if readonly:
        for ro in readonly.split(","):
            ro = ro.strip()
            if ro and ro in parts:
                raise ValueError(f"非法路径:{ro} 目录受保护,禁止写入({original_path})")


def create_sandbox(
    extra_volumes: list[tuple[str, str, bool]] | None = None,
) -> SandboxSession:
    """创建一个沙箱会话

    根据 settings.SANDBOX_MODE 决定走真实沙箱还是 local 模式。

    extra_volumes:额外挂载卷描述列表 (host_path, mount_path, read_only),
    供 sandbox 模式按任务挂载 bare 仓库缓存(local 模式忽略)。host_path 是
    Server 宿主机路径,需在 Server [storage].allowed_host_paths 放行前缀。
    """
    mode = settings.SANDBOX_MODE

    if mode == "local":
        logger.warning(
            "[sandbox] 使用 local 模式(本地文件系统,不用沙箱):"
            "LLM 生成的代码/命令将在宿主机直接执行,无隔离边界,仅适用于开发/调试"
        )
        if os.name == "nt":
            logger.warning(
                "[sandbox] 检测到 Windows:local 模式下 run_command 走 cmd.exe,"
                "Unix 命令(mkdir -p / find / rg / test 等)可能失败;"
                "文件操作(list/read/write/clone)用 Python 直接实现,跨平台可用"
            )
        return SandboxSession(mode="local")
    elif mode == "sandbox":
        return _create_real_sandbox(extra_volumes)
    else:
        raise ValueError(f"未知 SANDBOX_MODE: {mode}")


def _parse_domain(server_url: str) -> str:
    """从 SANDBOX_SERVER_URL 提取 SDK 需要的 domain(host:port,无 scheme)

    SDK 的 ConnectionConfig.domain 接受 "host:port" 形式(无 http:// 前缀)
    """
    if "://" in server_url:
        parsed = urlparse(server_url)
        return parsed.netloc
    return server_url.lstrip("/")


def _build_volumes(
    extra_volumes: list[tuple[str, str, bool]] | None = None,
) -> list[Any]:
    """根据配置构建挂载卷:SSH key(可选)+ 额外卷(bare 仓库缓存等)

    沙箱默认用户是 user,把宿主机 SSH 目录只读挂载到 /home/user/.ssh,
    供 git clone git@github.com:... 使用。需在 server [storage].allowed_host_paths 放行。

    extra_volumes:(host_path, mount_path, read_only) 描述列表。Volume/Host
    构造统一在此处,调用方(sandbox_tools)不直接依赖 opensandbox SDK。

    注意:SANDBOX_SSH_KEY_HOST_PATH 是 Server 宿主机上的路径(跨机部署时后端
    无法也不应本地验证),必须是绝对路径,不要用 ~。
    """
    from opensandbox.models.sandboxes import Host, Volume

    volumes: list[Any] = []
    if settings.SANDBOX_SSH_KEY_HOST_PATH:
        volumes.append(
            Volume(
                name="ssh-keys",
                host=Host(path=settings.SANDBOX_SSH_KEY_HOST_PATH),
                mountPath="/home/user/.ssh",
                readOnly=True,
            )
        )
    for idx, (host_path, mount_path, read_only) in enumerate(extra_volumes or []):
        volumes.append(
            Volume(
                name=f"extra-vol-{idx}",
                host=Host(path=host_path),
                mountPath=mount_path,
                readOnly=read_only,
            )
        )
    return volumes


def _build_resource() -> dict[str, str] | None:
    """根据配置构建资源限制(可选)"""
    resource: dict[str, str] = {}
    if settings.SANDBOX_CPU:
        resource["cpu"] = settings.SANDBOX_CPU
    if settings.SANDBOX_MEMORY:
        resource["memory"] = settings.SANDBOX_MEMORY
    return resource or None


def _create_real_sandbox(
    extra_volumes: list[tuple[str, str, bool]] | None = None,
) -> SandboxSession:
    """创建真实沙箱(同步)

    使用官方 SandboxSync 同步 API,无需 asyncio 包装。
    必须显式传 ConnectionConfigSync(domain + api_key),否则 SDK 只会连 localhost:8080。
    """
    from datetime import timedelta

    from opensandbox import SandboxSync
    from opensandbox.config import ConnectionConfigSync

    domain = _parse_domain(settings.SANDBOX_SERVER_URL)
    config = ConnectionConfigSync(
        domain=domain,
        api_key=settings.SANDBOX_API_KEY or None,
        # 跨机部署:走 Server 代理,后端只连 8080,无需放行容器端口范围
        use_server_proxy=settings.SANDBOX_USE_SERVER_PROXY,
        # 创建沙箱涉及拉镜像/启容器/等 healthy,首次尤慢,HTTP 请求超时给足 5 分钟
        request_timeout=timedelta(minutes=5),
    )
    volumes = _build_volumes(extra_volumes)
    resource = _build_resource()

    kwargs: dict[str, Any] = {
        "image": settings.SANDBOX_IMAGE,
        "connection_config": config,
        "timeout": timedelta(minutes=settings.SANDBOX_TIMEOUT_MINUTES),
    }
    if volumes:
        kwargs["volumes"] = volumes
    if resource:
        kwargs["resource"] = resource
    sandbox = SandboxSync.create(**kwargs)
    return SandboxSession(mode="sandbox", sandbox=sandbox)
