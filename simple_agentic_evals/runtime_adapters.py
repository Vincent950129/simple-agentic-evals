"""Safe inspection and loopback startup for local benchmark adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .client import EvalClient

_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATASETS = {"evovling_tools", "evovling_skills", "evovling_agents"}
_COMMON_ENFORCEMENT_CONTROLS = {
    "allowed_resource_available",
    "forbidden_resource_inaccessible",
    "ambient_bypass_blocked",
}
_TRACK_ENFORCEMENT_CONTROLS = {
    "evovling_tools": {"forbidden_tool_invocation_rejected"},
    "evovling_skills": {"forbidden_skill_path_inaccessible"},
    "evovling_agents": {"forbidden_agent_spawn_rejected"},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _enforcement_proof(
    manifest: Path,
    raw: dict[str, Any],
    tracks: list[str],
    runtime: dict[str, Any],
) -> tuple[list[str], dict[str, Any], list[str]]:
    """Verify checksum-bound negative controls for each declared enforced track."""
    declared_raw = runtime.get("enforced_tracks")
    declared = list(declared_raw) if isinstance(declared_raw, list) else []
    if len(set(declared)) != len(declared) or not set(declared).issubset(tracks):
        raise ValueError("runtime.enforced_tracks must be unique advertised tracks")
    if declared and not runtime.get("resource_enforcement"):
        raise ValueError("runtime.enforced_tracks requires resource_enforcement: true")

    summary: dict[str, Any] = {
        "status": "not_required" if not declared else "missing",
        "path": None,
        "sha256": None,
        "declared_tracks": declared,
        "verified_tracks": [],
    }
    if not declared:
        return [], summary, []

    errors: list[str] = []
    pointer = runtime.get("enforcement_evidence")
    if not isinstance(pointer, dict):
        errors.append(
            "declared enforced tracks have no checksum-bound runtime.enforcement_evidence"
        )
        return [], summary, errors
    relative = str(pointer.get("path") or "")
    expected = str(pointer.get("sha256") or "")
    if not relative or not _SHA256.fullmatch(expected):
        errors.append("runtime.enforcement_evidence requires path and lowercase SHA-256")
        return [], summary, errors
    report_path = (manifest.parent / relative).resolve()
    try:
        report_path.relative_to(manifest.parent)
    except ValueError as exc:
        raise ValueError("resource-enforcement evidence escapes its adapter bundle") from exc
    if not report_path.is_file():
        errors.append(f"resource-enforcement evidence does not exist: {report_path}")
        return [], summary, errors
    actual = _sha256(report_path)
    if actual != expected:
        raise ValueError(
            f"resource-enforcement evidence checksum mismatch: expected {expected}, actual {actual}"
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("resource-enforcement evidence must be UTF-8 JSON") from exc
    summary.update({"status": "failed", "path": str(report_path), "sha256": actual})
    if report.get("format_version") != 1:
        errors.append("resource-enforcement evidence format_version must be 1")
    if report.get("adapter_id") != raw.get("adapter_id"):
        errors.append("resource-enforcement evidence adapter_id does not match")
    if report.get("source_commit") != raw.get("source_commit"):
        errors.append("resource-enforcement evidence source_commit does not match")
    report_tracks = report.get("tracks")
    if not isinstance(report_tracks, dict):
        errors.append("resource-enforcement evidence tracks must be an object")
        report_tracks = {}
    unexpected = set(report_tracks) - set(declared)
    if unexpected:
        errors.append(
            "resource-enforcement evidence contains undeclared tracks: "
            + ", ".join(sorted(unexpected))
        )
    global_error_count = len(errors)

    modes = set((raw.get("capabilities") or {}).get("modes") or [])
    verified: list[str] = []
    for track in declared:
        track_errors: list[str] = []
        entry = report_tracks.get(track)
        if not isinstance(entry, dict) or entry.get("status") != "passed":
            track_errors.append("status is not passed")
            entry = entry if isinstance(entry, dict) else {}
        tested_modes = set(entry.get("modes_tested") or [])
        missing_modes = modes - tested_modes
        if missing_modes:
            track_errors.append("modes not tested: " + ", ".join(sorted(missing_modes)))
        controls = entry.get("controls") or []
        if not isinstance(controls, list):
            controls = []
            track_errors.append("controls must be a list")
        by_id: dict[str, dict[str, Any]] = {}
        for control in controls:
            if not isinstance(control, dict) or not control.get("id"):
                track_errors.append("every control requires an id")
                continue
            control_id = str(control["id"])
            if control_id in by_id:
                track_errors.append(f"duplicate control: {control_id}")
            by_id[control_id] = control
        required = set(_COMMON_ENFORCEMENT_CONTROLS)
        required.update(_TRACK_ENFORCEMENT_CONTROLS[track])
        if "task_specific" in modes:
            required.add("task_specific_oracle_only")
        if "self_evolving" in modes:
            required.add("self_evolving_stage_isolation")
        for control_id in sorted(required):
            control = by_id.get(control_id)
            if not control:
                track_errors.append(f"missing control: {control_id}")
            elif control.get("passed") is not True:
                track_errors.append(f"control did not pass: {control_id}")
            elif not str(control.get("evidence") or "").strip():
                track_errors.append(f"control has no evidence: {control_id}")
        if track_errors:
            errors.extend(f"{track}: {problem}" for problem in track_errors)
        else:
            verified.append(track)

    if global_error_count:
        verified = []
    summary["verified_tracks"] = verified
    summary["status"] = "passed" if len(verified) == len(declared) and not errors else "failed"
    return verified, summary, errors


def _runtime_artifacts(manifest: Path, runtime: dict[str, Any]) -> dict[str, str]:
    """Validate optional helper scripts that execute behind the hashed entrypoint."""
    declared = runtime.get("runtime_artifact_sha256") or {}
    if not isinstance(declared, dict):
        raise ValueError("runtime.runtime_artifact_sha256 must be an object")
    verified: dict[str, str] = {}
    for relative, expected_raw in sorted(declared.items()):
        expected = str(expected_raw)
        if not relative or not _SHA256.fullmatch(expected):
            raise ValueError("runtime artifact requires a relative path and lowercase SHA-256")
        path = (manifest.parent / str(relative)).resolve()
        try:
            path.relative_to(manifest.parent)
        except ValueError as exc:
            raise ValueError("runtime artifact escapes its adapter bundle") from exc
        if not path.is_file():
            raise ValueError(f"runtime artifact does not exist: {path}")
        actual = _sha256(path)
        if actual != expected:
            raise ValueError(
                f"runtime artifact checksum mismatch for {relative}: "
                f"expected {expected}, actual {actual}"
            )
        verified[str(relative)] = actual
    return verified


def inspect_adapter(path: str | Path) -> dict[str, Any]:
    """Validate a version-1 bundle without importing its Python entrypoint."""
    manifest = Path(path).expanduser().resolve()
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    if raw.get("format_version") != 1:
        raise ValueError("adapter manifest format_version must be 1")
    for field in ("benchmark", "adapter_id"):
        if not _ID.fullmatch(str(raw.get(field) or "")):
            raise ValueError(f"{field} must be a safe lowercase identifier")
    roots: dict[str, str] = {}
    for dataset, relative in (raw.get("dataset_roots") or {}).items():
        if dataset not in _DATASETS:
            raise ValueError(f"unknown evolving dataset {dataset!r}")
        root = (manifest.parent / str(relative)).resolve()
        if not root.is_dir():
            raise ValueError(f"dataset root does not exist: {root}")
        roots[dataset] = str(root)
    if not roots:
        raise ValueError("adapter must provide at least one existing dataset root")
    entrypoint = str(raw.get("entrypoint") or "")
    relative, sep, factory = entrypoint.partition(":")
    if not sep or not relative.endswith(".py") or not factory:
        raise ValueError("entrypoint must be relative.py:factory")
    module = (manifest.parent / relative).resolve()
    try:
        module.relative_to(manifest.parent)
    except ValueError as exc:
        raise ValueError("adapter entrypoint escapes its bundle") from exc
    if not module.is_file():
        raise ValueError(f"adapter entrypoint does not exist: {module}")
    actual = _sha256(module)
    expected = str(raw.get("entrypoint_sha256") or "")
    if expected != actual:
        raise ValueError(f"entrypoint checksum mismatch: expected {expected}, actual {actual}")

    caps = raw.get("capabilities") or {}
    tracks = list(caps.get("tracks") or roots)
    if not set(tracks).issubset(roots):
        raise ValueError("capability tracks must have matching dataset_roots")
    runtime = raw.get("runtime") or {}
    required_runtime = ("isolation", "network", "timeout_sec", "resource_enforcement", "grader_authority")
    missing = [key for key in required_runtime if key not in runtime]
    if missing:
        raise ValueError(f"runtime is missing required fields: {', '.join(missing)}")
    comparable_tracks, enforcement_evidence, enforcement_errors = _enforcement_proof(
        manifest, raw, tracks, runtime
    )
    runtime_artifacts = _runtime_artifacts(manifest, runtime)
    runtime_valid = bool(
        raw.get("runnable", True) and runtime.get("grader_authority")
        and runtime.get("isolation") not in (None, "none")
    )
    return {
        "manifest": str(manifest),
        "manifest_sha256": _sha256(manifest),
        "benchmark": raw["benchmark"],
        "adapter_id": raw["adapter_id"],
        "entrypoint": str(module),
        "entrypoint_sha256": actual,
        "source_url": str(raw.get("source_url") or ""),
        "source_commit": str(raw.get("source_commit") or ""),
        "dataset_roots": roots,
        "capabilities": caps,
        "credentials": list(raw.get("credentials") or []),
        "smoke_task_ids": list(raw.get("smoke_task_ids") or []),
        "deviations": list(raw.get("deviations") or []),
        "runtime": runtime,
        "runtime_artifacts": runtime_artifacts,
        "enforcement_evidence": enforcement_evidence,
        "enforcement_errors": enforcement_errors,
        "runnable": bool(raw.get("runnable", True)),
        "comparable": bool(runtime_valid and comparable_tracks and not enforcement_errors),
        "comparable_tracks": comparable_tracks if runtime_valid else [],
    }


def catalog_targets(client: EvalClient | None = None) -> list[dict[str, Any]]:
    """Flatten the service catalog into one row per dataset/benchmark."""
    client = client or EvalClient()
    rows: list[dict[str, Any]] = []
    for dataset in client.benchmarks().get("datasets", []):
        for benchmark in dataset.get("benchmarks", []):
            rows.append({"dataset": dataset.get("dataset"), **benchmark})
    return rows


def resolve_target(
    benchmark: str,
    *,
    dataset: str = "evovling_tools",
    track: str | None = None,
    mode: str = "deployment",
    harness: str = "command",
    action_type: str | None = None,
    runtime_adapter: str | Path | None = None,
    client: EvalClient | None = None,
) -> dict[str, Any]:
    """Choose hosted only when every requested capability is advertised."""
    requested_track = track or dataset
    hosted = next(
        (row for row in catalog_targets(client) if row.get("dataset") == dataset
         and row.get("benchmark") == benchmark),
        None,
    )
    reasons: list[str] = []
    if hosted is not None:
        checks = {
            "runnable": bool(hosted.get("runnable")),
            "health": bool((hosted.get("health") or {}).get("ok")),
            "track": requested_track in (hosted.get("tracks") or []),
            "mode": mode in (hosted.get("modes") or []),
            "harness": harness in (hosted.get("harnesses") or []),
            "action_type": action_type is None or action_type in (hosted.get("action_types") or []),
        }
        reasons = [name for name, ok in checks.items() if not ok]
        if not reasons:
            return {"location": "hosted", "benchmark": benchmark, "catalog": hosted,
                    "reason": "healthy runnable adapter advertises every requested capability"}
    else:
        reasons = ["not_in_hosted_catalog"]

    local = inspect_adapter(runtime_adapter) if runtime_adapter else None
    if local and local["benchmark"] != benchmark:
        raise ValueError(
            f"local adapter is for {local['benchmark']!r}, not requested {benchmark!r}"
        )
    local_rejections: list[str] = []
    if local:
        caps = local.get("capabilities") or {}
        local_checks = {
            "runnable": bool(local.get("runnable")),
            "track": requested_track in (caps.get("tracks") or []),
            "mode": mode in (caps.get("modes") or []),
            "harness": harness in (caps.get("harnesses") or []),
            "action_type": action_type is None or action_type in (caps.get("action_types") or []),
        }
        local_rejections = [name for name, ok in local_checks.items() if not ok]
    usable_local = bool(local and not local_rejections)
    return {
        "location": "local" if usable_local else "unavailable",
        "benchmark": benchmark,
        "hosted_rejections": reasons,
        "adapter": local,
        "local_rejections": local_rejections,
        "next": (
            "start a loopback sidecar after checkpoint-2 hash approval"
            if usable_local else (
                "choose capabilities advertised by the local adapter"
                if local else "provide a reviewed --runtime-adapter manifest"
            )
        ),
    }


def serve_local(
    manifest: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8078,
    approved_sha256: Iterable[str] = (),
    output_root: str | Path | None = None,
) -> int:
    """Start the shared REST service with one approved adapter, loopback only."""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("serve-local accepts only a loopback IP address")
    except ValueError as exc:
        raise ValueError("serve-local accepts only a loopback IP address") from exc
    if not 1 <= int(port) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    inspected = inspect_adapter(manifest)
    approved = set(approved_sha256)
    env_approved = {
        item for item in os.environ.get("EVAL_SERVICE_APPROVED_ADAPTER_SHA256", "").split(",")
        if item
    }
    approved.update(env_approved)
    required_hashes = {inspected["entrypoint_sha256"], inspected["manifest_sha256"]}
    if not required_hashes.issubset(approved):
        raise PermissionError(
            "adapter manifest and code are not approved; repeat --approve-sha256 for: "
            + ", ".join(sorted(required_hashes - approved))
        )
    if output_root is not None:
        root = Path(output_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        os.environ["EVAL_LOCAL_OUTPUT_ROOT"] = str(root)
    from .local_server import serve
    serve(inspected["manifest"], approved, host, int(port))
    return 0


__all__ = ["catalog_targets", "inspect_adapter", "resolve_target", "serve_local"]
