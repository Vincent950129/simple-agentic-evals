"""One call that reproduces the paper's headline number for your own agent.

The benchmark stages a domain into versions whose harness *grows* each stage --
more tools, more ``SKILL.md`` bundles, a bigger specialist roster. The paper
evaluates a system against that growth in two modes, and this module is those
two modes behind one function:

    from simple_agentic_evals import run_benchmark

    run_benchmark(my_agent, mode="deployment")       # fixed system, growing harness
    run_benchmark(my_agent, mode="self_evolving")    # system adapts as it grows
    run_benchmark(my_agent, mode="task_specific")    # reference: only the gold capabilities

What comes back is ``ACC`` as the paper defines it (Appendix "Metrics",
eq. ``benchmark-objective``): the micro-averaged score over **every** evaluation
task, all cohorts pooled, under the **final** harness :math:`\\mathcal{H}_T`::

    ACC = 1/|D_<=T,eval| * sum_i V(A(x_i; H_T, z_T), y_i)

Micro, not macro -- a cohort of 34 tasks counts 34 times as much as a cohort of
1. Reporting the mean of the per-cohort means instead is a different (and
usually higher) number.

Why one selector is enough for that: the service's ``version="full"`` pools every
stage's tasks, and ``resource_mode="accumulative"`` at ``"full"`` resolves to the
whole final-stage universe -- so ``version="full"`` *is* :math:`\\mathcal{H}_T`,
the last row of the performance matrix. The expensive per-stage sweep is only
needed for the transfer metrics, which is why ``matrix=True`` is opt-in.

The three modes, in the paper's own terms (§3.4 Evaluation Protocol):

``deployment``
    "the system has no persistent state across stages" -- a fresh instance at
    each stage under the cumulative harness. Degradation here is purely
    *harness-induced*: nothing about the agent changed, only the size of the
    pool it has to choose from.
``self_evolving``
    "the system maintains persistent adaptive state :math:`z_t`" -- before each
    stage's evaluation it may learn from that stage's adaptation split. Pass an
    ``adapt`` callable (or give your agent an ``.adapt`` method); everything
    else is identical, which is the point of comparing the two.
``task_specific``
    The reference condition, not a third continual-learning mode: each task is
    given only its own annotated capabilities, so there are no distractors and
    no stages to sweep. One pass, one number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .client import MissingAPIKey

__all__ = ["run_benchmark", "BenchmarkReport", "CohortResult", "PAPER_SCOPE"]

# Modes, with the aliases people actually type (including the repo's habitual
# "evovling" spelling, which the dataset names also carry).
_MODES = {
    "deployment": "deployment",
    "deployment_eval": "deployment",
    "deploy": "deployment",
    "none": "deployment",
    "self_evolving": "self_evolving",
    "self_evolving_adapt_eval": "self_evolving",
    "self_evovling_adapt_eval": "self_evolving",
    "self_evovling": "self_evolving",
    "adapt": "self_evolving",
    "task_specific": "task_specific",
    "task-specific": "task_specific",
    "oracle": "task_specific",
}
_RESOURCE_FOR_MODE = {
    "deployment": "accumulative",
    "self_evolving": "accumulative",
    "task_specific": "oracle",
}

# `enterprise_tri_hybrid` is a cross-gym stress domain that appears in no
# published table -- the paper's EOG streams are Calendar, CSM, Drive, Email,
# HR, Hybrid, ITSM and Teams. It is several times larger than all of those
# combined, so including it silently would move every denominator.
NON_PAPER_EOG_DOMAINS = frozenset({"enterprise_tri_hybrid"})

#: Task counts each published table divides by, for the scope check below.
PAPER_SCOPE: dict[tuple[str, str], int] = {
    ("evovling_tools", "eog"): 454, ("evovling_tools", "ale"): 63,
    ("evovling_skills", "eog"): 148, ("evovling_skills", "ale"): 68,
    ("evovling_agents", "eog"): 148, ("evovling_agents", "ale"): 63,
}


@dataclass
class CohortResult:
    """One cell of the performance matrix: cohort ``stage`` under harness ``at``."""
    domain: str | None
    stage: int
    at_stage: int | str          # harness the cohort was evaluated under
    n_tasks: int = 0
    score_sum: float = 0.0       # sum of per-task scores (micro-averaging)
    n_success: int = 0           # tasks solved outright, not just partially
    n_errors: int = 0
    latency_s: float = 0.0       # wall clock for the cohort, provisioning included
    agent_s: float = 0.0         # sum of per-task agent time (the comparable one)
    total_tokens: int = 0
    n_steps: int = 0
    # A task counts here only if it actually reported the quantity, so a
    # partially instrumented sweep reads as partial rather than as a total that
    # silently under-reports. A BYOA agent that never returns its usage leaves
    # n_with_tokens at 0, and the report says "not measured" instead of "0".
    n_with_latency: int = 0
    n_with_tokens: int = 0
    per_task: list[dict] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.score_sum / self.n_tasks if self.n_tasks else float("nan")

    @property
    def success_rate(self) -> float:
        return self.n_success / self.n_tasks if self.n_tasks else float("nan")


@dataclass
class BenchmarkReport:
    """The paper's headline number, plus what it was computed over."""
    mode: str
    dataset: str
    benchmark: str
    resource_mode: str
    domains: list[str | None]
    accuracy: float = float("nan")       # ACC, eq. benchmark-objective
    success_rate: float = float("nan")   # share of tasks solved outright
    n_tasks: int = 0
    n_success: int = 0
    n_errors: int = 0
    latency_s: float = 0.0               # wall clock, provisioning included
    agent_s: float = 0.0                 # agent time only; the comparable one
    total_tokens: int = 0
    n_steps: int = 0
    n_with_latency: int = 0
    n_with_tokens: int = 0
    complete: bool = True                # False when `limit` subsampled the scope
    paper_n_tasks: int | None = None     # what the published table divides by
    last_row: list[CohortResult] = field(default_factory=list)
    matrix: list[CohortResult] = field(default_factory=list)
    before_adapt: list[CohortResult] = field(default_factory=list)
    bwt: float | None = None
    fwt: float | None = None
    per_stage_acc: dict[int, float] = field(default_factory=dict)

    @property
    def task_results(self) -> list[dict]:
        """Flattened final-row task results, including ``per_verifier`` details."""
        return [task for cohort in self.last_row for task in cohort.per_task]

    @property
    def verifier_results(self) -> list[dict]:
        """One row per task/verifier, convenient for analysis or a DataFrame."""
        rows: list[dict] = []
        for task in self.task_results:
            for verifier in task.get("per_verifier", []) or []:
                rows.append({"task_id": task.get("task_id"), **dict(verifier)})
        return rows

    def __str__(self) -> str:
        return self.summary()

    @staticmethod
    def _per_task(total: float, n: int, unit: str = "") -> str:
        """A per-task mean, or ``N/A`` -- never a total that reads as a measured 0."""
        if not n:
            return "N/A"
        mean = total / n
        return (f"{mean:,.1f}{unit}" if unit else f"{mean:,.0f}")

    @property
    def tokens_per_task(self) -> float | None:
        """Mean tokens per task, or ``None`` when nothing reported any.

        ``None`` rather than ``0`` so a caller building a results row propagates
        "not applicable" instead of claiming the agent was free.
        """
        return (self.total_tokens / self.n_with_tokens) if self.n_with_tokens else None

    def summary(self) -> str:
        head = (f"{self.dataset}/{self.benchmark}  mode={self.mode}  "
                f"resource={self.resource_mode}")
        scope = f"{self.n_tasks} tasks"
        if self.paper_n_tasks is not None:
            scope += (" (the published scope)" if self.n_tasks == self.paper_n_tasks
                      else f" of the published {self.paper_n_tasks}")
        if not self.complete:
            scope += " -- SUBSAMPLED, not comparable to the paper"
        lines = [head, f"  ACC        {self.accuracy:.3f}   over {scope}"]
        # The paper reports partial credit and outright solves side by side:
        # a row at ACC .85 that solved nothing is a different result from one
        # that solved most of it, and only printing the mean hides which it is.
        if self.n_tasks:
            lines.append(f"  solved     {self.success_rate:.3f}   "
                         f"{self.n_success}/{self.n_tasks} tasks passed every check")
        lines.append(f"  latency    {self._per_task(self.agent_s, self.n_with_latency, 's')}"
                     f"   per task ({self.latency_s / 60:.1f} min wall clock)")
        # Tokens are the one column the service cannot fill for a bring-your-own
        # agent: the model calls are made in the caller's process, against the
        # caller's key, and never reach us. Saying N/A is the honest report --
        # printing 0 would state a measurement we never took.
        if not self.n_with_tokens:
            lines.append("  tokens     N/A (your own agent)   only it sees its model "
                         "calls -- return {'total_tokens': n} to fill this in")
        else:
            partial = ("" if self.n_with_tokens == self.n_tasks
                       else f"  [only {self.n_with_tokens}/{self.n_tasks} tasks reported]")
            lines.append(f"  tokens     {self.total_tokens / self.n_with_tokens:,.0f}"
                         f"   per task{partial}")
        if self.bwt is not None:
            lines.append(f"  BWT        {self.bwt:+.3f}   "
                         f"(negative = the growing harness cost you earlier cohorts)")
        if self.fwt is not None:
            lines.append(f"  FWT        {self.fwt:+.3f}   "
                         f"(negative = adapting hurt the new cohort)")
        if self.per_stage_acc:
            per = "  ".join(f"v{k}:{v:.2f}" for k, v in sorted(self.per_stage_acc.items()))
            lines.append(f"  by cohort  {per}")
        if self.n_errors:
            share = self.n_errors / self.n_tasks if self.n_tasks else 1.0
            verdict = ("this ACC is NOT a result -- fix the deployment and re-run"
                       if share > 0.1 else
                       "few enough to be noise, but check they are not systematic")
            lines.append(f"  WARNING    {self.n_errors}/{self.n_tasks} task(s) errored and "
                         f"scored 0 ({share:.0%}); {verdict}")
            first = next((t for c in self.last_row for t in c.per_task if t.get("error")), None)
            if first:
                lines.append(f"             first: {first['error'][:120]}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Scope resolution                                                             #
# --------------------------------------------------------------------------- #
def _domains_and_stages(client, dataset: str, benchmark: str,
                        domain: str | None | Sequence[str]) -> dict[str | None, list[int]]:
    """Map each in-scope domain to its stage list, straight from the catalog."""
    found: dict[str | None, list[int]] = {}
    for ds in client.benchmarks()["datasets"]:
        if ds["dataset"] != dataset:
            continue
        for b in ds["benchmarks"]:
            # Action surfaces are adapter metadata now; a benchmark no longer
            # has to masquerade as EOG or ALE to participate in the same stage
            # and resource evaluation loop.
            if b["benchmark"] != benchmark or not b.get("runnable", True):
                continue
            for d in b["domains"]:
                found[d["domain"] or None] = sorted(v["version"] for v in d["versions"])
    if not found:
        raise ValueError(f"{dataset}/{benchmark} is not on this deployment")

    if domain == "all":
        return found
    if domain is not None and not isinstance(domain, (list, tuple, set, frozenset)):
        if domain not in found:
            raise ValueError(f"domain {domain!r} not in {sorted(str(k) for k in found)}")
        return {domain: found[domain]}
    if domain is not None:
        return {d: found[d] for d in domain}
    # Default: the scope the published tables use.
    return {d: v for d, v in found.items() if d not in NON_PAPER_EOG_DOMAINS}


# --------------------------------------------------------------------------- #
# Running one cohort                                                           #
# --------------------------------------------------------------------------- #
def _score_task(task, agent, agent_kwargs: dict) -> tuple[float, bool, dict]:
    """Act on one task and grade it. Returns (score, solved, cost).

    A harness that *failed* is not a harness that scored zero. Both produce an
    empty environment and therefore the same grade, so the difference has to be
    read off the run itself -- otherwise a broken deployment reports a full,
    plausible-looking ACC at roughly the do-nothing floor. A cap the agent ran
    into (``max_episodes``, ``timeout``) is a real result and stays.

    ``cost`` mirrors the paper's per-trial block. A provided harness already
    measures all of it server-side, so we just carry it through. Your own agent
    is timed here, but its tokens are spent on model calls the service never
    sees -- so it may ``return {"total_tokens": n, "n_steps": k}`` to have them
    counted, and anything it omits is reported as unmeasured rather than zero.
    """
    with task:
        if isinstance(agent, str):
            rep = task.evaluate(agent=agent, keep_alive=True, **agent_kwargs)
            if getattr(rep.run, "stopped", "") == "error":
                raise RuntimeError(
                    f"the {agent!r} harness errored server-side after "
                    f"{getattr(rep.run, 'n_calls', 0)} tool calls "
                    f"({(getattr(rep.run, 'final_message', '') or 'no message')[:120]})")
            return float(rep.accuracy), bool(rep.overall_success), {
                "latency_s": rep.latency_s, "total_tokens": rep.total_tokens,
                "n_steps": rep.n_steps,
                "per_verifier": list(getattr(rep.grade, "per_verifier", []) or []),
            }
        t0 = time.time()
        reported = agent(task, **agent_kwargs)
        elapsed = time.time() - t0
        grade = task.grade(keep_alive=True)
        cost = {"latency_s": elapsed}
        if isinstance(reported, dict):
            cost.update({k: v for k, v in reported.items() if v is not None})
        # Grading, not the agent, owns verifier outcomes. Assign this after
        # telemetry so a callable cannot accidentally overwrite the truth.
        cost["per_verifier"] = list(grade.per_verifier or [])
        return float(grade.pass_rate), bool(grade.overall_success), cost


def _run_cohort(client, agent, *, dataset, benchmark, domain, stage, at_stage,
                resource_mode, split, limit, on_error, progress,
                agent_kwargs) -> CohortResult:
    """Evaluate one task cohort under one harness stage."""
    cell = CohortResult(domain=domain, stage=stage, at_stage=at_stage)
    t0 = time.time()
    for task in client.tasks(dataset, benchmark, at_stage, split, domain,
                             resource_mode=resource_mode, limit=limit):
        try:
            score, solved, cost = _score_task(task, agent, agent_kwargs)
        except MissingAPIKey:      # setup, not a failed task -- never score it 0
            raise
        except Exception as e:  # noqa: BLE001 - one bad task must not lose the run
            if on_error == "raise":
                raise
            cell.n_errors += 1
            score, solved, cost = 0.0, False, {}
            cell.per_task.append({"task_id": task.task_id, "score": 0.0,
                                  "per_verifier": [],
                                  "error": f"{type(e).__name__}: {e}"})
        else:
            cell.per_task.append({"task_id": task.task_id, "score": score,
                                  "solved": solved, **cost})
        cell.n_tasks += 1
        cell.score_sum += score
        cell.n_success += int(solved)
        if cost.get("latency_s") is not None:
            cell.agent_s += float(cost["latency_s"])
            cell.n_with_latency += 1
        if cost.get("total_tokens") is not None:
            cell.total_tokens += int(cost["total_tokens"])
            cell.n_with_tokens += 1
        cell.n_steps += int(cost.get("n_steps") or 0)
    cell.latency_s = time.time() - t0
    if progress:
        dom = domain or benchmark
        print(f"    {dom:24} {cell.n_tasks:4} tasks  acc {cell.accuracy:.3f}"
              + (f"  ({cell.n_errors} errored)" if cell.n_errors else ""))
    return cell


# --------------------------------------------------------------------------- #
# The entry point                                                              #
# --------------------------------------------------------------------------- #
def run_benchmark(
    agent: Callable[[Any], Any] | str,
    mode: str = "deployment",
    *,
    dataset: str = "evovling_tools",
    benchmark: str = "eog",
    domain: str | None | Sequence[str] = None,
    split: str = "test",
    adapt: Callable[[int, Iterable[Any]], Any] | None = None,
    matrix: bool = False,
    limit: int | None = None,
    agent_kwargs: dict[str, Any] | None = None,
    on_error: str = "score_zero",
    client: Any = None,
    progress: bool = True,
) -> BenchmarkReport:
    """Score ``agent`` the way the paper does, and return ``ACC``.

    Args:
        agent: ``act(task)`` -- anything that takes a task and acts on it. The
            strings ``"react"`` and ``"codex"`` select a provided harness
            instead, which also fills in the cost columns.
        mode: ``"deployment"`` (default), ``"self_evolving"``, or
            ``"task_specific"``. See the module docstring; aliases such as
            ``"deployment_eval"`` and ``"self_evolving_adapt_eval"`` work too.
        dataset / benchmark / domain: what to score on. ``domain=None`` means
            the published scope (every EOG domain the paper reports);
            ``domain="all"`` adds the ones it doesn't.
        adapt: ``adapt(stage, tasks)`` -- called once per stage in
            ``self_evolving`` mode with that stage's **adaptation** split, to
            update your agent's persistent state before it is evaluated. If
            omitted, ``agent.adapt`` is used when present.
        matrix: fill the whole lower-triangular matrix instead of only the last
            row, which is what BWT and FWT need. Costs roughly T times as much.
        limit: cap tasks per cohort. Turns the run into a smoke test -- the
            report will say so, because a subsampled ACC is not the paper's.
        agent_kwargs: forwarded to every call of ``agent`` -- for the provided
            harnesses that means ``api_key`` / ``model`` / ``max_steps``.
        on_error: ``"score_zero"`` (default, and what the paper does with a task
            the agent simply failed) or ``"raise"``.

    Returns:
        :class:`BenchmarkReport`; ``print(report)`` gives the paper-style row.
    """
    agent_kwargs = dict(agent_kwargs or {})
    if on_error not in ("score_zero", "raise"):
        raise ValueError(f"on_error must be 'score_zero' or 'raise', got {on_error!r}")
    key = _MODES.get(str(mode).strip().lower().replace("-", "_"))
    if key is None:
        raise ValueError(f"mode must be one of {sorted(set(_MODES.values()))}, got {mode!r}")
    resource_mode = _RESOURCE_FOR_MODE[key]

    if client is None:
        from .client import EvalClient
        client = EvalClient()
    if key == "self_evolving" and adapt is None:
        adapt = getattr(agent, "adapt", None)
        if adapt is None:
            raise ValueError(
                "self_evolving needs an adaptation step: pass adapt=fn(stage, tasks) "
                "or give your agent an .adapt method. With neither, the run would be "
                "identical to mode='deployment' and the comparison meaningless.")
    if key == "task_specific" and matrix:
        raise ValueError("task_specific is a single-pass reference condition, not a "
                         "continual-learning sweep -- there is no matrix to fill")

    scope = _domains_and_stages(client, dataset, benchmark, domain)
    report = BenchmarkReport(
        mode=key, dataset=dataset, benchmark=benchmark, resource_mode=resource_mode,
        domains=sorted(scope, key=lambda d: (d is None, d)),
        paper_n_tasks=PAPER_SCOPE.get((dataset, benchmark)) if domain is None else None,
        complete=limit is None,
    )
    if progress:
        print(f"{dataset}/{benchmark}  mode={key}  resource={resource_mode}  "
              f"{len(scope)} domain(s)")

    # Self-evolving: walk the stages so the agent's state z_t is built the way
    # the paper builds it, on the adaptation split, before anything is scored.
    if key == "self_evolving":
        for dom, stages in scope.items():
            for t in stages:
                # FWT compares the new cohort under its own harness before and
                # after this stage's adaptation, so the "before" half has to be
                # measured here -- once adapt() runs, z_{t-1} is gone.
                if matrix and t != stages[0]:
                    report.before_adapt.append(_run_cohort(
                        client, agent, dataset=dataset, benchmark=benchmark,
                        domain=dom, stage=t, at_stage=t, resource_mode=resource_mode,
                        split=split, limit=limit, on_error=on_error, progress=progress,
                        agent_kwargs=agent_kwargs))
                train = client.tasks(dataset, benchmark, t, "train", dom,
                                     resource_mode=resource_mode, limit=limit)
                if progress:
                    print(f"  adapt  {dom or benchmark:22} stage v{t}")
                adapt(t, train)
                if matrix:                       # cohorts tau <= t under harness t
                    for tau in [s for s in stages if s <= t]:
                        report.matrix.append(_run_cohort(
                            client, agent, dataset=dataset, benchmark=benchmark,
                            domain=dom, stage=tau, at_stage=t,
                            resource_mode=resource_mode, split=split, limit=limit,
                            on_error=on_error, progress=progress,
                            agent_kwargs=agent_kwargs))
    elif matrix:
        for dom, stages in scope.items():
            for t in stages:
                for tau in [s for s in stages if s <= t]:
                    report.matrix.append(_run_cohort(
                        client, agent, dataset=dataset, benchmark=benchmark,
                        domain=dom, stage=tau, at_stage=t,
                        resource_mode=resource_mode, split=split, limit=limit,
                        on_error=on_error, progress=progress, agent_kwargs=agent_kwargs))

    # The headline: every cohort under the final harness. `version="full"`
    # pools all stages and resolves the last stage's resource, so this one pass
    # IS the last row -- no sweep required.
    if progress:
        print(f"  eval   all cohorts under the final harness"
              f"{' (H_T + z_T)' if key == 'self_evolving' else ''}")
    for dom in scope:
        report.last_row.append(_run_cohort(
            client, agent, dataset=dataset, benchmark=benchmark, domain=dom,
            stage=-1, at_stage="full", resource_mode=resource_mode, split=split,
            limit=limit, on_error=on_error, progress=progress, agent_kwargs=agent_kwargs))

    report.n_tasks = sum(c.n_tasks for c in report.last_row)
    report.n_errors = sum(c.n_errors for c in report.last_row)
    report.latency_s = sum(c.latency_s for c in report.last_row)
    report.agent_s = sum(c.agent_s for c in report.last_row)
    report.total_tokens = sum(c.total_tokens for c in report.last_row)
    report.n_steps = sum(c.n_steps for c in report.last_row)
    report.n_with_latency = sum(c.n_with_latency for c in report.last_row)
    report.n_with_tokens = sum(c.n_with_tokens for c in report.last_row)
    report.n_success = sum(c.n_success for c in report.last_row)
    report.accuracy = (sum(c.score_sum for c in report.last_row) / report.n_tasks
                       if report.n_tasks else float("nan"))
    report.success_rate = (report.n_success / report.n_tasks
                           if report.n_tasks else float("nan"))
    if report.matrix:
        _add_transfer_metrics(report)
    return report


def _add_transfer_metrics(report: BenchmarkReport) -> None:
    """BWT / FWT exactly as the appendix defines them -- weighted by cohort size.

    ``BWT = sum_i n_i (P_T,i - P_i,i) / sum_i n_i`` for ``i < T``, and
    ``FWT = sum_i n_i (P_i,i - P_i,i^before) / sum_i n_i`` for ``i > 1``, where
    ``P_i,i^before`` is the same cohort under the same harness *before* that
    stage's adaptation. Note this is the cohort-size-weighted form, so it does
    not equal the unweighted mean :class:`ContinualMetrics` reports.
    """
    cells = {(c.domain, c.stage, c.at_stage): c for c in report.matrix}
    before = {(c.domain, c.stage): c for c in report.before_adapt}
    seen: dict[Any, set[int]] = {}
    for c in report.matrix:
        seen.setdefault(c.domain, set()).add(c.stage)
    stages_by_dom = {d: sorted(s) for d, s in seen.items()}

    bwt_num = bwt_den = fwt_num = fwt_den = 0.0
    diag_by_stage: dict[int, list[CohortResult]] = {}
    for dom, stages in stages_by_dom.items():
        T = stages[-1]
        for i in stages:
            diag = cells.get((dom, i, i))
            if diag is None:
                continue
            diag_by_stage.setdefault(i, []).append(diag)
            final = cells.get((dom, i, T))
            if i != T and final is not None:              # BWT: last row vs diagonal
                bwt_num += final.n_tasks * (final.accuracy - diag.accuracy)
                bwt_den += final.n_tasks
            prior = before.get((dom, i))
            if prior is not None:                          # FWT: diagonal, after vs before
                fwt_num += diag.n_tasks * (diag.accuracy - prior.accuracy)
                fwt_den += diag.n_tasks
    report.bwt = bwt_num / bwt_den if bwt_den else None
    # Left unset rather than reported as 0 when nothing measured the
    # before-adaptation half -- deployment runs have no adaptation to transfer.
    report.fwt = fwt_num / fwt_den if fwt_den else None
    report.per_stage_acc = {
        i: sum(c.score_sum for c in cs) / sum(c.n_tasks for c in cs)
        for i, cs in diag_by_stage.items() if sum(c.n_tasks for c in cs)
    }
