"""Anthropic tool schemas + Python executors for the denial-triage agent.

Seven tools. Each executor returns a plain dict (never raises — errors come
back as {"error": ...}) and includes citation dicts shaped like
app.models.Citation wherever a claim of fact is made:

  1. carc_lookup           CARC/RARC meaning + typical root cause and fix
  2. ncci_edit_check       PTP bundling edits for a set of CPT codes
  3. policy_retrieve       semantic search over payer policy chunks
  4. prior_auth_status     is precert required? (decided from retrieved payer
                           policy text, not a hardcoded list)
  5. timely_filing_check   days elapsed vs the payer's filing limit
  6. resubmission_history  vector search: has this fix worked for this payer?
  7. submit_claim          mock clearinghouse; accepted/rejected + trace id

Wire-up: pass TOOLS to messages.create(tools=...), then feed each tool_use
block to execute_tool(name, input) and return the dict as the tool_result.
"""
from __future__ import annotations

import csv
import re
import uuid
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from app.knowledge import store
from app.knowledge.ingest import ensure_ingested

SOURCES_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "sources"

DEFAULT_FILING_LIMIT_DAYS = 180

# Remark-code glossary for the RARCs used in the synthetic corpus.
RARC_GLOSSARY: dict[str, str] = {
    "M62": "Missing/incomplete/invalid treatment authorization code.",
    "M76": "Missing/incomplete/invalid diagnosis or condition.",
    "M80": "Not covered when performed during the same session/date as a previously processed service.",
    "MA04": "Secondary payment cannot be considered without the identity of the primary payer.",
    "N20": "Service not payable with other service rendered on the same date.",
    "N30": "Patient ineligible for this service.",
    "N54": "Claim information is inconsistent with the billed services or provider.",
    "N115": "This decision was based on a Local Coverage Determination (LCD) or payer policy.",
    "N130": "Consult plan benefit documents/guidelines for information about restrictions.",
    "N382": "Missing/incomplete/invalid patient identifier.",
    "N522": "Duplicate of a claim processed, or to be processed, as a crossover claim.",
    "N822": "Missing procedure modifier(s).",
}


# --------------------------------------------------------------------------- #
# Reference data (lazy-loaded CSVs)
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def _carc_table() -> dict[str, dict[str, str]]:
    with (SOURCES_DIR / "carc_codes.csv").open(encoding="utf-8", newline="") as f:
        return {row["code"]: row for row in csv.DictReader(f)}


@lru_cache(maxsize=1)
def _ncci_table() -> list[dict[str, str]]:
    with (SOURCES_DIR / "ncci_edits.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _citation(source_type: str, source_id: str, quote: str, chroma_doc_id: str | None = None) -> dict:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "quote": quote[:300],
        "chroma_doc_id": chroma_doc_id,
    }


# --------------------------------------------------------------------------- #
# 1. carc_lookup
# --------------------------------------------------------------------------- #


def carc_lookup(carc: str, rarc: str | None = None) -> dict:
    row = _carc_table().get(str(carc).strip())
    if row is None:
        return {"found": False, "carc": carc, "error": f"CARC {carc} not in reference table."}
    result = {
        "found": True,
        "carc": row["code"],
        "description": row["description"],
        "category": row["category"],
        "typical_root_cause": row["typical_root_cause"],
        "typical_fix": row["typical_fix"],
        "citations": [_citation("carc_definition", f"CARC-{row['code']}", row["description"])],
    }
    if rarc:
        meaning = RARC_GLOSSARY.get(rarc.strip().upper())
        result["rarc"] = rarc
        result["rarc_meaning"] = meaning or "Unknown remark code."
        if meaning:
            result["citations"].append(_citation("rarc_definition", f"RARC-{rarc}", meaning))
    return result


# --------------------------------------------------------------------------- #
# 2. ncci_edit_check
# --------------------------------------------------------------------------- #


def ncci_edit_check(cpt_codes: list[str]) -> dict:
    codes = [str(c).strip() for c in cpt_codes if str(c).strip()]
    if len(codes) < 2:
        return {"edits": [], "note": "Need at least two CPT codes to check PTP edits."}
    edits = []
    for row in _ncci_table():
        if row["column1_cpt"] in codes and row["column2_cpt"] in codes:
            edits.append(
                {
                    "column1_cpt": row["column1_cpt"],
                    "column2_cpt": row["column2_cpt"],
                    "modifier_indicator": row["modifier_indicator"],
                    "bypass_allowed": row["modifier_indicator"] == "1",
                    "rationale": row["rationale"],
                    "citation": _citation(
                        "ncci_edit",
                        f"NCCI-{row['column1_cpt']}/{row['column2_cpt']}",
                        row["rationale"],
                    ),
                }
            )
    return {"cpt_codes": codes, "edits": edits, "edit_count": len(edits)}


# --------------------------------------------------------------------------- #
# 3. policy_retrieve
# --------------------------------------------------------------------------- #


