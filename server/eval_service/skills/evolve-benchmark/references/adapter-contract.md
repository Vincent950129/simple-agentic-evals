# Normalized seed adapter contract

The seed-specific adapter extracts evidence and writes this bundle. It does not rank, stage, split or materialize the evolving datasets; `scripts/evolve_core.py` owns those shared operations.

```text
bundle/
├── spec.json
├── tasks.jsonl
├── approvals.json
├── deviations.json
├── catalogs/
│   ├── tools.json
│   └── skills.json
└── annotations/
    ├── tools.jsonl
    └── skills.jsonl
```

The tools files are required for tools or agents. The skills files are required for skills.

## `spec.json`

```json
{
  "format_version": 1,
  "benchmark": {
    "name": "My Seed Benchmark",
    "domain": "my_seed",
    "source_url": "https://example.org/my-seed",
    "source_commit": "immutable-revision",
    "source_root": "../seed-checkout",
    "expected_tasks": 120
  },
  "tracks": ["tools", "skills", "agents"],
  "seed": 42,
  "constraints": {
    "target_versions": 5,
    "min_versions": 3,
    "min_train": 2,
    "min_test": 5,
    "adapt_ratio": 0.3,
    "min_growth_frac": 0.15,
    "max_growth_frac": 0.35
  },
  "track_constraints": {},
  "grader": {
    "command": ["python", "adapter/run_verifier.py", "--task", "{task_id}"],
    "smoke_task_ids": ["task-001", "task-042", "task-099"],
    "timeout_seconds": 600
  }
}
```

The grader command is an argument array, not a shell string. It runs from the bundle directory and may use `{task_id}`, `{bundle}`, and `{candidate}`. A zero exit status means the configured real verifier passed. Do not put credentials in this file.

`source_root` is an existing checkout or extracted snapshot, expressed relative to the bundle when possible. Every task `source_repo_path` and annotation evidence path must resolve inside it. The engine rejects missing files, traversal, and out-of-range evidence line numbers.

## `tasks.jsonl`

One record per discovered source task:

```json
{"task_id":"task-001","payload":{"task_prompt":"Complete the task...","source_repo_path":"tasks/task-001","artifacts":[],"resource_limits":{},"metadata":{}},"private":{"grader_ref":"graders/task-001"}}
```

`payload` is copied into public dataset rows and must contain a prompt plus `source_repo_path`. Preserve task instructions, descriptions, public inputs, artifacts, resource limits, category, difficulty, tags and execution metadata there. Put gold data, solutions, rubrics, hidden verifier details and secrets under `private`; the engine never copies `private` into rows.

## Catalogs

`catalogs/tools.json` is a closed list:

```json
[
  {
    "name": "canonical-tool",
    "description": "Task-facing capability boundary",
    "agent_procedure": "Complete procedure the owning specialist follows for this capability.",
    "aliases": ["canonical_tool"],
    "owner": "owner-family",
    "provenance": {"method": "source-provided"}
  }
]
```

Every tool, including inactive catalog entries, has exactly one non-empty owner. Aliases cannot collide across tools.
When building agents, provide `agent_procedure` (or a complete `procedure`/`description`) so the engine can re-home the owned capability without losing substantive instructions.

`catalogs/skills.json` is a list with `name`, `title`, `description`, a complete `procedure`, matching-rule metadata, and provenance. Set optional `agent_scope` to `shared` only for a genuinely cross-cutting procedure; otherwise the engine re-homes it to owners by deterministic task/tool co-occurrence. An optional `owner_any` list may record a reviewed explicit owner routing. The procedure is materialized as a held-out `SKILL.md`; universal execution/output rules belong in the public house prompt rather than the skill catalog.

## Annotations

Each selected source task has a non-empty, sorted capability set and evidence for every label:

```json
{"task_id":"task-001","capabilities":["canonical-tool"],"evidence":{"canonical-tool":[{"source_path":"tasks/task-001/instruction.md","field":"instruction","line":12,"rule":"explicit-tool-name","match":"canonical_tool"}]}}
```

Tools prefer explicit oracle annotations. Otherwise match a frozen closed catalog against task-facing prompt/description evidence or direct oracle invocation/import evidence. Skills are deterministic procedural atoms mined from public prompts/descriptions and approved tool/software anchors; stable source metadata may support a match but cannot be its only evidence. Mark evidence with a prompt/description field or `basis: tool-anchor`/`basis: software-anchor`; never use rubrics or gold answers. Agent annotations are not supplied: the engine derives them exactly from the owners of each task's oracle tools and propagates each task's accepted `oracle_skills` into the agent row.

Tasks without accepted annotations may be absent from an axis annotation file; they are reported as excluded for that axis. An annotation row that exists may not have an empty capability set.

## Approvals and deviations

`approvals.json` records all four distinct decisions:

```json
{
  "suitability": {"approved": true, "selected_tracks": ["tools", "skills"]},
  "annotations": {"approved": true, "input_sha256": "hash printed by inspect"},
  "staging": {"approved": true, "plan_sha256": "..."},
  "promotion": {"approved": false}
}
```

`deviations.json` is `[]` for paper-native/source-provided annotations. Each alternative must be approved and auditable:

```json
[
  {
    "track": "tools",
    "paper_default": "source oracle_tool field",
    "adapter": "exact MCP operations invoked by the task",
    "evidence": "operation IDs and application availability metadata",
    "limitation": "applications are guards/owners, not tools",
    "approved": true,
    "approved_at_checkpoint": "annotations"
  }
]
```

Changing a catalog, annotation rule or capability definition invalidates the annotation and staging approvals. Changing tasks, source revision, constraints or split policy invalidates all downstream approvals.

## Canonical emitted contract

The engine normalizes benchmark-specific payloads into the established track interfaces. Skills rows contain `system_prompt`, `user_prompt`, `patcher_prompts`, `prompt_suffix`, and `evaluation`; `_oracle/stripped_system_prompt.txt` is byte-identical to the visible house-rules prompt. Agent rows contain both prompt columns, task `oracle_skills`, tool fields, and the cumulative owner roster. Agent materialization contains `_capabilities/<owner>/SKILL.md`, `_capabilities/_shared/house_rules.md`, `_agents/agents/*.toml`, and matching per-version resources. No accepted skill procedure may disappear during owner materialization.
