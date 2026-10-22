"""RED reliability regressions for 1.3.5-C-B-R."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import reminder_scheduler as rs
from app import reminders as rm
from app.main import init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "scheduler_r.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    connection = sqlite3.connect(db_path, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def _task(conn, *, task_id=1, status="todo", due="2026-10-21", updated=None):
    updated = updated or NOW - timedelta(days=1)
    conn.execute(
        "INSERT INTO tasks(id,title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (task_id, "任务", status, "normal", due, rm.iso(updated), rm.iso(updated)),
    )
    return task_id


def _settings(conn, **changes):
    rm.update_settings(conn, changes, now=NOW)


def _insert_task_reminder(conn, task_id, rule, due, *, dedup=None, status="scheduled",
                          evidence=None, scheduled=None, expires=None):
    scheduled = scheduled or NOW
    expires = expires or NOW + timedelta(days=2)
    evidence = evidence if evidence is not None else [
        {"kind": "task", "label": "任务截止日期", "value": due, "source_type": "tasks",
         "source_id": task_id, "window_start": None, "window_end": None,
         "coverage_status": "ok", "detail": "旧", "definition_version": rm.RULE_VERSION}
    ]
    dedup = dedup or f"{rule}:{task_id}:{due}"
    ts = rm.iso(NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,source_id,task_id,title,content,"
        "priority,status,evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rule, rm.RULE_VERSION, "task", task_id, task_id, "标题", "内容", "normal", status,
         json.dumps(evidence, ensure_ascii=False), "ok", dedup, rm.iso(scheduled), rm.iso(expires), ts, ts),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_structured_due_date_snapshot_takes_precedence_over_dedup_key(conn):
    _settings(conn, task_due_enabled=True)
    task_id = _task(conn, due="2026-10-25")
    # Evidence snapshot says old date 2026-10-21 even though malformed dedup contains the new date.
    rid = _insert_task_reminder(conn, task_id, rm.RULE_TASK_DUE_SOON, "2026-10-21",
                                dedup="task.due_soon_v1:1:2026-10-25")
    result = rs.run_tick(conn, now=NOW, run_key="snapshot-wins")
    saved = rm.get_reminder(conn, rid)
    assert saved["status"] == "cancelled"
    assert saved["cancel_reason"] == "source_rescheduled"
    assert result["cancelled_count"] == 1


def test_malformed_legacy_dedup_key_is_not_cancelled_on_reschedule(conn):
    _settings(conn, task_due_enabled=True)
    task_id = _task(conn, due="2026-10-25")
    future = NOW + timedelta(days=2)
    rid = _insert_task_reminder(conn, task_id, rm.RULE_TASK_DUE_SOON, "2026-10-21",
                                evidence=[], dedup="not-a-valid-reminder-key",
                                scheduled=future, expires=future + timedelta(days=1))
    rs.run_tick(conn, now=NOW, run_key="bad-legacy-key")
    saved = rm.get_reminder(conn, rid)
    assert saved["status"] == "scheduled"
    assert saved["cancel_reason"] is None


def test_stalled_reminder_is_not_cancelled_when_due_date_changes(conn):
    _settings(conn, task_due_enabled=True, task_stalled_enabled=True)
    task_id = _task(conn, status="doing", due="2026-10-25", updated=NOW - timedelta(days=10))
    ts = rm.iso(NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,source_id,task_id,title,content,"
        "priority,status,evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rm.RULE_TASK_STALLED, rm.RULE_VERSION, "task", task_id, task_id, "停滞", "内容", "normal",
         "scheduled", "[]", "partial_data", f"{rm.RULE_TASK_STALLED}:{task_id}:2026-W42:1",
         rm.iso(NOW + timedelta(days=1)), rm.iso(NOW + timedelta(days=5)), ts, ts),
    )
    rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    rs.run_tick(conn, now=NOW, run_key="stalled-kept")
    assert rm.get_reminder(conn, rid)["status"] == "scheduled"


def test_savepoint_rejects_unsafe_names_and_rollback_keeps_connection_usable(conn):
    with pytest.raises(rs.SchedulerError):
        with rs.savepoint(conn, "bad name; DROP TABLE tasks"):
            pass
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    conn.execute("INSERT INTO tasks(title,status,priority,created_at,updated_at) VALUES(?,?,?,?,?)",
                 ("x", "todo", "normal", rm.iso(NOW), rm.iso(NOW)))
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_savepoint_rolls_back_inner_write_preserves_outer_and_keeps_original_exception(conn):
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO tasks(title,status,priority,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("outer", "todo", "normal", rm.iso(NOW), rm.iso(NOW)),
    )
    original = sqlite3.DatabaseError("token=abcdef1234567890 inner failure")
    with pytest.raises(sqlite3.DatabaseError, match="inner failure") as exc_info:
        with rs.savepoint(conn, "reliability_sp"):
            conn.execute(
                "INSERT INTO tasks(title,status,priority,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("inner", "todo", "normal", rm.iso(NOW), rm.iso(NOW)),
            )
            assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title='inner'").fetchone()[0] == 1
            raise original
    assert exc_info.value is original
    conn.commit()
    titles = {row[0] for row in conn.execute("SELECT title FROM tasks")}
    assert titles == {"outer"}
    conn.execute(
        "INSERT INTO tasks(title,status,priority,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("after", "todo", "normal", rm.iso(NOW), rm.iso(NOW)),
    )
    assert {row[0] for row in conn.execute("SELECT title FROM tasks")} == {"outer", "after"}


def test_one_bad_candidate_does_not_roll_back_sibling_candidate_in_same_task(conn, monkeypatch):
    _settings(conn, task_due_enabled=True)
    _task(conn, due="2026-10-21")
    original = rm.task_overdue_candidate
    calls = {"n": 0}
    def flaky_second_overdue(task, settings, *, now, occurrence):
        calls["n"] += 1
        if occurrence == 2:
            raise sqlite3.DatabaseError("injected candidate failure")
        return original(task, settings, now=now, occurrence=occurrence)
    monkeypatch.setattr(rs.rm, "task_overdue_candidate", flaky_second_overdue)
    result = rs.run_tick(conn, now=NOW, run_key="sibling-candidate")
    assert result["status"] == "partial_failed"
    rules = {row["rule_code"] for row in conn.execute("SELECT rule_code FROM reminders")}
    assert rm.RULE_TASK_DUE_SOON in rules
    assert result["created_count"] >= 1
    assert result["error_count"] >= 1


def test_create_reminder_result_distinguishes_insert_from_duplicate(conn):
    settings = {**rm.get_settings(conn), "task_due_enabled": True}
    candidate = rm.task_due_soon_candidate(
        {"id": 1, "status": "todo", "due_date": "2026-10-21"}, settings, now=NOW)
    assert candidate is not None
    _task(conn)
    first = rm.create_reminder_result(conn, candidate.to_draft(), now=NOW)
    second = rm.create_reminder_result(conn, candidate.to_draft(), now=NOW)
    assert first.created is True
    assert second.created is False
    assert second.reminder["id"] == first.reminder["id"]
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1


def test_scheduler_duplicate_creation_across_connections_counts_skipped(tmp_path, monkeypatch):
    db_path = tmp_path / "dup.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    c1 = sqlite3.connect(db_path, timeout=2)
    c2 = sqlite3.connect(db_path, timeout=2)
    for c in (c1, c2):
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
    try:
        rm.update_settings(c1, {"task_due_enabled": True}, now=NOW)
        c1.execute(
            "INSERT INTO tasks(id,title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (1, "任务", "todo", "normal", "2026-10-21", rm.iso(NOW), rm.iso(NOW)),
        )
        c1.commit()
        r1 = rs.run_tick(c1, now=NOW, run_key="dup-run-1")
        r2 = rs.run_tick(c2, now=NOW + timedelta(minutes=1), run_key="dup-run-2")
        assert r1["created_count"] == 1
        assert r2["created_count"] == 0
        assert r2["skipped_count"] == 1
    finally:
        c1.close()
        c2.close()


def test_stale_run_boundary_five_minutes_and_one_second(conn):
    def insert(start, key):
        conn.execute(
            "INSERT INTO reminder_scheduler_runs(run_key,trigger_type,status,started_at,created_at) "
            "VALUES(?,?,?,?,?)",
            (key, "manual", "running", start, start),
        )
    exact = rm.iso(NOW - timedelta(minutes=5))
    later = rm.iso(NOW - timedelta(minutes=5, seconds=1))
    insert(exact, "old-exact")
    insert(later, "old-later")
    result = rs.run_tick(conn, now=NOW, run_key="boundary")
    statuses = dict(conn.execute("SELECT run_key,status FROM reminder_scheduler_runs").fetchall())
    assert statuses["old-exact"] == "running"
    assert statuses["old-later"] == "abandoned"
    assert result["recovered_run_count"] == 1


def _insert_task_reminder_direct(conn, *, task_id, rule, status="scheduled", scheduled=None,
                                 expires=None, key_suffix="legacy"):
    scheduled = scheduled or (NOW - timedelta(hours=1))
    expires = expires or (NOW + timedelta(days=2))
    ts = rm.iso(NOW - timedelta(hours=2))
    conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,source_id,task_id,title,content,"
        "priority,status,evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rule, rm.RULE_VERSION, "task", task_id, task_id, "标题", "内容", "normal", status,
         "[]", "ok", f"{rule}:{task_id}:{key_suffix}", rm.iso(scheduled), rm.iso(expires), ts, ts),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _insert_rec_reminder_direct(conn, rec_id, *, status="scheduled", scheduled=None, expires=None):
    scheduled = scheduled or (NOW - timedelta(hours=1))
    expires = expires or (NOW + timedelta(days=7))
    ts = rm.iso(NOW - timedelta(hours=2))
    conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,source_id,recommendation_id,title,content,"
        "priority,status,evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rm.RULE_RECOMMENDATION_FOLLOWUP, rm.RULE_VERSION, "recommendation", rec_id, rec_id,
         "建议跟进", "内容", "low", status, "[]", "partial_data",
         f"{rm.RULE_RECOMMENDATION_FOLLOWUP}:{rec_id}:legacy",
         rm.iso(scheduled), rm.iso(expires), ts, ts),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _recommendation(conn, rec_id, *, status="accepted", expires_at=None, reviewed_at=None):
    reviewed_at = reviewed_at or (NOW - timedelta(days=10))
    expires_at = expires_at or (NOW + timedelta(days=30))
    ts = rm.iso(reviewed_at) if not isinstance(reviewed_at, str) else reviewed_at
    expires_value = rm.iso(expires_at) if not isinstance(expires_at, str) else expires_at
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec_id, "建议", "内容", "task", "normal", status, "[]", "{}", "ok", "", "low", "", 0.8,
         "rule", "test", "test", f"rec-{rec_id}", expires_value, ts, ts, ts),
    )


def test_task_cancellation_cancels_existing_active_task_reminder(conn):
    _settings(conn, task_due_enabled=True)
    task_id = _task(conn, status="cancelled", due="2026-10-25")
    rid = _insert_task_reminder_direct(conn, task_id=task_id, rule=rm.RULE_TASK_DUE_SOON,
                                       scheduled=NOW + timedelta(days=1), key_suffix="2026-10-25")
    result = rs.run_tick(conn, now=NOW, run_key="task-cancelled")
    saved = rm.get_reminder(conn, rid)
    assert saved["status"] == "cancelled"
    assert saved["cancel_reason"] == "source_cancelled"
    assert result["cancelled_count"] == 1


def test_one_bad_recommendation_does_not_block_healthy_recommendation(conn, monkeypatch):
    _settings(conn, recommendation_followup_enabled=True)
    _recommendation(conn, 1, reviewed_at=NOW - timedelta(days=8))
    _recommendation(conn, 2, reviewed_at="bad-time")
    result = rs.run_tick(conn, now=NOW, run_key="rec-isolation")
    assert result["status"] == "partial_failed"
    assert {r["source_id"] for r in rm.list_reminders(conn, include_terminal=True)} == {1}
    assert result["created_count"] == 1
    assert result["error_count"] >= 1


def test_one_bad_task_does_not_block_other_task_candidate(conn, monkeypatch):
    _settings(conn, task_due_enabled=True)
    _task(conn, task_id=1, due="2026-10-21")
    _task(conn, task_id=2, due="2026-10-22")
    original = rm.task_due_soon_candidate
    def flaky(task, settings, *, now):
        if int(task["id"]) == 2:
            raise sqlite3.DatabaseError("injected task failure")
        return original(task, settings, now=now)
    monkeypatch.setattr(rs.rm, "task_due_soon_candidate", flaky)
    result = rs.run_tick(conn, now=NOW, run_key="task-isolation")
    assert result["status"] == "partial_failed"
    assert {r["source_id"] for r in rm.list_reminders(conn, include_terminal=True)} == {1}
    assert result["created_count"] >= 1
    assert result["error_count"] >= 1


def test_recommendation_unaccepted_and_logically_expired_cancel_followups(conn):
    _settings(conn, recommendation_followup_enabled=True)
    _recommendation(conn, 1, status="ignored")
    rid1 = _insert_rec_reminder_direct(conn, 1)
    _recommendation(conn, 2, status="accepted", expires_at=NOW - timedelta(minutes=1))
    rid2 = _insert_rec_reminder_direct(conn, 2)
    result = rs.run_tick(conn, now=NOW, run_key="rec-invalid")
    assert rm.get_reminder(conn, rid1)["status"] == "cancelled"
    assert rm.get_reminder(conn, rid2)["status"] == "cancelled"
    assert result["cancelled_count"] == 2


def test_terminal_reminders_are_not_revived_by_later_ticks(conn):
    _settings(conn, daily_review_enabled=True)
    rid = _review_like = conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,title,content,priority,status,"
        "evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,created_at,updated_at,resolved_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rm.RULE_REVIEW_DAILY, rm.RULE_VERSION, "review", "每日复盘", "内容", "normal", "completed",
         "[]", "no_data", "review:terminal", rm.iso(NOW - timedelta(hours=1)),
         rm.iso(NOW + timedelta(hours=1)), rm.iso(NOW - timedelta(hours=2)),
         rm.iso(NOW - timedelta(hours=1)), rm.iso(NOW - timedelta(hours=1))),
    ).lastrowid
    rs.run_tick(conn, now=NOW, run_key="terminal-1")
    rs.run_tick(conn, now=NOW + timedelta(minutes=5), run_key="terminal-2")
    assert rm.get_reminder(conn, rid)["status"] == "completed"


def test_run_last_error_redacts_injected_pseudo_token(conn, monkeypatch):
    monkeypatch.setattr(
        rs.rm, "expire_reminders",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("token=abcdef1234567890"))
    )
    with pytest.raises(rs.SchedulerError):
        rs.run_tick(conn, now=NOW, run_key="redact-fatal")
    row = conn.execute(
        "SELECT last_error FROM reminder_scheduler_runs WHERE run_key=?", ("redact-fatal",)
    ).fetchone()
    assert row is not None
    assert "abcdef1234567890" not in row["last_error"]
    assert "***" in row["last_error"]


def test_weekly_catchup_exact_24_hour_boundary_is_exclusive(tmp_path, monkeypatch):
    db_path = tmp_path / "weekly-boundary.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        rm.update_settings(c, {"weekly_review_enabled": True}, now=NOW)
        at_boundary = datetime(2026, 12, 29, 1, 0, tzinfo=UTC)  # Tue 09:00 SH, exactly +24h
        result = rs.run_tick(c, now=at_boundary, run_key="weekly-boundary-exact")
        assert result["created_count"] == 0
        db_count = c.execute(
            "SELECT COUNT(*) FROM reminders WHERE rule_code=?", (rm.RULE_REVIEW_WEEKLY,)
        ).fetchone()[0]
        assert db_count == 0

        just_after = at_boundary + timedelta(minutes=1)
        late_result = rs.run_tick(c, now=just_after, run_key="weekly-boundary-after")
        assert late_result["created_count"] == 0
        db_count_after = c.execute(
            "SELECT COUNT(*) FROM reminders WHERE rule_code=?", (rm.RULE_REVIEW_WEEKLY,)
        ).fetchone()[0]
        assert db_count_after == 0
    finally:
        c.close()

def test_fatal_after_candidate_persisted_marks_failed_run_without_hiding_reminder(conn, monkeypatch):
    _settings(conn, task_due_enabled=True)
    _task(conn, due="2026-10-21")

    def fail_after_candidates(*args, **kwargs):
        raise sqlite3.DatabaseError("token=abcdef1234567890 advance failure")

    monkeypatch.setattr(rs, "_advance_due", fail_after_candidates)
    with pytest.raises(rs.SchedulerError) as exc_info:
        rs.run_tick(conn, now=NOW, run_key="fatal-after-create")
    assert "abcdef1234567890" not in str(exc_info.value)

    run = conn.execute(
        "SELECT * FROM reminder_scheduler_runs WHERE run_key=?", ("fatal-after-create",)
    ).fetchone()
    assert run["status"] == "failed"
    assert run["created_count"] == 1
    assert run["due_count"] == 0
    # Fatal post-generation errors are recorded in last_error/status; per-candidate
    # recoverable errors are a separate counter and did not occur before the fatal phase.
    assert run["error_count"] == 0
    assert "abcdef1234567890" not in run["last_error"]
    assert "***" in run["last_error"]
    reminders = conn.execute(
        "SELECT status, rule_code FROM reminders WHERE rule_code=?", (rm.RULE_TASK_DUE_SOON,)
    ).fetchall()
    assert len(reminders) == 1
    assert reminders[0]["status"] == "scheduled"
    conn.execute("SELECT 1 FROM reminder_scheduler_runs").fetchone()


def test_concurrent_connections_create_same_dedup_key_exactly_once(tmp_path, monkeypatch, rollback_journal):
    db_path = tmp_path / "concurrent-create.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    # Fresh WAL database + simultaneous first write can hit "readonly database";
    # this test is about dedup uniqueness across connections, not WAL bootstrap.
    rollback_journal(db_path)
    setup = sqlite3.connect(db_path, timeout=5)
    setup.row_factory = sqlite3.Row
    setup.execute("PRAGMA foreign_keys=ON")
    rm.update_settings(setup, {"task_due_enabled": True}, now=NOW)
    setup.execute(
        "INSERT INTO tasks(id,title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (1, "任务", "todo", "normal", "2026-10-21", rm.iso(NOW), rm.iso(NOW)),
    )
    setup.commit()
    settings = {**rm.get_settings(setup), "task_due_enabled": True}
    setup.close()
    candidate = rm.task_due_soon_candidate(
        {"id": 1, "status": "todo", "due_date": "2026-10-21"}, settings, now=NOW,
    )
    assert candidate is not None
    draft = candidate.to_draft()
    barrier = threading.Barrier(2)
    results: list[rm.CreateReminderResult | Exception] = []
    results_lock = threading.Lock()

    def worker(index: int):
        connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            barrier.wait(timeout=5)
            result = rm.create_reminder_result(
                connection, draft.model_copy(update={"title": f"任务-{index}"}), now=NOW
            )
            with results_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - asserted below for deterministic diagnosis
            with results_lock:
                results.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(results) == 2
    assert all(not isinstance(item, Exception) for item in results), results
    assert sorted(item.created for item in results) == [False, True]
    assert {item.reminder["id"] for item in results} == {results[0].reminder["id"]}
    audit = sqlite3.connect(db_path)
    try:
        assert audit.execute("SELECT COUNT(*) FROM reminders WHERE dedup_key=?", (draft.dedup_key,)).fetchone()[0] == 1
    finally:
        audit.close()


def test_sanitize_error_redacts_pseudo_token():
    clean = rs.sanitize_error("failed authorization: token=abcdef1234567890")
    assert "abcdef1234567890" not in clean
    assert "***" in clean

