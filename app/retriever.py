"""Pluggable retrieval layer (phase 2 iteration 04).

The default implementation is keyword/lexical scoring on SQLite. Callers
depend on these functions rather than writing SQL, so a future PostgreSQL +
pgvector / hybrid retriever can replace the implementation without touching
the chat pipeline.

No vector dependencies are introduced in this phase.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

# Shared tokenizer with the matcher keeps scoring behaviour consistent.
from app.memory_match import tokenize

_MEMORY_LIMIT = 300
_DOC_LIMIT = 100
RESULT_LIMIT = 5
DOC_RESULT_LIMIT = 3


def lexical_score(query: str, text: str) -> int:
    """Count how many query tokens (word/bigram) appear in the text."""
    q_tokens = tokenize(query)
    if not q_tokens:
        return 0
    text_tokens = tokenize(text)
    return len(q_tokens & text_tokens)


def _valid_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def search_memories(conn: sqlite3.Connection, query: str, limit: int = RESULT_LIMIT) -> list[dict[str, Any]]:
    """Return confirmed, currently-valid memories ranked by relevance.

    Expired memories (valid_until in the past) and non-confirmed rows are
    excluded.
    """
    now = _valid_now_iso()
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM memories WHERE COALESCE(status,'confirmed')='confirmed' "
            "AND COALESCE(confirmed,1)=1 "
            "AND (valid_until IS NULL OR valid_until='' OR valid_until>?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (now, _MEMORY_LIMIT),
        ).fetchall()
    ]
    for row in rows:
        row["score"] = lexical_score(query, row["content"])
    rows.sort(key=lambda r: (r["score"], r.get("confidence", 0.0)), reverse=True)
    return [r for r in rows if r["score"] > 0][:limit]


def search_documents(conn: sqlite3.Connection, query: str, limit: int = DOC_RESULT_LIMIT) -> list[dict[str, Any]]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, filename, content, created_at FROM documents ORDER BY created_at DESC LIMIT ?",
            (_DOC_LIMIT,),
        ).fetchall()
    ]
    for row in rows:
        filename_boost = 2 if any(t in tokenize(row["filename"]) for t in tokenize(query)) else 0
        row["score"] = lexical_score(query, row["content"]) + filename_boost
    rows.sort(key=lambda r: r["score"], reverse=True)
    return [r for r in rows if r["score"] > 0][:limit]


# A retriever is just (conn, query, limit) -> rows. Reserved for future
# hybrid/vector implementations; not wired to config yet.
MemoryRetriever = Callable[[sqlite3.Connection, str, int], list[dict[str, Any]]]
DocumentRetriever = Callable[[sqlite3.Connection, str, int], list[dict[str, Any]]]


def search_chunks(conn: sqlite3.Connection, query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Keyword search over document chunks with provenance.

    Returns chunk-level hits (with document filename / page / heading) and
    collapses multiple chunks of the same document, keeping the strongest
    chunks. This is the swappable retrieval seam for a future hybrid/vector
    retriever.
    """
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.content, "
            "c.page_hint, c.heading, d.filename, d.status AS doc_status "
            "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE COALESCE(d.status,'parsed')='parsed' "
            "ORDER BY c.document_id, c.chunk_index"
        ).fetchall()
    ]
    query_tokens = tokenize(query)
    for row in rows:
        row["score"] = lexical_score(query, row["content"])
        filename_boost = 2 if any(t in tokenize(row["filename"]) for t in query_tokens) else 0
        heading_boost = 1 if (row.get("heading") and any(t in tokenize(row["heading"]) for t in query_tokens)) else 0
        row["score"] += filename_boost + heading_boost
        row["excerpt"] = row["content"][:280]
    hits = [r for r in rows if r["score"] > 0]
    hits.sort(key=lambda r: (r["score"], r["document_id"], -r["chunk_index"]), reverse=True)

    # Keep at most 2 chunks per document in the final result.
    per_doc: dict[int, int] = {}
    result: list[dict[str, Any]] = []
    for hit in hits:
        count = per_doc.get(hit["document_id"], 0)
        if count >= 2:
            continue
        per_doc[hit["document_id"]] = count + 1
        result.append(hit)
        if len(result) >= limit:
            break
    return result


def _date_due_bonus(due_date: str | None) -> int:
    """Extra weight for tasks due soon or overdue (lexical, no time zone math)."""
    if not due_date:
        return 0
    from datetime import date

    try:
        y, m, d = (int(x) for x in due_date.split("-"))
        due = date(y, m, d)
    except (ValueError, TypeError):
        return 0
    delta = (due - date.today()).days
    if delta < 0:
        return 3  # overdue
    if delta <= 3:
        return 2  # due within 3 days
    if delta <= 14:
        return 1
    return 0


def search_work_items(conn: sqlite3.Connection, query: str, limit: int = 4) -> dict[str, list[dict[str, Any]]]:
    """Keyword search over active projects and open tasks with provenance.

    Completed/archived items are excluded. Task urgency (overdue / due soon)
    and project-name matches add weight. Same swappable-retriever seam as
    memories and chunks; a vector/hybrid implementation can replace it later.
    """
    project_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM projects WHERE status IN ('planning','active','paused') ORDER BY updated_at DESC"
        ).fetchall()
    ]
    projects: list[dict[str, Any]] = []
    for row in project_rows:
        haystack = " ".join([row.get("name", ""), row.get("description", ""), row.get("tech_stack", "")])
        score = lexical_score(query, haystack)
        if score > 0:
            row["score"] = score
            projects.append(row)
    projects.sort(key=lambda r: (r["score"], r["status"] == "active"), reverse=True)
    projects = projects[:limit]
    project_ids = {r["id"] for r in projects}

    task_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT t.*, p.name AS project_name FROM tasks t "
            "LEFT JOIN projects p ON p.id = t.project_id "
            "WHERE t.status IN ('todo','doing') ORDER BY (t.due_date IS NULL), t.due_date, t.id"
        ).fetchall()
    ]
    tasks: list[dict[str, Any]] = []
    for row in task_rows:
        haystack = " ".join([row.get("title", ""), row.get("detail", ""), row.get("project_name") or ""])
        score = lexical_score(query, haystack)
        if score <= 0:
            continue
        if row.get("priority") == "high":
            score += 1
        score += _date_due_bonus(row.get("due_date"))
        if row.get("project_id") in project_ids:
            score += 1
        row["score"] = score
        tasks.append(row)
    tasks.sort(key=lambda r: r["score"], reverse=True)
    tasks = tasks[:limit]
    return {"projects": projects, "tasks": tasks}
