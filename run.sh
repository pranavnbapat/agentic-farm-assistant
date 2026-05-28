#!/usr/bin/env bash
# Run the Agentic Farm Assistant locally.
#   ./run.sh          # uvicorn with reload on :18002
#   PORT=9000 ./run.sh
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-18002}"
HOST="${HOST:-0.0.0.0}"

if [ ! -f .env ]; then
  echo "No .env found — copy .env.sample to .env and fill in OpenSearch + vLLM values." >&2
fi

exec uvicorn app.main:app --reload --host "$HOST" --port "$PORT"
