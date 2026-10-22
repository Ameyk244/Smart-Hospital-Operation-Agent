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

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
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

    # --- Voice input (see docs/voice.md) ---
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
    max_write_calls_per_turn: int = Field(0, ge=0)
    tool_timeout_seconds: int = 15
    llm_timeout_seconds: int = 30
    max_invalid_tool_calls: int = 3

    # --- Scheduled watches and alerts (see docs/SCHEDULER.md) ---
    scheduler_enabled: bool = False
    scheduler_tick_seconds: int = Field(30, gt=0)

    # --- Harness runtime (see docs/harness-smart-hospital-operations.md) ---
    agent_runtime: Literal["in_process", "harness"] = "in_process"
    harness_url: str = Field("http://127.0.0.1:8891", pattern=r"^https?://[^/\s]+$")
    harness_token: str | None = None
    harness_model: str = "anthropic/claude-sonnet-5"
    # A turn that fails with model_error before any tool call is retried once on this.
    harness_fallback_model: str | None = Field(None, pattern=r"^\S+$")
    # Per model round. A provider that checks the balance up front (OpenRouter
    # does) refuses a request whose cap the account cannot cover.
    harness_max_tokens: int = Field(4096, gt=0)
    # Whole-turn budget; replaces llm_timeout_seconds in harness mode.
    # At most an hour: the gateway refuses a longer deadline (Node timer limit).
    harness_turn_timeout_seconds: int = Field(120, gt=0, le=3600)

    @model_validator(mode="after")
    def require_harness_token(self) -> "Settings":
        """The token guards the internal tool-call route, so a harness runtime
        without a strong one must not start. The message omits the value."""
        if self.agent_runtime == "harness" and len(self.harness_token or "") < 32:
            raise ValueError(
                "HARNESS_TOKEN must be set to at least 32 characters when AGENT_RUNTIME=harness"
            )
        return self

    # When on, the agent's reschedule_appointment only records a proposal; the
    # appointment moves when the user confirms it in the app. Off: the tool
    # moves it immediately, as before.
    require_reschedule_confirmation: bool = False

    # Same for the agent's assign_bed / release_bed (docs/EXPANSION.md 7a).
    require_bed_confirmation: bool = False

    # When on, short task guidance (app/agent/skills/) is appended to the
    # system prompt for messages that call for it. Off: the prompt is unchanged.
    agent_skills_enabled: bool = False

    # --- Observability (see observability/README.md) ---
    # Off by default so a fresh clone and the test suite never try to export.
    # Turn on in .env when the local grafana/otel-lgtm stack is running.
    # With it off the OTel API is a no-op; see app/observability/telemetry.py.
    otel_enabled: bool = False
    # OTLP/HTTP base URL of the collector; `/v1/metrics` is appended.
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_metric_export_interval_ms: int = 15_000

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
