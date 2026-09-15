# Evolving-Benchmarks Evaluation Service

This directory contains the self-hosted backend for `simple-agentic-evals`. It
provisions isolated benchmark state, exposes each task's action surface, runs
the authoritative grader, and cleans the state up. The client machine needs only
the SDK; Docker gyms, ALE task data, reference harnesses, and graders remain on
the service host.

```text
client -> public auth/analytics proxy (:9077) -> eval API (:8077)
                                                     |
                                  EOG MCP gyms or ALE workspaces

operator -> localhost dashboard (:8078) -> tunnel analytics SQLite
```

The public proxy and local dashboard are separate processes. Port 8078 is never
forwarded through ngrok.

## Included features

- EOG sessions backed by freshly seeded gym databases, MCP proxying, SQL
  verifiers, and teardown.
- ALE file sessions, local or Docker grading, public `required_steps` and
  `evaluation` metadata, and safe per-verifier component/check results.
- Hosted ReAct and Codex harnesses; callers supply their own OpenAI key.
- Evolving tools, skills, and agents in `oracle`, `accumulative`, or `none` mode.
- Background jobs for runs that can outlive reverse-proxy request limits.
- Authenticated SDK wheels and checksummed `evolve-eval` / `evolve-benchmark`
  skill packages.
- Shared-key and hot-reloaded personal-key authentication, per-user request
  analytics, `last_used` updates, and a standalone operator dashboard.
- Checksum-approved local benchmark adapters using the same lifecycle contract.

## Repository layout

```text
server/
├── run.sh                         start FastAPI on :8077
├── ngrok_tunnel.py                auth, public reverse proxy, analytics
├── ngrok_tunnel.sh                optional ngrok launcher
├── requirements.txt               service runtime dependencies
├── deploy/eval-service.yaml       Kubernetes example
└── eval_service/
    ├── service.py                 routes, sessions, jobs, SDK/skill downloads
    ├── loader.py                  dataset catalog and task normalization
    ├── environment.py             EOG, ALE, and registered environments
    ├── authtokens.py              personal-key verification and last_used
    ├── usage.py                   direct-service request telemetry
    ├── dashboard/                 localhost dashboard application
    ├── skills/                    served portable skill packages
    ├── _harness/                  standalone EOG compatibility modules
    ├── ale_*                      ALE staging, runners, manifest, and graders
    └── _ale_runtime_overlay/      eval-owned ALE verifier capture override
```

The SDK source is at the repository root. FastAPI builds a wheel into the
Git-ignored root `dist/` directory when the service starts and the wheel is
missing or stale.

## Prerequisites

Install both the SDK and service dependencies from the repository root:

```bash
pip install -e .
pip install -r server/requirements.txt
```

`server/requirements.txt` includes FastAPI, Uvicorn, HTTPX, Pydantic,
`langchain-core`, `langchain-openai`, and OpenAI. The EOG ReAct runner may use a
separate interpreter selected by `EVAL_SERVICE_REACT_PYTHON`; `run.sh` validates
both required LangChain imports before accepting it.

Datasets use this layout:

```text
$EVAL_SERVICE_DATA_ROOT/<track>/<benchmark>/<domain>/vN/{train,test}.jsonl  # EOG
$EVAL_SERVICE_DATA_ROOT/<track>/<benchmark>/vN/{train,test}.jsonl           # ALE
```

`<track>` is `evovling_tools`, `evovling_skills`, or `evovling_agents`.

For EOG, set `EOG_ROOT` to an EnterpriseOps-Gym checkout and start its gym
containers. A standalone clone uses the upstream-derived modules in `_harness`
for task rows, endpoint discovery, and verification; a monorepo deployment can
use the full `evovle_skills` package automatically.

For ALE, set `ALE_ROOT` to an Agents' Last Exam checkout with its task data and
`uv` environment:

```bash
cd /data/agents-last-exam
uv sync
docker pull agentslastexam/ale-ubuntu22-docker   # needed by sandbox graders
```

The checked-in service manifest reconstructs the official 152-task suite and
serves the 96 tasks measurable by this deployment: 90 ordinary Docker tasks and
6 privileged tasks. Set `EVAL_SERVICE_ALE_DOCKER_DIND=1` only on a trusted host
to enable privileged Docker-in-Docker/Apptainer tasks. The remaining non-Linux
and measurement-excluded tasks are withheld. `/v1/health` reports the exact
task-set and verifier-registry coverage.

