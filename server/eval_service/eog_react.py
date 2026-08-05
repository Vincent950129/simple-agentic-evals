"""Run the reference EnterpriseOps-Gym ReAct agent server-side for a session.

The evolving-*tools* track drives EoG with EnterpriseOps-Gym's own reference
``ReactOrchestrator`` (see ``evolve_tools/src/runner.py``). This module runs that
*same* reference agent on the host, bound to an already-provisioned eval_service
session: the gym is local (the host's reachable gym URLs), the DB is the
session's seeded DB, and grading + teardown stay the service's job (the client
calls ``POST .../grade`` afterwards). So a caller gets exactly the reference
agent, with no local install and no local gym.

We reuse the reference ``BenchmarkExecutor`` + ``ReactOrchestrator`` unchanged
and only redirect its three environment seams (mirroring
``_run_single_task_via_service`` in the tools harness):

  * seed     -> the session already seeded a per-gym DB; bind it (short-circuit
                ``create_database_from_file`` to return the existing id).
  * act      -> ``gym_servers_config`` points the reference ``MCPClient`` at the
                local gym with the row's context; the bound ``database_id``
                scopes tool calls to the session DB.
  * grade    -> skipped in-run (``_run_verifiers`` -> ``{}``); the service's
                ``/grade`` reads the mutated DB with the reference verifier.
  * teardown -> ``delete_database`` is a no-op (the session lifecycle frees it).

Because the reference executor seeds/deletes DBs through *module-level*
functions (``benchmark.executor.create_database_from_file`` /
``delete_database``), running it in-process would require monkeypatching those
globals -- unsafe under concurrent sessions (one run could bind another's DB).
So we run each React trial in an **isolated subprocess** (this module's
``__main__``), which also keeps langchain out of the main service process and
off its event loop. The caller's OpenAI key is passed via the environment, never
on argv.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ._harness.config import EOG_ROOT

logger = logging.getLogger("eval_service.eog_react")

# The EnterpriseOps-Gym reference tree (benchmark/, orchestrators/, ...); set
# ``$EOG_ROOT`` when the gym sources live outside this checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE = EOG_ROOT

# Default ReAct model when the caller doesn't pass one (OpenAI, matching the
# reference LLMClient's ``openai`` provider path).
DEFAULT_REACT_MODEL = os.environ.get("EVAL_SERVICE_REACT_MODEL", "gpt-4o")
DEFAULT_MAX_TOKENS = int(os.environ.get("EVAL_SERVICE_REACT_MAX_TOKENS", "16384"))


# --------------------------------------------------------------------------- #
# Payload construction (main service process)                                 #
# --------------------------------------------------------------------------- #
def _self_base() -> str:
    """Loopback base URL for this service (server-side runs never leave the host).

    Defaults to ``http://127.0.0.1:$PORT`` (``run.sh`` binds 8077); override with
    ``EVAL_SERVICE_SELF_URL`` if the service listens elsewhere.
    """
    return (os.environ.get("EVAL_SERVICE_SELF_URL", "").strip()
            or f"http://127.0.0.1:{os.environ.get('PORT', '8077')}")


def _auth_config_from_headers(headers: dict[str, Any]) -> dict[str, Any] | None:
    """Reference ``MCPClient`` ``auth_config`` from proxy headers (bearer only).

    Mirrors ``evolve_tools.service_backend_tools._auth_config_from_headers``.
    """
    for k, v in (headers or {}).items():
        if k.lower() == "authorization" and isinstance(v, str) and v.startswith("Bearer "):
            return {"type": "bearer", "token": v[len("Bearer "):]}
    return None


def _gyms_from_action(action: dict[str, Any], self_base: str) -> list[dict[str, Any]]:
    """Point the reference ReAct agent at THIS service's MCP proxy.

    Mirrors ``evolve_tools.service_backend_tools.proxy_gym_servers``: the proxy
    URL already is the full MCP endpoint (``mcp_endpoint=""``) and the proxy
    injects the session's DB + identity headers (``x-database-id``, ``user-id``,
    context) so the agent hits the exact DB the verifier reads. We rewrite the
    (possibly public) URL to a loopback base so a server-side run stays on-host.
    """
    base = (self_base or "").rstrip("/")
    gyms: list[dict[str, Any]] = []
    for srv in (action or {}).get("mcp_servers", []) or []:
        headers = srv.get("headers") or {}
        low = {k.lower(): v for k, v in headers.items()}
        path = srv.get("path") or ""
        url = (base + path) if (base and path) else (srv.get("url") or "")
        gyms.append({
            "mcp_server_name": srv.get("name") or "default",
            "mcp_server_url": url,
            "mcp_endpoint": "",                       # url is already the full endpoint
            "database_id": low.get("x-database-id", ""),
            "auth_config": _auth_config_from_headers(headers),
        })
    return gyms


def build_payload(
    row: Any,
    action: dict[str, Any],
    self_base: str,
    *,
    model: str | None,
    restrict_to_selected_tools: bool,
    max_iterations: int | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """Serialize everything the subprocess needs from an EoG session.

    ``row`` is the harness ``TaskRow`` (prompts/verifiers/gold tools); ``action``
    is the session's MCP action (proxy URLs + DB-binding headers). We point the
    agent at this service's MCP proxy (via ``self_base``) so it acts on exactly
    the DB the verifier will read -- no re-discovery, no re-seed.
    """
    from ._harness.endpoints import patch_row

    patched = patch_row(row)
    return {
        "task_id": patched.task_id,
        "system_prompt": patched.system_prompt or "",
        "user_prompt": patched.user_prompt or "",
        "verifiers": list(patched.verifiers or []),
        "selected_tools": list(patched.selected_tools or []),
        "gyms": _gyms_from_action(action, self_base),
        "model": model or DEFAULT_REACT_MODEL,
        "restrict_to_selected_tools": bool(restrict_to_selected_tools),
        "max_iterations": max_iterations,
        "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
    }


async def run_react_session(
    row: Any,
    action: dict[str, Any],
    *,
    self_base: str | None = None,
    openai_api_key: str | None = None,
    model: str | None = None,
    restrict_to_selected_tools: bool = False,
    max_iterations: int | None = None,
    max_tokens: int | None = None,
    timeout_s: float = 1800.0,
    include_trace: bool = False,
) -> dict[str, Any]:
    """Run the reference ReAct agent for this session in an isolated subprocess.

    ``action`` is the session's MCP action (proxy servers + DB-binding headers);
    the agent is pointed at this service's MCP proxy (``self_base``, default
    loopback). Returns the run summary (``n_calls``, ``steps``, ``latency_s``,
    ``total_tokens``, ``final_message``, ...). With ``include_trace`` the summary
    also carries a per-step ``trace``. Raises ``RuntimeError`` on a hard failure
    (non-zero exit with no summary).
    """
    payload = build_payload(
        row, action, self_base or _self_base(), model=model,
        restrict_to_selected_tools=restrict_to_selected_tools,
        max_iterations=max_iterations, max_tokens=max_tokens,
    )
    if include_trace:
        payload["include_trace"] = True

    env = dict(os.environ)
    if openai_api_key:
        env["OPENAI_API_KEY"] = openai_api_key
    # Keep eval_service + the reference tree importable regardless of cwd.
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + existing_pp if existing_pp else "")

    # The reference React stack is langchain-coupled. If the service's own
    # interpreter lacks langchain, point ``EVAL_SERVICE_REACT_PYTHON`` at one
    # that has it (e.g. the tools-harness venv); default = this interpreter.
    python_bin = os.environ.get("EVAL_SERVICE_REACT_PYTHON", "").strip() or sys.executable

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="eog_react_") as tmp:
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        # start_new_session: own process group, so a timeout can kill the agent
        # *and* anything it spawned. Killing only the direct child strands its
        # descendants on PID 1, where they accumulate as zombies.
        proc = await asyncio.create_subprocess_exec(
            python_bin, "-m", "eval_service.eog_react", str(payload_path),
            cwd=str(_REPO_ROOT), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            # Bounded drain: descendants inherit the pipe write ends, so an
            # unbounded communicate() can block forever even after SIGKILL.
            try:
                await asyncio.wait_for(proc.communicate(), timeout=30)
            except (asyncio.TimeoutError, ProcessLookupError):
                logger.warning("reference React run: pipes still open after SIGKILL")
            raise RuntimeError(f"reference React run timed out after {timeout_s:.0f}s")
    latency_s = time.monotonic() - started

    stdout = (out or b"").decode("utf-8", errors="replace")
    stderr = (err or b"").decode("utf-8", errors="replace")
    summary = _parse_summary(stdout)
    if summary is None:
        tail = "\n".join(stderr.strip().splitlines()[-20:])
        raise RuntimeError(
            f"reference React run produced no summary (exit={proc.returncode}). "
            f"stderr tail:\n{tail}"
        )
    # Wall-clock for the whole server-side run (subprocess spawn + agent loop).
    summary["latency_s"] = round(latency_s, 3)
    # A summary with an ``error`` means the reference executor caught a per-run
    # failure (e.g. LLM/MCP error) -- surface it (the stderr has the traceback)
    # so a stopped=="error" run isn't a silent no-op.
    if summary.get("error"):
        tail = "\n".join(stderr.strip().splitlines()[-25:])
        logger.warning(
            "reference React run reported an error (task=%s): %s\nstderr tail:\n%s",
            summary.get("task_id", ""), summary.get("error"), tail,
        )
    return summary


def _parse_summary(stdout: str) -> dict[str, Any] | None:
    """Pull the ``__EOG_REACT_SUMMARY__ {json}`` line the subprocess prints."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("__EOG_REACT_SUMMARY__ "):
            try:
                return json.loads(line[len("__EOG_REACT_SUMMARY__ "):])
            except json.JSONDecodeError:
                return None
    return None


