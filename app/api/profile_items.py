from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import app.main as _root
from app import (
    model_config,
    profile_propose,
)
from app.api.work import _validate_iso_date

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter()

# ===== Personal AI OS 1.3.1 profile items (inserted block begin) =====


class ProfileItemIn(BaseModel):
    category: str
    title: str = Field(default="", max_length=profile_propose.MAX_TITLE)
    content: str = Field(min_length=1, max_length=profile_propose.MAX_CONTENT)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_type: str = Field(default="manual")
    source_id: int | None = None
    source_detail: str = Field(default="", max_length=profile_propose.MAX_SOURCE_DETAIL)
    meta: dict[str, Any] = Field(default_factory=dict)
    valid_from: str | None = None
    valid_until: str | None = None

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("画像内容不能为空")
        return value

    @field_validator("title", "source_detail")
    @classmethod
    def _strip_optional(cls, value: str) -> str:
        return value.strip()


class ProfileItemPatchIn(BaseModel):
    category: str | None = None
    title: str | None = Field(default=None, max_length=profile_propose.MAX_TITLE)
    content: str | None = Field(default=None, min_length=1, max_length=profile_propose.MAX_CONTENT)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_type: str | None = None
    source_detail: str | None = Field(default=None, max_length=profile_propose.MAX_SOURCE_DETAIL)
    meta: dict[str, Any] | None = None
    valid_from: str | None = None
    valid_until: str | None = None

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("画像内容不能为空")
        return value

    @field_validator("title", "source_detail")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()


def _reject_secret(*texts: str) -> None:
    for text in texts:
        if text and profile_propose.looks_like_secret(text):
            raise HTTPException(status_code=400, detail="画像内容疑似包含密码/API Key/令牌等凭据，已拒绝保存。")


def _log_item_revision(
    conn: sqlite3.Connection,
    event: str,
    detail: dict[str, Any] | str = "",
    item_id: int | None = None,
) -> None:
    detail_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO profile_item_revisions(item_id,event,detail,created_at) VALUES(?,?,?,?)",
        (item_id, event, detail_text, _root.now_iso()),
    )


def _serialize_item(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    try:
        item["meta"] = json.loads(item.get("meta") or "{}")
    except (json.JSONDecodeError, TypeError):
        item["meta"] = {}
    item["is_user_stated"] = bool(item.get("is_user_stated"))
    return item


def _validate_manual_fields(category: str, source_type: str) -> tuple[str, str]:
    category = category.strip().lower()
    source_type = source_type.strip().lower()
    if category not in profile_propose.MANUAL_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"非法 category（手动新增不能为 inference），允许：{sorted(profile_propose.MANUAL_CATEGORIES)}",
        )
    allowed_sources = profile_propose.PROFILE_SOURCES - {"model"}
    if source_type not in allowed_sources:
        raise HTTPException(status_code=400, detail=f"非法 source_type，允许：{sorted(allowed_sources)}")
    return category, source_type


@router.get("/api/profile-items")
def list_profile_items(status: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM profile_items WHERE 1=1"
    params: list[Any] = []
    # Rejected items are hidden by default; they stay auditable via status=rejected.
    if status is None or status == "":
        sql += " AND status != 'rejected'"
    else:
        if status not in profile_propose.PROFILE_ITEM_STATUSES:
            raise HTTPException(status_code=400, detail=f"非法 status，允许：{sorted(profile_propose.PROFILE_ITEM_STATUSES)}")
        sql += " AND status = ?"
        params.append(status)
    if category:
        category = category.strip().lower()
        if category not in profile_propose.PROFILE_CATEGORIES:
            raise HTTPException(status_code=400, detail="非法 category")
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'confirmed' THEN 1 ELSE 2 END, updated_at DESC, id DESC"
    with _root.db() as conn:
        return [_serialize_item(r) for r in conn.execute(sql, params).fetchall()]


@router.post("/api/profile-items")
def create_profile_item(body: ProfileItemIn) -> dict[str, Any]:
    category, source_type = _validate_manual_fields(body.category, body.source_type)
    valid_from = _validate_iso_date(body.valid_from, "valid_from")
    valid_until = _validate_iso_date(body.valid_until, "valid_until")
    _reject_secret(body.content, body.title, body.source_detail)
    meta_text = json.dumps(body.meta, ensure_ascii=False)
    if len(meta_text) > profile_propose.MAX_META:
        raise HTTPException(status_code=422, detail="meta 信息过长")
    timestamp = _root.now_iso()
    with _root.db() as conn:
        cur = conn.execute(
            "INSERT INTO profile_items(category,title,content,meta,status,is_user_stated,confidence,"
            "source_type,source_id,source_detail,valid_from,valid_until,created_at,updated_at,reviewed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (category, body.title, body.content, meta_text, "confirmed", 1, body.confidence,
             source_type, body.source_id, body.source_detail, valid_from, valid_until,
             timestamp, timestamp, timestamp),
        )
        new_id = cur.lastrowid
        _log_item_revision(conn, "create", {"category": category, "source_type": source_type}, item_id=new_id)
        return _serialize_item(conn.execute("SELECT * FROM profile_items WHERE id=?", (new_id,)).fetchone())


