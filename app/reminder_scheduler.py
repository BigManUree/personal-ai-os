"""Manual, deterministic local reminder scheduler core for Personal AI OS 1.3.5-C-B.

This module has no FastAPI route, startup hook, background thread, LLM call,
or external notification. Callers explicitly pass the reference instant.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from typing import Any

from app import notification_delivery as nd
from app import reminder_scheduler_lock as lease_svc
from app import reminders as rm
from app.analytics import get_timezone

logger = logging.getLogger("personal_ai_os.reminder_scheduler")

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_RECOVERY = "recovery"
TRIGGERS = frozenset({TRIGGER_MANUAL, TRIGGER_SCHEDULED, TRIGGER_RECOVERY})

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL_FAILED = "partial_failed"
STATUS_FAILED = "failed"
STATUS_ABANDONED = "abandoned"
RUN_STATUSES = frozenset({
    STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_PARTIAL_FAILED, STATUS_FAILED, STATUS_ABANDONED
})

STALE_RUN_MINUTES = 5
MAX_RUN_ERROR = 500
COUNT_KEYS = (
    "processed_count", "created_count", "updated_count", "skipped_count", "due_count",
    "expired_count", "cancelled_count", "recovered_run_count", "error_count",
)
_lock = threading.Lock()
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*\S+")
LEASE_TTL_SECONDS = 30.0
LEASE_BUSY_TIMEOUT_MS = 1000


class SchedulerError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LeaseLostError(SchedulerError):
    def __init__(self, message: str = "调度租约已失效或被其他进程接管") -> None:
        super().__init__("lease_lost", message, status_code=409)


class LeaseBusyError(SchedulerError):
    def __init__(self, message: str = "另一个调度进程正在执行", result: dict[str, Any] | None = None) -> None:
        super().__init__("lease_busy", message, status_code=409)
        self.result = result or {"status": "lease_busy"}


class LeaseDatabaseError(SchedulerError):
    def __init__(self, message: str = "调度租约数据库错误", *,
                 retryable: bool = False) -> None:
        super().__init__("lease_database_error", message, status_code=503)
        # Only acquisition failures can be retried safely: nothing was committed,
        # so the process holds no lease and no run row for this key.
        self.retryable = retryable


class SchedulerStopped(SchedulerError):
    """Raised at a safe boundary when shutdown asks a long tick to stop."""

    def __init__(self, message: str = "调度器正在关闭，已在安全边界停止当前 tick") -> None:
        super().__init__("scheduler_stopped", message, status_code=503)


def sanitize_error(error: BaseException | str | None) -> str:
    text = str(error or "").strip()
    text = _SECRET_RE.sub(lambda m: m.group(1) + "=***", text)
    # Never expose local absolute paths or raw SQL in run summaries.
    text = text.replace("\\", "/").split("\n", 1)[0]
    if ":" in text and ("/" in text or "INSERT" in text.upper() or "UPDATE" in text.upper()):
        text = "调度过程中发生数据处理错误"
    return text[:MAX_RUN_ERROR]


def _require_now(now: datetime) -> datetime:
    if not isinstance(now, datetime) or isinstance(now, bool) or now.tzinfo is None:
        raise SchedulerError("invalid_time", "now 必须是带时区信息的 datetime")
    return now.astimezone(rm.timezone.utc)


def _validate_trigger(trigger_type: str) -> str:
    value = (trigger_type or "").strip().lower()
    if value not in TRIGGERS:
        raise SchedulerError("invalid_trigger", "非法调度触发类型")
    return value


def quiet_until(local_now: datetime, settings: dict[str, Any]) -> datetime | None:
    """Return local quiet-period end when local_now is in the quiet interval.

    Interval semantics are [start, end) for a same-day interval. Cross-midnight
    intervals are [start, 24:00) union [00:00, end).
    """
    if not settings.get("quiet_hours_enabled"):
        return None
    start_h, start_m = map(int, str(settings["quiet_start"]).split(":"))
    end_h, end_m = map(int, str(settings["quiet_end"]).split(":"))
    start = time(start_h, start_m)
    end = time(end_h, end_m)
    current = local_now.timetz().replace(tzinfo=None)
    tzinfo = local_now.tzinfo
    today_end = datetime.combine(local_now.date(), end, tzinfo)
    if start < end:
        if start <= current < end:
            return today_end
        return None
    if current >= start:  # [start, midnight)
        return today_end + timedelta(days=1)
    if current < end:  # [midnight, end)
        return today_end
    return None


def _new_run_key(now: datetime) -> str:
    return f"manual:{rm.iso(now)}:{uuid.uuid4().hex[:12]}"


def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["run_id"] = data.get("id")
    return data


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM reminder_scheduler_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise SchedulerError("not_found", "调度运行记录不存在", status_code=404)
    return _run_dict(row)


def _find_run_by_key(conn: sqlite3.Connection, run_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reminder_scheduler_runs WHERE run_key=?", (run_key,)).fetchone()


def recover_stale_runs(conn: sqlite3.Connection, *, now: datetime,
                        guard: dict[str, Any] | None = None) -> int:
    cutoff = rm.iso(now - timedelta(minutes=STALE_RUN_MINUTES))
    ts = rm.iso(now)
    if guard is not None:
        _begin_guarded_write(conn, guard, now)
    cur = conn.execute(
        "UPDATE reminder_scheduler_runs SET status='abandoned', finished_at=?, last_error=? "
        "WHERE status='running' AND started_at<?",
        (ts, "上次调度进程中断，已标记为 abandoned", cutoff),
    )
    affected = int(cur.rowcount or 0)
    if guard is not None:
        _commit_guarded_write(conn, guard, now)
    else:
        conn.commit()
    return affected


def _lease_result_dict(result: lease_svc.LeaseResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "error_code": result.error_code,
        "message": result.message,
        "lease": result.lease.to_dict() if result.lease else None,
    }


def _acquire_scheduler_lease(conn: sqlite3.Connection, *, now: datetime,
                             owner_token: str) -> lease_svc.Lease:
    result = lease_svc.acquire_lease(
        conn, now=now, ttl_seconds=LEASE_TTL_SECONDS, owner_token=owner_token or None,
        busy_timeout_ms=LEASE_BUSY_TIMEOUT_MS,
    )
    if result.status == lease_svc.STATUS_ACQUIRED and result.lease is not None:
        return result.lease
    if result.status == lease_svc.STATUS_BUSY:
        raise LeaseBusyError(result=result.to_dict())
    raise LeaseDatabaseError(result.message or "无法获取调度租约")


def _guard_from_lease(lease: lease_svc.Lease) -> dict[str, Any]:
    return {
        "owner_token": lease.owner_token,
        "lease_token": lease.lease_token,
        "fencing_token": lease.fencing_token,
    }


def _lease_is_current(conn: sqlite3.Connection, guard: dict[str, Any], now: datetime,
                      *, ttl_seconds: float = LEASE_TTL_SECONDS) -> bool:
    result = lease_svc.heartbeat_lease(
        conn, owner_token=guard["owner_token"], lease_token=guard["lease_token"],
        fencing_token=guard["fencing_token"], now=now, ttl_seconds=ttl_seconds,
        busy_timeout_ms=LEASE_BUSY_TIMEOUT_MS,
    )
    if result.status == lease_svc.STATUS_ACQUIRED:
        logger.debug(
            "reminder scheduler lease heartbeat renewed",
            extra={"fencing_token": guard["fencing_token"]},
        )
        return True
    if result.status == lease_svc.STATUS_LOST:
        logger.warning(
            "reminder scheduler lease lost during heartbeat",
            extra={"fencing_token": guard["fencing_token"], "error_code": result.error_code},
        )
        return False
    logger.error(
        "reminder scheduler lease heartbeat failed",
        extra={"fencing_token": guard["fencing_token"], "error_code": result.error_code},
    )
    raise LeaseDatabaseError(result.message or "调度租约校验失败")


def _begin_guarded_write(conn: sqlite3.Connection, guard: dict[str, Any], now: datetime) -> None:
    """Start a write txn and linearize lease ownership before the protected mutation."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT owner_token, lease_token, fencing_token, expires_at, released_at "
        "FROM reminder_scheduler_leases WHERE scope=?",
        (lease_svc.SCOPE_GLOBAL,),
    ).fetchone()
    if row is None:
        raise LeaseLostError("调度租约不存在")
    if row["released_at"] is not None:
        raise LeaseLostError("调度租约已释放")
    if (row["owner_token"] != guard["owner_token"]
            or row["lease_token"] != guard["lease_token"]
            or int(row["fencing_token"]) != int(guard["fencing_token"])):
        raise LeaseLostError("调度租约已被新持有者接管")
    if rm.parse_iso(str(row["expires_at"]), field_name="expires_at") <= now:
        raise LeaseLostError("调度租约已过期")


