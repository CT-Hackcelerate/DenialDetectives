"""Knowledge-base tests: ingest is idempotent and retrieval actually works.

Run from backend/:  python -m pytest tests/test_knowledge.py -v
"""
from __future__ import annotations

from app.knowledge import ingest, store


def test_ingest_is_idempotent():
    n_first = ingest.ingest_policies()
    count_after_first = store.get_collection(store.POLICIES_COLLECTION).count()
    n_second = ingest.ingest_policies()
    count_after_second = store.get_collection(store.POLICIES_COLLECTION).count()
    assert n_first == n_second > 0
    assert count_after_first == count_after_second  # upsert, no duplicates


def test_chunking_size_and_overlap():
    words = " ".join(f"w{i}" for i in range(450))
    chunks = ingest.chunk_text(words, chunk_tokens=200, overlap=20)
    assert all(len(c.split()) <= 200 for c in chunks)
    # consecutive chunks share the 20-word overlap
    assert chunks[0].split()[-20:] == chunks[1].split()[:20]


def test_retrieves_cigna_timely_filing_policy():
    """The proof: a Cigna-scoped semantic query returns the Cigna filing policy."""
    ingest.ensure_ingested()
    hits = store.search_policies("how many days do I have to file a claim", k=3, payer="Cigna")
    assert hits, "no results returned for Cigna policy query"
    top = hits[0]
    assert top["metadata"]["payer"] == "Cigna"
    assert top["metadata"]["policy_number"] == "CIG-ADM-081"
    assert top["chroma_doc_id"]  # becomes Citation.chroma_doc_id
    # the 180-day limit is retrievable from the policy's chunks
    cig_chunks = [h for h in hits if h["metadata"]["policy_number"] == "CIG-ADM-081"]
    assert any("180" in h["text"] for h in cig_chunks)


def test_payer_filter_excludes_other_payers():
    ingest.ensure_ingested()
    hits = store.search_policies("prior authorization arthroscopy", k=5, payer="Cigna")
    assert all(h["metadata"]["payer"] == "Cigna" for h in hits)


def test_payer_filter_normalizes_case_and_aliases():
    ingest.ensure_ingested()
    lower = store.search_policies("timely filing", k=3, payer="cigna")
    assert lower and all(h["metadata"]["payer"] == "Cigna" for h in lower)
    alias = store.search_policies("modifier 25 same day E/M", k=3, payer="UHC")
    assert alias and all(h["metadata"]["payer"] == "UnitedHealthcare" for h in alias)
