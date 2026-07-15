"""Orchestrator tests.

Scripted tests (run offline): a fake Anthropic client drives the REAL loop,
REAL tools, and REAL ChromaDB retrieval, proving the mechanics the task
demands — event stream shape, uncited-finding dropping, the auto-resubmit
guardrail, and the 12-turn budget.

Live test (needs ANTHROPIC_API_KEY / backend/.env): DEN-007 end-to-end — the
model must reject the stated CARC 16 reason, find the bundling itself, and
route to AUTO_FIX_RESUBMIT.

Run as pytest:   python -m pytest tests/test_orchestrator.py -v   (from backend/)
Run as script:   python tests/test_orchestrator.py               -> live DEN-007 trace
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import Route, TraceEvent, TraceEventType  # noqa: E402
from app.services import claim_repo, memory  # noqa: E402

HAS_KEY = bool(settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Fresh claim cache + throwaway memory dir per test (runs mutate both)."""
    monkeypatch.setattr(claim_repo, "_claims", None)
    monkeypatch.setattr(claim_repo, "_denials", None)
    monkeypatch.setattr(claim_repo, "FEEDS_DIR", tmp_path / "feeds")
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / "memory")


def _collect(denial_id: str) -> list[TraceEvent]:
    from app.agent.orchestrator import process_denial

    async def run() -> list[TraceEvent]:
        denial = claim_repo.get_denial(denial_id)
        return [event async for event in process_denial(denial)]

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# Scripted-model harness
# --------------------------------------------------------------------------- #


def _text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def _tool(name: str, tool_input: dict, id_: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=id_)


def _fake_anthropic(script: list[list[SimpleNamespace]], call_log: list[int]):
    """AsyncAnthropic stand-in returning scripted content blocks per turn."""

    class _Messages:
        async def create(self, **kwargs):
            i = len(call_log)
            call_log.append(i)
            blocks = script[min(i, len(script) - 1)]
            return SimpleNamespace(content=blocks)

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    return _Client


_CITE_NCCI = {
    "source_type": "ncci_edit",
    "source_id": "NCCI-29881/99213",
    "quote": "E/M on the same day as knee arthroscopy bundles unless a separately identifiable service is documented; append modifier 25 to the E/M",
}
_CITE_UHC = {
    "source_type": "payer_policy",
    "source_id": "UHC-CP-044",
    "quote": "a CARC 16 'missing information' denial on an E/M line billed with a same-day procedure is frequently a modifier issue",
}
_CITE_INVENTED = {
    "source_type": "payer_policy",
    "source_id": "FAKE-999",  # never returned by any tool -> must be dropped
    "quote": "made-up evidence",
}

HERO_SCRIPT = [
    [
        _text("CARC 16 often masks bundling — the arthroscopy paid and only the E/M denied. Investigating."),
        _tool("carc_lookup", {"carc": "16", "rarc": "N54"}, "tu_1"),
        _tool("ncci_edit_check", {"cpt_codes": ["99213", "29881"]}, "tu_2"),
    ],
    [
        _tool("policy_retrieve", {"query": "modifier 25 same day E/M", "payer": "UnitedHealthcare"}, "tu_3"),
        _tool("resubmission_history", {"payer": "UnitedHealthcare", "carc": "16", "fix_type": "append modifier 25"}, "tu_4"),
    ],
    [
        _tool(
            "propose_fix",
            {
                "operations": [
                    {
                        "field_path": "lines[0].modifiers",
                        "op": "add",
                        "new_value": "25",
                        "reason": "Separately identifiable E/M documented; NCCI indicator 1 allows bypass.",
                    }
                ],
                "citation": _CITE_NCCI,
            },
            "tu_fix",
        ),
    ],
    [
        _tool(
            "record_root_cause",
            {
                "category": "bundling_ncci",
                "summary": "The stated 16/N54 'missing information' is a red herring: the 99213 bundled into the same-day 29881 because modifier 25 was missing.",
                "implicated_codes": ["16", "N54", "97"],
                "is_correctable": True,
                "confidence": 0.92,
                "citations": [_CITE_NCCI, _CITE_UHC, _CITE_INVENTED],
            },
            "tu_5",
        ),
        _tool(
            "record_decision",
            {
                "route": "auto_fix_resubmit",
                "confidence": 0.92,
                "rationale": "Append modifier 25 to line 1 and resubmit as corrected claim; paid precedent exists.",
                "rejected_routes": {
                    "appeal": "Nothing to argue — the claim is fixable as coded.",
                    "write_off": "$225 is recoverable with a one-field fix.",
                    "human_review": "Evidence is unambiguous and value is under the auto cap.",
                },
                "citations": [_CITE_NCCI, _CITE_UHC],
            },
            "tu_6",
        ),
    ],
]


