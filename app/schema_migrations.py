"""Idempotent SQLite schema migrations, run by app.main.init_db on every startup.

Pure helpers: every function takes an open connection and never opens its own;
none of them opens a database file, so they are safe to call inside the
caller's transaction and safe to re-run (every statement is conditional).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from app import document_chunk
from app import recommendations as rec_store
from app.document_parse import Page

logger = logging.getLogger("personal_ai_os.api")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_reminder_scheduler_runs(conn: sqlite3.Connection) -> None:
    columns = _column_names(conn, "reminder_scheduler_runs")
    if "lease_fencing_token" not in columns:
        conn.execute("ALTER TABLE reminder_scheduler_runs ADD COLUMN lease_fencing_token INTEGER")



def _migrate_memories(conn: sqlite3.Connection) -> None:
    """Add phase-2 columns to a legacy memories table without losing data.

    Idempotent: nullable ALTER TABLE ADD COLUMN followed by NULL-only
    backfills, safe to run on every startup.
    """
    columns = _column_names(conn, "memories")
    add_columns = {
        "memory_type": "TEXT",
        "source_type": "TEXT",
        "source_id": "INTEGER",
        "status": "TEXT",
        "valid_from": "TEXT",
        "valid_until": "TEXT",
        "supersedes_id": "INTEGER",
    }
    for name, decl in add_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {decl}")

    conn.execute(
        "UPDATE memories SET status = CASE WHEN confirmed = 1 THEN 'confirmed' ELSE 'pending' END "
        "WHERE status IS NULL"
    )
    conn.execute("UPDATE memories SET source_type = source WHERE source_type IS NULL")
    conn.execute("UPDATE memories SET memory_type = 'fact' WHERE memory_type IS NULL")


def _migrate_data_sources(conn: sqlite3.Connection) -> None:
    """Add 3.5.3 scheduling columns to a legacy data_sources table.

    Idempotent; every existing source defaults to scheduling DISABLED, so
    behaviour is unchanged until the user opts in per source.
    """
    columns = _column_names(conn, "data_sources")
    add_columns = {
        "sync_enabled": "INTEGER NOT NULL DEFAULT 0",
        "sync_interval_minutes": "INTEGER NOT NULL DEFAULT 60",
        "next_sync_at": "TEXT",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, decl in add_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE data_sources ADD COLUMN {name} {decl}")
    conn.execute(
        "UPDATE data_sources SET sync_enabled=COALESCE(sync_enabled,0), "
        "sync_interval_minutes=COALESCE(sync_interval_minutes,60), "
        "consecutive_failures=COALESCE(consecutive_failures,0)"
    )


def _migrate_documents(conn: sqlite3.Connection) -> None:
    """Add phase-3 document metadata columns + chunks table; keep old data."""
    columns = _column_names(conn, "documents")
    add_columns = {
        "file_hash": "TEXT",
        "source_type": "TEXT",
        "status": "TEXT",
        "original_filename": "TEXT",
        "file_ext": "TEXT",
        "byte_size": "INTEGER",
        "error": "TEXT",
        "chunk_count": "INTEGER",
        "updated_at": "TEXT",
    }
    for name, decl in add_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {decl}")
    # Legacy rows came from the paste-text endpoint and remain retrievable.
    conn.execute("UPDATE documents SET source_type='manual_text' WHERE source_type IS NULL")
    conn.execute("UPDATE documents SET status='parsed' WHERE status IS NULL")
    conn.execute("UPDATE documents SET chunk_count=0 WHERE chunk_count IS NULL")
    conn.execute("UPDATE documents SET updated_at=created_at WHERE updated_at IS NULL")
    # Backfill chunks for legacy documents using plain-text chunking so they
    # remain searchable through the new chunk retriever (idempotent: only
    # when the document has no chunks yet).
    legacy = conn.execute(
        "SELECT id, content FROM documents WHERE "
        "(SELECT COUNT(*) FROM document_chunks c WHERE c.document_id=documents.id)=0"
    ).fetchall()
    for row in legacy:
        pages = [Page(text=row["content"])]
        chunks = document_chunk.chunk_pages(pages, ext=".txt")
        for chunk in chunks:
            conn.execute(
                "INSERT INTO document_chunks(document_id,chunk_index,content,page_hint,heading,char_count,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (row["id"], chunk.chunk_index, chunk.content, chunk.page_hint,
                 chunk.heading, chunk.char_count, _now_iso()),
            )
        conn.execute("UPDATE documents SET chunk_count=? WHERE id=?", (len(chunks), row["id"]))


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _migrate_recommendations(conn: sqlite3.Connection) -> None:
    """Add 1.3.3-E consistency indexes without deleting or merging user data.

    If a pre-E database already contains active duplicates (possible because
    the old index was non-unique), do not silently choose or delete a row;
    leave the ordinary index in place and record an operational warning.
    """
    if "idx_recommendations_active_dedup" not in _index_names(conn, "recommendations"):
        active_duplicates = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT dedup_key FROM recommendations "
            "WHERE status IN ('pending','accepted') GROUP BY dedup_key HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if active_duplicates == 0:
            conn.execute(
                "CREATE UNIQUE INDEX idx_recommendations_active_dedup "
                "ON recommendations(dedup_key) WHERE status IN ('pending','accepted')"
            )
        else:
            logger.warning(
                "recommendation active-dedup index skipped: %s duplicate active groups",
                active_duplicates,
            )

    # Recover crashed/interrupted runs before enforcing the one-running index.
    rec_store.recover_stale_runs(conn)
    if "idx_recommendation_runs_one_running" not in _index_names(conn, "recommendation_runs"):
        running_count = conn.execute(
            "SELECT COUNT(*) FROM recommendation_runs WHERE status='running'"
        ).fetchone()[0]
        if running_count <= 1:
            conn.execute(
                "CREATE UNIQUE INDEX idx_recommendation_runs_one_running "
                "ON recommendation_runs(triggered_by) WHERE status='running'"
            )
        else:
            logger.warning(
                "recommendation one-running index skipped: %s unfinished runs",
                running_count,
            )




def _migrate_chunks_fts(conn: sqlite3.Connection) -> None:
    """FTS5 index over document_chunks (external-content, trigram tokenizer).

    trigram (SQLite >= 3.34) is required because unicode61 treats a run of
    CJK characters as ONE token, which makes Chinese queries useless. With
    trigram, queries shorter than 3 characters match nothing — the lexical
    bigram scorer in app.retriever remains the fallback for those.

    The index is derived data: rebuilding it is always safe, and the backfill
    below is incremental (rowid NOT IN fts) so init_db stays idempotent.
    """
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5("
        "content, heading, content='document_chunks', content_rowid='id', "
        "tokenize='trigram')"
    )
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(rowid, content, heading)
            VALUES (new.id, new.content, COALESCE(new.heading, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content, heading)
            VALUES ('delete', old.id, old.content, COALESCE(old.heading, ''));
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON document_chunks BEGIN
            INSERT INTO document_chunks_fts(document_chunks_fts, rowid, content, heading)
            VALUES ('delete', old.id, old.content, COALESCE(old.heading, ''));
            INSERT INTO document_chunks_fts(rowid, content, heading)
            VALUES (new.id, new.content, COALESCE(new.heading, ''));
        END;
    """)
    conn.execute(
        "INSERT INTO document_chunks_fts(rowid, content, heading) "
        "SELECT id, content, COALESCE(heading, '') FROM document_chunks "
        "WHERE id NOT IN (SELECT rowid FROM document_chunks_fts)"
    )


def rebuild_chunks_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the chunks FTS index from the content table.

    Self-heal for index/content desync (which makes every chunk DELETE raise
    "database disk image is malformed"): trigram re-tokenizes every row, so
    after this the delete triggers can run again. Safe to run any time —
    the index is derived data.
    """
    conn.execute("INSERT INTO document_chunks_fts(document_chunks_fts) VALUES('rebuild')")
