"""In-memory store for claims and denials.

Loads the synthetic seed set from data/synthetic, plus any uploaded live-feed
batches persisted under data/feeds/ (written by POST /api/feed), so uploads
survive a backend restart.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.models import Claim, Denial

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"
FEEDS_DIR = Path(__file__).resolve().parents[2] / "data" / "feeds"

_claims: dict[str, Claim] | None = None
_denials: dict[str, Denial] | None = None


def _load() -> None:
    global _claims, _denials
    if _claims is not None and _denials is not None:
        return
    raw = json.loads((DATA_DIR / "claims.json").read_text(encoding="utf-8"))
    _claims = {c["claim_id"]: Claim.model_validate(c) for c in raw}
    raw = json.loads((DATA_DIR / "denials.json").read_text(encoding="utf-8"))
    _denials = {d["denial_id"]: Denial.model_validate(d) for d in raw}
    # replay previously uploaded feeds (oldest first)
    feeds = Path(FEEDS_DIR)
    if feeds.exists():
        for path in sorted(feeds.glob("*.json")):
            batch = json.loads(path.read_text(encoding="utf-8"))
            for c in batch.get("claims", []):
                claim = Claim.model_validate(c)
                _claims[claim.claim_id] = claim
            for d in batch.get("denials", []):
                denial = Denial.model_validate(d)
                _denials[denial.denial_id] = denial


def get_claim(claim_id: str) -> Claim | None:
    _load()
    return _claims.get(claim_id)


def get_denial(denial_id: str) -> Denial | None:
    _load()
    return _denials.get(denial_id)


def list_denials() -> list[Denial]:
    _load()
    return list(_denials.values())


def save_claim(claim: Claim) -> None:
    """In-memory upsert (used after a validated Fix is applied)."""
    _load()
    _claims[claim.claim_id] = claim


def add_feed(claims: list[Claim], denials: list[Denial]) -> str:
    """Insert an already-validated feed batch and persist it to data/feeds/."""
    _load()
    for claim in claims:
        _claims[claim.claim_id] = claim
    for denial in denials:
        _denials[denial.denial_id] = denial
    feeds = Path(FEEDS_DIR)
    feeds.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    path = feeds / f"feed-{stamp}.json"
    path.write_text(
        json.dumps(
            {
                "claims": [c.model_dump(mode="json") for c in claims],
                "denials": [d.model_dump(mode="json") for d in denials],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path.name
