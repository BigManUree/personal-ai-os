"""Personal AI OS 1.3.2-B: deterministic behavior analytics engine."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import analytics, behavior_events
from app.main import init_db

SHANGHAI = timezone(timedelta(hours=8))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "analytics.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


def _now():
    return datetime(2026, 10, 12, 9, 43, 17, tzinfo=SHANGHAI)


def _ensure_task(conn, task_id, project_id=None):
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        conn.execute(
            "INSERT INTO tasks(id,title,status,priority,created_at,updated_at,project_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, f"task {task_id}", "todo", "normal",
             "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", project_id),
        )


def _ensure_project(conn, project_id):
    if conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (project_id, f"project {project_id}", "active",
             "2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        )


def _record(conn, event_type, occurred_at, **kwargs):
    subject_type = kwargs.pop("subject_type", "task")
    subject_id = kwargs.pop("subject_id", 1)
    project_id = kwargs.get("project_id")
    task_id = kwargs.get("task_id")
    if project_id:
        _ensure_project(conn, project_id)
    if task_id:
        _ensure_task(conn, task_id, project_id)
    if subject_type == "project":
        _ensure_project(conn, subject_id)
    behavior_events.record_event(
        conn,
        event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        occurred_at=occurred_at.isoformat(),
        metadata=kwargs.pop("metadata", None),
        **kwargs,
    )


def _sync_log(conn, status, started_at, **counters):
    if conn.execute("SELECT 1 FROM data_sources WHERE id=1").fetchone() is None:
        conn.execute(
            "INSERT INTO data_sources(id,kind,name,status,config_json,created_at,updated_at) "
            "VALUES(1,'mock','analytics source','active','{}',?,?)",
            ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        )
    conn.execute(
        """
        INSERT INTO sync_logs(
            data_source_id, started_at, finished_at, status, total, created,
            updated, unchanged, skipped, deleted, failed, error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            1,
            started_at.isoformat(),
            started_at.isoformat() if status != "running" else None,
            status,
            counters.get("total", 1),
            counters.get("created", 0),
            counters.get("updated", 0),
            counters.get("unchanged", 0),
            counters.get("skipped", 0),
            counters.get("deleted", 0),
            counters.get("failed", 0),
            counters.get("error"),
        ),
    )


def test_window_uses_shanghai_natural_days_and_includes_midnight_boundary(conn):
    window = analytics.build_window(days=7, now=_now(), timezone_name="Asia/Shanghai")
    assert window.timezone_name == "Asia/Shanghai"
    assert window.start_local.isoformat() == "2026-10-06T00:00:00+08:00"
    assert window.end_local.isoformat() == "2026-10-13T00:00:00+08:00"
    assert window.start_utc.isoformat() == "2026-10-05T16:00:00+00:00"
    assert window.end_utc.isoformat() == "2026-10-12T16:00:00+00:00"

    # 2026-10-12 00:00 Shanghai == 2026-10-11 16:00 UTC and must be included.
    _record(
        conn,
        behavior_events.EVENT_TASK_CREATED,
        datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc),
        task_id=1,
    )
    overview = analytics.overview(conn, days=7, now=_now(), timezone_name="Asia/Shanghai")
    assert overview["daily"][-1]["date"] == "2026-10-12"
    assert overview["daily"][-1]["tasks_created"] == 1
    assert overview["metrics"]["tasks_created"]["value"] == 1


def test_empty_window_is_no_data_not_zero_confirmed_activity(conn):
    overview = analytics.overview(conn, days=7, now=_now(), timezone_name="Asia/Shanghai")
    assert len(overview["daily"]) == 7
    assert [row["date"] for row in overview["daily"]] == [
        "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        "2026-10-10", "2026-10-11", "2026-10-12",
    ]
    assert all(row["tasks_created"] == 0 for row in overview["daily"])
    assert overview["metrics"]["tasks_created"]["value"] == 0
    assert overview["metrics"]["tasks_created"]["status"] == "no_data"
    assert overview["coverage"]["status"] == "no_data"
    assert overview["definition_version"] == "1.3.2-B"


