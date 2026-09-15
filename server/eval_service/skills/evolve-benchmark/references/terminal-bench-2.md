# Terminal-Bench 2 construction path

Use these blocks in order. They are generated from the same public source as the
rendered EvoHarnessBench construction page. Do not substitute another annotation,
scheduler, schema, or catalog.

- Terminal-Bench 2: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- Harbor `0.23.0`: `d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621`
- Python `3.12.12`, uv `0.12.13`, bashlex `0.18`, seed `42`
- Expected: 89 discovered; tools 86/50, skills 89/26, agents 86/17; five stages
- Expected tree hash: `7dd14ca35796ad7a91fdc08a825d53d298aed527cd67497317c78064fbef50a0`

The first block downloads the pinned Terminal-Bench 2 seed snapshot. The tools and
skills blocks later download the two versioned closed catalogs. Every executable
construction function is printed below; no Python builder is downloaded or imported.
The destination must be fresh, and promotion occurs only after two byte-identical builds
and five Harbor oracle rewards of `1.0`.

## Download seed data · Terminal-Bench 2

```bash
export EVOLVE_TB2_SERVICE="${EVOLVE_TB2_SERVICE:-https://educator-marrow-cultural.ngrok-free.dev}"
export EVOLVE_TB2_WORKDIR="${EVOLVE_TB2_WORKDIR:-$PWD/evoharnessbench-tb2}"

if [ -e "$EVOLVE_TB2_WORKDIR" ]; then
  echo "Refusing to overwrite existing path: $EVOLVE_TB2_WORKDIR" >&2
  exit 1
fi
source_dir="$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/cache/terminal-bench-2"
mkdir -p "$(dirname "$source_dir")"
git clone --filter=blob:none --no-checkout https://github.com/harbor-framework/terminal-bench-2.git "$source_dir"
git -C "$source_dir" fetch --force origin 2fd12b88aafdd04a52c298e3940bcb189f9766d6
git -C "$source_dir" checkout --detach 2fd12b88aafdd04a52c298e3940bcb189f9766d6
test "$(git -C "$source_dir" rev-parse HEAD)" = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"

printf 'Downloaded Terminal-Bench 2 seed data at %.8s into %s
'   "2fd12b88aafdd04a52c298e3940bcb189f9766d6" "$source_dir"
```

## Tools · Step 1 catalog

```bash
python - <<'PY'
import hashlib, json, os, pathlib, urllib.request

base = os.environ.get("EVOLVE_TB2_SERVICE", "https://educator-marrow-cultural.ngrok-free.dev").rstrip("/")
root = pathlib.Path(os.environ["EVOLVE_TB2_WORKDIR"])
eval_key = os.environ.get("EVAL_SERVICE_API_KEY", "").strip()
if not eval_key:
    raise RuntimeError("EVAL_SERVICE_API_KEY is required to download the closed catalog")
public_name = "software-catalog.json"
expected = "022cd1dc3dd686da146a2aca5d184901c07726b08eba5294e7d4edd1e33e437a"
url = base + "/resources/evolve-benchmark/references/tb2/v1/" + public_name
request = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {eval_key}",
    "ngrok-skip-browser-warning": "true",
})
with urllib.request.urlopen(request) as response:
    content = response.read()
actual = hashlib.sha256(content).hexdigest()
if actual != expected:
    raise RuntimeError(f"checksum mismatch for {public_name}: {actual}")
target = root / "data_dry_run" / "tb_builder" / "catalog.json"
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(target.name + ".download")
temporary.write_bytes(content)
temporary.replace(target)

catalog = json.loads((root / "data_dry_run/tb_builder/catalog.json").read_text())
required = {"name", "aliases", "binaries", "imports", "packages", "owner", "baseline"}
names = [item["name"] for item in catalog]
if len(names) != len(set(names)) or any(required - set(item) for item in catalog):
    raise RuntimeError("invalid closed software catalog")
aliases = {}
for item in catalog:
    for value in [item["name"], *item["aliases"], *item["binaries"],
                  *item["imports"], *item["packages"]]:
        key = value.casefold()
        if key in aliases and aliases[key] != item["name"]:
            raise RuntimeError(f"ambiguous software alias: {value}")
        aliases[key] = item["name"]
print(f"Tools catalog: {len(catalog)} canonical entries; aliases are closed and unambiguous")
PY
```

## Tools · Step 1 shared annotation implementation

