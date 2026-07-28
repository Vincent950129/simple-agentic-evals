"""Continual-learning metrics (ACC / BWT / FWT / forgetting).

Bundled, dependency-free port of the reference harness's ``ContinualMetrics``.
The evolving benchmark stages a domain into versions whose resource grows each
stage; evaluating an agent as the stages advance is a continual-learning study.
Record the results matrix

    R[k][j] = mean score on stage-j tasks using the agent adapted through stage k
              (lower triangle, j <= k)

and this computes the standard metrics. Convention: rows = adapt stage ``k``
(time), cols = eval stage ``j`` (which tasks); stages are 0-based. A no-memory
baseline for stage ``i`` is recorded with ``adapt_stage=-1`` to enable FWT.

    from simple_agentic_evals import ContinualMetrics, StageResult

    m = ContinualMetrics(num_stages=3)
    for k in range(3):                       # diagonal (each stage on its own tasks)
        m.record(StageResult(eval_stage=k, adapt_stage=k, num_tasks=n, success_rate=acc_k))
    report = m.compute()
    print(m.print_report(report))           # ACC, BWT, FWT, forgetting
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["StageResult", "ContinualMetrics"]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _std(xs: list[float]) -> float:
    """Population std (ddof=0); 0 for fewer than 2 samples."""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _pct(x: float | None) -> str:
    return "nan" if (x is None or math.isnan(x)) else f"{x:+.1%}"


@dataclass
class StageResult:
    """One ``(eval_stage, adapt_stage)`` cell of the matrix. Stages are 0-based;
    use ``adapt_stage=-1`` for a stage's no-memory baseline (enables FWT).

    When a cell aggregates several runs (avg@N), ``success_rate`` /
    ``verifier_pass_rate`` are the means and ``*_std`` hold the standard
    deviations; ``per_run`` may carry the per-run breakdown for ``save()``.
    """
    eval_stage: int
    adapt_stage: int
    num_tasks: int = 0
    success_rate: float = 0.0        # mean score (e.g. pass_rate; partial credit)
    verifier_pass_rate: float = 0.0  # mean fully-passed rate (optional)
    success_rate_std: float = 0.0
    verifier_pass_rate_std: float = 0.0
    num_runs: int = 1
    per_task: list[dict] = field(default_factory=list)
    per_run: list[dict] = field(default_factory=list)


class ContinualMetrics:
    """Builds + analyzes the R[k][j] results matrix and reports CL metrics."""

    def __init__(self, num_stages: int) -> None:
        self.num_stages = num_stages
        self.matrix: dict[tuple[int, int], StageResult] = {}

    def record(self, result: StageResult) -> None:
        self.matrix[(result.eval_stage, result.adapt_stage)] = result

    def compute(self) -> dict:
        n = self.num_stages
        report: dict[str, Any] = {
            "num_stages": n,
            "results_matrix": self._matrix_to_table(),
        }

        # ACC_k: mean over eval stages j<=k of R[j][k].
        acc_per_stage: list[float] = []
        for k in range(n):
            rates = [self.matrix[(i, k)].success_rate
                     for i in range(k + 1) if (i, k) in self.matrix]
            acc_per_stage.append(_mean(rates))
        report["ACC"] = acc_per_stage
        report["final_ACC"] = acc_per_stage[-1] if acc_per_stage else float("nan")

        # BWT = mean_{i<n-1} (R[i][n-1] - R[i][i]); negative => forgetting.
        if n > 1:
            terms = []
            for i in range(n - 1):
                rf, rd = self.matrix.get((i, n - 1)), self.matrix.get((i, i))
                if rf and rd:
                    terms.append(rf.success_rate - rd.success_rate)
            report["BWT"] = _mean(terms) if terms else float("nan")
        else:
            report["BWT"] = 0.0

        # FWT = mean_{i>=1} (R[i][i] - baseline[i]); baseline keyed (i, -1).
        if n > 1:
            terms = []
            for i in range(1, n):
                ra, rb = self.matrix.get((i, i)), self.matrix.get((i, -1))
                if ra and rb:
                    terms.append(ra.success_rate - rb.success_rate)
            report["FWT"] = _mean(terms) if terms else float("nan")
        else:
            report["FWT"] = 0.0

        # Forgetting: best-minus-final per eval stage.
        forgetting = []
        for i in range(n):
            best, final = -1.0, None
            for k in range(i, n):
                r = self.matrix.get((i, k))
                if r:
                    best = max(best, r.success_rate)
                    final = r.success_rate
            if final is not None and best >= 0:
                forgetting.append(best - final)
        report["max_forgetting"] = max(forgetting) if forgetting else 0.0
        report["avg_forgetting"] = _mean(forgetting) if forgetting else 0.0
        return report

    def _matrix_to_table(self) -> list[list[dict | None]]:
        table: list[list[dict | None]] = []
        for k in range(self.num_stages):       # adapt stage (row)
            row: list[dict | None] = []
            for j in range(self.num_stages):    # eval stage (col)
                r = self.matrix.get((j, k))
                row.append(
                    {"mean": r.success_rate, "std": r.success_rate_std, "n": r.num_runs}
                    if r else None
                )
            table.append(row)
        return table

    def print_report(self, report: dict | None = None) -> str:
        report = report or self.compute()
        n = self.num_stages
        lines = ["=" * 60, "CONTINUAL LEARNING METRICS", "=" * 60, ""]
        lines.append("R[k][j]: mean score on stage-j tasks after adapting through stage k")
        lines.append("  rows = adapt stage (time), cols = eval tasks (lower triangle)")
        lines.append("")
        col_w = 14
        lines.append("         " + "".join(f"{'Eval ' + str(j):^{col_w}}" for j in range(n)))
        for k, row in enumerate(report["results_matrix"]):
            cells = []
            for j, v in enumerate(row):
                if j > k:
                    cells.append(f"{'-':^{col_w}}")
                elif v is not None:
                    s = (f"{v['mean']:.1%}±{v['std']:.1%}" if v.get("n", 1) > 1
                         else f"{v['mean']:.1%}")
                    cells.append(f"{s:^{col_w}}")
                else:
                    cells.append(f"{'?':^{col_w}}")
            lines.append(f"Adpt {k}  " + "".join(cells))
        lines.append("")
        lines.append("Per-stage accuracy (ACC_k):")
        for k, acc in enumerate(report["ACC"]):
            lines.append(f"  After stage {k}: {acc:.1%}")
        lines.append(f"\nFinal ACC: {report['final_ACC']:.1%}")
        lines.append(f"BWT (backward transfer):  {_pct(report['BWT'])}")
        lines.append(f"FWT (forward transfer):   {_pct(report['FWT'])}")
        lines.append(f"Avg forgetting:           {_pct(report['avg_forgetting'])}")
        lines.append(f"Max forgetting:           {_pct(report['max_forgetting'])}")
        return "\n".join(lines)

    def save(self, path: str) -> None:
        """Persist the raw matrix cells to ``path`` as JSON.

        The layout matches the reference harness's ``metrics.json`` (cells keyed
        ``"eval,adapt"``; multi-run cells add ``num_runs`` + ``*_std`` +
        ``per_run``) so downstream report builders read it unchanged.
        """
        import json

        data: dict[str, Any] = {}
        for (i, j), r in self.matrix.items():
            cell: dict[str, Any] = {
                "eval_stage": r.eval_stage,
                "adapt_stage": r.adapt_stage,
                "num_tasks": r.num_tasks,
                "success_rate": r.success_rate,
                "verifier_pass_rate": r.verifier_pass_rate,
                "per_task": r.per_task,
            }
            if r.num_runs > 1:
                cell["num_runs"] = r.num_runs
                cell["success_rate_std"] = r.success_rate_std
                cell["verifier_pass_rate_std"] = r.verifier_pass_rate_std
                cell["per_run"] = r.per_run
            data[f"{i},{j}"] = cell
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
