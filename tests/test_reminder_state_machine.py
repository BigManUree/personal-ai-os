"""1.3.5-B reminder state-machine, expiry and concurrency tests."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import reminders as rm
from app.main import init_db

NOW = datetime(2026, 10, 13, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_5_reminder_state.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    connection = sqlite3.connect(db_path, timeout=3, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def _draft(key="state-key", scheduled=NOW, expires=None, **overrides):
    draft = {
        "rule_code": rm.RULE_REVIEW_DAILY,
        "source_type": "review",
        "title": "每日复盘",
        "content": "请进行每日复盘。",
        "evidence": [{
            "kind": "review_window",
            "label": "窗口",
            "value": "2026-10-12..2026-10-13",
            "source_type": "review_period",
            "coverage_status": "no_data",
            "detail": "状态机测试",
            "definition_version": rm.RULE_VERSION,
            "window_start": scheduled - timedelta(days=1),
            "window_end": scheduled,
        }],
        "coverage_status": "no_data",
        "dedup_key": key,
        "scheduled_at": scheduled,
        "expires_at": expires or scheduled + timedelta(days=1),
    }
    draft.update(overrides)
    return draft


def _create(conn, key="state-key", **overrides):
    return rm.create_reminder(conn, _draft(key=key, **overrides), now=NOW)


def test_legal_scheduled_due_acknowledge_complete_path(conn):
    created = _create(conn)
    due = rm.transition_reminder(conn, created["id"], "mark_due", now=NOW)
    assert due["status"] == "due"
    assert due["due_at"] == rm.iso(NOW)
    acknowledged = rm.transition_reminder(conn, created["id"], "acknowledge", now=NOW + timedelta(minutes=1))
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["acknowledged_at"]
    completed = rm.transition_reminder(conn, created["id"], "complete", now=NOW + timedelta(minutes=2))
    assert completed["status"] == "completed"
    assert completed["completed_at"] and completed["resolved_at"]


def test_snooze_returns_to_due_and_can_dismiss(conn):
    created = _create(conn, "snooze")
    rm.transition_reminder(conn, created["id"], "mark_due", now=NOW)
    snoozed = rm.snooze_reminder(conn, created["id"], NOW + timedelta(hours=2), now=NOW)
    assert snoozed["status"] == "snoozed"
    assert snoozed["snoozed_until"] == rm.iso(NOW + timedelta(hours=2))
    due = rm.transition_reminder(conn, created["id"], "mark_due", now=NOW + timedelta(hours=2))
    assert due["status"] == "due"
    dismissed = rm.transition_reminder(conn, created["id"], "dismiss", now=NOW + timedelta(hours=3))
    assert dismissed["status"] == "dismissed"
    assert dismissed["resolved_at"]


def test_cancel_and_expire_paths_set_reason_and_terminal_state(conn):
    cancelled = _create(conn, "cancel")
    result = rm.transition_reminder(conn, cancelled["id"], "cancel", now=NOW,
                                    cancel_reason="settings_disabled")
    assert result["status"] == "cancelled"
    assert result["cancel_reason"] == "settings_disabled"
    assert result["cancelled_at"] and result["resolved_at"]

    expired = _create(conn, "expire", expires=NOW + timedelta(minutes=1))
    result = rm.transition_reminder(conn, expired["id"], "expire", now=NOW + timedelta(minutes=1))
    assert result["status"] == "expired"
    assert result["cancel_reason"] == "expiry_reached"


def test_invalid_actions_transitions_and_cancel_reasons_are_409_or_400(conn):
    created = _create(conn, "invalid")
    for action in ("acknowledge", "dismiss", "complete", "snooze"):
        with pytest.raises(rm.ReminderError) as exc:
            if action == "snooze":
                rm.snooze_reminder(conn, created["id"], NOW + timedelta(hours=1), now=NOW)
            else:
                rm.transition_reminder(conn, created["id"], action, now=NOW)
        assert exc.value.status_code == 409
    with pytest.raises(rm.ReminderError) as exc:
        rm.transition_reminder(conn, created["id"], "accept", now=NOW)
    assert exc.value.status_code == 400
    with pytest.raises(rm.ReminderError):
        rm.transition_reminder(conn, created["id"], "cancel", now=NOW, cancel_reason="nope")


def test_terminal_states_cannot_revive_and_snooze_requires_future_time(conn):
    created = _create(conn, "terminal")
    rm.transition_reminder(conn, created["id"], "cancel", now=NOW)
    for action in ("mark_due", "snooze", "acknowledge", "complete", "dismiss", "expire"):
        with pytest.raises(rm.ReminderError) as exc:
            if action == "snooze":
                rm.snooze_reminder(conn, created["id"], NOW + timedelta(hours=1), now=NOW)
            else:
                rm.transition_reminder(conn, created["id"], action, now=NOW)
        assert exc.value.status_code == 409
    due = _create(conn, key="past-snooze")
    rm.transition_reminder(conn, due["id"], "mark_due", now=NOW)
    with pytest.raises(rm.ReminderError):
        rm.snooze_reminder(conn, due["id"], NOW - timedelta(minutes=1), now=NOW)


def test_idempotent_terminal_operations_return_same_record(conn):
    created = _create(conn, "idempotent")
    rm.transition_reminder(conn, created["id"], "mark_due", now=NOW)
    first = rm.transition_reminder(conn, created["id"], "dismiss", now=NOW + timedelta(minutes=1))
    second = rm.transition_reminder(conn, created["id"], "dismiss", now=NOW + timedelta(minutes=2))
    assert first["updated_at"] == second["updated_at"]
    assert second["status"] == "dismissed"


def test_concurrent_transitions_only_one_wins(conn):
    created = _create(conn, "concurrent-transition")
    rm.transition_reminder(conn, created["id"], "mark_due", now=NOW)
    db_file = conn.execute("PRAGMA database_list").fetchone()["file"]
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(action):
        barrier.wait()
        local = sqlite3.connect(db_file, timeout=5, isolation_level=None)
        local.row_factory = sqlite3.Row
        local.execute("PRAGMA foreign_keys=ON")
        try:
            result = rm.transition_reminder(local, created["id"], action, now=NOW + timedelta(minutes=1))
            results.append(result["status"])
        except Exception as exc:
            errors.append(exc)
        finally:
            local.close()

    threads = [
        threading.Thread(target=worker, args=("dismiss",)),
        threading.Thread(target=worker, args=("complete",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 1
    assert results[0] in {"dismissed", "completed"}
    assert len(errors) == 1
    assert errors[0].status_code == 409
    final = rm.get_reminder(conn, created["id"])
    assert final["status"] == results[0]


def test_expire_reminder_bulk_helper_respects_expires_at(conn):
    active = _create(conn, "bulk-expire", expires=NOW + timedelta(minutes=1))
    future = _create(conn, key="bulk-future", expires=NOW + timedelta(days=1))
    # Simulate reaching expiry after a previously scheduled row was persisted;
    # creating an already-expired row is intentionally rejected by the draft model.
    conn.execute("UPDATE reminders SET expires_at=? WHERE id=?", (rm.iso(NOW - timedelta(minutes=1)), active["id"]))
    count = rm.expire_reminders(conn, now=NOW)
    assert count == 1
    assert rm.get_reminder(conn, active["id"])["status"] == "expired"
    assert rm.get_reminder(conn, future["id"])["status"] == "scheduled"


def test_unknown_id_is_404_and_direct_invalid_sql_status_is_rejected(conn):
    with pytest.raises(rm.ReminderError) as exc:
        rm.get_reminder(conn, 9999)
    assert exc.value.status_code == 404
    created = _create(conn, "check-constraint")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE reminders SET status='accepted_email' WHERE id=?", (created["id"],))
    assert rm.get_reminder(conn, created["id"])["status"] == "scheduled"


def test_source_invalidation_cancel_is_service_controlled_with_reason(conn):
    conn.execute(
        "INSERT INTO tasks(title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("任务", "doing", "normal", "2026-10-20", rm.iso(NOW), rm.iso(NOW)),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    reminder = rm.create_reminder(conn, _draft(
        key="task-cancel",
        rule_code=rm.RULE_TASK_DUE_SOON,
        source_type="task",
        source_id=task_id,
    ), now=NOW)
    cancelled = rm.transition_reminder(conn, reminder["id"], "cancel", now=NOW,
                                       cancel_reason="source_completed")
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"] == "source_completed"
