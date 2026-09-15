"""Capture safe ALE component results without changing or rerunning evaluators.

This module is deliberately stdlib-only: it runs both in the eval service's
Python and in ALE's isolated virtual environment.  The official evaluator
return remains the sole source of the aggregate score.  Frame capture only
enriches that result with measurements the evaluator already computed.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import math
import os
import re
import sys
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

try:  # Works when imported as eval_service.ale_verifier_capture.
    from .ale_verifier_registry import REGISTRY_VERSION, normalize_task_id, task_spec
except ImportError:  # Works from the ALE subprocess PYTHONPATH.
    from ale_verifier_registry import REGISTRY_VERSION, normalize_task_id, task_spec

SIDECAR_SCHEMA = 1
SIDECAR_ENV = "EVAL_SERVICE_ALE_VERIFIER_OUTPUT"
_STATS_LOCK = threading.Lock()
_STATS: dict[str, Any] = {
    "accepted": 0,
    "missing_or_invalid": 0,
    "extraction_failures": 0,
    "tasks_with_failures": {},
}

_AGGREGATE_KEYS = {
    "score", "overall_score", "final_score", "total_score", "normalized_score",
    "raw_score", "aggregate", "summary", "success", "passed", "passes", "valid",
}
_NON_CHECK_KEYS = _AGGREGATE_KEYS | {
    "weight", "max_score", "max_points", "possible_points", "total_possible",
    "threshold", "tolerance", "description", "name",
}
_CONTAINER_KEYS = {
    "components", "component_scores", "scores", "score_breakdown", "breakdown",
    "criteria", "rubric", "rubric_items", "tiers", "checks", "check_results",
    "results", "details", "gates", "per_metric", "score_items",
}
_SAFE_KEY_PARTS = {
    "score", "pass", "valid", "correct", "wrong", "missing", "error", "fail",
    "point", "weight", "count", "total", "accuracy", "precision", "recall",
    "f1", "mae", "rmse", "rate", "gap", "tolerance", "threshold", "penalty",
    "violation", "status", "reason", "tier", "gate", "match", "quality",
    "mean", "average",
}
_DENY_KEY_PARTS = {
    "secret", "token", "password", "credential", "api_key", "apikey", "auth",
    "gold", "truth", "reference", "expected_value", "answer", "solution",
    "content", "prompt", "input", "output", "path", "command", "stdout",
    "stderr", "text", "payload", "raw_data",
}
_SENSITIVE_TEXT = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|mas_[A-Za-z0-9_-]{12,}|/[^\s]{3,}|[A-Za-z]:\\[^\s]+)"
)


def official_score(raw: Any) -> float | None:
    """Extract ALE's official aggregate without changing its semantics."""
    value = raw
    if isinstance(raw, Mapping):
        value = raw.get("score", raw.get("final_score"))
        if value is None:
            scores = raw.get("raw_scores")
            value = scores[0] if isinstance(scores, (list, tuple)) and scores else None
    elif isinstance(raw, (list, tuple)):
        value = raw[0] if raw else None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _key_safe(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if any(part in normalized for part in _DENY_KEY_PARTS):
        return False
    return any(part in normalized for part in _SAFE_KEY_PARTS)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    method = getattr(value, "to_dict", None)
    if callable(method):
        try:
            converted = method()
        except Exception:  # noqa: BLE001 - enrichment must never break grading
            return None
        return converted if isinstance(converted, Mapping) else None
    return None


def _safe_text(value: str) -> str:
    text = value.replace("\x00", "").strip()
    if not text:
        return ""
    text = _SENSITIVE_TEXT.sub("[redacted]", text)
    return text[:240]


def _safe_scalar(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str) and any(x in key.lower() for x in ("status", "reason", "error")):
        return _safe_text(value)
    return None


def _score_from(value: Any) -> tuple[float | None, bool | None, dict[str, Any]]:
    """Return normalized score, explicit pass state, and safe scalar metadata."""
    if isinstance(value, bool):
        return (1.0 if value else 0.0), value, {}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = float(value)
        return (raw if math.isfinite(raw) and 0.0 <= raw <= 1.0 else None), None, {
            "raw_score": raw,
        }
    data = _mapping(value)
    if data is None:
        return None, None, {}

    passed: bool | None = None
    for key in ("passed", "passes", "success", "valid", "is_valid"):
        if isinstance(data.get(key), bool):
            passed = bool(data[key])
            break

    raw: float | None = None
    selected = ""
    for key in ("normalized_score", "score", "overall_score", "final_score", "points", "total_points"):
        candidate = data.get(key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            number = float(candidate)
            if math.isfinite(number):
                raw, selected = number, key
                break
    maximum: float | None = None
    for key in ("max_score", "max_points", "possible_points", "total_possible"):
        candidate = data.get(key)
        if isinstance(candidate, (int, float)) and float(candidate) > 0:
            maximum = float(candidate)
            break
    if raw is None:
        score = 1.0 if passed is True else 0.0 if passed is False else None
    elif 0.0 <= raw <= 1.0 and selected != "points":
        score = raw
    elif maximum is not None:
        score = max(0.0, min(1.0, raw / maximum))
    else:
        score = None

    meta: dict[str, Any] = {}
    if raw is not None:
        meta["raw_score"] = raw
    if maximum is not None:
        meta["max_score"] = maximum
    for key, candidate in data.items():
        if not _key_safe(str(key)) or str(key) in _AGGREGATE_KEYS:
            continue
        safe = _safe_scalar(str(key), candidate)
        if safe is not None:
            meta[str(key)] = safe
    return score, passed, meta


def _score_for_named(name: str, value: Any) -> tuple[float | None, bool | None, dict[str, Any]]:
    score, passed, meta = _score_from(value)
    lowered = name.lower()
    # These names express defects, so True/non-zero means the check failed.
    if isinstance(value, bool) and any(part in lowered for part in ("missing", "error", "fail", "violation")):
        return (0.0 if value else 1.0), not value, meta
    if isinstance(value, (int, float)) and not isinstance(value, bool) and any(
        part in lowered for part in ("wrong", "missing", "error", "fail", "violation", "penalty")
    ):
        number = float(value)
        return (1.0 if number == 0 else 0.0), number == 0, meta
    return score, passed, meta


def _check_rows(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 3:
        return []
    if isinstance(value, (list, tuple)):
        rows: list[dict[str, Any]] = []
        for index, child in enumerate(value[:500]):
            child_map = _mapping(child)
            if child_map is None:
                continue
            raw_name = child_map.get("name") or child_map.get("criterion") or f"check_{index + 1}"
            name = _safe_text(str(raw_name)) or f"check_{index + 1}"
            score, passed, meta = _score_for_named(name, child)
            nested = _check_rows(child, depth=depth + 1)
            if nested:
                meta["checks"] = nested
            if passed is None:
                passed = score is not None and math.isclose(score, 1.0, abs_tol=1e-12)
            if score is not None or passed is not None or nested:
                rows.append({
                    "name": name,
                    "passed": bool(passed),
                    "score": score,
                    "details": meta,
                })
        return rows
    data = _mapping(value)
    if data is None:
        return []
    rows: list[dict[str, Any]] = []
    for raw_name, child in data.items():
        name = str(raw_name)
        lowered = name.lower()
        if lowered in _NON_CHECK_KEYS:
            continue
        if any(part in lowered for part in _DENY_KEY_PARTS):
            continue
        child_map = _mapping(child)
        is_container = lowered in _CONTAINER_KEYS
        score, passed, meta = _score_for_named(name, child)
        if name not in _AGGREGATE_KEYS and _key_safe(name) and (score is not None or passed is not None):
            if passed is None:
                passed = score is not None and math.isclose(score, 1.0, abs_tol=1e-12)
            rows.append({
                "name": name,
                "passed": bool(passed),
                "score": score,
                "details": meta,
            })
            continue
        if (child_map is not None or isinstance(child, (list, tuple))) and (is_container or depth == 0):
            nested = _check_rows(child, depth=depth + 1)
            if is_container:
                rows.extend(nested)
            elif nested:
                rows.append({
                    "name": name,
                    "passed": all(bool(row.get("passed")) for row in nested),
                    "score": (
                        sum(float(row["score"]) for row in nested if row.get("score") is not None)
                        / sum(1 for row in nested if row.get("score") is not None)
                        if any(row.get("score") is not None for row in nested) else None
                    ),
                    "details": {"checks": nested},
                })
    return rows[:500]


def _component_rows(root_name: str, root: Any) -> list[dict[str, Any]]:
    data = _mapping(root)
    if data is None:
        return []
    candidates: list[tuple[str, Any]] = []

    # Explicit component containers take precedence.
    for key, value in data.items():
        lowered = str(key).lower()
        nested = _mapping(value)
        if lowered in _CONTAINER_KEYS:
            if nested:
                candidates.extend((str(name), child) for name, child in nested.items())
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value[:100]):
                    child_map = _mapping(child)
                    if child_map is None:
                        continue
                    raw_name = child_map.get("name") or child_map.get("criterion") or f"component_{index + 1}"
                    candidates.append((_safe_text(str(raw_name)), child))

    for key, value in data.items():
        name = str(key)
        lowered = name.lower()
        if (
            name in _AGGREGATE_KEYS
            or lowered in _CONTAINER_KEYS
            or any(part in lowered for part in _DENY_KEY_PARTS)
            or lowered.startswith("per_")
        ):
            continue
        nested = _mapping(value)
        if nested is not None:
            score, passed, _ = _score_from(value)
            checks = _check_rows(value)
            if score is not None or passed is not None or checks:
                candidates.append((name, value))
        elif _key_safe(name):
            candidates.append((name, value))

    rows: list[dict[str, Any]] = []
    for name, value in candidates:
        score, passed, meta = _score_for_named(name, value)
        checks = _check_rows(value)
        if score is None and passed is None and not checks:
            # Diagnostic text/objects are not verifier measurements.  Keeping
            # one as a false row used to make an aggregate-only report look
            # like a successfully extracted component breakdown.
            continue
        if checks:
            meta["checks"] = checks
            meta["n_checks"] = len(checks)
            meta["n_checks_passed"] = sum(bool(check.get("passed")) for check in checks)
        if passed is None:
            passed = score is not None and math.isclose(score, 1.0, abs_tol=1e-12)
        rows.append({
            "name": name,
            "passed": bool(passed),
            "score": score,
            "expected": "full credit under the task's published rubric",
            "actual": score,
            "comparison_type": "ale_component:official_evaluator",
            "description": f"ALE evaluator component from {root_name}",
            "details": meta,
            "error": None,
        })

    # Per-item structures can reveal hidden document/reference identifiers.
    # Preserve their measured checks, but anonymize item names and nest them
    # under a top-level component instead of inflating n_total.
    per_item_groups: list[dict[str, Any]] = []
    for raw_group_name, value in data.items():
        group_name = str(raw_group_name)
        if not group_name.lower().startswith("per_"):
            continue
        items = _mapping(value)
        if not items:
            continue
        item_rows: list[dict[str, Any]] = []
        for index, child in enumerate(items.values()):
            checks = _check_rows(child, depth=1)
            if not checks:
                continue
            known = [check for check in checks if check.get("score") is not None]
            item_rows.append({
                "name": f"{group_name}_item_{index + 1}",
                "passed": bool(known) and all(bool(check.get("passed")) for check in known),
                "score": (
                    sum(float(check["score"]) for check in known) / len(known)
                    if known else None
                ),
                "details": {"checks": checks},
            })
        if item_rows:
            per_item_groups.append({
                "name": group_name,
                "passed": all(bool(item.get("passed")) for item in item_rows),
                "score": None,
                "details": {"checks": item_rows},
            })
    if rows and per_item_groups:
        details = rows[0].setdefault("details", {})
        details.setdefault("checks", []).extend(per_item_groups)
        details["n_checks"] = len(details["checks"])
        details["n_checks_passed"] = sum(
            bool(check.get("passed")) for check in details["checks"]
        )
    return rows[:100]


def _row(
    name: str,
    score: float | None,
    *,
    passed: bool | None = None,
    weight: float | None = None,
    gate: bool = False,
    reached: bool = True,
    description: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one stable public component without exposing evaluator objects."""
    if passed is None:
        passed = score is not None and math.isclose(score, 1.0, abs_tol=1e-12)
    metadata = dict(details or {})
    metadata["reached"] = bool(reached)
    if weight is not None:
        metadata["weight"] = float(weight)
        metadata["weighted_contribution"] = (
            float(weight) * float(score) if score is not None else None
        )
    if gate:
        metadata["gate"] = True
    return {
        "name": name,
        "passed": bool(passed),
        "score": score,
        "expected": "full credit under the task's published rubric",
        "actual": score,
        "comparison_type": "ale_component:official_evaluator",
        "description": description,
        "details": metadata,
        "error": None,
    }


def _missing_rows(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        _row(str(name), None, passed=False, reached=False,
             description="Official evaluator component was not reached")
        for name in spec.get("expected", ())
    ]


def _chisel_rows(score: float | None, _roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    if score is None:
        return _missing_rows(spec)
    # The evaluator awards 0.10 after schema validation, 0.10 for the target
    # signal, and 0.80 for the exact source-location set.  Those sums are
    # unique, so the official scalar losslessly identifies all three checks.
    rounded = round(score, 8)
    schema = rounded >= 0.1
    target = rounded in (0.2, 1.0)
    sources = rounded in (0.9, 1.0)
    return [
        _row("valid_output_schema", float(schema), passed=schema, weight=0.1),
        _row("target_signal", float(target), passed=target, weight=0.1),
        _row("chisel_sources", float(sources), passed=sources, weight=0.8),
    ]


def _cp_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    verdicts = roots.get("verdicts")
    if not isinstance(verdicts, Mapping):
        return _missing_rows(spec)

    def values(key: str) -> set[str]:
        raw = verdicts.get(key, ())
        if isinstance(raw, str):
            raw = (raw,)
        return {str(value).upper() for value in raw} if isinstance(raw, (set, list, tuple)) else set()

    gate = values("j") == {"AC"}
    rows = [_row("gate_j_ac", float(gate), passed=gate, weight=0.0, gate=True)]
    checks = (
        ("submission_i", values("i") == {"AC"}),
        ("submission_a", bool(values("a") & {"WA", "TLE"})),
        ("submission_c", bool(values("c") & {"WA", "TLE"})),
        ("submission_e", bool(values("e") & {"WA", "TLE"})),
        ("submission_f", bool(values("f") & {"WA", "TLE"})),
        ("submission_h", bool(values("h") & {"WA", "TLE"})),
        ("submission_b", bool(values("b") & {"WA", "TLE"})),
        ("submission_d", bool(values("d") & {"WA", "TLE"})),
        ("submission_g_wa", "WA" in values("g")),
        ("submission_g_ac", "WA" in values("g") and "AC" in values("g")),
    )
    for name, passed in checks:
        rows.append(_row(
            name, float(passed) if gate else None, passed=gate and passed,
            weight=0.1, reached=gate,
        ))
    return rows


def _os_log_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    report = _mapping(roots.get("report"))
    if report is None:
        return _missing_rows(spec)
    raw_errors = report.get("errors")
    errors = [str(value).lower() for value in raw_errors] if isinstance(raw_errors, list) else []
    categories = {
        "reference_rule_consistency": ("reference_state.json does not match",),
        "output_manifest": ("missing output/final_state.json", "invalid final_state.json"),
        "path_set": ("final_state.json missing", "unexpected path"),
        "ownership": ("owner changed",),
        "group": ("group changed",),
        "permission_modes": ("final_state mode", "filesystem mode"),
        "workspace_files": ("sandbox_fs", "missing from sandbox workspace"),
        "content_integrity": ("contents changed", "missing from input fs_snapshot"),
    }
    early = any(token in error for error in errors for token in categories["output_manifest"])
    rows: list[dict[str, Any]] = []
    for index, (name, tokens) in enumerate(categories.items()):
        reached = not early or index < 2
        failed = any(token in error for error in errors for token in tokens)
        rows.append(_row(
            name, (0.0 if failed else 1.0) if reached else None,
            passed=reached and not failed, reached=reached,
            details={"failure_count": sum(token in error for error in errors for token in tokens)},
        ))
    return rows


def _trace_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    hits = {str(value) for value in roots.get("__trace_checks__", ())}
    return [
        _row(
            str(check["name"]),
            1.0 if str(check["name"]) in hits else 0.0,
            passed=str(check["name"]) in hits,
            weight=float(check.get("weight", 0.0)),
        )
        for check in spec.get("trace_checks", ())
    ]


def _scene3_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = _mapping(roots.get("payload"))
    if payload is None:
        return _missing_rows(spec)
    reasons = {str(value) for value in payload.get("reasons", ())} if isinstance(payload.get("reasons"), list) else set()
    if any(reason.startswith("missing:") for reason in reasons):
        return _missing_rows(spec)
    checks = (
        ("chosen_mask", "chosen_mask_mismatch"),
        ("verdict", "verdict_mismatch"),
        ("rationale", "missing_rationale"),
    )
    return [_row(name, float(reason not in reasons), passed=reason not in reasons) for name, reason in checks]


def _scene2_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = _mapping(roots.get("payload"))
    if payload is None:
        return _missing_rows(spec)
    reasons = {str(value) for value in payload.get("reasons", ())} if isinstance(payload.get("reasons"), list) else set()
    ordered = (
        ("required_artifacts", any(value.startswith("missing:") for value in reasons), 0.0),
        ("settings_image", "unreadable_resample_settings_png" in reasons, 0.0),
        ("nifti_readable", any(value.startswith("unreadable_nifti:") for value in reasons), 0.0),
        ("mask_geometry", bool(reasons & {"mask_shape_mismatch", "mask_affine_mismatch"}), 0.0),
        ("nonempty_mask", "empty_mask" in reasons, 0.0),
        ("derived_statistics", any(value.startswith("derived_") for value in reasons), 0.7),
        ("reported_csv", any(value.startswith("csv_") for value in reasons), 0.3),
    )
    rows: list[dict[str, Any]] = []
    blocked = False
    for name, failed, weight in ordered:
        reached = not blocked
        rows.append(_row(
            name, (0.0 if failed else 1.0) if reached else None,
            passed=reached and not failed, weight=weight, gate=weight == 0.0,
            reached=reached,
        ))
        if reached and failed and weight == 0.0:
            blocked = True
    return rows


def _simglucose_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    scored = _mapping(roots.get("scored"))
    if scored is None:
        return _missing_rows(spec)
    try:
        tir = max(0.0, min(1.0, float(scored["mean_tir_70_180"])))
        completion = max(0.0, min(1.0, float(scored["completion_ratio"])))
    except (KeyError, TypeError, ValueError):
        return _missing_rows(spec)
    common = {
        "combination": "time_in_range * episode_safety ** 1.5",
        "episodes": int(scored.get("episodes", 0) or 0),
        "catastrophic_episode_count": int(scored.get("catastrophic_episode_count", 0) or 0),
    }
    return [
        _row("time_in_range", tir, details=common),
        _row("episode_safety", completion, details=common),
    ]


def _ltmle_rows(_score: float | None, roots: Mapping[str, Any], _spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    public = _mapping(roots.get("public_result"))
    hidden = _mapping(roots.get("hidden_payload"))
    public_score, public_passed, public_meta = _score_from(public) if public is not None else (None, None, {})
    hidden_passed = hidden.get("passed") if hidden is not None else None
    hidden_reason = hidden.get("reason") if hidden is not None else None
    return [
        _row(
            "public_summary", public_score, passed=public_passed, reached=public is not None,
            gate=True, details={"reason": public_meta.get("reason", getattr(roots.get("public_result"), "reason", None))},
        ),
        _row(
            "hidden_smoke", (1.0 if hidden_passed else 0.0) if isinstance(hidden_passed, bool) else None,
            passed=bool(hidden_passed), reached=hidden is not None, gate=True,
            details={"reason": _safe_text(str(hidden_reason)) if hidden_reason is not None else None},
        ),
    ]


def _materials_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    failures = roots.get("failures")
    if not isinstance(failures, list):
        return _missing_rows(spec)
    values = [str(value) for value in failures]
    rows = []
    for name in spec.get("expected", ()):
        subcase_failures = [
            value[len(str(name)) + 2:]
            for value in values if value.startswith(f"{name}: ")
        ]
        checks = _material_file_rows(
            subcase_failures,
            tuple(spec.get("subcase_files", {}).get(name, ())),
        )
        count = len(subcase_failures)
        rows.append(_row(
            str(name), 0.0 if count else 1.0, passed=count == 0, gate=True,
            details={"failure_count": count, "checks": checks},
        ))
    return rows


def _material_file_rows(failures: list[str], expected: tuple[str, ...]) -> list[dict[str, Any]]:
    first_read_failure: int | None = None
    for index, name in enumerate(expected):
        if any(value.startswith(f"{name}:") and "failed to read" in value for value in failures):
            first_read_failure = index
            break
    rows = []
    for index, name in enumerate(expected):
        reached = first_read_failure is None or index <= first_read_failure
        failed = any(value.startswith(f"{name}:") for value in failures)
        rows.append({
            "name": name,
            "passed": bool(reached and not failed),
            "score": (0.0 if failed else 1.0) if reached else None,
            "details": {"reached": reached},
        })
    return rows


def _material_files_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = _mapping(roots.get("result"))
    if result is None:
        return _missing_rows(spec)
    raw = result.get("failures")
    failures = [str(value) for value in raw] if isinstance(raw, list) else []
    checks = _material_file_rows(failures, tuple(str(value) for value in spec.get("expected", ())))
    return [
        _row(
            check["name"], check["score"], passed=check["passed"],
            reached=bool(check["details"]["reached"]), gate=True,
            details={"failure_count": sum(value.startswith(f"{check['name']}:") for value in failures)},
        )
        for check in checks
    ]


def _cvrp_rows(_score: float | None, roots: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = _mapping(roots.get("result"))
    if result is None:
        return _missing_rows(spec)
    raw_items = result.get("per_instance")
    items: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_items, (list, tuple)):
        for raw in raw_items:
            item = _mapping(raw)
            if item is not None and item.get("name") is not None:
                items[str(item["name"])] = item
    rows = []
    for expected in spec.get("expected", ()):
        name = str(expected)
        item = items.get(name)
        if item is None:
            rows.append(_row(name, None, passed=False, reached=False))
            continue
        passed = bool(item.get("passed"))
        safe_details: dict[str, Any] = {}
        for key in ("exists", "feasible", "gap"):
            value = item.get(key)
            if isinstance(value, (bool, int, float)) or value is None:
                safe_details[key] = value
        rows.append(_row(name, float(passed), passed=passed, details=safe_details))
    return rows


_CUSTOM_EXTRACTORS: dict[str, Callable[[float | None, Mapping[str, Any], Mapping[str, Any]], list[dict[str, Any]]]] = {
    "chisel_alignment": _chisel_rows,
    "cp_test_gen": _cp_rows,
    "os_log_permissions": _os_log_rows,
    "trace_checks": _trace_rows,
    "scene3_skullstrip": _scene3_rows,
    "scene2_resample": _scene2_rows,
    "simglucose": _simglucose_rows,
    "ltmle": _ltmle_rows,
    "computational_materials": _materials_rows,
    "material_files": _material_files_rows,
    "cvrp_instances": _cvrp_rows,
}


def build_verifier_payload(
    task_id: str, raw_result: Any, captured_roots: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Create a sidecar-safe component payload for one official evaluation."""
    task_id = normalize_task_id(task_id)
    score = official_score(raw_result)
    spec = task_spec(task_id)
    rows: list[dict[str, Any]] = []
    extraction_errors: list[str] = []
    if spec is not None:
        roots = captured_roots or {}
        configured_roots = tuple(spec.get("roots", ()))
        captured_configured = tuple(name for name in configured_roots if name in roots)
        extractor_name = spec.get("extractor")
        if extractor_name:
            extractor = _CUSTOM_EXTRACTORS.get(str(extractor_name))
            if extractor is None:
                extraction_errors.append(f"unknown extractor {extractor_name}")
            else:
                rows = extractor(score, roots, spec)
        else:
            for root_name in spec.get("roots", ()):
                if root_name in roots:
                    rows.extend(_component_rows(str(root_name), roots[root_name]))

        expected_container = spec.get("expected_container")
        if expected_container and captured_configured:
            has_container = any(
                isinstance(_mapping(roots.get(str(root_name))), Mapping)
                and expected_container in _mapping(roots.get(str(root_name)))
                for root_name in spec.get("roots", ())
            )
            if not has_container:
                extraction_errors.append(f"missing expected container: {expected_container}")

        expected = {str(value) for value in spec.get("expected", ())}
        observed = {str(row.get("name")) for row in rows}
        if expected and expected != observed:
            missing = sorted(expected - observed)
            extra = sorted(observed - expected)
            if missing:
                extraction_errors.append(f"missing expected components: {', '.join(missing)}")
            if extra:
                extraction_errors.append(f"unexpected components: {', '.join(extra)}")
        if (
            not spec.get("atomic")
            and not rows
            and score is not None
            and math.isclose(score, 0.0, abs_tol=1e-12)
            and configured_roots
            and not captured_configured
        ):
            # Many upstream evaluators return zero before constructing their
            # report when a required artifact is absent.  That is an official
            # precondition verifier, not extraction drift.  Source validation
            # separately proves that the configured report local still exists.
            rows = [_row(
                "evaluation_preconditions", 0.0, passed=False, gate=True,
                description="The evaluator stopped before component scoring",
                details={
                    "granularity": "precondition_failure",
                    "components_reached": False,
                    "registry_version": REGISTRY_VERSION,
                },
            )]
        elif not spec.get("atomic") and not rows:
            extraction_errors.append(
                "no component rows extracted"
                + (
                    f" from {captured_configured}"
                    if captured_configured
                    else f"; configured roots {configured_roots} were not captured"
                )
            )

    # Deduplicate stable names when reports expose the same breakdown twice.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("name") or "ale_component")
        if name not in seen:
            seen.add(name)
            unique.append(row)
    rows = unique

    if not rows:
        is_atomic = bool(spec and spec.get("atomic"))
        rows = [{
            "name": "ale_score",
            "passed": score is not None and math.isclose(score, 1.0, abs_tol=1e-12),
            "score": score,
            "expected": ">= 1.0",
            "actual": score,
            "comparison_type": "ale_score:official_evaluator",
            "description": (
                "Official atomic ALE task score" if is_atomic
                else "Official ALE score; detailed component extraction was unavailable"
            ),
            "details": {
                "granularity": "atomic" if is_atomic else "aggregate_fallback",
                "registry_version": REGISTRY_VERSION,
                **({"atomic_reason": spec.get("atomic_reason")} if is_atomic else {}),
                **({"extraction_error": "; ".join(extraction_errors)} if extraction_errors else {}),
                **({"extraction_error": "task is not present in the verifier registry"}
                   if spec is None else {}),
            },
            "error": None,
        }]

    if extraction_errors and rows:
        # A custom extractor normally returns its stable schema even when a
        # component was not reached.  Attach structural errors to the first row
        # so health diagnostics cannot silently report a green extraction.
        rows[0].setdefault("details", {})["extraction_error"] = "; ".join(extraction_errors)

    return {
        "schema": SIDECAR_SCHEMA,
        "registry_version": REGISTRY_VERSION,
        "task_id": task_id,
        "score": score,
        "per_verifier": rows,
    }


async def invoke_with_capture(
    evaluate_fn: Callable[..., Any], task_id: str, *args: Any, **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    """Invoke an evaluator once and capture only registry-allowlisted roots."""
    spec = task_spec(task_id)
    allowed = tuple(spec.get("roots", ())) if spec else ()
    capture_frames = tuple(spec.get("capture_frames", ())) if spec else ()
    trace_checks = tuple(spec.get("trace_checks", ())) if spec else ()
    code = inspect.unwrap(evaluate_fn).__code__
    captured: dict[str, Any] = {}
    trace_hits: set[str] = set()
    previous = sys.gettrace()

    def tracer(frame, event, arg):  # type: ignore[no-untyped-def]
        frame_roots: tuple[str, ...] = ()
        if frame.f_code is code:
            frame_roots = allowed
        else:
            filename = frame.f_code.co_filename.replace("\\", "/")
            for rule in capture_frames:
                if (
                    frame.f_code.co_name == rule.get("function")
                    and filename.endswith(str(rule.get("file_suffix", "")))
                ):
                    frame_roots = tuple(rule.get("roots", ()))
                    break

        if event == "line" and frame.f_code is code and trace_checks:
            for check in trace_checks:
                if frame.f_lineno != int(check.get("line", -1)):
                    continue
                dynamic = check.get("name_from")
                if dynamic:
                    observed = str(frame.f_locals.get(str(dynamic), ""))
                    if observed == str(check.get("name")):
                        trace_hits.add(observed)
                else:
                    trace_hits.add(str(check.get("name")))

        if frame_roots and event == "return" and arg is not None:
            # Coroutine suspension emits a return event with arg=None.  The
            # final ALE list/dict/scalar therefore reaches only this branch.
            for name in frame_roots:
                if name in frame.f_locals:
                    captured[str(name)] = frame.f_locals[name]
        return tracer

    sys.settrace(tracer)
    try:
        raw = evaluate_fn(*args, **kwargs)
        if inspect.isawaitable(raw):
            raw = await raw
    finally:
        sys.settrace(previous)
    if trace_checks:
        captured["__trace_checks__"] = sorted(trace_hits)
    try:
        payload = build_verifier_payload(task_id, raw, captured)
    except Exception as exc:  # noqa: BLE001 - official grading must survive enrichment
        score = official_score(raw)
        payload = {
            "schema": SIDECAR_SCHEMA,
            "registry_version": REGISTRY_VERSION,
            "task_id": task_id,
            "score": score,
            "per_verifier": [{
                "name": "ale_score",
                "passed": score is not None and math.isclose(score, 1.0, abs_tol=1e-12),
                "score": score,
                "expected": ">= 1.0",
                "actual": score,
                "comparison_type": "ale_score:official_evaluator",
                "description": "Official ALE score; component extraction failed safely",
                "details": {
                    "granularity": "aggregate_fallback",
                    "registry_version": REGISTRY_VERSION,
                    "extraction_error": type(exc).__name__,
                },
                "error": None,
            }],
        }
    return raw, payload


def write_sidecar(payload: Mapping[str, Any], path: str | os.PathLike[str] | None = None) -> None:
    """Atomically write the service-controlled verifier sidecar, if enabled."""
    target_text = str(path or os.environ.get(SIDECAR_ENV, "")).strip()
    if not target_text:
        return
    target = Path(target_text).resolve()
    tmp_name = ""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=target.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(payload), handle, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if tmp_name:
                try:
                    Path(tmp_name).unlink()
                except FileNotFoundError:
                    pass
    except OSError:
        # Sidecar persistence is enrichment only; never change the official run.
        return


def merge_sidecar(
    result: Mapping[str, Any], path: str | os.PathLike[str], task_id: str,
) -> dict[str, Any]:
    """Validate, merge, and remove one sidecar; invalid data is ignored."""
    merged = dict(result)
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = None
    finally:
        try:
            target.unlink()
        except OSError:
            pass
    if not isinstance(payload, Mapping):
        _bump("missing_or_invalid")
        return merged
    if payload.get("schema") != SIDECAR_SCHEMA or payload.get("task_id") != task_id:
        _bump("missing_or_invalid")
        return merged
    expected = official_score(result)
    observed = official_score(payload)
    if expected is None or observed is None or not math.isclose(expected, observed, abs_tol=1e-12):
        _bump("missing_or_invalid")
        return merged
    rows = payload.get("per_verifier")
    if isinstance(rows, list) and rows:
        merged["per_verifier"] = rows
        merged["verifier_registry_version"] = payload.get("registry_version")
        record_payload(payload)
    return merged


def _bump(name: str) -> None:
    with _STATS_LOCK:
        _STATS[name] += 1


def record_payload(payload: Mapping[str, Any]) -> None:
    """Record safe extraction diagnostics for health reporting."""
    rows = payload.get("per_verifier")
    if not isinstance(rows, list):
        return
    _bump("accepted")
    errors = [
        str(row["details"].get("extraction_error"))
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("details"), Mapping)
        and row["details"].get("extraction_error")
    ]
    task_id = str(payload.get("task_id") or "unknown")
    with _STATS_LOCK:
        failures = _STATS["tasks_with_failures"]
        if errors:
            _STATS["extraction_failures"] += 1
            failures[task_id] = errors[0][:500]
        else:
            failures.pop(task_id, None)


def runtime_diagnostics() -> dict[str, Any]:
    with _STATS_LOCK:
        return {
            **{key: value for key, value in _STATS.items() if key != "tasks_with_failures"},
            "tasks_with_failures": dict(_STATS["tasks_with_failures"]),
        }