def _commit_guarded_write(conn: sqlite3.Connection, guard: dict[str, Any], now: datetime) -> None:
    # Re-check inside the same write transaction immediately before COMMIT. SQLite serializes
    # writers; a newer owner cannot also have committed while this transaction is held.
    row = conn.execute(
        "SELECT owner_token, lease_token, fencing_token, expires_at, released_at "
        "FROM reminder_scheduler_leases WHERE scope=?",
        (lease_svc.SCOPE_GLOBAL,),
    ).fetchone()
    if (row is None or row["released_at"] is not None
            or row["owner_token"] != guard["owner_token"]
            or row["lease_token"] != guard["lease_token"]
            or int(row["fencing_token"]) != int(guard["fencing_token"])
            or rm.parse_iso(str(row["expires_at"]), field_name="expires_at") <= now):
        conn.execute("ROLLBACK")
        raise LeaseLostError("提交前检测到调度租约已失效")
    conn.execute("COMMIT")


@contextmanager
def guarded_write(conn: sqlite3.Connection, guard: dict[str, Any], now: datetime):
    _begin_guarded_write(conn, guard, now)
    try:
        yield
        _commit_guarded_write(conn, guard, now)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _heartbeat_if_needed(conn: sqlite3.Connection, guard: dict[str, Any], now: datetime,
                         last_heartbeat: datetime, *,
                         ttl_seconds: float = LEASE_TTL_SECONDS,
                         interval_seconds: float = 10.0) -> datetime:
    # Refresh at coarse stage/candidate-batch boundaries. There is deliberately no
    # separate heartbeat thread sharing this SQLite connection.
    if (now - last_heartbeat).total_seconds() >= interval_seconds:
        if not _lease_is_current(conn, guard, now, ttl_seconds=ttl_seconds):
            raise LeaseLostError()
        return now
    return last_heartbeat


