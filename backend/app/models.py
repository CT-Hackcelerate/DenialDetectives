"""ClaimGuard domain models.

Every Pydantic model for the ClaimGuard denial-triage agent lives here:
Claim, Denial, RootCause, TriageDecision, Fix, ResubmissionResult, TraceEvent,
plus the shared enums and the Citation type.

Design invariants encoded in these models:
  * The LLM never edits a claim. It proposes a `Fix` (structured operations);
    Python validates and applies it. `Fix.proposed_by` is frozen; `validated`
    and `applied` are set only by the guardrail layer.
  * Every decision cites evidence. `RootCause` and `TriageDecision` require at
    least one `Citation`; each `Fix` carries one.
  * Auto-resubmit is gated. `TriageDecision` cannot be constructed as an
    AUTO_FIX_RESUBMIT unless confidence > 0.85 AND value < $1000.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------- #
# Shared scalar types & helpers
# --------------------------------------------------------------------------- #

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

# Auto-resubmit guardrail thresholds (rule: confidence > .85 AND value < $1000).
MIN_AUTO_CONFIDENCE: float = 0.85
MAX_AUTO_VALUE: Decimal = Decimal("1000.00")


def utcnow() -> datetime:
    """Timezone-aware UTC now (used as a default_factory)."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class GroupCode(str, Enum):
    """X12 claim adjustment group codes."""

    CO = "CO"  # Contractual Obligation
    PR = "PR"  # Patient Responsibility
    OA = "OA"  # Other Adjustment
    PI = "PI"  # Payer Initiated
    CR = "CR"  # Correction / Reversal


class RootCauseCategory(str, Enum):
    """The true underlying reason a claim was denied."""

    MISSING_INFO = "missing_info"
    AUTH_REQUIRED = "auth_required"
    CODING_MISMATCH = "coding_mismatch"
    MEDICAL_NECESSITY = "medical_necessity"
    BUNDLING_NCCI = "bundling_ncci"
    DUPLICATE = "duplicate"
    TIMELY_FILING = "timely_filing"
    COORDINATION_OF_BENEFITS = "coordination_of_benefits"
    NON_COVERED = "non_covered"
    PATIENT_RESPONSIBILITY = "patient_responsibility"
    CONTRACTUAL = "contractual"
    UNKNOWN = "unknown"


class Route(str, Enum):
    """The four triage routes."""

    AUTO_FIX_RESUBMIT = "auto_fix_resubmit"
    APPEAL = "appeal"
    WRITE_OFF = "write_off"
    HUMAN_REVIEW = "human_review"


class CitationSourceType(str, Enum):
    """Where a piece of supporting evidence came from."""

    CARC_DEFINITION = "carc_definition"
    RARC_DEFINITION = "rarc_definition"
    NCCI_EDIT = "ncci_edit"
    PAYER_POLICY = "payer_policy"


class FixOpType(str, Enum):
    """A single structured edit operation the LLM may propose."""

    SET = "set"  # replace a scalar field
    ADD = "add"  # append to a list (e.g. a modifier)
    REMOVE = "remove"  # remove a list item


