"""Docker-sandbox grading for ALE tasks whose scorer runs in the box.

The file-only grader (:mod:`_ale_grader_driver`) grades *read-only* ALE tasks by
running each task's ``evaluate()`` against a local filesystem shim. Tasks whose
grader executes commands in the sandbox (``session.run_command`` / ``.interface``)
can't be graded that way and surface as ``needs_sandbox``.

When the local ALE Docker provider is available — the ~105 GB
``agentslastexam/ale-ubuntu22-docker`` image is present AND the ALE uv venv is
synced — this module grades those tasks for the **Linux docker-supported subset**
(``selected_tasks/docker_support.txt``, 99 tasks). It drives the real ``ale_run``
harness against ``configs/environments/docker.yaml`` with a Bring-Your-Own-Agent
deployer (:mod:`ale_byoa_agent.deployer`) that stages the client's submitted
artifact into the task's in-sandbox output directory before the task's own scorer
runs. The unit's ``[0, 1]`` score is read back from ``eval_result.json``.

This is the same provider/scorer path ``evovle_agents`` uses; the only twist is
the no-LLM agent that injects a pre-made submission instead of solving the task.
Linux-only (the image is Linux); Windows tasks stay ``needs_sandbox``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import ale_grader
from .ale_verifier_capture import SIDECAR_ENV, merge_sidecar

logger = logging.getLogger("eval_service.ale.docker")

ALE_ROOT = ale_grader.ALE_ROOT
# The dedicated PYTHONPATH root that holds *only* the ``ale_byoa_agent`` package
# (so importing it inside the ALE venv can't shadow a stdlib / task module).
_AGENT_PKG_ROOT = (Path(__file__).resolve().parent / "_ale_agent")
_ALE_OVERLAY_LAUNCHER = Path(__file__).resolve().parent / "_ale_runtime_overlay" / "launch.py"
_AGENT_CLASS = "ale_byoa_agent.deployer.ByoaDeployer"
_DOCKER_ENV_CONFIG = ALE_ROOT / "configs" / "environments" / "docker.yaml"
_DOCKER_SUPPORT_FILE = ALE_ROOT / "selected_tasks" / "docker_support.txt"
_DOCKER_IMAGE_TAG = "ale-ubuntu22-docker"
_DOCKER_IMAGE_REF = "agentslastexam/ale-ubuntu22-docker:latest"

# --- Extended tasks (excluded from docker_support.txt) -------------------------
# These need a *privileged* sandbox: the DinD set runs an inner dockerd (their
# inner images are baked into the sandbox image), the Apptainer set runs GUI
# .simg bundles via the baked-in apptainer/singularity. All ship their data +
# reference locally. Gated behind EVAL_SERVICE_ALE_DOCKER_DIND (auto/1/0) because
# `--privileged` has host-security implications (relevant for a public instance).
_DIND_TASKS = frozenset({
    "engineering/openroad_sky130_ibex_pnr_signoff",
    "computing_math/k8s_migration_1",
    "business_finance/bpmn_supply_disruption_l3",
    "business_finance/bpmn_category_governance_restructuring_l3",
})
_APPTAINER_TASKS = frozenset({
    "health_medicine/scene3_skullstrip_qc",
    "psychology_neuro/scene2_resample",
})
_PRIVILEGED_TASKS = _DIND_TASKS | _APPTAINER_TASKS
# auto (default) -> enable the extended set iff a one-time privileged-container
# probe succeeds; 1/true -> force on (trust the operator, skip the probe);
# 0/false -> keep them off (they stay HTTP 501).
_DIND_MODE = os.environ.get("EVAL_SERVICE_ALE_DOCKER_DIND", "auto").strip().lower()

# auto (default) -> enable iff the sandbox image is present; 1/true -> force on
# (still needs the image at grade time); 0/false/no -> disabled.
_MODE = os.environ.get("EVAL_SERVICE_ALE_DOCKER", "auto").strip().lower()
# Per-grade ceiling: container boot (~1-2 min for cua-ready) + task setup + the
# in-sandbox scorer. Generous by default; override for slow scorers.
# Deliberately *below* ale_grader.ale_run_outer_timeout(_AGENT_WALL_S), unlike
# the codex path: /grade answers a synchronous request, so a client waiting out
# ale_run's multi-hour worst case is a worse outcome than cutting the grade
# short. That trade is only safe because reap_containers() cleans up after the
# kill -- raise this if slow scorers start timing out, not to avoid leaks.
GRADE_TIMEOUT_SEC = int(os.environ.get("EVAL_SERVICE_ALE_DOCKER_TIMEOUT_SEC", "2400"))
# Wall budget handed to ale_run for the (no-LLM) agent phase. The eval phase has
# its own ceiling inside ale_run, so this only bounds our fast staging step.
_AGENT_WALL_S = int(os.environ.get("EVAL_SERVICE_ALE_DOCKER_AGENT_WALL_SEC", "600"))
# How ale_run reaches the sandbox cua-server (see grade_via_docker for details).
_ENDPOINT_MODE = os.environ.get(
    "EVAL_SERVICE_ALE_DOCKER_ENDPOINT_MODE", "container_ip"
).strip()


# --------------------------------------------------------------------------- #
# Availability                                                                #
# --------------------------------------------------------------------------- #
# Cache the (cheap-ish) ``docker images`` probe with a short TTL so a long-lived
# service picks up the image within ~TTL of a pull finishing (without a restart)
# while not shelling out to docker on every create/grade call.
_IMAGE_CACHE_TTL_S = int(os.environ.get("EVAL_SERVICE_ALE_DOCKER_IMAGE_TTL_SEC", "60"))
_image_cache: dict[str, Any] = {"present": False, "checked_at": 0.0}


def image_present() -> bool:
    """True if the ALE sandbox image is pulled locally (TTL-cached probe)."""
    now = time.monotonic()
    if now - _image_cache["checked_at"] < _IMAGE_CACHE_TTL_S and _image_cache["checked_at"]:
        return bool(_image_cache["present"])
    present = False
    try:
        out = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, text=True, timeout=30,
        )
        present = _DOCKER_IMAGE_TAG in (out.stdout or "")
    except Exception as e:  # noqa: BLE001 - docker missing / not responsive
        logger.debug("docker images probe failed: %s", e)
    _image_cache.update(present=present, checked_at=now)
    return present


@lru_cache(maxsize=1)
def docker_support_tasks() -> frozenset[str]:
    """The ``domain/task`` ids the Docker provider supports (Linux subset)."""
    if not _DOCKER_SUPPORT_FILE.is_file():
        return frozenset()
    ids: set[str] = set()
    for raw in _DOCKER_SUPPORT_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.add(line.strip("/"))
    return frozenset(ids)


_dind_cache: dict[str, Any] = {"ok": None}


def dind_available() -> bool:
    """True iff the extended (privileged DinD / Apptainer) tasks can be graded
    here. Cached for the process. In ``auto`` mode this probes once that the host
    actually permits a ``--privileged`` container (the capability those tasks
    need); ``1`` trusts the operator without probing; ``0`` disables."""
    if _DIND_MODE in ("0", "false", "no", "off"):
        return False
    if _dind_cache["ok"] is not None:
        return bool(_dind_cache["ok"])
    ok = False
    if _DIND_MODE in ("1", "true", "yes", "on"):
        ok = True
    elif image_present():  # auto: probe a throwaway privileged container once
        try:
            probe = subprocess.run(
                ["docker", "run", "--privileged", "--rm", "--entrypoint", "sh",
                 _DOCKER_IMAGE_REF, "-c", "echo dind-ok"],
                capture_output=True, text=True, timeout=90,
            )
            ok = probe.returncode == 0 and "dind-ok" in (probe.stdout or "")
            if not ok:
                logger.info("privileged probe failed rc=%s: %s",
                            probe.returncode, (probe.stderr or "")[:200])
        except Exception as e:  # noqa: BLE001 - docker missing / not permitted
            logger.info("privileged probe error: %s", e)
    _dind_cache["ok"] = ok
    return ok


def enabled() -> bool:
    """Whether docker-backed grading is turned on for this process."""
    if _MODE in ("0", "false", "no", "off"):
        return False
    if ale_grader.venv_python() is None:
        return False
    if _MODE in ("1", "true", "yes", "on"):
        return True
    # auto
    return image_present()


def can_grade(domain: str, task: str) -> bool:
    """True iff a docker-backed grade can be attempted for ``domain/task`` now."""
    if not enabled():
        return False
    if not image_present():
        return False
    dt = f"{domain}/{task}"
    if dt in docker_support_tasks():
        return True
    # Extended tasks: need a privileged sandbox (DinD / Apptainer).
    return dt in _PRIVILEGED_TASKS and dind_available()


def availability() -> dict[str, Any]:
    """Diagnostics for ``/v1/health`` and logs."""
    img = image_present()
    dind = dind_available() if img else False
    return {
        "mode": _MODE,
        "venv": ale_grader.venv_python() is not None,
        "image_present": img,
        "enabled": enabled(),
        "n_docker_support": len(docker_support_tasks()),
        "dind_mode": _DIND_MODE,
        "dind_available": dind,
        # extended tasks gradable only when dind_available
        "n_extended": len(_PRIVILEGED_TASKS) if dind else 0,
    }


# --------------------------------------------------------------------------- #
# Experiment generation + run                                                 #
# --------------------------------------------------------------------------- #
def _yaml_quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_privileged_env(exp_dir: Path, *, dind: bool) -> Path:
    """Copy ``docker.yaml``, injecting ``privileged: true`` (and, for the DinD
    set, ``enable_dind: true``) as the first key of every provider ``docker:``
    block. Returns the new env-config path. Pure text edit — no yaml dep in the
    service process; safe because we control docker.yaml's shape."""
    out: list[str] = []
    for line in _DOCKER_ENV_CONFIG.read_text(encoding="utf-8").splitlines(keepends=True):
        out.append(line)
        if line.strip() == "docker:":
            child = " " * (len(line) - len(line.lstrip()) + 2)
            out.append(f"{child}privileged: true\n")
            if dind:
                out.append(f"{child}enable_dind: true\n")
    dst = exp_dir / (f"env_docker_privileged{'_dind' if dind else ''}.yaml")
    dst.write_text("".join(out), encoding="utf-8")
    return dst