def policy_retrieve(query: str, payer: str | None = None, k: int = 4) -> dict:
    ensure_ingested()
    hits = store.search_policies(query, k=k, payer=payer)
    matches = [
        {
            "policy_number": h["metadata"].get("policy_number"),
            "payer": h["metadata"].get("payer"),
            "topic": h["metadata"].get("topic"),
            "text": h["text"],
            "distance": round(h["distance"], 4),
            "citation": _citation(
                "payer_policy",
                h["metadata"].get("policy_number", "UNKNOWN"),
                h["text"],
                chroma_doc_id=h["chroma_doc_id"],
            ),
        }
        for h in hits
    ]
    return {"query": query, "payer_filter": payer, "matches": matches}


# --------------------------------------------------------------------------- #
# 4. prior_auth_status
# --------------------------------------------------------------------------- #


def prior_auth_status(payer: str, cpt: str, auth_number: str | None = None) -> dict:
    """Whether `payer` requires precert for `cpt` — decided by reading the
    payer's retrieved policy text, not a hardcoded list."""
    ensure_ingested()
    cpt = str(cpt).strip()
    hits = store.search_policies(
        f"precertification prior authorization requirement for CPT {cpt}", k=4, payer=payer
    )
    evidence = None
    for h in hits:
        text = h["text"]
        if cpt in text and re.search(r"precert|prior auth", text, re.IGNORECASE):
            evidence = h
            break
    required = evidence is not None
    if not required:
        status = "not_required"
    elif auth_number:
        status = "auth_on_file"
    else:
        status = "auth_missing"
    result = {
        "payer": payer,
        "cpt": cpt,
        "auth_required": required,
        "auth_number": auth_number,
        "status": status,
        "citations": [],
    }
    if evidence:
        result["citations"].append(
            _citation(
                "payer_policy",
                evidence["metadata"].get("policy_number", "UNKNOWN"),
                evidence["text"],
                chroma_doc_id=evidence["chroma_doc_id"],
            )
        )
    else:
        result["note"] = f"No {payer} policy chunk mentions precert for {cpt}; treating as not required."
    return result


# --------------------------------------------------------------------------- #
# 5. timely_filing_check
# --------------------------------------------------------------------------- #


def timely_filing_check(payer: str, date_of_service: str, submission_date: str) -> dict:
    dos = date.fromisoformat(str(date_of_service))
    submitted = date.fromisoformat(str(submission_date))
    days_elapsed = (submitted - dos).days
    limit = DEFAULT_FILING_LIMIT_DAYS
    citations = []
    ensure_ingested()
    hits = store.search_policies("timely filing deadline calendar days", k=2, payer=payer)
    for h in hits:
        m = re.search(r"(\d{2,3})\s+calendar days", h["text"])
        if m:
            limit = int(m.group(1))
            citations.append(
                _citation(
                    "payer_policy",
                    h["metadata"].get("policy_number", "UNKNOWN"),
                    h["text"],
                    chroma_doc_id=h["chroma_doc_id"],
                )
            )
            break
    return {
        "payer": payer,
        "date_of_service": str(dos),
        "submission_date": str(submitted),
        "days_elapsed": days_elapsed,
        "filing_limit_days": limit,
        "within_limit": days_elapsed <= limit,
        "limit_source": "payer_policy" if citations else f"default ({DEFAULT_FILING_LIMIT_DAYS} days)",
        "citations": citations,
    }


# --------------------------------------------------------------------------- #
# 6. resubmission_history
# --------------------------------------------------------------------------- #


def resubmission_history(payer: str, carc: str, fix_type: str) -> dict:
    """Vector search over past resubmissions: has this fix worked for this payer?"""
    ensure_ingested()
    hits = store.search_history(f"CARC {carc} fixed by {fix_type}", k=5, payer=payer)
    matches = [
        {
            "resubmission_id": h["chroma_doc_id"],
            "summary": h["text"],
            "outcome": h["metadata"].get("outcome"),
            "carc": h["metadata"].get("carc"),
            "fields_changed": h["metadata"].get("fields_changed"),
            "distance": round(h["distance"], 4),
        }
        for h in hits
    ]
    paid = sum(1 for m in matches if m["outcome"] == "paid")
    return {
        "payer": payer,
        "carc": carc,
        "fix_type": fix_type,
        "matches": matches,
        "precedent_count": len(matches),
        "paid_count": paid,
        "has_successful_precedent": paid > 0,
    }


# --------------------------------------------------------------------------- #
# 7. submit_claim (mock clearinghouse)
# --------------------------------------------------------------------------- #


def submit_claim(claim: dict) -> dict:
    trace_id = f"TRC-{uuid.uuid4().hex[:10].upper()}"
    errors = []
    for field in ("claim_id", "payer_id", "subscriber_id", "lines"):
        if not claim.get(field):
            errors.append(f"missing required field: {field}")
    subscriber = str(claim.get("subscriber_id", ""))
    if subscriber and not re.fullmatch(r"SUB\d{9}", subscriber):
        errors.append(f"subscriber_id '{subscriber}' is not a valid member ID (expected SUB#########)")
    for i, ln in enumerate(claim.get("lines") or []):
        if not ln.get("icd10_pointers"):
            errors.append(f"line {ln.get('line_number', i + 1)} has no diagnosis pointer")
    if errors:
        return {"status": "rejected", "trace_id": trace_id, "payer_ack_code": "R3", "errors": errors}
    return {
        "status": "accepted",
        "trace_id": trace_id,
        "payer_ack_code": "A2",
        "message": f"Claim {claim['claim_id']} accepted for adjudication.",
        "errors": [],
    }


