"""ACP wire types -- JSON-RPC 2.0 messages for the Agent Client Protocol.

This is a focused subset of the ACP spec, scoped to what the evolving-skills
harness actually exercises:

  * The lifecycle we drive:
        initialize -> session/new -> session/set_model -> session/prompt (loop)
        -> close
  * The session/update notifications we consume to assemble a trajectory:
        tool_call, tool_call_update, agent_message_chunk, agent_thought_chunk,
        plan (Codex emits these whenever it updates its internal task list).
  * The single agent->client request we must answer:
        session/request_permission  (we auto-approve in benchmark mode).

We deliberately do NOT model the filesystem / terminal request family
(ACP's optional fs.* and terminal.* surface).  Codex-via-ACP never asks the
client to do file I/O for it -- it does its own with the local FS.  If we
later wrap an agent that DOES request fs/terminal proxying (e.g. an in-
container Claude Code via Zed's claude-agent-acp talking to a remote
sandbox), we add those types here and an ``_handle_agent_request`` arm in
``client.py``.

All field names use camelCase aliases (the on-the-wire spelling) with
snake_case Python attribute names.  Always call
``model_dump(by_alias=True)`` when serializing so the wire payload is
ACP-compliant.

Spec reference: https://github.com/zed-industries/agent-client-protocol
(rev pinned by ``codex-acp`` is ``agent-client-protocol = 0.14.0``).
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# Python 3.11+ ships ``enum.StrEnum``; we subclass ``(str, Enum)`` directly to
# keep wire compatibility (value is the bare string) on 3.10 as well.
class StrEnum(str, Enum):
    """``str`` + ``Enum`` -- the value IS the string on the wire."""

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StopReason(StrEnum):
    """Why the agent stopped producing output after a session/prompt."""

    END_TURN = "end_turn"                # Normal completion
    MAX_TOKENS = "max_tokens"            # Output token cap hit
    MAX_TURN_REQUESTS = "max_turn_requests"  # Per-turn tool-call cap hit
    REFUSAL = "refusal"                  # Agent refused the prompt
    CANCELLED = "cancelled"              # Client called session/cancel


class ToolKind(StrEnum):
    """Coarse category tag for tool calls; used by reporting/UI."""

    OTHER = "other"
    BASH = "bash"             # Shell/exec on host or sandbox
    SEARCH = "search"          # Read-only retrieval
    BROWSER = "browser"
    READ = "read"              # Filesystem read
    WRITE = "write"            # Filesystem write / apply_patch
    EDIT = "edit"              # Patch / diff
    FETCH = "fetch"            # HTTP fetch
    THINK = "think"            # Pure-reasoning tool
    EXECUTE = "execute"        # Generic exec
    MOVE = "move"
    DELETE = "delete"


class ToolCallStatus(StrEnum):
    """Lifecycle state of a single tool call."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


class _Camel(BaseModel):
    """Shared model config: accept either alias or python field name on input,
    serialize with camelCase aliases by default.

    ``protected_namespaces=()`` opts out of pydantic's ``model_*`` collision
    warning -- ACP itself uses ``modelId`` as a field name (session/set_model).
    """
    model_config = ConfigDict(
        populate_by_name=True,
        extra="allow",
        protected_namespaces=(),
    )


class TextContent(_Camel):
    """Plain text content block -- the workhorse for prompts and agent
    messages alike."""
    type: Literal["text"] = "text"
    text: str


# We intentionally leave image/resource_link out for now; add as needed.
ContentBlock = TextContent


# ---------------------------------------------------------------------------
# Capabilities  (advertised during the initialize handshake)
# ---------------------------------------------------------------------------


class FsCapabilities(_Camel):
    """File-system operations the *client* offers to the agent."""
    read_text_file: bool = Field(default=False, alias="readTextFile")
    write_text_file: bool = Field(default=False, alias="writeTextFile")


class ClientCapabilities(_Camel):
    """What the harness advertises during initialize.

    We default everything to False because Codex (and our wrapper) does its
    own I/O against the trial's working directory; we do not need to act as
    an fs/terminal proxy.  Enable selectively if a future agent we wrap
    routes fs/terminal calls back through the client.
    """
    fs: FsCapabilities = Field(default_factory=FsCapabilities)
    terminal: bool = False


