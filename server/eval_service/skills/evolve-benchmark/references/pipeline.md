# Construction pipeline

Use the paper's construction order for each axis independently:

1. **Annotate:** produce `task_id -> non-empty oracle capability set` from a closed canonical universe, with source evidence for every accepted label.
2. **Rank and release:** count each capability at most once per task, rank by descending task frequency with the canonical name as the tie-break, and release the frequent core before the long tail. Merge thin adjacent stages; do not weaken split floors.
3. **Accumulate:** stage `H_k` contains every capability released at or before `k`; capabilities are never withdrawn.
4. **Date and write:** place a task at the earliest stage covering its full oracle set. It must use at least one capability introduced at that stage. Split deterministically into adaptation (`train`) and evaluation (`test`) tasks.

Run the pipeline independently for tools, skills, and agents. The annotation source differs, but the observable invariants do not:

- `H_1` through `H_K` grow strictly.
- Every oracle set is a subset of its cumulative harness.
- Every placed task depends on its stage's new capabilities.
- Capability names, tie-breaks, JSON ordering, task ordering, and seeded shuffles are deterministic.
- A fresh build never appends to or partially overwrites an accepted dataset.

Carry the complete task instruction, artifacts, resource limits, source repository and commit, category, difficulty, tags, and annotation evidence into the rows. Preserve the seed's executable grader as the outcome authority.
