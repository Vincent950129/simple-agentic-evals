---
name: evolve-benchmark
description: Convert a verifier-backed seed task suite into deterministic EvoHarnessBench-style evolving tools, skills, or agents datasets. Use to audit an unfamiliar seed interactively, define evidence-backed capability adapters, construct and validate supported tracks, or reproduce the pinned Terminal-Bench 2 and APEX-Agents examples.
---

# Evolve Benchmark

Guide the user from a seed benchmark to locally validated evolving datasets. Preserve the seed, work in a separate output directory, and never publish or upload without a separate explicit request.

## Route the request

- For Terminal-Bench 2 at commit `2fd12b88aafdd04a52c298e3940bcb189f9766d6`, read [references/terminal-bench-2.md](references/terminal-bench-2.md) and follow its exact blocks.
- For APEX-Agents at commit `92c86856`, read [references/apex-agents.md](references/apex-agents.md) and follow its exact blocks.
- For every other seed, read [references/interactive-workflow.md](references/interactive-workflow.md), then [references/adapter-contract.md](references/adapter-contract.md). Read [references/tracks.md](references/tracks.md) while designing annotations and [references/validation.md](references/validation.md) before validation or promotion.
- Read [references/pipeline.md](references/pipeline.md) when explaining or checking the paper-aligned construction algorithm.

Do not load either exact example reference for an unrelated seed. TB2 and APEX are regression examples, not templates to copy blindly.

## General workflow

For an unfamiliar seed, pause for four explicit user approvals. Treat every checkpoint as a repair loop, not a one-shot confirmation: diagnose failures, propose a deterministic correction, regenerate the checkpoint report, and ask again. Do not advance while the report says `repair_required`, and do not collapse the approvals into one confirmation.

1. **Seed suitability:** inspect read-only and report the source pin, task/grader shape, evidence, repeated capabilities, supported tracks, gaps, and separate output path. Tools and skills may be supported independently; agents require valid tool annotations and ownership.
2. **Annotations:** propose closed catalogs, deterministic matching rules, evidence samples, exclusions, ambiguous cases, skill procedures, and the exact tool-owner partition. Skill labels must be grounded in public prompts/descriptions or approved tool/software anchors; metadata may support but not replace that evidence. A model may help design a catalog, but the frozen adapter must label every task without model calls.
3. **Ranking and staging:** use the packaged engine to show frequencies, release order, proposed versions, splits, exclusions, and smoke commands. Ask the user to approve this plan before building.
4. **Validation and promotion:** build twice, compare every file, run the seed's real verifier, and present the report. Ask separately before promoting or replacing an accepted destination.

Record each answer in `approvals.json`; record every user-approved annotation departure in `deviations.json`. Annotation approval must store the current `annotation_input_sha256` printed by `inspect`, and staging approval must store `plan_sha256`. Any upstream edit invalidates downstream approval and returns the conversation to the affected checkpoint. Approval may select a deterministic capability definition, such as APEX MCP operations as tools. It may not waive outcome authority, evidence, determinism, cumulative growth, earliest-solvable assignment, split floors, canonical row/materialization schemas, procedure preservation, or runtime validation.

Use `scripts/evolve_core.py` for shared construction mechanics. Seed-specific code should only normalize source data into the adapter contract. A failed or verifier-less build remains `preflight-only`; do not call it validated or promote it.

## Boundaries

- Never modify the source benchmark, append to existing JSONL, or expose gold answers, solutions, rubrics, or verifier internals to the evaluated agent.
- Never invent missing annotations silently. Offer the user evidence-backed choices: provide source annotations, approve a deterministic adapter, skip the unsupported track, or stop.
- Keep every active tool under exactly one owner. Agents are derived from tool owners, not independently invented personas.
- Preserve the existing schema spellings: `oracle_tools`/`cummulative_tools`, `oracle_skills`/`cummulative_oracle_skills`, and `oracle_agents`/`cumulative_agents`.
- Never promote fewer than three non-empty stages or a stage without both adaptation and test tasks at the configured floors.
