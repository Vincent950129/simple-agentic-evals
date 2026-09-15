"""Host-side helpers for the ALE (Agents' Last Exam) file-sandbox backend.

The eval-service runs under an ordinary interpreter; ALE's real grader needs the
ALE ``uv`` venv (py3.12 + cua_bench). So grading is delegated to
``_ale_grader_driver.py`` run under that venv (see its docstring). This module
handles the host-side plumbing that needs no heavy deps:

  * locate ``ALE_ROOT`` + its venv python + a task's staged ``task-data``;
  * stage a per-session workspace (symlink ``input``/``reference``/``software``,
    create a writable ``output/``);
  * enumerate input files to serve to the agent;
  * write the agent's submitted artifact(s) under ``output/``;
  * invoke the driver and parse its score.

Nothing here imports cua_bench; the only ALE dependency is the on-disk repo.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("eval_service.ale")

_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parent.parent  # .../server
_DRIVER = _THIS.parent / "_ale_grader_driver.py"

ALE_ROOT = Path(
    os.environ.get("ALE_ROOT", REPO_ROOT / "reference" / "agents-last-exam")
).resolve()
# Per-session workspaces (symlinks + a small output dir; cheap to create/remove).
WORKDIR_ROOT = Path(
    os.environ.get("EVAL_SERVICE_ALE_WORKDIR", _THIS.parent / ".ale_workspaces")
).resolve()
# ALE scores are continuous in [0,1]; the harness flags binary success at 1.0.
SUCCESS_THRESHOLD = float(os.environ.get("EVAL_SERVICE_ALE_SUCCESS_THRESHOLD", "1.0"))
GRADE_TIMEOUT_SEC = int(os.environ.get("EVAL_SERVICE_ALE_GRADE_TIMEOUT_SEC", "600"))

_RESULT_SENTINEL = "__ALE_RESULT__"


def _numeric_score(value: Any) -> float | None:
    """A finite verifier score, or ``None`` for non-numeric diagnostics."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def verifier_records(
    result: Mapping[str, Any], *, grading_path: str,
) -> list[dict[str, Any]]:
    """Normalize an ALE evaluator result into public per-verifier records.

    ALE permits evaluators to return a rich dict, but the current upstream task
    set normally returns ``[score]``.  Rich ``per_verifier`` /
    ``verifier_results`` records are preserved verbatim where safe; otherwise
    every value in ``raw_scores`` becomes a stable component record.  A task
    exposing only one scalar is labelled as aggregate granularity rather than
    pretending its natural-language rubric has separately measured checks.
    """
    explicit = result.get("per_verifier") or result.get("verifier_results")
    if isinstance(explicit, Mapping):
        explicit = [
            ({"name": str(name), **dict(value)} if isinstance(value, Mapping)
             else {"name": str(name), "score": value})
            for name, value in explicit.items()
        ]
    if isinstance(explicit, (list, tuple)) and explicit:
        records: list[dict[str, Any]] = []
        for index, raw in enumerate(explicit):
            item = dict(raw) if isinstance(raw, Mapping) else {"score": raw}
            score = _numeric_score(item.get("score", item.get("actual")))
            passed = item.get("passed")
            if passed is None:
                passed = score is not None and score >= SUCCESS_THRESHOLD
            details = item.get("details")
            records.append({
                "name": str(item.get("name") or f"ale_verifier_{index + 1}"),
                "passed": bool(passed),
                "score": score,
                "expected": item.get("expected", f">= {SUCCESS_THRESHOLD}"),
                "actual": item.get("actual", score),
                "comparison_type": str(
                    item.get("comparison_type") or f"ale_component:{grading_path}"
                ),
                "description": str(
                    item.get("description") or item.get("criterion") or ""
                ),
                "details": details,
                "error": item.get("error"),
            })
        return records

    values = result.get("raw_scores")
    if not isinstance(values, (list, tuple)) or not values:
        values = [result.get("score", 0.0)]
    names = result.get("verifier_names") or result.get("score_names") or []
    if not isinstance(names, (list, tuple)):
        names = []
    aggregate = len(values) == 1
    records = []
    for index, value in enumerate(values):
        score = _numeric_score(value)
        name = (
            str(names[index]) if index < len(names) and names[index]
            else "ale_score" if aggregate
            else f"ale_verifier_{index + 1}"
        )
        records.append({
            "name": name,
            "passed": score is not None and score >= SUCCESS_THRESHOLD,
            "score": score,
            "expected": f">= {SUCCESS_THRESHOLD}",
            "actual": round(score, 6) if score is not None else value,
            "comparison_type": (
                f"ale_score:{grading_path}" if aggregate
                else f"ale_component:{grading_path}"
            ),
            "description": "",
            "details": {
                "granularity": "aggregate" if aggregate else "component",
                **({"index": index} if not aggregate else {}),
            },
            "error": None,
        })
    return records


