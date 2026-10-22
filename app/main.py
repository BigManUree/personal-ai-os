from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx  # noqa: F401  (tests access main.httpx for monkeypatching)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import (  # noqa: F401  (connectors self-register on import)
    analytics,
    behavior_events,
    connector_scheduler,
    connectors,
    document_chunk,
    document_parse,
    github_connector,
    httpapi_connector,
    memory_extract,
    memory_match,
    model_config,
    notification_delivery,
    notification_delivery_service,
    profile_propose,
    recommendation_engine,
    recommendation_stats,
    reminder_scheduler_service,
    reminders,
    retriever,
    secret_crypto,
)
from app.connector_scheduler import ConnectorScheduler

logger = logging.getLogger("personal_ai_os.api")

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"
DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "personal_ai_os.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
from app.schema_migrations import (  # noqa: E402
    _migrate_data_sources,
    _migrate_documents,
    _migrate_memories,
    _migrate_recommendations,
    _migrate_reminder_scheduler_runs,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize SQLite, then start local background schedulers.

    Reminder scheduling is explicit opt-in and remains off under the default
    environment. Its shutdown is awaited before connector shutdown so the app
    does not continue producing reminder state while requests are draining.
    """
    reminder_config = reminder_scheduler_service.load_config()
    notification_config = notification_delivery_service.load_config()
    init_db()

    global connector_scheduler_instance, reminder_scheduler_instance, notification_delivery_instance
    connector = connector_scheduler_instance
    if connector is None:
        connector = ConnectorScheduler(_due_source_ids, _run_source_sync)
        connector_scheduler_instance = connector
    connector.start()
    reminder: reminder_scheduler_service.ReminderSchedulerService | None = None
    notification: notification_delivery_service.NotificationDeliveryService | None = None
    try:
        reminder = reminder_scheduler_service.ReminderSchedulerService(
            DB_PATH,
            enabled=reminder_config.enabled,
            poll_seconds=reminder_config.poll_seconds,
            shutdown_timeout_seconds=reminder_config.shutdown_timeout_seconds,
        )
        reminder_scheduler_instance = reminder
        await reminder.start()
        notification = notification_delivery_service.NotificationDeliveryService(
            DB_PATH,
            enabled=notification_config.enabled,
            poll_seconds=notification_config.poll_seconds,
            shutdown_timeout_seconds=notification_config.shutdown_timeout_seconds,
            channel=notification_delivery.FakeNotificationChannel(),
        )
        notification_delivery_instance = notification
        await notification.start()
        yield
    finally:
        try:
            if reminder is not None:
                await reminder.stop()
        finally:
            try:
                if notification is not None:
                    await notification.stop()
            finally:
                reminder_scheduler_instance = None
                notification_delivery_instance = None
                await connector.stop()
                connector_scheduler_instance = None


app = FastAPI(title="Personal AI OS MVP", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Module-level background schedulers; owned by the FastAPI lifespan handler.
connector_scheduler_instance: ConnectorScheduler | None = None
reminder_scheduler_instance: reminder_scheduler_service.ReminderSchedulerService | None = None
notification_delivery_instance: notification_delivery_service.NotificationDeliveryService | None = None
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _hostname_from_hostport(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    # urlsplit understands IPv6 literals when supplied as //[::1]:8000.
    parsed = urlsplit(f"//{value}")
    return (parsed.hostname or "").strip("[]").lower()


def _is_allowed_local_host(request: Request) -> bool:
    """Restrict the no-login server to loopback Host names.

    This blocks DNS-rebinding requests that reach 127.0.0.1 while presenting
    an attacker-controlled Host. Starlette's TestClient uses fixed synthetic
    peer/host values; recognise exactly that test transport.
    """
    host = _hostname_from_hostport(request.headers.get("host", ""))
    if request.client and request.client.host == "testclient" and host == "testserver":
        return True
    return host in _LOCAL_HOSTS


def _is_same_origin(request: Request) -> bool:
    """Validate browser state-changing requests for the local, no-login app.

    Non-browser clients (curl, TestClient, health checks) usually send no
    Origin/Sec-Fetch-Site and remain usable. A browser cross-site request
    carries one of these headers and is rejected before a route runs.
    """
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        host = (request.headers.get("host") or "").strip().lower()
        origin_port = f":{parsed.port}" if parsed.port is not None else ""
        origin_host = f"{parsed.hostname.lower()}{origin_port}"
        return origin_host == host

    sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if sec_fetch_site:
        return sec_fetch_site == "same-origin"
    return True


@app.middleware("http")
async def reject_cross_origin_state_changes(request: Request, call_next: Any) -> Any:
    if not _is_allowed_local_host(request):
        return JSONResponse(
            {"detail": "非法 Host：Personal AI OS 仅允许通过本机 loopback 地址访问。"},
            status_code=400,
        )
    if request.method not in _SAFE_METHODS and not _is_same_origin(request):
        return JSONResponse(
            {"detail": "跨站请求已拒绝：请从本机 Personal AI OS 页面发起操作。"},
            status_code=403,
        )
    return await call_next(request)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db() -> Any:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def record_behavior_event(conn: sqlite3.Connection, event_type: str, **kwargs: Any) -> dict[str, Any] | None:
    """Record a minimal behavior event inside the caller's existing transaction."""
    return behavior_events.record_event(conn, event_type, **kwargs)


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        # Log the effective mode: startup silently flips a rollback-journal database
        # to WAL, which changes what a correct backup must include.
        logger.info("sqlite journal_mode=%s database=%s",
                    conn.execute("PRAGMA journal_mode").fetchone()[0], DB_PATH)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            display_name TEXT NOT NULL DEFAULT '我',
            goals TEXT NOT NULL DEFAULT '',
            preferences TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        INSERT OR IGNORE INTO profile(id, display_name, goals, preferences, updated_at)
        VALUES (1, '我', '', '', CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'manual',
            confidence REAL NOT NULL DEFAULT 1.0,
            confirmed INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            memory_type TEXT NOT NULL DEFAULT 'fact',
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_id INTEGER,
            status TEXT NOT NULL DEFAULT 'confirmed',
            valid_from TEXT,
            valid_until TEXT,
            supersedes_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT '新对话',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            file_hash TEXT,
            source_type TEXT NOT NULL DEFAULT 'manual_text',
            status TEXT NOT NULL DEFAULT 'parsed',
            original_filename TEXT,
            file_ext TEXT,
            byte_size INTEGER,
            error TEXT,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            page_hint INTEGER,
            heading TEXT,
            char_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_document_chunks_doc ON document_chunks(document_id, chunk_index);
        CREATE TABLE IF NOT EXISTS memory_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            confidence REAL NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected','merged')),
            duplicate_of INTEGER,
            conflict_with INTEGER,
            created_at TEXT NOT NULL,
            reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
            candidate_id INTEGER,
            event TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            tech_stack TEXT NOT NULL DEFAULT '',
            repo_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_project_events_project ON project_events(project_id, id);
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'long_term',
            status TEXT NOT NULL DEFAULT 'active',
            measure TEXT NOT NULL DEFAULT '',
            target_date TEXT,
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo',
            priority TEXT NOT NULL DEFAULT 'normal',
            due_date TEXT,
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE TABLE IF NOT EXISTS profile_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            is_user_stated INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_id INTEGER,
            source_detail TEXT NOT NULL DEFAULT '',
            valid_from TEXT,
            valid_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            reviewed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_profile_items_status ON profile_items(status);
        CREATE INDEX IF NOT EXISTS idx_profile_items_cat_status ON profile_items(category, status);
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            scope TEXT NOT NULL DEFAULT '',
            config_json TEXT NOT NULL DEFAULT '{}',
            cursor TEXT,
            last_synced_at TEXT,
            last_error TEXT,
            sync_enabled INTEGER NOT NULL DEFAULT 0,
            sync_interval_minutes INTEGER NOT NULL DEFAULT 60,
            next_sync_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_data_sources_status ON data_sources(status);
        CREATE TABLE IF NOT EXISTS sync_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            cursor_before TEXT,
            cursor_after TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sync_logs_source ON sync_logs(data_source_id, id);
        CREATE TABLE IF NOT EXISTS source_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_source_id INTEGER NOT NULL REFERENCES data_sources(id) ON DELETE CASCADE,
            external_id TEXT NOT NULL,
            document_id INTEGER,
            content_hash TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(data_source_id, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_source_items_source ON source_items(data_source_id, status);
        CREATE TABLE IF NOT EXISTS connector_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_source_id INTEGER NOT NULL UNIQUE REFERENCES data_sources(id) ON DELETE CASCADE,
            ciphertext TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profile_item_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER REFERENCES profile_items(id) ON DELETE CASCADE,
            event TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS behavior_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            subject_type TEXT NOT NULL,
            subject_id INTEGER,
            project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
            goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
            task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            data_source_id INTEGER REFERENCES data_sources(id) ON DELETE SET NULL,
            source_type TEXT NOT NULL DEFAULT 'system',
            source_id INTEGER,
            dedup_key TEXT UNIQUE,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_events_time_type ON behavior_events(occurred_at, event_type);
        CREATE INDEX IF NOT EXISTS idx_behavior_events_type_time ON behavior_events(event_type, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_behavior_events_subject ON behavior_events(subject_type, subject_id);
        CREATE INDEX IF NOT EXISTS idx_behavior_events_project ON behavior_events(project_id);
        CREATE INDEX IF NOT EXISTS idx_behavior_events_goal ON behavior_events(goal_id);
        CREATE INDEX IF NOT EXISTS idx_behavior_events_task ON behavior_events(task_id);
        CREATE INDEX IF NOT EXISTS idx_behavior_events_source ON behavior_events(data_source_id);
        CREATE TABLE IF NOT EXISTS recommendations (
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
            reviewed_at TEXT,
            CHECK (category IN ('task','project','goal','learning','habit')),
            CHECK (priority IN ('low','normal','high')),
            CHECK (status IN ('pending','accepted','ignored','rejected','expired')),
            CHECK (coverage_status IN ('ok','partial_data','insufficient_data','no_data')),
            CHECK (risk_level IN ('low','medium','high')),
            CHECK (source IN ('rule','hybrid')),
            CHECK (confidence >= 0 AND confidence <= 1)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendations_status_priority
            ON recommendations(status, priority, id);
        CREATE INDEX IF NOT EXISTS idx_recommendations_category ON recommendations(category, status);
        CREATE INDEX IF NOT EXISTS idx_recommendations_expires ON recommendations(expires_at);
        CREATE INDEX IF NOT EXISTS idx_recommendations_dedup ON recommendations(dedup_key);
        -- The active-dedup partial UNIQUE index is added by
        -- _migrate_recommendations() after checking for pre-E duplicates.
        CREATE TABLE IF NOT EXISTS recommendation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_by TEXT NOT NULL DEFAULT 'manual',
            use_model INTEGER NOT NULL DEFAULT 0,
            window_days INTEGER,
            timezone TEXT,
            window_start TEXT,
            window_end TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            candidates INTEGER NOT NULL DEFAULT 0,
            created INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            suppressed_rejected INTEGER NOT NULL DEFAULT 0,
            expired_marked INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            CHECK (triggered_by IN ('manual')),
            CHECK (status IN ('running','success','no_evidence','degraded','failed'))
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_runs_id ON recommendation_runs(id);
        -- The one-running partial UNIQUE index is added by
        -- _migrate_recommendations() after recovering interrupted runs.
        CREATE TABLE IF NOT EXISTS recommendation_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id INTEGER NOT NULL UNIQUE,
            vote TEXT NOT NULL,
            reason_codes_json TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT '',
            preference_scope TEXT NOT NULL DEFAULT 'unspecified',
            resume_after_at TEXT,
            source TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (vote IN ('helpful','unhelpful')),
            CHECK (preference_scope IN ('temporary','persistent','unspecified')),
            CHECK (source IN ('user')),
            CHECK (length(note) <= 1000),
            CHECK (length(reason_codes_json) <= 3000),
            FOREIGN KEY (recommendation_id) REFERENCES recommendations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_vote
            ON recommendation_feedback(vote, updated_at);
        CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_scope
            ON recommendation_feedback(preference_scope, resume_after_at);
        CREATE TABLE IF NOT EXISTS recommendation_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id INTEGER NOT NULL UNIQUE,
            execution_status TEXT NOT NULL,
            result_rating TEXT NOT NULL DEFAULT 'unclear',
            actual_effect_note TEXT NOT NULL DEFAULT '',
            metric_observations_json TEXT NOT NULL DEFAULT '[]',
            causality_status TEXT NOT NULL DEFAULT 'not_inferred',
            occurred_at TEXT,
            source TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (execution_status IN (
                'planned','completed','partially_completed','not_completed','no_longer_relevant'
            )),
            CHECK (result_rating IN (
                'much_better','better','as_expected','worse','much_worse','unclear'
            )),
            CHECK (causality_status IN ('not_inferred')),
            CHECK (source IN ('user')),
            CHECK (length(actual_effect_note) <= 1000),
            CHECK (length(metric_observations_json) <= 3000),
            FOREIGN KEY (recommendation_id) REFERENCES recommendations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_execution
            ON recommendation_outcomes(execution_status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_rating
            ON recommendation_outcomes(result_rating, updated_at);

        CREATE TABLE IF NOT EXISTS reminder_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            task_due_enabled INTEGER NOT NULL DEFAULT 0,
            task_stalled_enabled INTEGER NOT NULL DEFAULT 0,
            recommendation_followup_enabled INTEGER NOT NULL DEFAULT 0,
            daily_review_enabled INTEGER NOT NULL DEFAULT 0,
            weekly_review_enabled INTEGER NOT NULL DEFAULT 0,
            daily_review_at TEXT NOT NULL DEFAULT '20:00',
            weekly_review_weekday INTEGER NOT NULL DEFAULT 1,
            weekly_review_at TEXT NOT NULL DEFAULT '09:00',
            quiet_hours_enabled INTEGER NOT NULL DEFAULT 0,
            quiet_start TEXT NOT NULL DEFAULT '22:00',
            quiet_end TEXT NOT NULL DEFAULT '08:00',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (timezone IN ('UTC','Asia/Shanghai')
                OR timezone GLOB 'UTC[+-][0-9][0-9]:[0-9][0-9]'
                OR timezone GLOB 'UTC[+-][0-9][0-9][0-9][0-9]'),
            CHECK (task_due_enabled IN (0,1)),
            CHECK (task_stalled_enabled IN (0,1)),
            CHECK (recommendation_followup_enabled IN (0,1)),
            CHECK (daily_review_enabled IN (0,1)),
            CHECK (weekly_review_enabled IN (0,1)),
            CHECK (weekly_review_weekday BETWEEN 1 AND 7),
            CHECK (daily_review_at GLOB '[0-2][0-9]:[0-5][0-9]' AND daily_review_at <= '23:59'),
            CHECK (weekly_review_at GLOB '[0-2][0-9]:[0-5][0-9]' AND weekly_review_at <= '23:59'),
            CHECK (quiet_hours_enabled IN (0,1)),
            CHECK (quiet_start GLOB '[0-2][0-9]:[0-5][0-9]' AND quiet_start <= '23:59'),
            CHECK (quiet_end GLOB '[0-2][0-9]:[0-5][0-9]' AND quiet_end <= '23:59')
        );
        INSERT OR IGNORE INTO reminder_settings(
            id, timezone, task_due_enabled, task_stalled_enabled,
            recommendation_followup_enabled, daily_review_enabled, weekly_review_enabled,
            daily_review_at, weekly_review_weekday, weekly_review_at,
            quiet_hours_enabled, quiet_start, quiet_end, created_at, updated_at
        ) VALUES (
            1, 'Asia/Shanghai', 0, 0, 0, 0, 0, '20:00', 1, '09:00',
            0, '22:00', '08:00',
            strftime('%Y-%m-%dT%H:%M:%SZ','now'), strftime('%Y-%m-%dT%H:%M:%SZ','now')
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_code TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id INTEGER,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            recommendation_id INTEGER REFERENCES recommendations(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'scheduled',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            coverage_status TEXT NOT NULL DEFAULT 'no_data',
            dedup_key TEXT NOT NULL UNIQUE,
            scheduled_at TEXT NOT NULL,
            due_at TEXT,
            snoozed_until TEXT,
            expires_at TEXT NOT NULL,
            acknowledged_at TEXT,
            completed_at TEXT,
            cancelled_at TEXT,
            resolved_at TEXT,
            cancel_reason TEXT,
            occurrence_index INTEGER NOT NULL DEFAULT 1,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (rule_code IN (
                'task.due_soon_v1','task.overdue_v1','task.stalled_doing_v1',
                'recommendation.followup_v1','review.daily_due_v1','review.weekly_due_v1')),
            CHECK (source_type IN ('task','recommendation','review')),
            CHECK (priority IN ('low','normal','high')),
            CHECK (status IN ('scheduled','due','snoozed','acknowledged','dismissed','completed','cancelled','expired')),
            CHECK (coverage_status IN ('ok','partial_data','insufficient_data','no_data')),
            CHECK (cancel_reason IS NULL OR cancel_reason IN (
                'source_completed','source_cancelled','source_deleted','source_rescheduled',
                'recommendation_closed','outcome_registered','rule_disabled','settings_disabled',
                'superseded_by_new_schedule','expiry_reached','user_cancelled')),
            CHECK (length(title) BETWEEN 1 AND 200),
            CHECK (length(content) BETWEEN 1 AND 1000),
            CHECK (length(rule_version) <= 50),
            CHECK (length(evidence_json) <= 3000),
            CHECK (length(last_error) <= 300),
            CHECK (occurrence_index >= 1),
            CHECK (attempt_count >= 0),
            CHECK (max_attempts BETWEEN 1 AND 10),
            CHECK (
                (source_type='task' AND task_id IS NOT NULL AND recommendation_id IS NULL AND source_id=task_id)
                OR (source_type='recommendation' AND recommendation_id IS NOT NULL AND task_id IS NULL AND source_id=recommendation_id)
                OR (source_type='review' AND task_id IS NULL AND recommendation_id IS NULL))
        );
        CREATE INDEX IF NOT EXISTS idx_reminders_status_scheduled ON reminders(status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_reminders_source ON reminders(source_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_reminders_task ON reminders(task_id);
        CREATE INDEX IF NOT EXISTS idx_reminders_recommendation ON reminders(recommendation_id);
        CREATE INDEX IF NOT EXISTS idx_reminders_active
            ON reminders(status, expires_at) WHERE status IN ('scheduled','due','snoozed');

        CREATE TABLE IF NOT EXISTS reminder_scheduler_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_key TEXT NOT NULL UNIQUE,
            trigger_type TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            owner_token TEXT NOT NULL DEFAULT '',
            lease_fencing_token INTEGER,
            processed_count INTEGER NOT NULL DEFAULT 0,
            created_count INTEGER NOT NULL DEFAULT 0,
            updated_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            due_count INTEGER NOT NULL DEFAULT 0,
            expired_count INTEGER NOT NULL DEFAULT 0,
            cancelled_count INTEGER NOT NULL DEFAULT 0,
            recovered_run_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            CHECK (trigger_type IN ('manual','scheduled','recovery')),
            CHECK (status IN ('running','succeeded','partial_failed','failed','abandoned')),
            CHECK (length(run_key) BETWEEN 1 AND 200),
            CHECK (length(owner_token) <= 120),
            CHECK (length(last_error) <= 500),
            CHECK (processed_count >= 0),
            CHECK (created_count >= 0),
            CHECK (updated_count >= 0),
            CHECK (skipped_count >= 0),
            CHECK (due_count >= 0),
            CHECK (expired_count >= 0),
            CHECK (cancelled_count >= 0),
            CHECK (recovered_run_count >= 0),
            CHECK (error_count >= 0)
        );
        CREATE INDEX IF NOT EXISTS idx_reminder_scheduler_runs_status_started
            ON reminder_scheduler_runs(status, started_at);
        CREATE INDEX IF NOT EXISTS idx_reminder_scheduler_runs_started
            ON reminder_scheduler_runs(started_at);
        """)
        _migrate_memories(conn)
        _migrate_documents(conn)
        _migrate_data_sources(conn)
        _migrate_recommendations(conn)
        _migrate_reminder_scheduler_runs(conn)
        conn.executescript(notification_delivery.SCHEMA_SQL)


class ApprovalIn(BaseModel):
    action_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any]




@app.get("/")
def index() -> FileResponse:
    # 开发期：HTML 入口禁止缓存，避免修复后的脚本被浏览器旧缓存挡住（曾导致页面一直“正在检查服务”）。
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/api/health")
def health() -> dict[str, Any]:
    reminder = reminder_scheduler_instance
    scheduler_snapshot = (
        reminder.snapshot() if reminder is not None
        else reminder_scheduler_service.default_snapshot(enabled=False)
    )
    notification_snapshot = (
        notification_delivery_instance.snapshot()
        if notification_delivery_instance is not None
        else notification_delivery_service.default_snapshot(enabled=False)
    )
    return {
        "status": "ok",
        "mode": "cloud" if model_config.load_model_config().enabled else "local-demo",
        "reminder_scheduler": scheduler_snapshot,
        "notification_delivery": notification_snapshot,
    }



# --- domain routers (imported after db/record_behavior_event definitions) ---
from app.api import analytics as analytics_routes  # noqa: E402
from app.api import chat as chat_routes  # noqa: E402
from app.api import connectors as connectors_routes  # noqa: E402
from app.api import documents as documents_routes  # noqa: E402
from app.api import memory as memory_routes  # noqa: E402
from app.api import profile_items as profile_items_routes  # noqa: E402
from app.api import recommendations as recommendations_routes  # noqa: E402
from app.api import reminders as reminders_routes  # noqa: E402
from app.api import work as work_routes  # noqa: E402

app.include_router(memory_routes.router)
app.include_router(reminders_routes.router)
app.include_router(documents_routes.router)
app.include_router(work_routes.router)
app.include_router(analytics_routes.router)
app.include_router(profile_items_routes.router)
app.include_router(connectors_routes.router)
app.include_router(recommendations_routes.router)
app.include_router(chat_routes.router)



@app.post("/api/approvals")
def create_approval(body: ApprovalIn) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute("INSERT INTO approvals(action_type,payload,status,created_at) VALUES(?,?,?,?)",
                           (body.action_type, json.dumps(body.payload, ensure_ascii=False), "pending", now_iso()))
        row = dict(conn.execute("SELECT * FROM approvals WHERE id=?", (cur.lastrowid,)).fetchone())
        row["payload"] = json.loads(row["payload"])
        return row


@app.get("/api/approvals")
def list_approvals() -> list[dict[str, Any]]:
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()]
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


@app.post("/api/approvals/{approval_id}/review")
def review_approval(approval_id: int, body: dict[str, str]) -> dict[str, Any]:
    decision = body.get("decision")
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="decision 必须是 approved 或 rejected")
    with db() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="审批记录不存在")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="该审批已处理，不能重复审批")
        conn.execute("UPDATE approvals SET status=?,reviewed_at=? WHERE id=?", (decision, now_iso(), approval_id))
        updated = dict(conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())
        updated["payload"] = json.loads(updated["payload"])
        return updated


# --- backwards-compatible re-exports (tests import these from app.main) ---
from app.api.chat import _run_memory_extraction as _run_memory_extraction  # noqa: E402
from app.api.chat import _store_candidates as _store_candidates  # noqa: E402
from app.api.chat import call_llm as call_llm  # noqa: E402
from app.api.chat import retrieve_context as retrieve_context  # noqa: E402
from app.api.connectors import _due_source_ids as _due_source_ids  # noqa: E402
from app.api.connectors import _run_source_sync as _run_source_sync  # noqa: E402
from app.api.documents import _store_parsed_document as _store_parsed_document  # noqa: E402
