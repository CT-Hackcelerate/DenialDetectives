"""Generate synthetic claims + matching X12 835 denials (no PHI) into
backend/data/synthetic/.

Usage:
    python scripts/generate_synthetic.py

Outputs (validated against app.models before writing):
    backend/data/synthetic/claims.json            list[Claim]
    backend/data/synthetic/denials.json           list[Denial]
    backend/data/synthetic/ground_truth.json      expected root cause + route per denial
                                                  (eval/demo only — the agent never reads this)
    backend/data/synthetic/resubmit_history.json  15 past resubmissions: what was fixed,
                                                  which payer, and whether it worked

Guarantees:
  * DEN-007 is the hero case: stated reason CARC 16 + RARC N54 ("missing
    information") is a red herring — the real cause is a missing modifier 25
    that bundled an E/M into a same-day knee arthroscopy (29881).
  * CARC spread exercises all four triage routes and includes
    16, 97, 4, 197, 50, 29, 27, 18 (plus extras).
  * Payers are Aetna / UnitedHealthcare / Cigna / Blue Cross Blue Shield.
  * Total denied lands near $60k (validate.py asserts 55k-70k).
  * Deterministic: seeded RNG, fixed date windows, fixed variant cycling.
    Patient/provider identifiers are fictional.
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # claimguard/
sys.path.insert(0, str(ROOT / "backend"))

from app.models import (  # noqa: E402
    Adjustment,
    Claim,
    ClaimLine,
    Denial,
    GroupCode,
    RootCauseCategory,
    Route,
)

OUT_DIR = ROOT / "backend" / "data" / "synthetic"

rng = random.Random(42)

# --------------------------------------------------------------------------- #
# Reference pools
# --------------------------------------------------------------------------- #

PAYERS = [
    ("60054", "Aetna"),
    ("87726", "UnitedHealthcare"),
    ("62308", "Cigna"),
    ("00590", "Blue Cross Blue Shield"),
]
AETNA, UHC, CIGNA, BCBS = PAYERS

# Fictional providers / NPIs.
PROVIDERS = [
    ("1093817465", "Lakeside Orthopedics LLC"),
    ("1245319878", "Riverbend Family Medicine"),
    ("1487265301", "Summit Imaging Center"),
    ("1356720944", "Harbor Physical Therapy Group"),
]
ORTHO, FAMILY, IMAGING, PT = PROVIDERS


def money(x: float | int | str) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


_seq = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def service_dates() -> tuple[date, date, date]:
    """(date_of_service, date_submitted, remit_date) within normal filing windows."""
    dos = date(2026, 2, 2) + timedelta(days=rng.randrange(100))
    submitted = dos + timedelta(days=rng.randint(2, 12))
    remit = submitted + timedelta(days=rng.randint(14, 30))
    return dos, submitted, remit


def line(
    num: int,
    cpt: str,
    charge: float,
    dx: list[str],
    *,
    mods: list[str] | None = None,
    units: int = 1,
    pos: str = "11",
) -> ClaimLine:
    return ClaimLine(
        line_number=num,
        cpt_hcpcs=cpt,
        modifiers=mods or [],
        icd10_pointers=dx,
        units=units,
        charge=money(charge),
        place_of_service=pos,
    )


def adj(
    carc: str,
    amount: Decimal,
    *,
    group: GroupCode = GroupCode.CO,
    rarc: str | None = None,
    line_no: int | None = None,
) -> Adjustment:
    return Adjustment(group_code=group, carc=carc, rarc=rarc, amount=amount, line_number=line_no)


def make_claim(
    *,
    lines: list[ClaimLine],
    diagnoses: list[str],
    dos: date,
    submitted: date,
    payer: tuple[str, str] | None = None,
    provider: tuple[str, str] | None = None,
    prior_auth: str | None = None,
    subscriber: str | None = None,
) -> Claim:
    n = _next_seq()
    payer = payer or rng.choice(PAYERS)
    provider = provider or rng.choice(PROVIDERS)
    return Claim(
        claim_id=f"CLM-{n:03d}",
        payer_id=payer[0],
        payer_name=payer[1],
        provider_npi=provider[0],
        provider_name=provider[1],
        patient_ref=f"PAT-{rng.randint(10000, 99999)}",
        subscriber_id=subscriber or f"SUB{rng.randint(100000000, 999999999)}",
        date_of_service=dos,
        date_submitted=submitted,
        prior_auth_number=prior_auth,
        diagnoses=diagnoses,
        lines=lines,
        total_charge=sum((ln.charge for ln in lines), Decimal("0.00")),
    )


def make_denial(
    claim: Claim,
    remit_date: date,
    adjustments: list[Adjustment],
    *,
    remit_note: str | None = None,
    payer_context: str | None = None,
) -> Denial:
    return Denial(
        denial_id=claim.claim_id.replace("CLM", "DEN"),
        claim_id=claim.claim_id,
        payer_id=claim.payer_id,
        payer_name=claim.payer_name,
        remit_date=remit_date,
        adjustments=adjustments,
        total_denied=sum((a.amount for a in adjustments), Decimal("0.00")),
        remit_note=remit_note,
        payer_context=payer_context,
    )


def truth(denial: Denial, category: RootCauseCategory, route: Route, synopsis: str) -> dict:
    return {
        "denial_id": denial.denial_id,
        "claim_id": denial.claim_id,
        "category": category.value,
        "expected_route": route.value,
        "synopsis": synopsis,
    }


Record = tuple[Claim, Denial, dict]

# --------------------------------------------------------------------------- #
# Scenario builders — each receives its per-scenario call index `i`
# --------------------------------------------------------------------------- #


def auth_required(i: int) -> Record:
    """CO-197: precertification absent. Sometimes the auth exists but was never keyed."""
    variants = [
        ("29881", 9800.00, ["M23.221"], "arthroscopic knee meniscectomy", AETNA, ORTHO, "24"),
        ("70553", 3400.00, ["R51.9"], "MRI brain with and without contrast", None, IMAGING, "22"),
        ("73721", 2600.00, ["M25.561"], "MRI knee without contrast", None, IMAGING, "22"),
        ("95810", 980.00, ["G47.33"], "attended polysomnography", None, IMAGING, "22"),
    ]
    cpt, charge, dx, desc, payer, provider, pos = variants[i % len(variants)]
    auth_on_file = [True, False, True, True, False][i % 5]
    dos, submitted, remit = service_dates()
    context = None
    if auth_on_file:
        auth_no = f"A-2026-{rng.randint(1000, 9999)}"
        context = (
            f"Front desk confirmed auth #{auth_no} was obtained by phone for the "
            f"{desc} but was never keyed into the claim."
        )
    claim = make_claim(
        lines=[line(1, cpt, charge, dx, pos=pos)],
        diagnoses=dx,
        dos=dos,
        submitted=submitted,
        payer=payer,
        provider=provider,
        prior_auth=None,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("197", claim.total_charge, rarc="M62")],
        remit_note="Precertification/authorization/notification absent.",
        payer_context=context,
    )
    if auth_on_file and claim.total_charge < Decimal("1000.00"):
        route, why = Route.AUTO_FIX_RESUBMIT, "Auth number exists — set prior_auth_number and resubmit."
    elif auth_on_file:
        route, why = Route.HUMAN_REVIEW, "Auth exists but value exceeds the $1000 auto-resubmit cap."
    else:
        route, why = Route.APPEAL, "No auth obtained — pursue retro-authorization appeal."
    return claim, denial, truth(denial, RootCauseCategory.AUTH_REQUIRED, route, why)


def missing_info(i: int) -> Record:
    """CO-16: claim genuinely lacks information (bad subscriber ID or missing dx pointer)."""
    variant = ["subscriber", "dx_pointer"][i % 2]
    dos, submitted, remit = service_dates()
    if variant == "subscriber":
        claim = make_claim(
            lines=[line(1, "99213", 185.00, ["I10"]), line(2, "36415", 18.00, ["I10"])],
            diagnoses=["I10"],
            dos=dos,
            submitted=submitted,
            provider=FAMILY,
            subscriber="SUB-TEMP",
        )
        denial = make_denial(
            claim,
            remit,
            [adj("16", claim.total_charge, rarc="N382")],
            remit_note="Claim/service lacks information: missing/incomplete/invalid patient identifier.",
            payer_context=(
                "Eligibility check shows the member's active ID is "
                f"SUB{rng.randint(100000000, 999999999)}; claim went out with a placeholder."
            ),
        )
        why = "Placeholder subscriber ID — correct the member ID and resubmit."
    else:
        claim = make_claim(
            lines=[line(1, "99214", 260.00, [])],  # pointer missing; dx exists at claim level
            diagnoses=["E11.9"],
            dos=dos,
            submitted=submitted,
            provider=FAMILY,
        )
        denial = make_denial(
            claim,
            remit,
            [adj("16", claim.total_charge, rarc="M76", line_no=1)],
            remit_note="Claim/service lacks information: missing/incomplete/invalid diagnosis.",
        )
        why = "Line has no diagnosis pointer though E11.9 is on the claim — repoint and resubmit."
    return claim, denial, truth(denial, RootCauseCategory.MISSING_INFO, Route.AUTO_FIX_RESUBMIT, why)


def hero_den_007(i: int) -> Record:
    """DEN-007 — the hero case.

    Stated reason: CARC 16 + RARC N54 'missing information' — a red herring.
    Real cause: 99213 billed same day as 29881 without modifier 25, so the
    E/M bundled into the arthroscopy. The agent must dig past the remit code:
    the arthroscopy PAID, only the E/M denied, and the chart documents a
    separately identifiable shoulder evaluation.
    """
    dos = date(2026, 3, 12)
    submitted = dos + timedelta(days=4)
    remit = submitted + timedelta(days=19)
    lines = [
        line(1, "99213", 225.00, ["M25.511"]),  # shoulder complaint — no modifier 25
        line(2, "29881", 8750.00, ["M23.221"], pos="24"),
    ]
    claim = make_claim(
        lines=lines,
        diagnoses=["M23.221", "M25.511"],
        dos=dos,
        submitted=submitted,
        payer=UHC,
        provider=ORTHO,
        prior_auth="A-2026-3187",
    )
    denial = make_denial(
        claim,
        remit,
        [adj("16", lines[0].charge, rarc="N54", line_no=1)],
        remit_note=(
            "Claim/service lacks information or has submission/billing error(s) "
            "which is needed for adjudication."
        ),
        payer_context=(
            "EOB shows the arthroscopy (29881) paid in full — only the office visit "
            "denied. Payer rep could not say what information is missing. Chart "
            "documents a separate left-shoulder evaluation (M25.511) completed "
            "before the knee procedure."
        ),
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.BUNDLING_NCCI,
        Route.AUTO_FIX_RESUBMIT,
        "Red herring: remit says 16/N54 'missing info', but the E/M bundled into "
        "same-day 29881 for lack of modifier 25. Documentation supports a "
        "separately identifiable E/M — append 25 to line 1 and resubmit.",
    )


def missing_modifier(i: int) -> Record:
    """CO-4: procedure code inconsistent with the modifier used, or required modifier missing."""
    variants = [
        ("97110", 640.00, ["M54.5"], 8, "GP", UHC, PT, "therapy discipline modifier GP"),
        ("20610", 310.00, ["M17.12"], 1, "LT", None, ORTHO, "laterality modifier LT"),
        ("73721", 2600.00, ["M25.562"], 1, "LT", None, IMAGING, "laterality modifier LT"),
    ]
    cpt, charge, dx, units, mod, payer, provider, desc = variants[i % len(variants)]
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, cpt, charge, dx, units=units, pos="22" if provider is IMAGING else "11")],
        diagnoses=dx,
        dos=dos,
        submitted=submitted,
        payer=payer,
        provider=provider,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("4", claim.total_charge, rarc="N822", line_no=1)],
        remit_note=(
            "The procedure code is inconsistent with the modifier used, "
            "or a required modifier is missing."
        ),
        payer_context=f"Documentation supports the service; the {desc} was omitted at charge entry.",
    )
    if claim.total_charge < Decimal("1000.00"):
        route, why = Route.AUTO_FIX_RESUBMIT, f"Append the omitted {desc} and resubmit."
    else:
        route, why = Route.HUMAN_REVIEW, f"Fix is the omitted {desc}, but value exceeds the $1000 auto cap."
    return claim, denial, truth(denial, RootCauseCategory.CODING_MISMATCH, route, why)


def coding_mismatch(i: int) -> Record:
    """CO-11: diagnosis inconsistent with the procedure (correct dx is on the claim)."""
    variants = [
        ("69210", 145.00, "M54.50", "H61.21"),  # cerumen removal pointed at back pain
        ("73721", 890.00, "J06.9", "M25.561"),  # knee MRI pointed at URI
    ]
    cpt, charge, wrong_dx, right_dx = variants[i % 2]
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, cpt, charge, [wrong_dx])],
        diagnoses=[wrong_dx, right_dx],
        dos=dos,
        submitted=submitted,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("11", claim.total_charge, line_no=1)],
        remit_note="The diagnosis is inconsistent with the procedure.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.CODING_MISMATCH,
        Route.AUTO_FIX_RESUBMIT,
        f"Line points at {wrong_dx}; supported diagnosis {right_dx} is already on the claim.",
    )


def medical_necessity(i: int) -> Record:
    """CO-50 + N115: not medically necessary per LCD. Needs clinical documentation."""
    dos, submitted, remit = service_dates()
    variant = ["pt", "labs", "tms"][i % 3]
    if variant == "pt":
        claim = make_claim(
            lines=[line(1, "97110", 2040.00, ["M54.5"], units=24)],
            diagnoses=["M54.5"],
            dos=dos,
            submitted=submitted,
            payer=AETNA,
            provider=PT,
        )
        context = "Visit count exceeds the payer's therapy threshold; progress notes on file."
    elif variant == "labs":
        claim = make_claim(
            lines=[line(1, "80053", 95.00, ["Z00.00"]), line(2, "85025", 65.00, ["Z00.00"])],
            diagnoses=["Z00.00"],
            dos=dos,
            submitted=submitted,
            provider=FAMILY,
        )
        context = "Screening labs billed with a routine-exam diagnosis only."
    else:
        claim = make_claim(
            lines=[line(1, "90867", 6800.00, ["F33.1"], units=4)],
            diagnoses=["F33.1"],
            dos=dos,
            submitted=submitted,
        )
        context = "TMS series billed before documented failure of two antidepressant trials."
    denial = make_denial(
        claim,
        remit,
        [adj("50", claim.total_charge, rarc="N115")],
        remit_note="These are non-covered services because this is not deemed a medical necessity by the payer.",
        payer_context=context,
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.MEDICAL_NECESSITY,
        Route.APPEAL,
        "Medical-necessity denial — appeal with clinical documentation supporting the LCD criteria.",
    )


def bundling_ncci(i: int) -> Record:
    """CO-97/236: service bundled into another same-day procedure (NCCI edit)."""
    variant = ["em_25", "scope", "venipuncture"][i % 3]
    dos, submitted, remit = service_dates()
    if variant == "em_25":
        # E/M denied against same-day injection — modifier 25 missing on the visit.
        lines = [
            line(1, "99213", 185.00, ["M17.11"]),
            line(2, "20610", 310.00, ["M17.11"]),
        ]
        claim = make_claim(lines=lines, diagnoses=["M17.11"], dos=dos, submitted=submitted, payer=UHC, provider=ORTHO)
        denial = make_denial(
            claim,
            remit,
            [adj("97", lines[0].charge, rarc="M80", line_no=1)],
            remit_note="Payment is included in the allowance for another service/procedure.",
            payer_context="Separate E/M documented for new symptom evaluation before the injection.",
        )
        return claim, denial, truth(
            denial,
            RootCauseCategory.BUNDLING_NCCI,
            Route.AUTO_FIX_RESUBMIT,
            "Distinct E/M documented — append modifier 25 to line 1 and resubmit.",
        )
    if variant == "scope":
        lines = [
            line(1, "29881", 2850.00, ["M23.205"], pos="24"),
            line(2, "29877", 1900.00, ["M23.205"], pos="24"),
        ]
        claim = make_claim(lines=lines, diagnoses=["M23.205"], dos=dos, submitted=submitted, payer=BCBS, provider=ORTHO)
        denial = make_denial(
            claim,
            remit,
            [adj("236", lines[1].charge, rarc="N20", line_no=2)],
            remit_note="This procedure is not paid separately when performed with another procedure on the same day.",
        )
        return claim, denial, truth(
            denial,
            RootCauseCategory.BUNDLING_NCCI,
            Route.HUMAN_REVIEW,
            "29877 hits an NCCI edit against 29881 — coder must confirm separate compartment before any modifier.",
        )
    lines = [line(1, "80053", 95.00, ["E11.9"]), line(2, "36415", 18.00, ["E11.9"])]
    claim = make_claim(lines=lines, diagnoses=["E11.9"], dos=dos, submitted=submitted, provider=FAMILY)
    denial = make_denial(
        claim,
        remit,
        [adj("97", lines[1].charge, rarc="N20", line_no=2)],
        remit_note="Payment is included in the allowance for another service/procedure.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.BUNDLING_NCCI,
        Route.WRITE_OFF,
        "Venipuncture correctly bundles into the panel — small-balance write-off.",
    )


def duplicate(i: int) -> Record:
    """CO-18: exact duplicate of an already-processed claim."""
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, "93000", 85.00, ["R00.2"])],
        diagnoses=["R00.2"],
        dos=dos,
        submitted=submitted,
        payer=UHC,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("18", claim.total_charge, rarc="N522")],
        remit_note="Exact duplicate claim/service.",
        payer_context=f"Original claim paid on remit dated {remit - timedelta(days=21)}.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.DUPLICATE,
        Route.WRITE_OFF,
        "Original already paid — close the duplicate, no resubmission.",
    )


def timely_filing(i: int) -> Record:
    """CO-29: filed past the payer's 180-day limit."""
    dos = date(2025, 8, 4) + timedelta(days=rng.randrange(45))
    submitted = dos + timedelta(days=rng.randint(200, 260))
    remit = submitted + timedelta(days=rng.randint(14, 30))
    claim = make_claim(
        lines=[line(1, "99285", 1850.00, ["R07.9"], pos="23")],
        diagnoses=["R07.9"],
        dos=dos,
        submitted=submitted,
        payer=CIGNA,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("29", claim.total_charge)],
        remit_note="The time limit for filing has expired.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.TIMELY_FILING,
        Route.WRITE_OFF,
        f"Submitted {(submitted - dos).days} days after DOS against a 180-day limit; no proof of timely filing.",
    )


