"""Personal AI OS 1.3.4-D: recommendation effectiveness statistics tests."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import recommendation_feedback as feedback_store
from app import recommendations as rec_store
from app.main import app, init_db

client = TestClient(app)
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=30)
END = NOW + timedelta(minutes=4)
# Relative instead of a hardcoded date: `resume_after_at` must be in the future.
FUTURE_RESUME = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_stats.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("RECOMMENDATIONS_USE_MODEL", raising=False)
    init_db()
    return db_path


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_rec(db_path, number, *, category="task", status="pending", now=NOW, evidence=None):
    conn = connect(db_path)
    try:
        result = rec_store.create_recommendation(conn, {
            "title": f"建议 {number} <img src=x onerror=alert(1)>",
            "content": "建议正文包含不应由统计接口返回的自由文本。",
            "category": category,
            "priority": "normal",
            "confidence": 0.72,
            "evidence": evidence or [{
                "kind": "metric",
                "ref_type": "analytics_metric",
                "label": "可观测指标",
                "value": number,
                "detail": "最近 7 天",
            }],
            "rule_code": f"test.stats.{number}",
            "coverage_status": "ok",
            "window": {"days": 7, "timezone": "Asia/Shanghai"},
        }, now=now)
        rec_id = result["recommendation"]["id"]
        if status == "accepted":
            rec_store.transition_status(conn, rec_id, "accept", now=now)
        elif status == "ignored":
            rec_store.transition_status(conn, rec_id, "ignore", now=now)
        elif status == "rejected":
            rec_store.transition_status(conn, rec_id, "reject", now=now)
        conn.commit()
        return rec_id
    finally:
        conn.close()


def table_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    finally:
        conn.close()


def test_empty_database_returns_zero_denominators_as_null(tmp_path, monkeypatch):
    from app.recommendation_stats import get_recommendation_statistics

    db_path = tmp_path / "phase_4_stats_empty.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    conn = connect(db_path)
    try:
        data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    finally:
        conn.close()

    assert data["definition_version"] == "recommendation_feedback_stats_v1"
    assert data["window"]["start_at"].startswith("2026-09-12T09:00:00")
    assert data["window"]["end_at"].startswith("2026-10-12T09:04:00")
    assert data["recommendations"]["total"] == 0
    assert data["recommendations"]["status_counts"] == {
        "pending": 0, "accepted": 0, "ignored": 0, "rejected": 0, "expired": 0,
    }
    assert data["feedback"]["feedback_rate"] is None
    assert data["feedback"]["helpful_rate"] is None
    assert data["adoption"]["current_accepted_rate"] is None
    assert data["outcomes"]["registration_rate"] is None
    assert data["outcomes"]["completion_rate"] is None
    assert data["quality"]["sample_status"] == "no_data"
    assert data["runs"]["status_counts"] == {
        "running": 0, "success": 0, "no_evidence": 0, "degraded": 0, "failed": 0,
    }


def test_statistics_count_current_feedback_outcomes_statuses_events_and_runs(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    r1 = create_rec(isolated_db, 1, category="task", status="accepted")
    r2 = create_rec(isolated_db, 2, category="project", status="accepted")
    r3 = create_rec(isolated_db, 3, category="habit", status="ignored")
    r4 = create_rec(isolated_db, 4, category="learning", status="rejected")
    r5 = create_rec(isolated_db, 5, category="task", status="pending")
    r6 = create_rec(isolated_db, 6, category="goal", status="accepted")

    conn = connect(isolated_db)
    conn.execute("UPDATE recommendations SET expires_at=? WHERE id=?", ("2026-10-01T00:00:00+00:00", r5))
    conn.execute("UPDATE recommendations SET evidence_json='[]', coverage_status='partial_data' WHERE id=?", (r4,))
    feedback_store.upsert_feedback(conn, r1, {
        "vote": "helpful", "reason_codes": ["actionable", "relevant"],
        "note": "备注自由文本 <b>x</b>", "preference_scope": "unspecified",
    }, now=NOW)
    feedback_store.upsert_feedback(conn, r2, {
        "vote": "unhelpful", "reason_codes": ["irrelevant"],
        "preference_scope": "temporary", "resume_after_at": FUTURE_RESUME,
    }, now=NOW)
    feedback_store.upsert_feedback(conn, r3, {
        "vote": "helpful", "reason_codes": ["evidence_clear"],
        "preference_scope": "persistent",
    }, now=NOW)
    feedback_store.upsert_outcome(conn, r1, {
        "execution_status": "completed", "result_rating": "better",
        "actual_effect_note": "结果自由文本 <script>alert(1)</script>",
        "metric_observations": [{
            "metric": "tasks_completed", "expected_value": 1, "observed_value": 2,
            "unit": "count", "window": {"days": 7}, "source": "user_reported",
            "captured_at": "2026-10-12T08:00:00+00:00", "attribution": "not_inferred",
        }],
    }, now=NOW)
    feedback_store.upsert_outcome(conn, r2, {
        "execution_status": "planned", "result_rating": "unclear",
        "metric_observations": [{
            "metric": "tasks_completed", "expected_value": 1, "observed_value": None,
            "unit": "count", "window": {"days": 7}, "source": "user_reported",
            "captured_at": "2026-10-12T08:00:00+00:00", "attribution": "not_inferred",
        }],
    }, now=NOW)
    feedback_store.upsert_outcome(conn, r6, {
        "execution_status": "completed", "result_rating": "much_better",
    }, now=NOW)
    rec_store.transition_status(conn, r6, "reopen", now=NOW + timedelta(minutes=1))
    run_id = rec_store.start_run(conn, now=NOW)
    rec_store.finish_run(conn, run_id, status="success", candidates=3, created=2, now=NOW)
    no_evidence_run = rec_store.start_run(conn, now=NOW + timedelta(minutes=2))
    rec_store.finish_run(conn, no_evidence_run, status="no_evidence", now=NOW + timedelta(minutes=3))
    old_run = rec_store.start_run(conn, now=START - timedelta(days=1))
    rec_store.finish_run(conn, old_run, status="failed", error="failed", now=START - timedelta(days=1))
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    conn.close()

    rec = data["recommendations"]
    assert rec["total"] == 6
    assert rec["status_counts"] == {
        "pending": 2, "accepted": 2, "ignored": 1, "rejected": 1, "expired": 0,
    }
    assert rec["category_counts"] == {"task": 2, "project": 1, "goal": 1, "learning": 1, "habit": 1}
    assert rec["logically_expired_active_count"] == 1
    assert rec["coverage_counts"] == {"ok": 5, "partial_data": 1, "insufficient_data": 0, "no_data": 0}

    feedback = data["feedback"]
    assert feedback["total"] == 3
    assert feedback["vote_counts"] == {"helpful": 2, "unhelpful": 1}
    assert feedback["feedback_rate"] == {"numerator": 3, "denominator": 6, "value": 0.5, "sample_status": "insufficient_data"}
    assert feedback["helpful_rate"] == {"numerator": 2, "denominator": 3, "value": 0.6667, "sample_status": "insufficient_data"}
    assert feedback["reason_code_counts"]["actionable"] == 1
    assert feedback["reason_code_counts"]["relevant"] == 1
    assert feedback["preference_scope_counts"] == {"temporary": 1, "persistent": 1, "unspecified": 1}
    assert feedback["record_type"] == "current_only"

    adoption = data["adoption"]
    assert adoption["current_accepted_count"] == 2
    assert adoption["current_accepted_rate"]["numerator"] == 2
    assert adoption["events"]["accepted_event_count"] == 3
    assert adoption["events"]["distinct_recommendation_count"] == 3
    assert adoption["events"]["event_counts"]["reopened"] == 1
    assert adoption["events"]["history_completeness"] == "partial_observability"

    outcomes = data["outcomes"]
    assert outcomes["total"] == 3
    assert outcomes["current_accepted_count"] == 2
    assert outcomes["registered_outcome_count"] == 2
    assert outcomes["registration_rate"] == {"numerator": 2, "denominator": 2, "value": 1.0, "sample_status": "insufficient_data"}
    assert outcomes["retained_after_reopen_count"] == 1
    assert outcomes["execution_status_counts"] == {
        "planned": 1, "completed": 2, "partially_completed": 0,
        "not_completed": 0, "no_longer_relevant": 0,
    }
    assert outcomes["completion_rate"] == {"numerator": 2, "denominator": 3, "value": 0.6667, "sample_status": "insufficient_data"}
    assert outcomes["rating_counts"] == {
        "much_better": 1, "better": 1, "as_expected": 0, "worse": 0, "much_worse": 0, "unclear": 1,
    }
    assert outcomes["comparable_metric_groups"] == [{
        "metric": "tasks_completed", "unit": "count", "sample_count": 1,
        "average_expected_value": 1.0, "average_observed_value": 2.0, "average_difference": 1.0,
        "sample_status": "insufficient_data", "causality_status": "not_inferred",
    }]
    assert data["ratings"]["denominator"] == 3
    assert data["ratings"]["rates"]["better"] == {
        "numerator": 1, "denominator": 3, "value": 0.3333, "sample_status": "insufficient_data",
    }
    assert data["ratings"]["rates"]["unclear"]["numerator"] == 1

    runs = data["runs"]
    assert runs["total"] == 2
    assert runs["status_counts"] == {"running": 0, "success": 1, "no_evidence": 1, "degraded": 0, "failed": 0}
    assert runs["completed_normally_count"] == 2
    assert data["quality"]["composite_score"] is None
    assert any("历史" in item for item in data["limitations"])


def test_feedback_update_counts_only_current_value(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    rec_id = create_rec(isolated_db, 1)
    conn = connect(isolated_db)
    feedback_store.upsert_feedback(conn, rec_id, {"vote": "helpful", "reason_codes": ["actionable"]}, now=NOW)
    feedback_store.upsert_feedback(conn, rec_id, {"vote": "unhelpful", "reason_codes": ["irrelevant"]}, now=NOW)
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    conn.close()
    assert data["feedback"]["total"] == 1
    assert data["feedback"]["vote_counts"] == {"helpful": 0, "unhelpful": 1}
    assert data["feedback"]["helpful_rate"]["numerator"] == 0


def test_lifecycle_events_use_event_time_even_when_recommendation_predates_window(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    old_rec_id = create_rec(isolated_db, 99, now=START - timedelta(days=2))
    conn = connect(isolated_db)
    conn.execute(
        "UPDATE recommendations SET expires_at=? WHERE id=?",
        ((NOW + timedelta(days=1)).isoformat(), old_rec_id),
    )
    conn.commit()
    rec_store.transition_status(conn, old_rec_id, "accept", now=NOW - timedelta(minutes=5))
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    conn.close()
    assert data["recommendations"]["total"] == 0
    assert data["adoption"]["events"]["accepted_event_count"] == 1
    assert data["adoption"]["events"]["distinct_recommendation_count"] == 1


def test_half_open_time_window_excludes_end_boundary(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    at_start = create_rec(isolated_db, 1, now=START)
    at_end = create_rec(isolated_db, 2, now=END)
    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=END)
    conn.close()
    assert data["recommendations"]["total"] == 1
    assert data["recommendations"]["status_counts"]["pending"] == 1
    assert at_start != at_end


def test_deleted_recommendation_cascades_and_disappears_from_stats(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    rec_id = create_rec(isolated_db, 1, status="accepted")
    conn = connect(isolated_db)
    feedback_store.upsert_feedback(conn, rec_id, {"vote": "helpful"}, now=NOW)
    feedback_store.upsert_outcome(conn, rec_id, {"execution_status": "completed", "result_rating": "better"}, now=NOW)
    rec_store.delete_recommendation(conn, rec_id)
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    conn.close()
    assert data["recommendations"]["total"] == 0
    assert data["feedback"]["total"] == 0
    assert data["outcomes"]["total"] == 0


def test_invalid_time_range_rejected(tmp_path, monkeypatch):
    from app.recommendation_stats import StatsError, get_recommendation_statistics

    db_path = tmp_path / "phase_4_stats_invalid.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    conn = connect(db_path)
    try:
        with pytest.raises(StatsError) as exc:
            get_recommendation_statistics(conn, start_at=END, end_at=START, now=NOW)
        assert exc.value.status_code == 400
    finally:
        conn.close()


def test_api_validates_window_and_is_read_only_and_private(isolated_db):
    rec_id = create_rec(isolated_db, 1, status="accepted")
    conn = connect(isolated_db)
    feedback_store.upsert_feedback(conn, rec_id, {
        "vote": "helpful", "note": "SECRET_NOTE <img src=x onerror=alert(1)>",
    }, now=NOW)
    feedback_store.upsert_outcome(conn, rec_id, {
        "execution_status": "completed", "result_rating": "better",
        "actual_effect_note": "SECRET_OUTCOME",
    }, now=NOW)
    conn.commit()
    conn.close()
    before = table_counts(isolated_db)

    res = client.get("/api/recommendation-statistics", params={
        "start_at": START.isoformat(), "end_at": END.isoformat(),
    })
    assert res.status_code == 200, res.text
    body = res.text
    assert "SECRET_NOTE" not in body
    assert "SECRET_OUTCOME" not in body
    assert "onerror" not in body
    assert res.json()["recommendations"]["total"] == 1
    assert table_counts(isolated_db) == before

    bad = client.get("/api/recommendation-statistics", params={"start_at": "not-a-date"})
    assert bad.status_code == 400, bad.text
    inverted = client.get("/api/recommendation-statistics", params={
        "start_at": END.isoformat(), "end_at": START.isoformat(),
    })
    assert inverted.status_code == 400, inverted.text


def test_api_repeated_calls_are_stable_and_do_not_invoke_model(isolated_db, monkeypatch):
    create_rec(isolated_db, 1)
    params = {"start_at": START.isoformat(), "end_at": END.isoformat()}

    def fail_model(*_args, **_kwargs):
        raise AssertionError("statistics must not call the model")

    monkeypatch.setattr("app.recommendation_engine.explain_with_model", fail_model)
    first = client.get("/api/recommendation-statistics", params=params)
    second = client.get("/api/recommendation-statistics", params=params)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_frontend_ships_only_the_referenced_bundle():
    """Guard against stale hashed bundles accumulating in app/static/assets.

    The old body of this test read app/static/recommendations.js, removed
    2026-10-22 together with the other hand-written scripts. The Vue statistics
    panel does not exist yet (docs/progress.md backlog), so what remains worth
    asserting is that the build output is exactly what index.html references —
    which is what ``emptyOutDir: true`` is for.
    """
    static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'/static/(assets/[^"]+\.(?:js|css))', index_html))
    actual = {f"assets/{p.name}" for p in (static_dir / "assets").iterdir() if p.is_file()}
    assert referenced, "index.html 未引用任何构建产物"
    assert referenced == actual, f"存在未被 index.html 引用的残留产物：{sorted(actual - referenced)}"


# ============================ 1.3.4-E statistics hardening ============================

def test_subsecond_query_boundaries_match_stored_second_precision(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    boundary = datetime(2026, 10, 12, 9, 4, tzinfo=timezone.utc)
    rec_id = create_rec(isolated_db, 1, status="accepted", now=boundary)
    conn = connect(isolated_db)
    rec_store.transition_status(conn, rec_id, "reopen", now=boundary)
    run_id = rec_store.start_run(conn, now=boundary)
    rec_store.finish_run(conn, run_id, status="success", now=boundary)
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    half_second_end = boundary + timedelta(microseconds=500_000)
    included = get_recommendation_statistics(
        conn,
        start_at=boundary - timedelta(minutes=1),
        end_at=half_second_end,
        now=half_second_end,
    )
    assert included["window"]["time_resolution"] == "stored_seconds"
    assert included["recommendations"]["total"] == 1
    assert included["adoption"]["events"]["event_counts"]["reopened"] == 1
    assert included["runs"]["total"] == 1

    exact_excluded = get_recommendation_statistics(
        conn,
        start_at=boundary - timedelta(minutes=1),
        end_at=boundary,
        now=boundary,
    )
    assert exact_excluded["recommendations"]["total"] == 0
    assert exact_excluded["runs"]["total"] == 0

    after_fractional_start = get_recommendation_statistics(
        conn,
        start_at=boundary + timedelta(microseconds=500_000),
        end_at=boundary + timedelta(minutes=1),
        now=boundary + timedelta(minutes=1),
    )
    assert after_fractional_start["recommendations"]["total"] == 0
    conn.close()


def test_statistics_ignore_corrupt_vocabulary_and_non_comparable_metrics(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    rec_id = create_rec(isolated_db, 1, status="accepted")
    conn = connect(isolated_db)
    feedback_store.upsert_feedback(conn, rec_id, {"vote": "helpful", "reason_codes": ["other"]})
    feedback_store.upsert_outcome(conn, rec_id, {"execution_status": "completed"})
    conn.execute(
        "UPDATE recommendation_feedback SET reason_codes_json=? WHERE recommendation_id=?",
        ('["other","injected_from_corrupt_row"]', rec_id),
    )
    conn.execute(
        "UPDATE recommendation_outcomes SET metric_observations_json=? WHERE recommendation_id=?",
        ('''[
            {"metric":"tasks_created","expected_value":1,"observed_value":2,"unit":"count",
             "window":{},"source":"user_reported","captured_at":"2026-10-12T08:00:00+00:00"},
            {"metric":"injected_metric","expected_value":1,"observed_value":2,"unit":"count",
             "window":{},"source":"user_reported","captured_at":"2026-10-12T08:00:00+00:00"},
            {"metric":"tasks_created","expected_value":1,"observed_value":2,"unit":"minutes",
             "window":{},"source":"user_reported","captured_at":"2026-10-12T08:00:00+00:00"},
            {"metric":"tasks_completed","expected_value":NaN,"observed_value":2,"unit":"count",
             "window":{},"source":"user_reported","captured_at":"2026-10-12T08:00:00+00:00"}
        ]''', rec_id),
    )
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    conn.close()
    assert data["feedback"]["reason_code_counts"] == {"other": 1}
    assert data["outcomes"]["comparable_metric_groups"] == [
        {
            "metric": "tasks_created",
            "unit": "count",
            "sample_count": 1,
            "average_expected_value": 1.0,
            "average_observed_value": 2.0,
            "average_difference": 1.0,
            "sample_status": "insufficient_data",
            "causality_status": "not_inferred",
        },
        {
            "metric": "tasks_created",
            "unit": "minutes",
            "sample_count": 1,
            "average_expected_value": 1.0,
            "average_observed_value": 2.0,
            "average_difference": 1.0,
            "sample_status": "insufficient_data",
            "causality_status": "not_inferred",
        },
    ]
    assert all(group["metric"] != "injected_metric" for group in data["outcomes"]["comparable_metric_groups"])


def test_statistics_reuse_callers_transaction_without_rolling_it_back(isolated_db):
    from app.recommendation_stats import get_recommendation_statistics

    rec_id = create_rec(isolated_db, 1)
    conn = connect(isolated_db)
    feedback_store.upsert_feedback(conn, rec_id, {"vote": "helpful", "reason_codes": ["other"]})
    assert conn.in_transaction is True
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    assert conn.in_transaction is True
    assert data["feedback"]["total"] == 1
    conn.rollback()
    assert conn.in_transaction is False
    conn.close()

    checker = connect(isolated_db)
    assert feedback_store.get_feedback(checker, rec_id) is None
    checker.close()


def test_statistics_chunk_id_lists_avoid_sqlite_variable_limit(isolated_db, monkeypatch):
    import app.recommendation_stats as stats
    from app.recommendation_stats import get_recommendation_statistics

    monkeypatch.setattr(stats, "MIN_STABLE_SAMPLE", 1)
    ids = [create_rec(isolated_db, index, status="accepted") for index in range(1, 1003)]
    conn = connect(isolated_db)
    helpful_ids = ids[::2]
    for index, rec_id in enumerate(helpful_ids, start=1):
        feedback_store.upsert_feedback(conn, rec_id, {"vote": "helpful", "reason_codes": ["other"]})
        if index % 5 == 0:
            feedback_store.upsert_outcome(conn, rec_id, {"execution_status": "completed"})
    conn.commit()
    conn.close()

    conn = connect(isolated_db)
    data = get_recommendation_statistics(conn, start_at=START, end_at=END, now=NOW)
    conn.close()
    assert data["recommendations"]["total"] == 1002
    assert data["feedback"]["total"] == len(helpful_ids)
    assert data["feedback"]["feedback_rate"]["denominator"] == 1002
    assert data["feedback"]["reason_code_counts"] == {"other": len(helpful_ids)}
    assert data["outcomes"]["total"] == len(helpful_ids) // 5
    assert data["outcomes"]["registration_rate"]["denominator"] == 1002
