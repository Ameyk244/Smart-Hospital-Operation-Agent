# Smart Hospital Operations Agent

A learning project: a small, real, synthetic-data hospital-operations backend
with a deterministic core, augmented by a LangGraph-orchestrated LLM agent
that uses real structured tool calling and code-level grounding. See
`docs/ARCHITECTURE.md` for the full design and `docs/PROGRESS.md` for
current status. No real patient data is ever used.

## Prerequisites

- Python 3.11
- Docker (for PostgreSQL)
- Node 18+ (for the frontend, once it exists)

## Setup

```bash
# 1. Start PostgreSQL (host port 5433 — see note below)
docker compose up -d postgres

# 2. Backend
cd backend
python -m venv .venv
./.venv/Scripts/activate        # Windows; source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
copy .env.example .env          # cp on macOS/Linux — fill in an LLM API key when you have one
alembic upgrade head
python -m app.seed.seed_data

# 3. Run tests
pytest

# 4. Run the dev server
python run.py                   # NOT `uvicorn app.main:app` directly — see note below
```

**Port note**: `docker-compose.yml` maps Postgres to host port **5433**, not
the usual 5432, because this dev machine already has a native PostgreSQL
service bound to 5432. If your machine doesn't have that conflict, you can
override with `POSTGRES_PORT=5432` in `.env` and it'll just work either way
since the app reads `DATABASE_URL` directly.

**Windows + `run.py` note**: the LangGraph Postgres checkpointer
(`app/agent/checkpointer.py`) uses `psycopg`, whose async mode refuses to run
under Windows' default `ProactorEventLoop`. `python run.py` sets the correct
event loop policy *before* starting uvicorn; running `uvicorn app.main:app`
directly on Windows will fail at startup, because uvicorn creates its event
loop before it ever imports the app module — see `run.py`'s docstring for
the full explanation. Not an issue on Linux/macOS; `run.py` works there too.

## Secrets

No secrets are committed. `backend/.env.example` documents every variable;
copy it to `backend/.env` and fill in real values. At minimum, running the
agent (not just the deterministic path) requires an LLM API key — see the
checkpoint note in `docs/PROGRESS.md` for exactly which one and where to get it.

## Project layout

```
backend/    FastAPI app: db models, repositories, deterministic parser,
            trusted command execution, agent (LangGraph + tools), API routes
frontend/   React + TypeScript UI (operations view, chat, trace panel)
docs/       architecture, progress log, concept coverage audit
```
