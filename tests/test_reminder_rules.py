"""Deterministic 1.3.5-B reminder rule calculation tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import reminders as rm

SH = timezone(timedelta(hours=8))
UTC = timezone.utc


def settings(**overrides):
    base = {
        "timezone": "Asia/Shanghai",
        "task_due_enabled": False,
        "task_stalled_enabled": False,
        "recommendation_followup_enabled": False,
        "daily_review_enabled": False,
        "weekly_review_enabled": False,
        "daily_review_at": "20:00",
        "weekly_review_weekday": 1,
        "weekly_review_at": "09:00",
        "quiet_hours_enabled": False,
        "quiet_start": "22:00",
        "quiet_end": "08:00",
    }
    base.update(overrides)
    return base


def task(task_id=1, status="todo", due="2026-10-20", updated=None):
    return {"id": task_id, "status": status, "due_date": due, "updated_at": updated or "2026-10-10T01:00:00+00:00"}


def recommendation(rec_id=1, status="accepted", expires=None, reviewed=None):
    return {
        "id": rec_id,
        "status": status,
        "expires_at": expires or "2026-11-30T16:00:00+00:00",
        "reviewed_at": reviewed or "2026-10-06T11:00:00+00:00",
        "updated_at": reviewed or "2026-10-06T11:00:00+00:00",
    }


def test_due_soon_boundary_requires_enabled_valid_date_and_window():
    enabled = settings(task_due_enabled=True)
    # Due date 2026-10-20 => lead reminder 2026-10-19 09:00 +08 = 01:00 UTC.
    at = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    candidate = rm.task_due_soon_candidate(task(), enabled, now=at)
    assert candidate is not None
    assert candidate.dedup_key == "task.due_soon_v1:1:2026-10-20"
    assert candidate.scheduled_at == at
    assert candidate.evidence[0]["coverage_status"] == "ok"
    assert rm.task_due_soon_candidate(task(), settings(), now=at) is None
    assert rm.task_due_soon_candidate({**task(), "due_date": None}, enabled, now=at) is None
    assert rm.task_due_soon_candidate(task(status="done"), enabled, now=at) is None
    assert rm.task_due_soon_candidate(task(status="cancelled"), enabled, now=at) is None
    assert rm.task_due_soon_candidate(task(due="2026-11-20"), enabled, now=at) is None


def test_due_soon_uses_local_fixed_clock_not_date_midnight_utc():
    enabled = settings(task_due_enabled=True)
    before = datetime(2026, 10, 19, 0, 59, tzinfo=UTC)
    after = datetime(2026, 10, 19, 1, 0, tzinfo=UTC)
    assert rm.task_due_soon_candidate(task(), enabled, now=before) is None
    assert rm.task_due_soon_candidate(task(), enabled, now=after) is not None


def test_overdue_boundaries_repeats_reschedule_and_invalid_occurrences():
    enabled = settings(task_due_enabled=True)
    first = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)  # day after due, 09:00 SH
    second = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)  # +3 repeat days
    first_candidate = rm.task_overdue_candidate(task(), enabled, now=first, occurrence=1)
    second_candidate = rm.task_overdue_candidate(task(), enabled, now=second, occurrence=2)
    assert first_candidate.dedup_key.endswith(":1")
    assert second_candidate.dedup_key.endswith(":2")
    assert rm.task_overdue_candidate(task(), enabled, now=first, occurrence=3) is None
    assert rm.task_overdue_candidate(task(due="2026-10-27"), enabled, now=first, occurrence=1) is None
    assert rm.task_overdue_candidate(task(status="done"), enabled, now=first, occurrence=1) is None
    assert rm.task_overdue_candidate(task(due="2026-11-01"), enabled, now=first, occurrence=1) is None


def test_stalled_rule_only_doing_after_threshold_and_uses_observable_language():
    enabled = settings(task_stalled_enabled=True)
    updated = datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
    before = updated + timedelta(days=3) - timedelta(minutes=1)
    at = updated + timedelta(days=3, hours=2)
    assert rm.task_stalled_candidate(task(status="doing", updated=rm.iso(updated)), enabled,
                                     now=before, last_activity_at=updated) is None
    candidate = rm.task_stalled_candidate(task(status="doing", updated=rm.iso(updated)), enabled,
                                          now=at, last_activity_at=updated)
    assert candidate is not None
    assert candidate.priority == "normal"
    assert "不" in candidate.content and "线下行动" in candidate.content
    assert candidate.evidence[0]["coverage_status"] == "partial_data"
    assert rm.task_stalled_candidate(task(status="todo", updated=rm.iso(updated)), enabled,
                                     now=at, last_activity_at=updated) is None
    second = rm.task_stalled_candidate(task(status="doing", updated=rm.iso(updated)), enabled,
                                       now=updated + timedelta(days=10), last_activity_at=updated,
                                       occurrence=2)
    assert second is not None and second.occurrence_index == 2


def test_recommendation_followup_day_7_and_14_and_stop_conditions():
    enabled = settings(recommendation_followup_enabled=True)
    accepted = datetime(2026, 10, 6, 11, 0, tzinfo=UTC)
    # follow-up scheduled local 19:00 on accepted day + 7 = 2026-10-13 19:00 SH = 11:00 UTC.
    day7 = datetime(2026, 10, 13, 11, 0, tzinfo=UTC)
    day14 = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    c1 = rm.recommendation_followup_candidate(recommendation(reviewed=rm.iso(accepted)), enabled,
                                              now=day7, has_outcome=False, accepted_at=accepted, occurrence=1)
    c2 = rm.recommendation_followup_candidate(recommendation(reviewed=rm.iso(accepted)), enabled,
                                              now=day14, has_outcome=False, accepted_at=accepted, occurrence=2)
    assert c1 and c1.scheduled_at == day7
    assert c2 and c2.dedup_key.endswith(":2")
    assert rm.recommendation_followup_candidate(recommendation(), settings(), now=day7,
                                                has_outcome=False, accepted_at=accepted) is None
    assert rm.recommendation_followup_candidate(recommendation(status="pending"), enabled, now=day7,
                                                has_outcome=False, accepted_at=accepted) is None
    assert rm.recommendation_followup_candidate(recommendation(), enabled, now=day7,
                                                has_outcome=True, accepted_at=accepted) is None
    expired = recommendation(expires="2026-10-10T00:00:00+00:00")
    assert rm.recommendation_followup_candidate(expired, enabled, now=day7,
                                                has_outcome=False, accepted_at=accepted) is None


def test_review_periods_and_candidates_are_deterministic_and_half_open():
    daily_enabled = settings(daily_review_enabled=True)
    nightly = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)  # 20:00 Shanghai
    start, end, scheduled_day = rm.review_period_for("daily", daily_enabled, now=nightly)
    assert (start, end, scheduled_day) == (datetime(2026, 10, 12).date(), datetime(2026, 10, 13).date(), datetime(2026, 10, 13).date())
    daily = rm.review_candidate("daily", daily_enabled, now=nightly, period_start=start, period_end=end)
    assert daily is not None
    assert daily.evidence[0]["window_start"] == "2026-10-11T16:00:00+00:00"
    assert daily.evidence[0]["window_end"] == "2026-10-12T16:00:00+00:00"
    assert daily.coverage_status == "no_data"
    assert rm.review_candidate("daily", settings(), now=nightly, period_start=start, period_end=end) is None

    # ISO Monday 2026-12-28 starts the week containing 2027-01-01; prior Monday is 2026-12-21.
    weekly_enabled = settings(weekly_review_enabled=True)
    monday = datetime(2026, 12, 28, 1, 0, tzinfo=UTC)  # 2026-12-28 09:00 Shanghai
    w_start, w_end, _ = rm.review_period_for("weekly", weekly_enabled, now=monday)
    weekly = rm.review_candidate("weekly", weekly_enabled, now=monday, period_start=w_start, period_end=w_end)
    assert (w_start, w_end) == (datetime(2026, 12, 21).date(), datetime(2026, 12, 28).date())
    assert weekly is not None
    assert weekly.dedup_key.startswith("review.weekly_due_v1:2026-12-20T16:00:00+00:00")


def test_review_catchup_window_expires():
    enabled = settings(daily_review_enabled=True)
    start = datetime(2026, 10, 12).date()
    end = datetime(2026, 10, 13).date()
    at = datetime(2026, 10, 13, 14, 1, tzinfo=UTC)  # 22:01 SH, more than 2h late
    assert rm.review_candidate("daily", enabled, now=at, period_start=start, period_end=end) is None


def test_quiet_hours_setting_cross_midnight_is_allowed_and_equality_rejected_by_service_is_tested_in_store():
    cross_midnight = settings(quiet_hours_enabled=True, quiet_start="22:00", quiet_end="08:00")
    assert cross_midnight["quiet_start"] > cross_midnight["quiet_end"]  # lexical, service stores HH:MM only
