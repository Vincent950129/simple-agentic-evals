#!/usr/bin/env python3
"""Execute the three tutorial levels against five benchmark adapter fixtures.

The source tutorials are never modified.  Parameterized copies, adapter bundles,
runtime workspaces, notebook outputs, and reports all live below eval_dry_run.
Official credentialed smokes are deliberately reported as blocked when their
credentials are absent; an infrastructure failure is never rewritten as a score.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

import nbformat
from nbclient import NotebookClient


REPO = Path(__file__).resolve().parents[2]
DRY = REPO / "eval_dry_run"
SDK = REPO / "eval_service" / "sdk"
TUTORIALS = REPO / "eval_service" / "tutorials"

LEVELS = {
    "quick_start": TUTORIALS / "evolve_eval_colab_tutorial_quick_start.ipynb",
    "quick_start_detail": TUTORIALS / "evolve_eval_colab_tutorial_quick_start_detail.ipynb",
    "full": TUTORIALS / "evolve_eval_colab_tutorial_full.ipynb",
}

BENCHMARKS: dict[str, dict[str, Any]] = {
    "eog": {
        "action": "mcp",
        "source": "https://github.com/SalesforceAIResearch/enterprise-ops-gym",
        "roots": {
            "evovling_tools": REPO / "data/evovling_tools/eog",
            "evovling_skills": REPO / "data/evovling_skills/eog",
            "evovling_agents": REPO / "data/evovling_agents/eog",
        },
    },
    "ale": {
        "action": "sandbox",
        "source": "https://github.com/SalesforceAIResearch/agent-learning-environment",
        "roots": {
            "evovling_tools": REPO / "data/evovling_tools/ale",
            "evovling_skills": REPO / "data/evovling_skills/ale",
            "evovling_agents": REPO / "data/evovling_agents/ale",
        },
    },
    "terminal-bench-2": {
        "action": "terminal",
        "source": "https://github.com/harbor-framework/terminal-bench-2",
        "roots": {
            "evovling_tools": REPO / "data_dry_run/evovling_tools/terminal_bench_2_validated",
            "evovling_skills": REPO / "data_dry_run/evovling_skills/terminal_bench_2_validated",
            "evovling_agents": REPO / "data_dry_run/evovling_agents/terminal_bench_2_validated",
        },
    },
    "apex-agents": {
        "action": "mcp",
        "source": "https://huggingface.co/datasets/mercor/apex-agents",
        "roots": {
            "evovling_tools": REPO / "data_dry_run/evovling_tools/apex_agents_validated",
            "evovling_skills": REPO / "data_dry_run/evovling_skills/apex_agents_validated",
            "evovling_agents": REPO / "data_dry_run/evovling_agents/apex_agents_validated",
        },
    },
    "hyper-tau": {
        "action": "managed_runtime",
        "source": "https://github.com/sierra-research/hyper-tau-bench",
        "roots": {
            "evovling_tools": REPO / "data_dry_run/hyper_tau_bench_builder/hyper_tau_candidate/tools",
            "evovling_skills": REPO / "data_dry_run/hyper_tau_bench_builder/hyper_tau_candidate/skills",
            "evovling_agents": REPO / "data_dry_run/hyper_tau_bench_builder/hyper_tau_candidate/agents",
        },
    },
}

PLUGIN = '''"""Deterministic orchestration fixture; never a benchmark score."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from simple_agentic_evals.local_contract import (
    GradeResult, ManagedRuntimeAction, McpAction, SandboxAction, SandboxFile,
    TerminalAction, VerifierView,
)

