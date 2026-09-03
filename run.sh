#!/usr/bin/env bash
# One-command launch: sets up the venv (first run only), starts the FastAPI
# backend, and starts the Streamlit dashboard in the foreground.
#
# Usage:
#   ./run.sh                       # start everything
#   DATASET_SIZE=1500 ./run.sh     # override dataset size for this session
#   API_PORT=8001 ./run.sh         # use a different API port
#   DASHBOARD_PORT=8502 ./run.sh   # use a different dashboard port
#
# Safe to re-run: if the API is already up and healthy on API_PORT (e.g. a
# previous ./run.sh is still alive), it is reused instead of failing with
# "address already in use". Stop with Ctrl+C -- an API server this script
# itself started is killed automatically on exit; a reused one is left alone.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -d .venv ]; then
    echo "==> Creating virtual environment..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import fastapi, streamlit, pandas, rapidfuzz, openai" >/dev/null 2>&1; then
    echo "==> Installing dependencies (first run only)..."
    pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> Created .env from .env.example (no LLM_API_KEY set -> AI reasoning will use the safe fallback path)."
fi

# Streamlit's one-time "Welcome" prompt asks for an email on the very first
# run on a machine and blocks stdin in non-interactive contexts. Pre-seeding
# an empty credentials file skips it permanently, for this user, everywhere.
mkdir -p "$HOME/.streamlit"
if [ ! -f "$HOME/.streamlit/credentials.toml" ]; then
    printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
fi
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

API_PORT="${API_PORT:-8000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
API_PID=""

if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
    echo "==> API already running and healthy on http://127.0.0.1:${API_PORT} -- reusing it."
elif (exec 3<>"/dev/tcp/127.0.0.1/${API_PORT}") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "ERROR: port ${API_PORT} is already in use by something that is not this API (health check failed)." >&2
    echo "       Free it, or run with a different port: API_PORT=8001 ./run.sh" >&2
    exit 1
else
    echo "==> Starting API on http://127.0.0.1:${API_PORT} ..."
    uvicorn app.api:app --host 127.0.0.1 --port "${API_PORT}" --log-level warning &
    API_PID=$!
    for _ in $(seq 1 30); do
        curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1 && break
        sleep 0.5
    done
fi

cleanup() {
    if [ -n "$API_PID" ]; then
        echo "==> Stopping API (pid ${API_PID})..."
        kill "$API_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

if (exec 3<>"/dev/tcp/127.0.0.1/${DASHBOARD_PORT}") 2>/dev/null; then
    exec 3>&- 3<&-
    echo "ERROR: port ${DASHBOARD_PORT} is already in use." >&2
    echo "       Free it, or run with a different port: DASHBOARD_PORT=8502 ./run.sh" >&2
    exit 1
fi

API_BASE_URL="http://127.0.0.1:${API_PORT}" \
    streamlit run dashboard/app.py --server.port "${DASHBOARD_PORT}"
