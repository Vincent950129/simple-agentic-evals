"""ACPClient -- talk JSON-RPC to an ACP agent server over a Transport.

Lifecycle (mirrors the ACP spec):

    client = ACPClient.from_stdio(command="our-codex-acp", ...)
    await client.connect()
    await client.initialize()
    session = await client.session_new(cwd="/path/to/trial", mcp_servers=[...])
    await client.set_model("gpt-5")
    result = await client.prompt("Do X")            # may return multiple times
    ...
    await client.close()

While a ``session/prompt`` is in flight, the agent streams ``session/update``
notifications (tool calls, agent text, plans) and may issue *requests* of
its own (``session/request_permission``).  The client transparently handles
all of these inside :meth:`_pump_until_response`:

  * ``session/update``         -> routed to ``ACPSession.handle_update`` and
                                   optionally the user-supplied
                                   ``on_update`` callback.
  * ``session/request_permission`` -> auto-approved with the most permissive
                                       option available (benchmark mode).
  * Anything else from the agent  -> answered with an empty result so the
                                      agent doesn't stall waiting on us.

The client is single-threaded with respect to outstanding requests: each
public call ``await``s its own response before returning.  Concurrent
prompts are not supported (and ACP itself ties one session to one logical
conversation, so this is fine).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .session import ACPSession
from .transport import StdioTransport, Transport
from .types import (
    InitializeParams,
    InitializeResult,
    NewSessionParams,
    PromptResult,
    StopReason,
)

logger = logging.getLogger(__name__)


# Optional hook called for every session/update notification observed during
# a prompt.  Receives (session_id, update_dict).  Use to e.g. count tool
# calls in real time for stall detection.
OnUpdateHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class ACPError(Exception):
    """Wraps a JSON-RPC error returned by the ACP agent."""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"ACP error {code}: {message}")


class ACPClient:
    """JSON-RPC client for an ACP agent server."""

    def __init__(
        self,
        transport: Transport,
        *,
        on_update: OnUpdateHook | None = None,
    ):
        self._transport = transport
        self._on_update = on_update

        # Request IDs start high to avoid collisions with IDs the agent picks
        # for its inbound requests (some agents use small sequential IDs).
        self._next_request_id = 100_000

        self._session: ACPSession | None = None
        self._init_result: InitializeResult | None = None
        self._closed = False

    # ---------------------------------------------------------------- factories

    @classmethod
    def from_stdio(
        cls,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        on_update: OnUpdateHook | None = None,
        on_stderr_line: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> "ACPClient":
        transport = StdioTransport(
            command=command, args=args, env=env, cwd=cwd,
            on_stderr_line=on_stderr_line,
        )
        return cls(transport, on_update=on_update)

    # ---------------------------------------------------------------- low level

    def _allocate_id(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    async def _send_request(
        self, method: str, params: dict[str, Any], *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and pump messages until the response lands."""
        request_id = self._allocate_id()
        await self._transport.send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
        pump = self._pump_until_response(request_id)
        if timeout_s is None:
            return await pump
        return await asyncio.wait_for(pump, timeout=timeout_s)

    async def _send_notification(
        self, method: str, params: dict[str, Any],
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        await self._transport.send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

    async def _pump_until_response(self, request_id: int) -> dict[str, Any]:
        """Read messages until the response for ``request_id`` arrives.

        Notifications and inbound agent requests are routed/answered inline
        so the agent never blocks waiting for us.
        """
        while True:
            msg = await self._transport.receive()

            # Response to our request? (has id, no method)
            if "id" in msg and msg.get("id") == request_id and "method" not in msg:
                if msg.get("error"):
                    err = msg["error"]
                    raise ACPError(
                        code=err.get("code", -1),
                        message=err.get("message", "unknown error"),
                        data=err.get("data"),
                    )
                result = msg.get("result")
                return result if isinstance(result, dict) else {}

            # Notification from agent (has method, no id)
            if "method" in msg and "id" not in msg:
                try:
                    await self._handle_notification(msg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "acp.client.notification_handler_failed method=%s err=%s",
                        msg.get("method"), exc,
                    )
                continue

            # Request from agent (has method AND id) -- must reply.
            if "method" in msg and "id" in msg:
                try:
                    await self._handle_agent_request(msg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "acp.client.agent_request_handler_failed method=%s err=%s",
                        msg.get("method"), exc,
                    )
                    # Best-effort fallback so we don't deadlock.
                    await self._transport.send({
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32000, "message": str(exc)},
                    })
                continue

            # Anything else: drop with a debug line.
            logger.debug("acp.client.dropped_unknown msg=%s", msg)

    # ---------------------------------------------------------------- inbound

    async def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Route a notification from the agent."""
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        if method == "session/update":
            session_id = str(params.get("sessionId", ""))
            update = params.get("update", {})
            if not isinstance(update, dict):
                return
            if self._session and session_id == self._session.session_id:
                self._session.handle_update(update)
            if self._on_update is not None:
                try:
                    ret = self._on_update(session_id, update)
                    if asyncio.iscoroutine(ret):
                        await ret
                except Exception as exc:  # noqa: BLE001
                    logger.warning("acp.client.on_update_failed err=%s", exc)
            return

        # Other notifications are uncommon; keep visible at DEBUG.
        logger.debug("acp.client.notification.unhandled method=%s", method)

    async def _handle_agent_request(self, msg: dict[str, Any]) -> None:
        """Answer an inbound JSON-RPC request from the agent."""
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {}) or {}

        if method == "session/request_permission":
            outcome = self._approve_permission(params)
            await self._transport.send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"outcome": outcome},
            })
            return

        # Unknown agent request: return empty result so we don't deadlock.
        # If a future agent we wrap requires fs / terminal proxying, add
        # handlers here.
        logger.debug(
            "acp.client.agent_request.unknown method=%s id=%s", method, req_id,
        )
        await self._transport.send({
            "jsonrpc": "2.0", "id": req_id, "result": {},
        })

    @staticmethod
    def _approve_permission(params: dict[str, Any]) -> dict[str, Any]:
        """Pick the most permissive option offered.

        Preference (decreasing): bypassPermissions > allow_always >
        allow_once > anything > the first option.  If no options are given,
        we synthesize ``optionId="default"``.
        """
        options = params.get("options") or []
        chosen: str | None = None
        for opt in options:
            if not isinstance(opt, dict):
                continue
            if opt.get("optionId") == "bypassPermissions":
                chosen = "bypassPermissions"
                break
        if chosen is None:
            for opt in options:
                if isinstance(opt, dict) and opt.get("kind") == "allow_always":
                    chosen = opt.get("optionId", "")
                    if chosen:
                        break
        if chosen is None:
            for opt in options:
                if isinstance(opt, dict) and opt.get("kind") == "allow_once":
                    chosen = opt.get("optionId", "")
                    if chosen:
                        break
        if chosen is None and options:
            first = options[0]
            chosen = first.get("optionId", "") if isinstance(first, dict) else None
        if not chosen:
            chosen = "default"
        return {"outcome": "selected", "optionId": chosen}

    # ---------------------------------------------------------------- public

    async def connect(self) -> None:
        await self._transport.start()

    async def initialize(self) -> InitializeResult:
        """Perform the ACP initialize handshake."""
        params = InitializeParams()
        raw = await self._send_request(
            "initialize", params.model_dump(by_alias=True),
        )
        self._init_result = InitializeResult.model_validate(raw)
        logger.info(
            "acp.client.initialized protocol_version=%d agent=%s",
            self._init_result.protocol_version,
            (self._init_result.agent_info.name
             if self._init_result.agent_info else "?"),
        )
        return self._init_result

    async def session_new(
        self,
        cwd: str,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> ACPSession:
        """Create a new session anchored at ``cwd``."""
        params = NewSessionParams(
            cwd=cwd,
            mcp_servers=mcp_servers or [],  # type: ignore[arg-type]
        )
        raw = await self._send_request(
            "session/new", params.model_dump(by_alias=True),
        )
        session_id = str(raw.get("sessionId", ""))
        if not session_id:
            raise ACPError(-32000, "session/new returned no sessionId", raw)
        self._session = ACPSession(session_id)
        if self._init_result is not None:
            self._session.agent_info = self._init_result.agent_info
            self._session.agent_capabilities = self._init_result.agent_capabilities
        logger.info("acp.client.session_new session_id=%s cwd=%s", session_id, cwd)
        return self._session

    async def set_model(self, model_id: str) -> dict[str, Any]:
        """Set the model for the active session.

        Returns the raw result dict (some agents echo capabilities here).
        """
        if not self._session:
            raise RuntimeError("set_model requires an active session")
        result = await self._send_request("session/set_model", {
            "sessionId": self._session.session_id,
            "modelId": model_id,
        })
        logger.info(
            "acp.client.set_model session_id=%s model=%s",
            self._session.session_id, model_id,
        )
        return result

    async def prompt(
        self,
        text: str,
        *,
        timeout_s: float | None = None,
    ) -> PromptResult:
        """Send a single prompt turn; pump updates until completion."""
        if not self._session:
            raise RuntimeError("prompt requires an active session")
        self._session.record_user_prompt(text)
        raw = await self._send_request(
            "session/prompt",
            {
                "sessionId": self._session.session_id,
                "prompt": [{"type": "text", "text": text}],
            },
            timeout_s=timeout_s,
        )
        try:
            result = PromptResult.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - defensive
            logger.warning(
                "acp.client.prompt_result_invalid raw=%s err=%s", raw, exc,
            )
            result = PromptResult(stopReason=StopReason.END_TURN)
        self._session.record_prompt_end(result.stop_reason)
        return result

    async def cancel(self) -> None:
        """Notify the agent to abort the in-flight prompt."""
        if not self._session:
            return
        await self._send_notification("session/cancel", {
            "sessionId": self._session.session_id,
        })

    @property
    def session(self) -> ACPSession | None:
        return self._session

    @property
    def initialize_result(self) -> InitializeResult | None:
        return self._init_result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._transport.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("acp.client.close_failed err=%s", exc)


__all__ = ["ACPClient", "ACPError", "OnUpdateHook"]
