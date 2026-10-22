"""Seed a temp database with one accepted + one pending recommendation.

Usage: SEED_DB_PATH=temp/e2e-rec.db python temp/seed_insights.py
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

db_path = os.environ["SEED_DB_PATH"]
os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
os.environ["DATABASE_PATH"] = db_path
os.environ["PAOS_SECRET_KEY_PATH"] = os.path.join(os.path.dirname(os.path.abspath(db_path)), "e2e.key")

from app.main import init_db  # noqa: E402

init_db()

now_dt = datetime.now(timezone.utc)
now = now_dt.isoformat(timespec="seconds")
expires = (now_dt + timedelta(days=30)).isoformat(timespec="seconds")

evidence = [{
    "kind": "metric",
    "label": "任务完成率",
    "value": "42%",
    "source_type": "analytics",
    "coverage_status": "ok",
    "detail": "近 7 天",
    "definition_version": "ui-smoke",
}]

rows = [
    (1, "UI 冒烟：优先完成临近截止的任务", "近 7 天有 3 个任务逾期，建议先收敛在办事项。", "accepted"),
    (2, "UI 冒烟：下午固定两小时深度工作", "近 14 天下午碎片化明显。", "pending"),
]

conn = sqlite3.connect(db_path)
for rec_id, title, content, status in rows:
    conn.execute(
        "INSERT INTO recommendations(id,title,content,category,priority,status,evidence_json,window_json,"
        "coverage_status,expected_benefit,risk_level,risk_note,confidence,source,rule_code,generation_version,"
        "dedup_key,expires_at,created_at,updated_at,reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec_id, title, content, "task", "high", status, json.dumps(evidence, ensure_ascii=False),
         "{}", "ok", "减少逾期", "low", "", 0.8, "rule", "ui.smoke_v1", "ui-smoke",
         f"ui-smoke-{rec_id}", expires, now, now, now if status == "accepted" else None),
    )
conn.commit()
conn.close()
print(db_path)
