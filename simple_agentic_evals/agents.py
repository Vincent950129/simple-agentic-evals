"""The reference ReAct agent for EOG tasks -- run by the service.

:func:`react_agent` runs EnterpriseOps-Gym's **reference ReAct agent** on an EOG
task. The **service** runs it for you against your session's gym + DB using the
OpenAI ``api_key`` you pass -- there is no local gym, model client, or binary to
install. It's the same reference agent the benchmark uses, so the score is
comparable.

    from simple_agentic_evals import EvalClient, react_agent

    client = EvalClient()                        # endpoint + auth from the env
    for task in client.tasks("evovling_tools", "eog", version=1, domain="hr", limit=3):
        with task:
            run = react_agent(task, api_key="sk-...")     # runs on the service
            grade = task.grade()
            print(task.task_id, grade.pass_rate, run.latency_s, run.total_tokens)

ReAct is **EOG-only**. ALE tasks require a CLI agent harness -- use
:func:`~simple_agentic_evals.codex.acp_codex_agent` (calling ``react_agent`` on an
ALE task raises a clear error).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import _render
from .client import ServiceError

__all__ = ["react_agent", "AgentRun"]


@dataclass
class AgentRun:
    """Outcome of an EOG ReAct agent run (run by the service)."""
    n_calls: int = 0                 # gym tool calls the agent made
    n_exec: int = 0                  # shell/exec calls (0 for pure ReAct)
    steps: int = 0                   # reason/act iterations taken
    stopped: str = ""                # "done" (agent finished) | "error"
    completed: bool = False
    final_message: str = ""
    # Metrics (accuracy comes separately from ``task.grade()``):
    latency_s: float | None = None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    tools_used: list[str] = field(default_factory=list)
    # Structured per-step trace (only when include_trace / verbose>=steps):
    trace: list[dict[str, Any]] = field(default_factory=list, repr=False)
    messages: list[dict] = field(default_factory=list, repr=False)


def react_agent(
    task: Any,
    *,
    model: str | None = None,
    api_key: str | None = None,
    max_steps: int | None = None,
    restrict_to_selected_tools: bool = False,
    timeout_s: float = 1800.0,
    verbose: bool | str = False,
    include_trace: bool | None = None,
) -> AgentRun:
    """Run EnterpriseOps-Gym's reference ReAct agent on an EOG task.

    The **service** runs the reference agent against this session's gym + DB
    using the OpenAI ``api_key`` you pass (no local gym/model/binary). The agent
    reasons and calls the gym's MCP tools in a loop until it stops (or
    ``max_steps`` iterations). ``restrict_to_selected_tools`` limits it to the
    task's gold tool set (oracle mode). Provisions the session if needed; does
    **not** close it or grade -- call ``task.grade()`` yourself.

    ``verbose`` is a level: ``False`` | ``"summary"`` | ``"steps"`` | ``"full"``
    (``True`` == ``"steps"``); ``include_trace`` forces the structured per-step
    ``trace`` on/off (auto-on for steps/full). Returns an :class:`AgentRun` with
    ``latency_s`` and token totals. **EOG only** -- an ALE task raises.
    """
    if (task.benchmark or "").lower() == "ale":
        raise ServiceError(
            400,
            "ALE requires a CLI agent harness -- use acp_codex_agent "
            "(react_agent is EOG-only).",
        )
    task.start()
    if not task.mcp_servers:
        raise RuntimeError(
            f"task {task.task_id!r} exposes no MCP servers "
            f"(action_type={task.action_type!r}); react_agent is for EOG tasks."
        )
    if task.session_id is None:
        raise RuntimeError("call task.start() (or use `with task:`) first")

    level = _render.normalize_verbose(verbose)
    body: dict[str, Any] = {
        "agent": "react",
        "openai_api_key": api_key,
        "model": model,
        "restrict_to_selected_tools": bool(restrict_to_selected_tools),
        "timeout_s": int(timeout_s),
        "include_trace": _render.resolve_include_trace(level, include_trace),
    }
    if max_steps is not None:
        body["max_iterations"] = int(max_steps)

    # Submit as a background job and poll so a long reason/act loop never rides one
    # long-held request that an upstream proxy/tunnel would time out.
    d = task._client._run_agent_job(
        task.session_id, body,
        max_wait=float(timeout_s) + 300.0,
        on_poll=_render.make_heartbeat("react", level),
    )
    run = _agent_run_from_dict(d)
    _render.emit(run, level, label="react")
    return run


def _opt_int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) else None


def _agent_run_from_dict(d: dict[str, Any]) -> AgentRun:
    run = AgentRun(
        n_calls=int(d.get("n_calls", 0) or 0),
        n_exec=int(d.get("n_exec", 0) or 0),
        steps=int(d.get("steps", 0) or 0),
        stopped=str(d.get("stopped", "") or ""),
        completed=bool(d.get("completed", d.get("stopped") == "done")),
        final_message=str(d.get("final_message", "") or ""),
        latency_s=(float(d["latency_s"]) if isinstance(d.get("latency_s"), (int, float)) else None),
        total_tokens=_opt_int(d.get("total_tokens")),
        input_tokens=_opt_int(d.get("input_tokens")),
        output_tokens=_opt_int(d.get("output_tokens")),
        tools_used=list(d.get("tools_used", []) or []),
        trace=list(d.get("trace") or []),
    )
    if run.final_message:
        run.messages = [{"role": "assistant", "content": run.final_message}]
    return run
