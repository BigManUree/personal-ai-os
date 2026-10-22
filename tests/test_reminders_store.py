"""1.3.5-B reminder settings and storage service tests.

All tests use an isolated temp SQLite file and point both DATABASE_PATH and the
already-imported app.main.DB_PATH at that file.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import reminders as rm
from app.main import init_db

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)
SH = timezone(timedelta(hours=8))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_5_reminders_store.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    init_db()  # settings seed and schema are idempotent
    connection = sqlite3.connect(db_path, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def _task(conn, title="任务", status="todo", due="2026-10-20", updated=None):
    updated = updated or NOW
    conn.execute(
        "INSERT INTO tasks(title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        (title, status, "normal", due, rm.iso(NOW), rm.iso(updated)),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _review_draft(scheduled=NOW, expires=None, key="review.daily_due_v1:2026-10-12T16:00:00+00:00"):
    return {
        "rule_code": rm.RULE_REVIEW_DAILY,
        "source_type": "review",
        "title": "每日复盘",
        "content": "可以回顾上一个本地自然日。",
        "evidence": [{
            "kind": "review_window",
            "label": "窗口",
            "value": "2026-10-12..2026-10-13",
            "source_type": "review_period",
            "coverage_status": "no_data",
            "detail": "测试证据",
            "definition_version": rm.RULE_VERSION,
            "window_start": scheduled - timedelta(days=1),
            "window_end": scheduled,
        }],
        "coverage_status": "no_data",
        "dedup_key": key,
        "scheduled_at": scheduled,
        "expires_at": expires or scheduled + timedelta(hours=2),
    }


def test_default_settings_all_rules_are_off_and_singleton(conn):
    settings = rm.get_settings(conn)
    assert settings["timezone"] == "Asia/Shanghai"
    assert settings["task_due_enabled"] is False
    assert settings["task_stalled_enabled"] is False
    assert settings["recommendation_followup_enabled"] is False
    assert settings["daily_review_enabled"] is False
    assert settings["weekly_review_enabled"] is False
    assert settings["daily_review_at"] == "20:00"
    assert settings["weekly_review_weekday"] == 1
    assert settings["weekly_review_at"] == "09:00"
    assert conn.execute("SELECT COUNT(*) FROM reminder_settings").fetchone()[0] == 1


def test_settings_update_validates_and_persists_allowed_fields(conn):
    updated = rm.update_settings(conn, {
        "timezone": "UTC+09:00",
        "task_due_enabled": True,
        "daily_review_at": "21:30",
        "weekly_review_weekday": 7,
        "quiet_hours_enabled": True,
        "quiet_start": "23:00",
        "quiet_end": "01:00",
    }, now=NOW)
    assert updated["timezone"] == "UTC+09:00"
    assert updated["task_due_enabled"] is True
    assert updated["daily_review_at"] == "21:30"
    assert updated["weekly_review_weekday"] == 7
    assert updated["updated_at"] == rm.iso(NOW)


def test_settings_reject_invalid_timezone_clock_values_and_unknown_fields(conn):
    for payload in (
        {"timezone": "America/New_York"},
        {"daily_review_at": "24:00"},
        {"weekly_review_weekday": 0},
        {"quiet_start": "9点"},
        {"created_at": "2026-01-01T00:00:00Z"},
    ):
        with pytest.raises(rm.ReminderError):
            rm.update_settings(conn, payload)


def test_settings_reject_equal_quiet_start_and_end_when_enabled(conn):
    with pytest.raises(rm.ReminderError) as exc:
        rm.update_settings(conn, {"quiet_hours_enabled": True, "quiet_start": "22:00", "quiet_end": "22:00"})
    assert exc.value.code == "invalid_settings"


def test_create_get_list_and_dedup_reminder(conn):
    created = rm.create_reminder(conn, _review_draft(), now=NOW)
    again = rm.create_reminder(conn, _review_draft(), now=NOW)
    assert again["id"] == created["id"]
    assert rm.get_reminder(conn, created["id"])["dedup_key"] == created["dedup_key"]
    assert len(rm.list_reminders(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_create_reminder_validates_source_enums_lengths_secret_and_evidence(conn):
    task_id = _task(conn)
    good = _review_draft()
    with pytest.raises(rm.ReminderError):
        bad = {**good, "rule_code": "not.allowed", "source_type": "review"}
        rm.create_reminder(conn, bad)
    with pytest.raises(rm.ReminderError):
        rm.create_reminder(conn, {**good, "title": "x" * 201, "dedup_key": "k1"})
    with pytest.raises(rm.ReminderError):
        rm.create_reminder(conn, {**good, "content": "api_key=abcdefgh1234567890", "dedup_key": "k2"})
    with pytest.raises(rm.ReminderError):
        bad_evidence = {**good, "dedup_key": "k3", "evidence": [{**good["evidence"][0], "kind": "secret"}]}
        rm.create_reminder(conn, bad_evidence)
    with pytest.raises(rm.ReminderError) as exc:
        task_draft = {
            **good,
            "rule_code": rm.RULE_TASK_DUE_SOON,
            "source_type": "task",
            "source_id": 999999,
            "dedup_key": "task-missing",
        }
        rm.create_reminder(conn, task_draft)
    assert exc.value.status_code == 404
    assert task_id  # fixture keeps a task row for foreign-key tests below


def test_task_reminder_foreign_key_and_cascade_delete(conn):
    task_id = _task(conn)
    scheduled = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)
    draft = {
        "rule_code": rm.RULE_TASK_DUE_SOON,
        "source_type": "task",
        "source_id": task_id,
        "title": "即将到期",
        "content": "任务即将到期",
        "evidence": [{
            "kind": "task", "label": "截止日期", "value": "2026-10-20",
            "source_type": "tasks", "source_id": task_id, "coverage_status": "ok",
            "detail": "外键级联测试", "definition_version": rm.RULE_VERSION,
        }],
        "coverage_status": "ok",
        "dedup_key": f"task.due_soon_v1:{task_id}:2026-10-20",
        "scheduled_at": scheduled,
        "expires_at": scheduled + timedelta(days=1),
    }
    reminder = rm.create_reminder(conn, draft)
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    assert rm.get_reminder_by_dedup(conn, reminder["dedup_key"]) is None
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_database_unique_constraint_and_concurrent_creators(conn, rollback_journal):
    # A brand-new WAL database plus a simultaneous first write can fail one thread
    # with "attempt to write a readonly database"; this test is about the dedup
    # unique constraint, so use the rollback journal.
    rollback_journal(conn)
    draft = _review_draft(key="concurrent-key")
    errors = []
    barrier = threading.Barrier(3)

    def worker(path):
        barrier.wait()
        try:
            local = sqlite3.connect(path, timeout=1, isolation_level=None)
            local.row_factory = sqlite3.Row
            local.execute("PRAGMA foreign_keys=ON")
            rm.create_reminder(local, draft)
            local.close()
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(conn.execute("PRAGMA database_list").fetchone()["file"],))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert conn.execute("SELECT COUNT(*) FROM reminders WHERE dedup_key='concurrent-key'").fetchone()[0] == 1


def test_transaction_rollback_keeps_settings_and_no_partial_reminder(conn):
    before = rm.get_settings(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("BEGIN")
        conn.execute("UPDATE reminder_settings SET daily_review_at='07:10' WHERE id=1")
        conn.execute(
            "INSERT INTO reminders(rule_code,rule_version,source_type,title,content,dedup_key,"
            "scheduled_at,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("bad", rm.RULE_VERSION, "review", "bad", "bad", "rollback", rm.iso(NOW),
             rm.iso(NOW + timedelta(days=1)), rm.iso(NOW), rm.iso(NOW)),
        )
        conn.execute("COMMIT")
    rm.get_settings(conn)["daily_review_at"] == before["daily_review_at"]
    assert rm.get_reminder_by_dedup(conn, "rollback") is None
