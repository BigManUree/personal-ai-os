from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

import app.main as _root
from app import (
    reminders,
)

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter()

@router.get("/api/reminders")
def list_reminders_api(status: str | None = None, include_terminal: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    with _root.db() as conn:
        try:
            return reminders.list_reminders(conn, status=status, limit=min(limit, 100), include_terminal=include_terminal)
        except reminders.ReminderError as exc:
            raise HTTPException(status_code=exc.status_code or 400, detail=str(exc))


@router.post("/api/reminders/{reminder_id}/transition")
def transition_reminder_api(reminder_id: int, body: dict[str, Any]) -> dict[str, Any]:
    action = body.get("action", "")
    cancel_reason = body.get("cancel_reason")
    snooze_minutes = body.get("snooze_minutes", 10)
    with _root.db() as conn:
        try:
            return reminders.transition_reminder(
                conn, reminder_id, action,
                cancel_reason=cancel_reason, snooze_minutes=snooze_minutes,
            )
        except reminders.ReminderError as exc:
            raise HTTPException(status_code=exc.status_code or 400, detail=str(exc))


@router.get("/api/reminder-settings")
def get_reminder_settings_api() -> dict[str, Any]:
    with _root.db() as conn:
        return reminders.get_settings(conn)


# GET /api/reminder-settings returns these two columns; a client that echoes the
# whole object back on PUT must not be rejected for them.
_REMINDER_SETTINGS_READ_ONLY = {"created_at", "updated_at"}


@router.put("/api/reminder-settings")
def update_reminder_settings_api(body: dict[str, Any]) -> dict[str, Any]:
    editable = set(reminders.SETTINGS_FIELDS)
    unknown = sorted(set(body) - editable - _REMINDER_SETTINGS_READ_ONLY)
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知的提醒设置字段：{unknown}")
    allowed = {k: v for k, v in body.items() if k in editable}
    with _root.db() as conn:
        try:
            reminders.update_settings(conn, allowed)
            return reminders.get_settings(conn)
        except reminders.ReminderError as exc:
            raise HTTPException(status_code=exc.status_code or 400, detail=str(exc))



