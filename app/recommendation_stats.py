"""Read-only effectiveness statistics for recommendations (1.3.4-D).

The service deliberately stores nothing and performs no model calls. It reports
current recommendation/feedback/outcome state plus separately labelled behavior
events, because current rows are mutable and do not form a complete audit log.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from app.recommendation_feedback import (
    HELPFUL_REASONS,
    OBSERVATION_METRICS,
    OBSERVATION_UNITS,
    UNHELPFUL_REASONS,
)

DEFINITION_VERSION = "recommendation_feedback_stats_v1"
DEFAULT_WINDOW_DAYS = 30
MIN_STABLE_SAMPLE = 10

RECOMMENDATION_STATUSES = ("pending", "accepted", "ignored", "rejected", "expired")
CATEGORIES = ("task", "project", "goal", "learning", "habit")
COVERAGE_STATUSES = ("ok", "partial_data", "insufficient_data", "no_data")
FEEDBACK_VOTES = ("helpful", "unhelpful")
PREFERENCE_SCOPES = ("temporary", "persistent", "unspecified")
EXECUTION_STATUSES = (
    "planned", "completed", "partially_completed", "not_completed", "no_longer_relevant",
)
RESULT_RATINGS = ("much_better", "better", "as_expected", "worse", "much_worse", "unclear")
RUN_STATUSES = ("running", "success", "no_evidence", "degraded", "failed")
ALLOWED_REASON_CODES = HELPFUL_REASONS | UNHELPFUL_REASONS
LIFECYCLE_EVENTS = {
    "recommendation.accepted": "accepted",
    "recommendation.ignored": "ignored",
    "recommendation.rejected": "rejected",
    "recommendation.reopened": "reopened",
}


class StatsError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | datetime | None, *, field_name: str, default: datetime | None = None) -> datetime:
    if value is None:
        if default is None:
            raise StatsError("invalid_time", f"{field_name} 必须提供")
        return default
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise StatsError("invalid_time", f"{field_name} 必须是 ISO-8601 时间")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise StatsError("invalid_time", f"{field_name} 必须是有效的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _exact_iso(value: datetime) -> str:
    # Rows use second precision, but query boundaries may carry sub-seconds.
    if value.microsecond:
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _stored_second_bounds(start_dt: datetime, end_dt: datetime) -> tuple[str, str]:
    # Widen SQL to complete stored seconds; Python applies the exact interval.
    lower = start_dt.replace(microsecond=0).isoformat(timespec="seconds")
    upper_dt = end_dt if end_dt.microsecond == 0 else end_dt.replace(microsecond=0) + timedelta(seconds=1)
    return lower, upper_dt.isoformat(timespec="seconds")


def _in_exact_window(stored_at: str, start_exact: str, end_exact: str) -> bool:
    return start_exact <= str(stored_at) < end_exact


@contextmanager
def _read_snapshot(conn: sqlite3.Connection):
    # Keep all aggregates in a response on one SQLite read snapshot. The
    # deferred transaction begins on the first SELECT; this service performs
    # no writes and always releases it via rollback.
    already_in_transaction = bool(conn.in_transaction)
    if not already_in_transaction:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if not already_in_transaction:
            conn.rollback()


def _zero_counts(values: tuple[str, ...]) -> dict[str, int]:
    return {value: 0 for value in values}


def _ratio(numerator: int, denominator: int) -> dict[str, Any] | None:
    if denominator <= 0:
        return None
    sample_status = "ok" if denominator >= MIN_STABLE_SAMPLE else "insufficient_data"
    return {
        "numerator": int(numerator),
        "denominator": int(denominator),
        "value": round(numerator / denominator, 4),
        "sample_status": sample_status,
    }


def _load_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _window_rows(
    conn: sqlite3.Connection,
    start_dt: datetime,
    end_dt: datetime,
    start_exact: str,
    end_exact: str,
) -> list[sqlite3.Row]:
    lower, upper = _stored_second_bounds(start_dt, end_dt)
    rows = conn.execute(
        "SELECT * FROM recommendations "
        "WHERE created_at >= ? AND created_at <= ? ORDER BY id",
        (lower, upper),
    ).fetchall()
    return [row for row in rows if _in_exact_window(row["created_at"], start_exact, end_exact)]


def _recommendation_stats(rows: list[sqlite3.Row], now_iso: str) -> dict[str, Any]:
    status_counts = _zero_counts(RECOMMENDATION_STATUSES)
    category_counts = _zero_counts(CATEGORIES)
    coverage_counts = _zero_counts(COVERAGE_STATUSES)
    logically_expired_active = 0
    evidence_present = 0

    for row in rows:
        status = row["status"]
        category = row["category"]
        coverage = row["coverage_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1
        coverage_counts[coverage] = coverage_counts.get(coverage, 0) + 1
        if status in ("pending", "accepted") and row["expires_at"] and row["expires_at"] < now_iso:
            logically_expired_active += 1
        evidence = _load_json(row["evidence_json"], [])
        if isinstance(evidence, list) and evidence:
            evidence_present += 1

    total = len(rows)
    return {
        "total": total,
        "status_counts": status_counts,
        "category_counts": category_counts,
        "coverage_counts": coverage_counts,
        "logically_expired_active_count": logically_expired_active,
        "evidence_present_count": evidence_present,
        "evidence_present_rate": _ratio(evidence_present, total),
    }


def _chunks(values: list[int], size: int = 500):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _feedback_stats(conn: sqlite3.Connection, ids: list[int]) -> dict[str, Any]:
    vote_counts = _zero_counts(FEEDBACK_VOTES)
    preference_counts = _zero_counts(PREFERENCE_SCOPES)
    reason_counts: dict[str, int] = {}
    total = 0

    if ids:
        rows = []
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(
                f"SELECT vote, reason_codes_json, preference_scope "
                f"FROM recommendation_feedback WHERE recommendation_id IN ({placeholders})",
                chunk,
            ).fetchall())
        total = len(rows)
        for row in rows:
            vote_counts[row["vote"]] = vote_counts.get(row["vote"], 0) + 1
            preference_counts[row["preference_scope"]] = preference_counts.get(row["preference_scope"], 0) + 1
            for code in _load_json(row["reason_codes_json"], []):
                # Ignore corrupt/legacy values rather than enlarging the fixed
                # public statistics vocabulary.
                if isinstance(code, str) and code in ALLOWED_REASON_CODES:
                    reason_counts[code] = reason_counts.get(code, 0) + 1

    return {
        "record_type": "current_only",
        "total": total,
        "vote_counts": vote_counts,
        "feedback_rate": _ratio(total, len(ids)),
        "helpful_rate": _ratio(vote_counts["helpful"], total),
        "reason_code_counts": dict(sorted(reason_counts.items())),
        "preference_scope_counts": preference_counts,
    }


def _lifecycle_event_stats(
    conn: sqlite3.Connection,
    start_dt: datetime,
    end_dt: datetime,
    start_exact: str,
    end_exact: str,
) -> dict[str, Any]:
    event_counts = {label: 0 for label in LIFECYCLE_EVENTS.values()}
    accepted_ids: set[int] = set()
    lower, upper = _stored_second_bounds(start_dt, end_dt)
    rows = conn.execute(
        "SELECT event_type, subject_id, occurred_at FROM behavior_events "
        "WHERE subject_type='recommendation' AND occurred_at >= ? AND occurred_at <= ? "
        "AND event_type IN (?,?,?,?)",
        (lower, upper, *LIFECYCLE_EVENTS.keys()),
    ).fetchall()
    for row in rows:
        if not _in_exact_window(row["occurred_at"], start_exact, end_exact):
            continue
        label = LIFECYCLE_EVENTS[row["event_type"]]
        event_counts[label] += 1
        if label == "accepted" and row["subject_id"] is not None:
            accepted_ids.add(int(row["subject_id"]))
    return {
        "accepted_event_count": event_counts["accepted"],
        "distinct_recommendation_count": len(accepted_ids),
        "event_counts": event_counts,
        "history_completeness": "partial_observability",
    }


def _metric_groups(outcome_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    if not outcome_rows:
        return []
    groups: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for row in outcome_rows:
        observations = _load_json(row["metric_observations_json"], [])
        if not isinstance(observations, list):
            continue
        for item in observations:
            if not isinstance(item, dict):
                continue
            expected = item.get("expected_value")
            observed = item.get("observed_value")
            metric = item.get("metric")
            unit = item.get("unit")
            if not isinstance(metric, str) or not isinstance(unit, str):
                continue
            metric = metric.strip().lower()
            unit = unit.strip().lower()
            if metric not in OBSERVATION_METRICS or unit not in OBSERVATION_UNITS:
                continue
            if isinstance(expected, bool) or isinstance(observed, bool):
                continue
            if not isinstance(expected, (int, float)) or not isinstance(observed, (int, float)):
                continue
            if not math.isfinite(expected) or not math.isfinite(observed):
                continue
            groups.setdefault((metric, unit), []).append((float(expected), float(observed)))

    result = []
    for (metric, unit), values in sorted(groups.items()):
        count = len(values)
        avg_expected = sum(item[0] for item in values) / count
        avg_observed = sum(item[1] for item in values) / count
        result.append({
            "metric": metric,
            "unit": unit,
            "sample_count": count,
            "average_expected_value": round(avg_expected, 4),
            "average_observed_value": round(avg_observed, 4),
            "average_difference": round(avg_observed - avg_expected, 4),
            "sample_status": "ok" if count >= MIN_STABLE_SAMPLE else "insufficient_data",
            "causality_status": "not_inferred",
        })
    return result


def _outcome_stats(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[str, Any]:
    execution_counts = _zero_counts(EXECUTION_STATUSES)
    rating_counts = _zero_counts(RESULT_RATINGS)
    ids = [int(row["id"]) for row in rows]
    current_accepted_ids = [int(row["id"]) for row in rows if row["status"] == "accepted"]
    total = 0
    registered_current_accepted = 0
    retained_after_reopen = 0

    if ids:
        outcome_rows = []
        for chunk in _chunks(ids):
            placeholders = ",".join("?" for _ in chunk)
            outcome_rows.extend(conn.execute(
                f"SELECT recommendation_id, execution_status, result_rating, metric_observations_json "
                f"FROM recommendation_outcomes WHERE recommendation_id IN ({placeholders})",
                chunk,
            ).fetchall())
        outcome_by_rec = {int(row["recommendation_id"]): row for row in outcome_rows}
        total = len(outcome_rows)
        registered_current_accepted = sum(1 for rec_id in current_accepted_ids if rec_id in outcome_by_rec)
        retained_after_reopen = sum(1 for rec_id in ids if rec_id not in set(current_accepted_ids) and rec_id in outcome_by_rec)
        for row in outcome_rows:
            execution_counts[row["execution_status"]] = execution_counts.get(row["execution_status"], 0) + 1
            rating_counts[row["result_rating"]] = rating_counts.get(row["result_rating"], 0) + 1

    metric_groups = _metric_groups(outcome_rows if ids else [])
    return {
        "record_type": "current_only",
        "total": total,
        "current_accepted_count": len(current_accepted_ids),
        "registered_outcome_count": registered_current_accepted,
        "registration_rate": _ratio(registered_current_accepted, len(current_accepted_ids)),
        "retained_after_reopen_count": retained_after_reopen,
        "execution_status_counts": execution_counts,
        "completion_rate": _ratio(execution_counts["completed"], total),
        "rating_counts": rating_counts,
        "comparable_metric_groups": metric_groups,
        "comparable_observation_count": sum(group["sample_count"] for group in metric_groups),
        "missing_outcome_means": "not_observed_not_not_completed",
    }


def _rate_counts(counts: dict[str, int], denominator: int) -> dict[str, dict[str, Any] | None]:
    return {key: _ratio(value, denominator) for key, value in counts.items()}


def _run_stats(
    conn: sqlite3.Connection,
    start_dt: datetime,
    end_dt: datetime,
    start_exact: str,
    end_exact: str,
) -> dict[str, Any]:
    counts = _zero_counts(RUN_STATUSES)
    model_requests = 0
    lower, upper = _stored_second_bounds(start_dt, end_dt)
    rows = conn.execute(
        "SELECT status, use_model, started_at FROM recommendation_runs "
        "WHERE started_at >= ? AND started_at <= ?",
        (lower, upper),
    ).fetchall()
    for row in rows:
        if not _in_exact_window(row["started_at"], start_exact, end_exact):
            continue
        status = row["status"]
        if status not in counts:
            continue
        counts[status] += 1
        try:
            model_requests += int(row["use_model"] or 0)
        except (TypeError, ValueError):
            pass
    total = sum(counts.values())
    normal = counts["success"] + counts["no_evidence"] + counts["degraded"]
    return {
        "total": total,
        "status_counts": counts,
        "completed_normally_count": normal,
        "failed_count": counts["failed"],
        "running_count": counts["running"],
        "model_enabled_run_count": model_requests,
        "failure_rate": _ratio(counts["failed"], total),
    }


def get_recommendation_statistics(
    conn: sqlite3.Connection,
    *,
    start_at: str | datetime | None = None,
    end_at: str | datetime | None = None,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    current_dt = _parse_time(now, field_name="now", default=utc_now())
    end_dt = _parse_time(end_at, field_name="end_at", default=current_dt)
    start_dt = _parse_time(start_at, field_name="start_at", default=end_dt - timedelta(days=DEFAULT_WINDOW_DAYS))
    if end_dt <= start_dt:
        raise StatsError("invalid_time_range", "end_at 必须晚于 start_at")

    start_iso = _exact_iso(start_dt)
    end_iso = _exact_iso(end_dt)
    now_iso = _iso(current_dt)
    with _read_snapshot(conn):
        rec_rows = _window_rows(conn, start_dt, end_dt, start_iso, end_iso)
        ids = [int(row["id"]) for row in rec_rows]
        recommendations = _recommendation_stats(rec_rows, now_iso)
        feedback = _feedback_stats(conn, ids)
        outcomes = _outcome_stats(conn, rec_rows)
        runs = _run_stats(conn, start_dt, end_dt, start_iso, end_iso)
        adoption_events = _lifecycle_event_stats(conn, start_dt, end_dt, start_iso, end_iso)
    total = recommendations["total"]
    quality_status = "no_data" if total == 0 else ("ok" if total >= MIN_STABLE_SAMPLE else "insufficient_data")

    return {
        "definition_version": DEFINITION_VERSION,
        "generated_at": now_iso,
        "window": {
            "start_at": start_iso,
            "end_at": end_iso,
            "interval": "[start_at,end_at)",
            "time_resolution": "stored_seconds",
            "default_days": DEFAULT_WINDOW_DAYS,
        },
        "recommendations": recommendations,
        "runs": runs,
        "feedback": feedback,
        "adoption": {
            "current_accepted_count": recommendations["status_counts"]["accepted"],
            "current_accepted_rate": _ratio(recommendations["status_counts"]["accepted"], total),
            "events": adoption_events,
        },
        "outcomes": outcomes,
        "ratings": {
            "counts": outcomes["rating_counts"],
            "rates": _rate_counts(outcomes["rating_counts"], outcomes["total"]),
            "denominator": outcomes["total"],
            "record_type": "current_only",
        },
        "quality": {
            "sample_status": quality_status,
            "recommendation_sample_size": total,
            "evidence_present_rate": recommendations["evidence_present_rate"],
            "feedback_coverage_rate": feedback["feedback_rate"],
            "accepted_outcome_registration_rate": outcomes["registration_rate"],
            "comparable_observation_count": outcomes["comparable_observation_count"],
            "composite_score": None,
        },
        "limitations": [
            "当前反馈和结果是可修改的当前记录，不代表完整历史版本。",
            "采纳事件仅覆盖行为事件表上线后的可观测数据，不能反推历史从未采纳。",
            "没有反馈代表未知，没有结果登记代表未观测，不代表无用或未执行。",
            "用户登记的指标差异不证明建议与结果之间存在因果关系。",
        ],
    }
