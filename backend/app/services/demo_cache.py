"""Demo trace cache: record agent traces on first run, replay them offline.

Every live /api/process run saves its full TraceEvent list to
backend/data/demo_cache/{denial_id}.json (first run only — the cache is never
overwritten, so the demo stays stable). With DEMO_MODE=replay the API serves
the cached trace at a fixed pace (default 100ms/event) without touching the
model or the network.

Warm the whole cache once while online:  POST /api/batch
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from app.config import settings
from app.models import TraceEvent

BACKEND_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = Path(os.environ.get("CLAIMGUARD_DEMO_CACHE_DIR") or BACKEND_DIR / "data" / "demo_cache")


def is_replay() -> bool:
    return settings.demo_mode.strip().lower() == "replay"


def _path(denial_id: str) -> Path:
    directory = Path(CACHE_DIR)  # read at call time so tests can repoint it
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{denial_id}.json"


def has_trace(denial_id: str) -> bool:
    return _path(denial_id).exists()


def save_trace(denial_id: str, events: list[TraceEvent]) -> bool:
    """Persist a trace — first run only, and only if it ran to completion
    (an aborted/error trace must never become the demo). Returns True if written."""
    path = _path(denial_id)
    if path.exists():
        return False
    if not any(e.type == "completed" for e in events):
        return False
    if any(e.type == "error" for e in events):
        return False
    payload = {
        "denial_id": denial_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events": [e.model_dump(mode="json") for e in events],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def load_trace(denial_id: str) -> list[TraceEvent] | None:
    path = _path(denial_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [TraceEvent.model_validate(e) for e in payload["events"]]


async def replay(denial_id: str) -> AsyncGenerator[TraceEvent, None]:
    """Yield the cached trace with fixed pacing (settings.demo_replay_delay)."""
    events = load_trace(denial_id) or []
    for event in events:
        yield event
        await asyncio.sleep(settings.demo_replay_delay)
