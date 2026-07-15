"""Guardrail layer: Python-enforced rules the LLM cannot override.

The LLM never mutates a claim or asserts unbacked facts — this module is where
its output gets checked.

Implemented here:
  * Evidence / validated_citations — drop any citation whose source_id or
    chroma_doc_id was not actually returned by a tool during this run
    (no invented evidence).
  * enforce_route — reroute AUTO_FIX_RESUBMIT to HUMAN_REVIEW when confidence
    or value fails the thresholds from app.models.
  * validate_fix — check every FixOperation against the field whitelist and the
    claim's current state; sets validated / validation_error.
  * apply_fix — deterministic pure function: applies a validated Fix to a COPY
    of the claim, bumps revision, and re-validates the result through the
    Claim pydantic model. The original claim is never mutated.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.models import (
    MAX_AUTO_VALUE,
    MIN_AUTO_CONFIDENCE,
    Citation,
    Claim,
    Fix,
    FixOpType,
    Route,
)


class Evidence:
    """Registry of citation ids actually returned by tools during one run."""

    def __init__(self) -> None:
        self.source_ids: set[str] = set()
        self.chroma_doc_ids: set[str] = set()

    def harvest(self, obj) -> None:
        """Walk a tool result and record every citation it contains."""
        if isinstance(obj, dict):
            if obj.get("source_id"):
                self.source_ids.add(str(obj["source_id"]))
                if obj.get("chroma_doc_id"):
                    self.chroma_doc_ids.add(str(obj["chroma_doc_id"]))
            # policy chunks and resubmission precedents are citable by id too
            if obj.get("policy_number"):
                self.source_ids.add(str(obj["policy_number"]))
            if obj.get("resubmission_id"):
                self.source_ids.add(str(obj["resubmission_id"]))
            for value in obj.values():
                self.harvest(value)
        elif isinstance(obj, list):
            for item in obj:
                self.harvest(item)


def validated_citations(
    raw_citations: list[dict] | None, evidence: Evidence
) -> tuple[list[Citation], int]:
    """Keep only citations backed by evidence gathered this run.

    Returns (kept, dropped_count). Malformed citations are dropped too.
    """
    kept: list[Citation] = []
    dropped = 0
    for raw in raw_citations or []:
        try:
            citation = Citation.model_validate(raw)
        except Exception:
            dropped += 1
            continue
        if citation.source_id in evidence.source_ids or (
            citation.chroma_doc_id and citation.chroma_doc_id in evidence.chroma_doc_ids
        ):
            kept.append(citation)
        else:
            dropped += 1
    return kept, dropped


def enforce_route(
    route: Route, confidence: float, value_at_stake: Decimal
) -> tuple[Route, str | None]:
    """Apply the auto-resubmit thresholds. Returns (final_route, guardrail_note)."""
    if route is not Route.AUTO_FIX_RESUBMIT:
        return route, None
    if confidence > MIN_AUTO_CONFIDENCE and value_at_stake < MAX_AUTO_VALUE:
        return route, (
            f"Auto path allowed: confidence {confidence:.2f} > {MIN_AUTO_CONFIDENCE} "
            f"and value ${value_at_stake} < ${MAX_AUTO_VALUE}."
        )
    return Route.HUMAN_REVIEW, (
        f"Auto path blocked: requires confidence > {MIN_AUTO_CONFIDENCE} and value < "
        f"${MAX_AUTO_VALUE}; got confidence={confidence:.2f}, value=${value_at_stake}. "
        "Rerouted to human_review."
    )


# --------------------------------------------------------------------------- #
# Fix pipeline — the ONLY place claims change
# --------------------------------------------------------------------------- #

# The LLM may only touch these fields. Everything else is off limits.
ALLOWED_SCALAR_FIELDS = {"prior_auth_number", "subscriber_id"}
_LINE_PATH_RE = re.compile(r"^lines\[(\d+)\]\.(modifiers|icd10_pointers)$")
ALLOWED_FIELD_PATHS = sorted(ALLOWED_SCALAR_FIELDS) + [
    "lines[<i>].modifiers",
    "lines[<i>].icd10_pointers",
]


def validate_fix(fix: Fix, claim: Claim) -> Fix:
    """Check every operation against the whitelist and the claim's current state.

    Returns a copy of the Fix with validated / validation_error set.
    """
    errors: list[str] = []
    for op in fix.operations:
        path = op.field_path.strip()
        if path in ALLOWED_SCALAR_FIELDS:
            if op.op is not FixOpType.SET:
                errors.append(f"{path}: only 'set' is allowed on scalar fields")
            elif not isinstance(op.new_value, str) or not op.new_value.strip():
                errors.append(f"{path}: 'set' requires a non-empty string new_value")
            continue
        match = _LINE_PATH_RE.match(path)
        if match is None:
            errors.append(f"{path}: not in the editable-field whitelist {ALLOWED_FIELD_PATHS}")
            continue
        index, field = int(match.group(1)), match.group(2)
        if index >= len(claim.lines):
            errors.append(f"{path}: line index out of range (claim has {len(claim.lines)} lines)")
            continue
        current = getattr(claim.lines[index], field)
        if op.op is FixOpType.ADD:
            if not isinstance(op.new_value, str) or not op.new_value.strip():
                errors.append(f"{path}: 'add' requires a non-empty string new_value")
            elif op.new_value in current:
                errors.append(f"{path}: '{op.new_value}' is already present")
        elif op.op is FixOpType.REMOVE:
            if op.old_value not in current:
                errors.append(f"{path}: '{op.old_value}' is not present to remove")
        else:
            errors.append(f"{path}: only 'add'/'remove' are allowed on list fields")
    return fix.model_copy(
        update={"validated": not errors, "validation_error": "; ".join(errors) or None}
    )


def apply_fix(fix: Fix, claim: Claim) -> Claim:
    """Apply a validated Fix to a copy of the claim.

    Deterministic (pure function of its inputs), never mutates the original,
    bumps revision, and re-validates the result through the Claim model so a
    fix can never produce an invalid claim.
    """
    if not fix.validated:
        raise ValueError(f"apply_fix requires a validated Fix: {fix.validation_error}")
    if fix.claim_id != claim.claim_id:
        raise ValueError(f"Fix targets {fix.claim_id}, not {claim.claim_id}")
    data = claim.model_dump()
    for op in fix.operations:
        path = op.field_path.strip()
        if path in ALLOWED_SCALAR_FIELDS:
            data[path] = op.new_value
            continue
        match = _LINE_PATH_RE.match(path)
        index, field = int(match.group(1)), match.group(2)
        if op.op is FixOpType.ADD:
            data["lines"][index][field].append(op.new_value)
        else:
            data["lines"][index][field].remove(op.old_value)
    data["revision"] = claim.revision + 1
    return Claim.model_validate(data)
