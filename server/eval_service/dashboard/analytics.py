"""Materialized per-user analytics used by the real-time dashboard.

The raw request table is intentionally append-only and is already over a
gigabyte. Re-scanning it on every browser refresh is not viable, so this module
maintains compact aggregate tables in the same SQLite database. Live writes are
updated per analytics batch; ``rebuild()`` performs the one-time historical
backfill in a single ordered pass.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable

UNKNOWN_USER = "unknown user"

_AGGREGATE_TABLES = (
    "dashboard_user_totals",
    "dashboard_user_routes",
    "dashboard_user_datasets",
    "dashboard_user_benchmarks",
    "dashboard_user_ips",
    "dashboard_user_days",
)


def _user(value: object) -> str:
    return (str(value or "").strip()[:200] or UNKNOWN_USER)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dashboard_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dashboard_user_totals (
            eval_user            TEXT PRIMARY KEY,
            requests             INTEGER NOT NULL DEFAULT 0,
            successful_requests  INTEGER NOT NULL DEFAULT 0,
            sessions_created     INTEGER NOT NULL DEFAULT 0,
            grade_attempts       INTEGER NOT NULL DEFAULT 0,
            successful_grades    INTEGER NOT NULL DEFAULT 0,
            pass_rate_sum        REAL NOT NULL DEFAULT 0,
            pass_rate_count      INTEGER NOT NULL DEFAULT 0,
            first_request        TEXT,
            last_request         TEXT
        );
        CREATE TABLE IF NOT EXISTS dashboard_user_routes (
            eval_user TEXT NOT NULL,
            route     TEXT NOT NULL,
            requests  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (eval_user, route)
        );
        CREATE TABLE IF NOT EXISTS dashboard_user_datasets (
            eval_user TEXT NOT NULL,
            dataset   TEXT NOT NULL,
            sessions  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (eval_user, dataset)
        );
        CREATE TABLE IF NOT EXISTS dashboard_user_benchmarks (
            eval_user TEXT NOT NULL,
            benchmark TEXT NOT NULL,
            sessions  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (eval_user, benchmark)
        );
        CREATE TABLE IF NOT EXISTS dashboard_user_ips (
            eval_user TEXT NOT NULL,
            ip        TEXT NOT NULL,
            PRIMARY KEY (eval_user, ip)
        );
        CREATE TABLE IF NOT EXISTS dashboard_user_days (
            eval_user            TEXT NOT NULL,
            day                  TEXT NOT NULL,
            requests             INTEGER NOT NULL DEFAULT 0,
            successful_requests  INTEGER NOT NULL DEFAULT 0,
            sessions_created     INTEGER NOT NULL DEFAULT 0,
            grade_attempts       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (eval_user, day)
        );
        """
    )


