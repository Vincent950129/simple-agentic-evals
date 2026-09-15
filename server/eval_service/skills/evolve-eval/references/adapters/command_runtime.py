"""Reviewed command lifecycle used by local official-harness adapters.

The adapter never invokes a shell.  Its manifest pins an argv, source revision,
timeout, and resource-enforcement declaration.  The command must print one JSON
object containing a finite score only after the official grader completes.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

try:  # Installed skill + SDK sidecar.
    from simple_agentic_evals.local_contract import (
        GradeResult, GradingNotSupported, ManagedRuntimeAction,
        TerminalAction, VerifierView,
    )
except ImportError:  # Repository service.
    from eval_service.contract import GradeResult, ManagedRuntimeAction, TerminalAction, VerifierView
    from eval_service.environment import GradingNotSupported


def _redact(value: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s'\"]+",
        r"\1[REDACTED]",
        value,
    )


def _last_object(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("official runtime did not print a JSON result object")


class CommandRuntimeEnvironment:
    def __init__(self, descriptor: Any):
        self.descriptor = descriptor
        self.runtime = dict(descriptor.runtime or {})
        self.kind = descriptor.benchmark

    def health(self) -> dict[str, Any]:
        missing: list[str] = []
        manifest_dir = str(self.descriptor.manifest_path.parent)
        for value in self.runtime.get("required_paths") or []:
            path = Path(str(value).replace("{manifest_dir}", manifest_dir)).expanduser()
            if not path.is_absolute() and self.descriptor.manifest_path:
                path = (self.descriptor.manifest_path.parent / path).resolve()
            if not path.exists():
                missing.append(str(path))
        argv = self.runtime.get("run_command") or []
        if not isinstance(argv, list) or not argv:
            missing.append("runtime.run_command")
        elif not (
            Path(str(argv[0]).replace("{manifest_dir}", manifest_dir)).expanduser().exists()
            or shutil.which(str(argv[0]))
        ):
            missing.append(f"executable:{argv[0]}")
        return {
            "ok": not missing,
            "status": "ready" if not missing else "missing_runtime",
            "missing": missing,
            "resource_enforcement": bool(self.runtime.get("resource_enforcement")),
            "enforced_tracks": list(self.runtime.get("enforced_tracks") or []),
            "verified_enforced_tracks": list(
                self.runtime.get("verified_enforced_tracks") or []
            ),
        }

    def create(self, row: Any, context: dict[str, Any]):
        output = Path(os.environ.get("EVAL_LOCAL_OUTPUT_ROOT", "eval-local-runs")).resolve()
        session = output / "sessions" / (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", row.task_id)[:80]
            + "-" + uuid.uuid4().hex[:10]
        )
        session.mkdir(parents=True, exist_ok=False)
        raw = getattr(row, "raw", None) or {}
        resource = context.get("resource") or {}
        if context.get("dataset") == "evovling_tools":
            effective_tools = list(resource.get("names") or [])
        elif context.get("dataset") == "evovling_agents":
            effective_tools = sorted({
                str(tool)
                for item in resource.get("items") or []
                for tool in item.get("tools") or []
            })
        else:
            effective_tools = [
                str(tool) for tool in (raw.get("software") or raw.get("oracle_tools") or [])
            ]
        context = {**context, "effective_tools": effective_tools}
        enforced_tracks = list(self.runtime.get("verified_enforced_tracks") or [])
        resource_enforced = bool(
            self.runtime.get("resource_enforcement")
            and context.get("dataset") in enforced_tracks
        )
        state = {
            "kind": self.kind,
            "session_root": str(session),
            "task_id": row.task_id,
            "context": context,
            "resource_enforced": resource_enforced,
        }
        (session / "context.json").write_text(
            json.dumps(context, indent=2, sort_keys=True), encoding="utf-8"
        )
        common = {
            "runtime": str(self.runtime.get("name") or self.descriptor.adapter_id),
            "task_id": row.task_id,
            "timeout_sec": int(self.runtime.get("timeout_sec") or 1800),
            "network": str(self.runtime.get("network") or "declared-by-adapter"),
            "resource_enforced": state["resource_enforced"],
        }
        action_type = self.descriptor.action_types[0]
        if action_type == "terminal":
            action = TerminalAction(
                runtime=common["runtime"], session_name=session.name,
                timeout_sec=common["timeout_sec"], network=common["network"],
                resource_enforced=common["resource_enforced"],
            )
        else:
            action = ManagedRuntimeAction(
                **common, credentials=list(self.descriptor.credentials)
            )
        return state, action

    def run_agent(self, row: Any, state: dict[str, Any], request: Any) -> dict[str, Any]:
        root = Path(state["session_root"])
        context = state["context"]
        replacements = {
            "{task_id}": row.task_id,
            "{session_dir}": str(root),
            "{manifest_dir}": str(self.descriptor.manifest_path.parent),
        }
        argv: list[str] = []
        for item in self.runtime["run_command"]:
            value = str(item)
            for old, new in replacements.items():
                value = value.replace(old, new)
            argv.append(value)
        env = os.environ.copy()
        env.update({
            "EVAL_RUNTIME_TASK_ID": row.task_id,
            "EVAL_RUNTIME_SESSION_DIR": str(root),
            "EVAL_RUNTIME_TRACK": str(context["dataset"]),
            "EVAL_RUNTIME_RESOURCE_MODE": str(context["resource_mode"]),
            "EVAL_RUNTIME_RESOURCES_JSON": json.dumps(
                context.get("resource", {}).get("names") or [], separators=(",", ":")
            ),
            "EVAL_RUNTIME_ALLOWED_TOOLS_JSON": json.dumps(
                context.get("effective_tools") or [], separators=(",", ":")
            ),
        })
        if getattr(request, "openai_api_key", None):
            env["OPENAI_API_KEY"] = request.openai_api_key
        started = time.monotonic()
        completed = subprocess.run(
            argv, cwd=root, env=env, text=True, capture_output=True, check=False,
            timeout=float(self.runtime.get("timeout_sec") or 1800),
        )
        secrets = [env.get(name, "") for name in (
            "EVAL_SERVICE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "HF_TOKEN"
        )]
        (root / "stdout.log").write_text(_redact(completed.stdout, secrets), encoding="utf-8")
        (root / "stderr.log").write_text(_redact(completed.stderr, secrets), encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(
                f"official runtime exited {completed.returncode}; see {root / 'stderr.log'}"
            )
        result = _last_object(completed.stdout)
        score_key = str(self.runtime.get("score_key") or "score")
        score = result.get(score_key)
        if (not isinstance(score, (int, float)) or isinstance(score, bool)
                or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
            raise RuntimeError(f"official runtime returned invalid {score_key}: {score!r}")
        score = float(score)
        verifier = str(self.runtime.get("grader_authority") or "official verifier")
        grade = {
            "overall_success": bool(result.get("overall_success", score >= 1.0)),
            "pass_rate": score,
            "n_passed": int(result.get("n_passed", score >= 1.0)),
            "n_total": int(result.get("n_total", 1)),
            "per_verifier": result.get("per_verifier") or [{
                "name": verifier,
                "passed": bool(result.get("overall_success", score >= 1.0)),
                "score": score,
                "comparison_type": "official_runtime",
            }],
            "comparable": bool(state.get("resource_enforced")),
            "resource_enforced": bool(state.get("resource_enforced")),
        }
        (root / "grade.json").write_text(
            json.dumps(grade, indent=2, sort_keys=True), encoding="utf-8"
        )
        return {
            "completed": True,
            "stopped": "done",
            "exit_code": 0,
            "latency_s": round(time.monotonic() - started, 3),
            "runtime_result": result,
            "comparable": bool(state.get("resource_enforced")),
            "resource_enforced": bool(state.get("resource_enforced")),
            "_grade": grade,
        }

    def grade(self, row: Any, state: dict[str, Any]) -> GradeResult:
        path = Path(state["session_root"]) / "grade.json"
        if not path.is_file():
            raise GradingNotSupported(
                "the official runtime has not completed; run the managed agent first"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return GradeResult(
            session_id="", task_id=row.task_id,
            overall_success=bool(raw["overall_success"]),
            pass_rate=float(raw["pass_rate"]), n_passed=int(raw["n_passed"]),
            n_total=int(raw["n_total"]),
            per_verifier=[VerifierView(**item) for item in raw["per_verifier"]],
        )

    def teardown(self, row: Any, state: dict[str, Any]) -> None:
        # Retain logs/results by default. The CLI's explicit cleanup controls
        # deletion, so an aborted or failed smoke remains diagnosable.
        if self.runtime.get("cleanup_on_teardown"):
            shutil.rmtree(state.get("session_root", ""), ignore_errors=True)


def create_environment(descriptor: Any) -> CommandRuntimeEnvironment:
    return CommandRuntimeEnvironment(descriptor)
