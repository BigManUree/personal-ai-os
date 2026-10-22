"""003-work-crud: Work 页的写操作覆盖，以及后端「全量替换」契约的回归锚点。

The frontend cannot be executed from pytest, so the source assertions below pin
the request shapes that Playwright later exercises end to end. The two contract
tests document *why* every PATCH must carry a full body: the backend reuses the
POST models and runs a full-column UPDATE (app/main.py:1995-2000).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, init_db

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
WORK_SRC = (ROOT / "frontend" / "src" / "pages" / "Work.vue").read_text(encoding="utf-8")
PROJECT_FORM = (ROOT / "frontend" / "src" / "components" / "ProjectForm.vue").read_text(encoding="utf-8")
GOAL_FORM = (ROOT / "frontend" / "src" / "components" / "GoalForm.vue").read_text(encoding="utf-8")
TASK_FORM = (ROOT / "frontend" / "src" / "components" / "TaskForm.vue").read_text(encoding="utf-8")

CALL_RE = re.compile(r"api\(\s*(['\"`])([^'\"`]+)\1\s*,\s*\{\s*method:\s*'(\w+)'", re.S)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "work_ui.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    return db_path


# ---------- frontend write coverage ----------

def test_work_page_calls_every_write_route():
    found = {(path, method) for _q, path, method in CALL_RE.findall(WORK_SRC)}
    expected = {
        ("/api/projects", "POST"),
        ("/api/projects/${editingId.value}", "PATCH"),
        ("/api/projects/${p.id}", "DELETE"),
        ("/api/projects/${p.id}/events", "POST"),
        ("/api/goals", "POST"),
        ("/api/goals/${editingId.value}", "PATCH"),
        ("/api/goals/${g.id}", "PATCH"),
        ("/api/goals/${g.id}", "DELETE"),
        ("/api/tasks", "POST"),
        ("/api/tasks/${editingId.value}", "PATCH"),
        ("/api/tasks/${t.id}", "PATCH"),
        ("/api/tasks/${t.id}", "DELETE"),
    }
    assert expected <= found, f"缺少写路由调用：{sorted(expected - found)}"


def test_work_page_still_reads_three_collections():
    assert "api('/api/projects')" in WORK_SRC
    assert "api('/api/goals')" in WORK_SRC
    assert "api('/api/tasks')" in WORK_SRC
    assert "Promise.allSettled" in WORK_SRC


def _fn_body(src: str, name: str) -> str:
    match = re.search(rf"function {name}\(.*?\n\}}\n", src, re.S)
    assert match, f"未找到函数 {name}"
    return match.group(0)


def test_body_mappers_cover_every_backend_field():
    fields = {
        "projectBody": ["name", "description", "tech_stack", "repo_url", "status"],
        "goalBody": ["title", "description", "kind", "status", "measure", "target_date", "project_id"],
        "taskBody": ["title", "detail", "status", "priority", "due_date", "project_id", "goal_id"],
    }
    for fn, keys in fields.items():
        body = _fn_body(WORK_SRC, fn)
        missing = [k for k in keys if f"{k}:" not in body]
        assert not missing, f"{fn} 缺少字段 {missing}"
        assert "..." in body or "s." in body


def test_shortcut_actions_go_through_full_body():
    """状态快捷动作不能只发 status —— 必须复用 taskBody/goalBody。"""
    assert "taskBody(t, { status })" in WORK_SRC
    assert "goalBody(g, { status })" in WORK_SRC
    assert "projectBody(p, { status })" not in WORK_SRC  # 项目状态走编辑表单


def test_destructive_actions_explain_side_effects():
    assert "事件会一并删除" in WORK_SRC
    assert "解除目标关联" in WORK_SRC
    assert "确认删除" in WORK_SRC


def test_enum_options_match_backend():
    for status in ["planning", "active", "paused", "completed", "archived"]:
        assert status in PROJECT_FORM
    for kind in ["long_term", "milestone"]:
        assert kind in GOAL_FORM
    for status in ["active", "done", "dropped"]:
        assert status in GOAL_FORM
    for status in ["todo", "doing", "done", "cancelled"]:
        assert status in TASK_FORM
    for priority in ["low", "normal", "high"]:
        assert priority in TASK_FORM
    for event_type in ["progress", "decision", "issue", "learn", "note"]:
        assert event_type in WORK_SRC


def test_work_sources_avoid_unsafe_html():
    combined = "\n".join([WORK_SRC, PROJECT_FORM, GOAL_FORM, TASK_FORM])
    for forbidden in ("v-html", "innerHTML", "onclick="):
        assert forbidden not in combined


# ---------- backend contract (the reason the mappers exist) ----------

def test_partial_task_patch_resets_omitted_fields(isolated_db):
    pid = client.post("/api/projects", json={"name": "P", "description": "保留我"}).json()["id"]
    task = client.post("/api/tasks", json={
        "title": "T", "detail": "关键细节", "priority": "high",
        "due_date": "2026-12-01", "project_id": pid,
    }).json()

    # Only title+status: the backend writes model defaults for everything else.
    body = client.patch(f"/api/tasks/{task['id']}", json={"title": "T", "status": "done"}).json()
    assert body["detail"] == ""
    assert body["due_date"] is None
    assert body["project_id"] is None
    assert body["priority"] == "normal"


def test_full_task_patch_keeps_every_field(isolated_db):
    """Exactly the body `taskBody` builds — status shortcut with all fields kept."""
    pid = client.post("/api/projects", json={"name": "P2"}).json()["id"]
    goal = client.post("/api/goals", json={"title": "G", "project_id": pid}).json()
    task = client.post("/api/tasks", json={
        "title": "T2", "detail": "细节", "priority": "high",
        "due_date": "2026-12-01", "project_id": pid, "goal_id": goal["id"],
    }).json()

    full = {
        "title": task["title"], "detail": task["detail"], "status": "doing",
        "priority": task["priority"], "due_date": task["due_date"],
        "project_id": pid, "goal_id": goal["id"],
    }
    body = client.patch(f"/api/tasks/{task['id']}", json=full).json()
    assert body["status"] == "doing"
    assert body["detail"] == "细节"
    assert body["due_date"] == "2026-12-01"
    assert body["project_id"] == pid
    assert body["goal_id"] == goal["id"]
    assert body["completed_at"] is None


def test_partial_project_patch_clears_description(isolated_db):
    pid = client.post("/api/projects", json={
        "name": "P3", "description": "别丢", "tech_stack": "FastAPI", "repo_url": "https://example.invalid",
    }).json()["id"]
    body = client.patch(f"/api/projects/{pid}", json={"name": "P3", "status": "completed"}).json()
    assert body["status"] == "completed"
    assert body["description"] == "" and body["tech_stack"] == "" and body["repo_url"] == ""

    body = client.patch(f"/api/projects/{pid}", json={
        "name": "P3", "description": "别丢", "tech_stack": "FastAPI",
        "repo_url": "https://example.invalid", "status": "active",
    }).json()
    assert body["description"] == "别丢" and body["tech_stack"] == "FastAPI"
