"""Personal AI OS 1.2 iteration 3.5.3 batch 2: connector sync scheduling.

Offline: uses the built-in Mock connector (incl. its fail modes) and drives
the scheduler directly with tick(); no real sleeps and no network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import connector_scheduler as sched
from app.main import app, init_db

client = TestClient(app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase353_schedule.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    init_db()
    return db_path


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _make_source(config=None, name="调度源"):
    payload = {"kind": "mock", "name": name, "scope": "test",
               "config": config if config is not None else {"items": []}}
    r = client.post("/api/data-sources", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _row(db_path, sid):
    c = _conn(db_path)
    r = c.execute("SELECT * FROM data_sources WHERE id=?", (sid,)).fetchone()
    c.close()
    return r


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #

def test_legacy_table_migration_defaults_off(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    # Build a pre-3.5.3 data_sources table without scheduling columns.
    c = sqlite3.connect(db_path)
    c.execute(
        "CREATE TABLE data_sources (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, "
        "status TEXT, scope TEXT, config_json TEXT, cursor TEXT, last_synced_at TEXT, last_error TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    c.execute(
        "INSERT INTO data_sources(kind,name,status,scope,config_json,created_at,updated_at) "
        "VALUES('mock','old','active','','{}','t','t')"
    )
    c.commit()
    c.close()

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()

    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM data_sources WHERE name='old'").fetchone()
    cols = {r[1] for r in c.execute("PRAGMA table_info(data_sources)")}
    c.close()
    assert {"sync_enabled", "sync_interval_minutes", "next_sync_at", "consecutive_failures"} <= cols
    assert row["sync_enabled"] == 0 and row["next_sync_at"] is None
    assert row["sync_interval_minutes"] == 60 and row["consecutive_failures"] == 0


# --------------------------------------------------------------------------- #
# schedule endpoint + status transitions
# --------------------------------------------------------------------------- #

def test_schedule_endpoint_validation_and_state(isolated_db):
    sid = _make_source()
    r = client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": True, "interval_minutes": 10})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sync_enabled"] in (1, True) and data["sync_interval_minutes"] == 10
    assert data["next_sync_at"]
    due_in = _parse(data["next_sync_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=9) < due_in < timedelta(minutes=11)

    # default interval kept when omitted
    sid2 = _make_source(name="b")
    assert client.post(f"/api/data-sources/{sid2}/schedule", json={"enabled": True}).json()["sync_interval_minutes"] == 60

    # disable clears next run
    r = client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": False})
    assert r.json()["next_sync_at"] is None

    # validation
    for body in [{"enabled": True, "interval_minutes": 0},
                 {"enabled": True, "interval_minutes": 10081},
                 {"enabled": "yes"},
                 {"enabled": True, "interval_minutes": 1.5}]:
        assert client.post(f"/api/data-sources/{sid}/schedule", json=body).status_code == 422, body
    assert client.post("/api/data-sources/9999/schedule", json={"enabled": True}).status_code == 404


def test_revoked_cannot_enable_and_pause_resume_rearm(isolated_db):
    sid = _make_source()
    client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": True, "interval_minutes": 30})
    assert client.get(f"/api/data-sources/{sid}").json()["next_sync_at"]

    client.post(f"/api/data-sources/{sid}/revoke")
    assert client.get(f"/api/data-sources/{sid}").json()["next_sync_at"] is None
    assert client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": True}).status_code == 409

    # paused active source clears, resume re-arms
    sid2 = _make_source(name="p")
    client.post(f"/api/data-sources/{sid2}/schedule", json={"enabled": True, "interval_minutes": 15})
    client.post(f"/api/data-sources/{sid2}/pause")
    assert client.get(f"/api/data-sources/{sid2}").json()["next_sync_at"] is None
    client.post(f"/api/data-sources/{sid2}/resume")
    assert client.get(f"/api/data-sources/{sid2}").json()["next_sync_at"]


def test_patch_interval_rearms_enabled_source(isolated_db):
    sid = _make_source()
    client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": True, "interval_minutes": 60})
    before = client.get(f"/api/data-sources/{sid}").json()["next_sync_at"]
    r = client.patch(f"/api/data-sources/{sid}", json={"sync_interval_minutes": 120})
    after = r.json()
    assert after["sync_interval_minutes"] == 120 and after["next_sync_at"]
    assert _parse(after["next_sync_at"]) > _parse(before)
    # disabled source keeps next_sync_at null even when interval patched
    sid2 = _make_source(name="off")
    r2 = client.patch(f"/api/data-sources/{sid2}", json={"sync_interval_minutes": 30})
    assert r2.json()["next_sync_at"] is None


# --------------------------------------------------------------------------- #
# pure scheduling maths
# --------------------------------------------------------------------------- #

def test_backoff_and_interval_bounds():
    now = datetime(2026, 10, 12, 0, 0, 0, tzinfo=timezone.utc)
    assert sched.next_run_after_success(60, now=now) == "2026-10-12T01:00:00+00:00"
    assert sched.next_run_after_failure(60, 1, now=now) == "2026-10-12T01:00:00+00:00"
    assert sched.next_run_after_failure(60, 2, now=now) == "2026-10-12T02:00:00+00:00"
    assert sched.next_run_after_failure(60, 3, now=now) == "2026-10-12T04:00:00+00:00"
    # capped at 24h regardless of failure count
    assert sched.next_run_after_failure(60, 10, now=now) == "2026-10-13T00:00:00+00:00"
    for bad in (0, -1, 10081, True, "30", 1.5):
        with pytest.raises(ValueError):
            sched.normalise_interval(bad)
    assert sched.normalise_interval(1) == 1 and sched.normalise_interval(10080) == 10080


# --------------------------------------------------------------------------- #
# scheduler tick mechanics (injected fakes, no real time)
# --------------------------------------------------------------------------- #

def test_scheduler_start_stop_idempotent_and_tick_serial():
    calls = []

    async def runner(sid):
        calls.append(sid)
        return {"status": "success"}

    sch = sched.ConnectorScheduler(lambda: [11, 12], runner, poll_seconds=1)
    out = asyncio.run(sch.tick())
    assert out == {"ran": [11, 12], "failed": []}
    assert calls == [11, 12]
    # empty due list -> no-op
    sch2 = sched.ConnectorScheduler(lambda: [], runner, poll_seconds=1)
    assert asyncio.run(sch2.tick()) == {"ran": [], "failed": []}
    # start/stop are safe to repeat (must run inside an event loop)
    async def lifecycle():
        sch.start()
        sch.start()
        assert sch.is_running()
        await sch.stop()
        await sch.stop()
        assert not sch.is_running()

    asyncio.run(lifecycle())


def test_tick_marks_failed_runner_as_failure():
    async def bad(sid):
        return {"status": "failed", "reason": "fetch_error"}

    sch = sched.ConnectorScheduler(lambda: [7], bad, poll_seconds=1)
    assert asyncio.run(sch.tick()) == {"ran": [], "failed": [7]}


# --------------------------------------------------------------------------- #
# real sync through the scheduler: backoff then reset
# --------------------------------------------------------------------------- #

def _force_due(db_path, sid):
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    c = _conn(db_path)
    c.execute("UPDATE data_sources SET next_sync_at=? WHERE id=?", (past, sid))
    c.commit()
    c.close()


def test_scheduled_failure_backs_off_and_success_resets(isolated_db):
    from app.main import ConnectorScheduler, _due_source_ids, _run_source_sync

    # failing source
    fail_id = _make_source({"items": [], "fail": "fetch"}, name="failing")
    client.post(f"/api/data-sources/{fail_id}/schedule", json={"enabled": True, "interval_minutes": 60})
    _force_due(isolated_db, fail_id)
    assert _due_source_ids() == [fail_id]

    sch = ConnectorScheduler(_due_source_ids, _run_source_sync, poll_seconds=1)
    out = asyncio.run(sch.tick())
    assert out == {"ran": [], "failed": [fail_id]}

    row = _row(isolated_db, fail_id)
    assert row["consecutive_failures"] == 1
    assert row["next_sync_at"] is not None
    due_in = _parse(row["next_sync_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=55) < due_in < timedelta(minutes=65)  # 60 * 2^0
    c = _conn(isolated_db)
    assert c.execute("SELECT COUNT(*) AS n FROM sync_logs WHERE data_source_id=? AND status='failed'",
                     (fail_id,)).fetchone()["n"] == 1
    c.close()
    # a failed source is no longer "due" until its backoff elapses
    assert _due_source_ids() == []

    # second failure doubles the backoff
    _force_due(isolated_db, fail_id)
    asyncio.run(ConnectorScheduler(_due_source_ids, _run_source_sync, poll_seconds=1).tick())
    row = _row(isolated_db, fail_id)
    assert row["consecutive_failures"] == 2
    due_in2 = _parse(row["next_sync_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=115) < due_in2 < timedelta(minutes=125)

    # repair the connector and sync successfully -> failures reset, re-arm interval
    client.patch(f"/api/data-sources/{fail_id}", json={"config": {"items": [
        {"external_id": "a", "content": "调度成功后内容 Omega"}]}})
    _force_due(isolated_db, fail_id)
    out = asyncio.run(ConnectorScheduler(_due_source_ids, _run_source_sync, poll_seconds=1).tick())
    assert out == {"ran": [fail_id], "failed": []}
    row = _row(isolated_db, fail_id)
    assert row["consecutive_failures"] == 0
    due_in3 = _parse(row["next_sync_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=55) < due_in3 < timedelta(minutes=65)


def test_disabled_source_never_due_even_with_past_next_sync(isolated_db):
    from app.main import _due_source_ids
    sid = _make_source({"items": [{"external_id": "x", "content": "y"}]})
    # no schedule enabled: next_sync_at stays null and source is not due
    assert _due_source_ids() == []
    client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": True, "interval_minutes": 5})
    _force_due(isolated_db, sid)
    assert _due_source_ids() == [sid]
    client.post(f"/api/data-sources/{sid}/schedule", json={"enabled": False})
    assert _due_source_ids() == []
