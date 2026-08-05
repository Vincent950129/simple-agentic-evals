#!/usr/bin/env python3
"""Standalone ALE grader — runs INSIDE the ALE ``uv`` venv (py3.12 + cua_bench).

The eval-service process (py3.10, no cua_bench) shells out to this script via
``<ALE_ROOT>/.venv/bin/python _ale_grader_driver.py ...`` with ``cwd=ALE_ROOT``.

It reproduces ALE's *real* grader without provisioning the 105 GB sandbox:

  1. point ``REMOTE_ROOT_DIR`` at a per-session workspace where the task's
     ``base/{input,reference,software}`` are staged and the agent's submitted
     artifact has been written under ``base/output/``;
  2. import the task module ``tasks.<domain>.<task>.main`` and read its
     cua_bench ``Task`` metadata (the same dict the sandbox grader sees);
  3. call the task's own ``@cb.evaluate_task`` coroutine with a **local
     filesystem session shim** (the file ops ``read_bytes`` / ``read_file`` /
     ``file_exists`` / ``directory_exists`` / ``list_dir`` are exactly what the
     read-only graders use). Tasks whose grader executes commands in the sandbox
     (``session.run_command``) cannot be graded this way and are reported as
     ``needs_sandbox`` instead of being silently scored 0.

The score is the task's published ``[0, 1]`` value. The result is emitted as a
single line ``__ALE_RESULT__ {json}`` so the caller can parse it even when the
task logs noise to stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

RESULT_SENTINEL = "__ALE_RESULT__"


class LocalSession:
    """Duck-typed ``cb.DesktopSession`` backed by the local filesystem.

    The real provider session exposes ``read_bytes``/``file_exists``/... ; the
    task graders only ``await`` those, so a plain object with matching async
    methods is sufficient. ``run_command`` is unsupported (it implies in-sandbox
    execution) — we flag it so the caller can report ``needs_sandbox``.
    """

    def __init__(self) -> None:
        self.sandbox_required = False

    async def read_bytes(self, path: str) -> bytes:
        return Path(path).read_bytes()

    async def read_file(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    async def write_file(self, path: str, data: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")

    async def write_bytes(self, path: str, data: bytes) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def file_exists(self, path: str) -> bool:
        return Path(path).is_file()

    async def directory_exists(self, path: str) -> bool:
        return Path(path).is_dir()

    async def list_dir(self, path: str) -> list[str]:
        p = Path(path)
        return sorted(c.name for c in p.iterdir()) if p.is_dir() else []

    async def run_command(self, *args, **kwargs):
        self.sandbox_required = True
        raise NotImplementedError(
            "session.run_command is unavailable in the local ALE grader; "
            "this task's evaluator executes commands inside the sandbox."
        )

    # Any session feature beyond the file-read surface above (e.g.
    # ``session.interface``, ``session.run_command`` wrappers) implies the task
    # needs the real sandbox. Flag it on first access so the driver reports
    # ``needs_sandbox`` instead of leaking a cryptic AttributeError.
    def __getattr__(self, name: str):  # pragma: no cover - defensive
        if name.startswith("__"):
            raise AttributeError(name)
        object.__setattr__(self, "sandbox_required", True)

        async def _unsupported(*_a, **_k):
            raise NotImplementedError(f"session.{name} unsupported in local grader")
        return _unsupported


def _load_metadata(mod, split: str) -> dict:
    """Return the cua_bench Task metadata dict for this task.

    Prefer a module-level ``config`` (the common template); fall back to calling
    the registered ``@cb.tasks_config`` loader and taking the first task.
    """
    cfg = getattr(mod, "config", None)
    if cfg is not None and hasattr(cfg, "to_metadata"):
        return cfg.to_metadata()
    # Fall back: find the tasks_config loader and call it.
    for name in dir(mod):
        obj = getattr(mod, name)
        if getattr(obj, "_td_type", None) == "tasks_config":
            tasks = obj()
            if tasks:
                md = getattr(tasks[0], "metadata", None)
                if md:
                    return dict(md)
    raise RuntimeError("could not resolve task metadata (no config / tasks_config)")


def _is_windows_paths(meta: dict) -> bool:
    """ALE has both Linux (POSIX paths) and Windows (``\\`` paths,
    ``GeneralTaskConfig``) tasks. The local grader maps POSIX paths only; a
    backslash in the staged paths means a Windows sandbox we can't reproduce."""
    for k in ("output_file", "task_dir", "remote_output_dir", "input_dir", "reference_dir"):
        v = meta.get(k)
        if isinstance(v, str) and "\\" in v:
            return True
    return False


def _find_evaluate(mod, split: str):
    candidates = []
    for name in dir(mod):
        obj = getattr(mod, name)
        if getattr(obj, "_td_type", None) == "evaluate_task":
            candidates.append(obj)
    if not candidates:
        raise RuntimeError("task module has no @cb.evaluate_task function")
    for obj in candidates:
        if getattr(obj, "_td_split", None) == split:
            return obj
    return candidates[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ale-root", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--variant", default="base")
    ap.add_argument("--split", default="train")
    ap.add_argument("--mode", default="grade", choices=["grade", "meta"])
    args = ap.parse_args()

    # Must be set BEFORE importing the task (config paths are built at import).
    os.environ["REMOTE_ROOT_DIR"] = args.workspace
    os.environ.setdefault("OS_TYPE", "linux")
    if args.ale_root not in sys.path:
        sys.path.insert(0, args.ale_root)

    out: dict = {"ok": False}
    try:
        mod = importlib.import_module(f"tasks.{args.domain}.{args.task}.main")
        meta = _load_metadata(mod, args.split)
        if args.mode == "meta":
            out = {"ok": True, "metadata": meta, "windows": _is_windows_paths(meta)}
        elif _is_windows_paths(meta):
            # Don't run evaluate() (it would mkdir backslash-named junk under the
            # workspace and never grade correctly) — report it honestly. The
            # ``windows`` flag tells the host this is NOT docker-gradable either
            # (the local Docker provider is Linux-only).
            out = {
                "ok": False,
                "needs_sandbox": True,
                "windows": True,
                "reason": "task targets a Windows sandbox (non-POSIX paths); "
                          "not gradable by the local Linux grader",
            }
        else:
            evfn = _find_evaluate(mod, args.split)
            session = LocalSession()
            cfg_ns = SimpleNamespace(metadata=meta)
            raised: Exception | None = None
            raw = None
            try:
                raw = asyncio.run(evfn(cfg_ns, session))
            except Exception as exc:  # noqa: BLE001
                raised = exc
            # If the grader touched run_command (whether it swallowed the error
            # or let it propagate), the local score is untrustworthy -> report
            # needs_sandbox rather than a misleading number.
            if session.sandbox_required:
                out = {
                    "ok": False,
                    "needs_sandbox": True,
                    "reason": "task grader executes commands in-sandbox (run_command)",
                }
            elif raised is not None:
                raise raised
            else:
                if isinstance(raw, (list, tuple)):
                    vals = [float(x) for x in raw]
                else:
                    vals = [float(raw)]
                score = sum(vals) / len(vals) if vals else 0.0
                out = {
                    "ok": True,
                    "score": score,
                    "raw_scores": vals,
                    "output_file": meta.get("output_file"),
                }
    except Exception as exc:  # noqa: BLE001
        out = {
            "ok": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(limit=6),
        }

    print(RESULT_SENTINEL, json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
