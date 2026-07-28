"""ACP transports.

The evolving-skills harness only needs **stdio**: we spawn an ACP server
binary (our_codex_acp wrapper, or in the future a Zed adapter / our own
claude wrapper), pipe JSON-RPC over its stdin/stdout, and tee its stderr
into a callback so the wrapper's diagnostic logs show up in the harness's
log stream.

Wire format on the line:
    one JSON object per ``\\n``-terminated line.  ACP doesn't use the
    LSP-style "Content-Length:" header framing.

If we later need HTTP / SSE / WebSocket transports (e.g. for a remote
sandboxed agent), add another Transport subclass; the rest of the client
is transport-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


# 1 MiB max line.  Codex emits large tool-result blocks; the default
# 64 KiB asyncio StreamReader limit truncates them.
_MAX_LINE_BYTES = 1024 * 1024


class Transport(ABC):
    """Abstract transport interface (send/receive JSON-RPC messages)."""

    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def send(self, message: dict[str, Any]) -> None: ...
    @abstractmethod
    async def receive(self) -> dict[str, Any]: ...
    @abstractmethod
    async def close(self) -> None: ...


class StdioTransport(Transport):
    """Drive an ACP server over its stdin/stdout pipes.

    Parameters
    ----------
    command : str
        Executable path or PATH-resolvable name.
    args : list[str]
        Command-line arguments passed to the executable.
    env : dict[str, str] | None
        Extra env vars merged on top of the parent process env.  Use this to
        propagate auth (``OPENAI_API_KEY``), CODEX_HOME, etc. to the wrapper.
    cwd : str | None
        Working directory of the spawned process.
    on_stderr_line : optional callback
        Called once per stderr line (decoded UTF-8, no trailing newline).
        Use it to forward the wrapper's diagnostic output into the harness's
        logger.  If None, stderr is silently consumed.
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        on_stderr_line: Callable[[str], Awaitable[None] | None] | None = None,
    ):
        self._command = command
        self._args = args or []
        self._env_extra = env
        self._cwd = cwd
        self._on_stderr_line = on_stderr_line
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # Captured at spawn: os.getpgid() stops working once the child is reaped,
        # but close() still needs the group id to sweep surviving descendants.
        self._pgid: int | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    async def start(self) -> None:
        proc_env = os.environ.copy()
        if self._env_extra:
            proc_env.update(self._env_extra)

        # start_new_session: the ACP server leads its own process group, so close()
        # can tear down the whole tree it builds (server -> codex -> one bwrap per
        # tool call). Terminating just the server leaves those descendants orphaned
        # onto PID 1, where they accumulate as unreaped zombies.
        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            cwd=self._cwd,
            limit=_MAX_LINE_BYTES,
            start_new_session=True,
        )
        try:
            self._pgid = os.getpgid(self._process.pid)
        except (ProcessLookupError, PermissionError):
            self._pgid = None
        logger.info(
            "acp.transport.started cmd=%s args=%s pid=%d cwd=%s",
            self._command, self._args, self._process.pid, self._cwd or os.getcwd(),
        )
        self._stderr_task = asyncio.create_task(self._pump_stderr())

    async def _pump_stderr(self) -> None:
        """Drain the agent's stderr; forward each line to the callback."""
        assert self._process is not None
        if self._process.stderr is None:
            return
        while True:
            try:
                line = await self._process.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # Oversized line: drop and keep going.
                line = b""
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            if self._on_stderr_line is None:
                logger.debug("acp.agent.stderr | %s", text)
                continue
            try:
                ret = self._on_stderr_line(text)
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception as exc:  # noqa: BLE001 - never let a logger kill us
                logger.warning("acp.transport.stderr_callback_failed: %s", exc)

    async def send(self, message: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("Transport not started")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._process.stdin.write(data)
        await self._process.stdin.drain()
        logger.debug(
            "acp.send method=%s id=%s",
            message.get("method", "<response>"), message.get("id"),
        )

    async def receive(self) -> dict[str, Any]:
        if not self._process or not self._process.stdout:
            raise RuntimeError("Transport not started")
        while True:
            try:
                raw = await self._process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                logger.warning("acp.transport.oversized_line: %s", exc)
                # Drain up to the next newline so we resync the stream.
                while True:
                    chunk = await self._process.stdout.read(_MAX_LINE_BYTES)
                    if not chunk or b"\n" in chunk:
                        break
                continue
            if not raw:
                raise ConnectionError(
                    "ACP agent process closed stdout (exit_code="
                    f"{self._process.returncode})"
                )
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                # Some servers print banner text on stdout before speaking
                # JSON; surface as DEBUG and keep reading.
                logger.debug("acp.transport.non_json_stdout | %s", text[:200])
                continue
            logger.debug(
                "acp.recv method=%s id=%s has_result=%s has_error=%s",
                msg.get("method", ""), msg.get("id"),
                "result" in msg, "error" in msg,
            )
            return msg

    async def close(self) -> None:
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        if not self._process:
            return
        if self._process.stdin and not self._process.stdin.is_closing():
            try:
                self._process.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning(
                    "acp.transport.kill pid=%d (terminate timeout)",
                    self._process.pid,
                )
                self._process.kill()
                await self._process.wait()
        # Sweep the group even after a clean server exit: codex (and the bwrap
        # sandboxes it forked) can outlive it, and anything left here is orphaned.
        if self._pgid is not None:
            try:
                os.killpg(self._pgid, signal.SIGKILL)
                logger.info("acp.transport.swept_process_group pgid=%d", self._pgid)
            except (ProcessLookupError, PermissionError):
                pass  # group already empty -- the normal case
        logger.info(
            "acp.transport.closed pid=%d exit_code=%s",
            self._process.pid, self._process.returncode,
        )


__all__ = ["Transport", "StdioTransport"]
