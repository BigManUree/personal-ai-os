"""007-connector-credential: 前端接线断言 + 凭据端点契约回归。

The credential store is write-only: plaintext goes in once, ciphertext comes
out never, and the list API only exposes a boolean. These tests pin the
encryption round-trip, the last_error reset, and the UI's write-only discipline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import secret_crypto
from app.main import app, init_db

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
PAGE_SRC = (SRC / "pages" / "Connectors.vue").read_text(encoding="utf-8")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "credential.db"
    key_path = tmp_path / "secret.key"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.setenv("PAOS_SECRET_KEY_PATH", str(key_path))
    secret_crypto.reset_cipher_cache()
    init_db()
    yield db_path
    secret_crypto.reset_cipher_cache()


def _create_source(**overrides) -> dict:
    # mock validates without a credential, so the PUT/DELETE contract tests do
    # not depend on GitHub's "token required" gate (which is the very UX this
    # feature closes).
    body = {"kind": "mock", "name": "测试源", "config": {"items": [{"title": "t", "content": "c"}]}}
    body.update(overrides)
    response = client.post("/api/data-sources", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _credential_row(db_path, source_id: int):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT ciphertext FROM connector_credentials WHERE data_source_id=?", (source_id,)
        ).fetchone()
    finally:
        conn.close()


# ------------------------------------------------------- backend contract

def test_put_credential_stores_ciphertext_without_plaintext(isolated_db):
    source = _create_source()
    assert source["has_credential"] is False
    secret = "github_pat_SUPER_SECRET_1234567890"

    body = client.put(f"/api/data-sources/{source['id']}/credential",
                      json={"secret": secret}).json()
    assert body["has_credential"] is True
    assert secret not in body.get("config_json", "")

    row = _credential_row(isolated_db, source["id"])
    assert row is not None
    ciphertext = row[0]
    assert secret not in ciphertext
    # Round-trip through the real decrypt path proves it is a real Fernet blob
    # for this instance's key, not opaque junk.
    assert secret_crypto.decrypt_secret(ciphertext) == secret


def test_put_credential_clears_last_error_and_replaces_single_row(isolated_db):
    source = _create_source()
    conn = sqlite3.connect(isolated_db)
    conn.execute("UPDATE data_sources SET last_error='旧错误' WHERE id=?", (source["id"],))
    conn.commit()
    conn.close()

    first = client.put(f"/api/data-sources/{source['id']}/credential",
                       json={"secret": "token-one"}).json()
    assert first["last_error"] is None
    second = client.put(f"/api/data-sources/{source['id']}/credential",
                        json={"secret": "token-two"}).json()
    assert second["has_credential"] is True

    conn = sqlite3.connect(isolated_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM connector_credentials WHERE data_source_id=?", (source["id"],)
    ).fetchone()[0]
    conn.close()
    assert count == 1, "重复 PUT 必须替换单行而不是追加"


def test_delete_credential_round_trip(isolated_db):
    source = _create_source()
    client.put(f"/api/data-sources/{source['id']}/credential", json={"secret": "token"})
    assert _credential_row(isolated_db, source["id"]) is not None

    assert client.delete(f"/api/data-sources/{source['id']}/credential").json() == {"deleted": True}
    assert _credential_row(isolated_db, source["id"]) is None

    listed = client.get("/api/data-sources").json()
    assert listed[0]["has_credential"] is False


def test_credential_endpoints_reject_missing_source_and_bad_secrets(isolated_db):
    assert client.put("/api/data-sources/9999/credential", json={"secret": "x"}).status_code == 404
    assert client.delete("/api/data-sources/9999/credential").status_code == 404

    source = _create_source()
    blank = client.put(f"/api/data-sources/{source['id']}/credential", json={"secret": "   "})
    assert blank.status_code == 422
    too_long = client.put(f"/api/data-sources/{source['id']}/credential",
                          json={"secret": "x" * 4001})
    assert too_long.status_code == 422


def test_plaintext_never_appears_in_list_or_detail(isolated_db):
    secret = "github_pat_LEAK_ME_000111222"
    source = _create_source(secret=secret)  # create-time credential path
    assert source["has_credential"] is True

    listed = client.get("/api/data-sources").json()
    detail = client.get(f"/api/data-sources/{source['id']}").json()
    dumped = repr(listed) + repr(detail)
    assert secret not in dumped
    assert "ciphertext" not in dumped


def test_create_with_secret_persists_encrypted_row(isolated_db):
    source = _create_source(secret="create-time-pat")
    assert source["has_credential"] is True
    row = _credential_row(isolated_db, source["id"])
    assert row is not None
    assert "create-time-pat" not in row[0]


# -------------------------------------------------------- frontend wiring

def test_page_wires_both_credential_endpoints():
    assert "method: 'PUT', body: JSON.stringify({ secret })" in PAGE_SRC
    assert "method: 'DELETE'" in PAGE_SRC
    assert "/credential`" in PAGE_SRC or "/credential'," in PAGE_SRC


def test_page_shows_credential_badge_from_has_credential():
    assert 'data-field="cred-badge"' in PAGE_SRC
    assert "s.has_credential ? '凭据已配置' : '凭据未配置'" in PAGE_SRC


def test_credential_input_is_write_only():
    # password type, no value echo, cleared after submit
    assert PAGE_SRC.count('type="password"') == 2  # create form + panel
    assert "credSecret.value = ''" in PAGE_SRC
    assert "fPat.value = ''" in PAGE_SRC
    assert ':value="credSecret"' not in PAGE_SRC
    assert ':value="fPat"' not in PAGE_SRC


def test_pat_field_is_create_only():
    create_block = PAGE_SRC[PAGE_SRC.index('v-if="formKind === '):]
    assert 'v-if="!editingId"' in create_block
    # The PATCH branch must not send a secret.
    patch_block = PAGE_SRC[PAGE_SRC.index("if (editingId.value) {"):]
    patch_block = patch_block[:patch_block.index("} else {")]
    # Strip comments: the explanatory note mentions the secret field by name.
    code_only = "\n".join(line.split("//")[0] for line in patch_block.splitlines())
    assert "secret" not in code_only


def test_page_has_no_inline_html_injection():
    assert "v-html" not in PAGE_SRC
    assert "innerHTML" not in PAGE_SRC
    assert "onclick=" not in PAGE_SRC


def test_form_controls_have_stable_names():
    for name in ("kind", "name", "root", "owner", "repo", "branch", "url", "mock_items", "secret"):
        assert f'name="{name}"' in PAGE_SRC, name
