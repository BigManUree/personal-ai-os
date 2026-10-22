from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

import app.main as _root
from app import (
    analytics,
    behavior_events,
)

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter()

# ===== Personal AI OS 1.3.2-A behavior events (inserted block begin) =====


@router.get("/api/behavior/events")
def list_behavior_events_route(
    event_type: str | None = None,
    from_time: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    with _root.db() as conn:
        try:
            return behavior_events.list_events(
                conn,
                event_type=event_type,
                start_at=from_time,
                end_at=to,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/behavior/backfill")
def backfill_behavior_events_route() -> dict[str, Any]:
    """Explicitly create minimal historical events; never runs at startup."""
    with _root.db() as conn:
        return behavior_events.backfill_events(conn)


# ===== Personal AI OS 1.3.2-A behavior events (inserted block end) =====


# ===== Personal AI OS 1.3.2-D read-only analytics API (inserted block begin) =====


@router.get("/api/analytics/overview")
def analytics_overview_route(
    days: int = Query(default=7, ge=1, le=366),
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Return deterministic windowed counts. Never invokes an LLM."""
    try:
        with _root.db() as conn:
            return analytics.overview(conn, days=days, timezone_name=timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/analytics/comparison")
def analytics_comparison_route(
    window_days: int = Query(default=7),
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Return deterministic equal-window comparison. Never invokes an LLM."""
    try:
        with _root.db() as conn:
            return analytics.compare_periods(
                conn,
                window_days=window_days,
                timezone_name=timezone,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ===== Personal AI OS 1.3.2-D read-only analytics API (inserted block end) =====



