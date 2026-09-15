from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ENGINE = (
    Path(__file__).resolve().parents[1]
    / "server/eval_service/skills/evolve-benchmark/scripts/evolve_core.py"
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture_bundle(root: Path, tracks: list[str] | None = None) -> Path:
    tracks = tracks or ["tools", "skills", "agents"]
    bundle = root / "bundle"
    source = bundle / "source"
    _write_json(bundle / "spec.json", {
        "format_version": 1,
        "benchmark": {
            "name": "Unseen Fixture",
            "domain": "unseen_fixture",
            "source_url": "https://example.invalid/unseen",
            "source_commit": "fixture-commit",
            "source_root": "source",
            "expected_tasks": 21,
        },
        "tracks": tracks,
        "seed": 42,
        "constraints": {
            "target_versions": 3,
            "min_versions": 3,
            "min_train": 1,
            "min_test": 1,
            "adapt_ratio": 0.3,
            "min_new_tasks": 2,
            "min_growth_frac": 0.1,
            "max_growth_frac": 0.34,
        },
        "grader": {
            "command": [sys.executable, "-c", "raise SystemExit(0)"],
            "smoke_task_ids": ["task-00", "task-07", "task-14"],
            "timeout_seconds": 10,
        },
    })
    _write_json(bundle / "approvals.json", {
        "suitability": {"approved": True, "selected_tracks": tracks},
        "annotations": {"approved": True},
        "staging": {"approved": False},
        "promotion": {"approved": False},
    })
    _write_json(bundle / "deviations.json", [])

    tasks = []
    tool_annotations = []
    skill_annotations = []
    for index in range(21):
        group = index // 7
        task_id = f"task-{index:02d}"
        task_source = source / "tasks" / task_id
        task_source.mkdir(parents=True, exist_ok=True)
        (task_source / "prompt.md").write_text(
            f"Use tool-{group} and procedure-{group}.\n", encoding="utf-8"
        )
        tasks.append({
            "task_id": task_id,
            "payload": {
                "task_prompt": f"Use tool-{group} and procedure-{group}.",
                "source_repo_path": f"tasks/{task_id}",
                "artifacts": [],
                "resource_limits": {"gpus": 0},
                "metadata": {"fixture": True},
            },
            "private": {"gold": f"must-not-leak-{task_id}"},
        })
        tool = f"tool-{group}"
        skill = f"skill-{group}"
        tool_annotations.append({
            "task_id": task_id,
            "capabilities": [tool],
            "evidence": {tool: [{
                "source_path": f"tasks/{task_id}/prompt.md",
                "field": "prompt",
                "line": 1,
                "rule": "source-provided-oracle-tool",
                "match": tool,
            }]},
        })
        skill_annotations.append({
            "task_id": task_id,
            "capabilities": [skill],
            "evidence": {skill: [{
                "source_path": f"tasks/{task_id}/prompt.md",
                "field": "prompt",
                "line": 1,
                "rule": "approved-procedural-atom",
                "match": skill,
            }]},
        })
    _write_jsonl(bundle / "tasks.jsonl", tasks)
    if "tools" in tracks or "agents" in tracks:
        _write_json(bundle / "catalogs/tools.json", [
            {
                "name": f"tool-{group}",
                "description": f"Fixture tool {group}",
                "aliases": [f"fixture-tool-{group}"],
                "owner": f"owner-{group}",
            }
            for group in range(3)
        ])
        _write_jsonl(bundle / "annotations/tools.jsonl", tool_annotations)
    if "skills" in tracks:
        _write_json(bundle / "catalogs/skills.json", [
            {
                "name": f"skill-{group}",
                "title": f"Fixture skill {group}",
                "description": f"Apply fixture procedure {group}.",
                "procedure": f"Read the task and execute deterministic procedure {group}.",
            }
            for group in range(3)
        ])
        _write_jsonl(bundle / "annotations/skills.jsonl", skill_annotations)
    _approve_annotations(bundle)
    return bundle


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENGINE), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _approve_annotations(bundle: Path) -> None:
    inspection = json.loads(_run("inspect", "--bundle", str(bundle)).stdout)
    approvals = json.loads((bundle / "approvals.json").read_text(encoding="utf-8"))
    approvals["annotations"] = {
        "approved": True,
        "input_sha256": inspection["annotation_input_sha256"],
    }
    approvals["staging"] = {"approved": False}
    approvals["promotion"] = {"approved": False}
    _write_json(bundle / "approvals.json", approvals)