class PromptCapabilities(_Camel):
    """Media types the agent accepts inside prompt content blocks."""
    image: bool = False
    audio: bool = False
    embedded_context: bool = Field(default=False, alias="embeddedContext")


class McpCapabilities(_Camel):
    """MCP transport modes the agent supports (server side)."""
    sse: bool = False
    http: bool = False


class AgentCapabilities(_Camel):
    """What the agent reports back during initialize."""
    prompt_capabilities: PromptCapabilities | None = Field(
        default=None, alias="promptCapabilities",
    )
    mcp_capabilities: McpCapabilities | None = Field(
        default=None, alias="mcpCapabilities",
    )
    load_session: bool = Field(default=False, alias="loadSession")


class ClientInfo(_Camel):
    """Identity block sent by the client during initialize."""
    name: str = "evovle-skills"
    version: str = "0.1.0"


class AgentInfo(_Camel):
    """Identity block returned by the agent during initialize."""
    name: str
    version: str


# ---------------------------------------------------------------------------
# Request / response: lifecycle
# ---------------------------------------------------------------------------


class InitializeParams(_Camel):
    """Client -> agent: open the ACP handshake."""
    protocol_version: int = Field(default=0, alias="protocolVersion")
    client_capabilities: ClientCapabilities = Field(
        default_factory=ClientCapabilities, alias="clientCapabilities",
    )
    client_info: ClientInfo = Field(default_factory=ClientInfo, alias="clientInfo")


class InitializeResult(_Camel):
    """Agent -> client: protocol version + agent identity/capabilities."""
    protocol_version: int = Field(alias="protocolVersion")
    agent_capabilities: AgentCapabilities | None = Field(
        default=None, alias="agentCapabilities",
    )
    agent_info: AgentInfo | None = Field(default=None, alias="agentInfo")


class McpServerSpec(_Camel):
    """MCP server to attach to the new session.

    ACP supports stdio servers (command/args/env) and remote (URL) servers.
    Our EOG bridge runs as a local stdio command, which is the default.
    """
    type: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: list[dict[str, str]] = Field(default_factory=list)
    url: str | None = None


class NewSessionParams(_Camel):
    """Client -> agent: create a fresh session anchored at ``cwd``."""
    cwd: str
    mcp_servers: list[McpServerSpec] = Field(
        default_factory=list, alias="mcpServers",
    )


class NewSessionResult(_Camel):
    """Agent -> client: identifier for the freshly-created session."""
    session_id: str = Field(alias="sessionId")


class SetModelParams(_Camel):
    """Client -> agent: change the model used by an existing session."""
    session_id: str = Field(alias="sessionId")
    model_id: str = Field(alias="modelId")


class PromptParams(_Camel):
    """Client -> agent: deliver user content to the session."""
    session_id: str = Field(alias="sessionId")
    prompt: list[dict[str, Any]]  # list of ContentBlock dicts


class PromptResult(_Camel):
    """Agent -> client: why the prompt loop ended."""
    stop_reason: StopReason = Field(alias="stopReason")


class CancelParams(_Camel):
    """Client -> agent: cancel an in-flight prompt."""
    session_id: str = Field(alias="sessionId")


# ---------------------------------------------------------------------------
# session/update notifications
# ---------------------------------------------------------------------------
#
# These are agent -> client *notifications* (no id; no response).  They
# stream while a ``session/prompt`` request is in flight; the request only
# completes once the agent emits a PromptResult.


class ToolCall(_Camel):
    """Notification: agent has just initiated a tool call.

    Carries enough context (title, kind, optional locations) for the client
    to render or log the call before the result arrives.
    """
    session_update: Literal["tool_call"] = Field(alias="sessionUpdate")
    tool_call_id: str = Field(alias="toolCallId")
    title: str = ""
    kind: ToolKind | str = ToolKind.OTHER
    status: ToolCallStatus = ToolCallStatus.PENDING
    # Arbitrary structured content blocks (e.g. {"type": "diff", ...} for
    # apply_patch).  We keep these as raw dicts -- harnesses that want
    # structured access can opt-in via a follow-up cast.
    content: list[dict[str, Any]] = Field(default_factory=list)
    locations: list[dict[str, Any]] = Field(default_factory=list)
    raw_input: dict[str, Any] | None = Field(default=None, alias="rawInput")
    raw_output: dict[str, Any] | None = Field(default=None, alias="rawOutput")