def _write_experiment(task: ale_grader.AleTask, *, output_dir_override: str = "") -> tuple[Path, Path]:
    """Write the one-task ale_run experiment (+ agent + tasks yaml). Returns
    ``(experiment_yaml, output_root)``."""
    exp_dir = task.session_root / "_docker_grade"
    exp_dir.mkdir(parents=True, exist_ok=True)
    out_root = exp_dir / "_runs"

    agent_yaml = exp_dir / "agent_byoa.yaml"
    agent_yaml.write_text(
        f"class: {_AGENT_CLASS}\n"
        "id: byoa\n"
        "executor: local\n"
        "model: byoa\n"
        "config:\n"
        f"  submission_dir: {_yaml_quote(str(task.output_dir))}\n"
        f"  output_dir_override: {_yaml_quote(output_dir_override)}\n"
        "  connect_timeout_s: 180\n",
        encoding="utf-8",
    )

    tasks_yaml = exp_dir / "tasks_byoa.yaml"
    tasks_yaml.write_text(
        "# auto-generated by eval_service.ale_docker_grader\n"
        f'- path: {_yaml_quote(f"{task.domain}/{task.task}")}\n'
        "  variants: [0]\n",
        encoding="utf-8",
    )

    secret = ALE_ROOT / "secret" / ".env"
    secret_line = f"secret_file: {_yaml_quote(str(secret))}\n" if secret.is_file() else ""

    # Extended tasks need a privileged sandbox (DinD / Apptainer); everyone else
    # uses the stock (unprivileged) docker env config.
    dt = f"{task.domain}/{task.task}"
    if dt in _PRIVILEGED_TASKS and dind_available():
        env_cfg = _write_privileged_env(exp_dir, dind=(dt in _DIND_TASKS))
    else:
        env_cfg = _DOCKER_ENV_CONFIG

    exp_yaml = exp_dir / "experiment_byoa.yaml"
    exp_yaml.write_text(
        f"name: eval_service_byoa_{task.domain}__{task.task}\n"
        f"{secret_line}"
        f"agents:\n  - {_yaml_quote(str(agent_yaml))}\n"
        f"environment: {_yaml_quote(str(env_cfg))}\n"
        f"tasks: {_yaml_quote(str(tasks_yaml))}\n"
        "output:\n"
        f"  root: {_yaml_quote(str(out_root))}\n"
        "concurrency: 1\n"
        f"wall_time_s: {_AGENT_WALL_S}\n"
        "cleanup_mode: delete\n",
        encoding="utf-8",
    )
    return exp_yaml, out_root