## Start the service

```bash
export EVAL_SERVICE_DATA_ROOT=/data/evolving-benchmarks
export EOG_ROOT=/data/EnterpriseOps-Gym
export ALE_ROOT=/data/agents-last-exam
bash server/run.sh
```

The API listens at `http://0.0.0.0:8077`. Override `HOST`, `PORT`, `PYTHON`, or
set `RELOAD=1` for development. `EVOVLE_CODEX_MODEL` defaults to `gpt-5`.

Long agent trees should run under `tini` so abandoned descendants are reaped.
Install it with the system package manager or set `TINI_BIN` to a verified
binary. The Kubernetes example uses `tini` as PID 1 and an exec liveness probe
that also detects PID exhaustion.

## Authentication and public access

The FastAPI process is intended to be reachable locally or behind the supplied
proxy. Start the public proxy with:

```bash
export NGROK_TOKEN=...
export EVAL_SERVICE_API_KEY=...       # shared deployment key
bash server/ngrok_tunnel.sh
```

The proxy authenticates every non-OPTIONS request under `/v1/`, `/sdk`, and
`/resources/`. The landing page, docs, and proxy analytics endpoints remain
operator-configurable/open; the localhost dashboard is explicitly blocked from
the public proxy. Set `EVAL_SERVICE_AUTH_DISABLE=1` only for an intentionally
open private deployment.

Personal tokens can be supplied as SQLite and/or CSV files. By default the
service looks under the ignored directory `server/eval_service/auth/`:

```text
server/eval_service/auth/authtokens.db
server/eval_service/auth/authtokens.csv
```

Override with an OS-path-separator-delimited `EVAL_SERVICE_AUTH_TOKENS`. Stores
hot-reload when their size or mtime changes. Authentication keeps only token
digests in memory, forwards a non-secret identity label as `X-Eval-User`, and
updates the authoritative token's `last_used` at most once per configured
resolution interval. Raw keys are never written to analytics.

Traffic recorded before identity attribution, admitted by a shared key, or sent
directly to an intentionally open service is grouped as `unknown user`.

The FastAPI download routes also validate `/sdk` and `/resources/*` themselves,
so bypassing the public proxy does not expose those artifacts. Direct `/v1/*`
protection is the public proxy's responsibility; do not expose port 8077 to an
untrusted network without an equivalent ingress policy.

## SDK and skill downloads

The manifest and wheel both require the eval key. Download the wheel before
installing because `pip` cannot attach a bearer header to a normal wheel URL:

```bash
BASE=http://127.0.0.1:9077
: "${EVAL_SERVICE_API_KEY:?eval key required}"
WHEEL=$(curl -sSL -H "Authorization: Bearer $EVAL_SERVICE_API_KEY" "$BASE/sdk" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["path"])')
curl -fsSL -H "Authorization: Bearer $EVAL_SERVICE_API_KEY" \
  "$BASE$WHEEL" -o "${WHEEL##*/}"
pip install "./${WHEEL##*/}"
```

Skill endpoints:

| path | purpose |
| --- | --- |
| `/resources/evolve-eval/SKILL.md` | interactive evaluation skill |
| `/resources/evolve-eval/manifest.json` | complete skill manifest/checksums |
| `/resources/evolve-eval/install.sh` | verified skill installer |
| `/resources/evolve-benchmark/SKILL.md` | benchmark-construction skill |
| `/resources/evolve-benchmark/manifest.json` | construction package manifest |
| `/resources/evolve-benchmark/install.sh` | verified construction-skill installer |

An unauthenticated `install.sh` request returns only a safe shell denial stub
that prints where to obtain a key and exits 22. It never returns protected
installer content. Other unauthenticated downloads return a descriptive 401
JSON response and `WWW-Authenticate: Bearer`.

## API contract

Every task follows:

```text
create session -> act through returned action -> grade -> close
```