```bash
mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat > "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/builder.py" <<'__EVOHARNESS_TB2_FILE__'
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
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
if cfg.PYDEPS.is_dir() and str(cfg.PYDEPS) not in sys.path:
    sys.path.insert(0, str(cfg.PYDEPS))

try:
    import tomllib  # type: ignore[attr-defined]
except ImportError:  # Python 3.10 builder bootstrap
    import tomli as tomllib  # type: ignore[no-redef]

from evolve_tools.builder.frequency_config import (  # noqa: E402
    ConstraintConfig,
    assign_frequency_stage,
    build_frequency_anchors_adaptive,
    count_tool_frequencies,
    rank_tools,
    validate_frequency_benchmark,
)

# Reuse the established evolving-skills sequencer verbatim.  Its legacy
# imports expect its builder directory on sys.path when used outside the CLI.
_SKILLS_BUILDER = cfg.REPO_ROOT / "evovle_skills" / "builder"
if str(_SKILLS_BUILDER) not in sys.path:
    sys.path.insert(0, str(_SKILLS_BUILDER))
from sequencer import sequence_curriculum as sequence_skill_curriculum  # noqa: E402


HOUSE_RULES = """# Terminal-Bench 2 shared house rules

- Work only inside the provided task environment and preserve supplied inputs.
- Read the complete task instruction before changing files.
- Produce every requested artifact at its exact path and in its exact format.
- Use deterministic commands and seeds whenever the task permits it.
- Validate the result with task-appropriate checks before finishing.
- Do not inspect or rely on held-out oracle solutions or verifier implementation.
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


def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
         capture: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess:
    print("+ " + " ".join(args), flush=True)
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, env=env, check=True,
        text=True, capture_output=capture, timeout=timeout,
    )


def _clone_pinned(url: str, commit: str, dest: Path) -> None:
    if not (dest / ".git").is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(dest)])
    _run(["git", "fetch", "--force", "origin", commit], cwd=dest)
    _run(["git", "checkout", "--detach", commit], cwd=dest)
    got = _run(["git", "rev-parse", "HEAD"], cwd=dest, capture=True).stdout.strip()
    if got != commit:
        raise RuntimeError(f"pin mismatch for {dest}: expected {commit}, got {got}")


def _uv_path() -> Path:
    return cfg.CACHE / "uv" / "bin" / "uv"


def setup_runtime() -> dict[str, Any]:
    cfg.CACHE.mkdir(parents=True, exist_ok=True)
    if not (cfg.HARBOR / ".git").is_dir():
        _clone_pinned(cfg.HARBOR_URL, cfg.HARBOR_COMMIT, cfg.HARBOR)
    else:
        got = _run(["git", "rev-parse", "HEAD"], cwd=cfg.HARBOR,
                   capture=True).stdout.strip()
        if got != cfg.HARBOR_COMMIT:
            _clone_pinned(cfg.HARBOR_URL, cfg.HARBOR_COMMIT, cfg.HARBOR)
    uv = _uv_path()
    if not uv.is_file():
        _run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--prefix", str(cfg.CACHE / "uv"), f"uv=={cfg.UV_VERSION}",
        ])
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(cfg.CACHE / "uv-cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(cfg.CACHE / "python")
    env["HARBOR_TELEMETRY"] = "0"
    py = cfg.RUNTIME / "bin" / "python"
    if not py.is_file():
        _run([str(uv), "python", "install", cfg.PYTHON_VERSION], env=env)
        _run([str(uv), "venv", "--python", cfg.PYTHON_VERSION, str(cfg.RUNTIME)], env=env)
    wanted_lock = {
        "python": cfg.PYTHON_VERSION, "uv": cfg.UV_VERSION,
        "harbor_version": cfg.HARBOR_VERSION, "harbor_commit": cfg.HARBOR_COMMIT,
        "bashlex": cfg.BASHLEX_VERSION, "tomli": cfg.TOMLI_VERSION,
    }
    lock_path = cfg.CACHE / "runtime-lock.json"
    installed_lock = read_json(lock_path) if lock_path.is_file() else None
    harbor_bin = cfg.RUNTIME / "bin" / "harbor"
    if installed_lock != wanted_lock or not harbor_bin.is_file():
        _run([
            str(uv), "pip", "install", "--python", str(py),
            f"bashlex=={cfg.BASHLEX_VERSION}", str(cfg.HARBOR),
        ], env=env)
        write_json(lock_path, wanted_lock)
    # A small dependency target lets the system Python run inspect/build too.
    cfg.PYDEPS.mkdir(parents=True, exist_ok=True)
    if not (cfg.PYDEPS / "bashlex").is_dir() or not (cfg.PYDEPS / "tomli").is_dir():
        _run([
            str(uv), "pip", "install", "--python", str(py),
            "--target", str(cfg.PYDEPS), f"bashlex=={cfg.BASHLEX_VERSION}",
            f"tomli=={cfg.TOMLI_VERSION}",
        ], env=env)
    if str(cfg.PYDEPS) not in sys.path:
        sys.path.insert(0, str(cfg.PYDEPS))
    versions = {
        "uv": _run([str(uv), "--version"], capture=True, env=env).stdout.strip(),
        "python": _run([str(py), "--version"], capture=True, env=env).stdout.strip(),
        "harbor": _run([
            str(py), "-c", "import importlib.metadata; print(importlib.metadata.version('harbor'))",
        ], capture=True, env=env).stdout.strip(),
        "source_commit": cfg.SOURCE_COMMIT,
        "harbor_commit": cfg.HARBOR_COMMIT,
        "bashlex": cfg.BASHLEX_VERSION,
    }
    write_json(cfg.REPORTS / "environment.json", versions)
    return versions


def fetch(*, runtime: bool = True) -> dict[str, Any]:
    _clone_pinned(cfg.SOURCE_URL, cfg.SOURCE_COMMIT, cfg.SOURCE)
    _clone_pinned(cfg.HARBOR_URL, cfg.HARBOR_COMMIT, cfg.HARBOR)
    result: dict[str, Any] = {
        "source": str(cfg.SOURCE), "source_commit": cfg.SOURCE_COMMIT,
        "harbor": str(cfg.HARBOR), "harbor_commit": cfg.HARBOR_COMMIT,
    }
    if runtime:
        result["runtime"] = setup_runtime()
    write_json(cfg.REPORTS / "fetch.json", result)
    return result


@dataclass(frozen=True)
class SourceLine:
    role: str
    path: str
    line: int
    text: str


@dataclass
class TaskRecord:
    task_id: str
    root: Path
    config: dict[str, Any]
    instruction: str
    tools: list[str]
    tool_evidence: dict[str, list[dict[str, Any]]]
    skills: list[str]
    skill_evidence: dict[str, list[dict[str, Any]]]

    @property
    def task(self) -> dict[str, Any]:
        return self.config.get("task") or {}

    @property
    def meta(self) -> dict[str, Any]:
        return self.config.get("metadata") or {}


def load_catalog() -> list[dict[str, Any]]:
    catalog = read_json(cfg.ROOT / "catalog.json")
    names: set[str] = set()
    signals: dict[tuple[str, str], str] = {}
    owners: set[str] = set()
    for item in catalog:
        name = item.get("name")
        owner = item.get("owner")
        if not name or name in names:
            raise ValueError(f"duplicate/empty catalog name: {name!r}")
        if not owner:
            raise ValueError(f"catalog entry {name!r} has no owner")
        names.add(name)
        owners.add(owner)
        for field in ("aliases", "binaries", "imports", "packages"):
            for raw in item.get(field) or []:
                key = (field, raw.casefold())
                previous = signals.get(key)
                if previous and previous != name:
                    raise ValueError(
                        f"ambiguous {field[:-1]} {raw!r}: {previous!r} and {name!r}"
                    )
                signals[key] = name
    if not owners:
        raise ValueError("catalog has no owner families")
    return catalog


def load_skill_catalog() -> list[dict[str, Any]]:
    skills = read_json(cfg.ROOT / "skills.json")
    names = [s.get("name") for s in skills]
    if any(not n for n in names) or len(names) != len(set(names)):
        raise ValueError("skill catalog contains empty or duplicate names")
    required = {"name", "title", "description", "tier", "terms", "tools",
                "procedure", "notes", "see_also"}
    known = set(names)
    for spec in skills:
        missing = required - set(spec)
        if missing:
            raise ValueError(f"skill {spec.get('name')!r} missing fields {sorted(missing)}")
        if spec["tier"] not in {"workflow", "capability"}:
            raise ValueError(f"skill {spec['name']!r} has invalid tier {spec['tier']!r}")
        if not str(spec["procedure"]).strip():
            raise ValueError(f"skill {spec['name']!r} has an empty procedure")
        unknown_links = set(spec.get("see_also") or []) - known
        if unknown_links:
            raise ValueError(
                f"skill {spec['name']!r} has unknown see_also links {sorted(unknown_links)}"
            )
    return skills


def _is_text(path: Path) -> bool:
    if path.suffix.lower() in {
        ".md", ".txt", ".sh", ".py", ".js", ".ts", ".r", ".c", ".h",
        ".cc", ".cpp", ".rs", ".go", ".ml", ".v", ".scm", ".sql",
        ".toml", ".yaml", ".yml", ".tex", ".proto", ".stan",
    }:
        return True
    return path.name in {"Dockerfile", "Makefile"}


def _read_lines(path: Path, role: str, task_root: Path) -> list[SourceLine]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rel = path.relative_to(task_root).as_posix()
    return [SourceLine(role, rel, i, line) for i, line in enumerate(text.splitlines(), 1)]


def _task_sources(root: Path, conf: dict[str, Any], instruction: str) -> list[SourceLine]:
    lines = [SourceLine("description", "task.toml", 1,
                        str((conf.get("task") or {}).get("description") or ""))]
    lines.extend(SourceLine("instruction", "instruction.md", i, line)
                 for i, line in enumerate(instruction.splitlines(), 1))
    solution = root / "solution"
    if solution.is_dir():
        for path in sorted(solution.rglob("*")):
            if path.is_file() and _is_text(path):
                lines.extend(_read_lines(path, "solution", root))
    dockerfile = root / "environment" / "Dockerfile"
    if dockerfile.is_file():
        lines.extend(_read_lines(dockerfile, "dockerfile", root))
    # Referenced helper scripts outside solution are parsed as oracle evidence only
    # when solve.sh names them; environment files alone remain non-accepting evidence.
    solve_text = "\n".join(x.text for x in lines if x.role == "solution")
    for path in sorted(root.rglob("*")):
        if not (path.is_file() and _is_text(path)):
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(("solution/", "tests/")) or rel in {
            "instruction.md", "task.toml", "environment/Dockerfile",
        }:
            continue
        if path.name in solve_text or rel in solve_text:
            lines.extend(_read_lines(path, "solution", root))
    return lines


def _boundary(raw: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(raw) +
                      r"(?![A-Za-z0-9_])", re.IGNORECASE)


def _shell_commands(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    commands: list[tuple[int, str]] = []
    try:
        import bashlex  # type: ignore

        def walk(node: Any) -> None:
            if getattr(node, "kind", None) == "command":
                parts = getattr(node, "parts", [])
                words = [p.word for p in parts if getattr(p, "kind", None) == "word"]
                if words:
                    line = text.count("\n", 0, node.pos[0]) + 1
                    commands.append((line, Path(words[0]).name))
            for attr in ("parts", "list", "command", "commands"):
                value = getattr(node, attr, None)
                if isinstance(value, list):
                    for child in value:
                        if hasattr(child, "kind"):
                            walk(child)
                elif hasattr(value, "kind"):
                    walk(value)

        for tree in bashlex.parse(text):
            walk(tree)
    except Exception:
        # Heredocs containing patch/program text can defeat a full shell parser;
        # this conservative fallback only accepts command-position tokens.
        for i, line in enumerate(text.splitlines(), 1):
            for segment in re.split(r"(?:^|&&|\|\||;|\|)", line):
                match = re.match(r"\s*(?:sudo\s+)?([A-Za-z0-9_./+:-]+)", segment)
                if match:
                    commands.append((i, Path(match.group(1)).name))
    return sorted(set(commands))


def _add_evidence(store: dict[str, list[dict[str, Any]]], name: str,
                  source: SourceLine, rule: str) -> None:
    item = {"source_path": source.path, "line": source.line, "rule": rule,
            "excerpt": source.text.strip()[:240]}
    if item not in store[name]:
        store[name].append(item)


def annotate_tools(root: Path, conf: dict[str, Any], instruction: str,
                   catalog: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    sources = _task_sources(root, conf, instruction)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_path_line = {(s.path, s.line): s for s in sources}
    shell_commands: list[tuple[str, int, str]] = []
    solution = root / "solution"
    if solution.is_dir():
        for shell in sorted(solution.rglob("*.sh")):
            rel = shell.relative_to(root).as_posix()
            shell_commands.extend((rel, line, command) for line, command in _shell_commands(shell))

    for item in catalog:
        name = item["name"]
        for source in sources:
            text = source.text
            for alias in item.get("aliases") or []:
                if _boundary(alias).search(text):
                    _add_evidence(evidence, name, source, f"alias:{alias}")
            if source.role in {"instruction", "description", "solution"}:
                for package in item.get("packages") or []:
                    package_context = re.search(
                        r"(?:pip|uv|npm|apt(?:-get)?|install|package|dependency)[^#\n]{0,100}" +
                        re.escape(package), text, re.IGNORECASE,
                    )
                    if package_context:
                        _add_evidence(evidence, name, source, f"package:{package}")
                for module in item.get("imports") or []:
                    patterns = [
                        rf"\b(?:from|import)\s+{re.escape(module)}(?:\b|\.)",
                        rf"\brequire\s*\(\s*['\"]{re.escape(module)}",
                        rf"\b(?:library|require)\s*\(\s*['\"]?{re.escape(module)}",
                    ]
                    if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                        _add_evidence(evidence, name, source, f"import:{module}")
        binaries = {b.casefold(): b for b in item.get("binaries") or []}
        for path, line, command in shell_commands:
            raw = binaries.get(command.casefold())
            if raw:
                source = by_path_line.get((path, line), SourceLine("solution", path, line, command))
                _add_evidence(evidence, name, source, f"shell-command:{raw}")

    # A solution that authors Python inside a heredoc still directly uses the
    # Python language even when the shell never launches the generated file.
    # Treat unmistakable Python syntax as language evidence, without promoting
    # arbitrary Docker/runtime packages.
    python_sources = [
        source for source in sources if source.role == "solution" and re.search(
            r"^\s*(?:#!.*python(?:3)?\b|from\s+[A-Za-z_][\w.]*\s+import\b|"
            r"import\s+[A-Za-z_][\w.]*\b|def\s+[A-Za-z_]\w*\s*\(|class\s+[A-Za-z_]\w*)",
            source.text,
        )
    ]
    for source in python_sources:
        _add_evidence(evidence, "python", source, "language-syntax:python")

    # Dockerfile matches are useful diagnostics, but never sufficient for acceptance.
    accepted: list[str] = []
    for item in catalog:
        name = item["name"]
        if any(e["source_path"] == "task.toml" or
               not e["source_path"].startswith("environment/Dockerfile")
               for e in evidence.get(name, [])):
            accepted.append(name)
    clean = {name: sorted(evidence[name], key=lambda e: (e["source_path"], e["line"], e["rule"]))
             for name in sorted(evidence)}
    return sorted(accepted), clean


def annotate_skills(task_id: str, conf: dict[str, Any], instruction: str,
                    tools: list[str], skill_catalog: list[dict[str, Any]]) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    task = conf.get("task") or {}
    meta = conf.get("metadata") or {}
    category = str(meta.get("category") or "").casefold()
    labels = [str(x) for x in (task.get("keywords") or []) + (meta.get("tags") or [])]
    label_text = " ".join(labels).casefold()
    description = str(task.get("description") or "")
    prompt_sources = [("task.toml", 1, description)] + [
        ("instruction.md", line, text)
        for line, text in enumerate(instruction.splitlines(), 1)
    ]
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for spec in skill_catalog:
        name = spec["name"]
        if category in {str(x).casefold() for x in spec.get("categories") or []}:
            evidence[name].append({"source_path": "task.toml", "line": 1,
                                   "rule": f"category:{category}", "excerpt": category})
        for term in spec.get("terms") or []:
            folded = term.casefold()
            if folded in label_text:
                evidence[name].append({"source_path": "task.toml", "line": 1,
                                       "rule": f"keyword:{term}", "excerpt": " ".join(labels)[:240]})
            else:
                for source_path, line, text in prompt_sources:
                    if _boundary(term).search(text):
                        evidence[name].append({
                            "source_path": source_path, "line": line,
                            "rule": f"prompt-pattern:{term}",
                            "excerpt": text.strip()[:240],
                        })
                        break
        for tool in sorted(set(tools) & set(spec.get("tools") or [])):
            evidence[name].append({"source_path": "tool-annotation", "line": 1,
                                   "rule": f"software-anchor:{tool}", "excerpt": tool})
    # Every official category has a deterministic procedural home; this is an
    # explicit catalog rule, not a learned fallback.
    if not evidence:
        raise ValueError(f"{task_id}: no procedural skill annotation")
    clean: dict[str, list[dict[str, Any]]] = {}
    for name in sorted(evidence):
        unique = {_json_dump(e): e for e in evidence[name]}
        clean[name] = sorted(unique.values(), key=lambda e: (e["source_path"], e["rule"]))
    return sorted(clean), clean


def _load_task(path: Path, catalog: list[dict[str, Any]],
               skills: list[dict[str, Any]]) -> TaskRecord:
    with (path / "task.toml").open("rb") as handle:
        conf = tomllib.load(handle)
    instruction = (path / "instruction.md").read_text(encoding="utf-8", errors="replace")
    tools, tool_evidence = annotate_tools(path, conf, instruction, catalog)
    skill_names, skill_evidence = annotate_skills(path.name, conf, instruction, tools, skills)
    return TaskRecord(path.name, path, conf, instruction, tools, tool_evidence,
                      skill_names, skill_evidence)


def discover_tasks() -> list[TaskRecord]:
    if not cfg.SOURCE.is_dir():
        raise FileNotFoundError(f"source missing: run `python -m tb_builder fetch` ({cfg.SOURCE})")
    try:
        import bashlex  # noqa: F401
        bashlex_version = importlib.metadata.version("bashlex")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise RuntimeError(
            "pinned bashlex is missing; run `python -m tb_builder fetch` first"
        ) from exc
    if bashlex_version != cfg.BASHLEX_VERSION:
        raise RuntimeError(
            f"bashlex pin mismatch: expected {cfg.BASHLEX_VERSION}, got {bashlex_version}"
        )
    catalog = load_catalog()
    skills = load_skill_catalog()
    roots = sorted(p for p in cfg.SOURCE.iterdir()
                   if p.is_dir() and (p / "task.toml").is_file() and
                   (p / "instruction.md").is_file())
    if len(roots) != cfg.EXPECTED_TASKS:
        raise ValueError(f"expected {cfg.EXPECTED_TASKS} tasks at pin, found {len(roots)}")
    tasks = [_load_task(path, catalog, skills) for path in roots]
    if len({t.task_id for t in tasks}) != len(tasks):
        raise ValueError("duplicate task IDs discovered")
    return tasks


def _frequency(items: Iterable[Iterable[str]]) -> dict[str, int]:
    return dict(sorted(count_tool_frequencies(items).items()))


def inspect_source(*, write: bool = True) -> dict[str, Any]:
    tasks = discover_tasks()
    accepted = [t for t in tasks if t.tools]
    excluded = [t for t in tasks if not t.tools]
    report = {
        "source_url": cfg.SOURCE_URL,
        "source_commit": cfg.SOURCE_COMMIT,
        "discovered_tasks": len(tasks),
        "tools_retained_tasks": len(accepted),
        "tools_excluded_tasks": [
            {"task_id": t.task_id, "reason": "no task-facing catalog software evidence"}
            for t in excluded
        ],
        "tool_frequencies": _frequency(t.tools for t in accepted),
        "skill_frequencies": _frequency(t.skills for t in tasks),
        "tool_annotations": {
            t.task_id: {"oracle_tools": t.tools, "evidence": t.tool_evidence}
            for t in tasks
        },
        "skill_annotations": {
            t.task_id: {"oracle_skills": t.skills, "evidence": t.skill_evidence}
            for t in tasks
        },
    }
    if write:
        write_json(cfg.REPORTS / "inspection.json", report)
    return report


__EVOHARNESS_TB2_FILE__
```