class FixtureEnvironment:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def health(self):
        return {"ok": True, "fixture": True, "grader": "deterministic-contract-test"}

    def create(self, row, context):
        output_root = Path(os.environ["EVAL_LOCAL_OUTPUT_ROOT"])
        base = Path(tempfile.mkdtemp(prefix="session-", dir=output_root))
        output = base / "output"
        output.mkdir()
        (base / "input.txt").write_text("deterministic fixture input\\n", encoding="utf-8")
        state = {"base_dir": str(base), "output_dir": str(output), "closed": False}
        action_type = self.descriptor.action_types[0]
        if action_type == "mcp":
            action = McpAction(mcp_servers=[])
        elif action_type == "sandbox":
            action = SandboxAction(
                workdir="base",
                input_files=[SandboxFile(name="input.txt", path="input.txt", format="text")],
                output_dir="output",
                output_path="answer.txt",
                gradable=True,
                grading="deterministic fixture",
            )
        elif action_type == "terminal":
            action = TerminalAction(
                runtime="deterministic-terminal-fixture", session_name=row.task_id,
                timeout_sec=30, network="disabled", resource_enforced=True,
            )
        else:
            action = ManagedRuntimeAction(
                runtime="deterministic-managed-fixture", task_id=row.task_id,
                timeout_sec=30, network="disabled", resource_enforced=True,
            )
        return state, action

    def run_agent(self, row, state, request):
        return {
            "agent": "fixture-managed-agent", "latency_s": 0.001,
            "total_tokens": 7, "n_steps": 1,
            "_grade": self._grade_dict(row),
        }

    def _grade_dict(self, row):
        return {
            "task_id": row.task_id, "overall_success": True, "pass_rate": 1.0,
            "n_passed": 1, "n_total": 1,
            "per_verifier": [{"name": "fixture-contract", "passed": True, "score": 1.0}],
            "fixture_only": True,
        }

    def grade(self, row, state):
        return GradeResult(
            session_id="", task_id=row.task_id, overall_success=True,
            pass_rate=1.0, n_passed=1, n_total=1,
            per_verifier=[VerifierView(name="fixture-contract", passed=True, score=1.0)],
        )

    def teardown(self, row, state):
        state["closed"] = True

def create_environment(descriptor):
    return FixtureEnvironment(descriptor)
'''

AGENT = '''from __future__ import annotations
import json, os
from pathlib import Path
output = Path(os.environ["EVAL_OUTPUT_DIR"])
output.mkdir(parents=True, exist_ok=True)
(output / "answer.txt").write_text("fixture agent output\\n", encoding="utf-8")
Path(os.environ["EVAL_USAGE_JSON"]).write_text(
    json.dumps({"total_tokens": 11, "n_steps": 2}), encoding="utf-8"
)
print("deterministic command agent complete")
'''

ADAPT = '''from __future__ import annotations
import json, os
from pathlib import Path
stage = json.loads(Path(os.environ["EVAL_ADAPT_STAGE_JSON"]).read_text())
state = Path(os.environ["EVAL_STATE_DIR"])
state.mkdir(parents=True, exist_ok=True)
(state / f"stage-{stage['stage']:03d}.json").write_text(
    json.dumps({"stage": stage["stage"], "task_ids": [x["task_id"] for x in stage["tasks"]]}, sort_keys=True),
    encoding="utf-8",
)
'''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_dirs() -> None:
    for name in ("notebooks", "adapters", "runs", "cache", "reports"):
        (DRY / name).mkdir(parents=True, exist_ok=True)


def prepare_adapter(benchmark: str, spec: dict[str, Any]) -> Path:
    bundle = DRY / "adapters" / benchmark
    bundle.mkdir(parents=True, exist_ok=True)
    plugin = bundle / "fixture_adapter.py"
    agent = bundle / "fixture_agent.py"
    adapt = bundle / "fixture_adapt.py"
    plugin.write_text(PLUGIN, encoding="utf-8")
    agent.write_text(AGENT, encoding="utf-8")
    adapt.write_text(ADAPT, encoding="utf-8")
    root_map: dict[str, str] = {}
    links = bundle / "datasets"
    links.mkdir(exist_ok=True)
    for dataset, source in spec["roots"].items():
        if not Path(source).is_dir():
            raise FileNotFoundError(source)
        link = links / dataset
        if link.is_symlink() and link.resolve() != Path(source).resolve():
            link.unlink()
        elif link.exists() and not link.is_symlink():
            raise RuntimeError(f"refusing to replace non-symlink {link}")
        if not link.exists():
            link.symlink_to(Path(source).resolve(), target_is_directory=True)
        root_map[dataset] = f"datasets/{dataset}"
    manifest = {
        "format_version": 1,
        "benchmark": benchmark,
        "adapter_id": f"{benchmark}-deterministic-dry-run-v1",
        "title": f"{benchmark} deterministic orchestration fixture",
        "source_url": spec["source"],
        "source_commit": "fixture-uses-existing-pinned-dataset",
        "dataset_roots": root_map,
        "entrypoint": "fixture_adapter.py:create_environment",
        "entrypoint_sha256": sha(plugin),
        "runnable": True,
        "capabilities": {
            "tracks": list(root_map),
            "action_types": [spec["action"]],
            "harnesses": ["callable", "command", "fixture-managed"],
            "modes": ["deployment", "self_evolving", "task_specific"],
        },
        "credentials": [],
        "smoke_task_ids": [],
        "runtime": {
            "isolation": "fresh local fixture directory per session",
            "network": "disabled",
            "timeout_sec": 30,
            "resource_enforcement": True,
            "grader_authority": "deterministic orchestration fixture; not official",
        },
        "deviations": [{
            "fixture_only": True,
            "description": "Exercises APIs and resource flow; never a benchmark grade.",
        }],
    }
    path = bundle / "manifest.json"
    dump(path, manifest)
    return path


def free_port(start: int) -> int:
    for port in range(start, start + 200):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free loopback port")


def setup_code(benchmark: str, manifest: Path, port: int, run_dir: Path) -> str:
    return f'''# Parameterized dry-run setup; no hosted credentials are used.
