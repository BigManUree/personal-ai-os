"""Personal AI OS 1.3.3-B: recommendation storage/service layer tests.

No HTTP routes, no rule engine and no LLM in this batch. Every test runs on
an isolated temp DB created by the same init_db() used at application startup,
which also verifies the new tables create cleanly on a fresh database.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import recommendations as rec
from app.main import init_db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_recommendations_store.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _evidence(kind="metric", ref_type="analytics_metric", ref_id=None, label="当前超期开放任务", value=3):
    return {
        "kind": kind,
        "ref_type": ref_type,
        "ref_id": ref_id,
        "label": label,
        "value": value,
        "detail": "窗口 7 天，样本 3",
        "definition_version": "1.3.2-B",
    }


def _draft(**overrides):
    base = {
        "title": "处理 3 个已超期任务",
        "content": "建议今天先为超期任务补上新的截止日期，并完成其中一个最小步骤。",
        "category": "task",
        "priority": "high",
        "risk_level": "low",
        "risk_note": "仅为时间安排建议，不代表任务本身有问题。",
        "confidence": 0.7,
        "expected_benefit": "可能降低积压继续扩大的风险。",
        "evidence": [_evidence()],
        "rule_code": "task.overdue_open",
        "source": "rule",
        "coverage_status": "insufficient_data",
        "window": {"days": 7, "timezone": "Asia/Shanghai"},
    }
    base.update(overrides)
    return base


# ---------- schema / migration ----------

def test_init_db_creates_recommendation_tables(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "recommendations" in tables
    assert "recommendation_runs" in tables


def test_init_db_idempotent_keeps_recommendation_data(conn):
    result = rec.create_recommendation(conn, _draft())
    assert result["status"] == "created"
    rec_id = result["recommendation"]["id"]

    conn.commit()  # release the write lock before init_db opens its own connection
    init_db()  # second startup on same file
    again = rec.get_recommendation(conn, rec_id)
    assert again["title"] == "处理 3 个已超期任务"
    assert again["status"] == "pending"


# ---------- draft validation ----------

def test_draft_requires_evidence(conn):
    with pytest.raises(ValidationError):
        rec.RecommendationDraft.model_validate(_draft(evidence=[]))


def test_draft_rejects_bad_enums_and_confidence(conn):
    with pytest.raises(ValidationError):
        rec.RecommendationDraft.model_validate(_draft(category="personality"))
    with pytest.raises(ValidationError):
        rec.RecommendationDraft.model_validate(_draft(priority="urgent"))
    with pytest.raises(ValidationError):
        rec.RecommendationDraft.model_validate(_draft(confidence=1.5))


def test_evidence_rejects_bad_kind_and_ref_id(conn):
    with pytest.raises(ValidationError):
        rec.RecommendationDraft.model_validate(
            _draft(evidence=[_evidence(kind="document_body")])
        )
    with pytest.raises(ValidationError):
        rec.RecommendationDraft.model_validate(
            _draft(evidence=[_evidence(ref_id=0)])
        )


def test_secret_like_content_is_rejected(conn):
    with pytest.raises(rec.RecommendationError) as exc:
        rec.create_recommendation(conn, _draft(content="我的 api_key: sk-abcdef1234567890ABCD"))
    assert exc.value.code == "secret_detected"
    assert exc.value.status_code == 400


# ---------- create / get / list ----------

def test_create_persists_serialised_fields(conn):
    result = rec.create_recommendation(conn, _draft())
    assert result["status"] == "created"
    item = result["recommendation"]
    assert item["status"] == "pending"
    assert item["source"] == "rule"
    assert item["generation_version"] == rec.GENERATION_VERSION
    assert len(item["dedup_key"]) == 64
    assert item["evidence"][0]["label"] == "当前超期开放任务"
    assert item["window"]["days"] == 7
    assert item["reviewed_at"] is None
    assert item["expires_at"] > item["created_at"]


def test_get_unknown_raises_404(conn):
    with pytest.raises(rec.RecommendationError) as exc:
        rec.get_recommendation(conn, 999)
    assert exc.value.status_code == 404


def test_list_hides_expired_and_rejected_by_default_and_orders_priority(conn):
    rec.create_recommendation(conn, _draft(rule_code="rule.low", priority="low", title="低优先级"))
    rec.create_recommendation(conn, _draft(rule_code="rule.high", priority="high", title="高优先级"))
    rec.create_recommendation(conn, _draft(rule_code="rule.rej", priority="normal", title="被拒绝"))
    items = rec.list_recommendations(conn)
    titles = [i["title"] for i in items]
    assert titles[0] == "高优先级"
    assert "低优先级" in titles
    assert "被拒绝" in titles  # still pending until rejected
    rec.transition_status(conn, 3, "reject")
    hidden = rec.list_recommendations(conn)
    assert all(i["title"] != "被拒绝" for i in hidden)
    rejected_only = rec.list_recommendations(conn, status="rejected")
    assert [i["title"] for i in rejected_only] == ["被拒绝"]


def test_list_filters_category_and_validates_params(conn):
    rec.create_recommendation(conn, _draft())
    assert len(rec.list_recommendations(conn, category="task")) == 1
    assert rec.list_recommendations(conn, category="goal") == []
    with pytest.raises(rec.RecommendationError):
        rec.list_recommendations(conn, status="bogus")
    with pytest.raises(rec.RecommendationError):
        rec.list_recommendations(conn, limit=0)


# ---------- state machine ----------

def test_status_transitions_and_conflicts(conn):
    rec_id = rec.create_recommendation(conn, _draft())["recommendation"]["id"]

    accepted = rec.transition_status(conn, rec_id, "accept")
    assert accepted["status"] == "accepted"
    assert accepted["reviewed_at"] is not None

    # accept is not valid from accepted
    with pytest.raises(rec.RecommendationError) as exc:
        rec.transition_status(conn, rec_id, "accept")
    assert exc.value.status_code == 409

    reopened = rec.transition_status(conn, rec_id, "reopen")
    assert reopened["status"] == "pending"
    ignored = rec.transition_status(conn, rec_id, "ignore")
    assert ignored["status"] == "ignored"

    # terminal status cannot move again
    with pytest.raises(rec.RecommendationError) as exc:
        rec.transition_status(conn, rec_id, "reopen")
    assert exc.value.status_code == 409


def test_unknown_action_and_unknown_id(conn):
    rec_id = rec.create_recommendation(conn, _draft())["recommendation"]["id"]
    with pytest.raises(rec.RecommendationError):
        rec.transition_status(conn, rec_id, "execute")
    with pytest.raises(rec.RecommendationError) as exc:
        rec.transition_status(conn, 4242, "accept")
    assert exc.value.status_code == 404


def test_delete_removes_row(conn):
    rec_id = rec.create_recommendation(conn, _draft())["recommendation"]["id"]
    rec.delete_recommendation(conn, rec_id)
    with pytest.raises(rec.RecommendationError) as exc:
        rec.get_recommendation(conn, rec_id)
    assert exc.value.status_code == 404
    with pytest.raises(rec.RecommendationError) as exc:
        rec.delete_recommendation(conn, rec_id)
    assert exc.value.status_code == 404


# ---------- dedup, cooldown, expiry ----------

def test_duplicate_active_draft_is_not_inserted(conn):
    first = rec.create_recommendation(conn, _draft())
    second = rec.create_recommendation(conn, _draft(title="改写标题但证据相同"))
    assert second["status"] == "duplicate"
    assert second["recommendation"]["id"] == first["recommendation"]["id"]
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1


def test_dedup_ignores_evidence_order(conn):
    draft_a = _draft(evidence=[_evidence(label="指标甲", value=1), _evidence(label="指标乙", value=2)])
    draft_b = _draft(evidence=[_evidence(label="指标乙", value=2), _evidence(label="指标甲", value=1)])
    first = rec.create_recommendation(conn, draft_a)
    second = rec.create_recommendation(conn, draft_b)
    assert first["status"] == "created"
    assert second["status"] == "duplicate"


def test_different_window_produces_new_recommendation(conn):
    first = rec.create_recommendation(conn, _draft(window={"days": 7}))
    second = rec.create_recommendation(conn, _draft(window={"days": 30}))
    assert first["status"] == "created"
    assert second["status"] == "created"
    assert second["recommendation"]["id"] != first["recommendation"]["id"]


def test_rejected_twin_suppressed_within_cooldown(conn):
    now = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
    rec_id = rec.create_recommendation(conn, _draft(), now=now)["recommendation"]["id"]
    rec.transition_status(conn, rec_id, "reject", now=now + timedelta(minutes=1))

    result = rec.create_recommendation(conn, _draft(), now=now + timedelta(days=10))
    assert result["status"] == "suppressed_rejected"
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1

    # after 30 days the same recommendation may be generated again
    later = rec.create_recommendation(conn, _draft(), now=now + timedelta(days=31))
    assert later["status"] == "created"
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 2


def test_expire_stale_marks_only_active_past_expiry(conn):
    now = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
    stale = rec.create_recommendation(
        conn, _draft(rule_code="rule.stale", expires_days=1), now=now - timedelta(days=5)
    )["recommendation"]["id"]
    fresh = rec.create_recommendation(
        conn, _draft(rule_code="rule.fresh", expires_days=30), now=now
    )["recommendation"]["id"]
    rejected = rec.create_recommendation(
        conn, _draft(rule_code="rule.rej", expires_days=1), now=now - timedelta(days=5)
    )["recommendation"]["id"]
    rec.transition_status(conn, rejected, "reject", now=now - timedelta(days=4))

    marked = rec.expire_stale(conn, now=now)
    assert marked == 1
    assert rec.get_recommendation(conn, stale)["status"] == "expired"
    assert rec.get_recommendation(conn, fresh)["status"] == "pending"
    assert rec.get_recommendation(conn, rejected)["status"] == "rejected"


def test_list_hides_logically_expired_rows_without_mutating_database(conn):
    now = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
    rec_id = rec.create_recommendation(
        conn, _draft(expires_days=1), now=now - timedelta(days=3)
    )["recommendation"]["id"]

    # A read/list call must remain read-only: it hides the row but must not
    # change its persisted status as a side effect.
    assert rec.list_recommendations(conn, now=now) == []
    assert rec.count_recommendations(conn, now=now) == 0
    assert rec.list_recommendations(conn, status="expired", now=now) == []
    assert rec.count_recommendations(conn, status="expired", now=now) == 0
    assert rec.get_recommendation(conn, rec_id)["status"] == "pending"

    # The explicit write-side cleanup marks rows expired, after which the
    # expired filter exposes them without relying on a GET side effect.
    assert rec.expire_stale(conn, now=now) == 1
    assert rec.get_recommendation(conn, rec_id)["status"] == "expired"
    assert len(rec.list_recommendations(conn, status="expired", now=now)) == 1


# ---------- generation runs ----------

def test_run_lifecycle_and_counts(conn):
    run_id = rec.start_run(
        conn, use_model=True, window_days=7, timezone_name="Asia/Shanghai",
        window_start="2026-10-05T00:00:00+08:00", window_end="2026-10-12T00:00:00+08:00",
    )
    finished = rec.finish_run(
        conn, run_id, status="degraded", candidates=4, created=2, duplicates=1,
        suppressed_rejected=1, expired_marked=0, error="httpx.ReadTimeout",
    )
    assert finished["status"] == "degraded"
    assert finished["created"] == 2
    assert finished["use_model"] == 1
    assert finished["finished_at"]
    runs = rec.list_runs(conn)
    assert len(runs) == 1 and runs[0]["id"] == run_id


def test_finish_run_validates_status_and_counts(conn):
    run_id = rec.start_run(conn)
    with pytest.raises(rec.RecommendationError):
        rec.finish_run(conn, run_id, status="ok")
    with pytest.raises(rec.RecommendationError):
        rec.finish_run(conn, run_id, status="success", created=-1)
    with pytest.raises(rec.RecommendationError):
        rec.start_run(conn, triggered_by="schedule")


def test_run_error_is_length_capped_and_secret_scrubbed(conn):
    long_error = "x" * 600
    assert rec.sanitize_run_error(long_error) == "x" * rec.MAX_RUN_ERROR
    scrubbed = rec.sanitize_run_error("upstream failed api_key: abcdef1234567890ABCD")
    assert "abcdef" not in scrubbed


# ============================ 1.3.3-E cross-process consistency ============================

def _index_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def test_fresh_db_has_cross_process_consistency_indexes(conn):
    names_rec = _index_names(conn, "recommendations")
    names_runs = _index_names(conn, "recommendation_runs")
    assert "idx_recommendations_active_dedup" in names_rec
    assert "idx_recommendation_runs_one_running" in names_runs


def test_database_rejects_two_active_twins_even_across_connections(conn):
    result = rec.create_recommendation(conn, _draft())
    key = result["recommendation"]["dedup_key"]
    rec_id = result["recommendation"]["id"]
    # Historical rejected rows are intentionally allowed and excluded.
    conn.execute(
        "INSERT INTO recommendations(title,content,category,priority,status,evidence_json,"
        "window_json,coverage_status,expected_benefit,risk_level,risk_note,confidence,source,"
        "rule_code,generation_version,dedup_key,expires_at,created_at,updated_at) "
        "SELECT title,content,category,priority,'rejected',evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,"
        "generation_version,dedup_key,expires_at,created_at,updated_at FROM recommendations WHERE id=?",
        (rec_id,),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE recommendations SET status='pending' WHERE id<>? AND dedup_key=?",
            (rec_id, key),
        )
    conn.rollback()


def test_start_run_database_singleton_returns_409(conn):
    assert rec.start_run(conn) == 1
    with pytest.raises(rec.RecommendationError) as exc:
        rec.start_run(conn)
    assert exc.value.code == "generation_in_progress"
    assert exc.value.status_code == 409
    conn.commit()


def test_recover_stale_runs_allows_a_new_run(conn):
    now = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)
    run_id = rec.start_run(conn, now=now - timedelta(minutes=10))
    conn.commit()
    assert rec.recover_stale_runs(conn, now=now - timedelta(minutes=6)) == 0
    assert rec.recover_stale_runs(conn, now=now) == 1
    finished = conn.execute(
        "SELECT status,error FROM recommendation_runs WHERE id=?", (run_id,)
    ).fetchone()
    assert finished["status"] == "failed" and finished["error"]
    assert rec.start_run(conn, now=now) == 2


def test_init_db_preserves_pre_e_active_duplicates_without_forced_unique(conn):
    conn.execute("DROP INDEX idx_recommendations_active_dedup")
    conn.execute("DROP INDEX idx_recommendation_runs_one_running")
    result = rec.create_recommendation(conn, _draft())
    rec_id = result["recommendation"]["id"]
    key = result["recommendation"]["dedup_key"]
    cols = (
        "title,content,category,priority,status,evidence_json,window_json,coverage_status,"
        "expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at"
    )
    conn.execute(
        f"INSERT INTO recommendations({cols}) "
        f"SELECT title,content,category,priority,'pending',evidence_json,window_json,coverage_status,"
        "expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at "
        f"FROM recommendations WHERE id=?",
        (rec_id,),
    )
    # Ensure the twin really has the same active dedup key.
    conn.execute("UPDATE recommendations SET dedup_key=? WHERE id<>?", (key, rec_id))
    conn.commit()

    init_db()  # must not fail, merge, or delete pre-existing user rows
    assert conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 2
    assert "idx_recommendations_active_dedup" not in _index_names(conn, "recommendations")
    # Runs have no conflicting rows, so that safety index can still be added.
    assert "idx_recommendation_runs_one_running" in _index_names(conn, "recommendation_runs")


def test_init_db_recovers_stale_runs_before_singleton_index(conn):
    conn.execute("DROP INDEX idx_recommendation_runs_one_running")
    old = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO recommendation_runs(triggered_by,use_model,status,started_at) "
        "VALUES('manual',0,'running',?)",
        (old,),
    )
    conn.commit()

    init_db()
    assert conn.execute(
        "SELECT COUNT(*) FROM recommendation_runs WHERE status='failed'"
    ).fetchone()[0] == 1
    assert "idx_recommendation_runs_one_running" in _index_names(conn, "recommendation_runs")