# --------------------------------------------------------------------------- #
# Anthropic tool definitions + dispatch
# --------------------------------------------------------------------------- #

TOOLS: list[dict[str, Any]] = [
    {
        "name": "carc_lookup",
        "description": (
            "Look up a claim adjustment reason code (CARC) and optional remark code "
            "(RARC): plain-language meaning, typical underlying root cause, and the fix "
            "that usually resolves it. Use this first on every denial."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "carc": {"type": "string", "description": "CARC, e.g. '16' or '197'."},
                "rarc": {"type": "string", "description": "Optional RARC, e.g. 'N54'."},
            },
            "required": ["carc"],
        },
    },
    {
        "name": "ncci_edit_check",
        "description": (
            "Check a set of CPT codes billed on the same day against NCCI "
            "procedure-to-procedure bundling edits. Returns each edit with its "
            "modifier_indicator (0 = never bypassable, 1 = bypassable with a modifier "
            "such as 25/59 when documented). Use when a claim has multiple lines or a "
            "denial smells like bundling even if the CARC says otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cpt_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All CPT/HCPCS codes on the claim for that date of service.",
                }
            },
            "required": ["cpt_codes"],
        },
    },
    {
        "name": "policy_retrieve",
        "description": (
            "Semantic search over payer policy documents. Returns matching policy "
            "chunks with citable policy numbers. Optionally restrict to one payer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to know."},
                "payer": {
                    "type": "string",
                    "description": "Optional exact payer name: Aetna, UnitedHealthcare, Cigna, Blue Cross Blue Shield.",
                },
                "k": {"type": "integer", "description": "Max results (default 4)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "prior_auth_status",
        "description": (
            "Determine whether a payer requires prior authorization for a CPT code by "
            "reading the payer's own policy, and report whether an auth number is on "
            "the claim. Use on CARC 197/198 denials."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payer": {"type": "string", "description": "Exact payer name."},
                "cpt": {"type": "string", "description": "Procedure code, e.g. '29881'."},
                "auth_number": {
                    "type": "string",
                    "description": "Auth number on the claim or found in payer_context, if any.",
                },
            },
            "required": ["payer", "cpt"],
        },
    },
    {
        "name": "timely_filing_check",
        "description": (
            "Compute days between date of service and submission and compare against "
            "the payer's filing limit (read from payer policy when available, else 180 "
            "days). Use on CARC 29 denials."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payer": {"type": "string", "description": "Exact payer name."},
                "date_of_service": {"type": "string", "description": "ISO date, e.g. '2025-08-04'."},
                "submission_date": {"type": "string", "description": "ISO date the claim was submitted."},
            },
            "required": ["payer", "date_of_service", "submission_date"],
        },
    },
    {
        "name": "resubmission_history",
        "description": (
            "Vector search past resubmissions: has this kind of fix worked for this "
            "payer and CARC before? Returns precedents with outcomes (paid / "
            "denied_again). Use before proposing an auto-fix to check the fix has a "
            "successful precedent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payer": {"type": "string", "description": "Exact payer name."},
                "carc": {"type": "string", "description": "The denial's CARC."},
                "fix_type": {
                    "type": "string",
                    "description": "Short description of the intended fix, e.g. 'append modifier 25'.",
                },
            },
            "required": ["payer", "carc", "fix_type"],
        },
    },
    {
        "name": "submit_claim",
        "description": (
            "Submit a corrected claim to the (mock) clearinghouse. Returns "
            "accepted/rejected with a trace id and front-end edit errors. Only call "
            "after a Fix has been validated and applied by the guardrail layer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "claim": {
                    "type": "object",
                    "description": "The full corrected claim as a JSON object (Claim model shape).",
                }
            },
            "required": ["claim"],
        },
    },
]

_EXECUTORS: dict[str, Callable[..., dict]] = {
    "carc_lookup": carc_lookup,
    "ncci_edit_check": ncci_edit_check,
    "policy_retrieve": policy_retrieve,
    "prior_auth_status": prior_auth_status,
    "timely_filing_check": timely_filing_check,
    "resubmission_history": resubmission_history,
    "submit_claim": submit_claim,
}


def execute_tool(name: str, tool_input: dict[str, Any]) -> dict:
    """Dispatch an Anthropic tool_use block to its executor. Never raises."""
    executor = _EXECUTORS.get(name)
    if executor is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return executor(**(tool_input or {}))
    except TypeError as exc:
        return {"error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 — tool results must never raise
        return {"error": f"{type(exc).__name__}: {exc}"}


__all__ = ["TOOLS", "execute_tool"] + list(_EXECUTORS)
