"""Phase 2 (iterations 01-02): memory schema migration + auto candidates.

All model calls are mocked; no real API key or network access is required.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import memory_extract
from app.main import app, init_db

client = TestClient(app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase2.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    return db_path


def _fresh_client(isolated_db):
    init_db()
    return client


def test_fresh_schema_has_new_columns(isolated_db):
    _fresh_client(isolated_db)
    with sqlite3.connect(isolated_db) as conn:
        mem_cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        cand_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_candidates)")}
    assert {"memory_type", "source_type", "source_id", "status", "valid_from", "valid_until", "supersedes_id"} <= mem_cols
    assert {"content", "memory_type", "source_type", "source_id", "confidence", "reason", "status", "duplicate_of", "conflict_with"} <= cand_cols


def test_legacy_migration_preserves_data_and_backfills(isolated_db):
    # Build a database with the phase-1 (legacy) schema and real data.
    with sqlite3.connect(isolated_db) as conn:
        conn.executescript("""
        CREATE TABLE profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            display_name TEXT NOT NULL DEFAULT '我',
            goals TEXT NOT NULL DEFAULT '',
            preferences TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        INSERT INTO profile VALUES (1, '旧用户', '旧目标', '', '2026-10-01T00:00:00+00:00');
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL DEFAULT 'manual',
            confidence REAL NOT NULL DEFAULT 1.0,
            confirmed INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO memories(content,category,source,confidence,confirmed,created_at,updated_at)
        VALUES ('已确认的旧记忆', 'preference', 'import', 0.8, 1, 't1', 't1'),
               ('未确认的旧记忆', 'general', 'conversation', 0.4, 0, 't2', 't2');
        CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '新对话', created_at TEXT NOT NULL);
        INSERT INTO conversations(id,title,created_at) VALUES (1, '旧对话', 't0');
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO messages(conversation_id,role,content,created_at) VALUES (1,'user','旧消息','t0');
        CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO documents(filename,content,created_at) VALUES ('旧资料.txt', '旧资料内容', 't0');
        """)

    init_db()  # must migrate in place without dropping anything

    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        profile = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
        assert profile["display_name"] == "旧用户" and profile["goals"] == "旧目标"
        rows = {r["content"]: dict(r) for r in conn.execute("SELECT * FROM memories")}
        assert set(rows) == {"已确认的旧记忆", "未确认的旧记忆"}
        assert rows["已确认的旧记忆"]["status"] == "confirmed"
        assert rows["已确认的旧记忆"]["source_type"] == "import"
        assert rows["已确认的旧记忆"]["memory_type"] == "fact"
        assert rows["未确认的旧记忆"]["status"] == "pending"
        # Legacy columns are retained, not wiped.
        assert rows["已确认的旧记忆"]["confirmed"] == 1
        assert rows["已确认的旧记忆"]["source"] == "import"
        assert conn.execute("SELECT content FROM messages").fetchone()[0] == "旧消息"
        assert conn.execute("SELECT filename FROM documents").fetchone()[0] == "旧资料.txt"

    # Re-running migration must be a no-op (idempotent).
    init_db()
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2


def test_parse_extraction_json_keeps_valid_drops_bad():
    raw = {
        "candidates": [
            {"content": "喜欢分阶段交付", "memory_type": "preference", "source_type": "conversation",
             "source_id": None, "confidence": 0.9, "reason": "用户明确表达稳定偏好"},
            {"content": "  ", "memory_type": "fact", "confidence": 0.5, "reason": "空内容"},
            {"content": "非法类型", "memory_type": "nope", "confidence": 0.5, "reason": "x"},
            {"content": "越界置信度", "memory_type": "fact", "confidence": 1.7, "reason": "x"},
            {"content": "缺少原因", "memory_type": "fact", "confidence": 0.5},
            "not-an-object",
            {"content": "重复内容", "memory_type": "goal", "confidence": 0.6, "reason": "a"},
            {"content": "重复内容", "memory_type": "goal", "confidence": 0.6, "reason": "b"},
        ]
    }
    import json as _json
    result = memory_extract.parse_extraction_json(_json.dumps(raw, ensure_ascii=False), conversation_id=7)
    assert [c["content"] for c in result] == ["喜欢分阶段交付", "重复内容"]
    first = result[0]
    # Server-side provenance always overrides model output.
    assert first["source_type"] == "conversation" and first["source_id"] == 7


def test_parse_extraction_json_rejects_markdown_fenced_and_secret():
    fenced = '```json\n{"candidates":[{"content":"目标是减重 5kg","memory_type":"goal","confidence":0.8,"reason":"长期目标"}]}\n```'
    result = memory_extract.parse_extraction_json(fenced, 3)
    assert len(result) == 1 and result[0]["source_id"] == 3

    secret_raw = '{"candidates":[{"content":"我的 API_KEY: sk-abcd1234efgh5678ijkl","memory_type":"fact","confidence":0.9,"reason":"凭据"}]}'
    assert memory_extract.parse_extraction_json(secret_raw, 1) == []


def test_chat_auto_extracts_pending_candidates(isolated_db, monkeypatch):
    _fresh_client(isolated_db)

    async def fake_extract(messages, conversation_id):
        assert any(m["role"] == "user" for m in messages)
        return [
            {"content": "用户在做个人 AI OS 项目", "memory_type": "experience", "source_type": "conversation",
             "source_id": conversation_id, "confidence": 0.86, "reason": "持续进行的项目状态"},
            {"content": "偏好干净的提交历史", "memory_type": "preference", "source_type": "conversation",
             "source_id": conversation_id, "confidence": 0.8, "reason": "反复表达"},
        ]

    monkeypatch.setattr(memory_extract, "extract_candidates_from_messages", fake_extract)

    resp = client.post("/api/chat", json={"message": "我最近在做个人 AI OS，我喜欢干净的提交历史"})
    assert resp.status_code == 200
    conversation_id = resp.json()["conversation_id"]

    candidates = client.get("/api/memory-candidates").json()
    assert len(candidates) == 2
    assert all(c["status"] == "pending" for c in candidates)
    assert all(c["source_type"] == "conversation" and c["source_id"] == conversation_id for c in candidates)
    # Candidates must NOT be written into the confirmed memories table.
    assert client.get("/api/memories").json() == []

    pending_only = client.get("/api/memory-candidates?status=pending").json()
    assert len(pending_only) == 2
    assert client.get("/api/memory-candidates?status=bogus").status_code == 400


def test_chat_survives_extraction_failure(isolated_db, monkeypatch):
    _fresh_client(isolated_db)

    async def boom(messages, conversation_id):
        raise RuntimeError("model down")

    monkeypatch.setattr(memory_extract, "extract_candidates_from_messages", boom)

    resp = client.post("/api/chat", json={"message": "你好"})
    assert resp.status_code == 200
    assert "conversation_id" in resp.json()
    # Assistant reply was still persisted; no candidates were stored.
    with sqlite3.connect(isolated_db) as conn:
        roles = [r[0] for r in conn.execute("SELECT role FROM messages ORDER BY id")]
        assert roles == ["user", "assistant"]
        assert conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] == 0


def test_exact_duplicate_candidate_is_skipped(isolated_db, monkeypatch):
    _fresh_client(isolated_db)

    async def fake_extract(messages, conversation_id):
        return [
            {"content": "重复候选", "memory_type": "fact", "source_type": "conversation",
             "source_id": conversation_id, "confidence": 0.7, "reason": "第一次"},
        ]

    monkeypatch.setattr(memory_extract, "extract_candidates_from_messages", fake_extract)
    client.post("/api/chat", json={"message": "第一次说"})
    client.post("/api/chat", json={"message": "第二次说"})

    rows = client.get("/api/memory-candidates?status=pending").json()
    assert [c["content"] for c in rows].count("重复候选") == 1


def test_manual_extract_requires_conversation_and_key(isolated_db, monkeypatch):
    _fresh_client(isolated_db)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    # No conversation at all -> 404.
    assert client.post("/api/memory-candidates/extract", json={}).status_code == 404
    # With a conversation but no key (local demo) -> 400.
    client.post("/api/memories", json={"content": "x"})
    chat = client.post("/api/chat", json={"message": "hi"})
    cid = chat.json()["conversation_id"]
    resp = client.post("/api/memory-candidates/extract", json={"conversation_id": cid})
    assert resp.status_code == 400
    # Unknown conversation id -> 404 even with key set.
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    assert client.post("/api/memory-candidates/extract", json={"conversation_id": 99999}).status_code == 404


def test_legacy_memory_api_still_works(isolated_db):
    _fresh_client(isolated_db)
    created = client.post("/api/memories", json={
        "content": "旧接口记忆", "category": "preference", "source": "test",
        "confidence": 1, "confirmed": True,
    })
    assert created.status_code == 200
    body = created.json()
    assert body["confirmed"] == 1 and body["status"] == "confirmed"
    assert body["memory_type"] == "fact" and body["source_type"] == "manual"
    mid = body["id"]

    patched = client.patch(f"/api/memories/{mid}", json={
        "content": "旧接口记忆-改", "category": "goal", "source": "test",
        "confidence": 0.5, "confirmed": False, "memory_type": "goal", "source_type": "manual",
    })
    assert patched.status_code == 200
    assert patched.json()["status"] == "pending" and patched.json()["confirmed"] == 0

    assert client.post("/api/memories", json={"content": "坏类型", "memory_type": "nope"}).status_code == 400
    assert client.delete(f"/api/memories/{mid}").json()["deleted"] is True
    assert client.delete(f"/api/memories/{mid}").status_code == 404