# --------------------------------------------------------------------------- #
# Subprocess entry point: build config, run the reference executor            #
# --------------------------------------------------------------------------- #
async def _execute_react(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one reference ReAct trial from a serialized payload (subprocess)."""
    if str(_REFERENCE) not in sys.path:
        sys.path.insert(0, str(_REFERENCE))

    import benchmark.executor as _ex  # langchain-coupled; import here (subprocess)
    from benchmark.executor import BenchmarkExecutor
    from benchmark.models import BenchmarkConfig, LLMConfig
    from orchestrators.react import ReactOrchestrator

    gyms = payload.get("gyms") or []
    # gym_servers_config the reference MCPClient understands. The URL is the
    # service's MCP proxy (full endpoint -> ``mcp_endpoint=""``), which injects
    # the session's DB/identity headers; ``create_database_from_file`` is
    # short-circuited (no re-seed) to bind the already-seeded DB.
    gym_servers_config: list[dict[str, Any]] = []
    url_to_db: dict[str, str] = {}
    for g in gyms:
        url = g.get("mcp_server_url") or ""
        gym_servers_config.append({
            "mcp_server_name": g.get("mcp_server_name") or "default",
            "mcp_server_url": url,
            "mcp_endpoint": g.get("mcp_endpoint", ""),   # "" -> url is the full endpoint
            "context": {},                               # proxy injects context headers
            "seed_database_file": "",
            "auth_config": g.get("auth_config"),
        })
        if g.get("database_id"):
            url_to_db[url] = g["database_id"]

    restrict = bool(payload.get("restrict_to_selected_tools"))
    config = BenchmarkConfig(
        system_prompt=payload.get("system_prompt", "") or "",
        user_prompt=payload.get("user_prompt", "") or "",
        verifiers=list(payload.get("verifiers") or []),
        number_of_runs=1,
        gym_servers_config=gym_servers_config,
        selected_tools=(list(payload.get("selected_tools") or []) if restrict else None),
        restricted_tools=[],
        temperature=0.0,
        max_tokens=int(payload.get("max_tokens") or DEFAULT_MAX_TOKENS),
    )
    llm_config = LLMConfig(
        llm_provider="openai",
        llm_model=payload.get("model") or DEFAULT_REACT_MODEL,
        llm_api_key=os.environ.get("OPENAI_API_KEY", ""),
        temperature=0.0,
        max_tokens=int(payload.get("max_tokens") or DEFAULT_MAX_TOKENS),
    )

    class _SessionExecutor(BenchmarkExecutor):
        async def _run_verifiers(self, task_result):
            # Grading is the service's /grade job (the bit-for-bit reference
            # verifier over the mutated DB), so we skip the in-run verifiers.
            # The reference executor's run-summary log divides by the number of
            # verifiers, so return one neutral placeholder (discarded by the
            # caller) rather than an empty dict, which would raise ZeroDivision.
            return {"grading_deferred_to_service": {
                "passed": True, "expected": None, "actual": None,
                "comparison_type": "deferred", "error": None,
            }}

    orig_seed = _ex.create_database_from_file
    orig_del = _ex.delete_database
    # Bind the agent's tools to the session's already-seeded DB instead of
    # creating a new one; never delete it here (the session lifecycle does).
    _ex.create_database_from_file = (
        lambda gym_url, seed_file, _m=url_to_db:
        _m.get((gym_url or "").rstrip("/")) or next(iter(_m.values()), None)
    )
    _ex.delete_database = lambda *a, **k: True
    try:
        orchestrator_kwargs: dict[str, Any] = {}
        if payload.get("max_iterations"):
            orchestrator_kwargs["max_iterations"] = int(payload["max_iterations"])
        executor = _SessionExecutor(
            config, llm_config=llm_config,
            orchestrator_class=ReactOrchestrator,
            orchestrator_kwargs=orchestrator_kwargs, config_path="config.json",
        )
        result = await executor.execute_benchmark()
    finally:
        _ex.create_database_from_file = orig_seed
        _ex.delete_database = orig_del

    return _summarize(
        payload.get("task_id", ""), result,
        include_trace=bool(payload.get("include_trace")),
    )


def _truncate_for_trace(v: Any, limit: int = 2000) -> str:
    """Stringify + clip a tool result so a trace stays a sane size on the wire."""
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= limit else s[:limit] + f"... (+{len(s) - limit} chars)"


def _build_react_trace(
    conversation: list[dict[str, Any]], *, max_result_chars: int = 2000
) -> list[dict[str, Any]]:
    """Per-step trace from ``conversation_flow``: thought + tool calls/results.

    One entry per reason/act step (each ``ai_message`` and the ``tool_result``
    turns that follow it), with the step's token ``usage``. Tool results are
    truncated so a verbose run doesn't return a huge payload.
    """
    steps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for m in conversation:
        t = m.get("type")
        if t == "ai_message":
            if cur is not None:
                steps.append(cur)
            um = m.get("usage_metadata") or {}
            cur = {
                "thought": m.get("content") or "",
                "tool_calls": [
                    {"name": tc.get("name"), "args": tc.get("args")}
                    for tc in (m.get("tool_calls") or [])
                ],
                "tool_results": [],
                "usage": {
                    "input_tokens": int(um.get("input_tokens") or 0),
                    "output_tokens": int(um.get("output_tokens") or 0),
                    "total_tokens": int(um.get("total_tokens") or 0),
                },
            }
        elif t == "tool_result":
            if cur is None:
                cur = {"thought": "", "tool_calls": [], "tool_results": [], "usage": {}}
            cur["tool_results"].append({
                "tool_name": m.get("tool_name"),
                "result": _truncate_for_trace(m.get("result"), max_result_chars),
            })
    if cur is not None:
        steps.append(cur)
    return steps


def _sum_react_tokens(conversation: list[dict[str, Any]]) -> dict[str, int]:
    """Sum LLM token usage over the ReAct conversation's ``ai_message`` turns.

    Each ai_message carries langchain ``usage_metadata`` (input/output/total);
    falls back to input+output when a turn omits ``total_tokens``.
    """
    tin = tout = ttot = 0
    for m in conversation:
        if m.get("type") != "ai_message":
            continue
        um = m.get("usage_metadata") or {}
        i = int(um.get("input_tokens") or um.get("prompt_tokens") or 0)
        o = int(um.get("output_tokens") or um.get("completion_tokens") or 0)
        ttot += int(um.get("total_tokens") or 0) or (i + o)
        tin += i
        tout += o
    return {"input_tokens": tin, "output_tokens": tout, "total_tokens": ttot}


def _summarize(
    task_id: str, result: dict[str, Any], *, include_trace: bool = False
) -> dict[str, Any]:
    """Reference executor result -> the uniform run-summary wire shape."""
    runs = result.get("runs") or [{}]
    run0 = runs[0] if runs else {}
    tool_results = run0.get("tool_results", []) or []
    conversation = run0.get("conversation_flow", []) or []
    steps = sum(1 for m in conversation if m.get("type") == "ai_message")
    tokens = _sum_react_tokens(conversation)
    err = run0.get("error")
    summary = {
        "agent": "react",
        "task_id": task_id,
        "n_calls": len(tool_results),
        "n_exec": 0,
        "episodes": 1,
        "steps": steps,
        "completed": err is None,
        "stopped": "error" if err else "done",
        "exit_code": 0 if err is None else -1,
        "thread_id": None,
        "total_tokens": tokens["total_tokens"],
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "final_message": str(run0.get("model_response") or ""),
        "tools_used": list(run0.get("tools_used", []) or []),
        "error": (str(err) if err else None),
    }
    if include_trace:
        summary["trace"] = _build_react_trace(conversation)
    return summary


def _main(argv: list[str]) -> int:
    logging.basicConfig(
        level=os.environ.get("EVAL_SERVICE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if len(argv) < 2:
        print("usage: python -m eval_service.eog_react <payload.json>", file=sys.stderr)
        return 2
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        summary = asyncio.run(_execute_react(payload))
    except Exception as e:  # noqa: BLE001 - surface as a summary-less failure
        logger.exception("reference React run failed")
        print(f"reference React run failed: {e}", file=sys.stderr)
        return 1
    # The parent parses this sentinel line out of stdout.
    print("__EOG_REACT_SUMMARY__ " + json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
