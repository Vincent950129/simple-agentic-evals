# Vendored verbatim from the EnterpriseOps-Gym harness (evovle_skills/src/eog_verifier.py).
# Fix bugs upstream and re-copy; local edits would fork the grader.
"""
EnterpriseOps-Gym verifier and database lifecycle.

The gym MCP servers (already running, e.g. via the gym's `docker compose up`)
expose an HTTP API that does double duty:

  * `/mcp`              -- JSON-RPC MCP transport for Codex's tool calls
  * `/api/seed-database` -- create a per-trial DB from a SQL seed file
  * `/api/sql-runner`    -- run arbitrary SQL against a DB by id
  * `/api/delete-database` -- tear down a DB

For each trial we:

  1. Seed a fresh per-trial DB from each gym's `seed_database_file`.
     Returns one `database_id` per gym (key = `mcp_server_name`).
  2. Codex runs against the gym; we pass `x-database-id: <db_id>` so its
     MCP tool calls hit the same DB that the verifier will read.
  3. After the agent completes, we run the row's `verifiers[]` against
     those DBs via `/api/sql-runner` and aggregate to (pass_rate, reward).
  4. Tear down the DBs.

This module talks to the SAME Docker container that hosts the MCP -- there
are no extra containers and no Harbor.

We deliberately re-implement the parts of EOG/benchmark/{verifier,mcp_client}.py
we need, instead of importing from EOG, so this package stays standalone.
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import EOG_ROOT
from .dataset import TaskRow

logger = logging.getLogger(__name__)


@dataclass
class VerifierResult:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    comparison_type: str = ""
    query: str = ""
    error: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerifierResult":
        return cls(
            name=str(d.get("name", "") or ""),
            passed=bool(d.get("passed", False)),
            expected=d.get("expected"),
            actual=d.get("actual"),
            comparison_type=str(d.get("comparison_type", "") or ""),
            query=str(d.get("query", "") or ""),
            error=d.get("error"),
        )


@dataclass
class VerificationReport:
    task_id: str
    overall_success: bool
    pass_rate: float
    reward: float           # alias for pass_rate, mirrors SkillsBench result.json
    n_passed: int
    n_total: int
    per_verifier: list[VerifierResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "overall_success": self.overall_success,
            "pass_rate": self.pass_rate,
            "rewards": {"reward": self.reward},  # SkillsBench-compatible
            "n_passed": self.n_passed,
            "n_total": self.n_total,
            "per_verifier": [vr.__dict__ for vr in self.per_verifier],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "VerificationReport":
        # The reward lives under rewards.reward (SkillsBench shape) but we
        # also accept a flat top-level "reward" for forward-compat.
        rewards = d.get("rewards") or {}
        reward = float(d.get("reward", rewards.get("reward", 0.0)) or 0.0)
        return cls(
            task_id=str(d.get("task_id", "") or ""),
            overall_success=bool(d.get("overall_success", False)),
            pass_rate=float(d.get("pass_rate", 0.0) or 0.0),
            reward=reward,
            n_passed=int(d.get("n_passed", 0) or 0),
            n_total=int(d.get("n_total", 0) or 0),
            per_verifier=[
                VerifierResult.from_dict(v) for v in (d.get("per_verifier") or []) if isinstance(v, dict)
            ],
        )


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------

def _new_database_id() -> str:
    timestamp = int(time.time() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
    return f"db_{timestamp}_{suffix}"


def _resolve_seed_path(seed_rel: str) -> Path:
    """Seed paths in the dataset are relative to EOG's repo root."""
    p = Path(seed_rel)
    if p.is_absolute() and p.exists():
        return p
    return EOG_ROOT / seed_rel


def seed_database_for_gym(gym: dict[str, Any]) -> str:
    """Create a fresh DB on the gym from its seed file; return database_id."""
    gym_url = gym["mcp_server_url"].rstrip("/")
    seed_rel = gym.get("seed_database_file", "")
    if not seed_rel:
        raise RuntimeError(
            f"gym {gym.get('mcp_server_name')!r} has no seed_database_file"
        )
    seed_path = _resolve_seed_path(seed_rel)
    if not seed_path.exists():
        raise FileNotFoundError(f"seed SQL not found: {seed_path}")

    sql_content = seed_path.read_text(encoding="utf-8")
    db_id = _new_database_id()
    payload = {
        "database_id": db_id,
        "name": f"evovle_{db_id}",
        "description": f"evovle_skills auto-seed from {seed_path.name}",
        "sql_content": sql_content,
    }
    timeout = max(1200, int(120 + len(sql_content) / 102400))
    with httpx.Client(timeout=timeout) as client:
        r = client.post(f"{gym_url}/api/seed-database", json=payload,
                        headers={"Content-Type": "application/json"})
        r.raise_for_status()
    return db_id


