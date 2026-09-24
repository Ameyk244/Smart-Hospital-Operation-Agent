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

    # --- Jev read-only fast path ---
    # Widens the deterministic fast path: when the regex parser returns
    # UNKNOWN, ask TypeSafe AI's Jev ("System One") which known command the
    # message maps to, and route a confident match through the existing
    # CommandRunner instead of a full Sonnet agent turn. The switch lets an
    # operator disable the optimization without changing code.
    enable_jev_fast_path: bool = True
    typesafe_api_key: str | None = None
    jev_model: str = "jev-latest"
    # Project policy: require a high-confidence decision because a match
    # triggers command execution. This is intentionally configurable rather
    # than presented as a universal provider-recommended cutoff.
    jev_confidence_threshold: float = 0.9
    # Deliberately small: this call sits on the hot request path purely as an
    # optimization. If Jev can't answer in a few seconds, falling through to
    # the agent is cheaper than making the user wait.
    jev_timeout_seconds: float = 5.0

    # --- Voice input (experimental, branch `voice` — see docs/voice.md) ---
    # Local faster-whisper, not a hosted API: no key, no per-call cost, and
    # it keeps the project's "offline except live LLM calls" property intact
    # through the STT layer too. "tiny.en" measured ~450ms transcription on
    # CPU for a short command-length utterance against this project's own
    # fixture audio — see docs/voice.md for the full measurement.
    voice_stt_model: str = "tiny.en"
    # Enforced max utterance length (Phase 3): the same bounded-execution
    # instinct as max_agent_rounds/max_tool_calls above, applied to a
    # WebSocket connection whose client might never signal end-of-speech.
    voice_max_utterance_seconds: float = 15.0
    # Energy-based (RMS) VAD (Phase 3) -- deliberately not Silero, see
    # docs/voice.md. RMS is computed on int16 samples normalized to
    # [-1, 1] (divide by 32768.0, the same normalization transcribe_array
    # uses), not on raw int16 magnitudes -- so this threshold means the same
    # thing regardless of how a sample is represented downstream. Measured
    # against this project's own fixture
    # (tests/fixtures/audio/list_delayed_mri_appointments.wav): its silent
    # gaps sit at ~0.0 normalized RMS and its in-speech 20ms windows range
    # ~0.04-0.24. 0.02 sits comfortably above that noise floor and well
    # below typical speech energy, leaving headroom for a quieter speaker or
    # mic gain than this project's own synthesized fixture. There is no
    # universally "correct" threshold -- this is a starting point to tune
    # against real usage, not a measured constant like voice_stt_model was.
    voice_vad_speech_rms_threshold: float = 0.02
    # How long RMS must stay below the threshold, once speech has started,
    # before the utterance is considered ended. 700-1000ms is the typical
    # range for command-style speech: long enough to survive a natural pause
    # between words (this project's own fixture's inter-word gaps are tens
    # of milliseconds, well under this) without making the user wait
    # noticeably after they've actually finished talking.
    voice_vad_silence_ms: float = 800.0
    # Minimum duration RMS must stay above threshold before speech_started is
    # declared at all, so a single loud click/pop doesn't trigger a false
    # utterance. Short relative to voice_vad_silence_ms on purpose -- this
    # guards utterance *start*, not end.
    voice_vad_min_speech_ms: float = 200.0
    # Audio kept from *before* speech was confirmed and prepended to the
    # utterance. Confirmation needs `voice_vad_min_speech_ms` of continuous
    # above-threshold audio, and a soft word onset ("wh" in "what") can sit
    # below the threshold for a chunk or two, so without a lookback the first
    # word was lost ("what preferences have you saved" -> "preferences have
    # you saved", which the domain gate then rejected). 300ms covers a soft
    # onset without dragging in a noticeable amount of room noise.
    voice_vad_preroll_ms: float = 300.0

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
