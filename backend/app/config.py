"""Settings: model id, ChromaDB path, API key — loaded from backend/.env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""  # falls back to the ANTHROPIC_API_KEY env var
    claimguard_model: str = "claude-opus-4-8"
    chroma_path: str | None = None
    max_agent_turns: int = 12

    # Demo cache: every live trace is saved on first run. DEMO_MODE=replay
    # serves the cached traces (paced) instead of calling the model — works
    # with no wifi.
    demo_mode: str = "live"  # "live" | "replay"
    demo_replay_delay: float = 0.1  # seconds between replayed events


settings = Settings()
