#!/usr/bin/env bash
# One-command dev startup: Postgres + backend + frontend, together.
# Run from the repo root: ./dev.sh
# Ctrl+C stops both dev servers (and leaves Postgres running, since that's
# a shared container you don't want torn down on every restart).
set -e
cd "$(dirname "$0")"

# Pick the venv's python regardless of platform (path relative to backend/,
# since we cd into it before using this).
if [ -f "backend/.venv/Scripts/python.exe" ]; then
  BACKEND_PY=".venv/Scripts/python.exe"
elif [ -f "backend/.venv/bin/python" ]; then
  BACKEND_PY=".venv/bin/python"
else
  echo "No backend/.venv found. Run the setup steps in docs first (see README.md)." >&2
  exit 1
fi

echo "Starting Postgres..."
docker compose up -d postgres

echo "Waiting for Postgres to be healthy..."
until [ "$(docker inspect --format='{{.State.Health.Status}}' hospital_agent_postgres 2>/dev/null)" = "healthy" ]; do
  sleep 1
done

cleanup() {
  echo ""
  echo "Stopping backend and frontend..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting backend..."
(cd backend && "$BACKEND_PY" run.py) &
BACKEND_PID=$!

echo "Starting frontend..."
(cd frontend && npm run dev) &
FRONTEND_PID=$!

sleep 2
echo ""
echo "Backend:  http://localhost:8000  (API only, don't open this in a browser)"
echo "Frontend: http://localhost:5173  <-- open this one"
echo ""
echo "Press Ctrl+C to stop both."

wait
