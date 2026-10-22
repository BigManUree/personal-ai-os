"""006-snooze-and-comparison: 前端接线断言 + 提醒/对比接口契约回归。

The reminder action set is defined once on the server (``app.reminders.TRANSITIONS``).
The page keeps its own copy only to decide which buttons render, so a parity test
compares the two tables directly: a drift makes the UI offer an action the state
machine rejects (409) or hide one it accepts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import reminders
from app.main import app, init_db

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "frontend" / "src"
REMINDERS_SRC = (SRC / "pages" / "Reminders.vue").read_text(encoding="utf-8")
ANALYTICS_SRC = (SRC / "pages" / "Analytics.vue").read_text(encoding="utf-8")


def _iso(days: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "snooze_compare.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setattr("app.main.DB_PATH", db_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    init_db()
    return db_path


def _frontend_actions() -> dict[str, list[str]]:
    start = REMINDERS_SRC.index("const ACTIONS_BY_STATUS")
    body = REMINDERS_SRC[start:REMINDERS_SRC.index("\n}", start)]
    found: dict[str, list[str]] = {}
    for status, actions in re.findall(r"(\w+):\s*\[([^\]]*)\]", body):
        found[status] = re.findall(r"'(\w+)'", actions)
    return found


def _insert_reminder(db_path, reminder_id: int, status: str, *,
                     snoozed_until: str | None = None) -> None:
    now = _iso()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO reminders(id,rule_code,rule_version,source_type,title,content,priority,"
            "status,evidence_json,coverage_status,dedup_key,scheduled_at,snoozed_until,expires_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reminder_id, "review.daily_due_v1", "v1", "review", f"提醒 {reminder_id}", "内容", "normal",
                status, json.dumps([], ensure_ascii=False), "ok", f"test-{reminder_id}",
                now, snoozed_until, _iso(30), now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------- action parity

def _server_actions() -> dict[str, list[str]]:
    """Invert TRANSITIONS (action -> allowed statuses) into status -> actions."""
    by_status: dict[str, list[str]] = {}
    for action, statuses in reminders.TRANSITIONS.items():
        if action == "mark_due":
            continue  # 调度器内部动作，人工界面不提供
        for status in statuses:
            by_status.setdefault(status, []).append(action)
    return by_status


# The page offers a curated subset: `expire` is scheduler-owned, `mark_due` never
# appears in the UI, and re-acknowledging an acknowledged reminder is a no-op
# that would only confuse the button row.
HIDDEN_ACTIONS = {
    ("due", "expire"),
    ("snoozed", "expire"),
    ("scheduled", "expire"),
    ("snoozed", "snooze"),
    ("acknowledged", "acknowledge"),
}


def test_frontend_actions_are_allowed_by_server_transitions():
    server = _server_actions()
    frontend = _frontend_actions()
    for status, actions in frontend.items():
        assert status in server, f"{status} 在服务端不可达"
        for action in actions:
            assert action in server[status], f"{status} 不允许 {action}，会得到 409"
    assert {status: set(actions) for status, actions in frontend.items()} == {
        status: set(server.get(status, [])) - {action for st, action in HIDDEN_ACTIONS if st == status}
        for status in server
        if status not in reminders.TERMINAL_STATUSES
    }


def test_terminal_statuses_have_no_actions():
    frontend = _frontend_actions()
    for status in reminders.TERMINAL_STATUSES:
        assert status not in frontend, f"终态 {status} 不应提供人工动作"


def test_snooze_is_only_offered_from_due():
    frontend = _frontend_actions()
    assert "snooze" in frontend["due"]
    for status, actions in frontend.items():
        if status != "due":
            assert "snooze" not in actions, f"{status} 不应出现延期按钮"


# --------------------------------------------------------------- snooze wiring

def test_page_sends_explicit_snooze_minutes():
    assert "{ action: 'snooze', snooze_minutes: minutes }" in REMINDERS_SRC
    assert "const MAX_SNOOZE_MINUTES = 10080" in REMINDERS_SRC
    assert "1..${MAX_SNOOZE_MINUTES}" in REMINDERS_SRC


def test_snooze_presets_are_inside_the_server_range():
    minutes = [int(m) for m in re.findall(r"minutes:\s*(\d+)", REMINDERS_SRC)]
    assert minutes, "预设必须存在"
    assert all(1 <= m <= reminders.MAX_SNOOZE_MINUTES for m in minutes), minutes


def test_page_renders_snoozed_until():
    assert 'data-field="snoozed-until"' in REMINDERS_SRC
    assert "fmtTime(r.snoozed_until)" in REMINDERS_SRC
    assert "return '—'" in REMINDERS_SRC  # 缺失时间不留空


def test_page_shows_server_detail_and_refreshes_on_rejection():
    assert "cardError.value = { ...cardError.value, [id]: e.message }" in REMINDERS_SRC
    assert "await load()" in REMINDERS_SRC


def test_page_has_no_inline_html_injection():
    for source, name in ((REMINDERS_SRC, "Reminders.vue"), (ANALYTICS_SRC, "Analytics.vue")):
        assert "v-html" not in source, name
        assert "innerHTML" not in source, name
        assert "onclick=" not in source, name


# ------------------------------------------------------- reminder API contract

def test_snooze_moves_status_and_sets_wake_time(isolated_db):
    _insert_reminder(isolated_db, 1, "due")
    before = client.get("/api/reminders?status=due").json()
    assert [r["id"] for r in before] == [1]
    assert before[0]["snoozed_until"] is None

    body = client.post("/api/reminders/1/transition",
                       json={"action": "snooze", "snooze_minutes": 60}).json()
    assert body["status"] == "snoozed"
    assert body["snoozed_until"] is not None

    listed = client.get("/api/reminders?status=snoozed").json()
    assert [r["id"] for r in listed] == [1]
    assert listed[0]["snoozed_until"] == body["snoozed_until"]


def test_snooze_rejects_out_of_range_minutes(isolated_db):
    _insert_reminder(isolated_db, 2, "due")
    for bad in (0, reminders.MAX_SNOOZE_MINUTES + 1, "30", True):
        response = client.post("/api/reminders/2/transition",
                               json={"action": "snooze", "snooze_minutes": bad})
        assert response.status_code == 400, bad
        assert "延期时长" in response.json()["detail"]


def test_snooze_from_terminal_status_is_409(isolated_db):
    _insert_reminder(isolated_db, 3, "completed")
    response = client.post("/api/reminders/3/transition",
                           json={"action": "snooze", "snooze_minutes": 30})
    assert response.status_code == 409
    assert response.json()["detail"]


def test_cancel_requires_a_known_reason(isolated_db):
    _insert_reminder(isolated_db, 4, "due")
    bad = client.post("/api/reminders/4/transition",
                      json={"action": "cancel", "cancel_reason": "because"})
    assert bad.status_code == 400
    ok = client.post("/api/reminders/4/transition",
                     json={"action": "cancel", "cancel_reason": "user_cancelled"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "cancelled"


# --------------------------------------------------------- comparison contract

def test_comparison_only_accepts_equal_seven_or_thirty_day_windows(isolated_db):
    for days in (7, 30):
        assert client.get(f"/api/analytics/comparison?window_days={days}").status_code == 200
    rejected = client.get("/api/analytics/comparison?window_days=3")
    assert rejected.status_code == 400
    assert "7" in rejected.json()["detail"] and "30" in rejected.json()["detail"]


def test_comparison_reports_null_percent_when_previous_window_is_empty(isolated_db):
    body = client.get("/api/analytics/comparison?window_days=7").json()
    assert body["window_days"] == 7
    assert body["current"]["start"] and body["previous"]["end"]
    assert body["metrics"], "指标集合不应为空"
    for name, metric in body["metrics"].items():
        assert metric["previous"]["value"] == 0
        assert metric["current"]["value"] == 0
        assert metric["percent_change"] is None, name
        assert metric["direction"] == "flat"
        assert metric["status"] == "no_data"


def test_frontend_requests_only_supported_windows():
    assert "v-for=\"d in [7, 30]\"" in ANALYTICS_SRC
    assert "window_days=${days}" in ANALYTICS_SRC


def test_frontend_renders_dash_for_null_percent_change():
    assert "if (value === null || value === undefined) return '—'" in ANALYTICS_SRC
    # A zero percent must stay distinguishable from "undefined".
    assert "return '—'" in ANALYTICS_SRC
    assert "(n * 100).toFixed(1)" in ANALYTICS_SRC


def test_frontend_shows_comparison_windows_and_definition_version():
    assert 'data-field="current-range"' in ANALYTICS_SRC
    assert 'data-field="previous-range"' in ANALYTICS_SRC
    assert "definition_version" in ANALYTICS_SRC


def test_frontend_keeps_previous_table_when_window_is_rejected():
    assert "compareError.value = e.message" in ANALYTICS_SRC
    # The rejected branch must not reset comparison.value to null.
    reject_block = ANALYTICS_SRC[ANALYTICS_SRC.index("catch (e: any) {"):]
    assert "comparison.value = null" not in reject_block.split("finally")[0]