class ResubmissionStatus(str, Enum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class TraceEventType(str, Enum):
    """One step in an agent run — streamed over SSE and persisted as audit log."""

    STARTED = "started"
    THOUGHT = "thought"  # model reasoning text between tool calls
    CONTEXT_RETRIEVED = "context_retrieved"  # ChromaDB retrieval hit
    ROOT_CAUSE = "root_cause"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DECISION = "decision"
    FIX_PROPOSED = "fix_proposed"
    FIX_VALIDATED = "fix_validated"
    FIX_REJECTED = "fix_rejected"
    FIX_APPLIED = "fix_applied"
    RESUBMITTED = "resubmitted"
    APPEAL_DRAFTED = "appeal_drafted"
    ROUTED_TO_HUMAN = "routed_to_human"
    COMPLETED = "completed"
    ERROR = "error"


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


class Citation(BaseModel):
    """Evidence backing a diagnosis, decision, or fix. Nothing is asserted uncited."""

    source_type: CitationSourceType
    source_id: str = Field(
        ...,
        description="e.g. 'CO-197', 'RARC-N657', 'NCCI-29881/99214', 'MERIDIAN-IMG-014'.",
    )
    quote: str = Field(..., min_length=1, description="Verbatim snippet supporting the claim.")
    chroma_doc_id: str | None = Field(
        default=None, description="Retrieval provenance from the knowledge base."
    )


# --------------------------------------------------------------------------- #
# Claim (simplified 837)
# --------------------------------------------------------------------------- #


class ClaimLine(BaseModel):
    line_number: int = Field(..., ge=1)
    cpt_hcpcs: str = Field(..., description="Procedure code.")
    modifiers: list[str] = Field(default_factory=list)
    icd10_pointers: list[str] = Field(
        default_factory=list, description="Diagnosis codes attached to this line."
    )
    units: int = Field(default=1, ge=1)
    charge: Money
    place_of_service: str | None = None


class Claim(BaseModel):
    """Simplified 837-style claim. Synthetic patient data only — no PHI."""

    claim_id: str
    payer_id: str
    payer_name: str
    provider_npi: str
    provider_name: str
    patient_ref: str = Field(..., description="Synthetic patient identifier — no PHI.")
    subscriber_id: str
    date_of_service: date
    date_submitted: date
    prior_auth_number: str | None = None
    diagnoses: list[str] = Field(default_factory=list, description="Claim-level ICD-10 set.")
    lines: list[ClaimLine] = Field(..., min_length=1)
    total_charge: Money
    revision: int = Field(default=0, description="Bumped each time a Fix is applied.")


# --------------------------------------------------------------------------- #
# Denial (simplified 835)
# --------------------------------------------------------------------------- #


class Adjustment(BaseModel):
    """One CARC/RARC adjustment from the 835 remittance."""

    group_code: GroupCode
    carc: str = Field(..., description="Claim Adjustment Reason Code, e.g. '197'.")
    rarc: str | None = Field(default=None, description="Remittance Advice Remark Code, e.g. 'N657'.")
    amount: Money
    line_number: int | None = Field(
        default=None, description="None = claim-level adjustment; else the affected line."
    )


class Denial(BaseModel):
    """Simplified X12 835 remittance denial for a single claim."""

    denial_id: str
    claim_id: str
    payer_id: str
    payer_name: str
    remit_date: date
    adjustments: list[Adjustment] = Field(..., min_length=1)
    total_denied: Money
    remit_note: str | None = None
    payer_context: str | None = Field(
        default=None, description="Free-text context from the biller / work queue."
    )


# --------------------------------------------------------------------------- #
# RootCause
# --------------------------------------------------------------------------- #


class RootCause(BaseModel):
    """The agent's diagnosis of *why* the claim was actually denied."""

    category: RootCauseCategory
    summary: str = Field(..., min_length=1, description="One or two sentences on the true cause.")
    implicated_codes: list[str] = Field(
        default_factory=list, description="CARC/RARC codes this diagnosis explains."
    )
    is_correctable: bool = Field(
        ..., description="Can a structured claim edit plausibly resolve this denial?"
    )
    confidence: Confidence
    citations: list[Citation] = Field(..., min_length=1)


# --------------------------------------------------------------------------- #
# TriageDecision (guardrail enforced here)
# --------------------------------------------------------------------------- #


class TriageDecision(BaseModel):
    """Routing decision. The auto-resubmit guardrail is enforced in the model itself."""

    route: Route
    confidence: Confidence
    value_at_stake: Money
    rationale: str = Field(..., min_length=1)
    citations: list[Citation] = Field(
        ..., min_length=1, description="Every decision must cite evidence."
    )
    guardrail_note: str | None = Field(
        default=None, description="Why the auto path was allowed or blocked."
    )

    @property
    def auto_resubmit_eligible(self) -> bool:
        """True only when both guardrail thresholds are cleared."""
        return self.confidence > MIN_AUTO_CONFIDENCE and self.value_at_stake < MAX_AUTO_VALUE

    @model_validator(mode="after")
    def _enforce_auto_guardrail(self) -> "TriageDecision":
        # An AUTO_FIX_RESUBMIT decision cannot exist unless it clears both thresholds.
        if self.route is Route.AUTO_FIX_RESUBMIT and not self.auto_resubmit_eligible:
            raise ValueError(
                f"AUTO_FIX_RESUBMIT requires confidence > {MIN_AUTO_CONFIDENCE} "
                f"and value < ${MAX_AUTO_VALUE}; got confidence={self.confidence}, "
                f"value=${self.value_at_stake}. Route to HUMAN_REVIEW instead."
            )
        return self


# --------------------------------------------------------------------------- #
# Fix (LLM proposes, Python validates + applies)
# --------------------------------------------------------------------------- #


class FixOperation(BaseModel):
    """A single proposed edit. `field_path` is validated against a whitelist at apply time."""

    field_path: str = Field(
        ..., description="Dotted/indexed path, e.g. 'prior_auth_number' or 'lines[0].modifiers'."
    )
    op: FixOpType
    old_value: Any | None = None
    new_value: Any | None = None
    reason: str = Field(..., min_length=1)


class Fix(BaseModel):
    """A structured correction proposed by the LLM.

    The model only ever *proposes* this (via a tool call). The guardrail layer
    validates each operation against a field whitelist and applies it to a copy
    of the Claim. `validated`/`applied` are set by Python, never by the model.
    """

    fix_id: str
    claim_id: str
    operations: list[FixOperation] = Field(..., min_length=1)
    citation: Citation = Field(..., description="Policy / CARC / NCCI basis for the correction.")
    proposed_by: str = Field(default="llm", frozen=True)
    validated: bool = Field(default=False)
    applied: bool = Field(default=False)
    validation_error: str | None = None


# --------------------------------------------------------------------------- #
# ResubmissionResult
# --------------------------------------------------------------------------- #


class ResubmissionResult(BaseModel):
    """Outcome of sending a corrected claim back to the (simulated) payer."""

    resubmission_id: str
    claim_id: str
    corrected_claim_revision: int = Field(..., description="Claim.revision that was submitted.")
    status: ResubmissionStatus
    payer_ack_code: str | None = Field(
        default=None, description="Simulated 277CA-style acknowledgment code."
    )
    message: str | None = None
    submitted_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# TraceEvent (SSE + audit spine)
# --------------------------------------------------------------------------- #


class TraceEvent(BaseModel):
    """One step in an agent run. Streamed over SSE and persisted as the audit trail."""

    event_id: str
    denial_id: str
    seq: int = Field(..., ge=0, description="Monotonic ordering within a single run.")
    type: TraceEventType
    message: str
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Model snapshot / data for this step."
    )
    citations: list[Citation] = Field(default_factory=list)
    ts: datetime = Field(default_factory=utcnow)


__all__ = [
    # scalar/config
    "Money",
    "Confidence",
    "MIN_AUTO_CONFIDENCE",
    "MAX_AUTO_VALUE",
    "utcnow",
    # enums
    "GroupCode",
    "RootCauseCategory",
    "Route",
    "CitationSourceType",
    "FixOpType",
    "ResubmissionStatus",
    "TraceEventType",
    # models
    "Citation",
    "ClaimLine",
    "Claim",
    "Adjustment",
    "Denial",
    "RootCause",
    "TriageDecision",
    "FixOperation",
    "Fix",
    "ResubmissionResult",
    "TraceEvent",
]
