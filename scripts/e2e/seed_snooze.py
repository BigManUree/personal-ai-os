"""Seed a temp database for the 006 snooze/comparison browser smoke test."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DB = Path(os.environ["SEED_DB_PATH"])
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB) + suffix)
    if p.exists():
        p.unlink()

from app.main import init_db  # noqa: E402
import app.main as main_mod  # noqa: E402

main_mod.DB_PATH = DB
init_db()

now = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys=ON")

# ---- reminders: one per relevant status -------------------------------
reminder_rows = [
    (1, "review.daily_due_v1", "v1", "review", "每日回顾提醒", "今天要做的三件事", "due", None),
    (2, "review.daily_due_v1", "v1", "review", "已延期提醒", "昨天延期的回顾", "snoozed",
     iso(now + timedelta(hours=2))),
    (3, "review.daily_due_v1", "v1", "review", "已完成提醒", "已经处理完", "completed", None),
    (4, "review.daily_due_v1", "v1", "review", "待触发提醒", "明天再看", "scheduled", None),
]
for rid, rule, version, source, title, content, status, snoozed in reminder_rows:
    conn.execute(
        "INSERT INTO reminders(id,rule_code,rule_version,source_type,title,content,priority,"
        "status,evidence_json,coverage_status,dedup_key,scheduled_at,due_at,snoozed_until,"
        "expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, rule, version, source, title, content, "normal", status,
         json.dumps([], ensure_ascii=False), "ok", f"seed-{rid}", iso(now - timedelta(days=1)),
         iso(now) if status == "due" else None, snoozed, iso(now + timedelta(days=30)),
         iso(now - timedelta(days=2)), iso(now - timedelta(days=2))),
    )

# ---- behavior events: current window has data, previous stays empty ----
event_types_current = ["task.created", "task.completed", "task.created",
                       "document.created", "goal.created", "project.activity"]
for index, event_type in enumerate(event_types_current):
    conn.execute(
        "INSERT INTO behavior_events(event_type,occurred_at,subject_type,subject_id,"
        "source_type,dedup_key,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (event_type, iso(now - timedelta(hours=index + 1)), "seed", index + 1,
         "system", f"seed-current-{index}", "{}", iso(now)),
    )

conn.commit()
conn.close()
print("seeded", DB)
