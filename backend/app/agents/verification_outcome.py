"""动态验证结论的结构化口径(PoC 到底验出了什么)

背景:verify 回给 agent2 的是自然语言总结,而证据台账此前只记"这次验证跑没跑成"
(`verify_ok` = 不以 `[验证失败` 开头)。于是"环境不可达 / 用户没授权 / 跑完但没定论"
也被当成成功条目:模型只要把 call_ref 填进 `evidence.verification`,审查项就拿到
「已动态验证」档(0.92)——档位含义从"验出了结论"漂成"试过了"。

本模块把结论收敛成固定几档,并规定**只有定论档**才配得上「已动态验证」:
- `confirmed`      已确认可利用(定论)
- `not_confirmed`  已核实不成立 / 确认为误报(定论)
- `inconclusive`   动作执行了,但响应看不出差异(未定论)
- `unreachable`    测试环境/端点不可达,等于没验成(未定论)
- `not_authorized` 用户拒绝或超时未授权,动作根本没执行(未定论)
- `aborted`        审查被终止/被新一轮执行取代,验证中途收手(未定论)
- `unknown`        未按契约申报(未定论,fail-closed)

注意 `not_confirmed` 是定论:被 PoC 驳回的发现同样经过动态验证,归"已核实不成立"。
"""
from __future__ import annotations

import re
from typing import Any

OUTCOME_CONFIRMED = "confirmed"
OUTCOME_NOT_CONFIRMED = "not_confirmed"
OUTCOME_INCONCLUSIVE = "inconclusive"
OUTCOME_UNREACHABLE = "unreachable"
OUTCOME_NOT_AUTHORIZED = "not_authorized"
OUTCOME_ABORTED = "aborted"
OUTCOME_UNKNOWN = "unknown"

# 只有这两档算"验出了结论",可支撑「已动态验证」置信档
DEFINITIVE_OUTCOMES: frozenset[str] = frozenset({OUTCOME_CONFIRMED, OUTCOME_NOT_CONFIRMED})

ALL_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_CONFIRMED, OUTCOME_NOT_CONFIRMED, OUTCOME_INCONCLUSIVE,
    OUTCOME_UNREACHABLE, OUTCOME_NOT_AUTHORIZED, OUTCOME_ABORTED, OUTCOME_UNKNOWN,
})

OUTCOME_LABELS: dict[str, str] = {
    OUTCOME_CONFIRMED: "已确认可利用",
    OUTCOME_NOT_CONFIRMED: "已核实不成立",
    OUTCOME_INCONCLUSIVE: "已执行·未定论",
    OUTCOME_UNREACHABLE: "测试环境不可达",
    OUTCOME_NOT_AUTHORIZED: "未获授权·动作未执行",
    OUTCOME_ABORTED: "验证已中止",
    OUTCOME_UNKNOWN: "未申报结论",
}

# 单目标结论的同义词归一(模型常写 confirmed=true / "已确认" / exploit_success 等)
_RESULT_ALIASES: dict[str, str] = {
    "confirmed": OUTCOME_CONFIRMED,
    "confirm": OUTCOME_CONFIRMED,
    "true": OUTCOME_CONFIRMED,
    "yes": OUTCOME_CONFIRMED,
    "success": OUTCOME_CONFIRMED,
    "exploitable": OUTCOME_CONFIRMED,
    "已确认": OUTCOME_CONFIRMED,
    "确认": OUTCOME_CONFIRMED,
    "可利用": OUTCOME_CONFIRMED,
    "not_confirmed": OUTCOME_NOT_CONFIRMED,
    "false_positive": OUTCOME_NOT_CONFIRMED,
    "not_exploitable": OUTCOME_NOT_CONFIRMED,
    "rejected": OUTCOME_NOT_CONFIRMED,
    "false": OUTCOME_NOT_CONFIRMED,
    "no": OUTCOME_NOT_CONFIRMED,
    "未确认": OUTCOME_NOT_CONFIRMED,
    "不成立": OUTCOME_NOT_CONFIRMED,
    "误报": OUTCOME_NOT_CONFIRMED,
    "inconclusive": OUTCOME_INCONCLUSIVE,
    "partial": OUTCOME_INCONCLUSIVE,
    "未定论": OUTCOME_INCONCLUSIVE,
    "unreachable": OUTCOME_UNREACHABLE,
    "timeout": OUTCOME_UNREACHABLE,
    "error": OUTCOME_UNREACHABLE,
    "不可达": OUTCOME_UNREACHABLE,
    "denied": OUTCOME_NOT_AUTHORIZED,
    "rejected_by_user": OUTCOME_NOT_AUTHORIZED,
    "not_authorized": OUTCOME_NOT_AUTHORIZED,
    "拒绝": OUTCOME_NOT_AUTHORIZED,
    "aborted": OUTCOME_ABORTED,
    "cancelled": OUTCOME_ABORTED,
    "canceled": OUTCOME_ABORTED,
    "stopped": OUTCOME_ABORTED,
    "中止": OUTCOME_ABORTED,
}

