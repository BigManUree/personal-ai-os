"""SQLite-backed cross-process lease for the reminder scheduler.

The lease service owns acquire/heartbeat/release semantics. Scheduler ticks in
``reminder_scheduler.run_tick`` use these identities with guarded writes; this
module itself contains no FastAPI lifecycle hook or background loop.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

SCOPE_GLOBAL = "global"
STATUS_ACQUIRED = "acquired"
STATUS_BUSY = "busy"
STATUS_LOST = "lost"
STATUS_DATABASE_ERROR = "database_error"
DEFAULT_TTL_SECONDS = 30.0
DEFAULT_BUSY_TIMEOUT_SECONDS = 2.0
MAX_OWNER_TOKEN = 120
MAX_LEASE_TOKEN = 80
MAX_HOSTNAME = 120


class LeaseError(ValueError):
    """Invalid lease API usage."""


@dataclass(frozen=True)
class Lease:
    scope: str
    owner_token: str
    lease_token: str
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    released_at: str | None
    pid: int | None
    hostname: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Lease:
        return cls(
            scope=str(row["scope"]),
            owner_token=str(row["owner_token"]),
            lease_token=str(row["lease_token"]),
            fencing_token=int(row["fencing_token"]),
            acquired_at=str(row["acquired_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["expires_at"]),
            released_at=row["released_at"],
            pid=row["pid"],
            hostname=str(row["hostname"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "owner_token": self.owner_token,
            "lease_token": self.lease_token,
            "fencing_token": self.fencing_token,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "expires_at": self.expires_at,
            "released_at": self.released_at,
            "pid": self.pid,
            "hostname": self.hostname,
        }


@dataclass(frozen=True)
class LeaseResult:
    status: str
    lease: Lease | None = None
    error_code: str = ""
    message: str = ""
    # True when the underlying SQLite error is a documented transient condition
    # (BUSY/LOCKED/READONLY). A caller may retry on a fresh connection; a retry
    # is only offered while this process holds no lease, i.e. before COMMIT.
    retryable: bool = False

    @property
    def acquired(self) -> bool:
        return self.status == STATUS_ACQUIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lease": self.lease.to_dict() if self.lease else None,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
        }


# SQLITE_READONLY (8) and its extended codes. On Windows a WAL database can
# transiently report SQLITE_READONLY to a fresh connection whose first write
# races another connection opening/closing the same file; an immediate retry on
# a new connection succeeds. Busy/locked are deliberately excluded: those are
# already covered by busy_timeout, and retrying them would multiply the wait.
RETRYABLE_SQLITE_CODES = frozenset({8})
RETRYABLE_SQLITE_MESSAGES = (
    "attempt to write a readonly database",
    "database is read-only",
)


def is_retryable_sqlite_error(exc: BaseException | None) -> bool:
    """Classify an exception as an immediately retryable SQLite condition."""
    if not isinstance(exc, sqlite3.Error):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in RETRYABLE_SQLITE_CODES:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RETRYABLE_SQLITE_MESSAGES)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if not isinstance(value, datetime) or isinstance(value, bool):
        raise LeaseError("时间必须是 datetime")
    if value.tzinfo is None:
        raise LeaseError("时间必须带时区信息")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str, *, field_name: str = "time") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LeaseError(f"{field_name} 必须是 ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LeaseError(f"{field_name} 必须是有效的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise LeaseError(f"{field_name} 必须带时区信息")
    return parsed.astimezone(timezone.utc)


def default_owner_token(pid: int | None = None, hostname: str | None = None) -> str:
    pid_value = os.getpid() if pid is None else pid
    host = socket.gethostname()[:60] if hostname is None else hostname[:60]
    return f"pid-{pid_value}-{host}-{uuid.uuid4().hex}"


def _validate_ttl(ttl_seconds: float) -> float:
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
        raise LeaseError("TTL 必须是秒数")
    ttl = float(ttl_seconds)
    if not 1.0 <= ttl <= 3600.0:
        raise LeaseError("TTL 必须在 1..3600 秒之间")
    return ttl


def _validate_identity(owner_token: str, lease_token: str, fencing_token: int) -> None:
    if not isinstance(owner_token, str) or not (1 <= len(owner_token) <= MAX_OWNER_TOKEN):
        raise LeaseError("owner_token 不合法")
    if not isinstance(lease_token, str) or not (1 <= len(lease_token) <= MAX_LEASE_TOKEN):
        raise LeaseError("lease_token 不合法")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 1:
        raise LeaseError("fencing_token 必须是正整数")


def configure_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
    if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool) or busy_timeout_ms < 0:
        raise LeaseError("busy timeout 不合法")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")


def init_lease_db(conn: sqlite3.Connection) -> None:
    """Create the singleton lease table and its initial row idempotently."""
    in_transaction = bool(getattr(conn, "in_transaction", False))
    if not in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        _init_lease_schema(conn)
        if not in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if not in_transaction:
            conn.execute("ROLLBACK")
        raise


def _init_lease_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_scheduler_leases (
            scope TEXT PRIMARY KEY CHECK (scope='global'),
            owner_token TEXT NOT NULL DEFAULT '',
            lease_token TEXT NOT NULL DEFAULT '',
            fencing_token INTEGER NOT NULL DEFAULT 0,
            acquired_at TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL DEFAULT '',
            expires_at TEXT NOT NULL DEFAULT '',
            released_at TEXT,
            pid INTEGER,
            hostname TEXT NOT NULL DEFAULT '',
            CHECK (fencing_token >= 0),
            CHECK (length(owner_token) <= 120),
            CHECK (length(lease_token) <= 80),
            CHECK (length(hostname) <= 120)
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO reminder_scheduler_leases(scope) VALUES (?)",
        (SCOPE_GLOBAL,),
    )


def _lease_is_free(row: sqlite3.Row, now: datetime) -> bool:
    if not row["owner_token"] or not row["lease_token"] or int(row["fencing_token"]) == 0:
        return True
    # A released lease is free regardless of the caller's clock. This keeps lease
    # availability independent of whether "now" is the wall clock or an injected
    # logical time (tests, backfill, replay); the write guard already treats
    # released_at as "not held" in _begin_guarded_write/_commit_guarded_write.
    if row["released_at"]:
        return True
    return parse_iso(str(row["expires_at"]), field_name="expires_at") <= now


def _row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM reminder_scheduler_leases WHERE scope=?", (SCOPE_GLOBAL,)
    ).fetchone()
    if row is None:  # Defensive: init_lease_db inserts the singleton.
        raise sqlite3.DatabaseError("调度租约单例不存在")
    return row


def acquire_lease(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    owner_token: str | None = None,
    pid: int | None = None,
    hostname: str | None = None,
    busy_timeout_ms: int = int(DEFAULT_BUSY_TIMEOUT_SECONDS * 1000),
) -> LeaseResult:
    """Atomically acquire or take over an expired global lease.

    Expiry is half-open: ``expires_at == now`` means the old lease has expired.
    """
    ttl = _validate_ttl(ttl_seconds)
    current = utc_now() if now is None else now
    ts = iso(current)
    expires = iso(current + timedelta(seconds=ttl))
    owner = owner_token or default_owner_token(pid=pid, hostname=hostname)
    host = (hostname or socket.gethostname())[:MAX_HOSTNAME]
    process_id = os.getpid() if pid is None else pid
    if not isinstance(owner, str) or not (1 <= len(owner) <= MAX_OWNER_TOKEN):
        raise LeaseError("owner_token 不合法")
    if not isinstance(host, str) or not (1 <= len(host) <= MAX_HOSTNAME):
        raise LeaseError("hostname 不合法")
    if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool) or busy_timeout_ms < 0:
        raise LeaseError("busy timeout 不合法")
    try:
        configure_connection(conn, busy_timeout_ms=busy_timeout_ms)
        init_lease_db(conn)
        lease_token = uuid.uuid4().hex
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn)
        if not _lease_is_free(row, current):
            conn.execute("ROLLBACK")
            existing = Lease.from_row(_row(conn))
            return LeaseResult(STATUS_BUSY, lease=existing, error_code="lease_busy",
                               message="另一个调度持有者仍有效")
        cur = conn.execute(
            """
            UPDATE reminder_scheduler_leases
            SET owner_token=?, lease_token=?, fencing_token=fencing_token+1,
                acquired_at=?, heartbeat_at=?, expires_at=?, released_at=NULL,
                pid=?, hostname=?
            WHERE scope=? AND (
                owner_token='' OR lease_token='' OR fencing_token=0
                OR released_at IS NOT NULL OR expires_at<=?
            )
            """,
            (owner, lease_token, ts, ts, expires, process_id, host, SCOPE_GLOBAL, ts),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            existing = Lease.from_row(_row(conn))
            return LeaseResult(STATUS_BUSY, lease=existing, error_code="lease_busy",
                               message="另一个调度持有者仍有效")
        conn.execute("COMMIT")
        return LeaseResult(STATUS_ACQUIRED, lease=Lease.from_row(_row(conn)))
    except (sqlite3.Error, LeaseError, OSError) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        code = "database_error" if isinstance(exc, sqlite3.Error) else "invalid_lease_request"
        return LeaseResult(
            STATUS_DATABASE_ERROR, error_code=code, message=str(exc)[:300],
            retryable=is_retryable_sqlite_error(exc),
        )


def _conditional_result(conn: sqlite3.Connection, updated: bool) -> LeaseResult:
    if updated:
        return LeaseResult(STATUS_ACQUIRED, lease=Lease.from_row(_row(conn)))
    current = _row(conn)
    # If no active owner exists, report lost rather than busy: caller identity is invalid.
    return LeaseResult(STATUS_LOST, lease=Lease.from_row(current), error_code="lease_lost",
                       message="调度租约不属于该持有者")


def heartbeat_lease(
    conn: sqlite3.Connection,
    *,
    owner_token: str,
    lease_token: str,
    fencing_token: int,
    now: datetime,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    busy_timeout_ms: int = int(DEFAULT_BUSY_TIMEOUT_SECONDS * 1000),
) -> LeaseResult:
    _validate_identity(owner_token, lease_token, fencing_token)
    ttl = _validate_ttl(ttl_seconds)
    ts = iso(now)
    expires = iso(now + timedelta(seconds=ttl))
    if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool) or busy_timeout_ms < 0:
        raise LeaseError("busy timeout 不合法")
    try:
        configure_connection(conn, busy_timeout_ms=busy_timeout_ms)
        init_lease_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE reminder_scheduler_leases
            SET heartbeat_at=?, expires_at=?
            WHERE scope=? AND owner_token=? AND lease_token=? AND fencing_token=?
              AND expires_at>?
            """,
            (ts, expires, SCOPE_GLOBAL, owner_token, lease_token, fencing_token, ts),
        )
        updated = cur.rowcount == 1
        if updated:
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
        return _conditional_result(conn, updated)
    except (sqlite3.Error, LeaseError, OSError) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        code = "database_error" if isinstance(exc, sqlite3.Error) else "invalid_lease_request"
        return LeaseResult(
            STATUS_DATABASE_ERROR, error_code=code, message=str(exc)[:300],
            retryable=is_retryable_sqlite_error(exc),
        )


