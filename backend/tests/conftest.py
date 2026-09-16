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
from collections.abc import AsyncGenerator

import asyncpg
import pytest_asyncio

if sys.platform == "win32":
    # psycopg's async mode (used by the LangGraph Postgres checkpointer,
    # app/agent/checkpointer.py) refuses to run under Windows' default
    # ProactorEventLoop. Must be set before pytest-asyncio creates any event
    # loop, so this runs at conftest import time, not inside a fixture.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent.checkpointer import build_checkpointer
from app.config import get_settings
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
