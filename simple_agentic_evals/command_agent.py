"""Fresh-process command adapter for portable agent evaluation."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["CommandAgent", "CommandAgentError", "materialize_task_workspace"]


class CommandAgentError(RuntimeError):
    """A task command failed or violated the adapter contract."""


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return value[:100] or "task"


def _inside(root: Path, relative: str) -> Path:
    out = (root / relative).resolve()
    if out != root.resolve() and root.resolve() not in out.parents:
        raise ValueError(f"resource path escapes workspace: {relative!r}")
    return out


def _resource_relative(kind: str, source: str, fallback: str) -> Path:
    parts = Path(source).parts
    marker = "skills" if kind == "skills" else ("agents" if source.endswith(".toml") else "agent_skills")
    if marker in parts:
        return Path(*parts[parts.index(marker) + 1 :])
    return Path(fallback) / Path(source).name


def _write_resources(root: Path, resource: Mapping[str, Any]) -> None:
    kind = str(resource.get("kind") or "unknown")
    for item in resource.get("items") or []:
        name = _safe_name(str(item.get("name") or "resource"))
        for entry in item.get("files") or []:
            content = entry.get("content")
            if content is None:
                continue
            source = str(entry.get("path") or "")
            rel = _resource_relative(kind, source, name)
            if kind == "skills":
                dest = _inside(root, str(Path("skills") / rel))
            elif source.endswith(".toml"):
                dest = _inside(root, str(Path("agents") / rel))
            else:
                dest = _inside(root, str(Path("agent_skills") / rel))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(content), encoding="utf-8")


def _task_resource(task: Any) -> dict[str, Any]:
    selector = task._selector
    return task._client.resources(
        task.dataset,
        task.benchmark,
        selector.get("version", "full"),
        task_id=task.task_id,
        split=selector.get("split", "test"),
        domain=selector.get("domain"),
        mode=selector.get("resource_mode"),
        include_content=True,
    )


def _action_type(task: Any) -> str:
    value = getattr(task, "action_type", "")
    if value:
        return str(value)
    return "mcp" if getattr(task, "benchmark", "") == "eog" else "sandbox"


def _codex_project_config(
    workspace: Path, resource: Mapping[str, Any], remote_mcp_server: Any | None = None,
) -> None:
    """Expose task-scoped skills and agent definitions without copying auth."""
    skills_target = workspace / ".agents" / "skills"
    skills_target.mkdir(parents=True, exist_ok=True)
    for source in (workspace / "resources" / "skills", workspace / "resources" / "agent_skills"):
        if not source.is_dir():
            continue
        for child in source.iterdir():
            target = skills_target / child.name
            if target.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, target)
            elif child.is_file():
                shutil.copy2(child, target)

    lines = ["[features]", "multi_agent = true", ""]
    if remote_mcp_server is not None:
        url = str(remote_mcp_server.url).replace("\\", "\\\\").replace('"', '\\"')
        lines += [
            "[mcp_servers.ale_sandbox]",
            f'url = "{url}"',
            'bearer_token_env_var = "EVAL_SERVICE_API_KEY"',
            "required = true",
            # This server is created only after the caller explicitly selects
            # ale_execution=remote_mcp. Non-interactive Codex cannot answer an
            # approval prompt, so approve this task-scoped server's tools.
            'default_tools_approval_mode = "approve"',
            "startup_timeout_sec = 60",
            "tool_timeout_sec = 3600",
            "",
        ]
    agents_root = workspace / "resources" / "agents"
    metadata = {str(i.get("name")): i for i in resource.get("items") or []}
    if agents_root.is_dir():
        for toml in sorted(agents_root.rglob("*.toml")):
            name = _safe_name(toml.stem).replace("-", "_")
            desc = str(metadata.get(toml.stem, {}).get("description") or f"Task specialist {toml.stem}")
            desc = desc.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            path = str(toml.resolve()).replace("\\", "\\\\").replace('"', '\\"')
            lines += [f"[agents.{name}]", f'description = "{desc}"', f'config_file = "{path}"', ""]
    cfg = workspace / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("\n".join(lines), encoding="utf-8")


def materialize_task_workspace(
    task: Any, workspace: str | Path, *, codex: bool = False,
    fetch_sandbox_inputs: bool = True, remote_mcp_server: Any | None = None,
) -> dict[str, Any]:
    """Create the documented task/input/output/resource command contract."""
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    input_dir, output_dir, resource_dir = root / "input", root / "output", root / "resources"
    input_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    resource_dir.mkdir(exist_ok=True)

    resource = _task_resource(task)
    _write_resources(resource_dir, resource)
    if _action_type(task) == "sandbox" and fetch_sandbox_inputs:
        task.fetch_inputs_to(input_dir)

    allowed = list(resource.get("names") or []) if resource.get("kind") == "tools" else []
    task_doc = {
        "task_id": task.task_id,
        "dataset": task.dataset,
        "benchmark": task.benchmark,
        "selector": {k: v for k, v in task._selector.items() if k != "api_key"},
        "system_prompt": task.system_prompt,
        "user_prompt": task.user_prompt,
        "required_steps": list(getattr(task, "required_steps", []) or []),
        "evaluation": str(getattr(task, "evaluation", "") or ""),
        "output_path": task.output_path,
        "action": dict(getattr(task, "action", {}) or {}),
        "resources": {
            "kind": resource.get("kind"),
            "mode": resource.get("mode"),
            "names": list(resource.get("names") or []),
        },
        "allowed_tools": allowed,
    }
    task_json = root / "task.json"
    task_json.write_text(json.dumps(task_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    state = {
        "base_url": task._client.base_url,
        "session_id": task.session_id,
        "servers": [
            {"name": server.name, "path": server.path, "url": task.mcp_url(server)}
            for server in ([remote_mcp_server] if remote_mcp_server is not None else task.mcp_servers)
        ],
        "allowed_tools": allowed,
        "enforce_allowlist": resource.get("kind") == "tools",
    }
    state_json = root / ".task-state.json"
    state_json.write_text(json.dumps(state, indent=2), encoding="utf-8")
    if codex:
        _codex_project_config(root, resource, remote_mcp_server)
    return {
        "root": root,
        "task_json": task_json,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "resource_dir": resource_dir,
        "state_json": state_json,
        "resource": resource,
    }


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s'\"]+", r"\1[REDACTED]", text)


def _usage_from_codex_json(output: str) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    found = False
    steps = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            for key in totals:
                if usage.get(key) is not None:
                    totals[key] += int(usage[key])
                    found = True
        if event.get("type") in ("item.completed", "turn.completed"):
            steps += 1
    if not found:
        return {"n_steps": steps} if steps else {}
    if not totals["total_tokens"]:
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    totals["n_steps"] = steps
    return totals


class CommandAgent:
    """Adapt a shell-capable agent CLI to ``run_benchmark``.

    The command is never passed to a shell. A fresh process receives the merged
    task prompt on stdin, runs with the task workspace as cwd, and sees only
    documented paths in ``EVAL_*`` environment variables. Optional
    ``usage.json`` may report ``total_tokens`` and ``n_steps``.
    """

    def __init__(
        self,
        command: str | Sequence[str] | None = None,
        *,
        adapter: str = "command",
        adapt_command: str | Sequence[str] | None = None,
        state_dir: str | Path | None = None,
        output_dir: str | Path = "evolve-eval-runs",
        timeout: float = 1800,
        keep_workspaces: bool = True,
        ale_execution: str = "artifact",
    ):
        if adapter not in ("command", "codex"):
            raise ValueError("adapter must be 'command' or 'codex'")
        if adapter == "command" and not command:
            raise ValueError("command adapter requires a command")
        if ale_execution not in ("artifact", "remote_mcp"):
            raise ValueError("ale_execution must be 'artifact' or 'remote_mcp'")
        self.adapter = adapter
        self.command = shlex.split(command) if isinstance(command, str) else list(command or [])
        self.adapt_command = (
            shlex.split(adapt_command) if isinstance(adapt_command, str)
            else list(adapt_command or [])
        )
        self.output_dir = Path(output_dir).resolve()
        self.state_dir = Path(state_dir or (self.output_dir / "state")).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(timeout)
        self.keep_workspaces = keep_workspaces
        self.ale_execution = ale_execution

    def _argv(self) -> list[str]:
        if self.adapter == "codex":
            return [
                "codex", "exec", "--ephemeral", "--json", "--skip-git-repo-check",
                "--sandbox", "workspace-write", "-c", "sandbox_workspace_write.network_access=true", "-",
            ]
        return list(self.command)

    def adapt(self, stage: int, tasks: Any) -> dict[str, Any]:
        """Run an approved adaptation command with persistent state.

        Training prompts are obtained through normal isolated sessions.  No
        grade is requested and no action or verifier state is serialized.
        """
        if not self.adapt_command:
            raise CommandAgentError(
                "self-evolving evaluation needs --adapt-command; deployment "
                "evaluation does not persist learning state"
            )
        snapshots: list[dict[str, Any]] = []
        for task in tasks:
            with task:
                resource = _task_resource(task)
                snapshots.append({
                    "task_id": task.task_id,
                    "dataset": task.dataset,
                    "benchmark": task.benchmark,
                    "selector": dict(task._selector),
                    "system_prompt": task.system_prompt,
                    "user_prompt": task.user_prompt,
                    "resources": {
                        "kind": resource.get("kind"),
                        "mode": resource.get("mode"),
                        "names": list(resource.get("names") or []),
                    },
                })
        adapt_root = self.output_dir / "adaptation" / f"stage-{int(stage):03d}"
        adapt_root.mkdir(parents=True, exist_ok=True)
        stage_path = adapt_root / "stage.json"
        stage_path.write_text(
            json.dumps({"stage": int(stage), "tasks": snapshots}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({
            "EVAL_ADAPT_STAGE_JSON": str(stage_path),
            "EVAL_STATE_DIR": str(self.state_dir),
        })
        started = time.time()
        proc = subprocess.run(
            self.adapt_command, cwd=adapt_root, env=env, text=True,
            capture_output=True, timeout=self.timeout, check=False,
        )
        secrets = [env.get(name, "") for name in (
            "EVAL_SERVICE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "HF_TOKEN"
        )]
        (adapt_root / "stdout.log").write_text(_redact(proc.stdout, secrets), encoding="utf-8")
        (adapt_root / "stderr.log").write_text(_redact(proc.stderr, secrets), encoding="utf-8")
        if proc.returncode:
            raise CommandAgentError(
                f"adaptation command exited {proc.returncode}; see {adapt_root / 'stderr.log'}"
            )
        return {"stage": int(stage), "n_tasks": len(snapshots),
                "latency_s": time.time() - started, "state_dir": str(self.state_dir)}

    def __call__(self, task: Any, **_: Any) -> dict[str, Any]:
        label = f"{_safe_name(task.benchmark)}-{_safe_name(task.task_id)}-{uuid.uuid4().hex[:8]}"
        workspace = self.output_dir / "tasks" / label
        remote = task.benchmark == "ale" and self.ale_execution == "remote_mcp"
        remote_server = None
        if remote:
            task.start_remote_sandbox(timeout=min(self.timeout, 900.0))
            remote_server = task.remote_mcp_server
            if remote_server is None:
                raise CommandAgentError("remote ALE sandbox did not advertise its MCP server")
        paths = materialize_task_workspace(
            task, workspace, codex=self.adapter == "codex",
            fetch_sandbox_inputs=not remote, remote_mcp_server=remote_server,
        )
        prompt = "\n\n".join(p for p in (task.system_prompt, task.user_prompt) if p).strip() + "\n"
        action_type = _action_type(task)
        if action_type == "mcp":
            prompt += (
                "\nUse `evolve-eval tool list` and `evolve-eval tool call NAME --arguments JSON` "
                "to act on the task. Only listed tools are permitted.\n"
            )
        elif action_type == "sandbox" and remote:
            prompt += (
                "\nThis ALE task runs in the hosted sandbox. Use the ale_sandbox MCP tools for "
                "all terminal, filesystem, PTY, clipboard, and desktop work. The local input/output "
                "directories are intentionally empty and are not the graded environment.\n"
            )
        elif action_type == "sandbox":
            prompt += "\nRead EVAL_INPUT_DIR and write the requested deliverable under EVAL_OUTPUT_DIR.\n"
        elif action_type == "terminal":
            prompt += "\nUse the isolated terminal action described in EVAL_TASK_JSON.\n"
        elif action_type == "managed_runtime":
            prompt += "\nFollow the official managed-runtime action described in EVAL_TASK_JSON.\n"
        if paths["resource"].get("names"):
            prompt += "Inspect EVAL_RESOURCE_DIR for the selected task-scoped resources.\n"

        env = os.environ.copy()
        env.update({
            "EVAL_TASK_JSON": str(paths["task_json"]),
            "EVAL_INPUT_DIR": str(paths["input_dir"]),
            "EVAL_OUTPUT_DIR": str(paths["output_dir"]),
            "EVAL_RESOURCE_DIR": str(paths["resource_dir"]),
            "EVAL_TASK_STATE": str(paths["state_json"]),
            "EVAL_USAGE_JSON": str(workspace / "usage.json"),
            "EVAL_STATE_DIR": str(self.state_dir),
        })
        if getattr(task._client, "api_key", ""):
            # Explicit EvalClient(api_key=...) callers get the same behavior as
            # env-based callers, but the value still exists only in child env.
            env["EVAL_SERVICE_API_KEY"] = task._client.api_key
        server_headers = {
            server.name: dict(server.headers or {}) for server in task.mcp_servers if server.headers
        }
        if server_headers:
            env["EVAL_MCP_HEADERS_JSON"] = json.dumps(server_headers)
        started = time.time()
        proc = subprocess.run(
            self._argv(),
            input=prompt,
            text=True,
            cwd=workspace,
            env=env,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        secrets = [env.get(name, "") for name in (
            "EVAL_SERVICE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "HF_TOKEN"
        )]
        secrets.extend(
            str(value)
            for headers in server_headers.values()
            for value in headers.values()
            if value
        )
        (workspace / "stdout.log").write_text(_redact(proc.stdout, secrets), encoding="utf-8")
        (workspace / "stderr.log").write_text(_redact(proc.stderr, secrets), encoding="utf-8")
        if proc.returncode:
            raise CommandAgentError(
                f"agent command exited {proc.returncode}; see {workspace / 'stderr.log'}"
            )

        usage: dict[str, Any] = {}
        usage_file = workspace / "usage.json"
        if usage_file.is_file():
            try:
                raw = json.loads(usage_file.read_text(encoding="utf-8"))
                usage = {
                    key: int(raw[key]) for key in ("total_tokens", "n_steps")
                    if raw.get(key) is not None
                }
            except Exception as exc:
                usage_file.unlink(missing_ok=True)
                raise CommandAgentError(f"invalid usage.json: {exc}") from exc
            # Discard unknown fields so a child cannot accidentally persist an
            # environment dump or credential alongside otherwise valid usage.
            usage_file.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        elif self.adapter == "codex":
            usage = _usage_from_codex_json(proc.stdout)
        if action_type == "sandbox" and not remote and any(paths["output_dir"].rglob("*")):
            task.submit_dir(paths["output_dir"])
        result = {"latency_s": time.time() - started, **usage}
        if remote:
            result["execution_mode"] = "remote_mcp"
        if not self.keep_workspaces:
            shutil.rmtree(workspace)
        return result
