"""证据台账 + 引用核验 + 置信度派生(证据驱动可信审查的核心)

设计要点:
- **置信度后端派生,模型绝不自报数字**。模型自报置信度校准差("会幻觉可信度"
  的模型同样会"幻觉 92%")。confidence 是**证据链的属性**,不是口供。
- **工具作"发射器",不作 true/false 裁判**。可信只来自**真执行并观测**的工具
  (verify 跑 PoC、read/search 命中真实代码),绝不把模型自填的 `confirmed=true`
  当证据。score_review_item 只核验"引用的证据是否真取过 + quote 是否吻合"。
- **优雅降级**:无 repo → 不核验 source;无 verify 环境 → verified 断言不采信
  但不判 mismatch;缺段=不适用。缺 PoC 不因"没跑成"而判不完整。

本模块为纯函数 + 轻量记录器,不依赖 DB / LLM,便于单测。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.agents.verification_outcome import DEFINITIVE_OUTCOMES, OUTCOME_UNKNOWN

# ============================================================
# 置信档位(取最高一档成立)
# ============================================================

TIER_VERIFIED = "verified"                    # 已动态验证(verify 跑成 PoC)
TIER_SOURCE = "source_confirmed"              # 源码取证(read/search + quote 吻合)
TIER_REFERENCE = "reference_corroborated"     # 外部佐证(check_reference ok + 权威)
TIER_ASSERTION = "assertion_only"             # 仅断言·待核实(引用缺失/核验不通过)

TIER_LABELS: dict[str, str] = {
    TIER_VERIFIED: "已动态验证",
    TIER_SOURCE: "源码取证",
    TIER_REFERENCE: "外部佐证",
    TIER_ASSERTION: "仅断言·待核实",
}

# cat -n 行号前缀(行首可选空白 + 数字 + 分隔),quote 剥离用
_LINE_NO_PREFIX = re.compile(r"^\s*\d+[\t \u00a0\u2007\u202f]*")
_WS = re.compile(r"\s+")


# ============================================================
# 文本归一化 + quote 吻合(宽松子串匹配)
# ============================================================


def _flatten_text(obj: Any) -> str:
    """把 dict/list/str 递归拼成纯文本(read_file 的 JSON 结果取正文用)"""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_flatten_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "\n".join(_flatten_text(v) for v in obj)
    if obj is None:
        return ""
    return str(obj)


def _strip_line_numbers(text: str) -> str:
    """逐行剥离 cat -n 行号前缀(模型 quote 不带行号、正文带,先剥再比)"""
    return "\n".join(_LINE_NO_PREFIX.sub("", ln) for ln in text.splitlines())


def _collapse_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def normalize_for_quote_match(raw: Any) -> str:
    """归一化证据正文/quote → 可比字符串:剥 JSON 取正文、去行号前缀、空白折叠。

    raw 可为 JSON 字符串(read/reference 工具回灌文本)、dict、或纯文本。
    """
    if isinstance(raw, dict) or isinstance(raw, list):
        text = _flatten_text(raw)
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        text = _flatten_text(parsed) if isinstance(parsed, (dict, list)) else raw
    else:
        text = "" if raw is None else str(raw)
    return _collapse_ws(_strip_line_numbers(text))


def quote_matches(quote: Any, body: Any) -> bool:
    """quote 是否为 body 的归一化宽松子串(空 quote 返回 False,由调用方判缺引用)"""
    needle = _collapse_ws(_strip_line_numbers(str(quote or "")))
    if not needle:
        return False
    return needle in normalize_for_quote_match(body)


# ============================================================
# 台账记录器(Part A):分配 _evidence_ref + 存证 + 回灌注入
# ============================================================


class EvidenceRecorder:
    """一轮审查内的证据台账:取证工具调用 → E1/E2… 引用号,回灌前注入、正文入台账。

    - read/search:JSON dict 结果加 `result["_evidence_ref"]=eref`
    - verify:natural-language 文本末尾追加 `[evidence_ref=E#]`(kind=verify)
    - reference:JSON dict 结果加 `_evidence_ref`(kind=reference)
    台账存**回灌用的正文**(quote 校验对齐模型实际所见);call_id 供追溯(可空)。
    """

    def __init__(self) -> None:
        self.ledger: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def _next_ref(self) -> str:
        self._seq += 1
        return f"E{self._seq}"

    def _inject_json_ref(self, result_str: str, eref: str) -> str:
        """把 _evidence_ref 注入 JSON 字符串结果;非合法 JSON(如已截断)则尾部追加标记"""
        try:
            obj = json.loads(result_str)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            obj["_evidence_ref"] = eref
            return json.dumps(obj, ensure_ascii=False, default=str)
        return f"{result_str}\n[evidence_ref={eref}]"

    def record_read(self, result_str: str, *, call_id: str | None = None,
                    args_brief: str = "") -> str:
        """read_file/list_files/find_files/search_code 取证结果入台账,返回注入 ref 的回灌文本"""
        eref = self._next_ref()
        self.ledger[eref] = {
            "kind": "read", "call_id": call_id, "args_brief": args_brief,
            "body": result_str,
        }
        return self._inject_json_ref(result_str, eref)

    def record_reference(self, result_str: str, *, call_id: str | None = None,
                         args_brief: str = "") -> str:
        """check_reference 取证结果入台账,返回注入 ref 的回灌文本"""
        eref = self._next_ref()
        self.ledger[eref] = {
            "kind": "reference", "call_id": call_id, "args_brief": args_brief,
            "body": result_str,
        }
        return self._inject_json_ref(result_str, eref)

    def record_verify(self, text: str, *, verify_ok: bool, outcome: str | None = None,
                      call_id: str | None = None,
                      args_brief: str = "") -> str:
        """verify 动态验证结果(natural-language)入台账,末尾追加 [evidence_ref=E#]

        `verify_ok` 只说"这次验证跑成了(没异常)",**不能支撑「已动态验证」档**;
        真正定论的是 `outcome`(见 verification_outcome.py):只有 confirmed /
        not_confirmed 这两档算定论。环境不可达、用户未授权、跑了但没看出差异
        都是"试过了",拿源码/引用档就好。
        """
        eref = self._next_ref()
        self.ledger[eref] = {
            "kind": "verify", "call_id": call_id, "args_brief": args_brief,
            "body": text, "verify_ok": bool(verify_ok),
            "outcome": outcome or OUTCOME_UNKNOWN,
        }
        return f"{text}\n[evidence_ref={eref}]"

    def adopt_entries(self, entries: dict[str, dict]) -> dict[str, str]:
        """把并行子台账按计划插入顺序整批搬进主台账,重新编号为新的 E#。

        背景:计划项并行核查时每个 worker 持自己的 EvidenceRecorder(线程边界:
        worker 不碰主台账);fan-in 主线程收养各 worker 的条目,否则
        submit_review_item 的 call_ref 指向的局部 ref 在主台账里查无此人,
        score_review_item 会把所有并行结论压成 assertion_only。

        body 原样保留(quote 子串匹配对齐的是台账正文,不碰 ref 编号);
        verify 类条目的 verify_ok/outcome 一并搬入,定论口径不变。

        返回 旧 ref → 新 ref 映射(含未搬入的非法条目跳过);调用方据此改写
        结论文本里的引用号(见 remap_refs_in_text)。
        """
        mapping: dict[str, str] = {}
        for old_ref, entry in (entries or {}).items():
            if not isinstance(entry, dict):
                continue
            new_ref = self._next_ref()
            adopted = dict(entry)
            adopted["call_id"] = (
                f"{entry.get('call_id') or ''}|adopted:{old_ref}" or entry.get("call_id")
            )
            self.ledger[new_ref] = adopted
            mapping[str(old_ref)] = new_ref
        return mapping


# 结论/信封文本里的证据引用号(\b 不单独适用于 CJK 语境,E 前紧邻字母/数字不算引用)
_EVIDENCE_REF_RE = re.compile(r"(?<![A-Za-z0-9_])E(\d+)(?![A-Za-z0-9_])")


def remap_refs_in_text(text: str, mapping: dict[str, str]) -> str:
    """把文本里的 E# 引用号按映射改写(未命中映射的引用号剥离并标注无效)

    并行 worker 结论里的 E1/E2… 是局部台账编号,收养后主台账重新编号;
    不改写则 call_ref 对不上主台账(命中校验①直接 mismatch)。
    未命中映射的 E# 是 worker 臆造或属于其他 worker 的局部编号,若原样保留会
    指向主台账里别人的证据(张冠李戴),故剔字并标注(P2-2)。
    mapping 为空时不动文本(worker 未产生局部台账 → 无可改写,不误伤)。
    """
    if not text or not mapping:
        return text or ""

    def _repl(m: "re.Match[str]") -> str:
        return mapping.get(m.group(0)) or "（无效引用）"

    return _EVIDENCE_REF_RE.sub(_repl, text)


def _lookup(ledger: dict[str, dict[str, Any]], call_ref: Any) -> dict[str, Any] | None:
    if not call_ref or not isinstance(call_ref, str):
        return None
    return ledger.get(call_ref.strip())


# ============================================================
# 置信度派生(Part B)
# ============================================================


def score_review_item(
    evidence: dict | None,
    verdict: str | None,
    ledger: dict | None,
    *,
    has_repo: bool,
    verify_available: bool,
) -> dict | None:
    """带 evidence 者:核验 call_ref 命中台账 + quote 吻合,派生 confidence 档。

    缺口/无证据者返回 None(confidence 不写)。

    返回:
        {tier, label, evidence_validated, evidence_mismatch,
         verification_outcome, note}
    校验规则:
      ① 非空 call_ref 必须命中台账,否则 evidence_mismatch;
      ② source.call_ref 指向 read/search 时对 quote 做归一化宽松子串匹配,不吻合 → mismatch;
      ③ verified=True 仅当指向"跑成且定论"(outcome 为 confirmed|not_confirmed)的
        verify 台账;跑成了但未定论(不可达/未授权/未申报)不抬档也不判 mismatch,
        指向跑失败的台账才判 mismatch(无 verify 环境时不采信也不判 mismatch);
      ④ ref_ok = ref_status=ok 且 authority∈{authoritative,credible} 且 call_ref 指向 reference 台账。
    取最高一档成立;mismatch 压到 assertion_only,
    但"false_positive 且 source_ok"例外保底 source_confirmed(误报剔除也要证据)。
    """
    if not isinstance(evidence, dict) or not evidence:
        return None
    ledger = ledger or {}
    source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    analysis = evidence.get("analysis_basis") if isinstance(evidence.get("analysis_basis"), dict) else {}
    verification = evidence.get("verification") if isinstance(evidence.get("verification"), dict) else {}

    mismatch = False
    source_ok = ref_ok = verified_ok = False
    verify_inconclusive = False
    verification_outcome: str | None = None

    # ① source(仅在有工作区时可核验;无 repo 缺 source 不判 mismatch)
    if has_repo and (source.get("file_path") or source.get("quote")):
        cref = source.get("call_ref")
        entry = _lookup(ledger, cref)
        if cref and entry is None:
            mismatch = True
        elif entry is not None and entry.get("kind") == "read":
            quote = source.get("quote")
            if quote:
                if quote_matches(quote, entry.get("body")):
                    source_ok = True
                else:
                    mismatch = True
            else:
                # 指向真实取证调用但无 quote:file_path 定位即算源码取证(宽松)
                source_ok = True

    # ② analysis_basis → ref_ok
    if analysis.get("ref_url"):
        cref = analysis.get("call_ref")
        entry = _lookup(ledger, cref)
        if cref and entry is None:
            mismatch = True
        elif (
            entry is not None
            and entry.get("kind") == "reference"
            and analysis.get("ref_status") == "ok"
            and analysis.get("ref_authority") in ("authoritative", "credible")
        ):
            ref_ok = True

    # ③ verification → verified_ok(仅当指向"跑出定论"的台账)
    #   跑成了但结论未定论(环境不可达/未授权/没申报)不判 mismatch——
    #   那是"试过但没验出来",不是"谎报引用";但不能拿它换「已动态验证」档。
    if verification.get("verified") is True:
        cref = verification.get("call_ref")
        entry = _lookup(ledger, cref)
        if not verify_available:
            verified_ok = False  # 无 verify 环境:优雅降级,不判 mismatch
        elif cref and entry is None:
            mismatch = True
        elif entry is not None and entry.get("kind") == "verify":
            outcome = entry.get("outcome") or OUTCOME_UNKNOWN
            verification_outcome = outcome
            if entry.get("verify_ok") and outcome in DEFINITIVE_OUTCOMES:
                verified_ok = True
            elif not entry.get("verify_ok"):
                mismatch = True  # 声称 verified 却指向一条跑失败的验证
            else:
                verify_inconclusive = True
        else:
            mismatch = True  # 声称 verified 却指向非 verify 台账

    # 选档:mismatch 压到 assertion_only(false_positive + source_ok 例外保底)
    if mismatch:
        if verdict == "false_positive" and source_ok:
            tier = TIER_SOURCE
        else:
            tier = TIER_ASSERTION
    elif verified_ok:
        tier = TIER_VERIFIED
    elif source_ok:
        tier = TIER_SOURCE
    elif ref_ok:
        tier = TIER_REFERENCE
    else:
        tier = TIER_ASSERTION

    validated = (not mismatch) and (source_ok or ref_ok or verified_ok)
    if mismatch:
        note = "引用未核验通过,置信下调为仅断言"
    elif verify_inconclusive:
        note = f"动态验证未定论({verification_outcome}),置信按其余证据"
    else:
        note = None
    return {
        "tier": tier,
        "label": TIER_LABELS[tier],
        "evidence_validated": bool(validated),
        "evidence_mismatch": bool(mismatch),
        "verification_outcome": verification_outcome,
        "note": note,
    }


def derive_confidence_column(score: dict | None) -> dict | None:
    """从 score_review_item 返回体抽出写入 ReviewItem.confidence 的 {tier,label}"""
    if not score:
        return None
    return {"tier": score["tier"], "label": score["label"]}
