from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

import app.main as _root
from app import (
    connector_scheduler,
    connectors,
    document_parse,
    github_connector,  # noqa: F401  (self-register on import)
    httpapi_connector,  # noqa: F401  (self-register on import)
    secret_crypto,
)
from app.document_parse import Page

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter(tags=["数据源"])


class DataSourceIn(BaseModel):
    kind: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    scope: str = Field(default="", max_length=1000)
    config: dict[str, Any] = Field(default_factory=dict)
    # Optional one-time credential; never stored in config_json and never
    # returned by any endpoint. Goes through the encrypted secret store.
    secret: str | None = Field(default=None, max_length=4000)
    sync_enabled: bool = False
    sync_interval_minutes: int | None = None

    @field_validator("sync_interval_minutes")
    @classmethod
    def _schedule_interval(cls, value: int | None) -> int | None:
        if value is not None:
            connector_scheduler.normalise_interval(value)
        return value

    @field_validator("secret")
    @classmethod
    def _secret_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 4000:
            raise ValueError("凭据过长（上限 4000 字符）")
        return value

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("数据源名称不能为空")
        return value


class DataSourcePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    scope: str | None = Field(default=None, max_length=1000)
    config: dict[str, Any] | None = None
    sync_interval_minutes: int | None = None

    @field_validator("sync_interval_minutes")
    @classmethod
    def _schedule_interval(cls, value: int | None) -> int | None:
        if value is not None:
            connector_scheduler.normalise_interval(value)
        return value

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("数据源名称不能为空")
        return value