def test_counts_documents_project_activity_and_task_lifecycle_by_day(conn):
    _record(
        conn, behavior_events.EVENT_DOCUMENT_CREATED,
        datetime(2026, 10, 6, 10, 0, tzinfo=SHANGHAI),
        subject_type="document", subject_id=1,
    )
    _record(
        conn, behavior_events.EVENT_DOCUMENT_CREATED,
        datetime(2026, 10, 6, 11, 0, tzinfo=SHANGHAI),
        subject_type="document", subject_id=2,
    )
    _record(
        conn, behavior_events.EVENT_PROJECT_CREATED,
        datetime(2026, 10, 7, 9, 0, tzinfo=SHANGHAI),
        subject_type="project", subject_id=1, project_id=1,
    )
    _record(
        conn, behavior_events.EVENT_PROJECT_EVENT_RECORDED,
        datetime(2026, 10, 7, 10, 0, tzinfo=SHANGHAI),
        subject_type="project_event", subject_id=1, project_id=1,
        metadata={"project_event_type": "decision"},
    )
    _record(
        conn, behavior_events.EVENT_TASK_CREATED,
        datetime(2026, 10, 8, 9, 0, tzinfo=SHANGHAI),
        task_id=1, project_id=1,
    )
    _record(
        conn, behavior_events.EVENT_TASK_COMPLETED,
        datetime(2026, 10, 8, 10, 0, tzinfo=SHANGHAI),
        task_id=1, project_id=1,
    )
    _record(
        conn, behavior_events.EVENT_TASK_REOPENED,
        datetime(2026, 10, 9, 10, 0, tzinfo=SHANGHAI),
        task_id=1, project_id=1,
    )
    _record(
        conn, behavior_events.EVENT_TASK_CANCELLED,
        datetime(2026, 10, 9, 11, 0, tzinfo=SHANGHAI),
        task_id=1, project_id=1,
    )

    overview = analytics.overview(conn, days=7, now=_now(), timezone_name="Asia/Shanghai")
    metrics = overview["metrics"]
    assert metrics["documents_created"]["value"] == 2
    assert metrics["project_activities"]["value"] == 2
    assert metrics["tasks_created"]["value"] == 1
    assert metrics["tasks_completed"]["value"] == 1
    assert metrics["tasks_reopened"]["value"] == 1
    assert metrics["tasks_cancelled"]["value"] == 1
    by_date = {row["date"]: row for row in overview["daily"]}
    assert by_date["2026-10-06"]["documents_created"] == 2
    assert by_date["2026-10-07"]["project_activities"] == 2
    assert by_date["2026-10-08"]["tasks_completed"] == 1
    assert by_date["2026-10-09"]["tasks_reopened"] == 1
    assert by_date["2026-10-09"]["tasks_cancelled"] == 1
    assert "metadata" not in str(overview["daily"])


def test_net_completion_ratio_can_exceed_one_and_uses_sample_status(conn):
    for idx in range(3):
        _record(
            conn, behavior_events.EVENT_TASK_COMPLETED,
            datetime(2026, 10, 8, 9 + idx, 0, tzinfo=SHANGHAI),
            subject_id=idx + 1, task_id=idx + 1,
        )
    _record(
        conn, behavior_events.EVENT_TASK_CREATED,
        datetime(2026, 10, 8, 12, 0, tzinfo=SHANGHAI),
        subject_id=4, task_id=4,
    )
    overview = analytics.overview(conn, days=7, now=_now())
    ratio = overview["metrics"]["net_completion_ratio"]
    assert ratio["value"] == 3.0
    assert ratio["numerator"] == 3
    assert ratio["denominator"] == 1
    assert ratio["status"] == "insufficient_data"

    for idx in range(5, 10):
        _record(
            conn, behavior_events.EVENT_TASK_CREATED,
            datetime(2026, 10, 8, 13 + idx, 0, tzinfo=SHANGHAI),
            subject_id=idx, task_id=idx,
        )
    overview = analytics.overview(conn, days=7, now=_now())
    assert overview["metrics"]["net_completion_ratio"]["denominator"] == 6
    assert overview["metrics"]["net_completion_ratio"]["status"] == "ok"


def test_connector_health_uses_sync_logs_and_not_behavior_events(conn):
    base = datetime(2026, 10, 10, 9, 0, tzinfo=SHANGHAI)
    _sync_log(conn, "success", base)
    _sync_log(conn, "success", base + timedelta(hours=1))
    _sync_log(conn, "failed", base + timedelta(hours=2), error="redacted error")
    _sync_log(conn, "running", base + timedelta(hours=3))

    overview = analytics.overview(conn, days=7, now=_now())
    health = overview["metrics"]["connector_sync_success_rate"]
    assert health["value"] == pytest.approx(2 / 3, abs=1e-4)
    assert health["numerator"] == 2
    assert health["denominator"] == 3
    assert health["status"] == "insufficient_data"
    assert overview["metrics"]["connector_sync_attempts"]["value"] == 4
    assert overview["coverage"]["observed_categories"]["connectors"] is True
    assert "redacted error" not in str(overview)


