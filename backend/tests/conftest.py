"""Shared pytest fixtures.

Why it exists: integration tests need a real Postgres database (per
docs/ARCHITECTURE.md, mocks are for unit tests only — the app's own
integration suite must hit a real database). This creates a dedicated
`<configured_db>_test` database on the same Postgres instance so integration
tests never touch dev data.

Design note: every async engine/connection here is created *and* disposed
within a single test function's event loop. pytest-asyncio gives each test
function its own event loop by default, and asyncpg connections cannot be
reused across event loops (doing so deadlocks rather than raising cleanly).
So this deliberately does *not* share a session-scoped engine across tests —
each test pays a small (~10ms) connect cost instead of risking that class of
bug. Schema creation is idempotent (`create_all` is a no-op if tables exist)
so there's no real setup cost being duplicated either.

What calls it: any test file under `tests/integration/` or `tests/e2e/`
that requests the `db_session` or `seeded_session` fixture. Unit tests
(`tests/unit/`) should not need this file at all.
"""

import asyncio
import sys
import types
from collections.abc import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio

if sys.platform == "win32":
    # psycopg's async mode (used by the LangGraph Postgres checkpointer,
    # app/agent/checkpointer.py) refuses to run under Windows' default
    # ProactorEventLoop. Must be set before pytest-asyncio creates any event
    # loop, so this runs at conftest import time, not inside a fixture.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent.checkpointer import build_checkpointer
from app.config import Settings, get_settings
from app.db import models  # noqa: F401  populates Base.metadata
from app.db.base import Base
from app.seed.seed_data import seed as seed_hospital_data


def _test_database_url() -> tuple[str, str]:
    """Returns (admin_url_for_createdb, test_db_url)."""
    settings = get_settings()
    base_url = settings.database_url
    prefix, db_name = base_url.rsplit("/", 1)
    test_url = f"{prefix}/{db_name}_test"
    admin_url = f"{prefix}/postgres".replace("postgresql+asyncpg://", "postgresql://")
    return admin_url, test_url