| method/path | purpose |
| --- | --- |
| `GET /v1/health` | service, ALE task-set, sandbox, and verifier diagnostics |
| `GET /v1/benchmarks` | datasets, benchmarks, domains, versions, capabilities |
| `GET /v1/tasks` | task IDs for a selected cell |
| `GET /v1/resources` | evolving tool/skill/agent resources |
| `POST /v1/sessions` | provision a fresh task environment |
| `GET /v1/sessions/{id}` | session/task/action view |
| `* /v1/sessions/{id}/mcp/{gym}` | EOG MCP action proxy |
| `GET /v1/sessions/{id}/inputs[/{path}]` | list/download ALE inputs |
| `POST /v1/sessions/{id}/submit` | submit ALE outputs |
| `POST /v1/sessions/{id}/run_agent` | run a provided harness in the service |
| `GET /v1/sessions/{id}/jobs/{job}` | poll a background run |
| `POST /v1/sessions/{id}/grade` | run authoritative verifiers |
| `DELETE /v1/sessions/{id}` | release databases/workspaces |

The OpenAPI schema at `/docs` is the source for request fields. Session TTL is
an idle lease renewed on session-scoped calls; `EVAL_SERVICE_SESSION_MAX_LIFETIME_SEC`
is the hard backstop.

## ALE verifier detail

The service does not modify the ALE reference checkout. Its overlay places an
eval-owned `ale_run.tasks.driver` first on `sys.path`, captures the evaluator's
final frame without rerunning it, and writes a short-lived sidecar. Docker and
Codex runners accept that sidecar only when task identity and official score
match. Local-file grading applies the same extractor directly.

`per_verifier` contains named top-level rubric components and safe low-level
checks under `details.checks`. It preserves the official aggregate score,
weights/gates, public expectations, and safe diagnostics while filtering hidden
references, credentials, and private paths. The registry explicitly covers all
96 runnable tasks (95 composite and one genuinely atomic evaluator). Extraction
failure falls back to an explicitly marked aggregate row rather than failing or
silently claiming a complete breakdown.

The public task-card metadata is separate from measured results:

```python
with task:
    print(task.required_steps)
    print(task.evaluation)
    grade = task.grade(keep_alive=True)
    for component in grade.per_verifier:
        print(component["name"], component.get("score"), component["passed"])
        for check in (component.get("details") or {}).get("checks", []):
            print("  ", check["name"], check.get("score"), check["passed"])
```

## Analytics dashboard

The public proxy appends raw requests to:

```text
server/eval_service/.tunnel_analytics/analytics.db
server/eval_service/.tunnel_analytics/analytics.csv
```

Materialized dashboard tables are additive; historical requests without an
identity remain `unknown user`. The direct FastAPI usage log is separately kept
at `server/eval_service/logs/usage.jsonl`.

Start the operator UI independently:

```bash
cd server
python -m eval_service.dashboard.server     # http://127.0.0.1:8078/
```

Override `EVAL_DASHBOARD_HOST`, `EVAL_DASHBOARD_PORT`, or `EVAL_DASHBOARD_DB`.
The UI refreshes automatically and reports per-user calls, routes, datasets,
benchmarks, grades, errors, IP counts, and recent activity.

## Additional local adapters

Set `EVAL_SERVICE_ADAPTER_MANIFESTS` to one or more version-1 adapter manifests.
The service reads metadata without importing code. To execute an adapter, both
its manifest and entrypoint SHA-256 must appear in
`EVAL_SERVICE_APPROVED_ADAPTER_SHA256`. This makes local benchmark code an
explicit operator decision rather than code loaded merely because a file exists.

Reference adapter manifests for Terminal-Bench 2, APEX-Agents, and Hyper-tau are
included in the `evolve-eval` skill. Their generated datasets and large pinned
upstream checkouts are not committed; build/place them under the documented
`data_dry_run/` paths before inspecting or serving those adapters.

## Verification

```bash
pytest -q
python -m compileall -q simple_agentic_evals server/eval_service
bash -n server/run.sh server/ngrok_tunnel.sh
```

Tests that need large external benchmark checkouts are skipped when those assets
are not mounted. Unit tests still cover auth, analytics, SDK/skill gating,
command isolation, leaderboard aggregation, resource materialization, adapter
safety, ALE sidecar matching, verifier capture, and manifest coverage.

## Security notes

- Callers must supply their own model credentials; do not lend a host OpenAI key
  on a public deployment.
- Keep `auth/`, `logs/`, `.tunnel_analytics/`, `dist/`, ALE workspaces, and run
  outputs out of Git. The repository `.gitignore` covers these paths.
- Privileged ALE tasks grant broad container capabilities and default off.
- The service accepts and executes benchmark artifacts; isolate it from unrelated
  host data and review every external adapter hash before approval.
