# simple-agentic-evals

**A lightweight, server-only client for evaluating language models** — inspired by
[openai/simple-evals](https://github.com/openai/simple-evals), but for *agentic*
benchmarks. It currently covers **EoG** (EnterpriseOps-Gym) and **ALE** (Agents'
Last Exam).

The **Evolving-Benchmarks Evaluation Service** hosts everything executable — the
environment (EoG Docker gyms + the ALE Docker sandbox), the **provided agent
harnesses**, and the grader. This client is a thin wrapper over its HTTP API: you
connect, pick a task, run a harness (or bring your own), and read the score.
**Nothing heavy runs on your machine** — no Docker gyms, no ALE sandbox, no
`codex` binary, no datasets to download. The only dependencies are `httpx` and
`pydantic`.

- **Provided harnesses** (run *on the service*; you just pass your OpenAI `api_key`):
  - `react_agent` — EnterpriseOps-Gym's reference **ReAct** agent. **EOG only.**
  - `acp_codex_agent` — **Codex over ACP**. For **EOG** it acts on the task's gym
    MCP tools (skills/agents/tools; the agents track runs the reference multi-agent
    orchestrator). For **ALE** it drives the Codex CLI agent inside the sandbox
    (solve **and** grade). This is the only supported harness for ALE.
- **Three metrics, every run** — **accuracy** (`grade().pass_rate`), **latency**
  (`run.latency_s`), **tokens** (`run.total_tokens`); use `task.evaluate(...)` to
  get all three at once as an `EvalReport`.
- **Observability** — `verbose="steps"` / `include_trace=True` surfaces per-step
  thoughts, tool calls, sub-agent (agent) calls, and (at `"full"`) the raw output.
- **Bring your own agent** — `to_openai_tools` turns the gym's MCP tools into
  OpenAI-ready function specs; hand `task.mcp_servers` to any MCP client.
- **Continual-learning metrics** — `ContinualMetrics` (ACC / BWT / FWT / forgetting).

## Install

The service **ships this SDK itself** at `GET /sdk`, so you can install it straight
from a running service — no PyPI account or repo checkout needed:

```bash
BASE=http://localhost:8077        # or your public URL
WHEEL=$(curl -s "$BASE/sdk" | python -c 'import json,sys; print(json.load(sys.stdin)["path"])')
pip install "$BASE$WHEEL"
```

Or, from a checkout of this repo:

```bash
pip install -e .
```

Both provided harnesses run **on the service**, so you need no gym, no ALE sandbox,
no model client, and no `codex` binary locally. (Only the *bring-your-own-agent*
examples that call OpenAI directly need `pip install openai`.)

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
    print("latency :", run.latency_s, "s")     # wall-clock of the run
    print("tokens  :", run.total_tokens)        # total LLM tokens (in+out)
```

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
    run = acp_codex_agent(task, api_key="sk-...", model="gpt-5-codex")
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

## API surface

- `EvalClient(base_url=None, timeout=1800, api_key="", extra_headers=None)` — `base_url`
  left unset resolves automatically (arg → `$EVAL_SERVICE_URL` → served-wheel default →
  `http://localhost:8077`).
  - `.health()`, `.benchmarks()`
  - `.task_ids(dataset, benchmark, version, split="test", domain=None, limit=None, offset=0)`
  - `.tasks(...)` → iterator of `Task`; `.task(..., task_id=...)` → one `Task`
  - `.resources(dataset, benchmark, version, task_id=None, split="test", domain=None, mode=None, include_content=True)`
- `Task` (context manager): `benchmark`, `dataset`, `system_prompt`, `user_prompt`,
  `oracle_tools`, `resources`, `action_type`, `mcp_servers`, `mcp_url(server)`,
  `mcp_session(server)`, `inputs()`, `fetch_input(path)`, `submit(...)`,
  `submit_text(path, text)`, `output_path`, `grade(keep_alive=False)` → `GradeResult`,
  `evaluate(agent="acp_codex", *, keep_alive=False, **kw)` → `EvalReport`
- `GradeResult`: `pass_rate`, `overall_success`, `n_passed`, `n_total`, `per_verifier`
- `EvalReport`: `accuracy`, `latency_s`, `total_tokens`, `input_tokens`,
  `output_tokens`, `overall_success`, `agent`, `run`, `grade`
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
