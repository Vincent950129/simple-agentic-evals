"""our-codex-acp -- an ACP server that wraps the Codex CLI.

This module is an executable ACP **server**: launched by the harness as a
subprocess, it reads JSON-RPC on stdin, drives ``codex exec --json``, and
re-emits Codex's native event stream as ACP ``session/update``
notifications.

Run as::

    python -m simple_agentic_evals.acp.codex_server

The driver never imports the server class directly -- it talks to it
over the ACP wire via :class:`simple_agentic_evals.acp.client.ACPClient`.

Why a wrapper at all?
---------------------
We want one uniform interface (ACP) to drive Codex now and Claude / Gemini
later.  This wrapper is the Codex *adapter*: it presents the ACP surface
that the harness already speaks, and translates each Codex `--json` event
into the matching ACP update (event-vocabulary table is below).  Crucially,
we link to Codex via its native CLI -- not via Zed's Rust `codex-acp` shim
-- so Codex updates flow in zero-lag (``npm i -g @openai/codex@latest``)
without waiting on a third-party version bump.

Event mapping (verified against codex-cli 0.137.0)
---------------------------------------------------
=================================== ===================================
Codex `--json` event                ACP session/update (or action)
=================================== ===================================
``thread.started {thread_id}``     captured into SessionState; used for
                                   ``codex exec resume`` on next prompt
``turn.started``                   no-op
``item.started`` command_execution ``tool_call``      (kind=bash, status=in_progress)
``item.completed`` command_exec    ``tool_call_update`` (status from exit_code)
``item.started`` mcp_tool_call     ``tool_call``      (kind=other, status=in_progress)
``item.completed`` mcp_tool_call   ``tool_call_update`` (status from item.status)
``item.completed`` agent_message   ``agent_message_chunk`` (whole text)
``item.completed`` agent_reasoning ``agent_thought_chunk`` (when emitted)
``turn.completed``                 prompt-loop ends, ``stopReason=end_turn``
``turn.failed``                    prompt-loop ends, ``stopReason=refusal``
``error``                          prompt-loop ends, ``stopReason=refusal``
=================================== ===================================

Session semantics
-----------------
One ACP session maps 1:1 to one Codex thread.  The first ``session/prompt``
spawns ``codex exec --json --cd <cwd> -- <prompt>``; the wrapper captures
the resulting ``thread_id``.  Every subsequent ``session/prompt`` on the
same ACP session spawns ``codex exec resume <thread_id> --json -- <prompt>``
so the model sees the prior turn's history (NOT a fresh context).  This is
the same mechanism the legacy ``codex_runner.py`` uses for the stall-guard's
nudge prompts, lifted up to ACP semantics.

What's intentionally simple (v1)
--------------------------------
* No ``session/load`` -- we never need to rehydrate an old thread.
* ``session/cancel`` kills the in-flight ``codex exec`` process *group* (codex
  plus the ``bwrap`` sandboxes it forks per tool call); the harness sends it on
  client-side timeout.
* ``session/new`` ignores the ``mcpServers`` parameter -- the harness pre-
  configures Codex's MCP wiring via ``CODEX_HOME/config.toml`` (see
  ``codex_config.render_codex_home``).  If we later want pure-ACP MCP
  topology, accept ``mcpServers`` here and merge into config.toml on
  session create.

Environment contract (inherited from the parent process)
--------------------------------------------------------
``EVOVLE_CODEX_BIN``       path/name of codex CLI       (default ``codex``)
``CODEX_HOME``             trial-specific codex home    (set by harness)
``OPENAI_API_KEY`` (etc.)  agent auth                   (set by harness)
``EVOVLE_CODEX_VERBOSE``   if ``1``, dump every codex event as a stderr
                            line for debugging the translation table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# --- module config (override via env, no other deps imported here) ---------
CODEX_BIN = os.environ.get("EVOVLE_CODEX_BIN", "codex")
_VERBOSE_RAW = os.environ.get("EVOVLE_CODEX_VERBOSE", "0") not in (
    "0", "", "false", "False",
)

# Server identity advertised in initialize.
_AGENT_NAME = "our-codex-acp"
_AGENT_VERSION = "0.1.0"
_PROTOCOL_VERSION = 0

# Log to stderr so the ACP wire on stdout stays clean.
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("EVOVLE_CODEX_ACP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s our-codex-acp | %(message)s",
)
logger = logging.getLogger("our-codex-acp")


@dataclass
class _SessionState:
    """Per-ACP-session mutable state held by the wrapper."""
    session_id: str
    cwd: str
    model_id: str | None = None
    thread_id: str | None = None  # set after Codex emits thread.started
    n_prompts: int = 0
    extra_args: list[str] = field(default_factory=list)  # currently unused; future hook
    current_codex_proc: asyncio.subprocess.Process | None = None


class CodexAcpServer:
    """Single-process ACP server that adapts Codex CLI.

    The lifecycle is single-client (one stdin/stdout pair), but
    multi-session: a client may open several ``session/new`` results and
    drive them independently.  Within a session we expect prompts to be
    issued sequentially (the harness does not pipeline turns).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._next_session_idx = 1
        self._stdin: asyncio.StreamReader | None = None
        self._stdout_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

    # ---------------------------------------------------------------- run

    async def run(self) -> int:
        """Main loop: read JSON-RPC lines off stdin, dispatch, repeat."""
        loop = asyncio.get_running_loop()
        self._stdin = asyncio.StreamReader(limit=4 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(self._stdin)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        logger.info("server.starting codex_bin=%s pid=%d", CODEX_BIN, os.getpid())
        while not self._shutdown.is_set():
            try:
                raw = await self._stdin.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                logger.warning("server.oversized_input: %s", exc)
                continue
            if not raw:
                logger.info("server.eof_on_stdin -- exiting")
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning("server.bad_json line=%s err=%s", text[:200], exc)
                continue
            await self._dispatch(msg)
        logger.info("server.exiting n_sessions=%d", len(self._sessions))
        return 0

    # ---------------------------------------------------------------- write

    async def _send(self, payload: dict[str, Any]) -> None:
        async with self._stdout_lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    async def _send_response(self, req_id: Any, result: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _send_error(
        self, req_id: Any, code: int, message: str,
    ) -> None:
        await self._send({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message},
        })

    async def _send_update(
        self, session_id: str, update: dict[str, Any],
    ) -> None:
        await self._send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        })

    # ---------------------------------------------------------------- dispatch

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if method == "initialize":
            await self._handle_initialize(req_id, params)
        elif method == "session/new":
            await self._handle_session_new(req_id, params)
        elif method == "session/set_model":
            await self._handle_set_model(req_id, params)
        elif method == "session/prompt":
            await self._handle_prompt(req_id, params)
        elif method == "session/cancel":
            await self._handle_cancel(params)  # notification: no response
        else:
            # JSON-RPC: only respond if it was a request (had an id).
            if req_id is not None:
                await self._send_error(
                    req_id, -32601, f"method not found: {method}",
                )
            else:
                logger.debug("server.notification.ignored method=%s", method)

    # ---------------------------------------------------------------- handlers

    async def _handle_initialize(
        self, req_id: Any, params: dict[str, Any],
    ) -> None:
        client = (params.get("clientInfo") or {})
        logger.info(
            "rpc.initialize client=%s/%s protocol=%s",
            client.get("name", "?"), client.get("version", "?"),
            params.get("protocolVersion"),
        )
        await self._send_response(req_id, {
            "protocolVersion": _PROTOCOL_VERSION,
            "agentInfo": {"name": _AGENT_NAME, "version": _AGENT_VERSION},
            "agentCapabilities": {
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                "mcpCapabilities": {"sse": False, "http": False},
                "loadSession": False,
            },
        })

    async def _handle_session_new(
        self, req_id: Any, params: dict[str, Any],
    ) -> None:
        cwd = params.get("cwd") or os.getcwd()
        session_id = f"sess_{self._next_session_idx}"
        self._next_session_idx += 1
        self._sessions[session_id] = _SessionState(session_id=session_id, cwd=cwd)
        logger.info("rpc.session/new id=%s cwd=%s", session_id, cwd)
        # mcpServers in params is intentionally ignored in v1; see module doc.
        if params.get("mcpServers"):
            logger.debug(
                "rpc.session/new.mcp_servers_ignored n=%d (using CODEX_HOME/config.toml)",
                len(params["mcpServers"]),
            )
        await self._send_response(req_id, {"sessionId": session_id})

    async def _handle_set_model(
        self, req_id: Any, params: dict[str, Any],
    ) -> None:
        sid = str(params.get("sessionId", ""))
        model_id = params.get("modelId")
        state = self._sessions.get(sid)
        if state is None:
            await self._send_error(req_id, -32000, f"unknown session: {sid}")
            return
        state.model_id = str(model_id) if model_id else None
        logger.info("rpc.session/set_model id=%s model=%s", sid, state.model_id)
        await self._send_response(req_id, {})

    async def _handle_cancel(self, params: dict[str, Any]) -> None:
        sid = str(params.get("sessionId", ""))
        state = self._sessions.get(sid)
        if state is None or state.current_codex_proc is None:
            return
        proc = state.current_codex_proc
        try:
            # Whole process group: codex plus the bwrap sandboxes it spawned.
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            logger.warning("rpc.session/cancel killed codex process group pid=%s", proc.pid)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _handle_prompt(
        self, req_id: Any, params: dict[str, Any],
    ) -> None:
        sid = str(params.get("sessionId", ""))
        state = self._sessions.get(sid)
        if state is None:
            await self._send_error(req_id, -32000, f"unknown session: {sid}")
            return

        prompt_blocks = params.get("prompt") or []
        text_parts: list[str] = []
        for blk in prompt_blocks:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text", "")
                if isinstance(t, str) and t:
                    text_parts.append(t)
        prompt_text = "\n".join(text_parts) if text_parts else ""
        if not prompt_text:
            await self._send_error(req_id, -32602, "empty prompt")
            return

        state.n_prompts += 1
        stop_reason, exit_code = await self._run_codex_turn(state, prompt_text)
        logger.info(
            "rpc.session/prompt.done id=%s turn=%d exit=%d stop_reason=%s",
            sid, state.n_prompts, exit_code, stop_reason,
        )
        await self._send_response(req_id, {"stopReason": stop_reason})

    # ------------------------------------------------------------ codex turn

    async def _run_codex_turn(
        self, state: _SessionState, prompt_text: str,
    ) -> tuple[str, int]:
        """Spawn one ``codex exec`` / ``codex exec resume`` and translate events.

        Returns ``(stop_reason, exit_code)``.  ``stop_reason`` is the ACP
        ``StopReason`` value to put in the prompt response.
        """
        is_resume = state.thread_id is not None
        cmd: list[str] = [CODEX_BIN, "exec"]
        if is_resume:
            cmd += ["resume", state.thread_id, "--json"]  # type: ignore[list-item]
        else:
            cmd += ["--json", "--cd", state.cwd]
        if state.model_id:
            cmd += ["--model", state.model_id]
        cmd += ["--", prompt_text]
        logger.info(
            "codex.spawn cwd=%s thread_id=%s n_prompts=%d argv=%s",
            state.cwd, state.thread_id, state.n_prompts,
            # Last argv slot is the (large) prompt; show preview only.
            cmd[:-1] + [f"<prompt len={len(prompt_text)}>"],
        )
        # start_new_session: codex leads its own process group, so cancelling a
        # turn can kill it together with the ``bwrap`` sandbox it forks per tool
        # call. Killing codex alone strands those on PID 1 as zombies.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=state.cwd,
            start_new_session=True,
        )
        state.current_codex_proc = proc
        started_at = time.time()

        stop_reason = "end_turn"
        seen_turn_completed = False
        n_tool_calls = 0

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                # We surface codex stderr as DEBUG; the parent harness's
                # log already shows it.  Keep visible at WARNING when codex
                # itself flags an error.
                txt = line.decode("utf-8", errors="replace").rstrip()
                if not txt:
                    continue
                lvl = logging.WARNING if (" ERROR " in txt or txt.lower().startswith("error")) else logging.DEBUG
                logger.log(lvl, "codex.stderr | %s", txt[:400])

        stderr_task = asyncio.create_task(drain_stderr())

        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # Oversized JSON line: skip up to the next newline.
                logger.warning("codex.oversized_event_line")
                while True:
                    chunk = await proc.stdout.read(1024 * 1024)
                    if not chunk or b"\n" in chunk:
                        break
                continue
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if _VERBOSE_RAW:
                logger.debug("codex.raw_event | %s", line[:500])
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("codex.non_json_event | %s", line[:200])
                continue

            # Capture thread_id for future resume turns.
            etype = str(ev.get("type", ""))
            if etype == "thread.started":
                tid = ev.get("thread_id")
                if isinstance(tid, str) and tid:
                    state.thread_id = tid
                    logger.info("codex.thread.started thread_id=%s", tid)
                continue
            if etype == "turn.started":
                continue
            if etype == "turn.completed":
                seen_turn_completed = True
                # Don't break -- we still want to drain any remaining lines,
                # and codex closes stdout naturally right after this.
                continue
            if etype == "turn.failed":
                stop_reason = "refusal"
                err = ev.get("error") or ev.get("message") or ev
                logger.warning("codex.turn.failed | %s", json.dumps(err)[:400])
                continue
            if etype == "error":
                stop_reason = "refusal"
                err = ev.get("message") or ev.get("error") or ev
                logger.warning("codex.error | %s",
                               err if isinstance(err, str) else json.dumps(err)[:400])
                continue
            if etype in ("item.started", "item.completed"):
                upd = _translate_item_event(etype, ev)
                if upd is not None:
                    if upd.get("sessionUpdate") == "tool_call":
                        n_tool_calls += 1
                    await self._send_update(state.session_id, upd)
                continue

            logger.debug("codex.unhandled_event type=%s", etype)

        await proc.wait()
        exit_code = proc.returncode if proc.returncode is not None else -1
        try:
            await asyncio.wait_for(stderr_task, timeout=5)
        except asyncio.TimeoutError:
            stderr_task.cancel()
        state.current_codex_proc = None

        # If codex died abnormally and we never saw turn.completed, surface
        # that as a refusal so the client doesn't think the turn ended cleanly.
        if exit_code not in (0,) and not seen_turn_completed and stop_reason == "end_turn":
            stop_reason = "refusal"

        logger.info(
            "codex.done exit=%d elapsed=%.2fs tool_calls=%d thread_id=%s",
            exit_code, time.time() - started_at, n_tool_calls, state.thread_id,
        )
        return stop_reason, exit_code


