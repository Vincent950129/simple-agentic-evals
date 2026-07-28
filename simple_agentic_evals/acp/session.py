"""ACP session state -- accumulates streaming session/update notifications.

One ``ACPSession`` per ``session/new`` result.  As ``session/update``
notifications arrive on the wire, the client calls :meth:`handle_update`,
which routes them into:

  * ``tool_calls``       — chronological list of ``ToolCallRecord``
  * ``message_chunks``   — visible agent text (joined by ``full_message``)
  * ``thought_chunks``   — agent reasoning text
  * ``plans``            — every Plan notification received
  * ``events``           — a flat, ordered list capturing every significant
                          event (user prompts, tool starts/updates, message
                          flushes, plan emissions).  This is the **single
                          unified trajectory format** the rest of the
                          harness should read; it does not depend on which
                          agent backend produced the run.

The class is deliberately passive -- it knows nothing about which ACP server
emitted the updates, and never talks back to the agent.  Permission /
agent-request handling lives in ``client.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .types import (
    AgentCapabilities,
    AgentInfo,
    StopReason,
    ToolCallStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolCallRecord:
    """One tool call observed during a session.

    Carries the full lifecycle: identity (``tool_call_id``, ``title``,
    ``kind``), status (latest), captured content blocks (output payloads),
    and structured raw I/O if the server provided them.
    """
    tool_call_id: str
    title: str
    kind: str
    status: ToolCallStatus = ToolCallStatus.PENDING
    content: list[dict[str, Any]] = field(default_factory=list)
    locations: list[dict[str, Any]] = field(default_factory=list)
    raw_input: dict[str, Any] | None = None
    raw_output: dict[str, Any] | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    def is_terminal(self) -> bool:
        return self.status in (
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
            ToolCallStatus.CANCELLED,
        )

    def apply_update(self, update: dict[str, Any]) -> None:
        """Merge a ``tool_call_update`` notification into this record."""
        if "title" in update and update["title"]:
            self.title = update["title"]
        if "kind" in update and update["kind"]:
            self.kind = update["kind"]
        if "status" in update and update["status"]:
            try:
                self.status = ToolCallStatus(update["status"])
            except ValueError:
                logger.warning(
                    "acp.session.unknown_tool_status status=%s tool_call_id=%s",
                    update["status"], self.tool_call_id,
                )
        new_content = update.get("content")
        if isinstance(new_content, list) and new_content:
            self.content.extend(new_content)
        new_locs = update.get("locations")
        if isinstance(new_locs, list) and new_locs:
            self.locations.extend(new_locs)
        if "rawInput" in update and update["rawInput"] is not None:
            self.raw_input = update["rawInput"]
        if "rawOutput" in update and update["rawOutput"] is not None:
            self.raw_output = update["rawOutput"]
        if self.is_terminal() and self.finished_at is None:
            self.finished_at = datetime.now()


class ACPSession:
    """Mutable state for one ACP session.

    Public attributes worth knowing:

    Identity / capabilities (populated after session_new):
        session_id, agent_info, agent_capabilities, stop_reason

    Streaming accumulators:
        message_chunks, thought_chunks, tool_calls, plans, events

    Convenience views:
        full_message  — joined agent_message_chunk text
        full_thought  — joined agent_thought_chunk text
        n_tool_calls  — total observed tool calls (any status)
        n_completed_tool_calls — tool calls in a terminal state

    The ``events`` list is the canonical trajectory: each entry is a small
    dict ``{"type": ..., ...}`` ordered chronologically.  Event types:
        - {"type": "user_message", "text": <prompt>}
        - {"type": "agent_message", "text": <merged chunks>}
        - {"type": "agent_thought", "text": <merged chunks>}
        - {"type": "tool_call", "tool_call_id", "title", "kind"}
        - {"type": "tool_call_update", "tool_call_id", "status", "content"}
        - {"type": "plan", "entries": [...]}
        - {"type": "prompt_done", "stop_reason": <StopReason>}

    Consecutive agent_message / agent_thought chunks are merged automatically
    at the next non-text event boundary (e.g. tool call, prompt end) so the
    event log reads as alternating user / assistant / tool turns.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.agent_info: AgentInfo | None = None
        self.agent_capabilities: AgentCapabilities | None = None
        self.stop_reason: StopReason | None = None

        self.message_chunks: list[str] = []
        self.thought_chunks: list[str] = []
        self.tool_calls: list[ToolCallRecord] = []
        self._tool_call_index: dict[str, ToolCallRecord] = {}
        self.plans: list[list[dict[str, Any]]] = []

        self.events: list[dict[str, Any]] = []
        self._pending_text: list[dict[str, str]] = []  # buffered text events
        self.created_at = datetime.now()

    # ------------------------------------------------------------------ records

    def record_user_prompt(self, text: str) -> None:
        """Append the user prompt that initiates a turn."""
        self._flush_pending_text()
        self.events.append({"type": "user_message", "text": text})

    def record_prompt_end(self, stop_reason: StopReason | None) -> None:
        """Append a ``prompt_done`` event when a session/prompt returns."""
        self._flush_pending_text()
        self.stop_reason = stop_reason
        self.events.append({
            "type": "prompt_done",
            "stop_reason": stop_reason.value if stop_reason else None,
        })

    # ------------------------------------------------------------------ updates

    def handle_update(self, update: dict[str, Any]) -> None:
        """Route one ``session/update`` notification payload."""
        kind = update.get("sessionUpdate")

        if kind == "tool_call":
            self._flush_pending_text()
            record = ToolCallRecord(
                tool_call_id=str(update.get("toolCallId", "")),
                title=str(update.get("title", "")),
                kind=str(update.get("kind", "other")),
            )
            try:
                record.status = ToolCallStatus(update.get("status", "pending"))
            except ValueError:
                record.status = ToolCallStatus.PENDING
            content = update.get("content")
            if isinstance(content, list):
                record.content.extend(content)
            locs = update.get("locations")
            if isinstance(locs, list):
                record.locations.extend(locs)
            record.raw_input = update.get("rawInput")
            record.raw_output = update.get("rawOutput")

            self.tool_calls.append(record)
            self._tool_call_index[record.tool_call_id] = record
            self.events.append({
                "type": "tool_call",
                "tool_call_id": record.tool_call_id,
                "title": record.title,
                "kind": record.kind,
                "status": record.status.value,
            })

        elif kind == "tool_call_update":
            tc_id = str(update.get("toolCallId", ""))
            record = self._tool_call_index.get(tc_id)
            if record is None:
                # Server emitted tool_call_update without a prior tool_call;
                # synthesize the start so the event log stays consistent.
                self._flush_pending_text()
                record = ToolCallRecord(
                    tool_call_id=tc_id,
                    title=str(update.get("title", "")),
                    kind=str(update.get("kind", "other")),
                )
                self.tool_calls.append(record)
                self._tool_call_index[tc_id] = record
                self.events.append({
                    "type": "tool_call",
                    "tool_call_id": tc_id,
                    "title": record.title,
                    "kind": record.kind,
                    "status": record.status.value,
                })
            record.apply_update(update)
            self.events.append({
                "type": "tool_call_update",
                "tool_call_id": tc_id,
                "status": record.status.value,
                "content": update.get("content", []),
            })

        elif kind == "agent_message_chunk":
            content = update.get("content", {})
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text", ""))
                if text:
                    self.message_chunks.append(text)
                    self._pending_text.append(
                        {"type": "agent_message", "text": text}
                    )

        elif kind == "agent_thought_chunk":
            content = update.get("content", {})
            if isinstance(content, dict) and content.get("type") == "text":
                text = str(content.get("text", ""))
                if text:
                    self.thought_chunks.append(text)
                    self._pending_text.append(
                        {"type": "agent_thought", "text": text}
                    )

        elif kind == "plan":
            entries = update.get("entries") or []
            if not isinstance(entries, list):
                entries = []
            self._flush_pending_text()
            self.plans.append(entries)
            self.events.append({"type": "plan", "entries": entries})

        else:
            logger.debug("acp.session.unhandled_update kind=%s", kind)

    # ------------------------------------------------------------------ helpers

    def _flush_pending_text(self) -> None:
        """Merge buffered agent_message / agent_thought chunks into events.

        Consecutive same-type chunks collapse into a single event; flipping
        types flushes the current accumulator.  Called whenever a non-text
        event (tool call, plan, prompt end) arrives so the event log reads
        as discrete turns rather than per-token chunks.
        """
        if not self._pending_text:
            return
        current = dict(self._pending_text[0])
        for ev in self._pending_text[1:]:
            if ev["type"] == current["type"]:
                current["text"] = (current.get("text", "") or "") + ev["text"]
            else:
                self.events.append(current)
                current = dict(ev)
        self.events.append(current)
        self._pending_text.clear()

    # ------------------------------------------------------------------ views

    @property
    def full_message(self) -> str:
        return "".join(self.message_chunks)

    @property
    def full_thought(self) -> str:
        return "".join(self.thought_chunks)

    @property
    def n_tool_calls(self) -> int:
        return len(self.tool_calls)

    @property
    def n_completed_tool_calls(self) -> int:
        return sum(1 for r in self.tool_calls if r.is_terminal())


__all__ = ["ACPSession", "ToolCallRecord"]
