#!/usr/bin/env python3
"""Bring-Your-Own-Agent example against the evaluation service.

Demonstrates the full BYOA loop without installing Docker, the gyms, or the
verifier wiring -- everything is hosted by the service:

    1. pick tasks from the service
    2. start a session  (service seeds a fresh per-task gym DB)
    3. ACT on the environment via the returned MCP server(s)
    4. grade            (service runs the SQL verifiers against that DB)
    5. close            (service tears the DB down)

The "agent" here is intentionally trivial -- it only connects to the gym MCP
and lists the available tools to prove the action surface is live. It does not
solve the task, so grades will be ~0.0; that's expected. Swap in your real
agent at the marked spot.

Run (against a reachable evaluation service):

    python examples/byoa_agent.py \
        --base-url http://localhost:8077 \
        --dataset evovling_tools --benchmark eog --domain hr \
        --version 1 --split test --limit 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

# Make the SDK importable without installing it.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from simple_agentic_evals import EvalClient, McpServer  # noqa: E402


# --------------------------------------------------------------------------- #
# Minimal MCP-over-streamable-HTTP client (initialize + tools/list)           #
# --------------------------------------------------------------------------- #
def _parse_rpc_response(resp: httpx.Response) -> dict:
    """Streamable-HTTP MCP returns either application/json or text/event-stream."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        payload = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
        return json.loads(payload) if payload else {}
    return resp.json()


def mcp_list_tools(url: str, headers: dict | None = None, timeout: float = 30.0) -> list[str]:
    """Connect to a streamable-HTTP MCP endpoint and return its tool names.

    `url` should be the service's proxy URL for the gym (``task.mcp_url(server)``);
    the service forwards each call to the live gym, bound to this session's DB.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    with httpx.Client(timeout=timeout) as http:
        init = http.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "byoa-example", "version": "0.1.0"},
            },
        })
        init.raise_for_status()
        _parse_rpc_response(init)
        # Echo the server session id on subsequent calls if present.
        sid = init.headers.get("mcp-session-id")
        if sid:
            headers["Mcp-Session-Id"] = sid
        # Required handshake completion notification.
        http.post(url, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        listed = http.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        listed.raise_for_status()
        data = _parse_rpc_response(listed)
    tools = (data.get("result") or {}).get("tools") or []
    return [t.get("name", "") for t in tools]


# --------------------------------------------------------------------------- #
# The BYOA loop                                                               #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8077")
    ap.add_argument("--dataset", default="evovling_tools")
    ap.add_argument("--benchmark", default="eog")
    ap.add_argument("--domain", default="hr")
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=2)
    args = ap.parse_args()

    client = EvalClient(args.base_url)
    print("service health:", client.health())

    n = 0
    for task in client.tasks(
        args.dataset, args.benchmark, version=args.version, split=args.split,
        domain=args.domain, limit=args.limit,
    ):
        n += 1
        with task:  # <- service seeds a fresh gym DB here; torn down on exit
            print(f"\n=== task {task.task_id} (session {task.session_id}) ===")
            print("user_prompt:", (task.user_prompt or "")[:160].replace("\n", " "), "...")
            print("oracle_tools:", task.oracle_tools)

            # ---- ACT on the environment -------------------------------- #
            # Replace this block with your real agent. It receives the task
            # prompts and the MCP servers; it acts by calling MCP tools.
            for server in task.mcp_servers:
                try:
                    url = task.mcp_url(server)   # proxied through the service
                    names = mcp_list_tools(url, server.headers)
                    print(f"  mcp[{server.name}] {url} -> {len(names)} tools")
                    print("   e.g.", ", ".join(names[:8]))
                except Exception as e:  # noqa: BLE001
                    print(f"  mcp[{server.name}] probe failed: {e}")
            # ------------------------------------------------------------- #

            result = task.grade()
            print(f"  GRADE pass_rate={result.pass_rate:.2f} "
                  f"({result.n_passed}/{result.n_total}) "
                  f"success={result.overall_success}")

    if n == 0:
        print("No tasks matched the selector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
