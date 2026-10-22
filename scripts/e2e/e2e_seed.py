"""Seed a temp database with one pending recommendation for the UI smoke test."""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, ".")

tmp = tempfile.mkdtemp(prefix="paos-ui-")
db_path = os.environ.get("SEED_DB_PATH") or os.path.join(tmp, "ui.db")
os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
os.environ["DATABASE_PATH"] = db_path
os.environ["PAOS_SECRET_KEY_PATH"] = os.path.join(os.path.dirname(os.path.abspath(db_path)), "ui.key")

from app.main import init_db  # noqa: E402

init_db()

now = "2026-10-22T01:00:00+00:00"
expires = "2026-11-21T01:00:00+00:00"
evidence = [{
    "kind": "metric",
    "label": "任务完成率",
    "value": "42%",
    "source_type": "analytics",
    "coverage_status": "ok",
    "detail": "近 7 天",
    "definition_version": "ui-smoke",
}]
conn = sqlite3.connect(db_path)
conn.execute(
    "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
    "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
    "dedup_key,expires_at,created_at,updated_at,reviewed_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (1, "UI 冒烟：优先完成临近截止的任务", "近 7 天有 3 个任务逾期，建议先收敛在办事项。",
     "task", "high", "pending", json.dumps(evidence, ensure_ascii=False), "{}",
     "ok", "减少逾期", "low", "", 0.8, "rule", "ui.smoke_v1", "ui-smoke",
     "ui-smoke-1", expires, now, now, None),
)
conn.commit()
conn.close()
print(db_path)
