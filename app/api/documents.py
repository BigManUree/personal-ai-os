from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

import app.main as _root
from app import (
    behavior_events,
    document_chunk,
    document_parse,
    embeddings,
)
from app.document_parse import DocumentParseError, Page
from app.schema_migrations import rebuild_chunks_fts as migrations_rebuild

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter(tags=["知识库"])


def _store_parsed_document(
    conn: sqlite3.Connection,
    *,
    display_name: str,
    content_for_legacy: str,
    pages: list[Page],
    ext: str,
    source_type: str,
    file_hash: str | None,
    byte_size: int | None,
) -> dict[str, Any]:
    """Insert a document and its chunks in one transaction."""
    timestamp = _root.now_iso()
    cur = conn.execute(
        "INSERT INTO documents(filename,content,created_at,file_hash,source_type,status,original_filename,"
        "file_ext,byte_size,error,chunk_count,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (display_name, content_for_legacy, timestamp, file_hash, source_type, "parsed", display_name,
         ext, byte_size, None, 0, timestamp),
    )
    document_id = cur.lastrowid
    chunks = document_chunk.chunk_pages(pages, ext=ext)
    for chunk in chunks:
        conn.execute(
            "INSERT INTO document_chunks(document_id,chunk_index,content,page_hint,heading,char_count,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (document_id, chunk.chunk_index, chunk.content, chunk.page_hint,
             chunk.heading, chunk.char_count, timestamp),
        )
    conn.execute("UPDATE documents SET chunk_count=? WHERE id=?", (len(chunks), document_id))
    # 向量索引在事务提交后异步补齐（不把网络往返压在写锁里，失败不影响入库）
    chunk_ids = [r[0] for r in conn.execute(
        "SELECT id FROM document_chunks WHERE document_id=? ORDER BY chunk_index", (document_id,)
    ).fetchall()]
    embeddings.schedule_chunks(chunk_ids)
    _root.record_behavior_event(
        conn,
        behavior_events.EVENT_DOCUMENT_CREATED,
        subject_type="document",
        subject_id=document_id,
        occurred_at=timestamp,
        metadata={"origin": source_type, "file_ext": ext, "chunk_count": len(chunks)},
    )
    return {"id": document_id, "filename": display_name, "status": "parsed",
            "chunk_count": len(chunks), "source_type": source_type, "file_ext": ext}


@router.get("/api/documents")
def list_documents() -> list[dict[str, Any]]:
    with _root.db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id,filename,substr(content,1,240) AS excerpt,created_at,"
            "source_type,status,file_ext,byte_size,chunk_count FROM documents ORDER BY id DESC"
        ).fetchall()]


@router.get("/api/documents/{document_id}")
def get_document(document_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        chunks = [dict(r) for r in conn.execute(
            "SELECT id,chunk_index,substr(content,1,320) AS excerpt,page_hint,heading,char_count "
            "FROM document_chunks WHERE document_id=? ORDER BY chunk_index",
            (document_id,),
        ).fetchall()]
        result = dict(row)
        result["chunks"] = chunks
        return result


@router.post("/api/documents/text")
def add_document(body: dict[str, str]) -> dict[str, Any]:
    filename = document_parse.safe_filename(body.get("filename", "未命名资料"))
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="资料内容不能为空")
    if len(content) > document_parse.MAX_TEXT_CHARS:
        raise HTTPException(status_code=413, detail=f"单份资料最多 {document_parse.MAX_TEXT_CHARS} 个字符")
    pages = [Page(text=content)]
    with _root.db() as conn:
        return _store_parsed_document(
            conn, display_name=filename, content_for_legacy=content, pages=pages,
            ext=".txt", source_type="manual_text", file_hash=None, byte_size=len(content.encode("utf-8")),
        )


@router.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
    raw_name = document_parse.safe_filename(file.filename or "未命名文件")
    ext = document_parse.normalize_extension(raw_name)
    if not ext:
        raise HTTPException(status_code=400, detail="不支持的文件类型，仅允许 PDF / DOCX / MD / TXT")
    data = await file.read()
    if len(data) > document_parse.MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 10MB 上限")
    file_hash = hashlib.sha256(data).hexdigest()

    with _root.db() as conn:
        dup = conn.execute("SELECT id FROM documents WHERE file_hash=?", (file_hash,)).fetchone()
    if dup:
        raise HTTPException(status_code=409, detail={
            "message": "相同内容的文件已上传", "existing_document_id": dup["id"],
        })

    try:
        pages = document_parse.parse_document(raw_name, data)
    except DocumentParseError as exc:
        # Recorded as failed metadata? Plan says no residue on failure; we only
        # respond 4xx and do not insert anything.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_text = "\n\n".join(page.text for page in pages)
    with _root.db() as conn:
        return _store_parsed_document(
            conn, display_name=raw_name, content_for_legacy=full_text, pages=pages,
            ext=ext, source_type="upload", file_hash=file_hash, byte_size=len(data),
        )


@router.delete("/api/documents/{document_id}")
def delete_document(document_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        row = conn.execute("SELECT id FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        try:
            conn.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
        except sqlite3.DatabaseError:
            # FTS 索引与内容表失同步时，删除触发器会抛
            # "database disk image is malformed" 并让整个删除 500。
            # 索引是派生数据：原地 rebuild 后重试一次即可自愈。
            migrations_rebuild(conn)
            conn.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
        conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
    return {"deleted": True}






@router.get("/api/embeddings/status")
def embeddings_status() -> dict[str, Any]:
    with _root.db() as conn:
        return embeddings.status(conn)


@router.post("/api/embeddings/reindex")
def embeddings_reindex(limit: int = 200) -> dict[str, Any]:
    """补齐缺失向量（分批调用直至 remaining=0）。"""
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit 必须在 1..1000 之间")
    with _root.db() as conn:
        return embeddings.backfill(conn, limit)
