"""向量检索层：千问 text-embedding（OpenAI 兼容 /embeddings）。

设计原则：
- **索引是派生数据**：`chunk_embeddings` 按 chunk_id 一行，缺失即补、模型变更即重算。
- **失败不影响主流程**：上传/对话/删除都不因向量服务抖动而报错，最差退化为纯词法检索。
- **归一化 float32 存储**：1024 维 × 4B = 4KB/条；单用户规模暴力余弦足够。
"""
from __future__ import annotations

import array
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("personal_ai_os.embeddings")

DEFAULT_MODEL = "text-embedding-v4"
BATCH_SIZE = 10
MAX_CHARS = 6000
# 实测分布：相关句 0.72 / 无关句 0.27 —— 0.45 留足两侧余量。
SIM_THRESHOLD = 0.45
# 向量命中在混合打分里的权重：sim=0.7 → +2.1，足以让零词法重叠的片段进结果。
VECTOR_WEIGHT = 3.0
_TIMEOUT = 30.0
_CACHE_LIMIT = 128
_query_cache: dict[tuple[str, str], list[float]] = {}


class EmbeddingError(RuntimeError):
    pass


def _base_url() -> str:
    return (os.environ.get("EMBEDDING_BASE_URL") or "").rstrip("/")


def _api_key() -> str:
    return os.environ.get("EMBEDDING_API_KEY") or ""


def model_name() -> str:
    return os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL


def enabled() -> bool:
    return bool(_base_url() and _api_key())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- 远端调用 ----------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化：10 条一批，429/5xx/超时 4 次退避重试。"""
    if not enabled():
        raise EmbeddingError("embedding endpoint not configured")
    if not texts:
        return []
    vectors: list[list[float]] = []
    with httpx.Client(timeout=_TIMEOUT) as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = [(t or " ")[:MAX_CHARS] for t in texts[start:start + BATCH_SIZE]]
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    resp = client.post(
                        f"{_base_url()}/embeddings",
                        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
                        json={"model": model_name(), "input": batch},
                    )
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise EmbeddingError(f"embedding http {resp.status_code}")
                    resp.raise_for_status()
                    data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
                    vectors.extend(d["embedding"] for d in data)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.2 * (2 ** attempt))
            if last_error is not None:
                raise EmbeddingError(str(last_error)) from last_error
    if len(vectors) != len(texts):
        raise EmbeddingError(f"embedding count mismatch: {len(vectors)} != {len(texts)}")
    return vectors


# ---------- 序列化 ----------

def serialize(vector: list[float] | array.array) -> bytes:
    return array.array("f", vector).tobytes()


def deserialize(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def _cosine(a: Any, b: Any) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a ** 0.5 * norm_b ** 0.5)


def _upsert(conn: sqlite3.Connection, chunk_id: int, vector: list[float]) -> None:
    conn.execute(
        "INSERT INTO chunk_embeddings(chunk_id, model, dim, vector, created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chunk_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
        "vector=excluded.vector, created_at=excluded.created_at",
        (chunk_id, model_name(), len(vector), serialize(vector), _now_iso()),
    )


# ---------- 索引维护 ----------

def ensure_chunks(conn: sqlite3.Connection, chunk_ids: list[int]) -> int:
    """为指定 chunk 补齐当前模型的向量；返回新增条数。失败只记日志。"""
    if not chunk_ids or not enabled():
        return 0
    model = model_name()
    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT id, content FROM document_chunks WHERE id IN ({placeholders})", chunk_ids
    ).fetchall()
    have = {
        r[0]
        for r in conn.execute(
            f"SELECT chunk_id FROM chunk_embeddings WHERE model=? AND chunk_id IN ({placeholders})",
            [model, *chunk_ids],
        ).fetchall()
    }
    todo = [(r[0], (r[1] or " ")) for r in rows if r[0] not in have]
    if not todo:
        return 0
    try:
        vectors = embed_texts([text for _, text in todo])
    except Exception as exc:
        logger.warning("embedding failed for %s chunk(s): %s", len(todo), exc)
        return 0
    for (chunk_id, _), vector in zip(todo, vectors):
        _upsert(conn, chunk_id, vector)
    logger.info("embedded %s chunk(s) with %s", len(todo), model)
    return len(todo)


def schedule_chunks(chunk_ids: list[int]) -> None:
    """请求提交后异步补齐向量，避免把网络往返压在写事务里。"""
    if not chunk_ids or not enabled():
        return

    def run() -> None:
        try:
            import app.main as main_module

            conn = sqlite3.connect(str(main_module.DB_PATH), timeout=10.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                ensure_chunks(conn, chunk_ids)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("background embedding failed: %s", exc)

    threading.Thread(target=run, name="embedding-indexer", daemon=True).start()


def backfill(conn: sqlite3.Connection, limit: int = 200) -> dict[str, Any]:
    """补齐最多 limit 个缺失向量的 chunk。"""
    model = model_name()
    if not enabled():
        return {"enabled": False, "indexed": 0, "remaining": 0, "model": model}
    rows = conn.execute(
        "SELECT c.id FROM document_chunks c "
        "LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id AND e.model = ? "
        "WHERE e.chunk_id IS NULL ORDER BY c.id LIMIT ?",
        (model, limit),
    ).fetchall()
    ids = [r[0] for r in rows]
    indexed = ensure_chunks(conn, ids) if ids else 0
    remaining = conn.execute(
        "SELECT COUNT(*) FROM document_chunks c "
        "LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id AND e.model = ? "
        "WHERE e.chunk_id IS NULL",
        (model,),
    ).fetchone()[0]
    return {"enabled": True, "indexed": indexed, "remaining": remaining, "model": model}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    model = model_name()
    chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
    indexed = conn.execute(
        "SELECT COUNT(*) FROM chunk_embeddings WHERE model=?", (model,)
    ).fetchone()[0]
    return {
        "enabled": enabled(),
        "model": model,
        "dim": conn.execute("SELECT dim FROM chunk_embeddings WHERE model=? LIMIT 1", (model,)).fetchone()[0]
        if indexed else None,
        "chunks_total": chunks,
        "chunks_indexed": indexed,
        "coverage": round(indexed / chunks, 4) if chunks else 0.0,
    }


# ---------- 检索 ----------

def _embed_query(query: str) -> list[float]:
    key = (model_name(), query)
    cached = _query_cache.get(key)
    if cached is not None:
        return cached
    vector = embed_texts([query])[0]
    if len(_query_cache) >= _CACHE_LIMIT:
        _query_cache.pop(next(iter(_query_cache)))
    _query_cache[key] = vector
    return vector


def query_scores(conn: sqlite3.Connection, query: str) -> dict[int, float]:
    """返回 {chunk_id: cosine}；任何失败返回空字典（调用方退化为词法检索）。"""
    if not enabled() or not query.strip():
        return {}
    model = model_name()
    try:
        rows = conn.execute(
            "SELECT chunk_id, vector FROM chunk_embeddings WHERE model=?", (model,)
        ).fetchall()
        if not rows:
            return {}
        query_vector = _embed_query(query)
    except Exception as exc:
        logger.warning("vector search unavailable: %s", exc)
        return {}
    scores: dict[int, float] = {}
    for chunk_id, blob in rows:
        try:
            scores[chunk_id] = _cosine(query_vector, deserialize(blob))
        except Exception:
            continue
    return scores
