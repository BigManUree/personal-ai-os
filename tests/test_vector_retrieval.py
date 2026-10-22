"""Contract tests for the vector retrieval layer (feature 014).

Deterministic fake embeddings: 3 orthogonal directions keyed by topic words, so
cosine similarity is exactly 1.0 / 0.0 and nothing depends on the network.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import embeddings, retriever
from app.main import app, init_db

client = TestClient(app)

NOW = "2026-10-22T09:00:00"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "vector.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    embeddings._query_cache.clear()
    return db_path


@pytest.fixture
def fake_embedding(monkeypatch):
    """Topic → orthogonal unit vector; unrelated text → third axis."""
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://fake.local/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "fake-embedding")

    def fake(texts):
        out = []
        for t in texts:
            if any(k in t for k in ("跑步", "晨练", "慢跑")):
                out.append([1.0, 0.0, 0.0])
            elif any(k in t for k in ("数据库", "索引")):
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out

    monkeypatch.setattr(embeddings, "embed_texts", fake)
    embeddings._query_cache.clear()
    return fake


def _seed_doc(db_path, chunks: list[str], filename: str = "笔记.md") -> list[int]:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO documents(filename,content,source_type,status,created_at,updated_at,chunk_count) "
            "VALUES(?,'x','manual_text','parsed',?,? ,?)",
            (filename, NOW, NOW, len(chunks)),
        )
        doc_id = cur.lastrowid
        ids = []
        for i, text in enumerate(chunks):
            c = conn.execute(
                "INSERT INTO document_chunks(document_id,chunk_index,content,heading,char_count,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (doc_id, i, text, None, len(text), NOW),
            )
            ids.append(c.lastrowid)
    return ids


def test_embedding_table_created(isolated_db):
    with sqlite3.connect(isolated_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chunk_embeddings)")}
    assert {"chunk_id", "model", "dim", "vector", "created_at"} <= cols


def test_ensure_chunks_stores_normalized_vectors(isolated_db, fake_embedding):
    ids = _seed_doc(isolated_db, ["每天早晨跑步五公里", "数据库索引优化"])
    with sqlite3.connect(isolated_db) as conn:
        stored = embeddings.ensure_chunks(conn, ids)
        assert stored == 2
        rows = conn.execute(
            "SELECT chunk_id, model, dim, vector FROM chunk_embeddings ORDER BY chunk_id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][1] == "fake-embedding" and rows[0][2] == 3
        vec = embeddings.deserialize(rows[0][3])
        assert list(vec) == [1.0, 0.0, 0.0]
        # second call is a no-op (already indexed for this model)
        assert embeddings.ensure_chunks(conn, ids) == 0


def test_hybrid_retrieval_finds_semantic_match_without_lexical_overlap(isolated_db, fake_embedding):
    ids = _seed_doc(isolated_db, ["每天早晨跑步五公里", "数据库索引优化"])
    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        embeddings.ensure_chunks(conn, ids)
        # sanity: the query shares no bigram/trigram with the running chunk,
        # so lexical + FTS alone score it zero.
        assert retriever.lexical_score("晨练习惯", "每天早晨跑步五公里") == 0
        hits = retriever.search_chunks(conn, "晨练习惯")
    assert hits, "vector recall should surface the semantically related chunk"
    assert hits[0]["content"] == "每天早晨跑步五公里"
    assert hits[0]["vector_similarity"] == 1.0
    assert hits[0]["score"] >= embeddings.SIM_THRESHOLD * embeddings.VECTOR_WEIGHT


def test_vector_disabled_degrades_to_lexical(isolated_db, monkeypatch):
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    embeddings._query_cache.clear()
    ids = _seed_doc(isolated_db, ["每天早晨跑步五公里"])
    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        assert embeddings.ensure_chunks(conn, ids) == 0
        assert embeddings.query_scores(conn, "晨练") == {}
        hits = retriever.search_chunks(conn, "跑步")
    assert hits and hits[0]["content"] == "每天早晨跑步五公里"
    assert "vector_similarity" not in hits[0]


def test_status_and_reindex_endpoints(isolated_db, fake_embedding):
    ids = _seed_doc(isolated_db, ["每天早晨跑步五公里", "数据库索引优化", "与两者无关的杂项"])
    status = client.get("/api/embeddings/status").json()
    assert status["enabled"] is True
    assert status["chunks_total"] == 3 and status["chunks_indexed"] == 0
    assert status["coverage"] == 0.0

    body = client.post("/api/embeddings/reindex", params={"limit": 2}).json()
    assert body["indexed"] == 2 and body["remaining"] == 1

    body = client.post("/api/embeddings/reindex", params={"limit": 2}).json()
    assert body["indexed"] == 1 and body["remaining"] == 0

    status = client.get("/api/embeddings/status").json()
    assert status["chunks_indexed"] == 3 and status["coverage"] == 1.0
    assert status["dim"] == 3

    assert client.post("/api/embeddings/reindex", params={"limit": 0}).status_code == 400


def test_embedding_failure_does_not_break_ingest(isolated_db, monkeypatch):
    """A dead embedding endpoint must not fail document creation."""
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_MODEL", "fake-embedding")
    embeddings._query_cache.clear()
    resp = client.post("/api/documents/text", json={
        "filename": "离线.md", "content": "向量服务不可用时仍应入库成功。" * 3,
    })
    assert resp.status_code == 200, resp.text
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0] >= 1
