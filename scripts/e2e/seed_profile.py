"""Seed a temp database with pending/confirmed/rejected profile items.

Usage: SEED_DB_PATH=temp/e2e-profile.db python temp/seed_profile.py
"""
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

now = datetime.now(timezone.utc).isoformat(timespec="seconds")
rows = [
    (1, "skill", "画像候选", "近两周连续在研究 SQLite 并发与租约机制", "pending", 0, 0.4, "model", "模型推测"),
    (2, "goal", "", "在维护一个本地优先的个人 AI OS 项目", "confirmed", 1, 1.0, "manual", ""),
    (3, "preference", "旧偏好", "曾经偏好把所有配置写进 YAML", "rejected", 0, 0.5, "manual", "已被推翻"),
]

conn = sqlite3.connect(db_path)
for item_id, category, title, content, status, stated, confidence, source, detail in rows:
    conn.execute(
        "INSERT INTO profile_items(id,category,title,content,meta,status,is_user_stated,confidence,"
        "source_type,source_id,source_detail,valid_from,valid_until,created_at,updated_at,reviewed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, category, title, content, "{}", status, stated, confidence, source, None, detail,
         None, None, now, now, now if status != "pending" else None),
    )
# A minimal text profile so the summary block renders too.
conn.execute(
    "INSERT OR IGNORE INTO profile(id,display_name,goals,preferences,updated_at) VALUES(?,?,?,?,?)",
    (1, "E2E 用户", "完成个人 AI OS MVP", "偏好像素级验证", now),
)
conn.commit()
conn.close()
print(db_path)
