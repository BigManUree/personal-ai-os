"""Personal AI OS 1.2 iteration 3.5.1: connector framework.

Everything runs offline against the built-in MockConnector (and a couple
of throwaway test connectors registered only for one test). No network,
no real GitHub/local-disk access, no LLM calls.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import connectors
from app.main import app, init_db

client = TestClient(app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase35_connectors.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    init_db()  # idempotent
    return db_path


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _make_source(name="测试源", config=None, scope="只读测试范围", client_=client):
    payload = {"kind": "mock", "name": name, "scope": scope,
               "config": config if config is not None else {"items": []}}
    r = client_.post("/api/data-sources", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _item(external_id, content, title="", **meta):
    d = {"external_id": external_id, "content": content, "title": title}
    if meta:
        d["metadata"] = meta
    return d


# ---------- registry ----------

def test_registry_register_duplicate_unknown():
    reg = connectors.ConnectorRegistry()
    reg.register("x", connectors.MockConnector)
    with pytest.raises(ValueError):
        reg.register("x", connectors.MockConnector)
    assert reg.is_registered("x")
    with pytest.raises(connectors.ConnectorUnknownError):
        reg.create("nope")
    with pytest.raises(ValueError):
        reg.register("", connectors.MockConnector)
    with pytest.raises(ValueError):
        reg.register("bad", object)  # type: ignore[arg-type]
    assert {"kind": "x", "label": "Mock 测试连接器"} in reg.available()


def test_builtin_registry_exposes_all_connectors():
    kinds = {c["kind"] for c in connectors.registry.available()}
    assert kinds == {"mock", "local_dir", "github", "http_api"}


# ---------- standard item validation ----------

def test_standard_item_validation_and_hash():
    item = connectors.StandardItem(external_id="a", content="hello")
    assert item.content_hash and len(item.content_hash) == 64  # sha256 auto
    with pytest.raises(ValidationError):
        connectors.StandardItem(external_id="", content="x")
    with pytest.raises(ValidationError):
        connectors.StandardItem(external_id="a", content="   ")
    with pytest.raises(ValidationError):
        connectors.StandardItem(external_id="a", content="x", metadata={"token": "sk-113febd4eaf24d2dafd2200f38913e3d"})


def test_redact_secrets():
    out = connectors.redact_secrets("key=sk-113febd4eaf24d2dafd2200f38913e3d Authorization: Bearer abcdef1234567890")
    assert "sk-" not in out and "abcdef1234567890" not in out and "[REDACTED]" in out
    with pytest.raises(connectors.ConnectorConfigError):
        connectors.assert_config_has_no_secrets({"api_key": "abcdef123456"})


# ---------- data source CRUD ----------

def test_data_source_crud_and_validation(isolated_db):
    assert client.get("/api/data-sources").json() == []
    # unknown kind
    r = client.post("/api/data-sources", json={"kind": "github", "name": "x"})
    assert r.status_code == 400
    # secret in config rejected
    r = client.post("/api/data-sources", json={"kind": "mock", "name": "x", "config": {"token": "sk-113febd4eaf24d2dafd2200f38913e3d"}})
    assert r.status_code == 400
    # blank name
    r = client.post("/api/data-sources", json={"kind": "mock", "name": "   "})
    assert r.status_code == 422

    src = _make_source("我的目录", {"items": []})
    assert src["status"] == "active" and src["cursor"] is None and src["config"] == {"items": []}
    sid = src["id"]
    assert client.get(f"/api/data-sources/{sid}").status_code == 200
    assert client.get("/api/data-sources/999").status_code == 404

    upd = client.patch(f"/api/data-sources/{sid}", json={"name": "改名", "scope": "新范围"})
    assert upd.status_code == 200 and upd.json()["name"] == "改名"
    assert client.patch(f"/api/data-sources/{sid}", json={}).status_code == 400


def test_create_rolls_back_when_validate_config_fails(isolated_db):
    r = client.post("/api/data-sources", json={"kind": "mock", "name": "坏配置", "config": {"fail": "config"}})
    assert r.status_code == 400
    assert client.get("/api/data-sources").json() == []  # row removed after failed validation


# ---------- first sync / dedup / cursor ----------

def test_first_sync_creates_documents_and_checkpoint(isolated_db):
    src = _make_source("S1", {"items": [_item("doc-1", "连接器首次同步内容Alpha", "说明.md")], "next_cursor": "c1"})
    r = client.post(f"/api/data-sources/{src['id']}/sync")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success" and body["created"] == 1 and body["cursor"] == "c1"

    detail = client.get(f"/api/data-sources/{src['id']}").json()
    assert detail["cursor"] == "c1"
    assert len(detail["items"]) == 1 and detail["items"][0]["document_id"] is not None

    # document + chunks landed through the existing pipeline, marked connector source
    db = _conn(isolated_db)
    doc = db.execute("SELECT source_type,file_hash FROM documents WHERE id=?",
                     (detail["items"][0]["document_id"],)).fetchone()
    chunk_count = db.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=?",
                             (detail["items"][0]["document_id"],)).fetchone()[0]
    db.close()
    assert doc["source_type"] == "connector" and doc["file_hash"] and chunk_count >= 1

    # idempotent re-sync: same hash -> unchanged, no duplicate documents
    r2 = client.post(f"/api/data-sources/{src['id']}/sync")
    assert r2.json()["unchanged"] == 1 and r2.json()["created"] == 0
    db = _conn(isolated_db)
    assert db.execute("SELECT COUNT(*) FROM documents WHERE source_type='connector'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM source_items").fetchone()[0] == 1
    db.close()


def test_incremental_update_replaces_old_content(isolated_db):
    src = _make_source("S2", {"items": [_item("doc-1", "旧内容独一无二AlphaToken")], "next_cursor": "c1"})
    client.post(f"/api/data-sources/{src['id']}/sync")
    old_doc_id = client.get(f"/api/data-sources/{src['id']}").json()["items"][0]["document_id"]

    client.patch(f"/api/data-sources/{src['id']}", json={"config": {
        "items": [_item("doc-1", "新内容独一无二BetaToken")], "next_cursor": "c2"}})
    r = client.post(f"/api/data-sources/{src['id']}/sync")
    assert r.json()["updated"] == 1 and r.json()["cursor"] == "c2"

    detail = client.get(f"/api/data-sources/{src['id']}").json()
    new_doc_id = detail["items"][0]["document_id"]
    assert new_doc_id != old_doc_id
    db = _conn(isolated_db)
    # old document and chunks removed, new ones searchable
    assert db.execute("SELECT COUNT(*) FROM documents WHERE id=?", (old_doc_id,)).fetchone()[0] == 0
    hits = db.execute("SELECT COUNT(*) FROM document_chunks WHERE content LIKE '%BetaToken%'").fetchone()[0]
    stale = db.execute("SELECT COUNT(*) FROM document_chunks WHERE content LIKE '%AlphaToken%'").fetchone()[0]
    db.close()
    assert hits >= 1 and stale == 0


def test_external_delete_only_affects_own_source(isolated_db):
    a = _make_source("A", {"items": [_item("shared", "两个源相同内容Gamma")]})
    b = _make_source("B", {"items": [_item("shared", "两个源相同内容Gamma")]})
    client.post(f"/api/data-sources/{a['id']}/sync")
    client.post(f"/api/data-sources/{b['id']}/sync")
    db = _conn(isolated_db)
    assert db.execute("SELECT COUNT(*) FROM documents WHERE source_type='connector'").fetchone()[0] == 2
    db.close()

    # A reports the resource deleted externally
    client.patch(f"/api/data-sources/{a['id']}", json={"config": {"items": [], "deleted_ids": ["shared"]}})
    r = client.post(f"/api/data-sources/{a['id']}/sync")
    assert r.json()["deleted"] == 1

    # deleting data source A must not touch B's document
    assert client.delete(f"/api/data-sources/{a['id']}").status_code == 200
    assert client.get(f"/api/data-sources/{a['id']}").status_code == 404
    db = _conn(isolated_db)
    b_doc = db.execute(
        "SELECT d.id FROM source_items si JOIN documents d ON d.id=si.document_id "
        "WHERE si.data_source_id=? AND si.status='active'", (b["id"],)).fetchall()
    a_left = db.execute("SELECT COUNT(*) FROM source_items WHERE data_source_id=?", (a["id"],)).fetchone()[0]
    logs_left = db.execute("SELECT COUNT(*) FROM sync_logs WHERE data_source_id=?", (a["id"],)).fetchone()[0]
    db.close()
    assert len(b_doc) == 1 and a_left == 0 and logs_left == 0

    # already-deleted id reported again -> skipped, not double counted
    client.patch(f"/api/data-sources/{b['id']}", json={"config": {"items": [], "deleted_ids": ["shared", "missing"]}})
    body = client.post(f"/api/data-sources/{b['id']}/sync").json()
    assert body["deleted"] == 1 and body["skipped"] == 1


# ---------- failure handling ----------

def test_temporary_fetch_error_keeps_data_and_cursor(isolated_db):
    src = _make_source("S3", {"items": [_item("d", "持久内容Delta")], "next_cursor": "c1"})
    client.post(f"/api/data-sources/{src['id']}/sync")
    client.patch(f"/api/data-sources/{src['id']}", json={"config": {"fail": "fetch", "next_cursor": "SHOULD-NOT-SAVE"}})
    r = client.post(f"/api/data-sources/{src['id']}/sync")
    assert r.json()["status"] == "failed" and r.json()["reason"] == "fetch_error"

    detail = client.get(f"/api/data-sources/{src['id']}").json()
    assert detail["cursor"] == "c1"  # cursor did not advance
    assert detail["status"] == "active"  # not revoked on a transient error
    db = _conn(isolated_db)
    assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1  # nothing deleted
    log = db.execute("SELECT status,error,cursor_after FROM sync_logs WHERE data_source_id=? ORDER BY id DESC LIMIT 1",
                     (src["id"],)).fetchone()
    db.close()
    assert log["status"] == "failed" and log["cursor_after"] is None and "网络错误" in log["error"]


def test_auth_error_revokes_source_without_deleting(isolated_db):
    src = _make_source("S4", {"items": [_item("d", "授权前内容Epsilon")]})
    client.post(f"/api/data-sources/{src['id']}/sync")
    client.patch(f"/api/data-sources/{src['id']}", json={"config": {"fail": "auth"}})
    r = client.post(f"/api/data-sources/{src['id']}/sync")
    assert r.json()["reason"] == "revoked"
    detail = client.get(f"/api/data-sources/{src['id']}").json()
    assert detail["status"] == "revoked"
    db = _conn(isolated_db)
    assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1  # data retained
    db.close()
    # revoked source refuses sync and config edits
    assert client.post(f"/api/data-sources/{src['id']}/sync").status_code == 409
    assert client.patch(f"/api/data-sources/{src['id']}", json={"config": {}}).status_code == 409


def test_pause_blocks_sync_resume_restores(isolated_db):
    src = _make_source("S5", {"items": [_item("d", "暂停内容Zeta")]})
    assert client.post(f"/api/data-sources/{src['id']}/pause").status_code == 200
    assert client.post(f"/api/data-sources/{src['id']}/sync").status_code == 409
    # double pause conflicts
    assert client.post(f"/api/data-sources/{src['id']}/pause").status_code == 409
    assert client.post(f"/api/data-sources/{src['id']}/resume").status_code == 200
    assert client.post(f"/api/data-sources/{src['id']}/sync").json()["status"] == "success"
    # revoke can happen from paused too
    assert client.post(f"/api/data-sources/{src['id']}/pause").status_code == 200
    assert client.post(f"/api/data-sources/{src['id']}/revoke").status_code == 200
    assert client.post(f"/api/data-sources/{src['id']}/resume").status_code == 409


def test_one_bad_item_does_not_abort_batch(isolated_db, monkeypatch):
    """A per-item storage failure increments failed but still commits siblings."""
    import app.main as main_module

    original = main_module._store_parsed_document

    def flaky(conn, **kwargs):
        if "坏苹果" in kwargs.get("content_for_legacy", ""):
            raise RuntimeError("模拟单项入库失败")
        return original(conn, **kwargs)

    monkeypatch.setattr(main_module, "_store_parsed_document", flaky)
    src = _make_source("S6", {"items": [_item("ok", "正常入库Eta"), _item("bad", "坏苹果不入库")]})
    body = client.post(f"/api/data-sources/{src['id']}/sync").json()
    assert body["status"] == "success" and body["created"] == 1 and body["failed"] == 1
    db = _conn(isolated_db)
    assert db.execute("SELECT COUNT(*) FROM source_items WHERE external_id='ok'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM source_items WHERE external_id='bad'").fetchone()[0] == 0
    db.close()


def test_sync_log_error_is_redacted(isolated_db):
    """Connector error containing a credential must be masked in sync_logs."""

    class LeakConnector(connectors.MockConnector):
        async def fetch_changes(self, cursor=None):
            raise connectors.ConnectorFetchError("token=sk-113febd4eaf24d2dafd2200f38913e3d leaked")

    connectors.registry.register("leaktest", LeakConnector)
    try:
        r = client.post("/api/data-sources", json={"kind": "leaktest", "name": "泄露源"})
        assert r.status_code == 200
        sid = r.json()["id"]
        client.post(f"/api/data-sources/{sid}/sync")
        db = _conn(isolated_db)
        log = db.execute("SELECT error FROM sync_logs WHERE data_source_id=? ORDER BY id DESC LIMIT 1",
                         (sid,)).fetchone()
        db.close()
        assert log["error"] and "sk-113f" not in log["error"] and "[REDACTED]" in log["error"]
    finally:
        connectors.registry.unregister("leaktest")
    assert not connectors.registry.is_registered("leaktest")


# ---------- sync logs & migration ----------

def test_sync_logs_listed_and_migration_keeps_legacy(isolated_db):
    src = _make_source("S7", {"items": [_item("d", "日志内容Theta")]})
    client.post(f"/api/data-sources/{src['id']}/sync")
    logs = client.get(f"/api/data-sources/{src['id']}/sync-logs").json()
    assert len(logs) == 1 and logs[0]["status"] == "success" and logs[0]["created"] == 1

    # legacy data remains intact across re-init
    db = _conn(isolated_db)
    db.execute("INSERT OR REPLACE INTO profile(id,display_name,goals,preferences,updated_at) "
               "VALUES(1,'老用户','g','p',CURRENT_TIMESTAMP)")
    db.execute("INSERT INTO memories(content,category,source,confidence,confirmed,created_at,updated_at) "
               "VALUES('旧记忆','general','manual',1.0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    db.commit()
    db.close()
    init_db()
    db = _conn(isolated_db)
    assert db.execute("SELECT display_name FROM profile WHERE id=1").fetchone()[0] == "老用户"
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0] == 1
    db.close()
