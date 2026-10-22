"""Seed a temp database for the 007 credential browser smoke test."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

if not os.environ.get("DATABASE_PATH"):
    raise SystemExit("refusing to seed: DATABASE_PATH not set (would touch the real database)")

DB = Path(os.environ["SEED_DB_PATH"])
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB) + suffix)
    if p.exists():
        p.unlink()

from app.main import init_db  # noqa: E402
import app.main as main_mod  # noqa: E402

main_mod.DB_PATH = DB
init_db()

import sqlite3  # noqa: E402

now = "2026-10-22T03:00:00+00:00"
conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys=ON")
conn.execute(
    "INSERT INTO data_sources(id,kind,name,status,scope,config_json,cursor,last_synced_at,"
    "last_error,sync_enabled,sync_interval_minutes,next_sync_at,consecutive_failures,"
    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (1, "mock", "无凭据源", "active", "", json.dumps({"items": [{"title": "t", "content": "c"}]}, ensure_ascii=False),
     None, None, None, 0, 30, None, 0, now, now),
)
conn.commit()
conn.close()
print("seeded", DB)
