"""Lifecycle and long-tick safety tests for C-C-2c reminder scheduling.

All database fixtures below use an explicit tmp_path SQLite file. The global
pytest guard in tests/conftest.py additionally refuses a non-temp DATABASE_PATH.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import reminder_scheduler as rs
from app import reminder_scheduler_lock as lease_svc
from app import reminder_scheduler_service as rss
from app import reminders as rm
from app.main import app, init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "reminder-lifecycle.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()
    return path


@pytest.fixture
def conn(db_path):
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _task_due_tomorrow(connection: sqlite3.Connection) -> None:
    rm.update_settings(connection, {"task_due_enabled": True}, now=NOW)
    connection.execute(
        "INSERT INTO tasks(title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("生命周期长任务", "todo", "normal", "2026-10-14", rm.iso(NOW - timedelta(days=1)),
         rm.iso(NOW - timedelta(days=1))),
    )
    connection.commit()


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def test_config_defaults_disabled_and_uses_safe_production_values(monkeypatch):
    monkeypatch.delenv("REMINDER_SCHEDULER_ENABLED", raising=False)
    config = rss.load_config()
    assert config.enabled is False
    assert config.poll_seconds == 60.0
    assert config.shutdown_timeout_seconds == 35.0


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_config_parses_explicit_enabled_values(monkeypatch, value):
    monkeypatch.setenv("REMINDER_SCHEDULER_ENABLED", value)
    assert rss.load_config().enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_config_parses_explicit_disabled_values(monkeypatch, value):
    monkeypatch.setenv("REMINDER_SCHEDULER_ENABLED", value)
    assert rss.load_config().enabled is False


@pytest.mark.parametrize("value", ["maybe", "2", ""])
def test_config_rejects_invalid_enabled_value(monkeypatch, value):
    monkeypatch.setenv("REMINDER_SCHEDULER_ENABLED", value)
    with pytest.raises(ValueError, match="REMINDER_SCHEDULER_ENABLED"):
        rss.load_config()


@pytest.mark.parametrize("name,minimum", [
    ("REMINDER_SCHEDULER_POLL_SECONDS", 30.0),
    ("REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", 1.0),
])
def test_config_rejects_out_of_range_numeric_values(monkeypatch, name, minimum):
    monkeypatch.setenv("REMINDER_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv(name, str(minimum - 0.1))
    with pytest.raises(ValueError, match=name):
        rss.load_config()


# --------------------------------------------------------------------------- #
# service state machine and executor ownership
# --------------------------------------------------------------------------- #

def test_service_start_stop_is_idempotent_with_single_loop(db_path):
    completed = threading.Event()
    started = threading.Event()
    thread_names: list[str] = []
    names_lock = threading.Lock()

    def runner(connection, **kwargs):
        with names_lock:
            thread_names.append(threading.current_thread().name)
        started.set()
        completed.wait(2)
        return {"status": "succeeded", "run_id": 1, **kwargs}

    service = rss.ReminderSchedulerService(
        db_path, runner=runner, poll_seconds=0.05, initial_delay=0,
        shutdown_timeout_seconds=2,
    )

    async def scenario():
        await service.start()
        await service.start()
        assert service.is_loop_running()
        deadline = asyncio.get_running_loop().time() + 1
        while not started.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        completed.set()
        await asyncio.sleep(0.05)
        await service.stop()
        await service.stop()
        return service.snapshot()

    snapshot = asyncio.run(scenario())
    assert not service.is_loop_running()
    assert snapshot["state"] == "stopped"
    assert snapshot["active_tick"] is False
    assert snapshot["last_status"] == "succeeded"
    assert len(set(thread_names)) == 1
    assert thread_names[0].startswith("reminder-scheduler-")


def test_disabled_service_start_creates_no_loop_or_executor(db_path):
    service = rss.ReminderSchedulerService(db_path, enabled=False)

    async def scenario():
        await service.start()
        await service.stop()

    asyncio.run(scenario())
    snapshot = service.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["state"] == "stopped"
    assert snapshot["loop_running"] is False
    assert snapshot["active_tick"] is False


def test_concurrent_tick_requests_share_single_worker_and_lease_serializes(db_path):
    barrier = threading.Barrier(2)
    release_first = threading.Event()

    def holder_runner(path, **kwargs):
        connection = _connect(Path(path))
        try:
            acquired = lease_svc.acquire_lease(
                connection, now=datetime.now(timezone.utc), ttl_seconds=30,
                owner_token="lifecycle-owner-1", busy_timeout_ms=1000,
            )
            assert acquired.acquired
            # Both workers are racing at this barrier; the second one then attempts
            # run_tick while this connection still owns the global lease.
            barrier.wait(timeout=2)
            release_first.wait(2)
            release = lease_svc.release_lease(
                connection, owner_token=acquired.lease.owner_token,
                lease_token=acquired.lease.lease_token,
                fencing_token=acquired.lease.fencing_token,
                now=datetime.now(timezone.utc), busy_timeout_ms=1000,
            )
            assert release.acquired
        finally:
            connection.close()
        return rss.run_scheduled_tick(path, **kwargs)

    def contender_runner(path, **kwargs):
        barrier.wait(timeout=2)
        try:
            return rss.run_scheduled_tick(path, **kwargs)
        finally:
            release_first.set()

    service1 = rss.ReminderSchedulerService(
        db_path, runner=holder_runner, poll_seconds=30, owner_token="lifecycle-owner-1",
        shutdown_timeout_seconds=2,
    )
    service2 = rss.ReminderSchedulerService(
        db_path, runner=contender_runner, poll_seconds=30, owner_token="lifecycle-owner-2",
        shutdown_timeout_seconds=2,
    )
    outcomes: list[tuple[str, str]] = []

    async def run(service: rss.ReminderSchedulerService, label: str, key: str) -> None:
        try:
            await service.tick_once(run_key=key)
            outcomes.append((label, "executed"))
        except rs.LeaseBusyError:
            outcomes.append((label, "busy"))

    async def scenario():
        await asyncio.gather(
            run(service1, "service-1", "lifecycle-lease-holder"),
            run(service2, "service-2", "lifecycle-lease-contender"),
        )

    asyncio.run(scenario())
    with _connect(db_path) as audit:
        assert audit.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 1
        lease = lease_svc.current_lease(audit)
        assert lease is not None and lease.released_at is not None
    results = [result for _label, result in outcomes]
    assert results.count("executed") == 1
    assert results.count("busy") == 1

def test_database_error_is_observable_retriable_and_does_not_kill_loop(db_path):
    missing_db = db_path.parent / "missing-dir" / "cannot-open.db"
    service = rss.ReminderSchedulerService(
        missing_db, enabled=True, poll_seconds=0.03, initial_delay=0,
        shutdown_timeout_seconds=2,
    )

    async def scenario():
        await service.start()
        deadline = asyncio.get_running_loop().time() + 2
        while service.snapshot()["last_error"] == "":
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(service.snapshot())
            await asyncio.sleep(0.02)
        await service.stop()
        return service.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["state"] == "stopped"
    assert snapshot["last_result"] == "database_error"
    assert snapshot["consecutive_database_errors"] >= 1
    assert snapshot["last_error"]


class AssertedTimeout(AssertionError):
    pass


def test_busy_lease_is_normal_idle_status_not_database_failure(db_path):
    calls = 0

    def runner(connection, **kwargs):
        nonlocal calls
        calls += 1
        raise rs.LeaseBusyError(result={"status": "busy"})

    service = rss.ReminderSchedulerService(
        db_path, runner=runner, poll_seconds=0.03, initial_delay=0,
        shutdown_timeout_seconds=2,
    )

    async def scenario():
        await service.start()
        deadline = asyncio.get_running_loop().time() + 2
        while calls == 0:
            await asyncio.sleep(0.02)
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("runner was not invoked")
        await service.stop()
        return service.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["last_result"] == "lease_busy"
    assert snapshot["consecutive_database_errors"] == 0
    assert snapshot["last_error"] == ""


def test_shutdown_wait_is_bounded_and_reports_thread_that_remains_active(db_path):
    entered = threading.Event()
    release = threading.Event()

    def slow_runner(connection, **kwargs):
        entered.set()
        release.wait(2)
        return {"status": "succeeded", "run_id": 99}

    service = rss.ReminderSchedulerService(
        db_path, runner=slow_runner, poll_seconds=30, initial_delay=0,
        shutdown_timeout_seconds=0.05,
    )

    async def scenario():
        await service.start()
        deadline = asyncio.get_running_loop().time() + 1
        while not entered.is_set():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        await service.stop()
        return service.snapshot()

    snapshot = asyncio.run(scenario())
    try:
        assert snapshot["state"] == "stopped_with_active_tick"
        assert snapshot["active_tick"] is True
        assert snapshot["shutdown_timed_out"] is True
        assert snapshot["loop_running"] is False
    finally:
        release.set()
        active = service.active_tick_future()
        if active is not None:
            active.result(timeout=2)


# --------------------------------------------------------------------------- #
# run_tick heartbeat and cooperative stop boundaries
# --------------------------------------------------------------------------- #

def test_long_tick_renews_lease_at_phase_boundary(conn, monkeypatch):
    heartbeats: list[datetime] = []
    original = lease_svc.heartbeat_lease

    def tracking_heartbeat(connection, **kwargs):
        heartbeats.append(kwargs["now"])
        return original(connection, **kwargs)

    monkeypatch.setattr(lease_svc, "heartbeat_lease", tracking_heartbeat)
    result = rs.run_tick(
        conn, now=NOW, run_key="long-tick-heartbeat", lease_ttl_seconds=30,
        clock=lambda: NOW + timedelta(seconds=11), heartbeat_interval_seconds=10,
    )

    assert result["status"] == "succeeded"
    assert heartbeats == [NOW + timedelta(seconds=11)]
    lease = lease_svc.current_lease(conn)
    assert lease is not None
    assert lease.released_at is not None


def test_heartbeat_database_error_fails_tick_without_pretending_success(conn, monkeypatch):
    calls = 0
    original = lease_svc.heartbeat_lease

    def fail_after_run_create(connection, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(connection, **kwargs)
        return lease_svc.LeaseResult(
            lease_svc.STATUS_DATABASE_ERROR, error_code="database_error", message="disk unavailable",
        )

    monkeypatch.setattr(lease_svc, "heartbeat_lease", fail_after_run_create)

    boundary_times = iter([NOW + timedelta(seconds=11), NOW + timedelta(seconds=22)])

    with pytest.raises(rs.LeaseDatabaseError):
        rs.run_tick(
            conn, now=NOW, run_key="heartbeat-db-error", lease_ttl_seconds=30,
            clock=lambda: next(boundary_times), heartbeat_interval_seconds=10,
        )
    row = conn.execute(
        "SELECT status,last_error FROM reminder_scheduler_runs WHERE run_key=?",
        ("heartbeat-db-error",),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["last_error"]


def test_cooperative_stop_takes_effect_at_candidate_batch_boundary(conn):
    _task_due_tomorrow(conn)
    stages: list[str] = []

    def should_stop(stage: str) -> bool:
        stages.append(stage)
        return stage == "candidate_batch"

    with pytest.raises(rs.SchedulerStopped):
        rs.run_tick(
            conn, now=NOW, run_key="stop-at-candidate-boundary",
            should_stop=should_stop,
        )

    run = conn.execute(
        "SELECT * FROM reminder_scheduler_runs WHERE run_key=?",
        ("stop-at-candidate-boundary",),
    ).fetchone()
    assert run["status"] == "failed"
    assert run["created_count"] == 1
    assert run["due_count"] == 0
    assert "candidate_batch" in stages



def test_startup_resource_failure_clears_executor_and_marks_failed(db_path, monkeypatch):
    def fail_executor(name):
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(rss, "_SingleThreadTickExecutor", fail_executor)
    service = rss.ReminderSchedulerService(db_path, poll_seconds=30)

    async def scenario():
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await service.start()

    asyncio.run(scenario())
    snapshot = service.snapshot()
    assert snapshot["state"] == "failed"
    assert snapshot["loop_running"] is False
    assert snapshot["active_tick"] is False


def test_sqlite_write_lock_is_classified_as_database_error(db_path):
    locker = _connect(db_path)
    try:
        locker.execute("BEGIN IMMEDIATE")
        service = rss.ReminderSchedulerService(
            db_path, poll_seconds=30, shutdown_timeout_seconds=2,
        )

        async def scenario():
            with pytest.raises(rs.LeaseDatabaseError):
                await service.tick_once(run_key="locked-lease-acquisition")
            return service.snapshot()

        snapshot = asyncio.run(scenario())
        assert snapshot["last_result"] == "database_error"
        assert snapshot["consecutive_database_errors"] == 1
    finally:
        locker.execute("ROLLBACK")
        locker.close()


def test_tick_logs_include_timing_and_lease_lifecycle_without_secrets(db_path, caplog):
    service = rss.ReminderSchedulerService(db_path, poll_seconds=30)
    caplog.set_level(logging.DEBUG, logger="personal_ai_os")

    async def successful_tick():
        return await service.tick_once(run_key="observability-success")

    result = asyncio.run(successful_tick())
    assert result["status"] == "succeeded"

    started = next(record for record in caplog.records if record.getMessage() == "reminder scheduler tick started")
    completed = next(record for record in caplog.records if record.getMessage() == "reminder scheduler tick completed")
    acquired = next(record for record in caplog.records if record.getMessage() == "reminder scheduler lease acquired")
    released = next(record for record in caplog.records if record.getMessage() == "reminder scheduler lease released")
    assert started.run_key == "observability-success"
    assert hasattr(completed, "elapsed_ms") and completed.elapsed_ms >= 0
    assert acquired.fencing_token >= 1
    assert released.fencing_token == acquired.fencing_token

    def fail_busy(path, **kwargs):
        raise rs.LeaseBusyError(result={"status": "busy"})

    failing = rss.ReminderSchedulerService(db_path, runner=fail_busy, poll_seconds=30)

    async def failed_tick():
        with pytest.raises(rs.LeaseBusyError):
            await failing.tick_once(run_key="observability-busy")

    asyncio.run(failed_tick())
    failed = next(record for record in caplog.records if record.getMessage() == "reminder scheduler tick failed")
    assert failed.result == "lease_busy"
    assert hasattr(failed, "elapsed_ms") and failed.elapsed_ms >= 0


def test_new_os_process_recovers_stale_running_run(tmp_path, monkeypatch):
    path = tmp_path / "service-recovery.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()
    old_time = "2026-10-13T08:00:00+00:00"
    setup = _connect(path)
    setup.execute(
        """
        INSERT INTO reminder_scheduler_runs(
            run_key,trigger_type,status,started_at,owner_token,created_at,
            processed_count,created_count,updated_count,skipped_count,due_count,
            expired_count,cancelled_count,recovered_run_count,error_count,last_error
        ) VALUES(?,?,?,?,?,?,0,0,0,0,0,0,0,0,0,'')
        """,
        ("old-running", "manual", "running", old_time, "crashed-owner", old_time),
    )
    setup.commit()
    setup.close()

    code = """
