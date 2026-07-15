# 🛡️ ClaimGuard

An agent that **identifies, triages, and resubmits denied US healthcare claims**.

**Input:** a simplified X12 835 denial (JSON) with CARC/RARC codes, paired with its 837 claim.
**The agent:** investigates the *true* root cause (the CARC says what the payer claimed, not
what went wrong), commits to one of four routes with cited evidence, and acts on it —
auto-fixing and resubmitting, drafting an appeal letter, or handing off to a human.

## Routes

| Route | When | Badge |
|---|---|---|
| `AUTO_FIX_RESUBMIT` | Correctable + confidence > 0.85 + value < $1000 + validated fix | 🟢 |
| `APPEAL` | Defensible clinical/administrative denial — agent drafts the letter | 🔵 |
| `WRITE_OFF` | Non-recoverable (timely filing w/o proof, contractual, true duplicates) | ⚪ |
| `HUMAN_REVIEW` | Fails a guardrail or is genuinely ambiguous | 🟡 |

## Hard rules (enforced in code, not prose)

1. **The LLM never edits the claim.** It proposes a structured `Fix` via the `propose_fix`
   tool; Python validates every operation against a field whitelist
   (`prior_auth_number`, `subscriber_id`, `lines[i].modifiers`, `lines[i].icd10_pointers`)
   and applies it to a *copy* of the claim (`guardrails.apply_fix` — deterministic,
   revision-bumped, re-validated through Pydantic).
2. **Every assertion cites evidence.** Citations are validated against an `Evidence`
   registry of what tools actually returned *this run* — invented citations are dropped
   in code, and a root cause or decision left uncited is forced to `HUMAN_REVIEW`.
3. **Auto-resubmit only if** confidence > 0.85 **and** value < $1000 **and** a validated
   fix exists — enforced by a `model_validator` on `TriageDecision` plus the orchestrator
   guardrail. Otherwise → `HUMAN_REVIEW` (where a human can *approve* the held fix).
4. **The agent learns.** Every outcome is stored; a failed resubmission writes a one-line
   lesson tagged (payer, CARC) that is injected into future prompts for that payer.

## Stack

- **Backend:** FastAPI + Pydantic + SSE, Anthropic tool calling (7 tools + 3 control
  tools), ChromaDB with all-MiniLM-L6-v2 embeddings
- **Frontend:** React + Vite + Tailwind — dark 3-column UI (worklist / live trace /
  decision card) with an animated $-recovered counter
- **Data:** synthetic only (no PHI) — 44 claim/denial pairs (~$61k denied) across
  Aetna / UnitedHealthcare / Cigna / BCBS, plus a citable reference corpus

## Getting started

```powershell
# 0. From the repo root — the venv lives at ..\.venv (Python 3.12+)
#    pip install -r backend\requirements.txt

# 1. Configure
cd backend
copy .env.example .env          # set ANTHROPIC_API_KEY=sk-ant-api03-...

# 2. Generate data + build the knowledge base (idempotent)
python ..\scripts\generate_synthetic.py
python ..\scripts\validate.py
python -m app.knowledge.ingest

# 3. Run
uvicorn app.main:app --reload   # backend  -> http://localhost:8000
cd ..\frontend
npm install && npm run dev      # frontend -> http://localhost:5173 (proxies /api)
```

## API

| Endpoint | What |
|---|---|
| `GET /api/denials` · `/api/claims/{id}` | Worklist + full documents |
| `GET /api/process/{id}` | **SSE** stream of the agent run (TraceEvents) |
| `POST /api/batch` | Triage every denial; live mode also warms the demo cache |
| `POST /api/approve/{id}` | Human approves — applies + resubmits any held fix |
| `POST /api/override/{id}` | Human overrides the route (`{"route": "...", "reason": "..."}`) |
| `POST /api/feed` | Import a live feed of new claims + denials (validated, atomic) |
| `GET /api/stats` | $ recovered + route counts + per-route denial details |
| `GET /api/report` | Payer-wise analytics (denied $, top CARCs, routes, fix win-rates) |
| `GET /api/lessons` | Learned lessons (seeded with 3; grows on failed resubmits) |

## Importing new claims (sample input feed)

Click **📥 Import** in the UI (or `POST /api/feed`) with a JSON file shaped as
`{"claims": [...], "denials": [...]}` matching the `Claim`/`Denial` models in
`backend/app/models.py`. Try the samples in `docs/`:

| File | Purpose | Expected result |
|---|---|---|
| `docs/sample-feed-valid.json` | Happy path — 2 new fixable denials (DEN-102 Aetna auth, DEN-103 Cigna modifier) | "✓ Import successful — 2 claims, 2 denials"; both appear as *pending* |
| `docs/sample-feed-invalid.json` | Every validator on display | "✗ Import rejected" with 7 per-record errors; **nothing** imported |
| `docs/sample-feed.json` | A third valid batch (DEN-101) | Accepted once; a re-upload is rejected as duplicate |

Validation is **atomic** (one bad record rejects the whole batch) and covers: full
Pydantic schema per record, duplicate `claim_id`/`denial_id`, every denial must
reference a claim (in the feed or the repo), and money math — `total_charge` must equal
the sum of line charges, `total_denied` must equal the sum of adjustments and cannot
exceed the claim's charge. Accepted batches persist to `backend/data/feeds/` and
survive restarts. Note: freshly imported denials have no recorded trace, so in
`DEMO_MODE=replay` they return 409 until run live once.

