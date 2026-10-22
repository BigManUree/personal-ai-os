"""Personal AI OS 1.3.3-D: recommendation HTTP API + page wiring tests.

All runs use an isolated temp DB and rule-only generation; no real model
network request is ever made.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import recommendation_engine as engine
from app import recommendations as store
from app.main import app, init_db

client = TestClient(app)
NOW = datetime(2026, 10, 12, 4, 17, tzinfo=timezone.utc)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_recommendations_api.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("RECOMMENDATIONS_USE_MODEL", raising=False)
    init_db()
    return db_path


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_overdue_task(db_path, task_id, due_date="2026-10-01"):
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO tasks(id,title,detail,status,priority,due_date,project_id,goal_id,"
        "completed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, "不应出现在任何响应中的标题XYZ", "", "todo", "normal", due_date,
         None, None, None, "2026-09-20T00:00:00+00:00", "2026-09-20T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


# ------------------------------- list / detail -------------------------------

def test_list_empty_is_paginated(isolated_db):
    res = client.get("/api/recommendations")
    assert res.status_code == 200, res.text
    data = res.json()
    assert set(data) == {"items", "total", "limit", "offset"}
    assert data["items"] == [] and data["total"] == 0
    assert data["limit"] == 20 and data["offset"] == 0


def test_list_filters_paging_and_detail(isolated_db):
    conn = _connect(isolated_db)
    for i in range(3):
        store.create_recommendation(conn, {
            "title": f"建议{i}", "content": "正文内容", "category": "task",
            "priority": "high" if i == 0 else "low",
            "confidence": 0.7,
            "evidence": [{"kind": "metric", "ref_type": "analytics_metric",
                          "label": "指标", "value": i}],
            "rule_code": f"rule.{i}", "window": {"days": 7},
        })
    conn.commit()
    rec_id = conn.execute("SELECT id FROM recommendations ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()

    page = client.get("/api/recommendations?limit=2&offset=0").json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert page["items"][0]["priority"] == "high"  # priority ordering

    second = client.get("/api/recommendations?limit=2&offset=2").json()
    assert len(second["items"]) == 1

    only_high = client.get("/api/recommendations?priority=high").json()
    assert only_high["total"] == 1

    detail = client.get(f"/api/recommendations/{rec_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == rec_id and body["evidence"][0]["ref_type"] == "analytics_metric"

    assert client.get("/api/recommendations/9999").status_code == 404


def test_list_rejects_bad_query(isolated_db):
    assert client.get("/api/recommendations?status=bogus").status_code == 400
    assert client.get("/api/recommendations?category=bogus").status_code == 400
    assert client.get("/api/recommendations?limit=0").status_code == 422
    assert client.get("/api/recommendations?offset=-1").status_code == 422


# ------------------------------- generate -------------------------------

def test_generate_no_evidence_returns_200_and_creates_nothing(isolated_db):
    res = client.post("/api/recommendations/generate", json={"days": 7})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "no_evidence"
    assert data["counts"]["created"] == 0
    assert data["model_used"] is False
    runs = client.get("/api/recommendation-runs").json()
    assert runs[0]["status"] == "no_evidence"


def test_generate_rule_only_creates_then_deduplicates(isolated_db):
    _insert_overdue_task(isolated_db, 1)
    first = client.post("/api/recommendations/generate", json={"days": 7})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "success"
    assert body["counts"]["created"] == 1
    assert body["created"][0]["source"] == "rule"
    assert body["model_used"] is False

    second = client.post("/api/recommendations/generate", json={"days": 7})
    assert second.json()["counts"]["created"] == 0
    assert second.json()["counts"]["duplicates"] == 1
    assert client.get("/api/recommendations").json()["total"] == 1


def test_generate_body_validation(isolated_db):
    bad_days = client.post("/api/recommendations/generate", json={"days": 5})
    assert bad_days.status_code == 422
    bad_type = client.post("/api/recommendations/generate", json={"days": 7, "use_model": "yes"})
    assert bad_type.status_code == 422


def test_generate_model_flag_is_forced_off_without_server_opt_in(isolated_db, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-used")
    monkeypatch.delenv("RECOMMENDATIONS_USE_MODEL", raising=False)
    _insert_overdue_task(isolated_db, 1)
    res = client.post("/api/recommendations/generate", json={"days": 7, "use_model": True})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["model_requested"] is True
    assert data["model_used"] is False
    assert data["model_disabled_by_server"] is True
    assert data["created"][0]["source"] == "rule"  # no network call happened


def test_generation_lock_blocks_second_run(isolated_db):
    async def scenario():
        conn = _connect(isolated_db)
        async with engine._generation_lock:
            with pytest.raises(engine.GenerationInProgress) as exc:
                await engine.generate(conn, days=7, now=NOW)
            assert exc.value.status_code == 409
        conn.close()
    asyncio.run(scenario())


def test_engine_failure_marks_run_failed(isolated_db):
    conn = _connect(isolated_db)
    with pytest.raises(ValueError):
        asyncio.run(engine.generate(conn, days=5, now=NOW))
    runs = store.list_runs(conn)
    assert runs[0]["status"] == "failed"
    assert runs[0]["finished_at"]
    conn.close()


# ------------------------------- transitions -------------------------------

def _generated_id(isolated_db):
    _insert_overdue_task(isolated_db, 1)
    return client.post("/api/recommendations/generate", json={"days": 7}).json()["created"][0]["id"]


def test_status_transitions_happy_path_and_conflicts(isolated_db):
    rec_id = _generated_id(isolated_db)
    assert client.post(f"/api/recommendations/{rec_id}/accept").json()["status"] == "accepted"
    # cannot accept twice
    assert client.post(f"/api/recommendations/{rec_id}/accept").status_code == 409
    # reopen accepted -> pending, then ignore
    assert client.post(f"/api/recommendations/{rec_id}/reopen").json()["status"] == "pending"
    assert client.post(f"/api/recommendations/{rec_id}/ignore").json()["status"] == "ignored"
    # terminal cannot reopen
    assert client.post(f"/api/recommendations/{rec_id}/reopen").status_code == 409


def test_reject_then_filter_and_unknown_id(isolated_db):
    rec_id = _generated_id(isolated_db)
    assert client.post(f"/api/recommendations/{rec_id}/reject").status_code == 200
    assert client.get("/api/recommendations").json()["total"] == 0  # hidden by default
    rejected = client.get("/api/recommendations?status=rejected").json()
    assert rejected["total"] == 1
    assert client.post("/api/recommendations/9999/accept").status_code == 404
    assert client.post(f"/api/recommendations/{rec_id}/accept").status_code == 409


def test_delete_recommendation(isolated_db):
    rec_id = _generated_id(isolated_db)
    assert client.delete(f"/api/recommendations/{rec_id}").json() == {"deleted": True}
    assert client.get(f"/api/recommendations/{rec_id}").status_code == 404
    assert client.delete("/api/recommendations/{rec_id}").status_code in (404, 422)


def test_runs_list_paging(isolated_db):
    client.post("/api/recommendations/generate", json={"days": 7})
    runs = client.get("/api/recommendation-runs?limit=1&offset=0").json()
    assert len(runs) == 1
    # error field never carries prompts/credentials on no_evidence
    assert runs[0]["error"] == ""


# ------------------------------- frontend wiring -------------------------------

def test_recommendations_page_is_linked_and_bundle_wires_list_and_feedback(frontend_bundle, frontend_sources):
    page = client.get("/")
    assert page.status_code == 200
    # Vue SPA shell
    assert 'id="app"' in page.text
    assert '/static/assets/' in page.text

    # The Vue page lists recommendations, generates them and writes feedback.
    assert "/api/recommendations" in frontend_bundle
    assert "/feedback" in frontend_bundle
    assert "/generate" in frontend_bundle
    # Status transitions share one dynamic URL (/api/recommendations/{id}/{action}),
    # so assert the dispatched actions in the component source instead of the bundle.
    for action in ("accept", "ignore", "reject", "reopen"):
        assert f"act(r, '{action}')" in frontend_sources
    assert "撤回采纳" in frontend_sources
    assert "确认拒绝" in frontend_sources


# ============================ 1.3.3-E browser boundary / run singleton ============================

def test_local_host_and_same_origin_state_request_allowed(isolated_db):
    res = client.post(
        "/api/recommendations/generate",
        json={"days": 7},
        headers={
            "Host": "127.0.0.1:8000",
            "Origin": "http://127.0.0.1:8000",
        },
    )
    assert res.status_code == 200, res.text


def test_cross_origin_state_request_is_rejected_before_route(isolated_db):
    evil_origin = client.post(
        "/api/recommendations/generate",
        json={"days": 7},
        headers={"Host": "127.0.0.1:8000", "Origin": "https://evil.example"},
    )
    assert evil_origin.status_code == 403
    assert client.get("/api/recommendation-runs").json() == []

    fetch_metadata = client.post(
        "/api/recommendations/generate",
        json={"days": 7},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert fetch_metadata.status_code == 403


def test_non_loopback_host_is_rejected_as_dns_rebinding_defense(isolated_db):
    res = client.post(
        "/api/recommendations/generate",
        json={"days": 7},
        headers={"Host": "evil.example"},
    )
    assert res.status_code == 400


def test_database_running_marker_blocks_second_generation(isolated_db):
    conn = _connect(isolated_db)
    store.start_run(conn)
    conn.commit()
    conn.close()

    res = client.post("/api/recommendations/generate", json={"days": 7})
    assert res.status_code == 409
    assert res.json()["detail"] == "已有建议生成任务正在运行，请稍后再试"