class AleUnavailable(Exception):
    """ALE backend prerequisite missing (venv / task-data / driver)."""


def kill_process_tree(
    proc: subprocess.Popen, *, drain_timeout: float = 30.0,
) -> tuple[str, str]:
    """SIGKILL a timed-out child *and every descendant it spawned*.

    Only meaningful for children started with ``start_new_session=True``: they
    lead their own process group, so one ``killpg`` reaps the whole subtree
    (``ale_run`` -> ``codex`` -> the ``bwrap`` sandbox it forks per tool call).
    Killing just the direct child instead strands those descendants on PID 1,
    where they pile up as zombies unless PID 1 is a real init.

    The drain afterwards is bounded: descendants inherit the stdout/stderr pipe
    write ends, so an unbounded ``communicate()`` can block forever even after
    the child is dead. It returns whatever the child managed to write before
    dying (``("", "")`` if the pipes never drained), which is the only record
    left of resources it never got to release -- see ``reap_containers``.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        out, err = proc.communicate(timeout=drain_timeout)
        return (out or ""), (err or "")
    except subprocess.TimeoutExpired:
        logger.warning("kill_process_tree: pipes still open after SIGKILL (pid=%s)", proc.pid)
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass
    return "", ""


# --------------------------------------------------------------------------- #
# ale_run sandbox containers                                                   #
# --------------------------------------------------------------------------- #
# ale_run removes its own sandbox container in a `finally` (we generate the
# experiment with cleanup_mode: delete), but only if it lives long enough to
# reach it -- a SIGKILL from our outer timeout skips that path and strands the
# container, holding its CPU/memory reservation until an operator notices.
# Two guards, because each covers what the other misses:
#   * ale_run_outer_timeout() keeps our ceiling above ale_run's own worst case,
#     so the graceful path wins whenever ale_run is merely slow;
#   * reap_containers() removes what is left when it doesn't -- a caller-
#     supplied budget shorter than that ceiling, or ale_run dying some other way.

# ale_run's per-unit ceilings, from the reference checkout's
# ale_run/orchestration/lifecycle.py. These bound *successive* phases: the three
# post-agent artifact steps and then the eval both start after the agent's wall
# budget is already spent, so the worst case is their sum, not their max.
_ALE_EVAL_TIMEOUT_S = 7200
_ALE_ARTIFACT_STEP_TIMEOUT_S = 1800
_ALE_ARTIFACT_STEPS = 3
# Container boot (~1-2 min to cua-ready), task staging, and cleanup itself.
_ALE_OVERHEAD_S = 1200


def ale_run_outer_timeout(agent_wall_s: int) -> int:
    """Smallest outer budget that still lets ale_run clean up after itself.

    A unit that hits every internal ceiling in turn exits on its own -- and
    deletes its container -- at roughly this point. Kill it any earlier and the
    container outlives us.
    """
    return (
        int(agent_wall_s)
        + _ALE_EVAL_TIMEOUT_S
        + _ALE_ARTIFACT_STEPS * _ALE_ARTIFACT_STEP_TIMEOUT_S
        + _ALE_OVERHEAD_S
    )


# ale_run's docker provider names containers "ale-<task-slug>-<hash8>", where
# the slug is the task id lowercased with each non-alphanumeric run replaced by
# "-" and then truncated (environments/providers/docker.py). The hash seeds off
# time.time(), so we can reconstruct the prefix but never the full name.
_CONTAINER_SLUG_MAX = 40
_CONTAINER_RE = re.compile(r"\bale-[a-z0-9][a-z0-9-]*-[0-9a-f]{8}\b")
_DOCKER_CLI_TIMEOUT_S = 60


def container_prefix(domain: str, task: str) -> str:
    """The ``ale-<slug>`` prefix every container for ``domain/task`` shares."""
    slug = re.sub(r"[^a-z0-9]", "-", f"{domain}/{task}".lower()).strip("-")
    return f"ale-{slug[:_CONTAINER_SLUG_MAX]}"


def _docker(*args: str) -> tuple[int, str]:
    """Run a docker CLI command; never raises, never blocks indefinitely."""
    try:
        p = subprocess.run(
            ["docker", *args], capture_output=True, text=True,
            timeout=_DOCKER_CLI_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("docker %s failed: %s: %s", args[0], type(e).__name__, e)
        return 1, ""
    return p.returncode, (p.stdout or "").strip()


def list_containers(prefix: str) -> set[str]:
    """Names of the containers under ``prefix``, in any state."""
    rc, out = _docker(
        "ps", "-a", "--filter", f"name=^{prefix}-", "--format", "{{.Names}}",
    )
    if rc != 0:
        return set()
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def reap_containers(
    domain: str, task: str, output: str = "", before: set[str] | None = None,
) -> list[str]:
    """Force-remove sandbox containers a finished run left behind.

    Cheap enough (one ``docker ps``) to call after every run, not just after a
    timeout: a run that cleaned up properly matches nothing.

    ``output`` is the run's combined stdout/stderr, which names each container
    ale_run created and so identifies ours exactly. Only when that yields
    nothing -- ale_run died before logging, or the pipes never drained -- do we
    fall back to whatever appeared under this task's prefix since ``before`` was
    sampled, which is ambiguous only if a second run of the same task started in
    that window.
    """
    prefix = container_prefix(domain, task)
    alive = list_containers(prefix)
    if not alive:
        return []
    ours = {n for n in _CONTAINER_RE.findall(output or "") if n in alive}
    if not ours:
        ours = alive - (before or set())
    removed = []
    for name in sorted(ours):
        rc, _ = _docker("rm", "-f", name)
        if rc == 0:
            removed.append(name)
        else:
            logger.warning("could not remove leaked ALE container %s", name)
    if removed:
        logger.warning(
            "reaped %d leaked ALE container(s) for %s/%s: %s",
            len(removed), domain, task, ", ".join(removed),
        )
    return removed


@dataclass
class AleTask:
    """A resolved ALE task + its per-session workspace."""

    domain: str
    task: str
    variant: str
    session_root: Path        # <WORKDIR_ROOT>/<session-ish>
    base_dir: Path            # <session_root>/<domain>/<task>/<variant>
    input_dir: Path
    output_dir: Path


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #
def venv_python() -> Path | None:
    """The ALE uv venv interpreter (py3.12 + cua_bench), or None if absent."""
    cand = ALE_ROOT / ".venv" / "bin" / "python"
    return cand if cand.exists() else None


def resolve_variant(domain: str, task: str) -> str:
    """The task-data variant dir name to stage for ``domain/task``.

    Published cells are single-variant. Most ship under ``base``, but a subset
    ships under the task's *real* variant name (e.g. ``zscaler_fy2025``,
    ``instance_1``, ``137``). ale_run resolves variants positionally (index 0),
    so the sole task-data subdir is that variant. We prefer ``base`` when
    present, else the sole subdir, else fall back to ``base`` (staging then
    reports the task as un-staged rather than silently grading the wrong dir).
    """
    root = ALE_ROOT / "task-data" / domain / task
    if (root / "base").is_dir():
        return "base"
    if root.is_dir():
        subs = [p.name for p in sorted(root.iterdir())
                if p.is_dir() and not p.name.startswith(".")]
        if len(subs) == 1:
            return subs[0]
    return "base"


def split_task_path(row: Any) -> tuple[str, str, str]:
    """Derive (domain, task, variant) from a dataset row.

    ALE rows carry ``source_repo_path`` like ``tasks/<domain>/<task>`` (and a
    ``task_id`` of ``<domain>/<task>``). The variant is resolved from the local
    task-data layout (see :func:`resolve_variant`) — ``base`` for most cells,
    the real variant name for the rest.
    """
    raw = row.raw or {}
    rel = str(raw.get("source_repo_path") or row.task_id or "")
    rel = re.sub(r"^tasks/", "", rel.strip()).strip("/")
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2:
        raise AleUnavailable(f"cannot parse domain/task from {rel!r}")
    return parts[0], parts[1], resolve_variant(parts[0], parts[1])


@lru_cache(maxsize=256)
def _public_task_card(source_repo_path: str) -> tuple[tuple[str, ...], str]:
    """Read only the public guidance fields from one ALE task card.

    ``source_repo_path`` originates in the dataset, but it is still resolved
    and checked beneath ``ALE_ROOT/tasks`` before reading. Reference files,
    hidden values, and every other task-card field stay out of the response.
    """
    tasks_root = (ALE_ROOT / "tasks").resolve()
    rel = re.sub(r"^tasks/", "", source_repo_path.strip()).strip("/")
    if not rel:
        return (), ""
    card = (tasks_root / rel / "task_card.json").resolve()
    if tasks_root not in card.parents or not card.is_file():
        return (), ""
    try:
        data = json.loads(card.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return (), ""
    if not isinstance(data, Mapping):
        return (), ""
    raw_steps = data.get("agentMustDo") or data.get("agent_must_do") or []
    steps = tuple(str(value) for value in raw_steps if isinstance(value, str)) \
        if isinstance(raw_steps, (list, tuple)) else ()
    evaluation = data.get("evaluation")
    return steps, str(evaluation) if isinstance(evaluation, str) else ""


def public_task_metadata(row: Any) -> dict[str, Any]:
    """Return ALE's public required steps and natural-language evaluation.

    The dataset's ``agent_must_do`` is preferred because it is the versioned
    row used for this evaluation. ``task_card.json`` supplies the evaluation
    text (and is a fallback for steps in older rows).
    """
    raw = row.raw if isinstance(getattr(row, "raw", None), Mapping) else {}
    source = str(raw.get("source_repo_path") or getattr(row, "task_id", "") or "")
    card_steps, card_evaluation = _public_task_card(source)
    raw_steps = raw.get("agent_must_do") or raw.get("agentMustDo") or []
    required_steps = (
        [str(value) for value in raw_steps if isinstance(value, str)]
        if isinstance(raw_steps, (list, tuple))
        else []
    )
    evaluation = raw.get("evaluation")
    return {
        "required_steps": required_steps or list(card_steps),
        "evaluation": str(evaluation) if isinstance(evaluation, str) else card_evaluation,
    }


def task_data_base(domain: str, task: str, variant: str) -> Path:
    return ALE_ROOT / "task-data" / domain / task / variant


def task_module_dir(domain: str, task: str) -> Path:
    return ALE_ROOT / "tasks" / domain / task


# --------------------------------------------------------------------------- #
# Workspace staging                                                           #
# --------------------------------------------------------------------------- #
def _link_or_copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    try:
        dst.symlink_to(src, target_is_directory=src.is_dir())
    except (OSError, NotImplementedError):
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def stage_workspace(session_id: str, domain: str, task: str, variant: str) -> AleTask:
    """Create the per-session workspace.

    ``input``/``reference``/``software`` are symlinked from the read-only
    ``task-data`` (graders only read them); ``output/`` is a fresh writable dir
    for the agent's artifact.
    """
    src_base = task_data_base(domain, task, variant)
    if not src_base.is_dir():
        raise AleUnavailable(
            f"ALE task-data not staged locally for {domain}/{task} "
            f"(expected {src_base})"
        )
    if not task_module_dir(domain, task).is_dir():
        raise AleUnavailable(f"ALE task module missing: tasks/{domain}/{task}")

    session_root = WORKDIR_ROOT / session_id
    base_dir = session_root / domain / task / variant
    base_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("input", "reference", "software"):
        s, d = src_base / sub, base_dir / sub
        if s.exists() and not d.exists():
            _link_or_copy(s, d)
    output_dir = base_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return AleTask(
        domain=domain, task=task, variant=variant, session_root=session_root,
        base_dir=base_dir, input_dir=base_dir / "input", output_dir=output_dir,
    )


def teardown_workspace(session_root: str | Path) -> None:
    # Only ever remove a session dir that lives strictly under WORKDIR_ROOT.
    p = Path(session_root).resolve()
    if p.exists() and WORKDIR_ROOT in p.parents:
        shutil.rmtree(p, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Input enumeration + artifact submission                                     #
# --------------------------------------------------------------------------- #
def list_inputs(base_dir: Path) -> list[dict[str, Any]]:
    """All files under the workspace ``input/`` as {path,size} (path rel to base)."""
    out: list[dict[str, Any]] = []
    idir = base_dir / "input"
    if not idir.exists():
        return out
    for p in sorted(idir.rglob("*")):
        if p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            out.append({"path": str(p.relative_to(base_dir)), "size": size})
    return out


def resolve_input_file(base_dir: Path, rel_path: str) -> Path:
    """Resolve a client-supplied input path safely under ``input/``."""
    rel = rel_path.strip().lstrip("/")
    if not rel.startswith("input/"):
        rel = f"input/{rel}"
    target = (base_dir / rel).resolve()
    base_input = (base_dir / "input").resolve()
    if base_input not in target.parents and target != base_input:
        raise AleUnavailable(f"input path escapes workspace: {rel_path!r}")
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    return target


def _safe_output_relpath(rel_path: str) -> str:
    """Normalize a submitted output path to live under ``output/``.

    Tolerates agents echoing the prompt's path: ``arbitration_fee_results.json``,
    ``output/...`` and ``base/output/...`` all land in the workspace output dir.
    """
    rel = rel_path.strip().lstrip("/")
    rel = re.sub(r"^base/", "", rel)
    rel = re.sub(r"^output/", "", rel)
    rel = rel.lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise AleUnavailable(f"unsafe output path: {rel_path!r}")
    return rel


def write_submission(output_dir: Path, files: list[dict[str, Any]]) -> list[str]:
    """Write submitted artifacts under ``output/``. Returns the written relpaths.

    Each file: ``{"path": str, "content": str}`` or ``{"path": str,
    "content_b64": str}``.
    """
    import base64

    written: list[str] = []
    for f in files:
        rel = _safe_output_relpath(str(f.get("path", "")))
        dst = output_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if f.get("content_b64") is not None:
            dst.write_bytes(base64.b64decode(f["content_b64"]))
        else:
            dst.write_text(str(f.get("content", "")), encoding="utf-8")
        written.append(rel)
    return written


def output_path_hint(prompt: str) -> str:
    """Best-effort: the deliverable path the prompt tells the agent to write.

    Looks for ``output/<file>`` (optionally ``base/output/...``). Returns a path
    relative to the workdir base (e.g. ``output/results.json``) or ``""``.
    """
    if not prompt:
        return ""
    m = re.search(r"(?:base/)?(output/[A-Za-z0-9._\-/]+)", prompt)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Driver invocation                                                           #
# --------------------------------------------------------------------------- #
def _run_driver(task: AleTask, mode: str, split: str = "train") -> dict[str, Any]:
    py = venv_python()
    if py is None:
        raise AleUnavailable(
            f"ALE grader venv not found at {ALE_ROOT}/.venv (run `uv sync` in "
            "ALE_ROOT to enable ALE grading)"
        )
    if not _DRIVER.exists():
        raise AleUnavailable(f"ALE grader driver missing: {_DRIVER}")
    cmd = [
        str(py), str(_DRIVER),
        "--ale-root", str(ALE_ROOT),
        "--workspace", str(task.session_root),
        "--domain", task.domain, "--task", task.task,
        "--variant", task.variant, "--split", split, "--mode", mode,
    ]
    # start_new_session puts the driver in its own process group so a timeout can
    # kill the whole tree. Killing only the direct child leaves its descendants
    # orphaned onto PID 1, which never reaps them unless PID 1 is a real init.
    proc = subprocess.Popen(
        cmd, cwd=str(ALE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=GRADE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        raise
    line = next(
        (ln for ln in reversed((out or "").splitlines())
         if ln.startswith(_RESULT_SENTINEL)),
        None,
    )
    if line is None:
        tail = (err or out or "").strip().splitlines()[-4:]
        raise AleUnavailable(
            "ALE grader produced no result; " + " | ".join(tail)
        )
    return json.loads(line[len(_RESULT_SENTINEL):].strip())


def grade(task: AleTask, split: str = "train") -> dict[str, Any]:
    """Grade the staged workspace. Returns the driver's parsed result dict.

    Result keys: ``ok`` (bool); on success ``score`` (float in [0,1]),
    ``raw_scores``, ``output_file``; on failure ``reason`` and possibly
    ``needs_sandbox``.
    """
    result = _run_driver(task, mode="grade", split=split)
    if result.get("ok"):
        from .ale_verifier_capture import record_payload
        record_payload(result)
    return result
