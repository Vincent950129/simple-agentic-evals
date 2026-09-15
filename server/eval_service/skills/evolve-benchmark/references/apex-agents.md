# APEX-Agents construction path

Use these blocks in order for APEX-Agents at the pinned source snapshot. They are
generated from the same source as the rendered EvoHarnessBench construction page.
Do not substitute application names for tools: tools are exact Archipelago MCP
operations, while applications are availability guards and agent owners.

- APEX-Agents: `92c86856cf1b11f9833a8a076b3a45a63afa3929`
- Archipelago: `bcacc2d1e99aa917bbe7f6f663c1551dc6ec7400`
- huggingface-hub `0.26.2`, tomli `2.0.1`, seed `42`
- Expected: 480 discovered; tools 392/22/4 stages, skills 470/19/6 stages,
  agents 392/10/3 stages
- Expected tree hash: `8ef9f9aaf2ea5f53447cb90e5a5716a7a0063d9450c5c488e3ac4f13714e088c`

The seed is gated. The first block securely prompts for the user's own `HF_TOKEN`
after they accept access, downloads only four small metadata files, and never writes
the token. The 33 world archives are not downloaded. Tools are annotated from prompt,
world description, expected-output type, input-file type, and world availability.
Skills use fixed procedural atoms matched against the prompt, world description,
output type, and tool anchors. Neither annotation reads rubrics or gold answers.

The last check is a four-task source/gold integrity preflight. It does not execute an
agent or judge and does not claim task rewards.

## Download seed data · APEX-Agents

```bash
export EVOLVE_APEX_SERVICE="${EVOLVE_APEX_SERVICE:-https://educator-marrow-cultural.ngrok-free.dev}"
export EVOLVE_APEX_WORKDIR="${EVOLVE_APEX_WORKDIR:-$PWD/evoharnessbench-apex-agents}"

if [ -e "$EVOLVE_APEX_WORKDIR" ]; then
  echo "Refusing to overwrite existing path: $EVOLVE_APEX_WORKDIR" >&2
  exit 1
fi
mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/cache/seed"
python -m venv "$EVOLVE_APEX_WORKDIR/.venv"
export EVOLVE_APEX_PYTHON="$EVOLVE_APEX_WORKDIR/.venv/bin/python"
"$EVOLVE_APEX_PYTHON" -m pip install --disable-pip-version-check   "huggingface-hub==0.26.2" "tomli==2.0.1"

if [ -z "${HF_TOKEN:-}" ] && [ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]; then
  read -rsp "Hugging Face token (after accepting APEX-Agents access): " HF_TOKEN
  echo
  export HF_TOKEN
fi

"$EVOLVE_APEX_PYTHON" - <<'PY'
import hashlib, json, os, pathlib
from huggingface_hub import HfApi, hf_hub_download

repo_id = "mercor/apex-agents"
revision = "92c86856cf1b11f9833a8a076b3a45a63afa3929"
files = {"eval.yaml": "231993cb91ab18596fb6c24c9fbb33fba2975dcea605de5696cf4c4245ec8151", "metadata.json": "24584749c6307602944739262a2f3ddd59e8e0ecc9f151dc3459adf84ab934ea", "tasks_and_rubrics.json": "94a85655bb983bb8bfff693182026f05f1df41ff17b2cd4589dd42a071531cb6", "world_descriptions.json": "3836f308d122b7204a463ce43d23ec63fb27460f36f6bd5ac32d724183a3fdd8"}
root = pathlib.Path(os.environ["EVOLVE_APEX_WORKDIR"])
seed = root / "data_dry_run/apex_builder/cache/seed"
token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
if not token:
    raise RuntimeError("HF_TOKEN is required after accepting APEX-Agents access")
for name, expected in sorted(files.items()):
    path = pathlib.Path(hf_hub_download(
        repo_id=repo_id, repo_type="dataset", revision=revision,
        filename=name, local_dir=str(seed), token=token,
    ))
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError(f"seed checksum mismatch for {name}: {actual}")
info = HfApi(token=token).dataset_info(repo_id, revision=revision, files_metadata=True)
if info.sha != revision:
    raise RuntimeError(f"APEX source pin mismatch: {info.sha}")
manifest = {
    "files": sorted(
        ({"path": item.rfilename, "size": item.size} for item in info.siblings),
        key=lambda item: item["path"],
    ),
    "repository_url": "https://huggingface.co/datasets/mercor/apex-agents",
    "source_commit": revision,
}
(seed / "repo_files.json").write_text(
    json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
print(f"Downloaded {len(files)} pinned APEX-Agents metadata files")
PY

archipelago="$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/cache/archipelago"
git clone --filter=blob:none --no-checkout https://github.com/Mercor-Intelligence/archipelago.git "$archipelago"
git -C "$archipelago" fetch --force origin bcacc2d1e99aa917bbe7f6f663c1551dc6ec7400
git -C "$archipelago" checkout --detach bcacc2d1e99aa917bbe7f6f663c1551dc6ec7400
test "$(git -C "$archipelago" rev-parse HEAD)" = "bcacc2d1e99aa917bbe7f6f663c1551dc6ec7400"
printf 'Downloaded APEX-Agents seed metadata at %.8s; large world archives were not downloaded\n'   "92c86856cf1b11f9833a8a076b3a45a63afa3929"
```

## Tools · Step 1 operation catalog

```bash
"$EVOLVE_APEX_PYTHON" - <<'PY'
import hashlib, json, os, pathlib, urllib.request

base = os.environ.get("EVOLVE_APEX_SERVICE", "https://educator-marrow-cultural.ngrok-free.dev").rstrip("/")
root = pathlib.Path(os.environ["EVOLVE_APEX_WORKDIR"])
eval_key = os.environ.get("EVAL_SERVICE_API_KEY", "").strip()
if not eval_key:
    raise RuntimeError("EVAL_SERVICE_API_KEY is required to download the closed catalog")
public_name = "tool-catalog.json"
expected = "65a621a15d86a609677a8957482bfd42836bd30f8cc3be749ce787a10d90d4b6"
url = base + "/resources/evolve-benchmark/references/apex/v1/" + public_name
request = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {eval_key}",
    "ngrok-skip-browser-warning": "true",
})
with urllib.request.urlopen(request) as response:
    content = response.read()
actual = hashlib.sha256(content).hexdigest()
if actual != expected:
    raise RuntimeError(f"checksum mismatch for {public_name}: {actual}")
target = root / "data_dry_run" / "apex_builder" / "catalog.json"
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(target.name + ".download")
temporary.write_bytes(content)
temporary.replace(target)

catalog = json.loads((root / "data_dry_run/apex_builder/catalog.json").read_text())
required = {"name", "owner", "description", "source_path"}
names = [item["name"] for item in catalog]
if len(catalog) != 33 or len(names) != len(set(names)):
    raise RuntimeError("the pinned operation catalog must contain 33 unique operations")
if any(required - set(item) for item in catalog):
    raise RuntimeError("invalid operation-catalog schema")
if any(not any(item.get(field) for field in ("patterns", "expected_outputs", "input_extensions"))
       for item in catalog):
    raise RuntimeError("every operation needs at least one deterministic evidence rule")
if any(not item["name"].startswith(item["owner"] + ".") for item in catalog):
    raise RuntimeError("every operation must belong to its namespace owner")
print("Tools catalog: 33 exact Archipelago MCP operations with one owner each")
PY
```

## Tools · Step 1 shared annotation implementation

