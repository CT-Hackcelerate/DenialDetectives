"""Read endpoints (claims, denials, lessons, analytics) + live feed ingestion."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError

from app.models import Claim, Denial
from app.services import claim_repo, memory

router = APIRouter(prefix="/api", tags=["claims"])


@router.get("/claims")
def list_claims() -> list[dict]:
    return [
        {
            "claim_id": c.claim_id,
            "payer_name": c.payer_name,
            "date_of_service": str(c.date_of_service),
            "total_charge": str(c.total_charge),
            "revision": c.revision,
        }
        for c in (claim_repo.get_claim(d.claim_id) for d in claim_repo.list_denials())
        if c is not None
    ]


@router.get("/claims/{claim_id}")
def get_claim(claim_id: str) -> dict:
    claim = claim_repo.get_claim(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail=f"claim {claim_id} not found")
    return claim.model_dump(mode="json")


@router.get("/denials")
def list_denials() -> list[dict]:
    return [
        {
            "denial_id": d.denial_id,
            "claim_id": d.claim_id,
            "payer_name": d.payer_name,
            "remit_date": str(d.remit_date),
            "total_denied": str(d.total_denied),
            "carcs": sorted({a.carc for a in d.adjustments}),
        }
        for d in claim_repo.list_denials()
    ]


@router.get("/denials/{denial_id}")
def get_denial(denial_id: str) -> dict:
    denial = claim_repo.get_denial(denial_id)
    if denial is None:
        raise HTTPException(status_code=404, detail=f"denial {denial_id} not found")
    return denial.model_dump(mode="json")


@router.get("/lessons")
def list_lessons() -> list[dict]:
    """Every lesson the agent has learned (seeded with 3, grows on failed resubmits)."""
    return memory.all_lessons()


class FeedPayload(BaseModel):
    """A live-feed batch: new claims paired with their 835 denials."""

    claims: list[dict] = Field(default_factory=list)
    denials: list[dict] = Field(default_factory=list)


def _pydantic_errors(prefix: str, exc: ValidationError) -> list[str]:
    return [
        f"{prefix}.{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
        for err in exc.errors()[:3]
    ]


@router.post("/feed")
def upload_feed(payload: FeedPayload) -> dict:
    """Validate and ingest an uploaded feed. All-or-nothing: any error rejects
    the whole batch with a per-record error list (HTTP 422)."""
    errors: list[str] = []
    claims: list[Claim] = []
    denials: list[Denial] = []

    if not payload.claims and not payload.denials:
        raise HTTPException(status_code=422, detail={"errors": [
            'feed is empty — expected {"claims": [...], "denials": [...]}'
        ]})

    new_claim_ids: set[str] = set()
    for i, raw in enumerate(payload.claims):
        label = f"claims[{i}]" + (f" ({raw.get('claim_id')})" if isinstance(raw, dict) and raw.get("claim_id") else "")
        try:
            claim = Claim.model_validate(raw)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(label, exc))
            continue
        if claim_repo.get_claim(claim.claim_id) or claim.claim_id in new_claim_ids:
            errors.append(f"{label}: duplicate claim_id — already exists")
            continue
        line_sum = sum((ln.charge for ln in claim.lines), Decimal("0"))
        if line_sum != claim.total_charge:
            errors.append(
                f"{label}: total_charge ${claim.total_charge} != sum of line charges ${line_sum}"
            )
            continue
        new_claim_ids.add(claim.claim_id)
        claims.append(claim)

    claims_by_id = {c.claim_id: c for c in claims}
    new_denial_ids: set[str] = set()
    for i, raw in enumerate(payload.denials):
        label = f"denials[{i}]" + (f" ({raw.get('denial_id')})" if isinstance(raw, dict) and raw.get("denial_id") else "")
        try:
            denial = Denial.model_validate(raw)
        except ValidationError as exc:
            errors.extend(_pydantic_errors(label, exc))
            continue
        if claim_repo.get_denial(denial.denial_id) or denial.denial_id in new_denial_ids:
            errors.append(f"{label}: duplicate denial_id — already exists")
            continue
        claim = claims_by_id.get(denial.claim_id) or claim_repo.get_claim(denial.claim_id)
        if claim is None:
            errors.append(f"{label}: claim_id '{denial.claim_id}' not found in this feed or the repo")
            continue
        if denial.total_denied > claim.total_charge:
            errors.append(
                f"{label}: total_denied ${denial.total_denied} exceeds the claim's "
                f"total_charge ${claim.total_charge}"
            )
            continue
        adj_sum = sum((a.amount for a in denial.adjustments), Decimal("0"))
        if adj_sum != denial.total_denied:
            errors.append(
                f"{label}: total_denied ${denial.total_denied} != sum of adjustments ${adj_sum}"
            )
            continue
        new_denial_ids.add(denial.denial_id)
        denials.append(denial)

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    feed_file = claim_repo.add_feed(claims, denials)
    return {
        "accepted": {"claims": len(claims), "denials": len(denials)},
        "persisted_as": feed_file,
        "note": "New denials are in the worklist; process them live (no cached demo trace).",
    }


@router.get("/report")
def report() -> dict:
    """Payer-wise denial analytics: volumes, reasons, routes, recovery, history."""
    import json
    from collections import Counter, defaultdict
    from decimal import Decimal
    from pathlib import Path

    from app.agent.tools import _carc_table

    denials = claim_repo.list_denials()
    latest = memory.latest_outcomes_by_denial()
    carc_desc = {code: row["description"] for code, row in _carc_table().items()}

    payers: dict[str, dict] = defaultdict(lambda: {
        "denials": 0, "denied": Decimal("0"), "recovered": Decimal("0"),
        "processed": 0, "route_counts": Counter(), "carc_counts": Counter(),
        "carc_denied": defaultdict(lambda: Decimal("0")), "lag_days": [],
    })
    carc_totals: dict[str, dict] = defaultdict(lambda: {"count": 0, "denied": Decimal("0")})

    for d in denials:
        p = payers[d.payer_name]
        p["denials"] += 1
        p["denied"] += d.total_denied
        claim = claim_repo.get_claim(d.claim_id)
        if claim:
            p["lag_days"].append((d.remit_date - claim.date_of_service).days)
        primary = d.adjustments[0].carc
        p["carc_counts"][primary] += 1
        p["carc_denied"][primary] += d.total_denied
        carc_totals[primary]["count"] += 1
        carc_totals[primary]["denied"] += d.total_denied
        outcome = latest.get(d.denial_id)
        if outcome:
            p["processed"] += 1
            p["route_counts"][outcome["route"]] += 1
            if outcome.get("resubmit_status") == "accepted":
                p["recovered"] += d.total_denied

    history_path = Path(claim_repo.DATA_DIR) / "resubmit_history.json"
    history: dict[str, Counter] = defaultdict(Counter)
    if history_path.exists():
        for entry in json.loads(history_path.read_text(encoding="utf-8")):
            history[entry["payer_name"]][entry["outcome"]] += 1

    payer_rows = [
        {
            "payer": name,
            "denials": p["denials"],
            "denied": str(p["denied"]),
            "recovered": str(p["recovered"]),
            "processed": p["processed"],
            "avg_remit_lag_days": round(sum(p["lag_days"]) / len(p["lag_days"]), 1) if p["lag_days"] else None,
            "route_counts": dict(p["route_counts"]),
            "top_carcs": [
                {"carc": carc, "count": count, "denied": str(p["carc_denied"][carc]),
                 "description": carc_desc.get(carc, "")}
                for carc, count in p["carc_counts"].most_common(3)
            ],
            "fix_history": {"paid": history[name]["paid"], "denied_again": history[name]["denied_again"]},
        }
        for name, p in sorted(payers.items(), key=lambda kv: kv[1]["denied"], reverse=True)
    ]
    carc_rows = [
        {"carc": carc, "count": v["count"], "denied": str(v["denied"]),
         "description": carc_desc.get(carc, "")}
        for carc, v in sorted(carc_totals.items(), key=lambda kv: kv[1]["denied"], reverse=True)
    ]
    return {
        "totals": {
            "denials": len(denials),
            "denied": str(sum((d.total_denied for d in denials), Decimal("0"))),
            "recovered": str(sum((p["recovered"] for p in payers.values()), Decimal("0"))),
            "processed": len(latest),
            "lessons_learned": sum(1 for x in memory.all_lessons() if x.get("source") == "learned"),
        },
        "payers": payer_rows,
        "carcs": carc_rows,
    }


@router.get("/stats")
def stats() -> dict:
    """Dollars recovered + route counts, from the latest outcome per denial."""
    from decimal import Decimal

    latest = memory.latest_outcomes_by_denial()
    route_counts: dict[str, int] = {}
    route_details: dict[str, list[dict]] = {}
    recovered = Decimal("0.00")
    at_stake = Decimal("0.00")
    for denial_id, outcome in latest.items():
        route = outcome["route"]
        route_counts[route] = route_counts.get(route, 0) + 1
        denial = claim_repo.get_denial(denial_id)
        if denial is None:
            continue
        at_stake += denial.total_denied
        if outcome.get("resubmit_status") == "accepted":
            recovered += denial.total_denied
        route_details.setdefault(route, []).append({
            "denial_id": denial_id,
            "payer_name": denial.payer_name,
            "total_denied": str(denial.total_denied),
            "root_cause_category": outcome.get("root_cause_category"),
            "resubmit_status": outcome.get("resubmit_status"),
        })
    for entries in route_details.values():
        entries.sort(key=lambda e: float(e["total_denied"]), reverse=True)
    return {
        "denials_processed": len(latest),
        "dollars_recovered": str(recovered),
        "dollars_processed": str(at_stake),
        "route_counts": route_counts,
        "route_details": route_details,
        "lessons_learned": sum(1 for x in memory.all_lessons() if x.get("source") == "learned"),
    }
