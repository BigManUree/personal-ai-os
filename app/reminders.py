"""Persistent reminder domain layer for Personal AI OS 1.3.5-B.

No scheduler, HTTP route, background thread, LLM call, or notification lives
here. Rule functions accept an explicit reference time for deterministic tests.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.analytics import get_timezone

RULE_VERSION = "1.3.5-B@1"
RULE_TASK_DUE_SOON = "task.due_soon_v1"
RULE_TASK_OVERDUE = "task.overdue_v1"
RULE_TASK_STALLED = "task.stalled_doing_v1"
RULE_RECOMMENDATION_FOLLOWUP = "recommendation.followup_v1"
RULE_REVIEW_DAILY = "review.daily_due_v1"
RULE_REVIEW_WEEKLY = "review.weekly_due_v1"
RULE_CODES = frozenset({RULE_TASK_DUE_SOON, RULE_TASK_OVERDUE, RULE_TASK_STALLED,
                        RULE_RECOMMENDATION_FOLLOWUP, RULE_REVIEW_DAILY, RULE_REVIEW_WEEKLY})
SOURCE_TASK = "task"
SOURCE_RECOMMENDATION = "recommendation"
SOURCE_REVIEW = "review"
SOURCE_TYPES = frozenset({SOURCE_TASK, SOURCE_RECOMMENDATION, SOURCE_REVIEW})
PRIORITIES = frozenset({"low", "normal", "high"})
STATUS_SCHEDULED = "scheduled"
STATUS_DUE = "due"
STATUS_SNOOZED = "snoozed"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_DISMISSED = "dismissed"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUSES = frozenset({STATUS_SCHEDULED, STATUS_DUE, STATUS_SNOOZED, STATUS_ACKNOWLEDGED,
                      STATUS_DISMISSED, STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED})
TERMINAL_STATUSES = frozenset({STATUS_DISMISSED, STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED})
ACTIVE_STATUSES = (STATUS_SCHEDULED, STATUS_DUE, STATUS_SNOOZED)
CANCEL_REASONS = frozenset({
    "source_completed", "source_cancelled", "source_deleted", "source_rescheduled",
    "recommendation_closed", "outcome_registered", "rule_disabled", "settings_disabled",
    "superseded_by_new_schedule", "expiry_reached", "user_cancelled",
})
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "mark_due": (STATUS_SCHEDULED, STATUS_SNOOZED),
    "acknowledge": (STATUS_DUE, STATUS_ACKNOWLEDGED),
    "snooze": (STATUS_DUE, STATUS_SNOOZED),
    "dismiss": (STATUS_DUE, STATUS_SNOOZED, STATUS_ACKNOWLEDGED, STATUS_DISMISSED),
    "complete": (STATUS_DUE, STATUS_SNOOZED, STATUS_ACKNOWLEDGED, STATUS_COMPLETED),
    "cancel": (STATUS_SCHEDULED, STATUS_DUE, STATUS_SNOOZED, STATUS_ACKNOWLEDGED, STATUS_CANCELLED),
    "expire": (STATUS_SCHEDULED, STATUS_DUE, STATUS_SNOOZED, STATUS_EXPIRED),
}
MAX_TITLE = 200
MAX_CONTENT = 1000
MAX_RULE_CODE = 80
MAX_VERSION = 50
MAX_EVIDENCE_LABEL = 200
MAX_EVIDENCE_DETAIL = 300
MAX_SNOOZE_MINUTES = 7 * 24 * 60
MAX_EVIDENCE_VALUE = 200
MAX_EVIDENCE_REF = 80
MAX_EVIDENCE_JSON = 3000
MAX_EVIDENCE_ITEMS = 10
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_DAILY_AT = "20:00"
DEFAULT_WEEKLY_AT = "09:00"
DEFAULT_WEEKLY_WEEKDAY = 1
OVERDUE_REPEAT_DAYS = 3
OVERDUE_MAX_OCCURRENCES = 2
OVERDUE_EXPIRY_DAYS = 7
STALLED_AFTER_DAYS = 3
STALLED_REPEAT_DAYS = 7
STALLED_MAX_OCCURRENCES = 2
STALLED_EXPIRY_DAYS = 30
FOLLOWUP_DAYS = (7, 14)
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*[A-Za-z0-9_./+=-]{8,}")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReminderError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | datetime | None, *, field_name: str = "time") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        if not isinstance(value, str) or not value.strip():
            raise ReminderError("invalid_time", f"{field_name} 必须是 ISO-8601 时间")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReminderError("invalid_time", f"{field_name} 必须是有效的 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_date(value: str | date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ReminderError("invalid_date", "日期必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReminderError("invalid_date", "日期必须是有效的 YYYY-MM-DD") from exc


def parse_hhmm(value: str) -> time:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ReminderError("invalid_time", "时间必须是 HH:MM")
    return time.fromisoformat(value)


def validate_timezone_name(value: str) -> str:
    value = (value or "").strip()
    try:
        get_timezone(value)
    except ValueError as exc:
        raise ReminderError("invalid_timezone", str(exc)) from exc
    return value


def _no_secret(*texts: Any) -> None:
    for text in texts:
        if isinstance(text, str) and _SECRET_RE.search(text):
            raise ReminderError("secret_detected", "内容中疑似包含密钥或令牌，请移除后再保存")


def _foreign_keys_on(conn: sqlite3.Connection) -> None:
    if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ReminderError("database_configuration", "SQLite 外键约束未启用", status_code=500)

# ---------- models ----------

class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    task_due_enabled: bool | None = None
    task_stalled_enabled: bool | None = None
    recommendation_followup_enabled: bool | None = None
    daily_review_enabled: bool | None = None
    weekly_review_enabled: bool | None = None
    daily_review_at: str | None = None
    weekly_review_weekday: int | None = Field(default=None, ge=1, le=7)
    weekly_review_at: str | None = None
    quiet_hours_enabled: bool | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return validate_timezone_name(value) if value is not None else None

    @field_validator("daily_review_at", "weekly_review_at", "quiet_start", "quiet_end")
    @classmethod
    def _clock(cls, value: str | None) -> str | None:
        if value is not None:
            parse_hhmm(value)
        return value


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(metric|task|recommendation|review_window)$")
    label: str = Field(min_length=1, max_length=MAX_EVIDENCE_LABEL)
    value: str = Field(min_length=1, max_length=MAX_EVIDENCE_VALUE)
    source_type: str = Field(min_length=1, max_length=MAX_EVIDENCE_REF)
    source_id: int | None = Field(default=None, gt=0)
    window_start: datetime | None = None
    window_end: datetime | None = None
    coverage_status: str = Field(default="ok", pattern=r"^(ok|partial_data|insufficient_data|no_data)$")
    detail: str = Field(default="", max_length=MAX_EVIDENCE_DETAIL)
    definition_version: str = Field(min_length=1, max_length=MAX_VERSION)

    @model_validator(mode="after")
    def _window(self) -> EvidenceItem:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("统计窗口必须同时提供开始和结束时间")
        if self.window_start and self.window_end and self.window_end <= self.window_start:
            raise ValueError("统计窗口必须满足 start < end")
        _no_secret(self.label, self.value, self.detail, self.source_type)
        return self


class ReminderDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_code: str = Field(min_length=1, max_length=MAX_RULE_CODE)
    rule_version: str = Field(default=RULE_VERSION, min_length=1, max_length=MAX_VERSION)
    source_type: str
    source_id: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    priority: str = "normal"
    status: str = STATUS_SCHEDULED
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=MAX_EVIDENCE_ITEMS)
    coverage_status: str = "ok"
    dedup_key: str = Field(min_length=1, max_length=300)
    scheduled_at: datetime
    snoozed_until: datetime | None = None
    expires_at: datetime
    due_at: datetime | None = None
    occurrence_index: int = Field(default=1, ge=1, le=100)
    max_attempts: int = Field(default=3, ge=1, le=10)
    cancel_reason: str | None = None

    @field_validator("rule_code")
    @classmethod
    def _rule(cls, value: str) -> str:
        if value not in RULE_CODES:
            raise ValueError(f"未知提醒规则：{value}")
        return value

    @field_validator("source_type")
    @classmethod
    def _source(cls, value: str) -> str:
        if value not in SOURCE_TYPES:
            raise ValueError(f"未知提醒来源：{value}")
        return value

    @field_validator("priority")
    @classmethod
    def _priority(cls, value: str) -> str:
        if value not in PRIORITIES:
            raise ValueError("非法优先级")
        return value

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        if value not in {STATUS_SCHEDULED, STATUS_DUE}:
            raise ValueError("新建提醒只能是 scheduled 或 due")
        return value

    @field_validator("coverage_status")
    @classmethod
    def _coverage(cls, value: str) -> str:
        if value not in {"ok", "partial_data", "insufficient_data", "no_data"}:
            raise ValueError("非法覆盖状态")
        return value

    @field_validator("cancel_reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        if value is not None and value not in CANCEL_REASONS:
            raise ValueError("非法取消原因")
        return value

    @model_validator(mode="after")
    def _times_and_source(self) -> ReminderDraft:
        if self.expires_at <= self.scheduled_at:
            raise ValueError("过期时间必须晚于计划展示时间")
        if self.source_type in {SOURCE_TASK, SOURCE_RECOMMENDATION} and self.source_id is None:
            raise ValueError("任务和建议提醒必须提供来源 ID")
        if self.source_type == SOURCE_REVIEW and self.source_id is not None:
            raise ValueError("复盘提醒不能绑定任务或建议 ID")
        if self.status == STATUS_DUE and self.due_at is None:
            raise ValueError("due 状态必须提供 due_at")
        _no_secret(self.title, self.content, self.dedup_key)
        return self


@dataclass(frozen=True)
class RuleCandidate:
    rule_code: str
    source_type: str
    source_id: int | None
    title: str
    content: str
    priority: str
    scheduled_at: datetime
    expires_at: datetime
    dedup_key: str
    evidence: list[dict[str, Any]]
    coverage_status: str
    occurrence_index: int = 1

    def to_draft(self) -> ReminderDraft:
        return ReminderDraft(
            rule_code=self.rule_code, source_type=self.source_type, source_id=self.source_id,
            title=self.title, content=self.content, priority=self.priority, evidence=self.evidence,
            coverage_status=self.coverage_status, dedup_key=self.dedup_key,
            scheduled_at=self.scheduled_at, expires_at=self.expires_at,
            occurrence_index=self.occurrence_index,
        )

# ---------- settings ----------

SETTINGS_FIELDS = (
    "timezone", "task_due_enabled", "task_stalled_enabled",
    "recommendation_followup_enabled", "daily_review_enabled", "weekly_review_enabled",
    "daily_review_at", "weekly_review_weekday", "weekly_review_at",
    "quiet_hours_enabled", "quiet_start", "quiet_end",
)
_BOOL_KEYS = {"task_due_enabled", "task_stalled_enabled", "recommendation_followup_enabled",
              "daily_review_enabled", "weekly_review_enabled", "quiet_hours_enabled"}


def _settings_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = {key: row[key] for key in SETTINGS_FIELDS}
    for key in _BOOL_KEYS:
        data[key] = bool(data[key])
    data["created_at"] = row["created_at"]
    data["updated_at"] = row["updated_at"]
    return data


def get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM reminder_settings WHERE id=1").fetchone()
    if row is None:
        raise ReminderError("not_found", "提醒设置不存在", status_code=404)
    return _settings_dict(row)


def update_settings(conn: sqlite3.Connection, changes: dict[str, Any] | BaseModel, *,
                    now: datetime | None = None) -> dict[str, Any]:
    current = get_settings(conn)
    if isinstance(changes, BaseModel):
        payload = changes.model_dump(exclude_none=True)
    else:
        payload = dict(changes or {})
    unknown = set(payload) - set(SETTINGS_FIELDS)
    if unknown:
        raise ReminderError("invalid_settings", f"不允许修改字段：{sorted(unknown)}")
    try:
        validated = SettingsUpdate.model_validate(payload).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ReminderError("invalid_settings", "提醒设置不合法") from exc
    merged = {**current, **validated}
    validate_timezone_name(str(merged["timezone"]))
    for field in ("daily_review_at", "weekly_review_at", "quiet_start", "quiet_end"):
        parse_hhmm(str(merged[field]))
    if bool(merged["quiet_hours_enabled"]) and merged["quiet_start"] == merged["quiet_end"]:
        raise ReminderError("invalid_settings", "安静时段开始时间和结束时间不能相同")
    if not 1 <= int(merged["weekly_review_weekday"]) <= 7:
        raise ReminderError("invalid_settings", "每周复盘星期必须在 1..7")
    timestamp = iso(now or utc_now())
    values = [int(merged[key]) if key in _BOOL_KEYS else merged[key] for key in SETTINGS_FIELDS]
    assignments = ", ".join(f"{key}=?" for key in SETTINGS_FIELDS)
    cur = conn.execute(
        f"UPDATE reminder_settings SET {assignments}, updated_at=? WHERE id=1",
        [*values, timestamp],
    )
    if cur.rowcount != 1:
        raise ReminderError("storage_conflict", "提醒设置更新失败", status_code=409)
    return get_settings(conn)


# ---------- deterministic rules ----------

def _tzinfo(settings: dict[str, Any]):
    return get_timezone(str(settings.get("timezone", DEFAULT_TIMEZONE)))


def _local_dt(day: date, clock: str, tzname: str) -> datetime:
    return datetime.combine(day, parse_hhmm(clock), tzinfo=get_timezone(tzname))


def _evidence(label: str, value: str, kind: str, source_type: str, source_id: int | None,
              detail: str, coverage: str, window: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    return {
        "kind": kind, "label": label, "value": value, "source_type": source_type,
        "source_id": source_id,
        "window_start": iso(window[0]) if window else None,
        "window_end": iso(window[1]) if window else None,
        "coverage_status": coverage, "detail": detail, "definition_version": RULE_VERSION,
    }


def task_due_soon_candidate(task: dict[str, Any], settings: dict[str, Any], *,
                            now: datetime) -> RuleCandidate | None:
    if not settings.get("task_due_enabled") or not task.get("due_date") or task.get("status") in {"done", "cancelled"}:
        return None
    due = parse_date(task["due_date"])
    tzname = str(settings["timezone"])
    scheduled = _local_dt(due - timedelta(days=1), "09:00", tzname)
    expires = _local_dt(due, "23:59", tzname)
    if not scheduled <= now < expires:
        return None
    task_id = int(task["id"])
    return RuleCandidate(RULE_TASK_DUE_SOON, SOURCE_TASK, task_id, "任务即将到期",
                         f"任务将于 {due.isoformat()} 到期，请安排时间处理。", "normal",
                         scheduled.astimezone(timezone.utc), expires.astimezone(timezone.utc),
                         f"{RULE_TASK_DUE_SOON}:{task_id}:{due.isoformat()}",
                         [_evidence("任务截止日期", due.isoformat(), "task", "tasks", task_id,
                                    "日期型截止时间，按本地 09:00 提醒", "ok")], "ok")


def task_overdue_candidate(task: dict[str, Any], settings: dict[str, Any], *, now: datetime,
                           occurrence: int = 1) -> RuleCandidate | None:
    if not settings.get("task_due_enabled") or occurrence < 1 or occurrence > OVERDUE_MAX_OCCURRENCES:
        return None
    if not task.get("due_date") or task.get("status") in {"done", "cancelled"}:
        return None
    due = parse_date(task["due_date"])
    tzname = str(settings["timezone"])
    first_day = due + timedelta(days=1)
    occurrence_day = first_day + timedelta(days=(occurrence - 1) * OVERDUE_REPEAT_DAYS)
    scheduled = _local_dt(occurrence_day, "09:00", tzname)
    expires = _local_dt(due + timedelta(days=OVERDUE_EXPIRY_DAYS), "23:59", tzname)
    if not scheduled <= now < expires:
        return None
    task_id = int(task["id"])
    return RuleCandidate(RULE_TASK_OVERDUE, SOURCE_TASK, task_id, "任务已超过截止日期",
                         f"任务截止日期 {due.isoformat()} 已过，请更新状态或重新安排日期。", "high",
                         scheduled.astimezone(timezone.utc), expires.astimezone(timezone.utc),
                         f"{RULE_TASK_OVERDUE}:{task_id}:{due.isoformat()}:{occurrence}",
                         [_evidence("任务截止日期", due.isoformat(), "task", "tasks", task_id,
                                    f"第 {occurrence} 次逾期提醒", "ok")], "ok", occurrence)


def task_stalled_candidate(task: dict[str, Any], settings: dict[str, Any], *, now: datetime,
                           last_activity_at: datetime | None = None, occurrence: int = 1) -> RuleCandidate | None:
    if not settings.get("task_stalled_enabled") or task.get("status") != "doing":
        return None
    if occurrence < 1 or occurrence > STALLED_MAX_OCCURRENCES:
        return None
    updated = parse_iso(last_activity_at or task.get("updated_at"), field_name="updated_at")
    threshold_days = STALLED_AFTER_DAYS + (occurrence - 1) * STALLED_REPEAT_DAYS
    age = now - updated
    if age < timedelta(days=threshold_days):
        return None
    tz = _tzinfo(settings)
    scheduled = _local_dt(updated.astimezone(tz).date() + timedelta(days=threshold_days), "10:00",
                          str(settings["timezone"]))
    expires = updated + timedelta(days=STALLED_EXPIRY_DAYS)
    if not scheduled <= now < expires:
        return None
    task_id = int(task["id"])
    iso_year, iso_week, _ = now.astimezone(tz).isocalendar()
    return RuleCandidate(RULE_TASK_STALLED, SOURCE_TASK, task_id, "进行中任务暂未观测到更新",
                         "系统已有一段时间未观测到该进行中任务的记录更新；这不等同于你没有线下行动。",
                         "normal", scheduled.astimezone(timezone.utc), expires,
                         f"{RULE_TASK_STALLED}:{task_id}:{iso_year}-W{iso_week:02d}:{occurrence}",
                         [_evidence("未观测到记录更新天数", str(age.days), "task", "tasks", task_id,
                                    "仅依据系统记录；不推断用户动机或线下行为", "partial_data",
                                    (updated, now))], "partial_data", occurrence)

def recommendation_followup_candidate(recommendation: dict[str, Any], settings: dict[str, Any], *,
                                      now: datetime, has_outcome: bool,
                                      accepted_at: datetime | None = None,
                                      occurrence: int = 1) -> RuleCandidate | None:
    if not settings.get("recommendation_followup_enabled") or has_outcome or occurrence not in (1, 2):
        return None
    if recommendation.get("status") != "accepted":
        return None
    expires_at = parse_iso(recommendation["expires_at"], field_name="expires_at")
    if now >= expires_at:
        return None
    accepted = parse_iso(accepted_at or recommendation.get("reviewed_at") or recommendation.get("updated_at"),
                         field_name="accepted_at")
    day_offset = FOLLOWUP_DAYS[occurrence - 1]
    scheduled = (accepted + timedelta(days=day_offset)).astimezone(_tzinfo(settings)).replace(
        hour=19, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    expires = min(scheduled + timedelta(days=7), expires_at)
    if not scheduled <= now < expires:
        return None
    rec_id = int(recommendation["id"])
    return RuleCandidate(RULE_RECOMMENDATION_FOLLOWUP, SOURCE_RECOMMENDATION, rec_id, "建议跟进",
                         "这条建议已被采纳。若已经执行，可以登记结果；没有登记不代表没有执行。",
                         "low", scheduled, expires,
                         f"{RULE_RECOMMENDATION_FOLLOWUP}:{rec_id}:{accepted.date().isoformat()}:{occurrence}",
                         [_evidence(f"采纳后第 {day_offset} 天", str(day_offset), "recommendation",
                                    "recommendations", rec_id,
                                    "无结果记录只表示未登记，不表示未执行", "partial_data",
                                    (accepted, now))], "partial_data", occurrence)


def review_period_for(kind: str, settings: dict[str, Any], *, now: datetime) -> tuple[date, date, date]:
    tz = _tzinfo(settings)
    local_now = now.astimezone(tz)
    if kind == "daily":
        period_end = local_now.date()
        return period_end - timedelta(days=1), period_end, period_end
    if kind == "weekly":
        this_monday = local_now.date() - timedelta(days=local_now.date().isoweekday() - 1)
        return this_monday - timedelta(days=7), this_monday, this_monday
    raise ReminderError("invalid_review_kind", "复盘类型必须是 daily 或 weekly")


def review_candidate(kind: str, settings: dict[str, Any], *, now: datetime,
                     period_start: date, period_end: date) -> RuleCandidate | None:
    """Build a review reminder for the half-open window [period_start, period_end).

    For a daily reminder period_end is the day after the reviewed local day;
    the reminder itself is scheduled on period_end. For a weekly reminder
    period_end is Monday, which is both the exclusive window end and schedule day.
    """
    if kind == "daily":
        if not settings.get("daily_review_enabled"):
            return None
        rule = RULE_REVIEW_DAILY
        title = "每日复盘"
        content = "可以回顾上一个本地自然日的记录、进展和阻碍。"
        scheduled_day = period_end
        scheduled = _local_dt(scheduled_day, str(settings.get("daily_review_at", DEFAULT_DAILY_AT)),
                              str(settings["timezone"]))
        expiry = scheduled + timedelta(hours=2)
    elif kind == "weekly":
        if not settings.get("weekly_review_enabled"):
            return None
        rule = RULE_REVIEW_WEEKLY
        title = "每周复盘"
        content = "可以回顾上一个完整 ISO 周的记录、进展和阻碍。"
        scheduled = _local_dt(period_end, str(settings.get("weekly_review_at", DEFAULT_WEEKLY_AT)),
                              str(settings["timezone"]))
        expiry = scheduled + timedelta(hours=24)
    else:
        return None
    if not scheduled <= now < expiry:
        return None
    tz = _tzinfo(settings)
    window = (
        datetime.combine(period_start, time.min, tzinfo=tz),
        datetime.combine(period_end, time.min, tzinfo=tz),
    )
    start_utc = iso(window[0])
    return RuleCandidate(rule, SOURCE_REVIEW, None, title, content, "normal",
                         scheduled.astimezone(timezone.utc), expiry.astimezone(timezone.utc),
                         f"{rule}:{start_utc}",
                         [_evidence("复盘统计窗口", f"{period_start.isoformat()}..{period_end.isoformat()}",
                                    "review_window", "review_period", None,
                                    "半开区间 [start,end)；本批不生成报告", "no_data", window)],
                         "no_data")

# ---------- persistence ----------

def _dump_evidence(items: list[dict[str, Any]] | list[EvidenceItem]) -> str:
    normalized = [item.model_dump(mode="json") if isinstance(item, EvidenceItem) else item for item in items]
    try:
        validated = [EvidenceItem.model_validate(item).model_dump(mode="json") for item in normalized]
    except ValidationError as exc:
        raise ReminderError("invalid_evidence", "提醒证据不合法") from exc
    raw = json.dumps(validated, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(raw.encode("utf-8")) > MAX_EVIDENCE_JSON:
        raise ReminderError("evidence_too_large", "提醒证据摘要过长")
    return raw


def _serialize(row: sqlite3.Row) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    data["evidence"] = json.loads(data.pop("evidence_json"))
    return data


def _row(conn: sqlite3.Connection, reminder_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
    if row is None:
        raise ReminderError("not_found", "提醒不存在", status_code=404)
    return row


def get_reminder(conn: sqlite3.Connection, reminder_id: int) -> dict[str, Any]:
    return _serialize(_row(conn, reminder_id))


def get_reminder_by_dedup(conn: sqlite3.Connection, dedup_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM reminders WHERE dedup_key=?", (dedup_key,)).fetchone()
    return _serialize(row) if row else None


def list_reminders(conn: sqlite3.Connection, *, status: str | None = None,
                   source_type: str | None = None, source_id: int | None = None,
                   limit: int = 50, offset: int = 0,
                   include_terminal: bool = False) -> list[dict[str, Any]]:
    if not 1 <= limit <= 100 or offset < 0:
        raise ReminderError("invalid_paging", "分页参数不合法")
    where: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in STATUSES:
            raise ReminderError("invalid_status", f"非法状态：{status}")
        where.append("status=?")
        params.append(status)
    elif not include_terminal:
        marks = ",".join("?" for _ in TERMINAL_STATUSES)
        where.append(f"status NOT IN ({marks})")
        params.extend(sorted(TERMINAL_STATUSES))
    if source_type is not None:
        if source_type not in SOURCE_TYPES:
            raise ReminderError("invalid_source", "非法来源类型")
        where.append("source_type=?")
        params.append(source_type)
    if source_id is not None:
        if source_id <= 0:
            raise ReminderError("invalid_source", "来源 ID 不合法")
        where.append("source_id=?")
        params.append(source_id)
    sql = "SELECT * FROM reminders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scheduled_at DESC, id DESC LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    return [_serialize(row) for row in conn.execute(sql, params).fetchall()]


def _validate_draft(draft: ReminderDraft | dict[str, Any]) -> ReminderDraft:
    try:
        return draft if isinstance(draft, ReminderDraft) else ReminderDraft.model_validate(draft)
    except ValidationError as exc:
        raise ReminderError("invalid_reminder", "提醒数据不合法") from exc


def _require_source_exists(conn: sqlite3.Connection, draft: ReminderDraft) -> None:
    if draft.source_type == SOURCE_TASK:
        exists = conn.execute("SELECT 1 FROM tasks WHERE id=?", (draft.source_id,)).fetchone()
    elif draft.source_type == SOURCE_RECOMMENDATION:
        exists = conn.execute("SELECT 1 FROM recommendations WHERE id=?", (draft.source_id,)).fetchone()
    else:
        return
    if exists is None:
        raise ReminderError("source_not_found", "提醒来源不存在", status_code=404)




@dataclass(frozen=True)
class CreateReminderResult:
    reminder: dict[str, Any]
    created: bool


def _insert_reminder(conn: sqlite3.Connection, validated: ReminderDraft, ts: str):
    task_id = validated.source_id if validated.source_type == SOURCE_TASK else None
    rec_id = validated.source_id if validated.source_type == SOURCE_RECOMMENDATION else None
    return conn.execute(
        "INSERT INTO reminders(rule_code,rule_version,source_type,source_id,task_id,recommendation_id,"
        "title,content,priority,status,evidence_json,coverage_status,dedup_key,scheduled_at,due_at,"
        "snoozed_until,expires_at,acknowledged_at,completed_at,cancelled_at,cancel_reason,"
        "occurrence_index,attempt_count,max_attempts,last_error,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (validated.rule_code, validated.rule_version, validated.source_type, validated.source_id,
         task_id, rec_id, validated.title, validated.content, validated.priority, validated.status,
         _dump_evidence(validated.evidence), validated.coverage_status, validated.dedup_key,
         iso(validated.scheduled_at), iso(validated.due_at) if validated.due_at else None,
         iso(validated.snoozed_until) if validated.snoozed_until else None,
         iso(validated.expires_at), None, None, None, validated.cancel_reason,
         validated.occurrence_index, 0, validated.max_attempts, "", ts, ts),
    )


def create_reminder_result(conn: sqlite3.Connection, draft: ReminderDraft | dict[str, Any], *,
                           now: datetime | None = None) -> CreateReminderResult:
    """Insert by relying on the dedup_key UNIQUE constraint and report the actual outcome.

    A pre-insert SELECT cannot make this decision safely under concurrent connections:
    two writers can both observe zero rows before one wins the INSERT. Therefore the
    insert is attempted first and IntegrityError is mapped to the existing row.
    """
    _foreign_keys_on(conn)
    validated = _validate_draft(draft)
    _require_source_exists(conn, validated)
    ts = iso(now or utc_now())
    try:
        cur = _insert_reminder(conn, validated, ts)
    except sqlite3.IntegrityError as exc:
        existing = get_reminder_by_dedup(conn, validated.dedup_key)
        if existing is not None:
            return CreateReminderResult(existing, False)
        raise ReminderError("storage_conflict", "提醒存储冲突", status_code=409) from exc
    return CreateReminderResult(get_reminder(conn, int(cur.lastrowid)), True)


def create_reminder(conn: sqlite3.Connection, draft: ReminderDraft | dict[str, Any], *,
                    now: datetime | None = None) -> dict[str, Any]:
    return create_reminder_result(conn, draft, now=now).reminder

def expire_reminders(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Deterministic storage helper; the future C batch will invoke it in ticks."""
    ts = iso(now or utc_now())
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    cur = conn.execute(
        f"UPDATE reminders SET status='expired', resolved_at=?, cancel_reason='expiry_reached', updated_at=? "
        f"WHERE status IN ({marks}) AND expires_at<=?",
        (ts, ts, *ACTIVE_STATUSES, ts),
    )
    return int(cur.rowcount)


