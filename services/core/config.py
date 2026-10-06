"""Settings for the whole backend.

Phase 0: one settings object, read from the environment / .env. Phase 3 splits
the app into a gateway plus MCP servers; each of those will read the same
variables, so keep this module free of app-specific imports.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database. Target is Postgres on Neon; falls back to a local SQLite file
    # so the phase can be run end-to-end before a Neon project exists.
    database_url: str = ""

    # LLM
    anthropic_api_key: str = ""
    agent_model: str = "claude-sonnet-5-5"
    comms_model: str = "claude-haiku-4-5"

    # Demo / UX
    demo_step_delay_seconds: float = 1.2
    # Streamlit calls the API from its server process, so CORS only matters
    # for direct browser access (e.g. /docs). 8501 is Streamlit's default.
    cors_origins: str = "http://localhost:8501"

    hub_icao: str = "VIDP"

    # Phase 4 placeholders — declared so .env.example and the code agree.
    aviationstack_key: str = ""
    resend_api_key: str = ""
    telegram_bot_token: str = ""
    opensky_client_id: str = ""
    opensky_client_secret: str = ""
    redis_url: str = ""

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        data_dir = REPO_ROOT / "data"
        data_dir.mkdir(exist_ok=True)
        return f"sqlite+pysqlite:///{(data_dir / 'orchestrator.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
