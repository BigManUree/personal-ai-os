"""Personal AI OS 1.2 iteration 3.5.2 batch 2: read-only GitHub connector.

Fully offline: GitHub HTTP is injected via httpx.MockTransport by patching
app.github_connector.build_transport. No real network and no LLM calls.
"""

from __future__ import annotations

import base64
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connectors, github_connector, secret_crypto
from app.main import app, init_db

client = TestClient(app)

TOKEN = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"
OWNER, REPO = "octo", "demo"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase352_github.db"
    key_path = tmp_path / "secret.key"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PAOS_SECRET_KEY_PATH", str(key_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    secret_crypto.reset_cipher_cache()
    init_db()
    init_db()
    return db_path


@pytest.fixture
def fake_github(monkeypatch):
    """In-memory GitHub API. Returns the patcher so tests can tweak state."""

    state = {
        "default_branch": "main",
        "files": {"README.md": "# Demo\n\nhello github sync"},
        "commits": [],
        "status_overrides": {},   # url-substring -> status code
        "auth_required": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        # Token must be presented on every API call.
        auth = request.headers.get("authorization", "")
        if state["auth_required"] and auth != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        url = str(request.url)
        for needle, code in state["status_overrides"].items():
            if needle in url:
                if code == "timeout":
                    raise httpx.ReadTimeout("simulated", request=request)
                return httpx.Response(code, json={"message": f"forced {code}"})

        if url.endswith(f"/repos/{OWNER}/{REPO}"):
            return httpx.Response(200, json={"default_branch": state["default_branch"]})
        if "/git/trees/" in url:
            tree = [
                {"path": path, "type": "blob", "sha": f"sha-{i}-{len(data)}"}
                for i, (path, data) in enumerate(sorted(state["files"].items()))
            ]
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            if path not in state["files"]:
                return httpx.Response(404, json={"message": "Not Found"})
            data = state["files"][path]
            raw = data if isinstance(data, bytes) else data.encode("utf-8")
            return httpx.Response(200, json={
                "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
            })
        if "/commits" in url:
            since = None
            if "since=" in url:
                from urllib.parse import parse_qs, urlparse
                since = parse_qs(urlparse(url).query).get("since", [None])[0]
            commits = state["commits"]
            if since:
                commits = [c for c in commits if (c["commit"]["author"].get("date") or "") > since]
            return httpx.Response(200, json=commits)
        return httpx.Response(404, json={"message": "unhandled"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(github_connector, "build_transport", lambda: transport)
    return state


def _connector(config=None, secret=TOKEN):
    base = {"owner": OWNER, "repo": REPO, "include": ["readme", "docs"]}
    if config:
        base.update(config)
    return github_connector.GitHubConnector(base, secret=secret)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


# --------------------------------------------------------------------------- #
# secret crypto
# --------------------------------------------------------------------------- #

def test_secret_crypto_roundtrip_and_ciphertext(tmp_path, monkeypatch):
    monkeypatch.setenv("PAOS_SECRET_KEY_PATH", str(tmp_path / "k.key"))
    secret_crypto.reset_cipher_cache()
    cipher = secret_crypto.encrypt_secret("ghp_1234567890abcdef1234")
    assert "ghp_" not in cipher and cipher != "ghp_1234567890abcdef1234"
    assert secret_crypto.decrypt_secret(cipher) == "ghp_1234567890abcdef1234"
    with pytest.raises(secret_crypto.SecretCryptoError):
        secret_crypto.encrypt_secret("   ")


def test_secret_crypto_rejects_wrong_key(tmp_path, monkeypatch):
    monkeypatch.setenv("PAOS_SECRET_KEY_PATH", str(tmp_path / "a.key"))
    secret_crypto.reset_cipher_cache()
    cipher = secret_crypto.encrypt_secret("tokentokentoken")
    monkeypatch.setenv("PAOS_SECRET_KEY_PATH", str(tmp_path / "b.key"))
    secret_crypto.reset_cipher_cache()
    with pytest.raises(secret_crypto.SecretCryptoError):
        secret_crypto.decrypt_secret(cipher)


# --------------------------------------------------------------------------- #
# config validation
# --------------------------------------------------------------------------- #

def test_validate_config_errors(fake_github):
    asyncio_bad = [
        ({}, TOKEN, connectors.ConnectorConfigError),                 # missing owner/repo
        ({"owner": "bad owner!", "repo": REPO}, TOKEN, connectors.ConnectorConfigError),
        ({"owner": OWNER, "repo": REPO, "include": ["wiki"]}, TOKEN, connectors.ConnectorConfigError),
        ({"owner": OWNER, "repo": REPO, "commits_limit": 0}, TOKEN, connectors.ConnectorConfigError),
        ({"owner": OWNER, "repo": REPO}, "", connectors.ConnectorConfigError),  # no token
    ]
    import asyncio
    for cfg, secret, exc in asyncio_bad:
        with pytest.raises(exc):
            asyncio.run(github_connector.GitHubConnector(cfg, secret=secret).validate_config())


def test_validate_config_404_is_config_error(fake_github):
    import asyncio
    fake_github["status_overrides"][f"/repos/{OWNER}/{REPO}"] = 404
    with pytest.raises(connectors.ConnectorConfigError):
        asyncio.run(_connector().validate_config())


def test_validate_config_401_is_auth_error(fake_github):
    import asyncio
    with pytest.raises(connectors.ConnectorAuthError):
        asyncio.run(_connector(secret="github_pat_WRONG_WRONG_WRONG_WRONG_WRONG00").validate_config())


def test_validate_config_rate_limit_is_temporary(fake_github):
    import asyncio

    def handler(request):
        return httpx.Response(403, json={"message": "rate"}, headers={"x-ratelimit-remaining": "0"})

    import httpx as _h
    transport = _h.MockTransport(handler)
    import app.github_connector as gc
    old = gc.build_transport
    gc.build_transport = lambda: transport
    try:
        with pytest.raises(connectors.ConnectorFetchError):
            asyncio.run(_connector().validate_config())
    finally:
        gc.build_transport = old


def test_validate_config_timeout_is_temporary(fake_github):
    import asyncio
    fake_github["status_overrides"][f"/repos/{OWNER}/{REPO}"] = "timeout"
    with pytest.raises(connectors.ConnectorFetchError):
        asyncio.run(_connector().validate_config())


# --------------------------------------------------------------------------- #
# sync behaviour
# --------------------------------------------------------------------------- #

def test_first_sync_readme_and_docs(fake_github):
    import asyncio
    fake_github["files"] = {
        "README.md": "# Demo\n\n首次同步内容 Alpha",
        "docs/guide.txt": "指南正文 Beta",
        "docs/spec.md": "规格说明 Gamma",
        "misc/ignore.md": "不应被同步",
        "docs/image.png": "binary ignore",
    }
    result = asyncio.run(_connector().fetch_changes(None))
    ids = {i.external_id for i in result.items}
    assert ids == {"README.md", "docs/guide.txt", "docs/spec.md"}
    readme = next(i for i in result.items if i.external_id == "README.md")
    assert readme.source_url == f"https://github.com/{OWNER}/{REPO}/blob/main/README.md"
    assert readme.metadata["sha"].startswith("sha-")
    assert TOKEN not in json.dumps([i.model_dump() for i in result.items])
    cursor = json.loads(result.next_cursor)
    assert set(cursor["files"]) == ids
    assert result.unchanged_external_ids == []


def test_incremental_sync_unchanged_changed_deleted(fake_github):
    import asyncio
    fake_github["files"] = {"README.md": "v1", "docs/a.md": "A"}
    c = _connector()
    first = asyncio.run(c.fetch_changes(None))
    assert {i.external_id for i in first.items} == {"README.md", "docs/a.md"}

    # nothing changed
    second = asyncio.run(c.fetch_changes(first.next_cursor))
    assert second.items == []
    assert set(second.unchanged_external_ids) == {"README.md", "docs/a.md"}
    assert second.deleted_external_ids == []

    # change one: blob sha derives from length, so alter content length
    fake_github["files"]["README.md"] = "v2-changed"
    third = asyncio.run(c.fetch_changes(second.next_cursor))
    assert [i.external_id for i in third.items] == ["README.md"]
    assert third.unchanged_external_ids == ["docs/a.md"]
    assert any(i.content == "v2-changed" for i in third.items)

    # delete one
    del fake_github["files"]["docs/a.md"]
    fourth = asyncio.run(c.fetch_changes(third.next_cursor))
    assert fourth.deleted_external_ids == ["docs/a.md"]
    assert fourth.unchanged_external_ids == ["README.md"]


def test_commits_incremental(fake_github):
    import asyncio
    fake_github["files"] = {"README.md": "r"}
    fake_github["commits"] = [
        {"sha": "a" * 40, "commit": {"message": "first commit", "author": {"name": "n", "date": "2026-10-01T00:00:00Z"}}},
    ]
    c = _connector({"include": ["readme", "commits"], "commits_limit": 10})
    first = asyncio.run(c.fetch_changes(None))
    ids = {i.external_id for i in first.items}
    assert "__commits__/recent.md" in ids
    commit_item = next(i for i in first.items if i.external_id == "__commits__/recent.md")
    assert "first commit" in commit_item.content
    cursor = json.loads(first.next_cursor)
    assert cursor["last_commit_date"] == "2026-10-01T00:00:00Z"

    # no new commits -> reported unchanged, not recreated
    second = asyncio.run(c.fetch_changes(first.next_cursor))
    assert all(i.external_id != "__commits__/recent.md" for i in second.items)
    assert "__commits__/recent.md" in second.unchanged_external_ids


def test_fetch_network_error_keeps_cursor(fake_github):
    import asyncio
    fake_github["files"] = {"README.md": "x"}
    first = asyncio.run(_connector().fetch_changes(None))
    fake_github["status_overrides"]["/git/trees/"] = 500
    with pytest.raises(connectors.ConnectorFetchError):
        asyncio.run(_connector().fetch_changes(first.next_cursor))


def test_token_never_leaks_in_error(fake_github):
    import asyncio
    c = _connector(secret=TOKEN)
    fake_github["status_overrides"][f"/repos/{OWNER}/{REPO}"] = 500
    with pytest.raises(connectors.ConnectorFetchError):
        asyncio.run(c.validate_config())
    # redaction masks a token if it ever appeared in a message
    assert connectors.redact_secrets(f"boom {TOKEN}") == "boom [REDACTED]"


# --------------------------------------------------------------------------- #
# credential API + end-to-end
# --------------------------------------------------------------------------- #

def test_credential_api_store_list_update_delete(isolated_db, fake_github):
    r = client.post("/api/data-sources", json={
        "kind": "github", "name": "gh", "scope": "指定仓库只读",
        "config": {"owner": OWNER, "repo": REPO},
    })
    assert r.status_code == 400 and "凭据" in r.json()["detail"]  # validate needs token

    # create with credential -> validate_config hits fake github -> success
    r = client.post("/api/data-sources", json={
        "kind": "github", "name": "gh", "scope": "指定仓库只读",
        "config": {"owner": OWNER, "repo": REPO}, "secret": TOKEN,
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["has_credential"] is True
    assert "secret" not in r.json()

    # DB stores ciphertext only
    db = _conn(isolated_db)
    row = db.execute("SELECT ciphertext FROM connector_credentials WHERE data_source_id=?", (sid,)).fetchone()
    db.close()
    assert row is not None and TOKEN not in row["ciphertext"]

    # config carrying a token-shaped string is rejected
    r2 = client.post("/api/data-sources", json={
        "kind": "github", "name": "gh2",
        "config": {"owner": OWNER, "repo": REPO, "note": TOKEN},
    })
    assert r2.status_code == 400

    # list/detail expose only the boolean
    listed = [x for x in client.get("/api/data-sources").json() if x["id"] == sid][0]
    assert listed["has_credential"] is True
    assert client.get(f"/api/data-sources/{sid}").json()["has_credential"] is True

    # replace then delete credential
    assert client.put(f"/api/data-sources/{sid}/credential", json={"secret": "github_pat_NEW_NEW_NEW_NEW_NEW00"}).status_code == 200
    assert client.delete(f"/api/data-sources/{sid}/credential").json() == {"deleted": True}
    listed = [x for x in client.get("/api/data-sources").json() if x["id"] == sid][0]
    assert listed["has_credential"] is False

    # sync without credential fails safely (config error -> generic 409 sync path)
    r = client.post(f"/api/data-sources/{sid}/sync")
    assert r.json()["status"] == "failed"


def test_github_source_end_to_end_sync_and_revoke(isolated_db, fake_github):
    fake_github["files"] = {"README.md": "# Demo\n\n端到端 GitHub 内容 Delta"}
    r = client.post("/api/data-sources", json={
        "kind": "github", "name": "gh-e2e", "scope": "只读 octo/demo",
        "config": {"owner": OWNER, "repo": REPO}, "secret": TOKEN,
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]

    first = client.post(f"/api/data-sources/{sid}/sync").json()
    assert first["status"] == "success" and first["created"] == 1, first
    docs = client.get("/api/documents").json()
    assert any(d["filename"] == "README.md" and d["source_type"] == "connector" for d in docs)
    db = _conn(isolated_db)
    hit = db.execute(
        "SELECT COUNT(*) AS n FROM document_chunks WHERE content LIKE '%端到端 GitHub 内容 Delta%'"
    ).fetchone()["n"]
    db.close()
    assert hit >= 1

    second = client.post(f"/api/data-sources/{sid}/sync").json()
    assert second["unchanged"] == 1 and second["created"] == 0

    # revoke forbids further syncing but keeps content
    client.post(f"/api/data-sources/{sid}/revoke")
    r = client.post(f"/api/data-sources/{sid}/sync")
    assert r.status_code == 409

    # deleting the source cascades credentials away
    assert client.delete(f"/api/data-sources/{sid}").json() == {"deleted": True}
    db = _conn(isolated_db)
    n = db.execute("SELECT COUNT(*) AS n FROM connector_credentials WHERE data_source_id=?", (sid,)).fetchone()["n"]
    db.close()
    assert n == 0