```bash
mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder"
cat > "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/builder.py" <<'__EVOHARNESS_APEX_FILE__'
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import config as cfg

if str(cfg.REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(cfg.REPO_ROOT))

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]

from evolve_tools.builder.frequency_config import (  # noqa: E402
    ConstraintConfig,
    assign_frequency_stage,
    build_frequency_anchors_adaptive,
    count_tool_frequencies,
    rank_tools,
    validate_frequency_benchmark,
)

_SKILLS_BUILDER = cfg.REPO_ROOT / "evovle_skills" / "builder"
if str(_SKILLS_BUILDER) not in sys.path:
    sys.path.insert(0, str(_SKILLS_BUILDER))
from sequencer import sequence_curriculum as sequence_skill_curriculum  # noqa: E402


HOUSE_RULES = """# APEX-Agents shared house rules

- Work only in the supplied Archipelago world and preserve all seed inputs.
- Read the complete task prompt before acting and use only the applications exposed by the world.
- Complete the requested response or artifact at the exact requested destination and format.
- Keep sourced facts, task assumptions, and calculations distinguishable.
- Validate calculations, formatting, citations, and saved artifacts before finishing.
- Do not use the public web unless the task world explicitly exposes a research application.
- Do not inspect or rely on held-out rubrics, gold responses, or gold artifacts.
"""

TOOLS_REQUIRED = {
    "domain", "version", "split", "task_id", "oracle_tools",
    "cummulative_tools", "title", "summary", "category", "subdomain",
    "task_split", "task_prompt", "agent_must_do", "software", "input_files",
    "taxonomy", "source_repo_path", "repository_url", "source_commit",
    "difficulty", "tags", "artifacts", "resource_limits", "metadata",
    "annotation_evidence",
}
SKILLS_REQUIRED = {
    "domain", "version", "split", "task_id", "oracle_skills",
    "cummulative_oracle_skills", "system_prompt", "user_prompt", "software",
    "agent_must_do", "category", "subdomain", "task_split", "input_files",
    "source_repo_path", "repository_url", "source_commit", "difficulty",
    "tags", "artifacts", "resource_limits", "metadata", "evaluation",
    "patcher_prompts", "prompt_suffix", "annotation_evidence",
}
AGENTS_REQUIRED = {
    "domain", "version", "split", "task_id", "oracle_agents",
    "cumulative_agents", "oracle_skills", "system_prompt", "task_prompt",
    "user_prompt", "software", "oracle_tools", "cummulative_tools",
    "agent_must_do", "category", "subdomain", "task_split", "title",
    "summary", "input_files", "source_repo_path", "repository_url",
    "source_commit", "difficulty", "tags", "artifacts", "resource_limits",
    "metadata", "taxonomy", "annotation_evidence",
}

APP_TO_OWNER = {
    "Calendar": "calendar",
    "Chat": "chat",
    "Code Execution": "code",
    "Word": "documents",
    "Excel": "spreadsheets",
    "Powerpoint": "presentations",
    "PDFs": "pdfs",
    "Mail": "mail",
    "Filesystem": "filesystem",
    "Edgar SEC": "edgar_sec",
    "EDGAR SEC": "edgar_sec",
    "FMP": "fmp",
}


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if pretty else _json_dump(value) + "\n"
    )
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(args: list[str], *, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    safe = ["<redacted>" if "token" in arg.casefold() else arg for arg in args]
    print("+ " + " ".join(safe), flush=True)
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, check=True, text=True,
        capture_output=capture,
    )


def _clone_pinned(url: str, commit: str, dest: Path) -> None:
    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(dest)])
    _run(["git", "fetch", "--force", "origin", commit], cwd=dest)
    _run(["git", "checkout", "--detach", commit], cwd=dest)
    got = _run(["git", "rev-parse", "HEAD"], cwd=dest, capture=True).stdout.strip()
    if got != commit:
        raise RuntimeError(f"Archipelago pin mismatch: expected {commit}, got {got}")


def _verify_seed() -> None:
    for relative, expected in cfg.SOURCE_FILES.items():
        path = cfg.SEED / relative
        if not path.is_file():
            raise FileNotFoundError(f"missing pinned seed metadata {path}")
        actual = _sha256(path)
        if actual != expected:
            raise ValueError(f"seed checksum mismatch for {relative}: {actual} != {expected}")


def _fetch_repo_manifest(token: str | None) -> list[dict[str, Any]]:
    from huggingface_hub import HfApi

    info = HfApi(token=token).dataset_info(
        cfg.HF_REPO_ID, revision=cfg.SOURCE_COMMIT, files_metadata=True,
    )
    if info.sha != cfg.SOURCE_COMMIT:
        raise ValueError(f"Hugging Face source pin mismatch: {info.sha}")
    files = [
        {"path": sibling.rfilename, "size": sibling.size}
        for sibling in info.siblings
    ]
    return sorted(files, key=lambda item: item["path"])


def fetch(*, refresh: bool = False) -> dict[str, Any]:
    """Fetch only the pinned metadata/catalog seed, never the 9 GB worlds."""
    cfg.SEED.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    missing = [name for name in cfg.SOURCE_FILES if not (cfg.SEED / name).is_file()]
    if refresh or missing:
        if not token:
            raise RuntimeError(
                "gated APEX metadata is missing; export HF_TOKEN after accepting dataset access"
            )
        from huggingface_hub import hf_hub_download

        for name in sorted(cfg.SOURCE_FILES):
            hf_hub_download(
                repo_id=cfg.HF_REPO_ID,
                repo_type="dataset",
                revision=cfg.SOURCE_COMMIT,
                filename=name,
                local_dir=str(cfg.SEED),
                token=token,
            )
    _verify_seed()
    manifest_path = cfg.SEED / "repo_files.json"
    if refresh or not manifest_path.is_file():
        write_json(manifest_path, {
            "repository_url": cfg.SOURCE_URL,
            "source_commit": cfg.SOURCE_COMMIT,
            "files": _fetch_repo_manifest(token),
        })
    manifest = read_json(manifest_path)
    if manifest.get("source_commit") != cfg.SOURCE_COMMIT:
        raise ValueError("cached repository manifest has the wrong source commit")
    _clone_pinned(cfg.ARCHIPELAGO_URL, cfg.ARCHIPELAGO_COMMIT, cfg.ARCHIPELAGO)
    result = {
        "source": cfg.SOURCE_URL,
        "source_commit": cfg.SOURCE_COMMIT,
        "seed_files": {name: cfg.SOURCE_FILES[name] for name in sorted(cfg.SOURCE_FILES)},
        "repository_file_count": len(manifest["files"]),
        "archipelago": str(cfg.ARCHIPELAGO),
        "archipelago_commit": cfg.ARCHIPELAGO_COMMIT,
        "world_archives_downloaded": False,
    }
    write_json(cfg.REPORTS / "fetch.json", result)
    return result


@dataclass
class TaskRecord:
    raw: dict[str, Any]
    world: dict[str, Any]
    input_files: list[str]
    gold_files: list[str]
    tools: list[str]
    tool_evidence: dict[str, list[dict[str, Any]]]
    skills: list[str]
    skill_evidence: dict[str, list[dict[str, Any]]]

    @property
    def task_id(self) -> str:
        return str(self.raw["task_id"])

    @property
    def prompt(self) -> str:
        return str(self.raw["prompt"])

    @property
    def domain(self) -> str:
        return str(self.raw["domain"])


def load_catalog() -> list[dict[str, Any]]:
    catalog = read_json(cfg.ROOT / "catalog.json")
    names: set[str] = set()
    for spec in catalog:
        missing = {"name", "owner", "description", "source_path"} - set(spec)
        if missing:
            raise ValueError(f"tool catalog entry missing {sorted(missing)}")
        name = str(spec["name"])
        if name in names:
            raise ValueError(f"duplicate tool catalog name {name}")
        names.add(name)
        if not name.startswith(str(spec["owner"]) + "."):
            raise ValueError(f"tool {name} does not belong to owner {spec['owner']}")
        for pattern in spec.get("patterns") or []:
            re.compile(pattern, re.IGNORECASE)
        source = cfg.ARCHIPELAGO / str(spec["source_path"])
        if not source.is_file():
            raise ValueError(f"tool {name} references missing Archipelago source {source}")
    return catalog


def load_skill_catalog() -> list[dict[str, Any]]:
    skills = read_json(cfg.ROOT / "skills.json")
    names = [str(spec.get("name") or "") for spec in skills]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("skill catalog contains empty or duplicate names")
    known = set(names)
    known_tools = {spec["name"] for spec in load_catalog()}
    required = {
        "name", "title", "description", "tier", "domains", "patterns", "tools",
        "expected_outputs", "procedure", "notes", "see_also",
    }
    for spec in skills:
        missing = required - set(spec)
        if missing:
            raise ValueError(f"skill {spec.get('name')} missing {sorted(missing)}")
        if spec["tier"] not in {"workflow", "capability"}:
            raise ValueError(f"skill {spec['name']} has invalid tier")
        if not str(spec["procedure"]).strip():
            raise ValueError(f"skill {spec['name']} has an empty procedure")
        if set(spec["tools"]) - known_tools:
            raise ValueError(f"skill {spec['name']} references unknown tools")
        if set(spec["see_also"]) - known:
            raise ValueError(f"skill {spec['name']} has broken see_also links")
        for pattern in spec["patterns"]:
            re.compile(pattern, re.IGNORECASE)
    return skills


def _evidence(source_path: str, field: str, rule: str, text: str) -> dict[str, Any]:
    collapsed = " ".join(str(text).split())
    return {
        "source_path": source_path,
        "line": 1,
        "field": field,
        "rule": rule,
        "excerpt": collapsed[:240],
    }


def _dedupe_evidence(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {_json_dump(item): item for item in items}
    return sorted(
        unique.values(),
        key=lambda item: (item["source_path"], item["field"], item["rule"], item["excerpt"]),
    )


def _world_owners(world: dict[str, Any]) -> set[str]:
    owners = set()
    for app in world.get("apps") or []:
        service = str(app.get("service_name") or "")
        owner = APP_TO_OWNER.get(service)
        if not owner:
            raise ValueError(f"unknown APEX world application {service!r}")
        owners.add(owner)
    return owners


def load_source_tool_annotations() -> dict[str, dict[str, Any]]:
    """Load task-level oracle tools inherited from the source benchmark.

    A source-authoritative annotation file takes precedence when available.
    The pinned APEX release does not publish one, so the normal build uses the
    same deterministic missing-annotation adaptation already validated for
    TB2.
    """
    if not cfg.SOURCE_TOOL_ANNOTATIONS.is_file():
        return {}
    payload = read_json(cfg.SOURCE_TOOL_ANNOTATIONS)
    if payload.get("source_commit") != cfg.SOURCE_COMMIT:
        raise ValueError("source oracle-tool annotations use the wrong APEX commit")
    if payload.get("annotation_kind") != "inherited-source-oracle-tools":
        raise ValueError("tool annotations are not marked as inherited source oracle labels")
    tasks = payload.get("tasks") or {}
    if not isinstance(tasks, dict):
        raise ValueError("source oracle-tool annotations must contain a tasks object")
    catalog = load_catalog()
    known = {spec["name"] for spec in catalog}
    owner_of = {spec["name"]: spec["owner"] for spec in catalog}
    raw_tasks = {row["task_id"]: row for row in read_json(cfg.SEED / "tasks_and_rubrics.json")}
    worlds = {row["world_id"]: row for row in read_json(cfg.SEED / "world_descriptions.json")}
    clean: dict[str, dict[str, Any]] = {}
    for task_id, item in sorted(tasks.items()):
        if task_id not in raw_tasks:
            raise ValueError(f"oracle tools reference unknown APEX task {task_id}")
        tools = sorted(set(item.get("oracle_tools") or []))
        if not tools:
            raise ValueError(f"{task_id}: inherited oracle tool set is empty")
        unknown = set(tools) - known
        if unknown:
            raise ValueError(f"{task_id}: inherited oracle tools are unknown: {sorted(unknown)}")
        available = _world_owners(worlds[raw_tasks[task_id]["world_id"]])
        if any(owner_of[tool] not in available for tool in tools):
            raise ValueError(f"{task_id}: inherited oracle tool owner is unavailable in its world")
        evidence = item.get("evidence") or {}
        if any(not evidence.get(tool) for tool in tools):
            raise ValueError(f"{task_id}: inherited oracle tool is missing source evidence")
        clean[task_id] = {"oracle_tools": tools, "evidence": evidence}
    return clean


def annotate_tools(
    raw: dict[str, Any], world: dict[str, Any], input_files: list[str],
    catalog: list[dict[str, Any]],
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """TB2-style deterministic adapter for APEX task-facing operations.

    TB2 derives its missing source annotations from the task instruction,
    description, oracle-side source evidence, and concrete software signals.
    APEX has no oracle program, so its public task prompt, world description,
    requested output type, and referenced input-file types are the available
    construction evidence.  The held-out rubric and gold response are not used.
    """
    available = _world_owners(world)
    sources = [
        ("tasks_and_rubrics.json", "prompt", "prompt", str(raw.get("prompt") or "")),
        (
            "world_descriptions.json", "world_description", "description",
            str(world.get("world_description") or ""),
        ),
    ]
    expected_output = str(raw.get("expected_output") or "")
    suffixes = [(path, Path(path).suffix.casefold()) for path in input_files]
    matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in catalog:
        name, owner = str(spec["name"]), str(spec["owner"])
        if owner not in available:
            continue
        if expected_output in set(spec.get("expected_outputs") or []):
            matched[name].append(_evidence(
                "tasks_and_rubrics.json", "expected_output",
                f"expected-output:{expected_output}", expected_output,
            ))
        extensions = {str(ext).casefold() for ext in spec.get("input_extensions") or []}
        for path, suffix in suffixes:
            if suffix in extensions:
                matched[name].append(_evidence(
                    path, "input_file", f"input-extension:{suffix}", path,
                ))
        for pattern in spec.get("patterns") or []:
            compiled = re.compile(pattern, re.IGNORECASE)
            for source_path, field, role, text in sources:
                match = compiled.search(text)
                if match:
                    matched[name].append(_evidence(
                        source_path, field, f"{role}-pattern:{pattern}", match.group(0),
                    ))
    cleaned = {name: _dedupe_evidence(items) for name, items in sorted(matched.items())}
    return sorted(cleaned), cleaned


def annotate_skills(
    raw: dict[str, Any], world: dict[str, Any], tools: list[str],
    skill_catalog: list[dict[str, Any]],
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    # Exact ALE association analogue: fixed patterns against task
    # specification, plus overlap with normalized required software/tools.
    # Held-out rubrics and gold responses are deliberately not consulted.
    sources = [
        (
            "tasks_and_rubrics.json", "prompt", "task-specification",
            str(raw.get("prompt") or ""),
        ),
        (
            "world_descriptions.json", "world_description", "benchmark-description",
            str(world.get("world_description") or ""),
        ),
    ]
    expected_output = str(raw.get("expected_output") or "")
    tool_set = set(tools)
    matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in skill_catalog:
        name = str(spec["name"])
        if expected_output in set(spec.get("expected_outputs") or []):
            matched[name].append(_evidence(
                "tasks_and_rubrics.json", "expected_output",
                f"expected-output:{expected_output}", expected_output,
            ))
        for tool in sorted(tool_set & set(spec.get("tools") or [])):
            matched[name].append(_evidence(
                "tool-annotation", "oracle_tools", f"software-anchor:{tool}", tool,
            ))
        for pattern in spec.get("patterns") or []:
            compiled = re.compile(pattern, re.IGNORECASE)
            for source_path, field, role, text in sources:
                match = compiled.search(text)
                if match:
                    matched[name].append(_evidence(
                        source_path, field, f"{role}-pattern:{pattern}", match.group(0),
                    ))
    cleaned = {name: _dedupe_evidence(items) for name, items in sorted(matched.items())}
    return sorted(cleaned), cleaned


def _repo_file_index() -> tuple[list[dict[str, Any]], set[str]]:
    manifest = read_json(cfg.SEED / "repo_files.json")
    if manifest.get("source_commit") != cfg.SOURCE_COMMIT:
        raise ValueError("repo_files.json source pin mismatch")
    files = manifest.get("files") or []
    return files, {str(item["path"]) for item in files}


def discover_tasks() -> list[TaskRecord]:
    _verify_seed()
    catalog = load_catalog()
    skills = load_skill_catalog()
    raw_tasks = read_json(cfg.SEED / "tasks_and_rubrics.json")
    worlds = read_json(cfg.SEED / "world_descriptions.json")
    if len(raw_tasks) != cfg.EXPECTED_TASKS:
        raise ValueError(f"expected {cfg.EXPECTED_TASKS} tasks, found {len(raw_tasks)}")
    if len(worlds) != cfg.EXPECTED_WORLDS:
        raise ValueError(f"expected {cfg.EXPECTED_WORLDS} worlds, found {len(worlds)}")
    world_by_id = {world["world_id"]: world for world in worlds}
    if len(world_by_id) != len(worlds):
        raise ValueError("duplicate world IDs")
    _, repo_paths = _repo_file_index()
    source_tools = load_source_tool_annotations()
    records: list[TaskRecord] = []
    seen: set[str] = set()
    for raw in sorted(raw_tasks, key=lambda item: item["task_id"]):
        task_id = str(raw["task_id"])
        if task_id in seen:
            raise ValueError(f"duplicate task ID {task_id}")
        seen.add(task_id)
        if raw.get("world_id") not in world_by_id:
            raise ValueError(f"{task_id}: missing world {raw.get('world_id')}")
        input_files = sorted(
            path for path in repo_paths if path.startswith(f"task_files/{task_id}/")
            and not path.endswith(".DS_Store")
        )
        gold_files = sorted(
            path for path in repo_paths if path.startswith(f"gold_files/{task_id}/")
            and not path.endswith(".DS_Store")
        )
        world = world_by_id[raw["world_id"]]
        inherited = source_tools.get(task_id) or {}
        if inherited:
            tools = list(inherited.get("oracle_tools") or [])
            tool_evidence = dict(inherited.get("evidence") or {})
        else:
            tools, tool_evidence = annotate_tools(raw, world, input_files, catalog)
        skill_names, skill_evidence = annotate_skills(raw, world, tools, skills)
        records.append(TaskRecord(
            raw=raw, world=world, input_files=input_files, gold_files=gold_files,
            tools=tools, tool_evidence=tool_evidence,
            skills=skill_names, skill_evidence=skill_evidence,
        ))
    return records


def _frequency(items: Iterable[Iterable[str]]) -> dict[str, int]:
    return dict(sorted(count_tool_frequencies(items).items()))


def inspect_source(*, write: bool = True) -> dict[str, Any]:
    records = discover_tasks()
    tool_records = [record for record in records if record.tools]
    excluded = [record for record in records if not record.tools]
    report = {
        "source_url": cfg.SOURCE_URL,
        "source_commit": cfg.SOURCE_COMMIT,
        "archipelago_url": cfg.ARCHIPELAGO_URL.removesuffix(".git"),
        "archipelago_commit": cfg.ARCHIPELAGO_COMMIT,
        "discovered_tasks": len(records),
        "discovered_worlds": len({record.raw["world_id"] for record in records}),
        "pipeline_status": "ready: TB2-style deterministic seed adapter",
        "tool_annotation_method": (
            "inherited source oracle labels where supplied; otherwise TB2-style closed-catalog "
            "matching over prompt, description, requested output, and input-file evidence"
        ),
        "tools_retained_tasks": len(tool_records),
        "skills_retained_tasks": sum(bool(record.skills) for record in records),
        "agents_retained_tasks": len(tool_records),
        "tools_excluded_tasks": [
            {
                "task_id": record.task_id,
                "reason": "no task-facing closed-catalog operation evidence",
            }
            for record in excluded
        ],
        "tool_frequencies": _frequency(record.tools for record in tool_records),
        "skill_frequencies": _frequency(record.skills for record in records if record.skills),
        "owner_frequencies": _frequency(
            {tool.split(".", 1)[0] for tool in record.tools} for record in tool_records
        ),
        "input_file_references": sum(len(record.input_files) for record in records),
        "gold_file_references": sum(len(record.gold_files) for record in records),
        "tool_annotations": {
            record.task_id: {"oracle_tools": record.tools, "evidence": record.tool_evidence}
            for record in records
        },
        "skill_annotations": {
            record.task_id: {"oracle_skills": record.skills, "evidence": record.skill_evidence}
            for record in records
        },
    }
    if write:
        write_json(cfg.REPORTS / "inspection.json", report)
    return report


__EVOHARNESS_APEX_FILE__
```

## Tools · Step 2 shared release implementation