def test_scripted_hero_loop_streams_all_event_types():
    from app.agent import orchestrator

    calls: list[int] = []
    orchestrator.AsyncAnthropic = _fake_anthropic(HERO_SCRIPT, calls)
    try:
        events = _collect("DEN-007")
    finally:
        from anthropic import AsyncAnthropic

        orchestrator.AsyncAnthropic = AsyncAnthropic

    types = [e.type for e in events]
    for required in (
        TraceEventType.STARTED,
        TraceEventType.THOUGHT,
        TraceEventType.TOOL_CALL,
        TraceEventType.TOOL_RESULT,
        TraceEventType.CONTEXT_RETRIEVED,
        TraceEventType.FIX_PROPOSED,
        TraceEventType.FIX_VALIDATED,
        TraceEventType.ROOT_CAUSE,
        TraceEventType.DECISION,
        TraceEventType.FIX_APPLIED,
        TraceEventType.RESUBMITTED,
        TraceEventType.COMPLETED,
    ):
        assert required in types, f"missing {required} in stream"
    assert types[-1] is TraceEventType.COMPLETED

    root_cause = next(e for e in events if e.type is TraceEventType.ROOT_CAUSE)
    assert root_cause.payload["category"] == "bundling_ncci"
    assert root_cause.payload["citations_dropped"] == 1  # FAKE-999 dropped in code
    assert {c.source_id for c in root_cause.citations} == {"NCCI-29881/99213", "UHC-CP-044"}

    decision = next(e for e in events if e.type is TraceEventType.DECISION)
    assert decision.payload["route"] == "auto_fix_resubmit"
    assert decision.payload["value_at_stake"] == "225.00"  # computed from denial, not model

    # deterministic apply: modifier 25 landed on a COPY, revision bumped, then accepted
    applied = next(e for e in events if e.type is TraceEventType.FIX_APPLIED)
    assert applied.payload["claim"]["lines"][0]["modifiers"] == ["25"]
    assert applied.payload["claim"]["revision"] == 1
    resubmitted = next(e for e in events if e.type is TraceEventType.RESUBMITTED)
    assert resubmitted.payload["status"] == "accepted"

    # memory: outcome stored
    outcomes = memory.all_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["denial_id"] == "DEN-007"
    assert outcomes[0]["resubmit_status"] == "accepted"
    assert len(calls) <= 12