def test_events_outside_window_are_absent_and_coverage_reflects_sources(conn):
    _record(
        conn, behavior_events.EVENT_TASK_CREATED,
        datetime(2026, 10, 5, 15, 59, tzinfo=SHANGHAI),  # before local window
        task_id=1,
    )
    _record(
        conn, behavior_events.EVENT_DOCUMENT_CREATED,
        datetime(2026, 10, 11, 10, 0, tzinfo=SHANGHAI),
        subject_type="document", subject_id=1,
    )
    _sync_log(conn, "success", datetime(2026, 10, 11, 11, 0, tzinfo=SHANGHAI))

    overview = analytics.overview(conn, days=7, now=_now())
    assert overview["metrics"]["tasks_created"]["status"] == "no_data"
    assert overview["metrics"]["documents_created"]["value"] == 1
    assert overview["coverage"]["observed_categories"] == {
        "documents": True,
        "projects": False,
        "tasks": False,
        "connectors": True,
    }
    assert overview["coverage"]["status"] == "ok"


def test_task_snapshot_metrics_use_current_table_not_event_count(conn):
    # These current-state rows need no behavior events; snapshot metrics still work.
    rows = [
        (101, "open no due", "todo", "normal", None, None),
        (102, "doing future", "doing", "normal", "2026-10-20", None),
        (103, "doing overdue", "doing", "high", "2026-10-11", None),
        (104, "cancelled overdue", "cancelled", "normal", "2026-10-10", None),
        (105, "done not overdue", "done", "normal", "2026-10-10", None),
    ]
    for task_id, title, status, priority, due_date, completed_at in rows:
        conn.execute(
            "INSERT INTO tasks(id,title,status,priority,due_date,completed_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (task_id, title, status, priority, due_date, completed_at,
             "2026-10-01T00:00:00+00:00", "2026-10-11T00:00:00+00:00"),
        )

    overview = analytics.overview(conn, days=7, now=_now())
    assert overview["metrics"]["open_backlog_count"]["value"] == 3
    assert overview["metrics"]["overdue_open_count"]["value"] == 1
    assert overview["metrics"]["overdue_open_count"]["numerator"] == 1
    assert overview["metrics"]["overdue_open_count"]["status"] == "insufficient_data"


def test_completion_duration_average_and_median_ignore_invalid_rows(conn):
    rows = [
        # 10 minutes
        (201, "fast", "2026-10-08T09:00:00+08:00", "2026-10-08T09:10:00+08:00"),
        # 20 minutes
        (202, "slow", "2026-10-08T10:00:00+08:00", "2026-10-08T10:20:00+08:00"),
        # completed before created: invalid clock data, ignored
        (203, "bad clock", "2026-10-08T12:00:00+08:00", "2026-10-08T11:00:00+08:00"),
        # open task has no completion time, ignored
        (204, "open", "2026-10-08T12:00:00+08:00", None),
    ]
    for task_id, title, created_at, completed_at in rows:
        conn.execute(
            "INSERT INTO tasks(id,title,status,priority,created_at,updated_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, title, "done" if completed_at else "todo", "normal",
             created_at, completed_at or created_at, completed_at),
        )

    duration = analytics.overview(conn, days=7, now=_now())["metrics"]["completion_duration_minutes"]
    assert duration["value"] == 15.0
    assert duration["average_minutes"] == 15.0
    assert duration["median_minutes"] == 15.0
    assert duration["sample_size"] == 2
    assert duration["status"] == "insufficient_data"
    assert duration["unit"] == "minutes"


def test_completion_duration_is_no_data_when_no_completed_tasks(conn):
    metric = analytics.overview(conn, days=7, now=_now())["metrics"]["completion_duration_minutes"]
    assert metric["value"] is None
    assert metric["average_minutes"] is None
    assert metric["median_minutes"] is None
    assert metric["sample_size"] == 0
    assert metric["status"] == "no_data"


# ---------------------------------------------------------------------------
# 1.3.2-C period comparison
# ---------------------------------------------------------------------------


def _comparison_now():
    return datetime(2026, 10, 12, 10, 1, 56, tzinfo=SHANGHAI)