```bash
mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder"
cat >> "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/builder.py" <<'__EVOHARNESS_APEX_FILE__'
def _axis_constraints(axis: str) -> ConstraintConfig:
    if axis == "agents":
        return ConstraintConfig(
            min_new_tasks_per_stage=cfg.AGENT_MIN_NEW_TASKS,
            min_adapt_per_stage=cfg.MIN_TRAIN,
            min_test_per_stage=cfg.MIN_TEST,
            min_growth_frac=cfg.AGENT_MIN_GROWTH_FRAC,
            max_growth_frac=cfg.AGENT_MAX_GROWTH_FRAC,
            min_tool_usage=1,
            drop_rare_tools=False,
        )
    return ConstraintConfig(
        min_new_tasks_per_stage=cfg.TOOL_MIN_NEW_TASKS,
        min_adapt_per_stage=cfg.MIN_TRAIN,
        min_test_per_stage=cfg.MIN_TEST,
        min_growth_frac=cfg.TOOL_MIN_GROWTH_FRAC,
        max_growth_frac=cfg.TOOL_MAX_GROWTH_FRAC,
        min_tool_usage=1,
        drop_rare_tools=False,
        initial_anchor_frac=cfg.TOOL_INITIAL_ANCHOR_FRAC,
    )


def _assign(task_caps: dict[str, set[str]], anchors: list[set[str]]) -> dict[str, int]:
    assignments: dict[str, int] = {}
    for task_id, capabilities in sorted(task_caps.items()):
        stage = assign_frequency_stage(capabilities, anchors)
        if stage is not None:
            assignments[task_id] = stage + 1
    return assignments


def _stage_ok(ids: list[str], splits: dict[str, str] | None) -> bool:
    if splits is None:
        return len(ids) >= cfg.MIN_TRAIN + cfg.MIN_TEST
    return (
        sum(splits.get(task_id) == "train" for task_id in ids) >= cfg.MIN_TRAIN
        and sum(splits.get(task_id) == "test" for task_id in ids) >= cfg.MIN_TEST
    )


def _merge_split_thin_stages(
    task_caps: dict[str, set[str]], anchors: list[set[str]], splits: dict[str, str],
) -> list[set[str]]:
    anchors = [set(anchor) for anchor in anchors]
    while len(anchors) >= 2:
        assignments = _assign(task_caps, anchors)
        bad = []
        for stage in range(1, len(anchors) + 1):
            ids = [task_id for task_id, value in assignments.items() if value == stage]
            if not _stage_ok(ids, splits):
                bad.append(stage - 1)
        if not bad:
            return anchors
        index = bad[0]
        anchors.pop(index if index < len(anchors) - 1 else index - 1)
    return anchors


def _build_frequency_curriculum(
    task_caps: dict[str, set[str]], *, axis: str,
    splits: dict[str, str] | None = None,
) -> tuple[list[set[str]], dict[str, int], dict[str, Any]]:
    if not task_caps or any(not value for value in task_caps.values()):
        raise ValueError(f"{axis} curriculum requires non-empty capability sets")
    ids = sorted(task_caps)
    constraints = _axis_constraints(axis)
    anchors, core_report = build_frequency_anchors_adaptive(
        [task_caps[task_id] for task_id in ids],
        target_num_versions=cfg.TARGET_VERSIONS,
        constraints=constraints,
    )
    if splits is not None:
        anchors = _merge_split_thin_stages(task_caps, anchors, splits)
    assignments = _assign(task_caps, anchors)
    if set(assignments) != set(task_caps):
        raise ValueError(f"{axis}: not every annotated task is covered")
    if len(anchors) < cfg.MIN_VERSIONS:
        raise ValueError(f"{axis}: emitted {len(anchors)} versions; need {cfg.MIN_VERSIONS}")
    for stage, cumulative in enumerate(anchors, 1):
        ids_at_stage = [task_id for task_id, value in assignments.items() if value == stage]
        if not _stage_ok(ids_at_stage, splits):
            raise ValueError(f"{axis} v{stage}: task/split floor failed")
        previous = anchors[stage - 2] if stage > 1 else set()
        introduced = cumulative - previous
        if not introduced:
            raise ValueError(f"{axis} v{stage}: cumulative chain did not grow")
        if any(not (task_caps[task_id] & introduced) for task_id in ids_at_stage):
            raise ValueError(f"{axis} v{stage}: earliest-stage pressure failed")
    return anchors, assignments, {
        "implementation": (
            "evolve_tools.builder.frequency_config.build_frequency_anchors_adaptive"
        ),
        "constraints": constraints.to_dict(),
        "core_report": core_report,
        "schedule_after_split_folds": [len(anchor) for anchor in anchors],
    }


def split_curriculum(assignments: dict[str, int]) -> dict[str, str]:
    result: dict[str, str] = {}
    for stage in sorted(set(assignments.values())):
        ids = sorted(task_id for task_id, value in assignments.items() if value == stage)
        rng = random.Random(cfg.RANDOM_SEED * 9973 + stage)
        rng.shuffle(ids)
        n_train = round(len(ids) * cfg.ADAPT_RATIO)
        n_train = max(cfg.MIN_TRAIN, n_train)
        n_train = min(n_train, len(ids) - cfg.MIN_TEST)
        if n_train < cfg.MIN_TRAIN:
            raise ValueError(f"v{stage}: cannot satisfy train/test floors")
        for task_id in ids[:n_train]:
            result[task_id] = "train"
        for task_id in ids[n_train:]:
            result[task_id] = "test"
    return result


def _build_skill_curriculum(records: list[TaskRecord]) -> tuple[
    list[set[str]], dict[str, int], dict[str, str], dict[str, Any]
]:
    per_task = {
        record.task_id: {"tagged_skills": sorted(record.skills)} for record in records
    }
    frequency = Counter(skill for record in records for skill in set(record.skills))
    result = sequence_skill_curriculum(
        per_task=per_task,
        active_skills=sorted(frequency),
        task_count_per_skill=dict(frequency),
        seed=cfg.RANDOM_SEED,
        min_step_size=cfg.SKILL_MIN_STEP_SIZE,
        adapt_ratio=cfg.ADAPT_RATIO,
        min_adapt_per_version=cfg.MIN_TRAIN,
        min_test_per_version=cfg.MIN_TEST,
    )
    anchors: list[set[str]] = []
    assignments: dict[str, int] = {}
    splits: dict[str, str] = {}
    for step in result["time_steps"]:
        stage = int(step["T"])
        anchors.append(set(step["cumulative_oracle_skills"]))
        for task in step["tasks"]:
            assignments[task["task_id"]] = stage
            splits[task["task_id"]] = task["split"]
    if set(assignments) != set(per_task):
        raise ValueError("skill sequencer did not place every annotated APEX task")
    if len(anchors) < cfg.MIN_VERSIONS:
        raise ValueError(f"skills: emitted {len(anchors)} versions; need {cfg.MIN_VERSIONS}")
    passed = True
    for stage, anchor in enumerate(anchors, 1):
        ids = [task_id for task_id, value in assignments.items() if value == stage]
        previous = anchors[stage - 2] if stage > 1 else set()
        introduced = anchor - previous
        passed &= (
            _stage_ok(ids, splits)
            and bool(introduced)
            and all(set(per_task[task_id]["tagged_skills"]) & introduced for task_id in ids)
        )
    if not passed:
        raise ValueError("skills: canonical sequencer validation failed")
    return anchors, assignments, splits, {
        "implementation": "evovle_skills.builder.sequencer.sequence_curriculum",
        "min_step_size": cfg.SKILL_MIN_STEP_SIZE,
        "stats": result["stats"],
        "validation": {
            "passed": True,
            "all_tasks_placed": True,
            "earliest_solving_assignment": True,
        },
    }


__EVOHARNESS_APEX_FILE__

mkdir -p "$EVOLVE_APEX_WORKDIR/evolve_tools/builder"
cat > "$EVOLVE_APEX_WORKDIR/evolve_tools/builder/frequency_config.py" <<'__EVOHARNESS_APEX_FILE__'
"""
Frequency-driven version sequence (data-driven staging).

Implements the canonical "evolving environment" recipe in a single,
dataset-agnostic place:

  1. From the dataset of (x_i, T_i, y_i), compute the universe T = U_i T_i.
  2. Rank tools by descending task-frequency (ties broken alphabetically)
     to mimic core-first / edge-later API rollout in real systems.
  3. Build a strictly increasing chain of cumulative tool sets:
        C_1 subsetneq C_2 subsetneq ... subsetneq C_K
     where |C_k| follows a user-supplied schedule (e.g., [3, 5, 8, ...]).
  4. Assign each task i to the earliest version where it is solvable:
        k(i) = min { k : T_i subset of C_k }
     Tasks whose oracle tool set is not contained in C_K are dropped
     (or, with `expand_to_fit=True`, the final anchor C_K is extended to
     T so every task fits).

This is the *strict-addition* baseline (Section 4(A) of the spec). Other
dynamics — deprecation, renaming, breaking changes — should live in
sibling modules and reuse the ranking / assignment helpers below.

Nothing in this file talks to MCP, the LLM, or HuggingFace, so it is
trivially unit-testable.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# 2A + 2B: collect tools, rank by frequency
# ---------------------------------------------------------------------------

def count_tool_frequencies(
    task_tool_sets: Iterable[Iterable[str]],
) -> Counter:
    """
    Count, for every tool, the number of tasks whose oracle set contains it.

    Each task contributes at most 1 to a tool's count, regardless of how
    many times the agent actually uses it during a rollout — we care about
    "how many tasks need this capability", not call volume.
    """
    counter: Counter = Counter()
    for tool_set in task_tool_sets:
        for tool in set(tool_set):
            counter[tool] += 1
    return counter


def rank_tools(
    task_tool_sets: Iterable[Iterable[str]],
    importance: dict[str, float] | None = None,
) -> list[str]:
    """
    Return tools ordered "core first":

      key = (-frequency, -importance_override, tool_name)

    `importance` lets the caller bump specific tools (e.g., authentication
    primitives) up the order independently of dataset frequency. If two
    tools tie on both, the alphabetic suffix gives a deterministic order.
    """
    importance = importance or {}
    freq = count_tool_frequencies(task_tool_sets)
    all_tools = sorted(freq.keys())
    return sorted(
        all_tools,
        key=lambda t: (-freq[t], -importance.get(t, 0.0), t),
    )


# ---------------------------------------------------------------------------
# 2C: build the chain C_1 subsetneq C_2 subsetneq ...
# ---------------------------------------------------------------------------

def default_schedule(num_unique_tools: int, num_versions: int = 5) -> list[int]:
    """
    Construct a reasonable default cumulative schedule when the caller
    just says "give me K versions".

    We grow geometrically from ~1/4 of the universe to the full universe,
    so early stages stay narrow ("core APIs only") and later ones add the
    long tail. Sizes are clipped to be strictly increasing and to land at
    `num_unique_tools` for the last version.
    """
    if num_versions <= 0:
        raise ValueError("num_versions must be > 0")
    if num_unique_tools <= 0:
        return []

    start = max(1, num_unique_tools // 4)
    if num_versions == 1:
        return [num_unique_tools]

    # Geometric interpolation on the log scale from `start` to `num_unique_tools`.
    import math
    log_start = math.log(start)
    log_end = math.log(num_unique_tools)
    raw = [
        int(round(math.exp(log_start + (log_end - log_start) * i / (num_versions - 1))))
        for i in range(num_versions)
    ]
    # Force strict monotonicity, ending exactly at num_unique_tools.
    sched: list[int] = []
    last = 0
    for v in raw:
        v = max(v, last + 1)
        v = min(v, num_unique_tools)
        sched.append(v)
        last = v
    sched[-1] = num_unique_tools
    # Repair any duplicates that were forced by the cap.
    for i in range(len(sched) - 2, -1, -1):
        if sched[i] >= sched[i + 1]:
            sched[i] = sched[i + 1] - 1
    if sched[0] < 1:
        sched[0] = 1
    return sched


def build_frequency_anchors(
    task_tool_sets: Sequence[Iterable[str]],
    schedule: Sequence[int] | None = None,
    num_versions: int | None = None,
    importance: dict[str, float] | None = None,
    expand_to_fit: bool = True,
) -> list[set[str]]:
    """
    Build the cumulative anchor chain C_1, ..., C_K from the dataset.

    Args:
        task_tool_sets: list of T_i (oracle tool sets) for every task.
        schedule: explicit cumulative sizes, e.g. [3, 5, 8, 12, 20].
            Must be strictly increasing positive ints. If omitted,
            `default_schedule(|T|, num_versions)` is used.
        num_versions: alternative to `schedule` — number of versions K
            for the auto-generated schedule (default: 5).
        importance: optional per-tool boost (see `rank_tools`).
        expand_to_fit: if True (default), extend the final anchor C_K so
            it equals the full tool universe T. This guarantees every
            task in the dataset is placeable. Set to False if you want
            tasks requiring "long-tail" tools beyond the schedule to be
            dropped (useful for stress-testing forward transfer).

    Returns:
        anchors[k] = C_{k+1} (zero-indexed list of cumulative tool sets).
    """
    ranked = rank_tools(task_tool_sets, importance=importance)
    universe_size = len(ranked)
    if universe_size == 0:
        return []

    if schedule is None:
        schedule = default_schedule(universe_size, num_versions or 5)
    schedule = list(schedule)

    if not schedule:
        raise ValueError("schedule must contain at least one entry")
    if any(s <= 0 for s in schedule):
        raise ValueError(f"schedule entries must be positive, got {schedule}")
    for a, b in zip(schedule, schedule[1:]):
        if a >= b:
            raise ValueError(
                f"schedule must be strictly increasing, got {schedule}"
            )
    if schedule[-1] > universe_size and not expand_to_fit:
        raise ValueError(
            f"schedule[-1]={schedule[-1]} exceeds tool universe size "
            f"{universe_size} and expand_to_fit=False"
        )

    if expand_to_fit:
        if schedule[-1] < universe_size:
            schedule.append(universe_size)
        else:
            schedule[-1] = universe_size

    anchors: list[set[str]] = []
    for size in schedule:
        anchors.append(set(ranked[:size]))
    return anchors


# ---------------------------------------------------------------------------
# Step 3: task -> version assignment
# ---------------------------------------------------------------------------

def assign_frequency_stage(
    oracle_tools: Iterable[str],
    anchors: Sequence[set[str]],
) -> int | None:
    """
    Earliest version k where the task is solvable: T subset of C_k.
    Returns None if no version covers T (only possible when
    expand_to_fit=False or T is empty).
    """
    tools = set(oracle_tools)
    if not tools:
        return None
    for k, anchor in enumerate(anchors):
        if tools <= anchor:
            return k
    return None


def num_frequency_stages(anchors: Sequence[set[str]]) -> int:
    return len(anchors)


def cumulative_tools_at_frequency_stage(
    anchors: Sequence[set[str]], stage_idx: int,
) -> set[str]:
    return set(anchors[stage_idx])


def tools_introduced_at_frequency_stage(
    anchors: Sequence[set[str]], stage_idx: int,
) -> set[str]:
    if stage_idx == 0:
        return set(anchors[0])
    return set(anchors[stage_idx]) - set(anchors[stage_idx - 1])


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

@dataclass
class FrequencyVersionReport:
    """Diagnostic snapshot of a frequency-driven version chain."""

    num_versions: int
    schedule: list[int]
    universe_size: int
    tool_frequencies: dict[str, int]
    ranked_tools: list[str]
    anchors: list[list[str]]                 # cumulative, sorted for readability
    delta_tools: list[list[str]]             # per-version new tools
    tasks_per_stage: dict[int, int]          # newly introduced
    cumulative_tasks_per_stage: dict[int, int]
    dropped_task_indices: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "num_versions": self.num_versions,
            "schedule": self.schedule,
            "universe_size": self.universe_size,
            "tool_frequencies": self.tool_frequencies,
            "ranked_tools": self.ranked_tools,
            "anchors": self.anchors,
            "delta_tools": self.delta_tools,
            "tasks_per_stage": self.tasks_per_stage,
            "cumulative_tasks_per_stage": self.cumulative_tasks_per_stage,
            "dropped_task_indices": self.dropped_task_indices,
        }


def summarize_version_chain(
    task_tool_sets: Sequence[Iterable[str]],
    anchors: Sequence[set[str]],
) -> FrequencyVersionReport:
    """Compute per-version statistics for the chain."""
    tool_sets = [list(t) for t in task_tool_sets]
    freq = count_tool_frequencies(tool_sets)
    ranked = sorted(
        freq.keys(),
        key=lambda t: (-freq[t], t),
    )

    tasks_per_stage: dict[int, int] = {k: 0 for k in range(len(anchors))}
    dropped: list[int] = []
    assignments: list[int | None] = []
    for i, T in enumerate(tool_sets):
        k = assign_frequency_stage(T, anchors)
        assignments.append(k)
        if k is None:
            dropped.append(i)
        else:
            tasks_per_stage[k] += 1

    cumulative: dict[int, int] = {}
    running = 0
    for k in range(len(anchors)):
        running += tasks_per_stage[k]
        cumulative[k] = running

    delta: list[list[str]] = []
    for k in range(len(anchors)):
        prev = anchors[k - 1] if k > 0 else set()
        delta.append(sorted(set(anchors[k]) - prev))

    return FrequencyVersionReport(
        num_versions=len(anchors),
        schedule=[len(a) for a in anchors],
        universe_size=len(freq),
        tool_frequencies=dict(freq.most_common()),
        ranked_tools=ranked,
        anchors=[sorted(a) for a in anchors],
        delta_tools=delta,
        tasks_per_stage=tasks_per_stage,
        cumulative_tasks_per_stage=cumulative,
        dropped_task_indices=dropped,
    )


# ---------------------------------------------------------------------------
# Constraint-aware adaptive anchor builder + validator
# ---------------------------------------------------------------------------

@dataclass
class ConstraintConfig:
    """
    Numeric constraints that make a frequency-staged benchmark practical and
    evaluable. See user spec:

      (1) min test size per stage:    |D_k^test|  >= min_test_per_stage
      (2) min adapt size per stage:   |D_k^adapt| >= min_adapt_per_stage
      (4) min usage of each new tool: count(t, D_>=k_t) >= min_tool_usage
      (6) balanced growth:
            min_growth_frac <= (|C_k|-|C_{k-1}|) / |C_K| <= max_growth_frac
            and |D_k^new| >= min_new_tasks_per_stage
      (7) hard: |D_k^test| > 0   (subsumed by (1) once min_test_per_stage>=1)

    The adaptive builder uses (6) and `min_new_tasks_per_stage` as construction
    constraints; (1), (2), and (4) are validated post-hoc and trigger merges
    (for (1)+(2)) or warnings (for (4), which is dataset-intrinsic).
    """
    min_new_tasks_per_stage: int = 7      # adapt + test floor by default
    min_adapt_per_stage: int = 2
    min_test_per_stage: int = 5
    min_growth_frac: float = 0.15
    max_growth_frac: float = 0.35
    min_tool_usage: int = 2
    # If True, before anchor building drop tools used in fewer than
    # `min_tool_usage` tasks (and the tasks that uniquely require them).
    # If False (default), keep them but flag as warnings in validation.
    drop_rare_tools: bool = False
    # Lower bound on |C_1| as a fraction of |U|. Default 0 = follow normal
    # growth bounds (V1 is small). Setting e.g. 0.4 forces V1 to cover at
    # least 40% of the universe, which lets larger-|T| tasks legally land
    # at V1 under the earliest-solvable rule, raising μ_1 and shrinking
    # the per-stage complexity gap. The earliest-solvable assignment is
    # always preserved.
    initial_anchor_frac: float = 0.0

    def to_dict(self) -> dict:
        return {
            "min_new_tasks_per_stage": self.min_new_tasks_per_stage,
            "min_adapt_per_stage": self.min_adapt_per_stage,
            "min_test_per_stage": self.min_test_per_stage,
            "min_growth_frac": self.min_growth_frac,
            "max_growth_frac": self.max_growth_frac,
            "min_tool_usage": self.min_tool_usage,
            "drop_rare_tools": self.drop_rare_tools,
            "initial_anchor_frac": self.initial_anchor_frac,
        }


def _compute_growth_bounds(
    universe_size: int,
    constraints: ConstraintConfig,
) -> tuple[int, int]:
    """Return (min_growth, max_growth) tool counts per stage."""
    min_growth = max(1, math.ceil(constraints.min_growth_frac * universe_size))
    max_growth = max(min_growth, math.floor(constraints.max_growth_frac * universe_size))
    # Cap at universe size (degenerate but defensive).
    max_growth = min(max_growth, universe_size)
    return min_growth, max_growth


def build_frequency_anchors_adaptive(
    task_tool_sets: Sequence[Iterable[str]],
    target_num_versions: int = 5,
    constraints: ConstraintConfig | None = None,
    importance: dict[str, float] | None = None,
) -> tuple[list[set[str]], dict]:
    """
    Greedy, constraint-aware version of build_frequency_anchors.

    Construction:
      - Rank tools by frequency (most-used first).
      - Sweep tools in rank order, opening a new stage when the running
        new-tool count crosses [min_growth, max_growth] AND the running
        new-task count crosses min_new_tasks_per_stage. Always force-close
        at max_growth.
      - The final stage absorbs any leftover tools (so C_K == universe).

    Post-processing:
      - Iteratively merge any stage whose new-task count is still below
        min_new_tasks_per_stage with its successor (or predecessor, if
        the violator is the final stage).

    Returns:
      (anchors, build_report)
        anchors[k]      -- C_{k+1} (zero-indexed cumulative tool sets)
        build_report    -- dict with bookkeeping: num_tools_dropped (always 0
                           in this fn; tool dropping happens upstream),
                           growth_bounds, constructed_K, post_merges, etc.
    """
    constraints = constraints or ConstraintConfig()
    tool_lists = [list(set(t)) for t in task_tool_sets]
    ranked = rank_tools(tool_lists, importance=importance)
    universe_size = len(ranked)
    if universe_size == 0:
        return [], {"reason": "empty_universe"}

    # Drop tasks with empty oracles -- they don't constrain anything.
    oracle_sets: list[set[str]] = [set(t) for t in tool_lists if t]

    min_growth, max_growth = _compute_growth_bounds(universe_size, constraints)

    # Greedy sweep.
    anchors: list[set[str]] = []
    cumulative: set[str] = set()
    solved = [False] * len(oracle_sets)
    i = 0

    initial_anchor_min = max(
        min_growth,
        math.ceil(constraints.initial_anchor_frac * universe_size),
    )

    while i < universe_size:
        stage_start_solved = sum(solved)
        stage_start_tools = len(cumulative)
        is_first_stage = len(anchors) == 0

        while i < universe_size:
            t = ranked[i]
            cumulative.add(t)
            i += 1

            # Update solved mask.
            for j, ts in enumerate(oracle_sets):
                if not solved[j] and ts <= cumulative:
                    solved[j] = True

            new_tools = len(cumulative) - stage_start_tools
            new_tasks = sum(solved) - stage_start_solved
            tools_remaining = universe_size - i

            # First-stage richness floor: keep absorbing until |C_1| reaches
            # the configured initial_anchor_min. This raises μ_1 by letting
            # more compound tasks legally land at V1.
            if is_first_stage and len(cumulative) < initial_anchor_min:
                continue

            # Force-close at upper growth bound (does not apply to V1 if it
            # was deliberately enlarged by initial_anchor_frac > min_growth).
            stage_max = (
                max(max_growth, initial_anchor_min)
                if is_first_stage
                else max_growth
            )
            if new_tools >= stage_max:
                break
            # Soft-close: enough new tools and enough new tasks, AND there
            # are at least min_growth tools left to form a future stage.
            if (
                new_tools >= min_growth
                and new_tasks >= constraints.min_new_tasks_per_stage
                and tools_remaining >= min_growth
            ):
                break

        anchors.append(set(cumulative))

    # Make sure C_K == universe (the loop guarantees this, but keep defensive).
    if anchors and anchors[-1] != set(ranked):
        anchors[-1] = set(ranked)
    if not anchors:
        anchors = [set(ranked)]

    # If we built more stages than target, that's fine -- constraints win.
    # If fewer, that's also fine (small universe / small dataset).

    # Post-pass: merge undersized stages.
    pre_merge_K = len(anchors)
    merge_log: list[dict] = []

    def _new_task_counts(anchors_: list[set[str]]) -> list[int]:
        prev: set[str] = set()
        out: list[int] = []
        for a in anchors_:
            cnt = sum(1 for ts in oracle_sets if ts <= a and not (ts <= prev))
            out.append(cnt)
            prev = a
        return out

    safety = len(anchors) + 5
    while safety > 0:
        safety -= 1
        counts = _new_task_counts(anchors)
        violators = [
            k for k, c in enumerate(counts)
            if c < constraints.min_new_tasks_per_stage
        ]
        if not violators or len(anchors) <= 1:
            break

        # Merge the first violator with a neighbour.
        k = violators[0]
        if k < len(anchors) - 1:
            # Drop anchors[k]: stage k+1 absorbs stage k's new tasks.
            dropped_size = len(anchors[k])
            anchors.pop(k)
            merge_log.append({
                "merged_stage": k,
                "into": "next",
                "reason": f"new_tasks={counts[k]} < min={constraints.min_new_tasks_per_stage}",
                "dropped_anchor_size": dropped_size,
            })
        else:
            # Final stage undersized: drop the previous anchor so the final
            # absorbs both bands.
            dropped_size = len(anchors[k - 1])
            anchors.pop(k - 1)
            merge_log.append({
                "merged_stage": k,
                "into": "previous",
                "reason": f"final_stage new_tasks={counts[k]} < min",
                "dropped_anchor_size": dropped_size,
            })

    build_report = {
        "universe_size": universe_size,
        "growth_bounds_tools": [min_growth, max_growth],
        "growth_bounds_frac": [
            constraints.min_growth_frac, constraints.max_growth_frac,
        ],
        "constraints": constraints.to_dict(),
        "K_pre_merge": pre_merge_K,
        "K_post_merge": len(anchors),
        "merges": merge_log,
        "schedule": [len(a) for a in anchors],
    }
    return anchors, build_report


# ---------------------------------------------------------------------------
# Validation: audit constraints on a built environment
# ---------------------------------------------------------------------------

def validate_frequency_benchmark(
    anchors: Sequence[set[str]],
    task_tool_sets: Sequence[Iterable[str]],
    stage_assignments: Sequence[int],
    adapt_counts: Sequence[int],
    test_counts: Sequence[int],
    constraints: ConstraintConfig | None = None,
) -> dict:
    """
    Audit a built frequency-staged benchmark against ConstraintConfig.

    Args:
        anchors:           cumulative tool sets C_1..C_K.
        task_tool_sets:    oracle tool set per task (same length as
                           stage_assignments).
        stage_assignments: assigned_stage per task (-1/None for dropped).
        adapt_counts:      |D_k^adapt| per stage.
        test_counts:       |D_k^test|  per stage.
        constraints:       thresholds; defaults to ConstraintConfig().

    Returns a dict with:
        - constraints: the thresholds used.
        - per_stage:   list of dicts (one per stage) with size + growth info
                       and the constraints each stage passes/fails.
        - rare_tools:  list of {tool, introduced_at_stage, usage_count}
                       for tools below min_tool_usage at-or-after introduction.
        - violations:  flat list of human-readable violation strings.
        - num_violations / passed.
    """
    constraints = constraints or ConstraintConfig()
    K = len(anchors)
    universe_size = len(anchors[-1]) if anchors else 0

    oracle_sets = [set(t) for t in task_tool_sets]

    # Build new_tools per stage and count tasks per stage by assignment.
    new_tools_per_stage: list[set[str]] = []
    prev: set[str] = set()
    for a in anchors:
        new_tools_per_stage.append(set(a) - prev)
        prev = set(a)

    new_task_counts = [0] * K
    for s in stage_assignments:
        if s is None or s < 0:
            continue
        if 0 <= s < K:
            new_task_counts[s] += 1

    violations: list[str] = []
    per_stage: list[dict] = []

    for k in range(K):
        new_tools = new_tools_per_stage[k]
        delta = len(new_tools)
        growth_frac = (delta / universe_size) if universe_size else 0.0
        n_new = new_task_counts[k]
        n_adapt = adapt_counts[k] if k < len(adapt_counts) else 0
        n_test = test_counts[k] if k < len(test_counts) else 0

        stage_violations: list[str] = []
        if n_test < constraints.min_test_per_stage:
            stage_violations.append(
                f"min_test: |D_test|={n_test} < {constraints.min_test_per_stage}"
            )
        if n_adapt < constraints.min_adapt_per_stage:
            stage_violations.append(
                f"min_adapt: |D_adapt|={n_adapt} < {constraints.min_adapt_per_stage}"
            )
        if n_new < constraints.min_new_tasks_per_stage:
            stage_violations.append(
                f"min_new: |D_new|={n_new} < {constraints.min_new_tasks_per_stage}"
            )
        if n_test <= 0:  # hard constraint (7)
            stage_violations.append("empty_test_set (hard constraint)")
        # Growth (6): only check intermediate stages strictly; first stage
        # can be small "core only", but we still warn if outside bounds.
        if not (constraints.min_growth_frac <= growth_frac <= constraints.max_growth_frac):
            stage_violations.append(
                f"growth: |C_k|-|C_(k-1)|={delta} ({growth_frac:.2%} of universe) "
                f"outside [{constraints.min_growth_frac:.0%}, "
                f"{constraints.max_growth_frac:.0%}]"
            )

        per_stage.append({
            "stage": k,
            "name": f"V{k+1}",
            "cumulative_tools": len(anchors[k]),
            "new_tools": delta,
            "growth_frac": round(growth_frac, 4),
            "new_tasks": n_new,
            "adapt": n_adapt,
            "test": n_test,
            "violations": stage_violations,
        })
        for v in stage_violations:
            violations.append(f"V{k+1}: {v}")

    # Rare tool check (constraint 4).
    rare_tools: list[dict] = []
    for k in range(K):
        for tool in sorted(new_tools_per_stage[k]):
            usage = sum(
                1 for j, oracle in enumerate(oracle_sets)
                if tool in oracle
                and stage_assignments[j] is not None
                and stage_assignments[j] >= k
            )
            if usage < constraints.min_tool_usage:
                rare_tools.append({
                    "tool": tool,
                    "introduced_at_stage": k,
                    "usage_count_at_or_after_intro": usage,
                })
                violations.append(
                    f"V{k+1}: rare tool {tool!r} used in {usage} tasks "
                    f"(min={constraints.min_tool_usage})"
                )

    return {
        "constraints": constraints.to_dict(),
        "K": K,
        "universe_size": universe_size,
        "per_stage": per_stage,
        "rare_tools": rare_tools,
        "violations": violations,
        "num_violations": len(violations),
        "passed": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# Pretty-printing the report
# ---------------------------------------------------------------------------

def format_report(report: FrequencyVersionReport, max_tools_shown: int = 60) -> str:
    """Pretty-print a FrequencyVersionReport. Used by the CLI."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("FREQUENCY-DRIVEN VERSION CHAIN")
    lines.append("=" * 72)
    lines.append(f"Universe size : {report.universe_size} unique tools")
    lines.append(f"Schedule      : {report.schedule}")
    lines.append(f"# versions    : {report.num_versions}")
    if report.dropped_task_indices:
        lines.append(f"Dropped tasks : {len(report.dropped_task_indices)}")
    lines.append("")

    lines.append("-- Tool frequencies (top {}) --".format(max_tools_shown))
    for i, (t, c) in enumerate(report.tool_frequencies.items()):
        if i >= max_tools_shown:
            lines.append(f"   ... ({len(report.tool_frequencies) - max_tools_shown} more)")
            break
        lines.append(f"   {c:4d}  {t}")
    lines.append("")

    lines.append("-- Versions --")
    for k, (anchor, delta) in enumerate(zip(report.anchors, report.delta_tools)):
        new_count = report.tasks_per_stage.get(k, 0)
        cum_count = report.cumulative_tasks_per_stage.get(k, 0)
        lines.append(
            f"   V{k+1}  |C_k|={len(anchor):3d}  +{len(delta):2d} new tools  "
            f"new tasks={new_count:4d}  cumulative tasks={cum_count:4d}"
        )
        # Show only delta to keep output compact.
        if delta:
            preview = ", ".join(delta[:8])
            more = "" if len(delta) <= 8 else f", ... (+{len(delta) - 8})"
            lines.append(f"          + {preview}{more}")
    lines.append("=" * 72)
    return "\n".join(lines)


def rebalance_schedule_for_complexity(
    anchors: list[set[str]],
    oracle_sets: list[set[str]],
    constraints: ConstraintConfig,
    eps: float = 1.0,
    max_iter: int = 100,
) -> tuple[list[set[str]], dict]:
    """Iteratively promote tools from hot stages into earlier ones to flatten
    per-stage mean(|T|), without violating growth bounds, the min-new-tasks
    floor, or the earliest-solvable rule.

    Returns (new_anchors, report).

    Algorithm:
      while iterations remain and spread > eps:
        - Compute μ_k for k=1..K under current anchors.
        - hot = argmax_k μ_k
        - For each tool t introduced at stage `hot`
          (i.e. t ∈ C_hot \\ C_{hot-1}):
            simulate "promote t into C_{hot-1}" (which keeps cumulative
            structure intact and only shifts task assignments earlier).
            Check that:
              (a) growth at hot-1 stays ≤ max_growth_frac · |U|
              (b) growth at hot stays ≥ min_growth_frac · |U|
              (c) every stage still has ≥ min_new_tasks_per_stage tasks
            If valid, score it by the new max-min spread.
        - Apply the best valid promotion; if none reduces the spread, stop.

    The earliest-solvable rule is preserved by construction because the
    only mutation we do is *adding* tools to earlier anchors.
    """
    K = len(anchors)
    if K < 2:
        return [set(a) for a in anchors], {
            "skipped": True,
            "reason": "K<2 nothing to rebalance",
        }

    new_anchors = [set(a) for a in anchors]
    universe_size = max(len(a) for a in new_anchors)
    min_g, max_g = _compute_growth_bounds(universe_size, constraints)
    min_per_stage = constraints.min_new_tasks_per_stage

    def _per_stage(anchs: list[set[str]]) -> tuple[list[float], list[int]]:
        cnt = [0] * K
        sm = [0] * K
        for T in oracle_sets:
            assigned = K - 1
            for k, C in enumerate(anchs):
                if T <= C:
                    assigned = k
                    break
            cnt[assigned] += 1
            sm[assigned] += len(T)
        mus = [sm[k] / max(1, cnt[k]) for k in range(K)]
        return mus, cnt

    def _growths(anchs: list[set[str]]) -> list[int]:
        prev = 0
        out = []
        for C in anchs:
            out.append(len(C) - prev)
            prev = len(C)
        return out

    initial_growths = _growths(new_anchors)
    # Per-stage max growth: at least max_g, but never below the initial
    # growth (the user may have deliberately set initial_anchor_frac to
    # exceed max_growth_frac for V_1).
    max_g_per_stage = [max(max_g, ig) for ig in initial_growths]

    def _valid(anchs: list[set[str]]) -> bool:
        gs = _growths(anchs)
        for k, g in enumerate(gs):
            if g > max_g_per_stage[k]:
                return False
            if k < K - 1 and g < min_g:
                return False
        _, cnt = _per_stage(anchs)
        if any(c < min_per_stage for c in cnt):
            return False
        return True

    def _score(anchs: list[set[str]]) -> float:
        cand_mus, _ = _per_stage(anchs)
        return max(cand_mus) - min(cand_mus)

    moves: list[dict] = []
    initial_mus, _ = _per_stage(new_anchors)
    for it in range(max_iter):
        mus, cnt = _per_stage(new_anchors)
        spread = max(mus) - min(mus)
        if spread <= eps:
            break

        best_anchors = None
        best_spread = spread
        best_meta: dict = {}

        # Strategy A: single-tool promotion at any (k -> k-1) boundary,
        # not just from the hottest stage. This handles tied/near-tied μ.
        for k_from in range(1, K):
            new_at_k = new_anchors[k_from] - new_anchors[k_from - 1]
            for tool in sorted(new_at_k):
                cand = [set(a) for a in new_anchors]
                cand[k_from - 1].add(tool)
                if not _valid(cand):
                    continue
                cand_spread = _score(cand)
                if cand_spread < best_spread - 1e-9:
                    best_spread = cand_spread
                    best_anchors = cand
                    best_meta = {
                        "strategy":  "single",
                        "promoted":  [tool],
                        "from_stage": k_from,
                        "to_stage":  k_from - 1,
                    }

        # Strategy B: task-targeted batch promotion.
        # For the heaviest task at each stage k>=1, try promoting *all*
        # of its tools that are not yet in C_{k-1} into C_{k-1}. This
        # moves the whole task earlier in one shot, which single-tool
        # promotion cannot do when a task needs multiple new tools at
        # the hot stage.
        for k_hot in range(1, K):
            tasks_at_hot = [
                T for T in oracle_sets
                if (T <= new_anchors[k_hot])
                and (k_hot == 0 or not (T <= new_anchors[k_hot - 1]))
            ]
            if not tasks_at_hot:
                continue
            tasks_sorted = sorted(tasks_at_hot, key=lambda T: -len(T))
            for T in tasks_sorted[:5]:  # top-5 heaviest at this stage
                missing = T - new_anchors[k_hot - 1]
                if not missing:
                    continue
                cand = [set(a) for a in new_anchors]
                cand[k_hot - 1] |= missing
                if not _valid(cand):
                    continue
                cand_spread = _score(cand)
                if cand_spread < best_spread - 1e-9:
                    best_spread = cand_spread
                    best_anchors = cand
                    best_meta = {
                        "strategy":  "batch",
                        "promoted":  sorted(missing),
                        "from_stage": k_hot,
                        "to_stage":  k_hot - 1,
                    }

        if best_anchors is None:
            break

        cand_mus, _ = _per_stage(best_anchors)
        moves.append({
            "iter":         it,
            **best_meta,
            "spread_after": best_spread,
            "mus_after":    cand_mus,
        })
        new_anchors = best_anchors

    final_mus, final_counts = _per_stage(new_anchors)
    return new_anchors, {
        "skipped":         False,
        "max_iter":        max_iter,
        "eps":             eps,
        "growth_bounds":   [min_g, max_g],
        "initial_mus":     initial_mus,
        "initial_spread":  max(initial_mus) - min(initial_mus),
        "final_mus":       final_mus,
        "final_spread":    max(final_mus) - min(final_mus),
        "final_counts":    final_counts,
        "moves":           moves,
    }
__EVOHARNESS_APEX_FILE__
```

