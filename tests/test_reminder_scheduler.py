"""RED: 1.3.5-C-B reminder scheduler core tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import reminder_scheduler as rs
from app import reminders as rm
from app.main import init_db

UTC = timezone.utc
SH = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)  # 10:00 Shanghai


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    connection = sqlite3.connect(db_path, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def _task(conn, *, status="todo", due="2026-10-21", updated=None):
    updated = updated or NOW - timedelta(days=1)
    conn.execute(
        "INSERT INTO tasks(title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("任务", status, "normal", due, rm.iso(updated), rm.iso(updated)),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _review_reminder(conn, *, scheduled_at, expires_at, status="scheduled", key=None, snoozed_until=None):
    key = key or f"review:test:{rm.iso(scheduled_at)}"
    ts = rm.iso(NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,title,content,priority,status,"
        "evidence_json,coverage_status,dedup_key,scheduled_at,snoozed_until,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rm.RULE_REVIEW_DAILY, rm.RULE_VERSION, "review", "每日复盘", "内容", "normal", status, "[]",
         "no_data", key, rm.iso(scheduled_at), rm.iso(snoozed_until) if snoozed_until else None,
         rm.iso(expires_at), ts, ts),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_empty_tick_creates_succeeded_run_and_no_reminders(conn):
    result = rs.run_tick(conn, now=NOW, run_key="run-empty")
    assert result["run_id"] > 0
    assert result["status"] == "succeeded"
    assert result["created_count"] == 0
    row = conn.execute("SELECT status FROM reminder_scheduler_runs WHERE id=?", (result["run_id"],)).fetchone()
    assert row["status"] == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_duplicate_run_key_returns_existing_without_changing_reminders(conn):
    first = rs.run_tick(conn, now=NOW, run_key="same-key")
    second = rs.run_tick(conn, now=NOW + timedelta(minutes=1), run_key="same-key")
    assert second["run_id"] == first["run_id"]
    assert second["status"] == "succeeded"
    assert conn.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 1


def test_tick_creates_due_soon_candidate_once_and_marks_it_due(conn):
    _task(conn, due="2026-10-21")  # due-soon scheduled at 2026-10-20 01:00 UTC
    rm.update_settings(conn, {"task_due_enabled": True}, now=NOW)
    first = rs.run_tick(conn, now=NOW, run_key="run-1")
    second = rs.run_tick(conn, now=NOW + timedelta(minutes=5), run_key="run-2")
    assert first["created_count"] == 1
    assert first["due_count"] == 1
    assert second["created_count"] == 0
    assert second["skipped_count"] == 1
    rows = conn.execute("SELECT status,dedup_key FROM reminders").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "due"


def test_expiry_has_priority_over_due(conn):
    expired_at = NOW - timedelta(minutes=1)
    rid = _review_reminder(conn, scheduled_at=NOW - timedelta(hours=1), expires_at=expired_at)
    result = rs.run_tick(conn, now=NOW, run_key="run-expiry")
    assert result["expired_count"] == 1
    assert result["due_count"] == 0
    assert rm.get_reminder(conn, rid)["status"] == "expired"


def test_quiet_hours_holds_scheduled_due_transition_until_quiet_ends(conn):
    rm.update_settings(conn, {"daily_review_enabled": True, "quiet_hours_enabled": True, "quiet_start": "09:00", "quiet_end": "11:00"}, now=NOW)
    rid = _review_reminder(conn, scheduled_at=NOW - timedelta(minutes=1),
                           expires_at=NOW + timedelta(hours=3), key="review:quiet")
    result = rs.run_tick(conn, now=NOW, run_key="run-quiet")  # 10:00 Shanghai
    assert result["due_count"] == 0
    assert result["skipped_count"] == 1
    assert rm.get_reminder(conn, rid)["status"] == "scheduled"
    later = datetime(2026, 10, 20, 3, 1, tzinfo=UTC)  # 11:01 Shanghai
    result2 = rs.run_tick(conn, now=later, run_key="run-quiet-after")
    assert result2["due_count"] == 1
    assert rm.get_reminder(conn, rid)["status"] == "due"


def test_stale_running_run_is_recovered_before_new_tick(conn):
    old_start = rm.iso(NOW - timedelta(minutes=6))
    conn.execute(
        "INSERT INTO reminder_scheduler_runs(run_key,trigger_type,status,started_at,owner_token,created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("old", "manual", "running", old_start, "", old_start),
    )
    result = rs.run_tick(conn, now=NOW, run_key="new")
    assert result["recovered_run_count"] == 1
    assert conn.execute("SELECT status FROM reminder_scheduler_runs WHERE run_key='old'").fetchone()["status"] == "abandoned"


def test_invalid_reference_time_is_rejected(conn):
    with pytest.raises(rs.SchedulerError):
        rs.run_tick(conn, now=datetime(2026, 10, 20, 2, 0), run_key="bad-now")


def test_snoozed_reminder_becomes_due_when_snooze_time_reaches(conn):
    rm.update_settings(conn, {"daily_review_enabled": True}, now=NOW)
    later = NOW + timedelta(hours=1)
    rid = _review_reminder(conn, scheduled_at=NOW - timedelta(hours=2),
                           expires_at=NOW + timedelta(hours=3), status="snoozed",
                           key="review:snoozed", snoozed_until=later - timedelta(minutes=1))
    result = rs.run_tick(conn, now=later, run_key="run-snoozed")
    assert result["due_count"] == 1
    assert rm.get_reminder(conn, rid)["status"] == "due"


def test_task_reschedule_cancels_old_due_date_reminder(conn):
    task_id = _task(conn, due="2026-10-21")
    rm.update_settings(conn, {"task_due_enabled": True}, now=NOW)
    rs.run_tick(conn, now=NOW, run_key="before-reschedule")
    conn.execute("UPDATE tasks SET due_date=?, updated_at=? WHERE id=?",
                 ("2026-10-25", rm.iso(NOW + timedelta(minutes=1)), task_id))
    result = rs.run_tick(conn, now=NOW + timedelta(minutes=2), run_key="after-reschedule")
    old = conn.execute("SELECT status,cancel_reason FROM reminders WHERE source_id=? AND dedup_key LIKE ?",
                       (task_id, "%:2026-10-21%")).fetchone()
    assert old["status"] == "cancelled"
    assert old["cancel_reason"] == "source_rescheduled"
    assert result["cancelled_count"] >= 1


def test_completed_task_cancels_active_task_reminder(conn):
    task_id = _task(conn, due="2026-10-21")
    rm.update_settings(conn, {"task_due_enabled": True}, now=NOW)
    rs.run_tick(conn, now=NOW, run_key="before-done")
    conn.execute("UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
                 (rm.iso(NOW + timedelta(minutes=1)), rm.iso(NOW + timedelta(minutes=1)), task_id))
    result = rs.run_tick(conn, now=NOW + timedelta(minutes=2), run_key="after-done")
    assert result["cancelled_count"] >= 1
    assert all(r["status"] == "cancelled" and r["cancel_reason"] == "source_completed"
               for r in conn.execute("SELECT * FROM reminders WHERE source_id=?", (task_id,)))


def test_disabled_daily_review_cancels_future_scheduled_review_but_keeps_due(conn):
    future = NOW + timedelta(days=1)
    scheduled_id = _review_reminder(conn, scheduled_at=future, expires_at=future + timedelta(hours=2),
                                    status="scheduled", key="review:future")
    due_id = _review_reminder(conn, scheduled_at=NOW - timedelta(minutes=10),
                              expires_at=NOW + timedelta(hours=2), status="due", key="review:due")
    result = rs.run_tick(conn, now=NOW, run_key="disable-review")
    assert rm.get_reminder(conn, scheduled_id)["status"] == "cancelled"
    assert rm.get_reminder(conn, due_id)["status"] == "due"
    assert result["cancelled_count"] == 1



def _rec(conn, rec_id, *, status="accepted", expires="2026-12-31T00:00:00+00:00", reviewed=None):
    reviewed = reviewed or "2026-10-06T11:00:00+00:00"
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec_id, "建议", "内容", "task", "normal", status, "[]", "{}", "ok", "", "low", "", 0.8,
         "rule", "test", "test", f"rec-{rec_id}", expires, reviewed, reviewed, reviewed),
    )


def test_followup_created_on_day7_and_duplicate_tick_is_skipped(conn):
    rm.update_settings(conn, {"recommendation_followup_enabled": True}, now=NOW)
    _rec(conn, 1, reviewed="2026-10-13T11:00:00+00:00")
    at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    first = rs.run_tick(conn, now=at, run_key="followup-1")
    second = rs.run_tick(conn, now=at + timedelta(minutes=5), run_key="followup-2")
    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert second["skipped_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_outcome_cancels_existing_followup_and_stops_new_candidate(conn):
    rm.update_settings(conn, {"recommendation_followup_enabled": True}, now=NOW)
    _rec(conn, 1, reviewed="2026-10-13T11:00:00+00:00")
    at = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    rs.run_tick(conn, now=at, run_key="before-outcome")
    conn.execute(
        "INSERT INTO recommendation_outcomes(recommendation_id,execution_status,result_rating,"
        "actual_effect_note,metric_observations_json,causality_status,source,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (1, "completed", "unclear", "", "[]", "not_inferred", "user", rm.iso(at), rm.iso(at)),
    )
    result = rs.run_tick(conn, now=at + timedelta(minutes=5), run_key="after-outcome")
    row = conn.execute("SELECT status,cancel_reason FROM reminders").fetchone()
    assert row["status"] == "cancelled"
    assert row["cancel_reason"] == "outcome_registered"
    assert result["cancelled_count"] == 1



def test_quiet_deferral_that_crosses_expiry_expires_instead_of_waiting(conn):
    rm.update_settings(conn, {"daily_review_enabled": True, "quiet_hours_enabled": True,
                              "quiet_start": "09:00", "quiet_end": "11:00"}, now=NOW)
    rid = _review_reminder(conn, scheduled_at=NOW - timedelta(minutes=10),
                           expires_at=NOW + timedelta(minutes=1), key="review:quiet-expired")
    result = rs.run_tick(conn, now=NOW, run_key="quiet-expired")
    assert result["due_count"] == 0
    assert result["expired_count"] == 1
    assert rm.get_reminder(conn, rid)["status"] == "expired"


def test_disabling_followup_preserves_already_due_reminder(conn):
    rm.update_settings(conn, {"recommendation_followup_enabled": True}, now=NOW)
    accepted = datetime(2026, 10, 13, 11, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "建议", "内容", "task", "normal", "accepted", "[]", "{}", "ok", "", "low", "", 0.8,
         "rule", "test", "test", "rec-1", "2026-12-31T00:00:00+00:00",
         rm.iso(accepted), rm.iso(accepted), rm.iso(accepted)),
    )
    rs.run_tick(conn, now=datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc), run_key="enabled")
    assert conn.execute("SELECT status FROM reminders").fetchone()[0] == "due"
    rm.update_settings(conn, {"recommendation_followup_enabled": False}, now=NOW)
    result = rs.run_tick(conn, now=NOW, run_key="disabled")
    assert result["cancelled_count"] == 0
    assert conn.execute("SELECT status FROM reminders").fetchone()[0] == "due"


def test_snooze_sets_wake_time_and_tick_waits_for_it(conn):
    """Regression: snooze used to leave snoozed_until NULL, so the next tick
    treated the reminder as ready and bounced it straight back to due."""
    _task(conn, due="2026-10-21")
    rm.update_settings(conn, {"task_due_enabled": True}, now=NOW)
    first = rs.run_tick(conn, now=NOW, run_key="snooze-create")
    assert first["due_count"] == 1
    rid = conn.execute("SELECT id FROM reminders").fetchone()["id"]

    snoozed = rm.transition_reminder(conn, rid, "snooze", now=NOW, snooze_minutes=15)
    assert snoozed["status"] == "snoozed"
    assert snoozed["snoozed_until"] == rm.iso(NOW + timedelta(minutes=15))

    waiting = rs.run_tick(conn, now=NOW + timedelta(minutes=5), run_key="snooze-wait")
    assert waiting["due_count"] == 0
    assert rm.get_reminder(conn, rid)["status"] == "snoozed"

    woken = rs.run_tick(conn, now=NOW + timedelta(minutes=20), run_key="snooze-wake")
    assert woken["due_count"] == 1
    assert rm.get_reminder(conn, rid)["status"] == "due"


def test_snooze_rejects_out_of_range_minutes(conn):
    rid = _review_reminder(conn, scheduled_at=NOW - timedelta(minutes=5),
                           expires_at=NOW + timedelta(days=1), status="due")
    for bad in (0, -1, 7 * 24 * 60 + 1, True, "15"):
        with pytest.raises(rm.ReminderError) as exc:
            rm.transition_reminder(conn, rid, "snooze", now=NOW, snooze_minutes=bad)
        assert exc.value.code == "invalid_snooze"