def _create_run(conn: sqlite3.Connection, *, now: datetime, trigger_type: str,
                run_key: str, owner_token: str, guard: dict[str, Any] | None = None) -> dict[str, Any] | None:
    ts = rm.iso(now)
    safe_owner = (owner_token or "").strip()[:120]
    try:
        if guard is not None:
            _begin_guarded_write(conn, guard, now)
        cur = conn.execute(
            "INSERT INTO reminder_scheduler_runs(run_key,trigger_type,status,started_at,owner_token,"
            "lease_fencing_token,created_at) VALUES(?,?,?,?,?,?,?)",
            (run_key, trigger_type, STATUS_RUNNING, ts, safe_owner,
             guard["fencing_token"] if guard else None, ts),
        )
        run_id = int(cur.lastrowid)
        if guard is not None:
            _commit_guarded_write(conn, guard, now)
        else:
            conn.commit()
    except sqlite3.IntegrityError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        existing = _find_run_by_key(conn, run_key)
        return _run_dict(existing) if existing is not None else None
    except LeaseLostError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return get_run(conn, run_id)
def _finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, now: datetime,
                counters: dict[str, int], last_error: str = "",
                guard: dict[str, Any] | None = None) -> dict[str, Any]:
    if status not in RUN_STATUSES:
        status = STATUS_FAILED
    ts = rm.iso(now)
    sql = (
        "UPDATE reminder_scheduler_runs SET status=?, finished_at=?, processed_count=?, created_count=?, "
        "updated_count=?, skipped_count=?, due_count=?, expired_count=?, cancelled_count=?, "
        "recovered_run_count=?, error_count=?, last_error=? WHERE id=?"
    )
    params: list[Any] = [status, ts, counters["processed_count"], counters["created_count"],
                         counters["updated_count"], counters["skipped_count"], counters["due_count"],
                         counters["expired_count"], counters["cancelled_count"],
                         counters["recovered_run_count"], counters["error_count"],
                         sanitize_error(last_error), run_id]
    if guard is not None:
        sql += " AND owner_token=? AND lease_fencing_token=?"
        params.extend([guard["owner_token"][:120], guard["fencing_token"]])
        _begin_guarded_write(conn, guard, now)
    cur = conn.execute(sql, params)
    if guard is not None and cur.rowcount != 1:
        conn.execute("ROLLBACK")
        raise LeaseLostError("无法结束当前持有者的调度运行记录")
    if guard is not None:
        _commit_guarded_write(conn, guard, now)
    else:
        conn.commit()
    return get_run(conn, run_id)


def expire_reminders_guarded(conn: sqlite3.Connection, *, now: datetime,
                             guard: dict[str, Any]) -> int:
    with guarded_write(conn, guard, now):
        count = rm.expire_reminders(conn, now=now)
    return count


def transition_reminder_guarded(conn: sqlite3.Connection, reminder_id: int, action: str, *,
                                now: datetime, guard: dict[str, Any],
                                cancel_reason: str | None = None,
                                snooze_minutes: int = 10) -> dict[str, Any]:
    with guarded_write(conn, guard, now):
        return rm.transition_reminder(conn, reminder_id, action, now=now,
                                      cancel_reason=cancel_reason,
                                      snooze_minutes=snooze_minutes)