## Tools · Steps 3 and 4 shared accumulation and writer

```bash
mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder"
cat >> "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/builder.py" <<'__EVOHARNESS_APEX_FILE__'
def _common(record: TaskRecord) -> dict[str, Any]:
    world = record.world
    raw = record.raw
    expected_output = str(raw.get("expected_output") or "")
    apps = [str(app["service_name"]) for app in world.get("apps") or []]
    return {
        "title": str(raw.get("task_name") or record.task_id),
        "summary": record.prompt,
        "category": record.domain,
        "subdomain": str(world.get("world_name") or world["world_id"]),
        "task_split": "official-evaluation",
        "agent_must_do": [
            "Return the requested console response"
            if expected_output == "message_in_console"
            else f"Produce the requested artifact ({expected_output})"
        ],
        "input_files": record.input_files,
        "taxonomy": {
            "domain": record.domain,
            "world_id": world["world_id"],
            "expected_output": expected_output,
        },
        "source_repo_path": f"tasks_and_rubrics.json#{record.task_id}",
        "repository_url": cfg.SOURCE_URL,
        "source_commit": cfg.SOURCE_COMMIT,
        "difficulty": None,
        "tags": sorted(set([record.domain, expected_output] + apps)),
        "artifacts": {
            "expected_output": expected_output,
            "gold_response_type": raw.get("gold_response_type"),
            "gold_files": record.gold_files,
        },
        "resource_limits": {
            "framework": "Archipelago",
            "max_steps": 100,
            "agent_timeout_sec": 10800,
            "tool_call_timeout_sec": 60,
            "allow_internet": False,
        },
        "metadata": {
            "world_id": world["world_id"],
            "world_name": world.get("world_name"),
            "world_description": world.get("world_description"),
            "apps": apps,
            "task_input_snapshot": raw.get("task_input_files"),
            "gold_response_type": raw.get("gold_response_type"),
        },
    }


def _held_out_evaluation(record: TaskRecord) -> dict[str, Any]:
    return {
        "framework": "Archipelago",
        "held_out": True,
        "expected_output": record.raw.get("expected_output"),
        "gold_response_type": record.raw.get("gold_response_type"),
        "gold_response": record.raw.get("gold_response"),
        "gold_files": record.gold_files,
        "rubric": record.raw.get("rubric") or [],
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json_dump(row) + "\n" for row in rows), encoding="utf-8")


def _write_rows(root: Path, rows: dict[int, list[dict[str, Any]]]) -> None:
    for stage, stage_rows in sorted(rows.items()):
        for split in ("train", "test"):
            selected = sorted(
                (row for row in stage_rows if row["split"] == split),
                key=lambda row: row["task_id"],
            )
            _write_jsonl(root / f"v{stage}" / f"{split}.jsonl", selected)


def _tools_rows(
    records: dict[str, TaskRecord], anchors: list[set[str]],
    assignments: dict[str, int], splits: dict[str, str],
) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task_id, stage in sorted(assignments.items()):
        record = records[task_id]
        rows[stage].append({
            "domain": cfg.DOMAIN,
            "version": f"v{stage}",
            "split": splits[task_id],
            "task_id": task_id,
            "oracle_tools": record.tools,
            "cummulative_tools": sorted(anchors[stage - 1]),
            "task_prompt": record.prompt,
            "software": sorted(anchors[stage - 1]),
            "annotation_evidence": record.tool_evidence,
            **_common(record),
        })
    return rows


def _skills_rows(
    records: dict[str, TaskRecord], anchors: list[set[str]],
    assignments: dict[str, int], splits: dict[str, str],
) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task_id, stage in sorted(assignments.items()):
        record = records[task_id]
        common = _common(record)
        row = {
            "domain": cfg.DOMAIN,
            "version": f"v{stage}",
            "split": splits[task_id],
            "task_id": task_id,
            "oracle_skills": record.skills,
            "cummulative_oracle_skills": sorted(anchors[stage - 1]),
            "system_prompt": HOUSE_RULES,
            "user_prompt": record.prompt,
            "software": record.tools,
            "evaluation": _held_out_evaluation(record),
            "patcher_prompts": [],
            "prompt_suffix": "",
            "annotation_evidence": record.skill_evidence,
            **common,
        }
        rows[stage].append(row)
    return rows


def _owner_maps(catalog: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    owner_of = {str(spec["name"]): str(spec["owner"]) for spec in catalog}
    owned: dict[str, list[str]] = defaultdict(list)
    for tool, owner in sorted(owner_of.items()):
        owned[owner].append(tool)
    return owner_of, dict(sorted(owned.items()))


def _agent_system(cumulative: list[str], owner_tools: dict[str, list[str]]) -> str:
    roster = "\n".join(
        f"- {owner}: {', '.join(owner_tools[owner])}" for owner in cumulative
    )
    return HOUSE_RULES.rstrip() + "\n\n# Specialist roster\n" + roster + (
        "\n\nDelegate to the smallest specialist set that covers the task, then "
        "integrate and validate the result."
    )


def _agents_rows(
    records: dict[str, TaskRecord], anchors: list[set[str]],
    assignments: dict[str, int], splits: dict[str, str],
    owner_of: dict[str, str], owner_tools: dict[str, list[str]],
) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for task_id, stage in sorted(assignments.items()):
        record = records[task_id]
        agents = sorted({owner_of[tool] for tool in record.tools})
        cumulative_agents = sorted(anchors[stage - 1])
        allowed = sorted({tool for owner in cumulative_agents for tool in owner_tools[owner]})
        rows[stage].append({
            "domain": cfg.DOMAIN,
            "version": f"v{stage}",
            "split": splits[task_id],
            "task_id": task_id,
            "oracle_agents": agents,
            "cumulative_agents": cumulative_agents,
            "oracle_skills": record.skills,
            "system_prompt": _agent_system(cumulative_agents, owner_tools),
            "task_prompt": record.prompt,
            "user_prompt": record.prompt,
            "software": allowed,
            "oracle_tools": record.tools,
            "cummulative_tools": allowed,
            "annotation_evidence": record.tool_evidence,
            **_common(record),
        })
    return rows


def _skill_content(spec: dict[str, Any], *, heading: int = 2) -> str:
    marker = "#" * heading
    return (
        f"{marker} {spec['title']}\n\n*{spec['description']}*\n\n"
        f"{str(spec['procedure']).strip()}\n\n"
        f"**Notes:** {str(spec['notes']).strip()}\n"
    )


def _skill_md(
    spec: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], total: int,
) -> str:
    examples = "\n".join(f"- `{task_id}`" for task_id in sorted(evidence)[:5])
    links = "\n".join(f"- `{name}`" for name in spec.get("see_also") or []) or "- _none_"
    return (
        "---\n"
        f"name: {spec['name']}\n"
        f"description: {json.dumps(spec['description'], ensure_ascii=False)}\n"
        "---\n\n"
        f"# {spec['title']}\n\n"
        "<!-- mined-by-apex-builder (deterministic; no LLM) -->\n"
        "## Overview\n\n"
        f"{spec['description']} Observed in **{len(evidence)}/{total}** APEX tasks.\n\n"
        "## When to use\n\n"
        "Use this procedure when its fixed patterns match the task specification "
        "or its software anchors overlap inherited oracle tools.\n\n"
        "## Procedure\n\n"
        f"{str(spec['procedure']).strip()}\n\n"
        "## Notes\n\n"
        f"- {str(spec['notes']).strip()}\n\n"
        "## See also\n\n"
        f"{links}\n\n"
        "## Evidence (mined)\n\n"
        f"- tagged tasks: **{len(evidence)}**\n"
        f"- representative tasks:\n{examples}\n"
        "<!-- end-mined -->\n"
    )


def _write_skill_library(root: Path, records: dict[str, TaskRecord]) -> None:
    by_skill: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    for record in records.values():
        for name, evidence in record.skill_evidence.items():
            by_skill[name][record.task_id] = evidence
    oracle = root / "_oracle"
    prompt = oracle / "stripped_system_prompt.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text(HOUSE_RULES, encoding="utf-8")
    active = sorted(by_skill)
    write_json(oracle / "active_skills.json", {
        "domain": cfg.DOMAIN,
        "method": (
            "ALE-compatible deterministic closed-catalog atoms matched against the "
            "task specification and inherited required-tool anchors; no LLM"
        ),
        "active_skills": active,
        "task_count_per_skill": {name: len(by_skill[name]) for name in active},
    })
    for spec in load_skill_catalog():
        name = str(spec["name"])
        if name not in by_skill:
            continue
        skill_root = oracle / "skills" / name
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "SKILL.md").write_text(
            _skill_md(spec, by_skill[name], len(records)), encoding="utf-8",
        )
        write_json(skill_root / "index.json", {
            "name": name,
            "title": spec["title"],
            "description": spec["description"],
            "tier": spec["tier"],
            "signature": {
                "domains": spec["domains"],
                "patterns": spec["patterns"],
                "software_any": spec["tools"],
                "expected_outputs": spec["expected_outputs"],
            },
            "task_count": len(by_skill[name]),
            "tagged_task_ids": sorted(by_skill[name]),
            "matching_evidence": by_skill[name],
        })


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _agent_skill_md(
    owner: str, tools: list[str], capability_specs: list[dict[str, Any]],
) -> str:
    description = f"APEX specialist that owns {', '.join(tools)}."
    text = (
        "---\n"
        f"name: {owner}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"# {owner.replace('_', ' ').title()} Specialist\n\n"
        "## Scope\n\n"
        f"Own task work requiring {', '.join(f'`{tool}`' for tool in tools)}. "
        "Coordinate through the lead orchestrator when another application is needed.\n\n"
        "## Operating procedure\n\n"
        "Read the delegated objective, inspect only the relevant application state, perform "
        "the requested work, validate it, and return exact results and artifact locations.\n"
    )
    if capability_specs:
        text += (
            "\n## Capabilities\n\n"
            "Procedures mined by the skills track and re-homed by operation ownership.\n\n"
            + "\n\n".join(_skill_content(spec, heading=3) for spec in capability_specs)
        )
    return text


def _agent_toml(owner: str, tools: list[str]) -> str:
    description = f"Route tasks requiring {', '.join(tools)} to this application specialist."
    instructions = (
        f"You are the {owner} specialist. Read agent_skills/{owner}/SKILL.md and "
        "agent_skills/_shared/house_rules.md. Your owned operations are: "
        + ", ".join(tools)
        + ". Complete only the delegated portion, validate it, and report to the orchestrator."
    )
    return "\n".join([
        f"name = {_toml_string(owner)}",
        f"description = {_toml_string(description)}",
        f"developer_instructions = {_toml_string(instructions)}",
        "",
        "[[skills.config]]",
        f"path = {_toml_string(f'agent_skills/{owner}/SKILL.md')}",
        "enabled = true",
        "",
        "[tools]",
        "exec_command = true",
        "shell_tool = true",
        "apply_patch = true",
        "",
    ])


def _materialize_agents(
    root: Path, anchors: list[set[str]], owner_tools: dict[str, list[str]],
    versions: list[dict[str, Any]], active_skills: set[str],
) -> None:
    owners = sorted(anchors[-1])
    specs = [spec for spec in load_skill_catalog() if spec["name"] in active_skills]
    tool_owner = {tool: owner for owner, tools in owner_tools.items() for tool in tools}
    owner_specs: dict[str, list[dict[str, Any]]] = {owner: [] for owner in owners}
    shared_specs: list[dict[str, Any]] = []
    homes: dict[str, list[str]] = {}
    for spec in specs:
        anchored = sorted({
            tool_owner[tool] for tool in spec.get("tools") or [] if tool in tool_owner
        })
        if spec["tier"] == "capability" and anchored:
            homes[spec["name"]] = anchored
            for owner in anchored:
                owner_specs[owner].append(spec)
        else:
            homes[spec["name"]] = ["_shared"]
            shared_specs.append(spec)

    capabilities = root / "_capabilities"
    shared = capabilities / "_shared" / "house_rules.md"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared_text = HOUSE_RULES.rstrip()
    if shared_specs:
        shared_text += "\n\n# Cross-cutting skills\n\n" + "\n\n".join(
            _skill_content(spec) for spec in shared_specs
        )
    shared_text += "\n"
    shared.write_text(shared_text, encoding="utf-8")
    family_text: dict[str, str] = {}
    for owner in owners:
        target = capabilities / owner / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        family_text[owner] = _agent_skill_md(owner, owner_tools[owner], owner_specs[owner])
        target.write_text(family_text[owner], encoding="utf-8")
    for spec in specs:
        haystacks = (
            [shared_text] if homes[spec["name"]] == ["_shared"]
            else [family_text[owner] for owner in homes[spec["name"]]]
        )
        if not any(str(spec["procedure"]).strip() in text for text in haystacks):
            raise ValueError(f"agent materialization dropped {spec['name']}")
    write_json(capabilities / "manifest.json", {
        "domain": cfg.DOMAIN,
        "method": "ALE-style application-owner re-homing of mined skill procedures",
        "active_skills": sorted(active_skills),
        "skill_homes": homes,
        "content_dropped": 0,
    })

    def write_pool(dest: Path, selected: list[str], manifest: dict[str, Any]) -> None:
        shared_dest = dest / "agent_skills" / "_shared" / "house_rules.md"
        shared_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shared, shared_dest)
        entries = []
        for owner in selected:
            skill_dest = dest / "agent_skills" / owner / "SKILL.md"
            skill_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(capabilities / owner / "SKILL.md", skill_dest)
            agent_dest = dest / "agents" / f"{owner}.toml"
            agent_dest.parent.mkdir(parents=True, exist_ok=True)
            agent_dest.write_text(_agent_toml(owner, owner_tools[owner]), encoding="utf-8")
            entries.append({
                "name": owner,
                "owned_software": owner_tools[owner],
                "file": f"agents/{owner}.toml",
                "skill": f"agent_skills/{owner}/SKILL.md",
            })
        write_json(dest / "manifest.json", {**manifest, "agents": entries})

    write_pool(root / "_agents", owners, {
        "domain": cfg.DOMAIN,
        "kind": "full-roster",
        "cumulative_agents": owners,
    })
    for stage, cumulative in enumerate(anchors, 1):
        write_pool(root / f"v{stage}", sorted(cumulative), versions[stage - 1])


def _stage_manifest(
    task_caps: dict[str, set[str]], anchors: list[set[str]],
    assignments: dict[str, int], splits: dict[str, str], noun: str,
) -> list[dict[str, Any]]:
    result = []
    previous: set[str] = set()
    for stage, cumulative in enumerate(anchors, 1):
        ids = sorted(task_id for task_id, value in assignments.items() if value == stage)
        result.append({
            "version": f"v{stage}",
            f"new_{noun}": sorted(cumulative - previous),
            f"cumulative_{noun}": sorted(cumulative),
            "tasks": len(ids),
            "train": sum(splits[task_id] == "train" for task_id in ids),
            "test": sum(splits[task_id] == "test" for task_id in ids),
            "task_ids": ids,
        })
        previous = cumulative
    return result


def _frequency_validation(
    task_caps: dict[str, set[str]], anchors: list[set[str]],
    assignments: dict[str, int], splits: dict[str, str], axis: str,
) -> dict[str, Any]:
    ids = sorted(task_caps)
    adapt_counts, test_counts = [], []
    for stage in range(1, len(anchors) + 1):
        selected = [task_id for task_id in ids if assignments[task_id] == stage]
        adapt_counts.append(sum(splits[task_id] == "train" for task_id in selected))
        test_counts.append(sum(splits[task_id] == "test" for task_id in selected))
    return validate_frequency_benchmark(
        anchors=anchors,
        task_tool_sets=[task_caps[task_id] for task_id in ids],
        stage_assignments=[assignments[task_id] - 1 for task_id in ids],
        adapt_counts=adapt_counts,
        test_counts=test_counts,
        constraints=_axis_constraints(axis),
    )


def _axis_manifest(
    axis: str, task_caps: dict[str, set[str]], anchors: list[set[str]],
    assignments: dict[str, int], splits: dict[str, str], noun: str,
    excluded: list[dict[str, str]], construction: dict[str, Any],
) -> dict[str, Any]:
    return {
        "domain": cfg.DOMAIN,
        "axis": axis,
        "seed": cfg.RANDOM_SEED,
        "source_url": cfg.SOURCE_URL,
        "source_commit": cfg.SOURCE_COMMIT,
        "archipelago_url": cfg.ARCHIPELAGO_URL.removesuffix(".git"),
        "archipelago_commit": cfg.ARCHIPELAGO_COMMIT,
        "discovered_tasks": cfg.EXPECTED_TASKS,
        "retained_tasks": len(task_caps),
        "excluded_tasks": excluded,
        "target_versions": cfg.TARGET_VERSIONS,
        "emitted_versions": len(anchors),
        "min_train": cfg.MIN_TRAIN,
        "min_test": cfg.MIN_TEST,
        "frequency": _frequency(task_caps.values()),
        "ranked_capabilities": rank_tools([task_caps[task_id] for task_id in sorted(task_caps)]),
        "construction": construction,
        "versions": _stage_manifest(task_caps, anchors, assignments, splits, noun),
    }


def build_into(dest: Path) -> dict[str, Any]:
    if dest.exists():
        raise FileExistsError(f"fresh build destination already exists: {dest}")
    records_list = discover_tasks()
    records = {record.task_id: record for record in records_list}
    annotated = [record for record in records_list if record.tools]
    excluded = [
        {"task_id": record.task_id, "reason": "no task-facing closed-catalog operation evidence"}
        for record in records_list if not record.tools
    ]
    tool_tasks = {record.task_id: set(record.tools) for record in annotated}
    tool_anchors, tool_assignments, tool_report = _build_frequency_curriculum(
        tool_tasks, axis="tools",
    )
    tool_splits = split_curriculum(tool_assignments)
    tool_report["validation"] = _frequency_validation(
        tool_tasks, tool_anchors, tool_assignments, tool_splits, "tools",
    )

    skill_records = [record for record in records_list if record.skills]
    skill_excluded = [
        {"task_id": record.task_id, "reason": "no ALE-style lexical or software-anchor match"}
        for record in records_list if not record.skills
    ]
    skill_tasks = {record.task_id: set(record.skills) for record in skill_records}
    skill_anchors, skill_assignments, skill_splits, skill_report = _build_skill_curriculum(
        skill_records
    )

    catalog = load_catalog()
    owner_of, owner_tools_all = _owner_maps(catalog)
    agent_tasks = {
        task_id: {owner_of[tool] for tool in tools}
        for task_id, tools in tool_tasks.items()
    }
    agent_anchors, agent_assignments, agent_report = _build_frequency_curriculum(
        agent_tasks, axis="agents", splits=tool_splits,
    )
    agent_report["validation"] = _frequency_validation(
        agent_tasks, agent_anchors, agent_assignments, tool_splits, "agents",
    )
    active_owner_tools = {
        owner: sorted(
            tool for tool in owner_tools_all[owner]
            if any(tool in task_tools for task_tools in tool_tasks.values())
        )
        for owner in sorted(agent_anchors[-1])
    }

    roots = {axis: dest / axis for axis in ("tools", "skills", "agents")}
    for root in roots.values():
        root.mkdir(parents=True)
    manifests = {
        "tools": _axis_manifest(
            "tools", tool_tasks, tool_anchors, tool_assignments, tool_splits,
            "tools", excluded, tool_report,
        ),
        "skills": _axis_manifest(
            "skills", skill_tasks, skill_anchors, skill_assignments, skill_splits,
            "skills", skill_excluded, skill_report,
        ),
        "agents": _axis_manifest(
            "agents", agent_tasks, agent_anchors, agent_assignments, tool_splits,
            "agents", excluded, agent_report,
        ),
    }
    for axis, manifest in manifests.items():
        write_json(roots[axis] / "manifest.json", manifest)
    write_json(roots["tools"] / "annotation_evidence.json", {
        record.task_id: {"oracle_tools": record.tools, "evidence": record.tool_evidence}
        for record in records_list
    })
    write_json(roots["skills"] / "annotation_evidence.json", {
        record.task_id: {"oracle_skills": record.skills, "evidence": record.skill_evidence}
        for record in records_list
    })
    _write_rows(
        roots["tools"],
        _tools_rows(records, tool_anchors, tool_assignments, tool_splits),
    )
    _write_skill_library(roots["skills"], {record.task_id: record for record in skill_records})
    _write_rows(
        roots["skills"],
        _skills_rows(records, skill_anchors, skill_assignments, skill_splits),
    )
    _materialize_agents(
        roots["agents"], agent_anchors, active_owner_tools,
        manifests["agents"]["versions"], set().union(*skill_tasks.values()),
    )
    _write_rows(
        roots["agents"],
        _agents_rows(
            records, agent_anchors, agent_assignments, tool_splits,
            owner_of, active_owner_tools,
        ),
    )
    return manifests


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _jsonl_rows(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows = []
    for path in sorted(root.glob("v*/*.jsonl")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append((path, json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return rows


def validate_candidate(candidate: Path) -> dict[str, Any]:
    problems: list[str] = []
    records_list = discover_tasks()
    records = {record.task_id: record for record in records_list}
    source_ids = set(records)
    _, repo_paths = _repo_file_index()
    catalog = load_catalog()
    catalog_names = {str(spec["name"]) for spec in catalog}
    owner_of, owner_tools = _owner_maps(catalog)
    skill_specs = {str(spec["name"]): spec for spec in load_skill_catalog()}
    skill_names = set(skill_specs)
    required = {
        "tools": TOOLS_REQUIRED,
        "skills": SKILLS_REQUIRED,
        "agents": AGENTS_REQUIRED,
    }
    fields = {
        "tools": ("oracle_tools", "cummulative_tools", "tools"),
        "skills": ("oracle_skills", "cummulative_oracle_skills", "skills"),
        "agents": ("oracle_agents", "cumulative_agents", "agents"),
    }
    summary: dict[str, Any] = {}
    rows_by_axis: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for axis in ("tools", "skills", "agents"):
        root = candidate / axis
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            problems.append(f"{axis}: missing manifest.json")
            continue
        manifest = read_json(manifest_path)
        if manifest.get("source_commit") != cfg.SOURCE_COMMIT:
            problems.append(f"{axis}: source pin mismatch")
        if manifest.get("archipelago_commit") != cfg.ARCHIPELAGO_COMMIT:
            problems.append(f"{axis}: Archipelago pin mismatch")
        if manifest.get("discovered_tasks") != cfg.EXPECTED_TASKS:
            problems.append(f"{axis}: discovered task count mismatch")
        construction = manifest.get("construction") or {}
        expected_impl = (
            "evovle_skills.builder.sequencer.sequence_curriculum"
            if axis == "skills"
            else "evolve_tools.builder.frequency_config.build_frequency_anchors_adaptive"
        )
        if construction.get("implementation") != expected_impl:
            problems.append(f"{axis}: non-canonical construction implementation")
        if not (construction.get("validation") or {}).get("passed"):
            problems.append(f"{axis}: canonical construction validation failed")
        versions = manifest.get("versions") or []
        if len(versions) < cfg.MIN_VERSIONS:
            problems.append(f"{axis}: fewer than {cfg.MIN_VERSIONS} versions")
        if len(versions) != manifest.get("emitted_versions"):
            problems.append(f"{axis}: emitted version count mismatch")
        previous: set[str] = set()
        for stage, version in enumerate(versions, 1):
            cumulative = set(version.get(f"cumulative_{fields[axis][2]}") or [])
            if not previous < cumulative:
                problems.append(f"{axis} v{stage}: cumulative chain is not strict")
            previous = cumulative
            if version.get("train", 0) < cfg.MIN_TRAIN:
                problems.append(f"{axis} v{stage}: train floor failed")
            if version.get("test", 0) < cfg.MIN_TEST:
                problems.append(f"{axis} v{stage}: test floor failed")

        rows = _jsonl_rows(root)
        rows_by_axis[axis] = rows
        seen: set[str] = set()
        schemas: set[tuple[str, ...]] = set()
        for path, row in rows:
            task_id = str(row.get("task_id") or "")
            if task_id not in source_ids:
                problems.append(f"{axis}: unknown source task {task_id!r}")
                continue
            if task_id in seen:
                problems.append(f"{axis}: duplicate task {task_id}")
            seen.add(task_id)
            schemas.add(tuple(sorted(row)))
            missing = required[axis] - set(row)
            if missing:
                problems.append(f"{axis} {task_id}: missing fields {sorted(missing)}")
            oracle_field, cumulative_field, noun = fields[axis]
            oracle = set(row.get(oracle_field) or [])
            cumulative = set(row.get(cumulative_field) or [])
            if not oracle or not oracle <= cumulative:
                problems.append(f"{axis} {task_id}: invalid oracle/cumulative coverage")
            try:
                stage = int(str(row["version"]).removeprefix("v"))
                introduced = set(versions[stage - 1].get(f"new_{noun}") or [])
            except (KeyError, IndexError, ValueError):
                introduced = set()
            if not oracle & introduced:
                problems.append(f"{axis} {task_id}: no assigned-stage new capability")
            record = records[task_id]
            if row.get("user_prompt", row.get("task_prompt")) != record.prompt:
                problems.append(f"{axis} {task_id}: full task prompt was not preserved")
            for source_path in record.input_files + record.gold_files:
                if source_path not in repo_paths:
                    problems.append(f"{axis} {task_id}: unknown artifact reference {source_path}")
            if axis == "tools":
                if oracle != set(record.tools):
                    problems.append(f"tools {task_id}: oracle differs from annotation")
                if oracle - catalog_names:
                    problems.append(f"tools {task_id}: unknown catalog operation")
                evidence = row.get("annotation_evidence") or {}
                if any(not evidence.get(tool) for tool in oracle):
                    problems.append(f"tools {task_id}: accepted operation lacks evidence")
                available = _world_owners(record.world)
                if any(owner_of[tool] not in available for tool in oracle):
                    problems.append(f"tools {task_id}: operation owner unavailable in world")
            elif axis == "skills":
                if oracle != set(record.skills):
                    problems.append(f"skills {task_id}: oracle differs from annotation")
                if oracle - skill_names:
                    problems.append(f"skills {task_id}: unknown catalog skill")
                if row.get("system_prompt") != HOUSE_RULES:
                    problems.append(f"skills {task_id}: held-out skill names leaked into prompt")
                for skill in oracle:
                    skill_root = root / "_oracle" / "skills" / skill
                    if not (skill_root / "SKILL.md").is_file():
                        problems.append(f"skills {task_id}: missing {skill}/SKILL.md")
                    if not (skill_root / "index.json").is_file():
                        problems.append(f"skills {task_id}: missing {skill}/index.json")
            else:
                expected = {owner_of[tool] for tool in row.get("oracle_tools") or []}
                if oracle != expected:
                    problems.append(f"agents {task_id}: owners do not match oracle tools")
                allowed = {
                    tool for owner in cumulative for tool in owner_tools.get(owner, [])
                    if any(tool in record_tools for record_tools in (r.tools for r in records_list))
                }
                if set(row.get("software") or []) != allowed:
                    problems.append(f"agents {task_id}: software union does not match roster")
        if len(schemas) > 1:
            problems.append(f"{axis}: schema drift across JSONL rows")
        if len(rows) != manifest.get("retained_tasks"):
            problems.append(f"{axis}: row count does not match retained_tasks")
        summary[axis] = {
            "rows": len(rows),
            "versions": len(versions),
            "capabilities": len(previous),
        }

    skills_root = candidate / "skills" / "_oracle" / "skills"
    for name, spec in skill_specs.items():
        root = skills_root / name
        if not root.is_dir():
            continue
        if str(spec["procedure"]).strip() not in (root / "SKILL.md").read_text(encoding="utf-8"):
            problems.append(f"skills: {name} dropped its catalog procedure")
        index = read_json(root / "index.json")
        if index.get("tier") != spec["tier"] or not index.get("signature"):
            problems.append(f"skills: {name} has incomplete index metadata")

    cap_manifest = candidate / "agents" / "_capabilities" / "manifest.json"
    if not cap_manifest.is_file():
        problems.append("agents: missing capability manifest")
    else:
        cap = read_json(cap_manifest)
        if cap.get("content_dropped") != 0:
            problems.append("agents: mined procedure content was dropped")
        active = {
            skill for _, row in rows_by_axis.get("skills", [])
            for skill in row.get("oracle_skills") or []
        }
        if set((cap.get("skill_homes") or {}).keys()) != active:
            problems.append("agents: capability homes do not cover every active skill")
    for path in sorted((candidate / "agents").glob("**/agents/*.toml")):
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            if not data.get("name") or not data.get("description"):
                problems.append(f"agents: invalid TOML identity {path}")
            for skill in (data.get("skills") or {}).get("config") or []:
                relative = skill.get("path")
                if not relative or not (path.parent.parent / relative).is_file():
                    problems.append(f"agents: TOML has missing skill path {path}: {relative}")
        except Exception as exc:  # noqa: BLE001 - validation boundary
            problems.append(f"agents: invalid TOML {path}: {exc}")

    if problems:
        raise ValueError("static validation failed:\n- " + "\n- ".join(problems))
    return {
        "ok": True,
        "candidate": str(candidate),
        "summary": summary,
        "content_hash": tree_hash(candidate),
    }


def write_statistics_report(candidate: Path) -> Path:
    lines = [
        "# APEX-Agents Evolving-Dataset Statistics",
        "",
        "The tables use the construction-table shape from Appendix B of the "
        "[EvoHarnessBench paper](https://arxiv.org/pdf/2609.04280).",
        "",
    ]
    labels = {"tools": "Tool", "skills": "Skill", "agents": "Agent"}
    for table, axis in enumerate(("tools", "skills", "agents"), 1):
        manifest = read_json(candidate / axis / "manifest.json")
        retained = int(manifest["retained_tasks"])
        lines.extend([
            f"## Table APEX.{table}: {labels[axis]} evolution by stage",
            "",
            f"| Domain | Stage | Train (adapt) | Test | Total | % | + new {axis} | Cumulative {axis} |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        total_train = total_test = 0
        for version in manifest["versions"]:
            train, test = int(version["train"]), int(version["test"])
            tasks = int(version["tasks"])
            total_train += train
            total_test += test
            domain = "APEX-Agents" if version["version"] == "v1" else ""
            stage = "H" + version["version"].removeprefix("v")
            lines.append(
                f"| {domain} | {stage} | {train} | {test} | {tasks} | "
                f"{100.0 * tasks / retained:.1f}% | "
                f"+{len(version[f'new_{axis}'])} | "
                f"{len(version[f'cumulative_{axis}'])} |"
            )
        final = len(manifest["versions"][-1][f"cumulative_{axis}"])
        lines.extend([
            f"|  | **Total** | **{total_train}** | **{total_test}** | "
            f"**{retained}** | **100%** | **—** | **{final}** |",
            "",
        ])
        excluded = manifest.get("excluded_tasks") or []
        if excluded:
            reasons = Counter(item.get("reason") or "unspecified" for item in excluded)
            reason_text = "; ".join(
                f"{reason} ({count})" for reason, count in sorted(reasons.items())
            )
            lines.append(
                f"Excluded: **{len(excluded)}** tasks. {reason_text}. "
                "See `reports/inspection.json` for IDs and evidence diagnostics.\n"
            )
    path = cfg.REPORTS / "statistics.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _attempt_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"


def build() -> dict[str, Any]:
    cfg.ATTEMPTS.mkdir(parents=True, exist_ok=True)
    attempt = cfg.ATTEMPTS / _attempt_id()
    first, second = attempt / "build_a", attempt / "build_b"
    try:
        build_into(first)
        validation_a = validate_candidate(first)
        build_into(second)
        validation_b = validate_candidate(second)
        hash_a, hash_b = tree_hash(first), tree_hash(second)
        if hash_a != hash_b:
            raise ValueError(f"determinism failure: {hash_a} != {hash_b}")
        result = {
            "ok": True,
            "attempt": str(attempt),
            "candidate": str(first),
            "replica": str(second),
            "content_hash": hash_a,
            "validation": validation_a,
            "replica_validation": validation_b,
        }
        write_json(attempt / "build_report.json", result)
        write_json(cfg.LATEST_CANDIDATE, {
            "candidate": str(first), "attempt": str(attempt), "content_hash": hash_a,
        })
        return result
    except Exception as exc:
        write_json(attempt / "build_report.json", {
            "ok": False, "attempt": str(attempt), "error": str(exc),
        })
        raise


def latest_candidate() -> Path:
    if not cfg.LATEST_CANDIDATE.is_file():
        raise FileNotFoundError("no candidate; run `python -m apex_builder build`")
    path = Path(read_json(cfg.LATEST_CANDIDATE)["candidate"])
    if not path.is_dir():
        raise FileNotFoundError(f"latest candidate no longer exists: {path}")
    return path


def validate(path: Path | None = None) -> dict[str, Any]:
    candidate = path or latest_candidate()
    result = validate_candidate(candidate)
    write_json(cfg.REPORTS / "validation.json", result)
    write_statistics_report(candidate)
    return result


def select_smoke_tasks(candidate: Path) -> list[dict[str, Any]]:
    records = {record.task_id: record for record in discover_tasks()}
    choices = []
    manifest = read_json(candidate / "tools" / "manifest.json")
    for version in manifest["versions"]:
        candidates = [records[task_id] for task_id in version["task_ids"]]
        selected = min(
            candidates,
            key=lambda record: (
                record.raw.get("gold_response_type") != "text",
                len(record.raw.get("rubric") or []),
                len(record.input_files),
                record.task_id,
            ),
        )
        choices.append({
            "version": version["version"],
            "task_id": selected.task_id,
            "gold_response_type": selected.raw.get("gold_response_type"),
            "rubric_criteria": len(selected.raw.get("rubric") or []),
            "input_files": len(selected.input_files),
        })
    return choices


def _promote(candidate: Path) -> None:
    for axis, target in cfg.FINAL_ROOTS.items():
        source = candidate / axis
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.promote-{os.getpid()}"
        backup = target.parent / f".{target.name}.previous-{os.getpid()}"
        for path in (temporary, backup):
            if path.exists():
                shutil.rmtree(path)
        shutil.copytree(source, temporary)
        if target.exists():
            target.rename(backup)
        try:
            temporary.rename(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)


def smoke(path: Path | None = None, *, promote: bool = True) -> dict[str, Any]:
    """Run a metadata/gold integrity preflight; this is not an agent reward run."""
    candidate = path or latest_candidate()
    validation = validate_candidate(candidate)
    records = {record.task_id: record for record in discover_tasks()}
    _, repo_paths = _repo_file_index()
    choices = select_smoke_tasks(candidate)
    root = cfg.REPORTS / "smoke" / _attempt_id()
    root.mkdir(parents=True)
    results = []
    for choice in choices:
        record = records[choice["task_id"]]
        checks = {
            "prompt_nonempty": bool(record.prompt.strip()),
            "rubric_nonempty": bool(record.raw.get("rubric")),
            "gold_response_nonempty": bool(str(record.raw.get("gold_response") or "").strip()),
            "world_apps_nonempty": bool(record.world.get("apps")),
            "tool_annotation_nonempty": bool(record.tools),
            "tool_evidence_complete": all(record.tool_evidence.get(tool) for tool in record.tools),
            "input_references_exist": all(path in repo_paths for path in record.input_files),
            "gold_references_exist": all(path in repo_paths for path in record.gold_files),
        }
        result = {**choice, "checks": checks, "passed": all(checks.values())}
        results.append(result)
        write_json(root / f"{choice['version']}-{choice['task_id']}.json", result)
    ok = len(results) == len(choices) and all(result["passed"] for result in results)
    report = {
        "ok": ok,
        "mode": "source-gold-preflight",
        "environment_execution": False,
        "agent_rewards": None,
        "limitation": (
            "The gated world archives were not downloaded and no Archipelago agent/judge run "
            "was executed. Passing means pinned source, gold, artifact references, application "
            "availability, and annotations are internally consistent—not that tasks earned 1.0."
        ),
        "candidate": str(candidate),
        "candidate_hash": validation["content_hash"],
        "selected": choices,
        "results": results,
        "promoted": False,
    }
    if ok and promote:
        _promote(candidate)
        report["promoted"] = True
        report["destinations"] = {axis: str(path) for axis, path in cfg.FINAL_ROOTS.items()}
        write_statistics_report(candidate)
    write_json(root / "summary.json", report)
    write_json(cfg.REPORTS / "smoke" / "latest.json", report)
    if not ok:
        raise RuntimeError(f"source/gold smoke preflight failed; see {root}")
    return report


def all_steps() -> dict[str, Any]:
    fetched = fetch(refresh=False)
    inspected = inspect_source(write=True)
    built = build()
    validated = validate(Path(built["candidate"]))
    smoked = smoke(Path(built["candidate"]), promote=True)
    return {
        "fetch": fetched,
        "inspect": {
            "discovered_tasks": inspected["discovered_tasks"],
            "tools_retained_tasks": inspected["tools_retained_tasks"],
            "skills_retained_tasks": inspected["skills_retained_tasks"],
            "agents_retained_tasks": inspected["agents_retained_tasks"],
        },
        "build": built,
        "validate": validated,
        "smoke": smoked,
    }
__EVOHARNESS_APEX_FILE__
```

