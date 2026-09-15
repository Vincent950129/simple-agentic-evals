#!/usr/bin/env python3
"""Deterministic construction engine for normalized evolving-benchmark bundles.

Seed-specific code is responsible only for producing the normalized bundle
described in ``references/adapter-contract.md``.  This module owns the shared
paper pipeline: frequency ranking, cumulative release, earliest-solvable task
assignment, deterministic splits, materialization, validation, reproduction,
and guarded promotion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import tomllib
except ImportError:  # Python 3.10 remains common in coding-agent shells.
    tomllib = None  # type: ignore[assignment]


AXES = ("tools", "skills", "agents")
AXIS_FIELDS = {
    "tools": ("oracle_tools", "cummulative_tools"),
    "skills": ("oracle_skills", "cummulative_oracle_skills"),
    "agents": ("oracle_agents", "cumulative_agents"),
}
FORMAT_VERSION = 1
PROMPT_EVIDENCE_FIELDS = {
    "agent_must_do",
    "description",
    "generated_user_prompt",
    "instruction",
    "prompt",
    "system_prompt",
    "task_description",
    "task_prompt",
    "user_prompt",
}
REQUIRED_ROW_FIELDS = {
    "tools": {
        "domain", "version", "split", "task_split", "task_id", "task_prompt",
        "oracle_tools", "cummulative_tools", "software", "source_repo_path",
    },
    "skills": {
        "domain", "version", "split", "task_split", "task_id", "system_prompt",
        "user_prompt", "oracle_skills", "cummulative_oracle_skills", "software",
        "patcher_prompts", "prompt_suffix", "evaluation", "source_repo_path",
    },
    "agents": {
        "domain", "version", "split", "task_split", "task_id", "system_prompt",
        "user_prompt", "task_prompt", "oracle_agents", "cumulative_agents",
        "oracle_skills", "oracle_tools", "cummulative_tools", "software",
        "source_repo_path",
    },
}


class BundleError(ValueError):
    """The normalized seed bundle cannot support the requested operation."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BundleError(f"invalid JSON in {path}: {exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise BundleError(f"missing required file: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleError(f"invalid JSON in {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise BundleError(f"expected object in {path}:{line_number}")
        rows.append(value)
    return rows


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                + "\n"
            )


def _unique(values: Iterable[str], what: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not value:
            raise BundleError(f"empty {what}")
        if value in seen:
            raise BundleError(f"duplicate {what}: {value}")
        seen.add(value)
        result.append(value)
    return result


def _tree_entries(root: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries.append(
                (
                    path.relative_to(root).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
    return entries


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, file_hash in _tree_entries(root):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _bundle_input_hash(bundle: dict[str, Any]) -> str:
    root = bundle["root"]
    digest = hashlib.sha256()
    paths = [root / "spec.json", root / "tasks.jsonl", root / "deviations.json"]
    paths.extend(sorted((root / "catalogs").glob("*.json")))
    paths.extend(sorted((root / "annotations").glob("*.jsonl")))
    for path in paths:
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    evidence_paths = {
        str(entry["source_path"])
        for annotations in bundle["annotations"].values()
        for annotation in annotations.values()
        for entries in annotation["evidence"].values()
        for entry in entries
    }
    for relative in sorted(evidence_paths):
        path = (bundle["source_root"] / relative).resolve()
        digest.update(f"source:{relative}".encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _annotation_input_hash(bundle: dict[str, Any]) -> str:
    """Fingerprint everything accepted at checkpoint 2, including evidence."""
    root = bundle["root"]
    digest = hashlib.sha256()
    digest.update(str(bundle["spec"]["benchmark"]["source_commit"]).encode("utf-8"))
    digest.update(b"\n")
    paths = [root / "tasks.jsonl", root / "deviations.json"]
    paths.extend(sorted((root / "catalogs").glob("*.json")))
    paths.extend(sorted((root / "annotations").glob("*.jsonl")))
    for path in paths:
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    evidence_paths = {
        str(entry["source_path"])
        for annotations in bundle["annotations"].values()
        for annotation in annotations.values()
        for entries in annotation["evidence"].values()
        for entry in entries
    }
    for relative in sorted(evidence_paths):
        path = (bundle["source_root"] / relative).resolve()
        digest.update(f"source:{relative}".encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _catalog_items(value: Any, path: Path) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("capabilities")
    if not isinstance(value, list):
        raise BundleError(f"{path} must be a list or an object with capabilities")
    if not all(isinstance(item, dict) for item in value):
        raise BundleError(f"every entry in {path} must be an object")
    return value


def _name(item: dict[str, Any], path: Path) -> str:
    value = str(item.get("name") or item.get("slug") or "").strip()
    if not value:
        raise BundleError(f"catalog entry in {path} has no name")
    return value


def _gate_approved(approvals: dict[str, Any], gate: str) -> bool:
    value = approvals.get(gate)
    return value is True or (isinstance(value, dict) and value.get("approved") is True)


def _require_gates(bundle: dict[str, Any], gates: Iterable[str]) -> None:
    missing = [gate for gate in gates if not _gate_approved(bundle["approvals"], gate)]
    if missing:
        raise BundleError("approval required before this command: " + ", ".join(missing))


def _require_annotation_approval(bundle: dict[str, Any]) -> None:
    value = bundle["approvals"].get("annotations")
    if not isinstance(value, dict) or value.get("approved") is not True:
        raise BundleError("approval required before this command: annotations")
    accepted = str(value.get("input_sha256") or "")
    if not accepted:
        raise BundleError(
            "annotation approval must record input_sha256 from inspect; "
            "review and re-approve checkpoint 2"
        )
    actual = _annotation_input_hash(bundle)
    if accepted != actual:
        raise BundleError(
            "annotations changed after checkpoint-2 approval; inspect, repair, "
            "and re-approve before planning"
        )


def load_bundle(root: Path) -> dict[str, Any]:
    root = root.resolve()
    spec = _read_json(root / "spec.json")
    if spec.get("format_version") != FORMAT_VERSION:
        raise BundleError(f"spec.format_version must be {FORMAT_VERSION}")
    benchmark = spec.get("benchmark")
    if not isinstance(benchmark, dict):
        raise BundleError("spec.benchmark must be an object")
    for key in ("name", "domain", "source_url", "source_commit"):
        if not str(benchmark.get(key, "")).strip():
            raise BundleError(f"spec.benchmark.{key} is required")
    source_root_value = str(benchmark.get("source_root", "")).strip()
    if not source_root_value:
        raise BundleError("spec.benchmark.source_root is required for evidence validation")
    source_root = Path(source_root_value).expanduser()
    if not source_root.is_absolute():
        source_root = root / source_root
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise BundleError(f"source_root is not a directory: {source_root}")
    tracks = spec.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        raise BundleError("spec.tracks must select at least one track")
    tracks = _unique(tracks, "track")
    unknown_tracks = sorted(set(tracks) - set(AXES))
    if unknown_tracks:
        raise BundleError(f"unknown tracks: {unknown_tracks}")

    task_rows = _read_jsonl(root / "tasks.jsonl")
    tasks: dict[str, dict[str, Any]] = {}
    for record in task_rows:
        task_id = str(record.get("task_id", "")).strip()
        if not task_id or task_id in tasks:
            raise BundleError(f"empty or duplicate task_id: {task_id!r}")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise BundleError(f"task {task_id} must contain a public payload object")
        if not any(str(payload.get(key, "")).strip() for key in ("task_prompt", "user_prompt", "instruction")):
            raise BundleError(f"task {task_id} has no public prompt")
        if not str(payload.get("source_repo_path", "")).strip():
            raise BundleError(f"task {task_id} has no source_repo_path")
        task_source = (source_root / str(payload["source_repo_path"])).resolve()
        try:
            task_source.relative_to(source_root)
        except ValueError as exc:
            raise BundleError(f"task {task_id} source_repo_path escapes source_root") from exc
        if not task_source.exists():
            raise BundleError(f"task {task_id} source_repo_path does not exist: {task_source}")
        tasks[task_id] = record
    expected = benchmark.get("expected_tasks")
    if expected is not None and int(expected) != len(tasks):
        raise BundleError(f"expected {expected} tasks, discovered {len(tasks)}")

    catalogs: dict[str, list[dict[str, Any]]] = {}
    catalog_maps: dict[str, dict[str, dict[str, Any]]] = {}
    annotations: dict[str, dict[str, dict[str, Any]]] = {}
    needed = set(tracks) | ({"tools"} if "agents" in tracks else set())
    for axis in sorted(needed & {"tools", "skills"}):
        catalog_path = root / "catalogs" / f"{axis}.json"
        items = _catalog_items(_read_json(catalog_path), catalog_path)
        names = _unique((_name(item, catalog_path) for item in items), f"{axis} catalog name")
        cmap = dict(zip(names, items))
        catalogs[axis] = items
        catalog_maps[axis] = cmap

        annotation_path = root / "annotations" / f"{axis}.jsonl"
        amap: dict[str, dict[str, Any]] = {}
        for annotation in _read_jsonl(annotation_path):
            task_id = str(annotation.get("task_id", "")).strip()
            if task_id not in tasks or task_id in amap:
                raise BundleError(f"unknown or duplicate annotated task_id: {task_id!r}")
            capabilities = annotation.get("capabilities")
            if not isinstance(capabilities, list) or not capabilities:
                raise BundleError(f"{axis} annotation for {task_id} is empty")
            capabilities = sorted(_unique(capabilities, f"{axis} capability for {task_id}"))
            unknown = sorted(set(capabilities) - set(cmap))
            if unknown:
                raise BundleError(f"{axis} annotation for {task_id} uses unknown labels: {unknown}")
            evidence = annotation.get("evidence")
            if not isinstance(evidence, dict):
                raise BundleError(f"{axis} annotation for {task_id} has no evidence map")
            for capability in capabilities:
                entries = evidence.get(capability)
                if not isinstance(entries, list) or not entries:
                    raise BundleError(f"missing evidence for {task_id}/{capability}")
                for entry in entries:
                    if (
                        not isinstance(entry, dict)
                        or not str(entry.get("source_path", "")).strip()
                        or not str(entry.get("rule", "")).strip()
                        or not str(entry.get("match", "")).strip()
                    ):
                        raise BundleError(f"invalid evidence for {task_id}/{capability}")
                    evidence_relative = Path(str(entry["source_path"]))
                    if evidence_relative.is_absolute():
                        raise BundleError(f"evidence path must be source-relative for {task_id}/{capability}")
                    evidence_path = (source_root / evidence_relative).resolve()
                    try:
                        evidence_path.relative_to(source_root)
                    except ValueError as exc:
                        raise BundleError(f"evidence path escapes source_root for {task_id}/{capability}") from exc
                    if not evidence_path.is_file():
                        raise BundleError(f"evidence path does not exist for {task_id}/{capability}: {evidence_path}")
                    if entry.get("line") is not None:
                        line = int(entry["line"])
                        if line < 1 or line > len(evidence_path.read_text(encoding="utf-8", errors="replace").splitlines()):
                            raise BundleError(f"evidence line is outside {entry['source_path']} for {task_id}/{capability}")
            normalized = dict(annotation)
            normalized["task_id"] = task_id
            normalized["capabilities"] = capabilities
            amap[task_id] = normalized
        if not amap:
            raise BundleError(f"{axis} has no annotated tasks")
        annotations[axis] = amap

    if "tools" in catalog_maps:
        aliases: dict[str, str] = {}
        for tool, item in catalog_maps["tools"].items():
            owner = str(item.get("owner", "")).strip()
            if not owner:
                raise BundleError(f"tool {tool} has no owner")
            for alias in item.get("aliases") or []:
                folded = str(alias).strip().casefold()
                if not folded:
                    raise BundleError(f"tool {tool} has an empty alias")
                previous = aliases.setdefault(folded, tool)
                if previous != tool:
                    raise BundleError(f"alias {alias!r} belongs to both {previous} and {tool}")

    deviations_path = root / "deviations.json"
    deviations = _read_json(deviations_path) if deviations_path.is_file() else []
    if not isinstance(deviations, list):
        raise BundleError("deviations.json must contain a list")
    for item in deviations:
        if not isinstance(item, dict) or item.get("approved") is not True:
            raise BundleError("every recorded deviation must be explicitly approved")
        for key in ("track", "paper_default", "adapter", "evidence", "approved_at_checkpoint"):
            if not item.get(key):
                raise BundleError(f"deviation is missing {key}")

    approvals_path = root / "approvals.json"
    approvals = _read_json(approvals_path) if approvals_path.is_file() else {}
    if not isinstance(approvals, dict):
        raise BundleError("approvals.json must contain an object")

    house_rules_path = root / "house_rules.json"
    house_rules = _read_json(house_rules_path) if house_rules_path.is_file() else spec.get("house_rules")

    return {
        "root": root,
        "source_root": source_root,
        "spec": spec,
        "tracks": tracks,
        "tasks": tasks,
        "catalogs": catalogs,
        "catalog_maps": catalog_maps,
        "annotations": annotations,
        "deviations": deviations,
        "approvals": approvals,
        "house_rules": house_rules,
    }


def _constraints(spec: dict[str, Any], axis: str) -> dict[str, Any]:
    shared = {
        "target_versions": 5,
        "min_versions": 3,
        "min_train": 2,
        "min_test": 5,
        "adapt_ratio": 0.3,
        "min_growth_frac": 0.15,
        "max_growth_frac": 0.35,
        "initial_anchor_frac": 0.0,
    }
    shared.update(spec.get("constraints") or {})
    shared.update((spec.get("track_constraints") or {}).get(axis) or {})
    shared["min_new_tasks"] = int(shared.get("min_new_tasks", int(shared["min_train"]) + int(shared["min_test"])))
    for key in ("target_versions", "min_versions", "min_train", "min_test", "min_new_tasks"):
        shared[key] = int(shared[key])
    shared["adapt_ratio"] = float(shared["adapt_ratio"])
    shared["min_growth_frac"] = float(shared["min_growth_frac"])
    shared["max_growth_frac"] = float(shared["max_growth_frac"])
    shared["initial_anchor_frac"] = float(shared["initial_anchor_frac"])
    if shared["min_versions"] < 3:
        raise BundleError("min_versions cannot be lower than 3")
    if not 0 < shared["adapt_ratio"] < 1:
        raise BundleError("adapt_ratio must be between 0 and 1")
    return shared


def _rank(capability_sets: Iterable[Iterable[str]]) -> tuple[list[str], dict[str, int]]:
    frequency: Counter[str] = Counter()
    for values in capability_sets:
        frequency.update(set(values))
    ranked = sorted(frequency, key=lambda value: (-frequency[value], value))
    return ranked, dict(sorted(frequency.items()))


def _assign(capabilities: Iterable[str], anchors: list[set[str]]) -> int | None:
    required = set(capabilities)
    if not required:
        return None
    for index, anchor in enumerate(anchors):
        if required <= anchor:
            return index
    return None


def _assignments(capabilities: dict[str, list[str]], anchors: list[set[str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for task_id, required in capabilities.items():
        stage = _assign(required, anchors)
        if stage is not None:
            result[task_id] = stage
    return result


def _new_task_counts(capabilities: dict[str, list[str]], anchors: list[set[str]]) -> list[int]:
    assigned = _assignments(capabilities, anchors)
    return [sum(stage == index for stage in assigned.values()) for index in range(len(anchors))]


def _frequency_anchors(capabilities: dict[str, list[str]], config: dict[str, Any]) -> tuple[list[set[str]], list[dict[str, Any]]]:
    ranked, _ = _rank(capabilities.values())
    if not ranked:
        raise BundleError("cannot stage an empty capability universe")
    universe_size = len(ranked)
    min_growth = max(1, math.ceil(config["min_growth_frac"] * universe_size))
    max_growth = max(min_growth, math.floor(config["max_growth_frac"] * universe_size))
    max_growth = min(max_growth, universe_size)
    first_min = max(min_growth, math.ceil(config["initial_anchor_frac"] * universe_size))
    oracle_sets = [set(values) for values in capabilities.values()]
    cumulative: set[str] = set()
    solved = [False] * len(oracle_sets)
    anchors: list[set[str]] = []
    cursor = 0
    while cursor < universe_size:
        solved_before = sum(solved)
        tools_before = len(cumulative)
        first = not anchors
        while cursor < universe_size:
            cumulative.add(ranked[cursor])
            cursor += 1
            for index, required in enumerate(oracle_sets):
                if not solved[index] and required <= cumulative:
                    solved[index] = True
            delta = len(cumulative) - tools_before
            newly_solved = sum(solved) - solved_before
            remaining = universe_size - cursor
            if first and len(cumulative) < first_min:
                continue
            stage_max = max(max_growth, first_min) if first else max_growth
            if delta >= stage_max:
                break
            if delta >= min_growth and newly_solved >= config["min_new_tasks"] and remaining >= min_growth:
                break
        anchors.append(set(cumulative))

    merges: list[dict[str, Any]] = []
    while len(anchors) > 1:
        counts = _new_task_counts(capabilities, anchors)
        violator = next((i for i, count in enumerate(counts) if count < config["min_new_tasks"]), None)
        if violator is None:
            break
        if violator < len(anchors) - 1:
            anchors.pop(violator)
            direction = "next"
        else:
            anchors.pop(violator - 1)
            direction = "previous"
        merges.append({"stage": violator + 1, "into": direction, "reason": "thin stage"})
    return anchors, merges


def _split_ids(task_ids: list[str], seed: int, config: dict[str, Any], stage: int) -> tuple[list[str], list[str]]:
    ids = sorted(task_ids)
    random.Random(seed * 9973 + stage + 1).shuffle(ids)
    count = round(len(ids) * config["adapt_ratio"])
    if len(ids) >= config["min_train"] + config["min_test"]:
        count = max(config["min_train"], min(count, len(ids) - config["min_test"]))
    elif len(ids) >= 2:
        count = max(1, min(len(ids) - 1, count or 1))
    return ids[:count], ids[count:]


def _frequency_plan(capabilities: dict[str, list[str]], config: dict[str, Any], seed: int, fixed_split: dict[str, str] | None = None) -> dict[str, Any]:
    anchors, merges = _frequency_anchors(capabilities, config)
    while len(anchors) > 1:
        assigned = _assignments(capabilities, anchors)
        deficient: int | None = None
        for stage in range(len(anchors)):
            ids = [task_id for task_id, value in assigned.items() if value == stage]
            if fixed_split is None:
                train, test = _split_ids(ids, seed, config, stage)
                counts = (len(train), len(test))
            else:
                counts = (
                    sum(fixed_split.get(task_id) == "train" for task_id in ids),
                    sum(fixed_split.get(task_id) == "test" for task_id in ids),
                )
            if counts[0] < config["min_train"] or counts[1] < config["min_test"]:
                deficient = stage
                break
        if deficient is None:
            break
        if deficient < len(anchors) - 1:
            anchors.pop(deficient)
            direction = "next"
        else:
            anchors.pop(deficient - 1)
            direction = "previous"
        merges.append({"stage": deficient + 1, "into": direction, "reason": "split floor"})

    assigned = _assignments(capabilities, anchors)
    stages: list[dict[str, Any]] = []
    split_by_task: dict[str, str] = {}
    previous: set[str] = set()
    for stage, anchor in enumerate(anchors):
        ids = [task_id for task_id, value in assigned.items() if value == stage]
        if fixed_split is None:
            train, test = _split_ids(ids, seed, config, stage)
        else:
            train = sorted(task_id for task_id in ids if fixed_split.get(task_id) == "train")
            test = sorted(task_id for task_id in ids if fixed_split.get(task_id) == "test")
        split_by_task.update({task_id: "train" for task_id in train})
        split_by_task.update({task_id: "test" for task_id in test})
        stages.append({
            "version": stage + 1,
            "introduced": sorted(anchor - previous),
            "cumulative": sorted(anchor),
            "train": train,
            "test": test,
        })
        previous = set(anchor)
    ranked, frequency = _rank(capabilities.values())
    return {
        "ranked_capabilities": ranked,
        "frequency": frequency,
        "stages": stages,
        "assignments": assigned,
        "split_by_task": split_by_task,
        "merges": merges,
    }


def _skill_plan(capabilities: dict[str, list[str]], config: dict[str, Any], seed: int) -> dict[str, Any]:
    ranked, frequency = _rank(capabilities.values())
    cumulative: set[str] = set()
    placed: set[str] = set()
    stages: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(ranked):
        introduced: list[str] = []
        eligible: list[str] = []
        while cursor < len(ranked):
            value = ranked[cursor]
            cursor += 1
            cumulative.add(value)
            introduced.append(value)
            eligible = sorted(
                task_id for task_id, required in capabilities.items()
                if set(required) <= cumulative and task_id not in placed
            )
            if len(eligible) >= config["min_new_tasks"]:
                break
        placed.update(eligible)
        stages.append({"introduced": introduced, "cumulative": sorted(cumulative), "task_ids": eligible})
    while len(stages) >= 2 and len(stages[-1]["task_ids"]) < config["min_new_tasks"]:
        tail = stages.pop()
        stages[-1]["introduced"].extend(tail["introduced"])
        stages[-1]["cumulative"] = tail["cumulative"]
        stages[-1]["task_ids"].extend(tail["task_ids"])

    final: list[dict[str, Any]] = []
    split_by_task: dict[str, str] = {}
    assignments: dict[str, int] = {}
    split_rng = random.Random(seed * 9973 + 1)
    for index, stage in enumerate(stages):
        ids = list(stage["task_ids"])
        split_rng.shuffle(ids)
        count = round(len(ids) * config["adapt_ratio"])
        if len(ids) >= config["min_train"] + config["min_test"]:
            count = max(config["min_train"], min(count, len(ids) - config["min_test"]))
        train, test = ids[:count], ids[count:]
        for task_id in train:
            split_by_task[task_id] = "train"
            assignments[task_id] = index
        for task_id in test:
            split_by_task[task_id] = "test"
            assignments[task_id] = index
        final.append({
            "version": index + 1,
            "introduced": sorted(set(stage["introduced"])),
            "cumulative": list(stage["cumulative"]),
            "train": train,
            "test": test,
        })
    return {
        "ranked_capabilities": ranked,
        "frequency": frequency,
        "stages": final,
        "assignments": assignments,
        "split_by_task": split_by_task,
        "merges": [],
    }


def _is_prompt_skill_evidence(entry: dict[str, Any]) -> bool:
    field = str(entry.get("field") or "").strip().casefold()
    rule = str(entry.get("rule") or "").strip().casefold()
    basis = str(entry.get("basis") or "").strip().casefold()
    return (
        field in PROMPT_EVIDENCE_FIELDS
        or "prompt" in field
        or "description" in field
        or "instruction" in field
        or basis in {"prompt", "description", "tool-anchor", "software-anchor"}
        or "prompt" in rule
        or "tool-anchor" in rule
        or "software-anchor" in rule
    )


def _skill_annotation_diagnostics(bundle: dict[str, Any]) -> dict[str, Any]:
    annotations = bundle["annotations"].get("skills", {})
    metadata_only: list[dict[str, str]] = []
    prompt_backed = 0
    total = 0
    by_rule: Counter[str] = Counter()
    for task_id, annotation in sorted(annotations.items()):
        for capability in annotation["capabilities"]:
            total += 1
            entries = annotation["evidence"][capability]
            by_rule.update(str(entry.get("rule") or "") for entry in entries)
            if any(_is_prompt_skill_evidence(entry) for entry in entries):
                prompt_backed += 1
            else:
                metadata_only.append({"task_id": task_id, "capability": capability})
    return {
        "labels": total,
        "prompt_or_anchor_backed": prompt_backed,
        "metadata_only": len(metadata_only),
        "metadata_only_examples": metadata_only[:20],
        "rules": dict(sorted(by_rule.items())),
        "repair_required": bool(metadata_only),
    }


def _annotation_repair_problems(bundle: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if "skills" in bundle["annotations"]:
        diagnostics = _skill_annotation_diagnostics(bundle)
        if diagnostics["metadata_only"]:
            problems.append(
                f"{diagnostics['metadata_only']} skill labels have metadata-only evidence; "
                "match public prompts/descriptions or approved tool/software anchors"
            )
    return problems


def _proposed_smoke(bundle: dict[str, Any], track_plans: dict[str, Any]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    command = (bundle["spec"].get("grader") or {}).get("command") or []
    for axis in bundle["tracks"]:
        for stage in track_plans[axis]["stages"]:
            task_ids = list(stage["train"]) + list(stage["test"])
            if not task_ids:
                continue
            def key(task_id: str) -> tuple[bool, float, str]:
                resources = bundle["tasks"][task_id]["payload"].get("resource_limits") or {}
                gpus = resources.get("gpus", resources.get("gpu", 0))
                timeout = resources.get(
                    "timeout_seconds",
                    resources.get("max_time_seconds", float("inf")),
                )
                try:
                    timeout_value = float(timeout)
                except (TypeError, ValueError):
                    timeout_value = float("inf")
                return (gpus not in (0, "0", None, False), timeout_value, task_id)
            task_id = min(task_ids, key=key)
            values = {"task_id": task_id, "candidate": "$CANDIDATE", "bundle": "$BUNDLE"}
            rendered = [str(part).format(**values) for part in command]
            proposals.append({
                "track": axis,
                "stage": stage["version"],
                "task_id": task_id,
                "command": rendered,
            })
    return proposals


def make_plan(bundle: dict[str, Any], require_approvals: bool = True) -> dict[str, Any]:
    if require_approvals:
        _require_gates(bundle, ("suitability",))
        _require_annotation_approval(bundle)
    repair_problems = _annotation_repair_problems(bundle)
    if repair_problems:
        raise BundleError("annotation repair required: " + "; ".join(repair_problems))
    spec = bundle["spec"]
    seed = int(spec.get("seed", 42))
    track_plans: dict[str, dict[str, Any]] = {}

    tool_annotations = bundle["annotations"].get("tools", {})
    tool_caps = {task_id: item["capabilities"] for task_id, item in tool_annotations.items()}
    dependency_tool_plan: dict[str, Any] | None = None
    if tool_caps and ({"tools", "agents"} & set(bundle["tracks"])):
        dependency_tool_plan = _frequency_plan(tool_caps, _constraints(spec, "tools"), seed)
        if "tools" in bundle["tracks"]:
            track_plans["tools"] = dependency_tool_plan

    if "skills" in bundle["tracks"]:
        skill_caps = {
            task_id: item["capabilities"]
            for task_id, item in bundle["annotations"]["skills"].items()
        }
        track_plans["skills"] = _skill_plan(skill_caps, _constraints(spec, "skills"), seed)

    if "agents" in bundle["tracks"]:
        if dependency_tool_plan is None:
            raise BundleError("agents require valid tool annotations")
        owners = {
            name: str(item["owner"]).strip()
            for name, item in bundle["catalog_maps"]["tools"].items()
        }
        agent_caps = {
            task_id: sorted({owners[tool] for tool in item["capabilities"]})
            for task_id, item in tool_annotations.items()
        }
        track_plans["agents"] = _frequency_plan(
            agent_caps,
            _constraints(spec, "agents"),
            seed,
            fixed_split=dependency_tool_plan["split_by_task"],
        )

    for axis, plan in track_plans.items():
        config = _constraints(spec, axis)
        if len(plan["stages"]) < config["min_versions"]:
            raise BundleError(
                f"{axis} supports only {len(plan['stages'])} valid stages; "
                f"minimum is {config['min_versions']}"
            )
        for stage in plan["stages"]:
            if len(stage["train"]) < config["min_train"] or len(stage["test"]) < config["min_test"]:
                raise BundleError(f"{axis} v{stage['version']} violates split floors")
    benchmark = spec["benchmark"]
    result = {
        "format_version": FORMAT_VERSION,
        "benchmark": benchmark,
        "seed": seed,
        "bundle_input_sha256": _bundle_input_hash(bundle),
        "selected_tracks": bundle["tracks"],
        "tracks": track_plans,
        "deviations": bundle["deviations"],
        "proposed_smoke": _proposed_smoke(bundle, track_plans),
    }
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    result["plan_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return result


def _require_staging_plan(bundle: dict[str, Any], plan: dict[str, Any]) -> None:
    staging = bundle["approvals"].get("staging")
    expected = staging.get("plan_sha256") if isinstance(staging, dict) else None
    if not expected:
        raise BundleError("staging approval must record plan_sha256")
    if expected != plan["plan_sha256"]:
        raise BundleError("normalized inputs changed after staging approval; re-plan and re-approve")


def inspect_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    annotation_hash = _annotation_input_hash(bundle)
    annotation_approval = bundle["approvals"].get("annotations")
    accepted_annotation_hash = (
        str(annotation_approval.get("input_sha256") or "")
        if isinstance(annotation_approval, dict)
        else ""
    )
    report: dict[str, Any] = {
        "benchmark": bundle["spec"]["benchmark"],
        "discovered_tasks": len(bundle["tasks"]),
        "selected_tracks": bundle["tracks"],
        "approvals": {
            gate: _gate_approved(bundle["approvals"], gate)
            for gate in ("suitability", "annotations", "staging", "promotion")
        },
        "tracks": {},
        "grader_configured": bool((bundle["spec"].get("grader") or {}).get("command")),
        "smoke_tasks_configured": len((bundle["spec"].get("grader") or {}).get("smoke_task_ids") or []),
        "deviations": bundle["deviations"],
        "annotation_input_sha256": annotation_hash,
        "annotation_approval_current": bool(
            _gate_approved(bundle["approvals"], "annotations")
            and accepted_annotation_hash == annotation_hash
        ),
        "repair_problems": _annotation_repair_problems(bundle),
    }
    for axis in ("tools", "skills"):
        if axis in bundle["annotations"]:
            used = {value for item in bundle["annotations"][axis].values() for value in item["capabilities"]}
            report["tracks"][axis] = {
                "annotated_tasks": len(bundle["annotations"][axis]),
                "catalog_capabilities": len(bundle["catalog_maps"][axis]),
                "active_capabilities": len(used),
            }
            if axis == "skills":
                report["tracks"][axis]["evidence_diagnostics"] = _skill_annotation_diagnostics(bundle)
    report["tracks"]["agents"] = {
        "supportable": "tools" in bundle["annotations"],
        "reason": "derived from the unique tool-owner partition" if "tools" in bundle["annotations"] else "tool annotations are unavailable",
    }
    return report


def _annotation_evidence(bundle: dict[str, Any], axis: str, task_id: str) -> dict[str, Any]:
    source_axis = "tools" if axis == "agents" else axis
    return bundle["annotations"][source_axis][task_id]["evidence"]


def _axis_capabilities(bundle: dict[str, Any], axis: str, task_id: str) -> list[str]:
    if axis != "agents":
        return bundle["annotations"][axis][task_id]["capabilities"]
    tools = bundle["annotations"]["tools"][task_id]["capabilities"]
    owners = bundle["catalog_maps"]["tools"]
    return sorted({str(owners[tool]["owner"]).strip() for tool in tools})


def _public_prompt(payload: dict[str, Any]) -> str:
    for key in ("user_prompt", "task_prompt", "instruction"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    raise BundleError("normalized task has no public prompt")


def _house_rules_text(bundle: dict[str, Any]) -> str:
    value = bundle.get("house_rules")
    if value is None:
        value = bundle["spec"].get("house_rules")
    if isinstance(value, dict):
        value = value.get("rules") or value.get("text") or []
    if isinstance(value, list):
        text = "# Shared house rules\n\n" + "\n".join(
            f"- {str(item).strip()}" for item in value if str(item).strip()
        )
    else:
        text = str(value or "").strip()
    if not text:
        text = (
            "# Shared house rules\n\n"
            "- Read the complete task before acting.\n"
            "- Work only in the provided environment and preserve supplied inputs.\n"
            "- Produce requested artifacts at their exact paths and validate them.\n"
            "- Do not inspect or rely on held-out solutions, rubrics, or verifier internals."
        )
    return text.rstrip() + "\n"


def _skill_prompt_suffix(task_tools: list[str]) -> str:
    if not task_tools:
        return ""
    allowed = ", ".join(f"`{tool}`" for tool in sorted(task_tools))
    return (
        "## Evaluation tool/software restriction\n"
        f"For this evaluation, use only the task-oracle software: {allowed}. "
        "The skills axis evolves procedural guidance, not tool availability."
    )


def _agent_system_prompt(bundle: dict[str, Any], owners: list[str]) -> str:
    owner_tools: dict[str, list[str]] = {}
    for name, item in bundle["catalog_maps"]["tools"].items():
        owner_tools.setdefault(str(item["owner"]).strip(), []).append(name)
    lines = [
        "# Evolving-agents lead orchestrator",
        "",
        "You are the tool-less lead orchestrator. Do not execute specialist-owned "
        "software directly. Delegate each operation to an available owner, preserve "
        "task context across handoffs, and integrate and validate the final result.",
        "",
        "## Available specialist roster",
        "",
    ]
    for owner in owners:
        tools = ", ".join(f"`{tool}`" for tool in sorted(owner_tools[owner]))
        lines.append(f"- **{owner}** — owns {tools}")
    lines.extend([
        "",
        "Every specialist receives its materialized capability skill and the shared "
        "house rules. Use only the cumulative roster above.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _task_row(bundle: dict[str, Any], axis: str, task_id: str, stage: dict[str, Any], split: str) -> dict[str, Any]:
    record = bundle["tasks"][task_id]
    row = dict(record["payload"])
    public_prompt = _public_prompt(row)
    row.update({
        "domain": bundle["spec"]["benchmark"]["domain"],
        "version": f"v{stage['version']}",
        "split": split,
        "task_split": split,
        "task_id": task_id,
        "source_commit": bundle["spec"]["benchmark"]["source_commit"],
        "repository_url": bundle["spec"]["benchmark"]["source_url"],
        "annotation_evidence": _annotation_evidence(bundle, axis, task_id),
    })
    oracle_field, cumulative_field = AXIS_FIELDS[axis]
    row[oracle_field] = _axis_capabilities(bundle, axis, task_id)
    row[cumulative_field] = stage["cumulative"]
    tool_annotation = bundle["annotations"].get("tools", {}).get(task_id)
    task_tools = tool_annotation["capabilities"] if tool_annotation else []
    if axis == "tools":
        row.pop("user_prompt", None)
        row["task_prompt"] = public_prompt
        row["software"] = stage["cumulative"]
    elif axis == "skills":
        row.pop("task_prompt", None)
        row["system_prompt"] = _house_rules_text(bundle)
        row["user_prompt"] = public_prompt
        row["patcher_prompts"] = bundle["spec"].get("skill_patcher_prompts") or {}
        row["prompt_suffix"] = _skill_prompt_suffix(task_tools)
        row["evaluation"] = str(bundle["spec"].get("evaluation") or "")
        row["software"] = task_tools
    else:
        owner_tools: dict[str, list[str]] = {}
        for name, item in bundle["catalog_maps"]["tools"].items():
            owner_tools.setdefault(str(item["owner"]).strip(), []).append(name)
        allowed = sorted({tool for owner in stage["cumulative"] for tool in owner_tools[owner]})
        row["oracle_tools"] = task_tools
        row["cummulative_tools"] = allowed
        row["software"] = allowed
        row["task_prompt"] = public_prompt
        row["user_prompt"] = public_prompt
        row["system_prompt"] = _agent_system_prompt(bundle, stage["cumulative"])
        skill_annotation = bundle["annotations"].get("skills", {}).get(task_id)
        row["oracle_skills"] = skill_annotation["capabilities"] if skill_annotation else []
    return row


def _materialize_skills(bundle: dict[str, Any], root: Path) -> None:
    oracle_root = root / "_oracle"
    oracle_root.mkdir(parents=True, exist_ok=False)
    (oracle_root / "stripped_system_prompt.txt").write_text(
        _house_rules_text(bundle), encoding="utf-8"
    )
    for item in bundle["catalogs"]["skills"]:
        slug = _name(item, bundle["root"] / "catalogs/skills.json")
        skill_root = root / "_oracle" / "skills" / slug
        procedure = str(item.get("procedure") or item.get("description") or "").strip()
        if not procedure:
            raise BundleError(f"skill {slug} has no procedure or description")
        title = str(item.get("title") or slug.replace("-", " ").title())
        description = str(item.get("description") or procedure.splitlines()[0])
        skill_root.mkdir(parents=True, exist_ok=False)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {slug}\ndescription: {description}\n---\n\n# {title}\n\n{procedure}\n",
            encoding="utf-8",
        )
        _write_json(skill_root / "index.json", {
            "name": slug,
            "title": title,
            "description": description,
            "provenance": item.get("provenance", {}),
            "matching_rule": item.get("matching_rule", {}),
            "signature": item.get("signature", {}),
        })


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _validate_generated_toml(text: str) -> None:
    if tomllib is not None:
        tomllib.loads(text)
        return
    # The portable engine emits only flat string and string-array values.  On
    # Python 3.10, validate that deliberately small subset without a dependency.
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.strip().startswith("[") and line.strip().endswith("]"):
            continue
        if "=" not in line:
            raise ValueError("expected key = value")
        key, value = line.split("=", 1)
        if not key.strip().replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"invalid key: {key.strip()!r}")
        json.loads(value.strip())


def _skill_agent_partition(bundle: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    """Route approved skill atoms to owners or the shared agent baseline."""
    owner_tools: dict[str, list[str]] = {}
    for name, item in bundle["catalog_maps"]["tools"].items():
        owner_tools.setdefault(str(item["owner"]).strip(), []).append(name)
    owner_skills: dict[str, set[str]] = {owner: set() for owner in owner_tools}
    shared: set[str] = set()
    skill_map = bundle["catalog_maps"].get("skills", {})
    for skill, item in sorted(skill_map.items()):
        scope = str(item.get("agent_scope") or "").strip().casefold()
        explicit_owners = sorted({str(value).strip() for value in item.get("owner_any") or [] if str(value).strip()})
        if scope == "shared":
            shared.add(skill)
            continue
        if explicit_owners:
            unknown = sorted(set(explicit_owners) - set(owner_tools))
            if unknown:
                raise BundleError(f"skill {skill} names unknown agent owners: {unknown}")
            for owner in explicit_owners:
                owner_skills[owner].add(skill)
            continue
        # Canonical fallback for a new seed: route by deterministic task
        # co-occurrence between the accepted skill and accepted tool owners.
        for task_id, annotation in bundle["annotations"].get("skills", {}).items():
            if skill not in annotation["capabilities"]:
                continue
            tool_annotation = bundle["annotations"].get("tools", {}).get(task_id)
            if not tool_annotation:
                continue
            for tool in tool_annotation["capabilities"]:
                owner_skills[str(bundle["catalog_maps"]["tools"][tool]["owner"]).strip()].add(skill)
        if not any(skill in values for values in owner_skills.values()):
            shared.add(skill)
    return {owner: sorted(values) for owner, values in owner_skills.items()}, sorted(shared)


def _agent_materialization(bundle: dict[str, Any]) -> dict[str, Any]:
    owner_tools: dict[str, list[str]] = {}
    for name, item in bundle["catalog_maps"]["tools"].items():
        owner_tools.setdefault(str(item["owner"]).strip(), []).append(name)
    owner_skills, shared_skills = _skill_agent_partition(bundle)
    skill_map = bundle["catalog_maps"].get("skills", {})
    shared_parts = [_house_rules_text(bundle).rstrip()]
    if shared_skills:
        shared_parts.extend(["", "## Reused procedural skills"])
        for skill in shared_skills:
            item = skill_map[skill]
            procedure = str(item.get("procedure") or item.get("description") or "").strip()
            shared_parts.extend(["", f"### {skill}", "", procedure])
    shared_text = "\n".join(shared_parts).rstrip() + "\n"
    family_text: dict[str, str] = {}
    agents: dict[str, str] = {}
    manifest_agents: list[dict[str, Any]] = []
    for owner in sorted(owner_tools):
        tools = sorted(owner_tools[owner])
        description = f"Route tasks requiring {', '.join(tools)} to this specialist."
        procedures: list[str] = []
        for tool in tools:
            item = bundle["catalog_maps"]["tools"][tool]
            procedure = str(item.get("agent_procedure") or item.get("procedure") or item.get("description") or "").strip()
            if not procedure:
                raise BundleError(f"tool {tool} has no description/procedure to materialize for owner {owner}")
            procedures.extend([f"### {tool}", "", procedure, ""])
        skill_procedures: list[str] = []
        for skill in owner_skills[owner]:
            item = skill_map[skill]
            procedure = str(item.get("procedure") or item.get("description") or "").strip()
            if not procedure:
                raise BundleError(f"skill {skill} has no procedure to re-home into agents")
            skill_procedures.extend([f"### {skill}", "", procedure, ""])
        family_text[owner] = (
            f"---\nname: {owner}\ndescription: {description}\n---\n\n"
            f"# {owner}\n\n## Scope\n\nOwn and execute work requiring "
            f"{', '.join(f'`{tool}`' for tool in tools)}. Coordinate through the lead "
            "orchestrator when another owner is needed.\n\n## Operating procedure\n\n"
            "Read the task, perform only the delegated work, validate it, and return exact "
            "artifact paths and results.\n\n## Owned tool capabilities\n\n"
            + "\n".join(procedures).rstrip()
            + ("\n\n## Reused procedural skills\n\n" + "\n".join(skill_procedures).rstrip() if skill_procedures else "")
            + "\n"
        )
        developer = (
            f"You are the {owner} specialist. Read agent_skills/{owner}/SKILL.md and "
            "agent_skills/_shared/house_rules.md. Do only the delegated portion, validate "
            "it, and report artifact paths and results to the orchestrator."
        )
        agents[owner] = (
            "name = " + _toml_string(owner) + "\n"
            "description = " + _toml_string(description) + "\n"
            "developer_instructions = " + _toml_string(developer) + "\n"
            "owned_software = [" + ", ".join(_toml_string(tool) for tool in tools) + "]\n\n"
            "[[skills.config]]\n"
            "path = " + _toml_string(f"agent_skills/{owner}/SKILL.md") + "\n"
            "enabled = true\n\n"
            "[tools]\n"
            "exec_command = true\n"
            "shell_tool = true\n"
            "apply_patch = true\n"
        )
        manifest_agents.append({
            "name": owner,
            "owned_software": tools,
            "file": f"agents/{owner}.toml",
            "skill": f"../_capabilities/{owner}/SKILL.md",
            "rehomed_skills": owner_skills[owner],
        })
    return {
        "owner_tools": {owner: sorted(tools) for owner, tools in sorted(owner_tools.items())},
        "owner_skills": owner_skills,
        "shared_skills": shared_skills,
        "shared_text": shared_text,
        "family_text": family_text,
        "agents": agents,
        "manifest": {
        "agents": manifest_agents,
        "owners": {owner: sorted(tools) for owner, tools in sorted(owner_tools.items())},
        "shared_house_rules": "../_capabilities/_shared/house_rules.md",
        "shared_skills": shared_skills,
        },
    }


def _materialize_agents(bundle: dict[str, Any], root: Path, plan: dict[str, Any]) -> None:
    material = _agent_materialization(bundle)
    capabilities_root = root / "_capabilities"
    shared_root = capabilities_root / "_shared"
    shared_root.mkdir(parents=True, exist_ok=False)
    (shared_root / "house_rules.md").write_text(material["shared_text"], encoding="utf-8")
    for owner, content in material["family_text"].items():
        skill_root = capabilities_root / owner
        skill_root.mkdir(parents=True, exist_ok=False)
        (skill_root / "SKILL.md").write_text(content, encoding="utf-8")
    all_root = root / "_agents"
    agent_root = all_root / "agents"
    agent_root.mkdir(parents=True, exist_ok=False)
    for owner, content in material["agents"].items():
        (agent_root / f"{owner}.toml").write_text(content, encoding="utf-8")
    _write_json(all_root / "manifest.json", material["manifest"])
    for stage in plan["stages"]:
        version_root = root / f"v{stage['version']}"
        for owner in stage["cumulative"]:
            target_skill = version_root / "agent_skills" / owner
            target_skill.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(capabilities_root / owner, target_skill)
            target_agents = version_root / "agents"
            target_agents.mkdir(parents=True, exist_ok=True)
            shutil.copy2(all_root / "agents" / f"{owner}.toml", target_agents / f"{owner}.toml")
        version_shared = version_root / "agent_skills" / "_shared"
        version_shared.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shared_root / "house_rules.md", version_shared / "house_rules.md")
        _write_json(version_root / "manifest.json", {
            "version": stage["version"],
            "cumulative_agents": stage["cumulative"],
            "introduced_agents": stage["introduced"],
            "agents": [
                {
                    "name": owner,
                    "owned_software": material["owner_tools"][owner],
                    "agent": f"agents/{owner}.toml",
                    "skill": f"agent_skills/{owner}/SKILL.md",
                    "rehomed_skills": material["owner_skills"][owner],
                }
                for owner in stage["cumulative"]
            ],
            "shared_house_rules": "agent_skills/_shared/house_rules.md",
            "shared_skills": material["shared_skills"],
        })


def _axis_manifest(bundle: dict[str, Any], axis: str, plan: dict[str, Any]) -> dict[str, Any]:
    retained = sorted(plan["assignments"])
    all_tasks = sorted(bundle["tasks"])
    config = _constraints(bundle["spec"], axis)
    return {
        "format_version": FORMAT_VERSION,
        "axis": axis,
        "domain": bundle["spec"]["benchmark"]["domain"],
        "source_url": bundle["spec"]["benchmark"]["source_url"],
        "source_commit": bundle["spec"]["benchmark"]["source_commit"],
        "seed": int(bundle["spec"].get("seed", 42)),
        "discovered_tasks": len(all_tasks),
        "retained_tasks": len(retained),
        "excluded_tasks": [
            {"task_id": task_id, "reason": f"no accepted {axis} annotation"}
            for task_id in all_tasks if task_id not in plan["assignments"]
        ],
        "target_versions": config["target_versions"],
        "emitted_versions": len(plan["stages"]),
        "min_train": config["min_train"],
        "min_test": config["min_test"],
        "frequency": plan["frequency"],
        "ranked_capabilities": plan["ranked_capabilities"],
        "construction": {
            "implementation": "evolve-benchmark/scripts/evolve_core.py",
            "algorithm": "skill-sequencer" if axis == "skills" else "frequency-adaptive",
            "merges": plan["merges"],
            "constraints": config,
            "deviations": bundle["deviations"],
        },
        "versions": [
            {
                "version": f"v{stage['version']}",
                "train": len(stage["train"]),
                "test": len(stage["test"]),
                "introduced": stage["introduced"],
                "cumulative": stage["cumulative"],
            }
            for stage in plan["stages"]
        ],
    }


def build_candidate(bundle: dict[str, Any], output: Path) -> dict[str, Any]:
    _require_gates(bundle, ("suitability", "annotations", "staging"))
    plan = make_plan(bundle)
    _require_staging_plan(bundle, plan)
    output = output.resolve()
    if output.exists():
        raise BundleError(f"refusing to overwrite existing path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.building-{os.getpid()}"
    failed = output.parent / f"{output.name}.failed-{os.getpid()}"
    if temporary.exists() or failed.exists():
        raise BundleError("temporary build path already exists")
    temporary.mkdir()
    try:
        for axis in bundle["tracks"]:
            axis_root = temporary / axis
            axis_root.mkdir()
            axis_plan = plan["tracks"][axis]
            if axis == "skills":
                _materialize_skills(bundle, axis_root)
            if axis == "agents":
                _materialize_agents(bundle, axis_root, axis_plan)
            for stage in axis_plan["stages"]:
                for split in ("train", "test"):
                    rows = [
                        _task_row(bundle, axis, task_id, stage, split)
                        for task_id in stage[split]
                    ]
                    _write_jsonl(axis_root / f"v{stage['version']}" / f"{split}.jsonl", rows)
            evidence_axis = "tools" if axis == "agents" else axis
            _write_json(axis_root / "annotation_evidence.json", bundle["annotations"][evidence_axis])
            _write_json(axis_root / "manifest.json", _axis_manifest(bundle, axis, axis_plan))
        _write_json(temporary / "construction.json", {
            "format_version": FORMAT_VERSION,
            "benchmark": bundle["spec"]["benchmark"],
            "selected_tracks": bundle["tracks"],
            "deviations": bundle["deviations"],
            "approvals_at_build": bundle["approvals"],
        })
        temporary.rename(output)
    except Exception:
        if temporary.exists():
            temporary.rename(failed)
        raise
    return {"candidate": str(output), "tree_hash": tree_hash(output), "plan": plan}


def _validate_static(bundle: dict[str, Any], candidate: Path) -> list[str]:
    problems: list[str] = []
    try:
        plan = make_plan(bundle, require_approvals=False)
    except BundleError as exc:
        return [str(exc)]
    for axis in bundle["tracks"]:
        axis_root = candidate / axis
        manifest_path = axis_root / "manifest.json"
        if not manifest_path.is_file():
            problems.append(f"{axis}: missing manifest.json")
            continue
        manifest = _read_json(manifest_path)
        expected_manifest = _axis_manifest(bundle, axis, plan["tracks"][axis])
        if manifest != expected_manifest:
            problems.append(f"{axis}: manifest differs from deterministic plan")
        oracle_field, cumulative_field = AXIS_FIELDS[axis]
        seen: set[str] = set()
        previous: set[str] = set()
        for stage in plan["tracks"][axis]["stages"]:
            cumulative = set(stage["cumulative"])
            introduced = cumulative - previous
            if previous and not previous < cumulative:
                problems.append(f"{axis} v{stage['version']}: cumulative set did not grow strictly")
            for split in ("train", "test"):
                path = axis_root / f"v{stage['version']}" / f"{split}.jsonl"
                if not path.is_file():
                    problems.append(f"{axis}: missing {path.relative_to(candidate)}")
                    continue
                rows = _read_jsonl(path)
                expected_rows = [
                    _task_row(bundle, axis, task_id, stage, split)
                    for task_id in stage[split]
                ]
                if rows != expected_rows:
                    problems.append(f"{axis} v{stage['version']}/{split}: rows differ from deterministic plan")
                for row in rows:
                    task_id = str(row.get("task_id", ""))
                    if task_id in seen:
                        problems.append(f"{axis}: duplicate emitted task {task_id}")
                    seen.add(task_id)
                    oracle = set(row.get(oracle_field) or [])
                    actual_cumulative = set(row.get(cumulative_field) or [])
                    if not oracle or not oracle <= actual_cumulative:
                        problems.append(f"{axis}/{task_id}: oracle is empty or outside cumulative set")
                    if not oracle & introduced:
                        problems.append(f"{axis}/{task_id}: task does not depend on a newly introduced capability")
                    if row.get("split") != split or row.get("version") != f"v{stage['version']}":
                        problems.append(f"{axis}/{task_id}: row stage or split drift")
                    missing_fields = sorted(REQUIRED_ROW_FIELDS[axis] - set(row))
                    if missing_fields:
                        problems.append(f"{axis}/{task_id}: schema missing fields {missing_fields}")
                    if axis in {"skills", "agents"}:
                        if not str(row.get("system_prompt") or "").strip():
                            problems.append(f"{axis}/{task_id}: empty system_prompt")
                        if not str(row.get("user_prompt") or "").strip():
                            problems.append(f"{axis}/{task_id}: empty user_prompt")
                    if axis == "skills" and row.get("system_prompt") != _house_rules_text(bundle):
                        problems.append(f"skills/{task_id}: system_prompt differs from house rules")
                    if axis == "agents":
                        expected_skills = (
                            bundle["annotations"].get("skills", {}).get(task_id, {}).get("capabilities", [])
                        )
                        if row.get("oracle_skills") != expected_skills:
                            problems.append(f"agents/{task_id}: oracle_skills were not propagated")
                        if row.get("system_prompt") != _agent_system_prompt(bundle, stage["cumulative"]):
                            problems.append(f"agents/{task_id}: system_prompt differs from cumulative roster")
            previous = cumulative
        if seen != set(plan["tracks"][axis]["assignments"]):
            problems.append(f"{axis}: emitted task membership differs from the plan")
        if axis == "skills":
            house_path = axis_root / "_oracle" / "stripped_system_prompt.txt"
            if not house_path.is_file():
                problems.append("skills: missing materialized stripped_system_prompt.txt")
            elif house_path.read_text(encoding="utf-8") != _house_rules_text(bundle):
                problems.append("skills: materialized house rules differ from row system_prompt")
            for item in bundle["catalogs"]["skills"]:
                slug = _name(item, bundle["root"] / "catalogs/skills.json")
                for filename in ("SKILL.md", "index.json"):
                    if not (axis_root / "_oracle" / "skills" / slug / filename).is_file():
                        problems.append(f"skills: missing materialized {slug}/{filename}")
        if axis == "agents":
            material = _agent_materialization(bundle)
            shared_path = axis_root / "_capabilities" / "_shared" / "house_rules.md"
            if not shared_path.is_file() or shared_path.read_text(encoding="utf-8") != material["shared_text"]:
                problems.append("agents: missing or stale shared house-rules materialization")
            for owner, expected_text in material["family_text"].items():
                skill_path = axis_root / "_capabilities" / owner / "SKILL.md"
                if not skill_path.is_file() or skill_path.read_text(encoding="utf-8") != expected_text:
                    problems.append(f"agents: missing or stale owner skill {owner}")
                agent_path = axis_root / "_agents" / "agents" / f"{owner}.toml"
                if not agent_path.is_file():
                    problems.append(f"agents: missing agent TOML {owner}")
                elif agent_path.read_text(encoding="utf-8") != material["agents"][owner]:
                    problems.append(f"agents: stale agent TOML {owner}")
            for path in (axis_root / "_agents" / "agents").glob("*.toml"):
                try:
                    _validate_generated_toml(path.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    problems.append(f"agents: invalid {path.name}: {exc}")
            combined = " ".join(material["family_text"].values()) + " " + material["shared_text"]
            normalized_combined = " ".join(combined.split())
            for skill, item in bundle["catalog_maps"].get("skills", {}).items():
                procedure = str(item.get("procedure") or item.get("description") or "").strip()
                if procedure and " ".join(procedure.split()) not in normalized_combined:
                    problems.append(f"agents: procedure content dropped for skill {skill}")
    return problems


def _run_smoke(bundle: dict[str, Any], candidate: Path) -> dict[str, Any]:
    grader = bundle["spec"].get("grader") or {}
    command = grader.get("command")
    task_ids = grader.get("smoke_task_ids") or []
    if not isinstance(command, list) or not command or not task_ids:
        return {"configured": False, "passed": False, "reason": "no executable grader command and smoke_task_ids"}
    unknown = sorted(set(task_ids) - set(bundle["tasks"]))
    if unknown:
        return {"configured": True, "passed": False, "reason": f"unknown smoke tasks: {unknown}"}
    timeout = float(grader.get("timeout_seconds", 600))
    results: list[dict[str, Any]] = []
    for task_id in task_ids:
        values = {
            "task_id": task_id,
            "candidate": str(candidate),
            "bundle": str(bundle["root"]),
        }
        rendered = [str(part).format(**values) for part in command]
        completed = subprocess.run(
            rendered,
            cwd=str(bundle["root"]),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        results.append({
            "task_id": task_id,
            "returncode": completed.returncode,
            "passed": completed.returncode == 0,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        })
    return {"configured": True, "passed": all(item["passed"] for item in results), "results": results}


def validate_candidate(bundle: dict[str, Any], candidate: Path, run_smoke: bool) -> dict[str, Any]:
    candidate = candidate.resolve()
    problems = _validate_static(bundle, candidate)
    smoke = _run_smoke(bundle, candidate) if run_smoke and not problems else {
        "configured": bool((bundle["spec"].get("grader") or {}).get("command")),
        "passed": False,
        "reason": "smoke not run" if not problems else "static validation failed",
    }
    runtime_validated = bool(run_smoke and smoke.get("configured") and smoke.get("passed"))
    return {
        "candidate": str(candidate),
        "tree_hash": tree_hash(candidate) if candidate.is_dir() else None,
        "static_passed": not problems,
        "runtime_validated": runtime_validated,
        "passed": not problems and runtime_validated,
        "status": "validated" if not problems and runtime_validated else ("preflight-only" if not problems else "failed"),
        "problems": problems,
        "smoke": smoke,
    }


def reproduce(bundle: dict[str, Any], output: Path, run_smoke: bool) -> dict[str, Any]:
    _require_gates(bundle, ("suitability", "annotations", "staging"))
    _require_staging_plan(bundle, make_plan(bundle))
    output = output.resolve()
    if output.exists():
        raise BundleError(f"refusing to overwrite existing path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{output.name}.reproduce-", dir=output.parent))
    first = work / "first"
    second = work / "second"
    completed = False
    try:
        build_candidate(bundle, first)
        build_candidate(bundle, second)
        first_entries = _tree_entries(first)
        second_entries = _tree_entries(second)
        if first_entries != second_entries:
            raise BundleError("fresh builds have different recursive file lists or bytes")
        first.rename(output)
        validation = validate_candidate(bundle, output, run_smoke=run_smoke)
        result = {
            "candidate": str(output),
            "reproducible": True,
            "tree_hash": tree_hash(output),
            "validation": validation,
        }
        completed = True
    finally:
        if completed:
            shutil.rmtree(work, ignore_errors=True)
        elif work.exists():
            failed = output.parent / f"{output.name}.failed-reproduce-{os.getpid()}"
            if not failed.exists():
                work.rename(failed)
    return result


def promote(bundle: dict[str, Any], candidate: Path, destination: Path, validation_report: Path, replace: bool) -> dict[str, Any]:
    _require_gates(bundle, ("suitability", "annotations", "staging", "promotion"))
    _require_staging_plan(bundle, make_plan(bundle))
    candidate = candidate.resolve()
    destination = destination.resolve()
    report = _read_json(validation_report.resolve())
    if report.get("passed") is not True or report.get("runtime_validated") is not True:
        raise BundleError("promotion requires a passing runtime validation report")
    if report.get("tree_hash") != tree_hash(candidate):
        raise BundleError("candidate changed after validation")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists():
        if not replace:
            raise BundleError(f"destination exists; pass --replace after explicit approval: {destination}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = destination.with_name(destination.name + f".backup-{stamp}")
        if backup.exists():
            raise BundleError(f"backup path already exists: {backup}")
        destination.rename(backup)
    temporary = destination.with_name(f".{destination.name}.promoting-{os.getpid()}")
    try:
        shutil.copytree(candidate, temporary)
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if backup is not None and not destination.exists():
            backup.rename(destination)
        raise
    return {
        "promoted": str(destination),
        "tree_hash": tree_hash(destination),
        "backup": str(backup) if backup else None,
    }


def _emit(value: Any, output: str | None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        path = Path(output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "plan"):
        command = sub.add_parser(name)
        command.add_argument("--bundle", required=True)
        command.add_argument("--report")
    build = sub.add_parser("build")
    build.add_argument("--bundle", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--report")
    validate = sub.add_parser("validate")
    validate.add_argument("--bundle", required=True)
    validate.add_argument("--candidate", required=True)
    validate.add_argument("--run-smoke", action="store_true")
    validate.add_argument("--report")
    repro = sub.add_parser("reproduce")
    repro.add_argument("--bundle", required=True)
    repro.add_argument("--output", required=True)
    repro.add_argument("--run-smoke", action="store_true")
    repro.add_argument("--report")
    promotion = sub.add_parser("promote")
    promotion.add_argument("--bundle", required=True)
    promotion.add_argument("--candidate", required=True)
    promotion.add_argument("--destination", required=True)
    promotion.add_argument("--validation-report", required=True)
    promotion.add_argument("--replace", action="store_true")
    promotion.add_argument("--report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        bundle = load_bundle(Path(args.bundle))
        if args.command == "inspect":
            result = inspect_bundle(bundle)
        elif args.command == "plan":
            result = make_plan(bundle)
        elif args.command == "build":
            result = build_candidate(bundle, Path(args.output))
        elif args.command == "validate":
            result = validate_candidate(bundle, Path(args.candidate), args.run_smoke)
        elif args.command == "reproduce":
            result = reproduce(bundle, Path(args.output), args.run_smoke)
        else:
            result = promote(
                bundle,
                Path(args.candidate),
                Path(args.destination),
                Path(args.validation_report),
                args.replace,
            )
        _emit(result, getattr(args, "report", None))
        if args.command == "validate" and result.get("passed") is not True:
            return 1
        if args.command == "reproduce" and result["validation"].get("passed") is not True:
            return 1
        return 0
    except (BundleError, OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"evolve_core: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
