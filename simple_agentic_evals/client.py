"""HTTP client for the evaluation service."""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx

#: Client-side logger. Silent by default; enable to see each call + its latency:
#:     logging.getLogger("simple_agentic_evals").setLevel(logging.DEBUG)
#: (the service records all usage centrally too -- see GET /v1/usage.)
_log = logging.getLogger("simple_agentic_evals")


def _user_agent() -> str:
    """``User-Agent`` the client sends so the service can attribute SDK traffic.

    Resolved lazily (the package ``__version__`` is defined after this module is
    imported), so this is only called at client construction time.
    """
    try:
        from . import __version__ as version
    except Exception:  # noqa: BLE001 - hand-built installs may lack it
        version = "0"
    return f"simple_agentic_evals/{version}"


class MissingAPIKey(ValueError):
    """No OpenAI key for a harness the service runs for you.

    Its own type because it is a setup mistake, not a task the agent failed:
    ``run_benchmark`` scores a failed task 0.0 and keeps going, which would turn
    a forgotten key into a clean-looking ACC of 0.0 across the whole sweep.
    """


def resolve_openai_key(api_key: str | None) -> str:
    """Your OpenAI key for a harness the service runs on your behalf.

    Two different keys are in play. The eval service key (MyAuthtoken) gets you
    into the service; this one pays for the model. The service hosts the
    environment, the harness and the grader but never lends its own credentials,
    so an explicit ``api_key`` wins and ``$OPENAI_API_KEY`` is the fallback.
    Raising here rather than letting the service answer 400 keeps the message
    next to the call that is missing the key.

    Bring-your-own-agent needs none of this: you call your model yourself and
    use the service only for the environment and grading.
    """
    key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise MissingAPIKey(
            "no OpenAI API key: pass api_key=... or set $OPENAI_API_KEY. The "
            "service runs this harness for you but does not pay for the "
            "inference, so it will not fall back to its own key. Get one at "
            "https://platform.openai.com/api-keys (this is NOT your eval "
            "service key from MyAuthtoken -- that one authenticates you to the "
            "service and is already working if you got this far)."
        )
    return key


class ServiceError(RuntimeError):
    """A non-2xx response from the evaluation service.

    Exposes ``status_code`` and ``detail`` so callers never need to import or
    catch ``httpx`` themselves. ``needs_sandbox`` flags the common ALE case
    (HTTP 501: the task's grader must run in a full sandbox not available here).
    """

    def __init__(self, status_code: int, detail: str = "", *, url: str = "",
                 body: dict | None = None):
        self.status_code = int(status_code)
        self.detail = detail or ""
        self.url = url
        self.body = body or {}
        #: Where to get a key, when the service says the request was unauthorized.
        self.signup_url = str(self.body.get("signup_url") or "")
        loc = f" from {url}" if url else ""
        super().__init__(f"HTTP {self.status_code}{loc}: {self.detail}".rstrip(": "))

    @property
    def needs_sandbox(self) -> bool:
        return self.status_code == 501

    @property
    def needs_api_key(self) -> bool:
        """True when this failed for lack of a valid key rather than anything else.

        Lets a caller react to a credential problem specifically -- prompt for a
        key and retry -- instead of pattern-matching the message.
        """
        return self.status_code in (401, 403)


def _raise_for_status(r: httpx.Response) -> None:
    """Raise :class:`ServiceError` on a non-2xx response (keeps httpx internal)."""
    if r.is_success:
        return
    detail, body = "", None
    try:
        parsed = r.json()
        if isinstance(parsed, dict):
            body, detail = parsed, parsed.get("detail", "")
        else:
            detail = str(parsed)
    except Exception:  # noqa: BLE001 - fall back to raw text
        detail = (r.text or "")[:500]
    if not detail and r.status_code in (401, 403):
        # A gate that rejects without saying why (or a proxy that swallowed the
        # body) would otherwise surface as a bare "HTTP 401".
        detail = ("missing or invalid API key for the evaluation service; set "
                  "$EVAL_SERVICE_API_KEY or pass EvalClient(api_key=...)")
    raise ServiceError(r.status_code, detail, url=str(r.request.url), body=body)


