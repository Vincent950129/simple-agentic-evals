"""Minimal streamable-HTTP MCP client for acting on EOG tasks.

The eval service proxies each call to the live gym and binds it to your session's
database, so acting works over the same base URL as everything else. This is a
*convenience* -- you can hand ``task.mcp_servers`` to any MCP client instead
(Codex / Claude / LangGraph / your own). Prefer ``task.mcp_session(server)``,
which wires this up with the client's auth headers for you.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class MCPSession:
    """``initialize`` + ``tools/list`` + ``tools/call`` over streamable-HTTP.

    Point it at the URL the service returned for a gym (``task.mcp_url(server)``).
    ``headers`` should include any per-server headers plus auth; ``api_key`` is a
    convenience that sets ``Authorization: Bearer`` when not already present.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        api_key: str = "",
    ):
        self.url = url
        base = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "ngrok-skip-browser-warning": "true",
        }
        base.update(headers or {})
        if api_key and "Authorization" not in base:
            base["Authorization"] = f"Bearer {api_key}"
        self.base_headers = base
        self._http = httpx.Client(timeout=timeout)
        self._sid: str | None = None
        self._ready = False

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        # Streamable-HTTP MCP returns application/json or text/event-stream.
        if "text/event-stream" in resp.headers.get("content-type", ""):
            payload = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
            return json.loads(payload) if payload else {}
        return resp.json()

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = dict(self.base_headers)
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        r = self._http.post(self.url, headers=headers, json=body)
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._sid = sid
        if r.status_code == 202 or not r.content:   # notifications return no body
            return {}
        return self._parse(r)

    def _initialize(self) -> None:
        if self._ready:
            return
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "simple_agentic_evals", "version": "0.3"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    def list_tools(self) -> list[dict[str, Any]]:
        self._initialize()
        d = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return (d.get("result") or {}).get("tools") or []

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._initialize()
        return self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MCPSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
