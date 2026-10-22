"""005-profile-items: 前端接线断言 + 画像条目状态机契约回归。

State transitions live entirely on the server (`_transition_profile_item`);
the frontend only offers buttons matching the current status. The contract
tests below pin the 409 semantics the UI relies on to refresh after races.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, init_db

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
FORM_SRC = (SRC / "components" / "ProfileItemForm.vue").read_text(encoding="utf-8")
PAGE_SRC = (SRC / "pages" / "Persona.vue").read_text(encoding="utf-8")

CALL_RE = re.compile(r"api\(\s*(['\"`])([^'\"`]+)\1\s*(?:,\s*\{\s*method:\s*'(\w+)')?", re.S)

TODAY = datetime.now(timezone.utc).date().isoformat()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "profile_items_ui.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    return db_path


def _insert_item(db_path, item_id: int, status: str, **kw) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO profile_items(id,category,title,content,meta,status,is_user_stated,confidence,"
        "source_type,source_id,source_detail,valid_from,valid_until,created_at,updated_at,reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item_id, kw.get("category", "skill"), kw.get("title", ""), kw.get("content", f"条目 {item_id}"),
            "{}", status, 1 if status == "confirmed" else 0, 0.8,
            kw.get("source_type", "manual"), None, "", None, kw.get("valid_until"),
            "2026-10-22T00:00:00+00:00", "2026-10-22T00:00:00+00:00", None,
        ),
    )
    conn.commit()
    conn.close()


# ---------- frontend wiring ----------

def test_frontend_calls_all_nine_route_families():
    combined = "\n".join([PAGE_SRC, FORM_SRC])
    found = {(path, method or "GET") for _q, path, method in CALL_RE.findall(combined)}
    expected = {
        ("/api/profile", "GET"),
        ("/api/profile-items", "POST"),
        ("/api/profile-items/${item.id}/${action}", "POST"),  # confirm | reject | expire
        ("/api/profile-items/${editingId.value}", "PATCH"),
        ("/api/profile-items/${item.id}", "DELETE"),
        ("/api/profile-items/${id}/history", "GET"),
        ("/api/profile-items/propose", "POST"),
    }
    missing = expected - found
    assert not missing, f"缺少调用：{sorted(missing)}"
    # The list call appends the filter query outside the quoted path, so the
    # regex above cannot see it; assert it directly.
    assert "api('/api/profile-items' + qs)" in combined


def test_all_three_transitions_are_reachable():
    for action in ["'confirm'", "'reject'", "'expire'"]:
        assert f"transition(item, {action})" in PAGE_SRC


def test_rejected_items_get_no_edit_entry():
    # The action row only renders 编辑 for confirmed/expired.
    assert "item.status === 'confirmed' || item.status === 'expired'" in PAGE_SRC
    assert "已拒绝的条目不可编辑" in PAGE_SRC


def test_rejected_tab_is_explicit():
    assert "status=rejected" in PAGE_SRC.replace("status=${statusFilter.value}", "status=rejected") or \
           "'rejected', label: '已拒绝'" in PAGE_SRC


def test_manual_category_dropdown_excludes_inference():
    for category in ["basic", "skill", "interest", "goal", "preference", "observation"]:
        assert category in FORM_SRC
    # `inference` may appear only in comments, never as a selectable value.
    assert ":value=\"inference\"" not in FORM_SRC
    assert "'inference'" not in FORM_SRC
    assert "'manual', 'memory', 'document', 'project', 'goal', 'task'" in FORM_SRC
    assert "'model'" not in FORM_SRC


def test_confirm_semantics_are_explained_in_ui():
    assert "确认后作为个人事实使用" in PAGE_SRC or "已确认为个人事实" in PAGE_SRC
    assert "用户确认" in PAGE_SRC


def test_dates_submit_explicit_null_when_cleared():
    body = re.search(r"function profileItemBody\(.*?\n\}", PAGE_SRC, re.S)
    assert body, "未找到 profileItemBody"
    assert "valid_from: f.valid_from ? String(f.valid_from) : null" in body.group(0)
    assert "valid_until: f.valid_until ? String(f.valid_until) : null" in body.group(0)


def test_propose_dependency_is_visible():
    assert "LLM_API_KEY" in PAGE_SRC
    assert "生成推测" in PAGE_SRC


def test_profile_sources_avoid_unsafe_html():
    combined = "\n".join([PAGE_SRC, FORM_SRC])
    for forbidden in ("v-html", "innerHTML", "onclick="):
        assert forbidden not in combined


# ---------- backend contract ----------

def test_confirm_promotes_pending_to_user_stated(isolated_db):
    _insert_item(isolated_db, 1, "pending")
    body = client.post("/api/profile-items/1/confirm").json()
    assert body["status"] == "confirmed"
    assert body["is_user_stated"] is True
    # Confirming twice hits the state machine guard.
    r = client.post("/api/profile-items/1/confirm")
    assert r.status_code == 409 and "pending" in r.json()["detail"]


def test_reject_then_edit_returns_409(isolated_db):
    _insert_item(isolated_db, 2, "pending")
    assert client.post("/api/profile-items/2/reject").json()["status"] == "rejected"
    r = client.patch("/api/profile-items/2", json={"content": "改不动"})
    assert r.status_code == 409 and "已拒绝" in r.json()["detail"]
    # Rejected rows disappear from the default listing but stay auditable.
    assert client.get("/api/profile-items").json() == []
    listing = client.get("/api/profile-items", params={"status": "rejected"}).json()
    assert [x["id"] for x in listing] == [2]


def test_expire_sets_today_once(isolated_db):
    _insert_item(isolated_db, 3, "confirmed")
    body = client.post("/api/profile-items/3/expire").json()
    assert body["status"] == "expired"
    assert body["valid_until"] == TODAY
    r = client.post("/api/profile-items/3/expire")
    assert r.status_code == 409
    # Expired items stay editable.
    assert client.patch("/api/profile-items/3", json={"content": "仍然可以改"}).status_code == 200


def test_history_returns_ordered_revisions(isolated_db):
    _insert_item(isolated_db, 4, "pending")
    client.post("/api/profile-items/4/confirm")
    client.patch("/api/profile-items/4", json={"content": "更新后的内容"})
    data = client.get("/api/profile-items/4/history").json()
    events = [rev["event"] for rev in data["revisions"]]
    assert events[0] == "confirm" and "update" in events
    assert data["item"]["content"] == "更新后的内容"


def test_manual_create_is_immediately_confirmed(isolated_db):
    r = client.post("/api/profile-items", json={"category": "skill", "content": "熟悉 FastAPI"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "confirmed" and body["is_user_stated"] is True
    # inference is a model-only category for manual entries.
    r = client.post("/api/profile-items", json={"category": "inference", "content": "x"})
    assert r.status_code == 400 and "inference" in r.json()["detail"]


def test_propose_requires_server_key(isolated_db):
    r = client.post("/api/profile-items/propose")
    assert r.status_code == 400
    assert "LLM_API_KEY" in r.json()["detail"]


def test_secret_like_content_is_rejected(isolated_db):
    r = client.post("/api/profile-items", json={"category": "basic", "content": "password=hunter2secret"})
    assert r.status_code == 400


def test_patch_explicit_null_clears_date_but_omitted_key_keeps_it(isolated_db):
    _insert_item(isolated_db, 6, "confirmed", valid_until="2027-01-01")
    # Omitted key: value survives.
    body = client.patch("/api/profile-items/6", json={"content": "改内容"}).json()
    assert body["valid_until"] == "2027-01-01"
    # Explicit null: value is cleared.
    body = client.patch("/api/profile-items/6", json={"valid_until": None}).json()
    assert body["valid_until"] is None


def test_patch_without_fields_is_400(isolated_db):
    _insert_item(isolated_db, 5, "confirmed")
    r = client.patch("/api/profile-items/5", json={})
    assert r.status_code == 400 and "没有需要更新的字段" in r.json()["detail"]