class ScheduleIn(BaseModel):
    enabled: bool
    interval_minutes: int | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled_must_be_bool(cls, value: Any) -> Any:
        # Reject pydantic lax string->bool coercion ("yes"/"true" strings).
        if not isinstance(value, bool):
            raise ValueError("enabled 必须是布尔值 true/false")
        return value

    @field_validator("interval_minutes")
    @classmethod
    def _interval_range(cls, value: int | None) -> int | None:
        if value is None:
            return None
        try:
            connector_scheduler.normalise_interval(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value


class CredentialIn(BaseModel):
    secret: str = Field(min_length=1, max_length=4000)

    @field_validator("secret")
    @classmethod
    def _secret_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("凭据不能为空")
        return value


def _delete_document_cascade(conn: sqlite3.Connection, document_id: int) -> None:
    conn.execute("DELETE FROM document_chunks WHERE document_id=?", (document_id,))
    conn.execute("DELETE FROM documents WHERE id=?", (document_id,))


def _serialize_data_source(row: sqlite3.Row, has_credential: bool = False) -> dict[str, Any]:
    item = dict(row)
    try:
        item["config"] = json.loads(item.get("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        item["config"] = {}
    item.pop("config_json", None)
    item["has_credential"] = bool(has_credential)
    return item


def _get_data_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM data_sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return row


def _source_has_credential(conn: sqlite3.Connection, source_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM connector_credentials WHERE data_source_id=?", (source_id,)
    ).fetchone()
    return row is not None


def _load_connector_secret(conn: sqlite3.Connection, source_id: int) -> str:
    row = conn.execute(
        "SELECT ciphertext FROM connector_credentials WHERE data_source_id=?", (source_id,)
    ).fetchone()
    if row is None:
        return ""
    try:
        return secret_crypto.decrypt_secret(row["ciphertext"])
    except secret_crypto.SecretCryptoError as exc:
        # A lost/rotated key must not crash sync; treat as missing credential
        # and surface a safe, non-secret error to the caller.
        logger.warning("connector credential undecryptable source=%s: %s", source_id, type(exc).__name__)
        raise connectors.ConnectorAuthError("已保存的访问令牌无法解密（密钥可能已更换），请重新设置凭据") from exc


def _build_connector(row: sqlite3.Row):
    try:
        config = json.loads(row["config_json"] or "{}")
    except json.JSONDecodeError:
        config = {}
    with _root.db() as conn:
        secret = _load_connector_secret(conn, row["id"])
    return connectors.registry.create(
        row["kind"], config, scope=row["scope"] or "", source_id=str(row["id"]), secret=secret
    )


@router.get("/api/connector-types")
def connector_types() -> list[dict[str, str]]:
    return connectors.registry.available()


@router.get("/api/data-sources")
def list_data_sources() -> list[dict[str, Any]]:
    with _root.db() as conn:
        rows = conn.execute("SELECT * FROM data_sources ORDER BY id DESC").fetchall()
        result = []
        for row in rows:
            has_cred = _source_has_credential(conn, row["id"])
            item = _serialize_data_source(row, has_cred)
            stats = conn.execute(
                "SELECT COUNT(*) AS n FROM source_items WHERE data_source_id=? AND status='active'",
                (row["id"],),
            ).fetchone()["n"]
            last_log = conn.execute(
                "SELECT id,status,started_at,finished_at,created,updated,unchanged,skipped,deleted,failed,error "
                "FROM sync_logs WHERE data_source_id=? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            item["active_item_count"] = stats
            item["last_sync"] = dict(last_log) if last_log else None
            result.append(item)
        return result


@router.post("/api/data-sources")
async def create_data_source(body: DataSourceIn) -> dict[str, Any]:
    kind = body.kind.strip().lower()
    if not connectors.registry.is_registered(kind):
        raise HTTPException(status_code=400, detail=f"未知的连接器类型：{kind}")
    try:
        connectors.assert_config_has_no_secrets(body.config)
    except connectors.ConnectorConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config_text = json.dumps(body.config, ensure_ascii=False)
    if len(config_text) > connectors.MAX_CONFIG_JSON:
        raise HTTPException(status_code=422, detail="连接配置过大")

    timestamp = _root.now_iso()
    secret_cipher = ""
    if body.secret:
        try:
            secret_cipher = secret_crypto.encrypt_secret(body.secret)
        except secret_crypto.SecretCryptoError as exc:
            raise HTTPException(status_code=500, detail=f"凭据加密失败：{exc}") from exc

    with _root.db() as conn:
        interval = (body.sync_interval_minutes
                    if body.sync_interval_minutes is not None
                    else connector_scheduler.DEFAULT_INTERVAL_MINUTES)
        next_at = connector_scheduler.next_run_after_success(interval) if body.sync_enabled else None
        cur = conn.execute(
            "INSERT INTO data_sources(kind,name,status,scope,config_json,cursor,last_synced_at,last_error,"
            "sync_enabled,sync_interval_minutes,next_sync_at,consecutive_failures,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, body.name, "active", body.scope.strip(), config_text, None, None, None,
             1 if body.sync_enabled else 0, interval, next_at, 0, timestamp, timestamp),
        )
        source_id = cur.lastrowid
        if secret_cipher:
            conn.execute(
                "INSERT INTO connector_credentials(data_source_id,ciphertext,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (source_id, secret_cipher, timestamp, timestamp),
            )
        row = _get_data_source(conn, source_id)
        has_cred = bool(secret_cipher)

    # validate authorisation/config out of transaction; inject decrypted secret
    connector = connectors.registry.create(
        kind, body.config, scope=body.scope.strip(), source_id=str(source_id), secret=body.secret or ""
    )
    try:
        await connector.validate_config()
    except connectors.ConnectorError as exc:
        with _root.db() as conn:
            # CASCADE removes connector_credentials too.
            conn.execute("DELETE FROM data_sources WHERE id=?", (source_id,))
        raise HTTPException(status_code=400, detail=connectors.redact_secrets(str(exc))) from exc
    return _serialize_data_source(row, has_cred)


@router.get("/api/data-sources/{source_id}")
def get_data_source(source_id: int) -> dict[str, Any]:
    with _root.db() as conn:
        row = _get_data_source(conn, source_id)
        item = _serialize_data_source(row, _source_has_credential(conn, source_id))
        item["items"] = [dict(r) for r in conn.execute(
            "SELECT id,external_id,document_id,content_hash,title,status,last_seen_at "
            "FROM source_items WHERE data_source_id=? ORDER BY id", (source_id,)
        ).fetchall()]
        return item


@router.patch("/api/data-sources/{source_id}")
def patch_data_source(source_id: int, body: DataSourcePatchIn) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.scope is not None:
        fields["scope"] = body.scope.strip()
    if body.config is not None:
        try:
            connectors.assert_config_has_no_secrets(body.config)
        except connectors.ConnectorConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        config_text = json.dumps(body.config, ensure_ascii=False)
        if len(config_text) > connectors.MAX_CONFIG_JSON:
            raise HTTPException(status_code=422, detail="连接配置过大")
        fields["config_json"] = config_text
    interval_change = body.sync_interval_minutes
    if not fields and interval_change is None:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")
    with _root.db() as conn:
        row = _get_data_source(conn, source_id)
        if row["status"] == "revoked" and ("config_json" in fields or "scope" in fields):
            raise HTTPException(status_code=409, detail="已撤销授权的数据源不能修改配置，请新建数据源")
        if fields:
            columns = ", ".join(f"{key}=?" for key in fields)
            values = list(fields.values()) + [_root.now_iso(), source_id]
            conn.execute(f"UPDATE data_sources SET {columns}, updated_at=? WHERE id=?", values)
        # Changing the interval of an enabled active source re-arms next run.
        if interval_change is not None and bool(row["sync_enabled"]) and row["status"] == "active":
            conn.execute(
                "UPDATE data_sources SET sync_interval_minutes=?, next_sync_at=?, updated_at=? WHERE id=?",
                (interval_change, connector_scheduler.next_run_after_success(interval_change),
                 _root.now_iso(), source_id),
            )
        return _serialize_data_source(_get_data_source(conn, source_id),
                                      _source_has_credential(conn, source_id))


def _set_data_source_status(source_id: int, allowed: set[str], new_status: str) -> dict[str, Any]:
    with _root.db() as conn:
        row = _get_data_source(conn, source_id)
        if row["status"] not in allowed:
            raise HTTPException(
                status_code=409,
                detail=f"当前状态为 {row['status']}，无法切换到 {new_status}",
            )
        if new_status in ("paused", "revoked"):
            # A stopped source must never be picked up by the scheduler.
            conn.execute(
                "UPDATE data_sources SET status=?, next_sync_at=NULL, updated_at=? WHERE id=?",
                (new_status, _root.now_iso(), source_id),
            )
        elif new_status == "active" and row["sync_enabled"]:
            # Resume: re-arm automatic sync if the user had opted in.
            interval = row["sync_interval_minutes"] or connector_scheduler.DEFAULT_INTERVAL_MINUTES
            conn.execute(
                "UPDATE data_sources SET status=?, next_sync_at=?, updated_at=? WHERE id=?",
                (new_status, connector_scheduler.next_run_after_success(interval), _root.now_iso(), source_id),
            )
        else:
            conn.execute("UPDATE data_sources SET status=?, updated_at=? WHERE id=?",
                         (new_status, _root.now_iso(), source_id))
        return _serialize_data_source(_get_data_source(conn, source_id),
                                      _source_has_credential(conn, source_id))


@router.post("/api/data-sources/{source_id}/pause")
def pause_data_source(source_id: int) -> dict[str, Any]:
    return _set_data_source_status(source_id, {"active"}, "paused")


@router.post("/api/data-sources/{source_id}/resume")
def resume_data_source(source_id: int) -> dict[str, Any]:
    return _set_data_source_status(source_id, {"paused"}, "active")


@router.post("/api/data-sources/{source_id}/revoke")
def revoke_data_source(source_id: int) -> dict[str, Any]:
    return _set_data_source_status(source_id, {"active", "paused"}, "revoked")


@router.post("/api/data-sources/{source_id}/schedule")
def set_data_source_schedule(source_id: int, body: ScheduleIn) -> dict[str, Any]:
    """Opt in/out of automatic background sync for one source."""
    with _root.db() as conn:
        row = _get_data_source(conn, source_id)
        interval = body.interval_minutes
        if interval is None:
            interval = row["sync_interval_minutes"] or connector_scheduler.DEFAULT_INTERVAL_MINUTES
        interval = connector_scheduler.normalise_interval(interval)
        if body.enabled:
            if row["status"] == "revoked":
                raise HTTPException(status_code=409, detail="已撤销授权的数据源不能开启定时同步，请先恢复或新建")
            next_at = connector_scheduler.next_run_after_success(interval)
        else:
            next_at = None
        conn.execute(
            "UPDATE data_sources SET sync_enabled=?, sync_interval_minutes=?, next_sync_at=?, updated_at=? WHERE id=?",
            (1 if body.enabled else 0, interval, next_at, _root.now_iso(), source_id),
        )
        return _serialize_data_source(_get_data_source(conn, source_id),
                                      _source_has_credential(conn, source_id))


@router.put("/api/data-sources/{source_id}/credential")
def set_data_source_credential(source_id: int, body: CredentialIn) -> dict[str, Any]:
    """Store/replace the encrypted credential. Plaintext is never returned."""
    try:
        cipher = secret_crypto.encrypt_secret(body.secret)
    except secret_crypto.SecretCryptoError as exc:
        raise HTTPException(status_code=500, detail=f"凭据加密失败：{exc}") from exc
    timestamp = _root.now_iso()
    with _root.db() as conn:
        _get_data_source(conn, source_id)
        conn.execute(
            "INSERT INTO connector_credentials(data_source_id,ciphertext,created_at,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(data_source_id) DO UPDATE SET ciphertext=excluded.ciphertext, "
            "updated_at=excluded.updated_at",
            (source_id, cipher, timestamp, timestamp),
        )
        # A fresh credential clears a stale error, but a user-revoked source
        # stays revoked until explicitly resumed.
        conn.execute("UPDATE data_sources SET last_error=NULL, updated_at=? WHERE id=?",
                     (timestamp, source_id))
        row = _get_data_source(conn, source_id)
        return _serialize_data_source(row, True)


@router.delete("/api/data-sources/{source_id}/credential")
def delete_data_source_credential(source_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        _get_data_source(conn, source_id)
        conn.execute("DELETE FROM connector_credentials WHERE data_source_id=?", (source_id,))
    return {"deleted": True}


@router.delete("/api/data-sources/{source_id}")
def delete_data_source(source_id: int) -> dict[str, bool]:
    with _root.db() as conn:
        _get_data_source(conn, source_id)
        # Only documents owned by THIS source are removed; other sources and
        # manually uploaded documents are untouched.
        owned = conn.execute(
            "SELECT document_id FROM source_items WHERE data_source_id=? AND document_id IS NOT NULL",
            (source_id,),
        ).fetchall()
        for r in owned:
            _delete_document_cascade(conn, r["document_id"])
        conn.execute("DELETE FROM source_items WHERE data_source_id=?", (source_id,))
        conn.execute("DELETE FROM sync_logs WHERE data_source_id=?", (source_id,))
        conn.execute("DELETE FROM data_sources WHERE id=?", (source_id,))
    return {"deleted": True}


@router.get("/api/data-sources/{source_id}/sync-logs")
def list_sync_logs(source_id: int, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with _root.db() as conn:
        _get_data_source(conn, source_id)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM sync_logs WHERE data_source_id=? ORDER BY id DESC LIMIT ?",
            (source_id, limit),
        ).fetchall()]


def _apply_schedule(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    ok: bool,
    interval: int,
    enabled: bool,
    failures_before: int,
) -> None:
    """Arm the next run (success resets, failure backs off) or clear it."""
    if ok:
        next_at = connector_scheduler.next_run_after_success(interval) if enabled else None
        conn.execute(
            "UPDATE data_sources SET consecutive_failures=0, next_sync_at=? WHERE id=?",
            (next_at, source_id),
        )
    else:
        failures = failures_before + 1
        next_at = connector_scheduler.next_run_after_failure(interval, failures) if enabled else None
        conn.execute(
            "UPDATE data_sources SET consecutive_failures=?, next_sync_at=? WHERE id=?",
            (failures, next_at, source_id),
        )


async def _run_source_sync(source_id: int) -> dict[str, Any]:
    """Core sync shared by the HTTP endpoint and the background scheduler.

    Fetch failures are returned (status=failed), never raised; only the
    not-active precondition raises HTTPException(409).
    """
    with _root.db() as conn:
        row = _get_data_source(conn, source_id)
        if row["status"] != "active":
            raise HTTPException(status_code=409, detail=f"数据源状态为 {row['status']}，无法同步（需 active）")
        cursor_before = row["cursor"]
        interval = row["sync_interval_minutes"] or connector_scheduler.DEFAULT_INTERVAL_MINUTES
        enabled = bool(row["sync_enabled"])
        failures_before = row["consecutive_failures"] or 0
        log_cur = conn.execute(
            "INSERT INTO sync_logs(data_source_id,started_at,finished_at,status,cursor_before,cursor_after) "
            "VALUES(?,?,?,?,?,?)",
            (source_id, _root.now_iso(), None, "running", cursor_before, None),
        )
        log_id = log_cur.lastrowid

    # ---- fetch OUTSIDE any write transaction; failures never delete data ----
    try:
        connector = _build_connector(row)
        await connector.validate_config()
        result = await connector.fetch_changes(cursor_before)
    except connectors.ConnectorAuthError as exc:
        error = connectors.redact_secrets(str(exc))
        with _root.db() as conn:
            conn.execute(
                "UPDATE data_sources SET status='revoked', last_error=?, next_sync_at=NULL, updated_at=? WHERE id=?",
                (error, _root.now_iso(), source_id),
            )
            conn.execute("UPDATE sync_logs SET status='failed', finished_at=?, error=? WHERE id=?",
                         (_root.now_iso(), error, log_id))
        return {"status": "failed", "reason": "revoked", "error": error, "log_id": log_id}
    except Exception as exc:
        error = connectors.redact_secrets(f"{type(exc).__name__}: {exc}")
        with _root.db() as conn:
            conn.execute("UPDATE data_sources SET last_error=?, updated_at=? WHERE id=?",
                         (error, _root.now_iso(), source_id))
            _apply_schedule(conn, source_id, ok=False, interval=interval,
                            enabled=enabled, failures_before=failures_before)
            conn.execute("UPDATE sync_logs SET status='failed', finished_at=?, error=?, failed=1 WHERE id=?",
                         (_root.now_iso(), error, log_id))
        return {"status": "failed", "reason": "fetch_error", "error": error, "log_id": log_id}

    # Server-owned provenance: never trust connector-provided source fields.
    for item in result.items:
        item.source_type = "connector"
        item.source_id = str(source_id)

    # ---- apply changes in a single transaction ----
    counters = {
        "total": len(result.items) + len(result.unchanged_external_ids) + len(result.deleted_external_ids),
        "created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "deleted": 0, "failed": 0,
    }
    timestamp = _root.now_iso()
    with _root.db() as conn:
        for item in result.items:
            try:
                existing = conn.execute(
                    "SELECT * FROM source_items WHERE data_source_id=? AND external_id=?",
                    (source_id, item.external_id),
                ).fetchone()
                ext = document_parse.normalize_extension(item.metadata.get("ext", "") or item.title or item.external_id) or ".txt"
                display_name = item.title or item.external_id
                if existing is None:
                    stored = _root._store_parsed_document(
                        conn, display_name=display_name, content_for_legacy=item.content,
                        pages=[Page(text=item.content)], ext=ext, source_type="connector",
                        file_hash=item.content_hash, byte_size=len(item.content.encode("utf-8")),
                    )
                    conn.execute(
                        "INSERT INTO source_items(data_source_id,external_id,document_id,content_hash,title,"
                        "status,last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (source_id, item.external_id, stored["id"], item.content_hash, item.title,
                         "active", timestamp, timestamp, timestamp),
                    )
                    counters["created"] += 1
                elif existing["content_hash"] == item.content_hash and existing["status"] == "active":
                    conn.execute("UPDATE source_items SET last_seen_at=?, status='active' WHERE id=?",
                                 (timestamp, existing["id"]))
                    counters["unchanged"] += 1
                else:
                    if existing["document_id"] is not None:
                        _delete_document_cascade(conn, existing["document_id"])
                    stored = _root._store_parsed_document(
                        conn, display_name=display_name, content_for_legacy=item.content,
                        pages=[Page(text=item.content)], ext=ext, source_type="connector",
                        file_hash=item.content_hash, byte_size=len(item.content.encode("utf-8")),
                    )
                    conn.execute(
                        "UPDATE source_items SET document_id=?, content_hash=?, title=?, status='active', "
                        "last_seen_at=?, updated_at=? WHERE id=?",
                        (stored["id"], item.content_hash, item.title, timestamp, timestamp, existing["id"]),
                    )
                    counters["updated"] += 1
            except Exception as exc:
                counters["failed"] += 1
                conn.execute(
                    "UPDATE sync_logs SET failed=? WHERE id=?", (counters["failed"], log_id)
                )
                logger.warning("connector item failed source=%s ext_id=%s: %s",
                               source_id, getattr(item, "external_id", "?"), type(exc).__name__)

        # Resources the connector observed but did not need to re-parse
        # (mtime/size unchanged). Refresh liveness and count them; never
        # recreate a row without content.
        for external_id in result.unchanged_external_ids:
            existing = conn.execute(
                "SELECT * FROM source_items WHERE data_source_id=? AND external_id=?",
                (source_id, str(external_id)),
            ).fetchone()
            if existing is None or existing["status"] != "active":
                counters["skipped"] += 1
                continue
            conn.execute(
                "UPDATE source_items SET last_seen_at=?, updated_at=? WHERE id=?",
                (timestamp, timestamp, existing["id"]),
            )
            counters["unchanged"] += 1

        # external deletions: only this source's own resources
        for external_id in result.deleted_external_ids:
            existing = conn.execute(
                "SELECT * FROM source_items WHERE data_source_id=? AND external_id=? AND status='active'",
                (source_id, str(external_id)),
            ).fetchone()
            if existing is None:
                counters["skipped"] += 1
                continue
            if existing["document_id"] is not None:
                _delete_document_cascade(conn, existing["document_id"])
            conn.execute(
                "UPDATE source_items SET status='deleted', document_id=NULL, updated_at=? WHERE id=?",
                (timestamp, existing["id"]),
            )
            counters["deleted"] += 1

        next_cursor = result.next_cursor
        conn.execute(
            "UPDATE data_sources SET cursor=?, last_synced_at=?, last_error=NULL, updated_at=? WHERE id=?",
            (next_cursor, timestamp, timestamp, source_id),
        )
        _apply_schedule(conn, source_id, ok=True, interval=interval,
                        enabled=enabled, failures_before=failures_before)
        conn.execute(
            "UPDATE sync_logs SET status='success', finished_at=?, cursor_after=?, total=?, created=?, "
            "updated=?, unchanged=?, skipped=?, deleted=?, failed=? WHERE id=?",
            (timestamp, next_cursor, counters["total"], counters["created"], counters["updated"],
             counters["unchanged"], counters["skipped"], counters["deleted"], counters["failed"], log_id),
        )

    return {"status": "success", "log_id": log_id, "cursor": result.next_cursor, **counters}


@router.post("/api/data-sources/{source_id}/sync")
async def sync_data_source(source_id: int) -> dict[str, Any]:
    """Single unified HTTP entry for first sync and incremental sync."""
    return await _run_source_sync(source_id)


def _due_source_ids() -> list[int]:
    """Active, opted-in sources whose next_sync_at is due (UTC compare)."""
    with _root.db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT id FROM data_sources WHERE status='active' AND sync_enabled=1 "
            "AND next_sync_at IS NOT NULL AND next_sync_at<=? ORDER BY next_sync_at",
            (_root.now_iso(),),
        ).fetchall()]


# ===== Personal AI OS 1.2 iteration 3.5.1 connector framework (inserted block end)






# ===== Personal AI OS 1.3.3-D recommendation management API =====



