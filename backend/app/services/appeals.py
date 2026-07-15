"""Appeal-letter drafting.

The orchestrator asks the model to write the letter (grounded in the run's own
citations); if the model is unavailable or returns nothing, render_appeal_letter
produces a deterministic template from the same facts — so appeal routes always
yield a letter, including in DEMO_MODE=replay and keyless runs.
"""
from __future__ import annotations

import json

from app.models import Claim, Denial, RootCause, TriageDecision

LETTER_SYSTEM_PROMPT = """You draft first-level appeal letters for a US healthcare \
provider's billing office. Write a formal, concise business letter (under 350 words) \
appealing the denial described in the user message.

Hard rules:
- Use ONLY the facts, codes, amounts, and citations provided. Never invent clinical \
details, dates, policy language, or attachments that are not listed.
- Reference each supporting citation by its source id (e.g. AET-THER-033, CARC-50).
- State the denial reason, why it should be overturned, and a clear request for \
reconsideration and payment.
- Output ONLY the letter text — no preamble, no markdown fences."""


def letter_context(
    denial: Denial, claim: Claim, root_cause: RootCause | None, decision: TriageDecision
) -> str:
    return json.dumps(
        {
            "denial": denial.model_dump(mode="json"),
            "claim": claim.model_dump(mode="json"),
            "root_cause": root_cause.model_dump(mode="json") if root_cause else None,
            "decision_rationale": decision.rationale,
            "citations": [c.model_dump(mode="json") for c in decision.citations],
        },
        indent=2,
    )


def render_appeal_letter(
    denial: Denial, claim: Claim, root_cause: RootCause | None, decision: TriageDecision
) -> str:
    """Deterministic fallback letter, grounded in the run's own citations."""
    adjustment = denial.adjustments[0]
    codes = f"CARC {adjustment.carc}" + (f" / RARC {adjustment.rarc}" if adjustment.rarc else "")
    cpts = ", ".join(line.cpt_hcpcs for line in claim.lines)
    citations = {c.source_id: c.quote for c in decision.citations}
    if root_cause:
        for c in root_cause.citations:
            citations.setdefault(c.source_id, c.quote)
    evidence = "\n".join(f'  - [{sid}] "{quote}"' for sid, quote in citations.items())
    basis = root_cause.summary if root_cause else decision.rationale

    return f"""{denial.payer_name} — Appeals Department

RE: First-Level Appeal — Claim {claim.claim_id}
Member ID: {claim.subscriber_id} | Patient Ref: {claim.patient_ref}
Date of Service: {claim.date_of_service} | Remit Date: {denial.remit_date}
Denied Amount: ${denial.total_denied} | Denial Reason: {codes}

To Whom It May Concern:

We respectfully appeal the denial of claim {claim.claim_id}, denied as
"{denial.remit_note or 'see remittance advice'}" ({codes}).

{basis}

The services billed ({cpts}) were rendered as documented and meet the
applicable coverage criteria. In support of this appeal we cite:

{evidence}

Supporting clinical documentation is enclosed. We request reconsideration of
this determination and payment of the denied amount of ${denial.total_denied}.
Please direct any questions to our billing office referencing claim
{claim.claim_id}.

Sincerely,

{claim.provider_name}
NPI {claim.provider_npi}"""
