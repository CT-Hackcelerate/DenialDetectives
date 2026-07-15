"""ChromaDB client + retrieval helpers for the knowledge base.

Two persistent collections:
  * payer_policies   — chunked payer policy markdown (see ingest.py)
  * resubmit_history — past resubmission outcomes for similarity lookup

Embeddings are all-MiniLM-L6-v2. The sentence-transformers implementation is
preferred; when that package is unavailable (e.g. no torch wheel for this
Python), ChromaDB's built-in ONNX port of the *same* model is used, so vectors
stay 384-dim MiniLM either way.

search_policies() returns doc ids that become Citation.chroma_doc_id.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import chromadb

BACKEND_DIR = Path(__file__).resolve().parents[2]  # backend/

POLICIES_COLLECTION = "payer_policies"
HISTORY_COLLECTION = "resubmit_history"

_client: chromadb.api.ClientAPI | None = None
_embedding_fn = None

# Metadata stores canonical payer names; normalize whatever the caller (or the
# LLM) sends — case, spacing, and common aliases — before filtering.
_PAYER_ALIASES = {
    "aetna": "Aetna",
    "unitedhealthcare": "UnitedHealthcare",
    "united healthcare": "UnitedHealthcare",
    "uhc": "UnitedHealthcare",
    "cigna": "Cigna",
    "blue cross blue shield": "Blue Cross Blue Shield",
    "bluecross blueshield": "Blue Cross Blue Shield",
    "bcbs": "Blue Cross Blue Shield",
}


def normalize_payer(payer: str | None) -> str | None:
    if payer is None:
        return None
    return _PAYER_ALIASES.get(" ".join(payer.split()).lower(), payer)


def get_client() -> chromadb.api.ClientAPI:
    """Singleton persistent client. CHROMA_PATH env var overrides the default."""
    global _client
    if _client is None:
        path = os.environ.get("CHROMA_PATH") or str(BACKEND_DIR / ".chroma")
        _client = chromadb.PersistentClient(path=path)
    return _client


def get_embedding_function():
    """all-MiniLM-L6-v2, via sentence-transformers when installed, else ONNX."""
    global _embedding_fn
    if _embedding_fn is None:
        from chromadb.utils import embedding_functions

        try:
            _embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
        except Exception:
            _embedding_fn = embedding_functions.ONNXMiniLM_L6_V2()
    return _embedding_fn


def get_collection(name: str = POLICIES_COLLECTION):
    return get_client().get_or_create_collection(
        name=name,
        embedding_function=get_embedding_function(),
        metadata={"hnsw:space": "cosine"},
    )


def _format_hits(result: dict) -> list[dict[str, Any]]:
    hits = []
    for doc_id, text, meta, dist in zip(
        result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        hits.append({"chroma_doc_id": doc_id, "text": text, "metadata": meta, "distance": dist})
    return hits


def search_policies(query: str, k: int = 4, payer: str | None = None) -> list[dict[str, Any]]:
    """Semantic search over policy chunks, optionally restricted to one payer."""
    payer = normalize_payer(payer)
    collection = get_collection(POLICIES_COLLECTION)
    result = collection.query(
        query_texts=[query],
        n_results=min(k, max(collection.count(), 1)),
        where={"payer": payer} if payer else None,
    )
    return _format_hits(result)


def search_history(query: str, k: int = 4, payer: str | None = None) -> list[dict[str, Any]]:
    """Semantic search over past resubmissions, optionally restricted to one payer."""
    payer = normalize_payer(payer)
    collection = get_collection(HISTORY_COLLECTION)
    result = collection.query(
        query_texts=[query],
        n_results=min(k, max(collection.count(), 1)),
        where={"payer": payer} if payer else None,
    )
    return _format_hits(result)
