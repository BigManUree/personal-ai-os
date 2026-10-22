"""Fencing tests for scheduler-owned reminder writes (1.3.5-C-C-2b)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath

import pytest

from app import reminder_scheduler as rs
from app import reminder_scheduler_lock as lease_svc
from app import reminders as rm
from app.main import init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "scheduler_fencing.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()
    return path


CHILD_CODE = r"""
import json, sqlite3, sys
from datetime import datetime, timezone
from app.main import DB_PATH  # noqa: F401  (ensures env override is imported if needed)
from app import reminder_scheduler as rs
path, run_key, owner, now_iso = sys.argv[1:5]
sys.stdin.buffer.read(1)
c = sqlite3.connect(path, timeout=5, isolation_level=None)
c.row_factory = sqlite3.Row
c.execute("PRAGMA foreign_keys=ON")
now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
try:
    run = rs.run_tick(c, now=now, run_key=run_key, owner_token=owner, lease_ttl_seconds=30)
    print(json.dumps({"outcome": "executed", "status": run["status"], "run_id": run["run_id"]}))
except rs.LeaseBusyError as exc:
    print(json.dumps({"outcome": "busy", "error_code": exc.code, "lease_status": exc.result.get("status")}))
finally:
    c.close()
"""

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


def review_draft(scheduled_at: datetime, dedup_key: str):
    return {
        "rule_code": rm.RULE_REVIEW_DAILY,
        "rule_version": rm.RULE_VERSION,
        "source_type": "review",
        "source_id": None,
        "title": "每日复盘",
        "content": "请进行每日复盘",
        "priority": "normal",
        "status": "scheduled",
        "evidence": [],
        "coverage_status": "no_data",
        "dedup_key": dedup_key,
        "scheduled_at": scheduled_at,
        "due_at": None,
        "snoozed_until": None,
        "expires_at": scheduled_at + timedelta(hours=2),
        "cancel_reason": None,
        "occurrence_index": 1,
        "max_attempts": 1,
    }


def expired_review_reminder(c, *, key, scheduled_at, expires_at):
    c.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,title,content,priority,status,"
        "evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rm.RULE_REVIEW_DAILY, rm.RULE_VERSION, "review", "每日复盘", "内容", "normal",
         "scheduled", "[]", "no_data", key, rm.iso(scheduled_at), rm.iso(expires_at),
         rm.iso(NOW - timedelta(hours=2)), rm.iso(NOW - timedelta(hours=2))),
    )
    return int(c.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_busy_lease_creates_no_running_run_or_business_write(db_path):
    c1, c2 = connect(db_path), connect(db_path)
    try:
        active = lease_svc.acquire_lease(c1, now=NOW, ttl_seconds=60, owner_token="active",
                                         pid=1, hostname="h1", busy_timeout_ms=1000)
        assert active.acquired
        with pytest.raises(rs.LeaseBusyError) as exc:
            rs.run_tick(c2, now=NOW, run_key="blocked-by-active-lease", lease_ttl_seconds=60)
        assert exc.value.result["status"] == "busy"
        assert c2.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 0
        assert c2.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
        assert lease_svc.current_lease(c2).owner_token == "active"
    finally:
        c1.close()
        c2.close()



def test_stale_guard_cannot_create_reminder_after_takeover(conn):
    old = lease_svc.acquire_lease(conn, now=NOW, ttl_seconds=1, owner_token="old",
                                  pid=1, hostname="old", busy_timeout_ms=1000).lease
    new = lease_svc.acquire_lease(conn, now=NOW + timedelta(seconds=2), ttl_seconds=30,
                                  owner_token="new", pid=2, hostname="new",
                                  busy_timeout_ms=1000).lease
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(rs.LeaseLostError):
        rs.create_reminder_guarded(
            conn, review_draft(NOW, "review:fencing:stale-create"),
            now=NOW + timedelta(seconds=3), guard=rs._guard_from_lease(old),
        )
    assert conn.execute("SELECT COUNT(*) FROM reminders WHERE dedup_key=?",
                        ("review:fencing:stale-create",)).fetchone()[0] == 0


def test_stale_guard_cannot_transition_or_expire_reminder_after_takeover(conn):
    rid = expired_review_reminder(
        conn, key="review:fencing:stale-transition",
        scheduled_at=NOW - timedelta(minutes=2), expires_at=NOW - timedelta(minutes=1),
    )
    old = lease_svc.acquire_lease(conn, now=NOW - timedelta(seconds=10), ttl_seconds=1,
                                  owner_token="old", pid=1, hostname="old",
                                  busy_timeout_ms=1000).lease
    new = lease_svc.acquire_lease(conn, now=NOW, ttl_seconds=30, owner_token="new",
                                  pid=2, hostname="new", busy_timeout_ms=1000).lease
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(rs.LeaseLostError):
        rs.transition_reminder_guarded(
            conn, rid, "expire", now=NOW + timedelta(seconds=1),
            guard=rs._guard_from_lease(old),
        )
    row = conn.execute("SELECT status FROM reminders WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "scheduled"


def test_stale_guard_cannot_finish_run_owned_by_older_fencing_token(conn):
    old = lease_svc.acquire_lease(conn, now=NOW - timedelta(seconds=10), ttl_seconds=1,
                                  owner_token="old", pid=1, hostname="old",
                                  busy_timeout_ms=1000).lease
    run = rs._create_run(
        conn, now=NOW - timedelta(seconds=10), trigger_type=rs.TRIGGER_MANUAL,
        run_key="run:fencing:old", owner_token=old.owner_token,
        guard=rs._guard_from_lease(old),
    )
    new = lease_svc.acquire_lease(conn, now=NOW, ttl_seconds=30, owner_token="new",
                                  pid=2, hostname="new", busy_timeout_ms=1000).lease
    counters = {key: 0 for key in rs.COUNT_KEYS}
    with pytest.raises(rs.LeaseLostError):
        rs._finish_run(conn, run["id"], status=rs.STATUS_FAILED, now=NOW,
                       counters=counters, guard=rs._guard_from_lease(old))
    row = conn.execute("SELECT status,owner_token,lease_fencing_token FROM reminder_scheduler_runs WHERE id=?",
                       (run["id"],)).fetchone()
    assert row["status"] == "running"
    assert row["owner_token"] == "old"
    assert row["lease_fencing_token"] == old.fencing_token
    assert new.fencing_token > old.fencing_token


def _hold_lease(db_path, owner: str, ttl_seconds: float = 30):
    """Hold the cross-process lease the way an in-flight tick would.

    Two `run_tick` calls inside one process are serialized by the module lock, so
    they only "compete" if some other holder keeps the lease taken; relying on a
    stale `expires_at` would test the clock, not the lease.
    """
    holder_conn = connect(db_path)
    result = lease_svc.acquire_lease(
        holder_conn, now=NOW, ttl_seconds=ttl_seconds, owner_token=owner,
        pid=1, hostname=owner, busy_timeout_ms=1000,
    )
    assert result.status == lease_svc.STATUS_ACQUIRED
    return holder_conn, result.lease


def test_two_independent_connections_competing_for_tick_have_one_run_and_release(
    db_path, rollback_journal
):
    rollback_journal(db_path)
    holder_conn, holder = _hold_lease(db_path, "holder")
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(owner):
        c = connect(db_path)
        try:
            barrier.wait(timeout=5)
            try:
                result = rs.run_tick(c, now=NOW, run_key=f"concurrent-{owner}",
                                     owner_token=owner, lease_ttl_seconds=30)
                outcomes.append(("executed", result["status"], result["run_id"]))
            except rs.LeaseBusyError as exc:
                outcomes.append(("busy", exc.result["status"], None))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert sorted(item[0] for item in outcomes) == ["busy", "busy"]
    audit = connect(db_path)
    try:
        assert audit.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 0
    finally:
        audit.close()

    released = lease_svc.release_lease(
        holder_conn, owner_token=holder.owner_token, lease_token=holder.lease_token,
        fencing_token=holder.fencing_token, now=NOW, busy_timeout_ms=1000,
    )
    assert released.status == lease_svc.STATUS_ACQUIRED

    result = rs.run_tick(holder_conn, now=NOW, run_key="after-release",
                         owner_token="after", lease_ttl_seconds=30)
    assert result["status"] == rs.STATUS_SUCCEEDED

    audit = connect(db_path)
    try:
        assert audit.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 1
        run_row = audit.execute(
            "SELECT owner_token, lease_fencing_token, status FROM reminder_scheduler_runs"
        ).fetchone()
        assert run_row["status"] == rs.STATUS_SUCCEEDED
        assert run_row["owner_token"] == "after"
        assert run_row["lease_fencing_token"] == holder.fencing_token + 1
        lease = lease_svc.current_lease(audit)
        assert lease.released_at is not None
        assert lease.owner_token == run_row["owner_token"]
        assert lease.fencing_token == run_row["lease_fencing_token"]
    finally:
        audit.close()
        holder_conn.close()


def _run_tick_children(db_path, owners):
    procs = []
    for owner in owners:
        procs.append(subprocess.Popen(
            [sys.executable, "-c", CHILD_CODE, str(db_path), f"os-{owner}", owner,
             NOW.isoformat().replace("+00:00", "Z")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=FilePath.cwd(),
        ))
    time.sleep(0.5)
    for proc in procs:
        proc.stdin.write("g")
        proc.stdin.flush()
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, err
        outputs.append(json.loads(out.strip().splitlines()[-1]))
    return outputs


def test_two_os_processes_competing_for_tick_only_one_executes(db_path, rollback_journal):
    rollback_journal(db_path)
    holder_conn, holder = _hold_lease(db_path, "holder")

    blocked = _run_tick_children(db_path, ("proc-a", "proc-b"))
    assert sorted(item["outcome"] for item in blocked) == ["busy", "busy"]
    for item in blocked:
        assert item["lease_status"] == "busy"
    audit = connect(db_path)
    try:
        assert audit.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 0
    finally:
        audit.close()

    released = lease_svc.release_lease(
        holder_conn, owner_token=holder.owner_token, lease_token=holder.lease_token,
        fencing_token=holder.fencing_token, now=NOW, busy_timeout_ms=1000,
    )
    assert released.status == lease_svc.STATUS_ACQUIRED

    executed = _run_tick_children(db_path, ("proc-after",))
    assert [item["outcome"] for item in executed] == ["executed"]

    audit = connect(db_path)
    try:
        assert audit.execute("SELECT COUNT(*) FROM reminder_scheduler_runs").fetchone()[0] == 1
        run = audit.execute(
            "SELECT owner_token, lease_fencing_token, status FROM reminder_scheduler_runs"
        ).fetchone()
        assert run["status"] == "succeeded"
        assert run["owner_token"] == "proc-after"
        assert run["lease_fencing_token"] == holder.fencing_token + 1
        assert executed[0]["run_id"] is not None
        lease = lease_svc.current_lease(audit)
        assert lease.released_at is not None
        assert lease.owner_token == run["owner_token"]
        assert lease.fencing_token == run["lease_fencing_token"]
    finally:
        audit.close()
        holder_conn.close()


# --------------------------------------------------------------------------- #
# P0-1: expired-takeover end-to-end fencing
# --------------------------------------------------------------------------- #
# An old owner must be unable to write after its lease expires and a new owner
# takes over, even when the old owner resumes the full ``run_tick()`` path.
# This is distinct from ``test_two_os_processes_competing_for_tick_only_one_executes``
# (simultaneous competition) and from the stale-run recovery tests (abandonment
# after a crash). Here the old owner is deliberately paused mid-tick, the lease
# expires, a new owner takes over on an independent connection, and only then is
# the old owner allowed to continue its remaining guarded writes.

EXPIRED_TAKEOVER_CHILD_CODE = r"""
import json, sqlite3, sys
from datetime import datetime, timezone
from app import reminder_scheduler as rs

