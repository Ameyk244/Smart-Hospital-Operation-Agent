"""LangGraph Postgres checkpointing (concept 36).

Why it exists: this is *graph execution/resumption* state — specifically,
enough of a running graph's internal state (its message list included) to
resume a thread without the caller manually reconstructing it. It is
deliberately not the same thing as `ConversationMessage` (concept 35, the
durable human-readable transcript used for the UI and written by *both* the
deterministic and agent paths) or `Preference` (concept 37, explicit
remembered facts). See docs/ARCHITECTURE.md §7 for why these three are kept
separate rather than merged into one generic "memory".

Uses `psycopg` (v3), not `asyncpg` — that's what the upstream
`langgraph-checkpoint-postgres` package is built on, so this module owns the
one conversion between our asyncpg-style `DATABASE_URL` and psycopg's
connection string format, rather than that leaking into callers.

Observability (Phase 3): the saver is ONE long-lived psycopg connection,
not a pool, and every operation takes the saver's own `asyncio.Lock` before
using it. So there is no pool size or checked-out count to report. What is
real is (a) how long each checkpoint operation takes, lock wait included,
which is where contention between concurrent agent turns shows up, and (b)
whether the connection is still open. `InstrumentedAsyncPostgresSaver`
records (a); `metrics.observe_checkpointer` registers the saver for (b).
It is a subclass rather than a wrapper so LangGraph sees an ordinary
`AsyncPostgresSaver`; only timing is added, behavior is unchanged.

What calls it: `app/main.py`'s lifespan (builds the real one, long-lived,
stored on `app.state`, `.setup()` called once at startup to create/migrate
its tables) and `tests/conftest.py` (a short-lived one per test, against the
test database).
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.observability import metrics


def to_psycopg_conn_string(database_url: str) -> str:
    """`postgresql+asyncpg://...` (SQLAlchemy) -> `postgresql://...` (psycopg)."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://")


class InstrumentedAsyncPostgresSaver(AsyncPostgresSaver):
    """Times the three operations an agent turn performs: read the thread's
    latest checkpoint, write a checkpoint, write pending task writes. (`alist`
    is an async generator the app never calls, so it is left alone.)"""

    async def _timed(self, operation: str, coro: Any) -> Any:
        started = time.perf_counter()
        status = "error"
        try:
            result = await coro
            status = "ok"
            return result
        finally:
            metrics.record_checkpointer_operation(
                operation, time.perf_counter() - started, status
            )

    async def aget_tuple(self, config):  # type: ignore[override]
        return await self._timed("get_tuple", super().aget_tuple(config))

    async def aput(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        return await self._timed(
            "put", super().aput(config, checkpoint, metadata, new_versions)
        )

    async def aput_writes(self, config, writes, task_id, task_path=""):  # type: ignore[override]
        return await self._timed(
            "put_writes", super().aput_writes(config, writes, task_id, task_path)
        )


@asynccontextmanager
async def build_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    conn_string = to_psycopg_conn_string(database_url)
    async with InstrumentedAsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        await checkpointer.setup()
        metrics.observe_checkpointer(checkpointer)
        try:
            yield checkpointer
        finally:
            metrics.observe_checkpointer(None)