def _add_task_events(conn, event_type, count, day, start_id=1):
    for idx in range(count):
        task_id = start_id + idx
        _record(
            conn,
            event_type,
            datetime(2026, 10, day, 9 + (idx % 12), 0, tzinfo=SHANGHAI),
            subject_id=task_id,
            task_id=task_id,
        )


def _add_document_events(conn, count, day, start_id=1):
    for idx in range(count):
        _record(
            conn,
            behavior_events.EVENT_DOCUMENT_CREATED,
            datetime(2026, 10, day, 9 + (idx % 12), 0, tzinfo=SHANGHAI),
            subject_type="document",
            subject_id=start_id + idx,
        )


def test_comparison_uses_two_equal_non_overlapping_shanghai_windows(conn):
    comparison = analytics.compare_periods(
        conn, window_days=7, now=_comparison_now(), timezone_name="Asia/Shanghai"
    )
    assert comparison["window_days"] == 7
    assert comparison["definition_version"] == "1.3.2-C"
    assert comparison["current"]["start"] == "2026-10-06T00:00:00+08:00"
    assert comparison["current"]["end"] == "2026-10-13T00:00:00+08:00"
    assert comparison["previous"]["start"] == "2026-09-29T00:00:00+08:00"
    assert comparison["previous"]["end"] == "2026-10-06T00:00:00+08:00"

    # Exactly at the shared boundary belongs to current in Shanghai time.
    _record(
        conn,
        behavior_events.EVENT_TASK_CREATED,
        datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc),
        subject_id=500,
        task_id=500,
    )
    comparison = analytics.compare_periods(
        conn, window_days=7, now=_comparison_now(), timezone_name="Asia/Shanghai"
    )
    metric = comparison["metrics"]["tasks_created"]
    assert metric["current"]["value"] == 1
    assert metric["previous"]["value"] == 0
    assert metric["delta"] == 1
    assert metric["direction"] == "up"
    assert metric["significant"] is False
    assert metric["status"] == "insufficient_data"
    assert metric["definition_version"] == "1.3.2-C"


def test_comparison_calculates_delta_percent_direction_and_significance(conn):
    # Previous: 10 created; current: 13 created -> +30%, significant.
    _add_task_events(conn, behavior_events.EVENT_TASK_CREATED, 10, 2, start_id=100)
    _add_task_events(conn, behavior_events.EVENT_TASK_CREATED, 13, 8, start_id=200)
    # Previous: 10 completed; current: 5 completed -> -50%, significant.
    _add_task_events(conn, behavior_events.EVENT_TASK_COMPLETED, 10, 2, start_id=300)
    _add_task_events(conn, behavior_events.EVENT_TASK_COMPLETED, 5, 8, start_id=400)
    # Enough samples, exactly unchanged -> no significant change.
    _add_document_events(conn, 10, 2, start_id=500)
    _add_document_events(conn, 10, 8, start_id=600)
    # Need document subject type rather than task FK: replace generated events is
    # cumbersome, so use direct project activity events for an additional flat metric.
    for idx in range(10):
        _record(
            conn,
            behavior_events.EVENT_PROJECT_EVENT_RECORDED,
            datetime(2026, 10, 2, 9, idx % 30, tzinfo=SHANGHAI),
            subject_type="project_event",
            subject_id=700 + idx,
            project_id=70,
            metadata={"project_event_type": "note"},
        )

    comparison = analytics.compare_periods(conn, window_days=7, now=_comparison_now())
    metrics = comparison["metrics"]
    created = metrics["tasks_created"]
    assert created["current"]["value"] == 13
    assert created["previous"]["value"] == 10
    assert created["delta"] == 3
    assert created["percent_change"] == 0.3
    assert created["direction"] == "up"
    assert created["significant"] is True

    completed = metrics["tasks_completed"]
    assert completed["delta"] == -5
    assert completed["percent_change"] == -0.5
    assert completed["direction"] == "down"
    assert completed["significant"] is True

    documents = metrics["documents_created"]
    assert documents["delta"] == 0
    assert documents["percent_change"] == 0.0
    assert documents["direction"] == "flat"
    assert documents["significant"] is False