def _approve_staging(bundle: Path, plan: dict) -> None:
    approvals = json.loads((bundle / "approvals.json").read_text(encoding="utf-8"))
    approvals["staging"] = {"approved": True, "plan_sha256": plan["plan_sha256"]}
    _write_json(bundle / "approvals.json", approvals)


def test_general_engine_builds_reproduces_validates_and_promotes_three_tracks(
    tmp_path: Path,
) -> None:
    bundle = _fixture_bundle(tmp_path)

    inspection = json.loads(_run("inspect", "--bundle", str(bundle)).stdout)
    assert inspection["discovered_tasks"] == 21
    assert inspection["tracks"]["agents"]["supportable"] is True

    plan = json.loads(_run("plan", "--bundle", str(bundle)).stdout)
    assert set(plan["tracks"]) == {"tools", "skills", "agents"}
    assert {axis: len(value["stages"]) for axis, value in plan["tracks"].items()} == {
        "tools": 3,
        "skills": 3,
        "agents": 3,
    }
    _approve_staging(bundle, plan)

    first = tmp_path / "candidate-one"
    result = json.loads(
        _run(
            "reproduce", "--bundle", str(bundle), "--output", str(first), "--run-smoke"
        ).stdout
    )
    assert result["reproducible"] is True
    assert result["validation"]["status"] == "validated"
    assert all((first / axis / "manifest.json").is_file() for axis in ("tools", "skills", "agents"))
    assert (first / "skills/_oracle/skills/skill-0/SKILL.md").is_file()
    assert (first / "skills/_oracle/stripped_system_prompt.txt").is_file()
    assert (first / "agents/_agents/agents/owner-0.toml").is_file()
    assert (first / "agents/_capabilities/owner-0/SKILL.md").is_file()
    agent_row = json.loads((first / "agents/v1/train.jsonl").read_text().splitlines()[0])
    assert agent_row["oracle_skills"]
    assert agent_row["system_prompt"].startswith("# Evolving-agents lead orchestrator")
    skill_row = json.loads((first / "skills/v1/train.jsonl").read_text().splitlines()[0])
    assert {"system_prompt", "user_prompt", "patcher_prompts", "prompt_suffix", "evaluation"} <= set(skill_row)
    assert "must-not-leak" not in "".join(
        path.read_text(encoding="utf-8")
        for path in first.rglob("*")
        if path.is_file()
    )

    second = tmp_path / "candidate-two"
    second_result = json.loads(
        _run(
            "reproduce", "--bundle", str(bundle), "--output", str(second), "--run-smoke"
        ).stdout
    )
    assert second_result["tree_hash"] == result["tree_hash"]

    validation_path = tmp_path / "validation.json"
    _run(
        "validate", "--bundle", str(bundle), "--candidate", str(first),
        "--run-smoke", "--report", str(validation_path),
    )
    denied = _run(
        "promote", "--bundle", str(bundle), "--candidate", str(first),
        "--destination", str(tmp_path / "validated"),
        "--validation-report", str(validation_path), check=False,
    )
    assert denied.returncode == 2
    assert "promotion" in denied.stderr

    approvals = json.loads((bundle / "approvals.json").read_text(encoding="utf-8"))
    approvals["promotion"] = {"approved": True}
    _write_json(bundle / "approvals.json", approvals)
    promoted = json.loads(
        _run(
            "promote", "--bundle", str(bundle), "--candidate", str(first),
            "--destination", str(tmp_path / "validated"),
            "--validation-report", str(validation_path),
        ).stdout
    )
    assert promoted["tree_hash"] == result["tree_hash"]