@router.patch("/api/profile-items/{item_id}")
def update_profile_item(item_id: int, body: ProfileItemPatchIn) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if body.category is not None:
        category = body.category.strip().lower()
        if category not in profile_propose.MANUAL_CATEGORIES:
            raise HTTPException(status_code=400, detail="非法 category（不能把条目改成 inference）")
        fields["category"] = category
    if body.title is not None:
        fields["title"] = body.title
    if body.content is not None:
        fields["content"] = body.content
    if body.confidence is not None:
        fields["confidence"] = body.confidence
    if body.source_type is not None:
        source_type = body.source_type.strip().lower()
        allowed_sources = profile_propose.PROFILE_SOURCES - {"model"}
        if source_type not in allowed_sources:
            raise HTTPException(status_code=400, detail="非法 source_type")
        fields["source_type"] = source_type
    if body.source_detail is not None:
        fields["source_detail"] = body.source_detail
    if body.meta is not None:
        meta_text = json.dumps(body.meta, ensure_ascii=False)
        if len(meta_text) > profile_propose.MAX_META:
            raise HTTPException(status_code=422, detail="meta 信息过长")
        fields["meta"] = meta_text
    # Date fields need a way to be *cleared*: an explicit null clears the value
    # while omitting the key leaves it alone. `is not None` cannot express that
    # difference, so this follows the outcome endpoint's model_fields_set pattern.
    if "valid_from" in body.model_fields_set:
        fields["valid_from"] = _validate_iso_date(body.valid_from, "valid_from")
    if "valid_until" in body.model_fields_set:
        fields["valid_until"] = _validate_iso_date(body.valid_until, "valid_until")

    _reject_secret(fields.get("content", ""), fields.get("title", ""), fields.get("source_detail", ""))
    if not fields:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    with _root.db() as conn:
        row = conn.execute("SELECT * FROM profile_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="画像条目不存在")
        if row["status"] == "rejected":
            raise HTTPException(status_code=409, detail="已拒绝的条目不能编辑，请重新新增")
        columns = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [_root.now_iso(), item_id]
        conn.execute(f"UPDATE profile_items SET {columns}, updated_at=? WHERE id=?", values)
        _log_item_revision(conn, "update", {"fields": sorted(fields)}, item_id=item_id)
        return _serialize_item(conn.execute("SELECT * FROM profile_items WHERE id=?", (item_id,)).fetchone())


def _transition_profile_item(item_id: int, expected: str, new_status: str, event: str, set_expiry: bool = False) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM profile_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="画像条目不存在")
        if row["status"] != expected:
            raise HTTPException(status_code=409, detail=f"当前状态为 {row['status']}，无法执行该操作（需要 {expected}）")
        timestamp = _root.now_iso()
        if set_expiry:
            today = datetime.now(timezone.utc).date().isoformat()
            conn.execute(
                "UPDATE profile_items SET status=?, valid_until=COALESCE(valid_until,?), reviewed_at=?, updated_at=? WHERE id=?",
                (new_status, today, timestamp, timestamp, item_id),
            )
        elif new_status == "confirmed":
            # A guess only becomes a fact through this explicit user action.
            conn.execute(
                "UPDATE profile_items SET status=?, is_user_stated=1, reviewed_at=?, updated_at=? WHERE id=?",
                (new_status, timestamp, timestamp, item_id),
            )
        else:
            conn.execute(
                "UPDATE profile_items SET status=?, reviewed_at=?, updated_at=? WHERE id=?",
                (new_status, timestamp, timestamp, item_id),
            )
        _log_item_revision(conn, event, f"{expected} -> {new_status}", item_id=item_id)
        return _serialize_item(conn.execute("SELECT * FROM profile_items WHERE id=?", (item_id,)).fetchone())