## Tools · Step 2 shared release implementation

```bash
mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat >> "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/builder.py" <<'__EVOHARNESS_TB2_FILE__'
def _stage_ok(ids: list[str], split_map: dict[str, str] | None) -> bool:
    if split_map is None:
        return len(ids) >= cfg.MIN_TRAIN + cfg.MIN_TEST
    return (
        sum(split_map.get(t) == "train" for t in ids) >= cfg.MIN_TRAIN and
        sum(split_map.get(t) == "test" for t in ids) >= cfg.MIN_TEST
    )


def _assign(task_caps: dict[str, set[str]], anchors: list[set[str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for task_id, capabilities in sorted(task_caps.items()):
        stage = assign_frequency_stage(capabilities, anchors)
        if stage is not None:
            result[task_id] = stage + 1
    return result


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


def _merge_split_thin_stages(task_caps: dict[str, set[str]],
                             anchors: list[set[str]],
                             split_map: dict[str, str]) -> list[set[str]]:
    """Use the established adjacent-stage fold used by the skill scheduler.

    Agent stages inherit the tools-track split.  A frequency anchor can
    therefore have enough tasks overall but too few on one inherited side.
    Removing that cumulative boundary folds its cohort into a neighbour while
    preserving frequency order and earliest-solvable assignment.
    """
    anchors = [set(a) for a in anchors]
    while len(anchors) >= 2:
        assignments = _assign(task_caps, anchors)
        bad = []
        for stage in range(1, len(anchors) + 1):
            ids = [tid for tid, value in assignments.items() if value == stage]
            if not _stage_ok(ids, split_map):
                bad.append(stage - 1)
        if not bad:
            return anchors
        index = bad[0]
        if index < len(anchors) - 1:
            anchors.pop(index)       # fold this stage forward
        else:
            anchors.pop(index - 1)   # fold the final stage backward
    return anchors


def _build_frequency_curriculum(
    task_caps: dict[str, set[str]], *, axis: str,
    split_map: dict[str, str] | None = None,
    target: int = cfg.TARGET_VERSIONS,
) -> tuple[list[set[str]], dict[str, int], dict[str, Any]]:
    if not task_caps or any(not caps for caps in task_caps.values()):
        raise ValueError("curriculum requires non-empty capability sets")
    ordered_ids = sorted(task_caps)
    constraints = _axis_constraints(axis)
    anchors, core_report = build_frequency_anchors_adaptive(
        [task_caps[tid] for tid in ordered_ids],
        target_num_versions=target,
        constraints=constraints,
    )
    if split_map is not None:
        anchors = _merge_split_thin_stages(task_caps, anchors, split_map)
    assignments = _assign(task_caps, anchors)
    if set(assignments) != set(task_caps):
        raise ValueError("not every annotated task is covered by final curriculum")
    if len(anchors) < cfg.MIN_VERSIONS:
        raise ValueError(f"curriculum produced {len(anchors)} versions; minimum is {cfg.MIN_VERSIONS}")
    for index, cumulative_set in enumerate(anchors, 1):
        ids = [tid for tid, stage in assignments.items() if stage == index]
        if not _stage_ok(ids, split_map):
            raise ValueError(f"v{index} violates split/task floors: {ids}")
        previous = anchors[index - 2] if index > 1 else set()
        delta = cumulative_set - previous
        if not delta:
            raise ValueError(f"v{index} has no new capability")
        for tid in ids:
            if not (task_caps[tid] & delta):
                raise ValueError(f"{tid} at v{index} uses no newly introduced capability")
    report = {
        "implementation": (
            "evolve_tools.builder.frequency_config."
            "build_frequency_anchors_adaptive"
        ),
        "constraints": constraints.to_dict(),
        "core_report": core_report,
        "schedule_after_split_folds": [len(a) for a in anchors],
    }
    return anchors, assignments, report


def build_curriculum(task_caps: dict[str, set[str]], *,
                     split_map: dict[str, str] | None = None,
                     target: int = cfg.TARGET_VERSIONS) -> tuple[list[set[str]], dict[str, int]]:
    """Compatibility wrapper around the canonical tools frequency scheduler."""
    anchors, assignments, _ = _build_frequency_curriculum(
        task_caps, axis="tools", split_map=split_map, target=target,
    )
    return anchors, assignments


def split_curriculum(assignments: dict[str, int]) -> dict[str, str]:
    result: dict[str, str] = {}
    for stage in sorted(set(assignments.values())):
        ids = sorted(t for t, v in assignments.items() if v == stage)
        rng = random.Random(cfg.SEED * 9973 + stage)
        rng.shuffle(ids)
        n_train = round(len(ids) * cfg.ADAPT_RATIO)
        n_train = max(cfg.MIN_TRAIN, n_train)
        n_train = min(n_train, len(ids) - cfg.MIN_TEST)
        for task_id in ids[:n_train]:
            result[task_id] = "train"
        for task_id in ids[n_train:]:
            result[task_id] = "test"
    return result


def _build_skill_curriculum(records: list[TaskRecord]) -> tuple[
    list[set[str]], dict[str, int], dict[str, str], dict[str, Any]
]:
    """Drive the established ALE/EOG skill sequencer with a TB2 tag adapter."""
    per_task = {
        task.task_id: {"tagged_skills": sorted(task.skills)} for task in records
    }
    frequency = Counter(
        skill for task in records for skill in set(task.skills)
    )
    result = sequence_skill_curriculum(
        per_task=per_task,
        active_skills=sorted(frequency),
        task_count_per_skill=dict(frequency),
        seed=cfg.SEED,
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
        missing = sorted(set(per_task) - set(assignments))
        raise ValueError(f"skill sequencer did not place tasks: {missing}")
    if len(anchors) < cfg.MIN_VERSIONS:
        raise ValueError(
            f"skill curriculum produced {len(anchors)} versions; minimum is {cfg.MIN_VERSIONS}"
        )
    report = {
        "implementation": "evovle_skills.builder.sequencer.sequence_curriculum",
        "min_step_size": cfg.SKILL_MIN_STEP_SIZE,
        "stats": result["stats"],
        "validation": {
            "passed": all(
                step["n_adapt"] >= cfg.MIN_TRAIN
                and step["n_test"] >= cfg.MIN_TEST
                and step["n_tasks"] >= cfg.SKILL_MIN_STEP_SIZE
                for step in result["time_steps"]
            ),
            "all_tasks_placed": set(assignments) == set(per_task),
            "earliest_solving_assignment": True,
        },
    }
    return anchors, assignments, splits, report


__EVOHARNESS_TB2_FILE__

mkdir -p "$EVOLVE_TB2_WORKDIR/evolve_tools/builder"
cat > "$EVOLVE_TB2_WORKDIR/evolve_tools/builder/frequency_config.py" <<'__EVOHARNESS_TB2_FILE__'
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
__EVOHARNESS_TB2_FILE__
```

