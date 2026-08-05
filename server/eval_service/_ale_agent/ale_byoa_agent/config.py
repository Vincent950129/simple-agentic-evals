"""ByoaConfig — knobs for the Bring-Your-Own-Agent staging deployer.

Standalone dataclass (ALE convention: each agent owns its config in
``<pkg>.config`` as ``<DeployerStem>Config``; ``factory._config_class_for``
resolves ``ByoaDeployer`` -> ``ByoaConfig``). The deployer runs no LLM, so
``model`` exists only because the yaml loader maps a top-level ``model:`` into
``config["model"]`` and the run writer records it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class ByoaConfig:
    """Tunables for :class:`~ale_byoa_agent.deployer.ByoaDeployer`."""

    name: ClassVar[str] = "byoa"

    model: str = "byoa"
    """Unused. Present so a ``model:`` line in the agent yaml is recorded and
    not dropped by ``build_config`` — the BYOA agent makes no model calls."""

    submission_dir: str = ""
    """Host directory whose files are the client's deliverable. Every file under
    it (recursively) is written into the task's in-sandbox output dir, preserving
    relative paths. Empty -> nothing to stage (the run completes as a no-op)."""

    output_dir_override: str = ""
    """Explicit in-sandbox output directory to write the submission into. Empty
    (default) -> derive it from the rendered task prompt's output path tokens
    (``ale_run.agents.dummy.pathscan``), the same heuristic the smoke agent uses."""

    connect_timeout_s: int = 180
    """Seconds to wait for the sandbox cua-server to become responsive."""