@dataclass
class McpServer:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "streamable_http"
    # Relative proxy path on the service (``/v1/sessions/{id}/mcp/{name}``); the
    # service forwards these calls to the live gym, bound to this session's DB.
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "McpServer":
        return cls(
            name=d.get("name", ""),
            url=d.get("url", ""),
            headers=dict(d.get("headers") or {}),
            transport=d.get("transport", "streamable_http"),
            path=d.get("path", ""),
        )


@dataclass
class GradeResult:
    task_id: str
    pass_rate: float
    overall_success: bool
    n_passed: int
    n_total: int
    per_verifier: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GradeResult":
        return cls(
            task_id=d.get("task_id", ""),
            pass_rate=float(d.get("pass_rate", 0.0) or 0.0),
            overall_success=bool(d.get("overall_success", False)),
            n_passed=int(d.get("n_passed", 0) or 0),
            n_total=int(d.get("n_total", 0) or 0),
            per_verifier=list(d.get("per_verifier") or []),
            raw=d,
        )


@dataclass
class EvalReport:
    """Accuracy + latency + tokens for one task, together (pick what you need).

    Produced by :meth:`Task.evaluate`: it runs a provided harness then grades, so
    ``accuracy`` (== ``grade.pass_rate``) sits next to the run's ``latency_s`` and
    ``total_tokens``. The raw ``run`` (AgentRun/CodexRun) and ``grade`` are kept
    for anything else you want.
    """
    task_id: str
    agent: str                      # "react" | "acp_codex"
    accuracy: float                 # == grade.pass_rate
    overall_success: bool
    latency_s: float | None = None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # ALE-only: ale_run separates cache reads and prices each trial (the EOG
    # rollout tally cannot), so these stay None on EOG runs.
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    n_steps: int | None = None
    n_subagent_spawns: int | None = None
    grade: "GradeResult | None" = None
    run: Any = field(default=None, repr=False)
    # ALE-only: tar.gz of the server-side ale_run tree, present when
    # ``evaluate(fetch_artifacts=True)`` captured it. None otherwise.
    artifacts_tgz: bytes | None = field(default=None, repr=False)


