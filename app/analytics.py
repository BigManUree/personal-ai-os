"""Deterministic behavior analytics (Personal AI OS 1.3.2-B).

Pure programmatic statistics only. This module never calls an LLM and never
interprets a numeric change as a personality trait, cause, or stable habit.

The deployed target is a single-user local app. Windows tzdata is not
installed in the project venv, so the first release supports UTC, the
user's local zone (Asia/Shanghai), and explicit fixed UTC offsets.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

DEFINITION_VERSION = "1.3.2-B"
COMPARISON_DEFINITION_VERSION = "1.3.2-C"
MIN_STRONG_SAMPLE = 5

PROJECT_ACTIVITY_EVENTS = {
    "project.created",
    "project.updated",
    "project.status_changed",
    "project_event.recorded",
}
TASK_EVENT_KEYS = {
    "task.created": "tasks_created",
    "task.completed": "tasks_completed",
    "task.reopened": "tasks_reopened",
    "task.cancelled": "tasks_cancelled",
}
DAILY_KEYS = (
    "documents_created",
    "project_activities",
    "goals_created",
    "tasks_created",
    "tasks_completed",
    "tasks_reopened",
    "tasks_cancelled",
)

_FIXED_OFFSET_RE = re.compile(r"^UTC([+-])(\d{2}):?(\d{2})$")


@dataclass(frozen=True)
class TimeWindow:
    days: int
    timezone_name: str
    tzinfo: timezone
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime


def get_timezone(name: str) -> timezone:
    """Resolve the deliberately small set of supported zone names."""
    value = (name or "").strip()
    if value in {"UTC", "Etc/UTC", "Z"}:
        return timezone.utc
    if value == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    match = _FIXED_OFFSET_RE.match(value)
    if match:
        sign, hours_text, minutes_text = match.groups()
        hours = int(hours_text)
        minutes = int(minutes_text)
        if hours > 23 or minutes > 59:
            raise ValueError("时区固定偏移超出范围")
        offset = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            offset = -offset
        return timezone(offset)
    raise ValueError("当前版本仅支持 UTC、Asia/Shanghai 或 UTC±HH:MM 时区")


def build_window(
    *,
    days: int,
    now: datetime,
    timezone_name: str = "Asia/Shanghai",
) -> TimeWindow:
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 366:
        raise ValueError("days 必须是 1..366 的整数")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now 必须是带时区信息的 datetime")
    tz = get_timezone(timezone_name)
    now_local = now.astimezone(tz)
    start_local = (now_local - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_local = start_local + timedelta(days=days)
    return TimeWindow(
        days=days,
        timezone_name=timezone_name,
        tzinfo=tz,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sample_status(sample_size: int) -> str:
    if sample_size <= 0:
        return "no_data"
    if sample_size < MIN_STRONG_SAMPLE:
        return "insufficient_data"
    return "ok"


def _count_metric(value: int, *, unit: str = "count") -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "numerator": value,
        "denominator": None,
        "sample_size": value,
        "status": _sample_status(value),
        "definition_version": DEFINITION_VERSION,
    }


def _ratio_metric(numerator: int, denominator: int) -> dict[str, Any]:
    if denominator <= 0:
        value = None
        status = "no_data"
    else:
        value = round(numerator / denominator, 4)
        status = _sample_status(denominator)
    return {
        "value": value,
        "unit": "ratio",
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": denominator,
        "status": status,
        "definition_version": DEFINITION_VERSION,
    }


def _empty_daily(window: TimeWindow) -> list[dict[str, Any]]:
    rows = []
    for offset in range(window.days):
        day = (window.start_local + timedelta(days=offset)).date().isoformat()
        row = {"date": day}
        row.update({key: 0 for key in DAILY_KEYS})
        rows.append(row)
    return rows


def _event_counts(conn: sqlite3.Connection, window: TimeWindow) -> tuple[dict[str, int], dict[str, bool], list[dict[str, int]]]:
    totals = {key: 0 for key in DAILY_KEYS}
    daily = _empty_daily(window)
    daily_by_date = {row["date"]: row for row in daily}
    observed = {"documents": False, "projects": False, "tasks": False}

    start = window.start_utc.isoformat(timespec="seconds")
    end = window.end_utc.isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT event_type, occurred_at FROM behavior_events "
        "WHERE occurred_at>=? AND occurred_at<? ORDER BY occurred_at, id",
        (start, end),
    ).fetchall()

    for row in rows:
        occurred_utc = _parse_iso(row["occurred_at"])
        if occurred_utc is None or not (window.start_utc <= occurred_utc < window.end_utc):
            continue
        event_type = row["event_type"]
        local_date = occurred_utc.astimezone(window.tzinfo).date().isoformat()
        day_row = daily_by_date.get(local_date)
        if day_row is None:
            continue

        if event_type == "document.created":
            totals["documents_created"] += 1
            day_row["documents_created"] += 1
            observed["documents"] = True
        elif event_type in PROJECT_ACTIVITY_EVENTS:
            totals["project_activities"] += 1
            day_row["project_activities"] += 1
            observed["projects"] = True
        elif event_type == "goal.created":
            totals["goals_created"] += 1
            day_row["goals_created"] += 1
        elif event_type in TASK_EVENT_KEYS:
            key = TASK_EVENT_KEYS[event_type]
            totals[key] += 1
            day_row[key] += 1
            observed["tasks"] = True

    return totals, observed, daily


def _task_snapshot_metrics(conn: sqlite3.Connection, window: TimeWindow) -> dict[str, Any]:
    """Current-state task metrics and windowed completion duration.

    Snapshot counts intentionally read the current tasks table; they are not
    event counts and therefore do not require historical behavior events.
    """
    current_local = window.end_local
    today = current_local.date()

    open_rows = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('todo', 'doing')"
    ).fetchone()[0]
    overdue_rows = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('todo', 'doing') "
        "AND due_date IS NOT NULL AND date(due_date) < date(?)",
        (today.isoformat(),),
    ).fetchone()[0]

    start = window.start_utc.isoformat(timespec="seconds")
    end = window.end_utc.isoformat(timespec="seconds")
    completed_rows = conn.execute(
        "SELECT created_at, completed_at FROM tasks "
        "WHERE status='done' AND completed_at IS NOT NULL "
        "AND completed_at>=? AND completed_at<? ORDER BY id",
        (start, end),
    ).fetchall()

    durations: list[float] = []
    for row in completed_rows:
        created = _parse_iso(row["created_at"])
        completed = _parse_iso(row["completed_at"])
        if created is None or completed is None or completed < created:
            continue
        minutes = (completed - created).total_seconds() / 60.0
        if minutes >= 0:
            durations.append(round(minutes, 2))

    if durations:
        ordered = sorted(durations)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            median = ordered[middle]
        else:
            median = round((ordered[middle - 1] + ordered[middle]) / 2, 2)
        average = round(sum(ordered) / len(ordered), 2)
        duration: dict[str, Any] = {
            "value": average,
            "average_minutes": average,
            "median_minutes": median,
            "unit": "minutes",
            "numerator": round(sum(ordered), 2),
            "denominator": len(ordered),
            "sample_size": len(ordered),
            "status": _sample_status(len(ordered)),
            "definition_version": DEFINITION_VERSION,
        }
    else:
        duration = {
            "value": None,
            "average_minutes": None,
            "median_minutes": None,
            "unit": "minutes",
            "numerator": None,
            "denominator": 0,
            "sample_size": 0,
            "status": "no_data",
            "definition_version": DEFINITION_VERSION,
        }

    return {
        "open_backlog_count": _count_metric(int(open_rows)),
        "overdue_open_count": _count_metric(int(overdue_rows)),
        "completion_duration_minutes": duration,
    }


def _sync_counts(conn: sqlite3.Connection, window: TimeWindow) -> dict[str, int]:
    attempts = 0
    successes = 0
    failures = 0
    running = 0
    rows = conn.execute(
        "SELECT status, started_at FROM sync_logs ORDER BY started_at, id"
    ).fetchall()
    for row in rows:
        started = _parse_iso(row["started_at"])
        if started is None or not (window.start_utc <= started < window.end_utc):
            continue
        attempts += 1
        if row["status"] == "success":
            successes += 1
        elif row["status"] == "failed":
            failures += 1
        elif row["status"] == "running":
            running += 1
    return {
        "attempts": attempts,
        "successes": successes,
        "failures": failures,
        "running": running,
        "finished": successes + failures,
    }


def _sync_metrics(conn: sqlite3.Connection, window: TimeWindow) -> tuple[dict[str, Any], bool]:
    counts = _sync_counts(conn, window)
    metrics = {
        "connector_sync_attempts": _count_metric(counts["attempts"]),
        "connector_sync_success_rate": _ratio_metric(
            counts["successes"], counts["finished"]
        ),
    }
    return metrics, counts["attempts"] > 0


def overview(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    window = build_window(days=days, now=current, timezone_name=timezone_name)
    totals, observed, daily = _event_counts(conn, window)
    sync_metrics, connector_observed = _sync_metrics(conn, window)
    task_snapshot_metrics = _task_snapshot_metrics(conn, window)
    observed["connectors"] = connector_observed

    observed_count = sum(1 for value in observed.values() if value)
    if observed_count == 0:
        coverage_status = "no_data"
    elif observed_count == 1:
        coverage_status = "partial_data"
    else:
        coverage_status = "ok"

    metrics: dict[str, Any] = {
        "documents_created": _count_metric(totals["documents_created"]),
        "project_activities": _count_metric(totals["project_activities"]),
        "goals_created": _count_metric(totals["goals_created"]),
        "tasks_created": _count_metric(totals["tasks_created"]),
        "tasks_completed": _count_metric(totals["tasks_completed"]),
        "tasks_reopened": _count_metric(totals["tasks_reopened"]),
        "tasks_cancelled": _count_metric(totals["tasks_cancelled"]),
        "net_completion_ratio": _ratio_metric(
            totals["tasks_completed"], totals["tasks_created"]
        ),
        **task_snapshot_metrics,
        **sync_metrics,
    }

    return {
        "definition_version": DEFINITION_VERSION,
        "generated_at": current.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "window": {
            "days": window.days,
            "timezone": window.timezone_name,
            "start": window.start_local.isoformat(timespec="seconds"),
            "end": window.end_local.isoformat(timespec="seconds"),
        },
        "daily": daily,
        "metrics": metrics,
        "coverage": {
            "status": coverage_status,
            "observed_categories": observed,
            "observed_category_count": observed_count,
        },
    }

def _window_payload(window: TimeWindow) -> dict[str, Any]:
    return {
        "start": window.start_local.isoformat(timespec="seconds"),
        "end": window.end_local.isoformat(timespec="seconds"),
    }


def _comparison_status(previous: int, current: int) -> str:
    if previous == 0 and current == 0:
        return "no_data"
    if max(previous, current) < MIN_STRONG_SAMPLE:
        return "insufficient_data"
    return "ok"


def _compare_count(name: str, previous: int, current: int) -> dict[str, Any]:
    delta = current - previous
    if previous == 0 and current == 0:
        direction = "flat"
        percent_change = None
        significant = False
    elif previous == 0:
        direction = "up" if current > 0 else "flat"
        percent_change = None
        # A zero-to-activity change is only noteworthy with enough current data.
        significant = current >= MIN_STRONG_SAMPLE
    elif current == 0:
        direction = "down"
        percent_change = -1.0
        significant = previous >= MIN_STRONG_SAMPLE
    else:
        percent_change = round(delta / previous, 4)
        if delta > 0:
            direction = "up"
        elif delta < 0:
            direction = "down"
        else:
            direction = "flat"
        significant = (
            max(previous, current) >= MIN_STRONG_SAMPLE
            and abs(percent_change) >= 0.20
        )

    return {
        "metric": name,
        "previous": {"value": previous},
        "current": {"value": current},
        "delta": delta,
        "percent_change": percent_change,
        "direction": direction,
        "significant": significant,
        "status": _comparison_status(previous, current),
        "definition_version": COMPARISON_DEFINITION_VERSION,
    }


def compare_periods(
    conn: sqlite3.Connection,
    *,
    window_days: int = 7,
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Compare two equal, adjacent windows (7/7 or 30/30 days).

    This is a numeric observation only. It does not explain causes and never
    treats correlation as a personal trait.
    """
    if window_days not in {7, 30}:
        raise ValueError("周期对比目前只支持 7 天或 30 天等长窗口")
    current_time = now or datetime.now(timezone.utc)
    current_window = build_window(
        days=window_days, now=current_time, timezone_name=timezone_name
    )
    previous_window = TimeWindow(
        days=window_days,
        timezone_name=timezone_name,
        tzinfo=current_window.tzinfo,
        start_local=current_window.start_local - timedelta(days=window_days),
        end_local=current_window.start_local,
        start_utc=current_window.start_utc - timedelta(days=window_days),
        end_utc=current_window.start_utc,
    )

    current_totals, _, _ = _event_counts(conn, current_window)
    previous_totals, _, _ = _event_counts(conn, previous_window)
    current_sync = _sync_counts(conn, current_window)
    previous_sync = _sync_counts(conn, previous_window)

    count_metrics = [
        "documents_created",
        "project_activities",
        "goals_created",
        "tasks_created",
        "tasks_completed",
        "tasks_reopened",
        "tasks_cancelled",
    ]
    metrics = {
        name: _compare_count(name, previous_totals[name], current_totals[name])
        for name in count_metrics
    }
    for name, sync_key in (
        ("connector_sync_attempts", "attempts"),
        ("connector_sync_successes", "successes"),
        ("connector_sync_failures", "failures"),
    ):
        metrics[name] = _compare_count(
            name, previous_sync[sync_key], current_sync[sync_key]
        )

    return {
        "definition_version": COMPARISON_DEFINITION_VERSION,
        "generated_at": current_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "window_days": window_days,
        "timezone": timezone_name,
        "current": _window_payload(current_window),
        "previous": _window_payload(previous_window),
        "metrics": metrics,
    }

