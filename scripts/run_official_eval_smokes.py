#!/usr/bin/env python3
"""Run the five credentialed official smokes and update acceptance.json.

This command is intentionally separate from deterministic notebook validation:
it can spend model credits, download the selected APEX world, and run Hyper-tau
for hours. Infrastructure failures are recorded with a null grade, never zero.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
DRY = REPO / "eval_dry_run"
sys.path.insert(0, str(REPO / "eval_service" / "sdk"))

from simple_agentic_evals import EvalClient, inspect_adapter, serve_local  # noqa: E402


SMOKES = {
    "eog": {
        "task_id": "task_20251223_184833_888_a8eea1c0_87c53cb9",
        "version": 1, "split": "test", "domain": "hr", "agent": "react",
        "credentials": ["OPENAI_API_KEY"],
    },
    "ale": {
        "task_id": "business_finance/american_option_pricing_ls",
        "version": 1, "split": "test", "domain": None, "agent": "codex",
        "credentials": ["OPENAI_API_KEY"],
    },
    "terminal-bench-2": {
        "task_id": "cancel-async-tasks", "version": 1, "split": "test",
        "domain": None, "agent": "codex", "manifest": "tb2.json",
        "credentials": ["OPENAI_API_KEY"],
    },
    "apex-agents": {
        "task_id": "task_b78c4510be784e6a8b8f0394aafd785d",
        "version": 2, "split": "train", "domain": None, "agent": "codex",
        "manifest": "apex-agents.json", "credentials": ["HF_TOKEN", "OPENAI_API_KEY"],
    },
    "hyper-tau": {
        "task_id": "034_banking_knowledge_construction_client_api_deposit_opening",
        "version": 1, "split": "test", "domain": None, "agent": "codex",
        "manifest": "hyper-tau.json",
        "credentials": ["OPENAI_API_KEY"],
    },
}


def _redact(text: str) -> str:
    for name in ("EVAL_SERVICE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "HF_TOKEN"):
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def _local_client(benchmark: str, manifest_name: str, port: int, output: Path) -> tuple[EvalClient, dict[str, Any]]:
    manifest = (
        REPO / "eval_service/skills/evolve-eval/references/adapters" / manifest_name
    )
    info = inspect_adapter(manifest)
    approved = {info["manifest_sha256"], info["entrypoint_sha256"]}
    threading.Thread(
        target=serve_local,
        kwargs={"manifest": manifest, "port": port, "approved_sha256": approved,
                "output_root": output},
        daemon=True,
    ).start()
    client = EvalClient(base_url=f"http://127.0.0.1:{port}", api_key="loopback")
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if client.health().get("ok"):
                return client, info
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"{benchmark} loopback sidecar did not become healthy")


def run_one(benchmark: str, spec: dict[str, Any], index: int) -> dict[str, Any]:
    output = DRY / "runs" / "official" / benchmark
    output.mkdir(parents=True, exist_ok=True)
    missing = [name for name in spec["credentials"] if not os.environ.get(name)]
    if missing:
        result = {"task_id": spec["task_id"], "status": "blocked_credentials",
                  "missing_credentials": missing, "grade": None}
        (output / "smoke-result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    started = time.time()
    info: dict[str, Any] = {}
    try:
        if spec.get("manifest"):
            client, info = _local_client(benchmark, spec["manifest"], 19100 + index, output)
        else:
            client = EvalClient(
                base_url=os.environ.get("EVAL_SERVICE_URL", "http://127.0.0.1:8077"),
                api_key=os.environ.get("EVAL_SERVICE_API_KEY", ""), timeout=60,
            )
        task = client.task(
            "evovling_tools", benchmark, spec["version"], spec["task_id"],
            split=spec["split"], domain=spec["domain"], resource_mode="accumulative",
        )
        with task:
            report = task.evaluate(
                agent=spec["agent"], api_key=os.environ["OPENAI_API_KEY"],
                keep_alive=True,
            )
            grade = report.grade
            if grade is None:
                raise RuntimeError("official runtime returned no grade")
            result = {
                "task_id": spec["task_id"], "status": "passed",
                "grade": grade.pass_rate, "overall_success": grade.overall_success,
                "duration_s": round(time.time() - started, 3),
                "per_verifier": grade.per_verifier,
                "runtime_validation": "official agent and official grader completed",
                "comparable": (
                    "evovling_tools" in info.get("comparable_tracks", [])
                    if info else True
                ),
                "adapter_manifest_sha256": info.get("manifest_sha256"),
                "adapter_entrypoint_sha256": info.get("entrypoint_sha256"),
            }
    except Exception as exc:  # infrastructure failure is not an agent score
        result = {
            "task_id": spec["task_id"], "status": "infrastructure_failure",
            "grade": None, "duration_s": round(time.time() - started, 3),
            "error": _redact(f"{type(exc).__name__}: {exc}"),
            "infrastructure_failure_is_not_score_zero": True,
        }
    (output / "smoke-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark", action="append", choices=sorted(SMOKES),
        help="Run only this smoke; repeat to select more than one.",
    )
    args = parser.parse_args()
    selected = args.benchmark or list(SMOKES)
    results = {
        benchmark: run_one(benchmark, SMOKES[benchmark], index)
        for index, benchmark in enumerate(selected)
    }
    report_path = DRY / "reports" / "acceptance.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.setdefault("official_smokes", {}).update(results)
    statuses = {item["status"] for item in report["official_smokes"].values()}
    report["overall_status"] = (
        "passed" if statuses == {"passed"} else
        "official_smokes_blocked" if statuses <= {"passed", "blocked_credentials"}
        else "official_smoke_infrastructure_failure"
    )
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if statuses == {"passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