class Task:
    """A handle to one benchmark task.

    Use as a context manager: ``__enter__`` provisions a session (seeds the
    environment) and ``__exit__`` tears it down (frees the gym DB). ``grade()``
    runs the verifiers against the current environment state.
    """

    def __init__(self, client: "EvalClient", selector: dict[str, Any], task_id: str):
        self._client = client
        self._selector = dict(selector)
        self.task_id = task_id
        # Populated after start():
        self.session_id: str | None = None
        self.system_prompt: str = ""
        self.user_prompt: str = ""
        self.required_steps: list[str] = []
        self.evaluation: str = ""
        self.oracle_tools: list[str] = []
        # Evolving tools/skills/agents attached when tasks(resource_mode=...) is set:
        # {"kind","mode","count","names",[...]}. Empty unless a mode was requested.
        self.resources: dict[str, Any] = {}
        self.action: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> "Task":
        if self.session_id is not None:
            return self
        body = dict(self._selector)
        body["task_id"] = self.task_id
        sv = self._client._post("/v1/sessions", body)
        self.session_id = sv["session_id"]
        task = sv.get("task", {})
        self.system_prompt = task.get("system_prompt", "")
        self.user_prompt = task.get("user_prompt", "")
        self.required_steps = list(task.get("required_steps") or [])
        self.evaluation = str(task.get("evaluation") or "")
        self.oracle_tools = list(task.get("oracle_tools") or [])
        self.resources = dict(task.get("resources") or {})
        self.action = sv.get("action", {})
        return self

    def close(self) -> None:
        if self.session_id is not None:
            try:
                self._client._delete(f"/v1/sessions/{self.session_id}")
            finally:
                self.session_id = None

    def __enter__(self) -> "Task":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- action surface ----------------------------------------------------- #
    @property
    def benchmark(self) -> str:
        """The benchmark this task belongs to (``eog`` | ``ale``)."""
        return str(self._selector.get("benchmark", "") or "")

    @property
    def dataset(self) -> str:
        """The track this task belongs to (evovling_tools|_skills|_agents)."""
        return str(self._selector.get("dataset", "") or "")

    @property
    def action_type(self) -> str:
        return self.action.get("type", "")

    @property
    def mcp_servers(self) -> list[McpServer]:
        """MCP servers to act on (EOG). Empty for non-mcp actions."""
        return [McpServer.from_dict(s) for s in self.action.get("mcp_servers", [])]

    def mcp_url(self, server: McpServer) -> str:
        """Absolute MCP endpoint for a gym. Prefers joining the client base with
        the service-provided relative ``path`` (always reachable from wherever the
        client can reach the API); falls back to the absolute ``url``."""
        return (self._client.base_url + server.path) if server.path else server.url

    def mcp_session(self, server: McpServer, timeout: float = 60.0) -> "MCPSession":
        """Open a minimal MCP client against a gym ``server``, pre-wired with this
        client's auth/headers (so acting works over the same base URL). Convenience
        only -- you can hand ``server`` to any MCP client instead."""
        from .mcp import MCPSession

        headers = {**self._client.headers, **(server.headers or {})}
        return MCPSession(self.mcp_url(server), headers=headers, timeout=timeout)

    @property
    def sandbox(self) -> dict[str, Any]:
        """Sandbox action payload (ALE). Empty dict for non-sandbox actions."""
        return self.action if self.action_type == "sandbox" else {}

    @property
    def terminal(self) -> dict[str, Any]:
        """Terminal action payload from a local runtime adapter."""
        return self.action if self.action_type == "terminal" else {}

    @property
    def managed_runtime(self) -> dict[str, Any]:
        """Official managed-runtime action payload from a local adapter."""
        return self.action if self.action_type == "managed_runtime" else {}

    @property
    def input_files(self) -> list[dict[str, Any]]:
        """Declared input files for an ALE sandbox task (path/name/size/...)."""
        return list(self.sandbox.get("input_files") or [])

    @property
    def output_path(self) -> str:
        """Deliverable path the prompt asks for, e.g. ``output/results.json`` (ALE)."""
        return self.sandbox.get("output_path", "")

    def _need_session(self) -> None:
        if self.session_id is None:
            raise RuntimeError("call start() (or use `with task:`) first")

    # -- ALE sandbox: fetch inputs + submit the produced artifact ----------- #
    def inputs(self) -> list[dict[str, Any]]:
        """List the input files staged for this session (ALE)."""
        self._need_session()
        d = self._client._get(f"/v1/sessions/{self.session_id}/inputs")
        return d.get("input_files", [])

    def fetch_input(self, rel_path: str) -> bytes:
        """Fetch one staged input file's bytes (ALE)."""
        self._need_session()
        return self._client._get_bytes(
            f"/v1/sessions/{self.session_id}/inputs/{rel_path.lstrip('/')}"
        )

    def fetch_run_artifacts(self) -> bytes:
        """The raw ale_run tree from this session's ALE run, as tar.gz bytes.

        Members are arcnamed ``_ale_runs/g0/...`` / ``_ale_experiments/...``, so
        extracting at a run's cell root reproduces what a local run leaves on disk
        (trajectory, event log, origin_log, produced output).

        Call it after the agent run but BEFORE grading: the tree lives in the
        session workspace, and ``grade()`` (``keep_alive`` false) closes the session
        and deletes it, after which this raises 410. ``evaluate(fetch_artifacts=True)``
        handles that ordering for you. Raises :class:`ServiceError` (404) if there is
        nothing to fetch.
        """
        self._need_session()
        return self._client._get_bytes(
            f"/v1/sessions/{self.session_id}/run_artifacts")

    def submit(self, files: list[dict[str, Any]] | dict[str, Any]) -> list[str]:
        """Submit the agent's output artifact(s) (ALE); returns written relpaths.

        Each file is ``{"path": str, "content": str}`` (text) or
        ``{"path": str, "content_b64": str}`` (binary).
        """
        self._need_session()
        if isinstance(files, dict):
            files = [files]
        d = self._client._post(
            f"/v1/sessions/{self.session_id}/submit", {"files": files}
        )
        return d.get("written", [])

    def submit_text(self, path: str, text: str) -> list[str]:
        """Convenience: submit a single text artifact at ``path`` (ALE)."""
        return self.submit([{"path": path, "content": text}])

    # -- directory in / directory out --------------------------------------- #
    # An existing system -- a CLI, a container, a repo you already run -- is
    # usually "read a directory, write a directory". These two turn that shape
    # into an ALE agent without any per-file glue: stage in, run it however you
    # like, ship whatever it wrote back.
    def fetch_inputs_to(self, dest: str | Path) -> Path:
        """Download every staged input into ``dest``, keeping relative paths (ALE).

        Returns ``dest`` so it can be handed straight to a subprocess or bind-mounted
        into a container. Creates the directory (and parents) if missing.
        """
        root = Path(dest)
        root.mkdir(parents=True, exist_ok=True)
        for f in self.inputs():
            rel = str(f.get("path") or "").lstrip("/")
            if not rel:
                continue
            out = (root / rel).resolve()
            if root.resolve() not in out.parents:   # a "../" path would escape dest
                raise ValueError(f"input path escapes {root}: {rel!r}")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(self.fetch_input(rel))
        return root

    def submit_dir(self, src: str | Path, *, max_bytes: int = 32 * 1024 * 1024) -> list[str]:
        """Submit every file under ``src`` (ALE); returns the written relpaths.

        Paths are submitted relative to ``src``, so pointing it at the directory your
        system treated as the sandbox root reproduces the layout the task asked for.
        Text is sent as text and anything that is not valid UTF-8 as base64, so
        binaries survive. ``max_bytes`` guards against posting a whole build tree by
        accident -- raise it deliberately if a task really needs to.
        """
        root = Path(src)
        if not root.is_dir():
            raise NotADirectoryError(f"{root} is not a directory")
        files, total = [], 0
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            raw = p.read_bytes()
            total += len(raw)
            if total > max_bytes:
                raise ValueError(
                    f"submission exceeds {max_bytes} bytes at {p.relative_to(root)}; "
                    f"submit only the deliverable, or raise max_bytes")
            rel = p.relative_to(root).as_posix()
            try:
                files.append({"path": rel, "content": raw.decode("utf-8")})
            except UnicodeDecodeError:
                files.append({"path": rel,
                              "content_b64": base64.b64encode(raw).decode("ascii")})
        return self.submit(files) if files else []

    # -- grading ------------------------------------------------------------ #
    def grade(self, keep_alive: bool = False) -> GradeResult:
        self._need_session()
        d = self._client._post(
            f"/v1/sessions/{self.session_id}/grade",
            None, params={"keep_alive": str(keep_alive).lower()},
        )
        return GradeResult.from_dict(d)

    # -- run a provided harness + grade, all three metrics at once ---------- #
    def evaluate(
        self, agent: str = "acp_codex", *, keep_alive: bool = False,
        fetch_artifacts: bool = False, **kwargs: Any
    ) -> EvalReport:
        """Run a provided harness on this task, then grade -- one call, all metrics.

        ``agent`` picks the harness: ``"react"`` (EOG's reference ReAct, EOG-only)
        or ``"acp_codex"`` (Codex over ACP; EOG + ALE). Extra keyword args
        (``model``, ``api_key``, ``verbose``, ``include_trace``, ``max_steps`` /
        ``max_episodes``, ...) are forwarded to the harness. Returns an
        :class:`EvalReport` bundling accuracy (from grading), ``latency_s`` and
        ``total_tokens``. On an ALE task, ``agent="react"`` raises the clear
        "ALE requires a CLI agent harness" error.

        ``fetch_artifacts`` (ALE) also returns the raw ale_run tree in
        ``report.artifacts_tgz``. It is captured between the run and grading
        because, with ``keep_alive`` false, grading closes the session and deletes
        the workspace the tree lives in -- fetching afterwards is too late. An ALE
        run grades inline, so the tree is already complete at that point.
        """
        a = (agent or "acp_codex").strip().lower()
        if a in ("react", "eog", "react_agent"):
            from .agents import react_agent
            run = react_agent(self, **kwargs)
            agent_name = "react"
        elif a in ("acp_codex", "codex", "acp", "acp_codex_agent", "codex_acp"):
            from .codex import acp_codex_agent
            run = acp_codex_agent(self, **kwargs)
            agent_name = "acp_codex"
        else:
            raise ValueError(f"unknown agent={agent!r} (use 'react' or 'acp_codex')")
        artifacts: bytes | None = None
        if fetch_artifacts:
            try:
                artifacts = self.fetch_run_artifacts()
            except Exception:  # noqa: BLE001 - evidence is optional, the score is not
                artifacts = None
        grade = self.grade(keep_alive=keep_alive)
        return EvalReport(
            task_id=self.task_id,
            agent=agent_name,
            accuracy=grade.pass_rate,
            overall_success=grade.overall_success,
            latency_s=getattr(run, "latency_s", None),
            total_tokens=getattr(run, "total_tokens", None),
            input_tokens=getattr(run, "input_tokens", None),
            output_tokens=getattr(run, "output_tokens", None),
            cache_read_tokens=getattr(run, "cache_read_tokens", None),
            cost_usd=getattr(run, "cost_usd", None),
            n_steps=getattr(run, "n_steps", None),
            n_subagent_spawns=getattr(run, "n_subagent_spawns", None),
            grade=grade,
            run=run,
            artifacts_tgz=artifacts,
        )


