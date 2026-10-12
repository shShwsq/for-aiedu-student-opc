"""规划对账与上下文护栏的纯函数测试(agent2 收尾链)

背景(实测任务 f4ff8649):旧对账拿计划 target 与发射 review_target 做"归一化后
互为子串",两条各自成句的长中文几乎不可能互为子串 → 8 条计划**全部**被判未执行,
凭空伪造 8 条 missing 审查项(聚合显示"核实 0 / 回填缺口 8")。现改为
plan_item_id 精确命中 + 重合度兜底,且未核实项不再落库。

顺带锚定取证额度 100 次的配套护栏:超出窗口的工具回传压成一行摘要,
但**不动证据台账**(call_ref 核验行为与不裁剪时一致)。
"""
from app.agents.agent2 import (
    MAX_LIVE_TOOL_RESULTS,
    _budget_exhausted,
    _reconcile_plan,
    _stamp_plan_items,
    _targets_overlap,
    _trim_agent2_tool_history,
)


# ============================================================
# 计划 id 铸造
# ============================================================

def test_stamp_plan_items_assigns_sequential_ids_and_drops_junk():
    stamped = _stamp_plan_items([
        {"target": "JWT 默认密钥", "origin": "agent1_claim"},
        {"target": "  SSRF 面  ", "origin": "agent1_claim", "priority": 2},
        {"origin": "agent1_claim"},          # 无 target → 丢
        "不是 dict 的脏条目",                  # → 丢
    ])
    assert [p["id"] for p in stamped] == ["P1", "P2"]
    assert stamped[1]["target"] == "SSRF 面"    # 首尾空白已去
    assert stamped[1]["priority"] == 2          # 原字段保留


def test_stamp_plan_items_empty_input():
    assert _stamp_plan_items([]) == []
    assert _stamp_plan_items(None) == []


# ============================================================
# target 重合度兜底(旧"互为子串"的替代)
# ============================================================

def test_overlap_matches_reworded_targets():
    """同一对象、各自成句的两种写法:旧子串匹配失配,重合度能跨过阈值"""
    plan_t = "JWT 默认密钥 fail-closed 缺失的断言(config.py:34 默认值、主流程是否校验)"
    item_t = "agent1 断言:JWT 默认密钥无 fail-closed、security.py:70/121 直接签发校验、主流程不校验"
    assert _targets_overlap(plan_t, item_t) >= 0.5
    # 旧口径(互为子串)在这一对上两条方向都不成立——正是 8/8 全失配的成因
    assert plan_t not in item_t and item_t not in plan_t


def test_overlap_keeps_different_targets_apart():
    a = "base_url / test_env_url 未校验导致的 SSRF 面(schemas/model_configs.py)"
    b = "匿名任务对任何人可读写的权限边界(tasks.py、workspace.py)"
    assert _targets_overlap(a, b) < 0.5


def test_overlap_empty_side_is_zero():
    assert _targets_overlap("", "任意对象") == 0.0
    assert _targets_overlap("任意对象", "") == 0.0


# ============================================================
# 对账主体:id 优先,重合度兜底,未核实只返回不落库
# ============================================================

def test_reconcile_prefers_plan_item_id():
    plan = _stamp_plan_items([
        {"target": "JWT 默认密钥", "origin": "agent1_claim"},
        {"target": "并发安全", "origin": "domain_baseline"},
    ])
    emitted = [
        # id 指名 P2,而 review_target 与计划文本完全不同句 —— 只能靠 id 命中
        {"plan_item_id": "P2", "review_target": "threading.Lock 未保护共享字典"},
    ]
    matched, unverified = _reconcile_plan(plan, emitted)
    assert matched == {"P2"}
    assert [p["id"] for p in unverified] == ["P1"]


def test_reconcile_falls_back_to_overlap_without_id():
    plan = _stamp_plan_items([
        {"target": "users.py SQL 注入风险", "origin": "agent1_claim"},
        {"target": "OAuth 无 state 参数", "origin": "agent1_claim"},
    ])
    emitted = [{"plan_item_id": None, "review_target": "users.py 的 SQL 注入风险"}]
    matched, unverified = _reconcile_plan(plan, emitted)
    assert matched == {"P1"}
    assert [p["id"] for p in unverified] == ["P2"]


def test_reconcile_unknown_plan_item_id_is_not_matched():
    """模型编了个 P99:不匹配任何计划项,但也不报错(审查项照常保留)"""
    plan = _stamp_plan_items([{"target": "JWT 默认密钥", "origin": "agent1_claim"}])
    matched, unverified = _reconcile_plan(
        plan, [{"plan_item_id": "P99", "review_target": "完全不相干的一句话"}]
    )
    assert matched == set()
    assert [p["id"] for p in unverified] == ["P1"]


def test_reconcile_empty_plan_matches_nothing():
    assert _reconcile_plan([], [{"review_target": "x"}]) == (set(), [])


# ============================================================
# 额度触顶判定(未核实项的 reason)
# ============================================================

def test_budget_exhausted_only_when_a_cap_is_hit():
    assert _budget_exhausted({"tool_counts": {"read": 3, "verify": 0, "reference": 0}}) is False
    assert _budget_exhausted({"tool_counts": {"read": 100}}) is True
    assert _budget_exhausted({"tool_counts": {"verify": 3}}) is True
    assert _budget_exhausted({}) is False


# ============================================================
# 上下文护栏:压旧回传正文,不动台账
# ============================================================

def _tool_msgs(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        out.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        out.append({
            "role": "tool", "tool_call_id": f"c{i}",
            "content": f'{{"_evidence_ref": "E{i}", "content": "x' + "9" * 500 + '"}',
        })
    return out


def test_trim_keeps_recent_window_and_stubs_older():
    messages = _tool_msgs(MAX_LIVE_TOOL_RESULTS + 5)
    ledger = {f"E{i}": {"args_brief": f"read_file:backend/app/f{i}.py"} for i in range(100)}
    _trim_agent2_tool_history(messages, ledger)

    tool_contents = [m["content"] for m in messages if m["role"] == "tool"]
    stubbed = [c for c in tool_contents if c.startswith("[取证记录")]
    assert len(stubbed) == 5
    # 摘要保留引用号与参数索引(模型据此不重复取证),且远短于原文
    assert "E0" in stubbed[0] and "read_file:backend/app/f0.py" in stubbed[0]
    assert len(stubbed[0]) < 300
    # 最近窗口内正文原样保留
    assert tool_contents[-1].startswith('{"_evidence_ref"')


def test_trim_is_idempotent_and_keeps_pairing():
    messages = _tool_msgs(MAX_LIVE_TOOL_RESULTS + 3)
    ledger = {"E0": {"args_brief": "read_file:a.py"}}
    _trim_agent2_tool_history(messages, ledger)
    before = [m.get("tool_call_id") for m in messages if m["role"] == "tool"]
    _trim_agent2_tool_history(messages, ledger)
    after = [m.get("tool_call_id") for m in messages if m["role"] == "tool"]
    # 不删消息(OpenAI 兼容接口要求 tool 与 assistant.tool_calls 成对)
    assert before == after
    stubs = [m["content"] for m in messages if m["role"] == "tool"
             and m["content"].startswith("[取证记录")]
    assert len(stubs) == 3
    # 未被引用的 ref 不编造 args_brief
    assert "E1" in stubs[1]
