# Evolving-Benchmarks Evaluation Service

The server behind [`simple_agentic_evals`](../README.md). It hosts the
*executable environment* — the EnterpriseOps-Gym (EoG) Docker gyms with per-task
database seeding, the Agents' Last Exam (ALE) sandbox, the reference agent
harnesses, and the graders — behind one HTTP session API, so a client needs
nothing heavier than `httpx`.

```
            ┌──────────────────────── eval service (this) ─────────────────────┐
 your agent │  POST /v1/sessions ──► seed per-task DB ──► return action surface │
   (BYOA) ──┼─►  act via action  (EOG: MCP tools │ ALE: file sandbox)           │──► shared gyms
            │  POST .../grade    ──► run verifiers against the session's state  │   (already running)
            │  DELETE /session   ──► tear the DB down  (+ TTL reaper safety net)│
            └──────────────────────────────────────────────────────────────────┘
```

The datasets themselves are easy to `load_dataset(...)`. The painful part is
standing up the *world* the tasks run against: 7–9 gym images, per-trial DB
seeding, pod networking, the SQL grader, and a ~97 GB ALE sandbox image. This
service does that once and exposes it over HTTP.

## The session contract

Every benchmark shares one lifecycle; only the **action surface** differs.

```
create_session  ->  agent acts (via `action`)  ->  grade  ->  teardown
```

| `action.type` | benchmark | how the agent acts | grading |
| --- | --- | --- | --- |
| `mcp` | EOG (`eog`) | calls MCP tools on the returned server(s), reverse-proxied through the service to the live gym and bound to a per-session DB | SQL `database_state` verifiers |
| `sandbox` | ALE (`ale`) | fetches `input_files`, works in its own environment, submits an output artifact | the task's own `evaluate()` / `score_outputs.py` |

A client written against `mcp` works for `sandbox` too — only the verbs in the
middle change (MCP tool calls vs. fetch-inputs / submit-artifact).

## Layout

```
server/
├── run.sh                    launch uvicorn (under tini; see "Process reaping")
├── requirements.txt          fastapi, uvicorn, httpx, pydantic
├── ngrok_tunnel.{py,sh}      public tunnel + usage analytics (optional)
├── deploy/eval-service.yaml  Kubernetes Deployment + Service
└── eval_service/
    ├── service.py            the FastAPI app: routes, sessions, jobs, TTL reaper
    ├── contract.py           wire types (actions, task/session views, grades)
    ├── loader.py             dataset discovery + row normalization
    ├── environment.py        EOG and ALE backends (create / grade / teardown)
    ├── resources.py          evolving tools/skills/agents (oracle|accumulative|none)
    ├── usage.py              request telemetry + the HTML dashboard
    ├── eog_react.py          reference ReAct agent, run in an isolated subprocess
    ├── ale_grader.py         ALE workspace staging, inputs, submissions
    ├── _ale_grader_driver.py ALE `evaluate()` under the ALE venv (file shim)
    ├── ale_docker_grader.py  ALE grading inside the real Docker sandbox
    ├── ale_codex_runner.py   ALE solve+grade via ale_run + the stock Codex agent
    ├── _ale_agent/           the no-LLM deployer that stages a submission in-sandbox
    └── _harness/             vendored EoG harness (see below)
```

### The vendored harness

`_harness/` carries three modules copied verbatim from the EnterpriseOps-Gym
research harness — `dataset` (`TaskRow`), `endpoints` (`patch_row`) and
`eog_verifier` (seed / SQL-verify / teardown) — so this checkout runs on its
own. They are the grader, so **fix bugs upstream and re-copy** rather than
editing them here; grading stays bit-for-bit identical to the reference runs.

The *optional* server-side agent runners for the EOG skills and agents tracks
(`evovle_skills.src.agent_runner`, `evovle_agents.src.*`) are not vendored —
they pull in the whole research harness. They are imported lazily and the
corresponding endpoints answer **HTTP 501** with a clear message when absent.
Everything else — sessions, MCP proxying, SQL grading, the reference ReAct
agent, and the full ALE path — works without them.

## Prerequisites

Sessions and grading only need what the benchmark you serve requires; the
service starts either way and reports what it found at `GET /v1/health`.

**Always**

```bash
pip install -r server/requirements.txt
```