def create_reminder_guarded(conn: sqlite3.Connection, draft: rm.RuleCandidate | Any, *,
                            now: datetime, guard: dict[str, Any]):
    with guarded_write(conn, guard, now):
        return rm.create_reminder_result(conn, draft.to_draft() if hasattr(draft, "to_draft") else draft, now=now)


def _mark_due_one(conn: sqlite3.Connection, row: sqlite3.Row, *, now: datetime,
                  guard: dict[str, Any]) -> bool:
    try:
        with guarded_write(conn, guard, now):
            reminder = rm.transition_reminder(conn, row["id"], "mark_due", now=now)
            if reminder["status"] == rm.STATUS_DUE:
                nd.create_reminder_due_outbox(conn, reminder, now=now)
    except rm.ReminderError:
        return False
    return reminder["status"] == rm.STATUS_DUE


def _advance_due(conn: sqlite3.Connection, settings: dict[str, Any], *, now: datetime,
                 counters: dict[str, int], guard: dict[str, Any],
                 control: _TickControl | None = None) -> None:
    tz = get_timezone(str(settings["timezone"]))
    local_now = now.astimezone(tz)
    deferred_until = quiet_until(local_now, settings)
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status IN ('scheduled','snoozed') "
        "AND expires_at>? ORDER BY COALESCE(snoozed_until,scheduled_at),id",
        (rm.iso(now),),
    ).fetchall()
    for row in rows:
        counters["processed_count"] += 1
        ready_at = row["snoozed_until"] if row["status"] == "snoozed" else row["scheduled_at"]
        if ready_at and rm.parse_iso(ready_at, field_name="ready_at") > now:
            counters["skipped_count"] += 1
            continue
        if deferred_until is not None:
            expires_at = rm.parse_iso(row["expires_at"], field_name="expires_at")
            if expires_at <= now or deferred_until >= expires_at:
                try:
                    transition_reminder_guarded(conn, row["id"], "expire", now=now, guard=guard)
                    counters["expired_count"] += 1
                except rm.ReminderError:
                    counters["error_count"] += 1
            else:
                counters["processed_count"] += 1
                counters["skipped_count"] += 1
            continue
        if _mark_due_one(conn, row, now=now, guard=guard):
            counters["due_count"] += 1
            counters["updated_count"] += 1
        else:
            counters["error_count"] += 1
        if control is not None:
            control.check("due_batch")



