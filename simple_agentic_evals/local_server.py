"""Loopback REST/session service for one explicitly approved local adapter."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .local_contract import GradingNotSupported
from .runtime_adapters import inspect_adapter


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _versions(root: Path) -> list[int]:
    return sorted(
        int(path.name[1:]) for path in root.glob("v*")
        if path.is_dir() and path.name[1:].isdigit()
    )


def _flat(root: Path) -> bool:
    return bool(_versions(root))


def _domains(root: Path) -> list[str | None]:
    if _flat(root):
        return [None]
    return sorted(
        path.name for path in root.iterdir()
        if path.is_dir() and not path.name.startswith("_") and _versions(path)
    ) if root.is_dir() else []


def _base(root: Path, domain: str | None) -> Path:
    if _flat(root):
        return root
    if not domain:
        raise ValueError("domain is required for this benchmark")
    return root / domain


def _rows(root: Path, version: int | str, split: str, domain: str | None) -> list[dict[str, Any]]:
    if split not in ("train", "test"):
        raise ValueError("split must be train or test")
    base = _base(root, domain)
    stages = _versions(base) if str(version) in ("full", "all") else [int(version)]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stage in stages:
        path = base / f"v{stage}" / f"{split}.jsonl"
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            task_id = str(row.get("task_id") or "")
            if task_id and task_id not in seen:
                row["_source_stage"] = stage
                rows.append(row)
                seen.add(task_id)
    return rows


def _row_object(raw: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=str(raw.get("task_id") or ""),
        system_prompt=str(raw.get("system_prompt") or ""),
        user_prompt=str(raw.get("user_prompt") or raw.get("task_prompt") or raw.get("prompt") or ""),
        selected_tools=list(raw.get("oracle_tools") or []),
        raw=raw,
    )


def _kind(dataset: str) -> str:
    return {"evovling_tools": "tools", "evovling_skills": "skills",
            "evovling_agents": "agents"}.get(dataset, "unknown")


def _resource_names(raw: dict[str, Any], dataset: str, mode: str) -> list[str]:
    if mode == "none":
        return []
    kind = _kind(dataset)
    fields = {
        ("tools", "oracle"): ("oracle_tools",),
        ("tools", "accumulative"): ("cummulative_tools", "cumulative_tools"),
        ("skills", "oracle"): ("oracle_skills",),
        ("skills", "accumulative"): ("cummulative_oracle_skills", "cumulative_oracle_skills"),
        ("agents", "oracle"): ("oracle_agents",),
        ("agents", "accumulative"): ("cumulative_agents", "cummulative_agents"),
    }.get((kind, mode), ())
    for field in fields:
        if raw.get(field):
            return [str(value) for value in raw[field]]
    return []


def _safe_files(root: Path, paths: list[Path], include: bool) -> list[dict[str, Any]]:
    output = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        item: dict[str, Any] = {"path": relative, "size": path.stat().st_size}
        if include:
            item["content"] = path.read_text(encoding="utf-8")
        output.append(item)
    return output


def _resources(
    root: Path, dataset: str, raw: dict[str, Any], mode: str,
    version: int | str, domain: str | None, include: bool,
) -> dict[str, Any]:
    names = _resource_names(raw, dataset, mode)
    if str(version) in ("full", "all") and mode == "accumulative":
        base = _base(root, domain)
        stages = _versions(base)
        if stages:
            for split_name in ("test", "train"):
                final_rows = _rows(root, stages[-1], split_name, domain)
                final_names = next(
                    (_resource_names(row, dataset, mode) for row in final_rows
                     if _resource_names(row, dataset, mode)), []
                )
                if final_names:
                    names = final_names
                    break
    kind = _kind(dataset)
    items: list[dict[str, Any]] = []
    if kind == "skills" and mode != "none":
        skills = (root / domain if domain else root) / "_oracle" / "skills"
        for name in names:
            folder = skills / name
            paths = sorted(path for path in folder.rglob("*") if path.is_file()) if folder.is_dir() else []
            items.append({"name": name, "present": folder.is_dir(),
                          "files": _safe_files(root, paths, include)})
    elif kind == "agents" and mode != "none":
        base = root / domain if domain else root
        stage = max(_versions(base)) if str(version) in ("full", "all") else int(version)
        vdir = base / f"v{stage}"
        manifest_path = vdir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        index = {item.get("name"): item for item in manifest.get("agents", [])}
        for name in names:
            toml = vdir / "agents" / f"{name}.toml"
            skill = vdir / "agent_skills" / name
            paths = [toml] + (sorted(p for p in skill.rglob("*") if p.is_file()) if skill.is_dir() else [])
            meta = index.get(name, {})
            items.append({
                "name": name, "present": toml.is_file(),
                "files": _safe_files(root, paths, include),
                "tools": list(meta.get("oracle_tools") or meta.get("software")
                              or meta.get("owned_software") or []),
                "description": str(meta.get("description") or ""),
                "title": str(meta.get("title") or ""),
            })
    return {"kind": kind, "mode": mode, "stage": str(version), "count": len(names),
            "names": names, "items": items,
            "note": "Resources are enforced by the approved local adapter."}


class LocalRuntime:
    def __init__(self, manifest_path: str | Path, approved: set[str]):
        self.inspected = inspect_adapter(manifest_path)
        required = {self.inspected["manifest_sha256"], self.inspected["entrypoint_sha256"]}
        if not required.issubset(approved):
            raise PermissionError("local adapter manifest and entrypoint were not approved")
        self.manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        self.manifest_path = Path(manifest_path).resolve()
        entry, _, factory_name = self.manifest["entrypoint"].partition(":")
        source = (self.manifest_path.parent / entry).resolve()
        spec = importlib.util.spec_from_file_location(
            f"evolve_eval_local_{self.manifest['adapter_id']}", source
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import {source}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        descriptor_values = dict(self.manifest)
        verified_runtime = dict(self.manifest.get("runtime") or {})
        verified_runtime["verified_enforced_tracks"] = list(
            self.inspected.get("comparable_tracks") or []
        )
        descriptor_values.update({
            "manifest_path": self.manifest_path,
            "action_types": tuple(self.manifest["capabilities"]["action_types"]),
            "tracks": tuple(self.manifest["capabilities"]["tracks"]),
            "modes": tuple(self.manifest["capabilities"]["modes"]),
            "harnesses": tuple(self.manifest["capabilities"]["harnesses"]),
            "credentials": tuple(self.manifest.get("credentials") or []),
            "runtime": verified_runtime,
        })
        descriptor = SimpleNamespace(**descriptor_values)
        self.environment = getattr(module, factory_name)(descriptor)
        self.roots = {key: Path(value) for key, value in self.inspected["dataset_roots"].items()}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()

    def catalog(self) -> dict[str, Any]:
        datasets = []
        health = self.environment.health() if hasattr(self.environment, "health") else {"ok": True}
        for dataset, root in self.roots.items():
            domains = []
            for domain in _domains(root):
                base = _base(root, domain)
                versions = []
                full_train = full_test = 0
                for stage in _versions(base):
                    train = len(_rows(root, stage, "train", domain))
                    test = len(_rows(root, stage, "test", domain))
                    full_train += train
                    full_test += test
                    versions.append({"version": stage, "n_train": train, "n_test": test})
                domains.append({"domain": domain, "versions": versions,
                                "full": {"n_train": full_train, "n_test": full_test}})
            caps = self.manifest["capabilities"]
            datasets.append({"dataset": dataset, "benchmarks": [{
                "benchmark": self.manifest["benchmark"], "title": self.manifest.get("title"),
                "adapter_id": self.manifest["adapter_id"], "execution": "local",
                "runnable": bool(self.manifest.get("runnable", True) and health.get("ok")),
                "health": health, "flat": _flat(root), "kind": _kind(dataset),
                "tracks": list(caps["tracks"]), "modes": list(caps["modes"]),
                "harnesses": list(caps["harnesses"]),
                "action_types": list(caps["action_types"]), "domains": domains,
                "comparable_tracks": list(self.inspected.get("comparable_tracks") or []),
                "enforcement_evidence": dict(
                    self.inspected.get("enforcement_evidence") or {}
                ),
                "enforcement_errors": list(
                    self.inspected.get("enforcement_errors") or []
                ),
                "runtime": dict(self.manifest.get("runtime") or {}),
                "source_url": self.manifest.get("source_url", ""),
                "source_commit": self.manifest.get("source_commit", ""),
                "credentials": list(self.manifest.get("credentials") or []),
                "smoke_task_ids": list(self.manifest.get("smoke_task_ids") or []),
                "deviations": list(self.manifest.get("deviations") or []),
                "adapter_manifest_sha256": self.inspected["manifest_sha256"],
                "adapter_entrypoint_sha256": self.inspected["entrypoint_sha256"],
            }]})
        return {"execution": "loopback", "datasets": datasets}

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        dataset = str(body.get("dataset") or "evovling_tools")
        root = self.roots.get(dataset)
        if root is None or body.get("benchmark") != self.manifest["benchmark"]:
            raise KeyError("benchmark or track is not available")
        version = body.get("version", 1)
        version = "full" if str(version) in ("full", "all") else int(version)
        split, domain = str(body.get("split") or "test"), body.get("domain")
        rows = _rows(root, version, split, domain)
        task_id = body.get("task_id")
        raw = next((row for row in rows if not task_id or row.get("task_id") == task_id), None)
        if raw is None:
            raise KeyError("task is not in the selected split")
        mode = str(body.get("resource_mode") or ("none" if _kind(dataset) == "skills" else "accumulative"))
        if mode in ("cumulative", "cummulative", "accumulate"):
            mode = "accumulative"
        resource = _resources(root, dataset, raw, mode, version, domain, False)
        context = {"dataset": dataset, "benchmark": self.manifest["benchmark"],
                   "version": version, "split": split, "domain": domain,
                   "resource_mode": mode, "resource": resource}
        row = _row_object(raw)
        state, action = self.environment.create(row, context)
        sid = "sess_" + uuid.uuid4().hex[:16]
        session = {"id": sid, "row": row, "raw": raw, "state": state,
                   "action": asdict(action) if is_dataclass(action) else dict(action),
                   "selector": context, "resource": resource, "status": "active",
                   "created_at": time.time(), "last_grade": None}
        with self.lock:
            self.sessions[sid] = session
        return self.view(session)

    def view(self, session: dict[str, Any]) -> dict[str, Any]:
        raw, context = session["raw"], session["selector"]
        return {
            "session_id": session["id"], "status": session["status"],
            "created_at": session["created_at"], "expires_at": session["created_at"] + 43200,
            "task": {
                "selector": context, "task_id": session["row"].task_id,
                "system_prompt": session["row"].system_prompt,
                "user_prompt": session["row"].user_prompt,
                "required_steps": list(raw.get("required_steps") or raw.get("agent_must_do") or []),
                "evaluation": str(raw.get("evaluation") or "official benchmark verifier"),
                "oracle_tools": list(raw.get("oracle_tools") or []),
                "resources": session["resource"],
            },
            "action": session["action"],
        }

    def run_agent(self, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions[sid]
        request = SimpleNamespace(**body)
        for name in ("openai_api_key", "model"):
            if not hasattr(request, name):
                setattr(request, name, None)
        result = self.environment.run_agent(session["row"], session["state"], request)
        inline = result.pop("_grade", None)
        if inline:
            if not math.isfinite(float(inline["pass_rate"])):
                raise RuntimeError("adapter returned a non-finite grade")
            session["last_grade"] = inline
            session["state"]["_inline_graded"] = True
        result.setdefault("session_id", sid)
        return result

    def grade(self, sid: str, keep_alive: bool) -> dict[str, Any]:
        session = self.sessions[sid]
        result = session.get("last_grade")
        if result is None:
            grade = self.environment.grade(session["row"], session["state"])
            result = asdict(grade) if is_dataclass(grade) else dict(grade)
        result = {**result, "session_id": sid, "task_id": session["row"].task_id}
        if not math.isfinite(float(result["pass_rate"])):
            raise RuntimeError("grader returned a non-finite score")
        session["last_grade"] = result
        session["status"] = "graded" if keep_alive else "closed"
        if not keep_alive:
            self.environment.teardown(session["row"], session["state"])
        return result

    def close(self, sid: str) -> dict[str, Any]:
        session = self.sessions[sid]
        if session["status"] != "closed":
            self.environment.teardown(session["row"], session["state"])
            session["status"] = "closed"
        return {"session_id": sid, "status": "closed"}


def _handler(runtime: LocalRuntime):
    class Handler(BaseHTTPRequestHandler):
        server_version = "evolve-eval-loopback/1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, value: Any, content_type: str = "application/json") -> None:
            body = value if isinstance(value, bytes) else _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            size = int(self.headers.get("content-length", "0") or 0)
            return json.loads(self.rfile.read(size) or b"{}")

        def _error(self, exc: Exception) -> None:
            status = 501 if isinstance(exc, GradingNotSupported) else (
                404 if isinstance(exc, (KeyError, FileNotFoundError)) else
                422 if isinstance(exc, ValueError) else 502
            )
            self._send(status, {"detail": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/v1/health":
                    return self._send(200, {"ok": True, "execution": "loopback",
                                            "adapter": runtime.manifest["adapter_id"]})
                if parsed.path == "/v1/benchmarks":
                    return self._send(200, runtime.catalog())
                if parsed.path == "/v1/usage":
                    return self._send(200, {"execution": "loopback", "uploads": 0})
                if parsed.path == "/v1/tasks":
                    q = parse_qs(parsed.query)
                    dataset = q.get("dataset", ["evovling_tools"])[0]
                    root = runtime.roots[dataset]
                    rows = _rows(root, q.get("version", ["1"])[0], q.get("split", ["test"])[0],
                                 q.get("domain", [None])[0])
                    ids = [row["task_id"] for row in rows]
                    offset = int(q.get("offset", [0])[0])
                    limit = int(q["limit"][0]) if q.get("limit") else None
                    ids = ids[offset:] if limit is None else ids[offset:offset + limit]
                    return self._send(200, {"count": len(ids), "task_ids": ids})
                match = re.fullmatch(r"/v1/sessions/([^/]+)/jobs/([^/]+)", parsed.path)
                if match:
                    return self._send(200, runtime.jobs[match.group(2)])
                match = re.fullmatch(r"/v1/sessions/([^/]+)", parsed.path)
                if match:
                    return self._send(200, runtime.view(runtime.sessions[match.group(1)]))
                if parsed.path == "/v1/resources":
                    q = parse_qs(parsed.query)
                    dataset = q.get("dataset", ["evovling_tools"])[0]
                    root = runtime.roots[dataset]
                    version, split, domain = q.get("version", ["1"])[0], q.get("split", ["test"])[0], q.get("domain", [None])[0]
                    rows = _rows(root, version, split, domain)
                    task_id = q.get("task_id", [None])[0]
                    raw = next(row for row in rows if not task_id or row["task_id"] == task_id)
                    mode = q.get("mode", ["none" if _kind(dataset) == "skills" else "accumulative"])[0]
                    include = q.get("include_content", ["true"])[0].lower() != "false"
                    resource = _resources(root, dataset, raw, mode, version, domain, include)
                    return self._send(200, {"selector": {"task_id": raw["task_id"]},
                                            "resource": resource})
                match = re.fullmatch(r"/v1/sessions/([^/]+)/inputs/(.+)", parsed.path)
                if match:
                    session = runtime.sessions[match.group(1)]
                    root = Path(session["state"]["base_dir"]).resolve()
                    target = (root / unquote(match.group(2))).resolve()
                    target.relative_to(root)
                    if not target.is_file():
                        raise FileNotFoundError(target)
                    return self._send(200, target.read_bytes(), "application/octet-stream")
                match = re.fullmatch(r"/v1/sessions/([^/]+)/inputs", parsed.path)
                if match:
                    session = runtime.sessions[match.group(1)]
                    root = Path(session["state"]["base_dir"])
                    files = [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size}
                             for p in root.rglob("*") if p.is_file()]
                    return self._send(200, {"input_files": files})
                self._send(404, {"detail": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/v1/sessions":
                    return self._send(200, runtime.create(body))
                match = re.fullmatch(r"/v1/sessions/([^/]+)/run_(?:agent|codex)", parsed.path)
                if match:
                    sid = match.group(1)
                    if body.get("background"):
                        jid = "job_" + uuid.uuid4().hex[:16]
                        job = {"job_id": jid, "session_id": sid, "status": "pending",
                               "created_at": time.time(), "updated_at": time.time()}
                        runtime.jobs[jid] = job
                        def work():
                            job["status"], job["updated_at"] = "running", time.time()
                            try:
                                job["result"] = runtime.run_agent(sid, body)
                                job["status"] = "done"
                            except Exception as exc:  # noqa: BLE001
                                job["error"] = {"status_code": 502, "detail": str(exc)}
                                job["status"] = "error"
                            job["updated_at"] = time.time()
                        threading.Thread(target=work, daemon=True).start()
                        return self._send(200, {"job_id": jid, "session_id": sid,
                                                "status": "pending"})
                    return self._send(200, runtime.run_agent(sid, body))
                match = re.fullmatch(r"/v1/sessions/([^/]+)/grade", parsed.path)
                if match:
                    keep = parse_qs(parsed.query).get("keep_alive", ["false"])[0].lower() == "true"
                    return self._send(200, runtime.grade(match.group(1), keep))
                match = re.fullmatch(r"/v1/sessions/([^/]+)/submit", parsed.path)
                if match:
                    session = runtime.sessions[match.group(1)]
                    output = Path(session["state"]["output_dir"]).resolve()
                    written = []
                    for item in body.get("files") or []:
                        target = (output / item["path"]).resolve()
                        target.relative_to(output)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        data = (base64.b64decode(item["content_b64"])
                                if item.get("content_b64") is not None
                                else str(item.get("content") or "").encode())
                        target.write_bytes(data)
                        written.append(target.relative_to(output).as_posix())
                    return self._send(200, {"session_id": match.group(1), "written": written})
                self._send(404, {"detail": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

        def do_DELETE(self) -> None:  # noqa: N802
            try:
                match = re.fullmatch(r"/v1/sessions/([^/]+)", urlparse(self.path).path)
                if not match:
                    return self._send(404, {"detail": "not found"})
                self._send(200, runtime.close(match.group(1)))
            except Exception as exc:  # noqa: BLE001
                self._error(exc)

    return Handler


def serve(manifest: str | Path, approved: set[str], host: str, port: int) -> None:
    runtime = LocalRuntime(manifest, approved)
    server = ThreadingHTTPServer((host, port), _handler(runtime))
    try:
        server.serve_forever()
    finally:
        server.server_close()
