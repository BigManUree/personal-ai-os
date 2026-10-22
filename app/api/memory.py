from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import app.main as _root
from app import (
    memory_extract,
    memory_match,
    model_config,
)

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter(tags=["记忆与画像"])

MEMORY_TYPES = {"fact", "preference", "goal", "experience", "pattern", "other"}
SOURCE_TYPES = {"conversation", "manual", "import"}
CANDIDATE_STATUSES = {"pending", "confirmed", "rejected", "merged"}
CONFLICT_RESOLUTIONS = {"replace_new", "keep_both", "keep_old"}

from app.api.chat import _recent_messages, _store_candidates  # noqa: E402  # noqa: E402


class ProfileIn(BaseModel):
    display_name: str = Field(default="我", max_length=100)
    goals: str = Field(default="", max_length=5000)
    preferences: str = Field(default="", max_length=5000)


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    category: str = Field(default="general", max_length=50)
    source: str = Field(default="manual", max_length=100)
    confidence: float = Field(default=1.0, ge=0, le=1)
    confirmed: bool = True
    memory_type: str = Field(default="fact")
    source_type: str = Field(default="manual")
    source_id: int | None = None



class ExtractIn(BaseModel):
    conversation_id: int | None = None


class ApproveIn(BaseModel):
    resolution: str | None = None


class CandidateEditIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    memory_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)


def _log_revision(
    conn: sqlite3.Connection,
    event: str,
    detail: dict[str, Any] | str = "",
    memory_id: int | None = None,
    candidate_id: int | None = None,
) -> None:
    detail_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO memory_revisions(memory_id,candidate_id,event,detail,created_at) VALUES(?,?,?,?,?)",
        (memory_id, candidate_id, event, detail_text, _root.now_iso()),
    )



@router.get("/api/profile")
def get_profile() -> dict[str, Any]:
    with _root.db() as conn:
        return dict(conn.execute("SELECT * FROM profile WHERE id = 1").fetchone())


@router.put("/api/profile")
def update_profile(body: ProfileIn) -> dict[str, Any]:
    with _root.db() as conn:
        conn.execute("UPDATE profile SET display_name=?, goals=?, preferences=?, updated_at=? WHERE id=1",
                      (body.display_name, body.goals, body.preferences, _root.now_iso()))
        return dict(conn.execute("SELECT * FROM profile WHERE id=1").fetchone())


@router.get("/api/memories")
def list_memories() -> list[dict[str, Any]]:
    with _root.db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()]


def _validate_memory_fields(body: MemoryIn) -> tuple[str, str]:
    memory_type = body.memory_type.strip().lower()
    source_type = body.source_type.strip().lower()
    if memory_type not in MEMORY_TYPES:
        raise HTTPException(status_code=400, detail=f"非法 memory_type，允许：{sorted(MEMORY_TYPES)}")
    if source_type not in SOURCE_TYPES:
        raise HTTPException(status_code=400, detail=f"非法 source_type，允许：{sorted(SOURCE_TYPES)}")
    return memory_type, source_type


@router.post("/api/memories")
def create_memory(body: MemoryIn) -> dict[str, Any]:
    memory_type, source_type = _validate_memory_fields(body)
    timestamp = _root.now_iso()
    status = "confirmed" if body.confirmed else "pending"
    with _root.db() as conn:
        cur = conn.execute(
            "INSERT INTO memories(content,category,source,confidence,confirmed,created_at,updated_at,"
            "memory_type,source_type,source_id,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (body.content.strip(), body.category, body.source, body.confidence, int(body.confirmed), timestamp, timestamp,
             memory_type, source_type, body.source_id, status),
        )
        new_id = cur.lastrowid
        _log_revision(conn, "manual_create", {"content": body.content.strip(), "source_type": source_type}, memory_id=new_id)
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (new_id,)).fetchone())


