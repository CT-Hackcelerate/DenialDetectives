"""The agentic ReAct loop: investigate -> root cause -> route decision, emitting
a TraceEvent at every step for the SSE stream.

    async for event in process_denial(denial):
        ...  # thought / tool_call / tool_result / root_cause / decision / completed

Control flow:
  * The model gets the 7 investigation tools (app.agent.tools) plus two
    control tools — record_root_cause and record_decision — which the loop
    intercepts instead of dispatching.
  * Hard turn budget: settings.max_agent_turns (12) API calls.
  * Guardrails applied in code, not prompt: citations not backed by a tool
    result from this run are dropped; a root cause or decision left with zero
    citations forces HUMAN_REVIEW; AUTO_FIX_RESUBMIT is rerouted to
    HUMAN_REVIEW unless confidence > 0.85 and value at stake < $1000.
  * value_at_stake is computed from the denial itself, never trusted from
    the model.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator

from anthropic import AsyncAnthropic

from app.agent import prompts
from app.agent.guardrails import (
    Evidence,
    apply_fix,
    enforce_route,
    validate_fix,
    validated_citations,
)
from app.agent.tools import TOOLS, execute_tool
from app.config import settings
from app.models import (
    Citation,
    Claim,
    Denial,
    Fix,
    FixOperation,
    RootCause,
    RootCauseCategory,
    Route,
    TraceEvent,
    TraceEventType,
    TriageDecision,
)
from app.services import appeals, claim_repo, memory

_CITATION_SCHEMA = {
    "type": "object",
    "properties": {
        "source_type": {
            "type": "string",
            "enum": ["carc_definition", "rarc_definition", "ncci_edit", "payer_policy"],
        },
        "source_id": {
            "type": "string",
            "description": "Exactly as returned by a tool, e.g. 'NCCI-29881/99213', 'UHC-CP-044', 'CARC-16', 'RSB-006'.",
        },
        "quote": {"type": "string", "description": "Verbatim snippet from the tool result."},
        "chroma_doc_id": {"type": "string"},
    },
    "required": ["source_type", "source_id", "quote"],
}

CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "record_root_cause",
        "description": (
            "Record your diagnosis of why this claim was ACTUALLY denied (which may "
            "differ from the stated CARC). Call exactly once, before record_decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": [c.value for c in RootCauseCategory]},
                "summary": {"type": "string", "description": "1-2 sentences on the true cause."},
                "implicated_codes": {"type": "array", "items": {"type": "string"}},
                "is_correctable": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "citations": {"type": "array", "items": _CITATION_SCHEMA, "minItems": 1},
            },
            "required": ["category", "summary", "is_correctable", "confidence", "citations"],
        },
    },
    {
        "name": "propose_fix",
        "description": (
            "Propose a structured correction to the claim. You never edit the claim — "
            "Python validates each operation against a field whitelist and applies it "
            "to a copy. Required before any auto_fix_resubmit decision. Editable: "
            "prior_auth_number, subscriber_id, lines[i].modifiers, lines[i].icd10_pointers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field_path": {
                                "type": "string",
                                "description": "e.g. 'prior_auth_number' or 'lines[0].modifiers'.",
                            },
                            "op": {"type": "string", "enum": ["set", "add", "remove"]},
                            "old_value": {},
                            "new_value": {},
                            "reason": {"type": "string"},
                        },
                        "required": ["field_path", "op", "reason"],
                    },
                },
                "citation": _CITATION_SCHEMA,
            },
            "required": ["operations", "citation"],
        },
    },
    {
        "name": "record_decision",
        "description": (
            "Commit to exactly one triage route and reject the other three. Call once, "
            "after record_root_cause. This ends the investigation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "route": {"type": "string", "enum": [r.value for r in Route]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "rejected_routes": {
                    "type": "object",
                    "description": "The three routes you did NOT pick -> one-line reason each was rejected.",
                    "additionalProperties": {"type": "string"},
                },
                "citations": {"type": "array", "items": _CITATION_SCHEMA, "minItems": 1},
            },
            "required": ["route", "confidence", "rationale", "rejected_routes", "citations"],
        },
    },
]


def _fallback_citation(denial: Denial) -> Citation:
    """A code-derived citation for forced HUMAN_REVIEW decisions (never invented)."""
    adjustment = denial.adjustments[0]
    lookup = execute_tool("carc_lookup", {"carc": adjustment.carc})
    if lookup.get("found"):
        return Citation.model_validate(lookup["citations"][0])
    return Citation(
        source_type="carc_definition",
        source_id=f"CARC-{adjustment.carc}",
        quote="Reason code not in reference table; routed for human review.",
    )


class _Emitter:
    def __init__(self, denial_id: str) -> None:
        self.denial_id = denial_id
        self.seq = 0

    def __call__(
        self,
        type_: TraceEventType,
        message: str,
        payload: dict | None = None,
        citations: list[Citation] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            event_id=f"{self.denial_id}-{self.seq:03d}-{uuid.uuid4().hex[:6]}",
            denial_id=self.denial_id,
            seq=self.seq,
            type=type_,
            message=message,
            payload=payload or {},
            citations=citations or [],
        )
        self.seq += 1
        return event


async def process_denial(
    denial: Denial, claim: Claim | None = None
) -> AsyncGenerator[TraceEvent, None]:
    """Run the ReAct triage loop for one denial, yielding TraceEvents."""
    emit = _Emitter(denial.denial_id)
    claim = claim or claim_repo.get_claim(denial.claim_id)
    if claim is None:
        yield emit(TraceEventType.ERROR, f"No claim found for {denial.claim_id}.")
        return

    try:
        client = AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    except Exception as exc:  # noqa: BLE001 — no key: emit a trace error, don't kill the SSE stream
        yield emit(
            TraceEventType.ERROR,
            f"Anthropic client unavailable ({exc}). Set ANTHROPIC_API_KEY in backend/.env, "
            "or set DEMO_MODE=replay to serve cached traces.",
        )
        return
    evidence = Evidence()
    root_cause: RootCause | None = None
    decision: TriageDecision | None = None
    validated_fix: Fix | None = None
    value_at_stake = denial.total_denied
    primary_carc = denial.adjustments[0].carc

    lessons = memory.lessons_for(denial.payer_name, primary_carc)
    yield emit(
        TraceEventType.STARTED,
        f"Triage started for {denial.denial_id} "
        f"({denial.payer_name}, ${value_at_stake} at stake"
        + (f", {len(lessons)} past lesson(s) injected)." if lessons else ")."),
        payload={
            "claim_id": claim.claim_id,
            "value_at_stake": str(value_at_stake),
            "lessons": lessons,
        },
    )

    messages: list[dict] = [
        {"role": "user", "content": prompts.build_user_message(denial, claim, lessons)}
    ]

    for turn in range(settings.max_agent_turns):
        try:
            response = await client.messages.create(
                model=settings.claimguard_model,
                max_tokens=2000,
                system=prompts.SYSTEM_PROMPT,
                tools=TOOLS + CONTROL_TOOLS,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001 — infrastructure failure, NOT a triage outcome
            yield emit(
                TraceEventType.ERROR,
                f"Model call failed on turn {turn + 1}: {exc} — run aborted, no outcome "
                "recorded. Fix the API key (or set DEMO_MODE=replay) and retry.",
            )
            return

        tool_result_blocks: list[dict] = []

        for block in response.content:
            if block.type == "text":
                if block.text.strip():
                    yield emit(TraceEventType.THOUGHT, block.text.strip())
                continue
            if block.type != "tool_use":
                continue

            name, tool_input = block.name, dict(block.input or {})

            # ---- control tool: record_root_cause -------------------------- #
            if name == "record_root_cause":
                kept, dropped = validated_citations(tool_input.get("citations"), evidence)
                if not kept:
                    tool_result_blocks.append(_tool_result(block.id, {
                        "ok": False,
                        "error": f"All {dropped} citations were dropped — none match a tool "
                                 "result from this run. Re-cite from actual tool output.",
                    }))
                    yield emit(
                        TraceEventType.FIX_REJECTED,
                        f"Root cause rejected: all {dropped} citations uncited/unverifiable.",
                    )
                    continue
                root_cause = RootCause(
                    category=RootCauseCategory(tool_input["category"]),
                    summary=tool_input["summary"],
                    implicated_codes=tool_input.get("implicated_codes", []),
                    is_correctable=bool(tool_input["is_correctable"]),
                    confidence=float(tool_input["confidence"]),
                    citations=kept,
                )
                yield emit(
                    TraceEventType.ROOT_CAUSE,
                    f"Root cause: {root_cause.category.value} — {root_cause.summary}",
                    payload={**root_cause.model_dump(mode="json"), "citations_dropped": dropped},
                    citations=kept,
                )
                tool_result_blocks.append(_tool_result(block.id, {
                    "ok": True, "citations_kept": len(kept), "citations_dropped": dropped,
                }))
                continue

            # ---- control tool: propose_fix --------------------------------- #
            if name == "propose_fix":
                kept, dropped = validated_citations([tool_input.get("citation")], evidence)
                if not kept:
                    yield emit(
                        TraceEventType.FIX_REJECTED,
                        "Fix rejected: its citation does not match any tool result from this run.",
                    )
                    tool_result_blocks.append(_tool_result(block.id, {
                        "ok": False,
                        "error": "Fix citation uncited/unverifiable — cite actual tool output.",
                    }))
                    continue
                try:
                    fix = Fix(
                        fix_id=f"FIX-{denial.denial_id}-{emit.seq:03d}",
                        claim_id=claim.claim_id,
                        operations=[FixOperation.model_validate(op) for op in tool_input["operations"]],
                        citation=kept[0],
                    )
                except Exception as exc:  # noqa: BLE001 — malformed proposal, tell the model
                    tool_result_blocks.append(_tool_result(block.id, {
                        "ok": False, "error": f"Malformed fix proposal: {exc}",
                    }))
                    continue
                yield emit(
                    TraceEventType.FIX_PROPOSED,
                    f"Fix proposed: {len(fix.operations)} operation(s) on {claim.claim_id}.",
                    payload=fix.model_dump(mode="json"),
                    citations=kept,
                )
                fix = validate_fix(fix, claim)
                if fix.validated:
                    validated_fix = fix
                    yield emit(
                        TraceEventType.FIX_VALIDATED,
                        "Fix passed whitelist validation.",
                        payload=fix.model_dump(mode="json"),
                    )
                    tool_result_blocks.append(_tool_result(block.id, {
                        "ok": True, "fix_id": fix.fix_id, "validated": True,
                    }))
                else:
                    yield emit(
                        TraceEventType.FIX_REJECTED,
                        f"Fix rejected by guardrails: {fix.validation_error}",
                        payload=fix.model_dump(mode="json"),
                    )
                    tool_result_blocks.append(_tool_result(block.id, {
                        "ok": False, "validated": False, "error": fix.validation_error,
                    }))
                continue

            # ---- control tool: record_decision ----------------------------- #
            if name == "record_decision":
                kept, dropped = validated_citations(tool_input.get("citations"), evidence)
                requested = Route(tool_input["route"])
                confidence = float(tool_input["confidence"])
                rationale = tool_input.get("rationale", "")
                rejected = tool_input.get("rejected_routes", {})

                if not kept:
                    final_route = Route.HUMAN_REVIEW
                    note = (
                        f"All {dropped} decision citations were uncited/unverifiable — "
                        "forced to human_review."
                    )
                    kept = [_fallback_citation(denial)]
                else:
                    final_route, note = enforce_route(requested, confidence, value_at_stake)
                    if final_route is Route.AUTO_FIX_RESUBMIT and validated_fix is None:
                        final_route = Route.HUMAN_REVIEW
                        note = (
                            "Auto path blocked: no validated fix on file — propose_fix "
                            "must succeed before auto_fix_resubmit. Rerouted to human_review."
                        )

                if final_route is not requested:
                    yield emit(
                        TraceEventType.ROUTED_TO_HUMAN,
                        f"Guardrail rerouted {requested.value} -> {final_route.value}: {note}",
                    )
                decision = TriageDecision(
                    route=final_route,
                    confidence=confidence,
                    value_at_stake=value_at_stake,
                    rationale=rationale,
                    citations=kept,
                    guardrail_note=note,
                )
                yield emit(
                    TraceEventType.DECISION,
                    f"Decision: {decision.route.value} (confidence {confidence:.2f}, "
                    f"${value_at_stake} at stake).",
                    payload={
                        **decision.model_dump(mode="json"),
                        "requested_route": requested.value,
                        "rejected_routes": rejected,
                        "citations_dropped": dropped,
                    },
                    citations=kept,
                )
                tool_result_blocks.append(_tool_result(block.id, {"ok": True, "final_route": final_route.value}))
                continue

            # ---- investigation tools ---------------------------------------- #
            yield emit(
                TraceEventType.TOOL_CALL,
                f"Calling {name}({json.dumps(tool_input, default=str)[:200]})",
                payload={"tool": name, "input": tool_input},
            )
            result = execute_tool(name, tool_input)
            evidence.harvest(result)
            yield emit(
                TraceEventType.TOOL_RESULT,
                f"{name} returned.",
                payload={"tool": name, "result": result},
            )
            if name == "policy_retrieve" and result.get("matches"):
                yield emit(
                    TraceEventType.CONTEXT_RETRIEVED,
                    f"Retrieved {len(result['matches'])} policy chunk(s): "
                    + ", ".join(sorted({m["policy_number"] for m in result["matches"]})),
                    payload={"chroma_doc_ids": [m["citation"]["chroma_doc_id"] for m in result["matches"]]},
                )
            tool_result_blocks.append(_tool_result(block.id, result))

        messages.append({"role": "assistant", "content": response.content})
        if tool_result_blocks:
            messages.append({"role": "user", "content": tool_result_blocks})
        elif decision is None:
            messages.append({"role": "user", "content": prompts.NUDGE})

        if decision is not None:
            break

    if decision is None:
        note = f"No decision within the {settings.max_agent_turns}-turn budget — forced to human_review."
        decision = TriageDecision(
            route=Route.HUMAN_REVIEW,
            confidence=0.0,
            value_at_stake=value_at_stake,
            rationale=note,
            citations=[_fallback_citation(denial)],
            guardrail_note=note,
        )
        yield emit(TraceEventType.ROUTED_TO_HUMAN, note)
        yield emit(
            TraceEventType.DECISION,
            f"Decision: {decision.route.value} (forced).",
            payload=decision.model_dump(mode="json"),
            citations=decision.citations,
        )

    # ---- auto pipeline: apply the validated fix, then resubmit -------------- #
    resubmit_status: str | None = None
    if decision.route is Route.AUTO_FIX_RESUBMIT and validated_fix is not None:
        try:
            corrected = apply_fix(validated_fix, claim)
        except Exception as exc:  # noqa: BLE001 — a failed apply falls back to humans
            yield emit(TraceEventType.ERROR, f"apply_fix failed: {exc} — routing to human.")
            decision = decision.model_copy(
                update={"route": Route.HUMAN_REVIEW, "guardrail_note": f"apply_fix failed: {exc}"}
            )
        else:
            validated_fix = validated_fix.model_copy(update={"applied": True})
            claim_repo.save_claim(corrected)
            yield emit(
                TraceEventType.FIX_APPLIED,
                f"Fix applied to a copy of {claim.claim_id}; revision "
                f"{claim.revision} -> {corrected.revision}.",
                payload={"fix": validated_fix.model_dump(mode="json"),
                         "claim": corrected.model_dump(mode="json")},
            )
            submit_result = execute_tool("submit_claim", {"claim": corrected.model_dump(mode="json")})
            resubmit_status = submit_result.get("status", "error")
            yield emit(
                TraceEventType.RESUBMITTED,
                f"Resubmission {resubmit_status} "
                f"(trace {submit_result.get('trace_id')}, ack {submit_result.get('payer_ack_code')}).",
                payload=submit_result,
            )
            if resubmit_status != "accepted":
                fix_summary = "; ".join(
                    f"{op.op.value} {op.field_path}" for op in validated_fix.operations
                )
                lesson = memory.lesson_from_failed_resubmit(
                    denial.payer_name, primary_carc, fix_summary, submit_result.get("errors", [])
                )
                memory.add_lesson(denial.payer_name, primary_carc, lesson)
                yield emit(
                    TraceEventType.ROUTED_TO_HUMAN,
                    f"Resubmission rejected — lesson recorded for "
                    f"({denial.payer_name}, CARC {primary_carc}); routing to human follow-up.",
                    payload={"lesson": lesson},
                )

    # ---- appeal pipeline: draft the appeal letter ---------------------------- #
    appeal_letter: str | None = None
    if decision.route is Route.APPEAL:
        generated_by = "template"
        try:
            response = await client.messages.create(
                model=settings.claimguard_model,
                max_tokens=1200,
                system=appeals.LETTER_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": appeals.letter_context(denial, claim, root_cause, decision),
                }],
            )
            text = "\n".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            if text:
                appeal_letter, generated_by = text, "model"
        except Exception:  # noqa: BLE001 — fall back to the deterministic template
            pass
        if appeal_letter is None:
            appeal_letter = appeals.render_appeal_letter(denial, claim, root_cause, decision)
        yield emit(
            TraceEventType.APPEAL_DRAFTED,
            f"Appeal letter drafted ({generated_by}), citing "
            f"{len(decision.citations)} source(s).",
            payload={"letter": appeal_letter, "generated_by": generated_by},
            citations=decision.citations,
        )

    # ---- memory: store every outcome + a reviewable decision record ---------- #
    memory.record_outcome(
        denial_id=denial.denial_id,
        payer=denial.payer_name,
        carc=primary_carc,
        route=decision.route.value,
        root_cause_category=root_cause.category.value if root_cause else None,
        confidence=decision.confidence,
        resubmit_status=resubmit_status,
    )
    memory.save_decision_record(denial.denial_id, {
        "denial_id": denial.denial_id,
        "claim_id": claim.claim_id,
        "route": decision.route.value,
        "confidence": decision.confidence,
        "value_at_stake": str(value_at_stake),
        "rationale": decision.rationale,
        "guardrail_note": decision.guardrail_note,
        "root_cause": root_cause.model_dump(mode="json") if root_cause else None,
        "fix": validated_fix.model_dump(mode="json") if validated_fix else None,
        "appeal_letter": appeal_letter,
        "resubmit_status": resubmit_status,
        "status": "pending_review" if decision.route is Route.HUMAN_REVIEW else "completed",
    })

    yield emit(
        TraceEventType.COMPLETED,
        f"Triage complete: {decision.route.value}"
        + (f" | root cause: {root_cause.category.value}" if root_cause else " | no root cause recorded"),
        payload={
            "route": decision.route.value,
            "root_cause_category": root_cause.category.value if root_cause else None,
            "value_at_stake": str(value_at_stake),
            "resubmit_status": resubmit_status,
        },
    )


def _tool_result(tool_use_id: str, result: dict) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(result, default=str),
    }
