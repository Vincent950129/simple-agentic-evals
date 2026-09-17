"""simple_agentic_evals -- a lightweight client for agentic evaluations.

Inspired by openai/simple-evals, but for *agentic* benchmarks: the **service**
hosts executable environments, reference agent harnesses, and graders; you
connect, pick a task, run a harness, and read the score. EOG and ALE are trusted
hosted adapters. Any reviewed verifier-backed benchmark can use the same API
through a checksum-approved loopback adapter, including the bundled TB2,
APEX-Agents, and Hyper-tau references. A typical loop is ~10 lines:

    from simple_agentic_evals import EvalClient, react_agent

    client = EvalClient()   # endpoint + auth resolved for you (env / served wheel)
    for task in client.tasks("evovling_tools", "eog", domain="hr",
                             version=1, split="test", limit=5):
        with task:                                   # provisions a fresh env
            run = react_agent(task, api_key="sk-...")   # the harness runs on the service
            grade = task.grade()
            print(task.task_id, grade.pass_rate, run.latency_s, run.total_tokens)

Two provided harnesses run **on the selected service** (hosted or loopback), so
the benchmark adapter—not the caller—owns provisioning and grading:

* ``react_agent`` -- EnterpriseOps-Gym's reference ReAct agent (EOG only).
* ``acp_codex_agent`` -- Codex over ACP. For EOG it acts on the task's gym MCP
  tools (skills/agents/tools; the agents track runs the reference multi-agent
  orchestrator); for **ALE** it drives the Codex CLI agent inside the sandbox
  (solve + grade). Pass your OpenAI ``api_key``; the service runs it end to end
  (completion-loop + resume-on-stall) and behaves like the reference harness.

ALE requires a CLI agent harness -- use ``acp_codex_agent`` (``react_agent`` on an
ALE task raises a clear error). Long runs (an ALE Docker+Codex solve, or a slow
EOG Codex loop) can take several minutes; the harness helpers submit them as a
background job on the service and poll for the result, so nothing is held open in
one long request that a proxy/tunnel would time out -- this is transparent, you
still just call ``react_agent(...)`` / ``acp_codex_agent(...)`` and get the run back.

Every run reports all three metrics so you pick what matters: **accuracy** from
``task.grade().pass_rate``, **latency** from ``run.latency_s``, **tokens** from
``run.total_tokens``. ``task.evaluate(agent=...)`` runs a harness then grades and
returns them together as an :class:`EvalReport`. Pass ``verbose="steps"`` (or
``include_trace=True``) to see per-step thoughts, tool calls, and sub-agent calls.

Bring your own agent instead -- hand ``task.mcp_servers`` to any MCP client and
use ``to_openai_tools`` to turn the gym's MCP tools into OpenAI-ready function
specs (the MCP<->OpenAI schema bridge):

    mcp = task.mcp_session(task.mcp_servers[0])     # minimal MCP client, pre-authed
    tools = to_openai_tools(mcp.list_tools())       # OpenAI-ready function specs
    mcp.call_tool(tools[0]["function"]["name"], {...})

``ContinualMetrics`` computes ACC/BWT/FWT for the evolving (continual-learning)
study. Advanced -- attach the evolving tools/skills/agents per stage:

    for task in client.tasks("evovling_tools", "eog", domain="hr", version=2,
                             resource_mode="accumulative"):
        ...  # task.resources -> {"kind","mode","count","names",...}
    client.resources("evovling_skills", "ale", version=3, mode="oracle",
                     task_id="legal/legal_dr_fees_01")    # inspect without a session
"""

import warnings
from typing import Any

from .agents import AgentRun, react_agent
from .benchmark import BenchmarkReport, CohortResult, run_benchmark
from .client import (EvalClient, EvalReport, GradeResult, McpServer, MissingAPIKey,
                     ServiceError, Task)
from .command_agent import CommandAgent, CommandAgentError
from .codex import CodexRun, acp_codex_agent
from .leaderboard import aggregate_leaderboard, run_leaderboard
from .mcp import MCPSession
from .metrics import ContinualMetrics, StageResult
from .runtime_adapters import catalog_targets, inspect_adapter, resolve_target, serve_local
from .tools import sanitize_tool_schema, to_openai_tools


# -- deprecated aliases (one release) --------------------------------------- #
def run_eog_agent(*args: Any, **kwargs: Any) -> AgentRun:
    """Deprecated alias for :func:`react_agent` (removed in a future release)."""
    warnings.warn(
        "run_eog_agent is deprecated; use react_agent instead.",
        DeprecationWarning, stacklevel=2,
    )
    return react_agent(*args, **kwargs)


def run_codex_agent(*args: Any, **kwargs: Any) -> CodexRun:
    """Deprecated alias for :func:`acp_codex_agent` (removed in a future release)."""
    warnings.warn(
        "run_codex_agent is deprecated; use acp_codex_agent instead.",
        DeprecationWarning, stacklevel=2,
    )
    return acp_codex_agent(*args, **kwargs)


__all__ = [
    # One call for a paper-comparable number
    "run_benchmark", "run_leaderboard", "aggregate_leaderboard",
    "BenchmarkReport", "CohortResult",
    "CommandAgent", "CommandAgentError",
    # Core client
    "EvalClient", "Task", "GradeResult", "EvalReport",
    "McpServer", "MCPSession", "MissingAPIKey", "ServiceError",
    # Provided harnesses (run on the service)
    "react_agent", "acp_codex_agent",
    "AgentRun", "CodexRun",
    # Deprecated aliases
    "run_eog_agent", "run_codex_agent",
    # MCP<->OpenAI bridge (bring your own agent) + continual-learning metrics
    "to_openai_tools", "sanitize_tool_schema",
    "ContinualMetrics", "StageResult",
    "catalog_targets", "inspect_adapter", "resolve_target", "serve_local",
]
__version__ = "0.19.1"
