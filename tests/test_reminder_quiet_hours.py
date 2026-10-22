"""RED: 1.3.5-C-B scheduler quiet-hours tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import reminder_scheduler as rs

UTC = timezone.utc
SH = timezone(timedelta(hours=8))


def settings(**overrides):
    base = {
        "timezone": "Asia/Shanghai", "quiet_hours_enabled": False,
        "quiet_start": "22:00", "quiet_end": "08:00",
        "task_due_enabled": False, "task_stalled_enabled": False,
        "recommendation_followup_enabled": False,
        "daily_review_enabled": False, "weekly_review_enabled": False,
    }
    base.update(overrides)
    return base


def test_quiet_until_disabled_or_outside_range_is_none():
    now = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
    assert rs.quiet_until(now.astimezone(SH), settings()) is None
    enabled = settings(quiet_hours_enabled=True, quiet_start="09:00", quiet_end="11:00")
    assert rs.quiet_until(now.astimezone(SH), enabled) == datetime(2026, 10, 20, 11, 0, tzinfo=SH)
    outside = datetime(2026, 10, 20, 4, 0, tzinfo=UTC).astimezone(SH)
    assert rs.quiet_until(outside, enabled) is None


def test_quiet_until_cross_midnight():
    enabled = settings(quiet_hours_enabled=True, quiet_start="22:00", quiet_end="08:00")
    at_23 = datetime(2026, 10, 19, 15, 0, tzinfo=UTC).astimezone(SH)
    assert rs.quiet_until(at_23, enabled) == datetime(2026, 10, 20, 8, 0, tzinfo=SH)
    at_07 = datetime(2026, 10, 19, 23, 0, tzinfo=UTC).astimezone(SH)
    assert rs.quiet_until(at_07, enabled) == datetime(2026, 10, 20, 8, 0, tzinfo=SH)


def test_quiet_boundaries_are_half_open():
    enabled = settings(quiet_hours_enabled=True, quiet_start="10:00", quiet_end="11:00")
    assert rs.quiet_until(datetime(2026, 10, 20, 1, 59, tzinfo=UTC).astimezone(SH), enabled) is None
    assert rs.quiet_until(datetime(2026, 10, 20, 2, 0, tzinfo=UTC).astimezone(SH), enabled) is not None
    assert rs.quiet_until(datetime(2026, 10, 20, 3, 0, tzinfo=UTC).astimezone(SH), enabled) is None
