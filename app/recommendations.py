"""Personalized recommendation storage (Personal AI OS 1.3.3-B).

Storage / service layer only:
* recommendation domain model (Pydantic validated drafts);
* evidence + window serialisation with hard size caps;
* deterministic dedup key and rejected-item cooldown;
* lazy expiry and the pending/accepted/ignored/rejected state machine;
* generation run records (`recommendation_runs`) with sanitised errors.

This module deliberately has NO HTTP routes, NO rule engine and NO LLM
calls. It never writes to memories or profile_items, and adopting a
recommendation never changes personal facts (that boundary belongs to a
later, explicitly approved phase).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app import behavior_events

# Reuse the single defensive secret filter rather than forking the patterns.
from app.profile_propose import looks_like_secret

GENERATION_VERSION = "1.3.3-C@1"

CATEGORIES = {"task", "project", "goal", "learning", "habit"}
PRIORITIES = {"low", "normal", "high"}
RISK_LEVELS = {"low", "medium", "high"}
STATUSES = {"pending", "accepted", "ignored", "rejected", "expired"}
SOURCES = {"rule", "hybrid"}
COVERAGE_STATUSES = {"ok", "partial_data", "insufficient_data", "no_data"}
RUN_STATUSES = {"success", "no_evidence", "degraded", "failed"}
TRIGGER_SOURCES = {"manual"}

# status, action -> allowed source statuses
TRANSITIONS: dict[str, tuple[str, str]] = {
    "accept": ("pending", "accepted"),
    "ignore": ("pending", "ignored"),
    "reject": ("pending", "rejected"),
    "reopen": ("accepted", "pending"),
}
TERMINAL_STATUSES = {"ignored", "rejected", "expired"}
ACTIVE_STATUSES = ("pending", "accepted")
DEFAULT_HIDDEN_STATUSES = ("expired", "rejected")

REJECT_COOLDOWN_DAYS = 30
DEFAULT_EXPIRES_DAYS = 30
MAX_EXPIRES_DAYS = 365
MAX_EVIDENCE_ITEMS = 10

MAX_TITLE = 200
MAX_CONTENT = 2000
MAX_BENEFIT = 500
MAX_RISK_NOTE = 500
MAX_RULE_CODE = 80
MAX_VERSION = 50
MAX_EVIDENCE_LABEL = 200
MAX_EVIDENCE_DETAIL = 300
MAX_EVIDENCE_REF_TYPE = 50
MAX_EVIDENCE_VALUE = 200
MAX_EVIDENCE_JSON = 3000
MAX_WINDOW_JSON = 500
MAX_RUN_ERROR = 500
STALE_RUN_MINUTES = 5

EVIDENCE_KINDS = {"metric", "task", "project", "goal", "profile_item"}
WINDOW_KEYS = {"days", "timezone", "start", "end"}


class RecommendationError(Exception):
    """Service-level error with an HTTP status hint for the future API layer."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


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


class EvidenceItem(BaseModel):
    kind: str
    ref_type: str = Field(min_length=1, max_length=MAX_EVIDENCE_REF_TYPE)
    ref_id: int | None = None
    label: str = Field(min_length=1, max_length=MAX_EVIDENCE_LABEL)
    value: int | float | str | bool | None = None
    detail: str = Field(default="", max_length=MAX_EVIDENCE_DETAIL)
    definition_version: str = Field(default="", max_length=MAX_VERSION)

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in EVIDENCE_KINDS:
            raise ValueError(f"非法证据 kind：{value}")
        return value

    @field_validator("ref_type", "label", "detail", "definition_version")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("ref_id")
    @classmethod
    def _ref_id_positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("ref_id 必须是正整数")
        return value

    @field_validator("value")
    @classmethod
    def _cap_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if len(value) > MAX_EVIDENCE_VALUE:
                raise ValueError(f"证据 value 字符串不能超过 {MAX_EVIDENCE_VALUE} 字符")
        return value


