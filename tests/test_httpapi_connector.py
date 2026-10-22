"""Personal AI OS 1.2 iteration 3.5.3 batch 1: generic HTTP/JSON connector.

Fully offline: HTTP is served by httpx.MockTransport and DNS is stubbed via
httpapi_connector.resolve_ips. No real network and no LLM calls.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import connectors, httpapi_connector, secret_crypto
from app.main import app, init_db

client = TestClient(app)

PUBLIC_URL = "https://notes.example.com/api/items"
TOKEN = "ghp_exampletoken1234567890abcd"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "phase353_http.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("PAOS_SECRET_KEY_PATH", str(tmp_path / "secret.key"))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    secret_crypto.reset_cipher_cache()
    init_db()
    init_db()
    return db_path


@pytest.fixture
def fake_http(monkeypatch):
    state = {"entries": [
        {"id": "n1", "title": "笔记一", "content": "HTTP 同步内容 Alpha", "updated_at": "2026-10-01T00:00:00Z",
         "url": "https://notes.example.com/n1"},
        {"id": "n2", "title": "笔记二", "content": "HTTP 同步内容 Beta", "updated_at": "2026-10-02T00:00:00Z"},
    ], "status": 200, "force": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["force"] == "timeout":
            raise httpx.ReadTimeout("simulated", request=request)
        if state["force"] == "redirect":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/meta"})
        if state["force"] == "huge":
            return httpx.Response(200, content=b"x" * (httpapi_connector.MAX_RESPONSE_BYTES + 1))
        if state["force"] == "nonjson":
            return httpx.Response(200, text="<html>not json</html>")
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"message": "err"})
        # Record auth header for assertions
        state["last_auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=state["entries"])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpapi_connector, "build_transport", lambda: transport)

    # Public example.com -> a global IP; everything else private by default.
    def fake_resolve(host):
        import ipaddress as ip
        if host.endswith("example.com"):
            return [ip.ip_address("93.184.216.34")]
        if host == "localhost":
            return [ip.ip_address("127.0.0.1")]
        return [ip.ip_address("10.0.0.5")]

    monkeypatch.setattr(httpapi_connector, "resolve_ips", fake_resolve)
    return state


def _connector(config=None, secret=""):
    base = {"url": PUBLIC_URL}
    if config:
        base.update(config)
    return httpapi_connector.HttpApiConnector(base, secret=secret)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


# --------------------------------------------------------------------------- #
# config validation & SSRF
# --------------------------------------------------------------------------- #

def test_config_rejects_bad_urls_and_ssrf(fake_http):
    cases = [
        {"url": "ftp://example.com/x"},
        {"url": "not a url at all"},
        {"url": ""},
        {"url": "http://10.0.0.5/api"},                        # private literal IP, http
        {"url": "https://10.0.0.5/api"},                       # private literal IP
        {"url": "http://169.254.169.254/latest/meta-data"},    # cloud metadata
        {"url": "https://192.168.1.10/x"},                     # private range
        {"url": "http://127.0.0.1:9000/x"},                    # loopback not allowed by default
        {"url": "https://intranet.example.org/x"},             # resolves private
        {"url": PUBLIC_URL, "fields": {"external_id": "key"}}, # missing content mapping
        {"url": PUBLIC_URL, "auth_header_name": "bad name!"},
    ]
    for cfg in cases:
        with pytest.raises(connectors.ConnectorConfigError):
            asyncio.run(_connector(cfg).validate_config())


def test_loopback_only_with_explicit_flag(fake_http):
    # https public endpoint works
    asyncio.run(_connector().validate_config())
    # localhost http works only with allow_localhost
    with pytest.raises(connectors.ConnectorConfigError):
        asyncio.run(_connector({"url": "http://localhost:9000/items"}).validate_config())
    asyncio.run(_connector({"url": "http://localhost:9000/items", "allow_localhost": True}).validate_config())


def test_dns_failure_is_temporary(fake_http, monkeypatch):
    def boom(host):
        raise connectors.ConnectorFetchError("域名解析失败：x")
    monkeypatch.setattr(httpapi_connector, "resolve_ips", boom)
    with pytest.raises(connectors.ConnectorFetchError):
        asyncio.run(_connector({"url": "https://nope.example.com/x"}).validate_config())


# --------------------------------------------------------------------------- #
# auth / status mapping
# --------------------------------------------------------------------------- #

def test_credential_sent_and_401_is_auth_error(fake_http):
    asyncio.run(_connector(secret=TOKEN).validate_config())
    assert fake_http["last_auth"] == f"Bearer {TOKEN}"
    fake_http["status"] = 401
    with pytest.raises(connectors.ConnectorAuthError):
        asyncio.run(_connector(secret=TOKEN).fetch_changes(None))


def test_429_5xx_redirect_huge_nonjson_are_temporary(fake_http):
    for force, status in [(None, 429), (None, 503)]:
        fake_http["status"] = status
        with pytest.raises(connectors.ConnectorFetchError):
            asyncio.run(_connector().fetch_changes(None))
    fake_http["status"] = 200
    for force in ["redirect", "huge", "nonjson", "timeout"]:
        fake_http["force"] = force
        with pytest.raises(connectors.ConnectorFetchError):
            asyncio.run(_connector().fetch_changes(None))
        fake_http["force"] = None


def test_validate_404_is_config_error(fake_http):
    fake_http["status"] = 404
    with pytest.raises(connectors.ConnectorConfigError):
        asyncio.run(_connector(secret=TOKEN).validate_config())


# --------------------------------------------------------------------------- #
# sync behaviour
# --------------------------------------------------------------------------- #

def test_first_and_incremental_sync(fake_http):
    c = _connector()
    first = asyncio.run(c.fetch_changes(None))
    assert {i.external_id for i in first.items} == {"n1", "n2"}
    n1 = next(i for i in first.items if i.external_id == "n1")
    assert n1.title == "笔记一" and n1.source_url.endswith("/n1")
    assert TOKEN not in json.dumps([i.model_dump() for i in first.items])

    second = asyncio.run(c.fetch_changes(first.next_cursor))
    assert second.items == []
    assert set(second.unchanged_external_ids) == {"n1", "n2"}
    assert second.deleted_external_ids == []

    # change one entry
    fake_http["entries"][0]["content"] = "HTTP 同步内容 Alpha-v2"
    third = asyncio.run(c.fetch_changes(second.next_cursor))
    assert [i.external_id for i in third.items] == ["n1"]
    assert third.unchanged_external_ids == ["n2"]

    # remove one entry
    fake_http["entries"].pop(1)
    fourth = asyncio.run(c.fetch_changes(third.next_cursor))
    assert fourth.deleted_external_ids == ["n2"]
    assert fourth.unchanged_external_ids == ["n1"]


def test_items_path_and_field_mapping(fake_http):
    fake_http["entries"] = {"data": {"items": [
        {"key": "x1", "body": "嵌套内容 Kappa", "headline": "标题X"},
        {"key": "", "body": "缺 id，跳过"},
        {"key": "x2", "body": ""},
        "not-a-dict",
    ]}}

    def handler(request):
        return httpx.Response(200, json=fake_http["entries"])
    import app.httpapi_connector as h
    old = h.build_transport
    h.build_transport = lambda: httpx.MockTransport(handler)
    try:
        c = _connector({"items_path": "data.items",
                        "fields": {"external_id": "key", "content": "body", "title": "headline"}})
        result = asyncio.run(c.fetch_changes(None))
        assert [i.external_id for i in result.items] == ["x1"]
        assert result.items[0].title == "标题X"
    finally:
        h.build_transport = old


def test_item_count_cap(fake_http):
    fake_http["entries"] = [{"id": f"i{k}", "content": "c"} for k in range(250)]
    result = asyncio.run(_connector().fetch_changes(None))
    assert len(result.items) == httpapi_connector.MAX_ITEMS_PER_SYNC


# --------------------------------------------------------------------------- #
# end-to-end API
# --------------------------------------------------------------------------- #

def test_http_api_source_end_to_end(isolated_db, fake_http):
    r = client.post("/api/data-sources", json={
        "kind": "http_api", "name": "http", "scope": "只读 example 接口",
        "config": {"url": PUBLIC_URL}, "secret": TOKEN,
    })
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["has_credential"] is True

    first = client.post(f"/api/data-sources/{sid}/sync").json()
    assert first["status"] == "success" and first["created"] == 2, first

    db = _conn(isolated_db)
    hit = db.execute(
        "SELECT COUNT(*) AS n FROM document_chunks WHERE content LIKE '%HTTP 同步内容 Alpha%'"
    ).fetchone()["n"]
    db.close()
    assert hit >= 1

    second = client.post(f"/api/data-sources/{sid}/sync").json()
    assert second["unchanged"] == 2 and second["created"] == 0

    # token never persisted in config_json/logs
    db = _conn(isolated_db)
    row = db.execute("SELECT config_json FROM data_sources WHERE id=?", (sid,)).fetchone()
    logs = " ".join(r["error"] or "" for r in db.execute("SELECT error FROM sync_logs").fetchall())
    db.close()
    assert TOKEN not in row["config_json"] and TOKEN not in logs

    # public endpoint without credential also works
    r2 = client.post("/api/data-sources", json={
        "kind": "http_api", "name": "http-public", "config": {"url": PUBLIC_URL},
    })
    assert r2.status_code == 200, r2.text

    assert client.delete(f"/api/data-sources/{sid}").json() == {"deleted": True}
