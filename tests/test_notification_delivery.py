"""C-C-2d notification outbox and fake delivery tests.

All database tests use an explicit tmp_path SQLite file. The only channel in
this phase is the in-process ``fake_test`` double; tests must never perform
network, desktop, email, SMS, or webhook delivery.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import notification_delivery as nd
from app import notification_delivery_service as nds
from app import reminder_scheduler as rs
from app import reminders as rm
from app.main import app, init_db

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "notification-delivery.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setattr("app.main.DB_PATH", path)
    init_db()
    return path


@pytest.fixture
def conn(db_path):
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    yield connection
    connection.close()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _review_reminder(
    connection: sqlite3.Connection,
    reminder_id: int | None = None,
    *,
    scheduled_at: datetime = NOW - timedelta(minutes=1),
    snoozed_until: datetime | None = None,
    status: str = rm.STATUS_SCHEDULED,
    key: str = "review:notification:1",
    expires_at: datetime | None = None,
) -> sqlite3.Row:
    if reminder_id is None:
        ts = rm.iso(NOW - timedelta(hours=1))
        connection.execute(
            "INSERT INTO reminders(rule_code,rule_version,source_type,title,content,priority,status,"
            "evidence_json,coverage_status,dedup_key,scheduled_at,snoozed_until,expires_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rm.RULE_REVIEW_DAILY, rm.RULE_VERSION, "review", "每日复盘", "请复盘今天", "normal",
             status, "[]", "no_data", key, rm.iso(scheduled_at),
             rm.iso(snoozed_until) if snoozed_until else None,
             rm.iso(expires_at or (NOW + timedelta(hours=2))),
             ts, ts),
        )
        reminder_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    return rm.get_reminder(connection, reminder_id)


def _run_tick_and_only_due_reminder(
    connection: sqlite3.Connection,
    run_key: str,
    *,
    now: datetime = NOW,
) -> tuple[dict, sqlite3.Row, dict]:
    reminder = _review_reminder(connection)
    result = rs.run_tick(connection, now=now, run_key=run_key)
    due = connection.execute(
        "SELECT * FROM reminders WHERE id=? AND status=?", (reminder["id"], rm.STATUS_DUE),
    ).fetchall()
    assert len(due) == 1, [dict(row) for row in connection.execute("SELECT * FROM reminders")]
    event = connection.execute("SELECT * FROM reminder_notification_events").fetchone()
    return result, due[0], dict(event)


def _event_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM reminder_notification_events").fetchone()[0])


def _delivery_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM reminder_notification_deliveries").fetchone()[0])


# --------------------------------------------------------------------------- #
# domain model, idempotency and schema
# --------------------------------------------------------------------------- #

def test_backoff_is_deterministic_capped_and_uses_no_random_jitter():
    assert nd.backoff_delay_seconds(1) == 30
    assert nd.backoff_delay_seconds(2) == 60
    assert nd.backoff_delay_seconds(3) == 120
    assert nd.backoff_delay_seconds(4) == 240
    assert nd.backoff_delay_seconds(6) == 960
    assert nd.backoff_delay_seconds(7) == 1920
    assert nd.backoff_delay_seconds(8) == 3600
    assert nd.backoff_delay_seconds(9) == 3600


def test_reminder_due_event_key_distinguishes_initial_and_each_snooze_occurrence(conn):
    first = _review_reminder(conn, scheduled_at=NOW - timedelta(minutes=5))
    first_key = nd.reminder_due_event_key(first)
    assert first_key == f"reminder.due.v1:{first['id']}:{first['scheduled_at']}"

    snooze_one = NOW + timedelta(minutes=10)
    conn.execute(
        "UPDATE reminders SET status='snoozed', snoozed_until=?, updated_at=? WHERE id=?",
        (rm.iso(snooze_one), rm.iso(NOW), first["id"]),
    )
    second = rm.get_reminder(conn, first["id"])
    assert nd.reminder_due_event_key(second) == f"reminder.due.v1:{first['id']}:{rm.iso(snooze_one)}"

    snooze_two = NOW + timedelta(minutes=20)
    conn.execute(
        "UPDATE reminders SET snoozed_until=?, updated_at=? WHERE id=?",
        (rm.iso(snooze_two), rm.iso(NOW), first["id"]),
    )
    third = rm.get_reminder(conn, first["id"])
    assert nd.reminder_due_event_key(third) != nd.reminder_due_event_key(second)


def test_create_reminder_due_outbox_is_idempotent_within_and_across_connections(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    first = nd.create_reminder_due_outbox(conn, reminder, now=NOW)
    assert first.event_created is True
    assert first.delivery_created is True
    assert first.event["event_type"] == nd.EVENT_TYPE_REMINDER_DUE
    assert first.event["reminder_id"] == reminder["id"]
    assert first.delivery["channel"] == nd.CHANNEL_FAKE_TEST
    assert first.delivery["status"] == nd.STATUS_PENDING
    assert first.delivery["attempt_count"] == 0
    assert first.delivery["delivery_key"] == (
        f"{first.event['event_key']}:{nd.CHANNEL_FAKE_TEST}:local"
    )

    second = nd.create_reminder_due_outbox(conn, dict(reminder), now=NOW + timedelta(minutes=1))
    assert second.event_created is False
    assert second.delivery_created is False
    assert second.event["id"] == first.event["id"]
    assert second.delivery["id"] == first.delivery["id"]
    assert _event_count(conn) == 1
    assert _delivery_count(conn) == 1

    with _connect(Path(conn.execute("PRAGMA database_list").fetchone()["file"])) as other:
        third = nd.create_reminder_due_outbox(other, dict(reminder), now=NOW + timedelta(minutes=2))
        assert third.event_created is False
        assert third.delivery_created is False


def test_database_unique_constraints_are_the_final_idempotency_guard(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    outcome = nd.create_reminder_due_outbox(conn, reminder, now=NOW)
    ts = rm.iso(NOW)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reminder_notification_events(event_key,event_type,reminder_id,source_type,"
            "source_id,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (outcome.event["event_key"], nd.EVENT_TYPE_REMINDER_DUE, reminder["id"], "review", None,
             "{}", ts, ts),
        )
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO reminder_notification_deliveries(event_id,delivery_key,channel,recipient,"
            "status,scheduled_at,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (outcome.event["id"], outcome.delivery["delivery_key"], nd.CHANNEL_FAKE_TEST, "local",
             nd.STATUS_PENDING, ts, ts, ts, ts),
        )


def test_payload_contains_only_notification_fields_and_no_environment_secret(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    payload = nd.build_reminder_due_payload(reminder, now=NOW)
    assert payload == {
        "reminder_id": reminder["id"],
        "title": "每日复盘",
        "content": "请复盘今天",
        "priority": "normal",
        "source_type": "review",
        "source_id": None,
        "rule_code": rm.RULE_REVIEW_DAILY,
        "scheduled_at": reminder["scheduled_at"],
        "snoozed_until": None,
        "due_at": reminder["due_at"],
        "event_time": rm.iso(NOW),
    }
    assert "LLM_API_KEY" not in repr(payload)


# --------------------------------------------------------------------------- #
# claim, retry, dead-letter and recovery state machine
# --------------------------------------------------------------------------- #

def test_claim_pending_delivery_uses_database_conditional_update(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    created = nd.create_reminder_due_outbox(conn, reminder, now=NOW)
    claimed = nd.claim_next_delivery(conn, "worker-1", now=NOW, lock_seconds=300)
    assert claimed is not None
    assert claimed["id"] == created.delivery["id"]
    assert claimed["status"] == nd.STATUS_RUNNING
    assert claimed["attempt_count"] == 1
    assert claimed["locked_by"] == "worker-1"
    assert claimed["lock_expires_at"] == rm.iso(NOW + timedelta(seconds=300))

    with _connect(Path(conn.execute("PRAGMA database_list").fetchone()["file"])) as other:
        assert nd.claim_next_delivery(other, "worker-2", now=NOW, lock_seconds=300) is None


def test_success_records_fake_external_message_id_and_cannot_be_claimed_again(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    nd.create_reminder_due_outbox(conn, reminder, now=NOW)
    delivery = nd.claim_next_delivery(conn, "worker", now=NOW)
    result = nd.DeliverySuccess(external_message_id="fake-42")
    completed = nd.complete_delivery(conn, delivery["id"], "worker", result, now=NOW)
    assert completed["status"] == nd.STATUS_SUCCEEDED
    assert completed["external_message_id"] == "fake-42"
    assert completed["succeeded_at"] == rm.iso(NOW)
    assert completed["locked_by"] is None
    assert nd.claim_next_delivery(conn, "worker", now=NOW) is None


def test_transient_failure_waits_for_deterministic_backoff_then_dead_letters_at_limit(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    nd.create_reminder_due_outbox(conn, reminder, now=NOW)
    first = nd.claim_next_delivery(conn, "worker", now=NOW)
    failed = nd.record_delivery_failure(
        conn, first["id"], "worker", nd.DeliveryTransientError("temporarily unavailable"), now=NOW,
    )
    assert failed["status"] == nd.STATUS_RETRY_WAITING
    assert failed["attempt_count"] == 1
    assert failed["next_retry_at"] == rm.iso(NOW + timedelta(seconds=30))
    assert failed["last_error_code"] == "transient"
    assert nd.claim_next_delivery(conn, "worker", now=NOW + timedelta(seconds=29)) is None

    claimed = nd.claim_next_delivery(conn, "worker", now=NOW + timedelta(seconds=30))
    assert claimed is not None and claimed["attempt_count"] == 2

    # Directly model the remaining attempts without waiting for production backoff.
    current = claimed
    for attempt in range(3, 6):
        current = nd.record_delivery_failure(
            conn, current["id"], "worker", nd.DeliveryTimeoutError("timed out"),
            now=NOW + timedelta(seconds=30 * attempt),
        )
        if attempt < 5:
            retry_after = rm.parse_iso(current["next_retry_at"], field_name="next_retry_at")
            current = nd.claim_next_delivery(conn, "worker", now=retry_after + timedelta(seconds=1),
                                             lock_seconds=300)
            assert current is not None and current["attempt_count"] == attempt
    fifth = nd.claim_next_delivery(
        conn, "worker", now=rm.parse_iso(current["next_retry_at"], field_name="next_retry_at")
        + timedelta(seconds=1), lock_seconds=300,
    )
    assert fifth is not None and fifth["attempt_count"] == 5
    current = nd.record_delivery_failure(
        conn, fifth["id"], "worker", nd.DeliveryTimeoutError("timed out again"),
        now=NOW + timedelta(minutes=10),
    )
    assert current["status"] == nd.STATUS_DEAD_LETTER
    assert current["attempt_count"] == 5
    assert current["dead_letter_at"]
    assert current["last_error_code"] == "timeout"
    assert nd.claim_next_delivery(conn, "worker", now=NOW + timedelta(days=1)) is None


def test_permanent_failure_dead_letters_immediately(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    nd.create_reminder_due_outbox(conn, reminder, now=NOW)
    delivery = nd.claim_next_delivery(conn, "worker", now=NOW)
    result = nd.record_delivery_failure(
        conn, delivery["id"], "worker", nd.DeliveryPermanentError("unknown recipient"), now=NOW,
    )
    assert result["status"] == nd.STATUS_DEAD_LETTER
    assert result["attempt_count"] == 1
    assert result["dead_letter_at"] == rm.iso(NOW)
    assert result["last_error_code"] == "permanent"


def test_stale_running_delivery_is_conservatively_returned_to_retry_waiting(conn):
    reminder = _review_reminder(conn, status=rm.STATUS_DUE)
    created = nd.create_reminder_due_outbox(conn, reminder, now=NOW - timedelta(minutes=10))
    conn.execute(
        "UPDATE reminder_notification_deliveries SET status='running', attempt_count=1, "
        "claimed_at=?, locked_by=?, lock_expires_at=?, updated_at=? WHERE id=?",
        (rm.iso(NOW - timedelta(minutes=10)), "dead-worker",
         rm.iso(NOW - timedelta(minutes=5, seconds=1)), rm.iso(NOW - timedelta(minutes=10)),
         created.delivery["id"]),
    )
    count = nd.recover_stale_deliveries(conn, now=NOW, lock_timeout_seconds=300)
    assert count == 1
    row = nd.get_delivery(conn, created.delivery["id"])
    assert row["status"] == nd.STATUS_RETRY_WAITING
    assert row["attempt_count"] == 1
    assert row["locked_by"] is None
    assert row["next_retry_at"] == rm.iso(NOW)
    assert row["last_error_code"] == "stale_running"


def test_error_summary_redacts_credentials_before_persistence(conn):
    message = 'failed with api_key=abcdefgh12345678 and Authorization: Bearer abcdefghijklmnop'
    safe = nd.sanitize_error_message(message)
    assert "abcdefgh12345678" not in safe
    assert "abcdefghijklmnop" not in safe
    assert "api_key=***" in safe
    assert "Bearer ***" in safe


def test_two_os_subprocesses_claim_the_same_delivery_at_most_once(db_path):
    with _connect(db_path) as setup:
        reminder = _review_reminder(setup, status=rm.STATUS_DUE)
        nd.create_reminder_due_outbox(setup, reminder, now=datetime.now(UTC))

    deadline = time.time() + 1.0
    code = """
