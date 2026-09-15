"""Expose the Evolving-Benchmarks Evaluation Service publicly via one ngrok tunnel.

This is a thin reverse proxy in front of the local FastAPI service (default
``http://localhost:8077``). It forwards every method/path/byte to the upstream
unchanged, while:

  * logging each request (authenticated user, IP + geo, route,
    dataset/benchmark/version/task/session, status, latency, grade pass_rate) to
    SQLite + CSV;
  * serving usage analytics + a cumulative-calls chart under ``/_tunnel/*``;
  * optionally gating the API behind an API key (``--api-key`` / env
    ``EVAL_SERVICE_API_KEY``) — strongly recommended for a public deployment,
    since the API provisions databases and runs graders.

Usage:
    python server/ngrok_tunnel.py --ngrok-token <token> \
        [--upstream-port 8077] [--proxy-port 9077] [--api-key <secret>]

The ngrok token may also come from ``$NGROK_AUTHTOKEN`` / ``$NGROK_TOKEN``.
"""

from __future__ import annotations

import argparse
import csv
import hmac
import json
import math
import os
import queue
import re
import shlex
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from eval_service.dashboard import analytics as _dashboard_analytics

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_EVAL_SERVICE_DIR = os.path.join(_THIS_DIR, "eval_service")
_ANALYTICS_DIR = os.path.join(_EVAL_SERVICE_DIR, ".tunnel_analytics")
DB_PATH = os.path.join(_ANALYTICS_DIR, "analytics.db")
CSV_PATH = os.path.join(_ANALYTICS_DIR, "analytics.csv")

UPSTREAM = os.environ.get("EVAL_SERVICE_UPSTREAM", "http://localhost:8077").rstrip("/")
PROXY_PORT = int(os.environ.get("EVAL_SERVICE_PROXY_PORT", "9077"))
# Reserved (static) ngrok domain. Published clients -- the Colab tutorials and
# the SDK's default base_url -- hardcode this hostname, so the tunnel must
# reclaim it on every restart instead of taking an ephemeral URL. Set
# NGROK_DOMAIN="" to force an ephemeral one.
NGROK_DOMAIN = os.environ.get("NGROK_DOMAIN", "educator-marrow-cultural.ngrok-free.dev").strip()
# When set, protected service calls and downloads must carry this key via
# `Authorization: Bearer <key>` or `x-api-key: <key>`. This is the single shared secret; per-user keys come from
# TOKENS below and either one opens the gate.
API_KEY = os.environ.get("EVAL_SERVICE_API_KEY", "").strip()
# Per-user keys issued by the demo site's MyAuthtoken page. Read-only and
# hot-reloading; see authtokens.py. Present store => the gate is on, with or
# without EVAL_SERVICE_API_KEY.
try:
    from eval_service import authtokens as _authtokens
    from eval_service.authtokens import SIGNUP_HINT, SIGNUP_URL, default_store
    TOKENS = default_store()
except Exception as _e:                                   # noqa: BLE001
    # Never let a broken token store take the tunnel down silently -- but never
    # let it fall open either. An unusable store denies /v1/* and says why.
    print(f"[tunnel] WARNING: token store unavailable ({type(_e).__name__}: {_e});"
          f" per-user API keys disabled", flush=True)
    TOKENS, SIGNUP_URL = None, "https://mas-orchestra.salesforceresearch.ai/mas_r1/demo/"
    SIGNUP_HINT = f"sign in at {SIGNUP_URL} and open MyAuthtoken to get a key"

    class _authtokens:                     # noqa: N801 - stand-in for the real module
        """Keeps 401 bodies well-formed even when the token module is unimportable."""

        @staticmethod
        def unauthorized_detail(presented, *, broken=False):
            what = ("the API key provided was not recognized" if presented
                    else "no API key provided")
            return (f"{what}. Sign in at {SIGNUP_URL} and open MyAuthtoken to get one, "
                    f"then send it as 'Authorization: Bearer <key>'.")


def _gate_on() -> bool:
    """Whether /v1/* requires a credential at all.

    Either source switches it on. Set ``EVAL_SERVICE_AUTH_DISABLE=1`` to run an
    intentionally open deployment even with a token store present -- explicit,
    because the alternative is discovering it by accident.
    """
    if os.environ.get("EVAL_SERVICE_AUTH_DISABLE", "").strip() in ("1", "true", "yes"):
        return False
    return bool(API_KEY) or (TOKENS is not None and TOKENS.enabled)
# Grades (esp. ALE) + DB seeding can be slow; give the upstream generous time.
UPSTREAM_TIMEOUT = int(os.environ.get("EVAL_SERVICE_PROXY_TIMEOUT_SEC", "900"))
# How long a DB call waits on a lock before giving up. The dashboards scan the
# whole (multi-million row) table, so the per-request writer needs more slack
# than sqlite3's 5s default.
DB_TIMEOUT = float(os.environ.get("EVAL_SERVICE_ANALYTICS_DB_TIMEOUT_SEC", "20"))

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1) + content-length is
# recomputed from the buffered body.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

_geo_cache: dict[str, tuple[str, str]] = {}
_geo_lock = threading.Lock()
_counts = {"eog": 0, "ale": 0, "other": 0}
_count_lock = threading.Lock()
_db_warn_lock = threading.Lock()
_db_warn_at = 0.0

# Analytics is written by one background thread, never on the request path.
# Inline it cost ~320ms per request on this host: a fresh sqlite3.connect()
# against the ~650MB file is ~180ms of that, because closing the last WAL
# connection checkpoints the log back into the main DB and unlinks -wal/-shm,
# and the next request rebuilds them. Geolocating a new IP could add 3s more.
# One long-lived connection plus batched commits removes all of it.
_LOG_QUEUE_MAX = int(os.environ.get("EVAL_SERVICE_ANALYTICS_QUEUE_MAX", "50000"))
_LOG_BATCH_MAX = 500
_log_queue: queue.Queue = queue.Queue(maxsize=_LOG_QUEUE_MAX)
_writer_stop = threading.Event()
_dropped_rows = 0

# Requests made before per-user keys were enabled, requests admitted by the
# shared deployment key, and deliberately open deployments cannot be attributed
# to a person. Keep one explicit value in every analytics sink so old and new
# records group together without exposing credential material.
UNKNOWN_USER = "unknown user"