@router.patch("/api/memories/{memory_id}")
def update_memory(memory_id: int, body: MemoryIn) -> dict[str, Any]:
    memory_type, source_type = _validate_memory_fields(body)
    status = "confirmed" if body.confirmed else "pending"
    with _root.db() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记忆不存在")
        conn.execute(
            "UPDATE memories SET content=?,category=?,source=?,confidence=?,confirmed=?,updated_at=?,"
            "memory_type=?,source_type=?,source_id=?,status=? WHERE id=?",
            (body.content.strip(), body.category, body.source, body.confidence, int(body.confirmed), _root.now_iso(),
             memory_type, source_type, body.source_id, status, memory_id),
        )
        _log_revision(conn, "manual_update", {"content": body.content.strip(), "status": status}, memory_id=memory_id)
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())


@router.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        row = conn.execute("SELECT content FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记忆不存在")
        _log_revision(conn, "deleted", f"记忆已物理删除：{row['content'][:200]}", memory_id=None, candidate_id=None)
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return {"deleted": True}


@router.post("/api/memories/{memory_id}/expire")
def expire_memory(memory_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记忆不存在")
        if row["status"] != "confirmed":
            raise HTTPException(status_code=409, detail=f"仅 confirmed 记忆可标记过期，当前状态：{row['status']}")
        timestamp = _root.now_iso()
        conn.execute(
            "UPDATE memories SET status='expired', confirmed=0, valid_until=?, updated_at=? WHERE id=?",
            (timestamp, timestamp, memory_id),
        )
        _log_revision(conn, "expired", "由用户标记过期", memory_id=memory_id)
        return dict(conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())


@router.get("/api/memories/{memory_id}/history")
def memory_history(memory_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记忆不存在")
        revisions = [dict(r) for r in conn.execute(
            "SELECT id,candidate_id,event,detail,created_at FROM memory_revisions WHERE memory_id=? ORDER BY id",
            (memory_id,),
        ).fetchall()]
        return {"memory": dict(row), "revisions": revisions}


@router.get("/api/memory-candidates")
def list_memory_candidates(status: str | None = None) -> list[dict[str, Any]]:
    if status is not None:
        status = status.strip().lower()
        if status not in CANDIDATE_STATUSES:
            raise HTTPException(status_code=400, detail=f"非法 status，允许：{sorted(CANDIDATE_STATUSES)}")
    with _root.db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM memory_candidates WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM memory_candidates ORDER BY id DESC").fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if item.get("conflict_with"):
                target = conn.execute("SELECT id,content FROM memories WHERE id=?", (item["conflict_with"],)).fetchone()
                item["conflict_memory"] = dict(target) if target else None
            if item.get("duplicate_of"):
                target = conn.execute("SELECT id,content FROM memories WHERE id=?", (item["duplicate_of"],)).fetchone()
                item["duplicate_memory"] = dict(target) if target else None
            result.append(item)
        return result


@router.patch("/api/memory-candidates/{candidate_id}")
def edit_memory_candidate(candidate_id: int, body: CandidateEditIn) -> dict[str, Any]:
    memory_type = body.memory_type.strip().lower()
    if memory_type not in MEMORY_TYPES:
        raise HTTPException(status_code=400, detail=f"非法 memory_type，允许：{sorted(MEMORY_TYPES)}")
    content = body.content.strip()
    reason = body.reason.strip()
    if not content or not reason:
        raise HTTPException(status_code=400, detail="内容和原因不能为空")
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记忆候选不存在")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="仅待审核候选可以编辑")
        confirmed_rows = [dict(r) for r in conn.execute(
            "SELECT id,content,memory_type FROM memories WHERE COALESCE(status,'confirmed')='confirmed'"
        ).fetchall()]
        pending_rows = [dict(r) for r in conn.execute(
            "SELECT id,content,memory_type FROM memory_candidates WHERE status='pending' AND id<>?",
            (candidate_id,),
        ).fetchall()]
        match = memory_match.match_candidate(content, memory_type, confirmed_rows, pending_rows)
        conn.execute(
            "UPDATE memory_candidates SET content=?,memory_type=?,confidence=?,reason=?,"
            "duplicate_of=?,conflict_with=? WHERE id=?",
            (content, memory_type, body.confidence, reason, match.duplicate_of, match.conflict_with, candidate_id),
        )
        return dict(conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone())


def _insert_memory_from_candidate(conn: sqlite3.Connection, cand: sqlite3.Row, supersedes_id: int | None = None) -> int:
    timestamp = _root.now_iso()
    cur = conn.execute(
        "INSERT INTO memories(content,category,source,confidence,confirmed,created_at,updated_at,"
        "memory_type,source_type,source_id,status,supersedes_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cand["content"], cand["memory_type"], cand["source_type"], cand["confidence"], 1, timestamp, timestamp,
         cand["memory_type"], cand["source_type"], cand["source_id"], "confirmed", supersedes_id),
    )
    return cur.lastrowid


@router.post("/api/memory-candidates/{candidate_id}/approve")
def approve_memory_candidate(candidate_id: int, body: ApproveIn | None = None) -> dict[str, Any]:
    resolution = (body.resolution.strip().lower() if body and body.resolution else None)
    if resolution is not None and resolution not in CONFLICT_RESOLUTIONS:
        raise HTTPException(status_code=400, detail=f"非法 resolution，允许：{sorted(CONFLICT_RESOLUTIONS)}")
    with _root.db() as conn:
        cand = conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()
        if cand is None:
            raise HTTPException(status_code=404, detail="记忆候选不存在")
        if cand["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"该候选已处理（{cand['status']}），不能重复审核")

        # --- Conflict path: requires an explicit human decision ---
        if cand["conflict_with"]:
            old = conn.execute("SELECT * FROM memories WHERE id=?", (cand["conflict_with"],)).fetchone()
            if old is None or old["status"] != "confirmed":
                conn.execute("UPDATE memory_candidates SET conflict_with=NULL WHERE id=?", (candidate_id,))
            elif resolution is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "该候选与已确认记忆冲突，必须显式选择处理方式",
                        "candidate_id": candidate_id,
                        "new_content": cand["content"],
                        "old_memory_id": old["id"],
                        "old_content": old["content"],
                        "resolutions": sorted(CONFLICT_RESOLUTIONS),
                    },
                )
            else:
                timestamp = _root.now_iso()
                if resolution == "keep_old":
                    conn.execute("UPDATE memory_candidates SET status='rejected',reviewed_at=? WHERE id=?",
                                 (timestamp, candidate_id))
                    _log_revision(conn, "candidate_rejected_conflict",
                                  {"new_content": cand["content"], "kept_memory_id": old["id"]},
                                  memory_id=old["id"], candidate_id=candidate_id)
                    return {"candidate_id": candidate_id, "resolution": "keep_old", "status": "rejected",
                            "kept_memory_id": old["id"]}

                supersedes_id = None
                if resolution == "replace_new":
                    conn.execute(
                        "UPDATE memories SET status='expired', confirmed=0, valid_until=?, updated_at=? WHERE id=?",
                        (timestamp, timestamp, old["id"]),
                    )
                    _log_revision(conn, "superseded",
                                  {"replaced_by_candidate": candidate_id},
                                  memory_id=old["id"], candidate_id=candidate_id)
                    supersedes_id = old["id"]

                new_id = _insert_memory_from_candidate(conn, cand, supersedes_id=supersedes_id)
                _log_revision(conn, "created_from_candidate",
                              {"resolution": resolution, "candidate_id": candidate_id,
                               "old_memory_id": old["id"], "supersedes_id": supersedes_id},
                              memory_id=new_id, candidate_id=candidate_id)
                conn.execute("UPDATE memory_candidates SET status='confirmed',reviewed_at=? WHERE id=?",
                             (timestamp, candidate_id))
                return {"candidate_id": candidate_id, "resolution": resolution, "status": "confirmed",
                        "memory_id": new_id, "old_memory_id": old["id"]}

        # --- Duplicate path: merge into the existing memory, never copy ---
        if cand["duplicate_of"]:
            target = conn.execute("SELECT * FROM memories WHERE id=?", (cand["duplicate_of"],)).fetchone()
            if target is not None and target["status"] == "confirmed":
                timestamp = _root.now_iso()
                conn.execute("UPDATE memory_candidates SET status='merged',reviewed_at=? WHERE id=?",
                             (timestamp, candidate_id))
                _log_revision(conn, "duplicate_merged",
                              {"candidate_id": candidate_id, "candidate_content": cand["content"][:200]},
                              memory_id=target["id"], candidate_id=candidate_id)
                return {"candidate_id": candidate_id, "resolution": "merged", "status": "merged",
                        "merged_into_memory_id": target["id"]}
            conn.execute("UPDATE memory_candidates SET duplicate_of=NULL WHERE id=?", (candidate_id,))

        # --- Plain confirmation ---
        new_id = _insert_memory_from_candidate(conn, cand)
        timestamp = _root.now_iso()
        conn.execute("UPDATE memory_candidates SET status='confirmed',reviewed_at=? WHERE id=?",
                     (timestamp, candidate_id))
        _log_revision(conn, "created_from_candidate",
                      {"resolution": "new", "candidate_id": candidate_id},
                      memory_id=new_id, candidate_id=candidate_id)
        return {"candidate_id": candidate_id, "resolution": "new", "status": "confirmed", "memory_id": new_id}


