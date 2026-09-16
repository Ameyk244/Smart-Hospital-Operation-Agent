"""One-command dev startup: Postgres + backend + frontend, together.

Run from the repo root:

    python run.py

Why Python instead of a shell script (this replaced an earlier `dev.sh`):
a `.py` file runs identically no matter which shell you're in — PowerShell,
cmd, or Git Bash — where a `.sh` script silently does nothing useful in
PowerShell/cmd without an explicit bash invocation. This is the only
top-level entrypoint meant to be run directly; `backend/run.py` (a
different file) is the backend-only entrypoint this script shells out to,
and still exists on its own for anyone who wants to run just the backend
(see that file's docstring for why *it* has to exist as its own script:
a Windows event-loop-policy fix for the LangGraph Postgres checkpointer
that has to happen before uvicorn starts, not after).

Ctrl+C stops the backend and frontend. Postgres is left running — it's a
shared container, not meant to be torn down on every restart.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
POSTGRES_CONTAINER = "hospital_agent_postgres"


def backend_python() -> str:
    win_py = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    unix_py = BACKEND_DIR / ".venv" / "bin" / "python"
    if win_py.exists():
        return str(win_py)
    if unix_py.exists():
        return str(unix_py)
    print(
        "No backend/.venv found. Run the setup steps in README.md first.", file=sys.stderr
    )
    sys.exit(1)


def wait_for_postgres_healthy(timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format={{.State.Health.Status}}",
                POSTGRES_CONTAINER,
            ],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() == "healthy":
            return
        time.sleep(1)
    print("Postgres did not become healthy in time.", file=sys.stderr)
    sys.exit(1)


def stop_process(proc: subprocess.Popen) -> None:
    """Kills the whole process tree, not just the top-level process.

    Why: `npm run dev` on Windows wraps a `node` child process; a plain
    `.terminate()` on the wrapper can leave that child (vite's actual dev
    server) running as an orphan. `taskkill /T` kills the tree.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    # Line-buffer stdout explicitly: Python fully-buffers stdout when it
    # isn't attached to an interactive terminal (e.g. output redirected to
    # a file/log), which would otherwise delay these status messages
    # arbitrarily relative to the child processes' own output.
    sys.stdout.reconfigure(line_buffering=True)

    print("Starting Postgres...")
    subprocess.run(["docker", "compose", "up", "-d", "postgres"], cwd=ROOT, check=True)

    print("Waiting for Postgres to be healthy...")
    wait_for_postgres_healthy()

    print("Starting backend...")
    backend_proc = subprocess.Popen([backend_python(), "run.py"], cwd=BACKEND_DIR)

    print("Starting frontend...")
    npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
    frontend_proc = subprocess.Popen([npm_cmd, "run", "dev"], cwd=FRONTEND_DIR)

    print()
    print("Backend:  http://localhost:8000  (API only — don't open this in a browser)")
    print("Frontend: http://localhost:5173  <-- open this one")
    print()
    print("Press Ctrl+C to stop both.")

    try:
        while backend_proc.poll() is None and frontend_proc.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping backend and frontend...")
        stop_process(backend_proc)
        stop_process(frontend_proc)


if __name__ == "__main__":
    main()
