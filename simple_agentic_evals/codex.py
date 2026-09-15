"""Run Codex against a task -- executed by the service, not your machine.

:func:`acp_codex_agent` is the one-liner: the **service** runs Codex for you (the
reference harness's ACP-based Codex runner) using the OpenAI ``api_key`` you pass.
There is **no local ``codex`` binary, no ``auth.json``, and no gym/sandbox** to
install -- the service runs the agent against your session's environment, so
behavior matches the reference harness exactly:

    from simple_agentic_evals import EvalClient, acp_codex_agent

    client = EvalClient()                        # endpoint + auth from the env
    task = client.task("evovling_skills", "eog", 1, task_id, domain="hr")
    with task:
        run = acp_codex_agent(task, api_key="sk-...", model="gpt-5")
        print(task.grade().pass_rate, run.latency_s, run.total_tokens)

Works for **EOG** (skills/agents/tools -- the run mutates the session DB;
``task.grade()`` reads it with the reference verifier) **and ALE** (the Codex CLI
agent solves + grades inside the sandbox via ale_run; ``task.grade()`` returns
that inline score). This is the only supported harness for ALE.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import _render
from .client import resolve_openai_key

logger = logging.getLogger(__name__)

__all__ = ["acp_codex_agent", "CodexRun", "DEFAULT_COMPLETION_SENTINEL"]

DEFAULT_COMPLETION_SENTINEL = "TASK_COMPLETE"

# Default per-run wall-clock budget (seconds), resolved from the task's benchmark
# when the caller leaves ``timeout_s=None``. EOG runs are light (900s). ALE is far
# heavier -- sandbox boot + a long agent solve + the in-sandbox scorer -- so it
# defaults to a reference-parity budget: ale_run gives the agent phase 7200s
# (lifecycle._DEFAULT_TIMEOUT_S), and the extra headroom covers boot + grading.
_EOG_DEFAULT_TIMEOUT_S = 900.0
_ALE_DEFAULT_TIMEOUT_S = 9000.0


def _default_contract(sentinel: str) -> str:
    # Byte-for-byte the reference harness's CODEX_COMPLETION_CONTRACT (see
    # evovle_skills/src/config.py). The service uses its own harness default;
    # this is kept so callers can inspect/override the sentinel wording.
    return (
        "\n\n---\n"
        "# Completion protocol\n"
        "When -- and ONLY when -- you have finished every required action via "
        "tool calls (or have determined you genuinely cannot proceed and have "
        "stated the specific reason), end your final message with the token "
        f"`{sentinel}` on its own line. Never end your turn with "
        "a plan or a status update such as \"now doing X\" or \"investigating "
        "Y\": either perform the action with a tool call, or finish and output "
        f"`{sentinel}`."
    )


@dataclass
class CodexRun:
    """Result of an :func:`acp_codex_agent` trial (run by the service).

    ``n_calls`` counts gym MCP tool calls; ``stopped`` is ``"done"`` (agent
    signaled completion), ``"max_episodes"``, ``"timeout"``, or ``"error"``.
    ``n_subagent_spawns`` is set for the multi-agent track (else None).

    ``cache_read_tokens`` / ``cost_usd`` / ``n_steps`` are ALE-only: ale_run
    separates cache reads and prices each trial, which the EOG rollout tally
    cannot do, so they stay None on EOG runs.
    """

    n_calls: int = 0
    n_exec: int = 0
    episodes: int = 0
    stopped: str = ""
    completed: bool = False
    exit_code: int = 0
    thread_id: str | None = None
    final_message: str = ""
    # Metrics (accuracy comes separately from ``task.grade()``):
    latency_s: float | None = None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    n_steps: int | None = None
    n_subagent_spawns: int | None = None
    # Structured trace (only when include_trace / verbose>=steps):
    trace: list[dict[str, Any]] = field(default_factory=list, repr=False)
    home: Any = None


def _signaled_complete(message: str, sentinel: str) -> bool:
    """True iff ``message`` contains ``sentinel`` on its own line."""
    if not message:
        return False
    return re.search(rf"(?m)^\s*{re.escape(sentinel)}\s*$", message) is not None


def _opt_int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) else None


def acp_codex_agent(
    task: Any,
    *,
    model: str | None = None,
    api_key: str | None = None,
    transport: str | None = None,
    allowed_tools: list[str] | None = None,
    mcp_only: bool | None = None,
    max_episodes: int = 4,
    timeout_s: float | None = None,
    require_completion: bool = True,
    completion_sentinel: str = DEFAULT_COMPLETION_SENTINEL,
    verbose: bool | str = False,
    include_trace: bool | None = None,
    prompt_suffix: str | None = None,
    sandbox_env: dict[str, str] | None = None,
    memory_key: str | None = None,
    memory_main_only: bool = False,
) -> CodexRun:
    """Run **Codex** (ACP) against a task -- on the service.

    The service runs Codex (the reference harness's ACP runner) using the OpenAI
    ``api_key`` you pass; you need **no** local ``codex`` binary, ``auth.json``,
    gym, or sandbox. For EOG it runs a completion loop against this session's gym
    + DB (re-driving with a nudge until the ``TASK_COMPLETE`` sentinel, up to
    ``max_episodes``, sharing one ``timeout_s``). For ALE it drives the Codex CLI
    agent inside the sandbox via ale_run (which solves *and* grades the task).

    ``allowed_tools`` (non-empty) restricts Codex to the task's gold tool set
    (oracle mode; EOG). Provisions the session if needed; does **not** close it.
    Call ``task.grade()`` afterwards (EOG grades the mutated DB; ALE returns the
    inline sandbox score). ``verbose`` is a level (``False``/``"summary"``/
    ``"steps"``/``"full"``); ``include_trace`` forces the structured ``trace``.

    ``timeout_s`` is the total wall-clock budget; left as ``None`` it defaults per
    benchmark -- 900s for EOG, and a reference-parity budget for ALE (the reference
    ale_run gives the agent phase 7200s, and heavy ALE tasks need it).

    ``prompt_suffix`` (ALE) is appended to the task prompt. On the multi-agent
    track it must carry the delegation protocol + specialist roster, because the
    orchestrator has no tools of its own; unset, the service falls back to the
    row's baked (cumulative-pool) system_prompt.

    ``sandbox_env`` (ALE) is forwarded into the sandbox by ale_run. Only
    ``ALE_AGENTS_*`` / ``ALE_GUARD_*`` keys are accepted: the specialist-pool
    bundle the in-sandbox stager unpacks, and the hard software fence. Omit it and
    the run has no specialists to spawn and only a soft (prompt-level) allowlist.

    ``memory_key`` (EOG agents track) turns on Codex's own memory: the service
    enables ``[features] memories`` and runs the trial in a persistent
    ``CODEX_HOME`` it keeps for that key, so Codex accumulates memory across every
    trial you send with the same key. Codex writes the memory itself; pass a key
    that is stable for one experiment and unique across experiments.

    ``memory_main_only`` keeps that memory to the multi-agent orchestrator: each
    specialist it spawns is given Codex's per-agent ``use_memories = false``, so
    only the agent doing the routing is informed by earlier trials. Ignored
    without ``memory_key``.
    """
    task.start()
    is_ale = (task.benchmark or "").lower() == "ale"
    if timeout_s is None:
        timeout_s = (
            _ALE_DEFAULT_TIMEOUT_S if is_ale else
            float(task.action.get("timeout_sec") or _EOG_DEFAULT_TIMEOUT_S)
        )
    managed = task.action_type in ("terminal", "managed_runtime")
    if not task.mcp_servers and not is_ale and not managed:
        raise RuntimeError(
            f"task {task.task_id!r} exposes no MCP servers "
            f"(action_type={task.action_type!r}); no compatible Codex runtime is advertised.")
    if task.session_id is None:
        raise RuntimeError("call task.start() (or use `with task:`) first")

    level = _render.normalize_verbose(verbose)
    body: dict[str, Any] = {
        "agent": "codex",
        "openai_api_key": resolve_openai_key(api_key),
        "model": model,
        "restrict_to_selected_tools": bool(allowed_tools),
        "max_episodes": int(max_episodes),
        "timeout_s": int(timeout_s),
        "require_completion": bool(require_completion),
        "completion_sentinel": completion_sentinel,
        "include_trace": _render.resolve_include_trace(level, include_trace),
    }
    if prompt_suffix is not None:
        body["prompt_suffix"] = prompt_suffix
    if sandbox_env:
        body["sandbox_env"] = dict(sandbox_env)
    if memory_key:
        body["memory_key"] = memory_key
        if memory_main_only:
            body["memory_main_only"] = True
    if transport is not None:
        body["transport"] = transport
    if mcp_only is not None:
        body["mcp_only"] = bool(mcp_only)

    # Submit as a background job and poll: a Codex/ALE run can outlast an upstream
    # proxy/tunnel's per-request timeout, so we keep each request short instead.
    d = task._client._run_agent_job(
        task.session_id, body,
        max_wait=float(timeout_s) + 300.0,
        on_poll=_render.make_heartbeat("codex", level),
    )
    run = CodexRun(
        n_calls=int(d.get("n_calls", 0) or 0),
        n_exec=int(d.get("n_exec", 0) or 0),
        episodes=int(d.get("episodes", 0) or 0),
        stopped=str(d.get("stopped", "") or ""),
        completed=bool(d.get("completed", False)),
        exit_code=int(d.get("exit_code", 0) or 0),
        thread_id=d.get("thread_id"),
        final_message=str(d.get("final_message", "") or ""),
        latency_s=(float(d["latency_s"]) if isinstance(d.get("latency_s"), (int, float)) else None),
        total_tokens=_opt_int(d.get("total_tokens")),
        input_tokens=_opt_int(d.get("input_tokens")),
        output_tokens=_opt_int(d.get("output_tokens")),
        cache_read_tokens=_opt_int(d.get("cache_read_tokens")),
        cost_usd=(float(d["cost_usd"]) if isinstance(d.get("cost_usd"), (int, float)) else None),
        n_steps=_opt_int(d.get("n_steps")),
        n_subagent_spawns=_opt_int(d.get("n_subagent_spawns")),
        trace=list(d.get("trace") or []),
        home=None,
    )
    _render.emit(run, level, label="codex")
    return run
