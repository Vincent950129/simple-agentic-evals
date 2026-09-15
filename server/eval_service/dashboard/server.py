"""Standalone localhost server for the eval-service operator dashboard."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_DASHBOARD_DIR = Path(__file__).resolve().parent
_EVAL_DIR = _DASHBOARD_DIR.parent

from .. import authtokens
from . import analytics

DEFAULT_DB = _EVAL_DIR / ".tunnel_analytics" / "analytics.db"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "EvalDashboard/1.0"

    def _write(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._write(status, json.dumps(value, indent=2).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/analytics":
            return self._analytics(parsed.query)
        asset = ASSETS.get(parsed.path)
        if asset is None:
            return self._json(404, {"error": "not found"})
        filename, content_type = asset
        try:
            body = (_DASHBOARD_DIR / filename).read_bytes()
        except OSError as exc:
            return self._json(503, {"error": f"dashboard asset unavailable: {exc}"})
        self._write(200, body, content_type)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _analytics(self, query: str) -> None:
        params = parse_qs(query)
        try:
            recent = int(params.get("recent", ["50"])[0])
            days = int(params.get("days", ["60"])[0])
        except (TypeError, ValueError):
            return self._json(400, {"error": "recent and days must be integers"})
        try:
            conn = sqlite3.connect(self.server.db_path, timeout=20.0)  # type: ignore[attr-defined]
            conn.execute("PRAGMA busy_timeout=20000")
            try:
                issued_users = self.server.tokens.identities()  # type: ignore[attr-defined]
                report = analytics.summary(
                    conn, recent=recent, days=days, issued_users=issued_users
                )
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            return self._json(503, {"error": f"analytics unavailable: {exc}"})
        self._json(200, report)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[dashboard] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the local eval dashboard")
    parser.add_argument(
        "--host", default=os.environ.get("EVAL_DASHBOARD_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("EVAL_DASHBOARD_PORT", "8078"))
    )
    parser.add_argument(
        "--db", default=os.environ.get("EVAL_DASHBOARD_DB", str(DEFAULT_DB))
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.db_path = args.db  # type: ignore[attr-defined]
    server.tokens = authtokens.default_store()  # type: ignore[attr-defined]
    print(f"Eval dashboard: http://{args.host}:{args.port}", flush=True)
    print(f"Analytics DB:  {args.db}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
