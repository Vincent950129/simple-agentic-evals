"""Environment backends behind the session API.

Each backend knows how to (a) provision a task into an *action surface* the
agent can act on, (b) grade the resulting state, and (c) tear down. Both return
a uniform :class:`~eval_service.contract.GradeResult` from ``grade``.

``EogEnvironment`` reuses the harness verbatim:

  * ``endpoints.patch_row``          -- rewrite gym URLs to something reachable
  * ``eog_verifier.seed_databases``  -- one fresh per-session DB per gym
  * ``eog_verifier.run_verifiers``   -- SQL state grading
  * ``eog_verifier.teardown_databases``

The MCP action's URL + headers match ``codex_config._render_mcp_entry`` exactly
(so a BYOA agent hits the same DB the verifier reads).

``AleEnvironment`` serves an ALE task's input files, accepts the agent's
submitted artifact, and grades it by running the task's own ``evaluate()``
against a local filesystem session shim under the ALE venv (no 105 GB sandbox);
see ``ale_grader`` + ``_ale_grader_driver``. Tasks whose grader executes
commands in the sandbox raise :class:`GradingNotSupported`.

The second argument to ``create``/``grade``/``teardown`` is an opaque per-backend
*env_state* dict (EOG: ``{gym_name: database_id}``; ALE: workspace info).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

try:  # monorepo deployment
    from evovle_skills.src.dataset import TaskRow
    from evovle_skills.src.endpoints import patch_row
    from evovle_skills.src.eog_verifier import (
        VerificationReport,
        normalize_context,
        run_verifiers,
        seed_databases,
        teardown_databases,
    )
except ImportError:  # standalone GitHub checkout
    from ._harness.dataset import TaskRow
    from ._harness.endpoints import patch_row
    from ._harness.eog_verifier import (
        VerificationReport,
        normalize_context,
        run_verifiers,
        seed_databases,
        teardown_databases,
    )

from . import ale_docker_grader, ale_grader
from .contract import (
    GradeResult,
    McpAction,
    McpServer,
    SandboxAction,
    SandboxFile,
    VerifierView,
)

logger = logging.getLogger("eval_service.environment")


class GradingNotSupported(Exception):
    """Raised when a backend cannot grade this task standalone (e.g. an ALE task
    whose grader needs the full sandbox, or a missing ALE venv)."""


# --------------------------------------------------------------------------- #
# EOG (MCP + SQL verifiers)                                                   #
# --------------------------------------------------------------------------- #
def _mcp_headers(gym: dict[str, Any], database_id: str | None) -> dict[str, str]:
    """Replicate codex_config._render_mcp_entry header construction.

    Order: normalized context -> user-id (from user_info) -> x-database-id.
    """
    headers = normalize_context(gym.get("context"))
    user_info = gym.get("user_info") or {}
    if user_info and not headers.get("user-id") and user_info.get("user_id"):
        headers["user-id"] = str(user_info["user_id"])
    if database_id:
        headers["x-database-id"] = database_id
    return headers


def _mcp_servers(row: TaskRow, db_ids: dict[str, str]) -> list[McpServer]:
    servers: list[McpServer] = []
    endpoint = row.mcp_endpoint or "/mcp"
    for gym in row.gym_servers_config:
        name = gym.get("mcp_server_name", "")
        base = (gym.get("mcp_server_url") or "").rstrip("/")
        url = base + endpoint
        servers.append(
            McpServer(name=name, url=url, headers=_mcp_headers(gym, db_ids.get(name)))
        )
    return servers


class EogEnvironment:
    kind = "eog"

    def create(self, row: TaskRow) -> tuple[dict[str, Any], McpAction]:
        """Seed one DB per gym, return (env_state={gym: db_id}, mcp action).

        Blocking (HTTP seed calls); the service runs it in a worker thread.
        """
        patched = patch_row(row)                       # reachable URLs
        db_ids = seed_databases(patched)               # {gym_name: db_id}
        action = McpAction(mcp_servers=_mcp_servers(patched, db_ids))
        return db_ids, action

    def grade(self, row: TaskRow, env_state: dict[str, Any]) -> GradeResult:
        patched = patch_row(row)
        report = run_verifiers(patched, env_state)
        return report_to_result("", report)

    def teardown(self, row: TaskRow, env_state: dict[str, Any]) -> None:
        try:
            patched = patch_row(row)
            teardown_databases(patched, env_state)
        except Exception as e:  # noqa: BLE001 - teardown is best-effort
            logger.warning("teardown failed for %s: %s", row.task_id, e)


# --------------------------------------------------------------------------- #
# ALE (file sandbox)                                                          #
# --------------------------------------------------------------------------- #
def _input_file_views(row: TaskRow, base_dir) -> list[SandboxFile]:
    """Real input files on disk, enriched with the row's declared metadata."""
    raw = row.raw or {}
    declared: list[dict[str, Any]] = [
        f for f in (raw.get("input_files") or []) if isinstance(f, dict)
    ]

    def _meta_for(rel: str) -> dict[str, Any]:
        for d in declared:
            dp = str(d.get("path", "")).strip("/")
            if dp and (dp == rel or rel.startswith(dp + "/")):
                return d
        return {}

    views: list[SandboxFile] = []
    for entry in ale_grader.list_inputs(base_dir):
        rel = entry["path"]                       # e.g. "input/cases.json"
        meta = _meta_for(rel)
        views.append(SandboxFile(
            name=str(meta.get("name") or rel.split("/")[-1]),
            path=rel,
            format=str(meta.get("format", "")),
            description=str(meta.get("description", "")),
            size=int(entry.get("size", -1)),
        ))
    return views


