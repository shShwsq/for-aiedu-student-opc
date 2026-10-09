"""evidence.py 纯函数回归测试(置信度派生 + quote 吻合 + 台账记录器)

覆盖 score_review_item 的核心判读:
- call_ref 命中 + quote 吻合 → source_confirmed
- call_ref 不在台账 → evidence_mismatch + 压到 assertion_only
- verify 成功台账 + verified=True → verified
- has_repo=False 缺 source → 不判 mismatch
- false_positive + source_ok → 至少 source_confirmed(mismatch 例外保底)
- 无 evidence → confidence None
外加 EvidenceRecorder 的 E# 分配与回灌注入。
"""
import json

from app.agents.evidence import (
    EvidenceRecorder,
    TIER_ASSERTION,
    TIER_SOURCE,
    TIER_VERIFIED,
    TIER_REFERENCE,
    derive_confidence_column,
    quote_matches,
    score_review_item,
)


def _read_body(path="src/api/users.py", content="    42\tcursor.execute(sql)\n    43\t"):
    # read_file 结果被 _execute_read_tool 序列化为 JSON 字符串存进台账
    return json.dumps(
        {"path": path, "content": content, "start_line": 42, "end_line": 43},
        ensure_ascii=False,
    )


# ============================================================
# score_review_item
# ============================================================

def test_source_confirmed_when_quote_matches():
    ledger = {"E1": {"kind": "read", "body": _read_body()}}
    ev = {"source": {"file_path": "src/api/users.py", "line": "42",
                     "quote": "cursor.execute(sql)", "call_ref": "E1"}}
    out = score_review_item(ev, "confirmed", ledger, has_repo=True, verify_available=False)
    assert out["tier"] == TIER_SOURCE
    assert out["evidence_validated"] is True
    assert out["evidence_mismatch"] is False
    assert out["score"] > 0.5


def test_mismatch_when_call_ref_not_in_ledger():
    ledger = {}
    ev = {"source": {"file_path": "x.py", "quote": "abc", "call_ref": "E9"}}
    out = score_review_item(ev, "confirmed", ledger, has_repo=True, verify_available=False)
    assert out["tier"] == TIER_ASSERTION
    assert out["evidence_mismatch"] is True
    assert out["note"]


def test_quote_mismatch_presses_to_assertion():
    ledger = {"E1": {"kind": "read", "body": _read_body(content="    42\tpass\n")}}
    ev = {"source": {"file_path": "src/api/users.py", "quote": "cursor.execute(sql)",
                     "call_ref": "E1"}}
    out = score_review_item(ev, "confirmed", ledger, has_repo=True, verify_available=False)
    assert out["tier"] == TIER_ASSERTION
    assert out["evidence_mismatch"] is True


def test_verified_tier_needs_success_verify_ledger():
    ledger = {
        "E1": {"kind": "read", "body": _read_body()},
        "E3": {"kind": "verify", "body": "PoC 成功:返回了全部用户", "verify_ok": True},
    }
    ev = {
        "source": {"file_path": "src/api/users.py", "quote": "cursor.execute(sql)", "call_ref": "E1"},
        "verification": {"method": "poc", "verified": True, "poc_evidence": "...", "call_ref": "E3"},
    }
    out = score_review_item(ev, "confirmed", ledger, has_repo=True, verify_available=True)
    assert out["tier"] == TIER_VERIFIED
    # verified + source 两类佐证 → 基线 + 0.03
    assert out["score"] == round(0.92 + 0.03, 3)


def test_verify_unavailable_does_not_mismatch():
    ledger = {}
    ev = {"verification": {"method": "poc", "verified": True, "call_ref": "E1"}}
    out = score_review_item(ev, "suspected", ledger, has_repo=False, verify_available=False)
    # 无 verify 环境:不判 mismatch,优雅降级为仅断言
    assert out["evidence_mismatch"] is False
    assert out["tier"] == TIER_ASSERTION


def test_no_repo_source_not_mismatch():
    ledger = {}
    ev = {"source": {"file_path": "x.py", "quote": "abc", "call_ref": "E1"}}
    out = score_review_item(ev, "confirmed", ledger, has_repo=False, verify_available=False)
    assert out["evidence_mismatch"] is False


