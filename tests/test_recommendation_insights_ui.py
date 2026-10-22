"""004-recommendation-insights: 前端接线断言 + outcome 部分更新契约回归。

The outcome PUT is a **partial** update (``exclude_unset=True`` + service-level
merge), which is the opposite of the work-item PATCH semantics pinned in
``tests/test_work_ui.py``. Both sets of tests exist so the two contracts cannot
silently drift into each other.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, init_db

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
OUTCOME_SRC = (SRC / "components" / "OutcomeForm.vue").read_text(encoding="utf-8")
INSIGHTS_SRC = (SRC / "components" / "RecommendationInsights.vue").read_text(encoding="utf-8")
PAGE_SRC = (SRC / "pages" / "Recommendations.vue").read_text(encoding="utf-8")

CALL_RE = re.compile(r"api\(\s*(['\"`])([^'\"`]+)\1\s*(?:,\s*\{\s*method:\s*'(\w+)')?", re.S)


def _iso(days: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "rec_insights.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    return db_path


def _insert_recommendation(db_path, rec_id: int, status: str, *, expires_days: float = 30) -> None:
    now = _iso()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec_id, f"测试建议 {rec_id}", "内容", "task", "high", status,
            json.dumps([], ensure_ascii=False), "{}", "ok", "收益", "low", "", 0.8,
            "rule", "test.rule_v1", "test", f"test-{rec_id}", _iso(expires_days), now, now, None,
        ),
    )
    conn.commit()
    conn.close()


# ---------- frontend wiring ----------

def test_frontend_calls_all_five_routes():
    combined = "\n".join([OUTCOME_SRC, INSIGHTS_SRC])
    found = {(path, method or "GET") for _q, path, method in CALL_RE.findall(combined)}
    expected = {
        ("/api/recommendations/${props.recId}/outcome", "GET"),
        ("/api/recommendations/${props.recId}/outcome", "PUT"),
        ("/api/recommendations/${props.recId}/outcome", "DELETE"),
        ("/api/recommendation-statistics", "GET"),
        ("/api/recommendation-runs?limit=${RUN_LIMIT}", "GET"),
    }
    missing = expected - found
    assert not missing, f"缺少调用：{sorted(missing)}"


def test_outcome_form_is_wired_only_for_accepted_cards():
    assert "r.status === 'accepted'" in PAGE_SRC
    assert "<OutcomeForm" in PAGE_SRC
    assert ":expired=\"isExpired(r)\"" in PAGE_SRC
    assert "<RecommendationInsights" in PAGE_SRC
    assert "统计与历史" in PAGE_SRC


def test_enums_match_backend_constants():
    # app/recommendation_feedback.py:29-59
    for value in ["planned", "completed", "partially_completed", "not_completed", "no_longer_relevant"]:
        assert value in OUTCOME_SRC
    for value in ["much_better", "better", "as_expected", "worse", "much_worse", "unclear"]:
        assert value in OUTCOME_SRC
    for value in [
        "documents_created", "project_activities", "goals_created", "tasks_created",
        "tasks_completed", "tasks_reopened", "tasks_cancelled", "net_completion_ratio",
        "connector_sync_success_rate",
    ]:
        assert value in OUTCOME_SRC
    for unit in ["count", "ratio", "minutes"]:
        assert unit in OUTCOME_SRC
    # Fixed protocol values must be sent, never exposed as free input.
    assert "user_reported" in OUTCOME_SRC
    assert "not_inferred" in OUTCOME_SRC
    assert "source: 'user_reported'" in OUTCOME_SRC


def test_ratio_renderer_handles_null_and_object_shape():
    assert "'—'" in INSIGHTS_SRC
    body = re.search(r"function fmtRatio\(.*?\n\}", INSIGHTS_SRC, re.S)
    assert body, "未找到 fmtRatio"
    assert "null" in body.group(0) and "'—'" in body.group(0)
    # Non-null ratios arrive as {numerator, denominator, value, sample_status}.
    assert 'ratio.value' in body.group(0)
    # Non-null ratios arrive as {numerator, denominator, value, sample_status}.
    assert 'ratio.value' in body.group(0)
    assert "0%" not in body.group(0)


def test_metric_rows_are_capped_and_editable():
    assert "MAX_METRICS = 10" in OUTCOME_SRC
    assert "metrics.value.length >= MAX_METRICS" in OUTCOME_SRC
    assert "removeMetric" in OUTCOME_SRC


def test_insights_sources_avoid_unsafe_html():
    combined = "\n".join([OUTCOME_SRC, INSIGHTS_SRC, PAGE_SRC])
    for forbidden in ("v-html", "innerHTML", "onclick="):
        assert forbidden not in combined


# ---------- backend contract ----------

def test_first_outcome_put_requires_execution_status(isolated_db):
    _insert_recommendation(isolated_db, 1, "accepted")
    r = client.put("/api/recommendations/1/outcome", json={"result_rating": "better"})
    assert r.status_code == 400, r.text


def test_outcome_put_is_partial_and_preserves_unset_fields(isolated_db):
    _insert_recommendation(isolated_db, 2, "accepted")
    r = client.put("/api/recommendations/2/outcome", json={
        "execution_status": "completed",
        "result_rating": "better",
        "actual_effect_note": "第一次备注",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created"

    # Only the rating changes; status/note must survive — this is the opposite
    # of the work-item PATCH behaviour.
    r = client.put("/api/recommendations/2/outcome", json={"result_rating": "worse"})
    assert r.status_code == 200, r.text
    outcome = r.json()["outcome"]
    assert outcome["result_rating"] == "worse"
    assert outcome["execution_status"] == "completed"
    assert outcome["actual_effect_note"] == "第一次备注"


def test_metric_observations_are_replaced_wholesale(isolated_db):
    _insert_recommendation(isolated_db, 3, "accepted")
    metric = {
        "metric": "tasks_completed",
        "unit": "count",
        "expected_value": 5,
        "observed_value": 3,
        "window": {},
        "source": "user_reported",
        "captured_at": _iso(-1),
        "attribution": "not_inferred",
    }
    r = client.put("/api/recommendations/3/outcome", json={
        "execution_status": "partially_completed",
        "metric_observations": [metric, {**metric, "metric": "tasks_created"}],
    })
    assert r.status_code == 200, r.text
    assert len(r.json()["outcome"]["metric_observations"]) == 2

    r = client.put("/api/recommendations/3/outcome", json={"metric_observations": [metric]})
    assert r.status_code == 200, r.text
    assert len(r.json()["outcome"]["metric_observations"]) == 1


def test_outcome_rejects_non_accepted_recommendation(isolated_db):
    _insert_recommendation(isolated_db, 4, "pending")
    r = client.put("/api/recommendations/4/outcome", json={"execution_status": "completed"})
    assert r.status_code == 409, r.text
    assert "已采纳" in r.json()["detail"]


def test_outcome_get_and_delete_round_trip(isolated_db):
    _insert_recommendation(isolated_db, 5, "accepted")
    assert client.get("/api/recommendations/5/outcome").json()["outcome"] is None
    client.put("/api/recommendations/5/outcome", json={"execution_status": "completed"})
    assert client.get("/api/recommendations/5/outcome").json()["outcome"]["execution_status"] == "completed"
    assert client.delete("/api/recommendations/5/outcome").status_code == 200
    assert client.get("/api/recommendations/5/outcome").json()["outcome"] is None


def test_statistics_empty_database_returns_null_ratios(isolated_db):
    body = client.get("/api/recommendation-statistics").json()
    assert body["recommendations"]["total"] == 0
    assert body["recommendations"]["evidence_present_rate"] is None
    assert body["feedback"]["feedback_rate"] is None
    assert body["quality"]["sample_status"] == "no_data"
    assert len(body["limitations"]) >= 3


def test_statistics_counts_seeded_rows_and_rejects_bad_range(isolated_db):
    _insert_recommendation(isolated_db, 6, "accepted")
    _insert_recommendation(isolated_db, 7, "pending")
    body = client.get("/api/recommendation-statistics").json()
    assert body["recommendations"]["total"] == 2
    assert body["recommendations"]["status_counts"]["accepted"] == 1
    assert body["recommendations"]["status_counts"]["pending"] == 1
    ratio = body["adoption"]["current_accepted_rate"]
    assert ratio["value"] == 0.5 and ratio["numerator"] == 1 and ratio["denominator"] == 2

    r = client.get("/api/recommendation-statistics", params={
        "start_at": _iso(0), "end_at": _iso(-3),
    })
    assert r.status_code == 400
    assert "end_at" in r.json()["detail"]


def test_runs_endpoint_returns_list(isolated_db):
    assert client.get("/api/recommendation-runs?limit=10").json() == []
    assert client.get("/api/recommendation-runs?limit=0").status_code == 422
