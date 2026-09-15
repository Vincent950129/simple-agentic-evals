import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_service import authtokens  # noqa: E402
from eval_service import usage  # noqa: E402
import ngrok_tunnel  # noqa: E402


def test_public_gate_includes_all_skill_and_sdk_downloads() -> None:
    for path in (
        "/resources/skill.md",
        "/resources/evolve-eval/SKILL.md",
        "/resources/evolve-benchmark/install.sh",
        "/resources/evolve-benchmark/manifest.json",
    ):
        assert ngrok_tunnel.requires_api_key("GET", path)
    assert not ngrok_tunnel.requires_api_key("OPTIONS", "/resources/skill.md")
    assert ngrok_tunnel.requires_api_key("GET", "/sdk")
    assert ngrok_tunnel.requires_api_key("GET", "/sdk/client.whl")
UNKNOWN_USER = usage.UNKNOWN_USER
UsageLog = usage.UsageLog


LEGACY_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ip TEXT NOT NULL,
    city TEXT,
    country TEXT,
    method TEXT,
    path TEXT,
    route TEXT,
    dataset TEXT,
    benchmark TEXT,
    version TEXT,
    split TEXT,
    task_id TEXT,
    session_id TEXT,
    status_code INTEGER,
    success INTEGER,
    latency_ms INTEGER,
    pass_rate REAL
)
"""


def test_usage_log_attributes_new_and_legacy_records(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_SERVICE_USAGE", raising=False)
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps({
        "ts": 1_700_000_000,
        "method": "GET",
        "endpoint": "/v1/tasks",
        "status": 200,
        "duration_ms": 4,
        "client": "old-client",
        "dataset": "evovling_tools",
    }) + "\n")

    log = UsageLog(path=path, exclude=set())
    assert log.load() == 1
    log.record(
        method="POST",
        endpoint="/v1/sessions",
        status=500,
        duration_ms=9,
        client="simple_agentic_evals/0.10.0",
        user="alice@example.com",
        dataset="evovling_agents",
        ts=1_700_000_001,
    )

    summary = log.summary()
    assert summary["by_user"] == {
        UNKNOWN_USER: 1,
        "alice@example.com": 1,
    }
    users = {row["user"]: row for row in summary["user_usage"]}
    assert users[UNKNOWN_USER]["by_endpoint"] == {"GET /v1/tasks": 1}
    assert users["alice@example.com"]["errors"] == 1
    assert users["alice@example.com"]["by_dataset"] == {"evovling_agents": 1}
    assert json.loads(path.read_text().splitlines()[-1])["user"] == "alice@example.com"


def test_tunnel_migrates_and_reports_per_user_usage(tmp_path, monkeypatch):
    db_path = tmp_path / "analytics.db"
    csv_path = tmp_path / "analytics.csv"
    con = sqlite3.connect(db_path)
    con.execute(LEGACY_SCHEMA)
    con.execute(
        "INSERT INTO requests (ts, ip, method, path, route, status_code, success, latency_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-09-01T00:00:00+00:00", "127.0.0.1", "GET", "/v1/health",
         "health", 200, 1, 1),
    )
    con.commit()
    con.close()
    with csv_path.open("w", newline="") as fh:
        csv.writer(fh).writerow(ngrok_tunnel._CSV_COLUMNS[:-1])

    monkeypatch.setattr(ngrok_tunnel, "_ANALYTICS_DIR", str(tmp_path))
    monkeypatch.setattr(ngrok_tunnel, "DB_PATH", str(db_path))
    monkeypatch.setattr(ngrok_tunnel, "CSV_PATH", str(csv_path))
    ngrok_tunnel._init_storage()

    con = sqlite3.connect(db_path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(requests)")}
    assert "eval_user" in columns
    assert con.execute("SELECT eval_user FROM requests").fetchone()[0] == UNKNOWN_USER

    session_dims = {
        "dataset": "evovling_agents",
        "benchmark": "eog",
        "version": "1",
        "split": "test",
        "task_id": "task-1",
        "session_id": "session-1",
        "pass_rate": None,
    }
    grade_dims = dict(session_dims, pass_rate=0.75)
    queued = [
        ("2026-09-08T00:00:00+00:00", "127.0.0.1", "POST", "/v1/sessions",
         "session.create", session_dims, 200, 1, 5, "alice@example.com"),
        ("2026-09-08T00:01:00+00:00", "127.0.0.1", "POST",
         "/v1/sessions/session-1/grade", "grade", grade_dims, 200, 1, 7,
         "alice@example.com"),
    ]
    con.executemany(ngrok_tunnel._INSERT_SQL, [ngrok_tunnel._expand(row) for row in queued])
    con.commit()
    con.close()

    report = ngrok_tunnel._query_analytics()
    users = {row["user"]: row for row in report["by_user"]}
    assert users[UNKNOWN_USER]["requests"] == 1
    assert users["alice@example.com"]["requests"] == 2
    assert users["alice@example.com"]["sessions_created"] == 1
    assert users["alice@example.com"]["grade_attempts"] == 1
    assert users["alice@example.com"]["avg_pass_rate"] == 0.75
    assert users["alice@example.com"]["by_route"] == {
        "grade": 1,
        "session.create": 1,
    }
    assert users["alice@example.com"]["sessions_by_dataset"] == {
        "evovling_agents": 1,
    }
    assert users["alice@example.com"]["sessions_by_benchmark"] == {"eog": 1}

    assert next(csv.reader(csv_path.open())) == ngrok_tunnel._CSV_COLUMNS
    assert len(list(tmp_path.glob("analytics.pre-user-*.csv"))) == 1


def test_successful_verification_updates_authoritative_last_used(tmp_path, monkeypatch):
    tokens_db = tmp_path / "authtokens.db"
    tokens_csv = tmp_path / "authtokens.csv"
    token = "mas_test-token"
    con = sqlite3.connect(tokens_db)
    con.executescript("""
        CREATE TABLE authtokens (
            user_sub TEXT PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            rotated_at REAL,
            last_used REAL,
            rotation_count INTEGER NOT NULL DEFAULT 0
        );
    """)
    con.execute(
        "INSERT INTO authtokens VALUES (?, ?, ?, ?, ?, ?)",
        ("sub-1", token, 1_600_000_000, None, None, 2),
    )
    con.commit()
    con.close()
    with tokens_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=authtokens._AUTHTOKENS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerow({
            "user_sub": "sub-1",
            "email": "a@example.com",
            "name": "A",
            "token": token,
            "created_at": authtokens._iso(1_600_000_000),
            "created_at_epoch": 1_600_000_000,
            "rotated_at": "",
            "rotated_at_epoch": "",
            "last_used": "",
            "last_used_epoch": "",
            "rotation_count": 2,
        })
        writer.writerow({
            "user_sub": "stale-sub",
            "email": "stale@example.com",
            "name": "Stale",
            "token": "mas_stale-token-not-in-db",
            "created_at": authtokens._iso(1_500_000_000),
            "created_at_epoch": 1_500_000_000,
            "rotated_at": "",
            "rotated_at_epoch": "",
            "last_used": "",
            "last_used_epoch": "",
            "rotation_count": 0,
        })

    monkeypatch.setattr(authtokens, "_LAST_USED_RESOLUTION_S", 60.0)
    store = authtokens.TokenStore(
        [str(tokens_db), str(tokens_csv)], configured=True
    )
    identity = store.verify(token)
    assert identity is not None
    assert identity.label == "a@example.com"
    assert store.verify("mas_stale-token-not-in-db") is None
    assert store.last_used_error is None

    con = sqlite3.connect(tokens_db)
    row = con.execute(
        "SELECT token, created_at, rotated_at, last_used, rotation_count "
        "FROM authtokens WHERE user_sub='sub-1'"
    ).fetchone()
    con.close()
    assert row[:3] == (token, 1_600_000_000, None)
    assert row[3] is not None
    assert row[4] == 2

    with tokens_csv.open(newline="", encoding="utf-8") as fh:
        csv_row = next(csv.DictReader(fh))
    assert csv_row["token"] == token
    assert csv_row["last_used"]
    assert float(csv_row["last_used_epoch"]) == row[3]