def test_reference_corroborated():
    ledger = {"E5": {"kind": "reference", "body": json.dumps({"reachable": True})}}
    ev = {"analysis_basis": {"ref_url": "https://nvd.nist.gov/CWE-89",
                             "ref_status": "ok", "ref_authority": "authoritative",
                             "call_ref": "E5"}}
    out = score_review_item(ev, "confirmed", ledger, has_repo=False, verify_available=False)
    assert out["tier"] == TIER_REFERENCE
    assert out["evidence_validated"] is True


def test_false_positive_with_source_ok_floors_at_source():
    # source_ok 成立,但 verification 指向非成功 verify → 会置 mismatch;
    # false_positive + source_ok 例外保底 source_confirmed
    ledger = {
        "E1": {"kind": "read", "body": _read_body()},
        "E3": {"kind": "verify", "body": "误报", "verify_ok": False},
    }
    ev = {
        "source": {"file_path": "src/api/users.py", "quote": "cursor.execute(sql)", "call_ref": "E1"},
        "verification": {"verified": True, "call_ref": "E3"},
    }
    out = score_review_item(ev, "false_positive", ledger, has_repo=True, verify_available=True)
    assert out["tier"] == TIER_SOURCE
    assert out["evidence_mismatch"] is True


def test_no_evidence_returns_none():
    assert score_review_item(None, "confirmed", {}, has_repo=True, verify_available=True) is None
    assert score_review_item({}, "pending", {}, has_repo=True, verify_available=True) is None


def test_assertion_only_when_empty_refs():
    # 带 evidence 但各段都无有效引用 → 仅断言
    ev = {"source": {"file_path": "x.py"}}  # 无 call_ref / quote 无台账
    out = score_review_item(ev, "suspected", {}, has_repo=True, verify_available=False)
    assert out["tier"] == TIER_ASSERTION


# ============================================================
# quote 吻合 + 归一化
# ============================================================

def test_quote_matches_strips_line_numbers_and_whitespace():
    body = _read_body(content="    42\tcursor.execute(sql)\n    43\tx=1\n")
    # 单行:剥 cat -n 行号后吻合
    assert quote_matches("cursor.execute(sql)", body) is True
    # 多行:剥行号后按空白折叠成一行,跨行 quote 也能命中
    assert quote_matches("cursor.execute(sql) x=1", body) is True
    # quote 里的多余空白被折叠,仍命中
    assert quote_matches("cursor.execute(sql)    x=1", body) is True


def test_derive_confidence_column():
    assert derive_confidence_column(None) is None
    sc = {"tier": TIER_SOURCE, "label": "源码取证", "score": 0.78,
          "evidence_validated": True, "evidence_mismatch": False, "note": None}
    assert derive_confidence_column(sc) == {"tier": TIER_SOURCE, "label": "源码取证", "score": 0.78}


# ============================================================
# EvidenceRecorder(Part A)
# ============================================================

def test_recorder_assigns_refs_and_injects_into_json():
    rec = EvidenceRecorder()
    out = rec.record_read(json.dumps({"path": "a.py", "content": "1\tx"}))
    obj = json.loads(out)
    assert obj["_evidence_ref"] == "E1"
    assert rec.ledger["E1"]["kind"] == "read"

    out2 = rec.record_read(json.dumps({"path": "b.py"}))
    assert json.loads(out2)["_evidence_ref"] == "E2"


def test_recorder_verify_appends_text_marker():
    rec = EvidenceRecorder()
    out = rec.record_verify("PoC 成功", verify_ok=True)
    assert "[evidence_ref=E1]" in out
    assert rec.ledger["E1"]["kind"] == "verify"
    assert rec.ledger["E1"]["verify_ok"] is True


def test_recorder_reference_kind():
    rec = EvidenceRecorder()
    out = rec.record_reference(json.dumps({"snippet": "hi"}))
    assert json.loads(out)["_evidence_ref"] == "E1"
    assert rec.ledger["E1"]["kind"] == "reference"
