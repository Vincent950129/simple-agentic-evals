# Guided workflow

Every benchmark, including built-ins, passes four approval checkpoints. Stop
after presenting each checkpoint and wait for the user's approval or revision.

## 1. Suitability and resolution

Inspect `/v1/benchmarks`, source/revision, task splits, prompts, outcome
verifier, artifacts, resource fields, and runtime. Report:

- whether a healthy hosted adapter exists;
- which tools, skills, and agents tracks exist;
- supported action surfaces, harnesses, and modes;
- what can be smoke-tested locally and which credentials are needed;
- anything that prevents a valid, comparable score.

Choose hosted only when the catalog explicitly advertises a healthy runnable
adapter for every requested capability. Otherwise propose a local adapter.

## 2. Runtime and deviations

For a local adapter, show the pinned sources, discovery rules, lifecycle hooks,
grader authority, timeout/isolation/network policy, credential names, resource
materialization and enforcement, adapter file hash, and all deviations. The
user approves the exact adapter code before it is imported. Capability
definition deviations may be approved; verifier authority, isolation,
determinism, and resource enforcement may not be waived for a comparable run.

### Resource-enforcement gate

Do not merely remind the user that enforcement is missing. For each selected
track, inspect the official runtime and propose the smallest concrete adapter
change that mounts/enables only the selected cumulative or oracle resources.
Show a negative-control plan that proves an allowed resource is usable, an
unreleased resource is inaccessible, ambient bypasses are blocked, and the
track-specific forbidden action is rejected. Cover every advertised mode.

Pause here and ask the user to approve or revise both the implementation and
the negative controls. After approval, implement them, run them in fresh
isolated sessions, and write the checksum-bound report described in
[resource enforcement](resource-enforcement.md). Present failed controls and
repair choices conversationally, then ask again before retrying. A manifest
claim, prompt instruction, hidden UI entry, or unexecuted test is not proof.
If a control cannot pass, remove the track from `enforced_tracks`, keep the run
diagnostic, and explain exactly what runtime work remains.

## 3. Evaluation plan

Show exact benchmark(s), dataset track, versions/splits/domains/tasks, mode,
resource mode, harness, repetitions, adaptation command/state directory,
estimated cost and duration, credentials by name, smoke selectors, and output
directory. A full run must be opt-in. A smoke is always labeled partial.

## 4. Results and reporting

Report task grades, verifier diagnostics, infrastructure failures, adapter and
source hashes, completeness, comparability, deviations, timing, token usage,
resource-enforcement report/hash and output paths. For continual learning include the stage matrix and
ACC/BWT/FWT. Ask before promotion, publication, or cleanup. Never turn a
provisioning, agent, timeout, or grader failure into a score of zero.

If approval is declined, preserve the checkpoint and stop without executing
the rejected step.
