"""Contract tests for feature 008: FTS5 chunk index + SSE chat streaming."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import retriever
from app.main import app, init_db

client = TestClient(app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "fts_stream.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    init_db()
    return db_path


# ---------- FTS migration ----------

def test_fts_table_created_and_backfilled(isolated_db):
    client.post("/api/documents/text", json={
        "filename": "咖啡.md", "content": "我的咖啡偏好是喝美式咖啡，不加糖。" * 5,
    })
    with sqlite3.connect(isolated_db) as conn:
        chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM document_chunks_fts").fetchone()[0]
        assert chunks >= 1 and fts == chunks
        # the FTS row actually indexes the content
        hits = conn.execute(
            "SELECT rowid FROM document_chunks_fts WHERE document_chunks_fts MATCH ?",
            ('"咖啡偏好"',),
        ).fetchall()
        assert hits


def test_fts_migration_is_idempotent(isolated_db):
    client.post("/api/documents/text", json={
        "filename": "idem.md", "content": "幂等性验证内容第一段落。" * 8,
    })
    with sqlite3.connect(isolated_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM document_chunks_fts").fetchone()[0]
    init_db()
    init_db()
    with sqlite3.connect(isolated_db) as conn:
        after = conn.execute("SELECT COUNT(*) FROM document_chunks_fts").fetchone()[0]
    assert before == after >= 1


def test_fts_triggers_keep_index_in_sync(isolated_db):
    client.post("/api/documents/text", json={
        "filename": "同步.md", "content": "触发器同步验证的内容样本。" * 8,
    })
    with sqlite3.connect(isolated_db) as conn:
        doc_id = conn.execute("SELECT id FROM documents WHERE filename='同步.md'").fetchone()[0]
        conn.execute(
            "INSERT INTO document_chunks(document_id,chunk_index,content,heading,char_count,created_at) "
            "VALUES(?,?,?,?,?,?)", (doc_id, 99, "后补的索引同步测试段落。", None, 12, "t"),
        )
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM document_chunks_fts WHERE document_chunks_fts MATCH ?",
            ('"索引同步"',),
        ).fetchone()[0] >= 1
        # delete via cascade removes the FTS row too (raw connections default
        # to foreign_keys=OFF, so enable it explicitly)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM document_chunks_fts WHERE document_chunks_fts MATCH ?",
            ('"索引同步"',),
        ).fetchone()[0] == 0


def test_deleted_chunks_leave_fts_via_api_cascade(isolated_db):
    doc = client.post("/api/documents/text", json={
        "filename": "级联.md", "content": "API 级联删除的索引一致性样本。" * 8,
    }).json()
    with sqlite3.connect(isolated_db) as conn:
        assert client.delete(f"/api/documents/{doc['id']}").status_code == 200
        fts = conn.execute("SELECT COUNT(*) FROM document_chunks_fts").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        assert fts == chunks


# ---------- search_chunks with FTS boost ----------

def test_search_chunks_uses_fts_boost_and_keeps_contract(isolated_db):
    client.post("/api/documents/text", json={
        "filename": "咖啡.md", "content": "我的咖啡偏好是喝美式咖啡，不加糖。" * 5,
    })
    client.post("/api/documents/text", json={
        "filename": "运动.md", "content": "每周跑步三次，力量训练两次。" * 5,
    })
    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        hits = retriever.search_chunks(conn, "咖啡偏好 美式")
        assert hits, "expected chunk hits"
        assert "咖啡" in hits[0]["filename"]
        assert hits[0]["score"] > 0
        assert {"chunk_id", "document_id", "filename", "excerpt", "score"} <= set(hits[0])


def test_search_chunks_short_query_falls_back_to_lexical(isolated_db):
    client.post("/api/documents/text", json={
        "filename": "跑.md", "content": "每天跑步五公里保持体能。" * 8,
    })
    with sqlite3.connect(isolated_db) as conn:
        conn.row_factory = sqlite3.Row
        # 「跑步」 is 2 chars: below trigram minimum, pure lexical must still hit
        hits = retriever.search_chunks(conn, "跑步")
        assert hits and hits[0]["score"] > 0


def test_search_chunks_survives_dropped_fts_table(isolated_db):
    client.post("/api/documents/text", json={
        "filename": "降级.md", "content": "索引缺失时的降级路径验证内容。" * 8,
    })
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("DROP TABLE document_chunks_fts")
        conn.commit()
        conn.row_factory = sqlite3.Row
        hits = retriever.search_chunks(conn, "降级路径验证")
        assert hits and hits[0]["score"] > 0  # lexical fallback, no exception


# ---------- /api/chat/stream ----------

def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        ev = data = None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
        if ev and data is not None:
            events.append((ev, data))
    return events


def test_chat_stream_event_sequence_and_persistence(isolated_db, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with client.stream("POST", "/api/chat/stream", json={"message": "流式输出的验证问题"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(chunk.decode("utf-8") for chunk in resp.iter_bytes())
    events = _parse_sse(body)
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[-1] == "done"
    assert "delta" in names and "error" not in names

    meta = events[0][1]
    done = events[-1][1]
    assert isinstance(meta["conversation_id"], int)
    deltas = "".join(d["t"] for _, d in events if _ == "delta") if False else \
        "".join(d["t"] for name, d in events if name == "delta")
    assert deltas == done["answer"], "delta stream must assemble into the final answer"

    conv_id = meta["conversation_id"]
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == done["answer"]


def test_chat_stream_reuses_conversation_and_rejects_unknown(isolated_db, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    first = client.post("/api/chat", json={"message": "用于复用的既有对话"}).json()
    conv_id = first["conversation_id"]
    with client.stream("POST", "/api/chat/stream", json={"message": "继续这个话题", "conversation_id": conv_id}) as resp:
        body = "".join(chunk.decode("utf-8") for chunk in resp.iter_bytes())
    events = _parse_sse(body)
    assert events[0][1]["conversation_id"] == conv_id
    msgs = client.get(f"/api/conversations/{conv_id}/messages").json()
    assert len(msgs) == 4  # 2 from POST /api/chat + 2 from the stream

    bad = client.post("/api/chat/stream", json={"message": "x", "conversation_id": 999999})
    assert bad.status_code == 404


def test_extraction_filters_hyphenated_key_shaped_secrets(isolated_db, monkeypatch):
    """E1 (feature 009 mock integration): 'api-key-sk-123' style embedded
    credentials used to slip past the ":"-anchored patterns."""
    from app import memory_extract

    raw = json.dumps({"candidates": [
        {"content": "机密信息请勿保存 api-key-sk-123", "memory_type": "fact",
         "confidence": 0.8, "reason": "疑似凭据"},
        {"content": "openai_key-abcdef123456 是我常用的", "memory_type": "fact",
         "confidence": 0.8, "reason": "疑似凭据"},
        {"content": "正常内容没有凭据", "memory_type": "fact",
         "confidence": 0.9, "reason": "普通事实"},
    ]}, ensure_ascii=False)
    cands = memory_extract.parse_extraction_json(raw, 1)
    assert [c["content"] for c in cands] == ["正常内容没有凭据"]


def test_chat_stream_error_event_when_llm_fails(isolated_db, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    with client.stream("POST", "/api/chat/stream", json={"message": "必然失败的问题"}) as resp:
        body = "".join(chunk.decode("utf-8") for chunk in resp.iter_bytes())
    events = _parse_sse(body)
    assert events[0][0] == "meta"
    assert events[-1][0] == "error" and "detail" in events[-1][1]
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)


# ---------- 删除自愈与对话删除（feature 013 追加） ----------

def test_delete_document_self_heals_desynced_fts(isolated_db):
    """Form B desync: a chunk inserted while the AI trigger was missing never
    enters the index; the delete-time 'delete' command then raises
    "database disk image is malformed". The route must rebuild the index and
    retry instead of returning 500."""
    now = "2026-10-22T08:00:00"
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.execute(
            "INSERT INTO documents(filename,content,source_type,status,created_at,updated_at,chunk_count) "
            "VALUES('desync.md','x','manual_text','parsed',?,? ,1)", (now, now))
        did = cur.lastrowid
        # simulate the desync: insert the chunk without the AI trigger present
        conn.execute("DROP TRIGGER chunks_fts_ai")
        conn.execute(
            "INSERT INTO document_chunks(document_id,chunk_index,content,heading,char_count,created_at) "
            "VALUES(?,?,?,?,?,?)", (did, 0, "从未入索引的内容" * 3, None, 30, now))
        conn.execute(
            "CREATE TRIGGER chunks_fts_ai AFTER INSERT ON document_chunks BEGIN "
            "INSERT INTO document_chunks_fts(rowid, content, heading) "
            "VALUES (new.id, new.content, COALESCE(new.heading, '')); END;")

    # sanity (separate connection so the rollback cannot undo the seeding):
    # the direct delete really does fail before the fix path
    with sqlite3.connect(isolated_db) as conn:
        try:
            conn.execute("DELETE FROM document_chunks WHERE document_id=?", (did,))
            raise AssertionError("expected malformed error, got clean delete")
        except sqlite3.DatabaseError:
            pass  # expected; the with-block rolls the failed statement back
        assert conn.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?", (did,)).fetchone()[0] == 1

    resp = client.delete(f"/api/documents/{did}")
    assert resp.status_code == 200, resp.text
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE id=?", (did,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?", (did,)).fetchone()[0] == 0


def test_delete_conversation_cascades_messages_and_404_after(isolated_db):
    now = "2026-10-22T08:00:00"
    with sqlite3.connect(isolated_db) as conn:
        cur = conn.execute("INSERT INTO conversations(title,created_at) VALUES('待删对话',?)", (now,))
        cid = cur.lastrowid
        conn.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                     (cid, "user", "你好", now))
        conn.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
                     (cid, "assistant", "你好呀", now))

    resp = client.delete(f"/api/conversations/{cid}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM conversations WHERE id=?", (cid,)).fetchone()[0] == 0
    assert client.delete(f"/api/conversations/{cid}").status_code == 404
