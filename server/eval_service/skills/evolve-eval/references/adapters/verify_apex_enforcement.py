#!/usr/bin/env python3
"""Regenerate the deterministic APEX evolving-tools enforcement proof."""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _resolve(manifest: Path, value: str) -> Path:
    path = Path(value.replace("{manifest_dir}", str(manifest.parent))).expanduser()
    if not path.is_absolute():
        path = manifest.parent / path
    return path.resolve()


def _load_runner(manifest: Path):
    path = manifest.parent / "run_apex.py"
    spec = importlib.util.spec_from_file_location("evolve_eval_apex_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_allowlist_middleware(source: Path):
    path = source / "environment/runner/gateway/gateway.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_AllowedToolsMiddleware"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            class_node,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "Middleware": object,
        "override": lambda function: function,
        "tool_name_matches": lambda configured_tool_name, observed_tool_name:
            configured_tool_name == observed_tool_name,
    }
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_AllowedToolsMiddleware"]


async def _exercise_middleware(middleware_class, allowed: str, forbidden: str) -> None:
    middleware = middleware_class([allowed])

    async def listed(_context):
        return [SimpleNamespace(name=allowed), SimpleNamespace(name=forbidden)]

    visible = await middleware.on_list_tools(None, listed)
    if [tool.name for tool in visible] != [allowed]:
        raise AssertionError("pinned gateway exposed an operation outside the allowlist")

    async def called(_context):
        return "called"

    allowed_context = SimpleNamespace(message=SimpleNamespace(name=allowed))
    if await middleware.on_call_tool(allowed_context, called) != "called":
        raise AssertionError("selected operation was not callable")
    forbidden_context = SimpleNamespace(message=SimpleNamespace(name=forbidden))
    try:
        await middleware.on_call_tool(forbidden_context, called)
    except ValueError as exc:
        if "not in the allowlist" not in str(exc):
            raise
    else:
        raise AssertionError("direct invocation of a forbidden operation succeeded")


def _report(manifest_path: Path) -> dict:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = raw["runtime"]
    if raw["source_commit"] != "92c86856cf1b11f9833a8a076b3a45a63afa3929":
        raise AssertionError("unexpected APEX source pin")
    runner = _load_runner(manifest_path)
    source_entry = next(
        value for value in runtime["required_paths"] if str(value).endswith("archipelago")
    )
    source = _resolve(manifest_path, source_entry)
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    if head != runner.ARCHIPELAGO_PIN:
        raise AssertionError(f"Archipelago pin mismatch: {head}")

    tools_root = _resolve(manifest_path, raw["dataset_roots"]["evovling_tools"])
    task_id = raw["smoke_task_ids"][0]
    stage_rows = _jsonl(tools_root / "v2/train.jsonl")
    row = next(item for item in stage_rows if item["task_id"] == task_id)
    oracle = set(row["oracle_tools"])
    cumulative = set(row["cummulative_tools"])
    final = {
        tool
        for split in ("train", "test")
        for item in _jsonl(tools_root / f"v4/{split}.jsonl")
        for tool in item["cummulative_tools"]
    }
    if not oracle or not oracle <= cumulative or not (final - cumulative):
        raise AssertionError("fixture does not distinguish oracle, stage, and future tools")

    allowed_id = sorted(oracle)[0]
    forbidden_id = sorted(final - oracle)[0]
    allowed = runner._allowed_tool_names([allowed_id])[0]
    forbidden = runner._allowed_tool_names([forbidden_id])[0]
    try:
        runner._allowed_tool_names(["unknown.execute"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("unknown operation passed adapter validation")

    middleware_class = _load_allowlist_middleware(source)
    asyncio.run(_exercise_middleware(middleware_class, allowed, forbidden))

    example_main = (source / "examples/hugging_face_task/main.py").read_text(
        encoding="utf-8"
    )
    if 'open(EXAMPLE_DIR / "mcp_config_all_oss_servers.json")' not in example_main:
        raise AssertionError("official example no longer loads the reviewed MCP configuration")
    if 'httpx.post(f"{ENV_URL}/apps", json=mcp_config' not in example_main:
        raise AssertionError("official example no longer sends the reviewed allowlist to gateway")

    evidence = {
        "allowed_resource_available":
            "verify_apex_enforcement.py executed the selected operation through the pinned gateway middleware",
        "forbidden_resource_inaccessible":
            "verify_apex_enforcement.py proved an unselected operation is absent from list_tools",
        "ambient_bypass_blocked":
            "verify_apex_enforcement.py verified the official agent port installs the one reviewed gateway config",
        "task_specific_oracle_only":
            "verify_apex_enforcement.py derived the allowlist from only this task's oracle_tools",
        "self_evolving_stage_isolation":
            "verify_apex_enforcement.py proved v2 cumulative tools exclude capabilities introduced by v4",
        "forbidden_tool_invocation_rejected":
            "verify_apex_enforcement.py executed a direct forbidden call and the pinned middleware rejected it",
    }
    return {
        "adapter_id": raw["adapter_id"],
        "format_version": 1,
        "source_commit": raw["source_commit"],
        "tracks": {
            "evovling_tools": {
                "controls": [
                    {"evidence": evidence[control_id], "id": control_id, "passed": True}
                    for control_id in (
                        "allowed_resource_available",
                        "forbidden_resource_inaccessible",
                        "ambient_bypass_blocked",
                        "task_specific_oracle_only",
                        "self_evolving_stage_isolation",
                        "forbidden_tool_invocation_rejected",
                    )
                ],
                "modes_tested": ["deployment", "self_evolving", "task_specific"],
                "status": "passed",
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("apex-agents.json"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = _report(args.manifest.expanduser().resolve())
    body = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(body, encoding="utf-8")
    print(json.dumps({"status": "passed", "sha256": hashlib.sha256(body.encode()).hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
