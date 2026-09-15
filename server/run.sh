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
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SCRIPT_DIR}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8077}"
PY="${PYTHON:-python3}"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

if ! "${PY}" -c 'import simple_agentic_evals' 2>/dev/null; then
  echo "WARNING: ${PY} cannot import the checkout's simple_agentic_evals package." >&2
  echo "         Install with 'pip install -e .' or set PYTHON to that environment." >&2
fi

# The reference EOG ReAct agent imports langchain, which the service's own
# interpreter need not carry -- without this the agent dies with
# "No module named 'langchain_core'". Point EVAL_SERVICE_REACT_PYTHON at an
# interpreter that has it (the reference gym's venv is the usual choice).
_react_ok() {
  local candidate
  candidate="$(command -v "$1" 2>/dev/null || true)"
  [[ -n "${candidate}" && -x "${candidate}" ]] && "${candidate}" -c \
    'from langchain_core.messages import HumanMessage; from langchain_openai import ChatOpenAI' \
    2>/dev/null
}

if [[ -n "${EVAL_SERVICE_REACT_PYTHON:-}" ]] && ! _react_ok "${EVAL_SERVICE_REACT_PYTHON}"; then
  echo "WARNING: EVAL_SERVICE_REACT_PYTHON=${EVAL_SERVICE_REACT_PYTHON} lacks ReAct dependencies; probing instead." >&2
  unset EVAL_SERVICE_REACT_PYTHON
fi
if [[ -z "${EVAL_SERVICE_REACT_PYTHON:-}" ]]; then
  for cand in "${EOG_ROOT:-${SCRIPT_DIR}/reference/EnterpriseOps-Gym}/.venv/bin/python" "${PY}"; do
    if _react_ok "${cand}"; then
      export EVAL_SERVICE_REACT_PYTHON="${cand}"
      echo "EOG ReAct interpreter: ${cand}"
      break
    fi
  done
fi
if [[ -z "${EVAL_SERVICE_REACT_PYTHON:-}" ]]; then
  echo "WARNING: no interpreter with langchain-core + langchain-openai found; EOG ReAct runs will fail." >&2
fi

export EVOVLE_CODEX_MODEL="${EVOVLE_CODEX_MODEL:-gpt-5}"

CMD=("${PY}" -m uvicorn eval_service.service:app --host "${HOST}" --port "${PORT}")
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

echo "Starting Evaluation Service on http://${HOST}:${PORT}  (cwd=${SCRIPT_DIR}, python=$(command -v "${PY}"))"
exec "${CMD[@]}"
