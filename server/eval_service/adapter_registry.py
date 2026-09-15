"""Versioned benchmark-adapter registry.

EOG and ALE are trusted built-ins.  Additional adapters are loaded only from an
explicit manifest and only when the caller approved the exact Python file hash.
The same registry is used by the hosted service and ``evolve-eval serve-local``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


@dataclass(frozen=True)
class AdapterDescriptor:
    benchmark: str
    adapter_id: str
    title: str
    execution: str
    action_types: tuple[str, ...]
    tracks: tuple[str, ...]
    modes: tuple[str, ...]
    harnesses: tuple[str, ...]
    runnable: bool = True
    source_url: str = ""
    source_commit: str = ""
    credentials: tuple[str, ...] = ()
    smoke_task_ids: tuple[str, ...] = ()
    deviations: tuple[dict[str, Any], ...] = ()
    runtime: dict[str, Any] = field(default_factory=dict)
    dataset_roots: dict[str, Path] = field(default_factory=dict)
    manifest_path: Path | None = None
    manifest_sha256: str = ""
    entrypoint: str = ""
    entrypoint_sha256: str = ""

    def wire(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "title": self.title,
            "execution": self.execution,
            "runnable": self.runnable,
            "action_types": list(self.action_types),
            "tracks": list(self.tracks),
            "modes": list(self.modes),
            "harnesses": list(self.harnesses),
            "source_url": self.source_url,
            "source_commit": self.source_commit,
            "adapter_manifest_sha256": self.manifest_sha256,
            "adapter_entrypoint_sha256": self.entrypoint_sha256,
            "credentials": list(self.credentials),
            "smoke_task_ids": list(self.smoke_task_ids),
            "deviations": list(self.deviations),
            "runtime": dict(self.runtime),
        }


_BUILTINS = {
    "eog": AdapterDescriptor(
        benchmark="eog", adapter_id="builtin-eog-v1", title="EvoHarnessBench-EOG",
        execution="hosted", action_types=("mcp",),
        tracks=("evovling_tools", "evovling_skills", "evovling_agents"),
        modes=("deployment", "self_evolving", "task_specific"),
        harnesses=("callable", "command", "react", "codex"),
    ),
    "ale": AdapterDescriptor(
        benchmark="ale", adapter_id="builtin-ale-v1", title="EvoHarnessBench-ALE",
        execution="hosted", action_types=("sandbox",),
        tracks=("evovling_tools", "evovling_skills", "evovling_agents"),
        modes=("deployment", "self_evolving", "task_specific"),
        harnesses=("callable", "command", "codex"),
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paths_from_env() -> list[Path]:
    raw = os.environ.get("EVAL_SERVICE_ADAPTER_MANIFESTS", "")
    return [Path(part).expanduser().resolve() for part in raw.split(os.pathsep) if part]


def read_manifest(path: str | Path) -> AdapterDescriptor:
    manifest = Path(path).expanduser().resolve()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("format_version") != 1:
        raise ValueError("adapter manifest format_version must be 1")
    benchmark = str(data.get("benchmark") or "")
    adapter_id = str(data.get("adapter_id") or "")
    if not _ID.fullmatch(benchmark) or not _ID.fullmatch(adapter_id):
        raise ValueError("benchmark and adapter_id must be safe lowercase identifiers")
    roots: dict[str, Path] = {}
    for dataset, value in (data.get("dataset_roots") or {}).items():
        if dataset not in ("evovling_tools", "evovling_skills", "evovling_agents"):
            raise ValueError(f"unknown evolving dataset {dataset!r}")
        root = (manifest.parent / str(value)).resolve()
        if not root.is_dir():
            raise ValueError(f"dataset root does not exist: {root}")
        roots[dataset] = root
    entrypoint = str(data.get("entrypoint") or "")
    entry_hash = str(data.get("entrypoint_sha256") or "")
    if entrypoint:
        rel, sep, _factory = entrypoint.partition(":")
        if not sep or not _factory:
            raise ValueError("entrypoint must be relative.py:factory")
        module = (manifest.parent / rel).resolve()
        try:
            module.relative_to(manifest.parent)
        except ValueError as exc:
            raise ValueError("adapter entrypoint escapes its bundle") from exc
        if not module.is_file() or _sha256(module) != entry_hash:
            raise ValueError("adapter entrypoint is missing or its SHA-256 does not match")
    caps = data.get("capabilities") or {}
    return AdapterDescriptor(
        benchmark=benchmark, adapter_id=adapter_id,
        title=str(data.get("title") or benchmark), execution="local",
        action_types=tuple(caps.get("action_types") or ("managed",)),
        tracks=tuple(caps.get("tracks") or roots.keys()),
        modes=tuple(caps.get("modes") or ("deployment", "self_evolving", "task_specific")),
        harnesses=tuple(caps.get("harnesses") or ("command", "codex")),
        runnable=bool(data.get("runnable", True)),
        source_url=str(data.get("source_url") or ""),
        source_commit=str(data.get("source_commit") or ""),
        credentials=tuple(data.get("credentials") or ()),
        smoke_task_ids=tuple(data.get("smoke_task_ids") or ()),
        deviations=tuple(data.get("deviations") or ()), dataset_roots=roots,
        runtime=dict(data.get("runtime") or {}),
        manifest_path=manifest, entrypoint=entrypoint,
        entrypoint_sha256=entry_hash,
        manifest_sha256=_sha256(manifest),
    )


def descriptors() -> dict[str, AdapterDescriptor]:
    found = dict(_BUILTINS)
    for path in _paths_from_env():
        item = read_manifest(path)
        if item.benchmark in found:
            raise ValueError(f"duplicate adapter for benchmark {item.benchmark!r}")
        found[item.benchmark] = item
    return found


def descriptor(benchmark: str) -> AdapterDescriptor | None:
    return descriptors().get(benchmark)


def dataset_root(dataset: str, benchmark: str) -> Path | None:
    item = descriptor(benchmark)
    return item.dataset_roots.get(dataset) if item else None


def load_factory(item: AdapterDescriptor) -> Callable[..., Any]:
    if not item.entrypoint or not item.manifest_path:
        raise ValueError(f"adapter {item.adapter_id!r} has no runtime entrypoint")
    approved = {x for x in os.environ.get("EVAL_SERVICE_APPROVED_ADAPTER_SHA256", "").split(",") if x}
    missing = {item.entrypoint_sha256, item.manifest_sha256} - approved
    if missing:
        raise PermissionError(
            "adapter manifest and code must both be explicitly approved: "
            + ", ".join(sorted(missing))
        )
    rel, _, factory_name = item.entrypoint.partition(":")
    path = (item.manifest_path.parent / rel).resolve()
    spec = importlib.util.spec_from_file_location(f"evolve_eval_adapter_{item.adapter_id}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load adapter module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise TypeError(f"adapter factory {factory_name!r} is not callable")
    return factory


def health(item: AdapterDescriptor) -> dict[str, Any]:
    """Return adapter health without importing unapproved code."""
    if item.benchmark in _BUILTINS:
        return {"ok": True, "status": "builtin"}
    if not item.runnable:
        return {"ok": False, "status": "disabled_by_manifest"}
    approved = {
        value for value in os.environ.get(
            "EVAL_SERVICE_APPROVED_ADAPTER_SHA256", ""
        ).split(",") if value
    }
    if not {item.entrypoint_sha256, item.manifest_sha256}.issubset(approved):
        return {"ok": False, "status": "adapter_not_approved"}
    try:
        environment = load_factory(item)(item)
        probe = getattr(environment, "health", None)
        result = probe() if callable(probe) else {"ok": True, "status": "loaded"}
        if not isinstance(result, dict):
            raise TypeError("health() must return an object")
        result = dict(result)
        result["ok"] = bool(result.get("ok"))
        return result
    except Exception as exc:  # noqa: BLE001 - health must be diagnostic
        return {"ok": False, "status": "load_failed", "detail": str(exc)}