_INSERT_SQL = (
    "INSERT INTO requests (ts, ip, city, country, method, path, route, "
    "dataset, benchmark, version, split, task_id, session_id, "
    "status_code, success, latency_ms, pass_rate, eval_user) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _geolocate_ip(ip: str) -> tuple[str, str]:
    """Resolve IP -> (city, country) via ip-api.com (free, no key, ~45 req/min)."""
    if ip in ("127.0.0.1", "::1", "localhost", ""):
        return "localhost", "localhost"
    with _geo_lock:
        if ip in _geo_cache:
            return _geo_cache[ip]
    try:
        resp = urlopen(f"http://ip-api.com/json/{ip}?fields=city,country", timeout=3)
        data = json.loads(resp.read())
        city = data.get("city") or "unknown"
        country = data.get("country") or "unknown"
    except Exception:
        city, country = "unknown", "unknown"
    with _geo_lock:
        _geo_cache[ip] = (city, country)
    return city, country


# --------------------------------------------------------------------------- #
# Analytics storage                                                           #
# --------------------------------------------------------------------------- #
_CSV_COLUMNS = [
    "ts", "ip", "city", "country", "method", "path", "route",
    "dataset", "benchmark", "version", "split", "task_id", "session_id",
    "status_code", "success", "latency_ms", "pass_rate", "eval_user",
]


def _connect_db() -> sqlite3.Connection:
    """Open the analytics DB in a mode where readers cannot stall writers.

    WAL is the load-bearing part: under the default rollback journal a single
    dashboard query holding a SHARED lock blocks the per-request INSERT, and the
    proxy then wedges permanently (the failed writer's connection leaks while
    still holding RESERVED, which nothing can release). Callers must close.
    """
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.execute(f"PRAGMA busy_timeout={int(DB_TIMEOUT * 1000)}")
    # Durability of an analytics row is worth far less than commit latency on
    # the request path; NORMAL still rules out corruption under WAL.
    conn.execute("PRAGMA synchronous=NORMAL")
    # A long dashboard scan blocks checkpointing, so the WAL can balloon while
    # one runs. Hand the space back once it completes instead of keeping the
    # high-water mark for the life of the process.
    conn.execute("PRAGMA journal_size_limit=67108864")
    return conn


def _warn_db_failure(exc: Exception) -> None:
    """Report a storage failure at most once a minute (never per request)."""
    global _db_warn_at
    now = time.time()
    with _db_warn_lock:
        if now - _db_warn_at < 60:
            return
        _db_warn_at = now
    print(f"  WARNING: analytics write failed ({type(exc).__name__}: {exc}); "
          f"requests are unaffected.", file=sys.stderr, flush=True)


def _init_storage() -> None:
    os.makedirs(_ANALYTICS_DIR, exist_ok=True)
    conn = _connect_db()
    # Persistent for the database file, so this survives every later connection.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            ip          TEXT NOT NULL,
            city        TEXT,
            country     TEXT,
            method      TEXT,
            path        TEXT,
            route       TEXT,
            dataset     TEXT,
            benchmark   TEXT,
            version     TEXT,
            split       TEXT,
            task_id     TEXT,
            session_id  TEXT,
            status_code INTEGER,
            success     INTEGER,
            latency_ms  INTEGER,
            pass_rate   REAL,
            eval_user   TEXT NOT NULL DEFAULT 'unknown user'
        )
        """
    )
    # SQLite can add a constant-default column without rewriting the historical
    # 1GB+ table. Existing requests therefore immediately read as unknown while
    # every request after this migration stores its resolved identity.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    if "eval_user" not in columns:
        conn.execute(
            "ALTER TABLE requests ADD COLUMN eval_user "
            "TEXT NOT NULL DEFAULT 'unknown user'"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON requests(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip ON requests(ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bench ON requests(benchmark)")
    _dashboard_analytics.ensure_schema(conn)
    dashboard_meta = dict(conn.execute("SELECT key,value FROM dashboard_meta"))
    if "ready" not in dashboard_meta:
        highwater = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM requests"
        ).fetchone()[0]
        _dashboard_analytics._upsert_meta(conn, "ready", 1 if highwater == 0 else 0)
        _dashboard_analytics._upsert_meta(conn, "cursor", 0)
    conn.commit()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    print(f"Analytics journal: {mode}")
    _init_csv_storage()


def _init_csv_storage() -> None:
    """Start a schema-correct CSV while preserving the pre-user export.

    Inserting a header field into the existing hundreds-of-megabytes CSV would
    require rewriting it. A cheap rename keeps that history intact; SQLite
    remains the continuous source of truth across the schema boundary.
    """
    if os.path.exists(CSV_PATH):
        try:
            with open(CSV_PATH, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except Exception:  # noqa: BLE001 - rotate an unreadable export safely
            header = []
        if header != _CSV_COLUMNS:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            root, ext = os.path.splitext(CSV_PATH)
            legacy = f"{root}.pre-user-{stamp}{ext}"
            n = 1
            while os.path.exists(legacy):
                legacy = f"{root}.pre-user-{stamp}-{n}{ext}"
                n += 1
            os.replace(CSV_PATH, legacy)
            print(f"Analytics CSV schema changed; preserved legacy export: {legacy}")
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_CSV_COLUMNS)


_SESSION_RE = re.compile(r"^/v1/sessions/([^/]+)")
def categorize(method: str, path_no_q: str) -> str:
    """Map (method, path) -> a coarse route label for analytics."""
    if (
        path_no_q == "/resources/skill.md"
        or path_no_q.startswith("/resources/evolve-eval/")
        or path_no_q.startswith("/resources/evolve-benchmark/")
    ):
        return "skill.download"
    if path_no_q == "/v1/health":
        return "health"
    if path_no_q == "/v1/benchmarks":
        return "benchmarks"
    if path_no_q == "/v1/tasks":
        return "tasks"
    if path_no_q == "/v1/sessions":
        return "session.create" if method == "POST" else "sessions"
    m = _SESSION_RE.match(path_no_q)
    if m:
        rest = path_no_q[m.end():]
        if rest in ("", "/"):
            return "session.delete" if method == "DELETE" else "session.get"
        if rest.startswith("/inputs"):
            return "inputs.list" if rest in ("/inputs", "/inputs/") else "inputs.get"
        if rest.startswith("/submit"):
            return "submit"
        if rest.startswith("/grade"):
            return "grade"
        if rest.startswith("/mcp"):
            return "mcp"
    return "other"


def requires_api_key(method: str, path_no_q: str) -> bool:
    """Whether the public tunnel must authenticate this service request."""
    return method != "OPTIONS" and (
        path_no_q.startswith("/v1/") or path_no_q.startswith("/resources/")
        or path_no_q == "/sdk" or path_no_q.startswith("/sdk/")
    )


def _extract_dims(route, path_no_q, query, req_json, resp_json):
    """Best-effort pull of analytics dimensions from the request/response."""
    d = {k: None for k in (
        "dataset", "benchmark", "version", "split", "task_id",
        "session_id", "pass_rate")}
    m = _SESSION_RE.match(path_no_q)
    if m:
        d["session_id"] = m.group(1)
    if route == "tasks" and query:
        q = parse_qs(query)
        for k in ("dataset", "benchmark", "version", "split"):
            if q.get(k):
                d[k] = q[k][0]
    if route == "session.create" and isinstance(req_json, dict):
        for k in ("dataset", "benchmark", "version", "split", "task_id"):
            if req_json.get(k) is not None:
                d[k] = req_json[k]
        if isinstance(resp_json, dict):
            d["session_id"] = resp_json.get("session_id") or d["session_id"]
            sel = (resp_json.get("task") or {}).get("selector") or {}
            d["task_id"] = resp_json.get("task", {}).get("task_id") or sel.get("task_id") or d["task_id"]
            d["benchmark"] = d["benchmark"] or sel.get("benchmark")
    if route == "grade" and isinstance(resp_json, dict):
        if isinstance(resp_json.get("pass_rate"), (int, float)):
            d["pass_rate"] = float(resp_json["pass_rate"])
        d["task_id"] = resp_json.get("task_id") or d["task_id"]
    if d["version"] is not None:
        d["version"] = str(d["version"])
    return d


def _normalise_user(value) -> str:
    """Return a bounded non-empty analytics label; never a key or token."""
    return (str(value or "").strip()[:200] or UNKNOWN_USER)


def _log_request(ip, method, path, route, dims, status_code, latency_ms, *,
                 eval_user=UNKNOWN_USER):
    """Hand the row to the writer thread. Never touches storage or the network.

    This runs before the response is relayed, so it must stay off the critical
    path: geolocation and the DB/CSV writes happen in :func:`_writer_loop`.
    """
    global _dropped_rows
    ts = datetime.now(timezone.utc).isoformat()
    success = 1 if (status_code and 200 <= status_code < 400) else 0
    try:
        _log_queue.put_nowait(
            (ts, ip, method, path, route, dims, status_code, success, latency_ms,
             _normalise_user(eval_user)))
    except queue.Full:
        # Storage is wedged or far behind. Losing analytics rows is always
        # preferable to blocking or failing the request they describe.
        _dropped_rows += 1
    bench = dims.get("benchmark")
    key = bench if bench in _counts else "other"
    with _count_lock:
        _counts[key] += 1


def _expand(item) -> tuple:
    """Turn a queued item into a full DB row (geolocation resolved here)."""
    (ts, ip, method, path, route, dims, status_code, success, latency_ms,
     eval_user) = item
    city, country = _geolocate_ip(ip)
    return (
        ts, ip, city, country, method, path, route,
        dims.get("dataset"), dims.get("benchmark"), dims.get("version"),
        dims.get("split"), dims.get("task_id"), dims.get("session_id"),
        status_code, success, latency_ms, dims.get("pass_rate"), eval_user,
    )


def _writer_loop() -> None:
    """Drain the queue onto one long-lived connection, committing per batch."""
    conn = None
    while True:
        try:
            item = _log_queue.get(timeout=0.5)
        except queue.Empty:
            if _writer_stop.is_set():
                break
            continue
        batch = [item]
        # Take whatever else is already waiting: batches form only under load,
        # so an idle tunnel still writes each row immediately.
        while len(batch) < _LOG_BATCH_MAX:
            try:
                batch.append(_log_queue.get_nowait())
            except queue.Empty:
                break
        try:
            rows = [_expand(i) for i in batch]
        except Exception as e:  # noqa: BLE001 - a bad row must not kill the writer
            _warn_db_failure(e)
            continue
        try:
            if conn is None:
                conn = _connect_db()
            conn.executemany(_INSERT_SQL, rows)
            cursor = conn.execute("SELECT COALESCE(MAX(id),0) FROM requests").fetchone()[0]
            compact_rows = [
                (None, row[0], row[1], row[6], row[7], row[8], row[13],
                 row[14], row[16], row[17])
                for row in rows
            ]
            # Raw rows and their materialized dashboard totals commit together,
            # so a refresh never observes one without the other.
            _dashboard_analytics.record_rows(conn, compact_rows, cursor=cursor)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            _warn_db_failure(e)
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None  # rebuild on the next batch
        try:
            with open(CSV_PATH, "a", newline="") as f:
                csv.writer(f).writerows(rows)
        except Exception as e:  # noqa: BLE001
            _warn_db_failure(e)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _start_writer() -> threading.Thread:
    t = threading.Thread(target=_writer_loop, name="analytics-writer", daemon=True)
    t.start()
    return t


def _stop_writer(t: threading.Thread | None, timeout: float = 10.0) -> None:
    """Let the writer drain what's queued before the process exits."""
    if t is None:
        return
    _writer_stop.set()
    t.join(timeout=timeout)
    if _dropped_rows:
        print(f"  {_dropped_rows} analytics rows dropped (queue full).",
              file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Analytics queries                                                           #
# --------------------------------------------------------------------------- #
_FMT = {
    "hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d",
    "week": "%Y-W%W", "month": "%Y-%m", "year": "%Y",
}


def _query_analytics(group_by="day"):
    fmt = _FMT.get(group_by, _FMT["day"])
    conn = _connect_db()
    try:
        cur = conn.cursor()

        def rows(sql, *a):
            cur.execute(sql, a)
            return cur.fetchall()

        total = rows("SELECT COUNT(*) FROM requests")[0][0]
        unique = rows("SELECT COUNT(DISTINCT ip) FROM requests")[0][0]
        by_route = {r[0]: r[1] for r in rows(
            "SELECT route, COUNT(*) FROM requests GROUP BY route ORDER BY 2 DESC")}
        by_benchmark = {r[0]: r[1] for r in rows(
            "SELECT benchmark, COUNT(*) FROM requests WHERE benchmark IS NOT NULL "
            "GROUP BY benchmark ORDER BY 2 DESC")}
        sessions_created = rows(
            "SELECT COUNT(*) FROM requests WHERE route='session.create' AND success=1")[0][0]
        grades = rows("SELECT COUNT(*) FROM requests WHERE route='grade' AND success=1")[0][0]
        avg_pass = rows("SELECT AVG(pass_rate) FROM requests WHERE pass_rate IS NOT NULL")[0][0]
        overview = [
            {"period": r[0], "total_requests": r[1], "unique_ips": r[2]}
            for r in rows(
                f"SELECT strftime('{fmt}', ts), COUNT(*), COUNT(DISTINCT ip) "
                "FROM requests GROUP BY 1 ORDER BY 1")
        ]
        by_bench_time = [
            {"period": r[0], "benchmark": r[1], "requests": r[2]}
            for r in rows(
                f"SELECT strftime('{fmt}', ts), benchmark, COUNT(*) FROM requests "
                "WHERE benchmark IS NOT NULL GROUP BY 1,2 ORDER BY 1,2")
        ]
        top_ips = [
            {"ip": r[0], "city": r[1], "country": r[2], "requests": r[3]}
            for r in rows("SELECT ip, city, country, COUNT(*) FROM requests "
                          "GROUP BY ip ORDER BY 4 DESC LIMIT 20")
        ]
        by_country = [
            {"country": r[0], "unique_ips": r[1], "requests": r[2]}
            for r in rows("SELECT country, COUNT(DISTINCT ip), COUNT(*) FROM requests "
                          "WHERE country IS NOT NULL GROUP BY country ORDER BY 2 DESC")
        ]
        top_tasks = [
            {"task_id": r[0], "requests": r[1]}
            for r in rows("SELECT task_id, COUNT(*) FROM requests WHERE task_id IS NOT NULL "
                          "GROUP BY task_id ORDER BY 2 DESC LIMIT 20")
        ]
        # Per-user totals answer both who is using the service and how. The
        # constant default on eval_user makes all pre-migration rows part of the
        # same explicit unknown bucket without an expensive historical rewrite.
        user_rows = rows(
            "SELECT COALESCE(NULLIF(eval_user, ''), ?), COUNT(*), "
            "COALESCE(SUM(success), 0), COUNT(DISTINCT ip), "
            "SUM(CASE WHEN route='session.create' AND success=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN route='grade' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN route='grade' AND success=1 THEN 1 ELSE 0 END), "
            "AVG(pass_rate), MIN(ts), MAX(ts) "
            "FROM requests GROUP BY 1 ORDER BY 2 DESC",
            UNKNOWN_USER,
        )
        by_user = [
            {
                "user": r[0],
                "requests": r[1],
                "successful_requests": r[2],
                "unique_ips": r[3],
                "sessions_created": r[4],
                "grade_attempts": r[5],
                "successful_grades": r[6],
                "avg_pass_rate": round(r[7], 4) if r[7] is not None else None,
                "first_request": r[8],
                "last_request": r[9],
                "by_route": {},
                "sessions_by_dataset": {},
                "sessions_by_benchmark": {},
            }
            for r in user_rows
        ]
        users = {row["user"]: row for row in by_user}

        # One grouped scan supplies all three breakdowns. Avoiding separate
        # route/dataset/benchmark scans matters for the multi-million-row DB.
        detail_rows = rows(
            "SELECT COALESCE(NULLIF(eval_user, ''), ?), route, dataset, benchmark, "
            "COUNT(*), COALESCE(SUM(success), 0) FROM requests "
            "GROUP BY 1, 2, 3, 4 ORDER BY 1, 5 DESC",
            UNKNOWN_USER,
        )
        for user, route, dataset, benchmark, count, successes in detail_rows:
            if route is not None:
                route_counts = users[user]["by_route"]
                route_counts[route] = route_counts.get(route, 0) + count
            if route == "session.create":
                if dataset is not None:
                    counts = users[user]["sessions_by_dataset"]
                    counts[dataset] = counts.get(dataset, 0) + successes
                if benchmark is not None:
                    counts = users[user]["sessions_by_benchmark"]
                    counts[benchmark] = counts.get(benchmark, 0) + successes
        for row in by_user:
            for field in ("by_route", "sessions_by_dataset", "sessions_by_benchmark"):
                row[field] = dict(sorted(
                    row[field].items(), key=lambda item: item[1], reverse=True
                ))
    finally:
        conn.close()
    return {
        "group_by": group_by,
        "total_requests": total,
        "total_unique_ips": unique,
        "sessions_created": sessions_created,
        "grades": grades,
        "avg_pass_rate": round(avg_pass, 4) if avg_pass is not None else None,
        "by_route": by_route,
        "by_benchmark": by_benchmark,
        "overview": overview,
        "by_benchmark_over_time": by_bench_time,
        "top_ips": top_ips,
        "by_country": by_country,
        "top_tasks": top_tasks,
        "by_user": by_user,
    }


def _query_chart_data(group_by="day"):
    """Cumulative request counts per benchmark over time (+ a total series)."""
    fmt = _FMT.get(group_by, _FMT["day"])
    conn = _connect_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT strftime('{fmt}', ts) AS period, "
            "COALESCE(benchmark, 'other') AS bench, COUNT(*) "
            "FROM requests GROUP BY period, bench ORDER BY period"
        )
        data = cur.fetchall()
    finally:
        conn.close()

    series_names = sorted({r[1] for r in data})
    periods = sorted({r[0] for r in data})
    per = {s: {} for s in series_names}
    for period, bench, cnt in data:
        per[bench][period] = cnt

    cumulative = {s: [] for s in series_names}
    totals = []
    running = {s: 0 for s in series_names}
    for p in periods:
        ptot = 0
        for s in series_names:
            running[s] += per[s].get(p, 0)
            cumulative[s].append(running[s])
            ptot += running[s]
        totals.append(ptot)
    return {"labels": periods, "series": cumulative, "total": totals}