path, run_key, owner, now_iso, ttl = sys.argv[1:6]
now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
c = sqlite3.connect(path, timeout=5, isolation_level=None)
c.row_factory = sqlite3.Row
c.execute("PRAGMA foreign_keys=ON")

def should_stop(stage):
    # Pause exactly once after the old owner has created its running run but
    # before it reaches the candidate-generation writes. Resume when the parent
    # writes one byte on stdin (after the new owner has taken over).
    if stage == "after_run_create":
        sys.stdin.buffer.read(1)
    return False

try:
    run = rs.run_tick(
        c, now=now, run_key=run_key, owner_token=owner,
        lease_ttl_seconds=float(ttl), should_stop=should_stop,
    )
    print(json.dumps({"outcome": "executed", "status": run["status"], "run_id": run["run_id"]}))
except rs.LeaseLostError as exc:
    print(json.dumps({"outcome": "lease_lost", "code": exc.code}))
except rs.LeaseBusyError as exc:
    print(json.dumps({"outcome": "busy", "code": exc.code}))
except rs.SchedulerError as exc:
    print(json.dumps({"outcome": "scheduler_error", "code": exc.code}))
finally:
    c.close()
"""


def _wait_until(predicate, *, timeout, interval=0.02, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {description}")


def test_expired_takeover_old_owner_cannot_finish_full_tick(tmp_path, monkeypatch):
    path = tmp_path / "expired-takeover.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()

    # Seed one due-able task so a successful stale write would be observable.
    seed = connect(path)
    try:
        rm.update_settings(seed, {"task_due_enabled": True}, now=NOW)
        seed.execute(
            "INSERT INTO tasks(title,status,priority,due_date,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("fencing e2e task", "todo", "normal", "2026-10-13",
             rm.iso(NOW - timedelta(days=1)), rm.iso(NOW - timedelta(days=1))),
        )
        seed.commit()
    finally:
        seed.close()

    now = NOW
    child = subprocess.Popen(
        [sys.executable, "-c", EXPIRED_TAKEOVER_CHILD_CODE,
         str(path), "e2e-old", "old-owner", now.isoformat().replace("+00:00", "Z"), "1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=FilePath.cwd(),
    )

    audit = connect(path)
    try:
        # 1. old owner created its running run and is paused after run creation.
        def _old_run_exists():
            row = audit.execute(
                "SELECT status FROM reminder_scheduler_runs WHERE run_key='e2e-old'"
            ).fetchone()
            return row is not None and row["status"] == "running"
        _wait_until(_old_run_exists, timeout=10, description="old running run")

        # 2. wait for the old lease (ttl=1) to actually expire on the lease row.
        def _old_lease_expired():
            lease = lease_svc.current_lease(audit)
            if lease is None or lease.owner_token != "old-owner":
                return False
            return lease_svc.parse_iso(lease.expires_at) <= lease_svc.utc_now()
        _wait_until(_old_lease_expired, timeout=10, description="old lease expiry")

        # 3. new owner takes over on this independent connection.
        takeover = lease_svc.acquire_lease(
            audit, now=lease_svc.utc_now(), ttl_seconds=60, owner_token="new-owner",
            pid=os.getpid(), hostname="new-host", busy_timeout_ms=1000,
        )
        assert takeover.acquired
        assert takeover.lease.fencing_token == 2
    finally:
        # 4. always release the old owner to avoid a hanging child.
        try:
            child.stdin.write("g")
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    out, err = child.communicate(timeout=15)
    if child.returncode != 0:
        audit.close()
        raise AssertionError(f"child failed rc={child.returncode}: {err}")
    result = json.loads(out.strip().splitlines()[-1])

    with connect(path) as check:
        lease = lease_svc.current_lease(check)
        # fencing strictly increased and the new owner still owns the lease.
        assert lease.owner_token == "new-owner"
        assert lease.fencing_token == 2
        assert lease.released_at is None

        old_run = check.execute(
            "SELECT status, owner_token, lease_fencing_token "
            "FROM reminder_scheduler_runs WHERE run_key='e2e-old'"
        ).fetchone()
        assert old_run is not None
        # The old owner must not have been able to finish its own run. It either
        # reports lease_lost and leaves the run running (stale for later recovery),
        # or the run stays owned by the old fencing token and unfinished.
        assert old_run["owner_token"] == "old-owner"
        assert old_run["lease_fencing_token"] == 1

        # The stale write must not have created or advanced any due reminder.
        reminders = check.execute(
            "SELECT COUNT(*) AS n FROM reminders WHERE source_type='task'"
        ).fetchone()
        assert reminders["n"] == 0
        task_due = check.execute(
            "SELECT COUNT(*) AS n FROM reminders WHERE rule_code=? AND source_type='task'",
            (rm.RULE_TASK_DUE_SOON,),
        ).fetchone()
        assert task_due["n"] == 0

    # The old owner's observable outcome must be lease_lost; the guarded writes
    # after resume must all be rejected by fencing, never report a clean success.
    assert result["outcome"] == "lease_lost", result
