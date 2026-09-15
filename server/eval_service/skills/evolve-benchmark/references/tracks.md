# Track annotations

## Tools

Prefer a seed's explicit oracle-tool field. When it has none, define a closed task-facing software catalog with canonical names, aliases, binaries, import names, package names, owner family, and baseline status. Accept software only when the task instruction requires it or the oracle solution directly invokes/imports it. Record file, line, task ID, and matching rule. Exclude shell syntax, core utilities, installer plumbing, and verifier/build-only dependencies.

Rows expose the cumulative software pool, never the task's oracle-only pool. Keep `oracle_tools` for analysis and `cummulative_tools` for the harness.

If neither explicit annotations nor defensible task-facing/oracle evidence exists, mark tools unsupported. Do not block an independently supportable skills track.

## Skills

Never ask a model to invent a skill per task. Use one of the established deterministic routes:

- Shared-policy seeds: split substantive procedural sections from the common prompt and remove them from the prompt given to the agent.
- Heterogeneous seeds: define a versioned closed catalog of recurring procedural atoms with canonical descriptions, lexical signatures, and software anchors; match tasks deterministically from public prompts/descriptions and approved software/tool anchors. Metadata may corroborate a prompt-derived match but cannot be the only evidence.

Universal execution/output rules remain in a fixed stripped or house-rules prompt. Materialize held-out `_oracle/skills/<slug>/SKILL.md` and `index.json`. Rows carry `oracle_skills` and `cummulative_oracle_skills`.

The coding agent may propose recurring atoms and procedures for the user's review. After approval, freeze them into a closed catalog and executable matching rules. The adapter that labels tasks must run without model calls; record evidence for every match.

## Agents

Build tools first. Map every active tool to exactly one owner family; missing and duplicate ownership are fatal. A task's `oracle_agents` are exactly the owners of its oracle tools. Rank owner families by task frequency and build an independent cumulative curriculum.

Materialize a routing description, owned-software list, owner `SKILL.md`, portable agent TOML, shared house rules, and full/per-version manifests. Propagate the task's accepted `oracle_skills` into every agent row. Re-home every accepted skill procedure into at least one owner capability, or into the shared baseline only when it is explicitly cross-cutting, without dropping content. The lead prompt must be tool-less and expose exactly the cumulative roster. Rows carry `oracle_agents` and `cumulative_agents`, plus the software union of the cumulative roster.

When tools are unsupported, agents are unsupported. Never invent an agent roster directly from task topics or authors.