import sqlite3, sys, time
from datetime import datetime, timezone
from pathlib import Path
from app import notification_delivery as nd
path = Path(sys.argv[1])
deadline = float(sys.argv[2])
while time.time() < deadline:
    time.sleep(0.001)
conn = sqlite3.connect(path, timeout=5, isolation_level=None)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA foreign_keys=ON')
row = nd.claim_next_delivery(conn, sys.argv[3], now=datetime.now(timezone.utc), lock_seconds=60)
print(row['id'] if row is not None else '')
conn.close()
"""
    env = dict(os.environ)
    env["DATABASE_PATH"] = str(db_path)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(db_path), str(deadline), f"subprocess-{index}"],
            cwd=Path.cwd(), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for index in (1, 2)
    ]
    outputs = []
    errors = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
        outputs.append(stdout.strip())
        errors.append(stderr)
    assert sorted(outputs) == ["", "1"]
    with _connect(db_path) as audit:
        row = audit.execute("SELECT status,locked_by FROM reminder_notification_deliveries").fetchone()
        assert row["status"] == nd.STATUS_RUNNING
        assert row["locked_by"].startswith("subprocess-")


# --------------------------------------------------------------------------- #
# fake channel
# --------------------------------------------------------------------------- #

def test_fake_channel_scripts_success_transient_permanent_timeout_and_repeats():
    channel = nd.FakeNotificationChannel(outcomes=[
        nd.DeliverySuccess("fake-1"),
        nd.DeliveryTransientError("temporary"),
        nd.DeliveryPermanentError("permanent"),
        nd.DeliveryTimeoutError("timeout"),
    ], default_success=False)
    assert channel.name == nd.CHANNEL_FAKE_TEST
    delivery = {"id": 7, "attempt_count": 1}
    event = {"id": 3, "payload": {"title": "测试"}}
    assert channel.send(delivery, event).external_message_id == "fake-1"
    with pytest.raises(nd.DeliveryTransientError):
        channel.send(delivery, event)
    with pytest.raises(nd.DeliveryPermanentError):
        channel.send(delivery, event)
    with pytest.raises(nd.DeliveryTimeoutError):
        channel.send(delivery, event)
    assert len(channel.calls) == 4
    assert all(call["event"] == event for call in channel.calls)
    assert channel.used_network is False


# --------------------------------------------------------------------------- #
# reminder scheduler transactional integration
# --------------------------------------------------------------------------- #

def test_scheduler_marks_due_and_persists_event_and_delivery_in_same_tick(conn):
    rm.update_settings(conn, {"daily_review_enabled": True}, now=NOW)
    result, reminder, event = _run_tick_and_only_due_reminder(conn, "notification-due-1")
    delivery = conn.execute("SELECT * FROM reminder_notification_deliveries").fetchone()
    assert result["due_count"] == 1
    assert event["event_key"] == f"reminder.due.v1:{reminder['id']}:{reminder['scheduled_at']}"
    assert delivery["status"] == nd.STATUS_PENDING
    assert delivery["channel"] == nd.CHANNEL_FAKE_TEST


def test_duplicate_tick_after_due_creates_no_duplicate_notification(conn):
    rm.update_settings(conn, {"daily_review_enabled": True}, now=NOW)
    first, _reminder, _event = _run_tick_and_only_due_reminder(conn, "notification-due-first")
    second = rs.run_tick(conn, now=NOW + timedelta(minutes=1), run_key="notification-due-second")
    assert first["due_count"] == 1
    assert second["due_count"] == 0
    assert _event_count(conn) == 1
    assert _delivery_count(conn) == 1


def test_quiet_hours_do_not_create_notification_intent(conn):
    rm.update_settings(
        conn,
        {"daily_review_enabled": True, "quiet_hours_enabled": True,
         "quiet_start": "09:00", "quiet_end": "11:00"},
        now=NOW,
    )
    reminder = _review_reminder(
        conn, scheduled_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=3), key="review:notification:quiet",
    )
    result = rs.run_tick(conn, now=NOW, run_key="notification-quiet")
    assert result["due_count"] == 0
    assert rm.get_reminder(conn, reminder["id"])["status"] == rm.STATUS_SCHEDULED
    assert _event_count(conn) == 0


def test_expired_or_cancelled_reminders_never_create_due_notification(conn):
    ts = rm.iso(NOW - timedelta(hours=1))
    conn.execute(
        "INSERT INTO tasks(title,status,priority,due_date,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("过期任务", "todo", "normal", "2026-10-01", ts, ts),
    )
    task_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,source_id,task_id,title,content,"
        "priority,status,evidence_json,coverage_status,dedup_key,scheduled_at,expires_at,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rm.RULE_TASK_DUE_SOON, rm.RULE_VERSION, "task", task_id, task_id, "任务到期", "内容",
         "normal", rm.STATUS_SCHEDULED, "[]", "no_data", "task:notification:expired",
         rm.iso(NOW - timedelta(hours=2)), rm.iso(NOW - timedelta(minutes=1)), ts, ts),
    )
    expired_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    result = rs.run_tick(conn, now=NOW, run_key="notification-expired")
    assert result["due_count"] == 0
    assert rm.get_reminder(conn, expired_id)["status"] == rm.STATUS_EXPIRED
    assert _event_count(conn) == 0

    rm.update_settings(conn, {"daily_review_enabled": True}, now=NOW)
    cancellable = _review_reminder(
        conn,
        scheduled_at=NOW + timedelta(hours=2, minutes=59),
        expires_at=NOW + timedelta(hours=5),
        key="review:notification:cancellable",
    )
    tick_time = NOW + timedelta(hours=3)
    lease = rs.lease_svc.acquire_lease(
        conn, now=tick_time, owner_token="cancel-owner", ttl_seconds=300,
    )
    guard = rs._guard_from_lease(lease.lease)
    assert rs._mark_due_one(conn, cancellable, now=tick_time, guard=guard) is True
    event_count_after_due = _event_count(conn)
    assert event_count_after_due == 1
    rs.transition_reminder_guarded(
        conn, cancellable["id"], "cancel", now=tick_time + timedelta(minutes=1), guard=guard,
        cancel_reason="user_cancelled",
    )
    assert rm.get_reminder(conn, cancellable["id"])["status"] == rm.STATUS_CANCELLED
    assert _event_count(conn) == event_count_after_due
    assert _delivery_count(conn) == 1


def test_notification_insert_failure_rolls_back_reminder_due_transition(conn, monkeypatch):
    reminder = _review_reminder(conn, status=rm.STATUS_SCHEDULED)
    lease = rs.lease_svc.acquire_lease(conn, now=NOW, owner_token="rollback-owner", ttl_seconds=30)
    guard = rs._guard_from_lease(lease.lease)

    def oversized_payload(_reminder, *, now):
        return {"content": "x" * 6000, "event_time": rm.iso(now)}

    monkeypatch.setattr(rs.nd, "build_reminder_due_payload", oversized_payload)
    with pytest.raises(sqlite3.DatabaseError):
        rs._mark_due_one(conn, reminder, now=NOW, guard=guard)
    assert rm.get_reminder(conn, reminder["id"])["status"] == rm.STATUS_SCHEDULED
    assert _event_count(conn) == 0
    assert _delivery_count(conn) == 0


# --------------------------------------------------------------------------- #
# service configuration and lifecycle
# --------------------------------------------------------------------------- #

def test_service_config_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv(nds.ENABLED_ENV, raising=False)
    config = nds.load_config()
    assert config.enabled is False
    assert config.poll_seconds == 30.0
    assert config.shutdown_timeout_seconds == 35.0


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_service_config_parses_enabled_values(monkeypatch, value):
    monkeypatch.setenv(nds.ENABLED_ENV, value)
    assert nds.load_config().enabled is True


def test_service_rejects_unknown_channel():
    class UnknownChannel:
        name = "webhook"

        def send(self, delivery, event):
            raise AssertionError("real channel must not be invoked")

    with pytest.raises(ValueError, match="fake_test"):
        nds.NotificationDeliveryService(
            Path("unused.db"), enabled=True, channel=UnknownChannel(),
        )


def test_disabled_service_creates_no_loop_and_does_not_consume_pending_delivery(db_path):
    with _connect(db_path) as setup:
        reminder = _review_reminder(setup, status=rm.STATUS_DUE)
        nd.create_reminder_due_outbox(setup, reminder, now=datetime.now(UTC))
    channel = nd.FakeNotificationChannel()
    service = nds.NotificationDeliveryService(
        db_path, enabled=False, poll_seconds=0.02, channel=channel,
    )

    async def scenario():
        await service.start()
        await asyncio.sleep(0.08)
        await service.stop()

    asyncio.run(scenario())
    snapshot = service.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["loop_running"] is False
    assert channel.calls == []
    with _connect(db_path) as audit:
        row = audit.execute("SELECT status FROM reminder_notification_deliveries").fetchone()
        assert row["status"] == nd.STATUS_PENDING


def test_enabled_fake_service_consumes_pending_delivery_and_is_idempotent(db_path):
    with _connect(db_path) as setup:
        reminder = _review_reminder(setup, status=rm.STATUS_DUE)
        nd.create_reminder_due_outbox(setup, reminder, now=datetime.now(UTC))
    channel = nd.FakeNotificationChannel()
    service = nds.NotificationDeliveryService(
        db_path, enabled=True, poll_seconds=30, initial_delay=0, channel=channel,
        shutdown_timeout_seconds=2,
    )

    async def scenario():
        await service.start()
        await service.start()
        deadline = asyncio.get_running_loop().time() + 2
        while service.snapshot()["last_status"] != nd.STATUS_SUCCEEDED:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(service.snapshot())
            await asyncio.sleep(0.01)
        await service.stop()
        await service.stop()
        return service.snapshot()

    snapshot = asyncio.run(scenario())
    assert snapshot["state"] == nds.STATE_STOPPED
    assert snapshot["loop_running"] is False
    assert snapshot["last_result"] == nd.STATUS_SUCCEEDED
    assert len(channel.calls) == 1
    assert channel.calls[0]["event"]["payload"]["title"] == "每日复盘"
    with _connect(db_path) as audit:
        row = audit.execute("SELECT status,external_message_id FROM reminder_notification_deliveries").fetchone()
        assert row["status"] == nd.STATUS_SUCCEEDED
        assert row["external_message_id"].startswith("fake-test:1:1")


def test_service_retries_without_real_network_then_succeeds(db_path, monkeypatch):
    monkeypatch.setattr(nd, "backoff_delay_seconds", lambda attempt: 0)
    with _connect(db_path) as setup:
        reminder = _review_reminder(setup, status=rm.STATUS_DUE)
        nd.create_reminder_due_outbox(setup, reminder, now=datetime.now(UTC))
    channel = nd.FakeNotificationChannel(outcomes=[
        nd.DeliveryTransientError("temporary"),
        nd.DeliverySuccess("fake-after-retry"),
    ])
    service = nds.NotificationDeliveryService(
        db_path, enabled=True, poll_seconds=0.02, initial_delay=0, channel=channel,
        shutdown_timeout_seconds=2,
    )

    async def scenario():
        await service.start()
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            with _connect(db_path) as audit:
                row = audit.execute("SELECT status FROM reminder_notification_deliveries").fetchone()
            if row["status"] == nd.STATUS_SUCCEEDED:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(service.snapshot())
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(scenario())
    assert len(channel.calls) == 2
    assert channel.used_network is False


def test_service_recovers_stale_running_after_process_restart(db_path):
    with _connect(db_path) as setup:
        reminder = _review_reminder(setup, status=rm.STATUS_DUE)
        created = nd.create_reminder_due_outbox(setup, reminder, now=datetime.now(UTC) - timedelta(minutes=10))
        setup.execute(
            "UPDATE reminder_notification_deliveries SET status='running', attempt_count=1, "
            "claimed_at=?, locked_by=?, lock_expires_at=?, updated_at=? WHERE id=?",
            (rm.iso(datetime.now(UTC) - timedelta(minutes=10)), "dead-worker",
             rm.iso(datetime.now(UTC) - timedelta(minutes=5, seconds=1)),
             rm.iso(datetime.now(UTC) - timedelta(minutes=10)),
             created.delivery["id"]),
        )
    channel = nd.FakeNotificationChannel()
    service = nds.NotificationDeliveryService(
        db_path, enabled=True, poll_seconds=30, initial_delay=0, channel=channel,
        shutdown_timeout_seconds=2,
    )

    async def scenario():
        await service.start()
        deadline = asyncio.get_running_loop().time() + 2
        while service.snapshot()["last_status"] != nd.STATUS_SUCCEEDED:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(service.snapshot())
            await asyncio.sleep(0.01)
        await service.stop()

    asyncio.run(scenario())
    assert len(channel.calls) == 1
    with _connect(db_path) as audit:
        row = audit.execute("SELECT status FROM reminder_notification_deliveries").fetchone()
        assert row["status"] == nd.STATUS_SUCCEEDED


def test_shutdown_is_bounded_and_reports_active_delivery_without_hiding_timeout(db_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingFakeChannel(nd.FakeNotificationChannel):
        def send(self, delivery, event):
            entered.set()
            release.wait(2)
            return nd.DeliverySuccess("should-not-complete-before-timeout")

    with _connect(db_path) as setup:
        reminder = _review_reminder(setup, status=rm.STATUS_DUE)
        nd.create_reminder_due_outbox(setup, reminder, now=datetime.now(UTC))
    service = nds.NotificationDeliveryService(
        db_path, enabled=True, poll_seconds=30, initial_delay=0,
        channel=BlockingFakeChannel(), shutdown_timeout_seconds=0.05,
    )

    async def scenario():
        await service.start()
        if not await asyncio.to_thread(entered.wait, 1):
            raise AssertionError("delivery did not start")
        await service.stop()
        snapshot = service.snapshot()
        release.set()
        return snapshot

    snapshot = asyncio.run(scenario())
    assert snapshot["shutdown_timed_out"] is True
    assert snapshot["state"] == nds.STATE_STOPPED_ACTIVE_DELIVERY


def test_lifespan_invalid_notification_config_fails_before_database_init(tmp_path, monkeypatch):
    db_file = tmp_path / "invalid-notification-config.db"
    monkeypatch.setenv(nds.ENABLED_ENV, "maybe")
    monkeypatch.setattr("app.main.DB_PATH", db_file)

    with pytest.raises(ValueError, match="NOTIFICATION_DELIVERY_ENABLED"):
        with TestClient(app):
            pass
    assert not db_file.exists()


def test_lifespan_registers_default_disabled_notification_service_and_clears_on_shutdown(db_path):
    import app.main as main_module

    assert main_module.notification_delivery_instance is None
    with TestClient(app):
        service = main_module.notification_delivery_instance
        assert service is not None
        assert service.snapshot()["enabled"] is False
        assert service.snapshot()["loop_running"] is False
    assert main_module.notification_delivery_instance is None


def test_lifespan_can_enable_fake_notification_worker_without_real_channel(db_path, monkeypatch):
    monkeypatch.setenv(nds.ENABLED_ENV, "1")
    with TestClient(app):
        import app.main as main_module
        service = main_module.notification_delivery_instance
        assert service is not None
        snapshot = service.snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["channel"] == nd.CHANNEL_FAKE_TEST
        assert snapshot["loop_running"] is True
    assert main_module.notification_delivery_instance is None


def test_health_contains_default_disabled_notification_snapshot(db_path):
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["reminder_scheduler"]["enabled"] is False
    assert payload["notification_delivery"]["enabled"] is False
    assert payload["notification_delivery"]["state"] == "stopped"
