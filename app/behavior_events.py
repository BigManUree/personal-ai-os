"""Append-only, minimal behavior events for Personal AI OS 1.3.2-A.

This module intentionally stores event metadata only: statuses, counts,
origin and linked ids. It never stores task titles, message/document content,
secrets, or arbitrary free text. Statistics and model-based interpretation
belong to later iterations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

EVENT_DOCUMENT_CREATED = "document.created"
EVENT_PROJECT_CREATED = "project.created"
EVENT_PROJECT_UPDATED = "project.updated"
EVENT_PROJECT_STATUS_CHANGED = "project.status_changed"
EVENT_PROJECT_EVENT_RECORDED = "project_event.recorded"
EVENT_GOAL_CREATED = "goal.created"
EVENT_GOAL_UPDATED = "goal.updated"
EVENT_GOAL_STATUS_CHANGED = "goal.status_changed"
EVENT_TASK_CREATED = "task.created"
EVENT_TASK_UPDATED = "task.updated"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TASK_REOPENED = "task.reopened"
EVENT_TASK_CANCELLED = "task.cancelled"
EVENT_RECOMMENDATION_ACCEPTED = "recommendation.accepted"
EVENT_RECOMMENDATION_IGNORED = "recommendation.ignored"
EVENT_RECOMMENDATION_REJECTED = "recommendation.rejected"
EVENT_RECOMMENDATION_REOPENED = "recommendation.reopened"
EVENT_RECOMMENDATION_FEEDBACK_SAVED = "recommendation.feedback_saved"
EVENT_RECOMMENDATION_FEEDBACK_DELETED = "recommendation.feedback_deleted"
EVENT_RECOMMENDATION_OUTCOME_SAVED = "recommendation.outcome_saved"
EVENT_RECOMMENDATION_OUTCOME_DELETED = "recommendation.outcome_deleted"

EVENT_TYPES = {
    EVENT_DOCUMENT_CREATED,
    EVENT_PROJECT_CREATED,
    EVENT_PROJECT_UPDATED,
    EVENT_PROJECT_STATUS_CHANGED,
    EVENT_PROJECT_EVENT_RECORDED,
    EVENT_GOAL_CREATED,
    EVENT_GOAL_UPDATED,
    EVENT_GOAL_STATUS_CHANGED,
    EVENT_TASK_CREATED,
    EVENT_TASK_UPDATED,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_REOPENED,
    EVENT_TASK_CANCELLED,
    EVENT_RECOMMENDATION_ACCEPTED,
    EVENT_RECOMMENDATION_IGNORED,
    EVENT_RECOMMENDATION_REJECTED,
    EVENT_RECOMMENDATION_REOPENED,
    EVENT_RECOMMENDATION_FEEDBACK_SAVED,
    EVENT_RECOMMENDATION_FEEDBACK_DELETED,
    EVENT_RECOMMENDATION_OUTCOME_SAVED,
    EVENT_RECOMMENDATION_OUTCOME_DELETED,
}

SUBJECT_TYPES = {
    "document",
    "project",
    "project_event",
    "goal",
    "task",
    "data_source",
    "recommendation",
    "recommendation_feedback",
    "recommendation_outcome",
}

EVENT_SOURCES = {"system", "api", "backfill"}
MAX_METADATA_JSON = 1000

_SECRET_KEY_RE = re.compile(
    r"(?i)^(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth(?:orization)?|"
    r"client[_-]?secret|credential|密码|口令|令牌|密钥)$"
)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)\bsk-[a-z0-9][a-z0-9-]{12,}"),
    re.compile(r"(?i)\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|credential|密码|口令|令牌|密钥)\s*[:：=]\s*['\"]?\S{4,}"),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_event_time(value: str | None) -> datetime:
    if not value or not isinstance(value, str):
        raise ValueError("事件时间不能为空")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("事件时间必须是 ISO-8601 格式") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_event_time(value: str | None) -> str:
    return parse_event_time(value).isoformat(timespec="seconds")


def _validate_metadata_key(key: Any) -> str:
    if not isinstance(key, str) or not key.strip() or len(key) > 80:
        raise ValueError("metadata 键必须是 1..80 字符的字符串")
    key = key.strip()
    if _SECRET_KEY_RE.match(key):
        raise ValueError("metadata 不能包含密码、令牌或密钥字段")
    return key


def _validate_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 300:
                raise ValueError("metadata 字符串不能超过 300 字符")
            if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
                raise ValueError("metadata 疑似包含密码、令牌或密钥")
        return value
    raise ValueError("metadata 只能包含 JSON 对象、数组和基础标量")


def _validate_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata 必须是对象")

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {_validate_metadata_key(k): walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node[:20]]
        return _validate_scalar(node)

    cleaned = walk(value)
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    if len(encoded) > MAX_METADATA_JSON:
        raise ValueError(f"metadata 序列化后不能超过 {MAX_METADATA_JSON} 字符")
    return cleaned


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    subject_type: str,
    subject_id: int | None = None,
    occurred_at: str | None = None,
    project_id: int | None = None,
    goal_id: int | None = None,
    task_id: int | None = None,
    data_source_id: int | None = None,
    source_type: str = "system",
    source_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    dedup_key: str | None = None,
) -> dict[str, Any] | None:
    """Insert one append-only event in the caller's transaction.

    Returns the inserted row, or None when a stable dedup_key already exists.
    """
    event_type = (event_type or "").strip().lower()
    subject_type = (subject_type or "").strip().lower()
    source_type = (source_type or "system").strip().lower()
    if event_type not in EVENT_TYPES:
        raise ValueError(f"非法 event_type：{event_type}")
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"非法 subject_type：{subject_type}")
    if source_type not in EVENT_SOURCES:
        raise ValueError(f"非法事件来源：{source_type}")
    if subject_id is not None and (not isinstance(subject_id, int) or subject_id <= 0):
        raise ValueError("subject_id 必须是正整数")
    if dedup_key is not None:
        dedup_key = dedup_key.strip()
        if not dedup_key or len(dedup_key) > 300:
            raise ValueError("dedup_key 必须是 1..300 字符")

    timestamp = normalize_event_time(occurred_at or utc_now_iso())
    metadata_json = json.dumps(_validate_metadata(metadata), ensure_ascii=False, sort_keys=True)
    created_at = utc_now_iso()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO behavior_events(
            event_type, occurred_at, subject_type, subject_id, project_id, goal_id,
            task_id, data_source_id, source_type, source_id, dedup_key,
            metadata_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (event_type, timestamp, subject_type, subject_id, project_id, goal_id,
         task_id, data_source_id, source_type, source_id, dedup_key,
         metadata_json, created_at),
    )
    if cur.lastrowid and cur.rowcount:
        row = conn.execute("SELECT * FROM behavior_events WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) if row is not None else None
    return None


def _already_recorded(conn: sqlite3.Connection, event_type: str, subject_type: str, subject_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM behavior_events WHERE event_type=? AND subject_type=? AND subject_id=? LIMIT 1",
        (event_type, subject_type, subject_id),
    ).fetchone() is not None


def _backfill_one(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    subject_type: str,
    subject_id: int,
    occurred_at: str | None,
    metadata: dict[str, Any],
    counters: dict[str, Any],
    project_id: int | None = None,
    goal_id: int | None = None,
    task_id: int | None = None,
    data_source_id: int | None = None,
) -> None:
    try:
        normalize_event_time(occurred_at)
    except ValueError:
        counters["skipped"] += 1
        counters["skipped_reasons"]["invalid_time"] += 1
        return
    if _already_recorded(conn, event_type, subject_type, subject_id):
        counters["skipped"] += 1
        counters["skipped_reasons"]["already_recorded"] += 1
        return
    dedup_key = f"backfill:{subject_type}:{subject_id}:{event_type}"
    inserted = record_event(
        conn,
        event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        occurred_at=occurred_at,
        project_id=project_id,
        goal_id=goal_id,
        task_id=task_id,
        data_source_id=data_source_id,
        source_type="backfill",
        metadata=metadata,
        dedup_key=dedup_key,
    )
    if inserted is None:
        counters["skipped"] += 1
        counters["skipped_reasons"]["duplicate_key"] += 1
        return
    counters["inserted"] += 1
    counters["by_type"][event_type] = counters["by_type"].get(event_type, 0) + 1


def backfill_events(conn: sqlite3.Connection) -> dict[str, Any]:
    """Explicitly and idempotently reconstruct only historically knowable events."""
    counters: dict[str, Any] = {
        "inserted": 0,
        "skipped": 0,
        "by_type": {},
        "skipped_reasons": {"already_recorded": 0, "duplicate_key": 0, "invalid_time": 0},
    }

    projects = conn.execute("SELECT id,status,created_at FROM projects ORDER BY id").fetchall()
    for row in projects:
        _backfill_one(
            conn, event_type=EVENT_PROJECT_CREATED, subject_type="project", subject_id=row["id"],
            occurred_at=row["created_at"], metadata={"status": row["status"] or "active"}, counters=counters,
            project_id=row["id"],
        )

    project_events = conn.execute(
        "SELECT id,project_id,event_type,created_at FROM project_events ORDER BY id"
    ).fetchall()
    for row in project_events:
        _backfill_one(
            conn, event_type=EVENT_PROJECT_EVENT_RECORDED, subject_type="project_event",
            subject_id=row["id"], occurred_at=row["created_at"], project_id=row["project_id"],
            metadata={"project_event_type": row["event_type"]}, counters=counters,
        )

    goals = conn.execute(
        "SELECT id,project_id,kind,status,created_at FROM goals ORDER BY id"
    ).fetchall()
    for row in goals:
        _backfill_one(
            conn, event_type=EVENT_GOAL_CREATED, subject_type="goal", subject_id=row["id"],
            occurred_at=row["created_at"], project_id=row["project_id"], goal_id=row["id"],
            metadata={"kind": row["kind"] or "long_term", "status": row["status"] or "active"},
            counters=counters,
        )

    tasks = conn.execute(
        "SELECT id,project_id,goal_id,status,priority,created_at,completed_at FROM tasks ORDER BY id"
    ).fetchall()
    for row in tasks:
        _backfill_one(
            conn, event_type=EVENT_TASK_CREATED, subject_type="task", subject_id=row["id"],
            occurred_at=row["created_at"], project_id=row["project_id"], goal_id=row["goal_id"],
            task_id=row["id"], metadata={"priority": row["priority"] or "normal"}, counters=counters,
        )
        if row["completed_at"]:
            _backfill_one(
                conn, event_type=EVENT_TASK_COMPLETED, subject_type="task", subject_id=row["id"],
                occurred_at=row["completed_at"], project_id=row["project_id"], goal_id=row["goal_id"],
                task_id=row["id"], metadata={"terminal_status": row["status"] or "done"}, counters=counters,
            )

    documents = conn.execute("SELECT id,source_type,status,file_ext,created_at FROM documents ORDER BY id").fetchall()
    for row in documents:
        _backfill_one(
            conn, event_type=EVENT_DOCUMENT_CREATED, subject_type="document", subject_id=row["id"],
            occurred_at=row["created_at"],
            metadata={"origin": row["source_type"] or "manual_text", "status": row["status"] or "parsed",
                      "file_ext": row["file_ext"] or ".txt"},
            counters=counters,
        )
    return counters


def list_events(
    conn: sqlite3.Connection,
    *,
    event_type: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM behavior_events WHERE 1=1"
    params: list[Any] = []
    if event_type:
        event_type = event_type.strip().lower()
        if event_type not in EVENT_TYPES:
            raise ValueError(f"非法 event_type：{event_type}")
        sql += " AND event_type=?"
        params.append(event_type)
    if start_at:
        sql += " AND occurred_at>=?"
        params.append(normalize_event_time(start_at))
    if end_at:
        sql += " AND occurred_at<=?"
        params.append(normalize_event_time(end_at))
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    for row in rows:
        try:
            row["metadata"] = json.loads(row.get("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            row["metadata"] = {}
        row.pop("metadata_json", None)
    return rows
