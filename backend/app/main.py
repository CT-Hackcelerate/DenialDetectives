"""FastAPI application entrypoint.

Run from backend/:  uvicorn app.main:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_claims import router as claims_router
from app.api.routes_triage import router as triage_router

app = FastAPI(title="ClaimGuard", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(claims_router)
app.include_router(triage_router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