def _baked_base_url() -> str:
    """The service URL shipped with the SDK build, if any (else ``""``).

    ``_service.py`` ships the hosted service's public URL, so whoever installs the
    SDK gets a build that already knows where to connect and ``EvalClient()`` just
    works -- the endpoint "lives on the server side", never in user code. The
    service can also override this at wheel-build time via ``$EVAL_SERVICE_PUBLIC_URL``
    (see the service's ``_build_sdk_wheel``). Returns ``""`` only if ``_service.py``
    is missing or blank, in which case the caller falls back to ``$EVAL_SERVICE_URL``
    / localhost.
    """
    try:
        from ._service import DEFAULT_BASE_URL  # baked at wheel-build time
    except Exception:  # noqa: BLE001 - older / hand-built installs may lack it
        return ""
    return (DEFAULT_BASE_URL or "").strip().rstrip("/")


class EvalClient:
    #: Final fallback when nothing else is configured (local-dev default).
    LOCAL_DEFAULT_URL = "http://localhost:8077"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 1800.0,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        # Resolve the service endpoint so callers can just do ``EvalClient()`` --
        # the URL never has to appear in user code. Precedence: explicit arg ->
        # ``$EVAL_SERVICE_URL`` -> the URL baked into the served wheel -> localhost.
        self.base_url = (
            base_url
            or os.environ.get("EVAL_SERVICE_URL")
            or _baked_base_url()
            or self.LOCAL_DEFAULT_URL
        ).rstrip("/")
        # Same idea for the (optional) service key: explicit arg, else env.
        self.api_key = api_key or os.environ.get("EVAL_SERVICE_API_KEY") or ""
        # ``ngrok-skip-browser-warning`` dodges the free-tier interstitial for
        # non-browser clients; ``User-Agent`` tags calls as SDK traffic so the
        # service's usage log (GET /v1/usage) can tell SDK from browser/curl;
        # ``Authorization`` is sent only when a key is set. All are harmless
        # against a plain host and are reused by the MCP client (see
        # ``Task.mcp_session``) so acting authenticates + is attributed too.
        headers = {
            "ngrok-skip-browser-warning": "true",
            "User-Agent": _user_agent(),
            **(extra_headers or {}),
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.headers = headers
        self._http = httpx.Client(timeout=timeout, headers=headers)

    # -- low-level ---------------------------------------------------------- #
    def _send(
        self, method: str, path: str, *, params: dict | None = None, json: Any = None
    ) -> httpx.Response:
        """Single choke point for every request: times it, logs at DEBUG, then
        raises :class:`ServiceError` on non-2xx. The service records usage
        centrally (GET /v1/usage); this DEBUG line is the matching client-side
        view -- enable ``logging.getLogger("simple_agentic_evals")`` to see it."""
        t0 = time.perf_counter()
        r = self._http.request(method, self.base_url + path, params=params, json=json)
        _log.debug(
            "%s %s -> %s (%.0f ms)", method, path, r.status_code,
            (time.perf_counter() - t0) * 1000.0,
        )
        _raise_for_status(r)
        return r

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._send("GET", path, params=params).json()

    def _get_bytes(self, path: str, params: dict | None = None) -> bytes:
        return self._send("GET", path, params=params).content

    def _post(self, path: str, json: Any, params: dict | None = None) -> Any:
        return self._send("POST", path, params=params, json=json).json()

    def _delete(self, path: str) -> Any:
        return self._send("DELETE", path).json()

    # -- Codex's memory across trials --------------------------------------- #
    def consolidate_memory(
        self,
        memory_key: str,
        *,
        label: str = "manual",
        model: str | None = None,
        api_key: str | None = None,
        root_only: bool = False,
    ) -> dict[str, Any]:
        """Ask Codex to distil the rollouts in a memory home into its memory file.

        The companion to ``memory_key`` on an agent run: that accumulates rollouts
        in a persistent ``CODEX_HOME`` server-side, this turns them into the
        ``memories/memory_summary.md`` Codex injects into later runs on the same
        key.  Needed because Codex's own extraction waits for a rollout to go idle
        for an hour, which a benchmark run never does -- so the caller consolidates
        at a point that means something to it, such as the end of a training stage.

        ``root_only`` builds the memory out of the orchestrator's own rollouts
        and leaves out the specialists', to go with ``memory_main_only`` on the
        run itself: that one decides who READS the memory, this one what is in
        it.  Setting either alone gives an orchestrator reading a memory made
        of sessions it never ran, or specialists reading one made without them.

        Session-less and safe to call between runs.  Returns the service's report
        (``exit_code``, ``summary_bytes_before/after``, ``wrote_memory``).
        """
        return self._post("/v1/memory/consolidate", {
            "memory_key": memory_key, "label": label,
            "model": model, "api_key": api_key, "root_only": root_only,
        })

    # -- long agent runs: submit as a background job, then poll ------------- #
    def _run_agent_job(
        self,
        session_id: str,
        body: dict[str, Any],
        *,
        poll_interval: float = 3.0,
        max_wait: float | None = None,
        on_poll: Any = None,
    ) -> dict[str, Any]:
        """Submit an agent run as a background job and poll until it finishes.

        Returns the run-summary dict. This keeps every HTTP request short (a
        submit plus cheap status polls) so a long ALE/Codex run is never held open
        in one request long enough to hit an upstream proxy/tunnel timeout. If the
        service ran synchronously (an older build that ignores ``background``), the
        returned summary is used as-is. Mirrors what a synchronous run would raise:
        a failed run surfaces as :class:`ServiceError` with the same status/detail.
        """
        sub = self._post(
            f"/v1/sessions/{session_id}/run_agent", {**body, "background": True}
        )
        job_id = sub.get("job_id") if isinstance(sub, dict) else None
        if not job_id:                       # older server: already the summary
            return sub if isinstance(sub, dict) else {}
        if max_wait is None:
            base = body.get("timeout_s")
            base = float(base) if isinstance(base, (int, float)) else 1800.0
            max_wait = base + 300.0
        poll_path = f"/v1/sessions/{session_id}/jobs/{job_id}"
        t0 = time.monotonic()
        interval, transient = poll_interval, 0
        while True:
            try:
                st = self._get(poll_path)
                transient = 0
            except ServiceError as e:
                # A brief upstream hiccup (proxy/tunnel 5xx, or a 404 race) must
                # not abort a run that's still cooking -- retry a few times.
                if (e.status_code >= 500 or e.status_code == 404) and transient < 5:
                    transient += 1
                    time.sleep(interval)
                    continue
                raise
            status = st.get("status")
            if on_poll is not None:
                try:
                    on_poll(status, time.monotonic() - t0, st)
                except Exception:  # noqa: BLE001 - progress must never break polling
                    pass
            if status == "done":
                return st.get("result") or {}
            if status == "error":
                err = st.get("error") or {}
                raise ServiceError(
                    int(err.get("status_code", 502) or 502),
                    str(err.get("detail", "") or "agent run failed"),
                    url=self.base_url + poll_path,
                )
            if time.monotonic() - t0 > max_wait:
                raise ServiceError(
                    504,
                    f"agent run did not finish within {int(max_wait)}s (job "
                    f"{job_id} still {status!r}); raise timeout_s or poll "
                    f"GET {poll_path} yourself.",
                    url=self.base_url + poll_path,
                )
            time.sleep(interval)
            interval = min(interval * 1.4, 12.0)   # gentle backoff for long runs

    # -- discovery ---------------------------------------------------------- #
    def health(self) -> dict[str, Any]:
        return self._get("/v1/health")

    def usage(self, top: int = 20) -> dict[str, Any]:
        """Service-side usage stats (GET /v1/usage): endpoints ranked by call
        count (popularity) and by p95 latency (bottleneck), plus per-hour/day
        counts and a by-client breakdown. Handy for operators watching adoption.
        """
        return self._get("/v1/usage", params={"top": top})

    def benchmarks(self) -> dict[str, Any]:
        return self._get("/v1/benchmarks")

    def task_ids(
        self, dataset: str, benchmark: str, version: int | str, split: str = "test",
        domain: str | None = None, limit: int | None = None, offset: int = 0,
    ) -> list[str]:
        params = {
            "dataset": dataset, "benchmark": benchmark, "version": version,
            "split": split, "offset": offset,
        }
        if domain is not None:
            params["domain"] = domain
        if limit is not None:
            params["limit"] = limit
        return self._get("/v1/tasks", params=params).get("task_ids", [])

    def resources(
        self, dataset: str, benchmark: str, version: int | str,
        task_id: str | None = None, split: str = "test", domain: str | None = None,
        mode: str | None = None, include_content: bool = True,
    ) -> dict[str, Any]:
        """Inspect the evolving tools/skills/agents for a task (no session needed).

        ``mode`` is ``oracle`` | ``accumulative`` | ``none`` (per-kind default);
        ``version`` may be an int stage or ``"full"`` (the whole universe). For
        skills/agents, set ``include_content=True`` to get the ``SKILL.md`` /
        ``.toml`` bodies inline.
        """
        params: dict[str, Any] = {
            "dataset": dataset, "benchmark": benchmark, "version": version,
            "split": split, "include_content": str(include_content).lower(),
        }
        for k, v in (("domain", domain), ("task_id", task_id), ("mode", mode)):
            if v is not None:
                params[k] = v
        return self._get("/v1/resources", params=params).get("resource", {})

    def _selector(
        self, dataset: str, benchmark: str, version: int | str, split: str,
        domain: str | None, resource_mode: str | None,
    ) -> dict[str, Any]:
        sel: dict[str, Any] = {
            "dataset": dataset, "benchmark": benchmark, "version": version,
            "split": split, "domain": domain,
        }
        if resource_mode is not None:
            sel["resource_mode"] = resource_mode   # oracle | accumulative | none
        return sel

    # -- the main loop ------------------------------------------------------ #
    def tasks(
        self, dataset: str, benchmark: str, version: int | str, split: str = "test",
        domain: str | None = None, resource_mode: str | None = None,
        limit: int | None = None, offset: int = 0,
    ) -> Iterator[Task]:
        """Yield (not-yet-started) Task handles for a (dataset, benchmark, …).

        Each Task lazily provisions its session on ``with task:`` / ``start()``
        so only the task you're actually running holds a live gym DB. Pass
        ``resource_mode`` to attach the evolving tools/skills/agents at
        ``oracle`` | ``accumulative`` | ``none`` (rides on ``task.resources``).
        """
        selector = self._selector(dataset, benchmark, version, split, domain, resource_mode)
        for tid in self.task_ids(
            dataset, benchmark, version, split, domain, limit=limit, offset=offset
        ):
            yield Task(self, selector, tid)

    def task(
        self, dataset: str, benchmark: str, version: int | str, task_id: str,
        split: str = "test", domain: str | None = None, resource_mode: str | None = None,
    ) -> Task:
        """A single (not-yet-started) Task handle addressed by ``task_id``."""
        selector = self._selector(dataset, benchmark, version, split, domain, resource_mode)
        return Task(self, selector, task_id)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EvalClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
