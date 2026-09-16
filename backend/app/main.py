"""FastAPI application entrypoint.

Why it exists: process entrypoint (`uvicorn app.main:app`). Deliberately
thin — route logic lives in `app/api/routes/*`, this module only wires them
together plus cross-cutting concerns (CORS for the local frontend dev
server, health check, and building the long-lived LangGraph checkpointer
for the process lifetime).
"""

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

if sys.platform == "win32":
    # See tests/conftest.py for the same fix and why: psycopg's async mode
    # (used by the LangGraph Postgres checkpointer) refuses to run under
    # Windows' default ProactorEventLoop. Must be set before uvicorn creates
    # its event loop, so this runs at import time, before `FastAPI()`.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.agent.checkpointer import build_checkpointer  # noqa: E402
from app.api.routes import chat, commands  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.observability.logging_config import configure_logging  # noqa: E402

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with build_checkpointer(settings.database_url) as checkpointer:
        app.state.checkpointer = checkpointer
        yield


app = FastAPI(title="Smart Hospital Operations Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(commands.router)
app.include_router(chat.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
