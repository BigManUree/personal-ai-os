"""Personal AI OS 1.3.2-D: read-only analytics API and static dashboard wiring."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import analytics, behavior_events
from app.main import app, init_db

client = TestClient(app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_analytics_api.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    return db_path


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_analytics_overview_empty_window_returns_calculated_not_model_data(isolated_db):
    response = client.get("/api/analytics/overview", params={"days": 7})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["definition_version"] == "1.3.2-B"
    assert data["window"]["days"] == 7
    assert data["window"]["timezone"] == "Asia/Shanghai"
    assert len(data["daily"]) == 7
    assert data["metrics"]["tasks_created"] == {
        "value": 0,
        "unit": "count",
        "numerator": 0,
        "denominator": None,
        "sample_size": 0,
        "status": "no_data",
        "definition_version": "1.3.2-B",
    }
    assert data["coverage"]["status"] == "no_data"


def test_analytics_overview_accepts_timezone_and_counts_current_observed_event(isolated_db):
    created = client.post("/api/tasks", json={"title": "当前窗口任务", "priority": "normal"})
    assert created.status_code == 200, created.text

    response = client.get("/api/analytics/overview", params={"days": 30, "timezone": "UTC"})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["window"]["days"] == 30
    assert data["window"]["timezone"] == "UTC"
    assert data["metrics"]["tasks_created"]["value"] == 1
    assert data["metrics"]["tasks_created"]["status"] == "insufficient_data"
    assert sum(day["tasks_created"] for day in data["daily"]) == 1
    assert data["coverage"]["observed_categories"]["tasks"] is True


def test_analytics_overview_rejects_invalid_timezone_and_days(isolated_db):
    bad_zone = client.get("/api/analytics/overview", params={"timezone": "Mars/Base"})
    assert bad_zone.status_code == 400
    assert "时区" in bad_zone.json()["detail"]

    bad_days = client.get("/api/analytics/overview", params={"days": 367})
    assert bad_days.status_code == 422


def test_analytics_comparison_returns_two_equal_windows_and_validates_window_size(isolated_db):
    response = client.get("/api/analytics/comparison", params={"window_days": 7})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["definition_version"] == "1.3.2-C"
    assert data["window_days"] == 7
    assert data["current"]["start"] == data["previous"]["end"]
    assert data["metrics"]["tasks_completed"]["status"] == "no_data"

    invalid = client.get("/api/analytics/comparison", params={"window_days": 14})
    assert invalid.status_code == 400
    assert "7 天或 30 天" in invalid.json()["detail"]


def test_analytics_comparison_counts_current_and_previous_events(isolated_db):
    now = datetime.now(timezone.utc)
    current = analytics.build_window(days=7, now=now, timezone_name="Asia/Shanghai")
    previous = analytics.TimeWindow(
        days=7,
        timezone_name="Asia/Shanghai",
        tzinfo=current.tzinfo,
        start_local=current.start_local - timedelta(days=7),
        end_local=current.start_local,
        start_utc=current.start_utc - timedelta(days=7),
        end_utc=current.start_utc,
    )

    with _connect(isolated_db) as conn:
        for index in range(6):
            behavior_events.record_event(
                conn,
                behavior_events.EVENT_TASK_CREATED,
                subject_type="task",
                subject_id=100 + index,
                occurred_at=(current.start_utc + timedelta(hours=1 + index)).isoformat(),
                task_id=100 + index,
            )
        for index in range(4):
            behavior_events.record_event(
                conn,
                behavior_events.EVENT_TASK_CREATED,
                subject_type="task",
                subject_id=200 + index,
                occurred_at=(previous.start_utc + timedelta(hours=1 + index)).isoformat(),
                task_id=200 + index,
            )

    response = client.get("/api/analytics/comparison", params={"window_days": 7})

    assert response.status_code == 200, response.text
    metric = response.json()["metrics"]["tasks_created"]
    assert metric["previous"]["value"] == 4
    assert metric["current"]["value"] == 6
    assert metric["delta"] == 2
    assert metric["percent_change"] == 0.5
    assert metric["direction"] == "up"
    assert metric["significant"] is True
    assert metric["status"] == "ok"


def test_analytics_page_is_linked_and_bundle_wires_overview(frontend_bundle):
    page = client.get("/")
    assert page.status_code == 200
    # Vue SPA shell
    assert 'id="app"' in page.text
    assert '/static/assets/' in page.text

    # The Vue analytics page currently wires the overview endpoint only; the
    # comparison endpoint has no UI yet (tracked in docs/progress.md backlog).
    # The old assertions targeted app/static/analytics.js, removed 2026-10-22.
    assert "/api/analytics/overview" in frontend_bundle