## Skills · Step 1 mine the prompts

```bash
"$EVOLVE_APEX_PYTHON" - <<'PY'
import hashlib, json, os, pathlib, urllib.request

base = os.environ.get("EVOLVE_APEX_SERVICE", "https://educator-marrow-cultural.ngrok-free.dev").rstrip("/")
root = pathlib.Path(os.environ["EVOLVE_APEX_WORKDIR"])
eval_key = os.environ.get("EVAL_SERVICE_API_KEY", "").strip()
if not eval_key:
    raise RuntimeError("EVAL_SERVICE_API_KEY is required to download the closed catalog")
public_name = "skill-catalog.json"
expected = "de76f39ff5a32d3fcd48f9e1ff8e1062f2c577451c81fa12903bd06c77a9c714"
url = base + "/resources/evolve-benchmark/references/apex/v1/" + public_name
request = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {eval_key}",
    "ngrok-skip-browser-warning": "true",
})
with urllib.request.urlopen(request) as response:
    content = response.read()
actual = hashlib.sha256(content).hexdigest()
if actual != expected:
    raise RuntimeError(f"checksum mismatch for {public_name}: {actual}")
target = root / "data_dry_run" / "apex_builder" / "skills.json"
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(target.name + ".download")
temporary.write_bytes(content)
temporary.replace(target)

skills = json.loads((root / "data_dry_run/apex_builder/skills.json").read_text())
required = {"name", "title", "description", "tier", "domains", "patterns", "tools",
            "expected_outputs", "procedure", "notes", "see_also"}
names = [item["name"] for item in skills]
if len(skills) != 20 or len(names) != len(set(names)):
    raise RuntimeError("the pinned skill catalog must contain 20 unique procedural atoms")
if any(required - set(item) for item in skills):
    raise RuntimeError("invalid skill-catalog schema")
print("Skills catalog: 20 fixed procedural atoms ready for deterministic matching")
PY
mkdir -p "$EVOLVE_APEX_WORKDIR/evovle_skills/builder"
cat > "$EVOLVE_APEX_WORKDIR/evovle_skills/builder/sequencer.py" <<'__EVOHARNESS_APEX_FILE__'
"""Step 4 of the builder (Track A — static): sequence the active-tag map into
an ordered task stream, then group tasks into time steps.

Track A goal (no mutations): test whether the agent can author and reuse a
skill library from scratch, given only the stripped system prompt + empty
seed library + a stream of tasks.

Curriculum sequencing: versions introduce skills *incrementally*. v1
exposes only the highest-coverage skills; v2 adds a few more; etc. A task
lands in the first version whose cumulative active set covers all of the
task's tagged skills. The cumulative oracle skill universe S_1 ⊊ S_2 ⊊
... ⊊ S_K grows monotonically. This tests: "as new policy areas appear in
the task stream, does the agent author skills for them?"

An "active" skill is any oracle skill with ≥1 tagged task (set by the tagger).
The threshold used to size time steps (`--min-step-size`, default 15) is a
sequencer concern only.

Output is TWO views of the same underlying sequence:

  - `order.json`         : flat list [{t, task_id}, ...] — useful for
                           per-task evaluation runners.
  - `time_steps.json`    : batched view [{T, tasks: [...], skill_exposures}, ...]
                           — this is the *canonical* view. Mutations
                           (Track B, later) fire *between* time steps, not
                           between individual tasks.

Algorithm:

  1. Sort active skills by descending task coverage.
  2. Greedy stage building. For each version k:
       a. Start with S_k = S_{k-1}.
       b. Walk the sorted-skill list; each skill encountered is added to S_k.
       c. After each addition, count "new eligible" tasks: those whose
          tagged_skills ⊆ S_k AND tagged_skills ⊄ S_{k-1} AND not yet
          placed in any earlier version.
       d. Stop adding skills once #new_eligible ≥ min_step_size or no
          more skills remain.
       e. Place all new_eligible tasks into v_k (random order, seeded).
  3. If the trailing version has fewer than min_step_size tasks, fold it
     into the previous version (rare skills get introduced alongside their
     predecessors). Step sizes are not uniform; the last step may be larger.
  4. Within each version, split tasks into `train` (=adapt) and `test`
     subsets via a seeded shuffle and `--adapt-ratio` (default 0.3), with
     floors `--min-adapt-per-version` and `--min-test-per-version`.

Why min_step_size = 15? Each time step is the unit of evaluation (and, in
Track B, the unit between which mutations fire). Sizing it large enough
that the per-version pass rate is statistically meaningful.

What are `train` and `test` *for* in the skill-evolution context?
  - train (=adapt) tasks: the agent runs them, observes results, and is
    free to author/update its SKILL.md library. This is where library
    *evolution* happens for that version.
  - test tasks: the agent evaluates with the library it has accumulated
    up to and including this version's adapt phase; pass rate at v_k on
    `test` is the canonical per-version metric.

Usage:
    python sequencer.py --domain itsm --track static [--seed 0] \
                        [--min-step-size 15] [--adapt-ratio 0.3]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

from shared.paths import OUT_DIR, tags_dir


def streams_dir(domain: str) -> Path:
    return OUT_DIR / domain / "streams"


def load_tags(domain: str) -> tuple[dict, dict]:
    """Return (per_task, active_meta) loaded from the tagger output."""
    tags_root = tags_dir(domain)
    per_task = json.loads((tags_root / "task_to_skills.json").read_text())
    active_meta = json.loads((tags_root / "active_skills.json").read_text())
    return per_task, active_meta


def _split_adapt_test(
    task_ids: list[str],
    adapt_ratio: float,
    min_adapt: int,
    min_test: int,
    rng: random.Random,
) -> tuple[list[str], list[str]]:
    """Deterministic per-version split into (adapt, test) lists.

    Floors are enforced when the version is large enough to support both;
    otherwise we fall back to leaving ≥1 in each side when n ≥ 2.
    """
    tids = list(task_ids)
    rng.shuffle(tids)
    n = len(tids)

    n_adapt = round(n * adapt_ratio)

    if n >= min_adapt + min_test:
        n_adapt = max(n_adapt, min_adapt)
        n_adapt = min(n_adapt, n - min_test)
    else:
        if n >= 2:
            n_adapt = max(1, min(n - 1, n_adapt or 1))
        else:
            n_adapt = max(0, min(n, n_adapt))

    return tids[:n_adapt], tids[n_adapt:]


def sequence_curriculum(
    per_task: dict,
    active_skills: list[str],
    task_count_per_skill: dict[str, int],
    seed: int,
    min_step_size: int,
    adapt_ratio: float,
    min_adapt_per_version: int,
    min_test_per_version: int,
    split_map: dict[str, str] | None = None,
    target_ratio: float | None = None,
) -> dict:
    """Produce the curriculum stream.

    Returns a dict with `time_steps` already shaped — each step is a batch of
    tasks placed in the earliest version whose active set covers them. The
    cumulative active set S_1 ⊊ S_2 ⊊ ... ⊊ S_K grows monotonically.

    `split_map` (optional): a task_id -> "train"/"test" map supplied by the
    caller. When given, the per-version adapt/test split is taken VERBATIM from
    this map (and `adapt_ratio` / the per-version floors are ignored). This lets
    a caller fix the split up front — e.g. the enterprise hybrid composes train
    and test tasks from DISJOINT atom pools, so the split must follow the pool a
    task came from, not a fresh per-version shuffle. Default (None) preserves
    the original behaviour: a seeded per-version `adapt_ratio` split.

    `target_ratio` (optional, only with `split_map`): force every version to a
    train fraction == `target_ratio` by trimming the over-represented side. A
    version can only hit the ratio AND keep >= `min_step_size` tasks when
    min(train/r, test/(1-r)) >= min_step_size, so stages that can't are folded
    into a neighbour first (this lowers the version count K — acceptable when a
    uniform per-version split matters more than K). Default (None) keeps each
    version's natural pool ratio (only a both-splits-present fold is applied).
    """
    rng = random.Random(seed)

    # Sort active skills by descending task coverage (ties broken by name).
    ordered_skills = sorted(
        active_skills,
        key=lambda s: (-task_count_per_skill.get(s, 0), s),
    )

    # tagged_skills as a frozen set per task (only tasks with ≥1 tag).
    tasks: dict[str, frozenset[str]] = {
        tid: frozenset(info["tagged_skills"])
        for tid, info in per_task.items()
        if info["tagged_skills"]
    }
    if not tasks:
        raise ValueError("No tagged tasks available; sequencing impossible.")

    placed: set[str] = set()
    cumulative: set[str] = set()
    stages: list[dict] = []  # each: {version, skills_added, S_size, tasks (list[str])}

    skill_idx = 0
    while skill_idx < len(ordered_skills):
        prev_S = set(cumulative)
        skills_added: list[str] = []
        new_eligible: list[str] = []

        # Greedily add skills until we have enough new-eligible tasks or run out.
        while skill_idx < len(ordered_skills):
            s = ordered_skills[skill_idx]
            cumulative.add(s)
            skills_added.append(s)
            skill_idx += 1

            new_eligible = [
                tid for tid, tags in tasks.items()
                if tags.issubset(cumulative) and tid not in placed
            ]
            if len(new_eligible) >= min_step_size:
                break

        # Place these tasks in this stage.
        rng.shuffle(new_eligible)
        for tid in new_eligible:
            placed.add(tid)

        stages.append({
            "version_idx": len(stages) + 1,  # may be renumbered after fold
            "skills_added_this_stage": list(skills_added),
            "cumulative_active_set": sorted(cumulative),
            "S_size": len(cumulative),
            "prev_S_size": len(prev_S),
            "task_ids": new_eligible,
        })

    # Fold any tail stage(s) smaller than min_step_size into the previous one.
    # We may fold multiple in a row (unlikely but defensive).
    while len(stages) >= 2 and len(stages[-1]["task_ids"]) < min_step_size:
        tail = stages.pop()
        prev = stages[-1]
        prev["task_ids"].extend(tail["task_ids"])
        prev["skills_added_this_stage"].extend(tail["skills_added_this_stage"])
        prev["cumulative_active_set"] = tail["cumulative_active_set"]  # bigger set
        prev["S_size"] = tail["S_size"]

    # Folds for the caller-supplied (disjoint-pool) split. Both variants reuse
    # one merge primitive: any stage flagged "deficient" is merged into a
    # neighbour (the previous stage, or the next one for the very first stage)
    # until none remain or only one stage is left. A fine-grained stage can be
    # one-sided (e.g. a rare skill whose atoms all live in the train pool unlocks
    # only train composites) -- folding fixes that, possibly lowering K.
    if split_map is not None:
        def _merge_deficient(is_deficient) -> None:
            changed = True
            while changed and len(stages) >= 2:
                changed = False
                for idx, st in enumerate(stages):
                    if not is_deficient(st):
                        continue
                    if idx > 0:  # merge backward into the previous stage
                        prev = stages[idx - 1]
                        prev["task_ids"].extend(st["task_ids"])
                        prev["skills_added_this_stage"].extend(st["skills_added_this_stage"])
                        if st["S_size"] > prev["S_size"]:
                            prev["cumulative_active_set"] = st["cumulative_active_set"]
                            prev["S_size"] = st["S_size"]
                    else:  # first stage: merge forward into the next stage
                        nxt = stages[idx + 1]
                        nxt["task_ids"] = st["task_ids"] + nxt["task_ids"]
                        nxt["skills_added_this_stage"] = (
                            st["skills_added_this_stage"] + nxt["skills_added_this_stage"])
                    stages.pop(idx)
                    changed = True
                    break

        def _split_counts(st: dict) -> tuple[int, int]:
            a = sum(1 for t in st["task_ids"] if split_map.get(t) == "train")
            return a, len(st["task_ids"]) - a

        if target_ratio is not None:
            # Each version will be trimmed to train fraction == target_ratio; it
            # can keep >= min_step_size tasks only if its post-trim capacity
            # min(train/r, test/(1-r)) reaches the floor. Fold the ones that
            # can't (also handles one-sided stages, whose capacity is 0).
            def _ratio_capacity(st: dict) -> float:
                a, b = _split_counts(st)
                if a == 0 or b == 0:
                    return 0.0
                return min(a / target_ratio, b / (1.0 - target_ratio))
            _merge_deficient(lambda st: _ratio_capacity(st) < min_step_size)
        else:
            # Just guarantee BOTH splits are present in every emitted version.
            _merge_deficient(lambda st: 0 in _split_counts(st))

    # A second RNG for the per-version adapt/test split, derived from `seed`
    # but distinct from the curriculum's task-ordering RNG so the two
    # decisions don't get correlated by accident.
    split_rng = random.Random(seed * 9973 + 1)

    # Renumber & build time_steps in the canonical shape used downstream.
    time_steps: list[dict] = []
    flat_order: list[dict] = []  # t-indexed flat view
    for k, stage in enumerate(stages, start=1):
        # Per-version split into adapt/test.
        if split_map is None:
            adapt_ids, test_ids = _split_adapt_test(
                stage["task_ids"], adapt_ratio,
                min_adapt_per_version, min_test_per_version, split_rng,
            )
        else:
            # Honour the caller-supplied, leakage-free split verbatim: a task's
            # side is fixed by the (disjoint) pool it was composed from, not by
            # this per-version shuffle. We still shuffle for a stable-but-mixed
            # within-version order.
            stage_tids = list(stage["task_ids"])
            split_rng.shuffle(stage_tids)
            adapt_ids = [t for t in stage_tids if split_map.get(t) == "train"]
            test_ids = [t for t in stage_tids if split_map.get(t) == "test"]
            if target_ratio is not None:
                # Trim the over-represented side so train/(train+test) ==
                # target_ratio, keeping the largest balanced subset (the lists
                # are already shuffled, so a prefix is a uniform random sample).
                a, b = len(adapt_ids), len(test_ids)
                if a / target_ratio <= b / (1.0 - target_ratio):
                    keep_a, keep_b = a, round(a * (1.0 - target_ratio) / target_ratio)
                else:
                    keep_b, keep_a = b, round(b * target_ratio / (1.0 - target_ratio))
                adapt_ids = adapt_ids[:keep_a]
                test_ids = test_ids[:keep_b]
        # Place adapt first then test within this version, so the flat
        # order respects the "agent learns first, then is evaluated" cadence.
        ordered_tids = adapt_ids + test_ids

        task_records = []
        skill_expo: Counter[str] = Counter()
        primary_in_step: Counter[str] = Counter()
        for tid in ordered_tids:
            tags = sorted(per_task[tid]["tagged_skills"])
            for s in tags:
                skill_expo[s] += 1
            # primary = the highest-coverage tag this task has
            primary = max(tags, key=lambda x: (task_count_per_skill.get(x, 0), x))
            primary_in_step[primary] += 1
            split = "train" if tid in set(adapt_ids) else "test"
            task_records.append({
                "task_id": tid,
                "tagged_skills": tags,
                "split": split,
            })
            flat_order.append({
                "t": len(flat_order),
                "task_id": tid,
                "primary_skill": primary,
                "all_tags": tags,
                "split": split,
            })
        time_steps.append({
            "T": k,
            "task_range": [
                flat_order[len(flat_order) - len(task_records)]["t"],
                flat_order[-1]["t"],
            ],
            "n_tasks": len(task_records),
            "n_adapt": len(adapt_ids),
            "n_test": len(test_ids),
            "adapt_task_ids": adapt_ids,
            "test_task_ids": test_ids,
            "skills_introduced_this_version": sorted(set(stage["skills_added_this_stage"])),
            "cumulative_oracle_skills": stage["cumulative_active_set"],
            "skill_exposures": dict(sorted(skill_expo.items())),
            "primary_skill_mix": dict(sorted(primary_in_step.items())),
            "tasks": task_records,
        })

    # Aggregate stats.
    exposures: Counter[str] = Counter()
    primary_counter: Counter[str] = Counter()
    for step in time_steps:
        for s, c in step["skill_exposures"].items():
            exposures[s] += c
        for s, c in step["primary_skill_mix"].items():
            primary_counter[s] += c
    multi_tag = sum(1 for item in flat_order if len(item["all_tags"]) > 1)

    unplaced = set(tasks.keys()) - placed
    n_adapt_total = sum(step["n_adapt"] for step in time_steps)
    n_test_total = sum(step["n_test"] for step in time_steps)
    return {
        "order": flat_order,
        "time_steps": time_steps,
        "stats": {
            "n_tasks_in_stream": len(flat_order),
            "n_adapt_total": n_adapt_total,
            "n_test_total": n_test_total,
            "n_unplaced_tagged_tasks": len(unplaced),
            "exposures_per_active_skill": dict(exposures),
            "primary_assignment_per_skill": dict(primary_counter),
            "multi_tag_tasks": multi_tag,
            "seed": seed,
            "adapt_ratio": adapt_ratio,
        },
    }


def write_coverage_md(
    domain: str,
    track: str,
    stats: dict,
    time_steps: list[dict],
    out_path: Path,
) -> None:
    step_sizes = [step["n_tasks"] for step in time_steps]
    lines = [
        f"# Sequencer — domain `{domain}`, track `{track}`",
        "",
        f"- stream length: **{stats['n_tasks_in_stream']}** tasks "
        f"(train={stats['n_adapt_total']}, test={stats['n_test_total']}, "
        f"adapt_ratio={stats['adapt_ratio']})",
        f"- time steps: **{len(time_steps)}** (min step size ≥ {stats['min_step_size']}, "
        f"actual sizes: {step_sizes})",
        f"- multi-tagged tasks: {stats['multi_tag_tasks']} "
        f"({100*stats['multi_tag_tasks']/max(1,stats['n_tasks_in_stream']):.1f}%)",
        f"- seed: {stats['seed']}",
        "",
        "## Curriculum: skills introduced per version (cumulative grows monotonically)",
        "",
        "| version | #train | #test | #cum_skills | skills introduced this version |",
        "|---:|---:|---:|---:|---|",
    ]
    for step in time_steps:
        new = ", ".join(f"`{s}`" for s in step["skills_introduced_this_version"])
        lines.append(
            f"| v{step['T']} | {step['n_adapt']} | {step['n_test']} | "
            f"{len(step['cumulative_oracle_skills'])} | {new or '_(none)_'} |"
        )
    lines += [
        "",
        "## Per-skill exposure across the whole stream",
        "",
        "| oracle skill | exposures |",
        "|---|---:|",
    ]
    for s, n in sorted(stats["exposures_per_active_skill"].items()):
        lines.append(f"| `{s}` | {n} |")
    lines += [
        "",
        "## Per-time-step skill exposure (tasks within each batch)",
        "",
        "| T | n_tasks | skill exposures (tag counts within step) |",
        "|---:|---:|---|",
    ]
    for step in time_steps:
        mix = ", ".join(f"`{s}`={c}" for s, c in step["skill_exposures"].items())
        lines.append(f"| {step['T']} | {step['n_tasks']} | {mix} |")
    out_path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--domain", required=True,
        help="Domain to sequence. Any EOG domain (calendar/csm/drive/email/hr/"
             "hybrid/itsm/teams) or the pooled ALE domain (ale). Dataset-"
             "agnostic: only reads out/<domain>/tags/ written by the miner.",
    )
    ap.add_argument("--track", default="static", choices=["static"],
                    help="Track A (static) only for now; mutating track later.")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--min-step-size", type=int, default=15,
        help="Minimum tasks per time step. Default 15.",
    )
    ap.add_argument(
        "--adapt-ratio", type=float, default=0.3,
        help="Fraction of each version's tasks placed in the `train` (=adapt) "
             "split; the rest go to `test`. Default 0.3.",
    )
    ap.add_argument(
        "--min-adapt-per-version", type=int, default=1,
        help="Floor on #train tasks per version (when feasible). Default 1.",
    )
    ap.add_argument(
        "--min-test-per-version", type=int, default=1,
        help="Floor on #test tasks per version (when feasible). Default 1.",
    )
    ap.add_argument(
        "--min-versions", type=int, default=3,
        help="Refuse to emit a benchmark if the curriculum produces fewer "
             "than this many versions. Default 3. Domains with too few "
             "active skills or extremely skewed task-skill distributions "
             "(e.g. teams where 33/34 tasks land on a single skill) collapse "
             "to K=1 and offer no curriculum-evolution signal — they are "
             "dropped from the benchmark.",
    )
    args = ap.parse_args()

    if not 0.0 <= args.adapt_ratio <= 1.0:
        print(f"ERROR: --adapt-ratio must be in [0,1], got {args.adapt_ratio}")
        sys.exit(2)

    per_task, active_meta = load_tags(args.domain)
    active_skills = active_meta["active_skills"]
    task_count_per_skill = active_meta.get("task_count_per_skill", {})

    if not active_skills:
        print(f"ERROR: no active skills for {args.domain}. Run tagger first.")
        sys.exit(1)

    print(f"Sequencing {args.domain} (track={args.track}, seed={args.seed}, "
          f"adapt_ratio={args.adapt_ratio})")
    print(f"  active skills ({len(active_skills)}): {active_skills}")

    result = sequence_curriculum(
        per_task, active_skills, task_count_per_skill,
        args.seed, args.min_step_size,
        args.adapt_ratio,
        args.min_adapt_per_version,
        args.min_test_per_version,
    )
    time_steps = result["time_steps"]

    result["stats"]["min_step_size"] = args.min_step_size
    result["stats"]["n_time_steps"] = len(time_steps)
    result["stats"]["step_sizes"] = [step["n_tasks"] for step in time_steps]

    if len(time_steps) < args.min_versions:
        # Also wipe any stale stream artifacts from a prior run so downstream
        # tooling (analyze, export) can't accidentally pick them up.
        out_root = streams_dir(args.domain) / args.track
        if out_root.exists():
            import shutil
            shutil.rmtree(out_root)
        print(
            f"\nDROPPING domain={args.domain}: curriculum produced only "
            f"{len(time_steps)} version(s) (< --min-versions={args.min_versions}). "
            f"Step sizes: {result['stats']['step_sizes']}. "
            f"Active skills ({len(active_skills)}), task-count-per-skill: "
            f"{dict(sorted(task_count_per_skill.items(), key=lambda kv: -kv[1]))}. "
            f"This domain's task-skill distribution is too skewed or sparse "
            f"to support a multi-stage evolving-skills benchmark.",
            file=sys.stderr,
        )
        sys.exit(2)

    out_root = streams_dir(args.domain) / args.track
    out_root.mkdir(parents=True, exist_ok=True)

    # Compact order file (just the run order, agent-facing, flat view).
    order_compact = [{"t": item["t"], "task_id": item["task_id"]} for item in result["order"]]
    (out_root / "order.json").write_text(json.dumps(order_compact, indent=2))

    # Full order with tag metadata (for diagnostics).
    (out_root / "order_with_tags.json").write_text(json.dumps(result["order"], indent=2))

    # *** Canonical time-step view ***
    (out_root / "time_steps.json").write_text(json.dumps(time_steps, indent=2))

    manifest = {
        "domain": args.domain,
        "track": args.track,
        "n_tasks_in_stream": result["stats"]["n_tasks_in_stream"],
        "n_time_steps": len(time_steps),
        "min_step_size": args.min_step_size,
        "step_sizes": result["stats"]["step_sizes"],
        "adapt_ratio": args.adapt_ratio,
        "n_adapt_total": result["stats"]["n_adapt_total"],
        "n_test_total": result["stats"]["n_test_total"],
        "active_skills": active_skills,
        "seed": args.seed,
        "stats": result["stats"],
        "agent_setup": {
            "seed_skill_library": "empty",
            "stripped_system_prompt": "out/<domain>/oracle/stripped_system_prompt.txt",
            "skill_meta_actions_required": [
                "skill.list", "skill.read", "skill.write", "skill.update", "skill.delete",
            ],
            "track_pressure": "endogenous_only",  # no mutations in Track A
            "step_semantics": (
                "Each time step is a batch of K consecutive tasks drawn from the "
                "stratified-random interleave. Mutations (Track B, later) fire "
                "between time steps."
            ),
        },
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    write_coverage_md(
        args.domain, args.track, result["stats"], time_steps, out_root / "coverage.md",
    )

    print(f"\nstream length: {result['stats']['n_tasks_in_stream']} tasks "
          f"in {len(time_steps)} time steps "
          f"(min step size={args.min_step_size}, sizes={result['stats']['step_sizes']})")
    print(f"split per version (train/test): " +
          " ".join(f"v{step['T']}={step['n_adapt']}/{step['n_test']}" for step in time_steps))
    print(f"totals: train={result['stats']['n_adapt_total']}  "
          f"test={result['stats']['n_test_total']}  "
          f"(adapt_ratio={args.adapt_ratio})")
    print(f"per-skill exposures: {result['stats']['exposures_per_active_skill']}")
    print(f"multi-tagged: {result['stats']['multi_tag_tasks']}")
    print(f"wrote: {out_root}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
__EVOHARNESS_APEX_FILE__

mkdir -p "$EVOLVE_APEX_WORKDIR/evovle_skills/builder/shared"
cat > "$EVOLVE_APEX_WORKDIR/evovle_skills/builder/shared/paths.py" <<'__EVOHARNESS_APEX_FILE__'
"""Central paths for the benchmark-builder pipeline.

Single source of truth for input (EOG configs) and output (oracle, tags, etc.)
locations. Keep relative-ish so the package is movable.
"""
from __future__ import annotations

from pathlib import Path

# This file lives at builder/shared/paths.py
BUILDER_DIR = Path(__file__).resolve().parent.parent
EVOLVE_SKILLS_DIR = BUILDER_DIR.parent
MAS_ENV_DIR = EVOLVE_SKILLS_DIR.parent  # repository root

EOG_BENCHMARK_BREATH = MAS_ENV_DIR / "evolve_tools" / "benchmark_breath"

OUT_DIR = BUILDER_DIR / "out"


def eog_configs_dir(domain: str) -> Path:
    """EOG benchmark configs directory for a domain (e.g. itsm, csm, hr, teams)."""
    return EOG_BENCHMARK_BREATH / domain / "benchmark" / "configs"


def oracle_dir(domain: str) -> Path:
    return OUT_DIR / domain / "oracle"


def oracle_skills_dir(domain: str) -> Path:
    return oracle_dir(domain) / "skills"


def tags_dir(domain: str) -> Path:
    return OUT_DIR / domain / "tags"
__EVOHARNESS_APEX_FILE__
```

