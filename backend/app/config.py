"""Central configuration.

Why it exists: every other module (DB engine, LLM provider, agent bounds,
logging) needs configuration values. Reading env vars ad hoc from twenty
different files makes it impossible to answer "what can I configure and
what's the default?" in one place. This module is that one place.

What calls it: `app.db.session`, `app.agent.providers.*`, `app.agent.graph`,
`app.observability.logging_config`, `app.main` — anything that needs a
setting imports `get_settings()`.

Fails: raises a pydantic `ValidationError` at process startup if a required
value is missing/malformed — deliberately fails fast rather than at first use
three requests later.
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Database ---
    database_url: str = (
        "postgresql+asyncpg://hospital_admin:hospital_dev_password@localhost:5432/hospital_ops"
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def use_async_postgres_driver(cls, value: object) -> object:
        """Render supplies a plain PostgreSQL URL; SQLAlchemy needs its async driver."""
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    # --- LLM provider ---
    llm_provider: Literal["anthropic", "openrouter"] = "anthropic"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    openrouter_api_key: str | None = None
    openrouter_model: str = "anthropic/claude-sonnet-5"

    # --- Bounded agent execution (see docs/ARCHITECTURE.md §5) ---
    max_agent_rounds: int = 6
    max_tool_calls: int = 10
    tool_timeout_seconds: int = 15
    llm_timeout_seconds: int = 30
    max_invalid_tool_calls: int = 3

    # --- App ---
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    run_live_llm_tests: bool = False
    cors_origins: str = "http://localhost:5173"

    @property
    def allowed_cors_origins(self) -> list[str]:
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    """Process-wide singleton. Cached so `.env` is parsed once, not per call."""
    return Settings()
