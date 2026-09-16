"""FastAPI application entrypoint.

Why it exists: process entrypoint (`uvicorn app.main:app`). Deliberately
thin — route logic lives in `app/api/routes/*`, this module only wires them
together plus cross-cutting concerns (CORS for the local frontend dev
server, health check).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import chat, commands
from app.observability.logging_config import configure_logging

configure_logging()

app = FastAPI(title="Smart Hospital Operations Agent")

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