class ToolCallUpdate(_Camel):
    """Notification: an existing tool call changed status / produced output.

    Status transitions: pending -> in_progress -> {completed, failed, cancelled}.
    """
    session_update: Literal["tool_call_update"] = Field(alias="sessionUpdate")
    tool_call_id: str = Field(alias="toolCallId")
    status: ToolCallStatus | None = None
    title: str | None = None
    kind: ToolKind | str | None = None
    content: list[dict[str, Any]] = Field(default_factory=list)
    raw_input: dict[str, Any] | None = Field(default=None, alias="rawInput")
    raw_output: dict[str, Any] | None = Field(default=None, alias="rawOutput")


class AgentMessageChunk(_Camel):
    """Notification: streaming chunk of agent-visible (user-facing) text."""
    session_update: Literal["agent_message_chunk"] = Field(alias="sessionUpdate")
    content: dict[str, Any]   # {"type": "text", "text": "..."}


class AgentThoughtChunk(_Camel):
    """Notification: streaming chunk of agent reasoning (hidden from user)."""
    session_update: Literal["agent_thought_chunk"] = Field(alias="sessionUpdate")
    content: dict[str, Any]


class PlanEntry(_Camel):
    """One item in an agent-emitted plan."""
    content: str
    priority: str = "medium"   # "high" | "medium" | "low"
    status: str = "pending"     # "pending" | "in_progress" | "completed"


class Plan(_Camel):
    """Notification: agent updated its task list (Codex emits these freely)."""
    session_update: Literal["plan"] = Field(alias="sessionUpdate")
    entries: list[PlanEntry] = Field(default_factory=list)


SessionUpdate = (
    ToolCall | ToolCallUpdate | AgentMessageChunk | AgentThoughtChunk | Plan
)


# ---------------------------------------------------------------------------
# Agent -> client requests we must answer
# ---------------------------------------------------------------------------


class PermissionOption(_Camel):
    """One choice presented by ``session/request_permission``."""
    option_id: str = Field(alias="optionId")
    name: str = ""
    kind: str = "allow_once"   # allow_once | allow_always | reject_once | reject_always | ...


class RequestPermissionParams(_Camel):
    """Agent -> client request: ask the user to approve a sensitive action.

    Our auto-approver picks the most permissive available option (preferring
    ``bypassPermissions`` if present, then ``allow_always``, then anything
    starting with ``allow``).  Benchmark trials run in a sandboxed trial
    directory, so blanket approval is acceptable; the verifier catches any
    misbehavior post-hoc.
    """
    session_id: str = Field(alias="sessionId")
    tool_call: dict[str, Any] = Field(default_factory=dict, alias="toolCall")
    options: list[PermissionOption] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON-RPC envelope helpers  (we mostly hand-roll dicts in client.py, but
# these are useful for debugging / tests).
# ---------------------------------------------------------------------------


class JsonRpcRequest(_Camel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResponse(_Camel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class JsonRpcNotification(_Camel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    # Enums
    "StopReason", "ToolKind", "ToolCallStatus",
    # Content
    "TextContent", "ContentBlock",
    # Capabilities
    "FsCapabilities", "ClientCapabilities", "PromptCapabilities",
    "McpCapabilities", "AgentCapabilities", "ClientInfo", "AgentInfo",
    # Lifecycle
    "InitializeParams", "InitializeResult",
    "McpServerSpec", "NewSessionParams", "NewSessionResult",
    "SetModelParams", "PromptParams", "PromptResult", "CancelParams",
    # Updates
    "ToolCall", "ToolCallUpdate", "AgentMessageChunk",
    "AgentThoughtChunk", "Plan", "PlanEntry", "SessionUpdate",
    # Permission
    "PermissionOption", "RequestPermissionParams",
    # Envelope
    "JsonRpcRequest", "JsonRpcResponse", "JsonRpcNotification",
]