async def _ensure_test_database_exists(admin_url: str, test_db_name: str) -> None:
    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", test_db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{test_db_name}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """One test = one engine = one connection = one rolled-back transaction,
    all confined to this test's own event loop (see module docstring)."""
    admin_url, test_url = _test_database_url()
    test_db_name = test_url.rsplit("/", 1)[-1]
    await _ensure_test_database_exists(admin_url, test_db_name)

    engine = create_async_engine(test_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        connection = await engine.connect()
        transaction = await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        session = session_factory()
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
            await connection.close()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded_session(db_session: AsyncSession) -> AsyncSession:
    """`db_session`, pre-populated with the reproducible synthetic dataset."""
    await seed_hospital_data(db_session)
    await db_session.flush()
    return db_session


@pytest_asyncio.fixture
async def agent_session_factory():
    """An `async_sessionmaker` (not a single session) bound to a real,
    genuinely committed, seeded test database.

    Why this is different from `db_session`/`seeded_session`: the agent's
    tool_node opens its *own* fresh session per tool call (see
    app/agent/graph.py's module docstring for why), so agent-loop tests need
    multiple independent connections that all see the same committed data —
    not one connection's uncommitted, rolled-back-at-the-end transaction.
    """
    admin_url, test_url = _test_database_url()
    test_db_name = test_url.rsplit("/", 1)[-1]
    await _ensure_test_database_exists(admin_url, test_db_name)

    engine = create_async_engine(test_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as seeding_session:
            await seed_hospital_data(seeding_session)
            await seeding_session.commit()

        yield session_factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def checkpointer(agent_session_factory):
    """A real `AsyncPostgresSaver` against the same test database
    `agent_session_factory` uses — for tests of concept 36 (LangGraph
    checkpointing) specifically. Depends on `agent_session_factory` purely
    to reuse its test-database setup; the checkpointer itself uses its own
    psycopg connection, separate from the asyncpg engine the rest of the
    app uses (see app/agent/checkpointer.py for why).

    Truncates the checkpointer's own tables before each test: they are
    created/migrated by `AsyncPostgresSaver.setup()`, not by
    `Base.metadata`, so nothing else clears them between test runs — a
    fixed thread_id (session_id) reused across two runs of the same test
    would otherwise silently resume the *previous run's* checkpoint data.
    """
    _admin_url, test_url = _test_database_url()
    async with build_checkpointer(test_url) as cp:
        # Only the state tables — checkpoint_migrations just tracks which
        # schema migrations have already run and doesn't need resetting.
        await cp.conn.execute("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes")
        yield cp


@pytest_asyncio.fixture
async def client(seeded_session) -> AsyncGenerator[AsyncClient, None]:
    """An `httpx.AsyncClient` against the real FastAPI app, with `get_db`
    bound to `seeded_session` and `get_checkpointer` stubbed to `None`.

    Shared across `tests/e2e/*` files that only need the deterministic/
    rejected request paths (or an agent path that's fine falling back to
    ConversationMessage-based history) — `httpx.ASGITransport` doesn't run
    the app's `lifespan`, so `app.state.checkpointer` is never set outside
    of this override; `run_agent` treats `checkpointer=None` as "use
    `history` instead", which is exactly what those tests need. Tests that
    specifically exercise checkpointing (e.g.
    `test_agent_path_via_http_with_live_model`) override `get_checkpointer`
    again themselves, pointing at the real `checkpointer` fixture.
    """
    from app.api.routes.chat import get_checkpointer
    from app.db.session import get_db
    from app.main import app

    async def _override_get_db():
        yield seeded_session

    async def _override_get_checkpointer():
        return None

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_checkpointer] = _override_get_checkpointer
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Jev fast-path test doubles.
#
# `typesafe_sdk` is imported lazily in production so a broken installation can
# still fall through safely, and these tests must never make a live call. Every
# Jev test therefore uses a fake module installed into `sys.modules` under the
# real package name: the
# production code's own `from typesafe_sdk import ...` then resolves to this,
# which means the module under test is exercised for real rather than
# stubbed out wholesale.
# --------------------------------------------------------------------------


class FakeJevAnswer:
    def __init__(self, choice: str, confidence: float, probabilities: dict | None = None):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = probabilities if probabilities is not None else {choice: confidence}


class FakeJevUsage:
    def __init__(self, input_tokens: int | None = 350, output_tokens: int | None = 5):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeJevResponse:
    def __init__(self, answers: dict, usage: FakeJevUsage | None = None, model: str = "jev-latest"):
        self.answers = answers
        self.usage = usage if usage is not None else FakeJevUsage()
        self.model = model


def _build_jev_response(
    command: str,
    confidence: float = 0.97,
    *,
    filters: dict[str, tuple[str, float]] | None = None,
    null_usage: bool = False,
    model: str = "jev-latest",
) -> FakeJevResponse:
    """Builds a whole `system_one` response from the command answer plus any
    filter answers under test. Every filter question the production code asks
    gets an answer, defaulting to a confident "unspecified" — matching the
    real contract, where all four questions are always answered.

    `null_usage=True` models the SDK's documented `int | None` token counts
    coming back null, which the cost endpoint has to handle explicitly."""
    usage = FakeJevUsage(None, None) if null_usage else None
    answers = {
        "command": FakeJevAnswer(command, confidence),
        "scanner_type": FakeJevAnswer("unspecified", 0.99),
        "scanner_status": FakeJevAnswer("unspecified", 0.99),
        "appointment_type": FakeJevAnswer("unspecified", 0.99),
    }
    for question, (label, conf) in (filters or {}).items():
        answers[question] = FakeJevAnswer(label, conf)
    return FakeJevResponse(answers, usage=usage, model=model)


class FakeTypeSafe:
    """Handle for the installed fake SDK: set `behavior`, then inspect
    `calls` and `client_constructions` afterwards. `client_constructions`
    is what proves the flag-off path never even builds a client."""

    def __init__(self, module: types.ModuleType):
        self.module = module
        self.behavior: object = None
        self.calls: list[dict] = []
        self.client_constructions = 0
        self.client_closes = 0
        self.accepts_model = True
        # Kwargs each TypeSafeClient(...) was constructed with. Recorded so a
        # test can assert the API key is passed explicitly: the real SDK
        # would otherwise fall back to reading TYPESAFE_API_KEY from the
        # environment, which this project never exports (pydantic-settings
        # loads .env into Settings, not into os.environ) — a bug that only
        # shows up on a live call, so the fake has to make it assertable.
        self.client_kwargs: list[dict] = []


@pytest.fixture
def fake_typesafe():
    module = types.ModuleType("typesafe_sdk")
    handle = FakeTypeSafe(module)

    class TypeSafeError(Exception):
        """Base class, mirroring the real SDK's hierarchy."""

    class TypeSafeAPIError(TypeSafeError):
        pass

    class TypeSafeRateLimitError(TypeSafeAPIError):
        retry_after_ms = 1000

    class TypeSafeAPITimeoutError(TypeSafeAPIError):
        pass

    class Choice:
        def __init__(self, *, instructions: str, criteria: dict):
            self.instructions = instructions
            self.criteria = criteria

    class TypeSafeClient:
        def __init__(self, **kwargs):
            handle.client_constructions += 1
            handle.client_kwargs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            handle.client_closes += 1

        def system_one(self, *, state, questions, **kwargs):
            if "model" in kwargs and not handle.accepts_model:
                raise TypeError("system_one() got an unexpected keyword argument 'model'")
            handle.calls.append({"state": state, "questions": questions, **kwargs})
            behavior = handle.behavior
            if isinstance(behavior, BaseException):
                raise behavior
            if callable(behavior):
                return behavior(state, questions)
            return behavior

    module.Choice = Choice
    module.TypeSafeClient = TypeSafeClient
    module.TypeSafeError = TypeSafeError
    module.TypeSafeAPIError = TypeSafeAPIError
    module.TypeSafeRateLimitError = TypeSafeRateLimitError
    module.TypeSafeAPITimeoutError = TypeSafeAPITimeoutError

    previous = sys.modules.get("typesafe_sdk")
    sys.modules["typesafe_sdk"] = module
    try:
        yield handle
    finally:
        if previous is None:
            sys.modules.pop("typesafe_sdk", None)
        else:
            sys.modules["typesafe_sdk"] = previous


def _build_jev_settings(**overrides) -> Settings:
    """Settings with the fast path on and a dummy key. Every value is passed
    explicitly so a developer's real `.env` can never leak a live key — or a
    real `ENABLE_JEV_FAST_PATH=true` — into a test run."""
    defaults = dict(
        enable_jev_fast_path=True,
        typesafe_api_key="test-key-not-real",
        jev_model="jev-latest",
        jev_confidence_threshold=0.9,
        jev_timeout_seconds=5.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


# Exposed as fixtures (rather than imported from this module by name) so test
# files never have to rely on `conftest` being importable on sys.path.
@pytest.fixture
def jev_settings():
    return _build_jev_settings


@pytest.fixture
def jev_response():
    return _build_jev_response
