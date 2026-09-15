# Validation and promotion

Reject a candidate on any of these conditions:

- source task count differs from the pinned manifest, retained IDs do not exist, or IDs repeat;
- an annotation is empty, accepted software is outside the closed catalog, aliases are broken, or evidence is missing;
- an active tool has zero or multiple owners;
- an emitted capability is absent from its materialized files;
- agent TOML is invalid, a referenced skill/index is missing, or procedure content is dropped;
- canonical prompt/tool fields are missing, task skills are not propagated into agent rows, the lead prompt exposes direct tools, schemas drift, cumulative sets shrink, an oracle is outside its cumulative set, or a task does not use something introduced at its stage;
- fewer than three stages are emitted, or a stage violates its adaptation/test floors;
- two fresh builds have different recursive file lists or bytes;
- a required executable smoke task fails its real verifier.

Write candidates atomically in fresh directories. Preserve failed candidates and reports. Promote only after static validation and every selected smoke task passes through the seed's real outcome authority; a failed promotion must leave any prior validated output unchanged. Static validation alone is `preflight-only`, not validated. A proxy, source/gold metadata preflight, or user approval cannot substitute for executable outcome validation.

The final report must include source pins, catalogs and checksums, exclusions with reasons, annotation evidence, stage/split/capability statistics, the deterministic tree hash, smoke selection and rewards, and promoted destinations.

For general adapters, use `scripts/evolve_core.py reproduce` to compare two fresh complete trees and `validate --run-smoke` to produce the promotion report. `promote` requires that report plus the separate promotion approval recorded in the bundle. Replacing an existing destination requires `--replace` and preserves the old directory as a timestamped backup.