def _unit_files(
    out_root: Path, task_id: str, name: str, variant_index: int = 0,
) -> list[Path]:
    """Files called ``name`` for one ale_run unit, newest first.

    ale_run writes results under ``<root>/<exp>/<agent>/<model>/<slug>/v<i>/<ts>/``;
    we match the task slug (or bare leaf) plus the ``v<i>`` segment. Shared so the
    score, the usage record and the spawn tally all resolve to the SAME run.
    """
    if not out_root.is_dir():
        return []
    slug = task_id.strip("/").replace("/", "__")
    leaf = task_id.rstrip("/").split("/")[-1]
    vseg = f"v{variant_index}"

    def _matches(p: Path) -> bool:
        parts = p.parts
        return (slug in parts or leaf in parts) and vseg in parts

    return sorted(
        (p for p in out_root.rglob(name) if _matches(p)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )


def _oi(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) else None


def _of(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def find_unit_usage(
    out_root: Path, task_id: str, variant_index: int = 0,
) -> dict[str, Any]:
    """Token / cost / step telemetry for one finished ale_run unit.

    ale_run aggregates the sandbox agent's usage into ``run.json`` ON THE HOST, so
    this is available server-side even though the Codex rollout itself stays inside
    the sandbox. Returns ``{}`` when no run is found, so callers can report None
    (not measured) rather than a misleading zero.
    """
    for rj in _unit_files(out_root, task_id, "run.json", variant_index):
        try:
            d = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - unreadable -> try the next-newest run
            continue
        if not isinstance(d, dict):
            continue
        u = d.get("usage") if isinstance(d.get("usage"), dict) else {}
        t = d.get("timings") if isinstance(d.get("timings"), dict) else {}
        tin, tout = _oi(u.get("total_input_tokens")), _oi(u.get("total_output_tokens"))
        total = _oi(u.get("total_tokens"))
        if total is None and (tin is not None or tout is not None):
            total = (tin or 0) + (tout or 0)
        dur = _of(t.get("duration_s"))
        if dur is None and isinstance(u.get("total_duration_ms"), (int, float)):
            dur = u["total_duration_ms"] / 1000.0
        return {
            "total_tokens": total,
            "input_tokens": tin,
            "output_tokens": tout,
            "cache_read_tokens": _oi(u.get("total_cache_read_tokens")),
            "cost_usd": _of(u.get("total_cost_usd")),
            "n_steps": _oi(u.get("total_steps")),
            "duration_s": dur,
        }
    return {}


def find_unit_spawns(
    out_root: Path, task_id: str, variant_index: int = 0,
) -> int | None:
    """Specialists the orchestrator actually spawned for one ale_run unit.

    The in-sandbox stager tallies this into ``subagent_spawns.json`` before
    teardown and ale_run gathers it under the unit's ``origin_log/``. None when
    absent (single-agent runs, or staging disabled), which keeps "not reported"
    distinct from "spawned nothing".
    """
    for sm in _unit_files(out_root, task_id, "subagent_spawns.json", variant_index):
        try:
            return _oi(json.loads(sm.read_text(encoding="utf-8")).get("n_subagent_spawns"))
        except Exception:  # noqa: BLE001 - unreadable -> try the next-newest run
            continue
    return None


def _find_unit_grade(
    out_root: Path, task_id: str, variant_index: int = 0,
) -> dict[str, Any] | None:
    """Read the unit's aggregate and component scores from ``eval_result.json``.

    ale_run writes results under ``<root>/<exp>/<agent>/<model>/<slug>/v<i>/<ts>/``;
    we match the task slug + ``v<i>`` segment and take the newest run. Newer
    runners preserve ``raw_scores`` and named ``per_verifier`` records; older
    artifacts remain compatible through their aggregate ``score``. Falls back
    to ``run.json`` when needed.
    """
    def _newest(name: str) -> list[Path]:
        return _unit_files(out_root, task_id, name, variant_index)

    for ev in _newest("eval_result.json"):
        try:
            rec = json.loads(ev.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        s = rec.get("score")
        if isinstance(s, (int, float)):
            result = dict(rec)
            result["score"] = float(s)
            if not isinstance(result.get("raw_scores"), list):
                result["raw_scores"] = [float(s)]
            return result
    for rj in _newest("run.json"):
        try:
            rec = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for key in ("score", "final_score"):
            if isinstance(rec.get(key), (int, float)):
                score = float(rec[key])
                return {"score": score, "raw_scores": [score]}
    return None


def _find_unit_score(out_root: Path, task_id: str, variant_index: int = 0) -> float | None:
    """Backward-compatible aggregate-only view of :func:`_find_unit_grade`."""
    result = _find_unit_grade(out_root, task_id, variant_index)
    return float(result["score"]) if result is not None else None


def grade_via_docker(
    task: ale_grader.AleTask, *, output_dir_override: str = "",
) -> dict[str, Any]:
    """Grade a submitted artifact by driving ale_run + the Docker provider.

    Returns ``{"ok": True, "score": float}`` on success, else
    ``{"ok": False, "reason": str}`` (never raises for an expected failure).
    """
    venv = ale_grader.venv_python()
    if venv is None:
        return {"ok": False, "reason": "ALE venv not found; cannot drive ale_run"}
    if not image_present():
        return {
            "ok": False,
            "reason": (
                "ALE sandbox image 'agentslastexam/ale-ubuntu22-docker' not present "
                "(docker pull is ~105 GB; see docs/local-docker)"
            ),
        }
    if not _DOCKER_ENV_CONFIG.is_file():
        return {"ok": False, "reason": f"docker env config missing: {_DOCKER_ENV_CONFIG}"}

    exp_yaml, out_root = _write_experiment(task, output_dir_override=output_dir_override)

    env = dict(os.environ)
    verifier_sidecar = exp_yaml.parent / "verifier_result.json"
    try:
        verifier_sidecar.unlink()
    except FileNotFoundError:
        pass
    env[SIDECAR_ENV] = str(verifier_sidecar)
    env["EVAL_SERVICE_ALE_ROOT"] = str(ALE_ROOT)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(_AGENT_PKG_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
    )
    # How ale_run's docker provider should reach the sandbox cua-server.
    # "container_ip" (default): connect to the container's bridge IP on the
    # internal cua port. Required where docker's published ports don't bind a
    # reachable listener on the daemon localhost (rootless docker / dockerd in a
    # separate netns — this deployment) and also works on native Linux docker.
    # Set EVAL_SERVICE_ALE_DOCKER_ENDPOINT_MODE="" on Docker Desktop (mac/win),
    # where the host reaches containers only via localhost:<published-port>.
    env["ALE_DOCKER_ENDPOINT_MODE"] = _ENDPOINT_MODE
    cmd = [str(venv), str(_ALE_OVERLAY_LAUNCHER), "run", str(exp_yaml)]
    logger.info(
        "docker-grade %s/%s: %s (timeout=%ds)",
        task.domain, task.task, " ".join(cmd), GRADE_TIMEOUT_SEC,
    )
    # Sampled before launch so the reaper can tell our sandbox container from one
    # a concurrent grade of the same task already owns.
    containers_before = ale_grader.list_containers(
        ale_grader.container_prefix(task.domain, task.task)
    )
    # start_new_session: own process group, so a timeout kills ale_run *and* the
    # sandbox descendants it spawned instead of orphaning them onto PID 1.
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ALE_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        try:
            verifier_sidecar.unlink()
        except OSError:
            pass
        return {"ok": False, "reason": f"ale_run launch failed: {type(e).__name__}: {e}"}
    out = err = ""
    timed_out = False
    try:
        out, err = proc.communicate(timeout=GRADE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        timed_out = True
        out, err = ale_grader.kill_process_tree(proc)
    finally:
        # A grade that exited cleanly already deleted its container and this is a
        # no-op; it is here to catch the ones that didn't, however they ended.
        ale_grader.reap_containers(
            task.domain, task.task, f"{out}\n{err}", containers_before,
        )
    if timed_out:
        try:
            verifier_sidecar.unlink()
        except OSError:
            pass
        return {
            "ok": False,
            "reason": f"docker grade exceeded {GRADE_TIMEOUT_SEC}s (sandbox boot + scorer)",
        }

    task_id = f"{task.domain}/{task.task}"
    grade = _find_unit_grade(out_root, task_id, variant_index=0)
    if grade is None:
        try:
            verifier_sidecar.unlink()
        except OSError:
            pass
        tail = "\n".join((err or out or "").strip().splitlines()[-6:])
        return {
            "ok": False,
            "reason": (
                f"docker grade produced no score (rc={proc.returncode}). "
                f"Tail: {tail[-800:]}"
            ),
        }
    grade = merge_sidecar(grade, verifier_sidecar, task_id)
    return {"ok": True, **grade}
