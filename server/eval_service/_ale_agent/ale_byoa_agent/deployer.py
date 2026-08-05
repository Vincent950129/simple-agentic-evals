"""ByoaDeployer — stage a Bring-Your-Own-Agent submission into the ALE sandbox.

The eval-service can grade *read-only* ALE tasks with a local filesystem shim
(``_ale_grader_driver``). Tasks whose ``evaluate()`` runs commands inside the
sandbox (``session.run_command``) need the real box. For those, the service
drives ``ale_run`` against the local Docker provider with THIS agent instead of
an LLM: rather than solving the task, it writes the client's already-produced
output artifact(s) into the task's in-sandbox output directory, so the
framework's own scorer (the task's ``score_outputs.py`` / ``@evaluate_task``)
grades a real submission end-to-end.

Modeled on :class:`ale_run.agents.dummy.deployer.DummyDeployer` (the no-LLM smoke
agent): runs with ``executor: local`` on the host and talks to the container's
cua-server over :class:`RemoteDesktopSession`. Imports only ``ale_run`` +
``cua_bench`` + stdlib, so it loads cleanly inside the ALE uv venv.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, ClassVar

from ale_run.agents.dummy import pathscan
from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    TrajectoryBuilder,
)

from .config import ByoaConfig

logger = logging.getLogger(__name__)

_REPORT_NAME = "byoa_report.json"


class ByoaDeployer(BaseAgentDeployer):
    """Bring-Your-Own-Agent submission stager. Runs on the host, drives the box."""

    default_executor: ClassVar[str] = "local"
    # Host-only: the submission lives on the host filesystem (``submission_dir``)
    # and is pushed into the sandbox over cua. A ``docker`` executor would run
    # this code inside a *separate* container with no view of that host dir.
    supported_executors: ClassVar[frozenset[str]] = frozenset({"local"})
    hot_artifacts: ClassVar[tuple[str, ...]] = (_REPORT_NAME,)

    @property
    def version(self) -> str | None:
        return "byoa-0.1.0"

    async def install(self) -> None:
        from cua_bench.computers.remote import RemoteDesktopSession  # noqa: F401

        Path(self.executor.work_dir).mkdir(parents=True, exist_ok=True)
        logger.info(
            "byoa: install ok (work_dir=%s, executor=%s, os=%s)",
            self.executor.work_dir, self.executor.type,
            getattr(self.executor.sandbox, "os", "?"),
        )

    async def launch(self, prompt: str) -> AgentRunResult:
        from cua_bench.computers.remote import RemoteDesktopSession

        cfg: ByoaConfig = self.config  # type: ignore[assignment]
        work_dir = Path(self.executor.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        sb = self.executor.sandbox

        sub_root = Path(cfg.submission_dir).expanduser() if cfg.submission_dir else None
        files = (
            [p for p in sorted(sub_root.rglob("*")) if p.is_file()]
            if sub_root and sub_root.is_dir()
            else []
        )

        # In-sandbox output dir(s): an explicit override wins; otherwise parse the
        # rendered prompt (which carries the task's absolute in-VM output path).
        if cfg.output_dir_override:
            out_dirs = [cfg.output_dir_override]
        else:
            out_dirs = pathscan.scan(prompt).output_dirs

        report: dict[str, Any] = {
            "os": getattr(sb, "os", None),
            "submission_dir": str(sub_root) if sub_root else None,
            "n_files": len(files),
            "files": [str(p) for p in files],
            "output_dirs": out_dirs,
            "written": [],
            "errors": [],
            "skipped": False,
            "skip_reason": None,
        }

        if not files:
            report["skipped"] = True
            report["skip_reason"] = "no submission files under submission_dir"
            self._write_report(work_dir, report)
            logger.warning("byoa: SKIP — no submission files (dir=%s)", sub_root)
            return self._result("completed", t0, work_dir)
        if not out_dirs:
            report["skipped"] = True
            report["skip_reason"] = "no output dir resolved (empty override + none in prompt)"
            self._write_report(work_dir, report)
            logger.warning("byoa: SKIP — could not resolve an in-sandbox output dir")
            return self._result("completed", t0, work_dir, error="no output dir resolved")

        session = RemoteDesktopSession(
            api_url=sb.endpoint, os_type=sb.os, ephemeral=False, headless=True,
        )
        if not await session.wait_until_ready(timeout=cfg.connect_timeout_s):
            report["errors"].append("sandbox cua-server not responsive within timeout")
            self._write_report(work_dir, report)
            return self._result("failed", t0, work_dir, error="VM not reachable")

        is_linux = bool(getattr(sb, "is_linux", True))
        for out_dir in out_dirs:
            await self._mkdir(session, out_dir, is_linux=is_linux)
            for fp in files:
                rel = fp.relative_to(sub_root).as_posix()
                remote = self._join(out_dir, rel, is_linux=is_linux)
                parent = self._parent(remote, is_linux=is_linux)
                try:
                    if parent and parent != out_dir:
                        await self._mkdir(session, parent, is_linux=is_linux)
                    await session.write_bytes(remote, fp.read_bytes())
                    report["written"].append(remote)
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append(
                        f"write {remote!r}: {type(exc).__name__}: {exc}"
                    )

        self._write_report(work_dir, report)
        logger.info(
            "byoa: staged %d/%d file(s) into %d output dir(s); errors=%d",
            len(report["written"]), len(files), len(out_dirs), len(report["errors"]),
        )
        # Even if some writes failed, let the run complete so the scorer reports
        # the real (possibly partial) state rather than masking it as agent-fail.
        return self._result("completed", t0, work_dir)

    # ------------------------------------------------------------------ #
    # path + shell helpers (linux/windows aware)                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _join(directory: str, rel_posix: str, *, is_linux: bool) -> str:
        d = directory.rstrip("/\\")
        if is_linux:
            return f"{d}/{rel_posix}"
        return d + "\\" + rel_posix.replace("/", "\\")

    @staticmethod
    def _parent(path: str, *, is_linux: bool) -> str:
        sep = "/" if is_linux else "\\"
        return path.rsplit(sep, 1)[0] if sep in path else ""

    @staticmethod
    async def _mkdir(session: Any, path: str, *, is_linux: bool) -> None:
        if is_linux:
            cmd = f"mkdir -p '{path}'"
        else:
            cmd = (
                "powershell -NoProfile -Command "
                f"\"New-Item -ItemType Directory -Force -Path '{path}' | Out-Null\""
            )
        await session.run_command(cmd, check=False)

    # ------------------------------------------------------------------ #
    # trajectory                                                          #
    # ------------------------------------------------------------------ #
    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: ByoaConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        report_path = Path(work_dir) / _REPORT_NAME
        if not report_path.exists():
            builder.add_step(
                source="system",
                message=f"byoa: report missing at {report_path}",
                extra={"reason": "no_report", "run_status": run_result.status},
            )
            return
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            builder.add_step(
                source="system",
                message=f"byoa: report unreadable: {type(exc).__name__}: {exc}",
                extra={"reason": "bad_report"},
            )
            return

        if report.get("skipped"):
            builder.add_step(
                source="system",
                message=f"byoa: SKIPPED — {report.get('skip_reason')}",
                extra={"reason": "skipped"},
            )
        else:
            builder.add_step(
                source="system",
                message=(
                    f"byoa: wrote {len(report.get('written', []))}/"
                    f"{report.get('n_files', 0)} submission file(s) into "
                    f"{report.get('output_dirs')}"
                ),
                extra={
                    "written": report.get("written", []),
                    "errors": report.get("errors", []),
                },
            )
        builder.trajectory.extra.setdefault("byoa", {}).update({
            "work_dir": str(work_dir),
            "run_status": run_result.status,
            "report": report,
        })

    # ------------------------------------------------------------------ #
    # misc                                                                #
    # ------------------------------------------------------------------ #
    def _result(
        self, status: str, t0: float, work_dir: Path, *, error: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            duration_s=time.monotonic() - t0,
            transcript_path=str(Path(work_dir) / _REPORT_NAME),
            error=error,
        )

    @staticmethod
    def _write_report(work_dir: Path, report: dict[str, Any]) -> None:
        (Path(work_dir) / _REPORT_NAME).write_text(
            json.dumps(report, indent=2), encoding="utf-8",
        )