# 兜底:总结正文里的申报行 `[验证结论] outcome=confirmed`(模型没用工具时的契约)
_OUTCOME_LINE_RE = re.compile(r"outcome\s*=\s*([a-zA-Z_\u4e00-\u9fff]+)", re.IGNORECASE)


def normalize_result(raw: Any) -> str | None:
    """把模型写的单目标结论归一到档位;认不出返回 None(由调用方按未定论处理)"""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    if key in ALL_OUTCOMES:
        return key
    return _RESULT_ALIASES.get(key)


def is_definitive(outcome: Any) -> bool:
    """该结论是否"验出了定论"(只有 confirmed / not_confirmed 算)"""
    return isinstance(outcome, str) and outcome in DEFINITIVE_OUTCOMES


def normalize_outcome(raw: Any) -> str:
    """整体结论归一;非法值一律 unknown(fail-closed,不白送置信档)"""
    norm = normalize_result(raw)
    return norm if norm and norm in ALL_OUTCOMES else OUTCOME_UNKNOWN


def aggregate_targets(targets: list[dict[str, Any]]) -> str:
    """多目标 → 一个整体结论(取最弱一环,fail-closed)。

    规则:只要有目标没定论(未定论/不可达/未授权/未申报),整批就不认定论
    → inconclusive;全部定论时任一 confirmed → confirmed,否则 not_confirmed。
    为什么不让"其中一条 confirmed"把整批抬上去:台账引用号是**一次 verify**的
    粒度,而审查项只能指向 E#、指不到"那一批里的第三条"。只要混进了未定论
    目标就当作坐实,没定论的那条会跟着搭便车拿到「已动态验证」——那正是这个
    改造要堆掉的假可信。代价(一批里有一条验不成就连坐)靠"把同一次都指望
    能出结论的发现放在一起验"的提示词约定来避开。
    """
    results = [normalize_result(t.get("result")) for t in (targets or []) if isinstance(t, dict)]
    results = [r for r in results if r in ALL_OUTCOMES]
    if not results:
        return OUTCOME_UNKNOWN
    if any(r not in DEFINITIVE_OUTCOMES for r in results):
        return OUTCOME_INCONCLUSIVE
    if OUTCOME_CONFIRMED in results:
        return OUTCOME_CONFIRMED
    return OUTCOME_NOT_CONFIRMED


def parse_outcome_line(text: Any) -> str | None:
    """从 verifier 总结正文里抓 `[验证结论] outcome=xxx` 申报行;抓不到返回 None。

    只作 submit_verification 工具缺席时的兜底:工具通道是首选契约
    (结构化回执可核验),正文申报容易被模型写成散文。
    """
    if not isinstance(text, str) or not text:
        return None
    m = _OUTCOME_LINE_RE.findall(text)
    for raw in reversed(m):
        norm = normalize_result(raw)
        if norm in ALL_OUTCOMES:
            return norm
    return None


def outcome_note(outcome: str) -> str:
    """回灌给 agent2 的一行结论标注(让它明确看到"定论/未定论")"""
    label = OUTCOME_LABELS.get(outcome, OUTCOME_LABELS[OUTCOME_UNKNOWN])
    return f"[验证结论] outcome={outcome}（{label}）"