def release_lease(
    conn: sqlite3.Connection,
    *,
    owner_token: str,
    lease_token: str,
    fencing_token: int,
    now: datetime,
    busy_timeout_ms: int = int(DEFAULT_BUSY_TIMEOUT_SECONDS * 1000),
) -> LeaseResult:
    _validate_identity(owner_token, lease_token, fencing_token)
    ts = iso(now)
    if not isinstance(busy_timeout_ms, int) or isinstance(busy_timeout_ms, bool) or busy_timeout_ms < 0:
        raise LeaseError("busy timeout 不合法")
    try:
        configure_connection(conn, busy_timeout_ms=busy_timeout_ms)
        init_lease_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE reminder_scheduler_leases
            SET released_at=?, expires_at=?
            WHERE scope=? AND owner_token=? AND lease_token=? AND fencing_token=?
              AND released_at IS NULL
            """,
            (ts, ts, SCOPE_GLOBAL, owner_token, lease_token, fencing_token),
        )
        updated = cur.rowcount == 1
        if updated:
            conn.execute("COMMIT")
        else:
            conn.execute("ROLLBACK")
        return _conditional_result(conn, updated)
    except (sqlite3.Error, LeaseError, OSError) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        code = "database_error" if isinstance(exc, sqlite3.Error) else "invalid_lease_request"
        return LeaseResult(
            STATUS_DATABASE_ERROR, error_code=code, message=str(exc)[:300],
            retryable=is_retryable_sqlite_error(exc),
        )


def current_lease(conn: sqlite3.Connection) -> Lease | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM reminder_scheduler_leases WHERE scope=?", (SCOPE_GLOBAL,)
    ).fetchone()
    return Lease.from_row(row) if row else None