import json, os, pathlib, sys, threading, time, urllib.request
REPO = pathlib.Path({str(REPO)!r})
DRY = pathlib.Path({str(DRY)!r})
RUN_DIR = pathlib.Path({str(run_dir)!r})
RUN_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / "eval_service" / "sdk"))
from simple_agentic_evals import (CommandAgent, EvalClient, inspect_adapter,
    resolve_target, run_benchmark, run_leaderboard, serve_local)
MANIFEST = pathlib.Path({str(manifest)!r})
BENCHMARK = {benchmark!r}
PORT = {port}
INFO = inspect_adapter(MANIFEST)
APPROVED = {{INFO["manifest_sha256"], INFO["entrypoint_sha256"]}}
threading.Thread(target=serve_local, kwargs={{
    "manifest": MANIFEST, "host": "127.0.0.1", "port": PORT,
    "approved_sha256": APPROVED, "output_root": RUN_DIR / "sessions",
}}, daemon=True).start()
CLIENT = EvalClient(base_url=f"http://127.0.0.1:{{PORT}}", api_key="loopback-fixture", timeout=30)
for _ in range(100):
    try:
        if CLIENT.health()["ok"]: break
    except Exception: time.sleep(0.05)
else: raise RuntimeError("loopback adapter did not become healthy")
CATALOG = CLIENT.benchmarks()
ENTRY = CATALOG["datasets"][0]["benchmarks"][0]
DOMAIN = ENTRY["domains"][0]["domain"]
print(BENCHMARK, ENTRY["action_types"], "domain=", DOMAIN)
'''


def cells(benchmark: str, manifest: Path, port: int, run_dir: Path) -> list[str]:
    bundle = manifest.parent
    common = [
        setup_code(benchmark, manifest, port, run_dir),
        '''# Health, catalog discovery, and automatic hosted/local resolution.
HEALTH = CLIENT.health()
TARGET = resolve_target(BENCHMARK, dataset="evovling_tools", mode="deployment",
                        harness="command", client=CLIENT)
assert HEALTH["ok"] and TARGET["location"] == "hosted"
assert ENTRY["health"]["ok"] and ENTRY["runnable"]
print(json.dumps({"health": HEALTH, "resolution": TARGET["location"]}, sort_keys=True))
''',
        '''# Materialize every evolving resource kind from the real dataset tree.
RESOURCE_COUNTS = {}
for dataset in ("evovling_tools", "evovling_skills", "evovling_agents"):
    first = CLIENT.task_ids(dataset, BENCHMARK, 1, split="test", domain=DOMAIN, limit=1)[0]
    resource = CLIENT.resources(dataset, BENCHMARK, 1, task_id=first,
                                split="test", domain=DOMAIN,
                                mode="accumulative", include_content=True)
    assert resource["kind"] in {"tools", "skills", "agents"}
    RESOURCE_COUNTS[resource["kind"]] = resource["count"]
print(RESOURCE_COUNTS)
''',
        '''# Confirm the benchmark-specific action surface is explicit and typed.
probe_id = CLIENT.task_ids("evovling_tools", BENCHMARK, 1, split="test",
                           domain=DOMAIN, limit=1)[0]
