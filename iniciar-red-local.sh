#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
set -a
source .env
set +a

export IDS_INTERNAL_BASE_URL="https://127.0.0.1:${UVICORN_PORT:-8000}"
backend_pid=
host_pid=
cleanup() {
    if [[ -n "$host_pid" ]]; then kill "$host_pid" 2>/dev/null || true; fi
    if [[ -n "$backend_pid" ]]; then kill "$backend_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

.venv/bin/python -m uvicorn backend.app.main:app \
    --host "${UVICORN_HOST:-0.0.0.0}" --port "${UVICORN_PORT:-8000}" \
    --ssl-certfile "$IDS_TLS_CERTFILE" --ssl-keyfile "$IDS_TLS_KEYFILE" &
backend_pid=$!

for ((attempt = 0; ; attempt++)); do
    if curl --noproxy '*' --cacert "$IDS_TLS_CA_FILE" --fail --silent \
        --max-time 1 "$IDS_INTERNAL_BASE_URL/api/v1/health" >/dev/null; then
        break
    fi
    if ! kill -0 "$backend_pid" 2>/dev/null || ((attempt >= 119)); then
        echo "El backend HTTPS no respondió. Revisa el error de Uvicorn." >&2
        exit 1
    fi
    sleep 1
done

.venv/bin/python examples/host_server.py &
host_pid=$!
wait -n "$backend_pid" "$host_pid"
