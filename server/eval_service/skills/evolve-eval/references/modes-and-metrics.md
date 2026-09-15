# Tracks, modes, and continual-learning metrics

Tracks select what evolves: `evovling_tools`, `evovling_skills`, or
`evovling_agents`. The adapter must enforce the selected resource semantics;
otherwise the result is diagnostic, not leaderboard-comparable.

- **Deployment**: agent state is fixed; stage `i` tests with cumulative
  resources `C_i`.
- **Self-evolving**: adapt on stage `i` train tasks, persist agent state, then
  evaluate. The diagonal is the normal stream; `--matrix` evaluates every
  saved learner state against every seen task stage.
- **Task-specific**: each test task receives only its oracle resources. This is
  a skyline, not a realistic deployment score.

Use deterministic ordering and one isolated environment per task. Adaptation
receives only approved training rows and stage metadata. Never expose test
verifiers, gold answers, oracle resources (except task-specific mode), or
another task's state.

For an accuracy matrix `R[i,j]` (learner after training stage `i`, evaluated on
task stage `j`):

- `ACC = mean(R[K,j])` over evaluated stages;
- `BWT = mean(R[K,j] - R[j,j])` for prior stages;
- `FWT` compares pre-training performance on future stages with the declared
  fixed baseline.

Store the matrix, cell task counts, errors, and baseline alongside ACC/BWT/FWT.
Do not calculate a metric from missing or infrastructure-failed cells.

Leaderboard aggregation is task-weighted across arbitrary benchmarks and is
returned in `benchmark_results`. Legacy flat EOG/ALE fields remain present.