## Agents · Step 1 partition the tools

```bash
"$EVOLVE_APEX_PYTHON" - <<'PY'
import collections, json, os, pathlib

root = pathlib.Path(os.environ["EVOLVE_APEX_WORKDIR"])
catalog = json.loads((root / "data_dry_run/apex_builder/catalog.json").read_text())
owners = collections.defaultdict(list)
for item in catalog:
    owner = item.get("owner", "").strip()
    if not owner or not item["name"].startswith(owner + "."):
        raise RuntimeError(f"invalid owner for {item['name']}")
    owners[owner].append(item["name"])
owned = [tool for tools in owners.values() for tool in tools]
if len(owned) != len(set(owned)) or len(owned) != len(catalog):
    raise RuntimeError("operation ownership is not an exact partition")
print(f"Agents catalog: {len(owners)} application owners partition all MCP operations")
PY
```

## Expected output · build, validate, and preflight

```bash
mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder"
cat > "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/config.py" <<'__EVOHARNESS_APEX_FILE__'
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DRY_RUN = ROOT.parent
REPO_ROOT = DATA_DRY_RUN.parent

HF_REPO_ID = "mercor/apex-agents"
SOURCE_URL = "https://huggingface.co/datasets/mercor/apex-agents"
SOURCE_COMMIT = "92c86856cf1b11f9833a8a076b3a45a63afa3929"
ARCHIPELAGO_URL = "https://github.com/Mercor-Intelligence/archipelago.git"
ARCHIPELAGO_COMMIT = "bcacc2d1e99aa917bbe7f6f663c1551dc6ec7400"

SOURCE_FILES = {
    "metadata.json": "24584749c6307602944739262a2f3ddd59e8e0ecc9f151dc3459adf84ab934ea",
    "world_descriptions.json": "3836f308d122b7204a463ce43d23ec63fb27460f36f6bd5ac32d724183a3fdd8",
    "tasks_and_rubrics.json": "94a85655bb983bb8bfff693182026f05f1df41ff17b2cd4589dd42a071531cb6",
    "eval.yaml": "231993cb91ab18596fb6c24c9fbb33fba2975dcea605de5696cf4c4245ec8151",
}

CACHE = ROOT / "cache"
SEED = CACHE / "seed"
ARCHIPELAGO = CACHE / "archipelago"
REPORTS = ROOT / "reports"
ATTEMPTS = ROOT / "attempts"
LATEST_CANDIDATE = ROOT / "latest_candidate.json"
SOURCE_TOOL_ANNOTATIONS = ROOT / "source_oracle_tools.json"

FINAL_ROOTS = {
    "tools": DATA_DRY_RUN / "evovling_tools" / "apex_agents_validated",
    "skills": DATA_DRY_RUN / "evovling_skills" / "apex_agents_validated",
    "agents": DATA_DRY_RUN / "evovling_agents" / "apex_agents_validated",
}

DOMAIN = "apex_agents"
EXPECTED_TASKS = 480
EXPECTED_WORLDS = 33
RANDOM_SEED = 42
TARGET_VERSIONS = 5
MIN_VERSIONS = 3
MIN_TRAIN = 8
MIN_TEST = 18
ADAPT_RATIO = 0.30
SKILL_MIN_STEP_SIZE = 45

# Dataset-scale parameters for the existing EvoHarness frequency scheduler.
# These are constraints, not a second construction algorithm.
TOOL_MIN_NEW_TASKS = MIN_TRAIN + MIN_TEST
TOOL_MIN_GROWTH_FRAC = 0.12
TOOL_MAX_GROWTH_FRAC = 0.50
TOOL_INITIAL_ANCHOR_FRAC = 0.20

AGENT_MIN_NEW_TASKS = MIN_TRAIN + MIN_TEST
AGENT_MIN_GROWTH_FRAC = 0.12
AGENT_MAX_GROWTH_FRAC = 0.60
__EVOHARNESS_APEX_FILE__

mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder"
cat > "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/__init__.py" <<'__EVOHARNESS_APEX_FILE__'
"""Deterministic APEX-Agents to EvoHarnessBench construction adapter."""

__EVOHARNESS_APEX_FILE__

mkdir -p "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder"
cat > "$EVOLVE_APEX_WORKDIR/data_dry_run/apex_builder/__main__.py" <<'__EVOHARNESS_APEX_FILE__'
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .builder import all_steps, build, fetch, inspect_source, smoke, validate


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="python -m apex_builder",
        description="Build deterministic APEX-Agents evolving datasets.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch", help="Fetch pinned metadata and Archipelago.")
    fetch_parser.add_argument(
        "--refresh", action="store_true",
        help="Redownload gated metadata; requires HF_TOKEN with accepted dataset access.",
    )
    commands.add_parser("inspect", help="Annotate the 480 tasks and write evidence reports.")
    commands.add_parser("build", help="Build twice and require byte-identical candidates.")
    validate_parser = commands.add_parser("validate", help="Statically validate a candidate.")
    validate_parser.add_argument("--candidate", type=Path)
    smoke_parser = commands.add_parser(
        "smoke", help="Run pinned source/gold preflights for one task per tools stage."
    )
    smoke_parser.add_argument("--candidate", type=Path)
    smoke_parser.add_argument("--no-promote", action="store_true")
    commands.add_parser("all", help="Fetch, inspect, build, validate, smoke, and promote.")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "fetch":
            result = fetch(refresh=args.refresh)
        elif args.command == "inspect":
            result = inspect_source(write=True)
        elif args.command == "build":
            result = build()
        elif args.command == "validate":
            result = validate(args.candidate)
        elif args.command == "smoke":
            result = smoke(args.candidate, promote=not args.no_promote)
        else:
            result = all_steps()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"apex_builder: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
__EVOHARNESS_APEX_FILE__

cd "$EVOLVE_APEX_WORKDIR/data_dry_run"
"$EVOLVE_APEX_PYTHON" -m apex_builder all
```
