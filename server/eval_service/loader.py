"""Dataset discovery + row normalization across the three on-disk trees.

On disk (mirrors the three HF datasets):

    data/<dataset>/<benchmark>/<domain>/v<k>/{train,test}.jsonl     # eog
    data/<dataset>/<benchmark>/v<k>/{train,test}.jsonl              # ale (flat)

where ``dataset`` ∈ {evovling_tools, evovling_skills, evovling_agents} and
``benchmark`` ∈ {eog, ale}.

The two encodings differ: ``evovling_tools`` stores ``gym_servers_config`` and
``verifiers`` as **JSON strings** (single stable Arrow schema), while
``evovling_skills`` stores them as real objects. We normalize both to parsed
objects and hand back the harness's own ``TaskRow`` dataclass, so the verifier /
endpoint code consumes them unchanged.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

# Reuse the harness's TaskRow (parsed-object shape the verifier expects).
from ._harness.dataset import TaskRow

_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parent.parent  # .../server

DATA_ROOT = Path(os.environ.get("EVAL_SERVICE_DATA_ROOT", REPO_ROOT / "data")).resolve()

KNOWN_DATASETS = ("evovling_tools", "evovling_skills", "evovling_agents")
EOG_BENCHMARK = "eog"
ALE_BENCHMARK = "ale"

# Stage sentinel: use *all* versions' rows (ignore the per-stage placement).
ALL_VERSIONS = "full"


def is_full_version(version: Any) -> bool:
    """True when ``version`` selects the whole stream (all stages)."""
    return isinstance(version, str) and version.strip().lower() in ("full", "all")


def parse_version(version: Any) -> int | str:
    """Coerce a wire ``version`` to ``int`` (a stage) or the ``full`` sentinel."""
    if is_full_version(version):
        return ALL_VERSIONS
    try:
        iv = int(version)
    except (TypeError, ValueError):
        raise ValueError(f"version must be a positive int or 'full', got {version!r}")
    if iv < 1:
        raise ValueError(f"version must be >= 1 (or 'full'), got {iv}")
    return iv


# --------------------------------------------------------------------------- #
# Path resolution                                                             #
# --------------------------------------------------------------------------- #
def _benchmark_root(dataset: str, benchmark: str) -> Path:
    return DATA_ROOT / dataset / benchmark


def is_flat(dataset: str, benchmark: str) -> bool:
    """True when versions live directly under the benchmark root (ale)."""
    root = _benchmark_root(dataset, benchmark)
    return bool(_version_dirs(root))


def is_eog(benchmark: str) -> bool:
    return benchmark == EOG_BENCHMARK


def is_ale(benchmark: str) -> bool:
    return benchmark == ALE_BENCHMARK


def _version_dirs(base: Path) -> list[int]:
    if not base.is_dir():
        return []
    out: list[int] = []
    for p in base.iterdir():
        if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit():
            out.append(int(p.name[1:]))
    return sorted(out)


def split_file(
    dataset: str, benchmark: str, version: int, split: str, domain: str | None
) -> Path:
    if split not in ("train", "test"):
        raise ValueError(f"split must be train|test, got {split!r}")
    base = _benchmark_root(dataset, benchmark)
    if not is_flat(dataset, benchmark):
        if not domain:
            raise ValueError(f"domain is required for benchmark {benchmark!r}")
        base = base / domain
    return base / f"v{version}" / f"{split}.jsonl"


def domains(dataset: str, benchmark: str) -> list[str]:
    root = _benchmark_root(dataset, benchmark)
    if not root.is_dir():
        return []
    if is_flat(dataset, benchmark):
        return []  # flat (ale): no domain axis
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith("_") and _version_dirs(p)
    )


def versions(dataset: str, benchmark: str, domain: str | None) -> list[int]:
    base = _benchmark_root(dataset, benchmark)
    if not is_flat(dataset, benchmark):
        if not domain:
            return []
        base = base / domain
    return _version_dirs(base)


# --------------------------------------------------------------------------- #
# Catalog (for /v1/benchmarks)                                                #
# --------------------------------------------------------------------------- #
def _count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    n = 0
    with p.open("rb") as f:
        for _ in f:
            n += 1
    return n


def catalog() -> dict[str, Any]:
    """Enumerate everything available on disk, with test/train counts."""
    out: dict[str, Any] = {"data_root": str(DATA_ROOT), "datasets": []}
    for dataset in KNOWN_DATASETS:
        droot = DATA_ROOT / dataset
        if not droot.is_dir():
            continue
        ds_entry: dict[str, Any] = {"dataset": dataset, "benchmarks": []}
        for benchmark in sorted(p.name for p in droot.iterdir() if p.is_dir()):
            flat = is_flat(dataset, benchmark)
            b_entry: dict[str, Any] = {
                "benchmark": benchmark,
                "flat": flat,
                "kind": "eog" if is_eog(benchmark) else ("ale" if is_ale(benchmark) else "unknown"),
                "domains": [],
            }
            dom_list = [None] if flat else domains(dataset, benchmark)
            for dom in dom_list:
                vers = versions(dataset, benchmark, dom)
                vinfo = []
                n_train_full = n_test_full = 0
                for v in vers:
                    nt = _count_lines(split_file(dataset, benchmark, v, "train", dom))
                    ns = _count_lines(split_file(dataset, benchmark, v, "test", dom))
                    n_train_full += nt
                    n_test_full += ns
                    vinfo.append({"version": v, "n_train": nt, "n_test": ns})
                b_entry["domains"].append(
                    {
                        "domain": dom,
                        "versions": vinfo,
                        # Each task is placed at a single stage, so the union
                        # across stages (selectable via version="full") is the
                        # sum of per-stage counts.
                        "full": {"n_train": n_train_full, "n_test": n_test_full},
                    }
                )
            ds_entry["benchmarks"].append(b_entry)
        out["datasets"].append(ds_entry)
    return out


# --------------------------------------------------------------------------- #
# Row loading + normalization                                                 #
# --------------------------------------------------------------------------- #
def _maybe_json(val: Any) -> Any:
    """``evovling_tools`` stores nested fields as JSON strings; decode them."""
    if isinstance(val, str):
        s = val.strip()
        if s[:1] in ("[", "{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return val
    return val


def _normalize_obj(obj: dict[str, Any]) -> dict[str, Any]:
    obj = dict(obj)
    if "gym_servers_config" in obj:
        obj["gym_servers_config"] = _maybe_json(obj["gym_servers_config"])
    if "verifiers" in obj:
        obj["verifiers"] = _maybe_json(obj["verifiers"])
    # evovling_tools uses ``oracle_tools``; the harness TaskRow reads
    # ``selected_tools``. Mirror it so the gold-tool hint is populated.
    if not obj.get("selected_tools") and obj.get("oracle_tools"):
        obj["selected_tools"] = obj["oracle_tools"]
    # ALE rows carry the agent-facing instruction in ``task_prompt`` (no
    # ``user_prompt``); surface it so the TaskView prompt isn't empty.
    if not obj.get("user_prompt") and obj.get("task_prompt"):
        obj["user_prompt"] = obj["task_prompt"]
    return obj


def _iter_one_version(
    dataset: str, benchmark: str, version: int, split: str, domain: str | None
) -> Iterator[TaskRow]:
    p = split_file(dataset, benchmark, version, split, domain)
    if not p.exists():
        raise FileNotFoundError(f"missing dataset file: {p}")
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield TaskRow.from_jsonl(_normalize_obj(json.loads(line)))


def iter_rows(
    dataset: str, benchmark: str, version: int | str, split: str, domain: str | None
) -> Iterator[TaskRow]:
    """Rows for a stage, or — with ``version='full'`` — every stage's rows
    unioned (each ``task_id`` once; a task is placed at a single stage per
    split, so the union is the whole stream)."""
    if is_full_version(version):
        seen: set[str] = set()
        for v in versions(dataset, benchmark, domain):
            try:
                for row in _iter_one_version(dataset, benchmark, v, split, domain):
                    if row.task_id in seen:
                        continue
                    seen.add(row.task_id)
                    yield row
            except FileNotFoundError:
                continue
        return
    yield from _iter_one_version(dataset, benchmark, int(version), split, domain)


@lru_cache(maxsize=256)
def _row_index(
    dataset: str, benchmark: str, version: int | str, split: str, domain: str | None
) -> tuple[str, ...]:
    return tuple(
        r.task_id for r in iter_rows(dataset, benchmark, version, split, domain)
    )


def list_task_ids(
    dataset: str, benchmark: str, version: int | str, split: str, domain: str | None,
    limit: int | None = None, offset: int = 0,
) -> list[str]:
    ids = list(_row_index(dataset, benchmark, version, split, domain))
    ids = ids[offset:]
    if limit is not None:
        ids = ids[:limit]
    return ids


def find_row(
    dataset: str, benchmark: str, version: int | str, split: str, domain: str | None,
    task_id: str,
) -> TaskRow:
    for r in iter_rows(dataset, benchmark, version, split, domain):
        if r.task_id == task_id:
            return r
    stage = "full" if is_full_version(version) else f"v{version}"
    raise KeyError(
        f"task_id {task_id!r} not found in "
        f"{dataset}/{benchmark}/{domain}/{stage}/{split}"
    )
