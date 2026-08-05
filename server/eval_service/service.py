"""FastAPI Evaluation-as-a-Service.

Endpoints (all under ``/v1``):

    GET    /v1/health
    GET    /v1/usage                       -- SDK/API usage: popularity + bottlenecks
    GET    /v1/usage/dashboard             -- HTML monitor page (alias: /dashboard)
    GET    /v1/benchmarks                  -- what's available on disk
    GET    /v1/tasks                       -- task ids for a (dataset,benchmark,...)
    POST   /v1/sessions                    -- provision a task -> action surface
    GET    /v1/sessions/{id}               -- inspect a session
    POST   /v1/sessions/{id}/grade         -- grade current env state
    DELETE /v1/sessions/{id}               -- tear down (free the DB)

Run from ``server/`` so ``eval_service`` imports as a package:

    bash server/run.sh
    # or
    cd server && python -m uvicorn eval_service.service:app --host 0.0.0.0 --port 8077
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import ale_codex_runner, ale_docker_grader, ale_grader, loader, resources, usage
from .contract import GradeResult, SessionView, TaskView, VerifierView
from .environment import (
    GradingNotSupported,
    get_environment,
)

logging.basicConfig(
    level=os.environ.get("EVAL_SERVICE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("eval_service")

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
SESSION_TTL_SEC = int(os.environ.get("EVAL_SERVICE_SESSION_TTL_SEC", "1800"))
REAPER_INTERVAL_SEC = int(os.environ.get("EVAL_SERVICE_REAPER_INTERVAL_SEC", "60"))
# Seeding is the heavy op (large SQL); cap concurrent seeds across all clients.
MAX_CONCURRENT_SEEDS = int(os.environ.get("EVAL_SERVICE_MAX_CONCURRENT_SEEDS", "4"))
# When the service sits behind a reverse proxy/tunnel, set this so the MCP action
# advertises a publicly reachable base (else it's inferred from X-Forwarded-Host
# or the request URL).
PUBLIC_URL = os.environ.get("EVAL_SERVICE_PUBLIC_URL", "").strip().rstrip("/")
MCP_PROXY_TIMEOUT = float(os.environ.get("EVAL_SERVICE_MCP_TIMEOUT_SEC", "300"))
# MCP streamable-HTTP headers we forward each way (request -> gym, gym -> client).
_MCP_REQ_HEADERS = {"content-type", "accept", "mcp-session-id",
                    "mcp-protocol-version", "last-event-id"}
_MCP_RESP_HEADERS = {"content-type", "mcp-session-id", "mcp-protocol-version", "cache-control"}

_mcp_client: httpx.AsyncClient | None = None


def _get_mcp_client() -> httpx.AsyncClient:
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = httpx.AsyncClient(
            timeout=httpx.Timeout(MCP_PROXY_TIMEOUT, read=MCP_PROXY_TIMEOUT),
            follow_redirects=True,
        )
    return _mcp_client


def _public_base(request: Request) -> str:
    """Best-effort public base URL for building reachable MCP proxy URLs."""
    if PUBLIC_URL:
        return PUBLIC_URL
    xfh = request.headers.get("x-forwarded-host")
    if xfh:
        proto = (request.headers.get("x-forwarded-proto", "https").split(",")[0] or "https").strip()
        return f"{proto}://{xfh.split(',')[0].strip()}"
    return str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------- #
# Session registry (in-memory; fine for an internal single-process service)   #
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    session_id: str
    dataset: str
    benchmark: str
    version: int | str             # stage index, or "full" (all stages)
    split: str
    domain: str | None
    row: Any                       # TaskRow
    env_state: dict[str, Any]      # EOG: {gym: db_id}; ALE: workspace info
    action: dict[str, Any]         # serialized McpAction | SandboxAction
    created_at: float
    expires_at: float
    status: str = "active"         # active | graded | closed | expired
    last_grade: dict[str, Any] | None = None
    resource_mode: str = "none"    # oracle | accumulative | none
    # EOG only: {gym_name: {"url": <real gym mcp url>, "headers": {...}}}. The
    # client-facing action carries proxied URLs; these are the real targets the
    # /v1/sessions/{id}/mcp/{gym} proxy forwards to (binding each call to this DB).
    mcp_targets: dict[str, dict[str, Any]] = field(default_factory=dict)


SESSIONS: dict[str, Session] = {}
_LOCK = asyncio.Lock()
_SEED_SEM = asyncio.Semaphore(MAX_CONCURRENT_SEEDS)


@dataclass
class Job:
    """A backgrounded agent run, polled via ``GET .../jobs/{job_id}``.

    Long agent runs (ALE's Docker+Codex solve+grade, or a slow EOG Codex loop)
    can outlast a reverse-proxy/tunnel's per-request timeout, which would surface
    to the caller as a spurious 5xx. Running them as a background job keeps every
    HTTP request short (submit + cheap status polls), so no single request is
    held open long enough to trip an upstream timeout.
    """
    job_id: str
    session_id: str
    status: str = "pending"          # pending | running | done | error
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None   # {"status_code": int, "detail": str}
    created_at: float = 0.0
    updated_at: float = 0.0


JOBS: dict[str, Job] = {}
# Cap how long a finished/orphaned job lingers in memory (its result is fetched
# once by the poller, then the session is graded/closed); the reaper drops the
# rest so JOBS can't grow without bound on a long-lived process.
JOB_RETENTION_SEC = int(os.environ.get("EVAL_SERVICE_JOB_RETENTION_SEC", "3600"))


# --------------------------------------------------------------------------- #
# Request models                                                              #
# --------------------------------------------------------------------------- #
class CreateSessionBody(BaseModel):
    dataset: str = Field("evovling_tools", description="evovling_tools|evovling_skills|evovling_agents")
    benchmark: str = Field("eog", description="eog|ale")
    version: int | str = Field(..., description="stage index (>=1) or 'full' for all stages")
    split: str = Field("test", description="train|test")
    domain: str | None = Field(None, description="required for eog; null for ale")
    task_id: str | None = Field(None, description="omit to take the first task in the split")
    resource_mode: str | None = Field(
        None,
        description="which evolving resource (tools/skills/agents) to expose: "
        "oracle|accumulative|none. Default: accumulative (tools/agents), none (skills).",
    )
    ttl_sec: int | None = Field(None, description="override default session TTL")


class SubmitFile(BaseModel):
    path: str = Field(..., description="output path (relative; e.g. 'results.json')")
    content: str | None = Field(None, description="text content")
    content_b64: str | None = Field(None, description="base64 bytes (for binary)")


class SubmitBody(BaseModel):
    files: list[SubmitFile] = Field(..., description="artifact file(s) to grade")


class RunAgentBody(BaseModel):
    """Ask the service to run the reference agent against this session (EOG only).

    The service runs the *reference* agent for the session's track on the host,
    so the caller needs no local install (no gym, no ``codex`` binary):
      * ``evovling_tools``  -> EnterpriseOps-Gym's reference ReAct agent.
      * ``evovling_skills`` -> Codex over ACP (the harness default runtime).
      * ``evovling_agents`` -> Codex multi-agent (orchestrator + subagents).

    ``agent`` overrides that auto-choice (``react`` | ``codex`` | ``auto``).
    Provide ``openai_api_key`` to run with the caller's own key (written to a
    per-trial API-key ``auth.json`` for Codex, or used as the OpenAI key for
    ReAct); omit it to use the host's own credentials. Grade afterwards with
    ``POST .../grade`` (the agent's tool calls mutated this session's DB).
    """
    agent: str | None = Field(
        None, description="which reference agent: react|codex|auto (default: auto by dataset)")
    openai_api_key: str | None = Field(
        None, description="OpenAI API key used to run the agent (else the host's credentials)")
    model: str | None = Field(None, description="model (default: server's configured model)")
    transport: str | None = Field(
        None, description="Codex only: stdio|streamable_http (default: server's configured transport)")
    restrict_to_selected_tools: bool = Field(
        False, description="oracle mode: restrict the agent to the task's gold tool set")
    mcp_only: bool | None = Field(
        None, description="Codex only: disable built-ins so its surface is the gym MCP tools")
    max_episodes: int | None = Field(None, description="Codex only: max completion-loop episodes (default 4)")
    max_iterations: int | None = Field(None, description="ReAct only: max reason/act iterations")
    timeout_s: int | None = Field(None, description="total wall-clock budget in seconds; default depends on harness/benchmark when unset: ReAct 1800, EOG Codex 900, ALE Codex 9000 (reference-parity)")
    background: bool = Field(
        False,
        description="run asynchronously: return a job_id immediately and poll "
        "GET .../jobs/{job_id} for the result. Recommended for long ALE/Codex runs "
        "so no single request is held open long enough to hit a proxy/tunnel timeout.")
    include_trace: bool = Field(
        False, description="also return a structured per-step trace (tool/agent calls); larger payload")
    prompt_suffix: str | None = Field(
        None,
        description="ALE only: text appended to the task prompt. On the agents track this "
        "carries the software allowlist + the orchestrator's delegation protocol and "
        "specialist roster; the tool-less orchestrator cannot delegate without it. "
        "Defaults to the task row's system_prompt (the cumulative-pool roster), so pass "
        "it explicitly for oracle mode to avoid naming distractor specialists.")
    sandbox_env: dict[str, str] | None = Field(
        None,
        description="ALE only: extra env forwarded into the sandbox by ale_run. Only "
        "ALE_AGENTS_* / ALE_GUARD_* keys are honoured (others are dropped): the "
        "specialist-pool bundle the in-sandbox stager unpacks into ~/.codex, and the "
        "hard software fence. Without it a multi-agent run has nobody to spawn.")
    memory_key: str | None = Field(
        None,
        description="EOG agents track only: enable Codex's own memory for this trial "
        "and name the memory it belongs to. The service turns on [features] memories "
        "and runs the trial in a PERSISTENT CODEX_HOME kept for this key, outside the "
        "per-request workspace it deletes afterwards, so Codex accumulates memory "
        "across every trial sent with the same key -- Codex does the extracting and "
        "writing itself. Use one key per experiment. Unset leaves the feature off "
        "(no memory).")
    memory_main_only: bool = Field(
        False,
        description="With memory_key: give the memory to the multi-agent ORCHESTRATOR "
        "only. The home and its memory are unchanged, but every specialist mounted "
        "under it gets Codex's per-agent [memories] use_memories = false, so the agent "
        "that routes is informed by earlier trials and the agents it spawns are not. "
        "This is the read half only; pass root_only to /v1/memory/consolidate so the "
        "memory is also BUILT from the orchestrator's rollouts. Ignored without "
        "memory_key.")
    # Accepted for API symmetry with the SDK; server-side runs the harness's own
    # completion protocol (same defaults: sentinel 'TASK_COMPLETE', require=on).
    require_completion: bool | None = Field(None, description="(server uses its harness default)")
    completion_sentinel: str | None = Field(None, description="(server uses its harness default)")


# Back-compat alias: the run endpoint historically accepted a "RunCodexBody".
RunCodexBody = RunAgentBody


# --------------------------------------------------------------------------- #
# App                                                                         #
# --------------------------------------------------------------------------- #
app = FastAPI(title="Evolving-Benchmarks Evaluation Service", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Usage telemetry: every SDK call is a request here, so timing each one gives
# both "how popular over time" (counts/hour) and "what's the bottleneck now"
# (per-endpoint latency). Exposed at GET /v1/usage; see eval_service/usage.py.
USAGE = usage.UsageLog()


@app.middleware("http")
async def _usage_middleware(request: Request, call_next):
    """Time every request into ``USAGE``; never fail a request over telemetry."""
    t0 = time.perf_counter()
    response = await call_next(request)
    try:
        # The matched route's template (e.g. ``/v1/sessions/{session_id}/run_agent``)
        # groups per-id calls into one endpoint; fall back to the raw path (404s).
        route = request.scope.get("route")
        endpoint = getattr(route, "path", None) or request.url.path
        USAGE.record(
            method=request.method,
            endpoint=endpoint,
            status=response.status_code,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            client=request.headers.get("user-agent", ""),
            dataset=request.query_params.get("dataset"),
        )
    except Exception as e:  # noqa: BLE001 - telemetry must never break a request
        logger.debug("usage: record failed: %s", e)
    return response


def _task_view(sess: Session) -> TaskView:
    row = sess.row
    sel = {
        "dataset": sess.dataset, "benchmark": sess.benchmark,
        "domain": sess.domain, "version": sess.version, "split": sess.split,
        "task_id": row.task_id,
    }
    # Compact resource block for the selected mode (names + bundle metadata; the
    # full SKILL.md/.toml contents are served by GET /v1/resources).
    try:
        res = resources.resolve(
            dataset=sess.dataset, benchmark=sess.benchmark, version=sess.version,
            domain=sess.domain, row=row, mode=sess.resource_mode,
            include_content=False,
        )
    except Exception as e:  # noqa: BLE001 - never fail the view over resources
        logger.warning("resource resolve failed for %s: %s", row.task_id, e)
        res = None
    return TaskView(
        selector=sel,
        task_id=row.task_id,
        system_prompt=row.system_prompt,
        user_prompt=row.user_prompt,
        oracle_tools=list(getattr(row, "selected_tools", []) or []),
        resources=res,
    )


def _session_view(sess: Session) -> dict[str, Any]:
    view = SessionView(
        session_id=sess.session_id,
        status=sess.status,
        task=_task_view(sess),
        action=sess.action,
        created_at=sess.created_at,
        expires_at=sess.expires_at,
    )
    return asdict(view)


# --------------------------------------------------------------------------- #
# Lifecycle: TTL reaper                                                        #
# --------------------------------------------------------------------------- #
@app.on_event("startup")
async def _start_reaper() -> None:
    app.state.reaper = asyncio.create_task(_reaper_loop())
    # Replay any prior usage log so "popularity over time" survives restarts.
    try:
        replayed = await asyncio.to_thread(USAGE.load)
        if replayed:
            logger.info("usage: replayed %d records from %s", replayed, USAGE.path)
    except Exception as e:  # noqa: BLE001 - never block startup on telemetry
        logger.warning("usage: replay failed: %s", e)
    # Best-effort: ensure the client SDK wheel exists so GET /sdk can serve it
    # (dist/ is gitignored, so a fresh checkout starts with none).
    if _latest_wheel() is None:
        try:
            await asyncio.to_thread(_build_sdk_wheel, PUBLIC_URL)
            logger.info("built SDK wheel: %s", _latest_wheel())
        except Exception as e:  # noqa: BLE001 - never block startup on this
            logger.warning("SDK wheel build failed (GET /sdk will 404): %s", e)


@app.on_event("shutdown")
async def _stop_reaper() -> None:
    task = getattr(app.state, "reaper", None)
    if task:
        task.cancel()
    # Best-effort teardown of any live sessions so we don't leak gym DBs.
    async with _LOCK:
        live = [s for s in SESSIONS.values() if s.status in ("active", "graded")]
    for s in live:
        await _teardown(s)


async def _reaper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(REAPER_INTERVAL_SEC)
            now = time.time()
            # Never reap a session that still has a background run in flight.
            busy = {j.session_id for j in JOBS.values()
                    if j.status in ("pending", "running")}
            async with _LOCK:
                expired = [
                    s for s in SESSIONS.values()
                    if s.status in ("active", "graded") and s.expires_at <= now
                    and s.session_id not in busy
                ]
            for s in expired:
                logger.info("reaper: expiring session %s", s.session_id)
                await _teardown(s, mark="expired")
            # Drop jobs whose session is gone, or that finished long ago, so the
            # in-memory JOBS map can't grow unbounded on a long-lived process.
            stale = [
                jid for jid, j in JOBS.items()
                if j.session_id not in SESSIONS
                or (j.status in ("done", "error")
                    and now - j.updated_at > JOB_RETENTION_SEC)
            ]
            for jid in stale:
                JOBS.pop(jid, None)
        except asyncio.CancelledError:  # noqa: PERF203
            break
        except Exception as e:  # noqa: BLE001
            logger.warning("reaper loop error: %s", e)


def _drop_jobs(session_id: str) -> None:
    """Forget any background jobs for a session (its result was already polled)."""
    for jid in [jid for jid, j in JOBS.items() if j.session_id == session_id]:
        JOBS.pop(jid, None)


async def _teardown(sess: Session, mark: str = "closed") -> None:
    env = get_environment(sess.benchmark)
    try:
        await asyncio.to_thread(env.teardown, sess.row, sess.env_state)
    except Exception as e:  # noqa: BLE001
        logger.warning("teardown error for %s: %s", sess.session_id, e)
    sess.status = mark
    _drop_jobs(sess.session_id)


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@app.get("/v1/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "data_root": str(loader.DATA_ROOT),
        "active_sessions": sum(1 for s in SESSIONS.values() if s.status == "active"),
        "ttl_sec": SESSION_TTL_SEC,
        "ale_docker": ale_docker_grader.availability(),
    }


@app.get("/v1/usage")
def usage_summary(top: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """Aggregate SDK/API usage -- popularity over time + current bottlenecks.

    ``top_by_calls`` ranks endpoints by request count (how popular each is);
    every row carries server-side latency percentiles, so ``top_by_latency_p95``
    surfaces the current bottleneck. ``by_hour``/``by_day`` are the over-time
    series and ``by_client`` attributes traffic by ``User-Agent`` (the SDK tags
    itself, so you can separate SDK usage from browsers/curl). Full history lives
    in the JSONL at ``log_path`` (survives restarts; replayed on startup).
    """
    return USAGE.summary(top=top)


@app.get("/v1/usage/dashboard", response_class=HTMLResponse, include_in_schema=False)
@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def usage_dashboard() -> HTMLResponse:
    """A self-contained HTML monitor for the usage stats (polls GET /v1/usage).

    No external assets/CDN, so it works on an internal/offline host. Reachable
    directly (``http://<host>:<port>/v1/usage/dashboard`` or ``/dashboard``) and,
    since the ngrok tunnel forwards non-``/_tunnel`` paths upstream, through the
    public URL too.
    """
    return HTMLResponse(usage.DASHBOARD_HTML)


# --------------------------------------------------------------------------- #
# SDK distribution                                                            #
# The service ships its own client. Anyone who can reach the service can       #
# `pip install` the wheel below -- no PyPI account or repo checkout needed:    #
#     pip install "$BASE_URL/sdk/<wheel>"   (filename via GET /sdk)            #
#                                                                              #
# In this repo the client SDK is the top level and the service lives under     #
# `server/`, so the source to build from is two directories up. Point          #
# `EVAL_SERVICE_SDK_SRC` at a `pyproject.toml` directory to build a different  #
# client (e.g. an installed checkout kept elsewhere).                          #
# --------------------------------------------------------------------------- #
_SDK_SRC = Path(
    os.environ.get("EVAL_SERVICE_SDK_SRC", "")
    or Path(__file__).resolve().parent.parent.parent
).resolve()
_SDK_DIST = _SDK_SRC / "dist"


def _latest_wheel() -> Path | None:
    if not _SDK_DIST.is_dir():
        return None
    wheels = list(_SDK_DIST.glob("*.whl"))
    if not wheels:
        return None
    return max(wheels, key=lambda p: p.stat().st_mtime)


def _build_sdk_wheel(public_url: str = "") -> None:
    """Build the ``simple_agentic_evals`` wheel into ``dist/`` (served artifact).

    The wheel is a build artifact (``dist/`` is gitignored), so a fresh checkout
    has none; we build it on startup so ``GET /sdk`` always has something to serve.

    The SDK source already ships a ``DEFAULT_BASE_URL`` (this service's public
    endpoint) in ``_service.py``, so ``EvalClient()`` is pointed here out of the
    box. Passing ``public_url`` (i.e. ``EVAL_SERVICE_PUBLIC_URL`` is set) *overrides*
    that at build time -- handy if the public URL differs from the baked default.
    The override happens in a throwaway copy so the source tree is untouched.
    """
    import subprocess
    import sys

    sdk_dir = _SDK_SRC  # the client checkout (has pyproject.toml)
    if not public_url:
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "-w", str(_SDK_DIST), str(sdk_dir)],
            check=True, capture_output=True,
        )
        return

    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        build_src = Path(tmp) / "sdk"
        shutil.copytree(
            sdk_dir, build_src,
            ignore=shutil.ignore_patterns(
                "dist", "build", "*.egg-info", "__pycache__", ".*", "server"
            ),
        )
        (build_src / "simple_agentic_evals" / "_service.py").write_text(
            '"""Service endpoint baked in when this wheel was built by the service."""\n\n'
            f"DEFAULT_BASE_URL = {public_url.rstrip('/')!r}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps",
             "-w", str(_SDK_DIST), str(build_src)],
            check=True, capture_output=True,
        )


@app.get("/sdk")
def sdk_index(request: Request) -> dict[str, Any]:
    """Manifest for the bundled ``simple_agentic_evals`` client wheel.

    ``base_url`` is this service's own (public) address, so a client can learn where
    it's talking to without the URL being wired in by hand -- the endpoint stays a
    server-side concern.
    """
    wheel = _latest_wheel()
    if wheel is None:
        raise HTTPException(
            status_code=404,
            detail="SDK wheel not built (run, from the repo root: "
            "pip wheel --no-deps -w dist .)",
        )
    base = _public_base(request)
    return {
        "name": "simple-agentic-evals",
        "filename": wheel.name,
        "path": f"/sdk/{wheel.name}",
        "base_url": base,
        "pip_install": f"pip install '{base}/sdk/{wheel.name}'",
    }


@app.get("/sdk/{filename}")
def sdk_file(filename: str) -> FileResponse:
    """Serve a built SDK artifact so clients can ``pip install`` it directly."""
    fp = (_SDK_DIST / filename).resolve()
    # Guard against path traversal: the resolved path must live under dist/.
    if _SDK_DIST != fp.parent or not fp.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        str(fp), filename=fp.name, media_type="application/octet-stream"
    )


@app.get("/v1/benchmarks")
def benchmarks() -> dict[str, Any]:
    return loader.catalog()


@app.get("/v1/tasks")
def tasks(
    dataset: str = Query("evovling_tools"),
    benchmark: str = Query("eog"),
    version: str = Query(..., description="stage index (>=1) or 'full' for all stages"),
    split: str = Query("test"),
    domain: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int = Query(0),
) -> dict[str, Any]:
    try:
        ver = loader.parse_version(version)
        ids = loader.list_task_ids(
            dataset, benchmark, ver, split, domain, limit=limit, offset=offset
        )
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "dataset": dataset, "benchmark": benchmark, "domain": domain,
        "version": ver, "split": split, "count": len(ids), "task_ids": ids,
    }


@app.get("/v1/resources")
def get_resources(
    dataset: str = Query("evovling_tools"),
    benchmark: str = Query("eog"),
    version: str = Query(..., description="stage index (>=1) or 'full'"),
    split: str = Query("test"),
    domain: str | None = Query(None),
    task_id: str | None = Query(None, description="omit to use the first task in the split"),
    mode: str | None = Query(None, description="oracle|accumulative|none (default per-kind)"),
    include_content: bool = Query(True, description="inline SKILL.md/.toml bundle contents"),
) -> dict[str, Any]:
    """Inspect the evolving resource (tools|skills|agents) for a task.

    ``evovling_tools`` -> tool names; ``evovling_skills`` -> gold SKILL.md
    bundles; ``evovling_agents`` -> agent ``.toml`` + skill bundles. Pick
    ``mode`` to see the oracle (gold), accumulative (stage universe), or none.
    """
    try:
        ver = loader.parse_version(version)
        if task_id:
            row = loader.find_row(dataset, benchmark, ver, split, domain, task_id)
        else:
            ids = loader.list_task_ids(dataset, benchmark, ver, split, domain, limit=1)
            if not ids:
                raise HTTPException(status_code=404, detail="split is empty")
            row = loader.find_row(dataset, benchmark, ver, split, domain, ids[0])
    except (FileNotFoundError, ValueError, KeyError) as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        res = resources.resolve(
            dataset=dataset, benchmark=benchmark, version=ver, domain=domain,
            row=row, mode=mode, include_content=include_content,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "selector": {
            "dataset": dataset, "benchmark": benchmark, "domain": domain,
            "version": ver, "split": split, "task_id": row.task_id,
        },
        "resource": res,
    }


@app.post("/v1/sessions")
async def create_session(body: CreateSessionBody, request: Request) -> dict[str, Any]:
    # Resolve stage + resource mode.
    try:
        ver = loader.parse_version(body.version)
        res_mode = resources.normalize_mode(
            body.resource_mode, resources.kind_for(body.dataset)
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # Resolve the row.
    try:
        if body.task_id:
            row = loader.find_row(
                body.dataset, body.benchmark, ver, body.split,
                body.domain, body.task_id,
            )
        else:
            ids = loader.list_task_ids(
                body.dataset, body.benchmark, ver, body.split,
                body.domain, limit=1,
            )
            if not ids:
                raise HTTPException(status_code=404, detail="split is empty")
            row = loader.find_row(
                body.dataset, body.benchmark, ver, body.split,
                body.domain, ids[0],
            )
    except (FileNotFoundError, ValueError, KeyError) as e:
        raise HTTPException(status_code=404, detail=str(e))

    env = get_environment(body.benchmark)

    # Provision the action surface (blocking I/O -> thread, capped concurrency).
    async with _SEED_SEM:
        try:
            env_state, action = await asyncio.to_thread(env.create, row)
        except Exception as e:  # noqa: BLE001
            logger.exception("session create failed for %s", row.task_id)
            raise HTTPException(status_code=502, detail=f"environment provisioning failed: {e}")

    now = time.time()
    ttl = body.ttl_sec or SESSION_TTL_SEC
    sid = f"sess_{uuid.uuid4().hex[:16]}"

    # EOG: the agent acts over MCP. Rewrite the gym URLs (host-local Docker-bridge
    # addresses) to a proxy on THIS service, so acting works over the public
    # tunnel just like ALE inputs/submit. Keep the real targets to forward to.
    mcp_targets: dict[str, dict[str, Any]] = {}
    if getattr(action, "type", "") == "mcp":
        base = _public_base(request)
        for srv in action.mcp_servers:
            mcp_targets[srv.name] = {"url": srv.url, "headers": dict(srv.headers or {})}
            srv.path = f"/v1/sessions/{sid}/mcp/{srv.name}"
            srv.url = f"{base}{srv.path}"

    sess = Session(
        session_id=sid,
        dataset=body.dataset, benchmark=body.benchmark, version=ver,
        split=body.split, domain=body.domain, row=row, env_state=env_state,
        action=asdict(action), created_at=now, expires_at=now + ttl,
        resource_mode=res_mode, mcp_targets=mcp_targets,
    )
    async with _LOCK:
        SESSIONS[sess.session_id] = sess
    if env_state.get("kind") == "ale":
        state_log = f"ale:{env_state.get('domain')}/{env_state.get('task')}"
    else:
        state_log = ",".join(str(v) for v in env_state.values())
    stage = "full" if loader.is_full_version(ver) else f"v{ver}"
    logger.info(
        "session.create %s  %s/%s/%s/%s/%s task=%s resource=%s state=%s",
        sess.session_id, body.dataset, body.benchmark, body.domain,
        stage, body.split, row.task_id, res_mode, state_log,
    )
    return _session_view(sess)


def _require_session(session_id: str) -> Session:
    sess = SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no such session {session_id!r}")
    if sess.status in ("closed", "expired"):
        raise HTTPException(status_code=410, detail=f"session is {sess.status}")
    return sess


@app.get("/v1/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    sess = SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no such session {session_id!r}")
    return _session_view(sess)


# --------------------------------------------------------------------------- #
# EOG act surface: reverse-proxy MCP to the gym (bound to this session's DB)   #
# --------------------------------------------------------------------------- #
@app.api_route(
    "/v1/sessions/{session_id}/mcp/{gym_name}",
    methods=["GET", "POST", "DELETE", "OPTIONS"],
)
async def mcp_proxy(session_id: str, gym_name: str, request: Request):
    """Forward an MCP streamable-HTTP request to the session's gym.

    This is what lets a BYOA agent *act* over the public tunnel: the gym's real
    endpoint is a host-local Docker-bridge address (unreachable from a remote
    client), so we relay here and inject the session's DB-binding headers
    (``x-database-id`` etc.) so the agent hits the exact DB the verifier reads.
    """
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        })

    sess = _require_session(session_id)
    targets = sess.mcp_targets or {}
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="this session has no MCP act surface (not an EOG session)",
        )
    target = targets.get(gym_name)
    if target is None and len(targets) == 1:
        target = next(iter(targets.values()))     # single-gym convenience
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"no MCP server {gym_name!r} in this session; have {sorted(targets)}",
        )

    body = await request.body()
    up_headers = {k: v for k, v in request.headers.items()
                  if k.lower() in _MCP_REQ_HEADERS}
    up_headers.update(target.get("headers") or {})  # session/DB binding wins

    client = _get_mcp_client()
    upstream = client.build_request(
        request.method, target["url"], headers=up_headers, content=body,
    )
    try:
        resp = await client.send(upstream, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"gym MCP endpoint unreachable from the service: {e}",
        )

    # content-type rides `media_type`; content-encoding/length are dropped since
    # aiter_bytes() returns decoded bytes over a chunked response.
    out_headers = {k: v for k, v in resp.headers.items()
                   if k.lower() in _MCP_RESP_HEADERS and k.lower() != "content-type"}
    out_headers["Access-Control-Allow-Origin"] = "*"

    async def _relay():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        _relay(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type"),
    )


@app.post("/v1/sessions/{session_id}/grade")
async def grade_session(
    session_id: str, keep_alive: bool = Query(False)
) -> dict[str, Any]:
    sess = _require_session(session_id)
    # An ALE Codex run grades inline (ale_run's scorer runs in the same sandbox
    # invocation); return that cached score instead of re-running the harness.
    if sess.env_state.get("_inline_graded") and sess.last_grade:
        cached = dict(sess.last_grade)
        cached["session_id"] = session_id
        sess.status = "graded"
        if not keep_alive:
            await _teardown(sess, mark="closed")
        return cached
    env = get_environment(sess.benchmark)
    try:
        result = await asyncio.to_thread(env.grade, sess.row, sess.env_state)
    except GradingNotSupported as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("grade failed for %s", session_id)
        raise HTTPException(status_code=502, detail=f"grading failed: {e}")

    result.session_id = session_id
    sess.last_grade = asdict(result)
    sess.status = "graded"
    if not keep_alive:
        await _teardown(sess, mark="closed")
    return asdict(result)


# --------------------------------------------------------------------------- #
# Server-side agent execution: run the *reference* agent for the session's     #
# track on the host, so callers need no local gym / no `codex` binary and just #
# pass their own OpenAI key. Dispatch by dataset:                              #
#   evovling_tools  -> EnterpriseOps-Gym reference ReAct agent                 #
#   evovling_skills -> Codex over ACP (harness default runtime)                #
#   evovling_agents -> Codex multi-agent (orchestrator + subagents)            #
# --------------------------------------------------------------------------- #
_AUTO_AGENT = {
    "evovling_tools": "react",
    "evovling_skills": "codex",
    "evovling_agents": "codex_agents",
}


def _resolve_agent_kind(sess: Session, requested: str | None) -> str:
    """Map an optional ``agent`` override + the session dataset to a runner.

    ALE only supports the CLI (Codex/ACP) harness, so ``react`` on an ALE task
    is a hard 400 -- the caller must use ``acp_codex_agent``.
    """
    kind = (requested or "auto").strip().lower()
    is_ale = sess.benchmark == "ale"
    if kind in ("", "auto", "default", "reference"):
        # ALE auto-selects Codex (multi-agent for the agents track); EOG maps by
        # dataset (tools->react, skills->codex, agents->codex_agents).
        if is_ale:
            kind = "codex_agents" if sess.dataset == "evovling_agents" else "codex"
        else:
            kind = _AUTO_AGENT.get(sess.dataset, "codex")
    if kind in ("codex-agents", "codex_agents", "agents", "multi-agent", "multi_agent"):
        return "codex_agents"
    if kind in ("codex", "acp", "acp_codex", "codex_acp"):
        # The agents track's reference Codex IS the multi-agent orchestrator.
        return "codex_agents" if sess.dataset == "evovling_agents" else "codex"
    if kind in ("react", "reference-react"):
        if is_ale:
            raise HTTPException(
                status_code=400,
                detail=("ALE requires a CLI agent harness -- use acp_codex_agent "
                        "(react is EOG-only)."))
        return "react"
    raise HTTPException(status_code=400, detail=f"unknown agent={requested!r} (react|codex|auto)")


def _clip_for_trace(v: Any, limit: int = 2000) -> Any:
    """Stringify + clip a value so a trace stays a sane size on the wire."""
    if v is None:
        return None
    import json as _json
    s = v if isinstance(v, str) else _json.dumps(v, default=str)
    return s if len(s) <= limit else s[:limit] + f"... (+{len(s) - limit} chars)"


def _read_codex_trace(
    trial_dir: Path, *, max_events: int = 400, max_text: int = 2000
) -> list[dict[str, Any]] | None:
    """Compact trace from the harness's ``acp_session.json`` event log.

    Surfaces tool calls (+results), agent messages/thoughts, plans and turn
    boundaries; long text/results are truncated. Returns None when the log is
    absent (best-effort; a run without a persisted session still summarizes).
    """
    import json as _json
    try:
        data = _json.loads((trial_dir / "acp_session.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - trace is advisory
        return None
    events = data.get("events")
    if not isinstance(events, list):
        return None
    trace: list[dict[str, Any]] = []
    for ev in events[:max_events]:
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t in ("agent_message", "agent_thought", "user_message"):
            trace.append({"type": t, "text": str(ev.get("text") or "")[:max_text]})
        elif t == "tool_call":
            trace.append({"type": "tool_call", "title": ev.get("title"),
                          "kind": ev.get("kind"), "status": ev.get("status")})
        elif t == "tool_call_update":
            trace.append({"type": "tool_call_update", "status": ev.get("status"),
                          "content": _clip_for_trace(ev.get("content"), max_text)})
        elif t == "plan":
            trace.append({"type": "plan", "entries": ev.get("entries")})
        elif t == "prompt_done":
            trace.append({"type": "prompt_done", "stop_reason": ev.get("stop_reason")})
    return trace


def _summarize_codex_trial(
    session_id: str, result: Any, work: Path, *, include_trace: bool = False
) -> dict[str, Any]:
    """Codex ``TrialResult`` + its ``trial_meta.json`` -> uniform run summary."""
    import json as _json
    import shutil as _shutil
    meta: dict[str, Any] = {}
    try:
        meta = _json.loads((result.trial_dir / "trial_meta.json").read_text())
    except Exception:  # noqa: BLE001 - meta is best-effort
        pass
    completed = bool(meta.get("completion_signaled"))
    exit_code = int(meta.get("exit_code", result.exit_code) or 0)
    n_episodes = int(meta.get("n_episodes", 0) or 0)
    n_calls = int(meta.get("n_mcp_tool_calls_stdout", 0) or 0)
    n_exec = int(meta.get("n_exec", meta.get("n_bash_tool_calls", 0)) or 0)
    final_message = result.final_message

    def _oi(v: Any) -> int | None:
        return int(v) if isinstance(v, (int, float)) else None

    duration_s = float(meta.get("duration_s", result.duration_s) or 0.0)
    # Token totals are written into trial_meta by the harness runner (best-effort,
    # parsed from the Codex rollout); absent -> None (not measured), not 0.
    total_tokens = _oi(meta.get("total_tokens"))
    input_tokens = _oi(meta.get("input_tokens"))
    output_tokens = _oi(meta.get("output_tokens"))
    # The harness persists the ACP event log next to the trial; read it (before
    # we delete ``work``) so an include_trace run can surface tool/agent calls.
    trace = _read_codex_trace(result.trial_dir) if include_trace else None
    _shutil.rmtree(work, ignore_errors=True)
    if completed:
        stopped = "done"
    elif exit_code not in (0,):
        stopped = "error"
    else:
        stopped = "max_episodes"
    summary = {
        "session_id": session_id,
        "agent": str(meta.get("runtime") or "codex"),
        "n_calls": n_calls,
        "n_exec": n_exec,
        "episodes": n_episodes,
        "steps": n_episodes,
        "completed": completed,
        "stopped": stopped,
        "exit_code": exit_code,
        "thread_id": meta.get("thread_id"),
        "duration_s": duration_s,
        "latency_s": duration_s,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        # Multi-agent track only: how many sub-agents the orchestrator spawned
        # (the "agent calls" surfaced in observability); None for single-agent.
        "n_subagent_spawns": _oi(meta.get("n_subagent_spawns_seen")),
        "final_message": final_message,
    }
    if trace is not None:
        summary["trace"] = trace
    return summary


async def _svc_run_react(sess: Session, body: RunAgentBody, session_id: str) -> dict[str, Any]:
    """EOG tools track: run EnterpriseOps-Gym's reference ReAct agent."""
    try:
        from . import eog_react
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=501, detail=f"reference React unavailable: {e}")
    try:
        summary = await eog_react.run_react_session(
            sess.row, sess.action,
            openai_api_key=body.openai_api_key,
            model=body.model,
            restrict_to_selected_tools=bool(body.restrict_to_selected_tools),
            max_iterations=body.max_iterations,
            timeout_s=float(body.timeout_s) if body.timeout_s else 1800.0,
            include_trace=bool(body.include_trace),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("run_agent(react) failed for %s", session_id)
        raise HTTPException(status_code=502, detail=f"reference React run failed: {e}")
    summary["session_id"] = session_id
    return summary


async def _svc_run_codex(sess: Session, body: RunAgentBody, session_id: str) -> dict[str, Any]:
    """EOG skills track: run Codex via the harness's default runtime (ACP)."""
    try:
        from evovle_skills.src.agent_runner import get_default_agent_runner
        from evovle_skills.src.config import CODEX_TIMEOUT_SEC, CODEX_TRANSPORT
    except Exception as e:  # noqa: BLE001 - harness Codex runner not importable
        raise HTTPException(status_code=501, detail=f"server-side Codex unavailable: {e}")

    from ._harness.endpoints import patch_row

    import shutil as _shutil
    import tempfile as _tempfile

    patched = patch_row(sess.row)                  # reachable local gym URLs
    work = Path(_tempfile.mkdtemp(prefix=f"codex_{session_id[:8]}_"))
    skills_dir = work / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Mount the resolved skill bundle(s) for oracle/accumulative modes; 'none'
    # (the realistic evolving-skills setting) leaves it empty -> gym-only run.
    try:
        n = resources.materialize_skills(
            benchmark=sess.benchmark, domain=sess.domain, version=sess.version,
            row=sess.row, mode=sess.resource_mode, dest_dir=skills_dir,
        )
        if n:
            logger.info("run_agent(codex) %s: mounted %d skill(s) mode=%s",
                        session_id, n, sess.resource_mode)
    except Exception as e:  # noqa: BLE001 - skills are advisory; never fail the run
        logger.warning("skills materialize failed for %s: %s", session_id, e)

    runner = get_default_agent_runner()
    try:
        result = await runner.run_trial(
            patched,
            trial_dir=work / "trial",
            shared_skills_dir=skills_dir,
            mode="service",
            database_ids=sess.env_state,
            timeout_s=int(body.timeout_s or CODEX_TIMEOUT_SEC),
            transport=(body.transport or CODEX_TRANSPORT),
            restrict_to_selected_tools=bool(body.restrict_to_selected_tools),
            openai_api_key=body.openai_api_key,
            model=body.model,
            max_episodes=body.max_episodes,
            mcp_only=body.mcp_only,
        )
    except Exception as e:  # noqa: BLE001
        _shutil.rmtree(work, ignore_errors=True)
        logger.exception("run_agent(codex) failed for %s", session_id)
        raise HTTPException(status_code=502, detail=f"server-side Codex failed: {e}")
    return _summarize_codex_trial(
        session_id, result, work, include_trace=bool(body.include_trace))


async def _svc_run_codex_agents(sess: Session, body: RunAgentBody, session_id: str) -> dict[str, Any]:
    """EOG agents track: run the Codex multi-agent orchestrator + subagents."""
    try:
        from evovle_agents.src.agent_library import agent_specs_for
        from evovle_agents.src.codex_agents_runner import run_codex_agents_trial
        from evovle_agents.src.config import (
            assert_memory_supported,
            service_memory_home,
        )
        from evovle_agents.src.modes import AgentMode
        from evovle_skills.src.config import CODEX_TIMEOUT_SEC, CODEX_TRANSPORT
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=501, detail=f"server-side agents unavailable: {e}")

    from ._harness.endpoints import patch_row

    import shutil as _shutil
    import tempfile as _tempfile

    patched = patch_row(sess.row)
    # Build the specialist pool for this task from the (host-local) capability
    # library: oracle -> the task's gold specialists, accumulative -> the pool
    # accumulated through the stage, none -> single-agent baseline (no subagents).
    amode = {"oracle": AgentMode.ORACLE_AGENTS,
             "accumulative": AgentMode.CUMULATIVE_AGENTS}.get(sess.resource_mode)
    specs = None
    if amode is not None:
        if loader.is_full_version(sess.version):
            vers = loader.versions("evovling_agents", sess.benchmark, sess.domain)
            ver = max(vers) if vers else 1
        else:
            ver = int(sess.version)
        try:
            specs = await asyncio.to_thread(
                agent_specs_for, patched.domain, ver, amode)
        except Exception as e:  # noqa: BLE001
            logger.warning("agents pool build failed for %s: %s", session_id, e)
            specs = None

    # Codex keeps its memory inside CODEX_HOME, and ``work`` is deleted once this
    # trial is graded -- so a memory run gets a home that lives outside it, keyed
    # by the client's memory_key and shared with every other trial of that
    # experiment.
    memory_home = None
    if body.memory_key:
        assert_memory_supported()
        memory_home = service_memory_home(body.memory_key)
        logger.info("run_agent(codex_agents) %s: codex memories on, home=%s%s",
                    session_id, memory_home,
                    " (root agent only)" if body.memory_main_only else "")

    work = Path(_tempfile.mkdtemp(prefix=f"agents_{session_id[:8]}_"))
    try:
        result = await run_codex_agents_trial(
            patched,
            trial_dir=work / "trial",
            agent_specs=specs,
            mode="service",
            database_ids=sess.env_state,
            timeout_s=int(body.timeout_s or CODEX_TIMEOUT_SEC),
            transport=(body.transport or CODEX_TRANSPORT),
            restrict_to_selected_tools=bool(body.restrict_to_selected_tools),
            openai_api_key=body.openai_api_key,
            model=body.model,
            memory_home=memory_home,
            memory_main_only=bool(body.memory_main_only),
        )
    except Exception as e:  # noqa: BLE001
        _shutil.rmtree(work, ignore_errors=True)
        logger.exception("run_agent(codex_agents) failed for %s", session_id)
        raise HTTPException(status_code=502, detail=f"server-side agents run failed: {e}")
    summary = _summarize_codex_trial(
        session_id, result, work, include_trace=bool(body.include_trace))
    summary["agent"] = "codex_agents"
    return summary


