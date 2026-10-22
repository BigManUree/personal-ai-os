"""Personal AI OS 1.3.4-B: recommendation feedback/outcome service tests.

These tests stay below the HTTP layer. Every test uses an isolated temp DB and
enables SQLite foreign keys so ON DELETE CASCADE is actually verified.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import recommendation_feedback as fb
from app import recommendations as rec_store
from app.main import init_db

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=2)
# Relative instead of a hardcoded date: `resume_after_at` must be in the future.
FUTURE_RESUME = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_feedback_store.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    connection = sqlite3.connect(db_path, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    yield connection
    connection.close()


def _add_recommendation(conn, rec_id: int, status: str = "pending") -> None:
    current = "2026-10-12T09:00:00+00:00"
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,"
        "window_json,coverage_status,expected_benefit,risk_level,risk_note,confidence,source,"
        "rule_code,generation_version,dedup_key,expires_at,created_at,updated_at,reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec_id, "Break down overdue work", "Do the next small task.", "task", "high",
            status, "[]", "{}", "ok", "Reduce backlog", "low", "", 0.8, "rule",
            "test.rule", "test", f"key-{rec_id}", "2026-12-31T00:00:00+00:00",
            current, current, current if status != "pending" else None,
        ),
    )
    conn.commit()


def _feedback(**overrides):
    data = {
        "vote": "helpful",
        "reason_codes": ["actionable", "evidence_clear"],
        "note": "The evidence made the next step clear.",
        "preference_scope": "unspecified",
        "resume_after_at": None,
    }
    data.update(overrides)
    return data


def _outcome(**overrides):
    data = {
        "execution_status": "planned",
        "result_rating": "unclear",
        "actual_effect_note": "",
        "metric_observations": [],
        "occurred_at": None,
    }
    data.update(overrides)
    return data


def _table_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_schema_is_created_idempotently_and_keeps_current_rows(conn):
    _add_recommendation(conn, 1)
    result = fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
    assert result["status"] == "created"
    conn.commit()

    init_db()
    init_db()
    assert _table_count(conn, "recommendation_feedback") == 1
    saved = fb.get_feedback(conn, 1)
    assert saved is not None
    assert saved["vote"] == "helpful"


def test_feedback_create_and_partial_update_preserves_unrelated_fields(conn):
    _add_recommendation(conn, 1)
    created = fb.upsert_feedback(conn, 1, _feedback(), now=NOW)["feedback"]
    assert created["source"] == "user"
    assert created["reason_codes"] == ["actionable", "evidence_clear"]

    updated = fb.upsert_feedback(
        conn,
        1,
        {"vote": "unhelpful", "reason_codes": ["not_now"]},
        now=LATER,
    )["feedback"]
    assert updated["vote"] == "unhelpful"
    assert updated["reason_codes"] == ["not_now"]
    assert updated["note"] == "The evidence made the next step clear."
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] > created["updated_at"]
    assert _table_count(conn, "recommendation_feedback") == 1


def test_feedback_explicit_empty_values_clear_fields(conn):
    _add_recommendation(conn, 1)
    fb.upsert_feedback(
        conn,
        1,
        _feedback(preference_scope="temporary", resume_after_at=FUTURE_RESUME),
        now=NOW,
    )
    updated = fb.upsert_feedback(
        conn,
        1,
        {"preference_scope": "unspecified", "resume_after_at": None, "note": ""},
        now=LATER,
    )["feedback"]
    assert updated["preference_scope"] == "unspecified"
    assert updated["resume_after_at"] is None
    assert updated["note"] == ""


def test_feedback_unknown_recommendation_is_404(conn):
    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_feedback(conn, 999, _feedback(), now=NOW)
    assert exc.value.status_code == 404
    assert fb.get_feedback(conn, 999) is None


def test_feedback_rejects_invalid_enums_reasons_length_secrets_and_times(conn):
    _add_recommendation(conn, 1)
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(_feedback(vote="maybe"))
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(_feedback(reason_codes=["not_now"]))
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(
            _feedback(reason_codes=["a", "b", "c", "d", "e", "f"])
        )
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(_feedback(note="x" * 1001))
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(
            _feedback(preference_scope="persistent", resume_after_at=FUTURE_RESUME)
        )
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(
            _feedback(preference_scope="temporary", resume_after_at="not-a-date")
        )
    with pytest.raises(ValidationError):
        fb.FeedbackDraft.model_validate(
            _feedback(preference_scope="temporary", resume_after_at="2026-10-01T09:00:00+00:00")
        )
    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_feedback(conn, 1, _feedback(note="api_key=sk-abcdef1234567890ABCD"), now=NOW)
    assert exc.value.code == "secret_detected"


def test_feedback_database_unique_constraint_backs_one_current_record(conn):
    _add_recommendation(conn, 1)
    fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recommendation_feedback(recommendation_id,vote,reason_codes_json,"
            "note,preference_scope,resume_after_at,source,created_at,updated_at) "
            "VALUES(1,'unhelpful','[]','','unspecified',NULL,'user',?,?)",
            ("2026-10-12T10:00:00+00:00", "2026-10-12T10:00:00+00:00"),
        )


def test_outcome_database_unique_constraint_backs_one_current_record(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_outcome(conn, 1, _outcome(), now=NOW)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recommendation_outcomes(recommendation_id,execution_status,result_rating,"
            "actual_effect_note,metric_observations_json,causality_status,occurred_at,source,"
            "created_at,updated_at) VALUES(1,'completed','better','','[]','not_inferred',?,"
            "'user',?,?)",
            ("2026-10-12T10:00:00+00:00", "2026-10-12T10:00:00+00:00",
             "2026-10-12T10:00:00+00:00"),
        )


def test_feedback_and_outcome_reject_orphan_rows(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recommendation_feedback(recommendation_id,vote,created_at,updated_at) "
            "VALUES(999,'helpful',?,?)",
            ("2026-10-12T10:00:00+00:00", "2026-10-12T10:00:00+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO recommendation_outcomes(recommendation_id,execution_status,created_at,updated_at) "
            "VALUES(999,'planned',?,?)",
            ("2026-10-12T10:00:00+00:00", "2026-10-12T10:00:00+00:00"),
        )


def test_outcome_requires_accepted_recommendation(conn):
    _add_recommendation(conn, 1, status="pending")
    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_outcome(conn, 1, _outcome(), now=NOW)
    assert exc.value.status_code == 409
    assert fb.get_outcome(conn, 1) is None


def test_outcome_create_planned_then_complete_sets_occurred_at_and_preserves_note(conn):
    _add_recommendation(conn, 1, status="accepted")
    planned = fb.upsert_outcome(
        conn, 1, _outcome(actual_effect_note="I will try it this week."), now=NOW
    )["outcome"]
    assert planned["execution_status"] == "planned"
    assert planned["occurred_at"] is None
    assert planned["causality_status"] == "not_inferred"

    completed = fb.upsert_outcome(
        conn,
        1,
        {"execution_status": "completed", "result_rating": "better"},
        now=LATER,
    )["outcome"]
    assert completed["execution_status"] == "completed"
    assert completed["occurred_at"] == "2026-10-12T11:00:00+00:00"
    assert completed["actual_effect_note"] == "I will try it this week."
    assert completed["causality_status"] == "not_inferred"
    assert _table_count(conn, "recommendation_outcomes") == 1


def test_outcome_validates_metric_observations_and_rejects_caller_causality(conn):
    _add_recommendation(conn, 1, status="accepted")
    valid_metric = {
        "metric": "tasks_completed",
        "expected_value": None,
        "observed_value": 2,
        "unit": "count",
        "window": {"days": 7, "timezone": "Asia/Shanghai"},
        "source": "user_reported",
        "captured_at": "2026-10-12T09:00:00+00:00",
        "attribution": "not_inferred",
    }
    saved = fb.upsert_outcome(
        conn, 1, _outcome(metric_observations=[valid_metric]), now=NOW
    )["outcome"]
    assert saved["metric_observations"][0]["metric"] == "tasks_completed"

    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(_outcome(causality_status="caused_by_recommendation"))
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(
            _outcome(metric_observations=[{**valid_metric, "metric": "unknown_metric"}])
        )
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(
            _outcome(metric_observations=[{**valid_metric, "observed_value": "two"}])
        )
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(
            _outcome(metric_observations=[{**valid_metric, "extra": True}])
        )
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(
            _outcome(metric_observations=[{**valid_metric, "window": {"unknown": 1}}])
        )
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(
            _outcome(metric_observations=[{**valid_metric, "observed_value": float("inf")}])
        )
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(
            _outcome(
                metric_observations=[
                    {**valid_metric, "captured_at": "2099-01-01T00:00:00+00:00"}
                ]
            )
        )


def test_outcome_rejects_invalid_status_rating_note_and_time(conn):
    _add_recommendation(conn, 1, status="accepted")
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(_outcome(execution_status="done_by_ai"))
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(_outcome(result_rating="perfect"))
    with pytest.raises(ValidationError):
        fb.OutcomeDraft.model_validate(_outcome(actual_effect_note="x" * 1001))
    with pytest.raises(ValidationError):
        fb.upsert_outcome(
            conn, 1, _outcome(execution_status="completed", occurred_at="bad-time"), now=NOW
        ) if False else fb.OutcomeDraft.model_validate(
            _outcome(execution_status="completed", occurred_at="bad-time")
        )
    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_outcome(
            conn,
            1,
            _outcome(actual_effect_note="token=ghp_abcdefghijklmnopqrstuvwxyz123456"),
            now=NOW,
        )
    assert exc.value.code == "secret_detected"


def test_outcome_uses_callers_clock_for_logical_expiration(conn):
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    _add_recommendation(conn, 1, status="accepted")
    # expires_at in the inserted fixture is 2026-12-31, so a 2099 generation
    # clock must treat it as expired even if the machine clock is earlier.
    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_outcome(conn, 1, _outcome(), now=future)
    assert exc.value.status_code == 409


def test_outcome_cannot_change_after_reopen_but_existing_result_is_retained(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_outcome(conn, 1, _outcome(execution_status="completed", result_rating="better"), now=NOW)
    rec_store.transition_status(conn, 1, "reopen", now=NOW)

    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_outcome(conn, 1, {"result_rating": "as_expected"}, now=LATER)
    assert exc.value.status_code == 409
    with pytest.raises(fb.FeedbackError) as delete_exc:
        fb.delete_outcome(conn, 1)
    assert delete_exc.value.status_code == 409
    assert fb.get_outcome(conn, 1)["result_rating"] == "better"


def test_delete_feedback_and_outcome_are_explicit(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
    fb.upsert_outcome(conn, 1, _outcome(), now=NOW)

    fb.delete_feedback(conn, 1)
    fb.delete_outcome(conn, 1)
    assert fb.get_feedback(conn, 1) is None
    assert fb.get_outcome(conn, 1) is None
    with pytest.raises(fb.FeedbackError) as exc:
        fb.delete_feedback(conn, 1)
    assert exc.value.status_code == 404


def test_deleting_recommendation_cascades_feedback_and_outcome(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
    fb.upsert_outcome(conn, 1, _outcome(), now=NOW)
    conn.commit()

    rec_store.delete_recommendation(conn, 1)
    conn.commit()
    assert _table_count(conn, "recommendation_feedback") == 0
    assert _table_count(conn, "recommendation_outcomes") == 0


def test_foreign_keys_must_be_enabled_for_writes(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_feedback_pragma_off.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    _add_recommendation(connection, 1)
    try:
        with pytest.raises(fb.FeedbackError, match="foreign_keys"):
            fb.upsert_feedback(connection, 1, _feedback(), now=NOW)
    finally:
        connection.close()


def test_failed_transaction_rolls_back_feedback_and_events(conn):
    _add_recommendation(conn, 1)
    before_events = _table_count(conn, "behavior_events")
    try:
        fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
        conn.execute("INSERT INTO recommendation_feedback(id) VALUES (?)", ("bad-id",))
    except sqlite3.Error:
        conn.rollback()
    assert fb.get_feedback(conn, 1) is None
    assert _table_count(conn, "behavior_events") == before_events


def test_recommendation_status_transitions_record_minimal_behavior_events(conn):
    _add_recommendation(conn, 1)
    _add_recommendation(conn, 2)
    _add_recommendation(conn, 3)

    rec_store.transition_status(conn, 1, "accept", now=NOW)
    rec_store.transition_status(conn, 1, "reopen", now=NOW)
    rec_store.transition_status(conn, 2, "ignore", now=NOW)
    rec_store.transition_status(conn, 3, "reject", now=NOW)

    rows = conn.execute(
        "SELECT event_type, subject_type, subject_id, metadata_json "
        "FROM behavior_events WHERE event_type LIKE 'recommendation.%' ORDER BY id"
    ).fetchall()
    assert [row["event_type"] for row in rows] == [
        "recommendation.accepted",
        "recommendation.reopened",
        "recommendation.ignored",
        "recommendation.rejected",
    ]
    assert {row["subject_type"] for row in rows} == {"recommendation"}
    assert {row["subject_id"] for row in rows} == {1, 2, 3}
    for row in rows:
        metadata = row["metadata_json"]
        assert "note" not in metadata
        assert "content" not in metadata
        assert "title" not in metadata


def test_concurrent_feedback_creation_never_leaves_duplicates(tmp_path, monkeypatch, rollback_journal):
    import sqlite3
    import threading
    import time

    db_path = tmp_path / "phase_4_feedback_concurrent.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    # Fresh WAL database + simultaneous first write can hit "readonly database";
    # this test is about the upsert unique constraint, not WAL bootstrap.
    rollback_journal(db_path)

    main_conn = sqlite3.connect(db_path, timeout=5)
    main_conn.row_factory = sqlite3.Row
    main_conn.execute("PRAGMA foreign_keys=ON")
    _add_recommendation(main_conn, 1)
    main_conn.close()

    results = []
    barrier = threading.Barrier(2)

    def worker(sleep_before_commit: float):
        connection = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            barrier.wait()
            outcome = fb.upsert_feedback(connection, 1, _feedback(), now=NOW)
            if sleep_before_commit:
                time.sleep(sleep_before_commit)
            connection.commit()
            results.append(("ok", outcome["status"]))
        except Exception as exc:
            connection.rollback()
            results.append(("error", type(exc).__name__, getattr(exc, "code", "")))
        finally:
            connection.close()

    first = threading.Thread(target=worker, args=(0.25,))
    second = threading.Thread(target=worker, args=(0.0,))
    first.start()
    second.start()
    first.join()
    second.join()

    checker = sqlite3.connect(db_path)
    count = checker.execute(
        "SELECT COUNT(*) FROM recommendation_feedback WHERE recommendation_id=1"
    ).fetchone()[0]
    checker.close()
    assert count == 1
    assert any(item[0] == "ok" and item[1] == "created" for item in results)
    # The second concurrent request must not create a duplicate. It may either
    # be rejected with a 409 conflict, or gracefully resolve to an "updated"
    # status if its SELECT saw the winner's committed row (timing-dependent
    # under WAL). Both outcomes keep count == 1.
    has_conflict = any(
        item[0] == "error" and item[1] in {"FeedbackError", "RecommendationError"}
        and item[2] == "conflict"
        for item in results
    )
    has_graceful_update = any(
        item[0] == "ok" and item[1] == "updated" for item in results
    )
    assert has_conflict or has_graceful_update, results


def test_init_db_migrates_a_minimal_legacy_recommendation_database(tmp_path, monkeypatch):
    db_path = tmp_path / "phase_4_feedback_legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("PRAGMA foreign_keys=ON")
    legacy.execute("""
        CREATE TABLE recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'pending',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            window_json TEXT NOT NULL DEFAULT '{}',
            coverage_status TEXT NOT NULL DEFAULT 'no_data',
            expected_benefit TEXT NOT NULL DEFAULT '',
            risk_level TEXT NOT NULL DEFAULT 'low',
            risk_note TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'rule',
            rule_code TEXT NOT NULL,
            generation_version TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT
        )
    """)
    legacy.execute(
        "INSERT INTO recommendations(title,content,category,status,confidence,rule_code,"
        "generation_version,dedup_key,expires_at,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("Legacy", "Existing row", "task", "pending", 0.5, "legacy", "legacy",
         "legacy-key", "2026-12-31T00:00:00+00:00", "2026-10-01T00:00:00+00:00",
         "2026-10-01T00:00:00+00:00"),
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()

    migrated = sqlite3.connect(db_path)
    migrated.row_factory = sqlite3.Row
    migrated.execute("PRAGMA foreign_keys=ON")
    assert migrated.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 1
    assert migrated.execute(
        "SELECT COUNT(*) FROM pragma_table_info('recommendation_feedback')"
    ).fetchone()[0] > 0
    assert migrated.execute(
        "SELECT COUNT(*) FROM pragma_table_info('recommendation_outcomes')"
    ).fetchone()[0] > 0
    migrated.close()


def test_feedback_and_outcome_do_not_write_memories_or_profile_items(conn):
    _add_recommendation(conn, 1, status="accepted")
    conn.execute(
        "INSERT INTO profile_items(category,title,content,meta,status,is_user_stated,confidence,"
        "source_type,source_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("basic", "Existing", "existing fact", "{}", "confirmed", 1, 1.0, "manual", None,
         "2026-10-12T08:00:00+00:00", "2026-10-12T08:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO memories(content,category,source,confidence,confirmed,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("existing memory", "general", "manual", 1.0, 1, "2026-10-12T08:00:00+00:00",
         "2026-10-12T08:00:00+00:00"),
    )
    conn.commit()
    before_profiles = _table_count(conn, "profile_items")
    before_memories = _table_count(conn, "memories")

    fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
    fb.upsert_outcome(conn, 1, _outcome(), now=NOW)
    conn.commit()

    assert _table_count(conn, "profile_items") == before_profiles
    assert _table_count(conn, "memories") == before_memories


# ============================ 1.3.4-E write race hardening ============================

def test_feedback_delete_race_with_parent_delete_records_no_delete_event(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_feedback(conn, 1, _feedback(), now=NOW)
    conn.commit()
    conn.execute("""
        CREATE TRIGGER phase4_e_feedback_delete_parent_race
        BEFORE DELETE ON recommendation_feedback
        BEGIN
            DELETE FROM recommendations WHERE id=OLD.recommendation_id;
        END;
    """)

    with pytest.raises(fb.FeedbackError) as exc:
        fb.delete_feedback(conn, 1)
    assert exc.value.status_code == 404
    conn.rollback()

    row = conn.execute("SELECT status FROM recommendations WHERE id=1").fetchone()
    assert row["status"] == "accepted"
    assert fb.get_feedback(conn, 1) is not None
    assert _table_count(conn, "recommendation_feedback") == 1
    delete_events = conn.execute(
        "SELECT COUNT(*) FROM behavior_events WHERE event_type='recommendation.feedback_deleted'"
    ).fetchone()[0]
    assert delete_events == 0


def test_outcome_delete_race_with_status_change_writes_no_delete_event(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_outcome(conn, 1, _outcome(execution_status="planned"), now=NOW)
    conn.commit()
    conn.execute("""
        CREATE TRIGGER phase4_e_outcome_delete_race
        BEFORE DELETE ON recommendation_outcomes
        BEGIN
            UPDATE recommendations SET status='pending' WHERE id=OLD.recommendation_id;
        END;
    """)

    with pytest.raises(fb.FeedbackError) as exc:
        fb.delete_outcome(conn, 1)
    assert exc.value.status_code == 409
    conn.rollback()

    row = conn.execute("SELECT status FROM recommendations WHERE id=1").fetchone()
    assert row["status"] == "accepted"
    outcome = fb.get_outcome(conn, 1)
    assert outcome["execution_status"] == "planned"
    delete_events = conn.execute(
        "SELECT COUNT(*) FROM behavior_events WHERE event_type='recommendation.outcome_deleted'"
    ).fetchone()[0]
    assert delete_events == 0


def test_outcome_insert_race_with_status_change_writes_no_orphan_event(conn):
    _add_recommendation(conn, 1, status="accepted")
    conn.commit()
    conn.execute("""
        CREATE TRIGGER phase4_e_outcome_insert_race
        BEFORE INSERT ON recommendation_outcomes
        BEGIN
            UPDATE recommendations SET status='pending' WHERE id=NEW.recommendation_id;
        END;
    """)

    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_outcome(conn, 1, _outcome(), now=NOW)
    assert exc.value.status_code == 409
    conn.rollback()

    row = conn.execute("SELECT status FROM recommendations WHERE id=1").fetchone()
    assert row["status"] == "accepted"
    assert fb.get_outcome(conn, 1) is None
    assert _table_count(conn, "behavior_events") == 0


def test_outcome_update_race_with_status_change_keeps_existing_outcome(conn):
    _add_recommendation(conn, 1, status="accepted")
    fb.upsert_outcome(conn, 1, _outcome(execution_status="planned"), now=NOW)
    conn.commit()
    conn.execute("""
        CREATE TRIGGER phase4_e_outcome_update_race
        BEFORE UPDATE ON recommendation_outcomes
        BEGIN
            UPDATE recommendations SET status='pending' WHERE id=NEW.recommendation_id;
        END;
    """)

    with pytest.raises(fb.FeedbackError) as exc:
        fb.upsert_outcome(conn, 1, _outcome(execution_status="completed", result_rating="better"), now=LATER)
    assert exc.value.status_code == 409
    conn.rollback()

    row = conn.execute("SELECT status FROM recommendations WHERE id=1").fetchone()
    assert row["status"] == "accepted"
    outcome = fb.get_outcome(conn, 1)
    assert outcome["execution_status"] == "planned"
    events = conn.execute(
        "SELECT COUNT(*) FROM behavior_events WHERE event_type='recommendation.outcome_saved'"
    ).fetchone()[0]
    assert events == 1