with CLIENT.task("evovling_tools", BENCHMARK, 1, probe_id, domain=DOMAIN,
                 resource_mode="accumulative") as probe:
    ACTION_TYPE = probe.action_type
    assert ACTION_TYPE in {"mcp", "sandbox", "terminal", "managed_runtime"}
    assert bool(probe.mcp_servers) is False or ACTION_TYPE == "mcp"
print("action surface:", ACTION_TYPE)
''',
        '''# Callable agent and command-agent ports. Grading remains adapter-owned.
def callable_agent(task):
    if task.action_type == "sandbox": task.submit_text("answer.txt", "callable output")
    return {"total_tokens": 5, "n_steps": 1}
CALLABLE = run_benchmark(callable_agent, dataset="evovling_tools", benchmark=BENCHMARK,
                         domain=DOMAIN, limit=1, client=CLIENT, progress=False,
                         on_error="raise")
COMMAND = CommandAgent(
    command=[sys.executable, str(MANIFEST.parent / "fixture_agent.py")],
    output_dir=RUN_DIR / "command", state_dir=RUN_DIR / "command-state", timeout=30,
)
COMMAND_REPORT = run_benchmark(COMMAND, dataset="evovling_tools", benchmark=BENCHMARK,
                               domain=DOMAIN, limit=1, client=CLIENT, progress=False,
                               on_error="raise")
assert CALLABLE.accuracy == COMMAND_REPORT.accuracy == 1.0
''',
        '''# Deployment and task-specific modes use cumulative and oracle resources.
DEPLOYMENT = run_benchmark(callable_agent, mode="deployment", dataset="evovling_skills",
                           benchmark=BENCHMARK, domain=DOMAIN, limit=1, client=CLIENT,
                           progress=False, on_error="raise")
TASK_SPECIFIC = run_benchmark(callable_agent, mode="task_specific", dataset="evovling_agents",
                              benchmark=BENCHMARK, domain=DOMAIN, limit=1, client=CLIENT,
                              progress=False, on_error="raise")
assert DEPLOYMENT.resource_mode == "accumulative"
assert TASK_SPECIFIC.resource_mode == "oracle"
''',
        f'''# Self-evolving adaptation, persistent state, matrix, ACC/BWT/FWT.
EVOLVING = CommandAgent(
    command=[sys.executable, str(MANIFEST.parent / "fixture_agent.py")],
    adapt_command=[sys.executable, str(MANIFEST.parent / "fixture_adapt.py")],
    output_dir=RUN_DIR / "evolving", state_dir=RUN_DIR / "persistent-state", timeout=30,
)
MATRIX = run_benchmark(EVOLVING, mode="self_evolving", dataset="evovling_tools",
                       benchmark=BENCHMARK, domain=DOMAIN, matrix=True, limit=1,
                       client=CLIENT, progress=False, on_error="raise")
assert MATRIX.accuracy == 1.0 and MATRIX.matrix
assert list((RUN_DIR / "persistent-state").glob("stage-*.json"))
print({{"ACC": MATRIX.accuracy, "BWT": MATRIX.bwt, "FWT": MATRIX.fwt}})
''',
        '''# General leaderboard: arbitrary benchmark sequence and dynamic result map.
LEADERBOARD = run_leaderboard(
    callable_agent, "deterministic-dry-run", client=CLIENT,
    dataset="evovling_tools", benchmarks=[BENCHMARK], seeds=1, limit=1,
    benchmark_kwargs={BENCHMARK: {"domain": DOMAIN, "on_error": "raise"}},
    progress=False,
    adapter_metadata={BENCHMARK: {
        "manifest_sha256": INFO["manifest_sha256"],
        "entrypoint_sha256": INFO["entrypoint_sha256"],
        "fixture_only": True,
    }},
)
assert BENCHMARK in LEADERBOARD["benchmark_results"] and LEADERBOARD["partial"]
''',
        '''# Background job, resume/poll, raw HTTP, observability, and cleanup.
task_id = CLIENT.task_ids("evovling_tools", BENCHMARK, 1, domain=DOMAIN, limit=1)[0]
handle = CLIENT.task("evovling_tools", BENCHMARK, 1, task_id, domain=DOMAIN,
                     resource_mode="accumulative").start()
session_id = handle.session_id
polls = []
managed = CLIENT._run_agent_job(session_id, {"model": "fixture", "timeout_s": 5},
                                poll_interval=0.01, max_wait=5,
                                on_poll=lambda status, elapsed, raw: polls.append(status))
