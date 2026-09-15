"""Lightweight usage telemetry for the evaluation service.

Every SDK call is an HTTP request against this service, so a small HTTP
middleware that times each request gives the operator both signals they asked
for, with zero client cooperation and zero external dependencies:

  * **popularity over time** -- request counts bucketed by hour/day, plus a
    breakdown by client (``User-Agent``; the SDK tags itself so you can tell
    SDK traffic from curl/browser), authenticated user, and dataset.
  * **current bottleneck** -- per-endpoint server-side latency percentiles
    (p50/p95/p99/max), so the slowest routes (e.g. ``run_agent``, the MCP proxy)
    rise to the top.

Records are kept in memory for a fast ``GET /v1/usage`` summary *and* appended
to a JSONL log so history survives restarts (the log is replayed into memory on
startup). Recording never raises into the request path -- telemetry must never
break a real call.

Env knobs (all optional):
  * ``EVAL_SERVICE_USAGE``       -- ``off``/``0`` disables telemetry entirely.
  * ``EVAL_SERVICE_USAGE_LOG``   -- JSONL path; empty string / ``none`` keeps
    telemetry in-memory only (no file). Defaults to ``eval_service/logs/usage.jsonl``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).parent / "logs" / "usage.jsonl"
# Per-endpoint latency samples kept in memory for percentile estimates. Bounded
# so a long-lived process can't grow without limit; percentiles are over the most
# recent ``_MAX_SAMPLES`` calls, which is exactly the "right now" view we want.
_MAX_SAMPLES = 5000
# Cap how many historical records we replay from disk on startup (newest kept).
_LOAD_LIMIT = 500_000
_HOUR_FMT = "%Y-%m-%dT%H"
_DAY_FMT = "%Y-%m-%d"
# Monitoring endpoints excluded from telemetry by default: the dashboard page and
# its data endpoint are the *monitor*, not real SDK/API usage -- and the dashboard
# auto-refreshes ``/v1/usage`` (every ~10s), which would otherwise dominate the
# counts. Override/extend via ``$EVAL_SERVICE_USAGE_EXCLUDE`` (comma-separated
# route paths). Matched on the request path (any method).
_DEFAULT_EXCLUDE = frozenset({"/v1/usage", "/v1/usage/dashboard", "/dashboard"})
UNKNOWN_USER = "unknown user"


def _user_label(value: Any) -> str:
    """Normalise the trusted proxy label without ever handling an API key."""
    return (str(value or "").strip()[:200] or UNKNOWN_USER)


def _now() -> float:
    return time.time()


def _pct(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list (q in [0, 1])."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _disabled(val: str | None) -> bool:
    """``EVAL_SERVICE_USAGE`` in {off,0,false,no,disable(d)} turns telemetry off."""
    return (val or "").strip().lower() in ("0", "off", "false", "no", "disable", "disabled")


class UsageLog:
    """In-memory usage aggregates + optional durable JSONL log.

    Thread-safe (a single lock guards the aggregates and the file append), so it
    is safe under Starlette's async middleware and FastAPI's threadpool routes.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        exclude: set[str] | None = None,
    ) -> None:
        self.enabled = not _disabled(os.environ.get("EVAL_SERVICE_USAGE"))
        # Endpoints to skip (monitor page + its polling data endpoint): explicit
        # arg > env (comma-separated, replaces default) > built-in default.
        env_ex = os.environ.get("EVAL_SERVICE_USAGE_EXCLUDE")
        if exclude is not None:
            self.exclude = set(exclude)
        elif env_ex is not None:
            self.exclude = {p.strip() for p in env_ex.split(",") if p.strip()}
        else:
            self.exclude = set(_DEFAULT_EXCLUDE)
        # Resolve the JSONL path: explicit arg > env > default. An empty/"none"
        # env value means "in-memory only" (no file).
        env_log = os.environ.get("EVAL_SERVICE_USAGE_LOG")
        if path is not None:
            resolved: Path | None = Path(path)
        elif env_log is not None:
            resolved = None if env_log.strip().lower() in ("", "none", "off") else Path(env_log)
        else:
            resolved = _DEFAULT_PATH
        self.path = resolved
        self._file_enabled = self.enabled and self.path is not None

        self._lock = threading.Lock()
        self.started_at = _now()
        # Aggregates (all guarded by ``self._lock``).
        self.total = 0
        self.count: Counter[str] = Counter()          # endpoint -> n
        self.errors: Counter[str] = Counter()         # endpoint -> n (status >= 400)
        self.status: Counter[str] = Counter()         # "200" -> n
        self.by_client: Counter[str] = Counter()      # User-Agent -> n
        self.by_user: Counter[str] = Counter()        # authenticated identity -> n
        self.by_dataset: Counter[str] = Counter()     # dataset -> n
        self.by_hour: Counter[str] = Counter()        # "YYYY-MM-DDTHH" -> n
        self.by_day: Counter[str] = Counter()         # "YYYY-MM-DD" -> n
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))
        self._sum: dict[str, float] = defaultdict(float)   # endpoint -> total ms (for mean)
        self._max: dict[str, float] = defaultdict(float)   # endpoint -> max ms
        self._n_all: Counter[str] = Counter()             # endpoint -> lifetime n (for mean)
        self._user_errors: Counter[str] = Counter()
        self._user_endpoints: dict[str, Counter[str]] = defaultdict(Counter)
        self._user_datasets: dict[str, Counter[str]] = defaultdict(Counter)
        self.last_ts: float | None = None

    # -- ingest -------------------------------------------------------------- #
    def load(self) -> int:
        """Replay an existing JSONL log into the in-memory aggregates.

        Called once on startup so "popularity over time" survives restarts. Only
        the newest ``_LOAD_LIMIT`` records are replayed. Best-effort: a corrupt
        line is skipped, a missing file is a no-op.
        """
        if not (self.path and self.path.exists()):
            return 0
        n = 0
        try:
            lines = self.path.read_text().splitlines()
        except Exception:  # noqa: BLE001 - unreadable log must not block startup
            return 0
        with self._lock:
            for line in lines[-_LOAD_LIMIT:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001 - skip corrupt lines
                    continue
                self._apply(rec, persist=False)
                n += 1
        return n

    def record(
        self,
        *,
        method: str,
        endpoint: str,
        status: int,
        duration_ms: float,
        client: str = "",
        user: str = UNKNOWN_USER,
        dataset: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Record one request. Never raises."""
        if not self.enabled:
            return
        rec: dict[str, Any] = {
            "ts": ts if ts is not None else _now(),
            "method": str(method),
            "endpoint": str(endpoint),
            "status": int(status),
            "duration_ms": round(float(duration_ms), 2),
            "client": (client or "")[:200],
            "user": _user_label(user),
        }
        if dataset:
            rec["dataset"] = str(dataset)
        try:
            with self._lock:
                self._apply(rec, persist=self._file_enabled)
        except Exception:  # noqa: BLE001 - telemetry must never break a request
            pass

    def _apply(self, rec: dict[str, Any], *, persist: bool) -> None:
        endpoint = str(rec.get("endpoint", ""))
        # Drop monitoring traffic (dashboard + its /v1/usage polling) so it neither
        # inflates the counts nor gets persisted/replayed. Guards both the live path
        # (record -> _apply) and the startup replay (load -> _apply).
        if endpoint in self.exclude:
            return
        ep = f"{rec.get('method', '')} {endpoint}".strip()
        self.total += 1
        self.count[ep] += 1
        self._n_all[ep] += 1
        st = int(rec.get("status", 0))
        self.status[str(st)] += 1
        user = _user_label(rec.get("user"))
        self.by_user[user] += 1
        self._user_endpoints[user][ep] += 1
        if st >= 400:
            self.errors[ep] += 1
            self._user_errors[user] += 1
        self.by_client[(rec.get("client") or "unknown")] += 1
        dt = float(rec.get("duration_ms", 0.0))
        self._samples[ep].append(dt)
        self._sum[ep] += dt
        if dt > self._max[ep]:
            self._max[ep] = dt
        ts = float(rec.get("ts", _now()))
        when = datetime.fromtimestamp(ts, timezone.utc)
        self.by_hour[when.strftime(_HOUR_FMT)] += 1
        self.by_day[when.strftime(_DAY_FMT)] += 1
        ds = rec.get("dataset")
        if ds:
            self.by_dataset[ds] += 1
            self._user_datasets[user][str(ds)] += 1
        self.last_ts = ts
        if persist and self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
            except Exception:  # noqa: BLE001 - a failed append must not break the call
                pass

    # -- report -------------------------------------------------------------- #
    def _endpoint_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ep, n in self.count.items():
            samples = sorted(self._samples[ep])
            lifetime = self._n_all[ep] or n
            rows.append(
                {
                    "endpoint": ep,
                    "count": n,
                    "errors": self.errors[ep],
                    "latency_ms": {
                        "mean": round(self._sum[ep] / lifetime, 1) if lifetime else None,
                        "p50": round(_pct(samples, 0.50), 1) if samples else None,
                        "p95": round(_pct(samples, 0.95), 1) if samples else None,
                        "p99": round(_pct(samples, 0.99), 1) if samples else None,
                        "max": round(self._max[ep], 1),
                        "samples": len(samples),
                    },
                }
            )
        return rows

    def summary(self, top: int = 20) -> dict[str, Any]:
        """Aggregate view for ``GET /v1/usage``.

        ``top_by_calls`` answers *how popular* (endpoints ranked by request
        count); ``top_by_latency_p95`` answers *what's the bottleneck* (same rows
        ranked by p95 server latency). ``by_hour``/``by_day`` are the over-time
        series; ``by_client`` attributes traffic (SDK vs other), while
        ``by_user`` and ``user_usage`` attribute it to the non-secret identity
        resolved by the authentication proxy. Legacy/unattributable records are
        grouped under ``unknown user``.
        """
        top = max(1, int(top))
        with self._lock:
            rows = self._endpoint_rows()
            by_calls = sorted(rows, key=lambda r: r["count"], reverse=True)[:top]
            by_latency = sorted(
                rows, key=lambda r: (r["latency_ms"]["p95"] or 0.0), reverse=True
            )[:top]
            hours = dict(sorted(self.by_hour.items())[-48:])
            days = dict(sorted(self.by_day.items())[-60:])
            user_usage = [
                {
                    "user": user,
                    "requests": count,
                    "errors": self._user_errors[user],
                    "by_endpoint": dict(self._user_endpoints[user].most_common(top)),
                    "by_dataset": dict(self._user_datasets[user].most_common(top)),
                }
                for user, count in self.by_user.most_common()
            ]
            return {
                "enabled": self.enabled,
                "since": datetime.fromtimestamp(self.started_at, timezone.utc).isoformat(),
                "last_request": (
                    datetime.fromtimestamp(self.last_ts, timezone.utc).isoformat()
                    if self.last_ts
                    else None
                ),
                "total_requests": self.total,
                "status_counts": dict(self.status),
                "endpoints": len(self.count),
                "excluded": sorted(self.exclude),
                "top_by_calls": by_calls,          # popularity
                "top_by_latency_p95": by_latency,  # bottleneck
                "by_client": dict(self.by_client.most_common(top)),
                "by_user": dict(self.by_user.most_common()),
                "user_usage": user_usage,
                "by_dataset": dict(self.by_dataset.most_common(top)),
                "by_hour": hours,
                "by_day": days,
                "log_path": str(self.path) if self._file_enabled else None,
            }


# --------------------------------------------------------------------------- #
# Monitor page                                                                #
# A single self-contained HTML page (no external CDN/assets, so it works on an #
# internal/offline host) that polls GET /v1/usage and renders it: header stats,#
# popularity-over-time bars, endpoints-by-calls (popularity), endpoints-by-p95 #
# (bottleneck), and by-client / by-dataset breakdowns. Served by the service   #
# at GET /v1/usage/dashboard (and /dashboard).                                 #
# --------------------------------------------------------------------------- #
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eval Service &mdash; SDK / API Usage</title>
<style>
  :root{
    --bg:#0f172a; --panel:#1e293b; --border:#334155; --text:#e2e8f0;
    --muted:#64748b; --accent:#38bdf8; --pink:#f472b6; --green:#34d399;
    --red:#ef4444; --track:#0b1220;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text);padding:1.5rem;line-height:1.4}
  h1{font-size:1.4rem;margin:0}
  h2{font-size:.95rem;margin:0 0 .75rem;color:#cbd5e1;font-weight:600}
  .sub{color:var(--muted);font-size:.82rem;margin-top:.3rem}
  .muted{color:var(--muted)}
  header,.wrap,.foot{max-width:1200px;margin-left:auto;margin-right:auto}
  header{margin-bottom:1.25rem}
  .controls{display:flex;gap:.75rem;align-items:center;margin-top:.9rem;flex-wrap:wrap}
  .controls label{font-size:.8rem;color:#94a3b8;display:flex;gap:.35rem;align-items:center}
  .controls input,.controls select{background:var(--panel);color:var(--text);border:1px solid var(--border);
       border-radius:6px;padding:.28rem .5rem;font-size:.8rem}
  .controls input[type=number]{width:64px}
  button{background:#0f2a4a;color:#f1f5f9;border:1px solid var(--accent);border-radius:6px;
       padding:.32rem .8rem;font-size:.8rem;cursor:pointer}
  button:hover{background:#123a63}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin-bottom:1.1rem}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:.85rem 1rem}
  .card .k{font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .card .v{font-size:1.5rem;font-weight:700;margin-top:.2rem}
  .card .v.small{font-size:.9rem;font-weight:600}
  .card .v.err{color:var(--red)}
  .grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:1rem;margin-bottom:1rem}
  @media(max-width:820px){.grid2{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:1rem 1.1rem;margin-bottom:1rem}
  table{width:100%;border-collapse:collapse;font-size:.82rem}
  th,td{text-align:right;padding:.34rem .5rem;border-bottom:1px solid #26324a;white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--muted);font-weight:600;font-size:.7rem;text-transform:uppercase;letter-spacing:.03em}
  td.ep{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem;
       max-width:360px;overflow:hidden;text-overflow:ellipsis}
  .cell-bar{position:relative}
  .cell-bar .cb{position:absolute;left:0;top:2px;bottom:2px;background:rgba(56,189,248,.18);border-radius:3px}
  .cell-bar span.n{position:relative}
  .barrow{display:flex;align-items:center;gap:.6rem;margin:.32rem 0}
  .barrow .lab{flex:0 0 auto;font-size:.78rem;min-width:120px;max-width:120px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,monospace;color:#cbd5e1}
  .barrow .track{flex:1;background:var(--track);border-radius:5px;height:16px;overflow:hidden;border:1px solid #223049}
  .barrow .fill{height:100%;border-radius:5px}
  .barrow .n{flex:0 0 auto;font-size:.78rem;color:#94a3b8;min-width:52px;text-align:right}
  .err{color:var(--red)}
  .banner{background:#4c1d1d;border:1px solid var(--red);color:#fecaca;padding:.6rem .9rem;
       border-radius:8px;margin-bottom:1rem;display:none;font-size:.85rem}
  .tabs{display:flex;gap:.4rem;margin-bottom:.7rem}
  a.tab{color:var(--muted);font-size:.75rem;padding:.2rem .55rem;border:1px solid var(--border);
       border-radius:6px;cursor:pointer;text-decoration:none}
  a.tab.active{color:#f1f5f9;border-color:var(--accent);background:#0f2a4a}
  .foot{margin-top:1rem;font-size:.75rem}
</style>
</head>
<body>
<header>
  <h1>Eval Service &mdash; SDK / API Usage</h1>
  <div class="sub">Popularity over time + current bottlenecks. Every SDK call is one request here. <span id="meta"></span></div>
  <div class="controls">
    <label>Top <input id="top" type="number" value="15" min="1" max="200"></label>
    <label>Auto-refresh
      <select id="refresh">
        <option value="0">off</option>
        <option value="10" selected>10s</option>
        <option value="30">30s</option>
        <option value="60">60s</option>
      </select>
    </label>
    <button id="reload">Reload now</button>
    <span id="updated" class="muted"></span>
  </div>
</header>
<div class="wrap">
  <div class="banner" id="banner"></div>
  <div class="cards" id="cards"></div>
  <div class="grid2">
    <div class="panel">
      <h2>Popularity over time</h2>
      <div class="tabs"><a class="tab active" data-b="hour">Hourly</a><a class="tab" data-b="day">Daily</a></div>
      <div id="timechart"></div>
    </div>
    <div class="panel">
      <h2>By client <span class="muted">(User-Agent)</span></h2>
      <div id="clients"></div>
    </div>
  </div>
  <div class="panel">
    <h2>By authenticated user <span class="muted">(legacy/shared-key traffic is unknown)</span></h2>
    <div id="users"></div>
  </div>
  <div class="panel">
    <h2>Endpoints by calls <span class="muted">&mdash; how popular</span></h2>
    <div id="bycalls"></div>
  </div>
  <div class="panel">
    <h2>Bottleneck <span class="muted">&mdash; endpoints by p95 server latency</span></h2>
    <div id="bylatency"></div>
  </div>
  <div class="panel">
    <h2>By dataset</h2>
    <div id="datasets"></div>
  </div>
</div>
<div class="foot muted" id="foot"></div>
<script>
const $ = s => document.querySelector(s);
const state = {bucket: "hour", timer: null};

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function fmtInt(n){return (n||0).toLocaleString();}
function fmtMs(x){
  if(x===null||x===undefined) return "&mdash;";
  return x>=1000 ? (x/1000).toFixed(x>=10000?0:2)+" s" : Math.round(x)+" ms";
}
function latColor(p){
  if(p===null||p===undefined) return "#475569";
  if(p>=10000) return "#ef4444";
  if(p>=1000)  return "#f59e0b";
  if(p>=200)   return "#eab308";
  return "#34d399";
}

function renderCards(u){
  const errs = Object.entries(u.status_counts||{}).reduce((a,[k,v])=>a+(parseInt(k)>=400?v:0),0);
  const rate = u.total_requests ? (100*errs/u.total_requests) : 0;
  const items = [
    ["Total requests", fmtInt(u.total_requests), ""],
    ["Endpoints", fmtInt(u.endpoints), ""],
    ["Errors 4xx/5xx", fmtInt(errs)+" ("+rate.toFixed(1)+"%)", errs?"err":""],
    ["Since", (u.since||"").replace("T"," ").slice(0,16), "small"],
    ["Last request", (u.last_request||"&mdash;").replace("T"," ").slice(0,19), "small"],
  ];
  $("#cards").innerHTML = items.map(([k,v,c])=>
    `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");
}

function renderBars(el, entries, color){
  const max = Math.max(1, ...entries.map(e=>e[1]));
  el.innerHTML = entries.length ? entries.map(([lab,n])=>{
    const pct = (100*n/max).toFixed(1);
    return `<div class="barrow"><span class="lab" title="${esc(lab)}">${esc(lab)}</span>`
         + `<span class="track"><span class="fill" style="width:${pct}%;background:${color}"></span></span>`
         + `<span class="n">${fmtInt(n)}</span></div>`;
  }).join("") : '<div class="muted" style="font-size:.8rem">No data yet.</div>';
}

function renderTimechart(u){
  const src = state.bucket==="day" ? (u.by_day||{}) : (u.by_hour||{});
  renderBars($("#timechart"), Object.entries(src), "linear-gradient(90deg,#38bdf8,#818cf8)");
}

function renderEndpointTable(el, rows){
  if(!rows || !rows.length){ el.innerHTML='<div class="muted" style="font-size:.8rem">No data yet.</div>'; return; }
  const maxCount = Math.max(1, ...rows.map(r=>r.count));
  const head = `<table><thead><tr><th>Endpoint</th><th>Calls</th><th>Err</th>`
             + `<th>p50</th><th>p95</th><th>p99</th><th>max</th></tr></thead><tbody>`;
  const body = rows.map(r=>{
    const lm = r.latency_ms||{};
    const cpct = (100*r.count/maxCount).toFixed(1);
    const p95c = latColor(lm.p95);
    return `<tr>`
      + `<td class="ep" title="${esc(r.endpoint)}">${esc(r.endpoint)}</td>`
      + `<td class="cell-bar"><span class="cb" style="width:${cpct}%"></span><span class="n">${fmtInt(r.count)}</span></td>`
      + `<td class="${r.errors?'err':''}">${fmtInt(r.errors)}</td>`
      + `<td>${fmtMs(lm.p50)}</td>`
      + `<td style="color:${p95c};font-weight:600">${fmtMs(lm.p95)}</td>`
      + `<td>${fmtMs(lm.p99)}</td>`
      + `<td>${fmtMs(lm.max)}</td></tr>`;
  }).join("");
  el.innerHTML = head + body + `</tbody></table>`;
}

async function load(){
  const top = Math.max(1, Math.min(200, parseInt($("#top").value)||15));
  try{
    const r = await fetch(`/v1/usage?top=${top}`, {headers:{"ngrok-skip-browser-warning":"true"}});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const u = await r.json();
    $("#banner").style.display="none";
    renderCards(u);
    renderTimechart(u);
    renderEndpointTable($("#bycalls"), u.top_by_calls||[]);
    renderEndpointTable($("#bylatency"), u.top_by_latency_p95||[]);
    renderBars($("#clients"), Object.entries(u.by_client||{}), "#f472b6");
    renderBars($("#users"), Object.entries(u.by_user||{}), "#38bdf8");
    renderBars($("#datasets"), Object.entries(u.by_dataset||{}), "#34d399");
    $("#meta").textContent = (u.enabled ? "" : "[telemetry DISABLED] ")
      + (u.log_path ? ("log: "+u.log_path) : "in-memory only");
    $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
    const excl = (u.excluded||[]).join(", ");
    $("#foot").innerHTML = "Source: GET /v1/usage &middot; latency is server-side (time to response); "
      + "long agent runs (run_agent / MCP proxy) show up here as the bottleneck."
      + (excl ? (" &middot; excluded (monitoring): " + esc(excl)) : "");
    state._u = u;
  }catch(e){
    const b=$("#banner"); b.style.display="block";
    b.textContent = "Failed to load /v1/usage: " + e.message
      + " -- is the service running with usage telemetry (restart required to pick it up)?";
  }
}

function setRefresh(){
  if(state.timer){ clearInterval(state.timer); state.timer=null; }
  const s = parseInt($("#refresh").value)||0;
  if(s>0) state.timer=setInterval(load, s*1000);
}

document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  t.classList.add("active"); state.bucket=t.dataset.b;
  if(state._u) renderTimechart(state._u);
}));
$("#reload").addEventListener("click", load);
$("#refresh").addEventListener("change", setRefresh);
$("#top").addEventListener("change", load);
load(); setRefresh();
</script>
</body>
</html>"""
