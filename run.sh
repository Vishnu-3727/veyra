#!/usr/bin/env bash
# One-command launch: sets up the venv (first run only), starts the FastAPI
# backend, and starts the Streamlit dashboard in the foreground.
#
# Usage:
#   ./run.sh                # start everything
#   DATASET_SIZE=1500 ./run.sh   # override dataset size for this session
#
# Stop with Ctrl+C -- the API server (started in the background) is killed
# automatically on exit.
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

API_PORT="${API_PORT:-8000}"
echo "==> Starting API on http://127.0.0.1:${API_PORT} ..."
uvicorn app.api:app --host 127.0.0.1 --port "${API_PORT}" --log-level warning &
API_PID=$!
trap 'echo "==> Stopping API (pid ${API_PID})..."; kill ${API_PID} 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

echo "==> Starting dashboard on http://127.0.0.1:8501 ..."
streamlit run dashboard/app.py --server.port 8501
