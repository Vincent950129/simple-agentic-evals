# Reviewed runtime examples

These are regression examples, not prerequisites for a new benchmark.

| Benchmark | Preferred runtime | Action surface | Official grader |
|---|---|---|---|
| EOG | healthy hosted adapter | MCP | SQL state verifiers |
| ALE | healthy hosted adapter | artifact sandbox | task `evaluate()` / sandbox scorer |
| Terminal-Bench 2 | local pinned Harbor | terminal container | task verifier reward |
| APEX-Agents | local pinned Archipelago | filtered MCP operations | official snapshot/rubric grading |
| Hyper-tau-bench | local pinned Developer harness | managed runtime | sealed evaluator |

First run `evolve-eval resolve-target NAME`. For EOG/ALE, preserve the hosted
behavior and action semantics. For the three local examples use the reviewed
manifest under `adapters/` as a starting point, update only its local dataset
and source-cache paths, run `inspect-adapter`, show the exact manifest and
entrypoint hashes at checkpoint 2, and wait for approval before `serve-local`.
Select the reviewed examples with `--harness official` or `--harness codex`;
the generic `command` harness is not their official agent port.

Physical enforcement plus passing negative controls, not a claim in prose,
determines comparability. The current APEX adapter enforces the evolving-tools
track through exact MCP operation filtering and carries a checksum-bound proof
report. Its skills/agents tracks, and all TB2/Hyper-tau tracks, remain useful
diagnostic runs but are not leaderboard-comparable until their official
runtimes can enforce and test the selected skill bundles/owner rosters or
software capabilities. Follow [the resource-enforcement gate](resource-enforcement.md)
rather than silently accepting the limitation. `inspect-adapter` reports the
result as `comparable_tracks`; the CLI rejects a full run on any other track
while still allowing a labeled one-task smoke.

Smoke selectors:

- EOG: adapter-selected deterministic task, ReAct harness;
- ALE: adapter-selected runnable task, Codex harness;
- TB2: `cancel-async-tasks` through Harbor (oracle sanity reward must be `1.0`);
- APEX-Agents: `task_b78c4510be784e6a8b8f0394aafd785d` with the smallest
  world archive and one text rubric;
- Hyper-tau: `034_banking_knowledge_construction_client_api_deposit_opening`
  through the official sealed evaluator.

The reviewed APEX adapter keeps the official snapshot/rubric grading code but
defaults both its configurable orchestrator and rubric-judge seats to
`openai/gpt-5`. Override them independently with `EVAL_APEX_MODEL` and
`EVAL_APEX_JUDGE_MODEL`; report both choices with the result.

For Hyper-tau, distinguish the Developer from the agent it constructs. The
default Developer is Codex (`HYPER_TAU_DEVELOPER_HARNESS=codex`) using
`gpt-5`, while `HYPER_TAU_AGENT_MODEL` selects one model from the task's
official constructed-agent menu. The reviewed adapter defaults that model to
OpenAI's `gpt-5.6-sol`, so an OpenAI-only smoke needs only `OPENAI_API_KEY`.
Selecting a model routed through OpenRouter additionally requires
`OPENROUTER_API_KEY`. Report the selected model and provider at checkpoint 3.
The constructed-agent choice is benchmark-mandated and is not a service model
default; do not replace it with a model absent from the task's official menu.

APEX smoke validation must not be described as an agent/judge run unless the
official environment and grader actually completed. Hyper-tau may take hours.
Missing credentials or infrastructure are blocked smokes, never agent scores.