# --------------------------------------------------------------------------- #
# Standalone SVG chart (no JS) — embeddable in READMEs / dashboards           #
# --------------------------------------------------------------------------- #
_SERIES_COLORS = {
    "eog": "#38bdf8", "ale": "#ec4899", "other": "#94a3b8", "total": "#f59e0b",
}


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _fmt_number(n):
    n = int(n) if n == int(n) else n
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}k".replace(".0k", "k")
    return str(int(n))


def _nice_ticks(vmax, count=5):
    if vmax <= 0:
        return [0, 1]
    raw_step = vmax / count
    mag = 10 ** math.floor(math.log10(raw_step)) if raw_step > 0 else 1
    norm = raw_step / mag
    step = (1 if norm < 1.5 else 2 if norm < 3 else 5 if norm < 7 else 10) * mag
    upper = math.ceil(vmax / step) * step
    ticks, v = [], 0.0
    while v <= upper + 1e-9:
        ticks.append(int(v) if abs(v - round(v)) < 1e-9 else round(v, 2))
        v += step
    return ticks


def _smooth_path(points):
    if not points:
        return ""
    if len(points) == 1:
        return f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    parts = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]
    for i in range(1, len(points)):
        p0 = points[i - 2] if i >= 2 else points[i - 1]
        p1, p2 = points[i - 1], points[i]
        p3 = points[i + 1] if i + 1 < len(points) else points[i]
        cp1x = p1[0] + (p2[0] - p0[0]) / 6
        cp1y = p1[1] + (p2[1] - p0[1]) / 6
        cp2x = p2[0] - (p3[0] - p1[0]) / 6
        cp2y = p2[1] - (p3[1] - p1[1]) / 6
        parts.append(f"C {cp1x:.2f} {cp1y:.2f} {cp2x:.2f} {cp2y:.2f} {p2[0]:.2f} {p2[1]:.2f}")
    return " ".join(parts)


