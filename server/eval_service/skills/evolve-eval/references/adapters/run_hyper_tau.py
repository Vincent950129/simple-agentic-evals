#!/usr/bin/env python3
"""Run one pinned Hyper-tau outer task through the sealed evaluator."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

PIN = "6e9f34c685d40fa7a9f5935d8970af6fd9d5f118"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "data_dry_run" / "hyper_tau_bench_builder").is_dir():
            return parent
    raise RuntimeError("set HYPER_TAU_SOURCE_ROOT outside the reference checkout")


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise RuntimeError(f"{name} must not be empty")
    return value


def _docker_can_bind(path: Path, image: str) -> bool:
    result = subprocess.run(
        ["docker", "run", "--rm", "--mount",
         f"type=bind,source={path.resolve()},target=/probe,readonly",
         image, "test", "-d", "/probe"],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def _run_on_docker_host(task_id: str, source: Path, session: Path, image: str) -> int:
    """Run from a Docker volume when this process and the daemon do not share paths."""
    suffix = uuid.uuid4().hex[:12]
    volume = f"evolve-hyper-tau-{suffix}"
    staging = f"evolve-hyper-stage-{suffix}"
    subprocess.run(["docker", "volume", "create", volume], check=True,
                   stdout=subprocess.DEVNULL)
    try:
        mountpoint = subprocess.run(
            ["docker", "volume", "inspect", volume, "--format", "{{.Mountpoint}}"],
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        subprocess.run(
            ["docker", "run", "-d", "--name", staging, "-v", f"{volume}:/bundle",
             image, "sleep", "infinity"],
            check=True, stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "exec", staging, "mkdir", "-p", "/bundle/source",
             "/bundle/session", "/bundle/tmp"], check=True,
        )
        archive = subprocess.Popen(
            ["tar", "-C", str(source), "--exclude=./.git", "--exclude=./.venv",
             "--exclude=__pycache__", "--exclude=*.pyc", "-cf", "-", "."],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        assert archive.stdout is not None
        extracted = subprocess.run(
            ["docker", "exec", "-i", staging, "tar", "-C", "/bundle/source",
             "-xf", "-"], stdin=archive.stdout, check=False,
        )
        archive.stdout.close()
        archive_status = archive.wait()
        if archive_status or extracted.returncode:
            raise RuntimeError("failed to copy the pinned Hyper-tau source into Docker")
        subprocess.run(
            ["docker", "cp", str(Path(__file__).resolve()),
             f"{staging}:/bundle/run_hyper_tau.py"], check=True,
        )
        docker_cli = shutil.which("docker")
        if not docker_cli:
            raise RuntimeError("docker CLI is required")
        subprocess.run(
            ["docker", "cp", docker_cli, f"{staging}:/bundle/docker"], check=True,
        )
        subprocess.run(["docker", "rm", "-f", staging], check=True,
                       stdout=subprocess.DEVNULL)

        forwarded = (
            "OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY", "HYPER_TAU_DEVELOPER_HARNESS",
            "HYPER_TAU_DEVELOPER_LLM", "HYPER_TAU_DEVELOPER_REASONING_EFFORT",
            "HYPER_TAU_AGENT_MODEL", "EVAL_RUNTIME_TRACK",
            "EVAL_RUNTIME_RESOURCE_MODE", "EVAL_RUNTIME_ALLOWED_TOOLS_JSON",
        )
        command = [
            "docker", "run", "--rm", "-v", f"{volume}:{mountpoint}",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-e", f"PATH={mountpoint}:/opt/tau2/.venv/bin:/usr/local/bin:/usr/bin:/bin",
            "-e", "HYPER_TAU_INNER=1",
            "-e", f"HYPER_TAU_SOURCE_ROOT={mountpoint}/source",
            "-e", f"EVAL_RUNTIME_SESSION_DIR={mountpoint}/session",
            "-e", f"TMPDIR={mountpoint}/tmp",
            "-e", f"TAU2_DATA_DIR={mountpoint}/source/data",
            "-e", f"TAU2_MODEL_ROUTING={mountpoint}/source/model_routing.toml",
        ]
        for name in forwarded:
            if os.environ.get(name):
                command.extend(["-e", name])
        command.extend([
            "-w", f"{mountpoint}/source", image, "python",
            f"{mountpoint}/run_hyper_tau.py", "--task", task_id,
        ])
        completed = subprocess.run(command, check=False)

        subprocess.run(
            ["docker", "run", "-d", "--name", staging, "-v", f"{volume}:/bundle",
             image, "sleep", "infinity"],
            check=True, stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "cp", f"{staging}:/bundle/session/.", str(session)],
            check=True,
        )
        return completed.returncode
    finally:
        subprocess.run(["docker", "rm", "-f", staging], capture_output=True,
                       check=False)
        subprocess.run(["docker", "volume", "rm", "-f", volume],
                       capture_output=True, check=False)


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--task":
        raise SystemExit("usage: run_hyper_tau.py --task TASK_ID")
    task_id = sys.argv[2]
    source_override = os.environ.get("HYPER_TAU_SOURCE_ROOT")
    source = (
        Path(source_override).resolve() if source_override else
        (_repo_root() / "data_dry_run/hyper_tau_bench_builder/cache/hyper-tau-bench").resolve()
    )
    if not os.environ.get("HYPER_TAU_INNER"):
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
            text=True, capture_output=True,
        ).stdout.strip()
        if head != PIN:
            raise RuntimeError(f"Hyper-tau revision mismatch: {head}")
        session = Path(os.environ["EVAL_RUNTIME_SESSION_DIR"]).resolve()
        image = os.environ.get(
            "TAU2_SANDBOX_DOCKER_IMAGE", "tau2-construction-runtime:contract-v7"
        )
        if not _docker_can_bind(session, image):
            return _run_on_docker_host(task_id, source, session, image)
    sys.path.insert(0, str(source / "src"))
    os.chdir(source)
    from tau2.cli import _build_hyper_tau_llm_args
    from tau2.hyper.harnesses.factory import create_developer_builder
    from tau2.hyper.recording import RecordingDisplay
    from tau2.hyper.sandbox.builder import BuildBudget
    from tau2.hyper.sandbox.orchestrator import SandboxOrchestrator
    from tau2.hyper.task_loader import load_active_hyper_tau_task
    from tau2.utils.model_routing import resolve_model

    task = load_active_hyper_tau_task(task_id)
    developer_harness = _env("HYPER_TAU_DEVELOPER_HARNESS", "codex")
    developer_llm = _env("HYPER_TAU_DEVELOPER_LLM", "gpt-5")
    agent_llm = _env("HYPER_TAU_AGENT_MODEL", "gpt-5.6-sol")
    matching_models = [
        config for config in (task.hyper.allowed_agent_models or [])
        if config.get("model") == agent_llm
    ]
    if not matching_models:
        allowed = ", ".join(
            sorted(config["model"] for config in task.hyper.allowed_agent_models or [])
        )
        raise RuntimeError(
            f"HYPER_TAU_AGENT_MODEL={agent_llm!r} is not in this task's official "
            f"model menu: {allowed}"
        )
    required_credentials = {"OPENAI_API_KEY"}
    route = resolve_model(agent_llm)
    if route.provider is not None:
        required_credentials.add(route.provider.api_key_env)
    missing_credentials = sorted(
        name for name in required_credentials if not os.environ.get(name)
    )
    if missing_credentials:
        raise RuntimeError(
            "missing credentials for the selected Hyper-tau models: "
            + ", ".join(missing_credentials)
        )
    effort = _env("HYPER_TAU_DEVELOPER_REASONING_EFFORT", "medium")
    developer_args = _build_hyper_tau_llm_args(developer_llm, effort, None)
    builder = create_developer_builder(
        developer_harness, developer_llm, developer_args, effort
    )
    limits = task.sandbox_config or {}
    budget = BuildBudget(
        max_steps=int(limits.get("max_steps", 0)),
        max_time_seconds=int(limits.get("max_time_seconds") or 8 * 60 * 60),
    )
    session = Path(os.environ["EVAL_RUNTIME_SESSION_DIR"])
    kit_dir = session / "kit"
    kit_dir.mkdir(parents=True, exist_ok=False)
    orchestrator = SandboxOrchestrator.from_task(
        task=task,
        builder=builder,
        agent_llm=agent_llm,
        allowed_agent_models_override=matching_models,
        budget=budget,
        kit_dir=kit_dir,
        keep_kit=True,
    )
    run_config = {
        "mode": "sandbox", "source_commit": PIN,
        "developer_harness": developer_harness, "developer_llm": developer_llm,
        "developer_llm_args": developer_args, "agent_llm": orchestrator.agent_llm,
        "agent_llm_args": orchestrator.agent_llm_args,
        "allowed_agent_models": orchestrator.allowed_agent_models,
        "agent_model_provider": route.provider.name if route.provider else None,
        "user_llm": orchestrator.user_llm, "user_llm_args": orchestrator.user_llm_args,
        "sandbox_wall_clock_seconds": budget.max_time_seconds,
        "evolving_track": os.environ.get("EVAL_RUNTIME_TRACK"),
        "resource_mode": os.environ.get("EVAL_RUNTIME_RESOURCE_MODE"),
        "allowed_tools": json.loads(os.environ.get("EVAL_RUNTIME_ALLOWED_TOOLS_JSON", "[]")),
    }
    recorder = RecordingDisplay(None, task=task, config=run_config, checkpoint_dir=session)
    result = orchestrator.run(display=recorder)
    build_result = result.run_metadata.get("build_result", {})
    if build_result.get("done_reason") == "harness_error":
        raise RuntimeError("Hyper-tau Developer harness failed before submission")
    if not result.test_details:
        raise RuntimeError("Hyper-tau sealed evaluator produced no task results")
    score = result.final_test_reward
    if (not isinstance(score, (int, float)) or isinstance(score, bool)
            or not math.isfinite(float(score)) or not 0 <= float(score) <= 1):
        raise RuntimeError(f"sealed evaluator returned invalid score: {score!r}")
    recording = recorder.save(task=task, result=result, config=run_config)
    print(json.dumps({"task_id": task_id, "final_test_reward": float(score),
                      "overall_success": float(score) >= 1, "recording": str(recording)},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
