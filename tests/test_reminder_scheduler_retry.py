"""Contract tests for transient-SQLite retry during reminder scheduler ticks.

A fresh WAL connection can lose a race with another connection opening/closing
the same file on Windows and receive SQLITE_READONLY ("attempt to write a
readonly database"). The failure is transient: an immediate retry on a new
connection succeeds. These tests pin that the tick retries exactly when it is
safe to do so and never duplicates a run row.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import reminder_scheduler as rs
from app import reminder_scheduler_lock as lease_svc
from app import reminder_scheduler_service as rss
from app.main import init_db


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "retry.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()
    return path


def _count_runs(path) -> int:
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM reminder_scheduler_runs WHERE trigger_type='scheduled'"
        ).fetchone()[0]
    finally:
        conn.close()


def _transient_result() -> lease_svc.LeaseResult:
    return lease_svc.LeaseResult(
        lease_svc.STATUS_DATABASE_ERROR,
        error_code="database_error",
        message="attempt to write a readonly database",
        retryable=True,
    )


def _permanent_result() -> lease_svc.LeaseResult:
    return lease_svc.LeaseResult(
        lease_svc.STATUS_DATABASE_ERROR,
        error_code="database_error",
        message="no such table: reminder_scheduler_leases",
        retryable=False,
    )


def test_transient_acquisition_error_is_retried_and_records_one_run(db_path, monkeypatch):
    calls = {"acquire": 0, "real": 0}
    real_acquire = lease_svc.acquire_lease

    def flaky_acquire(conn, **kwargs):
        calls["acquire"] += 1
        if calls["acquire"] == 1:
            return _transient_result()
        calls["real"] += 1
        return real_acquire(conn, **kwargs)

    monkeypatch.setattr(lease_svc, "acquire_lease", flaky_acquire)

    result = rss.run_scheduled_tick(
        db_path, run_key="scheduled:retry-1", owner_token="retry-owner",
    )

    assert calls["acquire"] == 2, "第一次瞬时错误后应重试一次"
    assert calls["real"] == 1
    assert result["status"] == "succeeded"
    assert _count_runs(db_path) == 1


def test_permanent_acquisition_error_is_not_retried(db_path, monkeypatch):
    calls = {"acquire": 0}

    def broken_acquire(conn, **kwargs):
        calls["acquire"] += 1
        return _permanent_result()

    monkeypatch.setattr(lease_svc, "acquire_lease", broken_acquire)

    with pytest.raises(rs.LeaseDatabaseError) as excinfo:
        rss.run_scheduled_tick(
            db_path, run_key="scheduled:retry-2", owner_token="retry-owner",
        )

    assert calls["acquire"] == 1, "不可重试的数据库错误必须立刻抛出"
    assert excinfo.value.retryable is False
    assert _count_runs(db_path) == 0


def test_retry_attempts_are_bounded(db_path, monkeypatch):
    calls = {"acquire": 0}

    def always_transient(conn, **kwargs):
        calls["acquire"] += 1
        return _transient_result()

    monkeypatch.setattr(lease_svc, "acquire_lease", always_transient)
    monkeypatch.setattr(rss, "TICK_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    with pytest.raises(rs.LeaseDatabaseError) as excinfo:
        rss.run_scheduled_tick(
            db_path, run_key="scheduled:retry-3", owner_token="retry-owner",
        )

    assert calls["acquire"] == rss.TICK_RETRY_ATTEMPTS
    assert excinfo.value.retryable is True
    assert _count_runs(db_path) == 0


def test_retry_preserves_run_key_so_a_completed_tick_is_not_duplicated(db_path):
    key = "scheduled:retry-4"
    first = rss.run_scheduled_tick(db_path, run_key=key, owner_token="owner-a")
    second = rss.run_scheduled_tick(db_path, run_key=key, owner_token="owner-b")

    assert first["run_key"] == second["run_key"] == key
    assert _count_runs(db_path) == 1


def test_retryable_classifier_accepts_readonly_only():
    readonly = sqlite3.OperationalError("attempt to write a readonly database")
    assert lease_svc.is_retryable_sqlite_error(readonly) is True


def test_retryable_classifier_excludes_busy_and_locked():
    # Busy/locked are covered by busy_timeout; retrying them would stack waits.
    busy = sqlite3.OperationalError("database is locked")
    locked = sqlite3.OperationalError("database table is locked")
    assert lease_svc.is_retryable_sqlite_error(busy) is False
    assert lease_svc.is_retryable_sqlite_error(locked) is False


def test_retryable_classifier_uses_extended_error_code_when_available():
    exc = sqlite3.OperationalError("disk I/O error")
    exc.sqlite_errorcode = 8  # SQLITE_READONLY, message alone would not match
    assert lease_svc.is_retryable_sqlite_error(exc) is True


def test_retryable_classifier_rejects_programming_errors():
    missing = sqlite3.OperationalError("no such table: leases")
    constraint = sqlite3.IntegrityError("UNIQUE constraint failed: x.y")
    assert lease_svc.is_retryable_sqlite_error(missing) is False
    assert lease_svc.is_retryable_sqlite_error(constraint) is False
    assert lease_svc.is_retryable_sqlite_error(ValueError("nope")) is False