def _legend_anchor(pos, ml, mt, cw, ch, n_items, lw=140):
    lh = n_items * 22 + 10
    pad = 14
    if pos == "top-right":
        return ml + cw - lw - pad, mt + pad
    if pos == "bottom-left":
        return ml + pad, mt + ch - lh - pad
    if pos == "bottom-right":
        return ml + cw - lw - pad, mt + ch - lh - pad
    return ml + pad, mt + pad


def _render_chart_svg(data, theme="light", legend_pos="top-left", group_by="day"):
    if theme == "dark":
        bg, text_c, sub_c = "#0d1117", "#e6edf3", "#8b949e"
        axis_c, grid_c, axis_line = "#8b949e", "#21262d", "#30363d"
        legend_bg, legend_border = "rgba(22,27,34,0.85)", "#30363d"
    else:
        bg, text_c, sub_c = "#ffffff", "#1f2328", "#656d76"
        axis_c, grid_c, axis_line = "#656d76", "#eaecef", "#afb8c1"
        legend_bg, legend_border = "rgba(255,255,255,0.9)", "#d0d7de"

    W, H = 800, 533
    ML, MR, MT, MB = 70, 50, 80, 80
    cw, ch = W - ML - MR, H - MT - MB
    labels = data.get("labels", []) or []
    series_map = data.get("series", {}) or {}
    totals = data.get("total", []) or []
    series = [(m, series_map[m]) for m in sorted(series_map.keys())]
    if totals:
        series.append(("total", totals))

    p = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="-apple-system, BlinkMacSystemFont, '
        f"'Segoe UI', Roboto, Helvetica, Arial, sans-serif\">",
        f'<rect width="{W}" height="{H}" fill="{bg}"/>',
        f'<text x="{ML}" y="38" font-size="20" font-weight="700" fill="{text_c}">'
        f'Eval Service — API Call History</text>',
        f'<text x="{ML}" y="60" font-size="12" fill="{sub_c}">'
        f'Cumulative requests over time ({_xml_escape(group_by)})</text>',
    ]
    if not labels or not series:
        p.append(f'<text x="{W/2}" y="{H/2}" font-size="16" fill="{sub_c}" '
                 f'text-anchor="middle">No data yet</text></svg>')
        return "\n".join(p)

    max_y = max((max(v) for _, v in series if v), default=1) or 1
    y_ticks = _nice_ticks(max_y, count=5)
    y_max = y_ticks[-1] if y_ticks[-1] > 0 else 1
    n = len(labels)
    x_pos = lambda i: ML + cw / 2 if n == 1 else ML + i * cw / (n - 1)
    y_pos = lambda v: MT + ch - (v / y_max) * ch

    if n <= 8:
        x_ticks = list(range(n))
    else:
        step = max(1, (n - 1) // 6)
        x_ticks = list(range(0, n, step))
        if x_ticks[-1] != n - 1:
            x_ticks.append(n - 1)

    for t in y_ticks:
        y = y_pos(t)
        p.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{W-MR}" y2="{y:.1f}" '
                 f'stroke="{grid_c}" stroke-width="1"/>')
        p.append(f'<text x="{ML-10}" y="{y+4:.1f}" font-size="11" fill="{axis_c}" '
                 f'text-anchor="end">{_fmt_number(t)}</text>')
    for i in x_ticks:
        x = x_pos(i)
        p.append(f'<line x1="{x:.1f}" y1="{MT+ch}" x2="{x:.1f}" y2="{MT+ch+5}" '
                 f'stroke="{axis_line}" stroke-width="1"/>')
        p.append(f'<text x="{x:.1f}" y="{MT+ch+20}" font-size="11" fill="{axis_c}" '
                 f'text-anchor="middle">{_xml_escape(labels[i])}</text>')
    p.append(f'<line x1="{ML}" y1="{MT}" x2="{ML}" y2="{MT+ch}" stroke="{axis_line}" stroke-width="1"/>')
    p.append(f'<line x1="{ML}" y1="{MT+ch}" x2="{W-MR}" y2="{MT+ch}" stroke="{axis_line}" stroke-width="1"/>')

    for name, vals in series:
        color = _SERIES_COLORS.get(name, "#94a3b8")
        pts = [(x_pos(i), y_pos(v)) for i, v in enumerate(vals)]
        dash = ' stroke-dasharray="6,4"' if name == "total" else ""
        p.append(f'<path d="{_smooth_path(pts)}" fill="none" stroke="{color}" '
                 f'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"{dash}/>')
        for x, y in pts:
            p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}"/>')

    lx, ly = _legend_anchor(legend_pos, ML, MT, cw, ch, len(series))
    lw, lh = 140, len(series) * 22 + 10
    p.append(f'<rect x="{lx-8}" y="{ly-6}" width="{lw}" height="{lh}" rx="6" '
             f'fill="{legend_bg}" stroke="{legend_border}" stroke-width="1"/>')
    for idx, (name, _) in enumerate(series):
        color = _SERIES_COLORS.get(name, "#94a3b8")
        row_y = ly + idx * 22 + 6
        p.append(f'<circle cx="{lx+6}" cy="{row_y+4}" r="5" fill="{color}"/>')
        p.append(f'<text x="{lx+20}" y="{row_y+8}" font-size="12" fill="{text_c}">'
                 f'{_xml_escape(name)}</text>')
    p.append("</svg>")
    return "\n".join(p)


