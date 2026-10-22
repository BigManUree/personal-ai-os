"""RED: 1.3.5-C-B scheduler recovery and failure-boundary tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import reminder_scheduler as rs
from app import reminders as rm
from app.main import init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler_recovery.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    connection = sqlite3.connect(db_path, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def test_recent_running_run_is_not_abandoned(conn):
    recent = NOW - timedelta(minutes=4)
    conn.execute(
        "INSERT INTO reminder_scheduler_runs(run_key,trigger_type,status,started_at,created_at) VALUES(?,?,?,?,?)",
        ("recent", "manual", "running", recent.isoformat(), recent.isoformat()),
    )
    result = rs.run_tick(conn, now=NOW, run_key="after-recent")
    assert result["recovered_run_count"] == 0
    assert conn.execute("SELECT status FROM reminder_scheduler_runs WHERE run_key='recent'").fetchone()["status"] == "running"


def test_unknown_trigger_is_rejected(conn):
    with pytest.raises(rs.SchedulerError):
        rs.run_tick(conn, now=NOW, trigger_type="cron")

def test_daily_review_catchup_boundary_creates_at_two_hours_and_skips_after(tmp_path, monkeypatch):
    import sqlite3

    from app.main import init_db
    db_path = tmp_path / "catchup.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    rm.update_settings(c, {"daily_review_enabled": True}, now=NOW)
    at_boundary = datetime(2026, 10, 13, 13, 59, tzinfo=UTC)  # 21:59 SH, inside the [20:00,22:00) catch-up window
    result = rs.run_tick(c, now=at_boundary, run_key="daily-boundary")
    assert result["created_count"] == 1
    # run_tick 释放租约时用真实时钟写入 expires_at，而本测试使用固定的历史业务时间
    # (2026-10-13)，真实时钟晚于该时间时租约会显示“未过期”，导致下一次 tick 误报
    # LeaseBusyError（日期敏感缺陷）。此处将租约置为已过期，模拟真实场景中上一租约
    # 已过期、下一次 tick 重新获取租约的正常状态。
    c.execute(
        "UPDATE reminder_scheduler_leases SET expires_at='2000-01-01T00:00:00+00:00', "
        "released_at='2000-01-01T00:00:00+00:00' WHERE scope='global'"
    )
    c.commit()
    late = datetime(2026, 10, 13, 14, 1, tzinfo=UTC)
    result2 = rs.run_tick(c, now=late, run_key="daily-late")
    assert result2["created_count"] == 0
    assert result2["skipped_count"] == 0
    c.close()


def test_weekly_review_uses_24_hour_catchup_and_does_not_backfill_old_weeks(tmp_path, monkeypatch):
    import sqlite3

    from app.main import init_db
    db_path = tmp_path / "weekly.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    rm.update_settings(c, {"weekly_review_enabled": True}, now=NOW)
    # 2026-12-29 09:01 Shanghai is 24h1m after Monday 2026-12-28 09:00.
    late = datetime(2026, 12, 29, 1, 1, tzinfo=UTC)
    result = rs.run_tick(c, now=late, run_key="weekly-late")
    assert result["created_count"] == 0
    c.close()


def test_partial_failure_when_one_source_is_invalid_but_other_sources_continue(conn):
    # A malformed accepted event cannot provide accepted_at; the recommendation source records
    # an error while an otherwise empty tick still completes as partial_failed.
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "建议", "内容", "task", "normal", "accepted", "[]", "{}", "ok", "", "low", "", 0.8,
         "rule", "test", "test", "rec-1", "2026-12-31T00:00:00+00:00",
         "bad-time", "bad-time", "bad-time"),
    )
    rm.update_settings(conn, {"recommendation_followup_enabled": True}, now=NOW)
    result = rs.run_tick(conn, now=NOW, run_key="partial-bad-rec")
    assert result["status"] == "partial_failed"
    assert result["error_count"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 1


def test_separate_connections_can_run_ticks_and_unique_run_keys_remain_stable(conn, tmp_path):
    """SQLite connections must not be shared across threads; durable uniqueness is in tables."""
    import sqlite3
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    other = sqlite3.connect(db_path, timeout=2)
    other.row_factory = sqlite3.Row
    other.execute("PRAGMA foreign_keys=ON")
    try:
        first = rs.run_tick(conn, now=NOW, run_key="connection-1")
        second = rs.run_tick(other, now=NOW + timedelta(minutes=1), run_key="connection-2")
        duplicate = rs.run_tick(other, now=NOW + timedelta(minutes=2), run_key="connection-1")
    finally:
        other.close()
    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert duplicate["id"] == first["id"]
    assert conn.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 2