## Demo mode (no wifi needed)

Live runs record their full trace to `backend/data/demo_cache/{id}.json` on first run
(error/aborted traces are never cached). To demo offline:

1. With a working key, run `POST /api/batch` once (or click **Process all**).
2. Set `DEMO_MODE=replay` in `backend/.env` and restart.
3. Every trace replays at 100ms pacing — typewriter, tool calls, and all.

## The hero case: DEN-007

The remit says **CARC 16 + RARC N54 "missing information"** — a red herring. The truth:
a 99213 E/M bundled into a same-day 29881 knee arthroscopy because **modifier 25** was
missing (the arthroscopy *paid*; only the visit denied). The agent must dig past the
stated reason via `ncci_edit_check`, the UHC-CP-044 policy, and the RSB-005/006
precedents, then auto-fix ($225 < $1000) by appending modifier 25.

## Testing

```powershell
cd backend
python -m pytest tests/ -v          # expect 33 passed, 1 skipped (no key needed)
python tests/tools_test.py          # per-tool pass/fail table: 8/8
python tests/test_orchestrator.py   # live DEN-007 trace (needs a valid key)
```

A scripted fake-model harness drives the **real** loop, tools, ChromaDB, and guardrails,
so the pipeline is verified without API access. The one live test (`test_den_007_live_...`)
skips without a key and asserts the agent rejects the stated reason, finds the bundling
itself, and routes to `AUTO_FIX_RESUBMIT`.

## Limitations (honest edges)

- **Mock clearinghouse.** `submit_claim` simulates front-end edits and 277CA-style acks;
  there is no real payer connection. This is the intended integration seam.
- **Simplified JSON, not real X12.** No 837/835 EDI parsing.
- **Synthetic corpus.** Payer policies, NCCI rows, and the CARC table are realistic but
  authored for this project; policy numbers are fictional. No PHI anywhere.
- **Claim edits are in-memory.** Applied fixes reset on backend restart. Imported feeds
  (`data/feeds/`), agent memory/lessons (`data/memory/`), and recorded traces
  (`data/demo_cache/`) persist; the seed JSON is never rewritten.
- **Single-user demo.** No auth on the API, JSON-file storage, not concurrency-safe.
- **Live runs cost tokens and time** — roughly 30–60 s per denial; a full live batch of
  44 takes ~10–20 minutes. Replay mode exists precisely for demos.
- **Replay covers only recorded traces**; new/imported denials must run live once.
- On ambiguous cases the agent's route can legitimately differ from `ground_truth.json`
  (an evaluation file the agent never reads).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `⚠ Model call failed … 401 invalid x-api-key` | The key is invalid on Anthropic's side. Generate a fresh one at console.anthropic.com (API console with billing — a Claude.ai subscription has no API key). Failed runs record **no** outcomes. |
| `Anthropic client unavailable` | No key at all: create `backend/.env` from `.env.example` and restart uvicorn (settings load at startup). |
| First `ingest` hangs/fails | It downloads the ~90 MB MiniLM model from Hugging Face — needs internet once. If sentence-transformers/torch won't install, the code automatically falls back to ChromaDB's ONNX build of the same model. |
| `python` opens the Microsoft Store (Windows) | PATH hits the Store stub — call the venv interpreter explicitly: `.\.venv\Scripts\python.exe`. |
| UI loads but the list is empty | Backend not running or wrong port — check `http://localhost:8000/api/health`; the Vite proxy targets :8000. |
| Port 8000/5173 already in use | Another instance is running — kill it or change ports (`uvicorn --port`, `vite --port`; update the proxy in `vite.config.ts` if the backend port changes). |
| `409` when processing a claim | `DEMO_MODE=replay` with no recorded trace for that claim — run it live once or set `DEMO_MODE=live`. |
| Import rejected as duplicate | That batch (or those ids) were already ingested — by design; change the `claim_id`/`denial_id` values to re-test. |
| Every claim routed to HUMAN after a bad-key session (older builds) | Stale outcomes — delete `backend/data/memory/` (lessons re-seed automatically) and refresh the page. |
| Live test fails: `run aborted — check the API key` | A key exists but is invalid — fix it, or run `pytest -k "not live"`. |

## Layout

```
backend/app/models.py       # all Pydantic models + enums (Claim, Denial, Fix, TraceEvent…)
backend/app/agent/          # orchestrator (ReAct loop, 12-turn cap), tools, prompts, guardrails
backend/app/knowledge/      # ChromaDB store + ingest; sources/ = CARC/NCCI CSVs + 8 payer policies
backend/app/services/       # claim_repo, memory (outcomes/lessons), appeals, demo_cache
backend/app/api/            # routes (SSE process, batch, approve, override, stats, lessons)
backend/data/synthetic/     # generated claims/denials/ground_truth/resubmit_history (gitignored)
backend/data/feeds/         # imported live-feed batches (persist across restarts)
backend/data/demo_cache/    # recorded traces for offline replay
frontend/src/               # 3-column React UI (worklist w/ filters+import, live trace, decision card)
scripts/                    # generate_synthetic.py + validate.py
docs/                       # sample feeds (valid + invalid) · hackathon deck (.pptx)
```
