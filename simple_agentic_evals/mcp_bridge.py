#!/usr/bin/env python3
"""stdio <-> HTTP MCP bridge for CLI agents (Codex, etc.), with OpenAI-safe schemas.

Some CLI agents (older Codex CLI versions) only spawn **stdio** MCP servers via
a `command`/`args` config entry; they can't talk to the gym's streamable-HTTP
MCP directly. This bridge exposes an HTTP MCP endpoint locally as a stdio MCP:
it reads newline-delimited JSON-RPC requests on stdin, forwards each to the HTTP
endpoint, and writes the response object on stdout.

It also intercepts `tools/list` and rewrites tool schemas the OpenAI API rejects
(a top-level `oneOf`/`anyOf`/`allOf`/`enum`/`not`), reusing
:func:`simple_agentic_evals.tools.sanitize_tool_schema` — so the schema handling
here is identical to :func:`simple_agentic_evals.to_openai_tools`. The offending
keyword is stripped and its constraint appended to the tool description, so the
agent still knows what to provide.

It strips the gym's advertised-but-unimplemented `resources`/`prompts`
capabilities from `initialize`, and answers the MCP resource/prompt *discovery*
methods locally with empty-but-valid results, so a CLI agent's built-in probes
can't be mistaken for "the server is down".

Run it as a module (this is what the SDK's Codex helper wires up) ::

    python3 -m simple_agentic_evals.mcp_bridge \
        --url https://host/v1/sessions/<sid>/mcp/<gym> \
        --header 'Authorization=Bearer <key>' \
        [--header 'ngrok-skip-browser-warning=true'] \
        [--allowed-tools t1,t2]   [--normalize-tools 1]

or via the installed console script ``simple-agentic-evals-mcp-bridge``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from .tools import BAD_TOP_LEVEL_KEYS, sanitize_tool_schema

_NO_RESOURCES_HINT = (
    "(no resources are served by this MCP server; this server exposes "
    "function tools only -- call the appropriate tool directly instead of "
    "reading resources)"
)

# Capability keys we strip from the gym's `initialize` response so CLI agents
# treat the gym as tools-only and don't offer/probe resource meta-tools.
_STRIP_CAPABILITIES = ("resources", "prompts")


def _strip_initialize_capabilities(resp: dict[str, Any]) -> dict[str, Any]:
    """Remove resource/prompt capability keys from an `initialize` result."""
    result = resp.get("result")
    if not isinstance(result, dict):
        return resp
    caps = result.get("capabilities")
    if not isinstance(caps, dict):
        return resp
    removed = [k for k in _STRIP_CAPABILITIES if k in caps]
    for k in removed:
        caps.pop(k, None)
    if removed:
        sys.stderr.write(
            "[mcp_bridge] stripped advertised capabilities "
            f"{removed} from initialize (gym is tools-only)\n"
        )
        sys.stderr.flush()
    return resp


def _local_resource_result(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Benign, spec-shaped results for MCP resource/prompt discovery methods the
    gym does not implement. Returns None for methods that should be forwarded."""
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method == "resources/read":
        uri = params.get("uri") if isinstance(params, dict) else None
        return {"contents": [{"uri": uri or "", "mimeType": "text/plain",
                              "text": _NO_RESOURCES_HINT}]}
    if method in ("resources/subscribe", "resources/unsubscribe"):
        return {}
    return None