import asyncio, sqlite3
from app.main import init_db, DB_PATH
from app import reminder_scheduler_service as service

init_db()

def has_scheduled_run():
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT 1 FROM reminder_scheduler_runs WHERE trigger_type='scheduled' LIMIT 1"
        ).fetchone() is not None
    finally:
        conn.close()

async def main():
    scheduler = service.ReminderSchedulerService(
        DB_PATH, poll_seconds=0.05, initial_delay=0, shutdown_timeout_seconds=2,
    )
    await scheduler.start()
    # Wait for the tick to be recorded instead of guessing a fixed sleep budget:
    # under full-suite load, starting Python + importing FastAPI + finishing a tick
    # can exceed the old 0.35s window and made this test flaky.
    for _ in range(200):
        await asyncio.sleep(0.02)
        if has_scheduled_run():
            break
    await scheduler.stop()

asyncio.run(main())
"""
    env = {**os.environ, "DATABASE_PATH": str(path), "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        [sys.executable, "-c", code], cwd=Path.cwd(), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, (stdout, stderr)
    with _connect(path) as audit:
        old = audit.execute(
            "SELECT * FROM reminder_scheduler_runs WHERE run_key='old-running'"
        ).fetchone()
        new = audit.execute(
            "SELECT * FROM reminder_scheduler_runs WHERE trigger_type='scheduled' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        assert old["status"] == "abandoned"
        assert old["finished_at"] is not None
        assert new is not None and new["status"] == "succeeded"
        assert new["recovered_run_count"] == 1


# --------------------------------------------------------------------------- #
# FastAPI lifespan
# --------------------------------------------------------------------------- #

def _enabled_service_env(monkeypatch):
    monkeypatch.setenv("REMINDER_SCHEDULER_ENABLED", "1")
    monkeypatch.delenv("REMINDER_SCHEDULER_POLL_SECONDS", raising=False)
    monkeypatch.delenv("REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", raising=False)


def test_lifespan_invalid_scheduler_config_fails_before_database_or_connector_start(
    tmp_path, monkeypatch
):
    db_file = tmp_path / "invalid-reminder-config.db"
    calls: list[str] = []

    class FakeConnector:
        def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    monkeypatch.setenv("REMINDER_SCHEDULER_ENABLED", "maybe")
    monkeypatch.setattr("app.main.DB_PATH", db_file)
    monkeypatch.setattr("app.main.ConnectorScheduler", lambda *args, **kwargs: FakeConnector())

    with pytest.raises(ValueError, match="REMINDER_SCHEDULER_ENABLED"):
        with TestClient(app):
            pass

    assert not db_file.exists()
    assert calls == []


def test_lifespan_stops_connector_when_reminder_service_construction_fails(db_path, monkeypatch):
    calls: list[str] = []

    class FakeConnector:
        def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    class FailingReminderService:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("reminder service unavailable")

    monkeypatch.setattr("app.main.ConnectorScheduler", lambda *args, **kwargs: FakeConnector())
    monkeypatch.setattr("app.main.reminder_scheduler_service.ReminderSchedulerService", FailingReminderService)

    with pytest.raises(RuntimeError, match="reminder service unavailable"):
        with TestClient(app):
            pass
    assert calls == ["start", "stop"]
    import app.main as main_module
    assert main_module.connector_scheduler_instance is None
    assert main_module.reminder_scheduler_instance is None


def test_lifespan_defaults_do_not_start_reminder_loop(db_path, monkeypatch):
    monkeypatch.delenv("REMINDER_SCHEDULER_ENABLED", raising=False)
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["mode"] in {"local-demo", "cloud"}
        assert body["reminder_scheduler"]["enabled"] is False
        assert body["reminder_scheduler"]["loop_running"] is False


def test_lifespan_enabled_runs_real_scheduled_tick_in_temp_db(db_path, monkeypatch):
    _enabled_service_env(monkeypatch)
    # Hold one connection for the whole test. A brand-new WAL database has to
    # bootstrap its -shm/-wal pair; racing that bootstrap from the scheduler thread
    # is what yields "attempt to write a readonly database". init_db() re-enables
    # WAL inside lifespan, so a warm connection (not a journal-mode switch) is the
    # isolation-safe mitigation here.
    warm = _connect(db_path)
    try:
        with TestClient(app) as client:
            assert client.get("/api/health").json()["reminder_scheduler"]["enabled"] is True
            found = False
            for _ in range(50):
                with _connect(db_path) as audit:
                    count = audit.execute(
                        "SELECT COUNT(*) FROM reminder_scheduler_runs WHERE trigger_type='scheduled'"
                    ).fetchone()[0]
                if count:
                    found = True
                    break
                import time
                time.sleep(0.02)
            assert found
    finally:
        warm.close()


def test_two_real_os_processes_share_lease_without_duplicate_runs(tmp_path, monkeypatch):
    path = tmp_path / "service-subprocess.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()

    code = """
import asyncio
from app.main import init_db, DB_PATH
from app import reminder_scheduler_service as service

init_db()
async def main():
    scheduler = service.ReminderSchedulerService(
        DB_PATH, poll_seconds=0.12, initial_delay=0, shutdown_timeout_seconds=2,
    )
    await scheduler.start()
    await asyncio.sleep(0.45)
    await scheduler.stop()
asyncio.run(main())
"""
    env = {**os.environ, "DATABASE_PATH": str(path), "PYTHONIOENCODING": "utf-8"}
    processes = [
        subprocess.Popen([sys.executable, "-c", code], cwd=Path.cwd(), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        outputs.append((process.returncode, stdout, stderr))
    assert all(code == 0 for code, _stdout, _stderr in outputs), outputs
    with _connect(path) as audit:
        runs = audit.execute(
            "SELECT status, COUNT(*) AS n FROM reminder_scheduler_runs GROUP BY status"
        ).fetchall()
        assert runs
        assert {row["status"] for row in runs} <= {"succeeded", "partial_failed", "failed"}
        assert any(row["status"] == "succeeded" for row in runs)
        assert 1 <= sum(row["n"] for row in runs) <= 12
        assert audit.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0