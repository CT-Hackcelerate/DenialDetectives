"""One test per agent tool, all through the execute_tool dispatcher.

Run as pytest:   python -m pytest tests/tools_test.py -v      (from backend/)
Run as script:   python tests/tools_test.py                    -> pass/fail table
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.tools import execute_tool  # noqa: E402


def test_carc_lookup():
    result = execute_tool("carc_lookup", {"carc": "197", "rarc": "M62"})
    assert result["found"] is True
    assert "Precertification" in result["description"]
    assert result["category"] == "auth_required"
    assert result["rarc_meaning"].startswith("Missing/incomplete/invalid treatment authorization")
    assert result["citations"][0]["source_type"] == "carc_definition"


def test_ncci_edit_check():
    result = execute_tool("ncci_edit_check", {"cpt_codes": ["99213", "29881"]})
    assert result["edit_count"] == 1
    edit = result["edits"][0]
    assert (edit["column1_cpt"], edit["column2_cpt"]) == ("29881", "99213")
    assert edit["modifier_indicator"] == "1" and edit["bypass_allowed"] is True
    assert "modifier 25" in edit["rationale"]
    assert edit["citation"]["source_id"] == "NCCI-29881/99213"


def test_policy_retrieve():
    result = execute_tool("policy_retrieve", {"query": "timely filing deadline", "payer": "Cigna"})
    assert result["matches"], "no policy chunks retrieved"
    top = result["matches"][0]
    assert top["policy_number"] == "CIG-ADM-081" and top["payer"] == "Cigna"
    assert top["citation"]["chroma_doc_id"]


def test_prior_auth_status():
    # Required + missing: Aetna's own policy (AET-SURG-014) mandates precert for 29881.
    result = execute_tool("prior_auth_status", {"payer": "Aetna", "cpt": "29881"})
    assert result["auth_required"] is True
    assert result["status"] == "auth_missing"
    assert result["citations"][0]["source_id"] == "AET-SURG-014"
    # Same CPT with an auth number on file.
    with_auth = execute_tool(
        "prior_auth_status", {"payer": "Aetna", "cpt": "29881", "auth_number": "A-2026-3187"}
    )
    assert with_auth["status"] == "auth_on_file"


def test_timely_filing_check():
    result = execute_tool(
        "timely_filing_check",
        {"payer": "Cigna", "date_of_service": "2025-08-04", "submission_date": "2026-04-15"},
    )
    assert result["days_elapsed"] == 254
    assert result["filing_limit_days"] == 180
    assert result["within_limit"] is False
    assert result["limit_source"] == "payer_policy"  # read from CIG-ADM-081, not the default


def test_resubmission_history():
    result = execute_tool(
        "resubmission_history",
        {"payer": "UnitedHealthcare", "carc": "16", "fix_type": "append modifier 25"},
    )
    assert result["precedent_count"] >= 1
    assert result["has_successful_precedent"] is True  # RSB-006 paid after modifier 25
    ids = {m["resubmission_id"] for m in result["matches"]}
    assert "RSB-006" in ids


def test_submit_claim():
    good = {
        "claim_id": "CLM-TEST",
        "payer_id": "62308",
        "subscriber_id": "SUB123456789",
        "lines": [{"line_number": 1, "cpt_hcpcs": "99213", "icd10_pointers": ["I10"]}],
    }
    accepted = execute_tool("submit_claim", {"claim": good})
    assert accepted["status"] == "accepted"
    assert accepted["trace_id"].startswith("TRC-") and accepted["payer_ack_code"] == "A2"

    bad = {**good, "subscriber_id": "SUB-TEMP"}
    rejected = execute_tool("submit_claim", {"claim": bad})
    assert rejected["status"] == "rejected" and rejected["errors"]


def test_dispatcher_never_raises():
    assert "error" in execute_tool("no_such_tool", {})
    assert "error" in execute_tool("carc_lookup", {"bogus_arg": True})
    assert "error" in execute_tool(
        "timely_filing_check",
        {"payer": "Cigna", "date_of_service": "not-a-date", "submission_date": "2026-01-01"},
    )


TESTS = [
    test_carc_lookup,
    test_ncci_edit_check,
    test_policy_retrieve,
    test_prior_auth_status,
    test_timely_filing_check,
    test_resubmission_history,
    test_submit_claim,
    test_dispatcher_never_raises,
]


def main() -> None:
    rows = []
    for fn in TESTS:
        try:
            fn()
            rows.append((fn.__name__, "PASS", ""))
        except AssertionError as exc:
            rows.append((fn.__name__, "FAIL", str(exc) or "assertion failed"))
        except Exception as exc:  # noqa: BLE001
            rows.append((fn.__name__, "ERROR", f"{type(exc).__name__}: {exc}"))

    width = max(len(name) for name, _, _ in rows)
    print(f"\n{'test'.ljust(width)}  result  detail")
    print(f"{'-' * width}  ------  ------")
    for name, status, detail in rows:
        print(f"{name.ljust(width)}  {status:<6}  {detail}")
    failed = sum(1 for _, s, _ in rows if s != "PASS")
    print(f"\n{len(rows) - failed}/{len(rows)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
