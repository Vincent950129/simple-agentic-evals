# Local adapter contract (format version 1)

An adapter bundle is reviewed code plus `adapter.json`. Validate it with
`evolve-eval inspect-adapter`; execution additionally requires approval of the
reported manifest and entrypoint SHA-256 values.

```json
{
  "format_version": 1,
  "benchmark": "my-benchmark",
  "adapter_id": "my-benchmark-pinned-v1",
  "title": "My Benchmark",
  "source_url": "https://example.org/repository",
  "source_commit": "full immutable revision",
  "dataset_roots": {
    "evovling_tools": "data/evovling_tools/my-benchmark",
    "evovling_skills": "data/evovling_skills/my-benchmark",
    "evovling_agents": "data/evovling_agents/my-benchmark"
  },
  "entrypoint": "adapter.py:create_environment",
  "entrypoint_sha256": "64 lowercase hex characters",
  "runnable": true,
  "capabilities": {
    "tracks": ["evovling_tools", "evovling_skills", "evovling_agents"],
    "action_types": ["terminal"],
    "harnesses": ["command", "codex"],
    "modes": ["deployment", "self_evolving", "task_specific"]
  },
  "credentials": [],
  "smoke_task_ids": ["deterministic-small-task"],
  "runtime": {
    "isolation": "container-per-session",
    "network": "disabled",
    "timeout_sec": 1800,
    "resource_enforcement": true,
    "enforced_tracks": ["evovling_tools", "evovling_skills", "evovling_agents"],
    "enforcement_evidence": {
      "path": "reports/resource-enforcement.json",
      "sha256": "64 lowercase hex characters"
    },
    "runtime_artifact_sha256": {
      "run_official.py": "64 lowercase hex characters"
    },
    "grader_authority": "seed benchmark verifier"
  },
  "deviations": []
}
```

The entrypoint factory receives the validated adapter descriptor and returns an
environment with:

```python
class Environment:
    kind = "my-benchmark"
    def health(self) -> dict: ...
    def create(self, row, context): ...
    def run_agent(self, row, state, request): ...
    def grade(self, row, state): ...
    def teardown(self, row, state): ...
```

`create` returns `(opaque_state, dataclass_action)`, makes an isolated session,
and enforces the declared cumulative or oracle resources. `grade` returns
`contract.GradeResult` from the seed benchmark's authoritative verifier.
`teardown` is idempotent. Never import an entrypoint unless its byte hash is
explicitly approved. Dataset rows retain the existing EvoHarnessBench schemas
and full verifier/source provenance.

Action descriptors are discriminated by `type`: `mcp`, `sandbox`, `terminal`,
or `managed_runtime`. Secrets are passed only to the child runtime environment
and redacted from stdout, stderr, results, and task workspaces.

`enforced_tracks` is a claim, not proof. A claimed track becomes comparable
only when `enforcement_evidence` is path-confined, its SHA-256 matches, and all
required negative controls pass. See
[the resource-enforcement gate](resource-enforcement.md). `inspect-adapter`
returns `enforcement_errors` and an empty `comparable_tracks` entry when proof
is missing or incomplete. A checksum mismatch or escaping path invalidates the
bundle.

List every helper script reached by the entrypoint in
`runtime_artifact_sha256`. This closes the gap where the approved entrypoint is
unchanged but an official-runner or enforcement helper is edited afterward.
