"""Reproducible, free, local traffic for the Grafana dashboard (observability
Phase 5).

Runs the real FastAPI app in-process (Starlette `TestClient`, entered as a
context manager so the real lifespan runs and builds the real LangGraph
checkpointer), with OpenTelemetry export on and pointed at the local
`grafana/otel-lgtm` stack. Every request goes through the real middleware,
routing, `CommandRunner`, agent graph, tools, STT and DB, so every metric,
span and log line the dashboard shows is produced by real code paths. Only
the two paid dependencies are replaced:

- the chat model: `app.api.routes.chat.get_default_chat_model` is patched to
  hand out scripted models (`FakeMessagesListChatModel`, the same pattern as
  `backend/tests/integration/test_agent_loop.py`). If a turn asks for a
  model the script did not queue, it raises instead of falling back;
- the Jev SDK: a fake `typesafe_sdk` module (the pattern from
  `backend/tests/conftest.py`) is installed in `sys.modules`, and the Jev
  flag is turned on only for the few turns that exercise it, with a dummy
  key.

On top of that, the provider and Jev API keys are blanked in the
environment before the app is imported, so even an accidental real client
would have no credentials.

Database: the dev database from `backend/.env` (the same one the app uses).
The traffic only reads hospital data. It never calls
`reschedule_appointment`, the only mutating tool. It does write the usual
per-session rows (sessions, messages, agent events, checkpoints, grounding),
all under session ids starting with `obs-traffic-`.

Run (from the repo root, with the stack up):

    backend/.venv/Scripts/python.exe observability/scripts/generate_traffic.py
    backend/.venv/Scripts/python.exe observability/scripts/generate_traffic.py --iterations 2 --pause 10

`--endpoint` overrides the OTLP/HTTP endpoint (default http://localhost:4318).
At the end it prints the session ids and request ids it used, so a trace can
be looked up in Tempo, and it shuts telemetry down (flushing all three
signals) before exiting.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import types
import uuid
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
AUDIO_FIXTURE = BACKEND / "tests" / "fixtures" / "audio" / "list_delayed_mri_appointments.wav"

# A real seeded patient: used to prove names never reach Tempo or Loki.
PATIENT_NAME = "Anthony Martin"
AGENT_PATIENT_NAME = "David Davis"


def _configure_environment(endpoint: str) -> None:
    """Must run before `app.main` is imported: telemetry is set up at import
    time, and `get_settings()` is cached on first use. Environment variables
    override `backend/.env`."""
    os.environ["OTEL_ENABLED"] = "true"
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    os.environ["OTEL_METRIC_EXPORT_INTERVAL_MS"] = "5000"
    # No paid calls, by construction: Jev off (switched on per turn with a
    # fake SDK and a dummy key), and no real credentials in this process.
    os.environ["ENABLE_JEV_FAST_PATH"] = "false"
    os.environ["TYPESAFE_API_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["OPENROUTER_API_KEY"] = ""
    os.environ.setdefault("APP_ENV", "development")
    # faster-whisper otherwise asks the Hugging Face Hub for the model's
    # latest revision on every load; the model is already cached locally.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    # pydantic-settings reads `.env` relative to the working directory.
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))


# --------------------------------------------------------------------------
# Fake Jev SDK (same shape as backend/tests/conftest.py's `fake_typesafe`).
# --------------------------------------------------------------------------


class _JevAnswer:
    def __init__(self, choice: str, confidence: float) -> None:
        self.choice = choice
        self.confidence = confidence
        self.probabilities = {choice: confidence}


class _JevUsage:
    input_tokens = 350
    output_tokens = 5


class _JevResponse:
    def __init__(self, command: str, confidence: float) -> None:
        self.answers = {
            "command": _JevAnswer(command, confidence),
            "scanner_type": _JevAnswer("unspecified", 0.99),
            "scanner_status": _JevAnswer("unspecified", 0.99),
            "appointment_type": _JevAnswer("unspecified", 0.99),
        }
        self.usage = _JevUsage()
        self.model = "jev-latest"


class FakeTypeSafe:
    """Installs a fake `typesafe_sdk` module. `behavior` is the next
    response (or exception) `system_one` returns. Counts calls."""

    def __init__(self) -> None:
        module = types.ModuleType("typesafe_sdk")
        self.behavior: Any = None
        self.calls = 0
        handle = self

        class TypeSafeError(Exception):
            pass

        class TypeSafeAPIError(TypeSafeError):
            pass

        class TypeSafeRateLimitError(TypeSafeAPIError):
            retry_after_ms = 1000

        class TypeSafeAPITimeoutError(TypeSafeAPIError):
            pass

        class Choice:
            def __init__(self, *, instructions: str, criteria: dict) -> None:
                self.instructions = instructions
                self.criteria = criteria

        class TypeSafeClient:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def system_one(self, *, state: Any, questions: Any, **kwargs: Any):
                handle.calls += 1
                time.sleep(0.05)  # a plausible, non-zero latency
                if isinstance(handle.behavior, BaseException):
                    raise handle.behavior
                return handle.behavior

        module.Choice = Choice
        module.TypeSafeClient = TypeSafeClient
        module.TypeSafeError = TypeSafeError
        module.TypeSafeAPIError = TypeSafeAPIError
        module.TypeSafeRateLimitError = TypeSafeRateLimitError
        module.TypeSafeAPITimeoutError = TypeSafeAPITimeoutError
        self.module = module
        self.api_error = TypeSafeAPIError
        sys.modules["typesafe_sdk"] = module


# --------------------------------------------------------------------------
# Scripted chat models.
# --------------------------------------------------------------------------


def _models():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class ScriptedChatModel(FakeMessagesListChatModel):
        """Cycles through scripted responses; ignores bound tools."""

        def bind_tools(self, tools, **kwargs):
            return self

    class SlowChatModel(ScriptedChatModel):
        """Sleeps (without blocking the loop) past the LLM timeout."""

        delay: float = 3.0

        async def _agenerate(self, *args, **kwargs):
            await asyncio.sleep(self.delay)
            return await super()._agenerate(*args, **kwargs)

    class FailingChatModel(ScriptedChatModel):
        """A provider error: the exception propagates out of the turn."""

        async def _agenerate(self, *args, **kwargs):
            raise RuntimeError("scripted provider failure")

    return ScriptedChatModel, SlowChatModel, FailingChatModel, AIMessage


def _call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


class ModelQueue:
    """Stands in for `get_default_chat_model`. Each agent turn takes the
    next queued model; a turn with nothing queued raises, so a routing
    mistake can never reach a real provider."""

    def __init__(self) -> None:
        self._queue: list[Any] = []

    def push(self, model: Any) -> None:
        self._queue.append(model)

    def __call__(self):
        if not self._queue:
            raise RuntimeError("generate_traffic: agent turn with no scripted model queued")
        return self._queue.pop(0)

    def assert_empty(self) -> None:
        if self._queue:
            raise RuntimeError(f"{len(self._queue)} scripted model(s) were never used")


# --------------------------------------------------------------------------
# Audio helpers (same idiom as tests/integration/test_voice_websocket.py).
# --------------------------------------------------------------------------

SAMPLE_RATE_HZ = 16_000


def _fixture_pcm() -> bytes:
    with wave.open(str(AUDIO_FIXTURE), "rb") as wav_file:
        assert wav_file.getframerate() == SAMPLE_RATE_HZ
        assert wav_file.getsampwidth() == 2 and wav_file.getnchannels() == 1
        return wav_file.readframes(wav_file.getnframes())


def _chunks(data: bytes, n: int) -> list[bytes]:
    samples = len(data) // 2
    step = (samples // n) * 2
    return [data[i * step : (i + 1) * step] if i < n - 1 else data[i * step :] for i in range(n)]


def _silence(ms: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE_HZ * ms / 1000)


def _drain_until_ready(ws, max_messages: int = 12) -> list[dict]:
    messages = []
    for _ in range(max_messages):
        message = ws.receive_json()
        messages.append(message)
        if message.get("type") == "ready":
            break
    return messages


# --------------------------------------------------------------------------
# The traffic.
# --------------------------------------------------------------------------


def _slug(label: str) -> str:
    """'agent:grounding-reject' -> 'grounding-' (session ids max 36 chars)."""
    return "".join(ch for ch in label.split(":")[-1] if ch.isalnum() or ch == "-")[:10]


class Traffic:
    def __init__(self, client, models: ModelQueue, jev: FakeTypeSafe, run_id: str) -> None:
        self.client = client
        self.models = models
        self.jev = jev
        self.run_id = run_id
        self.results: list[tuple[str, str, int, str]] = []  # (label, session, status, request_id)
        self._counter = 0
        (self.Scripted, self.Slow, self.Failing, self.AIMessage) = _models()

    def session(self, label: str) -> str:
        self._counter += 1
        sid = f"obs-traffic-{self.run_id}-{self._counter:02d}{label}"
        return sid[:36]

    def chat(self, label: str, text: str, session_id: str | None = None, **patches: Any) -> dict:
        from unittest.mock import patch

        sid = session_id or self.session(_slug(label))
        ctx = []
        if "settings" in patches:
            ctx.append(patch("app.api.routes.chat.get_settings", return_value=patches["settings"]))
        for c in ctx:
            c.start()
        try:
            response = self.client.post("/api/chat", json={"text": text, "session_id": sid})
        finally:
            for c in ctx:
                c.stop()
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        handled = body.get("handled_by", "?") if isinstance(body, dict) else "?"
        self.results.append((label, sid, response.status_code, response.headers.get("x-request-id", "")))
        detail = ""
        if isinstance(body, dict):
            if handled == "rejected":
                detail = f" ({body.get('message', '')[:70]})"
            elif body.get("terminated_reason"):
                detail = f" terminated={body['terminated_reason']}"
        print(f"  {label:<24} {response.status_code} handled_by={handled:<13} session={sid}{detail}")
        return body

    def get(self, label: str, path: str, **params: Any) -> None:
        response = self.client.get(path, params=params or None)
        self.results.append((label, "", response.status_code, response.headers.get("x-request-id", "")))
        print(f"  {label:<24} {response.status_code} GET {path}")

    # -- deterministic and gated routes -----------------------------------

    def parser_and_gates(self) -> None:
        self.chat("parser:list-departments", "list departments")
        self.chat("parser:list-scanners", "list scanners mri")
        self.chat("parser:delayed-appts", "list delayed appointments")
        self.chat("parser:next-appt", "show next appointment")
        self.chat("parser:show-patient", f"show patient {PATIENT_NAME}")
        self.chat("domain_rejected", "what's the weather like today?")
        self.chat("ineligible:too-long", "show the delayed mri appointments " * 70)

    def operations(self) -> None:
        self.get("ops:departments", "/api/operations/departments")
        self.get("ops:scanners", "/api/operations/scanners")
        self.get("ops:appointments", "/api/operations/appointments")
        self.get("ops:patient-search", "/api/operations/patients", query=PATIENT_NAME)
        self.get("ops:404", "/api/operations/does-not-exist")
        self.get("cost-comparison", "/api/cost-comparison")

    # -- Jev (fake SDK) ----------------------------------------------------

    def jev_turns(self, base_settings) -> None:
        jev_settings = base_settings.model_copy(
            update={"enable_jev_fast_path": True, "typesafe_api_key": "fake-key-not-real"}
        )
        text = "could you pull up every department we have"
        # matched: answered by CommandRunner, no agent turn.
        self.jev.behavior = _JevResponse("list_departments", 0.97)
        self.chat("jev:matched", text, settings=jev_settings)
        # declined: low confidence, falls through to a scripted agent turn.
        self.jev.behavior = _JevResponse("list_departments", 0.40)
        self.models.push(self.Scripted(responses=[
            self.AIMessage(content="Here are the departments.", usage_metadata=_usage(900, 30)),
        ]))
        self.chat("jev:declined->agent", text, settings=jev_settings)
        # failed: the SDK raises, falls through to a scripted agent turn.
        self.jev.behavior = self.jev.api_error("scripted outage")
        self.models.push(self.Scripted(responses=[
            self.AIMessage(content="Here are the departments.", usage_metadata=_usage(900, 30)),
        ]))
        self.chat("jev:failed->agent", text, settings=jev_settings)

    # -- scripted agent turns ----------------------------------------------

    def agent_turns(self, base_settings) -> None:
        S, A = self.Scripted, self.AIMessage

        # 1. Multi-round search: appointments, then scanners, then one
        #    scanner's availability (grounded by round 2), then an answer.
        self.models.push(S(responses=[
            A(content="", tool_calls=[_call("search_appointments", {"appointment_type": "MRI", "status": "DELAYED"}, "a1")], usage_metadata=_usage(1200, 60)),
            A(content="", tool_calls=[_call("search_scanners", {"type": "MRI"}, "a2")], usage_metadata=_usage(1600, 40)),
            A(content="", tool_calls=[_call("get_scanner_availability", {"scanner_code": "SCN-1"}, "a3")], usage_metadata=_usage(1900, 40)),
            A(content="SCN-1 is the MRI scanner to look at.", usage_metadata=_usage(2100, 50)),
        ]))
        self.chat("agent:multi-round", "which MRI scanner could take the delayed MRI patients")

        # 2. Grounding rejection: a scanner code this session was never shown.
        self.models.push(S(responses=[
            A(content="", tool_calls=[_call("get_scanner_availability", {"scanner_code": "SCN-7"}, "b1")], usage_metadata=_usage(1000, 30)),
            A(content="", tool_calls=[_call("search_scanners", {}, "b2")], usage_metadata=_usage(1300, 30)),
            A(content="I need to look the scanner up first.", usage_metadata=_usage(1500, 30)),
        ]))
        self.chat("agent:grounding-reject", "is scanner seven free this afternoon")

        # 3. An unknown tool and an invalid argument in one round.
        self.models.push(S(responses=[
            A(content="", tool_calls=[
                _call("made_up_tool", {}, "c1"),
                _call("get_scanner_availability", {}, "c2"),
            ], usage_metadata=_usage(1100, 40)),
            A(content="Sorry, I could not do that.", usage_metadata=_usage(1300, 20)),
        ]))
        self.chat("agent:unknown+invalid", "check the scanner calendar for me")

        # 4. A patient lookup through execute_command, carrying a real name
        #    (for the PHI check: it must not appear in Tempo or Loki).
        self.models.push(S(responses=[
            A(content="", tool_calls=[_call("execute_command", {"command_text": f"show patient {AGENT_PATIENT_NAME}"}, "d1")], usage_metadata=_usage(1000, 30)),
            A(content="Found the patient record.", usage_metadata=_usage(1400, 20)),
        ]))
        self.chat("agent:patient-lookup", "can you find the record for the patient I mentioned earlier")

        # 5. too_many_invalid_calls (no agent_events row; metric only).
        self.models.push(S(responses=[
            A(content="", tool_calls=[_call(f"nope_{i}", {}, f"e{i}") for i in range(3)], usage_metadata=_usage(900, 30)),
            A(content="unused"),
        ]))
        self.chat("agent:too-many-invalid", "do the scanner thing again please")

        # 6. max_rounds_exceeded: a model that never stops calling tools.
        # Distinct message objects: LangGraph's reducer dedupes by message id.
        self.models.push(S(responses=[
            A(content="", tool_calls=[_call("search_appointments", {"status": "DELAYED"}, f"f{i}")], usage_metadata=_usage(1000, 30))
            for i in range(4)
        ]))
        self.chat(
            "agent:max-rounds", "find delayed MRI appointments",
            settings=base_settings.model_copy(update={"max_agent_rounds": 2}),
        )

        # 7. llm_timeout: the model takes longer than the LLM timeout.
        self.models.push(self.Slow(responses=[A(content="too late")], delay=2.5))
        self.chat(
            "agent:llm-timeout", "check availability of the MRI scanners",
            settings=base_settings.model_copy(update={"llm_timeout_seconds": 1}),
        )

        # 8. A tool timeout: a real search with a zero-second budget.
        self.models.push(S(responses=[
            A(content="", tool_calls=[_call("search_appointments", {"appointment_type": "CT"}, "g1")], usage_metadata=_usage(1000, 30)),
            A(content="The search timed out.", usage_metadata=_usage(1200, 20)),
        ]))
        self.chat(
            "agent:tool-timeout", "how are the CT appointments looking",
            settings=base_settings.model_copy(update={"tool_timeout_seconds": 0}),
        )

        # 9. A provider error: the turn fails, route=error, HTTP 500.
        self.models.push(self.Failing(responses=[A(content="unused")]))
        self.chat("agent:provider-error", "search for delayed xray appointments")

    # -- voice --------------------------------------------------------------

    def voice(self) -> None:
        pcm = _fixture_pcm()
        speech = _chunks(pcm, 5)
        trailing = [_silence(250)] * 4
        sid = self.session("voice")
        with self.client.websocket_connect(f"/api/voice/{sid}") as ws:
            assert ws.receive_json() == {"type": "ready"}
            for _ in range(2):
                # The transcript ("List delayed MRI appointments.") does not
                # match the parser's word order, and Jev is off, so each
                # utterance is a (scripted) agent turn on channel=voice.
                self.models.push(self.Scripted(responses=[
                    self.AIMessage(content="", tool_calls=[_call("search_appointments", {"appointment_type": "MRI", "status": "DELAYED"}, "v1")], usage_metadata=_usage(1100, 40)),
                    self.AIMessage(content="Here are the delayed MRI appointments.", usage_metadata=_usage(1500, 30)),
                ]))
                for chunk in speech + trailing:
                    ws.send_bytes(chunk)
                messages = _drain_until_ready(ws)
                kinds = [m["type"] for m in messages]
                transcript = next((m.get("text") for m in messages if m["type"] == "transcript"), None)
                print(f"  voice:utterance           {kinds[-2] if len(kinds) > 1 else kinds} transcript={transcript!r}")
            # Stay connected past one metric export (5 s), so the
            # open-connections gauge is sampled while it reads 1.
            time.sleep(6)
        self.results.append(("voice:2-utterances", sid, 101, ""))

        # Dropped mid-utterance: speech starts, then the client goes away.
        sid = self.session("vdrop")
        with self.client.websocket_connect(f"/api/voice/{sid}") as ws:
            assert ws.receive_json() == {"type": "ready"}
            for chunk in speech[:3]:
                ws.send_bytes(chunk)
            assert ws.receive_json()["type"] == "speech_started"
            ws.close()
            time.sleep(0.5)  # let the server's disconnect handling finish
        print(f"  voice:dropped             session={sid}")
        self.results.append(("voice:dropped", sid, 101, ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", default="http://localhost:4318")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--pause", type=float, default=20.0, help="seconds between iterations")
    parser.add_argument("--no-voice", action="store_true", help="skip the (CPU-heavy) voice traffic")
    args = parser.parse_args()

    _configure_environment(args.endpoint)
    jev = FakeTypeSafe()  # before anything could import the real SDK

    from unittest.mock import patch

    from fastapi.testclient import TestClient

    import app.main as app_main  # telemetry is set up here, at import
    from app.config import get_settings

    settings = get_settings()
    assert settings.otel_enabled, "OTEL_ENABLED did not take effect"
    assert not settings.enable_jev_fast_path, "Jev must be off by default for this run"
    assert not settings.anthropic_api_key and not settings.openrouter_api_key
    assert not settings.typesafe_api_key

    models = ModelQueue()
    run_id = uuid.uuid4().hex[:6]
    print(f"run {run_id}: exporting to {args.endpoint} every 5 s")

    with (
        patch("app.api.routes.chat.get_default_chat_model", new=models),
        # Belt and braces: nothing may construct a real provider.
        patch("app.agent.providers.factory.get_provider", side_effect=RuntimeError("real provider blocked")),
        TestClient(app_main.app, raise_server_exceptions=False) as client,
    ):
        traffic = Traffic(client, models, jev, run_id)
        for i in range(args.iterations):
            print(f"iteration {i + 1}/{args.iterations}")
            traffic.parser_and_gates()
            traffic.operations()
            traffic.jev_turns(settings)
            traffic.agent_turns(settings)
            if not args.no_voice:
                traffic.voice()
            models.assert_empty()
            if i < args.iterations - 1:
                time.sleep(args.pause)
        # Let the periodic reader take one more sample before shutdown, so
        # the last iteration's counter increase is visible to rate().
        time.sleep(6)
    # Leaving the TestClient block runs the app's lifespan exit, which calls
    # shutdown_telemetry(): spans, logs and a final metric collection are
    # flushed to the collector before the process ends.

    print(f"\nfake Jev calls: {jev.calls}")
    print("requests with a trace to look up (label, session, status, X-Request-ID):")
    for label, sid, status, rid in traffic.results:
        if label.startswith(("agent:multi-round", "parser:show-patient", "ops:patient-search", "agent:patient-lookup")):
            print(f"  {label:<24} {status} session={sid} request_id={rid}")


if __name__ == "__main__":
    main()
