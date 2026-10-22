"""Personal AI OS 1.2 iteration 3.5.2: local read-only directory connector."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import connectors
from app.main import app, init_db

client = TestClient(app)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase352_local.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    init_db()
    return db_path


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connector(root: Path, **config):
    return connectors.LocalDirectoryConnector({"root": str(root), **config})


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------- config validation ----------

def test_validate_config_rejects_unsafe_or_invalid_roots(tmp_path):
    bad_cases = [
        {"root": "relative/notes"},                         # relative path
        {"root": str(tmp_path / "missing")},                # does not exist
        {"root": str(tmp_path / "file.txt")},               # file, not dir
        {"root": str(Path.cwd())},                          # project root
        {"root": str(Path(tmp_path).anchor)},               # drive root
        {"root": str(tmp_path), "recursive": "yes"},        # bad type
        {"root": str(tmp_path), "max_depth": -1},
        {"root": str(tmp_path), "max_depth": 13},
        {"root": str(tmp_path), "max_depth": True},
    ]
    for config in bad_cases:
        conn = connectors.LocalDirectoryConnector(config)
        with pytest.raises(connectors.ConnectorConfigError):
            asyncio.run(conn.validate_config())

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    good = connectors.LocalDirectoryConnector({"root": str(tmp_path)})
    asyncio.run(good.validate_config())


def test_registry_exposes_local_dir():
    kinds = {x["kind"] for x in connectors.registry.available()}
    assert "local_dir" in kinds


# ---------- first sync ----------

def test_first_sync_collects_allowed_files_and_ignores_others(tmp_path):
    _write(tmp_path / "a.md", "# A\n\n本地目录同步 Alpha")
    _write(tmp_path / "sub" / "b.txt", "本地目录同步 Beta")
    _write(tmp_path / "c.exe", "binary-ish")
    _write(tmp_path / "d.docx", "not really docx")

    result = asyncio.run(_connector(tmp_path).fetch_changes(None))
    ids = [x.external_id for x in result.items]
    assert ids == ["a.md", "sub/b.txt"]
    assert all(x.title == x.external_id for x in result.items)
    assert result.items[0].metadata["ext"] == ".md"
    assert result.items[1].metadata["ext"] == ".txt"
    assert all(x.content_hash for x in result.items)
    cursor = json.loads(result.next_cursor)
    assert set(cursor["files"]) == {"a.md", "sub/b.txt"}


# ---------- incremental sync ----------

def test_incremental_sync_detects_unchanged_changed_and_deleted(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.txt"
    _write(a, "版本一")
    _write(b, "Beta")

    conn = _connector(tmp_path)
    first = asyncio.run(conn.fetch_changes(None))
    assert [x.external_id for x in first.items] == ["a.md", "b.txt"]

    # Nothing changed: no re-parsed items, but both resources are reported alive.
    second = asyncio.run(conn.fetch_changes(first.next_cursor))
    assert second.items == []
    assert set(second.unchanged_external_ids) == {"a.md", "b.txt"}
    assert second.deleted_external_ids == []

    # Change one file (content + mtime): only that file is re-parsed.
    _write(a, "版本二")
    st = a.stat()
    os.utime(a, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))
    third = asyncio.run(conn.fetch_changes(second.next_cursor))
    assert [x.external_id for x in third.items] == ["a.md"]
    assert third.unchanged_external_ids == ["b.txt"]
    assert any(x.content == "版本二" for x in third.items)

    # Delete one file: it is reported through deleted_external_ids.
    b.unlink()
    fourth = asyncio.run(conn.fetch_changes(third.next_cursor))
    assert fourth.items == []
    assert fourth.unchanged_external_ids == ["a.md"]
    assert fourth.deleted_external_ids == ["b.txt"]


def test_max_depth_and_nonrecursive_modes(tmp_path):
    _write(tmp_path / "top.md", "top")
    _write(tmp_path / "sub" / "nested.md", "nested")

    # non-recursive only sees files directly in the root, ignoring subdirs.
    nonrecursive = asyncio.run(_connector(tmp_path, recursive=False).fetch_changes(None))
    assert [x.external_id for x in nonrecursive.items] == ["top.md"]

    # recursive with max_depth=0 behaves like non-recursive for descent.
    shallow = asyncio.run(_connector(tmp_path, recursive=True, max_depth=0).fetch_changes(None))
    assert [x.external_id for x in shallow.items] == ["top.md"]

    # default depth still reaches the one-level subdirectory (order-independent).
    deep = asyncio.run(_connector(tmp_path).fetch_changes(None))
    assert {x.external_id for x in deep.items} == {"sub/nested.md", "top.md"}


def test_unreadable_or_invalid_files_are_skipped(tmp_path):
    _write(tmp_path / "good.md", "good content")
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")

    result = asyncio.run(_connector(tmp_path).fetch_changes(None))
    assert [x.external_id for x in result.items] == ["good.md"]
    cursor = json.loads(result.next_cursor)
    assert set(cursor["files"]) == {"good.md"}


def test_symlink_escape_is_ignored(tmp_path):
    _write(tmp_path / "real.md", "real")
    outside = tmp_path.parent / "outside-target.md"
    _write(outside, "outside")
    link = tmp_path / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this environment cannot create symlinks")

    result = asyncio.run(_connector(tmp_path).fetch_changes(None))
    assert [x.external_id for x in result.items] == ["real.md"]


# ---------- API end-to-end ----------

def test_local_directory_source_end_to_end(isolated_db, tmp_path):
    root = tmp_path / "notes"
    _write(root / "note.md", "# 笔记\n\n本地目录端到端内容 Kappa")
    _write(root / "todo.txt", "本地目录端到端待办 Lambda")

    r = client.post("/api/data-sources", json={
        "kind": "local_dir",
        "name": "本地笔记",
        "scope": "只读同步临时笔记目录",
        "config": {"root": str(root), "recursive": True, "max_depth": 6},
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    first = client.post(f"/api/data-sources/{sid}/sync").json()
    assert first["status"] == "success"
    assert first["created"] == 2 and first["total"] == 2

    docs = client.get("/api/documents").json()
    names = {d["filename"] for d in docs}
    assert names == {"note.md", "todo.txt"}
    assert all(d["source_type"] == "connector" and d["chunk_count"] >= 1 for d in docs)

    second = client.post(f"/api/data-sources/{sid}/sync").json()
    assert second["status"] == "success"
    assert second["unchanged"] == 2 and second["created"] == 0 and second["updated"] == 0

    # Deleting the source file removes only that resource's document/chunks.
    (root / "todo.txt").unlink()
    third = client.post(f"/api/data-sources/{sid}/sync").json()
    assert third["deleted"] == 1 and third["unchanged"] == 1
    docs_after = client.get("/api/documents").json()
    assert {d["filename"] for d in docs_after} == {"note.md"}

    db = _conn(isolated_db)
    todo_rows = db.execute(
        "SELECT status,document_id FROM source_items WHERE external_id='todo.txt'"
    ).fetchall()
    chunk_left = db.execute(
        "SELECT COUNT(*) FROM document_chunks WHERE content LIKE '%Lambda%'"
    ).fetchone()[0]
    db.close()
    assert len(todo_rows) == 1
    assert todo_rows[0]["status"] == "deleted" and todo_rows[0]["document_id"] is None
    assert chunk_left == 0


def test_local_directory_config_is_stored_without_secrets(isolated_db, tmp_path):
    r = client.post("/api/data-sources", json={
        "kind": "local_dir", "name": "bad", "config": {"root": str(tmp_path), "token": "sk-1234567890abcdef"}
    })
    assert r.status_code == 400
