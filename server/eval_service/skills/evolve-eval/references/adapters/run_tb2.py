#!/usr/bin/env python3
"""Run one pinned TB2 task through Harbor and print its official reward."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

TB2_PIN = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
HARBOR_PIN = "d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621"


def _repo_root() -> Path:
    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        if (parent / "data_dry_run" / "tb_builder").is_dir():
            return parent
    raise RuntimeError("set TB2_SOURCE_ROOT and HARBOR_BIN outside the reference checkout")


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
        text=True, capture_output=True,
    ).stdout.strip()


def _rewards(root: Path) -> list[float]:
    found: list[float] = []
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in ("reward", "score") and isinstance(child, (int, float)):
                        found.append(float(child))
                    else:
                        stack.append(child)
            elif isinstance(item, list):
                stack.extend(item)
    return [value for value in found if math.isfinite(value) and 0 <= value <= 1]


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--task":
        raise SystemExit("usage: run_tb2.py --task TASK_ID")
    task_id = sys.argv[2]
    root = _repo_root()
    source = Path(os.environ.get(
        "TB2_SOURCE_ROOT", root / "data_dry_run/tb_builder/cache/terminal-bench-2"
    )).resolve()
    harbor_root = Path(os.environ.get(
        "HARBOR_SOURCE_ROOT", root / "data_dry_run/tb_builder/cache/harbor"
    )).resolve()
    harbor = Path(os.environ.get(
        "HARBOR_BIN", root / "data_dry_run/tb_builder/cache/venv/bin/harbor"
    )).resolve()
    if _head(source) != TB2_PIN or _head(harbor_root) != HARBOR_PIN:
        raise RuntimeError("TB2 or Harbor checkout is not at the reviewed revision")
    task = source / task_id
    if not task.is_dir():
        raise RuntimeError(f"unknown TB2 task {task_id!r}")
    session = Path(os.environ["EVAL_RUNTIME_SESSION_DIR"])
    jobs = session / "harbor-jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    agent = os.environ.get("EVAL_TB2_AGENT", "codex")
    command = [
        str(harbor), "run", "--path", str(task), "--agent", agent,
        "--n-concurrent", "1", "--n-attempts", "1", "--jobs-dir", str(jobs),
        "--job-name", f"evolve-eval-{task_id}", "--yes",
    ]
    if agent == "codex":
        command += ["--model", os.environ.get("EVAL_TB2_MODEL", "gpt-5")]
    elif os.environ.get("EVAL_TB2_MODEL"):
        command += ["--model", os.environ["EVAL_TB2_MODEL"]]
    command += ["--env", "tb2_remote_docker:RemoteDockerEnvironment"]
    env = os.environ.copy()
    adapters = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = adapters + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["HARBOR_TELEMETRY"] = "0"
    completed = subprocess.run(command, env=env, check=False)
    rewards = _rewards(jobs)
    if completed.returncode or not rewards:
        raise RuntimeError("Harbor did not complete with a finite official reward")
    score = max(rewards)
    print(json.dumps({"task_id": task_id, "reward": score,
                      "overall_success": score >= 1.0, "harbor_jobs": str(jobs)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