## Tools · Steps 3 and 4 shared accumulation and writer

```bash
mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat >> "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/builder.py" <<'__EVOHARNESS_TB2_FILE__'
def _input_files(task: TaskRecord) -> list[str]:
    excluded_prefixes = ("solution/", "tests/")
    excluded = {"task.toml", "instruction.md", "environment/Dockerfile"}
    files = []
    for path in task.root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(task.root)
        name = relative.as_posix()
        if name in excluded or name.startswith(excluded_prefixes):
            continue
        # Harbor/pytest smoke runs may compile helpers inside the cached source
        # checkout.  Those runtime artifacts are not seed inputs and would make
        # a later build depend on whether the checkout had already been tested.
        if (
            "__pycache__" in relative.parts
            or ".pytest_cache" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        files.append(name)
    return sorted(files)


def _common(task: TaskRecord) -> dict[str, Any]:
    raw_task = task.task
    meta = task.meta
    env = task.config.get("environment") or {}
    agent = task.config.get("agent") or {}
    verifier = task.config.get("verifier") or {}
    title = str(raw_task.get("name") or task.task_id).split("/")[-1]
    category = str(meta.get("category") or "uncategorized")
    return {
        "title": title,
        "summary": str(raw_task.get("description") or ""),
        "category": category,
        "subdomain": category,
        "task_split": "official",
        "agent_must_do": [],
        "input_files": _input_files(task),
        "taxonomy": {"category": category, "keywords": raw_task.get("keywords") or []},
        "source_repo_path": task.task_id,
        "repository_url": cfg.SOURCE_URL.removesuffix(".git"),
        "source_commit": cfg.SOURCE_COMMIT,
        "difficulty": meta.get("difficulty"),
        "tags": sorted(set(str(x) for x in (meta.get("tags") or []) +
                           (raw_task.get("keywords") or []))),
        "artifacts": task.config.get("artifacts") or [],
        "resource_limits": {
            "agent_timeout_sec": agent.get("timeout_sec"),
            "verifier_timeout_sec": verifier.get("timeout_sec"),
            "build_timeout_sec": env.get("build_timeout_sec"),
            "cpus": env.get("cpus"), "memory_mb": env.get("memory_mb"),
            "storage_mb": env.get("storage_mb"), "gpus": env.get("gpus"),
            "allow_internet": env.get("allow_internet"),
            "docker_image": env.get("docker_image"),
            "mcp_servers": env.get("mcp_servers") or [],
        },
        "metadata": meta,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json_dump(row) + "\n" for row in rows), encoding="utf-8")


def _stage_manifest(task_caps: dict[str, set[str]], anchors: list[set[str]],
                    assignments: dict[str, int], splits: dict[str, str], noun: str) -> list[dict[str, Any]]:
    out = []
    previous: set[str] = set()
    for stage, cumulative in enumerate(anchors, 1):
        ids = sorted(t for t, v in assignments.items() if v == stage)
        out.append({
            "version": f"v{stage}", f"new_{noun}": sorted(cumulative - previous),
            f"cumulative_{noun}": sorted(cumulative),
            "tasks": len(ids), "train": sum(splits[t] == "train" for t in ids),
            "test": sum(splits[t] == "test" for t in ids),
            "task_ids": ids,
        })
        previous = cumulative
    return out


def _tools_rows(tasks: dict[str, TaskRecord], anchors: list[set[str]],
                assignments: dict[str, int], splits: dict[str, str]) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for tid, stage in sorted(assignments.items()):
        task = tasks[tid]
        row = {
            "domain": cfg.DOMAIN, "version": f"v{stage}", "split": splits[tid],
            "task_id": tid, "oracle_tools": task.tools,
            "cummulative_tools": sorted(anchors[stage - 1]),
            "task_prompt": task.instruction,
            "software": sorted(anchors[stage - 1]),
            "annotation_evidence": task.tool_evidence,
            **_common(task),
        }
        rows[stage].append(row)
    return rows


def _skills_rows(tasks: dict[str, TaskRecord], anchors: list[set[str]],
                 assignments: dict[str, int], splits: dict[str, str]) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for tid, stage in sorted(assignments.items()):
        task = tasks[tid]
        active = sorted(anchors[stage - 1])
        common = _common(task)
        row = {
            "domain": cfg.DOMAIN, "version": f"v{stage}", "split": splits[tid],
            "task_id": tid, "oracle_skills": task.skills,
            "cummulative_oracle_skills": active,
            # As in the existing ALE export, oracle skill labels and the
            # cumulative answer-key set are metadata, never prompt-injected.
            "system_prompt": HOUSE_RULES,
            "user_prompt": task.instruction,
            "software": task.tools,
            "agent_must_do": common.pop("agent_must_do"),
            "category": common.pop("category"), "subdomain": common.pop("subdomain"),
            "task_split": common.pop("task_split"), "input_files": common.pop("input_files"),
            "source_repo_path": common.pop("source_repo_path"),
            "repository_url": common.pop("repository_url"),
            "source_commit": common.pop("source_commit"),
            "difficulty": common.pop("difficulty"), "tags": common.pop("tags"),
            "artifacts": common.pop("artifacts"), "resource_limits": common.pop("resource_limits"),
            "metadata": common.pop("metadata"),
            "evaluation": {"source": "Terminal-Bench 2 verifier", "held_out": True},
            "patcher_prompts": [], "prompt_suffix": "",
            "annotation_evidence": task.skill_evidence,
        }
        rows[stage].append(row)
    return rows


def _owner_maps(catalog: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    owner_of = {item["name"]: item["owner"] for item in catalog}
    owned: dict[str, list[str]] = defaultdict(list)
    for tool, owner in sorted(owner_of.items()):
        owned[owner].append(tool)
    return owner_of, dict(sorted(owned.items()))


def _agent_system(cumulative: list[str], owner_tools: dict[str, list[str]]) -> str:
    roster = "\n".join(
        f"- {owner}: {', '.join(owner_tools[owner])}" for owner in cumulative
    )
    return HOUSE_RULES.rstrip() + "\n\n# Specialist roster\n" + roster + (
        "\n\nDelegate work to the smallest set of specialists whose owned software "
        "covers the task, then integrate and validate their results."
    )


def _agents_rows(tasks: dict[str, TaskRecord], anchors: list[set[str]],
                 assignments: dict[str, int], splits: dict[str, str],
                 owner_of: dict[str, str], owner_tools: dict[str, list[str]]) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for tid, stage in sorted(assignments.items()):
        task = tasks[tid]
        agents = sorted({owner_of[t] for t in task.tools})
        cumulative_agents = sorted(anchors[stage - 1])
        allowed = sorted({tool for owner in cumulative_agents for tool in owner_tools[owner]})
        common = _common(task)
        row = {
            "domain": cfg.DOMAIN, "version": f"v{stage}", "split": splits[tid],
            "task_id": tid, "oracle_agents": agents,
            "cumulative_agents": cumulative_agents, "oracle_skills": task.skills,
            "system_prompt": _agent_system(cumulative_agents, owner_tools),
            "task_prompt": task.instruction, "user_prompt": task.instruction,
            "software": allowed, "oracle_tools": task.tools,
            "cummulative_tools": allowed,
            "annotation_evidence": task.tool_evidence,
            **common,
        }
        rows[stage].append(row)
    return rows


def _write_rows(root: Path, rows: dict[int, list[dict[str, Any]]]) -> None:
    for stage, stage_rows in sorted(rows.items()):
        vroot = root / f"v{stage}"
        for split in ("train", "test"):
            selected = sorted((r for r in stage_rows if r["split"] == split),
                              key=lambda r: r["task_id"])
            _write_jsonl(vroot / f"{split}.jsonl", selected)


def _skill_content_block(spec: dict[str, Any], *, heading: int = 2) -> str:
    marker = "#" * heading
    return (
        f"{marker} {spec['title']}\n\n"
        f"*{spec['description']}*\n\n"
        f"{spec['procedure'].strip()}\n\n"
        f"**Notes:** {spec['notes'].strip()}\n"
    )


def _skill_md(spec: dict[str, Any], evidence: dict[str, list[dict[str, Any]]],
              total_tasks: int) -> str:
    samples = "\n".join(f"- `{task_id}`" for task_id in sorted(evidence)[:5])
    see_also = "\n".join(f"- `{name}`" for name in spec.get("see_also") or [])
    tier = (
        "Cross-cutting workflow skill."
        if spec["tier"] == "workflow"
        else "Software-anchored capability skill."
    )
    return (
        "---\n"
        f"name: {spec['name']}\n"
        f"description: {json.dumps(spec['description'], ensure_ascii=False)}\n"
        "---\n\n"
        f"# {spec['title']}\n\n"
        "<!-- mined-by-tb2-builder (deterministic; no LLM) -->\n"
        "## Overview\n\n"
        f"{tier} Mined with the same closed-catalog lexical and software-anchor "
        "method used by the ALE benchmark adapter.\n\n"
        "## When to use\n\n"
        f"{spec['description']} Observed in **{len(evidence)}/{total_tasks}** "
        "Terminal-Bench 2 tasks.\n\n"
        "## Procedure\n\n"
        f"{spec['procedure'].strip()}\n\n"
        "## Notes\n\n"
        f"- {spec['notes'].strip()}\n\n"
        "## See also\n\n"
        f"{see_also or '- _none_'}\n\n"
        "## Evidence (mined)\n\n"
        f"- tagged tasks: **{len(evidence)}**\n"
        f"- representative tasks:\n{samples}\n"
        "<!-- end-mined -->\n"
    )


def _write_skill_library(root: Path, tasks: dict[str, TaskRecord]) -> None:
    evidence_by_skill: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    for task in tasks.values():
        for name, evidence in task.skill_evidence.items():
            evidence_by_skill[name][task.task_id] = evidence
    oracle = root / "_oracle"
    (oracle / "stripped_system_prompt.txt").parent.mkdir(parents=True, exist_ok=True)
    (oracle / "stripped_system_prompt.txt").write_text(HOUSE_RULES, encoding="utf-8")
    active = sorted(evidence_by_skill)
    write_json(oracle / "active_skills.json", {
        "domain": cfg.DOMAIN,
        "method": (
            "ALE-compatible deterministic closed-catalog mining: lexical "
            "signature plus accepted-software anchor; no LLM"
        ),
        "active_skills": active,
        "task_count_per_skill": {
            name: len(evidence_by_skill[name]) for name in active
        },
    })
    for spec in load_skill_catalog():
        name = spec["name"]
        if name not in evidence_by_skill:
            continue
        skill_root = oracle / "skills" / name
        skill_root.mkdir(parents=True, exist_ok=True)
        (skill_root / "SKILL.md").write_text(
            _skill_md(spec, evidence_by_skill[name], len(tasks)), encoding="utf-8"
        )
        write_json(skill_root / "index.json", {
            "name": name, "title": spec["title"], "description": spec["description"],
            "tier": spec["tier"],
            "signature": {
                "categories": spec.get("categories") or [],
                "terms": spec.get("terms") or [],
                "software_any": spec.get("tools") or [],
            },
            "task_count": len(evidence_by_skill[name]),
            "tagged_task_ids": sorted(evidence_by_skill[name]),
            "matching_evidence": evidence_by_skill[name],
        })


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _agent_skill_md(owner: str, tools: list[str],
                    capability_specs: list[dict[str, Any]]) -> str:
    description = f"Terminal-Bench specialist that owns: {', '.join(tools)}."
    body = (
        "---\n" + f"name: {owner}\n" +
        f"description: {json.dumps(description)}\n---\n\n" +
        f"# {owner.replace('-', ' ').title()}\n\n"
        "## Scope\n\n"
        f"Own and execute task work requiring {', '.join(f'`{x}`' for x in tools)}. "
        "Coordinate through the lead orchestrator when another software family is needed.\n\n"
        "## Operating procedure\n\n"
        "Read the task brief, inspect the current files, perform only the delegated work, "
        "validate the relevant artifact, and return exact paths and results to the orchestrator.\n"
    )
    if capability_specs:
        body += (
            "\n## Capabilities\n\n"
            "Reusable procedures mined by the skills track and re-homed by "
            "software ownership, following the ALE agent builder.\n\n"
            + "\n\n".join(
                _skill_content_block(spec, heading=3) for spec in capability_specs
            )
        )
    return body


def _agent_toml(owner: str, tools: list[str]) -> str:
    desc = f"Route tasks requiring {', '.join(tools)} to this specialist."
    instructions = (
        f"You are the {owner} specialist. Read agent_skills/{owner}/SKILL.md and "
        "agent_skills/_shared/house_rules.md. Your owned software is: " +
        ", ".join(tools) + ". Do only the delegated portion, validate it, and report "
        "artifact paths and results to the orchestrator."
    )
    return "\n".join([
        f"name = {_toml_string(owner)}", f"description = {_toml_string(desc)}",
        f"developer_instructions = {_toml_string(instructions)}", "",
        "[[skills.config]]", f"path = {_toml_string(f'agent_skills/{owner}/SKILL.md')}",
        "enabled = true", "", "[tools]", "exec_command = true", "shell_tool = true",
        "apply_patch = true", "",
    ])


def _materialize_agents(root: Path, anchors: list[set[str]], owner_tools: dict[str, list[str]],
                        manifest: list[dict[str, Any]], active_skills: set[str]) -> None:
    active_owners = sorted(anchors[-1])
    specs = [
        spec for spec in load_skill_catalog() if spec["name"] in active_skills
    ]
    tool_owner = {
        tool: owner for owner, tools in owner_tools.items() for tool in tools
    }
    owner_specs: dict[str, list[dict[str, Any]]] = {
        owner: [] for owner in active_owners
    }
    shared_specs: list[dict[str, Any]] = []
    homes: dict[str, list[str]] = {}
    for spec in specs:
        anchored_owners = sorted({
            tool_owner[tool] for tool in spec.get("tools") or []
            if tool in tool_owner
        })
        if spec["tier"] == "capability" and anchored_owners:
            homes[spec["name"]] = anchored_owners
            for owner in anchored_owners:
                owner_specs[owner].append(spec)
        else:
            homes[spec["name"]] = ["_shared"]
            shared_specs.append(spec)

    cap = root / "_capabilities"
    shared = cap / "_shared" / "house_rules.md"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared_text = HOUSE_RULES.rstrip() + (
        "\n\n# Cross-cutting skills\n\n" + "\n\n".join(
            _skill_content_block(spec, heading=2) for spec in shared_specs
        ) if shared_specs else ""
    ) + "\n"
    shared.write_text(shared_text, encoding="utf-8")
    family_text: dict[str, str] = {}
    for owner in active_owners:
        path = cap / owner / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        family_text[owner] = _agent_skill_md(
            owner, owner_tools[owner], owner_specs[owner]
        )
        path.write_text(family_text[owner], encoding="utf-8")

    # ALE's hard no-content-dropped invariant: every mined active procedure is
    # present in its software-family capability or in the shared baseline.
    for spec in specs:
        haystacks = [shared_text] if homes[spec["name"]] == ["_shared"] else [
            family_text[owner] for owner in homes[spec["name"]]
        ]
        if not any(spec["procedure"].strip() in text for text in haystacks):
            raise ValueError(f"agent capability generation dropped {spec['name']}")
    write_json(cap / "manifest.json", {
        "domain": cfg.DOMAIN,
        "method": "ALE-style software-owner re-homing of mined skill procedures",
        "active_skills": sorted(active_skills),
        "skill_homes": homes,
        "content_dropped": 0,
    })

    def pool(dest: Path, owners: list[str], pool_manifest: dict[str, Any]) -> None:
        shared_dest = dest / "agent_skills" / "_shared" / "house_rules.md"
        shared_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shared, shared_dest)
        entries = []
        for owner in owners:
            skill_dest = dest / "agent_skills" / owner / "SKILL.md"
            skill_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cap / owner / "SKILL.md", skill_dest)
            toml_path = dest / "agents" / f"{owner}.toml"
            toml_path.parent.mkdir(parents=True, exist_ok=True)
            toml_path.write_text(_agent_toml(owner, owner_tools[owner]), encoding="utf-8")
            entries.append({"name": owner, "owned_software": owner_tools[owner],
                            "file": f"agents/{owner}.toml",
                            "skill": f"agent_skills/{owner}/SKILL.md"})
        write_json(dest / "manifest.json", {**pool_manifest, "agents": entries})

    pool(root / "_agents", active_owners,
         {"domain": cfg.DOMAIN, "kind": "full-roster", "cumulative_agents": active_owners})
    for index, cumulative in enumerate(anchors, 1):
        pool(root / f"v{index}", sorted(cumulative), manifest[index - 1])


def _axis_manifest(axis: str, task_caps: dict[str, set[str]], anchors: list[set[str]],
                   assignments: dict[str, int], splits: dict[str, str], noun: str,
                   excluded: list[dict[str, str]],
                   construction: dict[str, Any]) -> dict[str, Any]:
    return {
        "domain": cfg.DOMAIN, "axis": axis, "seed": cfg.SEED,
        "source_url": cfg.SOURCE_URL.removesuffix(".git"),
        "source_commit": cfg.SOURCE_COMMIT,
        "discovered_tasks": cfg.EXPECTED_TASKS, "retained_tasks": len(task_caps),
        "excluded_tasks": excluded, "target_versions": cfg.TARGET_VERSIONS,
        "emitted_versions": len(anchors), "min_train": cfg.MIN_TRAIN,
        "min_test": cfg.MIN_TEST, "frequency": _frequency(task_caps.values()),
        "ranked_capabilities": rank_tools([task_caps[t] for t in sorted(task_caps)]),
        "construction": construction,
        "versions": _stage_manifest(task_caps, anchors, assignments, splits, noun),
    }


def _frequency_validation(task_caps: dict[str, set[str]],
                          anchors: list[set[str]],
                          assignments: dict[str, int],
                          splits: dict[str, str], axis: str) -> dict[str, Any]:
    ids = sorted(task_caps)
    adapt_counts = []
    test_counts = []
    for stage in range(1, len(anchors) + 1):
        stage_ids = [tid for tid in ids if assignments[tid] == stage]
        adapt_counts.append(sum(splits[tid] == "train" for tid in stage_ids))
        test_counts.append(sum(splits[tid] == "test" for tid in stage_ids))
    return validate_frequency_benchmark(
        anchors=anchors,
        task_tool_sets=[task_caps[tid] for tid in ids],
        stage_assignments=[assignments[tid] - 1 for tid in ids],
        adapt_counts=adapt_counts,
        test_counts=test_counts,
        constraints=_axis_constraints(axis),
    )


def write_statistics_report(candidate: Path) -> Path:
    """Render the paper's B.1/B.2/B.3 construction table shape."""
    lines = [
        "# Terminal-Bench 2 Evolving-Dataset Statistics",
        "",
        "These tables follow Tables B.1, B.2, and B.3 of the "
        "[EvoHarnessBench paper](https://arxiv.org/pdf/2609.04280). "
        "Percentages are relative to the retained sample for each axis; "
        "train (adaptation) and test splits are disjoint.",
        "",
    ]
    titles = {"tools": "Tool", "skills": "Skill", "agents": "Agent"}
    for table, axis in enumerate(("tools", "skills", "agents"), 1):
        manifest = read_json(candidate / axis / "manifest.json")
        retained = int(manifest["retained_tasks"])
        noun = axis
        lines.extend([
            f"## Table TB2.{table}: {titles[axis]}-evolution statistics by stage",
            "",
            f"| Domain | Stage | Train (adapt) | Test | Total tasks | % of sample | + new {noun} | Cumulative {noun} |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        total_train = total_test = 0
        for version in manifest["versions"]:
            train = int(version["train"])
            test = int(version["test"])
            tasks = int(version["tasks"])
            total_train += train
            total_test += test
            new_count = len(version[f"new_{noun}"])
            cumulative_count = len(version[f"cumulative_{noun}"])
            domain = "Terminal-Bench 2" if version["version"] == "v1" else ""
            stage = "H" + version["version"].removeprefix("v")
            lines.append(
                f"| {domain} | {stage} | {train} | {test} | {tasks} | "
                f"{100.0 * tasks / retained:.1f}% | +{new_count} | "
                f"{cumulative_count} |"
            )
        final_caps = len(manifest["versions"][-1][f"cumulative_{noun}"])
        lines.extend([
            f"|  | **Total** | **{total_train}** | **{total_test}** | "
            f"**{retained}** | **100%** | **—** | **{final_caps}** |",
            "",
        ])
        excluded = manifest.get("excluded_tasks") or []
        if excluded:
            task_ids = ", ".join(f"`{item['task_id']}`" for item in excluded)
            lines.extend([
                f"Excluded from this axis because no task-facing catalog "
                f"software evidence was found: {task_ids}.",
                "",
            ])
    path = cfg.REPORTS / "statistics.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def build_into(dest: Path) -> dict[str, Any]:
    if dest.exists():
        raise FileExistsError(f"fresh build destination already exists: {dest}")
    dest.mkdir(parents=True)
    records = discover_tasks()
    tasks = {t.task_id: t for t in records}
    tool_tasks = {t.task_id: set(t.tools) for t in records if t.tools}
    excluded = [{"task_id": t.task_id, "reason": "no task-facing catalog software evidence"}
                for t in records if not t.tools]
    tool_anchors, tool_assignment, tool_build_report = _build_frequency_curriculum(
        tool_tasks, axis="tools"
    )
    tool_split = split_curriculum(tool_assignment)
    tool_build_report["validation"] = _frequency_validation(
        tool_tasks, tool_anchors, tool_assignment, tool_split, "tools"
    )

    skill_tasks = {t.task_id: set(t.skills) for t in records}
    skill_anchors, skill_assignment, skill_split, skill_build_report = (
        _build_skill_curriculum(records)
    )

    catalog = load_catalog()
    owner_of, owner_tools_all = _owner_maps(catalog)
    agent_tasks = {
        tid: {owner_of[tool] for tool in tools} for tid, tools in tool_tasks.items()
    }
    agent_anchors, agent_assignment, agent_build_report = _build_frequency_curriculum(
        agent_tasks, axis="agents", split_map=tool_split,
    )
    agent_build_report["validation"] = _frequency_validation(
        agent_tasks, agent_anchors, agent_assignment, tool_split, "agents"
    )
    active_owner_tools = {
        owner: sorted(tool for tool in owner_tools_all[owner]
                      if any(tool in toolset for toolset in tool_tasks.values()))
        for owner in sorted(agent_anchors[-1])
    }

    roots = {axis: dest / axis for axis in ("tools", "skills", "agents")}
    for root in roots.values():
        root.mkdir(parents=True)

    tools_manifest = _axis_manifest("tools", tool_tasks, tool_anchors,
                                    tool_assignment, tool_split, "tools", excluded,
                                    tool_build_report)
    skills_manifest = _axis_manifest("skills", skill_tasks, skill_anchors,
                                     skill_assignment, skill_split, "skills", [],
                                     skill_build_report)
    agents_manifest = _axis_manifest("agents", agent_tasks, agent_anchors,
                                     agent_assignment, tool_split, "agents", excluded,
                                     agent_build_report)
    write_json(roots["tools"] / "manifest.json", tools_manifest)
    write_json(roots["skills"] / "manifest.json", skills_manifest)
    write_json(roots["agents"] / "manifest.json", agents_manifest)
    write_json(roots["tools"] / "annotation_evidence.json", {
        t.task_id: {"oracle_tools": t.tools, "evidence": t.tool_evidence} for t in records
    })
    write_json(roots["skills"] / "annotation_evidence.json", {
        t.task_id: {"oracle_skills": t.skills, "evidence": t.skill_evidence} for t in records
    })

    _write_rows(roots["tools"], _tools_rows(tasks, tool_anchors, tool_assignment, tool_split))
    _write_skill_library(roots["skills"], tasks)
    _write_rows(roots["skills"], _skills_rows(tasks, skill_anchors, skill_assignment, skill_split))
    _materialize_agents(
        roots["agents"], agent_anchors, active_owner_tools,
        agents_manifest["versions"], set().union(*skill_tasks.values()),
    )
    _write_rows(roots["agents"], _agents_rows(
        tasks, agent_anchors, agent_assignment, tool_split, owner_of, active_owner_tools,
    ))
    # _materialize_agents writes per-version manifests before JSONL; preserve both.
    return {
        "tools": tools_manifest, "skills": skills_manifest, "agents": agents_manifest,
    }


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
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
    source_ids = {p.name for p in cfg.SOURCE.iterdir()
                  if p.is_dir() and (p / "task.toml").is_file()}
    if len(source_ids) != cfg.EXPECTED_TASKS:
        problems.append(f"source discovery expected {cfg.EXPECTED_TASKS}, got {len(source_ids)}")
    catalog = load_catalog()
    catalog_names = {x["name"] for x in catalog}
    owner_of, _ = _owner_maps(catalog)
    skill_names = {x["name"] for x in load_skill_catalog()}
    required_by_axis = {
        "tools": TOOLS_REQUIRED, "skills": SKILLS_REQUIRED, "agents": AGENTS_REQUIRED,
    }
    caps_fields = {
        "tools": ("oracle_tools", "cummulative_tools", "tools"),
        "skills": ("oracle_skills", "cummulative_oracle_skills", "skills"),
        "agents": ("oracle_agents", "cumulative_agents", "agents"),
    }
    summary: dict[str, Any] = {}
    for axis in ("tools", "skills", "agents"):
        root = candidate / axis
        if not root.is_dir():
            problems.append(f"missing axis directory: {axis}")
            continue
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            problems.append(f"{axis}: missing manifest.json")
            continue
        manifest = read_json(manifest_path)
        if manifest.get("discovered_tasks") != cfg.EXPECTED_TASKS:
            problems.append(f"{axis}: discovered_tasks != {cfg.EXPECTED_TASKS}")
        versions = manifest.get("versions") or []
        construction = manifest.get("construction") or {}
        expected_pipeline = (
            "evovle_skills.builder.sequencer.sequence_curriculum"
            if axis == "skills"
            else "evolve_tools.builder.frequency_config."
                 "build_frequency_anchors_adaptive"
        )
        if construction.get("implementation") != expected_pipeline:
            problems.append(
                f"{axis}: construction pipeline is not canonical: "
                f"{construction.get('implementation')!r}"
            )
        if not (construction.get("validation") or {}).get("passed"):
            problems.append(f"{axis}: canonical construction validation did not pass")
        if len(versions) < cfg.MIN_VERSIONS:
            problems.append(f"{axis}: fewer than {cfg.MIN_VERSIONS} versions")
        previous: set[str] = set()
        for index, version in enumerate(versions, 1):
            cumulative = set(version.get(f"cumulative_{caps_fields[axis][2]}") or [])
            if not previous < cumulative:
                problems.append(f"{axis} v{index}: cumulative chain is not strict")
            previous = cumulative
            if version.get("train", 0) < cfg.MIN_TRAIN or version.get("test", 0) < cfg.MIN_TEST:
                problems.append(f"{axis} v{index}: split floor violated")
        seen: set[str] = set()
        schemas: set[tuple[str, ...]] = set()
        rows = _jsonl_rows(root)
        for path, row in rows:
            tid = row.get("task_id")
            if tid not in source_ids:
                problems.append(f"{axis}: unknown source task {tid!r}")
            if tid in seen:
                problems.append(f"{axis}: duplicate task ID {tid}")
            seen.add(tid)
            schemas.add(tuple(sorted(row)))
            missing = required_by_axis[axis] - set(row)
            if missing:
                problems.append(f"{axis} {tid}: missing fields {sorted(missing)}")
            oracle_field, cumulative_field, _ = caps_fields[axis]
            oracle = set(row.get(oracle_field) or [])
            cumulative = set(row.get(cumulative_field) or [])
            if not oracle or not oracle <= cumulative:
                problems.append(f"{axis} {tid}: empty oracle or oracle not cumulative subset")
            stage = int(str(row.get("version", "v0")).removeprefix("v"))
            stage_manifest = versions[stage - 1] if 0 < stage <= len(versions) else {}
            introduced = set(stage_manifest.get(f"new_{caps_fields[axis][2]}") or [])
            if not oracle & introduced:
                problems.append(f"{axis} {tid}: no assigned-stage introduced capability")
            if axis == "tools":
                unknown = oracle - catalog_names
                if unknown:
                    problems.append(f"tools {tid}: unknown accepted software {sorted(unknown)}")
                evidence = row.get("annotation_evidence") or {}
                if any(not evidence.get(tool) for tool in oracle):
                    problems.append(f"tools {tid}: accepted software missing evidence")
            elif axis == "skills":
                if oracle - skill_names:
                    problems.append(f"skills {tid}: unknown skill {sorted(oracle - skill_names)}")
                for skill in oracle:
                    skill_root = root / "_oracle" / "skills" / skill
                    if not (skill_root / "SKILL.md").is_file():
                        problems.append(f"skills {tid}: missing SKILL.md for {skill}")
                    if not (skill_root / "index.json").is_file():
                        problems.append(f"skills {tid}: missing index.json for {skill}")
                if row.get("system_prompt") != HOUSE_RULES:
                    problems.append(
                        f"skills {tid}: oracle skill catalog leaked into system_prompt"
                    )
            else:
                expected = {owner_of[x] for x in row.get("oracle_tools") or []}
                if oracle != expected:
                    problems.append(f"agents {tid}: owners do not match oracle tools")
        if len(schemas) > 1:
            problems.append(f"{axis}: schema drift across rows")
        if len(rows) != manifest.get("retained_tasks"):
            problems.append(f"{axis}: row count {len(rows)} != retained {manifest.get('retained_tasks')}")
        summary[axis] = {"rows": len(rows), "versions": len(versions)}

    agents_root = candidate / "agents"
    capability_manifest_path = agents_root / "_capabilities" / "manifest.json"
    if not capability_manifest_path.is_file():
        problems.append("agents: missing _capabilities/manifest.json")
    else:
        capability_manifest = read_json(capability_manifest_path)
        if capability_manifest.get("content_dropped") != 0:
            problems.append("agents: mined skill content was dropped")
        active_skill_names = {
            skill
            for _, row in _jsonl_rows(candidate / "skills")
            for skill in row.get("oracle_skills") or []
        }
        if set((capability_manifest.get("skill_homes") or {}).keys()) != active_skill_names:
            problems.append("agents: capability homes do not cover every active skill")

    specs = {spec["name"]: spec for spec in load_skill_catalog()}
    for name in skill_names:
        skill_root = candidate / "skills" / "_oracle" / "skills" / name
        if not skill_root.is_dir():
            continue  # inactive catalog atom
        md = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        if specs[name]["procedure"].strip() not in md:
            problems.append(f"skills: {name} does not contain its catalog procedure")
        index = read_json(skill_root / "index.json")
        if index.get("tier") != specs[name]["tier"] or not index.get("signature"):
            problems.append(f"skills: {name} has incomplete ALE-style index metadata")

    for path in sorted(agents_root.glob("**/agents/*.toml")):
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            if not data.get("name") or not data.get("description"):
                problems.append(f"invalid agent TOML identity: {path}")
            configs = (data.get("skills") or {}).get("config") or []
            for skill in configs:
                rel = skill.get("path")
                pool_root = path.parent.parent
                if not rel or not (pool_root / rel).is_file():
                    problems.append(f"agent TOML missing referenced skill: {path}: {rel}")
        except Exception as exc:
            problems.append(f"invalid agent TOML {path}: {exc}")
    if problems:
        raise ValueError("static validation failed:\n- " + "\n- ".join(problems))
    return {"ok": True, "candidate": str(candidate), "summary": summary,
            "content_hash": tree_hash(candidate)}


def _attempt_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{time.time_ns() % 1_000_000_000:09d}"


def build() -> dict[str, Any]:
    cfg.ATTEMPTS.mkdir(parents=True, exist_ok=True)
    attempt = cfg.ATTEMPTS / _attempt_id()
    first, second = attempt / "build_a", attempt / "build_b"
    try:
        build_into(first)
        validate_a = validate_candidate(first)
        build_into(second)
        validate_b = validate_candidate(second)
        hash_a, hash_b = tree_hash(first), tree_hash(second)
        if hash_a != hash_b:
            raise ValueError(f"determinism failure: {hash_a} != {hash_b}")
        result = {
            "ok": True, "attempt": str(attempt), "candidate": str(first),
            "replica": str(second), "content_hash": hash_a,
            "validation": validate_a, "replica_validation": validate_b,
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
        raise FileNotFoundError("no built candidate; run `python -m tb_builder build`")
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


def _reward_values(value: Any) -> list[float]:
    found: list[float] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "reward" and isinstance(child, (int, float)):
                found.append(float(child))
            else:
                found.extend(_reward_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_reward_values(child))
    return found


def _collect_rewards(root: Path) -> list[float]:
    rewards: list[float] = []
    for path in sorted(root.rglob("*.json")):
        try:
            rewards.extend(_reward_values(read_json(path)))
        except (OSError, json.JSONDecodeError):
            continue
    return rewards


def select_smoke_tasks(candidate: Path) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    tools = candidate / "tools"
    for vroot in sorted(tools.glob("v*"), key=lambda p: int(p.name[1:])):
        rows = [row for _, row in _jsonl_rows(vroot.parent) if row.get("version") == vroot.name]
        cpu = [r for r in rows if (r.get("resource_limits") or {}).get("gpus", 0) == 0]
        if not cpu:
            raise ValueError(f"{vroot.name}: no CPU-only smoke candidate")
        selected = min(cpu, key=lambda r: (
            float((r.get("resource_limits") or {}).get("agent_timeout_sec") or math.inf),
            r["task_id"],
        ))
        choices.append({
            "version": vroot.name, "task_id": selected["task_id"],
            "agent_timeout_sec": selected["resource_limits"].get("agent_timeout_sec"),
            "gpus": selected["resource_limits"].get("gpus"),
        })
    return choices


def _promote(candidate: Path) -> None:
    for axis, target in cfg.FINAL_ROOTS.items():
        source = candidate / axis
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.promote-{os.getpid()}"
        if temp.exists():
            shutil.rmtree(temp)
        shutil.copytree(source, temp)
        backup = target.parent / f".{target.name}.previous-{os.getpid()}"
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.rename(backup)
        try:
            temp.rename(target)
        except Exception:
            if backup.exists() and not target.exists():
                backup.rename(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)


def smoke(path: Path | None = None, *, promote: bool = True) -> dict[str, Any]:
    candidate = path or latest_candidate()
    validation = validate_candidate(candidate)
    setup_runtime()
    choices = select_smoke_tasks(candidate)
    smoke_root = cfg.REPORTS / "smoke" / _attempt_id()
    smoke_root.mkdir(parents=True)
    results = []
    harbor = cfg.RUNTIME / "bin" / "harbor"
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(cfg.CACHE / "uv-cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(cfg.CACHE / "python")
    env["HARBOR_TELEMETRY"] = "0"
    package_root = str(cfg.DATA_DRY_RUN)
    env["PYTHONPATH"] = package_root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    for choice in choices:
        task_id = choice["task_id"]
        task_root = cfg.SOURCE / task_id
        task_report = smoke_root / f"{choice['version']}-{task_id}"
        jobs = task_report / "jobs"
        jobs.mkdir(parents=True)
        command = [
            str(harbor), "run", "--path", str(task_root), "--agent", "oracle",
            "--n-concurrent", "1", "--n-attempts", "1", "--jobs-dir", str(jobs),
            "--job-name", f"smoke-{choice['version']}-{task_id}",
            "--env", "tb_builder.remote_docker:RemoteDockerEnvironment",
        ]
        started = time.monotonic()
        completed = subprocess.run(command, env=env, text=True, capture_output=True,
                                   timeout=float(choice["agent_timeout_sec"] or 900) * 2 + 900)
        (task_report / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        (task_report / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        rewards = _collect_rewards(jobs)
        passed = completed.returncode == 0 and bool(rewards) and max(rewards) >= 1.0
        item = {
            **choice, "command": command, "returncode": completed.returncode,
            "rewards": rewards, "passed": passed,
            "elapsed_sec": round(time.monotonic() - started, 3),
            "report_dir": str(task_report),
        }
        results.append(item)
        write_json(task_report / "result.json", item)
        if not passed:
            break
    ok = len(results) == len(choices) and all(r["passed"] for r in results)
    report = {
        "ok": ok, "candidate": str(candidate),
        "candidate_hash": validation["content_hash"], "selected": choices,
        "results": results, "promoted": False,
    }
    if ok and promote:
        _promote(candidate)
        report["promoted"] = True
        report["destinations"] = {k: str(v) for k, v in cfg.FINAL_ROOTS.items()}
        write_statistics_report(candidate)
    write_json(smoke_root / "summary.json", report)
    write_json(cfg.REPORTS / "smoke" / "latest.json", report)
    if not ok:
        raise RuntimeError(f"smoke validation failed; preserved report at {smoke_root}")
    return report


def all_steps() -> dict[str, Any]:
    fetched = fetch(runtime=True)
    inspected = inspect_source(write=True)
    built = build()
    validated = validate(Path(built["candidate"]))
    smoked = smoke(Path(built["candidate"]), promote=True)
    return {
        "fetch": fetched, "inspect": {
            "discovered_tasks": inspected["discovered_tasks"],
            "tools_retained_tasks": inspected["tools_retained_tasks"],
        }, "build": built, "validate": validated, "smoke": smoked,
    }
__EVOHARNESS_TB2_FILE__
```

