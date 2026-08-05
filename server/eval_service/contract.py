"""Wire contract for the evaluation service.

The central abstraction is a **session lifecycle** that is identical across
benchmark families; only the *action surface* differs:

    create_session  ->  agent acts (via `action`)  ->  grade  ->  teardown

``action`` is a discriminated union on ``type``:

  * ``"mcp"``     -- EnterpriseOps-Gym: the agent acts by calling MCP tools on
                     the returned servers. State lands in a per-session DB that
                     the verifiers grade.  (implemented)

  * ``"sandbox"`` -- Agents' Last Exam: the service serves the task's input
                     files; the agent works in its own environment and submits
                     an output artifact, which is graded by the task's own
                     ``evaluate()`` / ``score_outputs.py``.  (implemented)

Both shapes share the same lifecycle, so a client written against EOG today
needs no change when it starts driving ALE — only ``action.type`` differs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# --------------------------------------------------------------------------- #
# Action surfaces                                                             #
# --------------------------------------------------------------------------- #
@dataclass
class McpServer:
    """One MCP server the agent should call to act on the environment.

    ``url`` is a streamable-HTTP MCP endpoint (FastMCP). ``headers`` MUST be
    sent on every request — they bind the agent's tool calls to this session's
    seeded database (``x-database-id``) and carry the gym's user/context
    identity, so the agent mutates exactly the DB the verifier will read.
    """

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "streamable_http"
    # Relative proxy path on the eval service (``/v1/sessions/{id}/mcp/{name}``).
    # Prefer ``base_url + path`` from a client that already knows its base URL;
    # ``url`` is the same target as an absolute best-effort. The service proxies
    # these calls to the gym and binds them to this session's DB, so acting works
    # over the public tunnel exactly like ALE inputs/submit.
    path: str = ""


@dataclass
class McpAction:
    type: Literal["mcp"] = "mcp"
    mcp_servers: list[McpServer] = field(default_factory=list)


@dataclass
class SandboxFile:
    name: str
    path: str            # relative to the workdir base, e.g. "input/cases.json"
    format: str = ""
    description: str = ""
    size: int = -1       # bytes on disk (-1 if unknown)


@dataclass
class SandboxAction:
    """ALE-style action surface: a file workspace.

    The agent fetches the ``input_files`` (served at
    ``GET /v1/sessions/{id}/inputs/{path}``), does the work in its own
    environment, and submits its deliverable to
    ``POST /v1/sessions/{id}/submit`` (written under ``output_dir``). Grading
    runs the task's own ``evaluate()`` over that artifact.

    ``output_path`` is the deliverable path the task prompt asks for (a hint
    parsed from the prompt). ``gradable`` is True when the host can grade the
    submission standalone; when False, ``grading`` explains why (e.g. the task's
    grader executes commands in the full sandbox, or the ALE venv is absent).
    """

    type: Literal["sandbox"] = "sandbox"
    workdir: str = "base"
    input_files: list[SandboxFile] = field(default_factory=list)
    output_dir: str = "output"
    output_path: str = ""
    gradable: bool = True
    grading: str = "local_evaluate"


# --------------------------------------------------------------------------- #
# Task + session views                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class TaskSelector:
    """Fully-qualified address of a task on disk.

        data/<dataset>/<benchmark>[/<domain>]/v<version>/<split>.jsonl
    """

    dataset: str          # evovling_tools | evovling_skills | evovling_agents
    benchmark: str        # eog | ale
    version: int
    split: str            # train | test
    domain: str | None = None   # required for eog; None for the flat ale layout
    task_id: str | None = None


@dataclass
class ResourceSpec:
    """The evolving resource (tools|skills|agents) selected for this session.

    ``mode`` is ``oracle`` (minimal gold), ``accumulative`` (the whole set
    accumulated up to ``stage``; the realistic evolving setting) or ``none``.
    ``names`` are the tool/skill/agent ids; ``items`` carries on-disk bundle
    metadata for skills/agents (fetch full contents via ``GET /v1/resources``).
    """

    kind: str = "unknown"          # tools | skills | agents | unknown
    mode: str = "none"             # oracle | accumulative | none
    stage: str = ""                # "v2" | "full"
    count: int = 0
    names: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


@dataclass
class TaskView:
    """What the agent is given to attempt the task (no grading info leaks)."""

    selector: dict[str, Any]
    task_id: str
    system_prompt: str
    user_prompt: str
    # Advisory only — the gold tool set the task needs. The agent still sees the
    # full tool surface on the MCP server; this is a hint, not an allowlist.
    oracle_tools: list[str] = field(default_factory=list)
    # The evolving resource the caller selected for this session (see above).
    resources: dict[str, Any] | None = None


@dataclass
class SessionView:
    session_id: str
    status: str                       # active | graded | closed | expired
    task: TaskView
    action: dict[str, Any]            # McpAction | SandboxAction (asdict)
    created_at: float
    expires_at: float


# --------------------------------------------------------------------------- #
# Grading                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class VerifierView:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    comparison_type: str = ""
    error: str | None = None


@dataclass
class GradeResult:
    session_id: str
    task_id: str
    overall_success: bool
    pass_rate: float
    n_passed: int
    n_total: int
    per_verifier: list[VerifierView] = field(default_factory=list)


def to_dict(obj: Any) -> Any:
    """asdict() that tolerates already-plain values."""
    try:
        return asdict(obj)
    except TypeError:
        return obj