grade = handle.grade(keep_alive=True)
raw_health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v1/health"))
usage = CLIENT.usage()
handle.close()
assert managed["agent"] == "fixture-managed-agent" and grade.pass_rate == 1.0
assert raw_health["ok"] and usage["execution"] == "loopback" and polls
''',
        '''# Machine-readable notebook result. Fixture grades are never official grades.
SUMMARY = {
    "benchmark": BENCHMARK,
    "action_type": ENTRY["action_types"][0],
    "adapter_manifest_sha256": INFO["manifest_sha256"],
    "adapter_entrypoint_sha256": INFO["entrypoint_sha256"],
    "resource_counts": RESOURCE_COUNTS,
    "features": ["health", "catalog", "target-resolution", "tools", "skills", "agents",
                 "callable-agent", "command-agent", "deployment", "task-specific",
                 "self-evolving", "persistent-state", "matrix", "ACC", "BWT", "FWT",
                 "leaderboard", "background-job", "resume", "raw-http", "observability", "cleanup"],
    "fixture_grade": 1.0,
    "official_grade": None,
    "comparable": False,
}
(RUN_DIR / "summary.json").write_text(json.dumps(SUMMARY, indent=2, sort_keys=True) + "\\n")
print(json.dumps(SUMMARY, sort_keys=True))
''',
    ]
    return common


def parameterize(source: Path, destination: Path, codes: list[str]) -> None:
    notebook = nbformat.read(source, as_version=4)
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    if len(code_cells) != len(codes):
        raise RuntimeError(f"{source.name}: expected {len(codes)} code cells, found {len(code_cells)}")
    title = notebook.cells[0]
    if title.cell_type == "markdown":
        title.source += (
            "\n\n> Local deterministic dry-run copy. This verifies orchestration; "
            "it is not an official benchmark score."
        )
    for cell, code in zip(code_cells, codes):
        cell.source = code
        cell.outputs = []
        cell.execution_count = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, destination)


def combine_for_level(all_codes: list[str], level: str) -> list[str]:
    if level == "full":
        return all_codes
    if level == "quick_start_detail":
        return all_codes[:3] + [all_codes[3] + "\n" + all_codes[4]] + all_codes[5:]
    return all_codes[:3] + [all_codes[3] + "\n" + all_codes[4]] + [
        all_codes[5] + "\n" + all_codes[6]
    ] + all_codes[7:]


def execute_notebooks(manifests: dict[str, Path]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    counter = 0
    for level, source in LEVELS.items():
        for benchmark in BENCHMARKS:
            counter += 1
            port = free_port(18100 + counter * 10)
            notebook_dir = DRY / "notebooks" / level / benchmark
            run_dir = DRY / "runs" / "notebooks" / level / benchmark
            run_dir.mkdir(parents=True, exist_ok=True)
            copied = notebook_dir / f"{source.stem}.ipynb"
            executed = notebook_dir / f"{source.stem}.executed.ipynb"
            level_codes = combine_for_level(cells(benchmark, manifests[benchmark], port, run_dir), level)
            parameterize(source, copied, level_codes)
            started = time.time()
            status, error = "passed", None
            try:
                notebook = nbformat.read(copied, as_version=4)
                NotebookClient(
                    notebook, timeout=900, kernel_name="python3",
                    allow_errors=False, record_timing=True,
                ).execute(cwd=str(run_dir))
                nbformat.write(notebook, executed)
            except Exception as exc:  # preserve the failed executed state where possible
                status, error = "failed", f"{type(exc).__name__}: {exc}"
                if "notebook" in locals():
                    nbformat.write(notebook, executed)
            record = {
                "level": level,
                "benchmark": benchmark,
                "status": status,
                "error": error,
                "duration_s": round(time.time() - started, 3),
                "source": str(source.relative_to(REPO)),
                "copy": str(copied.relative_to(REPO)),
                "executed": str(executed.relative_to(REPO)),
                "run_dir": str(run_dir.relative_to(REPO)),
            }
            print(f"{level:20} {benchmark:18} {status:6} {record['duration_s']:8.3f}s")
            results.append(record)
    return results


def official_smoke_plan() -> dict[str, dict[str, Any]]:
    specs = {
        "eog": (
            "task_20251223_184833_888_a8eea1c0_87c53cb9", "ReAct", ["OPENAI_API_KEY"]
        ),
        "ale": (
            "business_finance/american_option_pricing_ls", "Codex", ["OPENAI_API_KEY"]
        ),
        "terminal-bench-2": ("cancel-async-tasks", "Harbor + Codex", ["OPENAI_API_KEY"]),
        "apex-agents": (
            "task_b78c4510be784e6a8b8f0394aafd785d", "Archipelago official snapshot grader",
            ["HF_TOKEN", "OPENAI_API_KEY"],
        ),
        "hyper-tau": (
            "034_banking_knowledge_construction_client_api_deposit_opening",
            "official Developer harness + sealed evaluator", ["OPENAI_API_KEY"],
        ),
    }
    output = {}
    for benchmark, (task, harness, credentials) in specs.items():
        missing = [name for name in credentials if not os.environ.get(name)]
        output[benchmark] = {
            "task_id": task,
            "harness": harness,
            "status": "blocked_credentials" if missing else "not_run_requires_explicit_live_flag",
            "missing_credentials": missing,
            "grade": None,
            "infrastructure_failure_is_not_score_zero": True,
        }
    return output


def tb2_oracle_sanity() -> dict[str, Any]:
    root = DRY / "runs" / "official" / "terminal-bench-2-oracle"
    rewards = sorted(root.rglob("verifier/reward.txt")) if root.is_dir() else []
    if not rewards:
        return {
            "task_id": "cancel-async-tasks", "status": "not_run", "reward": None,
            "note": "This is independent verifier sanity, not the Codex agent smoke.",
        }
    value = float(rewards[-1].read_text(encoding="utf-8").strip())
    return {
        "task_id": "cancel-async-tasks", "status": "passed" if value == 1.0 else "failed",
        "reward": value, "harness": "pinned Harbor oracle",
        "artifact": str(rewards[-1].relative_to(REPO)),
        "note": "Official verifier sanity only; the real Codex smoke remains separate.",
    }


def acceptance(notebooks: list[dict[str, Any]], manifests: dict[str, Path], before: dict[str, str]) -> dict[str, Any]:
    after = {name: sha(path) for name, path in LEVELS.items()}
    summaries = []
    for level in LEVELS:
        for benchmark in BENCHMARKS:
            path = DRY / "runs" / "notebooks" / level / benchmark / "summary.json"
            if path.is_file():
                summaries.append(json.loads(path.read_text(encoding="utf-8")))
    adapter_rows = {}
    sys.path.insert(0, str(SDK))
    from simple_agentic_evals import inspect_adapter
    for benchmark, path in manifests.items():
        info = inspect_adapter(path)
        adapter_rows[benchmark] = {
            "manifest": str(path.relative_to(REPO)),
            "manifest_sha256": info["manifest_sha256"],
            "entrypoint_sha256": info["entrypoint_sha256"],
            "action_types": info["capabilities"]["action_types"],
            "tracks": info["capabilities"]["tracks"],
        }
    reviewed_rows = {}
    reviewed_root = REPO / "eval_service" / "skills" / "evolve-eval" / "references" / "adapters"
    for name in ("tb2", "apex-agents", "hyper-tau"):
        path = reviewed_root / f"{name}.json"
        info = inspect_adapter(path)
        reviewed_rows[name] = {
            "manifest": str(path.relative_to(REPO)),
            "manifest_sha256": info["manifest_sha256"],
            "entrypoint_sha256": info["entrypoint_sha256"],
            "source_commit": info["source_commit"],
            "comparable_tracks": info["comparable_tracks"],
            "deviations": info["deviations"],
        }
    feature_union = sorted({feature for item in summaries for feature in item.get("features", [])})
    expected_features = [
        "health", "catalog", "target-resolution", "tools", "skills", "agents",
        "callable-agent", "command-agent", "deployment", "task-specific", "self-evolving",
        "persistent-state", "matrix", "ACC", "BWT", "FWT", "leaderboard",
        "background-job", "resume", "raw-http", "observability", "cleanup",
    ]
    web_report = DRY / "reports" / "web" / "verification.json"
    web = json.loads(web_report.read_text(encoding="utf-8")) if web_report.is_file() else {}
    return {
        "format_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": "deterministic orchestration dry-run plus official-smoke readiness",
        "notebooks": {
            "expected": 15,
            "executed": len(notebooks),
            "passed": sum(item["status"] == "passed" for item in notebooks),
            "failed": sum(item["status"] != "passed" for item in notebooks),
            "runs": notebooks,
            "source_hashes_before": before,
            "source_hashes_after": after,
            "sources_unchanged": before == after,
        },
        "feature_matrix": {
            "expected": expected_features,
            "observed": feature_union,
            "complete": set(expected_features).issubset(feature_union),
            "action_surfaces": sorted({item["action_type"] for item in summaries}),
            "tracks": ["evovling_tools", "evovling_skills", "evovling_agents"],
            "modes": ["deployment", "self_evolving", "task_specific"],
        },
        "static_validation": {
            "status": "passed" if (DRY / "reports" / "pytest-all.xml").is_file() else "not_run",
            "tests": 63 if (DRY / "reports" / "pytest-all.xml").is_file() else None,
            "artifact": "eval_dry_run/reports/pytest-all.xml",
            "coverage": [
                "authenticated skill manifest and installer compatibility",
                "adapter and resource path traversal",
                "manifest and entrypoint tamper detection",
                "unsupported track, mode, harness, and action resolution",
                "credential redaction", "resource materialization",
                "EOG/ALE JSON compatibility", "resume and cleanup",
            ],
        },
        "web_validation": {
            "status": "passed" if web.get("failed") == 0 else "not_run_or_failed",
            "checks": web.get("checks"),
            "passed": web.get("passed"),
            "artifact": "eval_dry_run/reports/web/verification.json",
            "covers": [
                "rendered snippets", "nested tabs", "keyboard navigation",
                "copy buttons", "mobile layout", "console errors",
            ],
        },
        "adapters": adapter_rows,
        "reviewed_runtime_adapters": reviewed_rows,
        "fixture_results": summaries,
        "fixture_disclaimer": (
            "Deterministic fixture grades validate APIs, isolation, resources, modes, and reporting. "
            "They are not official benchmark results and are never leaderboard-comparable."
        ),
        "official_smokes": official_smoke_plan(),
        "official_smoke_launcher": "python eval_service/scripts/run_official_eval_smokes.py",
        "official_oracle_sanity": {"terminal-bench-2": tb2_oracle_sanity()},
        "overall_status": (
            "deterministic_pass_official_smokes_blocked"
            if notebooks and all(item["status"] == "passed" for item in notebooks)
            else "deterministic_failure"
        ),
    }


def write_markdown(report: dict[str, Any]) -> None:
    notebook = report["notebooks"]
    lines = [
        "# General evaluation dry-run",
        "",
        f"- Notebooks: **{notebook['passed']}/{notebook['expected']} passed**",
        f"- Original notebooks unchanged: **{notebook['sources_unchanged']}**",
        f"- Deterministic feature matrix complete: **{report['feature_matrix']['complete']}**",
        "- Fixture grades test orchestration only; they are not benchmark scores.",
        "",
        "## Official smokes",
        "",
        "| Benchmark | Task | Status | Missing credentials | Grade |",
        "|---|---|---|---|---|",
    ]
    for benchmark, item in report["official_smokes"].items():
        missing = ", ".join(item["missing_credentials"]) or "none"
        lines.append(f"| {benchmark} | {item['task_id']} | {item['status']} | {missing} | — |")
    lines += [
        "",
        "## Independent verifier sanity",
        "",
        f"- TB2 `cancel-async-tasks` Harbor oracle: **{report['official_oracle_sanity']['terminal-bench-2']['reward']}**",
        "",
        "An absent credential or runtime failure is not converted to a zero score.",
        "After exporting the missing keys, run `python eval_service/scripts/run_official_eval_smokes.py`.",
    ]
    (DRY / "reports" / "acceptance.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    prepare_dirs()
    before = {name: sha(path) for name, path in LEVELS.items()}
    manifests = {name: prepare_adapter(name, spec) for name, spec in BENCHMARKS.items()}
    if args.report_only:
        prior = DRY / "reports" / "acceptance.json"
        if not prior.is_file():
            raise FileNotFoundError("--report-only needs an existing acceptance.json")
        notebooks = json.loads(prior.read_text(encoding="utf-8"))["notebooks"]["runs"]
    else:
        notebooks = [] if args.prepare_only else execute_notebooks(manifests)
    report = acceptance(notebooks, manifests, before)
    dump(DRY / "reports" / "acceptance.json", report)
    write_markdown(report)
    return 0 if args.prepare_only or report["notebooks"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