## Skills · Step 1 mine the prompts

```bash
python - <<'PY'
import hashlib, json, os, pathlib, urllib.request

base = os.environ.get("EVOLVE_TB2_SERVICE", "https://educator-marrow-cultural.ngrok-free.dev").rstrip("/")
root = pathlib.Path(os.environ["EVOLVE_TB2_WORKDIR"])
eval_key = os.environ.get("EVAL_SERVICE_API_KEY", "").strip()
if not eval_key:
    raise RuntimeError("EVAL_SERVICE_API_KEY is required to download the closed catalog")
public_name = "skill-catalog.json"
expected = "91deca9269aedb5c00c27cae8bb639112a52bbe56939d61eaefac7d4e98a25ef"
url = base + "/resources/evolve-benchmark/references/tb2/v1/" + public_name
request = urllib.request.Request(url, headers={
    "Authorization": f"Bearer {eval_key}",
    "ngrok-skip-browser-warning": "true",
})
with urllib.request.urlopen(request) as response:
    content = response.read()
actual = hashlib.sha256(content).hexdigest()
if actual != expected:
    raise RuntimeError(f"checksum mismatch for {public_name}: {actual}")
target = root / "data_dry_run" / "tb_builder" / "skills.json"
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_name(target.name + ".download")
temporary.write_bytes(content)
temporary.replace(target)

skills = json.loads((root / "data_dry_run/tb_builder/skills.json").read_text())
required = {"name", "title", "tier", "description", "categories", "terms", "tools",
            "procedure", "notes", "see_also"}
names = [item["name"] for item in skills]
if len(skills) != 26 or len(names) != len(set(names)):
    raise RuntimeError("the pinned skill catalog must contain 26 unique skills")
if any(required - set(item) for item in skills):
    raise RuntimeError("invalid skill-catalog schema")
print("Skills catalog: 26 deterministic procedural atoms ready for prompt mining")
PY
mkdir -p "$EVOLVE_TB2_WORKDIR/evovle_skills/builder"
cat > "$EVOLVE_TB2_WORKDIR/evovle_skills/builder/sequencer.py" <<'__EVOHARNESS_TB2_FILE__'
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
__EVOHARNESS_TB2_FILE__

mkdir -p "$EVOLVE_TB2_WORKDIR/evovle_skills/builder/shared"
cat > "$EVOLVE_TB2_WORKDIR/evovle_skills/builder/shared/paths.py" <<'__EVOHARNESS_TB2_FILE__'
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
__EVOHARNESS_TB2_FILE__
```