def test_general_engine_supports_skills_only_and_approved_annotation_adapter(
    tmp_path: Path,
) -> None:
    bundle = _fixture_bundle(tmp_path, tracks=["skills"])
    _write_json(bundle / "deviations.json", [{
        "track": "skills",
        "paper_default": "shared source policy sections",
        "adapter": "closed procedural atoms matched against public prompts",
        "evidence": "prompt fields and frozen lexical rules",
        "limitation": "no source-provided skill identifiers",
        "approved": True,
        "approved_at_checkpoint": "annotations",
    }])
    _approve_annotations(bundle)
    inspection = json.loads(_run("inspect", "--bundle", str(bundle)).stdout)
    assert inspection["tracks"]["agents"]["supportable"] is False
    plan = json.loads(_run("plan", "--bundle", str(bundle)).stdout)
    assert set(plan["tracks"]) == {"skills"}
    _approve_staging(bundle, plan)

    candidate = tmp_path / "skills-candidate"
    result = json.loads(
        _run(
            "reproduce", "--bundle", str(bundle), "--output", str(candidate),
            "--run-smoke",
        ).stdout
    )
    assert result["validation"]["passed"] is True
    assert (candidate / "skills/manifest.json").is_file()
    assert not (candidate / "tools").exists()
    assert not (candidate / "agents").exists()


def test_general_engine_refuses_invalid_ownership_and_preflight_promotion(
    tmp_path: Path,
) -> None:
    bundle = _fixture_bundle(tmp_path)
    catalog = json.loads((bundle / "catalogs/tools.json").read_text(encoding="utf-8"))
    catalog[0]["owner"] = ""
    _write_json(bundle / "catalogs/tools.json", catalog)
    invalid = _run("inspect", "--bundle", str(bundle), check=False)
    assert invalid.returncode == 2
    assert "has no owner" in invalid.stderr

    bundle = _fixture_bundle(tmp_path / "preflight")
    spec = json.loads((bundle / "spec.json").read_text(encoding="utf-8"))
    spec.pop("grader")
    _write_json(bundle / "spec.json", spec)
    plan = json.loads(_run("plan", "--bundle", str(bundle)).stdout)
    _approve_staging(bundle, plan)
    candidate = tmp_path / "preflight-candidate"
    built = _run(
        "reproduce", "--bundle", str(bundle), "--output", str(candidate), check=False
    )
    assert built.returncode == 1
    report = json.loads(built.stdout)
    assert report["validation"]["status"] == "preflight-only"
    assert report["validation"]["passed"] is False


def test_general_engine_invalidates_annotation_and_staging_approval_when_evidence_changes(
    tmp_path: Path,
) -> None:
    bundle = _fixture_bundle(tmp_path, tracks=["skills"])
    plan = json.loads(_run("plan", "--bundle", str(bundle)).stdout)
    _approve_staging(bundle, plan)
    evidence = bundle / "source/tasks/task-00/prompt.md"
    evidence.write_text(evidence.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

    result = _run(
        "reproduce", "--bundle", str(bundle),
        "--output", str(tmp_path / "should-not-build"), check=False,
    )
    assert result.returncode == 2
    assert "changed after checkpoint-2 approval" in result.stderr


def test_general_engine_returns_metadata_only_skill_labels_to_checkpoint_two(
    tmp_path: Path,
) -> None:
    bundle = _fixture_bundle(tmp_path, tracks=["skills"])
    rows = [
        json.loads(line)
        for line in (bundle / "annotations/skills.jsonl").read_text().splitlines()
        if line
    ]
    for entries in rows[0]["evidence"].values():
        for evidence in entries:
            evidence["field"] = "internal_metadata"
            evidence["rule"] = "frozen-public-metadata"
            evidence.pop("basis", None)
    _write_jsonl(bundle / "annotations/skills.jsonl", rows)
    _approve_annotations(bundle)

    inspection = json.loads(_run("inspect", "--bundle", str(bundle)).stdout)
    assert inspection["tracks"]["skills"]["evidence_diagnostics"]["metadata_only"] == 1
    assert inspection["repair_problems"]
    denied = _run("plan", "--bundle", str(bundle), check=False)
    assert denied.returncode == 2
    assert "annotation repair required" in denied.stderr
