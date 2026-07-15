"""Outcome + lesson memory for the triage agent.

Two JSON files under backend/data/memory/ (override dir via CLAIMGUARD_MEMORY_DIR):
  * outcomes.json — every completed triage (route, category, resubmit status)
  * lessons.json  — one-line lessons tagged (payer, carc). Seeded with 3;
    a new lesson is written automatically after every failed resubmission.

lessons_for(payer, carc) feeds the prompt: past failures with this payer+CARC
are injected into future runs so the agent doesn't repeat them.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
MEMORY_DIR = Path(os.environ.get("CLAIMGUARD_MEMORY_DIR") or BACKEND_DIR / "data" / "memory")

SEED_LESSONS = [
    {
        "payer": "UnitedHealthcare",
        "carc": "16",
        "lesson": (
            "UHC 16/N54 on an E/M billed with a same-day procedure is usually a missing "
            "modifier 25, not missing documentation — run ncci_edit_check before attaching records."
        ),
        "source": "seed",
    },
    {
        "payer": "Aetna",
        "carc": "197",
        "lesson": (
            "Aetna denies retro-auth requests for 29881 made more than 3 business days after "
            "service (AET-SURG-014) — resubmitting unchanged without an auth number never pays."
        ),
        "source": "seed",
    },
    {
        "payer": "Cigna",
        "carc": "29",
        "lesson": (
            "Cigna CARC 29 reconsiderations fail without clearinghouse acceptance proof "
            "(CIG-ADM-081); a cover letter or ledger screenshot is not proof."
        ),
        "source": "seed",
    },
]


def _path(name: str) -> Path:
    directory = Path(MEMORY_DIR)  # read at call time so tests can repoint it
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def _read(name: str) -> list[dict]:
    path = _path(name)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _read_obj(name: str) -> dict:
    path = _path(name)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(name: str, records) -> None:
    _path(name).write_text(
        json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Lessons
# --------------------------------------------------------------------------- #


def all_lessons() -> list[dict]:
    """All lessons, seeding the file on first access."""
    lessons = _read("lessons.json")
    if not lessons:
        lessons = [dict(seed) for seed in SEED_LESSONS]
        _write("lessons.json", lessons)
    return lessons


def add_lesson(payer: str, carc: str, lesson: str) -> dict:
    """Append a one-line lesson tagged (payer, carc). Deduplicates exact repeats."""
    lessons = all_lessons()
    for existing in lessons:
        if (existing["payer"], existing["carc"], existing["lesson"]) == (payer, carc, lesson):
            return existing
    entry = {"payer": payer, "carc": carc, "lesson": lesson, "source": "learned", "ts": _now()}
    lessons.append(entry)
    _write("lessons.json", lessons)
    return entry


def lessons_for(payer: str, carc: str, limit: int = 5) -> list[dict]:
    """Lessons for this payer, most specific first: (payer, carc) then payer-wide."""
    lessons = all_lessons()
    exact = [x for x in lessons if x["payer"] == payer and x["carc"] == str(carc)]
    same_payer = [x for x in lessons if x["payer"] == payer and x["carc"] != str(carc)]
    return (exact + same_payer)[:limit]


def lesson_from_failed_resubmit(payer: str, carc: str, fix_summary: str, errors: list[str]) -> str:
    """Deterministic one-line lesson from a clearinghouse rejection."""
    reason = errors[0] if errors else "rejected by clearinghouse"
    return f"{payer} CARC {carc}: resubmit with '{fix_summary}' failed — {reason}."


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


def record_outcome(
    *,
    denial_id: str,
    payer: str,
    carc: str,
    route: str,
    root_cause_category: str | None,
    confidence: float,
    resubmit_status: str | None = None,
) -> dict:
    """Store the outcome of one triage run (called for every completed run)."""
    outcomes = _read("outcomes.json")
    entry = {
        "ts": _now(),
        "denial_id": denial_id,
        "payer": payer,
        "carc": carc,
        "route": route,
        "root_cause_category": root_cause_category,
        "confidence": confidence,
        "resubmit_status": resubmit_status,
    }
    outcomes.append(entry)
    _write("outcomes.json", outcomes)
    return entry


def all_outcomes() -> list[dict]:
    return _read("outcomes.json")


def latest_outcomes_by_denial() -> dict[str, dict]:
    """Latest outcome per denial (append order is chronological, last wins)."""
    latest: dict[str, dict] = {}
    for outcome in all_outcomes():
        latest[outcome["denial_id"]] = outcome
    return latest


# --------------------------------------------------------------------------- #
# Decision records (feed the approve / override endpoints)
# --------------------------------------------------------------------------- #


def save_decision_record(denial_id: str, record: dict) -> dict:
    """Upsert the latest decision record for a denial."""
    records = _read_obj("decisions.json")
    records[denial_id] = {**record, "ts": _now()}
    _write("decisions.json", records)
    return records[denial_id]


def get_decision_record(denial_id: str) -> dict | None:
    return _read_obj("decisions.json").get(denial_id)
