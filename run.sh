#!/usr/bin/env bash
# One-command launch: sets up the venv (first run only), starts the FastAPI
# backend, and serves the static frontend (web/).
#
# Usage:
#   ./run.sh                       # start everything
#   DATASET_SIZE=1500 ./run.sh     # override dataset size for this session
#   API_PORT=8001 ./run.sh         # use a different API port
#   DASHBOARD_PORT=8502 ./run.sh   # use a different frontend port
#
# Safe to re-run: if the API is already up and healthy on API_PORT (e.g. a
# previous ./run.sh is still alive), it is reused instead of failing with
# "address already in use". Stop with Ctrl+C -- an API server this script
# itself started is killed automatically on exit; a reused one is left alone.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# `python3` is not a universal name: Windows (Git Bash) ships `python`, and its
# `python3.exe` is usually a Microsoft Store stub that resolves but refuses to run --
# so a candidate is only accepted once it has actually executed something.
pick_python() {
    for candidate in "${PYTHON:-}" python3 python; do
        [ -n "$candidate" ] || continue
        command -v "$candidate" >/dev/null 2>&1 || continue
        "$candidate" -c "import sys; sys.exit(0)" >/dev/null 2>&1 && { echo "$candidate"; return 0; }
    done
    echo "ERROR: no working Python interpreter found (tried \$PYTHON, python3, python)." >&2
    echo "       Install Python 3.10+ or point PYTHON at it: PYTHON=/path/to/python ./run.sh" >&2
    exit 1
}

if [ ! -d .venv ]; then
    echo "==> Creating virtual environment..."
    "$(pick_python)" -m venv .venv
fi
# POSIX venvs put their entry points in bin/, Windows venvs in Scripts/.
VENV_ACTIVATE=".venv/bin/activate"
[ -f "$VENV_ACTIVATE" ] || VENV_ACTIVATE=".venv/Scripts/activate"
if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "ERROR: .venv exists but has no activate script ($VENV_ACTIVATE)." >&2
    echo "       Delete .venv and re-run to rebuild it." >&2
    exit 1
fi
# shellcheck disable=SC1090,SC1091
source "$VENV_ACTIVATE"

if ! python -c "import fastapi, rapidfuzz, openai" >/dev/null 2>&1; then
    echo "==> Installing dependencies (first run only)..."
    pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> Created .env from .env.example (no LLM_API_KEY set -> AI reasoning will use the safe fallback path)."
fi

API_PORT="${API_PORT:-8000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
API_PID=""
# Bounded readiness wait: long enough for a cold import of fastapi/openai on a
# slow disk, short enough that a broken start is reported instead of hanging.
API_READY_TIMEOUT=20
API_READY_ATTEMPTS=$((API_READY_TIMEOUT * 2))

# Exported so the API process this script starts derives its CORS default from
# the port the dashboard is actually served on (config.py reads DASHBOARD_PORT).
# An explicit CORS_ALLOWED_ORIGINS already in the environment still wins.
export API_PORT DASHBOARD_PORT

# Installed before the API is started, so every failure path below -- and
# Ctrl+C -- tears down a server this script owns. A reused server keeps API_PID
# empty and is deliberately left running.
cleanup() {
    if [ -n "$API_PID" ]; then
        echo "==> Stopping API (pid ${API_PID})..."
        kill "$API_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
    echo "==> API already running and healthy on http://127.0.0.1:${API_PORT} -- reusing it."
elif (exec 3<>"/dev/tcp/127.0.0.1/${API_PORT}") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "ERROR: port ${API_PORT} is already in use by something that is not this API (health check failed)." >&2
    echo "       Free it, or run with a different port: API_PORT=8001 ./run.sh" >&2
    exit 1
else
    echo "==> Starting API on http://127.0.0.1:${API_PORT} (waiting up to ${API_READY_TIMEOUT}s for /health) ..."
    uvicorn app.api:app --host 127.0.0.1 --port "${API_PORT}" --log-level warning &
    API_PID=$!
    api_ready=0
    for attempt in $(seq 1 "${API_READY_ATTEMPTS}"); do
        if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
            api_ready=1
            break
        fi
        # A dead child never becomes healthy: stop waiting at once so the
        # operator reads uvicorn's own traceback instead of watching a stall.
        if ! kill -0 "$API_PID" 2>/dev/null; then
            wait "$API_PID" 2>/dev/null || true
            API_PID=""
            break
        fi
        if [ "$attempt" -eq 10 ]; then
            echo "    still waiting for the API to report healthy..."
        fi
        sleep 0.5
    done
    if [ "$api_ready" -ne 1 ]; then
        echo "" >&2
        if [ -z "$API_PID" ]; then
            echo "ERROR: the API process exited during startup (port ${API_PORT})." >&2
        else
            echo "ERROR: the API never reported healthy on http://127.0.0.1:${API_PORT} after ${API_READY_TIMEOUT}s." >&2
        fi
        echo "       The dashboard was NOT started -- it would only have shown failing requests." >&2
        echo "       Reproduce the startup error directly to see the real reason:" >&2
        echo "           cd $(pwd) && source ${VENV_ACTIVATE} && uvicorn app.api:app --port ${API_PORT}" >&2
        echo "       Usual causes: an import error under app/, a missing dependency" >&2
        echo "       (pip install -r requirements.txt), or port ${API_PORT} held by another" >&2
        echo "       process (retry with API_PORT=8001 ./run.sh)." >&2
        exit 1
    fi
fi

if (exec 3<>"/dev/tcp/127.0.0.1/${DASHBOARD_PORT}") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "ERROR: port ${DASHBOARD_PORT} is already in use." >&2
    echo "       Free it, or run with a different port: DASHBOARD_PORT=8502 ./run.sh" >&2
    exit 1
fi

# The frontend is static (no build step) -- it auto-detects the API at
# <same-host>:API_PORT. This gitignored file always reflects the current
# API_PORT explicitly, so a previous run's override never lingers stale.
cat > web/runtime-config.js <<EOF
// Auto-generated by run.sh on each launch -- do not edit, do not commit.
localStorage.setItem('veyra_api_base', 'http://127.0.0.1:${API_PORT}');
EOF

echo "==> Serving dashboard on http://127.0.0.1:${DASHBOARD_PORT} ..."
python -m http.server "${DASHBOARD_PORT}" --directory web --bind 127.0.0.1