## Agents · Step 1 partition the tools

```bash
python - <<'PY'
import collections, json, os, pathlib

root = pathlib.Path(os.environ["EVOLVE_TB2_WORKDIR"])
catalog = json.loads((root / "data_dry_run/tb_builder/catalog.json").read_text())
owners = collections.defaultdict(list)
for item in catalog:
    owner = item.get("owner", "").strip()
    if not owner:
        raise RuntimeError(f"missing owner for {item['name']}")
    owners[owner].append(item["name"])
owned = [tool for tools in owners.values() for tool in tools]
if len(owned) != len(set(owned)) or len(owned) != len(catalog):
    raise RuntimeError("software ownership is not an exact partition")
print(f"Agents catalog: {len(owners)} owner families form an exact software partition")
PY
```

## Expected output · build, validate, and smoke-test

```bash
mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat > "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/config.py" <<'__EVOHARNESS_TB2_FILE__'
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DRY_RUN = ROOT.parent
REPO_ROOT = DATA_DRY_RUN.parent

SOURCE_URL = "https://github.com/harbor-framework/terminal-bench-2.git"
SOURCE_COMMIT = "2fd12b88aafdd04a52c298e3940bcb189f9766d6"
HARBOR_URL = "https://github.com/laude-institute/harbor.git"
HARBOR_COMMIT = "d8cfe6b6fd463fc1f2a84abf8f1406f46e70c621"
HARBOR_VERSION = "0.23.0"
UV_VERSION = "0.12.13"
PYTHON_VERSION = "3.12.12"
BASHLEX_VERSION = "0.18"
TOMLI_VERSION = "2.2.1"

CACHE = ROOT / "cache"
SOURCE = CACHE / "terminal-bench-2"
HARBOR = CACHE / "harbor"
RUNTIME = CACHE / "venv"
PYDEPS = CACHE / "pydeps"
REPORTS = ROOT / "reports"
ATTEMPTS = ROOT / "attempts"
LATEST_CANDIDATE = ROOT / "latest_candidate.json"

FINAL_ROOTS = {
    "tools": DATA_DRY_RUN / "evovling_tools" / "terminal_bench_2_validated",
    "skills": DATA_DRY_RUN / "evovling_skills" / "terminal_bench_2_validated",
    "agents": DATA_DRY_RUN / "evovling_agents" / "terminal_bench_2_validated",
}

SEED = 42
EXPECTED_TASKS = 89
TARGET_VERSIONS = 5
MIN_VERSIONS = 3
MIN_TRAIN = 2
MIN_TEST = 5
ADAPT_RATIO = 0.30
DOMAIN = "terminal_bench_2"

# Dataset-specific numeric knobs for the *existing* EvoHarnessBench builders.
# The construction algorithms themselves are imported from evolve_tools and
# evovle_skills; these values only tune them to TB2's 89-task/50-tool scale.
TOOL_MIN_NEW_TASKS = MIN_TRAIN + MIN_TEST
TOOL_MIN_GROWTH_FRAC = 0.15
TOOL_MAX_GROWTH_FRAC = 0.50
TOOL_INITIAL_ANCHOR_FRAC = 0.25

SKILL_MIN_STEP_SIZE = 14

AGENT_MIN_NEW_TASKS = MIN_TRAIN + MIN_TEST
AGENT_MIN_GROWTH_FRAC = 0.15
AGENT_MAX_GROWTH_FRAC = 0.35
__EVOHARNESS_TB2_FILE__

mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat > "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/remote_docker.py" <<'__EVOHARNESS_TB2_FILE__'
"""Docker environment shim for a daemon in a separate mount namespace.

The workspace's Docker API is reachable, but bind-mount source paths belong to
the daemon host rather than this process. Harbor already transfers task files
with compose/engine copy operations; this shim disables only its three log bind
mounts and leaves all task execution, oracle, and verifier behavior unchanged.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.environments.docker.compose_env import legacy_log_mount_env_vars
from harbor.environments.docker.docker import DockerEnvironment


class RemoteDockerEnvironment(DockerEnvironment):
    """Pinned Harbor Docker backend without client-host bind mounts."""

    @property
    def capabilities(self) -> EnvironmentCapabilities:
        base = super().capabilities
        return base.model_copy(update={"mounted": False})

    def _write_mounts_compose_file(self) -> Path:
        self._cleanup_mounts_compose_file()
        self._mounts_compose_temp_dir = tempfile.TemporaryDirectory()
        path = Path(self._mounts_compose_temp_dir.name) / "docker-compose-mounts.json"
        # Harbor's base compose declares no volumes. An empty main override lets
        # ensure_dirs + compose cp provide the same log paths without a bind.
        path.write_text(json.dumps({"services": {"main": {}}}, indent=2) + "\n")
        return path

    def _compose_infra_env_vars(self) -> dict[str, str]:
        env_vars = super()._compose_infra_env_vars()
        # Legacy task compose files expect HOST_* variables. Without binds the
        # meaningful location is the matching in-container target.
        env_vars.update(legacy_log_mount_env_vars(self._mounts, host_value="target"))
        return env_vars

    async def prepare_logs_for_host(self) -> None:
        # There are no bind-mounted files whose UID needs changing. Harbor
        # downloads the log directories with docker compose cp.
        return None
__EVOHARNESS_TB2_FILE__

mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat > "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/__init__.py" <<'__EVOHARNESS_TB2_FILE__'
"""Deterministic Terminal-Bench 2 evolving-dataset builder."""

from .config import HARBOR_COMMIT, SOURCE_COMMIT

__all__ = ["HARBOR_COMMIT", "SOURCE_COMMIT"]

__EVOHARNESS_TB2_FILE__

mkdir -p "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder"
cat > "$EVOLVE_TB2_WORKDIR/data_dry_run/tb_builder/__main__.py" <<'__EVOHARNESS_TB2_FILE__'
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .builder import all_steps, build, fetch, inspect_source, smoke, validate


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="python -m tb_builder",
        description="Build and validate deterministic Terminal-Bench 2 evolving datasets.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch", help="Fetch pinned sources and runtime.")
    fetch_parser.add_argument("--no-runtime", action="store_true",
                              help="Fetch source repositories without preparing Harbor.")
    commands.add_parser("inspect", help="Annotate all 89 source tasks and write evidence.")
    commands.add_parser("build", help="Build twice and require byte-identical candidates.")
    validate_parser = commands.add_parser("validate", help="Statically validate a candidate.")
    validate_parser.add_argument("--candidate", type=Path)
    smoke_parser = commands.add_parser("smoke", help="Run one pinned Harbor oracle task per tool stage.")
    smoke_parser.add_argument("--candidate", type=Path)
    smoke_parser.add_argument("--no-promote", action="store_true")
    commands.add_parser("all", help="Fetch, inspect, build, validate, smoke, and promote.")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "fetch":
            result = fetch(runtime=not args.no_runtime)
        elif args.command == "inspect":
            report = inspect_source(write=True)
            result = {
                "discovered_tasks": report["discovered_tasks"],
                "tools_retained_tasks": report["tools_retained_tasks"],
                "tools_excluded_tasks": report["tools_excluded_tasks"],
            }
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
    except Exception as exc:
        print(f"tb_builder: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

__EVOHARNESS_TB2_FILE__

cd "$EVOLVE_TB2_WORKDIR/data_dry_run"
python -m tb_builder all
```