**For EOG** — the gym containers must already be running (`docker ps` should
show `enterpriseops-gym-mcp-*`) and the gym checkout must be readable, because
seed-SQL paths in the dataset rows resolve against it:

```bash
export EOG_ROOT=/path/to/EnterpriseOps-Gym
```

Endpoint discovery is automatic: `patch_row` probes `docker ps` for each gym's
bridge IP and the port its inner process actually binds, which is what makes
this work from a container with `/var/run/docker.sock` mounted but no shared
network namespace. Set `EVOVLE_NO_ENDPOINT_DISCOVERY=1` on bare metal where the
dataset's `localhost:<port>` URLs already resolve.

**For ALE** — the ALE repo plus its `uv` venv (py3.12 + `cua_bench`), and the
task data including each task's ground-truth `reference/` directory:

```bash
cd /path/to/agents-last-exam && uv sync
export ALE_ROOT=/path/to/agents-last-exam
```

For tasks whose grader *executes commands* in the box (and for any Codex solve),
also pull the sandbox image — `docker pull agentslastexam/ale-ubuntu22-docker`,
~97 GB. A few task scorers import third-party packages that aren't in the base
ALE venv; install those on demand with
`uv pip install --python $ALE_ROOT/.venv/bin/python <pkg>`.

**Datasets** — the on-disk tree the loader walks:

```
$EVAL_SERVICE_DATA_ROOT/<dataset>/<benchmark>/<domain>/v<k>/{train,test}.jsonl   # eog
$EVAL_SERVICE_DATA_ROOT/<dataset>/<benchmark>/v<k>/{train,test}.jsonl            # ale (flat)
```

where `dataset` is `evovling_tools` | `evovling_skills` | `evovling_agents`.

## Run it

```bash
bash server/run.sh                 # http://0.0.0.0:8077
PORT=9000 bash server/run.sh       # custom port
RELOAD=1 bash server/run.sh        # autoreload for dev
```

Then `curl localhost:8077/v1/health`, browse the OpenAPI docs at `/docs`, or
point the client at it:

```python
from simple_agentic_evals import EvalClient
client = EvalClient("http://localhost:8077")
```

### Process reaping

