# Smart Hospital Operations Agent

A learning project: a small, real, synthetic-data hospital-operations backend
with a deterministic core, augmented by a LangGraph-orchestrated LLM agent
that uses real structured tool calling and code-level grounding. See
`docs/ARCHITECTURE.md` for the full design and `docs/PROGRESS.md` for
current status. No real patient data is ever used.

## Prerequisites

- Python 3.11
- Docker (for PostgreSQL)
- Node 18+ (for the frontend)

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

# 3. Frontend
cd ../frontend
npm install

# 4. Run tests (from backend/)
cd ../backend && pytest
```

## Running it

```bash
python run.py
```

One command, from the **repo root** — starts Postgres (if not already up),
the backend, and the frontend together, and stops both on Ctrl+C (Postgres
is left running). This is a plain Python script (stdlib only), so it works
identically in PowerShell, cmd, or a bash-like shell — no shell-specific
scripting involved. Open **`http://localhost:5173`** — that's the app.
`http://localhost:8000` is the backend API only; don't open it in a
browser, there's no page there.

There are **two different `run.py` files** — don't confuse them:
- **`run.py`** (repo root) — starts everything, described above.
- **`backend/run.py`** — starts *only* the backend. `run.py` (root) calls
  this internally; run it directly yourself only if you want the backend
  running without the frontend (e.g. hitting the API from Postman/curl,
  or debugging the frontend against a backend you're restarting less
  often):
  ```bash
  cd backend && python run.py     # NOT `uvicorn app.main:app` directly — see note below
  cd frontend && npm run dev      # separate terminal, if you also want the UI
  ```

CORS on the backend is already configured for `http://localhost:5173`; the
frontend's API base URL is configurable via `VITE_API_BASE_URL` (see
`frontend/.env.example`) if you need to point it elsewhere.

**Port note**: `docker-compose.yml` maps Postgres to host port **5433**, not
the usual 5432, because this dev machine already has a native PostgreSQL
service bound to 5432. If your machine doesn't have that conflict, you can
override with `POSTGRES_PORT=5432` in `.env` and it'll just work either way
since the app reads `DATABASE_URL` directly.

**Windows + `backend/run.py` note**: the LangGraph Postgres checkpointer
(`backend/app/agent/checkpointer.py`) uses `psycopg`, whose async mode
refuses to run under Windows' default `ProactorEventLoop`. `backend/run.py`
sets the correct event loop policy *before* starting uvicorn; running
`uvicorn app.main:app` directly on Windows will fail at startup, because
uvicorn creates its event loop before it ever imports the app module — see
that file's docstring for the full explanation. Not an issue on
Linux/macOS; `backend/run.py` works there too.

## Secrets

No secrets are committed. `backend/.env.example` documents every variable;
copy it to `backend/.env` and fill in real values. At minimum, running the
agent (not just the deterministic path) requires an LLM API key — see the
checkpoint note in `docs/PROGRESS.md` for exactly which one and where to get it.

## Project layout

```
backend/    FastAPI app: db models, repositories, deterministic parser,
            trusted command execution, agent (LangGraph + tools), API routes
frontend/   React + TypeScript + Vite UI: operations view (departments,
            scanners, appointments, patient search), chat panel (POST
            /api/chat, session persisted in localStorage), and a trace
            panel (GET /api/sessions/{id}/trace) showing the real
            parser/agent decisions and tool calls behind each reply
docs/       architecture, progress log, concept coverage audit
```