def coverage_terminated(i: int) -> Record:
    """CO-27: expenses incurred after coverage terminated."""
    variants = [
        ("45380", 2400.00, ["K63.5"], "22"),
        ("99214", 260.00, ["I10"], "11"),
    ]
    cpt, charge, dx, pos = variants[i % 2]
    dos, submitted, remit = service_dates()
    term_date = dos - timedelta(days=rng.randint(10, 40))
    claim = make_claim(
        lines=[line(1, cpt, charge, dx, pos=pos)],
        diagnoses=dx,
        dos=dos,
        submitted=submitted,
        payer=BCBS,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("27", claim.total_charge)],
        remit_note="Expenses incurred after coverage terminated.",
        payer_context=f"Eligibility file shows coverage termed {term_date} following employment change.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.NON_COVERED,
        Route.WRITE_OFF,
        "Coverage termed before DOS — verify term date, then bill the patient; nothing to resubmit.",
    )


def coordination_of_benefits(i: int) -> Record:
    """CO-22 / OA-23: another payer is primary."""
    variants = [
        ("99214", 260.00, ["I10", "E78.5"], "11"),
        ("73721", 1450.00, ["M25.561"], "22"),
    ]
    cpt, charge, dx, pos = variants[i % 2]
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, cpt, charge, dx, pos=pos)],
        diagnoses=dx,
        dos=dos,
        submitted=submitted,
        payer=CIGNA,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("22", claim.total_charge, group=[GroupCode.CO, GroupCode.OA][i % 2], rarc="MA04")],
        remit_note="This care may be covered by another payer per coordination of benefits.",
        payer_context="Member reported new employer coverage at last visit; COB questionnaire outstanding.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.COORDINATION_OF_BENEFITS,
        Route.HUMAN_REVIEW,
        "Primary payer unknown — staff must resolve COB and bill the primary first.",
    )


