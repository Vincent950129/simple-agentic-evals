# Interactive construction workflow

Use this workflow for a seed other than the two exact pinned examples. Ask only questions that change benchmark semantics or authorization. Prefer a small set of concrete choices plus a free-form correction.

## Checkpoint 1 — seed suitability

Inspect the seed without changing it. Establish:

- source URL, immutable revision/checksum, license and required credentials;
- task discovery rule, stable IDs, prompts/descriptions, inputs, artifacts and resource limits;
- executable environment and the outcome authority that produces pass/fail or an equivalent deterministic score;
- existing per-task capability annotations and evidence available from prompts, metadata, executable solutions, traces or tool schemas;
- whether capabilities repeat enough to form at least three stages with viable adaptation/test samples.

Present a compact audit table for tools, skills and agents with `supported`, `repairable`, or `unsupported`. Explain every missing prerequisite. Ask the user to select the tracks and a fresh output destination. Do not require all three tracks: skills can proceed without tools, but agents cannot proceed without a valid tool catalog and ownership partition.

Record the decision in `approvals.json` under `suitability`. If no track has a defensible annotation source, or the seed has no outcome authority, stop with a repair checklist.

## Checkpoint 2 — annotations

Create a closed proposed universe for each selected track. Show:

- canonical names and descriptions;
- deterministic match/derivation rules and at least representative positive evidence;
- task coverage, frequency, exclusions, collisions and ambiguous cases;
- shared house rules versus substantive procedural skills;
- the unique owner of each active tool and the resulting agent roster.

The coding agent may cluster evidence and draft descriptions during this design checkpoint. The committed adapter must be deterministic and must not call a model for task-level labels. Freeze catalogs before staging; changes after this checkpoint require returning here and re-approving.

When the seed lacks the paper's preferred annotation field, offer an evidence-backed adapter rather than silently changing the definition. Record it in `deviations.json` with the paper default, adopted adapter, evidence, limitation, and the user's approval. Workarounds apply only to annotation semantics, never to the grader or validation invariants.

Run `inspect` again after every repair. It must report zero `repair_problems`, zero metadata-only skill labels, and the new `annotation_input_sha256`. Show the changed examples and coverage to the user. Record the accepted hash under `approvals.annotations.input_sha256`; clear staging and promotion approval. If a catalog, rule, or annotation changes later, return to this checkpoint rather than silently carrying the old approval forward.

## Checkpoint 3 — ranking and staging

Normalize the seed according to [adapter-contract.md](adapter-contract.md), then run:

```bash
python "$EVOLVE_BUILD_SKILL_DIR/scripts/evolve_core.py" inspect --bundle "$BUNDLE"
python "$EVOLVE_BUILD_SKILL_DIR/scripts/evolve_core.py" plan --bundle "$BUNDLE" --report "$BUNDLE/reports/plan.json"
```

Present per track: discovered/annotated/retained counts, capability frequencies, deterministic rank, capabilities introduced per version, cumulative sizes, train/test counts, exclusions, and proposed smoke task IDs/commands. Defaults are seed `42`, five target versions, a minimum of three emitted versions, 30% adaptation, and floors of two train plus five test tasks. Thin adjacent stages may merge; floors may not weaken.

Record approval under `approvals.staging`, then create a fresh candidate with `reproduce`, not a one-off build:

```bash
python "$EVOLVE_BUILD_SKILL_DIR/scripts/evolve_core.py" reproduce \
  --bundle "$BUNDLE" --output "$CANDIDATE" \
  --report "$BUNDLE/reports/reproduction.json"
```

Without `--run-smoke`, this intentionally returns nonzero with a reproducible `preflight-only` candidate. That is useful before the environment is ready, but it is not validated.

## Checkpoint 4 — validation and promotion

Run the real verifier only after showing the exact command and selected tasks:

```bash
python "$EVOLVE_BUILD_SKILL_DIR/scripts/evolve_core.py" validate \
  --bundle "$BUNDLE" --candidate "$CANDIDATE" --run-smoke \
  --report "$BUNDLE/reports/validation.json"
```

Report static invariants, recursive hash, each smoke result, exclusions, deviations and output paths. A proxy, source/gold preflight, or judge-free metadata check is not runtime validation.

If validation reports missing schema fields, missing materialized resources, dropped procedures, empty propagated skills, or a failed verifier, repair the adapter/engine and return to the earliest affected checkpoint. Re-run both fresh builds after every repair. Never ask the user to waive these invariants.

Only after a separate user confirmation, record `approvals.promotion` and run:

```bash
python "$EVOLVE_BUILD_SKILL_DIR/scripts/evolve_core.py" promote \
  --bundle "$BUNDLE" --candidate "$CANDIDATE" \
  --destination "$DESTINATION" \
  --validation-report "$BUNDLE/reports/validation.json"
```

If the destination exists, explain that `--replace` moves it to a timestamped backup; use that flag only when the user explicitly approved replacement. Promotion never means publication or upload.