def test_guardrail_reroutes_high_value_auto_to_human_review():
    """DEN-001 is $9,800 — an auto_fix decision must be forced to human_review."""
    from app.agent import orchestrator

    cite_197 = {
        "source_type": "carc_definition",
        "source_id": "CARC-197",
        "quote": "Precertification/authorization/notification absent.",
    }
    script = [
        [_tool("carc_lookup", {"carc": "197"}, "tu_1")],
        [
            _tool("record_root_cause", {
                "category": "auth_required", "summary": "Auth obtained but never keyed onto the claim.",
                "is_correctable": True, "confidence": 0.95, "citations": [cite_197],
            }, "tu_2"),
            _tool("record_decision", {
                "route": "auto_fix_resubmit", "confidence": 0.95,
                "rationale": "Key the auth number and resubmit.",
                "rejected_routes": {"appeal": "n/a", "write_off": "n/a", "human_review": "n/a"},
                "citations": [cite_197],
            }, "tu_3"),
        ],
    ]
    calls: list[int] = []
    orchestrator.AsyncAnthropic = _fake_anthropic(script, calls)
    try:
        events = _collect("DEN-001")
    finally:
        from anthropic import AsyncAnthropic

        orchestrator.AsyncAnthropic = AsyncAnthropic

    assert any(e.type is TraceEventType.ROUTED_TO_HUMAN for e in events)
    decision = next(e for e in events if e.type is TraceEventType.DECISION)
    assert decision.payload["route"] == Route.HUMAN_REVIEW.value
    assert decision.payload["requested_route"] == "auto_fix_resubmit"
    assert "blocked" in decision.payload["guardrail_note"]


def test_turn_budget_forces_human_review():
    from app.agent import orchestrator

    script = [[_text("Still thinking...")]]  # never records a decision
    calls: list[int] = []
    orchestrator.AsyncAnthropic = _fake_anthropic(script, calls)
    try:
        events = _collect("DEN-018")
    finally:
        from anthropic import AsyncAnthropic

        orchestrator.AsyncAnthropic = AsyncAnthropic

    assert len(calls) == settings.max_agent_turns  # hard cap: 12
    decision = next(e for e in events if e.type is TraceEventType.DECISION)
    assert decision.payload["route"] == Route.HUMAN_REVIEW.value
    assert events[-1].type is TraceEventType.COMPLETED


def test_appeal_route_drafts_letter():
    """DEN-017 (Aetna CARC 50 medical necessity) must produce an appeal letter
    grounded in the run's citations — via template when the model yields none."""
    from app.agent import orchestrator

    cite_50 = {
        "source_type": "carc_definition",
        "source_id": "CARC-50",
        "quote": "These are non-covered services because this is not deemed a medical necessity by the payer.",
    }
    script = [
        [_tool("carc_lookup", {"carc": "50", "rarc": "N115"}, "tu_1")],
        [
            _tool("record_root_cause", {
                "category": "medical_necessity",
                "summary": "PT visits exceeded Aetna's therapy threshold; progress notes on file support continued care.",
                "is_correctable": False, "confidence": 0.88, "citations": [cite_50],
            }, "tu_2"),
            _tool("record_decision", {
                "route": "appeal", "confidence": 0.88,
                "rationale": "Appeal with clinical documentation mapped to the LCD criteria.",
                "rejected_routes": {"auto_fix_resubmit": "no claim edit fixes a necessity denial",
                                    "write_off": "documentation supports the care",
                                    "human_review": "clear appeal path with precedent"},
                "citations": [cite_50],
            }, "tu_3"),
        ],
    ]
    calls: list[int] = []
    orchestrator.AsyncAnthropic = _fake_anthropic(script, calls)
    try:
        events = _collect("DEN-017")
    finally:
        from anthropic import AsyncAnthropic

        orchestrator.AsyncAnthropic = AsyncAnthropic

    drafted = next(e for e in events if e.type is TraceEventType.APPEAL_DRAFTED)
    letter = drafted.payload["letter"]
    assert drafted.payload["generated_by"] == "template"  # fake model returned no text
    assert "CLM-017" in letter and "Aetna" in letter and "CARC-50" in letter
    assert "First-Level Appeal" in letter

    record = memory.get_decision_record("DEN-017")
    assert record["appeal_letter"] == letter
    assert record["route"] == "appeal"