def _task_reminder_is_stale(row: sqlite3.Row, current_due: str) -> bool:
    """Return true for due/overdue reminders generated from an old task due date.

    The structured evidence snapshot is authoritative. Legacy rows without that
    snapshot may fall back to the historical dedup-key encoding. A malformed key
    is intentionally not treated as evidence of rescheduling. Stalled reminders
    never participate in due-date cancellation.
    """
    if row["rule_code"] not in {rm.RULE_TASK_DUE_SOON, rm.RULE_TASK_OVERDUE}:
        return False
    task_id = str(row["source_id"])
    snapshot_due: str | None = None
    try:
        evidence = json.loads(row["evidence_json"] or "[]")
    except (ValueError, TypeError):
        evidence = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if (item.get("kind") == "task" and item.get("label") == "任务截止日期"
                and item.get("source_type") == "tasks"
                and str(item.get("source_id")) == task_id):
            value = str(item.get("value") or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                snapshot_due = value
                break
    if snapshot_due is not None:
        return snapshot_due != current_due

    parts = str(row["dedup_key"]).split(":")
    # task.due_soon_v1:<task_id>:<date>; task.overdue_v1:<task_id>:<date>:<occurrence>
    if len(parts) < 3 or parts[0] != row["rule_code"] or parts[1] != task_id:
        return False
    legacy_due = parts[2]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", legacy_due):
        return False
    return legacy_due != current_due


_SAVEPOINT_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@contextmanager
def savepoint(conn: sqlite3.Connection, name: str | None = None):
    """Nested transaction boundary with a caller-provided or generated SQL-safe name."""
    savepoint_name = name if name is not None else f"sp_{uuid.uuid4().hex}"
    if not isinstance(savepoint_name, str) or not _SAVEPOINT_NAME_RE.fullmatch(savepoint_name):
        raise SchedulerError("invalid_savepoint_name", "SAVEPOINT 名称只能包含字母、数字和下划线")
    conn.execute(f"SAVEPOINT {savepoint_name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")


def _savepoint(conn: sqlite3.Connection, name: str | None = None):
    return savepoint(conn, name)

def _cancel_active(conn: sqlite3.Connection, *, source_type: str, source_id: int, reason: str,
                   now: datetime, include_statuses: tuple[str, ...], guard: dict[str, Any]) -> int:
    marks = ",".join("?" for _ in include_statuses)
    rows = conn.execute(
        f"SELECT id FROM reminders WHERE source_type=? AND source_id=? AND status IN ({marks})",
        (source_type, source_id, *include_statuses),
    ).fetchall()
    count = 0
    for row in rows:
        try:
            transition_reminder_guarded(conn, row["id"], "cancel", now=now, cancel_reason=reason, guard=guard)
            count += 1
        except rm.ReminderError:
            continue
    return count


def _cancel_disabled_task_rules(conn: sqlite3.Connection, settings: dict[str, Any], *,
                                now: datetime, counters: dict[str, int], guard: dict[str, Any]) -> None:
    rules = []
    if not settings.get("task_due_enabled"):
        rules.extend((rm.RULE_TASK_DUE_SOON, rm.RULE_TASK_OVERDUE))
    if not settings.get("task_stalled_enabled"):
        rules.append(rm.RULE_TASK_STALLED)
    for rule_code in rules:
        rows = conn.execute(
            "SELECT id FROM reminders WHERE rule_code=? AND status IN ('scheduled','snoozed')",
            (rule_code,),
        ).fetchall()
        for row in rows:
            try:
                transition_reminder_guarded(conn, row["id"], "cancel", now=now,
                                       cancel_reason="settings_disabled", guard=guard)
                counters["cancelled_count"] += 1
            except rm.ReminderError:
                counters["error_count"] += 1


def _cancel_for_task_state(conn: sqlite3.Connection, task: sqlite3.Row, *, now: datetime,
                           counters: dict[str, int], guard: dict[str, Any]) -> None:
    reason: str | None = None
    if task["status"] == "done":
        reason = "source_completed"
    elif task["status"] == "cancelled":
        reason = "source_cancelled"
    if reason:
        counters["cancelled_count"] += _cancel_active(
            conn, source_type="task", source_id=task["id"], reason=reason, now=now,
            include_statuses=(rm.STATUS_SCHEDULED, rm.STATUS_SNOOZED, rm.STATUS_DUE, rm.STATUS_ACKNOWLEDGED),
            guard=guard)
        return

    # A date-type due date has no time component. Old due-soon/overdue reminders are
    # superseded when the task's due date changes; stalled reminders are unaffected.
    current_due = str(task["due_date"] or "")
    if current_due:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE source_type='task' AND source_id=? "
            "AND status IN ('scheduled','snoozed','due','acknowledged') ORDER BY id",
            (task["id"],),
        ).fetchall()
        for row in rows:
            if _task_reminder_is_stale(row, current_due):
                try:
                    transition_reminder_guarded(conn, row["id"], "cancel", now=now,
                                           cancel_reason="source_rescheduled", guard=guard)
                    counters["cancelled_count"] += 1
                except rm.ReminderError:
                    counters["error_count"] += 1


def _cancel_for_recommendation_state(conn: sqlite3.Connection, rec: sqlite3.Row, has_outcome: bool, *,
                                     settings: dict[str, Any], now: datetime, counters: dict[str, int],
                                     guard: dict[str, Any]) -> None:
    reason = None
    if has_outcome:
        reason = "outcome_registered"
    elif not settings.get("recommendation_followup_enabled"):
        reason = "settings_disabled"
    elif rec["status"] != "accepted":
        reason = "recommendation_closed"
    else:
        try:
            if now >= rm.parse_iso(rec["expires_at"], field_name="expires_at"):
                reason = "recommendation_closed"
        except rm.ReminderError:
            reason = "recommendation_closed"
    if reason:
        # Turning the rule off stops future reminders but preserves a reminder that is already
        # visible. Closed/outcome sources invalidate active follow-ups, including visible ones.
        include = ((rm.STATUS_SCHEDULED, rm.STATUS_SNOOZED)
                   if reason == "settings_disabled"
                   else (rm.STATUS_SCHEDULED, rm.STATUS_SNOOZED,
                         rm.STATUS_DUE, rm.STATUS_ACKNOWLEDGED))
        counters["cancelled_count"] += _cancel_active(
            conn, source_type="recommendation", source_id=rec["id"], reason=reason, now=now,
            include_statuses=include,
            guard=guard)


