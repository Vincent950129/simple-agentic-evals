"""MCP -> OpenAI tool bridging (schema sanitization).

The gym's MCP ``tools/list`` advertises JSON-Schema that the OpenAI (and
compatible) function-calling APIs reject: a few EOG tools carry a *top-level*
``anyOf``/``oneOf``/``allOf``/``enum``/``not``, which OpenAI refuses with

    Invalid schema for function ...: schema must have type 'object' and not have
    'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level.

This module is the SDK-bundled version of the reference harness's
``mcp_http_bridge`` schema rewriter. Rather than *drop* those tools (silently
losing capability), it strips the offending keyword and surfaces the constraint
as a human-readable hint appended to the tool description, so the model still
knows what it must provide. Server-side validation catches bad combinations and
returns a recoverable error.

Use :func:`to_openai_tools` to turn a session's ``list_tools()`` output into the
``tools=[...]`` argument for ``chat.completions.create``; :func:`sanitize_tool_schema`
exposes the underlying rewrite if you build the tool specs yourself.
"""

from __future__ import annotations

from typing import Any

__all__ = ["to_openai_tools", "sanitize_tool_schema", "BAD_TOP_LEVEL_KEYS"]

# JSON-Schema keywords OpenAI forbids at the top level of a function's parameters.
BAD_TOP_LEVEL_KEYS = ("oneOf", "anyOf", "allOf", "enum", "not")


def _describe_anyof_required(branches: list[Any]) -> str | None:
    """If ``branches`` is a list of ``{"required": [field]}`` clauses, summarize
    them as ``"Provide at least one of: a, b, c."``; else return ``None``."""
    fields: list[str] = []
    for b in branches:
        if not isinstance(b, dict) or set(b.keys()) != {"required"}:
            return None
        req = b["required"]
        if not isinstance(req, list) or len(req) != 1 or not isinstance(req[0], str):
            return None
        fields.append(req[0])
    if not fields:
        return None
    seen: set[str] = set()
    uniq = [f for f in fields if not (f in seen or seen.add(f))]
    return "Provide at least one of: " + ", ".join(uniq) + "."


def sanitize_tool_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rewrite one tool's input schema into an OpenAI-compatible object schema.

    Strips top-level ``oneOf|anyOf|allOf|enum|not`` (nested occurrences are left
    alone -- OpenAI only forbids them at the top level) and returns
    ``(rewritten_schema, hints)`` where ``hints`` are human-readable constraint
    strings you can append to the tool description.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}, []

    out = dict(schema)
    hints: list[str] = []

    if out.get("type") != "object":
        # Wrap atypical top-level schemas so the API accepts them.
        out = {"type": "object", "properties": {"value": dict(schema)}}

    if "anyOf" in out:
        branches = out.pop("anyOf")
        hint = _describe_anyof_required(branches) if isinstance(branches, list) else None
        hints.append(hint or "Constraint: at least one of the listed fields is required.")
    if "oneOf" in out:
        branches = out.pop("oneOf")
        hint = _describe_anyof_required(branches) if isinstance(branches, list) else None
        hints.append(hint or "Constraint: exactly one of the listed fields must be provided.")
    if "allOf" in out:
        clauses = out.pop("allOf")
        if isinstance(clauses, list):
            for c in clauses:
                if not isinstance(c, dict):
                    continue
                for k, v in (c.get("properties") or {}).items():
                    out.setdefault("properties", {}).setdefault(k, v)
                for r in c.get("required") or []:
                    if r not in out.setdefault("required", []):
                        out["required"].append(r)
        hints.append("Constraint: all of the listed conditions apply.")
    if "enum" in out:
        vals = out.pop("enum")
        hints.append(f"Constraint: top-level enum values: {vals!r}.")
    if "not" in out:
        out.pop("not")
        hints.append("Constraint: the listed schema must NOT match.")

    return out, hints


def to_openai_tools(
    mcp_tools: list[dict[str, Any]], *, max_desc: int = 1024
) -> list[dict[str, Any]]:
    """Convert MCP ``tools/list`` descriptors into OpenAI function-tool specs.

    Each returned item is ``{"type": "function", "function": {name, description,
    parameters}}`` with the schema sanitized (see :func:`sanitize_tool_schema`)
    and any stripped-constraint hints appended to the description. Feed the
    result straight to ``chat.completions.create(tools=...)``.
    """
    out: list[dict[str, Any]] = []
    for t in mcp_tools:
        if not isinstance(t, dict):
            continue
        raw = t.get("inputSchema") or t.get("parameters") or {}
        schema, hints = sanitize_tool_schema(raw if isinstance(raw, dict) else {})
        desc = (t.get("description") or "").rstrip()
        if hints:
            desc = (desc + " " + " ".join(hints)).strip()
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": desc[:max_desc],
                "parameters": schema,
            },
        })
    return out