def delete_database_for_gym(gym: dict[str, Any], database_id: str) -> None:
    gym_url = gym["mcp_server_url"].rstrip("/")
    try:
        with httpx.Client(timeout=30) as client:
            client.request(
                "DELETE",
                f"{gym_url}/api/delete-database",
                json={"database_id": database_id},
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:  # noqa: BLE001 - best effort
        logger.warning("delete_database_for_gym failed for %s: %s", database_id, e)


def seed_databases(row: TaskRow) -> dict[str, str]:
    """Seed one DB per gym in the row. Returns {gym_name: database_id}."""
    out: dict[str, str] = {}
    tag = f"[{row.task_id}/{row.split}]"
    for gym in row.gym_servers_config:
        name = gym["mcp_server_name"]
        t0 = time.time()
        db_id = seed_database_for_gym(gym)
        out[name] = db_id
        logger.info("%s db.seed.ok: gym=%s  db_id=%s  (%.1fs, url=%s)",
                    tag, name, db_id, time.time() - t0,
                    gym.get("mcp_server_url", "?"))
    return out


def teardown_databases(row: TaskRow, db_ids: dict[str, str]) -> None:
    tag = f"[{row.task_id}/{row.split}]"
    for gym in row.gym_servers_config:
        name = gym["mcp_server_name"]
        if name in db_ids:
            delete_database_for_gym(gym, db_ids[name])
            logger.info("%s db.teardown: gym=%s  db_id=%s",
                        tag, name, db_ids[name])


# ---------------------------------------------------------------------------
# SQL execution
# ---------------------------------------------------------------------------

def normalize_context(context: dict[str, Any] | None) -> dict[str, str]:
    """Sanitize a row's ``gym_servers_config[*].context`` dict for use as
    HTTP headers (verifier or MCP bridge).

    Two upstream-config quirks this helper repairs:

    A. Whitespace-padded near-duplicate keys (observed: all 50 CSM rows)::

           {"x-user-email ": "",
            "x-user-email": "thomas.green@servicenow.com",
            " x-user-email": ""}

       Sending the whitespace-padded variants verbatim trips httpx's
       ``Illegal header name`` check and explodes the verifier; the MCP
       bridge silently picks the wrong key.

    B. Schema-padding empty placeholders.  ``builder/export_dataset.py``
       intentionally pads each row's ``context`` with ``""`` for keys
       absent from that particular task so all rows in a domain share a
       uniform schema (pyarrow JSON inference requires this when train
       and test JSONL are loaded together).  These empty values are
       legitimate -- they are NOT bugs -- and must be preserved so the
       gym still receives any header it expects to see, even if empty.

    Logic:

      * strip leading/trailing whitespace from every key,
      * drop entries whose stripped key is empty,
      * KEEP entries with empty values (legitimate schema padding),
      * on duplicate stripped keys, keep the FIRST non-empty value if
        any exists, else the empty placeholder (insertion order matches
        the source dict).

    Always returns a fresh dict; never mutates the input.
    """
    out: dict[str, str] = {}
    for k, v in (context or {}).items():
        sk = (k or "").strip() if isinstance(k, str) else ""
        if not sk:
            continue
        sv = "" if v is None else str(v)
        if sk not in out:
            out[sk] = sv
            continue
        # Stripped duplicate.  Upgrade an empty placeholder to a
        # non-empty value if one shows up later; never downgrade.
        if not out[sk] and sv:
            out[sk] = sv
    return out


def _context_headers(gym: dict[str, Any]) -> dict[str, str]:
    """Build the x-* headers EOG expects on /api/sql-runner."""
    headers: dict[str, str] = {}
    for k, v in normalize_context(gym.get("context")).items():
        if not k.lower().startswith("x-"):
            hk = "x-" + k.lower().replace("_", "-")
        else:
            hk = k
        headers[hk] = v
    user_info = gym.get("user_info") or {}
    if "user-id" not in headers and user_info.get("user_id"):
        headers["user-id"] = str(user_info["user_id"])
    return headers


def execute_sql(
    gym: dict[str, Any], database_id: str, query: str
) -> dict[str, Any]:
    """POST /api/sql-runner. Returns {success, result?, error?}."""
    gym_url = gym["mcp_server_url"].rstrip("/")
    api_url = f"{gym_url}/api/sql-runner"
    headers = {
        "Content-Type": "application/json",
        "x-database-id": database_id,
    }
    headers.update(_context_headers(gym))

    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(api_url, json={"query": query, "database_id": database_id},
                            headers=headers)
            r.raise_for_status()
            return {"success": True, "result": r.json()}
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text
        except Exception:
            pass
        return {"success": False,
                "error": f"HTTP {e.response.status_code}: {body[:500]}"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


def _extract_scalar(api_result: dict[str, Any]) -> Any:
    """Mirror EOG/_extract_value_from_sql_result for the common shapes."""
    if not isinstance(api_result, dict):
        return api_result

    for key in ("data", "rows"):
        if key not in api_result:
            continue
        rows = api_result[key]
        if isinstance(rows, list):
            if len(rows) == 1:
                row = rows[0]
                if isinstance(row, dict) and len(row) == 1:
                    return next(iter(row.values()))
                if isinstance(row, list) and len(row) == 1:
                    return row[0]
                return row
            return rows
        return rows

    if "result" in api_result and isinstance(api_result["result"], (str, int, float, bool)):
        return api_result["result"]
    return api_result


def _compare(actual: Any, expected: Any, kind: str) -> bool:
    """Mirror EnterpriseOps-Gym ``VerifierEngine._compare_values`` bit-for-bit.

    Ordering comparisons use RAW Python semantics on the un-coerced operands
    (no numeric casting): an int-vs-str raises ``TypeError`` -> fail, and
    str-vs-str is lexicographic -- matching the reference grader exactly. Any
    exception, or an unknown/unsupported comparison type, -> fail.
    """
    try:
        if kind == "equals":
            return actual == expected
        if kind == "greater_than":
            return actual > expected
        if kind == "less_than":
            return actual < expected
        if kind == "contains":
            return expected in str(actual)
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# Verifier engine
# ---------------------------------------------------------------------------

def _gym_for_verifier(row: TaskRow, verifier: dict[str, Any]) -> dict[str, Any]:
    name = verifier.get("gym_name")
    for gym in row.gym_servers_config:
        if gym.get("mcp_server_name") == name:
            return gym
    if row.gym_servers_config:
        return row.gym_servers_config[0]
    raise RuntimeError(f"no gym for verifier {verifier.get('name')!r}")


def run_verifiers(row: TaskRow, db_ids: dict[str, str]) -> VerificationReport:
    tag = f"[{row.task_id}/{row.split}]"
    results: list[VerifierResult] = []
    logger.info("%s verifier.start: n=%d  db_ids=%s",
                tag, len(row.verifiers), list(db_ids.keys()))
    for i, v in enumerate(row.verifiers, start=1):
        kind = v.get("verifier_type", "")
        name = v.get("name", "<unnamed>")

        if kind != "database_state":
            # We don't currently implement response_check / tool_execution
            # since they require either an LLM judge or the agent's tool log.
            # Mark them as skipped (counted as passed=False with an error).
            results.append(VerifierResult(
                name=name, passed=False,
                error=f"unsupported verifier type: {kind}",
            ))
            logger.warning("%s verifier[%d/%d] %r: SKIP (unsupported kind=%s)",
                           tag, i, len(row.verifiers), name, kind)
            continue

        cfg = v.get("validation_config", {}) or {}
        query = cfg.get("query", "")
        expected = cfg.get("expected_value")
        comp = cfg.get("comparison_type", "equals")
        if not query:
            results.append(VerifierResult(
                name=name, passed=False, error="empty query"))
            logger.warning("%s verifier[%d/%d] %r: SKIP (empty query)",
                           tag, i, len(row.verifiers), name)
            continue

        gym = _gym_for_verifier(row, v)
        gym_name = gym["mcp_server_name"]
        db_id = db_ids.get(gym_name, "")
        if not db_id:
            results.append(VerifierResult(
                name=name, passed=False, query=query,
                error=f"no database_id for gym {gym_name!r}"))
            logger.warning("%s verifier[%d/%d] %r: SKIP (no db_id for gym=%s)",
                           tag, i, len(row.verifiers), name, gym_name)
            continue

        t0 = time.time()
        sql = execute_sql(gym, db_id, query)
        sql_dt = time.time() - t0
        if not sql["success"]:
            err = sql.get("error", "")
            results.append(VerifierResult(
                name=name, passed=False, query=query,
                comparison_type=comp, expected=expected,
                error=err))
            logger.warning(
                "%s verifier[%d/%d] %r: SQL FAIL (%.2fs)  expected=%r  error=%s",
                tag, i, len(row.verifiers), name, sql_dt, expected, err,
            )
            continue

        actual = _extract_scalar(sql.get("result", {}))
        passed = _compare(actual, expected, comp)
        results.append(VerifierResult(
            name=name, passed=passed, query=query,
            comparison_type=comp, expected=expected, actual=actual,
        ))
        logger.info(
            "%s verifier[%d/%d] %r: %s (%.2fs)  expected %s %r  got %r",
            tag, i, len(row.verifiers), name,
            "PASS" if passed else "FAIL", sql_dt, comp, expected, actual,
        )

    n_total = len(results)
    n_passed = sum(1 for r in results if r.passed)
    pass_rate = n_passed / n_total if n_total else 0.0
    overall = (n_passed == n_total and n_total > 0)
    logger.info("%s verifier.summary: %d/%d passed  pass_rate=%.2f  overall=%s",
                tag, n_passed, n_total, pass_rate, overall)
    return VerificationReport(
        task_id=row.task_id,
        overall_success=overall,
        pass_rate=pass_rate,
        reward=pass_rate,
        n_passed=n_passed,
        n_total=n_total,
        per_verifier=results,
    )


def write_result_json(report: VerificationReport, trial_dir: Path) -> Path:
    """SkillsBench-compatible result.json (so summarize.py-style tools work)."""
    p = trial_dir / "result.json"
    p.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    return p