def non_covered(i: int) -> Record:
    """CO-96 + N130: excluded under the member's plan."""
    variants = [
        ("97810", 130.00, ["M54.5"], "acupuncture"),
        ("S8930", 95.00, ["M79.606"], "electro-acupuncture"),
        ("82306", 78.00, ["Z13.21"], "screening vitamin D"),
    ]
    cpt, charge, dx, desc = variants[i % 3]
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, cpt, charge, dx)],
        diagnoses=dx,
        dos=dos,
        submitted=submitted,
        payer=BCBS,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("96", claim.total_charge, rarc="N130")],
        remit_note="Non-covered charge(s). Consult plan benefit documents/guidelines for coverage.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.NON_COVERED,
        Route.WRITE_OFF,
        f"Plan excludes {desc} — not correctable or appealable on these facts.",
    )


def patient_responsibility(i: int) -> Record:
    """PR-1/PR-3: applied to deductible or copay — money moves to the patient."""
    carc, label = [("1", "deductible"), ("3", "copay")][i % 2]
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, "99204", 245.00, ["K21.9"])],
        diagnoses=["K21.9"],
        dos=dos,
        submitted=submitted,
    )
    denial = make_denial(
        claim,
        remit,
        [adj(carc, claim.total_charge, group=GroupCode.PR)],
        remit_note=f"Amount applied to patient {label}.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.PATIENT_RESPONSIBILITY,
        Route.WRITE_OFF,
        f"Payer processed correctly — transfer balance to patient {label}; nothing to resubmit.",
    )