def snooze_reminder(conn: sqlite3.Connection, reminder_id: int, until: str | datetime, *,
                    now: datetime | None = None) -> dict[str, Any]:
    target = parse_iso(until, field_name="snooze_until")
    current_dt = now or utc_now()
    if target <= current_dt:
        raise ReminderError("invalid_time", "延期时间必须晚于当前时间")
    ts = iso(current_dt)
    cur = conn.execute(
        "UPDATE reminders SET status='snoozed', snoozed_until=?, updated_at=? "
        "WHERE id=? AND status='due'",
        (iso(target), ts, reminder_id),
    )
    if cur.rowcount == 0:
        _row(conn, reminder_id)
        raise ReminderError("conflict", "只有 due 状态的提醒可以延期", status_code=409)
    return get_reminder(conn, reminder_id)


def transition_reminder(conn: sqlite3.Connection, reminder_id: int, action: str, *,
                        now: datetime | None = None,
                        cancel_reason: str | None = None,
                        snooze_minutes: int = 10) -> dict[str, Any]:
    action = (action or "").strip().lower()
    if action not in TRANSITIONS:
        raise ReminderError("invalid_action", f"非法提醒操作：{action}")
    if cancel_reason is not None and cancel_reason not in CANCEL_REASONS:
        raise ReminderError("invalid_cancel_reason", "非法取消原因")
    if action == "snooze" and (
        isinstance(snooze_minutes, bool)
        or not isinstance(snooze_minutes, int)
        or not 1 <= snooze_minutes <= MAX_SNOOZE_MINUTES
    ):
        raise ReminderError("invalid_snooze", f"延期时长必须是 1..{MAX_SNOOZE_MINUTES} 分钟")
    row = _row(conn, reminder_id)
    current_status = row["status"]
    if current_status not in TRANSITIONS[action]:
        raise ReminderError("conflict", f"提醒当前状态为 {current_status}，不能执行 {action}",
                            status_code=409)
    target_status = {
        "mark_due": STATUS_DUE,
        "acknowledge": STATUS_ACKNOWLEDGED,
        "snooze": STATUS_SNOOZED,
        "dismiss": STATUS_DISMISSED,
        "complete": STATUS_COMPLETED,
        "cancel": STATUS_CANCELLED,
        "expire": STATUS_EXPIRED,
    }[action]
    if current_status == target_status:
        return _serialize(row)
    reference = now or utc_now()
    ts = iso(reference)
    sets = ["status=?", "updated_at=?"]
    params: list[Any] = [target_status, ts]
    if action == "mark_due":
        sets.append("due_at=COALESCE(due_at,?)")
        params.append(ts)
    if action == "acknowledge":
        sets.append("acknowledged_at=COALESCE(acknowledged_at,?)")
        params.append(ts)
    if action == "snooze":
        # Without an explicit wake time the scheduler treats a snoozed reminder as
        # ready immediately (`ready_at is None`) and bounces it straight back to due.
        sets.append("snoozed_until=?")
        params.append(iso(reference + timedelta(minutes=snooze_minutes)))
    if action == "complete":
        sets.append("completed_at=COALESCE(completed_at,?)")
        params.append(ts)
        sets.append("resolved_at=COALESCE(resolved_at,?)")
        params.append(ts)
    if action in {"dismiss", "cancel", "expire"}:
        sets.append("resolved_at=COALESCE(resolved_at,?)")
        params.append(ts)
    if action == "cancel":
        sets.append("cancelled_at=COALESCE(cancelled_at,?)")
        params.append(ts)
        sets.append("cancel_reason=?")
        params.append(cancel_reason or "user_cancelled")
    if action == "expire":
        sets.append("cancel_reason=?")
        params.append("expiry_reached")
    params.extend((reminder_id, current_status))
    cur = conn.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id=? AND status=?", params)
    if cur.rowcount == 0:
        latest = _row(conn, reminder_id)
        raise ReminderError("conflict", f"提醒状态已变为 {latest['status']}，请刷新后重试",
                            status_code=409)
    return get_reminder(conn, reminder_id)
