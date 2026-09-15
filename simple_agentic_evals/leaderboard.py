"""Leaderboard aggregation shared by notebooks, command adapters and the CLI."""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Any, Callable, Mapping, Sequence

from .benchmark import BenchmarkReport, run_benchmark

__all__ = ["run_leaderboard", "aggregate_leaderboard", "report_summary"]


def report_summary(report: BenchmarkReport | Mapping[str, Any]) -> dict[str, Any]:
    """Return a checkpoint-safe report, including task/verifier diagnostics."""
    if isinstance(report, Mapping):
        return dict(report)
    return {
        "benchmark": report.benchmark,
        "accuracy": report.accuracy,
        "success_rate": report.success_rate,
        "n_tasks": report.n_tasks,
        "n_success": report.n_success,
        "n_errors": report.n_errors,
        "agent_s": report.agent_s,
        "total_tokens": report.total_tokens,
        "n_with_tokens": report.n_with_tokens,
        "complete": report.complete,
        "task_results": list(getattr(report, "task_results", []) or []),
    }


def aggregate_leaderboard(
    reports: Mapping[str, Sequence[BenchmarkReport | Mapping[str, Any]]],
    name: str,
    *,
    cat: str = "deploy",
    dataset: str = "evovling_tools",
    mode: str = "deployment_eval",
    seeds: int | None = None,
    harness: str | None = None,
    mas: bool = False,
    force_partial: bool = False,
    adapter_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a submission-shaped record for any benchmark set.

    Overall pass rate is micro-averaged over all tasks and repeats. Standard
    deviations are population standard deviations, matching the leaderboard.
    Missing token telemetry remains ``None`` rather than becoming a false zero.
    When reports contain task diagnostics, ``task_results`` retains every
    seed's task rows; each task row contains its ``per_verifier`` results.
    """
    normalized = {
        benchmark: [report_summary(report) for report in bench_reports]
        for benchmark, bench_reports in reports.items()
    }
    inferred_seeds = max((len(v) for v in normalized.values()), default=0)
    rec: dict[str, Any] = {
        "name": name,
        "cat": cat,
        "dataset": dataset,
        "mode": mode,
        "seeds": inferred_seeds if seeds is None else seeds,
    }
    if harness:
        rec["harness"] = harness
    if mas:
        rec["mas"] = True

    solved = attempted = errors = 0
    partial = force_partial
    benchmark_results: dict[str, dict[str, Any]] = {}
    benchmark_order = list(normalized)
    for benchmark in benchmark_order:
        runs = normalized.get(benchmark, [])
        if not runs:
            partial = True
            continue
        pass_rates = [float(r.get("success_rate", 0.0)) * 100 for r in runs]
        scores = [float(r.get("accuracy", 0.0)) * 100 for r in runs]
        result = {
            "pass_rate": round(mean(pass_rates), 1),
            "pass_rate_sd": round(pstdev(pass_rates), 1),
            "score": round(mean(scores), 1),
            "score_sd": round(pstdev(scores), 1),
            "hours": round(mean(float(r.get("agent_s", 0.0)) for r in runs) / 3600, 2),
            "n_tasks": int(runs[0].get("n_tasks", 0) or 0),
            "complete": all(bool(r.get("complete", True)) for r in runs),
        }
        measured = [
            int(r.get("total_tokens", 0))
            for r in runs if int(r.get("n_with_tokens", 0) or 0) > 0
        ]
        result["tokens_m"] = round(mean(measured) / 1e6, 1) if measured else None
        if adapter_metadata and benchmark in adapter_metadata:
            result["adapter"] = dict(adapter_metadata[benchmark])
        benchmark_results[benchmark] = result
        # Preserve the published EOG/ALE flat columns exactly.
        if benchmark in ("eog", "ale"):
            rec[benchmark] = result["pass_rate"]
            rec[f"{benchmark}Sd"] = result["pass_rate_sd"]
            rec[f"{benchmark}Score"] = result["score"]
            rec[f"{benchmark}ScoreSd"] = result["score_sd"]
            rec[f"{benchmark}H"] = result["hours"]
            rec[f"{benchmark}Tok"] = result["tokens_m"]
            rec[f"{benchmark}N"] = result["n_tasks"]
        solved += sum(int(r.get("n_success", 0) or 0) for r in runs)
        attempted += sum(int(r.get("n_tasks", 0) or 0) for r in runs)
        errors += sum(int(r.get("n_errors", 0) or 0) for r in runs)
        partial = partial or any(not bool(r.get("complete", True)) for r in runs)

    rec["overall"] = round(100 * solved / attempted, 1) if attempted else None
    rec["benchmark_results"] = benchmark_results
    if partial:
        rec["partial"] = True
    if errors:
        rec["errors"] = errors
    detailed: dict[str, list[dict[str, Any]]] = {}
    for benchmark in benchmark_order:
        seeded = []
        for index, run in enumerate(normalized.get(benchmark, [])):
            tasks = run.get("task_results")
            if isinstance(tasks, list):
                seeded.append({"seed": index + 1, "tasks": tasks})
        if seeded:
            detailed[benchmark] = seeded
    if detailed:
        rec["task_results"] = detailed
    return rec


def run_leaderboard(
    agent: Callable[[Any], Any] | str,
    name: str,
    *,
    client: Any = None,
    dataset: str = "evovling_tools",
    mode: str = "deployment_eval",
    seeds: int = 3,
    limit: int | None = None,
    cat: str = "deploy",
    harness: str | None = None,
    mas: bool = False,
    agent_kwargs: dict[str, Any] | None = None,
    progress: bool = True,
    benchmark_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
    existing_reports: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    on_report: Callable[[str, int, dict[str, Any]], None] | None = None,
    confirm_full_cost: bool = False,
    benchmarks: Sequence[str] | None = None,
    adapter_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the selected benchmarks repeatedly and return leaderboard JSON.

    ``limit`` creates a rehearsal row and always marks it ``partial``. CLI
    callers can checkpoint after every repeat with ``on_report`` and resume by
    supplying those summaries as ``existing_reports``.
    """
    if seeds < 1:
        raise ValueError("seeds must be at least 1")
    if limit is None and not confirm_full_cost:
        raise ValueError(
            "a no-limit leaderboard run evaluates every selected benchmark and may consume "
            "many agent-hours and millions of tokens; pass confirm_full_cost=True"
        )
    selected = tuple(benchmarks or ("eog", "ale"))
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("benchmarks must be a non-empty sequence of unique names")
    reports: dict[str, list[dict[str, Any]]] = {
        b: [report_summary(r) for r in (existing_reports or {}).get(b, [])]
        for b in selected
    }
    for benchmark in selected:
        while len(reports[benchmark]) < seeds:
            index = len(reports[benchmark])
            if progress:
                print(f"=== {name}  {benchmark}  run {index + 1}/{seeds}")
            opts = dict((benchmark_kwargs or {}).get(benchmark, {}))
            report = run_benchmark(
                agent,
                mode,
                client=client,
                dataset=dataset,
                benchmark=benchmark,
                limit=limit,
                agent_kwargs=agent_kwargs,
                progress=progress,
                **opts,
            )
            summary = report_summary(report)
            reports[benchmark].append(summary)
            if on_report is not None:
                on_report(benchmark, index, summary)
    return aggregate_leaderboard(
        reports,
        name,
        cat=cat,
        dataset=dataset,
        mode=mode,
        seeds=seeds,
        harness=harness,
        mas=mas,
        force_partial=limit is not None,
        adapter_metadata=adapter_metadata,
    )