def contractual(i: int) -> Record:
    """CO-45: charge exceeds the contracted fee schedule (partial adjustment)."""
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, "99285", 3200.00, ["R07.9"], pos="23")],
        diagnoses=["R07.9"],
        dos=dos,
        submitted=submitted,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("45", money(1850.00))],
        remit_note="Charge exceeds fee schedule/maximum allowable or contracted/legislated fee arrangement.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.CONTRACTUAL,
        Route.WRITE_OFF,
        "Standard contractual adjustment above the allowed amount — write off per contract.",
    )


def unknown(i: int) -> Record:
    """Vague payer denial with no usable detail — must go to a human."""
    dos, submitted, remit = service_dates()
    claim = make_claim(
        lines=[line(1, "99213", 185.00, ["J02.9"])],
        diagnoses=["J02.9"],
        dos=dos,
        submitted=submitted,
        payer=CIGNA,
    )
    denial = make_denial(
        claim,
        remit,
        [adj("A1", claim.total_charge)],
        remit_note="Claim/Service denied. See payer portal for details.",
    )
    return claim, denial, truth(
        denial,
        RootCauseCategory.UNKNOWN,
        Route.HUMAN_REVIEW,
        "Generic A1 denial with no remark code — insufficient signal to classify.",
    )


# Order matters: the 7th record generated is the hero case → CLM-007 / DEN-007.
PLAN: list[tuple] = [
    (auth_required, 5),  # 1-5
    (missing_info, 1),  # 6
    (hero_den_007, 1),  # 7  ← DEN-007
    (missing_info, 3),  # 8-10
    (missing_modifier, 3),
    (coding_mismatch, 3),
    (medical_necessity, 4),
    (bundling_ncci, 4),
    (duplicate, 3),
    (timely_filing, 3),
    (coverage_terminated, 2),
    (coordination_of_benefits, 3),
    (non_covered, 3),
    (patient_responsibility, 3),
    (contractual, 2),
    (unknown, 1),
]

