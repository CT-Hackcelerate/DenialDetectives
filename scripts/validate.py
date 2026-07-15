"""Validate every synthetic data file against the Pydantic models and the
dataset invariants.

Usage:
    python scripts/validate.py

Checks:
  * claims.json / denials.json parse as app.models.Claim / Denial
  * ground_truth.json / resubmit_history.json parse against local schemas
  * referential integrity (denial -> claim 1:1, ground truth covers all denials)
  * payers are Aetna / UnitedHealthcare / Cigna / Blue Cross Blue Shield
  * total denied lands in the $55k-$70k target band
  * required CARC spread: 16, 97, 4, 197, 50, 29, 27, 18
  * all four triage routes are exercised
  * DEN-007 hero case: CARC 16 + RARC N54 red herring over a missing
    modifier 25 (99213 + same-day 29881, no '25' on the E/M line)
  * carc_codes.csv / ncci_edits.csv have the right columns, row counts,
    and the 29881/99213 NCCI pair
  * 8 policy markdown files, 300-600 words each, each with a citable policy
    number; modifier-25 and Aetna-29881-precert policies present

Exits non-zero on the first failure.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]  # claimguard/
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import BaseModel, Field  # noqa: E402

from app.models import Claim, Denial, RootCauseCategory, Route  # noqa: E402

DATA_DIR = ROOT / "backend" / "data" / "synthetic"
SOURCES_DIR = ROOT / "backend" / "app" / "knowledge" / "sources"
POLICIES_DIR = SOURCES_DIR / "policies"

BIG_FOUR = {"Aetna", "UnitedHealthcare", "Cigna", "Blue Cross Blue Shield"}
REQUIRED_CARCS = {"16", "97", "4", "197", "50", "29", "27", "18"}


class GroundTruthEntry(BaseModel):
    denial_id: str
    claim_id: str
    category: RootCauseCategory
    expected_route: Route
    synopsis: str = Field(..., min_length=1)


class ResubmitHistoryEntry(BaseModel):
    resubmission_id: str
    claim_id: str
    payer_name: str
    original_carc: str
    original_rarc: str | None
    fix_applied: str = Field(..., min_length=1)
    fields_changed: list[str]
    resubmitted_date: date
    outcome: Literal["paid", "denied_again"]
    days_to_outcome: int = Field(..., ge=0)
    notes: str | None


_passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed
    if not condition:
        print(f"FAIL  {label}" + (f" — {detail}" if detail else ""))
        sys.exit(1)
    _passed += 1
    print(f"ok    {label}")


def main() -> None:
    # ---- JSON files parse against the models ------------------------------ #
    claims = [Claim.model_validate(x) for x in json.loads((DATA_DIR / "claims.json").read_text(encoding="utf-8"))]
    denials = [Denial.model_validate(x) for x in json.loads((DATA_DIR / "denials.json").read_text(encoding="utf-8"))]
    truths = [
        GroundTruthEntry.model_validate(x)
        for x in json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    ]
    history = [
        ResubmitHistoryEntry.model_validate(x)
        for x in json.loads((DATA_DIR / "resubmit_history.json").read_text(encoding="utf-8"))
    ]
    check("claims.json parses as list[Claim]", len(claims) >= 20, f"{len(claims)} claims")
    check("denials.json parses as list[Denial]", len(denials) >= 20, f"{len(denials)} denials")
    check("ground_truth.json parses", len(truths) == len(denials))
    check("resubmit_history.json parses (15 entries)", len(history) == 15, f"{len(history)} entries")

    # ---- Referential integrity -------------------------------------------- #
    claim_ids = {c.claim_id for c in claims}
    check("claim IDs unique", len(claim_ids) == len(claims))
    check("every denial pairs with exactly one claim",
          len({d.claim_id for d in denials}) == len(denials) and all(d.claim_id in claim_ids for d in denials))
    check("ground truth covers every denial",
          {t.denial_id for t in truths} == {d.denial_id for d in denials})
    for d in denials:
        c = next(c for c in claims if c.claim_id == d.claim_id)
        if d.total_denied > c.total_charge:
            check("denied amount <= total charge", False, d.denial_id)
    check("denied amount <= total charge", True)

    # ---- Dataset-level requirements ---------------------------------------- #
    payers = {c.payer_name for c in claims}
    check("payers are Aetna/UHC/Cigna/BCBS", payers <= BIG_FOUR, str(payers - BIG_FOUR))
    total = sum((d.total_denied for d in denials), Decimal("0"))
    check("total denied in $55k-$70k band", Decimal("55000") <= total <= Decimal("70000"), f"${total}")
    carcs = {a.carc for d in denials for a in d.adjustments}
    check("required CARC spread present", REQUIRED_CARCS <= carcs, f"missing {REQUIRED_CARCS - carcs}")
    routes = {t.expected_route for t in truths}
    check("all four routes exercised", routes == set(Route), f"got {sorted(r.value for r in routes)}")

    # ---- Hero case DEN-007 -------------------------------------------------- #
    hero = next((d for d in denials if d.denial_id == "DEN-007"), None)
    check("DEN-007 exists", hero is not None)
    a = hero.adjustments[0]
    check("DEN-007 stated reason is CARC 16 + RARC N54", a.carc == "16" and a.rarc == "N54")
    hero_claim = next(c for c in claims if c.claim_id == hero.claim_id)
    cpts = {ln.cpt_hcpcs for ln in hero_claim.lines}
    em = next(ln for ln in hero_claim.lines if ln.cpt_hcpcs == "99213")
    check("DEN-007 real cause in place: 99213 + same-day 29881, no modifier 25",
          {"99213", "29881"} <= cpts and "25" not in em.modifiers)
    hero_truth = next(t for t in truths if t.denial_id == "DEN-007")
    check("DEN-007 ground truth is bundling_ncci (not missing_info)",
          hero_truth.category is RootCauseCategory.BUNDLING_NCCI)

    # ---- carc_codes.csv ------------------------------------------------------ #
    with (SOURCES_DIR / "carc_codes.csv").open(encoding="utf-8", newline="") as f:
        carc_rows = list(csv.DictReader(f))
    check("carc_codes.csv has ~30 rows", 28 <= len(carc_rows) <= 40, f"{len(carc_rows)} rows")
    check("carc_codes.csv columns",
          set(carc_rows[0]) == {"code", "description", "category", "typical_root_cause", "typical_fix"})
    check("carc_codes.csv rows complete", all(all(v.strip() for v in r.values()) for r in carc_rows))
    check("carc_codes.csv covers the denial CARCs used in denials.json",
          {a.carc for d in denials for a in d.adjustments} <= {r["code"] for r in carc_rows})

    # ---- ncci_edits.csv ------------------------------------------------------ #
    with (SOURCES_DIR / "ncci_edits.csv").open(encoding="utf-8", newline="") as f:
        ncci_rows = list(csv.DictReader(f))
    check("ncci_edits.csv has ~40 rows", 38 <= len(ncci_rows) <= 50, f"{len(ncci_rows)} rows")
    check("ncci_edits.csv columns",
          set(ncci_rows[0]) == {"column1_cpt", "column2_cpt", "modifier_indicator", "rationale"})
    check("ncci_edits.csv modifier_indicator is 0/1",
          all(r["modifier_indicator"] in {"0", "1"} for r in ncci_rows))
    check("ncci_edits.csv includes the 29881/99213 pair",
          any(r["column1_cpt"] == "29881" and r["column2_cpt"] == "99213" for r in ncci_rows))

    # ---- Payer policies -------------------------------------------------------- #
    policies = sorted(POLICIES_DIR.glob("*.md"))
    check("8 policy files", len(policies) == 8, f"{len(policies)} files")
    texts = {p.name: p.read_text(encoding="utf-8") for p in policies}
    for name, text in texts.items():
        words = len(text.split())
        check(f"{name} is 300-600 words", 300 <= words <= 600, f"{words} words")
        check(f"{name} has a citable policy number",
              re.search(r"\*\*Policy Number:\*\*\s+[A-Z]{3,4}-[A-Z]{2,4}-\d{3}", text) is not None)
    flat = {name: re.sub(r"\s+", " ", text).lower() for name, text in texts.items()}
    check("a policy requires modifier 25 for same-day E/M",
          any("modifier 25" in t and "same date of service" in t for t in flat.values()))
    check("a policy states Aetna requires precert for 29881",
          any("aetna" in t and "29881" in t and "precert" in t for t in flat.values()))

    print(f"\nAll {_passed} checks passed.")
    print(f"  claims/denials: {len(claims)}  |  total denied: ${total}")
    print(f"  CARCs: {sorted(carcs)}")


if __name__ == "__main__":
    main()
