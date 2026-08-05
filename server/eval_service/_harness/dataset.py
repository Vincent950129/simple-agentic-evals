# Vendored verbatim from the EnterpriseOps-Gym harness (evovle_skills/src/dataset.py).
# Fix bugs upstream and re-copy; local edits would fork the grader.
"""
Dataset loader for `data/evovling_skills/<domain>/v_k/{train,test}.jsonl`.

Each row is produced by `evovle_skills/builder/export_dataset.py`.  Schema
v4 stores the entire pre-baked framing (stripped policy + ``# Setting`` +
``# Guardrails`` + ``# Skill library`` note) in ``system_prompt``, so a
row is a flat ``(system_prompt, user_prompt)`` pair plus metadata:

    {
      "domain": "itsm",
      "version": 1,
      "split": "train",
      "task_id": "task_...",
      "oracle_skills": ["3-3-incident-lifecycle-and-closure"],
      "cummulative_oracle_skills": [...],
      "system_prompt": "<stripped policy>\n\n# Setting\n...\n\n# Guardrails\n...\n\n# Skill library\n...",
      "user_prompt": "...",
      "patcher_prompts": {
        # Curator-side prompts for the post-stage BATCH skill-evolution
        # LLM call.  Same content for every row in a given build (the
        # curator is a global behaviour) -- shipped per-row so each row
        # is self-contained.  Two-stage rendering at trial time:
        #   1. inner per-trial blocks rendered from `trial_block_template`
        #   2. concatenated and substituted as `$trial_blocks` into
        #      `user_prompt_template` (the outer batch wrapper)
        "system_prompt":         "<PATCHER_SYSTEM_PROMPT>",
        "user_prompt_template":  "<outer batch wrapper, $snapshot_rendered + $n_trials + $trial_blocks>",
        "trial_block_template":  "<inner per-trial block, $task_id + $user_instruction + $trajectory_json + ...>"
      },
      "selected_tools": ["get_user", "list_incidents", ...],
      "mcp_endpoint": "/mcp",
      "gym_servers_config": [
        {
          "context": {"user-id": "", "x-itsm-user-token": "..."},
          "mcp_server_name": "gym-itsm-mcp",
          "mcp_server_url": "http://localhost:8006",
          "seed_database_file": "...",
          "user_info": {...},
        }
      ],
      "verifiers": [
        {
          "verifier_type": "database_state",
          "name": "verify_incident_reopened_in_progress",
          "gym_name": "gym-itsm-mcp",
          "validation_config": {
            "comparison_type": "equals",
            "expected_value": 1,
            "query": "SELECT COUNT(*) FROM incident WHERE ..."
          }
        }
      ]
    }

Older rows (schema v3 and below) shipped ``task_frame`` and
``skill_library_note`` as separate fields; both are still tolerated by
``from_jsonl`` as legacy reads but are otherwise ignored.  Run
``python -m evovle_skills.builder.bake_task_frame --all`` to migrate
old data into the v4 layout in place.

Note the field is the typo'd `cummulative_oracle_skills` (sic) -- we expose
both spellings on `TaskRow` for callers' convenience.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_ROOT


@dataclass
class TaskRow:
    """One row of the evovling_skills dataset."""

    domain: str
    version: int
    split: str
    task_id: str
    user_prompt: str
    system_prompt: str
    oracle_skills: list[str]
    cumulative_oracle_skills: list[str]
    selected_tools: list[str]
    mcp_endpoint: str
    gym_servers_config: list[dict[str, Any]]
    verifiers: list[dict[str, Any]]
    # Schema-v3 legacy fields, kept here only so that loading an
    # un-migrated JSONL still succeeds.  In v4 the same content is
    # baked directly into ``system_prompt`` and these fields stay
    # empty.  The harness no longer reads them; ``compose_system_prompt``
    # in ``codex_runner._build_instruction`` is the on-the-fly fallback
    # for un-baked rows.
    task_frame: str = ""
    skill_library_note: str = ""
    # Curator-side LLM prompts used by the post-stage BATCH
    # skill-evolution call.  Schema v6+ ships three keys:
    #   * ``system_prompt``         -- curator system message
    #   * ``user_prompt_template``  -- outer batch wrapper template
    #   * ``trial_block_template``  -- inner per-trial block template
    # The runner passes these to ``patcher.run_patcher_batch`` as
    # overrides.  Earlier schemas (v4-v5) shipped only the first two
    # keys; an empty dict or missing keys falls back to the module-level
    # constants in ``evovle_skills.src.patcher``.
    patcher_prompts: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @staticmethod
    def _as_list(value: Any) -> list:
        """Coerce a field to a list, accepting a JSON-encoded string.

        ``gym_servers_config`` and ``verifiers`` ship JSON-encoded in the
        evovling_skills JSONL but as real arrays in evovling_agents.  Without
        this, ``list()`` on the string form yields one entry per character.
        """
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            try:
                value = json.loads(value)
            except ValueError:
                return []
        return list(value or [])

    @classmethod
    def from_jsonl(cls, obj: dict[str, Any]) -> "TaskRow":
        # The dataset writes version as "v1", "v2", ... (string).  Accept
        # both that form and a bare int.
        ver_raw = obj["version"]
        if isinstance(ver_raw, str):
            ver_raw = ver_raw.lstrip("vV")
        version = int(ver_raw)
        return cls(
            domain=obj["domain"],
            version=version,
            split=obj["split"],
            task_id=obj["task_id"],
            user_prompt=obj.get("user_prompt", ""),
            system_prompt=obj.get("system_prompt", ""),
            oracle_skills=list(obj.get("oracle_skills", []) or []),
            cumulative_oracle_skills=list(
                obj.get("cumulative_oracle_skills")
                or obj.get("cummulative_oracle_skills")  # tolerate typo
                or []
            ),
            selected_tools=list(obj.get("selected_tools", []) or []),
            mcp_endpoint=obj.get("mcp_endpoint", "/mcp"),
            gym_servers_config=cls._as_list(obj.get("gym_servers_config")),
            verifiers=cls._as_list(obj.get("verifiers")),
            task_frame=obj.get("task_frame", "") or "",
            skill_library_note=obj.get("skill_library_note", "") or "",
            patcher_prompts=dict(obj.get("patcher_prompts") or {}),
            raw=obj,
        )


def split_path(domain: str, version: int, split: str) -> Path:
    """Path to one (domain, version, split) JSONL file."""
    if split not in ("train", "test"):
        raise ValueError(f"split must be 'train' or 'test', got {split!r}")
    return DATA_ROOT / domain / f"v{version}" / f"{split}.jsonl"


def iter_split(domain: str, version: int, split: str) -> Iterator[TaskRow]:
    p = split_path(domain, version, split)
    if not p.exists():
        raise FileNotFoundError(f"missing dataset file: {p}")
    with p.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield TaskRow.from_jsonl(json.loads(line))


def load_split(domain: str, version: int, split: str) -> list[TaskRow]:
    return list(iter_split(domain, version, split))


def discover_versions(domain: str) -> list[int]:
    """Return sorted list of version indices that exist on disk for a domain."""
    dom = DATA_ROOT / domain
    if not dom.exists():
        raise FileNotFoundError(f"unknown domain: {domain} (no {dom})")
    versions = []
    for p in dom.iterdir():
        if p.is_dir() and p.name.startswith("v") and p.name[1:].isdigit():
            versions.append(int(p.name[1:]))
    return sorted(versions)


def find_row(
    domain: str, version: int, split: str, task_id: str
) -> TaskRow:
    """Look up a single row by id; raises if not found.

    The error message also peeks at the *other* split to point users at
    the right place when they used a train id with --split test (or
    vice-versa), and lists a few sample ids from the requested split.
    """
    matches: list[TaskRow] = []
    for r in iter_split(domain, version, split):
        if r.task_id == task_id:
            return r
        matches.append(r)

    other = "train" if split == "test" else "test"
    in_other = any(r.task_id == task_id for r in iter_split(domain, version, other))
    sample = ", ".join(r.task_id for r in matches[:3])

    msg_parts = [f"task_id {task_id!r} not in {domain} v{version} {split}"]
    if in_other:
        msg_parts.append(f"(it does exist in v{version} {other!r}; did you mean --split {other}?)")
    if sample:
        msg_parts.append(f"sample ids in v{version} {split}: {sample}")
    raise KeyError(" -- ".join(msg_parts))
