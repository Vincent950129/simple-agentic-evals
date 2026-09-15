---
name: evolve-eval
description: Evaluate a shell-capable agent on hosted or local EvoHarnessBench datasets. Use for guided smoke tests, deployment/self-evolving/task-specific evaluation, continual-learning matrices, or explicitly confirmed leaderboard runs on EOG, ALE, TB2, APEX-Agents, Hyper-tau, and new verifier-backed benchmarks.
---

# Evolve Eval

Use `__BASE_URL__` as the hosted catalog. Keep credentials in environment
variables; never print, persist, place in prompts, or pass them on command lines.

For an evaluation run, read [the guided workflow](references/guided-workflow.md)
and do not skip its four user checkpoints. For a documentation, API, or result
question, answer directly without starting an evaluation; read
[HTTP and SDK](references/http-and-sdk.md), inspect the installed SDK or service
schema when needed, and tailor the smallest working query to the field the user
wants. Then read only the other references relevant to the selected runtime:

- [adapter contract](references/adapter-contract.md) for a new or local benchmark;
- [resource-enforcement gate](references/resource-enforcement.md) for every
  local tools, skills, or agents track;
- [modes and metrics](references/modes-and-metrics.md) for track/mode/matrix choices;
- [runtime examples](references/runtime-examples.md) for EOG, ALE, TB2,
  APEX-Agents, or Hyper-tau;
- [HTTP and SDK](references/http-and-sdk.md) for execution and recovery.

The default OpenAI model is `gpt-5` unless the user explicitly selects another
model or an official benchmark fixes a separate simulator/judge/model-menu seat.

Resolve the target before evaluating:

```bash
evolve-eval targets
evolve-eval resolve-target BENCHMARK --runtime-adapter ./adapter.json
```

Use the hosted endpoint only when its catalog entry says `runnable: true` and
advertises the requested track, mode, action surface, and harness. Otherwise,
validate the user-approved adapter and start a loopback sidecar:

```bash
evolve-eval inspect-adapter ./adapter.json
evolve-eval serve-local --runtime-adapter ./adapter.json --port 8078
```

Run a bounded smoke before any full evaluation. A no-limit or publishable run
requires explicit user confirmation after the exact task count, repetitions,
expected model cost, credentials, runtime, and smoke plan are shown. Never
upload results or adapters automatically. Environment/runtime failures are
errors, never zero scores.

For a local adapter, do not accept `enforced_tracks` as proof. At checkpoint 2,
show the user how selected resources are physically enabled, propose negative
controls for forbidden resources, and ask them to approve the implementation
and tests. Run the approved controls and attach their checksum-bound report.
Only tracks verified by `inspect-adapter` are comparable; otherwise label the
smoke diagnostic and do not offer a full leaderboard run.
