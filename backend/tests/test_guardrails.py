"""Guardrail tests: the auto-resubmit gate, the fix whitelist, and apply_fix
determinism / immutability."""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agent.guardrails import apply_fix, enforce_route, validate_fix
from app.models import (
    Citation,
    Claim,
    ClaimLine,
    Fix,
    FixOperation,
    Route,
    TriageDecision,
)


def _claim() -> Claim:
    return Claim(
        claim_id="CLM-T1",
        payer_id="87726",
        payer_name="UnitedHealthcare",
        provider_npi="1093817465",
        provider_name="Lakeside Orthopedics LLC",
        patient_ref="PAT-00001",
        subscriber_id="SUB123456789",
        date_of_service="2026-03-12",
        date_submitted="2026-03-16",
        diagnoses=["M25.511", "M23.221"],
        lines=[
            ClaimLine(line_number=1, cpt_hcpcs="99213", icd10_pointers=["M25.511"], charge=Decimal("225.00")),
            ClaimLine(line_number=2, cpt_hcpcs="29881", icd10_pointers=["M23.221"], charge=Decimal("8750.00")),
        ],
        total_charge=Decimal("8975.00"),
    )


def _cite() -> Citation:
    return Citation(source_type="ncci_edit", source_id="NCCI-29881/99213", quote="bundles without modifier 25")


def _fix(*ops: FixOperation) -> Fix:
    return Fix(fix_id="FIX-T1", claim_id="CLM-T1", operations=list(ops), citation=_cite())


def test_triage_decision_model_rejects_bad_auto():
    with pytest.raises(ValidationError):
        TriageDecision(
            route=Route.AUTO_FIX_RESUBMIT,
            confidence=0.5,  # too low
            value_at_stake=Decimal("100.00"),
            rationale="x",
            citations=[_cite()],
        )
    with pytest.raises(ValidationError):
        TriageDecision(
            route=Route.AUTO_FIX_RESUBMIT,
            confidence=0.95,
            value_at_stake=Decimal("5000.00"),  # too high
            rationale="x",
            citations=[_cite()],
        )


def test_enforce_route_reroutes_and_allows():
    route, note = enforce_route(Route.AUTO_FIX_RESUBMIT, 0.95, Decimal("5000.00"))
    assert route is Route.HUMAN_REVIEW and "blocked" in note
    route, note = enforce_route(Route.AUTO_FIX_RESUBMIT, 0.92, Decimal("225.00"))
    assert route is Route.AUTO_FIX_RESUBMIT and "allowed" in note
    route, note = enforce_route(Route.APPEAL, 0.1, Decimal("99999.00"))
    assert route is Route.APPEAL and note is None


def test_validate_fix_rejects_paths_outside_whitelist():
    fix = validate_fix(_fix(
        FixOperation(field_path="payer_id", op="set", new_value="60054", reason="nope"),
        FixOperation(field_path="lines[0].charge", op="set", new_value="1.00", reason="nope"),
        FixOperation(field_path="lines[9].modifiers", op="add", new_value="25", reason="bad index"),
    ), _claim())
    assert fix.validated is False
    assert "payer_id" in fix.validation_error
    assert "lines[0].charge" in fix.validation_error
    assert "out of range" in fix.validation_error


def test_validate_fix_accepts_whitelisted_ops():
    fix = validate_fix(_fix(
        FixOperation(field_path="lines[0].modifiers", op="add", new_value="25", reason="NCCI bypass"),
        FixOperation(field_path="prior_auth_number", op="set", new_value="A-2026-1111", reason="auth on file"),
    ), _claim())
    assert fix.validated is True and fix.validation_error is None


def test_apply_fix_is_deterministic_and_never_mutates():
    claim = _claim()
    fix = validate_fix(_fix(
        FixOperation(field_path="lines[0].modifiers", op="add", new_value="25", reason="NCCI bypass"),
    ), claim)

    first = apply_fix(fix, claim)
    second = apply_fix(fix, claim)
    assert first == second  # deterministic: pure function of (fix, claim)
    assert first is not claim
    assert first.lines[0].modifiers == ["25"]
    assert first.revision == claim.revision + 1
    assert claim.lines[0].modifiers == []  # original untouched
    assert claim.revision == 0


def test_apply_fix_refuses_unvalidated_fix():
    claim = _claim()
    fix = _fix(FixOperation(field_path="lines[0].modifiers", op="add", new_value="25", reason="x"))
    with pytest.raises(ValueError, match="validated"):
        apply_fix(fix, claim)  # validate_fix was never run
