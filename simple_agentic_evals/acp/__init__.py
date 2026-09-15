"""ACP (Agent Client Protocol) layer for simple_agentic_evals.

This package contains a self-contained ACP **client** plus matching wire
types, and a Codex ACP **server** (``python -m simple_agentic_evals.acp.codex_server``).
It is the *unifying* layer used to drive any agent backend (Codex now,
Claude/Gemini later): each backend ships an ACP **server** wrapper that emits
the same ``session/update`` events and ``stopReason``s, and the client talks
JSON-RPC to whichever one is configured. Used by the service's Codex runner to
drive Codex over ACP for a session (see the harness ``agent_runner``).

We intentionally do NOT depend on ``benchflow.acp`` or any other external
ACP library so that:

  * Codex updates flow in via ``codex exec`` natively (no Zed `codex-acp`
    middle box pinning Codex versions).
  * Adding a new agent backend requires only a server wrapper, not a new
    client.
  * The harness has no Zed runtime dependency for Codex trials; Zed's
    adapters (``@zed-industries/codex-acp``, ``claude-agent-acp``, ...)
    remain *optional* drop-in alternatives that speak the same wire.

Public API:

    >>> from simple_agentic_evals.acp import ACPClient, McpServerSpec, StopReason
    >>> client = ACPClient.from_stdio(command="our-codex-acp", env={...})
    >>> await client.connect()
    >>> await client.initialize()
    >>> session = await client.session_new(cwd="/path/to/trial")
    >>> await client.set_model("gpt-5")
    >>> result = await client.prompt("Do X.")
    >>> result.stop_reason
    <StopReason.END_TURN>
    >>> session.events
    [{'type': 'user_message', ...}, {'type': 'tool_call', ...}, ...]
"""

from .client import ACPClient, ACPError, OnUpdateHook
from .session import ACPSession, ToolCallRecord
from .transport import StdioTransport, Transport
from .types import (
    AgentCapabilities,
    AgentInfo,
    AgentMessageChunk,
    AgentThoughtChunk,
    CancelParams,
    ClientCapabilities,
    ClientInfo,
    ContentBlock,
    InitializeParams,
    InitializeResult,
    McpServerSpec,
    NewSessionParams,
    NewSessionResult,
    PermissionOption,
    Plan,
    PlanEntry,
    PromptParams,
    PromptResult,
    RequestPermissionParams,
    SessionUpdate,
    SetModelParams,
    StopReason,
    TextContent,
    ToolCall,
    ToolCallStatus,
    ToolCallUpdate,
    ToolKind,
)

__all__ = [
    "ACPClient",
    "ACPError",
    "ACPSession",
    "OnUpdateHook",
    "ToolCallRecord",
    "StdioTransport",
    "Transport",
    "AgentCapabilities",
    "AgentInfo",
    "AgentMessageChunk",
    "AgentThoughtChunk",
    "CancelParams",
    "ClientCapabilities",
    "ClientInfo",
    "ContentBlock",
    "InitializeParams",
    "InitializeResult",
    "McpServerSpec",
    "NewSessionParams",
    "NewSessionResult",
    "PermissionOption",
    "Plan",
    "PlanEntry",
    "PromptParams",
    "PromptResult",
    "RequestPermissionParams",
    "SessionUpdate",
    "SetModelParams",
    "StopReason",
    "TextContent",
    "ToolCall",
    "ToolCallStatus",
    "ToolCallUpdate",
    "ToolKind",
]