# --------------------------------------------------------------------------- #
# Resubmission history — 15 past outcomes the agent can learn from
# --------------------------------------------------------------------------- #

RESUBMIT_HISTORY = [
    {
        "resubmission_id": "RSB-001",
        "claim_id": "HIST-CLM-091",
        "payer_name": "Aetna",
        "original_carc": "197",
        "original_rarc": "M62",
        "fix_applied": "Keyed the existing prior-auth number onto the claim.",
        "fields_changed": ["prior_auth_number"],
        "resubmitted_date": "2026-01-08",
        "outcome": "paid",
        "days_to_outcome": 18,
        "notes": "Auth had been obtained by phone but never entered.",
    },
    {
        "resubmission_id": "RSB-002",
        "claim_id": "HIST-CLM-102",
        "payer_name": "UnitedHealthcare",
        "original_carc": "97",
        "original_rarc": "M80",
        "fix_applied": "Appended modifier 25 to the E/M billed with a same-day 20610 injection.",
        "fields_changed": ["lines[0].modifiers"],
        "resubmitted_date": "2026-01-15",
        "outcome": "paid",
        "days_to_outcome": 14,
        "notes": "Separately identifiable E/M documented; per UHC-CP-044.",
    },
    {
        "resubmission_id": "RSB-003",
        "claim_id": "HIST-CLM-117",
        "payer_name": "Cigna",
        "original_carc": "16",
        "original_rarc": "N382",
        "fix_applied": "Replaced placeholder subscriber ID with the active member ID.",
        "fields_changed": ["subscriber_id"],
        "resubmitted_date": "2026-01-22",
        "outcome": "paid",
        "days_to_outcome": 12,
        "notes": None,
    },
    {
        "resubmission_id": "RSB-004",
        "claim_id": "HIST-CLM-124",
        "payer_name": "Blue Cross Blue Shield",
        "original_carc": "4",
        "original_rarc": "N822",
        "fix_applied": "Added laterality modifier LT omitted at charge entry.",
        "fields_changed": ["lines[0].modifiers"],
        "resubmitted_date": "2026-02-02",
        "outcome": "paid",
        "days_to_outcome": 16,
        "notes": None,
    },
    {
        "resubmission_id": "RSB-005",
        "claim_id": "HIST-CLM-131",
        "payer_name": "UnitedHealthcare",
        "original_carc": "16",
        "original_rarc": "N54",
        "fix_applied": "Resubmitted unchanged with medical records attached.",
        "fields_changed": [],
        "resubmitted_date": "2026-02-05",
        "outcome": "denied_again",
        "days_to_outcome": 21,
        "notes": "16/N54 was masking an NCCI bundling edit — attachments did not address it.",
    },
    {
        "resubmission_id": "RSB-006",
        "claim_id": "HIST-CLM-131",
        "payer_name": "UnitedHealthcare",
        "original_carc": "16",
        "original_rarc": "N54",
        "fix_applied": "Appended modifier 25 to the E/M billed same day as an arthroscopy.",
        "fields_changed": ["lines[0].modifiers"],
        "resubmitted_date": "2026-03-01",
        "outcome": "paid",
        "days_to_outcome": 15,
        "notes": "Second attempt on the same claim — real cause was bundling, not missing info.",
    },
    {
        "resubmission_id": "RSB-007",
        "claim_id": "HIST-CLM-138",
        "payer_name": "Aetna",
        "original_carc": "50",
        "original_rarc": "N115",
        "fix_applied": "First-level appeal with progress notes and LCD criteria mapping.",
        "fields_changed": [],
        "resubmitted_date": "2026-02-10",
        "outcome": "paid",
        "days_to_outcome": 45,
        "notes": "Overturned on appeal; per AET-THER-033 continued-care documentation.",
    },
    {
        "resubmission_id": "RSB-008",
        "claim_id": "HIST-CLM-142",
        "payer_name": "Cigna",
        "original_carc": "29",
        "original_rarc": None,
        "fix_applied": "Resubmitted with a cover letter but no proof of timely filing.",
        "fields_changed": [],
        "resubmitted_date": "2026-02-12",
        "outcome": "denied_again",
        "days_to_outcome": 20,
        "notes": "CIG-ADM-081 requires clearinghouse acceptance proof; none existed.",
    },
    {
        "resubmission_id": "RSB-009",
        "claim_id": "HIST-CLM-150",
        "payer_name": "Blue Cross Blue Shield",
        "original_carc": "11",
        "original_rarc": None,
        "fix_applied": "Repointed the line to the supported diagnosis already on the claim.",
        "fields_changed": ["lines[0].icd10_pointers"],
        "resubmitted_date": "2026-02-18",
        "outcome": "paid",
        "days_to_outcome": 13,
        "notes": None,
    },
    {
        "resubmission_id": "RSB-010",
        "claim_id": "HIST-CLM-155",
        "payer_name": "Aetna",
        "original_carc": "197",
        "original_rarc": "M62",
        "fix_applied": "Retro-authorization appeal for a 29881 performed without precert.",
        "fields_changed": [],
        "resubmitted_date": "2026-02-20",
        "outcome": "denied_again",
        "days_to_outcome": 38,
        "notes": "AET-SURG-014 allows retro requests only within 3 business days of service.",
    },
    {
        "resubmission_id": "RSB-011",
        "claim_id": "HIST-CLM-161",
        "payer_name": "UnitedHealthcare",
        "original_carc": "18",
        "original_rarc": "N522",
        "fix_applied": "Resubmitted the same claim a third time.",
        "fields_changed": [],
        "resubmitted_date": "2026-02-25",
        "outcome": "denied_again",
        "days_to_outcome": 9,
        "notes": "Original had already paid — resubmitting duplicates only creates rework.",
    },
    {
        "resubmission_id": "RSB-012",
        "claim_id": "HIST-CLM-168",
        "payer_name": "Cigna",
        "original_carc": "22",
        "original_rarc": "MA04",
        "fix_applied": "Billed the primary payer first, then resubmitted with the primary EOB.",
        "fields_changed": [],
        "resubmitted_date": "2026-03-04",
        "outcome": "paid",
        "days_to_outcome": 60,
        "notes": "COB resolved via member questionnaire per CIG-ADM-102.",
    },
    {
        "resubmission_id": "RSB-013",
        "claim_id": "HIST-CLM-172",
        "payer_name": "Blue Cross Blue Shield",
        "original_carc": "236",
        "original_rarc": "N20",
        "fix_applied": "Appended modifier 59 without supporting documentation.",
        "fields_changed": ["lines[1].modifiers"],
        "resubmitted_date": "2026-03-08",
        "outcome": "denied_again",
        "days_to_outcome": 17,
        "notes": "BCBS-RP-107: modifier 59 requires documented distinct anatomic site.",
    },
    {
        "resubmission_id": "RSB-014",
        "claim_id": "HIST-CLM-172",
        "payer_name": "Blue Cross Blue Shield",
        "original_carc": "236",
        "original_rarc": "N20",
        "fix_applied": "Corrected coding per the NCCI edit and wrote off the bundled component.",
        "fields_changed": ["lines[1].modifiers"],
        "resubmitted_date": "2026-03-22",
        "outcome": "paid",
        "days_to_outcome": 14,
        "notes": "Same-compartment chondroplasty is genuinely bundled with 29881.",
    },
    {
        "resubmission_id": "RSB-015",
        "claim_id": "HIST-CLM-180",
        "payer_name": "Aetna",
        "original_carc": "16",
        "original_rarc": "M76",
        "fix_applied": "Added the missing diagnosis pointer to the service line.",
        "fields_changed": ["lines[0].icd10_pointers"],
        "resubmitted_date": "2026-03-25",
        "outcome": "paid",
        "days_to_outcome": 11,
        "notes": None,
    },
]


def main() -> None:
    records: list[Record] = []
    calls: dict = defaultdict(int)
    for builder, count in PLAN:
        for _ in range(count):
            records.append(builder(calls[builder]))
            calls[builder] += 1

    hero_claim, hero_denial, _ = records[6]
    assert hero_denial.denial_id == "DEN-007", f"hero landed at {hero_denial.denial_id}"
    assert hero_claim.lines[0].cpt_hcpcs == "99213" and "25" not in hero_claim.lines[0].modifiers

    claims = [c.model_dump(mode="json") for c, _, _ in records]
    denials = [d.model_dump(mode="json") for _, d, _ in records]
    ground_truth = [t for _, _, t in records]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in [
        ("claims.json", claims),
        ("denials.json", denials),
        ("ground_truth.json", ground_truth),
        ("resubmit_history.json", RESUBMIT_HISTORY),
    ]:
        path = OUT_DIR / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(payload)} records)")

    total = sum(Decimal(d["total_denied"]) for d in denials)
    by_route: dict[str, int] = {}
    for t in ground_truth:
        by_route[t["expected_route"]] = by_route.get(t["expected_route"], 0) + 1
    print(f"total denied: ${total}")
    print(f"routes: {by_route}")


if __name__ == "__main__":
    main()
