"""Storage service for recommendation feedback and outcomes (1.3.4-B).

This module is intentionally below the HTTP layer. It stores only explicit
user-supplied feedback/outcomes and never writes tasks, goals, projects,
memories, profile items, or recommendation statuses.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app import behavior_events
from app.profile_propose import looks_like_secret
from app.recommendations import RecommendationError

# Re-exported so callers/tests do not need to depend on the recommendations
# module for every service-layer failure.
FeedbackError = RecommendationError

VOTES = {"helpful", "unhelpful"}
PREFERENCE_SCOPES = {"temporary", "persistent", "unspecified"}
EXECUTION_STATUSES = {
    "planned",
    "completed",
    "partially_completed",
    "not_completed",
    "no_longer_relevant",
}
TERMINAL_EXECUTION_STATUSES = EXECUTION_STATUSES - {"planned"}
RESULT_RATINGS = {
    "much_better",
    "better",
    "as_expected",
    "worse",
    "much_worse",
    "unclear",
}
CAUSALITY_STATUS = "not_inferred"
OUTCOME_SOURCES = {"user"}
OBSERVATION_SOURCES = {"user_reported"}
OBSERVATION_UNITS = {"count", "ratio", "minutes"}
OBSERVATION_METRICS = {
    "documents_created",
    "project_activities",
    "goals_created",
    "tasks_created",
    "tasks_completed",
    "tasks_reopened",
    "tasks_cancelled",
    "net_completion_ratio",
    "connector_sync_success_rate",
}

MAX_REASON_CODES = 5
MAX_NOTE = 1000
MAX_REASONS_JSON = 3000
MAX_METRICS = 10
MAX_METRICS_JSON = 3000
MAX_ENUM = 40
MAX_VERSION = 50

_EXTRA_SECRET_PATTERNS = (
    re.compile(r"(?i)github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)

HELPFUL_REASONS = {
    "actionable",
    "relevant",
    "evidence_clear",
    "timely",
    "realistic",
    "other",
}
UNHELPFUL_REASONS = {
    "irrelevant",
    "too_obvious",
    "unrealistic",
    "outdated",
    "wrong_evidence",
    "too_risky",
    "not_now",
    "duplicate",
    "other",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是 ISO-8601 时间字符串")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是有效的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_foreign_keys(conn: sqlite3.Connection) -> None:
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if int(enabled) != 1:
        raise FeedbackError(
            "foreign_keys_disabled",
            "SQLite foreign_keys 未启用，拒绝写入反馈/结果数据",
            status_code=500,
        )


def _require_recommendation(conn: sqlite3.Connection, recommendation_id: int) -> sqlite3.Row:
    if isinstance(recommendation_id, bool) or not isinstance(recommendation_id, int) or recommendation_id <= 0:
        raise FeedbackError("invalid_recommendation_id", "建议 ID 必须是正整数", status_code=400)
    row = conn.execute(
        "SELECT * FROM recommendations WHERE id=?", (recommendation_id,)
    ).fetchone()
    if row is None:
        raise FeedbackError("not_found", "建议不存在", status_code=404)
    return row


def _require_accepted_active_recommendation(
    conn: sqlite3.Connection,
    recommendation_id: int,
    *,
    now: datetime | None = None,
) -> sqlite3.Row:
    row = _require_recommendation(conn, recommendation_id)
    if row["status"] != "accepted":
        raise FeedbackError(
            "conflict",
            "只有已采纳的建议可以登记或更新执行结果；采纳不等于已经执行",
            status_code=409,
        )
    expires_at = row["expires_at"]
    if expires_at and str(expires_at) < _iso(now or utc_now()):
        raise FeedbackError("conflict", "建议已过期，不能更新执行结果", status_code=409)
    return row


def _require_no_secret(*texts: str) -> None:
    for text in texts:
        if text and (looks_like_secret(text) or any(pattern.search(text) for pattern in _EXTRA_SECRET_PATTERNS)):
            raise FeedbackError(
                "secret_detected",
                "反馈或结果中疑似包含密钥/API Key/凭据等秘密信息，已拒绝保存",
                status_code=400,
            )


class FeedbackDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote: str
    reason_codes: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=MAX_NOTE)
    preference_scope: str = "unspecified"
    resume_after_at: str | None = None

    @field_validator("vote", "preference_scope")
    @classmethod
    def _validate_enums(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("note")
    @classmethod
    def _strip_note(cls, value: str) -> str:
        return value.strip()

    @field_validator("reason_codes")
    @classmethod
    def _validate_reasons(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_REASON_CODES:
            raise ValueError(f"原因码最多 {MAX_REASON_CODES} 个")
        cleaned = [item.strip().lower() for item in value if item and item.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("原因码不能重复")
        return cleaned

    @model_validator(mode="after")
    def _validate_combination(self) -> FeedbackDraft:
        if self.vote not in VOTES:
            raise ValueError("非法 vote")
        if self.preference_scope not in PREFERENCE_SCOPES:
            raise ValueError("非法 preference_scope")
        allowed = HELPFUL_REASONS if self.vote == "helpful" else UNHELPFUL_REASONS
        invalid = [code for code in self.reason_codes if code not in allowed]
        if invalid:
            raise ValueError(f"原因码与 vote 不匹配：{', '.join(invalid)}")
        resume = _parse_iso(self.resume_after_at, field_name="resume_after_at") if self.resume_after_at else None
        if self.preference_scope == "temporary":
            if resume is None:
                raise ValueError("temporary 偏好必须提供未来的 resume_after_at")
            if resume <= utc_now():
                raise ValueError("resume_after_at 必须是未来时间")
        elif resume is not None:
            raise ValueError("只有 temporary 偏好可以设置 resume_after_at")
        return self


class MetricObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(min_length=1, max_length=80)
    expected_value: int | float | None = None
    observed_value: int | float | None = None
    unit: str
    window: dict[str, Any] = Field(default_factory=dict)
    source: str
    captured_at: str
    attribution: str = CAUSALITY_STATUS

    @field_validator("metric", "unit", "source", "attribution")
    @classmethod
    def _strip_enums(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("expected_value", "observed_value")
    @classmethod
    def _no_bool_number(cls, value: int | float | None) -> int | float | None:
        if isinstance(value, bool):
            raise ValueError("指标值不能是布尔值")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("指标值必须是有限数字")
        return value

    @field_validator("window")
    @classmethod
    def _validate_window(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {"days", "timezone", "start", "end"}
        if set(value) - allowed:
            raise ValueError("window 含非法字段")
        if len(value) > len(allowed):
            raise ValueError("window 字段过多")
        return value

    @model_validator(mode="after")
    def _validate(self) -> MetricObservation:
        if self.metric not in OBSERVATION_METRICS:
            raise ValueError("metric 不在允许名单中")
        if self.unit not in OBSERVATION_UNITS:
            raise ValueError("非法 unit")
        if self.source not in OBSERVATION_SOURCES:
            raise ValueError("非法 source")
        if self.attribution != CAUSALITY_STATUS:
            raise ValueError("attribution 必须是 not_inferred")
        captured_at = _parse_iso(self.captured_at, field_name="captured_at")
        if captured_at is not None and captured_at > utc_now():
            raise ValueError("captured_at 不能是未来时间")
        return self


class OutcomeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_status: str
    result_rating: str = "unclear"
    actual_effect_note: str = Field(default="", max_length=MAX_NOTE)
    metric_observations: list[MetricObservation] = Field(default_factory=list)
    occurred_at: str | None = None
    causality_status: str = CAUSALITY_STATUS
    source: str = "user"

    @field_validator("execution_status", "result_rating", "source", "causality_status")
    @classmethod
    def _strip_enums(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("actual_effect_note")
    @classmethod
    def _strip_note(cls, value: str) -> str:
        return value.strip()

    @field_validator("metric_observations")
    @classmethod
    def _limit_metrics(cls, value: list[MetricObservation]) -> list[MetricObservation]:
        if len(value) > MAX_METRICS:
            raise ValueError(f"metric_observations 最多 {MAX_METRICS} 条")
        return value

    @model_validator(mode="after")
    def _validate(self) -> OutcomeDraft:
        if self.execution_status not in EXECUTION_STATUSES:
            raise ValueError("非法 execution_status")
        if self.result_rating not in RESULT_RATINGS:
            raise ValueError("非法 result_rating")
        if self.source != "user":
            raise ValueError("第一版结果来源只能是 user")
        if self.causality_status != CAUSALITY_STATUS:
            raise ValueError("causality_status 不能被调用方改写")
        parsed_occurred = _parse_iso(self.occurred_at, field_name="occurred_at") if self.occurred_at else None
        if self.execution_status == "planned" and parsed_occurred is not None:
            raise ValueError("planned 状态不能设置 occurred_at")
        if self.execution_status in TERMINAL_EXECUTION_STATUSES and parsed_occurred is not None:
            if parsed_occurred > utc_now():
                raise ValueError("occurred_at 不能是未来时间")
        return self


def _feedback_payload(draft: FeedbackDraft) -> dict[str, Any]:
    reasons_json = json.dumps(draft.reason_codes, ensure_ascii=False, sort_keys=True)
    if len(reasons_json) > MAX_REASONS_JSON:
        raise FeedbackError("reasons_too_large", "原因码数据超过长度上限", status_code=400)
    resume = _parse_iso(draft.resume_after_at, field_name="resume_after_at") if draft.resume_after_at else None
    return {
        "vote": draft.vote,
        "reason_codes_json": reasons_json,
        "note": draft.note,
        "preference_scope": draft.preference_scope,
        "resume_after_at": _iso(resume) if resume else None,
    }


def _outcome_payload(draft: OutcomeDraft, *, current: datetime) -> dict[str, Any]:
    metrics = [item.model_dump() for item in draft.metric_observations]
    metrics_json = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
    if len(metrics_json) > MAX_METRICS_JSON:
        raise FeedbackError("metrics_too_large", "指标观察数据超过长度上限", status_code=400)
    occurred = _parse_iso(draft.occurred_at, field_name="occurred_at") if draft.occurred_at else None
    if draft.execution_status in TERMINAL_EXECUTION_STATUSES and occurred is None:
        occurred = current
    return {
        "execution_status": draft.execution_status,
        "result_rating": draft.result_rating,
        "actual_effect_note": draft.actual_effect_note,
        "metric_observations_json": metrics_json,
        "causality_status": CAUSALITY_STATUS,
        "occurred_at": _iso(occurred) if occurred else None,
        "source": "user",
    }


def _serialize_feedback(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["reason_codes"] = json.loads(item.pop("reason_codes_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        item["reason_codes"] = []
    return item


def _serialize_outcome(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["metric_observations"] = json.loads(item.pop("metric_observations_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        item["metric_observations"] = []
    return item


def _get_feedback_row(conn: sqlite3.Connection, recommendation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM recommendation_feedback WHERE recommendation_id=?",
        (recommendation_id,),
    ).fetchone()


def _get_outcome_row(conn: sqlite3.Connection, recommendation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM recommendation_outcomes WHERE recommendation_id=?",
        (recommendation_id,),
    ).fetchone()


def _existing_feedback_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "vote": row["vote"],
        "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
        "note": row["note"],
        "preference_scope": row["preference_scope"],
        "resume_after_at": row["resume_after_at"],
    }


def _existing_outcome_data(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "execution_status": row["execution_status"],
        "result_rating": row["result_rating"],
        "actual_effect_note": row["actual_effect_note"],
        "metric_observations": json.loads(row["metric_observations_json"] or "[]"),
        "occurred_at": row["occurred_at"],
    }


def _validate_feedback_data(data: FeedbackDraft | dict[str, Any], existing: dict[str, Any] | None) -> FeedbackDraft:
    if isinstance(data, FeedbackDraft):
        return data
    if not isinstance(data, dict):
        raise FeedbackError("invalid_feedback", "反馈数据必须是对象", status_code=400)
    merged = dict(existing or {})
    merged.update(data)
    try:
        return FeedbackDraft.model_validate(merged)
    except ValidationError as exc:
        raise exc


def _validate_outcome_data(data: OutcomeDraft | dict[str, Any], existing: dict[str, Any] | None) -> OutcomeDraft:
    if isinstance(data, OutcomeDraft):
        return data
    if not isinstance(data, dict):
        raise FeedbackError("invalid_outcome", "结果数据必须是对象", status_code=400)
    merged = dict(existing or {})
    merged.update(data)
    try:
        return OutcomeDraft.model_validate(merged)
    except ValidationError as exc:
        raise exc


def get_feedback(conn: sqlite3.Connection, recommendation_id: int) -> dict[str, Any] | None:
    row = _get_feedback_row(conn, recommendation_id)
    return _serialize_feedback(row) if row is not None else None


def get_outcome(conn: sqlite3.Connection, recommendation_id: int) -> dict[str, Any] | None:
    row = _get_outcome_row(conn, recommendation_id)
    return _serialize_outcome(row) if row is not None else None


def upsert_feedback(
    conn: sqlite3.Connection,
    recommendation_id: int,
    data: FeedbackDraft | dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_foreign_keys(conn)
    _require_recommendation(conn, recommendation_id)
    current_dt = now or utc_now()
    current = _iso(current_dt)
    existing_row = _get_feedback_row(conn, recommendation_id)
    existing_data = _existing_feedback_data(existing_row) if existing_row else None
    draft = _validate_feedback_data(data, existing_data)
    payload = _feedback_payload(draft)
    _require_no_secret(payload["note"], payload["reason_codes_json"])

    if existing_row is None:
        try:
            cur = conn.execute(
                "INSERT INTO recommendation_feedback(recommendation_id,vote,reason_codes_json,note,"
                "preference_scope,resume_after_at,source,created_at,updated_at) "
                "SELECT ?,?,?,?,?,?,?,?,? "
                "WHERE EXISTS (SELECT 1 FROM recommendations WHERE id=?)",
                (
                    recommendation_id,
                    payload["vote"],
                    payload["reason_codes_json"],
                    payload["note"],
                    payload["preference_scope"],
                    payload["resume_after_at"],
                    "user",
                    current,
                    current,
                    recommendation_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            winner = _get_feedback_row(conn, recommendation_id)
            if winner is not None:
                raise FeedbackError("conflict", "反馈已由并发请求创建，请刷新后重试", status_code=409) from exc
            _require_recommendation(conn, recommendation_id)
            raise
        if cur.rowcount == 0:
            _require_recommendation(conn, recommendation_id)
            raise FeedbackError("conflict", "建议状态已变化，请刷新后重试", status_code=409)
        feedback_id = int(cur.lastrowid)
        status = "created"
    else:
        feedback_id = int(existing_row["id"])
        cur = conn.execute(
            "UPDATE recommendation_feedback SET vote=?, reason_codes_json=?, note=?, preference_scope=?, "
            "resume_after_at=?, updated_at=? "
            "WHERE id=? AND EXISTS ("
            "SELECT 1 FROM recommendations r WHERE r.id=recommendation_feedback.recommendation_id"
            ")",
            (
                payload["vote"],
                payload["reason_codes_json"],
                payload["note"],
                payload["preference_scope"],
                payload["resume_after_at"],
                current,
                feedback_id,
            ),
        )
        if cur.rowcount == 0:
            _require_recommendation(conn, recommendation_id)
            if _get_feedback_row(conn, recommendation_id) is None:
                raise FeedbackError("not_found", "反馈不存在", status_code=404)
            raise FeedbackError("conflict", "建议状态已变化，请刷新后重试", status_code=409)
        status = "updated"

    behavior_events.record_event(
        conn,
        "recommendation.feedback_saved",
        subject_type="recommendation_feedback",
        subject_id=feedback_id,
        source_type="system",
        metadata={
            "recommendation_id": recommendation_id,
            "vote": payload["vote"],
            "reason_count": len(draft.reason_codes),
            "preference_scope": payload["preference_scope"],
        },
    )
    return {"status": status, "feedback": _serialize_feedback(_get_feedback_row(conn, recommendation_id))}


def upsert_outcome(
    conn: sqlite3.Connection,
    recommendation_id: int,
    data: OutcomeDraft | dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_foreign_keys(conn)
    current_dt = now or utc_now()
    _require_accepted_active_recommendation(conn, recommendation_id, now=current_dt)
    current = _iso(current_dt)
    existing_row = _get_outcome_row(conn, recommendation_id)
    existing_data = _existing_outcome_data(existing_row) if existing_row else None
    draft = _validate_outcome_data(data, existing_data)
    payload = _outcome_payload(draft, current=current_dt)
    _require_no_secret(payload["actual_effect_note"], payload["metric_observations_json"])

    if existing_row is None:
        try:
            cur = conn.execute(
                "INSERT INTO recommendation_outcomes(recommendation_id,execution_status,result_rating,"
                "actual_effect_note,metric_observations_json,causality_status,occurred_at,source,"
                "created_at,updated_at) "
                "SELECT ?,?,?,?,?,?,?,?,?,? "
                "WHERE EXISTS ("
                "SELECT 1 FROM recommendations WHERE id=? AND status='accepted' "
                "AND (expires_at IS NULL OR expires_at>=?)"
                ")",
                (
                    recommendation_id,
                    payload["execution_status"],
                    payload["result_rating"],
                    payload["actual_effect_note"],
                    payload["metric_observations_json"],
                    CAUSALITY_STATUS,
                    payload["occurred_at"],
                    "user",
                    current,
                    current,
                    recommendation_id,
                    current,
                ),
            )
        except sqlite3.IntegrityError as exc:
            winner = _get_outcome_row(conn, recommendation_id)
            if winner is not None:
                raise FeedbackError("conflict", "结果已由并发请求创建，请刷新后重试", status_code=409) from exc
            _require_accepted_active_recommendation(conn, recommendation_id, now=current_dt)
            raise
        if cur.rowcount == 0:
            _require_accepted_active_recommendation(conn, recommendation_id, now=current_dt)
            raise FeedbackError("conflict", "建议状态已变化，请刷新后重试", status_code=409)
        outcome_id = int(cur.lastrowid)
        status = "created"
    else:
        outcome_id = int(existing_row["id"])
        cur = conn.execute(
            "UPDATE recommendation_outcomes SET execution_status=?, result_rating=?, actual_effect_note=?, "
            "metric_observations_json=?, causality_status=?, occurred_at=?, updated_at=? "
            "WHERE id=? AND EXISTS ("
            "SELECT 1 FROM recommendations r WHERE r.id=recommendation_outcomes.recommendation_id "
            "AND r.status='accepted' AND (r.expires_at IS NULL OR r.expires_at>=?)"
            ")",
            (
                payload["execution_status"],
                payload["result_rating"],
                payload["actual_effect_note"],
                payload["metric_observations_json"],
                CAUSALITY_STATUS,
                payload["occurred_at"],
                current,
                outcome_id,
                current,
            ),
        )
        if cur.rowcount == 0:
            _require_accepted_active_recommendation(conn, recommendation_id, now=current_dt)
            if _get_outcome_row(conn, recommendation_id) is None:
                raise FeedbackError("not_found", "结果不存在", status_code=404)
            raise FeedbackError("conflict", "建议状态已变化，请刷新后重试", status_code=409)
        status = "updated"

    # Re-check inside the same writer transaction before appending an event.
    # If a competing status change or delete races the write predicate, raise
    # before recording the outcome event; the caller's transaction rolls back.
    _require_accepted_active_recommendation(conn, recommendation_id, now=current_dt)

    behavior_events.record_event(
        conn,
        "recommendation.outcome_saved",
        subject_type="recommendation_outcome",
        subject_id=outcome_id,
        source_type="system",
        metadata={
            "recommendation_id": recommendation_id,
            "execution_status": payload["execution_status"],
            "result_rating": payload["result_rating"],
            "metric_count": len(draft.metric_observations),
            "causality_status": CAUSALITY_STATUS,
        },
    )
    return {"status": status, "outcome": _serialize_outcome(_get_outcome_row(conn, recommendation_id))}


def delete_feedback(conn: sqlite3.Connection, recommendation_id: int) -> None:
    _require_foreign_keys(conn)
    row = _get_feedback_row(conn, recommendation_id)
    if row is None:
        # Still distinguish an unknown recommendation from an absent feedback row.
        _require_recommendation(conn, recommendation_id)
        raise FeedbackError("not_found", "反馈不存在", status_code=404)
    feedback_id = int(row["id"])
    cur = conn.execute(
        "DELETE FROM recommendation_feedback WHERE id=? AND EXISTS ("
        "SELECT 1 FROM recommendations r WHERE r.id=recommendation_feedback.recommendation_id"
        ")",
        (feedback_id,),
    )
    if cur.rowcount == 0:
        _require_recommendation(conn, recommendation_id)
        if _get_feedback_row(conn, recommendation_id) is None:
            raise FeedbackError("not_found", "反馈不存在", status_code=404)
        raise FeedbackError("conflict", "建议状态已变化，请刷新后重试", status_code=409)
    # A trigger or competing statement in the same writer transaction can delete
    # the parent as part of this DELETE; fail before appending an orphan event.
    _require_recommendation(conn, recommendation_id)

    behavior_events.record_event(
        conn,
        "recommendation.feedback_deleted",
        subject_type="recommendation_feedback",
        subject_id=feedback_id,
        source_type="system",
        metadata={"recommendation_id": recommendation_id},
    )


def delete_outcome(conn: sqlite3.Connection, recommendation_id: int) -> None:
    _require_foreign_keys(conn)
    _require_accepted_active_recommendation(conn, recommendation_id)
    row = _get_outcome_row(conn, recommendation_id)
    if row is None:
        raise FeedbackError("not_found", "结果不存在", status_code=404)
    outcome_id = int(row["id"])
    cur = conn.execute(
        "DELETE FROM recommendation_outcomes WHERE id=? AND EXISTS ("
        "SELECT 1 FROM recommendations r WHERE r.id=recommendation_outcomes.recommendation_id "
        "AND r.status='accepted' AND (r.expires_at IS NULL OR r.expires_at>=?)"
        ")",
        (outcome_id, _iso(utc_now())),
    )
    if cur.rowcount == 0:
        _require_accepted_active_recommendation(conn, recommendation_id)
        if _get_outcome_row(conn, recommendation_id) is None:
            raise FeedbackError("not_found", "结果不存在", status_code=404)
        raise FeedbackError("conflict", "建议状态已变化，请刷新后重试", status_code=409)
    # Re-check before appending an event. A BEFORE trigger can move a competing
    # recommendation out of accepted state while this DELETE is executing.
    _require_accepted_active_recommendation(conn, recommendation_id)

    behavior_events.record_event(
        conn,
        "recommendation.outcome_deleted",
        subject_type="recommendation_outcome",
        subject_id=outcome_id,
        source_type="system",
        metadata={"recommendation_id": recommendation_id},
    )
