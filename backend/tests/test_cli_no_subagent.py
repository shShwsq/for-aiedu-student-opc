"""CLI 执行器禁止 fan-out(预防性不派生子智能体)止血单元测试。

覆盖 Part A 的三个承重决策:
- prompts.executor.build_cli_no_subagent_section:非空、含"不要派生"、零参数(纯文本)
- acp_base._disallow_subagents_enabled:全局开关 + 类型白名单
- 互斥不变式:命中禁止 → autocontinue 关闭(落盘段不再注入)→ 不会同时发出
  "禁派生"与"教子任务落盘"两段矛盾指令
"""
from app.agents import acp_base as A
from app.config import settings
from app.prompts.executor import (
    build_cli_no_subagent_section,
    build_cli_subagent_reports_section,
)


# ============================================================
# 提示词段本体(零 app.* 依赖由 test_prompts_purity 统一守住)
# ============================================================


def test_no_subagent_section_nonempty_and_directive():
    s = build_cli_no_subagent_section()
    assert s.strip()
    assert "子智能体" in s
    # 明确禁止派生 + 要求主循环串行 + 一次性完整报告
    assert "不要派生" in s
    assert "串行" in s
    assert "一次性" in s


def test_no_subagent_section_is_constant_no_args():
    # 无参数、内容恒定(可稳定并入记忆桶参与去重)
    assert build_cli_no_subagent_section() == build_cli_no_subagent_section()


# ============================================================
# _disallow_subagents_enabled:全局开关 + 类型白名单
# ============================================================


def test_disallow_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENTS", False)
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENT_TYPES", "qoder_cli")
    assert A._disallow_subagents_enabled("qoder_cli") is False


def test_disallow_gate_flag_and_type(monkeypatch):
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENTS", True)
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENT_TYPES", "qoder_cli,codex_cli")
    assert A._disallow_subagents_enabled("qoder_cli") is True
    assert A._disallow_subagents_enabled("codex_cli") is True
    # 白名单外的类型不受影响
    assert A._disallow_subagents_enabled("deepseek_cli") is False


# ============================================================
# 互斥不变式:命中禁止 → 续轮回收关闭 → 落盘段不再注入
# ============================================================


def test_disallow_forces_autoccontinue_off(monkeypatch):
    # 常规白名单本应允许 qoder_cli 续轮回收
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", True)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_TYPES", "qoder_cli")
    assert A._async_agent_autoccontinue_enabled("qoder_cli") is True

    # 开启禁止 fan-out:同一 agent_type 的续轮回收被强制关闭
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENTS", True)
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENT_TYPES", "qoder_cli")
    assert A._async_agent_autoccontinue_enabled("qoder_cli") is False


def test_disallow_suppresses_reports_section(monkeypatch):
    """raw_subagent 由 _async_agent_autoccontinue_enabled 把门:关闭即不注入落盘段。

    复现 run 链路 raw_subagent 的决策门(见 acp_base run_acp_agent 内同名判定):
    命中禁止 → autocontinue False → raw_subagent 为空串。
    """
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_AUTOCONTINUE", True)
    monkeypatch.setattr(settings, "ACP_ASYNC_AGENT_TYPES", "qoder_cli")
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENTS", True)
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENT_TYPES", "qoder_cli")

    agent_type = "qoder_cli"
    # 门:命中禁止即 autocontinue False,故 raw_subagent 保持空串(与 run 链路一致)
    raw_subagent = ""
    if A._async_agent_autoccontinue_enabled(agent_type):
        raw_subagent = build_cli_subagent_reports_section("/tmp/reports")

    assert raw_subagent == ""
    # 对照:未命中禁止时同一门会产出非空落盘段(证明判定确实随开关翻转)
    monkeypatch.setattr(settings, "ACP_DISALLOW_SUBAGENTS", False)
    assert A._async_agent_autoccontinue_enabled(agent_type) is True
