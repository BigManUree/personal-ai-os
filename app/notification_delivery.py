"""Reliable local notification outbox and fake delivery channel.

The module deliberately has no FastAPI route, background thread, network call,
or real notification integration. A reminder tick durably stores an event and a
per-channel delivery in the same SQLite transaction; a separate worker may later
claim that delivery and invoke a test-only fake channel.
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app import reminders as rm

EVENT_TYPE_REMINDER_DUE = "reminder_due"
EVENT_TYPES = frozenset({EVENT_TYPE_REMINDER_DUE})

SOURCE_TYPES = frozenset({"task", "recommendation", "review"})

CHANNEL_FAKE_TEST = "fake_test"
ALLOWED_CHANNELS = frozenset({CHANNEL_FAKE_TEST})
DEFAULT_RECIPIENT = "local"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_RETRY_WAITING = "retry_waiting"
STATUS_SUCCEEDED = "succeeded"
STATUS_DEAD_LETTER = "dead_letter"
STATUS_CANCELLED = "cancelled"
STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_RETRY_WAITING,
    STATUS_SUCCEEDED,
    STATUS_DEAD_LETTER,
    STATUS_CANCELLED,
})
CLAIMABLE_STATUSES = (STATUS_PENDING, STATUS_RETRY_WAITING)
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_DEAD_LETTER, STATUS_CANCELLED})

DEFAULT_MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
DEFAULT_LOCK_SECONDS = 300
MAX_ERROR_LENGTH = 500

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reminder_notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    source_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (event_type IN ('reminder_due')),
    CHECK (source_type IN ('task','recommendation','review')),
    CHECK (length(event_key) BETWEEN 1 AND 200),
    CHECK (length(payload_json) BETWEEN 2 AND 5000),
    CHECK (reminder_id > 0),
    CHECK (source_id IS NULL OR source_id > 0)
);
CREATE INDEX IF NOT EXISTS idx_reminder_notification_events_reminder
    ON reminder_notification_events(reminder_id, event_type);
CREATE TABLE IF NOT EXISTS reminder_notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES reminder_notification_events(id) ON DELETE CASCADE,
    delivery_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    scheduled_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    next_retry_at TEXT,
    claimed_at TEXT,
    locked_by TEXT,
    lock_expires_at TEXT,
    succeeded_at TEXT,
    dead_letter_at TEXT,
    cancelled_at TEXT,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    external_message_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (channel IN ('fake_test')),
    CHECK (status IN ('pending','running','retry_waiting','succeeded','dead_letter','cancelled')),
    CHECK (length(delivery_key) BETWEEN 1 AND 240),
    CHECK (length(recipient) BETWEEN 1 AND 120),
    CHECK (length(coalesce(locked_by,'')) <= 120),
    CHECK (length(coalesce(last_error_code,'')) <= 80),
    CHECK (length(last_error) <= 500),
    CHECK (attempt_count >= 0),
    CHECK (max_attempts BETWEEN 1 AND 10)
);
CREATE INDEX IF NOT EXISTS idx_reminder_notification_deliveries_due
    ON reminder_notification_deliveries(status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_reminder_notification_deliveries_event
    ON reminder_notification_deliveries(event_id);
CREATE INDEX IF NOT EXISTS idx_reminder_notification_deliveries_locked
    ON reminder_notification_deliveries(status, lock_expires_at)
    WHERE status='running';
"""

_BEARER_RE = re.compile(r"(?i)(Bearer\s+)(?:api[_-]?key\s+)?[A-Za-z0-9._~+/=-]{6,}")
_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret(?:[_-]?key)?|token|password)\s*[:=]\s*)"
    r"[A-Za-z0-9._~+/=-]{4,}"
)


class NotificationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class NotificationStateError(NotificationError):
    def __init__(self, message: str = "通知投递状态冲突") -> None:
        super().__init__("state_conflict", message, status_code=409)