@router.post("/api/profile-items/{item_id}/confirm")
def confirm_profile_item(item_id: int) -> dict[str, Any]:
    return _transition_profile_item(item_id, "pending", "confirmed", "confirm")


@router.post("/api/profile-items/{item_id}/reject")
def reject_profile_item(item_id: int) -> dict[str, Any]:
    return _transition_profile_item(item_id, "pending", "rejected", "reject")


@router.post("/api/profile-items/{item_id}/expire")
def expire_profile_item(item_id: int) -> dict[str, Any]:
    return _transition_profile_item(item_id, "confirmed", "expired", "expire", set_expiry=True)


@router.delete("/api/profile-items/{item_id}")
def delete_profile_item(item_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        row = conn.execute("SELECT content FROM profile_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="画像条目不存在")
        # Keep a deletion trace before the cascading revision rows disappear.
        _log_item_revision(conn, "delete", f"画像条目已物理删除：{row['content'][:200]}", item_id=None)
        conn.execute("DELETE FROM profile_items WHERE id=?", (item_id,))
    return {"deleted": True}


@router.get("/api/profile-items/{item_id}/history")
def profile_item_history(item_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM profile_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="画像条目不存在")
        revisions = [dict(r) for r in conn.execute(
            "SELECT id,item_id,event,detail,created_at FROM profile_item_revisions WHERE item_id=? ORDER BY id",
            (item_id,),
        ).fetchall()]
        return {"item": _serialize_item(row), "revisions": revisions}


@router.post("/api/profile-items/propose")
async def propose_profile_items_route() -> dict[str, Any]:
    if not model_config.load_model_config().enabled:
        raise HTTPException(status_code=400, detail="未配置 LLM_API_KEY（本地演示模式不生成画像推测）")
    with _root.db() as conn:
        profile_row = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
        profile = dict(profile_row) if profile_row else {}
        memories = [dict(r) for r in conn.execute(
            "SELECT id,content,memory_type FROM memories "
            "WHERE COALESCE(status,'confirmed')='confirmed' ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()]
        goals = [dict(r) for r in conn.execute(
            "SELECT id,title,kind FROM goals WHERE status='active' ORDER BY id DESC LIMIT 15"
        ).fetchall()]

    try:
        proposals = await profile_propose.propose_profile_items(
            {"profile": profile, "memories": memories, "goals": goals}
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"画像生成失败：{type(exc).__name__}") from exc

    timestamp = _root.now_iso()
    inserted: list[dict[str, Any]] = []
    with _root.db() as conn:
        batch_keys: set[str] = set()
        for proposal in proposals:
            content = proposal["content"]
            key = content.casefold()
            if key in batch_keys:
                continue
            dup = conn.execute(
                "SELECT 1 FROM profile_items WHERE content=? AND status!='rejected' LIMIT 1", (content,)
            ).fetchone()
            if dup:
                continue
            batch_keys.add(key)
            cur = conn.execute(
                "INSERT INTO profile_items(category,title,content,meta,status,is_user_stated,confidence,"
                "source_type,source_id,source_detail,valid_from,valid_until,created_at,updated_at,reviewed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal["category"], proposal.get("title", ""), content, "{}", "pending", 0,
                 proposal["confidence"], "model", None, proposal.get("evidence", ""),
                 None, None, timestamp, timestamp, None),
            )
            new_id = cur.lastrowid
            _log_item_revision(
                conn, "propose",
                {"category": proposal["category"], "evidence": proposal.get("evidence", "")},
                item_id=new_id,
            )
            inserted.append(_serialize_item(conn.execute("SELECT * FROM profile_items WHERE id=?", (new_id,)).fetchone()))
    return {"proposed": len(inserted), "items": inserted}
# ===== Personal AI OS 1.3.1 profile items (inserted block end) =====


# ===== Personal AI OS 1.2 iteration 3.5.1 connector framework (inserted block begin)

DATA_SOURCE_STATUSES = {"active", "paused", "revoked"}



