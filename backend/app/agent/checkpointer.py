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

What calls it: `app/main.py`'s lifespan (builds the real one, long-lived,
stored on `app.state`, `.setup()` called once at startup to create/migrate
its tables) and `tests/conftest.py` (a short-lived one per test, against the
test database).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def to_psycopg_conn_string(database_url: str) -> str:
    """`postgresql+asyncpg://...` (SQLAlchemy) -> `postgresql://...` (psycopg)."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def build_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    conn_string = to_psycopg_conn_string(database_url)
    async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
