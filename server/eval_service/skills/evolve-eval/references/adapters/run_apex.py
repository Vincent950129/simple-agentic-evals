#!/usr/bin/env python3
"""Run one pinned APEX-Agents task with operation-level MCP filtering."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

ARCHIPELAGO_PIN = "bcacc2d1e99aa917bbe7f6f663c1551dc6ec7400"
APEX_PIN = "92c86856cf1b11f9833a8a076b3a45a63afa3929"
SERVER = {
    "filesystem": "filesystem_server", "pdfs": "pdf_server",
    "code": "code_execution_server", "spreadsheets": "sheets_server",
    "presentations": "slides_server", "documents": "docs_server",
    "calendar": "calendar_server", "chat": "chat_server", "mail": "mail_server",
    "edgar_sec": "edgar_sec", "fmp": "fmp_server",
}


def _environment_url() -> str:
    explicit = os.environ.get("EVAL_APEX_ENV_URL", "").strip()
    if explicit:
        return explicit
    # The eval service commonly controls a sibling Docker daemon. Published
    # ports are then reachable on that daemon's bridge gateway, not this
    # process's localhost. The same gateway is valid on an ordinary host.
    probe = subprocess.run(
        ["docker", "network", "inspect", "bridge", "--format",
         "{{(index .IPAM.Config 0).Gateway}}"],
        text=True, capture_output=True, check=False,
    )
    gateway = probe.stdout.strip()
    if probe.returncode == 0 and gateway:
        return f"http://{gateway}:8080"
    return "http://localhost:8080"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data_dry_run" / "apex_builder").is_dir():
            return parent
    raise RuntimeError("set APEX_ARCHIPELAGO_ROOT outside the reference checkout")


def _allowed_tool_names(tools: list[str]) -> list[str]:
    """Translate only selected EvoHarness operation IDs to Archipelago IDs."""
    allowed: list[str] = []
    for value in tools:
        owner, sep, operation = str(value).partition(".")
        if not sep or owner not in SERVER or not operation:
            raise RuntimeError(f"unknown APEX operation in resource set: {value!r}")
        allowed.append(f"{SERVER[owner]}_{operation}")
    return sorted(set(allowed))


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--task":
        raise SystemExit("usage: run_apex.py --task TASK_ID")
    task_id = sys.argv[2]
    root = _repo_root()
    source = Path(os.environ.get(
        "APEX_ARCHIPELAGO_ROOT", root / "data_dry_run/apex_builder/cache/archipelago"
    )).resolve()
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    if head != ARCHIPELAGO_PIN:
        raise RuntimeError(f"Archipelago revision mismatch: {head}")
    session = Path(os.environ["EVAL_RUNTIME_SESSION_DIR"])
    # The official example creates environment/.env and Docker/agent outputs.
    # Run it from a session-local source copy so the reviewed checkout remains
    # immutable and every controllable artifact stays inside the dry-run tree.
    runtime_source = session / "archipelago-runtime"
    shutil.copytree(
        source, runtime_source, symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"),
    )
    example = session / "apex-example"
    shutil.copytree(runtime_source / "examples/hugging_face_task", example)
    main_path = example / "main.py"
    text = main_path.read_text(encoding="utf-8")
    text = text.replace('repo_type="dataset"',
                        'repo_type="dataset", revision=os.environ["APEX_SOURCE_COMMIT"]')
    main_path.write_text(text, encoding="utf-8")

    tools = json.loads(os.environ.get("EVAL_RUNTIME_ALLOWED_TOOLS_JSON", "[]"))
    config_path = example / "mcp_config_all_oss_servers.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["allowed_tool_names"] = _allowed_tool_names(tools)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    orchestrator = json.loads((example / "orchestrator_config.json").read_text())
    agent_model = os.environ.get("EVAL_APEX_MODEL", "openai/gpt-5")
    judge_model = os.environ.get("EVAL_APEX_JUDGE_MODEL", "openai/gpt-5")
    orchestrator["model"] = agent_model
    (example / "orchestrator_config.json").write_text(
        json.dumps(orchestrator, indent=2, sort_keys=True), encoding="utf-8"
    )
    grading = json.loads((example / "grading_settings.json").read_text())
    grading["llm_judge_model"] = judge_model
    (example / "grading_settings.json").write_text(
        json.dumps(grading, indent=2, sort_keys=True), encoding="utf-8"
    )
    env = os.environ.copy()
    env.update({
        "APEX_SOURCE_COMMIT": APEX_PIN,
        "EXAMPLE_DIR": str(example),
        "ARCHIPELAGO_DIR": str(runtime_source),
        "ENVIRONMENT_DIR": str(runtime_source / "environment"),
        "AGENTS_DIR": str(runtime_source / "agents"),
        "GRADING_DIR": str(runtime_source / "grading"),
        "ENV_URL": _environment_url(),
    })
    try:
        completed = subprocess.run([sys.executable, str(main_path), task_id], env=env, check=False)
    finally:
        subprocess.run(["docker", "compose", "down", "-v"], cwd=runtime_source / "environment",
                       capture_output=True, check=False)
    grades_path = example / "output" / task_id / "grades.json"
    if completed.returncode or not grades_path.is_file():
        raise RuntimeError("APEX official runtime did not produce grades.json")
    grades = json.loads(grades_path.read_text(encoding="utf-8"))
    score = grades.get("scoring_results", {}).get("final_score")
    if (not isinstance(score, (int, float)) or isinstance(score, bool)
            or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
        raise RuntimeError(f"APEX grade has no finite final_score: {score!r}")
    verifier_rows = [
        {"name": str(item.get("verifier_id")), "passed": float(item.get("score") or 0) >= 1,
         "score": float(item.get("score") or 0), "comparison_type": "apex_official_rubric"}
        for item in grades.get("verifier_results", [])
    ]
    print(json.dumps({"task_id": task_id, "score": float(score),
                      "agent_model": agent_model, "judge_model": judge_model,
                      "overall_success": float(score) >= 1, "per_verifier": verifier_rows,
                      "grades": str(grades_path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