_CHART_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eval Service — Call History</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:#0f172a;color:#e2e8f0;padding:2rem;display:flex;flex-direction:column;align-items:center}
  h1{font-size:1.6rem;font-weight:700;margin-bottom:.25rem}
  .subtitle{font-size:.85rem;color:#64748b;margin-bottom:1.5rem}
  .controls{margin-bottom:1.5rem;display:flex;gap:.5rem}
  .controls a{padding:.4rem .9rem;border-radius:6px;font-size:.8rem;text-decoration:none;
              color:#94a3b8;background:#1e293b;border:1px solid #334155;transition:all .2s}
  .controls a.active,.controls a:hover{color:#f1f5f9;border-color:#38bdf8;background:#0f2a4a}
  .chart-wrap{width:100%;max-width:960px;background:#1e293b;border-radius:12px;
              padding:1.5rem;border:1px solid #334155}
  canvas{width:100%!important}
</style>
</head>
<body>
<h1>Eval Service — Call History</h1>
<p class="subtitle">Cumulative API requests over time, by benchmark</p>
<div class="controls">
  <a href="/_tunnel/chart?group_by=hour">Hourly</a>
  <a href="/_tunnel/chart?group_by=day">Daily</a>
  <a href="/_tunnel/chart?group_by=week">Weekly</a>
  <a href="/_tunnel/chart?group_by=month">Monthly</a>
</div>
<div class="chart-wrap"><canvas id="chart"></canvas></div>
<script>
const data = __CHART_DATA__;
const groupBy = "__GROUP_BY__";
document.querySelectorAll('.controls a').forEach(a => {
  if (a.href.includes('group_by=' + groupBy)) a.classList.add('active');
});
const colors = {eog:'#38bdf8', ale:'#f472b6', other:'#94a3b8', total:'#facc15'};
const datasets = [];
Object.entries(data.series).forEach(([name, values]) => {
  datasets.push({label: name, data: values, borderColor: colors[name] || '#94a3b8',
    backgroundColor: 'transparent', borderWidth: 2.5, pointRadius: 3,
    pointBackgroundColor: colors[name] || '#94a3b8', tension: 0.3});
});
datasets.push({label: 'total', data: data.total, borderColor: colors.total,
  backgroundColor: 'transparent', borderWidth: 2, borderDash: [6,3],
  pointRadius: 2, pointBackgroundColor: colors.total, tension: 0.3});
new Chart(document.getElementById('chart'), {
  type: 'line', data: {labels: data.labels, datasets},
  options: {responsive: true,
    plugins: {legend: {labels: {color: '#e2e8f0', font: {size: 13}, usePointStyle: true, pointStyle: 'circle'}},
              tooltip: {mode: 'index', intersect: false}},
    scales: {x: {ticks: {color: '#64748b', maxRotation: 45}, grid: {color: '#1e293b'}},
             y: {ticks: {color: '#64748b'}, grid: {color: '#334155'},
                 title: {display: true, text: 'Cumulative Requests', color: '#94a3b8'}}},
    interaction: {mode: 'nearest', axis: 'x', intersect: false}}
});
</script>
</body>
</html>"""


def _landing_html(public_url: str, auth_required: bool) -> str:
    base = public_url.rstrip("/")
    broken = TOKENS is not None and TOKENS.enabled and not TOKENS.healthy
    if broken:
        lock = ("<b>temporarily closed</b> — the API key store on this deployment is "
                "unreadable, so no request can be authorized until it is fixed")
    elif auth_required:
        lock = (f'<b>API key required</b> — sign in at <a href="{SIGNUP_URL}">the demo</a> '
                f'and open <b>MyAuthtoken</b> to get one, then send '
                f'<code>Authorization: Bearer &lt;key&gt;</code>')
    else:
        lock = "<b>Open access</b> (no API key configured)"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Evolving-Benchmarks Eval Service</title>
<style>
 body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
   background:#0f172a;color:#e2e8f0;max-width:760px;margin:0 auto;padding:2.5rem 1.5rem;line-height:1.55}}
 h1{{font-size:1.5rem}} code{{background:#1e293b;padding:.1rem .35rem;border-radius:4px;font-size:.85em}}
 a{{color:#38bdf8;text-decoration:none}} a:hover{{text-decoration:underline}}
 .card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:1rem 1.25rem;margin:1rem 0}}
 li{{margin:.25rem 0}}
</style></head><body>
<h1>Evolving-Benchmarks — Evaluation as a Service</h1>
<p>Bring-Your-Own-Agent evaluation for EOG (MCP) and ALE (file-sandbox) tasks.
Access: {lock}.</p>
<div class="card"><b>Get started</b>
<ul>
 <li>Interactive docs: <a href="{base}/docs">{base}/docs</a></li>
 <li>Evaluation skill installer: <code>{base}/resources/evolve-eval/install.sh</code> <small>(needs a key)</small></li>
 <li>Evaluation skill manifest: <code>{base}/resources/evolve-eval/manifest.json</code> <small>(needs a key)</small></li>
 <li>Benchmark-construction skill: <code>{base}/resources/evolve-benchmark/install.sh</code> <small>(needs a key)</small></li>
 <li>Health: <code>{base}/v1/health</code> <small>(needs a key)</small></li>
 <li>Benchmarks: <code>{base}/v1/benchmarks</code> <small>(needs a key)</small></li>
 <li>Liveness, no key: <a href="{base}/_tunnel/health">{base}/_tunnel/health</a></li>
 <li>Python SDK base_url: <code>{base}</code></li>
</ul></div>
<div class="card"><b>Usage monitor (SDK / API)</b>
<ul>
 <li>JSON (popularity + bottlenecks): <a href="{base}/v1/usage">{base}/v1/usage</a></li>
</ul></div>
<div class="card"><b>Tunnel usage analytics</b>
<ul>
 <li>Dashboard chart: <a href="{base}/_tunnel/chart">{base}/_tunnel/chart</a></li>
 <li>SVG chart: <a href="{base}/_tunnel/chart.svg">{base}/_tunnel/chart.svg</a></li>
 <li>JSON analytics: <a href="{base}/_tunnel/analytics?group_by=day">{base}/_tunnel/analytics</a></li>
 <li>Live counts: <a href="{base}/_tunnel/counts">{base}/_tunnel/counts</a></li>
</ul></div>
</body></html>"""


# --------------------------------------------------------------------------- #
# Reverse proxy                                                               #
# --------------------------------------------------------------------------- #
_PUBLIC_URL = ""   # set in main(); shown on the landing page
_PUBLIC_HOST = ""  # netloc of _PUBLIC_URL; forwarded so the service can build
                   # publicly reachable MCP proxy URLs (X-Forwarded-Host).


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "EvalServiceTunnel/1.0"

    # -- helpers ----------------------------------------------------------- #
    def _client_ip(self):
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _write(self, status, body: bytes, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, extra=None):
        self._write(
            status,
            json.dumps(obj, indent=2).encode(),
            "application/json",
            extra=extra,
        )

    def _presented_key(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return self.headers.get("x-api-key", "").strip()

    def _has_api_key(self):
        """True when the caller may proceed. Sets ``self.identity`` when known.

        Either credential opens the gate: the shared ``EVAL_SERVICE_API_KEY``, or a
        per-user key from the demo site. A configured-but-unreadable token store
        denies everything -- see the fail-closed note in authtokens.py.
        """
        self.identity = None
        if not _gate_on():
            return True
        key = self._presented_key()
        if not key:
            return False
        if API_KEY and hmac.compare_digest(key, API_KEY):
            return True
        if TOKENS is not None and TOKENS.enabled:
            self.identity = TOKENS.verify(key)
            return self.identity is not None
        return False

    def _deny(self, path):
        """Reject with the one thing the caller actually needs: where to get a key.

        The guidance goes in ``detail`` because that is the field every client
        surfaces -- the SDK's ``ServiceError`` reads only ``detail``, so a signup
        link parked in a sibling field is one the user never sees. The structured
        fields are kept alongside it for programmatic handling.
        """
        broken = TOKENS is not None and TOKENS.enabled and not TOKENS.healthy
        status = 503 if broken else 401
        detail = _authtokens.unauthorized_detail(
            bool(self._presented_key()), broken=broken
        )
        if path in (
            "/resources/evolve-benchmark/install.sh",
            "/resources/evolve-eval/install.sh",
        ):
            # `curl -f` suppresses every 4xx body. Return no installer content;
            # return only a denial stub so `curl -fsSL ... | bash` can explain
            # where to get a key and then fail locally.
            body = (
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' {shlex.quote('Authentication required: ' + detail)} >&2\n"
                "exit 22\n"
            ).encode()
            self._write(200, body, "text/x-shellscript; charset=utf-8", {
                "Cache-Control": "no-store",
                "WWW-Authenticate": 'Bearer realm="eval-service"',
                "X-Eval-Auth-Required": "true",
            })
            _log_request(
                self._client_ip(), self.command, self.path,
                categorize(self.command, path), {}, status, 0,
                eval_user=UNKNOWN_USER,
            )
            return
        self._json(status, {
            "error": "unauthorized",
            "detail": detail,
            "how_to_get_a_key": SIGNUP_HINT,
            "signup_url": SIGNUP_URL,
        }, {"WWW-Authenticate": 'Bearer realm="eval-service"'})
        _log_request(self._client_ip(), self.command, self.path,
                     categorize(self.command, path), {}, status, 0,
                     eval_user=UNKNOWN_USER)

    def _analytics_user(self) -> str:
        identity = getattr(self, "identity", None)
        return _normalise_user(identity.label if identity is not None else None)

    # -- dispatch ---------------------------------------------------------- #
    def _handle(self):
        # A BaseHTTPRequestHandler instance can serve several keep-alive
        # requests. Never let an authenticated request's identity bleed into a
        # later ungated request (for example OPTIONS).
        self.identity = None
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""

        parsed = urlparse(self.path)
        path = parsed.path
        query = parsed.query

        # Tunnel-owned endpoints (served locally, never forwarded).
        if path == "/_tunnel/health":
            return self._json(200, {"ok": True, "upstream": UPSTREAM})
        # Operator dashboards are deliberately localhost-only. Do not forward
        # either the standalone path or the service's older HTML dashboard
        # through the public tunnel.
        if (path == "/dashboard" or path.startswith("/dashboard/")
                or path == "/v1/usage/dashboard"):
            return self._json(404, {"error": "not found"})
        if path in ("/", "/_tunnel", "/_tunnel/"):
            html = _landing_html(_PUBLIC_URL or f"http://localhost:{PROXY_PORT}", _gate_on())
            return self._write(200, html.encode(), "text/html; charset=utf-8")
        if path.startswith("/_tunnel/"):
            return self._serve_tunnel(path, query)

        # API key gate for the real service. Every /v1/* route, skill download,
        # SDK manifest, and SDK wheel is gated. Health is included: a rejected
        # key cannot use this service, so
        # answering "ok" would be a lie in the only sense the caller cares about.
        # It also stops an anonymous GET from disclosing the data root, session
        # counts and task-set internals. Liveness monitors that cannot carry a
        # credential should poll /_tunnel/health, which is served above.
        if requires_api_key(self.command, path) and not self._has_api_key():
            return self._deny(path)

        self._forward(path, query, body)

    def _serve_tunnel(self, path, query):
        if not self._has_api_key():
            return self._json(401, {"error": "unauthorized",
                                    "detail": "analytics require an API key",
                                    "how_to_get_a_key": SIGNUP_HINT})
        params = parse_qs(query)
        group_by = params.get("group_by", ["day"])[0]
        if path == "/_tunnel/analytics":
            return self._json(200, _query_analytics(group_by))
        if path == "/_tunnel/counts":
            with _count_lock:
                counts = dict(_counts)
            return self._json(200, {"by_benchmark": counts, "total": sum(counts.values())})
        if path == "/_tunnel/chart.svg":
            theme = params.get("theme", ["light"])[0].lower()
            legend = params.get("legend", ["top-left"])[0].lower()
            svg = _render_chart_svg(_query_chart_data(group_by), theme=theme,
                                    legend_pos=legend, group_by=group_by)
            return self._write(200, svg.encode("utf-8"), "image/svg+xml; charset=utf-8",
                               {"Cache-Control": "no-cache, no-store, must-revalidate"})
        if path == "/_tunnel/chart":
            html = (_CHART_HTML.replace("__CHART_DATA__", json.dumps(_query_chart_data(group_by)))
                    .replace("__GROUP_BY__", group_by))
            return self._write(200, html.encode(), "text/html; charset=utf-8")
        return self._json(404, {"error": "not found"})

    def _forward(self, path, query, body):
        route = categorize(self.command, path)
        eval_user = self._analytics_user()
        target = f"{UPSTREAM}{self.path}"
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _HOP_BY_HOP and k.lower() != "x-eval-user"}
        headers["Host"] = urlparse(UPSTREAM).netloc
        headers["X-Forwarded-For"] = self._client_ip()
        headers["X-Forwarded-Proto"] = "https"
        # Let the service advertise a publicly reachable base (for MCP proxy URLs).
        if _PUBLIC_HOST:
            headers["X-Forwarded-Host"] = _PUBLIC_HOST
        # Attribute the call to whoever's key opened the gate, so a multi-user
        # deployment can tell its users apart in the logs. Any inbound value was
        # dropped above, so this cannot be spoofed by the client.
        identity = getattr(self, "identity", None)
        if identity is not None:
            headers["X-Eval-User"] = identity.label

        req = Request(target, data=(body or None), headers=headers, method=self.command)
        t0 = time.time()
        try:
            resp = urlopen(req, timeout=UPSTREAM_TIMEOUT)
            status = resp.status
            resp_body = resp.read()
            resp_headers = resp.getheaders()
            resp_ctype = resp.headers.get("Content-Type", "")
        except HTTPError as e:
            status = e.code
            resp_body = e.read()
            resp_headers = list(e.headers.items())
            resp_ctype = e.headers.get("Content-Type", "")
        except (URLError, ConnectionError, OSError) as e:
            latency = int((time.time() - t0) * 1000)
            self._json(502, {"error": "bad_gateway",
                             "detail": f"upstream {UPSTREAM} unreachable: {e}"})
            _log_request(self._client_ip(), self.command, self.path, route, {}, 502,
                         latency, eval_user=eval_user)
            return
        latency = int((time.time() - t0) * 1000)

        # Best-effort analytics extraction.
        req_json = resp_json = None
        if route == "session.create" and body and "json" in self.headers.get("Content-Type", "").lower():
            try:
                req_json = json.loads(body)
            except Exception:
                pass
        if route in ("session.create", "grade") and "json" in resp_ctype.lower() and len(resp_body) < 1_000_000:
            try:
                resp_json = json.loads(resp_body)
            except Exception:
                pass
        dims = _extract_dims(route, path, query, req_json, resp_json)
        _log_request(self._client_ip(), self.command, self.path, route, dims, status,
                     latency, eval_user=eval_user)
        self._console(route, dims, status, latency, eval_user)

        # Relay response verbatim (minus hop-by-hop; Content-Length recomputed).
        self.send_response(status)
        seen_cors = False
        for k, v in resp_headers:
            kl = k.lower()
            if kl in _HOP_BY_HOP:
                continue
            if kl == "access-control-allow-origin":
                seen_cors = True
            self.send_header(k, v)
        if not seen_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(resp_body)

    def _console(self, route, dims, status, latency, eval_user=UNKNOWN_USER):
        tag = dims.get("benchmark") or "-"
        extra = ""
        if route == "grade" and dims.get("pass_rate") is not None:
            extra = f"  pass_rate={dims['pass_rate']}"
        ok = "OK" if 200 <= status < 400 else "!!"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] {ok} {self.command:6} {route:14} {tag:5} "
              f"{status} {latency}ms{extra}  <- {self._client_ip()} [{eval_user}]")

    # -- verbs ------------------------------------------------------------- #
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def log_message(self, fmt, *args):
        pass


def _probe_upstream():
    try:
        resp = urlopen(f"{UPSTREAM}/v1/health", timeout=3)
        return 200 <= resp.status < 400
    except Exception:
        return False


def main():
    global UPSTREAM, PROXY_PORT, API_KEY, _PUBLIC_URL, _PUBLIC_HOST, TOKENS

    parser = argparse.ArgumentParser(description="Public ngrok tunnel for the eval service.")
    parser.add_argument("--ngrok-token", default=os.environ.get("NGROK_AUTHTOKEN")
                        or os.environ.get("NGROK_TOKEN"),
                        help="ngrok authtoken (or set $NGROK_AUTHTOKEN / $NGROK_TOKEN).")
    parser.add_argument("--upstream-host", default=os.environ.get("EVAL_SERVICE_HOST", "localhost"))
    parser.add_argument("--upstream-port", type=int,
                        default=int(os.environ.get("EVAL_SERVICE_PORT", "8077")))
    parser.add_argument("--proxy-port", type=int, default=PROXY_PORT)
    parser.add_argument("--api-key", default=API_KEY,
                        help="Shared key accepted on /v1/* alongside per-user keys.")
    parser.add_argument("--auth-tokens", default=os.environ.get("EVAL_SERVICE_AUTH_TOKENS", ""),
                        help="Per-user token store(s) issued by the demo site's "
                             "MyAuthtoken page: CSV and/or SQLite, os.pathsep-separated. "
                             "Defaults to the known locations when they exist.")
    parser.add_argument("--region", default=os.environ.get("NGROK_REGION"),
                        help="ngrok region (e.g. us, eu, ap).")
    parser.add_argument("--domain", default=NGROK_DOMAIN,
                        help="Reserved ngrok domain to bind (keeps the public URL "
                             "stable across restarts). Empty = ephemeral URL.")
    args = parser.parse_args()

    UPSTREAM = f"http://{args.upstream_host}:{args.upstream_port}".rstrip("/")
    PROXY_PORT = args.proxy_port
    API_KEY = (args.api_key or "").strip()

    if args.auth_tokens.strip():
        try:
            from eval_service.authtokens import TokenStore
            TOKENS = TokenStore(args.auth_tokens.split(os.pathsep), configured=True)
        except Exception as e:                              # noqa: BLE001
            print(f"[tunnel] token store {args.auth_tokens!r} failed to load: {e}", flush=True)
    st = TOKENS.status() if TOKENS is not None else {"enabled": False}
    if os.environ.get("EVAL_SERVICE_AUTH_DISABLE", "").strip() in ("1", "true", "yes"):
        print("[tunnel] auth: DISABLED by EVAL_SERVICE_AUTH_DISABLE -- /v1/* is open", flush=True)
    elif st.get("enabled") and st.get("healthy"):
        print(f"[tunnel] auth: {st['n_tokens']} per-user key(s) from "
              f"{', '.join(os.path.basename(p) for p in st['sources'])}"
              f"{' + shared key' if API_KEY else ''}", flush=True)
    elif st.get("enabled"):
        print(f"[tunnel] auth: token store UNREADABLE ({st.get('error')}) -- /v1/* will "
              f"return 503 until fixed", flush=True)
    elif API_KEY:
        print("[tunnel] auth: shared key only (no per-user token store found)", flush=True)
    else:
        print("[tunnel] auth: OPEN -- no shared key and no token store", flush=True)

    try:
        from pyngrok import conf, ngrok
    except ImportError:
        sys.exit("pyngrok is not installed. Run: pip install pyngrok  "
                 "(or use ./ngrok_tunnel.sh which installs it for you).")

    _init_storage()
    writer = _start_writer()
    print(f"Upstream service: {UPSTREAM}")
    print(f"Analytics DB:     {DB_PATH}")
    if not _probe_upstream():
        print(f"  WARNING: {UPSTREAM}/v1/health did not respond. Start the service first "
              f"(./run.sh) — the tunnel will still come up and return 502 until it does.")
    if _gate_on():
        print("API key gate:     ENABLED (clients must send Authorization: Bearer <key>)")
    else:
        print("API key gate:     DISABLED — the API is PUBLIC and unauthenticated. "
              "Set --api-key / $EVAL_SERVICE_API_KEY to require a key.")

    server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Reverse proxy:    http://0.0.0.0:{PROXY_PORT}")

    if not args.ngrok_token:
        server.shutdown()
        sys.exit("No ngrok token. Pass --ngrok-token <token> or set $NGROK_AUTHTOKEN.")
    conf.get_default().auth_token = args.ngrok_token
    if args.region:
        conf.get_default().region = args.region
    tunnel = None
    if args.domain:
        try:
            tunnel = ngrok.connect(PROXY_PORT, "http", domain=args.domain)
        except Exception as e:  # noqa: BLE001 - domain may be taken/not reserved
            print(f"  WARNING: could not bind reserved domain '{args.domain}': {e}")
            print("           Falling back to an ephemeral URL — clients pinned to the "
                  "reserved domain will need updating.")
    if tunnel is None:
        tunnel = ngrok.connect(PROXY_PORT, "http")
    _PUBLIC_URL = tunnel.public_url
    _PUBLIC_HOST = urlparse(_PUBLIC_URL).netloc

    print("\n" + "=" * 72)
    print(f"  ngrok tunnel is LIVE:  {_PUBLIC_URL}")
    print("=" * 72)
    print("\n  Point the SDK / clients at this base URL:")
    print(f"      base_url = \"{_PUBLIC_URL}\"")
    if API_KEY:
        print(f"      headers  = {{\"Authorization\": \"Bearer {API_KEY}\"}}")
    print("\n  Endpoints:")
    print(f"    Landing / docs:  {_PUBLIC_URL}/  |  {_PUBLIC_URL}/docs")
    print(f"    Health:          {_PUBLIC_URL}/v1/health")
    print(f"    Benchmarks:      {_PUBLIC_URL}/v1/benchmarks")
    print(f"    Tasks:           {_PUBLIC_URL}/v1/tasks?benchmark=eog")
    print(f"    Sessions:        {_PUBLIC_URL}/v1/sessions  (POST)")
    print(f"    Analytics JSON:  {_PUBLIC_URL}/_tunnel/analytics?group_by=day")
    print(f"    Chart:           {_PUBLIC_URL}/_tunnel/chart")
    print(f"    Chart SVG:       {_PUBLIC_URL}/_tunnel/chart.svg?theme=light")
    print("\n" + "=" * 72)
    print("\nPress Ctrl+C to stop.\n")

    def cleanup(sig=None, frame=None):
        print("\nShutting down...")
        try:
            ngrok.kill()
        finally:
            server.shutdown()
            _stop_writer(writer)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    threading.Event().wait()


if __name__ == "__main__":
    main()
