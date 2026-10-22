"""Personal AI OS 1.3.4-C: recommendation feedback/outcome HTTP API tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import recommendations as rec_store
from app.main import app, init_db

client = TestClient(app)
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
# Relative instead of a hardcoded date: `resume_after_at` must be in the future,
# so a literal timestamp turns these tests red as soon as the wall clock passes it.
FUTURE_RESUME = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_feedback_api.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("RECOMMENDATIONS_USE_MODEL", raising=False)
    init_db()
    return db_path


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _create_recommendation(db_path, *, status="pending", expired=False):
    conn = _connect(db_path)
    next_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM recommendations").fetchone()[0])
    created = rec_store.create_recommendation(conn, {
        "title": "测试建议",
        "content": "把下一步拆成一个 15 分钟可完成的小任务。",
        "category": "task",
        "priority": "high",
        "confidence": 0.82,
        "evidence": [{
            "kind": "metric",
            "ref_type": "analytics_metric",
            "label": "逾期任务数",
            "value": 3,
            "detail": "最近 7 天",
        }],
        "rule_code": f"test.overdue_task.{next_id}",
        "window": {"days": 7, "timezone": "Asia/Shanghai"},
    }, now=NOW)
    assert created["status"] == "created", created
    rec_id = created["recommendation"]["id"]
    if status == "accepted":
        rec_store.transition_status(conn, rec_id, "accept", now=NOW)
    if expired:
        conn.execute(
            "UPDATE recommendations SET expires_at=? WHERE id=?",
            ("2026-10-01T00:00:00+00:00", rec_id),
        )
    conn.commit()
    conn.close()
    return rec_id


def _event_count(db_path, event_type):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM behavior_events WHERE event_type=?",
            (event_type,),
        ).fetchone()[0]
    finally:
        conn.close()


def test_feedback_get_empty_and_unknown_recommendation_404(isolated_db):
    rec_id = _create_recommendation(isolated_db)
    res = client.get(f"/api/recommendations/{rec_id}/feedback")
    assert res.status_code == 200, res.text
    assert res.json() == {"feedback": None}

    assert client.get("/api/recommendations/99999/feedback").status_code == 404
    assert client.put("/api/recommendations/99999/feedback", json={
        "vote": "helpful", "reason_codes": ["actionable"],
    }).status_code == 404
    assert client.delete("/api/recommendations/99999/feedback").status_code == 404


def test_feedback_create_partial_update_explicit_clear_and_delete(isolated_db):
    rec_id = _create_recommendation(isolated_db)
    payload = {
        "vote": "helpful",
        "reason_codes": ["actionable", "evidence_clear"],
        "note": "证据和下一步都很清楚。",
        "preference_scope": "temporary",
        "resume_after_at": FUTURE_RESUME,
    }
    created = client.put(f"/api/recommendations/{rec_id}/feedback", json=payload)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "created"
    assert body["feedback"]["vote"] == "helpful"
    assert body["feedback"]["source"] == "user"
    assert body["feedback"]["resume_after_at"] == FUTURE_RESUME

    partial = client.put(f"/api/recommendations/{rec_id}/feedback", json={
        "vote": "unhelpful",
        "reason_codes": ["not_now"],
    })
    assert partial.status_code == 200, partial.text
    updated = partial.json()["feedback"]
    assert updated["vote"] == "unhelpful"
    assert updated["reason_codes"] == ["not_now"]
    assert updated["note"] == "证据和下一步都很清楚。"

    cleared = client.put(f"/api/recommendations/{rec_id}/feedback", json={
        "preference_scope": "unspecified",
        "resume_after_at": None,
        "note": None,
        "reason_codes": None,
    })
    assert cleared.status_code == 200, cleared.text
    final = cleared.json()["feedback"]
    assert final["preference_scope"] == "unspecified"
    assert final["resume_after_at"] is None
    assert final["note"] == ""

    assert client.delete(f"/api/recommendations/{rec_id}/feedback").json() == {"deleted": True}
    assert client.get(f"/api/recommendations/{rec_id}/feedback").json() == {"feedback": None}
    assert client.delete(f"/api/recommendations/{rec_id}/feedback").status_code == 404


@pytest.mark.parametrize("payload", [
    {"vote": "maybe"},
    {"vote": "helpful", "reason_codes": ["not_now"]},
    {"vote": "helpful", "note": "x" * 1001},
    {"vote": "unhelpful", "preference_scope": "temporary"},
    {"vote": "helpful", "preference_scope": "persistent",
     "resume_after_at": FUTURE_RESUME},
    {"vote": "helpful", "resume_after_at": "not-a-time"},
    {"vote": "helpful", "source": "model"},
    {"vote": "helpful", "id": 1},
])
def test_feedback_rejects_invalid_payload_with_400(isolated_db, payload):
    rec_id = _create_recommendation(isolated_db)
    res = client.put(f"/api/recommendations/{rec_id}/feedback", json=payload)
    assert res.status_code == 400, res.text
    assert client.get(f"/api/recommendations/{rec_id}/feedback").json() == {"feedback": None}


def test_feedback_and_outcome_reject_non_object_bodies_with_400(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    assert client.put(f"/api/recommendations/{rec_id}/feedback", json=["helpful"]).status_code == 400
    assert client.put(f"/api/recommendations/{rec_id}/outcome", json="planned").status_code == 400
    assert client.put(f"/api/recommendations/{rec_id}/feedback", json=None).status_code == 400


def test_outcome_empty_requires_accepted_and_unknown_recommendation_404(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="pending")
    assert client.get(f"/api/recommendations/{rec_id}/outcome").json() == {"outcome": None}
    res = client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "planned",
    })
    assert res.status_code == 409, res.text
    assert client.get(f"/api/recommendations/{rec_id}/outcome").json() == {"outcome": None}
    assert client.get("/api/recommendations/99999/outcome").status_code == 404
    assert client.put("/api/recommendations/99999/outcome", json={
        "execution_status": "planned",
    }).status_code == 404


def test_outcome_create_update_validate_and_delete(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    planned = client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "planned",
        "result_rating": "unclear",
        "actual_effect_note": "本周计划先完成一个子任务。",
        "metric_observations": [],
    })
    assert planned.status_code == 200, planned.text
    planned_body = planned.json()
    assert planned_body["status"] == "created"
    assert planned_body["outcome"]["occurred_at"] is None
    assert planned_body["outcome"]["causality_status"] == "not_inferred"
    assert planned_body["outcome"]["source"] == "user"

    metric = {
        "metric": "tasks_completed",
        "expected_value": 1,
        "observed_value": 1,
        "unit": "count",
        "window": {"days": 7, "timezone": "Asia/Shanghai"},
        "source": "user_reported",
        "captured_at": "2026-10-12T10:00:00+00:00",
        "attribution": "not_inferred",
    }
    completed = client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "completed",
        "result_rating": "better",
        "metric_observations": [metric],
        "occurred_at": "2026-10-12T10:30:00+00:00",
    })
    assert completed.status_code == 200, completed.text
    outcome = completed.json()["outcome"]
    assert outcome["execution_status"] == "completed"
    assert outcome["actual_effect_note"] == "本周计划先完成一个子任务。"
    assert outcome["metric_observations"][0]["metric"] == "tasks_completed"
    assert outcome["occurred_at"] == "2026-10-12T10:30:00+00:00"

    assert client.delete(f"/api/recommendations/{rec_id}/outcome").json() == {"deleted": True}
    assert client.get(f"/api/recommendations/{rec_id}/outcome").json() == {"outcome": None}
    assert client.delete(f"/api/recommendations/{rec_id}/outcome").status_code == 404


@pytest.mark.parametrize("payload", [
    {"execution_status": "done_by_ai"},
    {"execution_status": "completed", "result_rating": "perfect"},
    {"execution_status": "planned", "actual_effect_note": "x" * 1001},
    {"execution_status": "planned", "occurred_at": "2026-10-12T10:00:00+00:00"},
    {"execution_status": "completed", "occurred_at": "2099-01-01T00:00:00+00:00"},
    {"execution_status": "completed", "metric_observations": [{"metric": "unknown"}]},
    {"execution_status": "planned", "causality_status": "caused"},
    {"execution_status": "planned", "source": "model"},
])
def test_outcome_rejects_invalid_payload_with_400(isolated_db, payload):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    before = _event_count(isolated_db, "recommendation.outcome_saved")
    res = client.put(f"/api/recommendations/{rec_id}/outcome", json=payload)
    assert res.status_code == 400, res.text
    assert client.get(f"/api/recommendations/{rec_id}/outcome").json() == {"outcome": None}
    assert _event_count(isolated_db, "recommendation.outcome_saved") == before


def test_outcome_is_visible_but_not_mutable_after_reopen(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "completed",
        "result_rating": "better",
    })
    assert client.post(f"/api/recommendations/{rec_id}/reopen").status_code == 200

    visible = client.get(f"/api/recommendations/{rec_id}/outcome")
    assert visible.status_code == 200
    assert visible.json()["outcome"]["execution_status"] == "completed"

    update = client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "not_completed",
    })
    assert update.status_code == 409, update.text
    delete = client.delete(f"/api/recommendations/{rec_id}/outcome")
    assert delete.status_code == 409, delete.text
    assert client.get(f"/api/recommendations/{rec_id}/outcome").json()["outcome"]["result_rating"] == "better"


def test_expired_accepted_outcome_cannot_be_mutated(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted", expired=True)
    res = client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "planned",
    })
    assert res.status_code == 409, res.text
    assert client.get(f"/api/recommendations/{rec_id}/outcome").json() == {"outcome": None}


def test_feedback_and_outcome_are_isolated_between_recommendations(isolated_db):
    first = _create_recommendation(isolated_db, status="accepted")
    second = _create_recommendation(isolated_db, status="accepted")
    client.put(f"/api/recommendations/{first}/feedback", json={
        "vote": "helpful", "note": "第一条备注", "reason_codes": ["relevant"],
    })
    client.put(f"/api/recommendations/{first}/outcome", json={
        "execution_status": "planned", "actual_effect_note": "第一条结果",
    })

    assert client.get(f"/api/recommendations/{second}/feedback").json() == {"feedback": None}
    assert client.get(f"/api/recommendations/{second}/outcome").json() == {"outcome": None}
    assert client.get(f"/api/recommendations/{first}/feedback").json()["feedback"]["note"] == "第一条备注"
    assert client.get(f"/api/recommendations/{first}/outcome").json()["outcome"]["actual_effect_note"] == "第一条结果"


def test_feedback_and_outcome_support_xss_text_as_json(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    malicious = "<img src=x onerror=alert(1)>\"'><script>alert(2)</script>"
    res = client.put(f"/api/recommendations/{rec_id}/feedback", json={
        "vote": "helpful",
        "reason_codes": ["other"],
        "note": malicious,
    })
    assert res.status_code == 200
    assert res.json()["feedback"]["note"] == malicious
    assert "<script>" in res.text  # JSON encoding does not execute in the browser.

    outcome = client.put(f"/api/recommendations/{rec_id}/outcome", json={
        "execution_status": "no_longer_relevant",
        "actual_effect_note": malicious,
    })
    assert outcome.json()["outcome"]["actual_effect_note"] == malicious


def test_feedback_and_outcome_state_changes_pass_same_origin_and_csrf_checks(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    same_origin = client.put(
        f"/api/recommendations/{rec_id}/feedback",
        json={"vote": "helpful", "reason_codes": ["other"]},
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
    )
    assert same_origin.status_code == 200, same_origin.text

    cross_origin = client.put(
        f"/api/recommendations/{rec_id}/feedback",
        json={"vote": "unhelpful", "reason_codes": ["not_now"]},
        headers={"Host": "127.0.0.1:8000", "Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403
    cross_site_delete = client.request(
        "DELETE",
        f"/api/recommendations/{rec_id}/feedback",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site_delete.status_code == 403
    bad_host = client.put(
        f"/api/recommendations/{rec_id}/feedback",
        json={"vote": "unhelpful", "reason_codes": ["not_now"]},
        headers={"Host": "evil.example"},
    )
    assert bad_host.status_code == 400
    assert client.get(f"/api/recommendations/{rec_id}/feedback").json()["feedback"]["vote"] == "helpful"


def test_recommendations_ui_wires_feedback_and_escapes_raw_html(frontend_bundle, frontend_sources):
    # The old assertions targeted app/static/recommendations.js (removed 2026-10-22).
    # Feedback is wired in the Vue page; the outcome panel is still missing, so only
    # the feedback contract is asserted here, plus a real escaping guard: the UI
    # must not opt out of Vue's text interpolation anywhere.
    assert "/feedback" in frontend_bundle
    assert "v-html" not in frontend_sources
    assert "innerHTML" not in frontend_sources
    # The built bundle must not resurrect inline on* handlers for dynamic actions.
    assert "onclick=" not in frontend_bundle

# ============================ 1.3.4-E malformed request boundary ============================

def test_feedback_and_outcome_malformed_json_does_not_return_500(isolated_db):
    rec_id = _create_recommendation(isolated_db, status="accepted")
    for path in ("feedback", "outcome"):
        res = client.put(
            f"/api/recommendations/{rec_id}/{path}",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )
        assert res.status_code in (400, 422), res.text
        assert "Traceback" not in res.text
        assert "sqlite3" not in res.text.lower()