def _upsert_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO dashboard_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def record_rows(conn: sqlite3.Connection, rows: Iterable[tuple], *, cursor: int) -> int:
    """Fold compact request rows into the dashboard tables.

    Row layout is ``(id, ts, ip, route, dataset, benchmark, status_code,
    success, pass_rate, eval_user)``. The caller owns the transaction.
    """
    totals: dict[str, dict[str, object]] = {}
    routes: Counter[tuple[str, str]] = Counter()
    datasets: Counter[tuple[str, str]] = Counter()
    benchmarks: Counter[tuple[str, str]] = Counter()
    ips: set[tuple[str, str]] = set()
    days: Counter[tuple[str, str, str]] = Counter()
    seen = 0

    for row in rows:
        (_request_id, ts, ip, route, dataset, benchmark, _status_code,
         success, pass_rate, eval_user) = row
        seen += 1
        user = _user(eval_user)
        success = 1 if success else 0
        is_session = 1 if route == "session.create" and success else 0
        is_grade = 1 if route == "grade" else 0
        successful_grade = 1 if is_grade and success else 0
        has_pass_rate = isinstance(pass_rate, (int, float))
        item = totals.setdefault(user, {
            "requests": 0,
            "successful": 0,
            "sessions": 0,
            "grades": 0,
            "successful_grades": 0,
            "pass_sum": 0.0,
            "pass_count": 0,
            "first": ts,
            "last": ts,
        })
        item["requests"] += 1
        item["successful"] += success
        item["sessions"] += is_session
        item["grades"] += is_grade
        item["successful_grades"] += successful_grade
        if has_pass_rate:
            item["pass_sum"] += float(pass_rate)
            item["pass_count"] += 1
        if ts and (not item["first"] or ts < item["first"]):
            item["first"] = ts
        if ts and (not item["last"] or ts > item["last"]):
            item["last"] = ts

        if route:
            routes[(user, str(route))] += 1
        if is_session and dataset:
            datasets[(user, str(dataset))] += 1
        if is_session and benchmark:
            benchmarks[(user, str(benchmark))] += 1
        if ip:
            ips.add((user, str(ip)))
        day = str(ts or "")[:10]
        if day:
            days[(user, day, "requests")] += 1
            days[(user, day, "successful")] += success
            days[(user, day, "sessions")] += is_session
            days[(user, day, "grades")] += is_grade

    conn.executemany(
        """
        INSERT INTO dashboard_user_totals (
            eval_user, requests, successful_requests, sessions_created,
            grade_attempts, successful_grades, pass_rate_sum, pass_rate_count,
            first_request, last_request
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(eval_user) DO UPDATE SET
            requests=dashboard_user_totals.requests+excluded.requests,
            successful_requests=dashboard_user_totals.successful_requests+excluded.successful_requests,
            sessions_created=dashboard_user_totals.sessions_created+excluded.sessions_created,
            grade_attempts=dashboard_user_totals.grade_attempts+excluded.grade_attempts,
            successful_grades=dashboard_user_totals.successful_grades+excluded.successful_grades,
            pass_rate_sum=dashboard_user_totals.pass_rate_sum+excluded.pass_rate_sum,
            pass_rate_count=dashboard_user_totals.pass_rate_count+excluded.pass_rate_count,
            first_request=MIN(dashboard_user_totals.first_request, excluded.first_request),
            last_request=MAX(dashboard_user_totals.last_request, excluded.last_request)
        """,
        [(
            user, item["requests"], item["successful"], item["sessions"],
            item["grades"], item["successful_grades"], item["pass_sum"],
            item["pass_count"], item["first"], item["last"],
        ) for user, item in totals.items()],
    )
    conn.executemany(
        "INSERT INTO dashboard_user_routes(eval_user,route,requests) VALUES(?,?,?) "
        "ON CONFLICT(eval_user,route) DO UPDATE SET requests=requests+excluded.requests",
        [(user, value, count) for (user, value), count in routes.items()],
    )
    conn.executemany(
        "INSERT INTO dashboard_user_datasets(eval_user,dataset,sessions) VALUES(?,?,?) "
        "ON CONFLICT(eval_user,dataset) DO UPDATE SET sessions=sessions+excluded.sessions",
        [(user, value, count) for (user, value), count in datasets.items()],
    )
    conn.executemany(
        "INSERT INTO dashboard_user_benchmarks(eval_user,benchmark,sessions) VALUES(?,?,?) "
        "ON CONFLICT(eval_user,benchmark) DO UPDATE SET sessions=sessions+excluded.sessions",
        [(user, value, count) for (user, value), count in benchmarks.items()],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO dashboard_user_ips(eval_user,ip) VALUES(?,?)",
        list(ips),
    )
    day_values: dict[tuple[str, str], list[int]] = {}
    for user, day, metric in days:
        values = day_values.setdefault((user, day), [0, 0, 0, 0])
        index = {"requests": 0, "successful": 1, "sessions": 2, "grades": 3}[metric]
        values[index] = days[(user, day, metric)]
    conn.executemany(
        """
        INSERT INTO dashboard_user_days(
            eval_user,day,requests,successful_requests,sessions_created,grade_attempts
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(eval_user,day) DO UPDATE SET
            requests=dashboard_user_days.requests+excluded.requests,
            successful_requests=dashboard_user_days.successful_requests+excluded.successful_requests,
            sessions_created=dashboard_user_days.sessions_created+excluded.sessions_created,
            grade_attempts=dashboard_user_days.grade_attempts+excluded.grade_attempts
        """,
        [(user, day, *values) for (user, day), values in day_values.items()],
    )
    _upsert_meta(conn, "cursor", cursor)
    _upsert_meta(conn, "updated_at", datetime.now(timezone.utc).isoformat())
    return seen