class _TickControl:
    """Clock, cooperative-stop, heartbeat and soft deadline checks for one tick."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        guard: dict[str, Any],
        *,
        logical_now: datetime,
        started_at: datetime,
        ttl_seconds: float,
        clock: Callable[[], datetime] | None = None,
        should_stop: Callable[[str], bool] | None = None,
        heartbeat_interval_seconds: float = 10.0,
        max_tick_seconds: float = 90.0,
    ) -> None:
        self.conn = conn
        self.guard = guard
        self.logical_now = logical_now
        self.started_at = started_at
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.should_stop = should_stop
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.max_tick_seconds = max_tick_seconds
        self.last_heartbeat = started_at

    def _wall_time(self) -> datetime:
        return self.clock() if self.clock is not None else self.logical_now

    def check(self, stage: str) -> None:
        if self.should_stop is not None and self.should_stop(stage):
            raise SchedulerStopped()
        current = self._wall_time()
        elapsed = (current - self.started_at).total_seconds()
        if elapsed > self.max_tick_seconds:
            raise SchedulerStopped(f"调度 tick 超过软上限 {self.max_tick_seconds:g} 秒，已在 {stage} 边界停止")
        self.last_heartbeat = _heartbeat_if_needed(
            self.conn, self.guard, current, self.last_heartbeat,
            ttl_seconds=self.ttl_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
        )


def _create_candidate_safely(conn: sqlite3.Connection, candidate: rm.RuleCandidate | None, *,
                             now: datetime, counters: dict[str, int], guard: dict[str, Any]) -> None:
    if candidate is None:
        return
    counters["processed_count"] += 1
    try:
        result = create_reminder_guarded(conn, candidate, now=now, guard=guard)
    except (sqlite3.Error, rm.ReminderError, ValueError):
        counters["error_count"] += 1
        return
    # LeaseLostError is intentionally not swallowed: after fencing takeover the
    # stale executor must stop before starting another candidate/write.
    if result.created:
        counters["created_count"] += 1
    else:
        counters["skipped_count"] += 1


def _cancel_disabled_reviews(conn: sqlite3.Connection, settings: dict[str, Any], *, now: datetime,
                            counters: dict[str, int], guard: dict[str, Any]) -> None:
    rules = []
    if not settings.get("daily_review_enabled"):
        rules.append(rm.RULE_REVIEW_DAILY)
    if not settings.get("weekly_review_enabled"):
        rules.append(rm.RULE_REVIEW_WEEKLY)
    for rule_code in rules:
        rows = conn.execute(
            "SELECT id FROM reminders WHERE rule_code=? AND status IN ('scheduled','snoozed')",
            (rule_code,),
        ).fetchall()
        for row in rows:
            try:
                transition_reminder_guarded(conn, row["id"], "cancel", now=now,
                                       cancel_reason="settings_disabled", guard=guard)
                counters["cancelled_count"] += 1
            except rm.ReminderError:
                counters["error_count"] += 1


def _generate_candidates(conn: sqlite3.Connection, settings: dict[str, Any], *, now: datetime,
                         counters: dict[str, int], guard: dict[str, Any],
                         control: _TickControl | None = None) -> None:
    has_task_reminders = conn.execute(
        "SELECT 1 FROM reminders WHERE source_type='task' "
        "AND status IN ('scheduled','snoozed','due','acknowledged') LIMIT 1"
    ).fetchone() is not None
    if settings.get("task_due_enabled") or settings.get("task_stalled_enabled") or has_task_reminders:
        _cancel_disabled_task_rules(conn, settings, now=now, counters=counters, guard=guard)
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('todo','doing','done','cancelled') ORDER BY id"
        ).fetchall()
        for task in tasks:
            counters["processed_count"] += 1
            try:
                # Cancellation is a source-level consistency action. Candidate generation is
                # isolated per candidate below, so one bad candidate cannot roll back siblings.
                _cancel_for_task_state(conn, task, now=now, counters=counters, guard=guard)
                if task["status"] not in {"done", "cancelled"}:
                    if settings.get("task_due_enabled"):
                        _create_candidate_safely(
                            conn, rm.task_due_soon_candidate(dict(task), settings, now=now),
                            now=now, counters=counters, guard=guard)
                        for occurrence in (1, 2):
                            _create_candidate_safely(
                                conn,
                                rm.task_overdue_candidate(dict(task), settings, now=now,
                                                          occurrence=occurrence),
                                now=now, counters=counters, guard=guard)
                    if settings.get("task_stalled_enabled") and task["status"] == "doing":
                        for occurrence in (1, 2):
                            _create_candidate_safely(
                                conn,
                                rm.task_stalled_candidate(dict(task), settings, now=now,
                                                          occurrence=occurrence),
                                now=now, counters=counters, guard=guard)
            except (sqlite3.Error, rm.ReminderError, ValueError):
                counters["error_count"] += 1
            if control is not None:
                control.check("candidate_batch")

    has_rec_reminders = conn.execute(
        "SELECT 1 FROM reminders WHERE source_type='recommendation' "
        "AND status IN ('scheduled','snoozed','due','acknowledged') LIMIT 1"
    ).fetchone() is not None
    if settings.get("recommendation_followup_enabled") or has_rec_reminders:
        recs = conn.execute("SELECT * FROM recommendations ORDER BY id").fetchall()
        for rec in recs:
            counters["processed_count"] += 1
            try:
                has_outcome = conn.execute(
                    "SELECT 1 FROM recommendation_outcomes WHERE recommendation_id=?", (rec["id"],)
                ).fetchone() is not None
                _cancel_for_recommendation_state(conn, rec, has_outcome, settings=settings,
                                                 now=now, counters=counters, guard=guard)
                if (settings.get("recommendation_followup_enabled")
                        and rec["status"] == "accepted" and not has_outcome):
                    accepted_event = conn.execute(
                        "SELECT occurred_at FROM behavior_events WHERE event_type='recommendation.accepted' "
                        "AND subject_type='recommendation' AND subject_id=? ORDER BY occurred_at DESC LIMIT 1",
                        (rec["id"],),
                    ).fetchone()
                    accepted_at = rm.parse_iso(accepted_event["occurred_at"]) if accepted_event else None
                    for occurrence in (1, 2):
                        candidate = rm.recommendation_followup_candidate(
                            dict(rec), settings, now=now, has_outcome=False,
                            accepted_at=accepted_at, occurrence=occurrence,
                        )
                        _create_candidate_safely(conn, candidate, now=now, counters=counters, guard=guard)
            except (sqlite3.Error, rm.ReminderError, ValueError):
                counters["error_count"] += 1
            if control is not None:
                control.check("candidate_batch")

    _cancel_disabled_reviews(conn, settings, now=now, counters=counters, guard=guard)
    for kind, enabled_key in (("daily", "daily_review_enabled"), ("weekly", "weekly_review_enabled")):
        if not settings.get(enabled_key):
            continue
        start, end, _scheduled_day = rm.review_period_for(kind, settings, now=now)
        candidate = rm.review_candidate(kind, settings, now=now, period_start=start, period_end=end)
        _create_candidate_safely(conn, candidate, now=now, counters=counters, guard=guard)
    if control is not None:
        control.check("candidate_batch")


def run_tick(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    trigger_type: str = TRIGGER_MANUAL,
    run_key: str | None = None,
    owner_token: str = "",
    lease_ttl_seconds: float = LEASE_TTL_SECONDS,
    clock: Callable[[], datetime] | None = None,
    should_stop: Callable[[str], bool] | None = None,
    heartbeat_interval_seconds: float = 10.0,
    max_tick_seconds: float = 90.0,
) -> dict[str, Any]:
    """Run one deterministic scheduler tick under the cross-process SQLite lease.

    Every scheduler-owned business write is performed in a BEGIN IMMEDIATE
    transaction that verifies the same owner/lease/fencing tuple immediately
    before COMMIT. The process-local lock only reduces same-process overlap;
    the lease table is the cross-process authority.
    """
    current_dt = _require_now(now)
    trigger = _validate_trigger(trigger_type)
    key = (run_key or _new_run_key(current_dt)).strip()
    if not key or len(key) > 200:
        raise SchedulerError("invalid_run_key", "run_key 必须是 1..200 字符")
    with _lock:
        if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise SchedulerError("database_configuration", "SQLite 外键约束未启用", status_code=500)
        # Python sqlite3's legacy implicit-transaction mode may leave an open write
        # transaction after a caller-owned helper (for example update_settings). The
        # scheduler lease must be the boundary for this tick; commit that pre-tick work
        # before acquiring the cross-process lease.
        if conn.in_transaction:
            conn.commit()
        if run_key:
            existing = _find_run_by_key(conn, run_key)
            if existing is not None:
                return _run_dict(existing)

        lease_result = lease_svc.acquire_lease(
            conn, now=current_dt, ttl_seconds=lease_ttl_seconds,
            owner_token=owner_token or None, busy_timeout_ms=LEASE_BUSY_TIMEOUT_MS,
        )
        if lease_result.status != lease_svc.STATUS_ACQUIRED or lease_result.lease is None:
            if lease_result.status == lease_svc.STATUS_BUSY:
                raise LeaseBusyError(result=lease_result.to_dict())
            raise LeaseDatabaseError(
                lease_result.message or "无法获取调度租约",
                retryable=bool(getattr(lease_result, "retryable", False)),
            )
        lease = lease_result.lease
        guard = _guard_from_lease(lease)
        logger.info(
            "reminder scheduler lease acquired",
            extra={"fencing_token": lease.fencing_token, "lease_ttl_seconds": lease_ttl_seconds},
        )
        # Manual deterministic calls use their business timestamp throughout.
        # Automatic calls inject a wall clock; that clock is consulted at safety
        # boundaries to measure elapsed time and renew the lease.
        tick_started = current_dt
        control = _TickControl(
            conn, guard, logical_now=current_dt, started_at=tick_started,
            ttl_seconds=lease_ttl_seconds, clock=clock, should_stop=should_stop,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            max_tick_seconds=max_tick_seconds,
        )
        counters = {name: 0 for name in COUNT_KEYS}
        run: dict[str, Any] | None = None
        exc_being_handled: BaseException | None = None
        try:
            counters["recovered_run_count"] = recover_stale_runs(conn, now=current_dt, guard=guard)
            control.check("after_recovery")
            run = _create_run(conn, now=current_dt, trigger_type=trigger, run_key=key,
                              owner_token=lease.owner_token, guard=guard)
            if run is None:
                existing = _find_run_by_key(conn, key)
                if existing is not None:
                    return _run_dict(existing)
                raise SchedulerError("storage_conflict", "无法创建调度运行记录", status_code=409)
            run_id = int(run["id"])
            control.check("after_run_create")
            settings = rm.get_settings(conn)
            counters["expired_count"] += expire_reminders_guarded(conn, now=current_dt, guard=guard)
            control.check("after_initial_expiry")
            _generate_candidates(conn, settings, now=current_dt, counters=counters, guard=guard,
                                 control=control)
            control.check("after_candidate_generation")
            counters["expired_count"] += expire_reminders_guarded(conn, now=current_dt, guard=guard)
            control.check("after_second_expiry")
            _advance_due(conn, settings, now=current_dt, counters=counters, guard=guard,
                         control=control)
            control.check("after_due_advance")
            status = STATUS_PARTIAL_FAILED if counters["error_count"] else STATUS_SUCCEEDED
            return _finish_run(conn, run_id, status=status, now=current_dt,
                               counters=counters, guard=guard)
        except Exception as exc:
            exc_being_handled = exc
            try:
                row = _find_run_by_key(conn, key)
                if row is not None and row["status"] == STATUS_RUNNING:
                    _finish_run(conn, int(row["id"]), status=STATUS_FAILED, now=current_dt,
                                counters=counters, last_error=sanitize_error(exc), guard=guard)
            except sqlite3.Error:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            except LeaseLostError:
                # A newer fencing owner owns the database; this stale process must not
                # overwrite that owner's run record or turn it into failed.
                pass
            if isinstance(exc, SchedulerError):
                raise
            raise SchedulerError("scheduler_failed", sanitize_error(exc), status_code=500) from exc
        finally:
            # Release is conditional on the exact owner/lease/fencing tuple. If the lease
            # was lost, leave it for the current owner and never overwrite its state.
            try:
                released = lease_svc.release_lease(
                    conn, owner_token=lease.owner_token, lease_token=lease.lease_token,
                    fencing_token=lease.fencing_token, now=current_dt,
                    busy_timeout_ms=LEASE_BUSY_TIMEOUT_MS,
                )
                if released.status == lease_svc.STATUS_ACQUIRED:
                    logger.debug(
                        "reminder scheduler lease released",
                        extra={"fencing_token": lease.fencing_token},
                    )
                elif released.status == lease_svc.STATUS_LOST:
                    logger.warning(
                        "reminder scheduler lease release skipped because ownership changed",
                        extra={"fencing_token": lease.fencing_token, "error_code": released.error_code},
                    )
                else:
                    logger.warning(
                        "reminder scheduler lease release failed",
                        extra={
                            "fencing_token": lease.fencing_token,
                            "status": released.status,
                            "error_code": released.error_code,
                        },
                    )
                    # Release failure is operational metadata; do not erase already committed work.
                    if run is not None:
                        run["lease_release_status"] = released.status
            except (sqlite3.Error, OSError, lease_svc.LeaseError) as exc:
                logger.warning(
                    "reminder scheduler lease release raised",
                    extra={"fencing_token": lease.fencing_token, "error_type": type(exc).__name__},
                )
                if run is not None:
                    run["lease_release_status"] = lease_svc.STATUS_DATABASE_ERROR
            # Avoid masking an in-flight exception with a release error.
            if exc_being_handled is not None:
                # Re-raise is already in progress via exception propagation; nothing else to do.
                pass