`run.sh` execs uvicorn under [tini](https://github.com/krallin/tini) in
subreaper mode (`-s`). This is not optional hygiene: agent runs spawn deep
process trees (`ale_run` → `codex` → one `bwrap` per tool call), and any
descendant that outlives its parent reparents to PID 1. If PID 1 never calls
`wait()`, those orphans accumulate as zombies until the PID limit is exhausted
and every `fork()` fails — we have seen ~616,000 of them wedge a pod that
Kubernetes still reported as Ready. Install tini (`apt-get install tini`) or
point `$TINI_BIN` at a static binary; `run.sh` warns and continues without it.

`deploy/eval-service.yaml` encodes the rest of that lesson: tini as PID 1, an
`exec` liveness probe (it has to `fork()`, so it fails precisely when the PID
table is full — which `httpGet` would miss), a Deployment rather than a bare
Pod, and a node-level `podPidsLimit`.

## API

| method & path | purpose |
| --- | --- |
| `GET /v1/health` | liveness, active session count, ALE sandbox availability |
| `GET /v1/benchmarks` | everything on disk: datasets → benchmarks → domains → versions, with counts |
| `GET /v1/tasks` | task ids for a cell (`version` is a stage `1,2,…` **or `full`** = all stages) |
| `GET /v1/resources` | inspect the evolving tools/skills/agents for a task |
| `POST /v1/sessions` | provision a task → `{session_id, task, action, expires_at}` |
| `GET /v1/sessions/{id}` | inspect a session |
| `* /v1/sessions/{id}/mcp/{gym}` | (EOG) reverse-proxy an MCP call to the session's gym, bound to its DB |
| `GET /v1/sessions/{id}/inputs[/{path}]` | (ALE) list / download the staged input files |
| `POST /v1/sessions/{id}/submit` | (ALE) upload the produced artifact(s) |
| `POST /v1/sessions/{id}/run_agent` | run the reference agent for this session's track on the host |
| `GET /v1/sessions/{id}/jobs/{job_id}` | poll a backgrounded agent run |
| `GET /v1/sessions/{id}/run_artifacts` | (ALE) the raw `ale_run` tree for this session, as tar.gz |
| `POST /v1/sessions/{id}/grade` | grade current env state; tears down unless `?keep_alive=true` |
| `DELETE /v1/sessions/{id}` | tear down (free the gym DB / workspace) |
| `GET /v1/usage`, `/v1/usage/dashboard` | request telemetry + an HTML monitor |
| `GET /sdk`, `/sdk/{wheel}` | the client wheel, built from this repo's root on startup |

`POST /v1/sessions` body:

```json
{"dataset": "evovling_tools", "benchmark": "eog", "domain": "hr",
 "version": 1, "split": "test", "task_id": null,
 "resource_mode": "accumulative", "ttl_sec": null}
```

`task_id: null` takes the first task in the split. `domain` is required for
`eog` and omitted for the flat `ale` layout. `version` is a stage index or the
string `"full"`.

### Long runs are polled, not held open

An ALE Docker+Codex solve can take many minutes — longer than a reverse
proxy's per-request timeout, which would surface as a spurious 5xx. Post
`run_agent` with `background: true` to get a `{job_id}` back immediately and
poll `GET .../jobs/{job_id}` until it reports `done`. The client's
`acp_codex_agent` / `react_agent` helpers do this for you.

## Evolving resources & stages

The three datasets are continual-learning benchmarks: over the version axis
(`v1 ⊆ v2 ⊆ …`, one curriculum stage each) a *resource* grows, and each task
sits at the earliest stage that covers it. The two axes move independently.

**Stage** (`version`) — a single stage (`1, 2, …`) or `"full"` for every task
across all stages. Works on `/v1/tasks`, `/v1/sessions` and `/v1/resources`.

**Resource mode** (`resource_mode`) — which slice of the evolving resource to
expose:

| dataset | resource | what `mode` selects |
| --- | --- | --- |
| `evovling_tools` | MCP **tools** (names) | `oracle` = minimal gold set · `accumulative` = the cumulative tool universe at the stage (realistic; has distractors) · `none` |
| `evovling_skills` | **skills** (`SKILL.md` bundles) | `oracle`/`accumulative` = the held-out gold bundle(s), a **skyline** · `none` = empty library (the realistic setting — the agent authors its own) |
| `evovling_agents` | **agents** (`.toml` + skill bundle) | `oracle` = this task's gold specialists · `accumulative` = the pool mounted at the stage (realistic; has distractors) · `none` = orchestrator solo |

Defaults: `accumulative` for tools and agents, `none` for skills. The selected
set rides on every session under `task.resources` (names + bundle metadata);
`GET /v1/resources?...&include_content=true` also inlines the on-disk
`SKILL.md` / `.toml` bodies.

For tools this is **advisory**: the gym MCP server exposes its full surface and
the service does not restrict calls, so the set is an allowlist you enforce on
your own agent.

## ALE grading paths

The service picks one of two paths per task, automatically:

- **local file shim** — for graders that only *read files*, the task's
  `evaluate()` runs in-process against a filesystem session. Fast, no sandbox.
- **docker sandbox** — for graders that *execute commands* in the box
  (`session.run_command`), the service boots the real ALE Linux sandbox, stages
  the submitted artifact with a no-LLM deployer, and lets the task's own scorer
  grade a real run end to end. Adds ~30–60 s.

Of the 150 ALE tasks, **102 are gradable** on Linux: 96 in the stock
docker-supported subset, plus 6 that need a `--privileged` container (4 run an
inner dockerd, 2 use Apptainer). Those 6 are gated behind
`EVAL_SERVICE_ALE_DOCKER_DIND` (`auto` probes once that the host permits
`--privileged`) — **set it to `0` on a shared or public host**, since
`--privileged` has real host-security implications. The remaining 48 target a
Windows sandbox and return **HTTP 501** (`needs_sandbox`); creating sessions,
serving inputs and accepting submissions work for all 150.

## Configuration

| variable | default | purpose |
| --- | --- | --- |
| `HOST`, `PORT` | `0.0.0.0`, `8077` | bind address |
| `EVAL_SERVICE_DATA_ROOT` | `server/data` | the dataset tree |
| `EOG_ROOT` | `server/reference/EnterpriseOps-Gym` | gym checkout (seed-SQL paths resolve against it) |
| `ALE_ROOT` | `server/reference/agents-last-exam` | ALE repo + its `.venv` |
| `EVAL_SERVICE_SESSION_TTL_SEC` | `1800` | session lease before the reaper tears it down |
| `EVAL_SERVICE_MAX_CONCURRENT_SEEDS` | `4` | cap on the heavy DB-seeding op |
| `EVAL_SERVICE_PUBLIC_URL` | — | public base for the MCP proxy URLs behind a tunnel |
| `EVAL_SERVICE_API_KEY` | — | require `Authorization: Bearer` on `/v1/*` (enforced by the tunnel) |
| `EVAL_SERVICE_REACT_PYTHON` | this interpreter | interpreter with `langchain` for the reference ReAct agent |
| `EVAL_SERVICE_SDK_SRC` | repo root | client checkout to build the served wheel from |
| `EVAL_SERVICE_USAGE_LOG` | `eval_service/logs/usage.jsonl` | telemetry JSONL; `none` keeps it in memory |
| `EVAL_SERVICE_ALE_DOCKER` | `auto` | docker-backed ALE grading (`auto`/`1`/`0`) |
| `EVAL_SERVICE_ALE_DOCKER_DIND` | `auto` | the 6 privileged ALE tasks (`auto`/`1`/`0`) |
| `EVAL_SERVICE_ALE_DOCKER_ENDPOINT_MODE` | `container_ip` | how `ale_run` reaches the sandbox; set empty on Docker Desktop |
| `EVAL_SERVICE_ALE_SUCCESS_THRESHOLD` | `1.0` | score at which an ALE task counts as passed |
| `EVOVLE_NO_ENDPOINT_DISCOVERY` | — | `1` disables gym URL rewriting (bare metal) |

## Public access

`ngrok_tunnel.py` is a reverse proxy in front of the app that forwards every
method/path/byte unchanged, logs usage (IP + geo, route, dataset/task/session,
status, latency, grade pass-rate) to SQLite + CSV, and serves a dashboard.

```bash
NGROK_TOKEN=... bash server/ngrok_tunnel.sh                       # tunnel a running service
NGROK_TOKEN=... EVAL_SERVICE_API_KEY=secret bash server/ngrok_tunnel.sh
NGROK_TOKEN=... START_SERVICE=1 bash server/ngrok_tunnel.sh       # boot the service too
```

It prints a public URL to hand to clients as `base_url`. Extra endpoints:
`GET /` (landing page), `/_tunnel/analytics?group_by=day`, `/_tunnel/chart`,
`/_tunnel/counts`.

> **Security.** This API provisions databases and runs graders, and ALE's
> privileged tasks start `--privileged` containers. For any real public
> deployment set `EVAL_SERVICE_API_KEY` (clients then send
> `Authorization: Bearer <key>` or `x-api-key` on every `/v1/*` call except
> `/v1/health`) and set `EVAL_SERVICE_ALE_DOCKER_DIND=0`. Without a key the API
> is open to anyone with the URL.

## Multi-tenancy & safety

- Sessions are isolated by `x-database-id` against the *shared* gym containers,
  so many callers run concurrently without a container each.
- Seeding is capped by `EVAL_SERVICE_MAX_CONCURRENT_SEEDS`.
- A TTL reaper tears down abandoned sessions, so a crashed client can't leak
  gym DBs; live sessions are also torn down on graceful shutdown.
- Background jobs are never reaped mid-run, and finished ones are dropped after
  `EVAL_SERVICE_JOB_RETENTION_SEC` so the in-memory map stays bounded.

## Known limitations

- The MCP action exposes the gym's **full** tool surface. `oracle_tools` and
  the `resource_mode` tool set are advisory allowlists, not enforcement.
- A few EOG tools have a top-level `anyOf` in their JSON schema that the OpenAI
  API rejects. The client's `to_openai_tools` sanitizes this; for CLI agents the
  bundled `mcp_bridge` does the same rewrite in a stdio↔HTTP proxy.
- ALE tasks return 501 when they need a sandbox that isn't available: the 48
  Windows tasks, tasks missing their reference data, a missing image or venv,
  or the 6 privileged tasks when DinD is off.
