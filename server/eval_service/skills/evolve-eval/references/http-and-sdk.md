# HTTP, SDK, recovery, and observability

Install the checksum-verified package from the deployment:

```bash
test -n "$EVAL_SERVICE_API_KEY"
curl -fsSL -H "Authorization: Bearer $EVAL_SERVICE_API_KEY" \
  __BASE_URL__/resources/evolve-eval/install.sh | bash
```

Discover targets with `GET /v1/benchmarks`; inspect resources with
`GET /v1/resources`; create isolated sessions with `POST /v1/sessions`. The
returned action descriptor chooses the port: MCP calls, artifact inputs/submit,
terminal transport, or managed official runtime. Grade with
`POST /v1/sessions/{id}/grade` and always `DELETE /v1/sessions/{id}` on abort.

For a bounded command-agent run:

```bash
evolve-eval leaderboard --benchmarks eog,ale --agent-command 'my-agent --stdin' \
  --name 'My Agent' --output ./eval-run
```

For Codex use `--adapter codex`. Each task gets a new process and workspace.
`EVAL_TASK_JSON`, `EVAL_INPUT_DIR`, `EVAL_OUTPUT_DIR`, `EVAL_RESOURCE_DIR`,
`EVAL_TASK_STATE`, and optional `EVAL_USAGE_JSON` form the command contract.
An adaptation command additionally receives `EVAL_ADAPT_STAGE_JSON` and a
persistent `EVAL_STATE_DIR`.

ALE defaults to the existing local artifact contract. When the deployment
advertises remote ALE MCP and the user explicitly wants local orchestration with
a shared hosted filesystem, use:

```bash
evolve-eval leaderboard --benchmarks ale --adapter codex \
  --ale-execution remote_mcp --name 'My Agent' --output ./eval-run
```

This starts one hosted sandbox per isolated task process. Codex receives a
session-scoped `ale_sandbox` Streamable HTTP MCP server; its subagents use the
same server. Grading first disables that act surface and then runs the official
ALE evaluator once. Never fall back from `remote_mcp` to artifacts silently.

Resume an interrupted leaderboard with identical flags plus `--resume`.
Background service runs return a job id; poll its session job endpoint until
`done` or `error`, then grade. Use the service usage/dashboard endpoints and
the run's per-task stdout/stderr for diagnostics. Logs redact known secrets.
Cleanup is explicit: `evolve-eval cleanup --output ./eval-run`.

## Asking for specific results

Treat the skill as an interactive API guide. When the user asks for a field,
first identify whether they have a one-task `EvalReport`, a `BenchmarkReport`,
a saved `leaderboard.json`, or a raw HTTP session. Show only the corresponding
access path, and offer to format or filter it. This is benchmark-general because
built-in and local adapters normalize verifier output to the same
`per_verifier` schema.

For one evaluated task:

```python
report = task.evaluate(agent="codex", api_key=os.environ["OPENAI_API_KEY"])
for verifier in report.grade.per_verifier:
    print(verifier["name"], verifier["passed"], verifier.get("score"),
          verifier.get("details"))
```

For a benchmark run, use `report.verifier_results` for flattened rows or
`report.task_results` for task-grouped rows. In `leaderboard.json`, detailed
rows live under `task_results[benchmark][seed_index]["tasks"]`, and each task
has `per_verifier`. Raw `POST /v1/sessions/{id}/grade` JSON exposes
`per_verifier` directly. Preserve adapter-specific verifier fields in addition
to the normalized `name`, `passed`, `score`, `comparison_type`, and `details`;
never infer a missing detail or turn an infrastructure error into a verifier.

A full run requires both `--full` and `--confirm-full-cost`. It is never
uploaded automatically.
