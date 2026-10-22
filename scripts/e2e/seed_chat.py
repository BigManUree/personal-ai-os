"""Seed a temp database for the 008 chat-streaming browser smoke test."""
from __future__ import annotations

import os
import sqlite3
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

conn = sqlite3.connect(DB)
cur = conn.execute(
    "INSERT INTO documents(filename,content,source_type,status,created_at,updated_at,chunk_count) "
    "VALUES('项目笔记.md','本项目使用 FastAPI 与 Vue3 构建。','manual_text','parsed','t','t',1)"
)
doc_id = cur.lastrowid
conn.execute(
    "INSERT INTO document_chunks(document_id,chunk_index,content,heading,char_count,created_at) "
    "VALUES(?,?,?,?,?,?)",
    (doc_id, 0, "本项目使用 FastAPI 与 Vue3 构建，聊天支持流式输出与 FTS5 检索。" * 3, "技术栈", 90, "t"),
)
conn.commit()
conn.close()
print(f"seeded {DB}")