class NotificationStorageError(NotificationError):
    def __init__(self, message: str = "通知投递存储错误") -> None:
        super().__init__("storage_conflict", message, status_code=409)


class NotificationConfigError(NotificationError):
    """Invalid channel/recipient configuration (was referenced but never defined,
    so any invalid channel would have crashed with NameError instead)."""

    def __init__(self, message: str = "通知投递配置错误") -> None:
        super().__init__("invalid_config", message, status_code=400)


class DeliveryResult:
    """Marker base class for explicit channel results."""


@dataclass(frozen=True)
class DeliverySuccess(DeliveryResult):
    external_message_id: str = ""


class DeliveryError(Exception):
    error_code = "transient"
    retriable = True

    def __init__(self, message: str = "") -> None:
        super().__init__(message)


class DeliveryTransientError(DeliveryError):
    error_code = "transient"
    retriable = True


class DeliveryTimeoutError(DeliveryTransientError):
    error_code = "timeout"


class DeliveryPermanentError(DeliveryError):
    error_code = "permanent"
    retriable = False


class DeliveryConfigError(DeliveryPermanentError):
    error_code = "config"


class DeliveryProtocolError(DeliveryPermanentError):
    error_code = "protocol"


class NotificationChannel(Protocol):
    name: str

    def send(self, delivery: dict[str, Any], event: dict[str, Any]) -> DeliveryResult:
        ...


@dataclass(frozen=True)
class OutboxResult:
    event: dict[str, Any]
    delivery: dict[str, Any]
    event_created: bool
    delivery_created: bool


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_delay_seconds(attempt_count: int) -> int:
    """Deterministic exponential backoff. Jitter is intentionally absent."""
    if not isinstance(attempt_count, int) or attempt_count < 1:
        raise ValueError("attempt_count 必须是正整数")
    return min(INITIAL_BACKOFF_SECONDS * (2 ** (attempt_count - 1)), MAX_BACKOFF_SECONDS)


def sanitize_error_message(error: BaseException | str | None) -> str:
    text = str(error or "").strip().replace("\x00", "")
    text = _BEARER_RE.sub(r"\1***", text)
    text = _SECRET_RE.sub(r"\1***", text)
    return text[:MAX_ERROR_LENGTH]


def default_worker_name() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"[:120]


def reminder_due_event_key(reminder: dict[str, Any] | sqlite3.Row) -> str:
    """Stable logical-occurrence key.

    A scheduled reminder uses scheduled_at. Each snooze occurrence uses the new
    snoozed_until, so retrying the same tick is idempotent but later snoozes are
    not incorrectly deduplicated.
    """
    occurrence = reminder["snoozed_until"] or reminder["scheduled_at"]
    if not occurrence:
        raise NotificationError("invalid_reminder", "提醒缺少 scheduled_at")
    return f"reminder.due.v1:{int(reminder['id'])}:{occurrence}"


def delivery_key_for(event_key: str, channel: str = CHANNEL_FAKE_TEST,
                     recipient: str = DEFAULT_RECIPIENT) -> str:
    if channel not in ALLOWED_CHANNELS:
        raise NotificationConfigError("本阶段只允许 fake_test 测试渠道")
    recipient = (recipient or DEFAULT_RECIPIENT).strip()
    return f"{event_key}:{channel}:{recipient}"


def build_reminder_due_payload(reminder: dict[str, Any] | sqlite3.Row, *,
                               now: datetime | None = None) -> dict[str, Any]:
    return {
        "reminder_id": int(reminder["id"]),
        "title": str(reminder["title"]),
        "content": str(reminder["content"]),
        "priority": str(reminder["priority"]),
        "source_type": str(reminder["source_type"]),
        "source_id": reminder["source_id"],
        "rule_code": str(reminder["rule_code"]),
        "scheduled_at": reminder["scheduled_at"],
        "snoozed_until": reminder["snoozed_until"],
        "due_at": reminder["due_at"],
        "event_time": rm.iso(now or utc_now()),
    }


