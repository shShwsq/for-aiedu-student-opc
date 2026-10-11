"""CLI 运行时预热单元测试(不连真实沙箱:acp_base 原语全部替身)

覆盖 `app/agents/acp_prewarm.py`:
- should_prewarm 门控(总开关 / 运行模式 / 类型白名单)
- 预热主体把 bridge 与 initialize 产物挂到 handle 上,并**用主线程算好的 acp_args**
  (不回读 task.params:预热线程不能携带 ORM 对象)
- single-flight:同一任务重复触发只起一个线程
- consume 的三种拒绝:指纹不符 / 预算超时 / 预热失败 → 都返回 None 且停掉 bridge
- discard / stop_task_bridge 的回收:不留无人认领的 bridge 进程
- 预热失败绝不冒泡到任务层
"""
import threading
import time
import uuid

import pytest

from app.agents import acp_base, acp_prewarm
from app.config import settings

AGENT = "qoder_cli"
FP = "fp-equal"


class _FakeSession:
    """只实现预热会碰到的那几个会话方法"""

    mode = "sandbox"

    def __init__(self):
        self.commands: list[str] = []
        self.written: dict[str, str] = {}

    def get_endpoint(self, port):
        return f"http://ep:{port}", {"x-sandbox-port": str(port)}

    def run_command(self, cmd, timeout=60, check=False):
        self.commands.append(cmd)
        return "/usr/local/bin/qodercli\n"

    def write_file(self, path, content):
        self.written[path] = content


@pytest.fixture(autouse=True)
def _clean_registry():
    acp_prewarm._handles.clear()
    yield
    acp_prewarm._handles.clear()


@pytest.fixture
def sandbox_cli(monkeypatch):
    """sandbox 模式 + 预热开启 + 名单含 qoder_cli"""
    monkeypatch.setattr(settings, "SANDBOX_MODE", "sandbox")
    monkeypatch.setattr(settings, "ACP_PREWARM_ENABLED", True)
    monkeypatch.setattr(settings, "ACP_PREWARM_AGENTS", "qoder_cli")
    monkeypatch.setattr(settings, "ACP_PREWARM_TIMEOUT_SECONDS", 30)
    return monkeypatch


class _FakeClient:
    """ACPClient 替身:initialize 返回能力声明,记录 close"""

    created: list["_FakeClient"] = []
    init_result = {"protocolVersion": 1, "authMethods": [{"id": "qodercli-login"}]}

    def __init__(self, base_url, headers=None, recorder=None, permission_handler=None):
        self.base_url = base_url
        self.closed = False
        _FakeClient.created.append(self)

    def initialize(self, timeout=None):
        return dict(_FakeClient.init_result)

    def close(self):
        self.closed = True


@pytest.fixture
def primitives(monkeypatch):
    """把 acp_base 的沙箱原语换成计数替身;返回 calls 字典供断言"""
    calls = {
        "ensure": 0, "inject": 0, "start": 0, "wait": 0,
        "acp_args": None, "envs": None, "stopped": [],
    }

    def fake_ensure(session, agent_type, ctx=None):
        calls["ensure"] += 1

    def fake_inject(session, task_id, tokens):
        calls["inject"] += 1

    def fake_start(session, credential_envs, task=None, agent_type="",
                   extra_acp_args=None, acp_args=None):
        calls["start"] += 1
        calls["acp_args"] = acp_args
        calls["envs"] = credential_envs
        calls["task_is_none"] = task is None
        return "exec-1", 8088

    def fake_wait(session, execution_id, endpoint_url, endpoint_headers, agent_type=""):
        calls["wait"] += 1
        return {"status": "ok", "bridge_protocol": 2}

    def fake_stop(session, execution_id, agent_type=""):
        calls["stopped"].append(execution_id)

    monkeypatch.setattr(acp_base, "_ensure_cli_env", fake_ensure)
    monkeypatch.setattr(acp_base, "_inject_git_tokens_to_cache", fake_inject)
    monkeypatch.setattr(acp_base, "_start_acp_bridge", fake_start)
    monkeypatch.setattr(acp_base, "_wait_for_bridge_ready", fake_wait)
    monkeypatch.setattr(acp_base, "_stop_acp_bridge", fake_stop)
    monkeypatch.setattr(acp_base, "ACPClient", _FakeClient)
    _FakeClient.created.clear()
    return calls


