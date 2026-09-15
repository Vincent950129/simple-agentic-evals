"""Dependency-free contract available to reviewed local adapter bundles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


class GradingNotSupported(RuntimeError):
    pass


@dataclass
class McpServer:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "streamable_http"
    path: str = ""


@dataclass
class McpAction:
    type: Literal["mcp"] = "mcp"
    mcp_servers: list[McpServer] = field(default_factory=list)


@dataclass
class SandboxFile:
    name: str
    path: str
    format: str = ""
    description: str = ""
    size: int = -1


@dataclass
class SandboxAction:
    type: Literal["sandbox"] = "sandbox"
    workdir: str = "base"
    input_files: list[SandboxFile] = field(default_factory=list)
    output_dir: str = "output"
    output_path: str = ""
    gradable: bool = True
    grading: str = "official"


@dataclass
class TerminalAction:
    type: Literal["terminal"] = "terminal"
    runtime: str = ""
    session_name: str = ""
    workdir: str = "/workspace"
    transport: str = "managed"
    timeout_sec: int = 1800
    network: str = "disabled"
    resource_enforced: bool = False


@dataclass
class ManagedRuntimeAction:
    type: Literal["managed_runtime"] = "managed_runtime"
    runtime: str = ""
    task_id: str = ""
    timeout_sec: int = 1800
    network: str = "declared-by-adapter"
    resource_enforced: bool = False
    credentials: list[str] = field(default_factory=list)


@dataclass
class VerifierView:
    name: str
    passed: bool
    score: float | None = None
    expected: Any = None
    actual: Any = None
    comparison_type: str = ""
    description: str = ""
    details: Any = None
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
