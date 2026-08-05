"""Evolving resources: tools / skills / agents, in oracle | accumulative | none.

The three datasets each evolve a different *resource* over the version axis (=
curriculum stage). This module lets a caller **inspect** that resource for a
task and **select** how much of it to use:

    dataset            resource   oracle field        cumulative field
    ---------------    --------   -----------------   --------------------------
    evovling_tools     tools      oracle_tools        cummulative_tools
    evovling_skills    skills     oracle_skills       cummulative_oracle_skills
    evovling_agents    agents     oracle_agents       cumulative_agents

Modes:
  * ``oracle``       — the minimal gold set for *this* task (skyline / upper bound).
  * ``accumulative`` — the whole set accumulated *up to the task's stage* (the
                       realistic evolving setting; includes distractors). With
                       stage ``full`` this is the entire universe (last stage).
  * ``none``         — nothing (baseline).

For ``skills`` and ``agents`` the names resolve to on-disk **bundles**:
  * skills: ``evovling_skills/<benchmark>[/<domain>]/_oracle/skills/<slug>/`` —
    ``SKILL.md`` (+ ``references/*.md``, ``index.json``).
  * agents: ``evovling_agents/<benchmark>[/<domain>]/v<k>/agents/<name>.toml``
    plus that agent's ``agent_skills/<name>/`` bundle.
``tools`` are just names — the gym MCP server already exposes them; the set is
an (advisory) allowlist the caller enforces on its own agent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import loader

# --------------------------------------------------------------------------- #
# Kind / mode taxonomy                                                         #
# --------------------------------------------------------------------------- #
KIND_BY_DATASET = {
    "evovling_tools": "tools",
    "evovling_skills": "skills",
    "evovling_agents": "agents",
}
MODES = ("oracle", "accumulative", "none")

# Field spellings per kind (dataset ships the typo'd ``cummulative_*`` for
# tools/skills but ``cumulative_agents`` for agents — tolerate all).
_ORACLE_FIELDS = {
    "tools": ("oracle_tools",),
    "skills": ("oracle_skills",),
    "agents": ("oracle_agents",),
}
_CUMULATIVE_FIELDS = {
    "tools": ("cummulative_tools", "cumulative_tools"),
    "skills": ("cummulative_oracle_skills", "cumulative_oracle_skills"),
    "agents": ("cumulative_agents", "cummulative_agents"),
}

# Per-file content cap when inlining bundle contents.
MAX_CONTENT_BYTES = int(os.environ.get("EVAL_SERVICE_RESOURCE_MAX_BYTES", str(256 * 1024)))

_NOTES: dict[tuple[str, str], str] = {
    ("tools", "oracle"): "Minimal gold tool set for this task (oracle/skyline). The gym MCP server still exposes its full surface; use as an allowlist hint.",
    ("tools", "accumulative"): "Cumulative tool universe at this stage (the realistic evolving-tools setting; includes distractors). Enforce as the agent's allowlist.",
    ("tools", "none"): "No tools provided (baseline).",
    ("skills", "oracle"): "Held-out gold SKILL.md bundle(s) this task exercises (oracle/skyline; normally hidden). Mount to upper-bound. Realistic setting is 'none' (the agent authors its own library).",
    ("skills", "accumulative"): "Cumulative gold skill set active at this stage (skyline). Realistic setting is 'none'.",
    ("skills", "none"): "Empty skill library — the realistic evolving-skills setting (agent discovers/authors its own skills).",
    ("agents", "oracle"): "This task's gold specialist sub-agents (oracle/skyline).",
    ("agents", "accumulative"): "The specialist pool the orchestrator mounts at this stage (the realistic evolving-agents setting; includes distractor agents).",
    ("agents", "none"): "No sub-agents (baseline — orchestrator solo).",
}


def kind_for(dataset: str) -> str:
    return KIND_BY_DATASET.get(dataset, "unknown")


def normalize_mode(mode: str | None, kind: str) -> str:
    """Coerce/validate a mode; ``None`` picks the per-kind realistic default."""
    if mode is None or mode == "":
        # skills' realistic setting is an empty (agent-authored) library; tools
        # and agents ship the accumulated universe as their realistic setting.
        return "none" if kind == "skills" else "accumulative"
    m = str(mode).strip().lower()
    if m in ("accumulate", "cumulative", "cummulative"):
        m = "accumulative"
    if m not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    return m


# --------------------------------------------------------------------------- #
# Name sets                                                                    #
# --------------------------------------------------------------------------- #
def _row_raw(row: Any) -> dict[str, Any]:
    return getattr(row, "raw", None) or {}


def _first_field(raw: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    for n in names:
        v = raw.get(n)
        if v:
            return [str(x) for x in v]
    return []


def full_universe(dataset: str, benchmark: str, domain: str | None) -> list[str]:
    """The complete accumulated resource set (cumulative set at the last stage).

    Cumulative sets are monotonic (``C_1 ⊆ … ⊆ C_K``), so the last version's
    cumulative field is the whole universe. Read it from any row of that stage.
    """
    kind = kind_for(dataset)
    fields = _CUMULATIVE_FIELDS.get(kind)
    if not fields:
        return []
    vers = loader.versions(dataset, benchmark, domain)
    if not vers:
        return []
    last = max(vers)
    for split in ("test", "train"):
        try:
            for r in loader.iter_rows(dataset, benchmark, last, split, domain):
                vals = _first_field(_row_raw(r), fields)
                if vals:
                    return sorted(set(vals))
        except FileNotFoundError:
            continue
    return []


def names_for(
    dataset: str, row: Any, mode: str, *, full_names: list[str] | None = None
) -> list[str]:
    """The resource names for ``mode``. ``full_names`` overrides the cumulative
    set (used for stage ``full`` → the whole universe)."""
    kind = kind_for(dataset)
    if kind == "unknown" or mode == "none":
        return []
    raw = _row_raw(row)
    if mode == "oracle":
        return _first_field(raw, _ORACLE_FIELDS[kind])
    if mode == "accumulative":
        if full_names is not None:
            return list(full_names)
        return _first_field(raw, _CUMULATIVE_FIELDS[kind])
    return []


# --------------------------------------------------------------------------- #
# On-disk bundle resolution (skills + agents)                                  #
# --------------------------------------------------------------------------- #
def _skills_oracle_root(benchmark: str, domain: str | None) -> Path:
    base = loader.DATA_ROOT / "evovling_skills" / benchmark
    if domain:
        base = base / domain
    return base / "_oracle" / "skills"


def _agents_version_dir(benchmark: str, domain: str | None, version: int) -> Path:
    base = loader.DATA_ROOT / "evovling_agents" / benchmark
    if domain:
        base = base / domain
    return base / f"v{version}"


def _read_files(paths: list[Path], include_content: bool) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        entry: dict[str, Any] = {
            "path": str(p.relative_to(loader.DATA_ROOT)),
            "size": size,
        }
        if include_content:
            if 0 <= size <= MAX_CONTENT_BYTES:
                try:
                    entry["content"] = p.read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001 - binary / unreadable
                    entry["content"] = None
                    entry["error"] = "not-utf8-or-unreadable"
            else:
                entry["truncated"] = True
        files.append(entry)
    return files


def _skill_items(
    benchmark: str, domain: str | None, names: list[str], include_content: bool
) -> list[dict[str, Any]]:
    root = _skills_oracle_root(benchmark, domain)
    items = []
    for n in names:
        d = root / n
        paths = sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []
        items.append({"name": n, "present": d.is_dir(), "files": _read_files(paths, include_content)})
    return items


def _agent_manifest_index(agents_dir: Path) -> dict[str, dict[str, Any]]:
    """Map agent name -> its ``manifest.json`` entry (tools/description/title).

    The published pool's ``agents/manifest.json`` carries the per-agent
    ``oracle_tools`` (EOG) / ``software`` (ALE), agentized ``description`` and
    ``title`` — everything a caller needs to reconstruct a scoped specialist
    without the local capability library. Missing/unreadable -> empty index.
    """
    mf = agents_dir / "manifest.json"
    if not mf.is_file():
        return {}
    try:
        import json
        data = json.loads(mf.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {e.get("name"): e for e in data.get("agents", []) if e.get("name")}


def _agent_items(
    dataset: str, benchmark: str, domain: str | None, version: int | str,
    names: list[str], include_content: bool,
) -> list[dict[str, Any]]:
    # agents live per-version; for `full` use the last (largest) pool.
    if loader.is_full_version(version):
        vers = loader.versions(dataset, benchmark, domain)
        av = max(vers) if vers else 1
    else:
        av = int(version)
    vdir = _agents_version_dir(benchmark, domain, av)
    agents_dir, skills_dir = vdir / "agents", vdir / "agent_skills"
    manifest = _agent_manifest_index(agents_dir)
    items = []
    for n in names:
        toml = agents_dir / f"{n}.toml"
        paths = [toml]
        sk = skills_dir / n
        if sk.is_dir():
            paths += sorted(p for p in sk.rglob("*") if p.is_file())
        entry = manifest.get(n, {})
        items.append({
            "name": n,
            "present": toml.is_file(),
            "files": _read_files(paths, include_content),
            # Reconstruction metadata (scoped tools + routing hint) so a remote
            # caller can rebuild the specialist without the capability library.
            "tools": [str(t) for t in (entry.get("oracle_tools") or [])],
            "description": str(entry.get("description") or ""),
            "title": str(entry.get("title") or ""),
        })
    return items


# --------------------------------------------------------------------------- #
# Public: resolve a resource view for a task                                   #
# --------------------------------------------------------------------------- #
def resolve(
    *,
    dataset: str,
    benchmark: str,
    version: int | str,
    domain: str | None,
    row: Any,
    mode: str | None,
    include_content: bool = False,
) -> dict[str, Any]:
    """Return ``{kind, mode, stage, count, names, items, note}`` for a task.

    ``items`` carries the on-disk bundle (file paths + sizes, and inline
    ``content`` when ``include_content``) for skills/agents; empty for tools.
    """
    kind = kind_for(dataset)
    mode = normalize_mode(mode, kind)
    is_full = loader.is_full_version(version)
    stage = "full" if is_full else f"v{int(version)}"

    full_names = None
    if is_full and mode == "accumulative":
        full_names = full_universe(dataset, benchmark, domain)
    names = names_for(dataset, row, mode, full_names=full_names)

    items: list[dict[str, Any]] = []
    if mode != "none":
        if kind == "skills":
            items = _skill_items(benchmark, domain, names, include_content)
        elif kind == "agents":
            items = _agent_items(dataset, benchmark, domain, version, names, include_content)

    return {
        "kind": kind,
        "mode": mode,
        "stage": stage,
        "count": len(names),
        "names": names,
        "items": items,
        "note": _NOTES.get((kind, mode), ""),
    }


# --------------------------------------------------------------------------- #
# Materialize a skills bundle on disk (for a server-side skills-track run)     #
# --------------------------------------------------------------------------- #
def materialize_skills(
    *,
    benchmark: str,
    domain: str | None,
    version: int | str,
    row: Any,
    mode: str | None,
    dest_dir: Path,
) -> int:
    """Copy the resolved skill bundle(s) into ``dest_dir`` (returns #copied).

    This is the run-time counterpart of :func:`resolve` for the skills track:
    the hosted Codex runner takes a ``shared_skills_dir`` and mounts whatever
    ``SKILL.md`` bundles live under it. ``mode='none'`` (the realistic
    evolving-skills setting -- the agent authors its own library) copies
    nothing, matching a bare gym-only run.
    """
    mode = normalize_mode(mode, "skills")
    if mode == "none":
        return 0
    is_full = loader.is_full_version(version)
    full_names = (
        full_universe("evovling_skills", benchmark, domain)
        if (is_full and mode == "accumulative") else None
    )
    names = names_for("evovling_skills", row, mode, full_names=full_names)
    root = _skills_oracle_root(benchmark, domain)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    import shutil
    copied = 0
    for name in names:
        src = root / name
        if src.is_dir():
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
            copied += 1
    return copied