def _prewarm(session=None, acp_args=None, git_tokens=None):
    """发起预热并等它收工(测试里同步化,避免时序抖动)"""
    handle = acp_prewarm.prewarm_agent_runtime(
        task_id=str(uuid.uuid4()),
        session=session or _FakeSession(),
        ctx={"mode": "sandbox"},
        agent_type=AGENT,
        fingerprint=FP,
        acp_args=acp_args if acp_args is not None else ["--acp", "--yolo"],
        credential_envs={"QODER_PERSONAL_ACCESS_TOKEN": "pat"},
        git_tokens=git_tokens if git_tokens is not None else {"github": "ghp_x"},
    )
    assert handle is not None
    handle.wait(5)
    return handle


# ============================================================
# should_prewarm 门控
# ============================================================


def test_should_prewarm_respects_switch_and_whitelist(sandbox_cli):
    assert acp_prewarm.should_prewarm(AGENT) is True

    sandbox_cli.setattr(settings, "ACP_PREWARM_ENABLED", False)
    assert acp_prewarm.should_prewarm(AGENT) is False

    sandbox_cli.setattr(settings, "ACP_PREWARM_ENABLED", True)
    sandbox_cli.setattr(settings, "ACP_PREWARM_AGENTS", "qoder_cli,deepseek_cli")
    assert acp_prewarm.should_prewarm("deepseek_cli") is True
    # codex 需要 pre_bridge_hook,不在名单里
    assert acp_prewarm.should_prewarm("codex_cli") is False


def test_should_prewarm_off_in_local_mode(sandbox_cli):
    """local 模式的命令层并发未实测过(Windows Popen 路径),不预热"""
    sandbox_cli.setattr(settings, "SANDBOX_MODE", "local")
    assert acp_prewarm.should_prewarm(AGENT) is False


# ============================================================
# 预热主体 + 认领
# ============================================================


def test_prewarm_full_chain_then_consume(sandbox_cli, primitives):
    handle = _prewarm()
    assert handle.error is None
    # 环境准备 / 凭证注入 / 起 bridge / 等就绪 / initialize 各一次
    assert (primitives["ensure"], primitives["inject"], primitives["start"],
            primitives["wait"]) == (1, 1, 1, 1)
    assert handle.init_result["protocolVersion"] == 1
    assert handle.bridge_protocol == 2
    assert handle.endpoint_url == "http://ep:8088"
    assert handle.cli_env_ready and handle.git_cred_done
    # 预热线程不携带 ORM 对象:task=None,启动参数由主线程算好传入
    assert primitives["task_is_none"] is True
    assert primitives["acp_args"] == ["--acp", "--yolo"]
    # 预热的 ACPClient 用完即关(不占着 SSE 连接)
    assert _FakeClient.created and all(c.closed for c in _FakeClient.created)

    got = acp_prewarm.consume_prewarm(handle.task_id, AGENT, FP)
    assert got is handle
    # 认领后 bridge 不该被停
    assert primitives["stopped"] == []


def test_consume_is_one_shot(sandbox_cli, primitives):
    handle = _prewarm()
    assert acp_prewarm.consume_prewarm(handle.task_id, AGENT, FP) is not None
    # 第二次没有 handle 可认领 → None(调用方走全串行,不会双重接管 bridge)
    assert acp_prewarm.consume_prewarm(handle.task_id, AGENT, FP) is None


def test_fingerprint_mismatch_discards_and_stops_bridge(sandbox_cli, primitives):
    """模型/思考强度/命令确认模式在执行阶段变了 → 预热作废且必须停掉旧 bridge

    (旧 bridge 是用旧启动参数起的,留着既占端口又可能被判成可用)
    """
    handle = _prewarm()
    assert acp_prewarm.consume_prewarm(handle.task_id, AGENT, "fp-changed") is None
    assert primitives["stopped"] == ["exec-1"]


def test_agent_type_mismatch_discards(sandbox_cli, primitives):
    handle = _prewarm()
    assert acp_prewarm.consume_prewarm(handle.task_id, "deepseek_cli", FP) is None
    assert primitives["stopped"] == ["exec-1"]


