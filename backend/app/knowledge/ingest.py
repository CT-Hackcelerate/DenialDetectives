"""Embed the synthetic reference corpus into ChromaDB.

Usage:
    python -m app.knowledge.ingest        (from backend/)

Ingests:
  * sources/policies/*.md  -> payer_policies collection, chunked to ~200 tokens
    with 20-token overlap, metadata {payer, doc_id, policy_number, topic}
  * ../data/synthetic/resubmit_history.json -> resubmit_history collection,
    one document per past resubmission, metadata {payer, carc, outcome, ...}

Idempotent: chunk IDs are deterministic (filename + chunk index) and writes go
through upsert, so re-running refreshes in place instead of duplicating.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.knowledge import store

SOURCES_DIR = Path(__file__).resolve().parent / "sources"
POLICIES_DIR = SOURCES_DIR / "policies"
HISTORY_JSON = store.BACKEND_DIR / "data" / "synthetic" / "resubmit_history.json"

CHUNK_TOKENS = 200
CHUNK_OVERLAP = 20


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on whitespace into ~chunk_tokens-word windows with overlap."""
    words = text.split()
    if len(words) <= chunk_tokens:
        return [" ".join(words)]
    chunks = []
    step = chunk_tokens - overlap
    for start in range(0, len(words), step):
        window = words[start : start + chunk_tokens]
        chunks.append(" ".join(window))
        if start + chunk_tokens >= len(words):
            break
    return chunks


def parse_policy_metadata(text: str, stem: str) -> dict:
    title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    policy_number = re.search(r"\*\*Policy Number:\*\*\s+(\S+)", text)
    payer = re.search(r"\*\*Payer:\*\*\s+(.+)", text)
    return {
        "doc_id": stem,
        "topic": title.group(1).strip() if title else stem,
        "policy_number": policy_number.group(1).strip() if policy_number else "UNKNOWN",
        "payer": payer.group(1).strip() if payer else "UNKNOWN",
    }


def ingest_policies() -> int:
    collection = store.get_collection(store.POLICIES_COLLECTION)
    ids: list[str] = []
    docs: list[str] = []
    metas: list[dict] = []
    for path in sorted(POLICIES_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta = parse_policy_metadata(text, path.stem)
        for i, chunk in enumerate(chunk_text(text)):
            ids.append(f"{path.stem}:{i:02d}")
            docs.append(chunk)
            metas.append({**meta, "chunk_index": i})
    if ids:
        collection.upsert(ids=ids, documents=docs, metadatas=metas)
    return len(ids)


def ingest_resubmit_history() -> int:
    if not HISTORY_JSON.exists():
        return 0
    collection = store.get_collection(store.HISTORY_COLLECTION)
    entries = json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
    ids, docs, metas = [], [], []
    for e in entries:
        rarc = f"/{e['original_rarc']}" if e.get("original_rarc") else ""
        doc = (
            f"{e['payer_name']} denial CARC {e['original_carc']}{rarc}. "
            f"Fix applied: {e['fix_applied']} "
            f"Outcome: {e['outcome']} after {e['days_to_outcome']} days."
        )
        if e.get("notes"):
            doc += f" Notes: {e['notes']}"
        ids.append(e["resubmission_id"])
        docs.append(doc)
        metas.append(
            {
                "payer": e["payer_name"],
                "carc": e["original_carc"],
                "outcome": e["outcome"],
                "fields_changed": ", ".join(e["fields_changed"]) or "none",
                "claim_id": e["claim_id"],
            }
        )
    if ids:
        collection.upsert(ids=ids, documents=docs, metadatas=metas)
    return len(ids)


def ensure_ingested() -> None:
    """Cheap guard for tests/tools: ingest only when a collection is empty."""
    if store.get_collection(store.POLICIES_COLLECTION).count() == 0:
        ingest_policies()
    if store.get_collection(store.HISTORY_COLLECTION).count() == 0:
        ingest_resubmit_history()


def main() -> None:
    n_policy = ingest_policies()
    n_history = ingest_resubmit_history()
    print(f"payer_policies:   upserted {n_policy} chunks "
          f"(collection now {store.get_collection(store.POLICIES_COLLECTION).count()})")
    print(f"resubmit_history: upserted {n_history} entries "
          f"(collection now {store.get_collection(store.HISTORY_COLLECTION).count()})")


if __name__ == "__main__":
    main()