def _require_fk(conn: sqlite3.Connection) -> None:
    if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise NotificationError(
            "database_configuration", "SQLite 外键约束未启用", status_code=500,
        )


def _row_by_id(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise NotificationError("not_found", f"{table} 记录不存在", status_code=404)
    return row


def get_event(conn: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    return dict(_row_by_id(conn, "reminder_notification_events", event_id))


def get_delivery(conn: sqlite3.Connection, delivery_id: int) -> dict[str, Any]:
    return dict(_row_by_id(conn, "reminder_notification_deliveries", delivery_id))


def create_reminder_due_outbox(
    conn: sqlite3.Connection,
    reminder: dict[str, Any] | sqlite3.Row,
    *,
    now: datetime | None = None,
    channel: str = CHANNEL_FAKE_TEST,
    recipient: str = DEFAULT_RECIPIENT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> OutboxResult:
    """Create event and fake-channel delivery.

    The caller owns the transaction. In reminder scheduling this runs inside the
    same guarded SQLite transaction as the scheduled/snoozed -> due update.
    """
    _require_fk(conn)
    if str(reminder["status"]) != rm.STATUS_DUE:
        raise NotificationStateError("只有 due 状态提醒可以创建到期通知事件")
    if not 1 <= int(max_attempts) <= 10:
        raise ValueError("max_attempts 必须在 1..10 之间")
    current = utc_now() if now is None else now
    ts = rm.iso(current)
    event_key = reminder_due_event_key(reminder)
    payload_json = json.dumps(
        build_reminder_due_payload(reminder, now=current),
        ensure_ascii=False, separators=(",", ":"),
    )

    event_row = conn.execute(
        "INSERT INTO reminder_notification_events("
        "event_key,event_type,reminder_id,source_type,source_id,payload_json,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(event_key) DO NOTHING RETURNING *",
        (
            event_key, EVENT_TYPE_REMINDER_DUE, int(reminder["id"]),
            str(reminder["source_type"]), reminder["source_id"], payload_json, ts, ts,
        ),
    ).fetchone()
    event_created = event_row is not None
    if event_row is None:
        event_row = conn.execute(
            "SELECT * FROM reminder_notification_events WHERE event_key=?", (event_key,),
        ).fetchone()
    if event_row is None:  # defensive; unique conflicts should always leave a row
        raise NotificationStorageError("通知事件创建后无法读取")

    dkey = delivery_key_for(event_key, channel=channel, recipient=recipient)
    delivery_row = conn.execute(
        "INSERT INTO reminder_notification_deliveries("
        "event_id,delivery_key,channel,recipient,status,attempt_count,max_attempts,"
        "scheduled_at,available_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(delivery_key) DO NOTHING RETURNING *",
        (
            int(event_row["id"]), dkey, channel, recipient, STATUS_PENDING, 0,
            int(max_attempts), ts, ts, ts, ts,
        ),
    ).fetchone()
    delivery_created = delivery_row is not None
    if delivery_row is None:
        delivery_row = conn.execute(
            "SELECT * FROM reminder_notification_deliveries WHERE delivery_key=?", (dkey,),
        ).fetchone()
    if delivery_row is None:
        raise NotificationStorageError("通知投递记录创建后无法读取")
    return OutboxResult(
        event=dict(event_row), delivery=dict(delivery_row),
        event_created=event_created, delivery_created=delivery_created,
    )


def claim_next_delivery(
    conn: sqlite3.Connection,
    worker: str,
    *,
    now: datetime | None = None,
    lock_seconds: int = DEFAULT_LOCK_SECONDS,
    channel: str = CHANNEL_FAKE_TEST,
) -> dict[str, Any] | None:
    """Atomically claim one due row using a database-level conditional UPDATE."""
    _require_fk(conn)
    if channel not in ALLOWED_CHANNELS:
        raise NotificationConfigError("本阶段只允许 fake_test 测试渠道")
    current = now or utc_now()
    ts = rm.iso(current)
    lock_expires = rm.iso(current + timedelta(seconds=lock_seconds))
    safe_worker = (worker or "").strip()[:120]
    if not safe_worker:
        raise ValueError("worker 不能为空")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "UPDATE reminder_notification_deliveries SET "
            "status=?, attempt_count=attempt_count+1, claimed_at=?, locked_by=?, "
            "lock_expires_at=?, last_error_code='', last_error='', updated_at=? "
            "WHERE id=("
            "SELECT id FROM reminder_notification_deliveries "
            "WHERE channel=? AND status IN (?,?) AND available_at<=? "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY available_at,id LIMIT 1) RETURNING *",
            (STATUS_RUNNING, ts, safe_worker, lock_expires, ts,
             channel, STATUS_PENDING, STATUS_RETRY_WAITING, ts, ts),
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return dict(row) if row is not None else None


def _require_running_owner(conn: sqlite3.Connection, delivery_id: int, worker: str) -> sqlite3.Row:
    row = _row_by_id(conn, "reminder_notification_deliveries", delivery_id)
    if row["status"] != STATUS_RUNNING:
        raise NotificationStateError(f"投递记录当前状态为 {row['status']}，不能更新")
    if row["locked_by"] != worker:
        raise NotificationStateError("投递记录已被其他执行者锁定")
    return row


def complete_delivery(
    conn: sqlite3.Connection,
    delivery_id: int,
    worker: str,
    result: DeliverySuccess,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    ts = rm.iso(current)
    external_id = str(result.external_message_id or "")[:300]
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_running_owner(conn, delivery_id, worker)
        cur = conn.execute(
            "UPDATE reminder_notification_deliveries SET status=?, external_message_id=?, "
            "succeeded_at=?, next_retry_at=NULL, claimed_at=claimed_at, locked_by=NULL, "
            "lock_expires_at=NULL, last_error_code='', last_error='', updated_at=? "
            "WHERE id=? AND status=? AND locked_by=?",
            (STATUS_SUCCEEDED, external_id, ts, ts, delivery_id, STATUS_RUNNING, worker),
        )
        if cur.rowcount != 1:
            raise NotificationStateError("无法完成投递记录")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_delivery(conn, delivery_id)


def record_delivery_failure(
    conn: sqlite3.Connection,
    delivery_id: int,
    worker: str,
    error: DeliveryError,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    ts = rm.iso(current)
    safe_error = sanitize_error_message(error)
    code = str(getattr(error, "error_code", "transient"))[:80]
    retriable = bool(getattr(error, "retriable", True))

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _require_running_owner(conn, delivery_id, worker)
        attempt = int(row["attempt_count"])
        max_attempts = int(row["max_attempts"])
        terminal = (not retriable) or attempt >= max_attempts
        if terminal:
            status = STATUS_DEAD_LETTER
            next_retry = None
            dead_letter_at = ts
        else:
            status = STATUS_RETRY_WAITING
            next_retry = rm.iso(current + timedelta(seconds=backoff_delay_seconds(attempt)))
            dead_letter_at = None
        cur = conn.execute(
            "UPDATE reminder_notification_deliveries SET status=?, next_retry_at=?, "
            "dead_letter_at=?, locked_by=NULL, lock_expires_at=NULL, last_error_code=?, "
            "last_error=?, updated_at=? WHERE id=? AND status=? AND locked_by=?",
            (status, next_retry, dead_letter_at, code, safe_error, ts,
             delivery_id, STATUS_RUNNING, worker),
        )
        if cur.rowcount != 1:
            raise NotificationStateError("无法记录投递失败")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_delivery(conn, delivery_id)


def recover_stale_deliveries(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    lock_timeout_seconds: int = DEFAULT_LOCK_SECONDS,
) -> int:
    """Return stale running rows to retry_waiting, or dead-letter exhausted ones."""
    _require_fk(conn)
    current = now or utc_now()
    cutoff = rm.iso(current - timedelta(seconds=lock_timeout_seconds))
    ts = rm.iso(current)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE reminder_notification_deliveries SET "
            "status=CASE WHEN attempt_count>=max_attempts THEN ? ELSE ? END, "
            "next_retry_at=CASE WHEN attempt_count>=max_attempts THEN NULL ELSE ? END, "
            "dead_letter_at=CASE WHEN attempt_count>=max_attempts THEN ? ELSE NULL END, "
            "locked_by=NULL, lock_expires_at=NULL, last_error_code='stale_running', "
            "last_error='投递执行器崩溃或关闭时仍处于 running；恢复时可能已外部发送', updated_at=? "
            "WHERE status=? AND lock_expires_at IS NOT NULL AND lock_expires_at<?",
            (STATUS_DEAD_LETTER, STATUS_RETRY_WAITING, ts, ts, ts,
             STATUS_RUNNING, cutoff),
        )
        count = int(cur.rowcount)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return count


class FakeNotificationChannel:
    """Deterministic in-memory test channel. It never performs I/O."""

    name = CHANNEL_FAKE_TEST

    def __init__(self, outcomes: list[DeliveryResult | DeliveryError] | None = None,
                 *, default_success: bool = True) -> None:
        self._outcomes = list(outcomes or [])
        self._default_success = default_success
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.used_network = False

    def send(self, delivery: dict[str, Any], event: dict[str, Any]) -> DeliveryResult:
        with self._lock:
            self.calls.append({"delivery": dict(delivery), "event": dict(event)})
            if self._outcomes:
                outcome = self._outcomes.pop(0)
            elif self._default_success:
                outcome = DeliverySuccess(
                    f"fake-test:{event['id']}:{delivery['attempt_count']}"
                )
            else:
                raise DeliveryProtocolError("FakeNotificationChannel 未配置后续结果")
        if isinstance(outcome, DeliveryError):
            raise outcome
        if not isinstance(outcome, DeliverySuccess):
            raise DeliveryProtocolError("通知渠道返回了非法结果类型")
        return outcome


def process_delivery_batch(
    db_path: Any,
    channel: NotificationChannel,
    *,
    worker: str | None = None,
    now: datetime | None = None,
    lock_timeout_seconds: int = DEFAULT_LOCK_SECONDS,
) -> dict[str, Any]:
    """Open an isolated connection and process at most one fake delivery."""
    if getattr(channel, "name", "") != CHANNEL_FAKE_TEST:
        raise NotificationConfigError("本阶段只允许 fake_test 测试渠道")
    owner = (worker or default_worker_name())[:120]
    current = now or utc_now()
    connection = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        recovered = recover_stale_deliveries(
            connection, now=current, lock_timeout_seconds=lock_timeout_seconds,
        )
        delivery = claim_next_delivery(
            connection, owner, now=current, lock_seconds=lock_timeout_seconds,
        )
        if delivery is None:
            return {
                "status": "idle", "processed": 0, "recovered": recovered,
                "result_status": "idle", "external_message_id": "",
            }
        event = get_event(connection, int(delivery["event_id"]))
        try:
            event_payload = json.loads(event.get("payload_json") or "{}")
            channel_event = dict(event)
            channel_event["payload"] = event_payload
            result = channel.send(delivery, channel_event)
            if not isinstance(result, DeliverySuccess):
                raise DeliveryProtocolError("通知渠道返回了非法结果类型")
            completed = complete_delivery(
                connection, int(delivery["id"]), owner, result, now=current,
            )
            return {
                "status": STATUS_SUCCEEDED, "processed": 1, "recovered": recovered,
                "result_status": STATUS_SUCCEEDED,
                "external_message_id": completed["external_message_id"],
            }
        except DeliveryError as exc:
            failed = record_delivery_failure(
                connection, int(delivery["id"]), owner, exc, now=current,
            )
            return {
                "status": str(failed["status"]), "processed": 1, "recovered": recovered,
                "result_status": str(failed["status"]), "external_message_id": "",
                "last_error_code": failed["last_error_code"],
            }
    finally:
        connection.close()
