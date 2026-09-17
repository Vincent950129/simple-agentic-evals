# simple-agentic-evals

An SDK and self-hostable service for evaluating agents on evolving,
verifier-backed benchmarks. It is inspired by
[openai/simple-evals](https://github.com/openai/simple-evals), but evaluates what
an agent changes in a tool environment or filesystem rather than only its text.
The hosted adapters cover **EOG** (EnterpriseOps-Gym) and **ALE** (Agents' Last
Exam); reviewed local adapters can expose additional benchmarks through the same
contract.

The **Evolving-Benchmarks Evaluation Service** hosts everything executable — the
environment (EoG Docker gyms + the ALE Docker sandbox), the **provided agent
harnesses**, and the grader. This client is a thin wrapper over its HTTP API: you
connect, pick a task, run a harness (or bring your own), and read the score.
The service always owns the benchmark environment and grader. The default
artifact and provided-harness paths need no local Docker gym or ALE image. The
optional local-orchestration path needs only the user's agent CLI; its ALE
sandbox and official evaluator still run on the service.

- **Provided harnesses** (run *on the service*; you pass your OpenAI `api_key`,
  or set `$OPENAI_API_KEY`). The key is **required** — the service hosts the
  environment, the harness and the grader, but the inference is yours to pay
  for, so there is no fallback to the host's credentials. Note this is a
  *different* key from the eval service key that `EvalClient(api_key=...)`
  takes: that one gets you into the service, this one runs the model. Bringing
  your own agent needs no OpenAI key here at all.
  - `react_agent` — EnterpriseOps-Gym's reference **ReAct** agent. **EOG only.**
  - `acp_codex_agent` — **Codex over ACP**. For **EOG** it acts on the task's gym
    MCP tools (skills/agents/tools; the agents track runs the reference multi-agent
    orchestrator). For **ALE** it drives the Codex CLI agent inside the sandbox
    (solve **and** grade).
- **Three metrics, every run** — **accuracy** (`grade().pass_rate`), **latency**
  (`run.latency_s`), **tokens** (`run.total_tokens`); use `task.evaluate(...)` to
  get all three at once as an `EvalReport`.
- **Observability** — `verbose="steps"` / `include_trace=True` surfaces per-step
  thoughts, tool calls, sub-agent (agent) calls, and (at `"full"`) the raw output.
- **Bring your own agent** — `to_openai_tools` turns the gym's MCP tools into
  OpenAI-ready function specs; hand `task.mcp_servers` to any MCP client.
- **Continual-learning metrics** — `ContinualMetrics` (ACC / BWT / FWT / forgetting).
- **Diagnostic detail** — per-task, per-verifier results; ALE also exposes safe
  nested checks plus the public required steps and natural-language rubric.
- **Operations** — per-user request analytics, a separate localhost dashboard,
  key-gated SDK/skill downloads, and hot-reloaded personal token stores.

The repository contains the installable SDK at the root, the deployable service
under [`server/`](server/README.md), current notebooks under [`tutorials/`](tutorials/),
and portable agent skills under `server/eval_service/skills/`. Runtime data,
credentials, analytics databases, generated wheels, and benchmark datasets are
deliberately excluded from Git.

## Install

The service **ships this SDK itself** at `GET /sdk`, so you can install it straight
from a running service — no PyPI account or repo checkout needed:

```bash
BASE=http://localhost:8077        # or your public URL
: "${EVAL_SERVICE_API_KEY:?Get a key from MyAuthtoken first}"
WHEEL=$(curl -sSL -H "Authorization: Bearer $EVAL_SERVICE_API_KEY" "$BASE/sdk" \
  | python -c 'import json,sys; d=json.load(sys.stdin); print(d["path"]) if "path" in d else sys.exit("SDK download denied: " + d.get("detail", "unknown error"))')
curl -fsSL -H "Authorization: Bearer $EVAL_SERVICE_API_KEY" \
  "$BASE$WHEEL" -o "${WHEEL##*/}"
pip install "./${WHEEL##*/}"
```

Or, from a checkout of this repo:

```bash
pip install -e .
```

Both provided harnesses run **on the service**, so you need no gym, no ALE sandbox,
no model client, and no `codex` binary locally. (Only bring-your-own-agent paths
need their own agent runtime.)

## Interactive skill and isolated command runs

For a quick interactive dry run, an agent can read the portable instructions from
`GET /resources/evolve-eval/SKILL.md` (alias: `/resources/skill.md`). Downloading
the Markdown and all `/v1` evaluation calls require an eval key. The installed
wheel separately provides `CommandAgent`, `run_leaderboard`, and the
`evolve-eval` CLI for repeatable fresh-process runs.

The general benchmark-construction skill and its checksummed references are
also key-protected. Install them from
`GET /resources/evolve-benchmark/install.sh`; the installer verifies every file
against `GET /resources/evolve-benchmark/manifest.json`.
It converts a verifier-backed seed suite into evolving tools, skills, or agents
datasets locally; it does not call the hosted evaluation API or publish outputs.

```bash
export EVAL_SERVICE_API_KEY=...                  # do not put this on the command line
: "${EVAL_SERVICE_API_KEY:?Required — sign in at https://mas-orchestra.salesforceresearch.ai/mas_r1/demo/ and open MyAuthtoken}" &&
curl -fsSL -H "Authorization: Bearer $EVAL_SERVICE_API_KEY" \
  "$EVAL_SERVICE_URL/resources/evolve-eval/SKILL.md" -o SKILL.md
evolve-eval leaderboard --adapter codex --name "My Agent" --output ./eval-run
# or any fresh-process CLI that reads its prompt from stdin
evolve-eval leaderboard --agent-command 'my-agent --stdin' --name "My Agent" --output ./eval-run
```

The default is a one-seed smoke test with two tasks per benchmark. Its
`leaderboard.json` is marked `partial`. A publishable run has no task limit and
three seeds; because it can consume many agent-hours and millions of model tokens,
both flags are required:

```bash
evolve-eval leaderboard --adapter codex --name "My Agent" --output ./full-run \
  --full --confirm-full-cost
```

Every task uses a new process and workspace. The prompt arrives on stdin and the
workspace paths are supplied as `EVAL_TASK_JSON`, `EVAL_INPUT_DIR`,
`EVAL_OUTPUT_DIR`, and `EVAL_RESOURCE_DIR`. An optional `usage.json` reports
`total_tokens` and `n_steps`. The three datasets materialize respectively as an
enforced MCP tool allowlist, `SKILL.md` bundles, or agent TOMLs plus
`agent_skills`; Codex also receives task-scoped skill discovery and multi-agent
configuration. Checkpoints support `--resume`, and `--cleanup` removes task
workspaces after success. The key is inherited only through the environment and
is redacted from captured logs.

The Python API applies the same guard: a no-limit `run_leaderboard(...)` call
requires `confirm_full_cost=True`; rehearsal calls with `limit=` do not.
The returned and saved `leaderboard.json` remains a leaderboard row and also
includes `task_results`: benchmark → seed → task rows, with every task's
`per_verifier` measurements retained for diagnosis.

For a same-conversation rehearsal, use `evolve-eval start`, `status`, `grade`,
and `abort`. Interactive results are always partial because they do not provide
fresh-conversation isolation. The CLI writes/prints leaderboard JSON only; it
does not upload or centrally rank results.

## Quickstart

`EvalClient()` resolves its endpoint for you (in order: the `base_url` argument →
`$EVAL_SERVICE_URL` → the URL baked into the served wheel → `http://localhost:8077`),
so there is usually **no URL to paste**:

```python
from simple_agentic_evals import EvalClient, react_agent

client = EvalClient()                                  # endpoint + auth resolved for you
for task in client.tasks("evovling_tools", "eog", domain="hr",
                         version=1, split="test", limit=5):
    with task:                                         # seeds a fresh gym DB
        run = react_agent(task, api_key="sk-...")      # the harness acts on the task
        grade = task.grade()                           # graded; DB torn down on exit
        print(task.task_id, grade.pass_rate, run.latency_s, run.total_tokens)
```

Every dataset ships `train` and `test` splits — pass `split=` to pick one (hold out
`test` to report on), and `version=` to choose an evolving stage (or `"full"` for the
whole corpus):

```python
train_ids = client.task_ids("evovling_tools", "eog", version="full", split="train", domain="hr")
test_ids  = client.task_ids("evovling_tools", "eog", version="full", split="test",  domain="hr")
```

## Three metrics at once

Accuracy, latency, and token usage are reported for **every** run, so you decide
which to report on. They live where they're produced — accuracy on the grade,
latency + tokens on the run:

```python
with client.task("evovling_skills", "eog", 1, task_id, domain="hr") as task:
    run   = acp_codex_agent(task, api_key="sk-...")
    grade = task.grade()
    print("accuracy:", grade.pass_rate)        # from grading
    for verifier in grade.per_verifier:        # EOG and ALE verifier rows
        print(verifier["name"], verifier.get("score"), verifier["passed"])
    print("latency :", run.latency_s, "s")     # wall-clock of the run
    print("tokens  :", run.total_tokens)        # total LLM tokens (in+out)
```

Every grade returns `per_verifier`. Each row includes `name`, `score`, `passed`,
`expected`, `actual`, `comparison_type`, `description`, `details`, and `error`.
For ALE, the public task-card guidance is also available after the task starts:

```python
with task:
    print(task.required_steps)  # ordered task-card agentMustDo entries
    print(task.evaluation)      # public natural-language scoring rubric
```

These fields are task metadata, not measured results; measured component scores
remain in `task.grade().per_verifier`. Hidden reference files and values are not
included.

ALE task evaluators preserve their named top-level components and safe nested
checks. The service validates every capture policy against the evaluator source
and reports capture drift in `/v1/health`. The one currently scalar-only ALE
task returns `ale_score` with `details.granularity == "atomic"`; an unexpected
aggregate fallback is marked `aggregate_fallback` with `extraction_error` rather
than being presented as a complete verifier breakdown. If an evaluator rejects
missing or malformed artifacts before constructing its component report, the
result is an explicit `evaluation_preconditions` row with
`details.granularity == "precondition_failure"`.

`task.evaluate(...)` runs a harness **then grades**, bundling all three into an
`EvalReport`:

```python
with client.task("evovling_tools", "eog", 1, task_id, domain="hr") as task:
    report = task.evaluate(agent="react", api_key="sk-...")
    print(report.accuracy, report.latency_s, report.total_tokens)
    # report.run (AgentRun/CodexRun) and report.grade (GradeResult) are kept too
```

`agent=` is `"react"` (EOG ReAct) or `"acp_codex"` (Codex; EOG + ALE). Extra kwargs
(`model`, `api_key`, `verbose`, `max_steps`/`max_episodes`, …) forward to the harness.

## Provided harnesses

```python
from simple_agentic_evals import EvalClient, react_agent, acp_codex_agent

client = EvalClient()

# EOG tools -> reference ReAct agent
for task in client.tasks("evovling_tools", "eog", domain="hr", version=1, limit=3):
    with task:
        run = react_agent(task, api_key="sk-...")
        print(task.task_id, task.grade().pass_rate, run.latency_s)

# EOG skills / agents -> Codex over ACP
with client.task("evovling_skills", "eog", 1, task_id, domain="hr") as task:
    run = acp_codex_agent(task, api_key="sk-...", model="gpt-5")
    print(task.grade().pass_rate, run.total_tokens)

# ALE -> Codex CLI agent inside the sandbox (solves + grades)
with client.task("evovling_tools", "ale", 1,
                 "legal/agora_governance_classify_instance_1") as task:
    run = acp_codex_agent(task, api_key="sk-...")
    print(task.grade().pass_rate)                # inline sandbox score
```

- `react_agent` → `AgentRun` (`n_calls`, `steps`, `stopped`, `completed`,
  `final_message`, `tools_used`, `latency_s`, `total_tokens`, `trace`). Runs the
  **reference ReAct agent**; the score is comparable to the benchmark's own runs.
  **EOG only** — an ALE task raises `ServiceError` ("ALE requires a CLI agent
  harness — use acp_codex_agent").
- `acp_codex_agent` → `CodexRun` (`n_calls`, `n_exec`, `episodes`, `stopped`,
  `completed`, `thread_id`, `final_message`, `latency_s`, `total_tokens`,
  `n_subagent_spawns`, `trace`). Runs **Codex** in a completion-sentinel loop with
  resume-on-stall; for an `evovling_agents` task it runs the reference multi-agent
  orchestrator; for **ALE** it drives the Codex CLI agent inside the sandbox.

Grading is identical regardless of harness — it reads the environment state (EoG gym
DB / ALE sandbox score), not the transcript.

> **Long runs are polled, not held open.** An ALE Docker+Codex solve (or a slow EOG
> Codex loop) can take several minutes — longer than a reverse-proxy/tunnel's
> per-request timeout. The harness helpers therefore submit the run as a **background
> job** on the service and poll a cheap status endpoint until it finishes, so no
> single request stays open long enough to be cut off with a spurious 5xx. This is
> transparent: you still just call `acp_codex_agent(...)` / `react_agent(...)` and get
> the run back. Pass `verbose="steps"` to see a liveness heartbeat while it runs; raise
> `timeout_s` for very long tasks (the client waits `timeout_s + 300s`).

> The old names `run_eog_agent` / `run_codex_agent` still work as thin deprecated
> aliases (they emit a `DeprecationWarning`); `run_ale_agent` has been removed —
> use `acp_codex_agent` for ALE.

## Observability (verbose / trace)

`verbose` is a level — `False` (default) | `"summary"` | `"steps"` | `"full"`
(`True` == `"steps"`). `include_trace=True` forces the structured per-step `trace`
(auto-on for `steps`/`full`); large tool results are truncated so payloads stay small.

```python
run = acp_codex_agent(task, api_key="sk-...", verbose="steps")
# prints a summary line + per step: thoughts, tool calls (name+args),
# tool results, sub-agent (agent) calls; "full" also prints the raw final output.

# Or capture the trace programmatically without printing:
run = react_agent(task, api_key="sk-...", include_trace=True)
for step in run.trace:
    print(step["thought"], step["tool_calls"])
```

## Bring your own agent

Prefer your own loop? The service still hosts the environment and grader — you just
drive the tools yourself.

**EOG** — `task.mcp_servers` are the gym MCP endpoints (proxied through the service,
bound to your session's DB). Hand them to **any** MCP client; `to_openai_tools`
sanitizes the MCP schemas for OpenAI function-calling:

```python
import json, openai
from simple_agentic_evals import EvalClient, to_openai_tools

client = EvalClient()
oai = openai.OpenAI()                                   # your own OpenAI client

with client.task("evovling_tools", "eog", 1, task_id, domain="hr") as task:
    mcp = task.mcp_session(task.mcp_servers[0])          # minimal MCP client, pre-authed
    tools = to_openai_tools(mcp.list_tools())            # OpenAI-ready function specs
    messages = [
        {"role": "system", "content": task.system_prompt},
        {"role": "user", "content": task.user_prompt},
    ]
    for _ in range(20):                                  # simple tool loop
        resp = oai.chat.completions.create(
            model="gpt-4o", messages=messages, tools=tools)
        msg = resp.choices[0].message
        messages.append(msg)
        if not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            result = mcp.call_tool(tc.function.name, json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)})
    print(task.grade().pass_rate)                        # graded off the mutated DB
```

**ALE** — no gym; fetch the staged inputs, produce the deliverable however you like,
submit it, then grade:

```python
with client.task("evovling_skills", "ale", 3, "legal/legal_dr_fees_01") as task:
    files = task.inputs()                                # list staged input files
    data  = task.fetch_input(files[0]["path"])           # download one
    answer = my_model(data)                              # ... your agent produces the artifact
    task.submit_text(task.output_path, answer)           # submit the deliverable
    print(task.grade().pass_rate)
```

Some ALE tasks need an environment we can't provide here (e.g. Windows-only) and return
HTTP 501; `grade()` then raises `ServiceError` with `.needs_sandbox == True`.

### ALE with local orchestration and a hosted sandbox

This mode is opt-in. `CommandAgent` still uses the artifact workflow unless
`ale_execution="remote_mcp"` is set. In remote mode, each local agent process and
its subagents share one session-scoped hosted ALE filesystem through the
`ale_sandbox` MCP server; after the process exits, `Task.grade()` closes MCP and
runs the unchanged official evaluator once.

```python
from simple_agentic_evals import CommandAgent, EvalClient

client = EvalClient()
task = client.task("evovling_agents", "ale", "full",
                   "business_finance/american_option_pricing_ls")
agent = CommandAgent(adapter="codex", ale_execution="remote_mcp",
                     output_dir="./ale-remote-run")

with task:
    run = agent(task)             # local Codex/subagents, hosted ALE MCP
    grade = task.grade()          # official aggregate + per_verifier
    print(grade.pass_rate, grade.execution_mode)
```

For manual orchestration, call `task.start_remote_sandbox()` and read
`task.remote_mcp_server`; `task.remote_sandbox_status()` reports provisioning and
execution state. The server must be enabled by its operator. The eval key is
supplied to child agents only through the environment and is not written to task
files or captured logs.

## Continual learning — advanced (the evolving axis)

Each dataset evolves a resource (tools / skills / agents). Choose a stage (`version`, or
`"full"`) and how much of the resource to expose (`resource_mode`):

```python
# attach the stage's set to each session (rides on task.resources)
for task in client.tasks("evovling_tools", "eog", domain="hr", version=2,
                         resource_mode="accumulative"):
    ...  # task.resources -> {"kind", "mode", "count", "names", ...}

# or inspect it without a session (SKILL.md / .toml bodies via include_content=True)
client.resources("evovling_skills", "ale", version=3, mode="oracle",
                 task_id="legal/legal_dr_fees_01")
```

`resource_mode` is `oracle` (gold), `accumulative` (the realistic growing set), or
`none` (baseline).

Score a sweep client-side with the bundled `ContinualMetrics` (ACC / BWT / FWT /
forgetting) — the metrics the research runs report:

```python
from simple_agentic_evals import ContinualMetrics, StageResult

m = ContinualMetrics(num_stages=n)
for k, acc in enumerate(per_stage_scores):        # R[k][k] diagonal
    m.record(StageResult(eval_stage=k, adapt_stage=k, num_tasks=x, success_rate=acc))
print(m.print_report())                           # ACC, BWT, FWT, forgetting
```

## Tutorials

- [`tutorials/evolve_eval_colab_tutorial_quick_start.ipynb`](tutorials/evolve_eval_colab_tutorial_quick_start.ipynb)
  is the shortest end-to-end path.
- [`tutorials/evolve_eval_colab_tutorial_quick_start_detail.ipynb`](tutorials/evolve_eval_colab_tutorial_quick_start_detail.ipynb)
  explains the same workflow in more detail.
- [`tutorials/evolve_eval_colab_tutorial_full.ipynb`](tutorials/evolve_eval_colab_tutorial_full.ipynb)
  covers EOG, ALE task inputs, public steps/rubrics, and detailed verifier results.
- [`evolve_eval_colab_tutorial.ipynb`](evolve_eval_colab_tutorial.ipynb) remains a
  compatibility copy of the full tutorial.

The setup cells send `Authorization: Bearer $EVAL_SERVICE_API_KEY` when fetching
`/sdk`; this is required because SDK and skill downloads are gated just like the
evaluation API.

## Self-hosting

The SDK is lightweight; the service is not. A service host needs the benchmark
datasets plus the EOG gyms and/or ALE checkout and sandbox image. Once those are
available:

```bash
pip install -e .
pip install -r server/requirements.txt
export EVAL_SERVICE_DATA_ROOT=/data/evolving-benchmarks
export EOG_ROOT=/data/EnterpriseOps-Gym                 # when serving EOG
export ALE_ROOT=/data/agents-last-exam                  # when serving ALE
bash server/run.sh                                      # 0.0.0.0:8077
```

For public access, run `bash server/ngrok_tunnel.sh` with `NGROK_TOKEN` and a
shared `EVAL_SERVICE_API_KEY`, or mount personal tokens at
`server/eval_service/auth/authtokens.db`. The public proxy authenticates `/v1/*`,
`/sdk`, and `/resources/*`; personal-key activity is attributed to its non-secret
user label and updates `last_used`. See the [server documentation](server/README.md)
for configuration, security boundaries, dashboard setup, ALE verifier capture,
and deployment notes.

## API surface

- `EvalClient(base_url=None, timeout=1800, api_key="", extra_headers=None)` — `base_url`
  left unset resolves automatically (arg → `$EVAL_SERVICE_URL` → served-wheel default →
  `http://localhost:8077`).
  - `.health()`, `.benchmarks()`
  - `.task_ids(dataset, benchmark, version, split="test", domain=None, limit=None, offset=0)`
  - `.tasks(...)` → iterator of `Task`; `.task(..., task_id=...)` → one `Task`
  - `.resources(dataset, benchmark, version, task_id=None, split="test", domain=None, mode=None, include_content=True)`
- `Task` (context manager): `benchmark`, `dataset`, `system_prompt`, `user_prompt`,
  `required_steps`, `evaluation`, `oracle_tools`, `resources`, `action_type`,
  `mcp_servers`, `mcp_url(server)`,
  `mcp_session(server)`, `inputs()`, `fetch_input(path)`, `submit(...)`,
  `submit_text(path, text)`, `output_path`, `grade(keep_alive=False)` → `GradeResult`,
  `start_remote_sandbox()`, `remote_sandbox_status()`, `remote_mcp_server`,
  `evaluate(agent="acp_codex", *, keep_alive=False, **kw)` → `EvalReport`
- `GradeResult`: `pass_rate`, `overall_success`, `n_passed`, `n_total`,
  `per_verifier` (named score/pass/detail rows for EOG and ALE), optional
  `execution_mode`
- `CommandAgent(..., ale_execution="artifact" | "remote_mcp")`; the default is
  the existing local-artifact workflow.
- `EvalReport`: `accuracy`, `latency_s`, `total_tokens`, `input_tokens`,
  `output_tokens`, `overall_success`, `agent`, `run`, `grade`
- `BenchmarkReport`: headline metrics plus `.task_results` (one row per task,
  including `per_verifier`) and `.verifier_results` (flattened task × verifier rows)
- `MCPSession`: `list_tools()`, `call_tool(name, arguments)`, `close()`
- `ServiceError`: raised on any non-2xx response — `.status_code`, `.detail`,
  `.needs_sandbox` (True for ALE 501s). Lets you handle errors without importing `httpx`.
- **Provided harnesses (run on the service):**
  - `react_agent(task, *, model=None, api_key=None, max_steps=None, restrict_to_selected_tools=False, timeout_s=1800.0, verbose=False, include_trace=None)` → `AgentRun` (EOG only)
  - `acp_codex_agent(task, *, model=None, api_key=None, transport=None, allowed_tools=None, mcp_only=None, max_episodes=4, timeout_s=None, require_completion=True, completion_sentinel="TASK_COMPLETE", verbose=False, include_trace=None)` → `CodexRun` (EOG + ALE). `timeout_s=None` defaults per benchmark: 900s for EOG, 9000s for ALE (reference-parity; the agent phase gets 7200s).
  - Deprecated aliases: `run_eog_agent` → `react_agent`, `run_codex_agent` → `acp_codex_agent`
- **MCP↔OpenAI bridge & metrics:**
  - `to_openai_tools(mcp_tools, *, max_desc=1024)` → OpenAI function-tool specs; `sanitize_tool_schema(schema)` → `(schema, hints)`
  - `ContinualMetrics(num_stages)`: `.record(StageResult)`, `.compute()`, `.print_report()`; `StageResult(eval_stage, adapt_stage, num_tasks, success_rate, …)`
