"""Cross-process SQLite lease tests for 1.3.5-C-C-2a."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import reminder_scheduler_lock as lock_svc
from app.main import init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 4, 0, 0, tzinfo=UTC)
CHILD_CODE = r"""
import json, os, sqlite3, sys
from datetime import datetime
from app import reminder_scheduler_lock as l
path, mode, now_iso, ttl = sys.argv[1:5]
ttl=int(ttl)
sys.stdin.buffer.read(1)
c=sqlite3.connect(path, timeout=5, isolation_level=None)
c.row_factory=sqlite3.Row
c.execute("PRAGMA busy_timeout=3000")
now=datetime.fromisoformat(now_iso.replace("Z","+00:00"))
r=l.acquire_lease(c, now=now, ttl_seconds=ttl, owner_token=mode+"-"+str(os.getpid()),
                  pid=os.getpid(), hostname="child", busy_timeout_ms=3000)
print(json.dumps({"status":r.status,"fencing":r.lease.fencing_token if r.lease else None,
                  "owner":r.lease.owner_token if r.lease else ""}))
c.close()
"""


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "reminder_lock.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    return path


@pytest.fixture
def conn(db_path):
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def connect(path):
    c = sqlite3.connect(path, timeout=5, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def acquire(c, owner="owner-a", now=NOW, ttl=30):
    return lock_svc.acquire_lease(c, now=now, ttl_seconds=ttl, owner_token=owner,
                                  pid=100, hostname="test-host", busy_timeout_ms=1000)


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_init_is_idempotent_and_creates_single_free_row(conn):
    lock_svc.init_lease_db(conn)
    lock_svc.init_lease_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM reminder_scheduler_leases").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM reminder_scheduler_leases WHERE scope='global'").fetchone()
    assert row["fencing_token"] == 0
    assert row["owner_token"] == ""


def test_first_acquire_succeeds_and_same_or_other_active_owner_is_busy(conn):
    first = acquire(conn, "owner-a")
    second = acquire(conn, "owner-a", now=NOW + timedelta(seconds=1))
    other = acquire(conn, "owner-b", now=NOW + timedelta(seconds=2))
    assert first.status == "acquired"
    assert first.lease.fencing_token == 1
    assert second.status == "busy"
    assert other.status == "busy"
    assert other.lease.owner_token == "owner-a"
    assert conn.execute("SELECT COUNT(*) FROM reminder_scheduler_leases").fetchone()[0] == 1


def test_heartbeat_requires_exact_owner_lease_and_fencing_token(conn):
    got = acquire(conn, "owner-a").lease
    ok = lock_svc.heartbeat_lease(conn, owner_token="owner-a", lease_token=got.lease_token,
                                  fencing_token=got.fencing_token, now=NOW + timedelta(seconds=5))
    cases = [
        ("owner-b", got.lease_token, got.fencing_token),
        ("owner-a", "x" * len(got.lease_token), got.fencing_token),
        ("owner-a", got.lease_token, got.fencing_token + 1),
    ]
    lost = [
        lock_svc.heartbeat_lease(conn, owner_token=o, lease_token=lt, fencing_token=f,
                                 now=NOW + timedelta(seconds=6 + i))
        for i, (o, lt, f) in enumerate(cases)
    ]
    assert ok.status == "acquired"
    assert parse(ok.lease.expires_at) > parse(got.expires_at)
    assert {item.status for item in lost} == {"lost"}


def test_release_is_conditional_and_then_another_owner_can_acquire(conn):
    got = acquire(conn, "owner-a").lease
    wrong = lock_svc.release_lease(conn, owner_token="owner-b", lease_token=got.lease_token,
                                   fencing_token=got.fencing_token, now=NOW + timedelta(seconds=1))
    assert wrong.status == "lost"
    ok = lock_svc.release_lease(conn, owner_token="owner-a", lease_token=got.lease_token,
                                fencing_token=got.fencing_token, now=NOW + timedelta(seconds=2))
    assert ok.status == "acquired"
    second = acquire(conn, "owner-b", now=NOW + timedelta(seconds=3))
    assert second.status == "acquired"
    assert second.lease.fencing_token == 2


def test_expiry_boundary_is_half_open_and_takeover_increments_fencing(conn):
    got = acquire(conn, "owner-a", ttl=10).lease
    at_expiry = NOW + timedelta(seconds=10)
    same = lock_svc.heartbeat_lease(conn, owner_token="owner-a", lease_token=got.lease_token,
                                    fencing_token=got.fencing_token, now=at_expiry, ttl_seconds=10)
    assert same.status == "lost"
    takeover = acquire(conn, "owner-b", now=at_expiry, ttl=10)
    assert takeover.status == "acquired"
    assert takeover.lease.fencing_token == 2
    old_heartbeat = lock_svc.heartbeat_lease(conn, owner_token="owner-a", lease_token=got.lease_token,
                                             fencing_token=1, now=at_expiry + timedelta(seconds=1))
    old_release = lock_svc.release_lease(conn, owner_token="owner-a", lease_token=got.lease_token,
                                         fencing_token=1, now=at_expiry + timedelta(seconds=2))
    assert old_heartbeat.status == old_release.status == "lost"
    assert lock_svc.current_lease(conn).owner_token == "owner-b"


def test_two_connections_competing_for_expired_lease_only_one_wins(db_path):
    seed = connect(db_path)
    try:
        assert lock_svc.acquire_lease(seed, now=NOW, ttl_seconds=1, owner_token="old",
                                      pid=1, hostname="h1", busy_timeout_ms=1000).acquired
    finally:
        seed.close()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(owner):
        local_conn = connect(db_path)
        try:
            barrier.wait(timeout=5)
            result = lock_svc.acquire_lease(
                local_conn, now=NOW + timedelta(seconds=2), ttl_seconds=30,
                owner_token=owner, pid=2, hostname="h2", busy_timeout_ms=3000,
            )
            results.append(result.status)
        finally:
            local_conn.close()

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert sorted(results) == ["acquired", "busy"]
    audit = connect(db_path)
    try:
        assert audit.execute("SELECT COUNT(*) FROM reminder_scheduler_leases").fetchone()[0] == 1
    finally:
        audit.close()


def test_busy_lock_returns_database_error_not_false_acquired(db_path):
    holder = connect(db_path)
    waiter = connect(db_path)
    try:
        lock_svc.init_lease_db(holder)
        holder.execute("BEGIN IMMEDIATE")
        result = lock_svc.acquire_lease(waiter, now=NOW, owner_token="locked",
                                        pid=2, hostname="h", busy_timeout_ms=50)
        assert result.status == "database_error"
        assert result.error_code == "database_error"
        assert result.lease is None
        assert not result.acquired
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        waiter.close()


def test_invalid_now_and_ttl_are_rejected(conn):
    with pytest.raises(lock_svc.LeaseError):
        lock_svc.acquire_lease(conn, now=datetime(2026, 10, 13, 4, 0), ttl_seconds=10, owner_token="x")
    with pytest.raises(lock_svc.LeaseError):
        lock_svc.acquire_lease(conn, now=NOW, ttl_seconds=0.5, owner_token="x")


def test_schema_works_through_main_init_db_and_initialization_is_repeatable(db_path):
    init_db()
    init_db()
    audit = connect(db_path)
    try:
        result = lock_svc.acquire_lease(audit, now=NOW, owner_token="recovered",
                                        pid=1, hostname="h", busy_timeout_ms=1000)
        assert result.acquired
    finally:
        audit.close()



class FailingCommitConnection:
    """Wrap a real sqlite3 connection and fail exactly one COMMIT."""

    def __init__(self, inner: sqlite3.Connection):
        self.inner = inner
        self.fail_commits_remaining = 1

    def execute(self, sql: str, params=()):
        if sql.strip().upper() == "COMMIT" and self.fail_commits_remaining:
            self.fail_commits_remaining -= 1
            raise sqlite3.OperationalError("injected commit failure")
        return self.inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_failed_acquire_commit_returns_database_error_and_new_connection_can_acquire(db_path):
    first_inner = connect(db_path)
    failing = FailingCommitConnection(first_inner)
    try:
        result = lock_svc.acquire_lease(
            failing, now=NOW, ttl_seconds=30, owner_token="failed-owner",
            pid=10, hostname="h", busy_timeout_ms=1000,
        )
        assert result.status == "database_error"
        assert result.error_code == "database_error"
        assert result.lease is None
        assert not result.acquired
    finally:
        first_inner.close()

    second = connect(db_path)
    try:
        retry = lock_svc.acquire_lease(
            second, now=NOW + timedelta(seconds=1), ttl_seconds=30,
            owner_token="healthy-owner", pid=11, hostname="h2", busy_timeout_ms=1000,
        )
        assert retry.status == "acquired"
        assert retry.lease.owner_token == "healthy-owner"
        assert retry.lease.fencing_token == 1
        assert second.execute("SELECT COUNT(*) FROM reminder_scheduler_leases").fetchone()[0] == 1
    finally:
        second.close()


def test_release_database_error_does_not_overwrite_new_owner_after_takeover(db_path):
    first = connect(db_path)
    second = connect(db_path)
    try:
        old = lock_svc.acquire_lease(
            first, now=NOW, ttl_seconds=1, owner_token="old",
            pid=1, hostname="old-host", busy_timeout_ms=1000,
        ).lease
        takeover = lock_svc.acquire_lease(
            second, now=NOW + timedelta(seconds=2), ttl_seconds=30,
            owner_token="new", pid=2, hostname="new-host", busy_timeout_ms=1000,
        )
        assert takeover.status == "acquired"
        assert takeover.lease.fencing_token == old.fencing_token + 1

        class CommitFailOnce:
            def __init__(self, inner):
                self.inner = inner
                self.fail_commit = True

            def execute(self, sql, params=()):
                if self.fail_commit and sql.strip().upper() == "COMMIT":
                    self.fail_commit = False
                    raise sqlite3.OperationalError("injected release commit failure")
                return self.inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self.inner, name)


        result = lock_svc.release_lease(
            CommitFailOnce(first), owner_token=old.owner_token, lease_token=old.lease_token,
            fencing_token=old.fencing_token, now=NOW + timedelta(seconds=3),
            busy_timeout_ms=1000,
        )
        assert result.status in {"lost", "database_error"}
        current = lock_svc.current_lease(second)
        assert current.owner_token == "new"
        assert current.fencing_token == 2
    finally:
        first.close()
        second.close()


def test_concurrent_initialization_keeps_one_free_lease_row(db_path, rollback_journal):
    rollback_journal(db_path)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        local_conn = connect(db_path)
        try:
            barrier.wait(timeout=5)
            lock_svc.init_lease_db(local_conn)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            local_conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert errors == []

    audit = connect(db_path)
    try:
        rows = audit.execute("SELECT scope, owner_token, fencing_token FROM reminder_scheduler_leases").fetchall()
        assert len(rows) == 1
        assert rows[0]["scope"] == "global"
        assert rows[0]["owner_token"] == ""
        assert rows[0]["fencing_token"] == 0
    finally:
        audit.close()


def run_child_pair(db_path: Path, mode: str, now: datetime, ttl: int):
    procs = []
    for _ in range(2):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", CHILD_CODE, str(db_path), mode,
             now.isoformat().replace("+00:00", "Z"), str(ttl)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=Path.cwd(),
        ))
    time.sleep(0.5)
    for p in procs:
        p.stdin.write("g")
        p.stdin.flush()
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=10)
        assert p.returncode == 0, err
        outputs.append(json.loads(out.strip().splitlines()[-1]))
    return outputs


def test_two_os_processes_compete_for_initial_acquisition(db_path):
    c = connect(db_path)
    try:
        lock_svc.init_lease_db(c)
    finally:
        c.close()
    outputs = run_child_pair(db_path, "initial", NOW, 30)
    assert sorted(item["status"] for item in outputs) == ["acquired", "busy"]
    assert len({item["owner"] for item in outputs if item["status"] == "acquired"}) == 1


def test_two_os_processes_compete_for_expired_takeover(db_path):
    c = connect(db_path)
    try:
        assert lock_svc.acquire_lease(c, now=NOW, ttl_seconds=1, owner_token="old",
                                      pid=1, hostname="seed").acquired
    finally:
        c.close()
    outputs = run_child_pair(db_path, "takeover", NOW + timedelta(seconds=2), 30)
    assert sorted(item["status"] for item in outputs) == ["acquired", "busy"]
    acquired = [item for item in outputs if item["status"] == "acquired"][0]
    assert acquired["fencing"] == 2
    audit = connect(db_path)
    try:
        assert audit.execute("SELECT fencing_token FROM reminder_scheduler_leases").fetchone()[0] == 2
    finally:
        audit.close()
