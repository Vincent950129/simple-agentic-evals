"""The ALE task set this deployment is allowed to serve.

ALE ships 152 tasks, and on a Linux host only a subset of them can produce a
number. Two independent filters decide which:

* **47 non-Linux tasks.** ALE's suite spans two operating systems and its
  version partition was built from all 152, so a large part of every version
  needs a Windows/macOS VM and cannot run in the Linux sandbox at all. The
  sweep runners drop these *before* a run starts.
* **9 measurement exclusions.** The task's own loader or grader dies on this
  host for a reason that has nothing to do with the agent (a package ALE never
  declares; one grader whose prebuilt venv hardcodes the author's build paths).

Both lists are shipped in ``config/ale_excluded_tasks.json``. Deployments that
publish against another task-set revision can point
``EVAL_SERVICE_ALE_EXCLUSIONS`` at their authoritative manifest.

Serving the unfiltered 152 is the failure this module exists to prevent: a
non-Linux task provisions fine when its data happens to be staged and can even
return a score down the local-``evaluate()`` path, which reads as a real result
from a task no published row counts. So when the manifest cannot be read we
serve **no** ALE tasks and say why in ``/v1/health`` -- an empty benchmark is a
question, whereas a quietly re-based one is a wrong answer. Set
``EVAL_SERVICE_ALE_SERVE_ALL=1`` to serve every row on disk regardless (the
service-side twin of ``main_results_table.py --keep-env-broken``).
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .ale_grader import ALE_ROOT

logger = logging.getLogger("eval_service.ale_manifest")

SCHEMA = 1
MANIFEST_PATH = Path(
    os.environ.get(
        "EVAL_SERVICE_ALE_EXCLUSIONS",
        Path(__file__).resolve().parent / "config" / "ale_excluded_tasks.json",
    )
).resolve()
_SELECTED_DIR = ALE_ROOT / "selected_tasks"
_SERVE_ALL = os.environ.get("EVAL_SERVICE_ALE_SERVE_ALL", "0").strip().lower() in (
    "1", "true", "yes", "on")


class ManifestError(Exception):
    """The exclusion manifest or a selected-tasks list is missing or unusable."""


def _read_list(name: str) -> frozenset[str]:
    """Parse a ``selected_tasks/*.txt`` manifest into ``domain/task`` ids."""
    path = _SELECTED_DIR / name
    if not path.is_file():
        raise ManifestError(f"missing ALE task list {path}")
    ids = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip().strip("/")
        if line:
            ids.add(line)
    if not ids:
        raise ManifestError(f"ALE task list {path} is empty")
    return frozenset(ids)


def _resolve() -> dict[str, Any]:
    doc = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if int(doc.get("schema", 0)) != SCHEMA:
        raise ManifestError(f"schema {doc.get('schema')!r} is not {SCHEMA}")

    excluded = doc.get("excluded")
    if not isinstance(excluded, dict) or not excluded:
        raise ManifestError("'excluded' is absent or empty")
    # Same rule main_results_table.py enforces: an exclusion without a stated
    # cause is a task someone dropped without saying why.
    causeless = sorted(t for t, meta in excluded.items()
                       if not str((meta or {}).get("cause", "")).strip())
    if causeless:
        raise ManifestError(f"exclusions state no cause: {causeless}")

    not_linux = frozenset((doc.get("not_linux") or {}).get("tasks") or ())
    if not not_linux:
        raise ManifestError("'not_linux.tasks' is absent or empty")

    try:
        full = _read_list("full.txt")
        linux = _read_list("linux_only.txt")
        docker = _read_list("docker_support.txt")
    except ManifestError:
        # A source checkout installed from GitHub may validate the service before
        # ALE's large task repository is mounted.  The checked-in verifier
        # registry is the exact 96-task runnable set; combine it with this
        # manifest's 9 exclusions and 47 non-Linux tasks to reconstruct the
        # official 152/105/99 counts.  Once ALE_ROOT is present, its own selected
        # task lists are used and cross-checked instead.
        from .ale_docker_grader import _PRIVILEGED_TASKS
        from .ale_verifier_registry import TASK_SPECS

        runnable_from_registry = frozenset(TASK_SPECS)
        linux = frozenset(runnable_from_registry | frozenset(excluded))
        full = frozenset(linux | not_linux)
        docker = frozenset(linux - _PRIVILEGED_TASKS)
    runnable = frozenset(linux - set(excluded))

    # The manifest documents how its lists derive from the task lists. Check
    # that here so drift shows up as a red row instead of a wrong denominator.
    agrees = {
        "not_linux_is_full_minus_linux": not_linux == frozenset(full - linux),
        "linux_is_docker_plus_6_privileged": len(linux - docker) == 6,
        "exclusions_are_all_runnable_on_linux": frozenset(excluded) <= linux,
    }
    causes = {t: str((meta or {}).get("cause", "")) for t, meta in excluded.items()}
    return {
        "ok": True,
        "reason": None,
        "excluded": frozenset(excluded),
        "not_linux": not_linux,
        "runnable": runnable,
        "n_suite": len(full),
        "n_docker_support": len(docker & runnable),
        "n_privileged": len(linux - docker),
        "causes": causes,
        "agrees": agrees,
        # A deterministic dropped id, so a caller can prove the filter is live.
        "example_filtered": sorted(excluded)[0],
    }


@lru_cache(maxsize=1)
def _state() -> dict[str, Any]:
    """Resolve the served set once per process. Never raises."""
    try:
        state = _resolve()
    except Exception as e:  # noqa: BLE001 - a bad manifest must not kill the app
        logger.error(
            "ALE exclusion manifest unusable (%s: %s) -> serving NO ale tasks. "
            "Fix %s, or set EVAL_SERVICE_ALE_SERVE_ALL=1 to serve every row on disk.",
            type(e).__name__, e, MANIFEST_PATH,
        )
        return {
            "ok": False, "reason": f"{type(e).__name__}: {e}",
            "excluded": frozenset(), "not_linux": frozenset(),
            "runnable": frozenset(), "n_suite": 0, "n_docker_support": 0,
            "n_privileged": 0, "causes": {}, "agrees": {}, "example_filtered": None,
        }
    bad = sorted(k for k, v in state["agrees"].items() if not v)
    logger.info(
        "ALE manifest: %d runnable of %d (%d non-Linux, %d excluded)%s",
        len(state["runnable"]), state["n_suite"], len(state["not_linux"]),
        len(state["excluded"]), f"; DRIFT in {bad}" if bad else "",
    )
    if bad:
        logger.error("ALE manifest disagrees with selected_tasks/: %s", bad)
    return state


def filtering() -> bool:
    """False when the operator asked for every row on disk."""
    return not _SERVE_ALL


def runnable_ids() -> frozenset[str]:
    return _state()["runnable"]


def is_served(task_id: str) -> bool:
    """Whether ``domain/task`` may appear in the catalog and be provisioned."""
    if _SERVE_ALL:
        return True
    return task_id.strip().strip("/") in _state()["runnable"]


def why_not(task_id: str) -> str | None:
    """Why a task id is withheld, phrased for an API error. None if it's served."""
    if is_served(task_id):
        return None
    tid = task_id.strip().strip("/")
    state = _state()
    if not state["ok"]:
        return (f"the ALE task set is unavailable ({state['reason']}); no ALE task "
                f"can be served until {MANIFEST_PATH.name} is readable")
    if tid in state["excluded"]:
        return (f"excluded from every published ALE number on this host "
                f"(cause: {state['causes'].get(tid, 'unknown')}; see "
                f"{MANIFEST_PATH.name})")
    if tid in state["not_linux"]:
        return ("not runnable in the Linux sandbox -- this task needs a non-Linux "
                "VM, so no ALE row counts it")
    return f"not in ALE's Linux task list ({_SELECTED_DIR.name}/linux_only.txt)"


def availability() -> dict[str, Any]:
    """Task-set diagnostics for ``/v1/health``."""
    state = _state()
    return {
        "ok": bool(state["ok"]),
        "reason": state["reason"],
        "manifest": str(MANIFEST_PATH),
        "filtered": filtering(),
        "n_suite": state["n_suite"],
        "n_runnable": len(state["runnable"]),
        "n_docker_support": state["n_docker_support"],
        "n_privileged": state["n_privileged"],
        "n_excluded": len(state["excluded"]),
        "n_not_linux": len(state["not_linux"]),
        "manifest_agrees": all(state["agrees"].values()) if state["agrees"] else False,
        "disagreements": sorted(k for k, v in state["agrees"].items() if not v),
        "example_filtered": state["example_filtered"],
    }