@router.post("/api/memory-candidates/{candidate_id}/reject")
def reject_memory_candidate(candidate_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM memory_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记忆候选不存在")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"该候选已处理（{row['status']}），不能重复审核")
        conn.execute("UPDATE memory_candidates SET status='rejected',reviewed_at=? WHERE id=?", (_root.now_iso(), candidate_id))
        _log_revision(conn, "candidate_rejected", {"content": row["content"][:200]}, candidate_id=candidate_id)
        return {"candidate_id": candidate_id, "status": "rejected"}


@router.get("/api/memory-insights")
def memory_insights() -> dict[str, Any]:
    with _root.db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        counts = {
            status: conn.execute("SELECT COUNT(*) FROM memory_candidates WHERE status=?", (status,)).fetchone()[0]
            for status in CANDIDATE_STATUSES
        }
        confirmed_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE COALESCE(status,'confirmed')='confirmed'"
        ).fetchone()[0]
        expired = conn.execute("SELECT COUNT(*) FROM memories WHERE status='expired'").fetchone()[0]
    reviewed = counts["confirmed"] + counts["rejected"] + counts["merged"]
    acceptance_rate = round(counts["confirmed"] / reviewed, 3) if reviewed else None
    return {
        "candidates_total": total,
        "candidates_by_status": counts,
        "reviewed": reviewed,
        "acceptance_rate": acceptance_rate,
        "memories_confirmed": confirmed_memories,
        "memories_expired": expired,
        "note": "acceptance_rate = confirmed / (confirmed+rejected+merged)；词法去重/冲突为保守提示，人工裁决结果才是最终事实。",
    }


# ── Reminder HTTP API (read-only list + manual transition; scheduler stays off by default) ──

@router.post("/api/memory-candidates/extract")
async def extract_memory_candidates(body: ExtractIn | None = None) -> dict[str, Any]:
    conversation_id = body.conversation_id if body else None
    with _root.db() as conn:
        if conversation_id is None:
            row = conn.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="还没有任何对话，无法提取记忆")
            conversation_id = row["id"]
        else:
            exists = conn.execute("SELECT id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="对话不存在")
        window_messages = _recent_messages(conn, conversation_id)

    if not model_config.load_model_config().enabled:
        raise HTTPException(status_code=400, detail="未配置 LLM_API_KEY（本地演示模式不提取记忆）")
    try:
        candidates = await memory_extract.extract_candidates_from_messages(window_messages, conversation_id)
    except Exception as exc:
        logger.warning("manual extraction failed conversation %s: %s", conversation_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"记忆提取失败：{type(exc).__name__}") from exc

    with _root.db() as conn:
        inserted = _store_candidates(conn, candidates)
    return {"conversation_id": conversation_id, "extracted": len(candidates), "stored": inserted, "candidates": candidates}


