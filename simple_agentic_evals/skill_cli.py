"""``evolve-eval``: portable command-agent evaluation and interactive rehearsal."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import EvalClient
from .command_agent import CommandAgent, materialize_task_workspace
from .leaderboard import run_leaderboard
from .mcp import MCPSession
from .runtime_adapters import catalog_targets, inspect_adapter, resolve_target, serve_local

DATASETS = ("evovling_tools", "evovling_skills", "evovling_agents")


class _ManagedCodexAgent:
    """Use the adapter-owned official Codex port, with optional local adaptation."""

    def __init__(self, adaptation: CommandAgent | None = None):
        self._adaptation = adaptation

    def adapt(self, stage: int, tasks: Any) -> Any:
        if self._adaptation is None:
            raise RuntimeError("self-evolving managed Codex needs --adapt-command")
        return self._adaptation.adapt(stage, tasks)

    def __call__(self, task: Any) -> dict[str, Any]:
        from .codex import acp_codex_agent

        run = acp_codex_agent(task)
        if run.stopped == "error":
            raise RuntimeError(run.final_message or "managed Codex runtime failed")
        return {
            "latency_s": run.latency_s,
            "total_tokens": run.total_tokens,
            "n_steps": run.n_steps,
        }


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _default_run_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("evolve-eval-runs") / stamp


def _smoke_domain(client: EvalClient, dataset: str, benchmark: str) -> str | None:
    domains: list[str | None] = []
    for ds in client.benchmarks().get("datasets", []):
        if ds.get("dataset") != dataset:
            continue
        for bench in ds.get("benchmarks", []):
            if bench.get("benchmark") == benchmark:
                domains = [d.get("domain") or None for d in bench.get("domains", [])]
    if not domains:
        raise ValueError(f"{dataset}/{benchmark} is not available")
    if benchmark == "eog" and "hr" in domains:
        return "hr"
    return domains[0]


def _leaderboard(args: argparse.Namespace) -> int:
    if args.full and not args.confirm_full_cost:
        raise SystemExit(
            "--full runs every selected task three times and may consume many agent-hours "
            "and millions of tokens; repeat with --confirm-full-cost"
        )
    if args.confirm_full_cost and not args.full:
        raise SystemExit("--confirm-full-cost is only valid with --full")
    benchmarks = tuple(
        part.strip() for part in getattr(args, "benchmarks", "eog,ale").split(",")
        if part.strip()
    )
    if not benchmarks or len(set(benchmarks)) != len(benchmarks):
        raise SystemExit("--benchmarks must contain unique comma-separated names")
    if bool(args.adapter) == bool(args.agent_command):
        raise SystemExit("choose exactly one of --adapter codex or --agent-command COMMAND")

    output = Path(args.output or _default_run_dir()).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "checkpoint.json"
    final = output / "leaderboard.json"
    seeds, limit = ((3, None) if args.full else (1, 2))
    client = EvalClient()
    local_inspected: dict[str, Any] | None = None
    if args.runtime_adapter:
        if len(benchmarks) != 1:
            raise SystemExit("automatic local fallback currently accepts one benchmark per sidecar")
        resolution_mode = {
            "deployment_eval": "deployment",
            "self_evolving_adapt_eval": "self_evolving",
            "self_evovling_adapt_eval": "self_evolving",
            "task_specific_eval": "task_specific",
        }.get(args.mode, args.mode.replace("_eval", ""))
        resolution = resolve_target(
            benchmarks[0], dataset=args.dataset, mode=resolution_mode,
            harness="codex" if args.adapter == "codex" else "command",
            runtime_adapter=args.runtime_adapter, client=client,
        )
        if resolution["location"] == "local":
            inspected = resolution["adapter"]
            local_inspected = inspected
            approvals = set(args.approve_sha256 or [])
            required = {inspected["manifest_sha256"], inspected["entrypoint_sha256"]}
            if not required.issubset(approvals):
                raise SystemExit(
                    "local adapter requires --approve-sha256 for manifest and entrypoint: "
                    + ", ".join(sorted(required - approvals))
                )
            thread = threading.Thread(
                target=serve_local,
                kwargs={"manifest": args.runtime_adapter, "host": "127.0.0.1",
                        "port": args.local_port, "approved_sha256": approvals,
                        "output_root": output / "sidecar"},
                daemon=True,
            )
            thread.start()
            local_url = f"http://127.0.0.1:{args.local_port}"
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    local_client = EvalClient(base_url=local_url, api_key="local-loopback")
                    if local_client.health().get("ok"):
                        client = local_client
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise SystemExit("local sidecar did not become healthy within 15 seconds")
        elif resolution["location"] != "hosted":
            raise SystemExit(str(resolution))
    if (
        args.full and local_inspected
        and args.dataset not in local_inspected.get("comparable_tracks", [])
    ):
        details = "; ".join(local_inspected.get("enforcement_errors") or [])
        raise SystemExit(
            f"{args.dataset} is diagnostic for this adapter: checksum-bound negative "
            "controls have not proved its resource semantics, so a full leaderboard "
            f"run is rejected{': ' + details if details else ''}"
        )
    # A broken runtime or grader is infrastructure failure, never an agent score.
    benchmark_kwargs: dict[str, dict[str, Any]] = {
        benchmark: {"on_error": "raise"} for benchmark in benchmarks
    }
    if not args.full:
        for benchmark in benchmarks:
            benchmark_kwargs[benchmark]["domain"] = _smoke_domain(
                client, args.dataset, benchmark
            )

    previous: dict[str, Any] = {}
    if args.resume:
        if not checkpoint.is_file():
            raise SystemExit(f"cannot resume: no checkpoint at {checkpoint}")
        previous = json.loads(checkpoint.read_text(encoding="utf-8"))
        expected = {"dataset": args.dataset, "mode": args.mode, "seeds": seeds,
                    "full": args.full, "benchmarks": list(benchmarks)}
        if previous.get("config") != expected:
            raise SystemExit("checkpoint settings differ from this run; use the original flags")
    elif checkpoint.exists() or final.exists():
        raise SystemExit(f"output already contains a run; pass --resume or choose another --output: {output}")

    reports = previous.get("reports") or {benchmark: [] for benchmark in benchmarks}
    config = {"dataset": args.dataset, "mode": args.mode, "seeds": seeds,
              "full": args.full, "benchmarks": list(benchmarks)}

    def save_report(benchmark: str, _index: int, summary: dict[str, Any]) -> None:
        reports.setdefault(benchmark, []).append(summary)
        _atomic_json(checkpoint, {"config": config, "reports": reports})

    managed_action = bool(
        local_inspected and args.adapter == "codex"
        and {"terminal", "managed_runtime"}
        & set((local_inspected.get("capabilities") or {}).get("action_types") or [])
    )
    if managed_action:
        adaptation = None
        if args.adapt_command:
            adaptation = CommandAgent(
                [sys.executable, "-c", "pass"], adapt_command=args.adapt_command,
                state_dir=args.state_dir, output_dir=output, timeout=args.timeout,
                keep_workspaces=not args.cleanup,
            )
        agent: Any = _ManagedCodexAgent(adaptation)
    else:
        agent = CommandAgent(
            args.agent_command,
            adapter=args.adapter or "command",
            adapt_command=args.adapt_command,
            state_dir=args.state_dir,
            output_dir=output,
            timeout=args.timeout,
            keep_workspaces=not args.cleanup,
        )
    adapter_metadata = None
    if local_inspected:
        adapter_metadata = {benchmarks[0]: {
            "adapter_id": local_inspected["adapter_id"],
            "manifest_sha256": local_inspected["manifest_sha256"],
            "entrypoint_sha256": local_inspected["entrypoint_sha256"],
            "source_commit": local_inspected["source_commit"],
            "comparable": args.dataset in local_inspected.get("comparable_tracks", []),
            "enforcement_evidence": local_inspected.get("enforcement_evidence", {}),
            "enforcement_errors": local_inspected.get("enforcement_errors", []),
            "runtime_artifacts": local_inspected.get("runtime_artifacts", {}),
            "deviations": local_inspected.get("deviations", []),
        }}
    row = run_leaderboard(
        agent,
        args.name,
        client=client,
        dataset=args.dataset,
        mode=args.mode,
        seeds=seeds,
        limit=limit,
        harness="codex" if args.adapter == "codex" else "command",
        progress=False,
        benchmark_kwargs=benchmark_kwargs,
        existing_reports=reports,
        on_report=save_report,
        confirm_full_cost=args.confirm_full_cost,
        benchmarks=benchmarks,
        adapter_metadata=adapter_metadata,
    )
    _atomic_json(final, row)
    if args.cleanup:
        shutil.rmtree(output / "tasks", ignore_errors=True)
    _json(row)
    return 0


def _interactive_state(output: str | None) -> tuple[Path, Path]:
    root = Path(output or "evolve-eval-interactive").resolve()
    return root, root / ".interactive.json"


def _start(args: argparse.Namespace) -> int:
    root, state_path = _interactive_state(args.output)
    if state_path.exists():
        old = json.loads(state_path.read_text(encoding="utf-8"))
        if old.get("status") == "active":
            raise SystemExit(f"an interactive task is already active; grade or abort {state_path}")
    client = EvalClient()
    domain = args.domain if args.domain is not None else _smoke_domain(client, args.dataset, args.benchmark)
    task = next(client.tasks(
        args.dataset, args.benchmark, "full", "test", domain,
        resource_mode=args.resource_mode, limit=1,
    ))
    try:
        task.start()
        paths = materialize_task_workspace(task, root / "task", codex=False)
        state = {
            "status": "active",
            "session_id": task.session_id,
            "dataset": args.dataset,
            "benchmark": args.benchmark,
            "version": "full",
            "split": "test",
            "domain": domain,
            "resource_mode": args.resource_mode,
            "task_id": task.task_id,
            "workspace": str(paths["root"]),
        }
        _atomic_json(state_path, state)
    except Exception:
        task.close()
        raise
    _json({
        **state,
        "task_json": str(paths["task_json"]),
        "input_dir": str(paths["input_dir"]),
        "output_dir": str(paths["output_dir"]),
        "resource_dir": str(paths["resource_dir"]),
        "tool_state": str(paths["state_json"]),
        "note": "interactive rehearsal only; this cannot produce a publishable row",
    })
    return 0


def _load_interactive(output: str | None) -> tuple[Path, Path, dict[str, Any]]:
    root, state_path = _interactive_state(output)
    if not state_path.is_file():
        raise SystemExit(f"no interactive state at {state_path}")
    return root, state_path, json.loads(state_path.read_text(encoding="utf-8"))


def _status(args: argparse.Namespace) -> int:
    _root, _path, state = _load_interactive(args.output)
    if state.get("status") != "active":
        _json(state)
        return 0
    client = EvalClient()
    remote = client._get(f"/v1/sessions/{state['session_id']}")
    _json({**state, "remote": remote})
    return 0


def _task_from_state(client: EvalClient, state: dict[str, Any]):
    task = client.task(
        state["dataset"], state["benchmark"], state["version"], state["task_id"],
        state.get("split", "test"), state.get("domain"), state.get("resource_mode"),
    )
    task.session_id = state["session_id"]
    return task


def _grade(args: argparse.Namespace) -> int:
    root, state_path, state = _load_interactive(args.output)
    if state.get("status") != "active":
        raise SystemExit(f"interactive task is {state.get('status')!r}, not active")
    client = EvalClient()
    task = _task_from_state(client, state)
    if state["benchmark"] == "ale":
        task.submit_dir(root / "task" / "output")
    grade = task.grade(keep_alive=False)
    result = {
        "name": args.name,
        "dataset": state["dataset"],
        "benchmark": state["benchmark"],
        "task_id": state["task_id"],
        "score": round(grade.pass_rate * 100, 1),
        "passed": bool(grade.overall_success),
        "per_verifier": list(grade.per_verifier or []),
        "partial": True,
        "note": "interactive rehearsal only; use leaderboard for isolated publishable runs",
    }
    state.update({"status": "graded", "result": result})
    _atomic_json(state_path, state)
    _atomic_json(root / "leaderboard.json", result)
    _json(result)
    return 0


def _abort(args: argparse.Namespace) -> int:
    root, state_path, state = _load_interactive(args.output)
    if state.get("status") == "active":
        EvalClient()._delete(f"/v1/sessions/{state['session_id']}")
    state["status"] = "aborted"
    _atomic_json(state_path, state)
    if args.cleanup:
        shutil.rmtree(root / "task", ignore_errors=True)
    _json({"status": "aborted", "task_id": state.get("task_id")})
    return 0


def _tool(args: argparse.Namespace) -> int:
    raw_state = args.state or os.environ.get("EVAL_TASK_STATE", "")
    if not raw_state:
        raise SystemExit("set EVAL_TASK_STATE or pass --state PATH")
    state_path = Path(raw_state)
    if not state_path.is_file():
        raise SystemExit(f"task state does not exist: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    servers = state.get("servers") or []
    selected = next((s for s in servers if not args.server or s.get("name") == args.server), None)
    if not selected:
        raise SystemExit("no matching MCP server in task state")
    client = EvalClient(base_url=state.get("base_url"))
    extra_headers = json.loads(os.environ.get("EVAL_MCP_HEADERS_JSON", "{}"))
    headers = {**client.headers, **(extra_headers.get(selected.get("name")) or {})}
    url = client.base_url + selected["path"] if selected.get("path") else selected["url"]
    mcp = MCPSession(url, headers=headers)
    try:
        if args.tool_command == "list":
            tools = mcp.list_tools()
            if state.get("enforce_allowlist"):
                allowed = set(state.get("allowed_tools") or [])
                tools = [tool for tool in tools if tool.get("name") in allowed]
            _json(tools)
        else:
            name = args.name
            if state.get("enforce_allowlist") and name not in set(state.get("allowed_tools") or []):
                raise SystemExit(f"tool {name!r} is not in this task's selected allowlist")
            try:
                arguments = json.loads(args.arguments)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"--arguments must be a JSON object: {exc}") from exc
            if not isinstance(arguments, dict):
                raise SystemExit("--arguments must be a JSON object")
            _json(mcp.call_tool(name, arguments))
    finally:
        mcp.close()
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    root = Path(args.output).resolve()
    if root == Path("/") or root == Path.home():
        raise SystemExit("refusing to clean a broad directory")
    shutil.rmtree(root / "tasks", ignore_errors=True)
    _json({"cleaned": str(root / "tasks")})
    return 0


def _targets(_args: argparse.Namespace) -> int:
    _json({"targets": catalog_targets()})
    return 0


def _inspect_adapter(args: argparse.Namespace) -> int:
    _json(inspect_adapter(args.manifest))
    return 0


def _resolve_target(args: argparse.Namespace) -> int:
    _json(resolve_target(
        args.benchmark,
        dataset=args.dataset,
        track=args.track,
        mode=args.mode,
        harness=args.harness,
        action_type=args.action_type,
        runtime_adapter=args.runtime_adapter,
    ))
    return 0


def _serve_local(args: argparse.Namespace) -> int:
    return serve_local(
        args.runtime_adapter,
        host=args.host,
        port=args.port,
        approved_sha256=args.approve_sha256,
        output_root=args.output,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evolve-eval", description="Evaluate a shell-capable agent")
    sub = parser.add_subparsers(dest="command", required=True)

    lead = sub.add_parser("leaderboard", help="run isolated tasks on any advertised benchmark")
    lead.add_argument("--adapter", choices=("codex",))
    lead.add_argument("--agent-command", help="command invoked once per task; prompt arrives on stdin")
    lead.add_argument("--name", default="My Agent")
    lead.add_argument("--dataset", choices=DATASETS, default="evovling_tools")
    lead.add_argument("--mode", default="deployment_eval")
    lead.add_argument("--benchmarks", default="eog,ale",
                      help="comma-separated benchmark ids (default: eog,ale)")
    lead.add_argument("--adapt-command",
                      help="self-evolving adaptation command; receives EVAL_ADAPT_STAGE_JSON")
    lead.add_argument("--state-dir", help="persistent state directory shared across stages")
    lead.add_argument("--runtime-adapter", help="reviewed local adapter used only if hosted is unsuitable")
    lead.add_argument("--approve-sha256", action="append", default=[],
                      help="approve an exact local manifest/entrypoint hash; repeat twice")
    lead.add_argument("--local-port", type=int, default=8078)
    lead.add_argument("--output")
    lead.add_argument("--timeout", type=float, default=1800)
    lead.add_argument("--resume", action="store_true")
    lead.add_argument("--cleanup", action="store_true", help="remove per-task workspaces after success")
    lead.add_argument("--full", action="store_true", help="all selected tasks, three repeats")
    lead.add_argument("--confirm-full-cost", action="store_true")
    lead.set_defaults(func=_leaderboard)

    start = sub.add_parser("start", help="start one interactive smoke task")
    start.add_argument("--dataset", choices=DATASETS, default="evovling_tools")
    start.add_argument("--benchmark", default="eog")
    start.add_argument("--domain")
    start.add_argument("--resource-mode", choices=("oracle", "accumulative", "none"), default="accumulative")
    start.add_argument("--output")
    start.set_defaults(func=_start)

    for command, func in (("status", _status), ("grade", _grade), ("abort", _abort)):
        p = sub.add_parser(command)
        p.add_argument("--output")
        if command == "grade":
            p.add_argument("--name", default="Interactive Agent")
        if command == "abort":
            p.add_argument("--cleanup", action="store_true")
        p.set_defaults(func=func)

    tool = sub.add_parser("tool", help="MCP helper constrained by the selected tool allowlist")
    tool.add_argument("--state")
    tool.add_argument("--server")
    tool_sub = tool.add_subparsers(dest="tool_command", required=True)
    tool_sub.add_parser("list")
    call = tool_sub.add_parser("call")
    call.add_argument("name")
    call.add_argument("--arguments", default="{}")
    tool.set_defaults(func=_tool)

    clean = sub.add_parser("cleanup", help="remove completed task workspaces")
    clean.add_argument("--output", required=True)
    clean.set_defaults(func=_cleanup)

    targets = sub.add_parser("targets", help="list hosted benchmark adapters and capabilities")
    targets.set_defaults(func=_targets)

    inspect = sub.add_parser("inspect-adapter", help="validate a local adapter without importing it")
    inspect.add_argument("manifest")
    inspect.set_defaults(func=_inspect_adapter)

    resolve = sub.add_parser("resolve-target", help="select a healthy hosted adapter or local fallback")
    resolve.add_argument("benchmark")
    resolve.add_argument("--dataset", choices=DATASETS, default="evovling_tools")
    resolve.add_argument("--track", choices=DATASETS)
    resolve.add_argument("--mode", choices=("deployment", "self_evolving", "task_specific"),
                         default="deployment")
    resolve.add_argument("--harness", default="command")
    resolve.add_argument("--action-type")
    resolve.add_argument("--runtime-adapter")
    resolve.set_defaults(func=_resolve_target)

    local = sub.add_parser("serve-local", help="run an approved adapter on loopback")
    local.add_argument("--runtime-adapter", required=True)
    local.add_argument("--approve-sha256", action="append", default=[])
    local.add_argument("--host", default="127.0.0.1")
    local.add_argument("--port", type=int, default=8078)
    local.add_argument("--output", default="eval-local-runs")
    local.set_defaults(func=_serve_local)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted; checkpoint retained for --resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