# ---------------------------------------------------------------------------
# Event translation (pure-function -- tested standalone)
# ---------------------------------------------------------------------------


def _wrap_text_block(text: Any) -> list[dict[str, Any]]:
    """Wrap an arbitrary value as a single ACP text-content block (or empty)."""
    if text is None:
        return []
    if isinstance(text, (dict, list)):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            text = str(text)
    text = str(text)
    if not text:
        return []
    return [{"type": "text", "text": text}]


def _exit_to_status(exit_code: Any) -> str:
    """Map a command-execution exit code (int or None) to a ToolCallStatus."""
    if exit_code is None:
        return "in_progress"
    try:
        return "completed" if int(exit_code) == 0 else "failed"
    except (TypeError, ValueError):
        return "completed"


def _translate_item_event(
    etype: str, ev: dict[str, Any],
) -> dict[str, Any] | None:
    """Translate one Codex ``item.started`` / ``item.completed`` event.

    Returns the ACP ``session/update`` payload, or ``None`` if the event has
    no ACP equivalent.  Kept as a pure function so we can unit-test it
    without spinning up a server.
    """
    item = ev.get("item")
    if not isinstance(item, dict):
        return None
    itype = str(item.get("type", ""))
    iid = str(item.get("id", "") or "")

    # ---- agent text (visible) ----
    if itype == "agent_message":
        if etype != "item.completed":
            return None
        text = item.get("text") or ""
        if not isinstance(text, str) or not text:
            return None
        return {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }

    # ---- agent reasoning (hidden chain-of-thought, if codex emits) ----
    if itype in ("agent_reasoning", "reasoning"):
        if etype != "item.completed":
            return None
        text = item.get("text") or item.get("content") or ""
        if not isinstance(text, str) or not text:
            return None
        return {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": text},
        }

    # ---- local shell exec ----
    if itype == "command_execution":
        command = str(item.get("command") or "")
        output = item.get("aggregated_output") or item.get("output") or ""
        status_str = (item.get("status") or "").lower()
        if etype == "item.started":
            return {
                "sessionUpdate": "tool_call",
                "toolCallId": iid,
                "title": command[:200] or "command",
                "kind": "bash",
                "status": "in_progress" if status_str in ("", "in_progress") else status_str,
                "rawInput": {"command": command},
            }
        # item.completed
        status = status_str or _exit_to_status(item.get("exit_code"))
        if status == "in_progress":
            status = _exit_to_status(item.get("exit_code"))
        return {
            "sessionUpdate": "tool_call_update",
            "toolCallId": iid,
            "status": status,
            "content": _wrap_text_block(output),
            "rawOutput": {
                "exit_code": item.get("exit_code"),
                "aggregated_output": output if isinstance(output, str) else "",
            },
        }

    # ---- MCP tool call ----
    if itype == "mcp_tool_call":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or item.get("name") or "")
        title = (server + "." + tool).strip(".") if (server or tool) else "mcp_tool_call"
        if etype == "item.started":
            return {
                "sessionUpdate": "tool_call",
                "toolCallId": iid,
                "title": title,
                "kind": "other",
                "status": "in_progress",
                "rawInput": item.get("arguments") or item.get("input"),
            }
        status_str = (item.get("status") or "").lower()
        if status_str in ("failed", "error"):
            status = "failed"
        elif status_str in ("cancelled", "canceled"):
            status = "cancelled"
        else:
            status = "completed"
        result = item.get("result") or item.get("output")
        return {
            "sessionUpdate": "tool_call_update",
            "toolCallId": iid,
            "status": status,
            "content": _wrap_text_block(result),
            "rawOutput": (result if isinstance(result, (dict, list)) else None),
        }

    # ---- apply_patch / other future item types ----
    if itype in ("apply_patch", "file_change"):
        if etype == "item.started":
            return {
                "sessionUpdate": "tool_call",
                "toolCallId": iid,
                "title": itype,
                "kind": "edit",
                "status": "in_progress",
            }
        return {
            "sessionUpdate": "tool_call_update",
            "toolCallId": iid,
            "status": "completed",
            "content": _wrap_text_block(item.get("summary") or item.get("output")),
        }

    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _amain() -> int:
    server = CodexAcpServer()
    return await server.run()


def main() -> None:
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
