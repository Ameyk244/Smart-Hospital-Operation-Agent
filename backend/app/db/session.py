"""Async engine + session factory.

Why it exists: one shared `AsyncEngine`/`async_sessionmaker` so connection
pooling is process-wide rather than re-created per request. Used purposefully
async (concept 30) because every call site is real I/O: FastAPI request
handlers, tool execution, and the LangGraph nodes all await a DB round trip.

What calls it: `app/api/routes/*` (via the `get_db` FastAPI dependency) and
`app/db/repositories/*` (via `AsyncSession` passed in from the caller —
repositories never create their own session).

Fails: connection errors surface as `sqlalchemy.exc.OperationalError` from
whichever call awaited the query; FastAPI's exception handling turns that
into a 503 at the route layer (see `app/api/routes`).
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, echo=False, pool_pre_ping=True)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one session per request, committed/rolled back and
    closed automatically at the end of the request."""
    async with async_session_factory() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency handing out the *factory* itself, not a session —
    for callers that need to open several independent sessions of their own
    (the agent graph's tool_node, one fresh session per tool call; see
    app/agent/graph.py). Overridden the same way as `get_db` in tests that
    need the agent path to use a test database instead of the dev one."""
    return async_session_factory
