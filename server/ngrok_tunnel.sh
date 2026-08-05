#!/usr/bin/env bash
# Expose the Evolving-Benchmarks Evaluation Service publicly via a single ngrok
# tunnel (reverse proxy + usage analytics; see ngrok_tunnel.py).
#
# Requires an ngrok authtoken in $NGROK_TOKEN (or $NGROK_AUTHTOKEN); get one at
# https://dashboard.ngrok.com/get-started/your-authtoken.
#
# Usage:
#   NGROK_TOKEN=xxxxx ./ngrok_tunnel.sh                # tunnel the running service
#   EVAL_SERVICE_API_KEY=secret ./ngrok_tunnel.sh      # require an API key
#   START_SERVICE=1 ./ngrok_tunnel.sh                  # also boot the service if down
#   PORT=8077 PROXY_PORT=9077 ./ngrok_tunnel.sh        # custom ports
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- config (override via env) --------------------------------------------- #
NGROK_TOKEN="${NGROK_TOKEN:-${NGROK_AUTHTOKEN:-}}"
if [[ -z "${NGROK_TOKEN}" ]]; then
  echo "ERROR: no ngrok authtoken." >&2
  echo "       Set NGROK_TOKEN (or NGROK_AUTHTOKEN) to the token from" >&2
  echo "       https://dashboard.ngrok.com/get-started/your-authtoken" >&2
  exit 1
fi

UPSTREAM_HOST="${EVAL_SERVICE_HOST:-localhost}"
UPSTREAM_PORT="${PORT:-8077}"
PROXY_PORT="${PROXY_PORT:-9077}"
API_KEY="${EVAL_SERVICE_API_KEY:-}"
PY="${PYTHON:-python3}"

# --- ensure pyngrok is available ------------------------------------------- #
if ! "${PY}" -c "import pyngrok" >/dev/null 2>&1; then
  echo "Installing pyngrok ..."
  "${PY}" -m pip install --quiet --upgrade pyngrok
fi

# --- optionally start the service if it isn't already up ------------------- #
HEALTH_URL="http://${UPSTREAM_HOST}:${UPSTREAM_PORT}/v1/health"
if ! curl -sf -m 2 "${HEALTH_URL}" >/dev/null 2>&1; then
  if [[ "${START_SERVICE:-0}" == "1" ]]; then
    echo "Service not up — launching it in the background (logs: /tmp/eval_service.log) ..."
    ( HOST="${UPSTREAM_HOST}" PORT="${UPSTREAM_PORT}" \
        bash "${SCRIPT_DIR}/run.sh" >/tmp/eval_service.log 2>&1 & )
    for _ in $(seq 1 30); do
      curl -sf -m 2 "${HEALTH_URL}" >/dev/null 2>&1 && { echo "Service is up."; break; }
      sleep 1
    done
  else
    echo "WARNING: ${HEALTH_URL} not reachable."
    echo "         Start it with ./run.sh (or re-run with START_SERVICE=1)."
    echo "         The tunnel will still come up and return 502 until the service is live."
  fi
fi

# --- launch the public tunnel ---------------------------------------------- #
ARGS=(--ngrok-token "${NGROK_TOKEN}"
      --upstream-host "${UPSTREAM_HOST}"
      --upstream-port "${UPSTREAM_PORT}"
      --proxy-port "${PROXY_PORT}")
[[ -n "${API_KEY}" ]] && ARGS+=(--api-key "${API_KEY}")

exec "${PY}" "${SCRIPT_DIR}/ngrok_tunnel.py" "${ARGS[@]}"
