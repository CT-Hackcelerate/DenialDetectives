"""API tests: lessons, batch, stats, approve/override, and the demo cache
(record on first run -> replay offline). All driven by scripted models."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import claim_repo, demo_cache, memory  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(claim_repo, "_claims", None)
    monkeypatch.setattr(claim_repo, "_denials", None)
    monkeypatch.setattr(claim_repo, "FEEDS_DIR", tmp_path / "feeds")
    monkeypatch.setattr(memory, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(demo_cache, "CACHE_DIR", tmp_path / "demo_cache")
    monkeypatch.setattr(settings, "demo_mode", "live")
    monkeypatch.setattr(settings, "demo_replay_delay", 0.0)


# --------------------------------------------------------------------------- #
# Scripted model helpers
# --------------------------------------------------------------------------- #


def _tool(name: str, tool_input: dict, id_: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=id_)


def _scripted(script: list[list[SimpleNamespace]]):
    class _Messages:
        def __init__(self):
            self.i = 0

        async def create(self, **kwargs):
            blocks = script[min(self.i, len(script) - 1)]
            self.i += 1
            return SimpleNamespace(content=blocks)

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    return _Client


class _Boom:
    def __init__(self, **kwargs):
        raise AssertionError("model was called — replay mode must not touch the model")


_CITE_16 = {"source_type": "carc_definition", "source_id": "CARC-16",
            "quote": "Claim/service lacks information."}
_CITE_197 = {"source_type": "carc_definition", "source_id": "CARC-197",
             "quote": "Precertification/authorization/notification absent."}

WRITE_OFF_SCRIPT = [
    [_tool("carc_lookup", {"carc": "16"}, "tu_1")],
    [
        _tool("record_root_cause", {
            "category": "unknown", "summary": "Scripted.", "is_correctable": False,
            "confidence": 0.9, "citations": [_CITE_16],
        }, "tu_2"),
        _tool("record_decision", {
            "route": "write_off", "confidence": 0.9, "rationale": "Scripted.",
            "rejected_routes": {"appeal": "n/a", "auto_fix_resubmit": "n/a", "human_review": "n/a"},
            "citations": [_CITE_16],
        }, "tu_3"),
    ],
]

# Auth fix on a $9,800 claim: auto request gets rerouted to human_review with a
# validated-but-unapplied fix on file — exactly what /api/approve consumes.
AUTH_FIX_SCRIPT = [
    [_tool("carc_lookup", {"carc": "197"}, "tu_1")],
    [_tool("propose_fix", {
        "operations": [{"field_path": "prior_auth_number", "op": "set",
                        "new_value": "A-2026-4417", "reason": "auth obtained by phone"}],
        "citation": _CITE_197,
    }, "tu_2")],
    [
        _tool("record_root_cause", {
            "category": "auth_required", "summary": "Auth never keyed onto the claim.",
            "is_correctable": True, "confidence": 0.95, "citations": [_CITE_197],
        }, "tu_3"),
        _tool("record_decision", {
            "route": "auto_fix_resubmit", "confidence": 0.95, "rationale": "Key auth, resubmit.",
            "rejected_routes": {"appeal": "n/a", "write_off": "n/a", "human_review": "n/a"},
            "citations": [_CITE_197],
        }, "tu_4"),
    ],
]


def _run_sse(path: str) -> list[str]:
    """Consume an SSE stream, returning the event names (pings filtered)."""
    names: list[str] = []
    with client.stream("GET", path) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
                if name != "ping":
                    names.append(name)
    return names


def _with_model(script_client):
    from app.agent import orchestrator

    orchestrator.AsyncAnthropic = script_client


def _restore_model():
    from anthropic import AsyncAnthropic

    from app.agent import orchestrator

    orchestrator.AsyncAnthropic = AsyncAnthropic


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_get_lessons_is_seeded_with_three():
    lessons = client.get("/api/lessons").json()
    assert len(lessons) == 3
    assert all(x["source"] == "seed" for x in lessons)
    assert {x["payer"] for x in lessons} == {"UnitedHealthcare", "Aetna", "Cigna"}


def test_denials_and_claims_endpoints():
    denials = client.get("/api/denials").json()
    assert len(denials) >= 20
    assert client.get(f"/api/denials/{denials[0]['denial_id']}").status_code == 200
    assert client.get("/api/denials/DEN-999").status_code == 404
    assert client.get(f"/api/claims/{denials[0]['claim_id']}").status_code == 200


def test_process_records_trace_then_replays_offline():
    _with_model(_scripted(WRITE_OFF_SCRIPT))
    try:
        live_events = _run_sse("/api/process/DEN-003")
    finally:
        _restore_model()
    assert "completed" in live_events
    assert demo_cache.has_trace("DEN-003")  # saved on first run

    # replay: poison the model to prove it is never touched, no wifi needed
    _with_model(_Boom)
    try:
        settings.demo_mode = "replay"
        memory._write("outcomes.json", [])  # fresh slate to prove replay records
        replayed = _run_sse("/api/process/DEN-003")
        assert replayed == live_events  # identical trace, served from cache
        assert client.get("/api/process/DEN-004").status_code == 409  # not cached
        # replay parity: the replayed trace's outcome lands in stats
        outcome = memory.latest_outcomes_by_denial().get("DEN-003")
        assert outcome and outcome["route"] == "write_off"
        replayed_again = _run_sse("/api/process/DEN-003")
        assert replayed_again == live_events
        assert len(memory.all_outcomes()) == 2  # re-recorded, but...
        stats = client.get("/api/stats").json()
        assert stats["route_counts"] == {"write_off": 1}  # ...latest-wins, no double count
    finally:
        settings.demo_mode = "live"
        _restore_model()


def test_batch_runs_all_and_warms_cache():
    _with_model(_scripted(WRITE_OFF_SCRIPT))
    try:
        body = client.post("/api/batch").json()
    finally:
        _restore_model()
    count = len(claim_repo.list_denials())
    assert body["count"] == count
    assert body["by_route"] == {"write_off": count}
    assert all(demo_cache.has_trace(r["denial_id"]) for r in body["results"])

    # replay batch summarizes from cache without the model
    _with_model(_Boom)
    try:
        settings.demo_mode = "replay"
        replay_body = client.post("/api/batch").json()
        assert replay_body["by_route"] == {"write_off": count}
    finally:
        settings.demo_mode = "live"
        _restore_model()


def test_stats_approve_recovers_dollars():
    _with_model(_scripted(AUTH_FIX_SCRIPT))
    try:
        events = _run_sse("/api/process/DEN-001")  # $9,800 -> rerouted to human_review
    finally:
        _restore_model()
    assert "routed_to_human" in events

    stats = client.get("/api/stats").json()
    assert stats["route_counts"] == {"human_review": 1}
    assert stats["dollars_recovered"] == "0.00"

    approved = client.post("/api/approve/DEN-001").json()
    assert approved["status"] == "approved"
    assert approved["resubmit_status"] == "accepted"
    assert approved["fix"]["applied"] is True

    stats = client.get("/api/stats").json()
    assert stats["dollars_recovered"] == "9800.00"
    assert client.post("/api/approve/DEN-999").status_code == 404


def _feed_claim(claim_id: str = "CLM-900") -> dict:
    return {
        "claim_id": claim_id,
        "payer_id": "62308",
        "payer_name": "Cigna",
        "provider_npi": "1245319878",
        "provider_name": "Riverbend Family Medicine",
        "patient_ref": "PAT-90001",
        "subscriber_id": "SUB900000001",
        "date_of_service": "2026-06-01",
        "date_submitted": "2026-06-05",
        "prior_auth_number": None,
        "diagnoses": ["I10"],
        "lines": [{"line_number": 1, "cpt_hcpcs": "99213", "modifiers": [],
                   "icd10_pointers": ["I10"], "units": 1, "charge": "185.00",
                   "place_of_service": "11"}],
        "total_charge": "185.00",
    }


def _feed_denial(denial_id: str = "DEN-900", claim_id: str = "CLM-900") -> dict:
    return {
        "denial_id": denial_id,
        "claim_id": claim_id,
        "payer_id": "62308",
        "payer_name": "Cigna",
        "remit_date": "2026-06-25",
        "adjustments": [{"group_code": "CO", "carc": "16", "rarc": "N382",
                         "amount": "185.00", "line_number": None}],
        "total_denied": "185.00",
        "remit_note": "Claim/service lacks information.",
        "payer_context": None,
    }


def test_feed_accepts_valid_batch_and_persists():
    before = len(client.get("/api/denials").json())
    response = client.post("/api/feed", json={"claims": [_feed_claim()], "denials": [_feed_denial()]})
    assert response.status_code == 200
    assert response.json()["accepted"] == {"claims": 1, "denials": 1}

    denials = client.get("/api/denials").json()
    assert len(denials) == before + 1
    assert client.get("/api/claims/CLM-900").status_code == 200

    # persisted: survives a repo reload
    claim_repo._claims = None
    claim_repo._denials = None
    assert client.get("/api/denials/DEN-900").status_code == 200


def test_feed_rejects_bad_batches_atomically():
    # schema violation (no lines) + orphan denial + duplicate id + bad math
    bad_claim = _feed_claim("CLM-901"); bad_claim["lines"] = []
    orphan = _feed_denial("DEN-902", "CLM-DOES-NOT-EXIST")
    dup = _feed_denial("DEN-001")  # already exists in the seed data
    bad_math = _feed_claim("CLM-903"); bad_math["total_charge"] = "999.00"
    response = client.post("/api/feed", json={
        "claims": [bad_claim, bad_math, _feed_claim("CLM-904")],
        "denials": [orphan, dup],
    })
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert any("CLM-901" in e for e in errors)
    assert any("total_charge" in e and "CLM-903" in e for e in errors)
    assert any("CLM-DOES-NOT-EXIST" in e for e in errors)
    assert any("duplicate denial_id" in e for e in errors)
    # atomic: the one valid claim was NOT imported
    assert client.get("/api/claims/CLM-904").status_code == 404
    # empty feed rejected too
    assert client.post("/api/feed", json={}).status_code == 422


def test_report_aggregates_by_payer():
    body = client.get("/api/report").json()
    assert body["totals"]["denials"] == len(claim_repo.list_denials())
    assert float(body["totals"]["denied"]) > 55000
    payers = {p["payer"] for p in body["payers"]}
    assert payers == {"Aetna", "UnitedHealthcare", "Cigna", "Blue Cross Blue Shield"}
    assert sum(p["denials"] for p in body["payers"]) == body["totals"]["denials"]
    assert abs(sum(float(p["denied"]) for p in body["payers"]) - float(body["totals"]["denied"])) < 0.01
    for p in body["payers"]:
        assert p["top_carcs"] and p["top_carcs"][0]["description"]
        assert p["avg_remit_lag_days"] is None or p["avg_remit_lag_days"] > 0
        assert set(p["fix_history"]) == {"paid", "denied_again"}
    assert body["carcs"][0]["denied"] >= body["carcs"][-1]["denied"]  # sorted by $

    # outcomes flow into the report: process one denial, then recovery shows up
    _with_model(_scripted(AUTH_FIX_SCRIPT))
    try:
        _run_sse("/api/process/DEN-001")
    finally:
        _restore_model()
    client.post("/api/approve/DEN-001")
    body = client.get("/api/report").json()
    aetna = next(p for p in body["payers"] if p["payer"] == "Aetna")
    assert float(aetna["recovered"]) == 9800.00
    assert aetna["processed"] == 1


def test_override_changes_route_in_stats():
    _with_model(_scripted(WRITE_OFF_SCRIPT))
    try:
        _run_sse("/api/process/DEN-003")
    finally:
        _restore_model()

    overridden = client.post("/api/override/DEN-003", json={"route": "appeal", "reason": "payer erred"}).json()
    assert overridden["status"] == "overridden"
    assert overridden["route"] == "appeal" and overridden["agent_route"] == "write_off"

    stats = client.get("/api/stats").json()
    assert stats["route_counts"] == {"appeal": 1}
    assert client.post("/api/override/DEN-999", json={"route": "appeal"}).status_code == 404