class RecommendationDraft(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    category: str
    priority: str = "normal"
    risk_level: str = "low"
    risk_note: str = Field(default="", max_length=MAX_RISK_NOTE)
    confidence: float = Field(ge=0.0, le=1.0)
    expected_benefit: str = Field(default="", max_length=MAX_BENEFIT)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=MAX_EVIDENCE_ITEMS)
    rule_code: str = Field(min_length=1, max_length=MAX_RULE_CODE)
    source: str = "rule"
    coverage_status: str = "no_data"
    window: dict[str, Any] = Field(default_factory=dict)
    generation_version: str = Field(default=GENERATION_VERSION, max_length=MAX_VERSION)
    expires_days: int = Field(default=DEFAULT_EXPIRES_DAYS)

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in CATEGORIES:
            raise ValueError(f"非法 category：{value}")
        return value

    @field_validator("priority")
    @classmethod
    def _priority(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in PRIORITIES:
            raise ValueError(f"非法 priority：{value}")
        return value

    @field_validator("risk_level", "source", "coverage_status")
    @classmethod
    def _enums(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = RISK_LEVELS | SOURCES | COVERAGE_STATUSES
        if value not in allowed:
            raise ValueError(f"非法枚举值：{value}")
        return value

    @field_validator("title", "content", "risk_note", "expected_benefit", "rule_code", "generation_version")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, value: float) -> float:
        return round(float(value), 2)

    @field_validator("expires_days")
    @classmethod
    def _expires_range(cls, value: int) -> int:
        if isinstance(value, bool) or not 1 <= value <= MAX_EXPIRES_DAYS:
            raise ValueError(f"expires_days 必须是 1..{MAX_EXPIRES_DAYS} 的整数")
        return value

    @field_validator("window")
    @classmethod
    def _window(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or len(value) > len(WINDOW_KEYS):
            raise ValueError("window 必须是不超过 4 个键的对象")
        clean: dict[str, Any] = {}
        for key, raw in value.items():
            if key not in WINDOW_KEYS:
                raise ValueError(f"window 含非法键：{key}")
            if isinstance(raw, str):
                raw = raw.strip()
                if len(raw) > 100:
                    raise ValueError("window 字符串不能超过 100 字符")
            elif raw is not None and not isinstance(raw, (int, bool)):
                raise ValueError("window 值只能是字符串、整数或 null")
            clean[key] = raw
        return clean


def _require_no_secret(*texts: str) -> None:
    for text in texts:
        if text and looks_like_secret(text):
            raise RecommendationError(
                "secret_detected",
                "建议内容疑似包含密码/API Key/令牌等凭据，已拒绝保存。",
                status_code=400,
            )



def build_dedup_key(draft: RecommendationDraft) -> str:
    """Stable fingerprint: rule + category + window + ordered evidence refs.

    Evidence order in the list must not matter; two drafts with the same
    evidence set and window are the same recommendation.
    """
    evidence_sig = sorted(
        (
            item.kind,
            item.ref_type,
            item.ref_id if item.ref_id is not None else -1,
            item.label.casefold(),
            "" if item.value is None else str(item.value),
        )
        for item in draft.evidence
    )
    window_sig = {key: draft.window.get(key) for key in sorted(WINDOW_KEYS)}
    payload = json.dumps(
        {
            "rule_code": draft.rule_code,
            "category": draft.category,
            "window": window_sig,
            "evidence": evidence_sig,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _serialize(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        item["evidence"] = []
    try:
        item["window"] = json.loads(item.pop("window_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        item["window"] = {}
    return item


def _get_row(conn: sqlite3.Connection, rec_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        raise RecommendationError("not_found", "建议不存在", status_code=404)
    return row


def expire_stale(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Mark pending/accepted rows past expires_at as expired.

    Ignored/rejected rows are terminal and untouched. Returns the count. This
    write-side cleanup is used by generation; read/list calls must remain
    read-only and use _active_clause() to hide logically expired rows.
    """
    current = _iso(now or utc_now())
    cur = conn.execute(
        "UPDATE recommendations SET status='expired', updated_at=? "
        "WHERE status IN ('pending','accepted') AND expires_at IS NOT NULL AND expires_at<?",
        (current, current),
    )
    return int(cur.rowcount or 0)


def _effective_expired(row: sqlite3.Row | dict[str, Any], current_iso: str) -> bool:
    status = row["status"] if isinstance(row, sqlite3.Row) else row.get("status")
    expires_at = row["expires_at"] if isinstance(row, sqlite3.Row) else row.get("expires_at")
    return (
        status in ("pending", "accepted")
        and bool(expires_at)
        and str(expires_at) < current_iso
    )


def _active_clause(current_iso: str) -> str:
    """SQL predicate that treats expired pending/accepted rows as inactive."""
    return (
        "(status NOT IN ('pending','accepted') "
        "OR expires_at IS NULL OR expires_at>=?)"
    )


def create_recommendation(
    conn: sqlite3.Connection,
    data: RecommendationDraft | dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and store a draft.

    Returns {"status": "created", "recommendation": {...}}; an active twin
    returns status="duplicate"; a twin rejected within the cooldown window
    returns status="suppressed_rejected". Nothing is inserted in those cases.
    """
    draft = data if isinstance(data, RecommendationDraft) else RecommendationDraft.model_validate(data)
    _require_no_secret(
        draft.title,
        draft.content,
        draft.expected_benefit,
        draft.risk_note,
        " ".join(item.label + " " + item.detail for item in draft.evidence),
    )

    evidence_json = json.dumps(
        [item.model_dump() for item in draft.evidence], ensure_ascii=False
    )
    window_json = json.dumps(draft.window, ensure_ascii=False)
    if len(evidence_json) > MAX_EVIDENCE_JSON:
        raise RecommendationError("evidence_too_large", "证据数据超过长度上限")
    if len(window_json) > MAX_WINDOW_JSON:
        raise RecommendationError("window_too_large", "窗口数据超过长度上限")

    key = build_dedup_key(draft)
    current_dt = now or utc_now()
    current = _iso(current_dt)

    active = conn.execute(
        "SELECT id FROM recommendations WHERE dedup_key=? AND status IN ('pending','accepted') "
        "AND (expires_at IS NULL OR expires_at>=?) LIMIT 1",
        (key, current),
    ).fetchone()
    if active is not None:
        twin = _serialize(_get_row(conn, active["id"]))
        return {"status": "duplicate", "recommendation": twin}

    cutoff = _iso(current_dt - timedelta(days=REJECT_COOLDOWN_DAYS))
    rejected = conn.execute(
        "SELECT id FROM recommendations WHERE dedup_key=? AND status='rejected' "
        "AND COALESCE(reviewed_at, updated_at) >= ? ORDER BY id DESC LIMIT 1",
        (key, cutoff),
    ).fetchone()
    if rejected is not None:
        twin = _serialize(_get_row(conn, rejected["id"]))
        return {"status": "suppressed_rejected", "recommendation": twin}

    expires_at = _iso(current_dt + timedelta(days=draft.expires_days))
    try:
        cur = conn.execute(
        "INSERT INTO recommendations(title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,"
        "generation_version,dedup_key,expires_at,created_at,updated_at,reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            draft.title,
            draft.content,
            draft.category,
            draft.priority,
            "pending",
            evidence_json,
            window_json,
            draft.coverage_status,
            draft.expected_benefit,
            draft.risk_level,
            draft.risk_note,
            draft.confidence,
            draft.source,
            draft.rule_code,
            draft.generation_version,
            key,
            expires_at,
            current,
            current,
            None,
        ),
    )
    except sqlite3.IntegrityError as exc:
        # Cross-process race on the partial UNIQUE(dedup_key WHERE active)
        # index. Re-read the winner instead of creating a second active twin.
        winner = conn.execute(
            "SELECT id FROM recommendations WHERE dedup_key=? "
            "AND status IN ('pending','accepted') "
            "AND (expires_at IS NULL OR expires_at>=?) ORDER BY id LIMIT 1",
            (key, current),
        ).fetchone()
        if winner is not None:
            return {"status": "duplicate", "recommendation": _serialize(_get_row(conn, winner["id"]))}
        raise RecommendationError(
            "storage_conflict",
            "建议保存时发生并发冲突，请重试",
            status_code=409,
        ) from exc
    return {"status": "created", "recommendation": _serialize(_get_row(conn, cur.lastrowid))}


def get_recommendation(conn: sqlite3.Connection, rec_id: int) -> dict[str, Any]:
    return _serialize(_get_row(conn, rec_id))


def _validate_paging(limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise RecommendationError("invalid_limit", "limit 必须是 1..200 的整数")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise RecommendationError("invalid_offset", "offset 必须是非负整数")


def count_recommendations(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    now: datetime | None = None,
) -> int:
    """Count rows under the same read-only visibility rule as list.

    Logically expired pending/accepted rows are excluded without writing them
    to status='expired'; explicit status=expired queries still show rows that
    were physically marked by generation or the cleanup path.
    """
    if status is not None:
        status = status.strip().lower()
        if status not in STATUSES:
            raise RecommendationError("invalid_status", f"非法 status：{status}")
    if category is not None:
        category = category.strip().lower()
        if category not in CATEGORIES:
            raise RecommendationError("invalid_category", f"非法 category：{category}")
    if priority is not None:
        priority = priority.strip().lower()
        if priority not in PRIORITIES:
            raise RecommendationError("invalid_priority", f"非法 priority：{priority}")

    sql = "SELECT COUNT(*) FROM recommendations WHERE 1=1"
    params: list[Any] = []
    current_iso = _iso(now or utc_now())
    if status is None:
        placeholders = ",".join("?" for _ in DEFAULT_HIDDEN_STATUSES)
        sql += f" AND status NOT IN ({placeholders})"
        params.extend(DEFAULT_HIDDEN_STATUSES)
        sql += f" AND {_active_clause(current_iso)}"
        params.append(current_iso)
    elif status in ("pending", "accepted"):
        sql += " AND status=? AND (expires_at IS NULL OR expires_at>=?)"
        params.extend((status, current_iso))
    else:
        # ignored/rejected/expired show only their physically stored state.
        sql += " AND status=?"
        params.append(status)
    if category is not None:
        sql += " AND category=?"
        params.append(category)
    if priority is not None:
        sql += " AND priority=?"
        params.append(priority)
    return int(conn.execute(sql, params).fetchone()[0])


def list_recommendations(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    _validate_paging(limit, offset)
    if status is not None:
        status = status.strip().lower()
        if status not in STATUSES:
            raise RecommendationError("invalid_status", f"非法 status：{status}")
    if category is not None:
        category = category.strip().lower()
        if category not in CATEGORIES:
            raise RecommendationError("invalid_category", f"非法 category：{category}")
    if priority is not None:
        priority = priority.strip().lower()
        if priority not in PRIORITIES:
            raise RecommendationError("invalid_priority", f"非法 priority：{priority}")

    # Read-only endpoint semantics: do not mutate rows during a GET/list call.
    current_iso = _iso(now or utc_now())
    sql = "SELECT * FROM recommendations WHERE 1=1"
    params: list[Any] = []
    if status is None:
        placeholders = ",".join("?" for _ in DEFAULT_HIDDEN_STATUSES)
        sql += f" AND status NOT IN ({placeholders})"
        params.extend(DEFAULT_HIDDEN_STATUSES)
        sql += f" AND {_active_clause(current_iso)}"
        params.append(current_iso)
    elif status in ("pending", "accepted"):
        sql += " AND status=? AND (expires_at IS NULL OR expires_at>=?)"
        params.extend((status, current_iso))
    else:
        # ignored/rejected/expired show only their physically stored state.
        sql += " AND status=?"
        params.append(status)
    if category is not None:
        sql += " AND category=?"
        params.append(category)
    if priority is not None:
        sql += " AND priority=?"
        params.append(priority)
    sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id DESC LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    return [_serialize(row) for row in conn.execute(sql, params).fetchall()]


def transition_status(
    conn: sqlite3.Connection,
    rec_id: int,
    action: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    action = (action or "").strip().lower()
    if action not in TRANSITIONS:
        raise RecommendationError(
            "invalid_action", f"非法操作：{action}，允许 {sorted(TRANSITIONS)}"
        )
    row = _get_row(conn, rec_id)
    current_iso = _iso(now or utc_now())
    if _effective_expired(row, current_iso):
        raise RecommendationError(
            "conflict",
            "建议已超过有效期，请先生成或刷新建议",
            status_code=409,
        )
    current_status = row["status"]
    from_status, to_status = TRANSITIONS[action]
    if current_status != from_status:
        raise RecommendationError(
            "conflict",
            f"建议当前状态为 {current_status}，不能执行 {action}（仅允许 {from_status} → {to_status}）",
            status_code=409,
        )
    current = _iso(now or utc_now())
    cur = conn.execute(
        "UPDATE recommendations SET status=?, updated_at=?, reviewed_at=? "
        "WHERE id=? AND status=?",
        (to_status, current, current, rec_id, from_status),
    )
    if cur.rowcount:
        event_type = {
            "accept": behavior_events.EVENT_RECOMMENDATION_ACCEPTED,
            "ignore": behavior_events.EVENT_RECOMMENDATION_IGNORED,
            "reject": behavior_events.EVENT_RECOMMENDATION_REJECTED,
            "reopen": behavior_events.EVENT_RECOMMENDATION_REOPENED,
        }[action]
        behavior_events.record_event(
            conn,
            event_type,
            subject_type="recommendation",
            subject_id=rec_id,
            source_type="system",
            occurred_at=current,
            metadata={"from_status": from_status, "to_status": to_status},
        )
    if cur.rowcount == 0:
        # The row may have been deleted or changed by a concurrent request.
        latest = _get_row(conn, rec_id)  # 404 if another request deleted it
        if latest["status"] != current_status:
            raise RecommendationError(
                "conflict",
                f"建议状态已变为 {latest['status']}，请刷新后重试",
                status_code=409,
            )
        raise RecommendationError("conflict", "建议状态已被其他操作修改，请刷新后重试", status_code=409)
    return _serialize(_get_row(conn, rec_id))


def delete_recommendation(conn: sqlite3.Connection, rec_id: int) -> None:
    _get_row(conn, rec_id)  # 404 before delete
    cur = conn.execute("DELETE FROM recommendations WHERE id=?", (rec_id,))
    if cur.rowcount == 0:
        raise RecommendationError("not_found", "建议不存在", status_code=404)


def sanitize_run_error(error: str | None) -> str:
    """Cap and scrub a run error so logs can never carry credentials."""
    text = (error or "").strip()[:MAX_RUN_ERROR]
    if text and looks_like_secret(text):
        return "生成失败（错误详情包含疑似凭据，已脱敏）"
    return text


def recover_stale_runs(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    timeout_minutes: int = STALE_RUN_MINUTES,
) -> int:
    """Mark interrupted runs from crashed processes as failed.

    A live request is bounded by the model timeout; only old running rows are
    recovered. This never cancels a request that is still executing.
    """
    if isinstance(timeout_minutes, bool) or not isinstance(timeout_minutes, int) or timeout_minutes <= 0:
        raise RecommendationError("invalid_timeout", "timeout_minutes 必须是正整数")
    current_dt = now or utc_now()
    cutoff = _iso(current_dt - timedelta(minutes=timeout_minutes))
    current = _iso(current_dt)
    cur = conn.execute(
        "UPDATE recommendation_runs SET status='failed', finished_at=?, error=? "
        "WHERE status='running' AND started_at<?",
        (current, "上次生成进程中断，已自动标记为失败", cutoff),
    )
    return int(cur.rowcount or 0)


def start_run(
    conn: sqlite3.Connection,
    *,
    triggered_by: str = "manual",
    use_model: bool = False,
    window_days: int | None = None,
    timezone_name: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    now: datetime | None = None,
) -> int:
    triggered_by = (triggered_by or "manual").strip().lower()
    if triggered_by not in TRIGGER_SOURCES:
        raise RecommendationError("invalid_trigger", f"非法触发方式：{triggered_by}")
    current = _iso(now or utc_now())
    try:
        cur = conn.execute(
            "INSERT INTO recommendation_runs(triggered_by,use_model,window_days,timezone,window_start,"
            "window_end,status,started_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                triggered_by,
                1 if use_model else 0,
                window_days,
                timezone_name,
                window_start,
                window_end,
                "running",
                current,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise RecommendationError(
            "generation_in_progress",
            "已有建议生成任务正在运行，请稍后再试",
            status_code=409,
        ) from exc
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    candidates: int = 0,
    created: int = 0,
    duplicates: int = 0,
    suppressed_rejected: int = 0,
    expired_marked: int = 0,
    error: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    status = (status or "").strip().lower()
    if status not in RUN_STATUSES:
        raise RecommendationError("invalid_run_status", f"非法运行状态：{status}")
    counts = {
        "candidates": candidates,
        "created": created,
        "duplicates": duplicates,
        "suppressed_rejected": suppressed_rejected,
        "expired_marked": expired_marked,
    }
    for name, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RecommendationError("invalid_count", f"{name} 必须是非负整数")
    current = _iso(now or utc_now())
    safe_error = sanitize_run_error(error)
    conn.execute(
        "UPDATE recommendation_runs SET status=?, candidates=?, created=?, duplicates=?, "
        "suppressed_rejected=?, expired_marked=?, error=?, finished_at=? WHERE id=?",
        (
            status,
            candidates,
            created,
            duplicates,
            suppressed_rejected,
            expired_marked,
            safe_error,
            current,
            run_id,
        ),
    )
    row = conn.execute("SELECT * FROM recommendation_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise RecommendationError("not_found", "运行记录不存在", status_code=404)
    return dict(row)


def list_runs(
    conn: sqlite3.Connection, *, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    _validate_paging(limit, offset)
    rows = conn.execute(
        "SELECT * FROM recommendation_runs ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]
