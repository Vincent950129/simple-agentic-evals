"""Shared verbosity + trace rendering for the reference-agent helpers.

``verbose`` is a level -- ``False`` (off) | ``"summary"`` | ``"steps"`` | ``"full"``
(``True`` == ``"steps"``). A structured ``trace`` is only requested from the
service when it's useful (``include_trace=True``, else auto-on for steps/full),
so default runs stay lightweight. ``emit`` prints a run at the chosen level and
handles both trace shapes (ReAct per-step and Codex ACP event log).
"""
from __future__ import annotations

from typing import Any

_LEVELS = ("summary", "steps", "full")


def normalize_verbose(verbose: Any) -> str:
    """Coerce a ``verbose`` argument to a level string (``off`` when disabled)."""
    if verbose is True:
        return "steps"
    if not verbose:
        return "off"
    v = str(verbose).strip().lower()
    return v if v in _LEVELS else "steps"


def resolve_include_trace(level: str, include_trace: bool | None) -> bool:
    """Whether to ask the service for a structured trace.

    Explicit ``include_trace`` wins; otherwise auto-on for ``steps`` / ``full``.
    """
    if include_trace is not None:
        return bool(include_trace)
    return level in ("steps", "full")


def make_heartbeat(label: str, level: str, *, every: float = 15.0):
    """An ``on_poll(status, elapsed, raw)`` that prints liveness for a long run.

    Returns ``None`` unless ``level`` is ``steps``/``full`` (so quiet runs stay
    quiet), and prints at most once per ``every`` seconds so a multi-minute
    background run shows it's alive without spamming the output.
    """
    if level not in ("steps", "full"):
        return None
    state = {"last": -1.0}

    def _cb(status: str, elapsed: float, _raw: Any) -> None:
        if status in ("done", "error"):
            return
        if state["last"] < 0 or elapsed - state["last"] >= every:
            state["last"] = elapsed
            print(f"[{label}] ...{status} ({elapsed:.0f}s)")

    return _cb


def _fmt_latency(run: Any) -> str:
    lat = getattr(run, "latency_s", None)
    return f"{lat:.1f}s" if isinstance(lat, (int, float)) else "n/a"


def _fmt_tokens(run: Any) -> str:
    tot = getattr(run, "total_tokens", None)
    return f"{tot} tokens" if isinstance(tot, int) else "tokens=n/a"


def emit(run: Any, level: str, *, label: str) -> None:
    """Print ``run`` at ``level`` (no-op when ``level == 'off'``)."""
    if level == "off":
        return
    steps = getattr(run, "steps", None)
    if steps is None:
        steps = getattr(run, "episodes", None)
    n_calls = getattr(run, "n_calls", 0)
    line = (f"[{label}] steps={steps} tool_calls={n_calls} "
            f"completed={getattr(run, 'completed', False)} "
            f"stopped={getattr(run, 'stopped', '')!s} "
            f"latency={_fmt_latency(run)} {_fmt_tokens(run)}")
    spawns = getattr(run, "n_subagent_spawns", None)
    if isinstance(spawns, int):
        line += f" agent_calls={spawns}"
    print(line)
    if level in ("steps", "full"):
        _emit_trace(getattr(run, "trace", None) or [])
    if level == "full":
        msg = getattr(run, "final_message", "") or ""
        if msg:
            print(f"[{label}] final_message:\n{msg}")


def _clip(v: Any, n: int) -> str:
    s = v if isinstance(v, str) else str(v)
    return s if len(s) <= n else s[:n] + "..."


def _emit_trace(trace: list[dict[str, Any]]) -> None:
    """Print a structured trace, tolerating both ReAct and Codex shapes."""
    for i, step in enumerate(trace, start=1):
        if not isinstance(step, dict):
            continue
        # ReAct per-step shape: {thought, tool_calls[], tool_results[], usage}
        if "tool_calls" in step or "thought" in step:
            thought = (step.get("thought") or "").strip()
            if thought:
                print(f"  step {i} thought: {_clip(thought, 500)}")
            for tc in step.get("tool_calls") or []:
                print(f"  step {i} tool_call: {tc.get('name')} args={_clip(tc.get('args'), 300)}")
            for tr in step.get("tool_results") or []:
                print(f"  step {i} result[{tr.get('tool_name')}]: {_clip(tr.get('result'), 300)}")
            continue
        # Codex ACP event shape: {type, ...}
        t = step.get("type")
        if t in ("agent_message", "agent_thought", "user_message"):
            print(f"  {t}: {_clip(step.get('text', ''), 500)}")
        elif t == "tool_call":
            print(f"  tool_call: {step.get('title')} ({step.get('kind')}) [{step.get('status')}]")
        elif t == "tool_call_update":
            print(f"  tool_result[{step.get('status')}]: {_clip(step.get('content'), 300)}")
        elif t == "plan":
            print(f"  plan: {_clip(step.get('entries'), 300)}")
        elif t == "prompt_done":
            print(f"  turn_end: stop_reason={step.get('stop_reason')}")