def _breakdown(conn: sqlite3.Connection, table: str, dimension: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    metric = "requests" if table == "dashboard_user_routes" else "sessions"
    for user, value, count in conn.execute(
        f"SELECT eval_user,{dimension},{metric} FROM {table} "
        f"ORDER BY eval_user,{metric} DESC"
    ):
        out.setdefault(user, {})[value] = count
    return out


def summary(
    conn: sqlite3.Connection,
    *,
    recent: int = 50,
    days: int = 60,
    issued_users: Iterable[str] = (),
) -> dict:
    ensure_schema(conn)
    recent = max(1, min(int(recent), 200))
    days = max(1, min(int(days), 366))
    meta = dict(conn.execute("SELECT key,value FROM dashboard_meta"))
    ip_counts = dict(conn.execute(
        "SELECT eval_user,COUNT(*) FROM dashboard_user_ips GROUP BY eval_user"
    ))
    routes = _breakdown(conn, "dashboard_user_routes", "route")
    datasets = _breakdown(conn, "dashboard_user_datasets", "dataset")
    benchmarks = _breakdown(conn, "dashboard_user_benchmarks", "benchmark")
    users = []
    for row in conn.execute(
        "SELECT eval_user,requests,successful_requests,sessions_created,"
        "grade_attempts,successful_grades,pass_rate_sum,pass_rate_count,"
        "first_request,last_request FROM dashboard_user_totals ORDER BY requests DESC"
    ):
        (user, requests, successful, sessions, grades, successful_grades,
         pass_sum, pass_count, first, last) = row
        users.append({
            "user": user,
            "requests": requests,
            "successful_requests": successful,
            "errors": requests - successful,
            "success_rate": round(successful / requests, 6) if requests else None,
            "unique_ips": ip_counts.get(user, 0),
            "sessions_created": sessions,
            "grade_attempts": grades,
            "successful_grades": successful_grades,
            "avg_pass_rate": round(pass_sum / pass_count, 6) if pass_count else None,
            "first_request": first,
            "last_request": last,
            "by_route": routes.get(user, {}),
            "sessions_by_dataset": datasets.get(user, {}),
            "sessions_by_benchmark": benchmarks.get(user, {}),
        })

    issued = sorted({_user(value) for value in issued_users} - {UNKNOWN_USER})
    present = {item["user"] for item in users}
    for user in issued:
        if user in present:
            continue
        users.append({
            "user": user,
            "requests": 0,
            "successful_requests": 0,
            "errors": 0,
            "success_rate": None,
            "unique_ips": 0,
            "sessions_created": 0,
            "grade_attempts": 0,
            "successful_grades": 0,
            "avg_pass_rate": None,
            "first_request": None,
            "last_request": None,
            "by_route": {},
            "sessions_by_dataset": {},
            "sessions_by_benchmark": {},
        })

    by_day: dict[str, dict[str, int]] = {}
    day_rows = conn.execute(
        "SELECT day,SUM(requests),SUM(successful_requests),SUM(sessions_created),"
        "SUM(grade_attempts) FROM dashboard_user_days GROUP BY day "
        "ORDER BY day DESC LIMIT ?", (days,),
    ).fetchall()
    for day, requests, successful, sessions, grades in reversed(day_rows):
        by_day[day] = {
            "requests": requests,
            "successful_requests": successful,
            "sessions_created": sessions,
            "grade_attempts": grades,
        }

    recent_rows = [
        {
            "ts": row[0], "user": _user(row[1]), "method": row[2],
            "route": row[3], "dataset": row[4], "benchmark": row[5],
            "status": row[6], "success": bool(row[7]), "latency_ms": row[8],
            "pass_rate": row[9],
        }
        for row in conn.execute(
            "SELECT ts,eval_user,method,route,dataset,benchmark,status_code,success,"
            "latency_ms,pass_rate FROM requests ORDER BY id DESC LIMIT ?", (recent,),
        )
    ]
    totals = {
        "requests": sum(item["requests"] for item in users),
        "successful_requests": sum(item["successful_requests"] for item in users),
        "sessions_created": sum(item["sessions_created"] for item in users),
        "grade_attempts": sum(item["grade_attempts"] for item in users),
        "successful_grades": sum(item["successful_grades"] for item in users),
        "known_users": sum(1 for item in users if item["user"] != UNKNOWN_USER),
        "active_known_users": sum(
            1 for item in users
            if item["user"] != UNKNOWN_USER and item["requests"] > 0
        ),
        "issued_users": len(issued),
        "unknown_requests": next(
            (item["requests"] for item in users if item["user"] == UNKNOWN_USER), 0
        ),
    }
    raw_max = conn.execute("SELECT COALESCE(MAX(id),0) FROM requests").fetchone()[0]
    cursor = int(meta.get("cursor", "0"))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "aggregation": {
            "ready": meta.get("ready") == "1",
            "cursor": cursor,
            "raw_requests": raw_max,
            "progress": round(cursor / raw_max, 6) if raw_max else 1.0,
            "updated_at": meta.get("updated_at"),
        },
        "totals": totals,
        "users": users,
        "by_day": by_day,
        "recent": recent_rows,
    }