def _parse_headers(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items:
        if "=" not in it:
            continue
        k, v = it.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def _normalize_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenAI-compatible copy of `tool` (no top-level oneOf/etc.)."""
    schema = tool.get("inputSchema") or tool.get("parameters") or {}
    new_schema, hints = sanitize_tool_schema(schema if isinstance(schema, dict) else {})
    out = dict(tool)
    if "inputSchema" in tool:
        out["inputSchema"] = new_schema
    if "parameters" in tool:
        out["parameters"] = new_schema
    if hints:
        base = (tool.get("description") or "").rstrip()
        out["description"] = (base + " " + " ".join(hints)).strip()
    return out


def _filter_tools_list_response(
    resp: dict[str, Any], *, normalize: bool, allowed: set[str] | None,
) -> dict[str, Any]:
    """Rewrite incompatible tool schemas in place; optionally restrict to an allowlist."""
    result = resp.get("result")
    if not isinstance(result, dict):
        return resp
    tools = result.get("tools")
    if not isinstance(tools, list):
        return resp

    kept: list[dict[str, Any]] = []
    rewrote: list[str] = []
    dropped: list[tuple[str, str]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or "<unnamed>"
        if allowed is not None and name not in allowed:
            dropped.append((name, "not in allowlist"))
            continue
        if normalize:
            schema = t.get("inputSchema") or t.get("parameters") or {}
            had_bad = isinstance(schema, dict) and (
                schema.get("type") != "object"
                or any(k in schema for k in BAD_TOP_LEVEL_KEYS)
            )
            t = _normalize_tool(t) if had_bad else t
            if had_bad:
                rewrote.append(name)
        kept.append(t)

    if rewrote:
        sys.stderr.write(
            f"[mcp_bridge] rewrote {len(rewrote)}/{len(tools)} tool schema(s) "
            f"for OpenAI compat: {', '.join(rewrote[:6])}"
            f"{' ...' if len(rewrote) > 6 else ''}\n"
        )
    if dropped:
        sys.stderr.write(
            f"[mcp_bridge] dropped {len(dropped)}/{len(tools)} tool(s): "
            f"{', '.join(f'{n} ({r})' for n, r in dropped[:6])}"
            f"{' ...' if len(dropped) > 6 else ''}\n"
        )
    sys.stderr.flush()
    result["tools"] = kept
    return resp


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True,
                    help="HTTP MCP endpoint (e.g. a task's mcp_url)")
    ap.add_argument("--header", action="append", default=[], help="key=value (repeatable)")
    ap.add_argument("--timeout-s", type=float, default=120.0)
    ap.add_argument("--normalize-tools", type=int, default=1,
                    help="rewrite tools with a top-level oneOf/anyOf/allOf/enum/not into "
                         "an OpenAI-compatible form (default 1)")
    ap.add_argument("--allowed-tools", default="",
                    help="comma-separated allowlist; if set, only these tools are forwarded")
    args = ap.parse_args()

    headers = _parse_headers(args.header)
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json, text/event-stream")
    normalize = bool(args.normalize_tools)
    allowed: set[str] | None = (
        {t.strip() for t in args.allowed_tools.split(",") if t.strip()}
        if args.allowed_tools.strip() else None
    )

    client = httpx.Client(timeout=args.timeout_s)
    stdin, stdout = sys.stdin, sys.stdout
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req: dict[str, Any] = json.loads(line)
            except Exception:
                continue

            method = req.get("method") if isinstance(req, dict) else None
            params = req.get("params") if isinstance(req, dict) else None
            local = (
                _local_resource_result(method, params if isinstance(params, dict) else {})
                if isinstance(method, str) else None
            )
            if local is not None:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                                        "result": local}) + "\n")
                stdout.flush()
                continue

            try:
                r = client.post(args.url, json=req, headers=headers)
                r.raise_for_status()
                # The gym serves SSE for /mcp; pull the JSON envelope from a `data:` line.
                body = r.text
                if "data:" in body and body.lstrip().startswith(("event:", "data:")):
                    for line2 in body.splitlines():
                        line2 = line2.strip()
                        if line2.startswith("data:"):
                            body = line2[5:].strip()
                            break
                resp = json.loads(body)
            except Exception as e:  # noqa: BLE001 - surface as JSON-RPC error
                req_id = req.get("id") if isinstance(req, dict) else None
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32603, "message": f"bridge: {e}"}}

            if isinstance(req, dict) and req.get("method") == "tools/list":
                resp = _filter_tools_list_response(resp, normalize=normalize, allowed=allowed)
            elif isinstance(req, dict) and req.get("method") == "initialize":
                resp = _strip_initialize_capabilities(resp)
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
