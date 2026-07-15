"""Agent endpoints.

  * GET  /api/process/{denial_id} — SSE stream of TraceEvents. Live runs are
    recorded to the demo cache on first run; with DEMO_MODE=replay the cached
    trace is served at fixed pacing (no model, no network).
  * POST /api/batch — run the agent over every denial (live), or summarize
    cached traces (replay). A live batch warms the entire demo cache.
  * POST /api/approve/{denial_id} — human approves the agent's decision; a
    pending validated fix is applied and resubmitted.
  * POST /api/override/{denial_id} — human overrides the route.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.guardrails import apply_fix, validate_fix
from app.agent.orchestrator import process_denial
from app.agent.tools import execute_tool
from app.models import Denial, Fix, Route, TraceEvent, TraceEventType
from app.services import claim_repo, demo_cache, memory

router = APIRouter(prefix="/api", tags=["triage"])


async def _live_and_record(denial: Denial):
    """Run the real agent, saving the full trace to the demo cache (first run only).

    Persistence happens BEFORE the completed event is yielded: browsers close the
    EventSource as soon as they see it, which cancels this generator — code after
    the loop would never run.
    """
    events: list[TraceEvent] = []
    async for event in process_denial(denial):
        events.append(event)
        if event.type is TraceEventType.COMPLETED:
            demo_cache.save_trace(denial.denial_id, events)
        yield event


def _record_outcome_from_events(denial: Denial, events: list[TraceEvent]) -> None:
    """Replay parity: store the replayed trace's outcome so stats/tallies update.

    Keyed by denial_id with latest-wins semantics, so repeated replays
    overwrite rather than double-count.
    """
    completed = next((e for e in events if e.type is TraceEventType.COMPLETED), None)
    if completed is None or not completed.payload.get("route"):
        return
    decision = next((e for e in events if e.type is TraceEventType.DECISION), None)
    memory.record_outcome(
        denial_id=denial.denial_id,
        payer=denial.payer_name,
        carc=denial.adjustments[0].carc,
        route=completed.payload["route"],
        root_cause_category=completed.payload.get("root_cause_category"),
        confidence=float(decision.payload.get("confidence", 0.0)) if decision else 0.0,
        resubmit_status=completed.payload.get("resubmit_status"),
    )


async def _replay_and_record(denial: Denial):
    """Serve the cached trace, recording its outcome like a live run would.

    Recorded before the completed event is yielded — the client disconnects on
    seeing it, which would cancel this generator.
    """
    events: list[TraceEvent] = []
    async for event in demo_cache.replay(denial.denial_id):
        events.append(event)
        if event.type is TraceEventType.COMPLETED:
            _record_outcome_from_events(denial, events)
        yield event


def _event_stream(denial: Denial):
    if demo_cache.is_replay():
        if not demo_cache.has_trace(denial.denial_id):
            raise HTTPException(
                status_code=409,
                detail=f"DEMO_MODE=replay but no cached trace for {denial.denial_id}; "
                       "run it live (or POST /api/batch) once to record.",
            )
        return _replay_and_record(denial)
    return _live_and_record(denial)


@router.get("/process/{denial_id}")
async def process_stream(denial_id: str) -> EventSourceResponse:
    denial = claim_repo.get_denial(denial_id)
    if denial is None:
        raise HTTPException(status_code=404, detail=f"denial {denial_id} not found")
    source = _event_stream(denial)

    async def sse():
        async for event in source:
            yield {"event": event.type.value, "data": event.model_dump_json()}

    return EventSourceResponse(sse())


def _summary_from_events(denial: Denial, events: list[TraceEvent]) -> dict:
    summary = {
        "denial_id": denial.denial_id,
        "claim_id": denial.claim_id,
        "payer_name": denial.payer_name,
        "total_denied": str(denial.total_denied),
        "route": None,
        "root_cause_category": None,
        "resubmit_status": None,
        "events": len(events),
    }
    for event in events:
        if event.type is TraceEventType.COMPLETED:
            summary["route"] = event.payload.get("route")
            summary["root_cause_category"] = event.payload.get("root_cause_category")
            summary["resubmit_status"] = event.payload.get("resubmit_status")
    return summary


@router.post("/batch")
async def batch() -> dict:
    """Triage every denial. Live mode also warms the demo cache."""
    results = []
    for denial in claim_repo.list_denials():
        if demo_cache.is_replay():
            events = demo_cache.load_trace(denial.denial_id) or []
            _record_outcome_from_events(denial, events)
        else:
            events = []
            async for event in process_denial(denial):
                events.append(event)
            demo_cache.save_trace(denial.denial_id, events)
        results.append(_summary_from_events(denial, events))
    by_route: dict[str, int] = {}
    for r in results:
        key = r["route"] or ("no_cache" if demo_cache.is_replay() else "error")
        by_route[key] = by_route.get(key, 0) + 1
    return {"count": len(results), "by_route": by_route, "results": results}


@router.post("/approve/{denial_id}")
def approve(denial_id: str) -> dict:
    """Human approves the agent's decision. Applies + resubmits a pending fix."""
    record = memory.get_decision_record(denial_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no decision on file for {denial_id}; run /api/process/{denial_id} first",
        )
    denial = claim_repo.get_denial(denial_id)
    fix_data = record.get("fix")
    if fix_data and not fix_data.get("applied"):
        claim = claim_repo.get_claim(record["claim_id"])
        fix = validate_fix(Fix.model_validate(fix_data), claim)
        if fix.validated:
            corrected = apply_fix(fix, claim)
            claim_repo.save_claim(corrected)
            submit = execute_tool("submit_claim", {"claim": corrected.model_dump(mode="json")})
            record["fix"] = fix.model_copy(update={"applied": True}).model_dump(mode="json")
            record["resubmit_status"] = submit.get("status")
            record["resubmission"] = submit
            if submit.get("status") != "accepted" and denial is not None:
                fix_summary = "; ".join(f"{op['op']} {op['field_path']}" for op in fix_data["operations"])
                memory.add_lesson(
                    denial.payer_name,
                    denial.adjustments[0].carc,
                    memory.lesson_from_failed_resubmit(
                        denial.payer_name, denial.adjustments[0].carc,
                        fix_summary, submit.get("errors", []),
                    ),
                )
        else:
            record["fix_error"] = fix.validation_error
    record["status"] = "approved"
    memory.save_decision_record(denial_id, record)
    if denial is not None:
        memory.record_outcome(
            denial_id=denial_id,
            payer=denial.payer_name,
            carc=denial.adjustments[0].carc,
            route=record["route"],
            root_cause_category=(record.get("root_cause") or {}).get("category"),
            confidence=record.get("confidence", 0.0),
            resubmit_status=record.get("resubmit_status"),
        )
    return record


class OverrideBody(BaseModel):
    route: Route
    reason: str = Field(default="", max_length=500)


@router.post("/override/{denial_id}")
def override(denial_id: str, body: OverrideBody) -> dict:
    """Human overrides the agent's route."""
    record = memory.get_decision_record(denial_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"no decision on file for {denial_id}; run /api/process/{denial_id} first",
        )
    record["status"] = "overridden"
    record["agent_route"] = record.get("route")
    record["route"] = body.route.value
    record["override_reason"] = body.reason
    memory.save_decision_record(denial_id, record)
    denial = claim_repo.get_denial(denial_id)
    if denial is not None:
        memory.record_outcome(
            denial_id=denial_id,
            payer=denial.payer_name,
            carc=denial.adjustments[0].carc,
            route=body.route.value,
            root_cause_category=(record.get("root_cause") or {}).get("category"),
            confidence=1.0,  # human decision
            resubmit_status=record.get("resubmit_status"),
        )
    return record
