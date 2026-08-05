#!/usr/bin/env bash
# Launch the Evolving-Benchmarks Evaluation Service.
#
# Runs from `server/` (this script's directory) so `eval_service` imports as a
# package. See README.md for prerequisites and the full env-var list.
#
# Usage:
#   bash server/run.sh                       # http://0.0.0.0:8077
#   PORT=9000 bash server/run.sh             # custom port
#   RELOAD=1 bash server/run.sh              # autoreload for dev

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8077}"

# The reference EOG ReAct agent imports langchain, which the service's own
# interpreter need not carry -- without this the agent dies with
# "No module named 'langchain_core'". Point EVAL_SERVICE_REACT_PYTHON at an
# interpreter that has it (the reference gym's venv is the usual choice).
if [[ -z "${EVAL_SERVICE_REACT_PYTHON:-}" ]]; then
  for cand in "${EOG_ROOT:-${SCRIPT_DIR}/reference/EnterpriseOps-Gym}/.venv/bin/python"; do
    if [[ -x "${cand}" ]] && "${cand}" -c 'import langchain_core' 2>/dev/null; then
      export EVAL_SERVICE_REACT_PYTHON="${cand}"
      echo "EOG ReAct interpreter: ${cand}"
      break
    fi
  done
fi

CMD=(python3 -m uvicorn eval_service.service:app --host "${HOST}" --port "${PORT}")
if [[ "${RELOAD:-0}" == "1" ]]; then
  CMD+=(--reload --reload-dir eval_service)
fi

# Run under tini in subreaper mode (-s). Agent runs spawn deep trees
# (ale_run -> codex -> one bwrap per tool call); any that outlive their parent
# reparent to the nearest subreaper, or to PID 1 if there is none. When PID 1
# never calls wait() those orphans pile up as zombies until the PID limit is
# gone and every fork() fails. -s makes this process the reaper for its own
# subtree, which works even though it is not PID 1. See README ("Process
# reaping") for how to install it.
TINI="${TINI_BIN:-$(command -v tini || true)}"
if [[ -n "${TINI}" && -x "${TINI}" ]]; then
  CMD=("${TINI}" -s -- "${CMD[@]}")
else
  echo "WARNING: tini not found -- orphaned agent processes will not be reaped." >&2
  echo "         Install it (apt-get install tini) or set \$TINI_BIN." >&2
fi

echo "Starting Evaluation Service on http://${HOST}:${PORT}  (cwd=${SCRIPT_DIR})"
exec "${CMD[@]}"
