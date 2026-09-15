import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_service.dashboard import analytics  # noqa: E402


REQUEST_SCHEMA = """
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ip TEXT NOT NULL,
    method TEXT,
    route TEXT,
    dataset TEXT,
    benchmark TEXT,
    status_code INTEGER,
    success INTEGER,
    latency_ms INTEGER,
    pass_rate REAL,
    eval_user TEXT
)
"""


def test_materialized_dashboard_summary_groups_users_and_usage():
    conn = sqlite3.connect(":memory:")
    conn.execute(REQUEST_SCHEMA)
    analytics.ensure_schema(conn)
    rows = [
        (1, "2026-09-07T23:59:00+00:00", "10.0.0.1", "health", None,
         None, 200, 1, None, None),
        (2, "2026-09-08T00:00:00+00:00", "10.0.0.2", "session.create",
         "evovling_agents", "eog", 200, 1, None, "alice@example.com"),
        (3, "2026-09-08T00:01:00+00:00", "10.0.0.2", "grade",
         "evovling_agents", "eog", 200, 1, 0.75, "alice@example.com"),
        (4, "2026-09-08T00:02:00+00:00", "10.0.0.3", "tasks",
         "evovling_tools", "ale", 500, 0, None, "alice@example.com"),
    ]
    conn.executemany(
        "INSERT INTO requests(id,ts,ip,method,route,dataset,benchmark,status_code,"
        "success,latency_ms,pass_rate,eval_user) VALUES(?,?,?,'GET',?,?,?,?,?,1,?,?)",
        rows,
    )
    analytics.record_rows(conn, rows, cursor=4)
    analytics._upsert_meta(conn, "ready", 1)
    conn.commit()

    report = analytics.summary(
        conn, recent=4, days=2,
        issued_users=["alice@example.com", "unused@example.com"],
    )
    users = {row["user"]: row for row in report["users"]}

    assert report["aggregation"]["ready"] is True
    assert report["totals"] == {
        "requests": 4,
        "successful_requests": 3,
        "sessions_created": 1,
        "grade_attempts": 1,
        "successful_grades": 1,
        "known_users": 2,
        "active_known_users": 1,
        "issued_users": 2,
        "unknown_requests": 1,
    }
    assert users[analytics.UNKNOWN_USER]["requests"] == 1
    assert users["alice@example.com"]["errors"] == 1
    assert users["alice@example.com"]["unique_ips"] == 2
    assert users["alice@example.com"]["avg_pass_rate"] == 0.75
    assert users["alice@example.com"]["by_route"] == {
        "grade": 1, "session.create": 1, "tasks": 1,
    }
    assert users["alice@example.com"]["sessions_by_dataset"] == {
        "evovling_agents": 1,
    }
    assert users["alice@example.com"]["sessions_by_benchmark"] == {"eog": 1}
    assert users["unused@example.com"]["requests"] == 0
    assert list(report["by_day"]) == ["2026-09-07", "2026-09-08"]
    assert report["recent"][0]["route"] == "tasks"


def test_record_rows_is_additive():
    conn = sqlite3.connect(":memory:")
    conn.execute(REQUEST_SCHEMA)
    analytics.ensure_schema(conn)
    row = (1, "2026-09-08T00:00:00+00:00", "10.0.0.1", "tasks",
           "evovling_tools", "eog", 200, 1, None, "alice@example.com")
    analytics.record_rows(conn, [row], cursor=1)
    analytics.record_rows(conn, [row], cursor=2)
    requests = conn.execute(
        "SELECT requests FROM dashboard_user_totals WHERE eval_user=?",
        ("alice@example.com",),
    ).fetchone()[0]
    assert requests == 2