def test_prewarm_error_falls_back_without_raising(sandbox_cli, primitives, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("bridge 起不来")

    monkeypatch.setattr(acp_base, "_wait_for_bridge_ready", _boom)
    handle = _prewarm()

    assert handle.error and "bridge 起不来" in handle.error
    # 半成品状态全部清零,消费方按"没有预热"处理
    assert handle.bridge_exec_id == "" and handle.endpoint_url == ""
    assert handle.init_result == {} and handle.cli_env_ready is False
    # 但已起的 bridge 要停掉,别空转吃容器 TTL
    assert primitives["stopped"] == ["exec-1"]
    assert acp_prewarm.consume_prewarm(handle.task_id, AGENT, FP) is None


def test_consume_timeout_rejects_without_double_wait(sandbox_cli, primitives, monkeypatch):
    """预算从 handle 创建起算:已经跑掉的秒数要扣掉,不与 BRIDGE_STARTUP_TIMEOUT 叠加"""
    monkeypatch.setattr(settings, "ACP_PREWARM_TIMEOUT_SECONDS", 1)

    def _never_finishes(handle, *a, **kw):
        """预热没收工(done 不复位),且没起过 bridge"""

    monkeypatch.setattr(acp_prewarm, "_run_prewarm", _never_finishes)
    handle = acp_prewarm.prewarm_agent_runtime(
        task_id=str(uuid.uuid4()), session=_FakeSession(), ctx={"mode": "sandbox"},
        agent_type=AGENT, fingerprint=FP, acp_args=["--acp"],
        credential_envs={"QODER_PERSONAL_ACCESS_TOKEN": "pat"},
    )
    # 把 handle 推到"已经跑了 10s"的状态(预算 1s 已透支)
    handle.started_at = time.monotonic() - 10

    started = time.perf_counter()
    assert acp_prewarm.consume_prewarm(handle.task_id, AGENT, FP) is None
    # 预算被扣成 0 → 立刻判超时,不再干等 1s(更不等 30s 的 bridge 超时)
    assert time.perf_counter() - started < 0.5
    assert primitives["stopped"] == []


def test_preload_absent_returns_none(sandbox_cli, primitives):
    """没预热过(名单外/开关关)→ consume 直接 None,不影响全串行链路"""
    assert acp_prewarm.consume_prewarm(str(uuid.uuid4()), AGENT, FP) is None


# ============================================================
# single-flight 与回收
# ============================================================


def test_single_flight_starts_one_thread(sandbox_cli, primitives, monkeypatch):
    started: list[threading.Thread] = []

    def _slow(handle, *a, **kw):
        time.sleep(0.3)

    monkeypatch.setattr(acp_prewarm, "_run_prewarm", _slow)
    real_thread = threading.Thread

    def spy(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        started.append(t)
        return t

    monkeypatch.setattr(acp_prewarm.threading, "Thread", spy)

    task_id = str(uuid.uuid4())
    kwargs = dict(
        task_id=task_id, session=_FakeSession(), ctx={"mode": "sandbox"},
        agent_type=AGENT, fingerprint=FP, acp_args=["--acp"],
        credential_envs={"QODER_PERSONAL_ACCESS_TOKEN": "pat"},
    )
    first = acp_prewarm.prewarm_agent_runtime(**kwargs)
    second = acp_prewarm.prewarm_agent_runtime(**kwargs)
    assert first is second
    assert len(started) == 1
    first.wait(2)


def test_failed_handle_can_be_restarted(sandbox_cli, primitives, monkeypatch):
    """已失败的 handle 不挡住重试:第二次要真的重开一个"""
    handle = _prewarm()
    handle.error = "boom"  # 模拟失败态残留在注册表里
    monkeypatch.setattr(acp_prewarm, "_run_prewarm", lambda h, *a, **kw: h.done.set())
    fresh = acp_prewarm.prewarm_agent_runtime(
        task_id=handle.task_id, session=_FakeSession(), ctx={"mode": "sandbox"},
        agent_type=AGENT, fingerprint=FP, acp_args=["--acp"],
        credential_envs={"QODER_PERSONAL_ACCESS_TOKEN": "pat"},
    )
    assert fresh is not handle


def test_discard_task_prewarm_is_idempotent(sandbox_cli, primitives):
    acp_prewarm.discard_task_prewarm("no-such-task", reason="x")  # 不炸
    handle = _prewarm()
    acp_prewarm.discard_task_prewarm(handle.task_id, reason="x")
    assert primitives["stopped"] == ["exec-1"]
    assert handle.task_id not in acp_prewarm._handles
    acp_prewarm.discard_task_prewarm(handle.task_id, reason="x")  # 第二次空转
    assert primitives["stopped"] == ["exec-1"]


def test_stop_task_bridge_also_discards_prewarm(sandbox_cli, primitives):
    """任务删除/会话关闭路径必须连预热一起收:否则预热 bridge 成孤儿进程"""
    handle = _prewarm()
    acp_base.stop_task_bridge(handle.task_id)
    assert primitives["stopped"] == ["exec-1"]
    assert handle.task_id not in acp_prewarm._handles


def test_stop_bridge_failure_is_swallowed(sandbox_cli, primitives, monkeypatch):
    def _raise(session, execution_id, agent_type=""):
        raise RuntimeError("沙箱已关闭")

    monkeypatch.setattr(acp_base, "_stop_acp_bridge", _raise)
    handle = _prewarm()
    # 停 bridge 失败只记日志,不能把异常抛回任务删除/会话关闭路径
    acp_prewarm.discard_task_prewarm(handle.task_id, reason="x")
    assert handle.task_id not in acp_prewarm._handles