def test_comparison_zero_baseline_requires_enough_current_sample(conn):
    _record(
        conn,
        behavior_events.EVENT_TASK_CREATED,
        datetime(2026, 10, 8, 9, 0, tzinfo=SHANGHAI),
        subject_id=800,
        task_id=800,
    )
    comparison = analytics.compare_periods(conn, window_days=7, now=_comparison_now())
    metric = comparison["metrics"]["tasks_created"]
    assert metric["previous"]["value"] == 0
    assert metric["delta"] == 1
    assert metric["percent_change"] is None
    assert metric["significant"] is False
    assert metric["status"] == "insufficient_data"
    assert metric["definition_version"] == "1.3.2-C"

    for idx in range(5):
        _record(
            conn,
            behavior_events.EVENT_DOCUMENT_CREATED,
            datetime(2026, 10, 8 + idx // 3, 10 + idx % 3, 0, tzinfo=SHANGHAI),
            subject_type="document",
            subject_id=900 + idx,
        )
    comparison = analytics.compare_periods(conn, window_days=7, now=_comparison_now())
    documents = comparison["metrics"]["documents_created"]
    assert documents["previous"]["value"] == 0
    assert documents["current"]["value"] == 5
    assert documents["percent_change"] is None
    assert documents["significant"] is True
    assert documents["status"] == "ok"


def test_comparison_no_data_and_small_change_are_not_significant(conn):
    comparison = analytics.compare_periods(conn, window_days=7, now=_comparison_now())
    metric = comparison["metrics"]["project_activities"]
    assert metric["current"]["value"] == 0
    assert metric["previous"]["value"] == 0
    assert metric["delta"] == 0
    assert metric["percent_change"] is None
    assert metric["direction"] == "flat"
    assert metric["significant"] is False
    assert metric["status"] == "no_data"

    # 5 -> 6 is +20%; threshold is met and sample is sufficient.
    _add_task_events(conn, behavior_events.EVENT_TASK_REOPENED, 5, 2, start_id=1000)
    _add_task_events(conn, behavior_events.EVENT_TASK_REOPENED, 6, 8, start_id=1100)
    metric = analytics.compare_periods(conn, window_days=7, now=_comparison_now())["metrics"]["tasks_reopened"]
    assert metric["percent_change"] == 0.2
    assert metric["significant"] is True

    # 5 -> 5 via cancelled is flat/no significance despite task lifecycle activity.
    _add_task_events(conn, behavior_events.EVENT_TASK_CANCELLED, 5, 2, start_id=1200)
    _add_task_events(conn, behavior_events.EVENT_TASK_CANCELLED, 5, 8, start_id=1300)
    metric = analytics.compare_periods(conn, window_days=7, now=_comparison_now())["metrics"]["tasks_cancelled"]
    assert metric["delta"] == 0
    assert metric["significant"] is False


def test_comparison_reads_connector_logs_from_both_windows_and_validates_input(conn):
    _sync_log(conn, "success", datetime(2026, 10, 1, 9, 0, tzinfo=SHANGHAI))
    _sync_log(conn, "failed", datetime(2026, 10, 1, 10, 0, tzinfo=SHANGHAI))
    for hour in range(6):
        _sync_log(conn, "success", datetime(2026, 10, 8, 9 + hour, 0, tzinfo=SHANGHAI))
    _sync_log(conn, "running", datetime(2026, 10, 8, 16, 0, tzinfo=SHANGHAI))

    comparison = analytics.compare_periods(conn, window_days=7, now=_comparison_now())
    metrics = comparison["metrics"]
    assert metrics["connector_sync_attempts"]["current"]["value"] == 7
    assert metrics["connector_sync_attempts"]["previous"]["value"] == 2
    assert metrics["connector_sync_successes"]["current"]["value"] == 6
    assert metrics["connector_sync_successes"]["previous"]["value"] == 1
    assert metrics["connector_sync_failures"]["current"]["value"] == 0
    assert metrics["connector_sync_failures"]["previous"]["value"] == 1

    assert analytics.compare_periods(conn, window_days=30, now=_comparison_now())["window_days"] == 30
    with pytest.raises(ValueError):
        analytics.compare_periods(conn, window_days=14, now=_comparison_now())
    with pytest.raises(ValueError):
        analytics.compare_periods(conn, window_days=7, now=_comparison_now(), timezone_name="Bad/Zone")


def test_invalid_window_inputs_are_rejected(conn):
    with pytest.raises(ValueError):
        analytics.build_window(days=0, now=_now())
    with pytest.raises(ValueError):
        analytics.build_window(days=367, now=_now())
    with pytest.raises(ValueError):
        analytics.build_window(days=7, now=_now(), timezone_name="America/No_Such_Place")
    with pytest.raises(ValueError):
        analytics.build_window(days=7, now=datetime(2026, 10, 12, 9, 43))  # naive



