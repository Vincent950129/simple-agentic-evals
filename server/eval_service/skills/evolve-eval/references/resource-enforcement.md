# Resource-enforcement gate

Use this gate for every local tools, skills, or agents track. The coding agent
must guide the user through it at checkpoint 2; it must not silently downgrade
or claim comparability from manifest text.

## What physical enforcement means

- **Tools:** the runtime exposes only the selected operations/software. A
  forbidden operation is absent from discovery and rejected if called by name.
- **Skills:** only selected skill files are mounted in the agent's readable
  skill roots. Unselected skill paths and alternate ambient roots are absent or
  inaccessible.
- **Agents:** only selected agent definitions can be listed or spawned. A
  direct request for an unselected owner is rejected, and its tools are not
  inherited through another route.

Prompt instructions, names hidden only in the UI, and resources that remain
readable/executable elsewhere do not count. Check preinstalled binaries,
alternate tool endpoints, filesystem paths, inherited agent configuration,
network services, caches, and environment variables for bypasses.

## Interactive approval and test loop

For each requested track:

1. Show the exact resource boundary, the code/configuration that implements
   it, ambient bypasses, and any official-runtime limitation.
2. Propose allowed and forbidden fixtures plus the commands/assertions for all
   advertised modes. Ask the user to approve or revise this plan.
3. After approval, run every control in a fresh session. Do not reuse a session
   that may have loaded a later-stage resource.
4. If a control fails, show the evidence and offer a concrete adapter repair.
   Ask before applying the revised design. Repeat until it passes or the user
   accepts a diagnostic-only run.
5. Write the report below, hash it, place its relative path and hash in
   `runtime.enforcement_evidence`, then run `evolve-eval inspect-adapter`.

The controls required for every track are:

- `allowed_resource_available`
- `forbidden_resource_inaccessible`
- `ambient_bypass_blocked`

Add `task_specific_oracle_only` when task-specific mode is advertised and
`self_evolving_stage_isolation` when self-evolving mode is advertised. Add the
track-specific control: `forbidden_tool_invocation_rejected`,
`forbidden_skill_path_inaccessible`, or `forbidden_agent_spawn_rejected`.

## Checksum-bound report

Use deterministic JSON. Each evidence string must identify the executed test
or retained log/assertion; a promise about future behavior is not evidence.

```json
{
  "format_version": 1,
  "adapter_id": "my-benchmark-pinned-v1",
  "source_commit": "full immutable revision",
  "tracks": {
    "evovling_tools": {
      "status": "passed",
      "modes_tested": ["deployment", "self_evolving", "task_specific"],
      "controls": [
        {
          "id": "allowed_resource_available",
          "passed": true,
          "evidence": "test_resource_boundary.py::test_selected_tool_is_callable"
        },
        {
          "id": "forbidden_resource_inaccessible",
          "passed": true,
          "evidence": "test_resource_boundary.py::test_unreleased_tool_is_not_listed"
        },
        {
          "id": "ambient_bypass_blocked",
          "passed": true,
          "evidence": "test_resource_boundary.py::test_alternate_endpoint_is_unreachable"
        },
        {
          "id": "task_specific_oracle_only",
          "passed": true,
          "evidence": "test_resource_boundary.py::test_oracle_mode_exact_set"
        },
        {
          "id": "self_evolving_stage_isolation",
          "passed": true,
          "evidence": "test_resource_boundary.py::test_future_stage_tool_is_blocked"
        },
        {
          "id": "forbidden_tool_invocation_rejected",
          "passed": true,
          "evidence": "test_resource_boundary.py::test_direct_forbidden_call_fails"
        }
      ]
    }
  }
}
```

Only tracks appearing in `inspect-adapter`'s `comparable_tracks` may proceed to
a full or publishable evaluation. Missing, failed, stale, or tampered proof
keeps the bounded smoke diagnostic and must be stated in the result.
