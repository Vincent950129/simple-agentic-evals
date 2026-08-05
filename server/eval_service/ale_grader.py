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
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class AleUnavailable(Exception):
    """ALE backend prerequisite missing (venv / task-data / driver)."""


def kill_process_tree(proc: subprocess.Popen, *, drain_timeout: float = 30.0) -> None:
    """SIGKILL a timed-out child *and every descendant it spawned*.

    Only meaningful for children started with ``start_new_session=True``: they
    lead their own process group, so one ``killpg`` reaps the whole subtree
    (``ale_run`` -> ``codex`` -> the ``bwrap`` sandbox it forks per tool call).
    Killing just the direct child instead strands those descendants on PID 1,
    where they pile up as zombies unless PID 1 is a real init.

    The drain afterwards is bounded: descendants inherit the stdout/stderr pipe
    write ends, so an unbounded ``communicate()`` can block forever even after
    the child is dead.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=drain_timeout)
    except subprocess.TimeoutExpired:
        logger.warning("kill_process_tree: pipes still open after SIGKILL (pid=%s)", proc.pid)
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass


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
    return _run_driver(task, mode="grade", split=split)