async def _svc_run_codex_ale(sess: Session, body: RunAgentBody, session_id: str) -> dict[str, Any]:
    """ALE track: solve + grade the task via ale_run + the stock Codex agent.

    ale_run runs the agent *and* the task's scorer in one sandbox invocation, so
    the ``[0,1]`` score is produced inline. We cache it on the session (as a
    ``GradeResult``) so a subsequent ``POST /grade`` returns it without re-running.
    """
    st = sess.env_state
    domain, task_name = st.get("domain", ""), st.get("task", "")
    if not ale_codex_runner.can_run(domain, task_name):
        raise HTTPException(
            status_code=501,
            detail=(
                f"ALE Codex runs need the local ALE Docker sandbox + venv; "
                f"{domain}/{task_name} is not runnable here (see /v1/health ale_docker)."),
        )
    task_obj = ale_grader.AleTask(
        domain=domain, task=task_name, variant=st.get("variant", "base"),
        session_root=Path(st["session_root"]), base_dir=Path(st["base_dir"]),
        input_dir=Path(st["input_dir"]), output_dir=Path(st["output_dir"]),
    )
    multi = (sess.dataset == "evovling_agents")
    # ale_run appends this to the task prompt. codex_agents.yaml makes the root
    # agent tool-less, so the delegation protocol + specialist roster in this block
    # is what makes a multi-agent run able to do anything at all. The caller should
    # send the roster for the mode it asked for; the row's baked system_prompt is
    # the cumulative-pool one, which is right for accumulative but names
    # distractors under oracle.
    suffix = body.prompt_suffix
    if suffix is None and multi:
        suffix = getattr(sess.row, "system_prompt", "") or ""
        logger.info("ale-codex %s: no prompt_suffix sent; falling back to the row's "
                    "system_prompt (%d chars)", sess.row.task_id, len(suffix))
    sandbox_env = ale_codex_runner.filter_sandbox_env(body.sandbox_env)
    if multi and not sandbox_env:
        logger.warning("ale-codex %s: multi-agent run with no ALE_AGENTS_* pool bundle; "
                       "the orchestrator will have no specialists to spawn",
                       sess.row.task_id)
    try:
        res = await asyncio.to_thread(
            ale_codex_runner.run_codex_ale,
            task_obj, model=body.model, openai_api_key=body.openai_api_key,
            multi_agent=multi, timeout_s=(float(body.timeout_s) if body.timeout_s else None),
            prompt_suffix=(suffix or ""), sandbox_env=sandbox_env,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("run_agent(codex_ale) failed for %s", session_id)
        raise HTTPException(status_code=502, detail=f"ALE Codex run failed: {e}")
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=f"ALE Codex run failed: {res.get('reason')}")

    score = float(res["score"])
    passed = score >= ale_grader.SUCCESS_THRESHOLD
    # Cache the inline grade so POST /grade returns it without re-running ale_run.
    grade = GradeResult(
        session_id=session_id, task_id=sess.row.task_id, overall_success=passed,
        pass_rate=score, n_passed=1 if passed else 0, n_total=1,
        per_verifier=[VerifierView(
            name="ale_score", passed=passed,
            expected=f">= {ale_grader.SUCCESS_THRESHOLD}", actual=round(score, 6),
            comparison_type="ale_score:codex_inline", error=None)],
    )
    sess.last_grade = asdict(grade)
    sess.env_state["_inline_graded"] = True
    # Keep the raw ale_run tree reachable for GET .../run_artifacts. It is inside the
    # session workspace, so it only survives until teardown -- clients that want the
    # trajectory / events / produced output must fetch it before closing the session.
    if res.get("work_dir"):
        sess.env_state["_ale_run_dir"] = str(res["work_dir"])
    # The Codex rollout itself stays inside the sandbox, but ale_run aggregates
    # that run's token/cost usage into run.json on this host (and gathers the
    # stager's spawn tally), so we report the real figures. Anything ale_run did
    # not record stays None -- "not measured", never a misleading zero.
    usage = res.get("usage") or {}
    n_steps = usage.get("n_steps")
    return {
        "session_id": session_id,
        "agent": "codex_agents" if multi else "codex",
        "benchmark": "ale",
        "n_calls": 0,
        "n_exec": 0,
        "episodes": 1,
        "steps": n_steps if isinstance(n_steps, int) else 1,
        "completed": True,
        "stopped": "done",
        "exit_code": 0,
        "latency_s": res.get("latency_s"),
        "total_tokens": usage.get("total_tokens"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        # ALE-only extras: ale_run separates cache reads and prices the trial.
        "cache_read_tokens": usage.get("cache_read_tokens"),
        "cost_usd": usage.get("cost_usd"),
        "n_steps": n_steps,
        "n_subagent_spawns": res.get("n_subagent_spawns"),
        "pass_rate": score,
        "final_message": "",
    }


async def _run_agent_session(session_id: str, body: RunAgentBody) -> dict[str, Any]:
    """Dispatch a server-side reference-agent run for this session.

    EOG runs the reference agent for the session's track (ReAct for tools, Codex
    over ACP for skills, Codex multi-agent for agents). ALE runs the Codex CLI
    agent inside the sandbox via ale_run (``react`` is rejected for ALE).
    """
    sess = _require_session(session_id)
    kind = _resolve_agent_kind(sess, body.agent)  # 400s on react-for-ALE
    logger.info("run_agent %s dataset=%s benchmark=%s agent=%s",
                session_id, sess.dataset, sess.benchmark, kind)
    _t0 = time.monotonic()
    if sess.benchmark == "ale":
        summary = await _svc_run_codex_ale(sess, body, session_id)
    elif kind == "react":
        summary = await _svc_run_react(sess, body, session_id)
    elif kind == "codex_agents":
        summary = await _svc_run_codex_agents(sess, body, session_id)
    else:
        summary = await _svc_run_codex(sess, body, session_id)
    # Uniform wall-clock fallback so ``latency_s`` is always present even if a
    # runner didn't measure its own (a runner's own measurement wins when set).
    summary.setdefault("latency_s", round(time.monotonic() - _t0, 3))
    return summary


# Strong refs to in-flight background jobs: ``asyncio.create_task`` only keeps a
# weak reference, so without this a job could be GC'd mid-run.
_BG_TASKS: set[asyncio.Task] = set()


async def _run_job(job: Job, body: RunAgentBody) -> None:
    """Execute a backgrounded agent run, storing its result/error on the job."""
    job.status = "running"
    job.updated_at = time.time()
    try:
        job.result = await _run_agent_session(job.session_id, body)
        job.status = "done"
    except HTTPException as e:
        job.error = {"status_code": int(e.status_code), "detail": str(e.detail)}
        job.status = "error"
    except Exception as e:  # noqa: BLE001 - a job must never crash the loop silently
        logger.exception("background run_agent job %s failed", job.job_id)
        job.error = {"status_code": 502, "detail": f"{type(e).__name__}: {e}"}
        job.status = "error"
    finally:
        job.updated_at = time.time()


async def _submit_or_run(
    session_id: str, body: RunAgentBody, response: Response
) -> dict[str, Any]:
    """Run synchronously, or -- when ``body.background`` -- spawn a job + return its id.

    Background mode keeps every HTTP request short: the caller polls the cheap
    ``GET .../jobs/{job_id}`` until the run finishes, so a long ALE/Codex run is
    never held open in one request past an upstream proxy/tunnel timeout.
    """
    sess = _require_session(session_id)          # 404/410 before accepting a job
    if not body.background:
        return await _run_agent_session(session_id, body)
    # Surface obvious rejections (e.g. react-on-ALE -> 400) on the submit call
    # itself rather than burying them in a polled job error.
    _resolve_agent_kind(sess, body.agent)
    job = Job(job_id="job_" + uuid.uuid4().hex[:16], session_id=session_id,
              created_at=time.time(), updated_at=time.time())
    JOBS[job.job_id] = job
    task = asyncio.create_task(_run_job(job, body))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    response.status_code = 202
    return {"job_id": job.job_id, "session_id": session_id, "status": job.status}


@app.post("/v1/sessions/{session_id}/run_agent")
async def run_agent_session(
    session_id: str, body: RunAgentBody, response: Response
) -> dict[str, Any]:
    """Run the reference agent for this session's track; return the run summary.

    The service runs the agent on the host (reference ReAct for tools, Codex via
    ACP for skills, Codex multi-agent for agents), pointed at this session's
    already-seeded gym + DB binding, so behavior matches a local harness run.
    With ``background=true`` this returns a ``{job_id}`` to poll instead (see
    ``GET .../jobs/{job_id}``). Grade afterwards with ``POST .../grade``.
    """
    return await _submit_or_run(session_id, body, response)


@app.post("/v1/sessions/{session_id}/run_codex")
async def run_codex_session(
    session_id: str, body: RunAgentBody, response: Response
) -> dict[str, Any]:
    """Back-compat alias for ``POST .../run_agent`` (dispatches by dataset)."""
    return await _submit_or_run(session_id, body, response)


class ConsolidateMemoryBody(BaseModel):
    memory_key: str = Field(
        ..., description="The persistent CODEX_HOME to consolidate, as passed to "
                         "run_agent's memory_key.")
    label: str = Field(
        "manual", description="Names this pass in the home's _consolidate/ log; "
                              "the agents track uses the curriculum version, e.g. v2.")
    model: str | None = Field(None, description="Model for the consolidation pass.")
    api_key: str | None = Field(
        None, description="OpenAI key for the pass; falls back to the service's.")
    root_only: bool = Field(
        False, description="Build the memory from the orchestrator's own rollouts "
                           "only, leaving out the specialists it delegated to. The "
                           "write half of run_agent's memory_main_only; pass both "
                           "together or the memory and its reader disagree.")


@app.post("/v1/memory/consolidate")
async def consolidate_memory(body: ConsolidateMemoryBody) -> dict[str, Any]:
    """Have Codex distil the rollouts in a keyed memory home into its memory file.

    Session-less on purpose: this touches only ``CODEX_HOME``, so provisioning a
    gym would be pure overhead.  The agents track calls this at each curriculum
    boundary -- after a version's train split, before its test split.

    Codex writes the memory.  What this supplies is the stage it is missing: under
    ``codex exec`` its phase 1 never runs, so its consolidation wakes with nothing
    to merge.  The rollouts are extracted here and handed to that consolidation,
    which then writes ``MEMORY.md`` and ``memory_summary.md`` with its own prompt.
    """
    try:
        from evovle_agents.src.codex_agents_runner import consolidate_memory_home
        from evovle_agents.src.config import (
            assert_memory_supported,
            service_memory_home,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=501,
                            detail=f"server-side agents unavailable: {e}")
    assert_memory_supported()
    home = service_memory_home(body.memory_key)
    if not home.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"no memory home for key {body.memory_key!r}; run its train "
                   "split with memory_key first")
    return await consolidate_memory_home(
        home, label=body.label, model=body.model,
        openai_api_key=body.api_key, root_only=bool(body.root_only))