def test_failed_resubmit_writes_lesson():
    """DEN-006's claim has a placeholder subscriber ID; a fix that ignores it gets
    rejected by the clearinghouse, which must write a (payer, CARC) lesson."""
    from app.agent import orchestrator

    cite_16 = {
        "source_type": "carc_definition",
        "source_id": "CARC-16",
        "quote": "Claim/service lacks information or has submission/billing error(s).",
    }
    script = [
        [_tool("carc_lookup", {"carc": "16"}, "tu_1")],
        [_tool("propose_fix", {
            "operations": [{"field_path": "lines[0].modifiers", "op": "add",
                            "new_value": "25", "reason": "wrong fix on purpose"}],
            "citation": cite_16,
        }, "tu_2")],
        [
            _tool("record_root_cause", {
                "category": "missing_info", "summary": "Missing info per remit.",
                "is_correctable": True, "confidence": 0.90, "citations": [cite_16],
            }, "tu_3"),
            _tool("record_decision", {
                "route": "auto_fix_resubmit", "confidence": 0.90,
                "rationale": "Fix and resubmit.",
                "rejected_routes": {"appeal": "n/a", "write_off": "n/a", "human_review": "n/a"},
                "citations": [cite_16],
            }, "tu_4"),
        ],
    ]
    calls: list[int] = []
    orchestrator.AsyncAnthropic = _fake_anthropic(script, calls)
    try:
        events = _collect("DEN-006")
    finally:
        from anthropic import AsyncAnthropic

        orchestrator.AsyncAnthropic = AsyncAnthropic

    resubmitted = next(e for e in events if e.type is TraceEventType.RESUBMITTED)
    assert resubmitted.payload["status"] == "rejected"

    denial = claim_repo.get_denial("DEN-006")
    learned = [x for x in memory.lessons_for(denial.payer_name, "16") if x["source"] == "learned"]
    assert learned, "failed resubmit did not write a lesson"
    assert "failed" in learned[0]["lesson"]

    outcome = memory.all_outcomes()[-1]
    assert outcome["denial_id"] == "DEN-006" and outcome["resubmit_status"] == "rejected"

    # the lesson is injected into the NEXT run for this payer/CARC
    from app.agent.prompts import build_user_message

    claim = claim_repo.get_claim(denial.claim_id)
    prompt = build_user_message(denial, claim, memory.lessons_for(denial.payer_name, "16"))
    assert "Lessons from past attempts" in prompt and learned[0]["lesson"] in prompt


# --------------------------------------------------------------------------- #
# Live end-to-end: the hero case
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAS_KEY, reason="ANTHROPIC_API_KEY not set (add it to backend/.env)")
def test_den_007_live_rejects_red_herring_and_routes_auto_fix():
    events = _collect("DEN-007")

    errors = [e.message for e in events if e.type is TraceEventType.ERROR]
    assert not errors, f"run aborted before triage — check the API key: {errors[0]}"

    tool_calls = [e.payload["tool"] for e in events if e.type is TraceEventType.TOOL_CALL]
    assert "ncci_edit_check" in tool_calls, "agent never checked for bundling"

    root_cause = next(e for e in events if e.type is TraceEventType.ROOT_CAUSE)
    assert root_cause.payload["category"] == "bundling_ncci", (
        f"agent accepted the stated reason: {root_cause.payload['category']}"
    )

    decision = next(e for e in events if e.type is TraceEventType.DECISION)
    assert decision.payload["route"] == Route.AUTO_FIX_RESUBMIT.value
    assert float(decision.payload["confidence"]) > 0.85
    assert decision.citations, "decision must carry citations"


def main() -> None:
    if not HAS_KEY:
        print("ANTHROPIC_API_KEY not set — add it to backend/.env to run the live DEN-007 trace.")
        sys.exit(2)
    from app.agent.orchestrator import process_denial

    async def run() -> None:
        denial = claim_repo.get_denial("DEN-007")
        async for e in process_denial(denial):
            print(f"[{e.seq:02d}] {e.type.value:<18} {e.message[:160]}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