def rebuild(db_path: str, *, batch_size: int = 50_000) -> None:
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        ensure_schema(conn)
        highwater = conn.execute("SELECT COALESCE(MAX(id),0) FROM requests").fetchone()[0]
        for table in _AGGREGATE_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM dashboard_meta")
        _upsert_meta(conn, "ready", 0)
        _upsert_meta(conn, "cursor", 0)
        conn.commit()
        cursor = 0
        processed = 0
        started = time.perf_counter()
        while cursor < highwater:
            rows = conn.execute(
                "SELECT id,ts,ip,route,dataset,benchmark,status_code,success,"
                "pass_rate,eval_user FROM requests WHERE id>? AND id<=? "
                "ORDER BY id LIMIT ?",
                (cursor, highwater, batch_size),
            ).fetchall()
            if not rows:
                break
            cursor = rows[-1][0]
            processed += record_rows(conn, rows, cursor=cursor)
            conn.commit()
            elapsed = max(time.perf_counter() - started, 0.001)
            print(
                f"dashboard backfill: {processed:,}/{highwater:,} "
                f"({processed/highwater:.1%}) at {processed/elapsed:,.0f} rows/s",
                flush=True,
            )
        _upsert_meta(conn, "cursor", highwater)
        _upsert_meta(conn, "ready", 1)
        _upsert_meta(conn, "updated_at", datetime.now(timezone.utc).isoformat())
        conn.commit()
        print(f"dashboard backfill complete: {processed:,} requests", flush=True)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build eval dashboard aggregates")
    parser.add_argument("db", help="path to tunnel analytics.db")
    parser.add_argument("--batch-size", type=int, default=50_000)
    args = parser.parse_args()
    rebuild(args.db, batch_size=max(1_000, args.batch_size))


if __name__ == "__main__":
    main()
