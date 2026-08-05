"""Expose the Evolving-Benchmarks Evaluation Service publicly via one ngrok tunnel.

This is a thin reverse proxy in front of the local FastAPI service (default
``http://localhost:8077``). It forwards every method/path/byte to the upstream
unchanged, while:

  * logging each request (IP + geo, route, dataset/benchmark/version/task/session,
    status, latency, grade pass_rate) to SQLite + CSV;
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
import json
import math
import os
import re
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

# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYTICS_DIR = os.path.join(_THIS_DIR, ".tunnel_analytics")
DB_PATH = os.path.join(_ANALYTICS_DIR, "analytics.db")
CSV_PATH = os.path.join(_ANALYTICS_DIR, "analytics.csv")

UPSTREAM = os.environ.get("EVAL_SERVICE_UPSTREAM", "http://localhost:8077").rstrip("/")
PROXY_PORT = int(os.environ.get("EVAL_SERVICE_PROXY_PORT", "9077"))
# When set, every /v1/* call (except /v1/health) must carry this key via
# `Authorization: Bearer <key>` or `x-api-key: <key>`.
API_KEY = os.environ.get("EVAL_SERVICE_API_KEY", "").strip()
# Grades (esp. ALE) + DB seeding can be slow; give the upstream generous time.
UPSTREAM_TIMEOUT = int(os.environ.get("EVAL_SERVICE_PROXY_TIMEOUT_SEC", "900"))

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1) + content-length is
# recomputed from the buffered body.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}

_db_lock = threading.Lock()
_geo_cache: dict[str, tuple[str, str]] = {}
_geo_lock = threading.Lock()
_counts = {"eog": 0, "ale": 0, "other": 0}
_count_lock = threading.Lock()


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
    "status_code", "success", "latency_ms", "pass_rate",
]


def _init_storage() -> None:
    os.makedirs(_ANALYTICS_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
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
            pass_rate   REAL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON requests(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip ON requests(ip)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bench ON requests(benchmark)")
    conn.commit()
    conn.close()
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(_CSV_COLUMNS)


_SESSION_RE = re.compile(r"^/v1/sessions/([^/]+)")


def categorize(method: str, path_no_q: str) -> str:
    """Map (method, path) -> a coarse route label for analytics."""
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


def _log_request(ip, method, path, route, dims, status_code, latency_ms):
    city, country = _geolocate_ip(ip)
    ts = datetime.now(timezone.utc).isoformat()
    success = 1 if (status_code and 200 <= status_code < 400) else 0
    row = (
        ts, ip, city, country, method, path, route,
        dims.get("dataset"), dims.get("benchmark"), dims.get("version"),
        dims.get("split"), dims.get("task_id"), dims.get("session_id"),
        status_code, success, latency_ms, dims.get("pass_rate"),
    )
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO requests (ts, ip, city, country, method, path, route, "
            "dataset, benchmark, version, split, task_id, session_id, "
            "status_code, success, latency_ms, pass_rate) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        conn.commit()
        conn.close()
        with open(CSV_PATH, "a", newline="") as f:
            csv.writer(f).writerow(row)
    bench = dims.get("benchmark")
    key = bench if bench in _counts else "other"
    with _count_lock:
        _counts[key] += 1


# --------------------------------------------------------------------------- #
# Analytics queries                                                           #
# --------------------------------------------------------------------------- #
_FMT = {
    "hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d",
    "week": "%Y-W%W", "month": "%Y-%m", "year": "%Y",
}


def _query_analytics(group_by="day"):
    fmt = _FMT.get(group_by, _FMT["day"])
    conn = sqlite3.connect(DB_PATH)
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
    }


def _query_chart_data(group_by="day"):
    """Cumulative request counts per benchmark over time (+ a total series)."""
    fmt = _FMT.get(group_by, _FMT["day"])
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        f"SELECT strftime('{fmt}', ts) AS period, "
        "COALESCE(benchmark, 'other') AS bench, COUNT(*) "
        "FROM requests GROUP BY period, bench ORDER BY period"
    )
    data = cur.fetchall()
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
    lock = ("<b>API key required</b> — send <code>Authorization: Bearer &lt;key&gt;</code>"
            if auth_required else "<b>Open access</b> (no API key configured)")
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
 <li>Health: <a href="{base}/v1/health">{base}/v1/health</a></li>
 <li>Benchmarks: <a href="{base}/v1/benchmarks">{base}/v1/benchmarks</a></li>
 <li>Python SDK base_url: <code>{base}</code></li>
</ul></div>
<div class="card"><b>Usage monitor (SDK / API)</b>
<ul>
 <li>Dashboard: <a href="{base}/v1/usage/dashboard">{base}/v1/usage/dashboard</a></li>
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

    def _json(self, status, obj):
        self._write(status, json.dumps(obj, indent=2).encode(), "application/json")

    def _has_api_key(self):
        if not API_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == API_KEY:
            return True
        return self.headers.get("x-api-key", "").strip() == API_KEY

    # -- dispatch ---------------------------------------------------------- #
    def _handle(self):
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
        if path in ("/", "/_tunnel", "/_tunnel/"):
            html = _landing_html(_PUBLIC_URL or f"http://localhost:{PROXY_PORT}", bool(API_KEY))
            return self._write(200, html.encode(), "text/html; charset=utf-8")
        if path.startswith("/_tunnel/"):
            return self._serve_tunnel(path, query)

        # API key gate for the real service (health stays open for liveness).
        if (API_KEY and self.command != "OPTIONS"
                and path.startswith("/v1/") and path != "/v1/health"
                and not self._has_api_key()):
            self._json(401, {"error": "unauthorized",
                             "detail": "missing or invalid API key; send "
                                       "'Authorization: Bearer <key>' or 'x-api-key: <key>'"})
            _log_request(self._client_ip(), self.command, self.path,
                         categorize(self.command, path), {}, 401, 0)
            return

        self._forward(path, query, body)

    def _serve_tunnel(self, path, query):
        if API_KEY and not self._has_api_key():
            return self._json(401, {"error": "unauthorized",
                                    "detail": "analytics require the API key"})
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
        target = f"{UPSTREAM}{self.path}"
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in _HOP_BY_HOP}
        headers["Host"] = urlparse(UPSTREAM).netloc
        headers["X-Forwarded-For"] = self._client_ip()
        headers["X-Forwarded-Proto"] = "https"
        # Let the service advertise a publicly reachable base (for MCP proxy URLs).
        if _PUBLIC_HOST:
            headers["X-Forwarded-Host"] = _PUBLIC_HOST

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
            _log_request(self._client_ip(), self.command, self.path, route, {}, 502, latency)
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
        _log_request(self._client_ip(), self.command, self.path, route, dims, status, latency)
        self._console(route, dims, status, latency)

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

    def _console(self, route, dims, status, latency):
        tag = dims.get("benchmark") or "-"
        extra = ""
        if route == "grade" and dims.get("pass_rate") is not None:
            extra = f"  pass_rate={dims['pass_rate']}"
        ok = "OK" if 200 <= status < 400 else "!!"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] {ok} {self.command:6} {route:14} {tag:5} "
              f"{status} {latency}ms{extra}  <- {self._client_ip()}")

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
    global UPSTREAM, PROXY_PORT, API_KEY, _PUBLIC_URL, _PUBLIC_HOST

    parser = argparse.ArgumentParser(description="Public ngrok tunnel for the eval service.")
    parser.add_argument("--ngrok-token", default=os.environ.get("NGROK_AUTHTOKEN")
                        or os.environ.get("NGROK_TOKEN"),
                        help="ngrok authtoken (or set $NGROK_AUTHTOKEN / $NGROK_TOKEN).")
    parser.add_argument("--upstream-host", default=os.environ.get("EVAL_SERVICE_HOST", "localhost"))
    parser.add_argument("--upstream-port", type=int,
                        default=int(os.environ.get("EVAL_SERVICE_PORT", "8077")))
    parser.add_argument("--proxy-port", type=int, default=PROXY_PORT)
    parser.add_argument("--api-key", default=API_KEY,
                        help="Require this key on /v1/* (Authorization: Bearer / x-api-key).")
    parser.add_argument("--region", default=os.environ.get("NGROK_REGION"),
                        help="ngrok region (e.g. us, eu, ap).")
    args = parser.parse_args()

    UPSTREAM = f"http://{args.upstream_host}:{args.upstream_port}".rstrip("/")
    PROXY_PORT = args.proxy_port
    API_KEY = (args.api_key or "").strip()

    try:
        from pyngrok import conf, ngrok
    except ImportError:
        sys.exit("pyngrok is not installed. Run: pip install pyngrok  "
                 "(or use ./ngrok_tunnel.sh which installs it for you).")

    _init_storage()
    print(f"Upstream service: {UPSTREAM}")
    print(f"Analytics DB:     {DB_PATH}")
    if not _probe_upstream():
        print(f"  WARNING: {UPSTREAM}/v1/health did not respond. Start the service first "
              f"(./run.sh) — the tunnel will still come up and return 502 until it does.")
    if API_KEY:
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
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    threading.Event().wait()


if __name__ == "__main__":
    main()
