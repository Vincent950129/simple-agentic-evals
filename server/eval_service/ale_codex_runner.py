"""Server-side ALE execution via the reference Codex CLI agent.

ALE tasks run inside the ~105 GB ALE Docker sandbox: ``ale_run`` provisions the
sandbox from the task definition, runs the configured agent to *solve* the task,
then grades with the task's own scorer -- all in one invocation. This module
drives that path with ALE's stock Codex agent (``configs/agents/codex_direct.yaml``
for the skills/tools tracks, ``codex_agents.yaml`` for the multi-agent track),
overriding the model and injecting the caller's ``OPENAI_API_KEY``. The unit's
``[0, 1]`` score is read back from ``eval_result.json`` (same reader as
:mod:`ale_docker_grader`).

This is the ``acp_codex_agent`` path for ``benchmark=ale``. There is no ReAct/EOG
equivalent: ALE requires a CLI agent harness, so ``agent=react`` on an ALE task is
rejected upstream (see ``service._resolve_agent_kind``).

Availability mirrors docker grading: the sandbox image must be pulled and the ALE
uv venv synced (gated by :func:`ale_docker_grader.can_grade`). Linux-only.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

from . import ale_docker_grader, ale_grader

logger = logging.getLogger("eval_service.ale.codex")

ALE_ROOT = ale_grader.ALE_ROOT
_AGENTS_DIR = ALE_ROOT / "configs" / "agents"
_DIRECT_CONFIG = _AGENTS_DIR / "codex_direct.yaml"      # skills/tools: direct OpenAI
_AGENTS_CONFIG = _AGENTS_DIR / "codex_agents.yaml"      # agents: multi-agent V2 router

# Total wall-clock ceiling for one ALE Codex run (sandbox boot + agent solve +
# in-sandbox scorer). Reference parity: ale_run gives the agent phase 7200s
# (_AGENT_WALL_S below) plus its own eval ceiling, so this end-to-end fallback adds
# headroom for boot + grading on top. Callers normally pass an explicit timeout_s.
RUN_TIMEOUT_SEC = int(os.environ.get("EVAL_SERVICE_ALE_CODEX_TIMEOUT_SEC", "9000"))
# Wall budget handed to ale_run for the agent phase (the eval phase has its own
# ceiling inside ale_run). Matches the reference default (ale_run
# lifecycle._DEFAULT_TIMEOUT_S == example_exp.yaml wall_time_s == 7200s).
_AGENT_WALL_S = int(os.environ.get("EVAL_SERVICE_ALE_CODEX_AGENT_WALL_SEC", "7200"))


def availability() -> dict[str, Any]:
    """Diagnostics (delegates to the shared docker-sandbox probe)."""
    return ale_docker_grader.availability()


def can_run(domain: str, task: str) -> bool:
    """True iff an ALE Codex run can be attempted for ``domain/task`` now.

    Same prerequisites as docker grading: the sandbox image is present, the ALE
    venv is synced, and the task is in the Linux docker-supported subset.
    """
    return ale_docker_grader.can_grade(domain, task)


def _yaml_quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Env prefixes ale_run forwards into the sandbox that a caller may legitimately
# set: the agents track's specialist-pool bundle (unpacked by the in-sandbox
# CodexAgentsStager), the skills track's skill-library bundle (unpacked by the
# in-sandbox CodexSkillsStager -- ``ale_run/agents/codex/_ale_skills.py``), and the
# hard software fence. Anything else is dropped -- this is a remote caller writing
# into a subprocess environment, so it stays a strict allowlist rather than a
# denylist.
#
# ``ALE_SKILLS_`` matters as much as ``ALE_AGENTS_``: ale_run's env-passthrough and
# the in-sandbox stager both already handle it (that is how the LOCAL skills path
# stages its library), so dropping it here was the single thing that made a hosted
# oracle-skill cell indistinguishable from a no-skill one -- same empty
# ``~/.codex/skills/``, so the axis the experiment varies silently did not vary.
_SANDBOX_ENV_PREFIXES = ("ALE_AGENTS_", "ALE_SKILLS_", "ALE_GUARD_")


def filter_sandbox_env(env: dict[str, str] | None) -> dict[str, str]:
    """Keep only the sandbox env keys a caller is allowed to set (see
    :data:`_SANDBOX_ENV_PREFIXES`); values are coerced to ``str``."""
    if not env:
        return {}
    out: dict[str, str] = {}
    for k, v in env.items():
        key = str(k)
        if key.startswith(_SANDBOX_ENV_PREFIXES) and v is not None:
            out[key] = str(v)
        else:
            logger.warning("ignoring sandbox_env key %r (not in the ALE allowlist)", key)
    return out


# Rebuildable scaffolding an agent may leave beside its deliverable: interpreter
# environments and package/build caches. Matched by directory NAME at any depth, so
# a nested copy is pruned too. Kept out of the artifact tar (see
# build_run_artifacts_tar) -- the answer is the evidence, its toolchain is not.
# Both uv spellings appear in practice (``.uv_cache`` here, ``.uv-cache`` on other
# runs), so list each. Deliberately NOT matching a bare ``env``/``cache``: those are
# plausible names for real task data, and dropping a deliverable from the evidence
# tree to save bytes would be a far worse bug than shipping one.
_ARTIFACT_SKIP_DIRS = frozenset({
    ".agent_runtime_env", ".agent-runtime-env", ".venv", "venv",
    ".uv_cache", ".uv-cache", ".cache", ".npm", "node_modules",
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


def build_run_artifacts_tar(work_dir: Path, dest: Path) -> Path:
    """Pack one ALE unit's ale_run tree into ``dest`` (tar.gz), arcnamed into the
    layout a LOCAL cell uses, so the client can extract it at its cell root and end
    up with the same tree a local run produces:

      ``<work>/_runs/...``  ->  ``_ale_runs/g0/...``   (run.json, eval_result.json,
                                trajectory.json, events.jsonl, origin_log/, output/)
      ``<work>/*.yaml``     ->  ``_ale_experiments/...`` (the generated experiment,
                                task list and agent config)

    ``g0`` is hardcoded because the service runs exactly one task per experiment,
    which is the local path's single-tool-group case.

    Directories named in :data:`_ARTIFACT_SKIP_DIRS` are left out: an agent that
    installs its own dependencies leaves a venv and a wheel cache next to the
    deliverable it was asked to produce (``.agent_runtime_env`` + ``.uv_cache`` came
    to ~190 MB / 4.9k files for a 20 KB answer), and shipping those to every client
    would dwarf the evidence they came with while adding nothing that isn't
    reproducible from the task definition.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = skipped = 0
    with tarfile.open(dest, "w:gz") as tar:
        for p in sorted(work_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(work_dir)
            parts = rel.parts
            if _ARTIFACT_SKIP_DIRS.intersection(parts[:-1]):
                skipped += 1
                continue
            if parts[0] == "_runs":
                arc = Path("_ale_runs", "g0", *parts[1:])
            else:
                arc = Path("_ale_experiments", *parts)
            tar.add(str(p), arcname=str(arc))
            n += 1
    logger.info("packed %d ALE artifact file(s) from %s -> %s (%.1f KiB)%s",
                n, work_dir, dest.name, dest.stat().st_size / 1024,
                f"; skipped {skipped} dependency/cache file(s)" if skipped else "")
    return dest


def _write_agent_config(exp_dir: Path, *, model: str | None, multi_agent: bool) -> Path:
    """Copy the stock Codex agent config, overriding the model when given.

    Both stock configs use ``provider: direct`` (OpenAI via ``OPENAI_API_KEY``)
    and carry a top-level ``model:`` line; we rewrite that line so the caller's
    model is used, otherwise the file is copied verbatim (config default).
    """
    src = _AGENTS_CONFIG if multi_agent else _DIRECT_CONFIG
    text = src.read_text(encoding="utf-8")
    if model:
        out: list[str] = []
        replaced = False
        for line in text.splitlines():
            if not replaced and re.match(r"^model:\s", line):
                out.append(f"model: {model}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.insert(0, f"model: {model}")
        text = "\n".join(out) + "\n"
    dst = exp_dir / src.name
    dst.write_text(text, encoding="utf-8")
    return dst


def _indent_block(text: str, pad: str = "    ") -> str:
    """Indent ``text`` for a YAML ``|`` literal block (blank lines stay blank)."""
    return "\n".join((pad + ln) if ln.strip() else "" for ln in text.splitlines())


def _write_experiment(
    task: ale_grader.AleTask, agent_yaml: Path, *, exp_dir: Path,
    prompt_suffix: str = "",
) -> tuple[Path, Path]:
    """Write the one-task ale_run experiment (+ task list). Returns
    ``(experiment_yaml, output_root)``.

    ``prompt_suffix`` is appended to the task prompt by ale_run. On the agents
    track it carries the software allowlist plus the orchestrator's delegation
    protocol and specialist roster -- with ``codex_agents.yaml`` the root agent is
    tool-less, so WITHOUT that block it cannot delegate and solves nothing. The
    caller supplies it so the hosted run gets the same prompt as a local one.
    """
    out_root = exp_dir / "_runs"

    tasks_yaml = exp_dir / "tasks_codex.yaml"
    tasks_yaml.write_text(
        "# auto-generated by eval_service.ale_codex_runner\n"
        f'- path: {_yaml_quote(f"{task.domain}/{task.task}")}\n'
        "  variants: [0]\n",
        encoding="utf-8",
    )

    secret = ALE_ROOT / "secret" / ".env"
    secret_line = f"secret_file: {_yaml_quote(str(secret))}\n" if secret.is_file() else ""

    # Extended tasks need a privileged sandbox (DinD / Apptainer); everyone else
    # uses the stock (unprivileged) docker env config. Reuse the docker grader's
    # privileged-env writer so the two paths stay in lockstep.
    dt = f"{task.domain}/{task.task}"
    if dt in ale_docker_grader._PRIVILEGED_TASKS and ale_docker_grader.dind_available():
        env_cfg = ale_docker_grader._write_privileged_env(
            exp_dir, dind=(dt in ale_docker_grader._DIND_TASKS))
    else:
        env_cfg = ale_docker_grader._DOCKER_ENV_CONFIG

    suffix_line = (
        f"prompt_suffix: |\n{_indent_block(prompt_suffix)}\n" if prompt_suffix.strip() else ""
    )
    exp_yaml = exp_dir / "experiment_codex.yaml"
    exp_yaml.write_text(
        f"name: eval_service_codex_{task.domain}__{task.task}\n"
        f"{secret_line}"
        f"agents:\n  - {_yaml_quote(str(agent_yaml))}\n"
        f"environment: {_yaml_quote(str(env_cfg))}\n"
        f"tasks: {_yaml_quote(str(tasks_yaml))}\n"
        "output:\n"
        f"  root: {_yaml_quote(str(out_root))}\n"
        "concurrency: 1\n"
        f"wall_time_s: {_AGENT_WALL_S}\n"
        "cleanup_mode: delete\n"
        f"{suffix_line}",
        encoding="utf-8",
    )
    return exp_yaml, out_root


def run_codex_ale(
    task: ale_grader.AleTask,
    *,
    model: str | None = None,
    openai_api_key: str | None = None,
    multi_agent: bool = False,
    timeout_s: float | None = None,
    prompt_suffix: str = "",
    sandbox_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Solve + grade an ALE task by driving ale_run with the stock Codex agent.

    Returns ``{"ok": True, "score": float, "latency_s": float, "usage": {...},
    "n_subagent_spawns": int | None}`` on success, else
    ``{"ok": False, "reason": str, ...}`` (never raises for an expected failure).
    Blocking (shells out to ale_run/docker); call from a worker thread.
    """
    venv = ale_grader.venv_python()
    if venv is None:
        return {"ok": False, "reason": "ALE venv not found; cannot drive ale_run"}
    if not ale_docker_grader.image_present():
        return {
            "ok": False,
            "reason": (
                "ALE sandbox image 'agentslastexam/ale-ubuntu22-docker' not present "
                "(docker pull is ~105 GB; see docs/local-docker)"
            ),
        }
    src_cfg = _AGENTS_CONFIG if multi_agent else _DIRECT_CONFIG
    if not src_cfg.is_file():
        return {"ok": False, "reason": f"Codex agent config missing: {src_cfg}"}

    exp_dir = task.session_root / "_codex_run"
    exp_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml = _write_agent_config(exp_dir, model=model, multi_agent=multi_agent)
    exp_yaml, out_root = _write_experiment(
        task, agent_yaml, exp_dir=exp_dir, prompt_suffix=prompt_suffix)

    env = dict(os.environ)
    if openai_api_key:
        env["OPENAI_API_KEY"] = openai_api_key
    # ale_run's lifecycle passes these through to the sandbox: the specialist pool
    # the stager unpacks into ~/.codex, and the hard software fence. Already
    # allowlisted by filter_sandbox_env.
    staged_env = filter_sandbox_env(sandbox_env)
    env.update(staged_env)
    # How ale_run's docker provider should reach the sandbox cua-server (see
    # ale_docker_grader.grade_via_docker for the rationale).
    env["ALE_DOCKER_ENDPOINT_MODE"] = ale_docker_grader._ENDPOINT_MODE

    cmd = [str(venv), "-m", "ale_run", "run", str(exp_yaml)]
    budget = int(timeout_s or RUN_TIMEOUT_SEC)
    dt = f"{task.domain}/{task.task}"
    logger.info(
        "ale-codex %s: %s (timeout=%ds model=%s multi_agent=%s pool=%s guard=%s)",
        dt, " ".join(cmd), budget, model or "(config default)", multi_agent,
        "yes" if "ALE_AGENTS_BUNDLE_B64" in staged_env else "NO",
        "yes" if any(k.startswith("ALE_GUARD_") for k in staged_env) else "NO",
    )
    started = time.monotonic()
    # start_new_session: own process group, so a timeout kills ale_run *and* the
    # codex/bwrap descendants it spawned instead of orphaning them onto PID 1.
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ALE_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ale_run launch failed: {type(e).__name__}: {e}"}
    try:
        out, err = proc.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        ale_grader.kill_process_tree(proc)
        return {
            "ok": False,
            "reason": f"ale-codex run exceeded {budget}s (sandbox boot + agent + scorer)",
            "latency_s": round(time.monotonic() - started, 3),
        }
    latency_s = round(time.monotonic() - started, 3)

    score = ale_docker_grader._find_unit_score(out_root, dt, variant_index=0)
    if score is None:
        tail = "\n".join((err or out or "").strip().splitlines()[-8:])
        return {
            "ok": False,
            "reason": (
                f"ale-codex run produced no score (rc={proc.returncode}). "
                f"Tail: {tail[-800:]}"
            ),
            "latency_s": latency_s,
        }
    # ale_run aggregates the sandbox agent's token/cost usage into run.json on
    # THIS host, and the stager's spawn tally lands under the unit's origin_log,
    # so both are readable here even though the Codex rollout stays in the sandbox.
    usage = ale_docker_grader.find_unit_usage(out_root, dt, variant_index=0)
    spawns = ale_docker_grader.find_unit_spawns(out_root, dt, variant_index=0)
    return {
        "ok": True, "score": float(score), "latency_s": latency_s,
        "usage": usage, "n_subagent_spawns": spawns,
        # Where the raw tree lives, so GET .../run_artifacts can ship it back
        # before session teardown removes it.
        "work_dir": str(exp_dir),
    }