class AleEnvironment:
    kind = "ale"

    def create(self, row: TaskRow) -> tuple[dict[str, Any], SandboxAction]:
        domain, task, variant = ale_grader.split_task_path(row)
        # Workspace keyed by a fresh id (the service's session_id is assigned
        # after create); env_state carries the paths grade/teardown need.
        ws_id = f"ale_{uuid.uuid4().hex[:16]}"
        staged = ale_grader.stage_workspace(ws_id, domain, task, variant)

        gradable = ale_grader.venv_python() is not None
        if not gradable:
            grading = f"unavailable: ALE grader venv not found at {ale_grader.ALE_ROOT}/.venv"
        elif ale_docker_grader.can_grade(domain, task):
            # Read-only tasks grade locally; run_command tasks fall through to the
            # Docker sandbox. Both are gradable for this Linux docker-supported task.
            grading = "local_evaluate+docker_sandbox"
        else:
            grading = "local_evaluate"
        action = SandboxAction(
            workdir=str(staged.base_dir.name),
            input_files=_input_file_views(row, staged.base_dir),
            output_dir="output",
            output_path=ale_grader.output_path_hint(row.user_prompt or ""),
            gradable=gradable,
            grading=grading,
        )
        env_state: dict[str, Any] = {
            "kind": "ale",
            "domain": domain, "task": task, "variant": variant,
            "split": "train",                      # cua_bench task split (not the data split)
            "session_root": str(staged.session_root),
            "base_dir": str(staged.base_dir),
            "input_dir": str(staged.input_dir),
            "output_dir": str(staged.output_dir),
            "gradable": gradable,
        }
        return env_state, action

    def _task(self, env_state: dict[str, Any]) -> ale_grader.AleTask:
        from pathlib import Path
        return ale_grader.AleTask(
            domain=env_state["domain"], task=env_state["task"],
            variant=env_state["variant"],
            session_root=Path(env_state["session_root"]),
            base_dir=Path(env_state["base_dir"]),
            input_dir=Path(env_state["input_dir"]),
            output_dir=Path(env_state["output_dir"]),
        )

    def grade(self, row: TaskRow, env_state: dict[str, Any]) -> GradeResult:
        task = self._task(env_state)
        try:
            res = ale_grader.grade(task, split=env_state.get("split", "train"))
        except ale_grader.AleUnavailable as e:
            raise GradingNotSupported(str(e))

        grading_path = "local_evaluate"
        if not res.get("ok"):
            # run_command graders need the real sandbox. If the local ALE Docker
            # provider is available, grade the Linux subset for real by driving
            # ale_run + the BYOA stager. Windows tasks stay unsupported (the
            # image is Linux-only — they need a Windows VM).
            if (
                res.get("needs_sandbox")
                and not res.get("windows")
                and ale_docker_grader.can_grade(env_state["domain"], env_state["task"])
            ):
                logger.info(
                    "ale grade %s/%s: run_command task -> docker sandbox",
                    env_state["domain"], env_state["task"],
                )
                dres = ale_docker_grader.grade_via_docker(task)
                if dres.get("ok"):
                    res = dres
                    grading_path = "docker_sandbox"
                else:
                    raise GradingNotSupported(
                        "this ALE task is graded by executing commands inside the "
                        "full sandbox; the docker-backed grade did not produce a "
                        f"score: {dres.get('reason')}"
                    )
            else:
                reason = res.get("reason", "grading failed")
                if res.get("needs_sandbox"):
                    if res.get("windows"):
                        reason = (
                            "this ALE task targets a Windows sandbox; the local "
                            "Linux Docker provider cannot grade it (it needs a "
                            "Windows VM, e.g. the GCloud provider)."
                        )
                    else:
                        reason = (
                            "this ALE task is graded by executing commands inside "
                            "the full sandbox (session.run_command). Pull the local "
                            "ALE Docker image (agentslastexam/ale-ubuntu22-docker) to "
                            "grade the Linux subset, or use a managed ale_run."
                        )
                raise GradingNotSupported(reason)

        score = float(res.get("score", 0.0))
        passed = score >= ale_grader.SUCCESS_THRESHOLD
        verifier_rows = ale_grader.verifier_records(res, grading_path=grading_path)
        return GradeResult(
            session_id="",
            task_id=row.task_id,
            overall_success=passed,
            pass_rate=score,
            n_passed=sum(bool(v["passed"]) for v in verifier_rows),
            n_total=len(verifier_rows),
            per_verifier=[VerifierView(**v) for v in verifier_rows],
        )

    def teardown(self, row: TaskRow, env_state: dict[str, Any]) -> None:
        try:
            ale_grader.teardown_workspace(env_state.get("session_root", ""))
        except Exception as e:  # noqa: BLE001 - teardown is best-effort
            logger.warning("ale teardown failed for %s: %s", row.task_id, e)


def get_environment(benchmark: str):
    if benchmark == "eog":
        return EogEnvironment()
    if benchmark == "ale":
        return AleEnvironment()
    from . import adapter_registry

    item = adapter_registry.descriptor(benchmark)
    if item and item.runnable:
        return adapter_registry.load_factory(item)(item)
    raise ValueError(f"no environment backend for benchmark {benchmark!r}")


# --------------------------------------------------------------------------- #
# Report -> wire shape                                                        #
# --------------------------------------------------------------------------- #
def report_to_result(
    session_id: str, report: VerificationReport
) -> GradeResult:
    return GradeResult(
        session_id=session_id,
        task_id=report.task_id,
        overall_success=report.overall_success,
        pass_rate=report.pass_rate,
        n_passed=report.n_passed,
        n_total=report.n_total,
        per_verifier=[
            VerifierView(
                name=v.name,
                passed=v.passed,
                score=1.0 if v.passed else 0.0,
                expected=v.expected,
                actual=v.actual,
                comparison_type=v.comparison_type,
                error=v.error,
            )
            for v in report.per_verifier
        ],
    )