@app.get("/v1/sessions/{session_id}/jobs/{job_id}")
async def get_job(session_id: str, job_id: str) -> dict[str, Any]:
    """Poll a backgrounded agent run (submitted with ``background=true``).

    ``status`` is ``pending`` | ``running`` | ``done`` | ``error``. On ``done``
    the full run summary is under ``result``; on ``error`` the failing
    ``{status_code, detail}`` (what the synchronous call would have raised) is
    under ``error``.
    """
    job = JOBS.get(job_id)
    if job is None or job.session_id != session_id:
        raise HTTPException(status_code=404, detail=f"no such job {job_id!r}")
    out: dict[str, Any] = {
        "job_id": job.job_id,
        "session_id": job.session_id,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    if job.status == "done":
        out["result"] = job.result or {}
    elif job.status == "error":
        out["error"] = job.error or {"status_code": 502, "detail": "unknown error"}
    return out


# --------------------------------------------------------------------------- #
# ALE sandbox: serve inputs + accept the agent's submitted artifact           #
# --------------------------------------------------------------------------- #
def _require_ale(sess: Session) -> dict[str, Any]:
    if sess.env_state.get("kind") != "ale":
        raise HTTPException(
            status_code=400,
            detail="not a sandbox (ALE) session; inputs/submit apply to benchmark=ale only",
        )
    return sess.env_state


@app.get("/v1/sessions/{session_id}/inputs")
def list_inputs(session_id: str) -> dict[str, Any]:
    sess = _require_session(session_id)
    st = _require_ale(sess)
    from pathlib import Path
    return {
        "session_id": session_id,
        "workdir": Path(st["base_dir"]).name,
        "input_files": ale_grader.list_inputs(Path(st["base_dir"])),
    }


@app.get("/v1/sessions/{session_id}/inputs/{rel_path:path}")
def get_input(session_id: str, rel_path: str):
    sess = _require_session(session_id)
    st = _require_ale(sess)
    from pathlib import Path
    try:
        fp = ale_grader.resolve_input_file(Path(st["base_dir"]), rel_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"no such input: {rel_path!r}")
    except ale_grader.AleUnavailable as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(str(fp))


@app.get("/v1/sessions/{session_id}/run_artifacts")
def get_run_artifacts(session_id: str):
    """The raw ale_run tree for this session's ALE run, as a tar.gz.

    Gives a hosted run the same on-disk evidence a local one leaves behind: the
    unit's ``run.json`` / ``eval_result.json`` / ``trajectory.json`` /
    ``events.jsonl`` / ``origin_log/`` / produced ``output/``, plus the generated
    experiment files. Members are already arcnamed ``_ale_runs/g0/...`` and
    ``_ale_experiments/...``, so extracting at a cell root reproduces the local
    layout.

    Only valid between the agent run and session teardown (the tree lives in the
    session workspace); 404 before a run, or once the workspace is gone.
    """
    sess = _require_session(session_id)
    d = (sess.env_state or {}).get("_ale_run_dir")
    if not d or not Path(d).is_dir():
        raise HTTPException(
            status_code=404,
            detail=("no ALE run artifacts for this session: run the agent first, and "
                    "fetch them before the session is torn down"))
    import tempfile
    dest = Path(tempfile.mkdtemp(prefix="ale_artifacts_")) / "run_artifacts.tar.gz"
    try:
        ale_codex_runner.build_run_artifacts_tar(Path(d), dest)
    except Exception as e:  # noqa: BLE001
        logger.exception("packing ALE artifacts failed for %s", session_id)
        raise HTTPException(status_code=500, detail=f"packing artifacts failed: {e}")
    return FileResponse(str(dest), media_type="application/gzip",
                        filename="run_artifacts.tar.gz")


@app.post("/v1/sessions/{session_id}/submit")
def submit_artifact(session_id: str, body: SubmitBody) -> dict[str, Any]:
    sess = _require_session(session_id)
    st = _require_ale(sess)
    from pathlib import Path
    try:
        written = ale_grader.write_submission(
            Path(st["output_dir"]),
            [f.model_dump() for f in body.files],
        )
    except ale_grader.AleUnavailable as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"session_id": session_id, "written": written}


@app.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    sess = SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"no such session {session_id!r}")
    if sess.status in ("active", "graded"):
        await _teardown(sess, mark="closed")
    return {"session_id": session_id, "status": sess.status}
