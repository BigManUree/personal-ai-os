"""Data-source connector framework (Personal AI OS 1.2, iteration 3.5.1).

This module is intentionally self-contained:

* it never imports the FastAPI app / database layer,
* it never performs network or filesystem access itself (the built-in
  MockConnector only reads static config),
* it never calls an LLM or writes memories / profile items.

Concrete connectors (local directory, GitHub, ...) arrive in 3.5.2+.
They implement :class:`BaseConnector` and register via
:class:`ConnectorRegistry`. Registration is a code-time, developer action;
there is deliberately no HTTP endpoint that can register a connector, and
registering a connector type never authorises access to any user data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app import document_parse
from app.document_parse import DocumentParseError

logger = logging.getLogger("personal_ai_os.connectors")

# ---------------------------------------------------------------------------
# Limits & secret handling
# ---------------------------------------------------------------------------

MAX_ITEM_CHARS = 200_000          # mirrors document_parse.MAX_TEXT_CHARS
MAX_TITLE_CHARS = 500
MAX_URL_CHARS = 500
MAX_EXTERNAL_ID_CHARS = 300
MAX_METADATA_JSON = 2000
MAX_CONFIG_JSON = 20_000
MAX_LOCAL_FILES = 200
LOCAL_DEFAULT_MAX_DEPTH = 6
LOCAL_MAX_DEPTH_CAP = 12

_SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)\bsk-[a-z0-9][a-z0-9\-]{12,}"),
    re.compile(r"(?i)\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth|credential|密码|口令|令牌|密钥)\s*[:：=]\s*[\"']?\S{4,}"),
]
_SECRET_KEY_RE = re.compile(
    r"(?i)^(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret|credential|密码|口令|令牌|密钥)$"
)


def redact_secrets(text: str | None) -> str:
    """Return *text* with credential-like substrings masked (for logs)."""
    if not text:
        return ""
    redacted = str(text)
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:2000]


def _json_contains_secret(obj: Any) -> bool:
    """Recursively detect secret-looking keys/values inside a config payload."""

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and _SECRET_KEY_RE.match(key.strip()):
                    return True
                if walk(value):
                    return True
        elif isinstance(node, (list, tuple)):
            return any(walk(v) for v in node)
        elif isinstance(node, str):
            return any(p.search(node) for p in _SECRET_VALUE_PATTERNS)
        return False

    return walk(obj)


def assert_config_has_no_secrets(config: Any) -> None:
    """Reject plaintext credentials in a connector config.

    3.5.x connectors that genuinely need a token must source it from a
    dedicated secret store instead of embedding it in config_json.
    """
    if _json_contains_secret(config):
        raise ConnectorConfigError("连接配置中疑似包含密钥/令牌，凭据不能明文保存在普通配置中。")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConnectorError(Exception):
    """Base class for all connector failures."""


class ConnectorUnknownError(ConnectorError):
    """Requested connector type is not registered."""


class ConnectorConfigError(ConnectorError):
    """Configuration / authorisation scope is invalid."""


class ConnectorFetchError(ConnectorError):
    """Temporary fetch failure (network/timeout). Existing data must be kept."""


class ConnectorUnavailable(ConnectorError):
    """Source temporarily unavailable (distinct from permanent delete)."""


class ConnectorAuthError(ConnectorError):
    """Authorisation revoked / missing. Caller should mark source revoked."""


# ---------------------------------------------------------------------------
# Standardised data item & sync result
# ---------------------------------------------------------------------------


class StandardItem(BaseModel):
    """Normalised resource produced by every connector.

    ``source_type`` / ``source_id`` are server-owned and stamped by the
    sync service, never trusted from connector output.
    """

    source_type: str = Field(default="", max_length=50)
    source_id: str = Field(default="", max_length=100)
    external_id: str = Field(min_length=1, max_length=MAX_EXTERNAL_ID_CHARS)
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    content: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)
    content_hash: str = Field(default="", max_length=80)
    source_url: str = Field(default="", max_length=MAX_URL_CHARS)
    updated_at: str = Field(default="", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalise(self) -> StandardItem:
        self.external_id = self.external_id.strip()
        self.title = self.title.strip()
        if not self.content.strip():
            raise ValueError("content 不能为空字符串")
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        meta_json = _safe_json(self.metadata)
        if meta_json is None or len(meta_json) > MAX_METADATA_JSON:
            raise ValueError("metadata 必须是可 JSON 序列化的小对象")
        if any(p.search(meta_json) for p in _SECRET_VALUE_PATTERNS):
            raise ValueError("metadata 中不允许包含凭据")
        return self


def _safe_json(obj: Any) -> str | None:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None


@dataclass
class SyncResult:
    items: list[StandardItem] = field(default_factory=list)
    next_cursor: str | None = None
    deleted_external_ids: list[str] = field(default_factory=list)
    # Resources observed but unchanged since cursor (avoids re-parsing while
    # still letting the sync service count/refresh them and prove liveness).
    unchanged_external_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Connector contract
# ---------------------------------------------------------------------------


class BaseConnector(ABC):
    """Connector implementers only fetch and normalise data."""

    kind: str = "base"
    label: str = "基础连接器"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        scope: str = "",
        source_id: str = "",
        secret: str = "",
    ):
        self.config = config or {}
        self.scope = scope or ""
        self.source_id = source_id or ""
        # Decrypted credential injected from the dedicated secret store for the
        # duration of one request only; never persisted here or logged.
        self.secret = secret or ""

    @abstractmethod
    async def validate_config(self) -> None:
        """Raise ConnectorConfigError when configuration/authorisation is invalid."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_changes(self, cursor: str | None = None) -> SyncResult:
        """Return first batch or incremental changes from *cursor*."""
        raise NotImplementedError

    async def close(self) -> None:
        """Release any held resources (default: nothing)."""
        return None


class MockConnector(BaseConnector):
    """Deterministic in-memory connector for framework tests and UI try-out.

    config:
      items: list of dicts accepted by StandardItem (minus source fields)
      next_cursor / deleted_ids: returned verbatim
      fail: "config" | "fetch" | "auth" | "unavailable" to simulate failures
    Mock is the ONLY connector allowed to carry content directly in config.
    """

    kind = "mock"
    label = "Mock 测试连接器"

    async def validate_config(self) -> None:
        if self.config.get("fail") == "config":
            raise ConnectorConfigError("模拟的配置错误")
        items = self.config.get("items", [])
        if not isinstance(items, list):
            raise ConnectorConfigError("items 必须是列表")

    async def fetch_changes(self, cursor: str | None = None) -> SyncResult:
        fail = self.config.get("fail")
        if fail == "auth":
            raise ConnectorAuthError("模拟的授权失效")
        if fail == "unavailable":
            raise ConnectorUnavailable("模拟的数据源暂时不可访问")
        if fail == "fetch":
            raise ConnectorFetchError("模拟的网络错误")

        raw_items = self.config.get("items", [])
        items: list[StandardItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            data = {k: v for k, v in raw.items() if k not in ("source_type", "source_id")}
            items.append(StandardItem(**data))
        deleted = [str(x) for x in self.config.get("deleted_ids", [])]
        return SyncResult(
            items=items,
            next_cursor=self.config.get("next_cursor"),
            deleted_external_ids=deleted,
        )


# ---------------------------------------------------------------------------
# Local read-only directory connector (iteration 3.5.2, batch 1)
# ---------------------------------------------------------------------------


def _denied_roots() -> list[Path]:
    """Absolute directories that must never be authorised as a sync root."""
    roots: list[Path] = []

    def add(raw: str | None) -> None:
        if raw:
            roots.append(Path(raw).expanduser())

    add(os.environ.get("SystemRoot") or r"C:\Windows")
    add(os.environ.get("ProgramFiles"))
    add(os.environ.get("ProgramFiles(x86)"))
    add(os.environ.get("ProgramData"))
    add(os.environ.get("APPDATA"))
    add(os.environ.get("LOCALAPPDATA"))
    add(os.environ.get("USERPROFILE"))  # the whole home directory
    # This project itself (its code + SQLite database must not be indexed).
    roots.append(Path(__file__).resolve().parent.parent)
    return roots


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class LocalDirectoryConnector(BaseConnector):
    """Read-only sync of allowed documents inside one authorised directory."""

    kind = "local_dir"
    label = "本地目录（只读）"

    def _resolved_root(self) -> Path:
        raw = (self.config or {}).get("root")
        if not isinstance(raw, str) or not raw.strip():
            raise ConnectorConfigError("必须提供授权目录 root（绝对路径）")
        root = Path(raw).expanduser()
        if not root.is_absolute():
            raise ConnectorConfigError("root 必须是绝对路径，不允许相对路径")
        return root.resolve(strict=False)

    def _options(self) -> tuple[bool, int]:
        recursive = self.config.get("recursive", True)
        max_depth = self.config.get("max_depth", LOCAL_DEFAULT_MAX_DEPTH)
        if not isinstance(recursive, bool):
            raise ConnectorConfigError("recursive 必须是布尔值")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool):
            raise ConnectorConfigError("max_depth 必须是整数")
        if not 0 <= max_depth <= LOCAL_MAX_DEPTH_CAP:
            raise ConnectorConfigError(f"max_depth 必须在 0 到 {LOCAL_MAX_DEPTH_CAP} 之间")
        return recursive, max_depth

    async def validate_config(self) -> None:
        root = self._resolved_root()
        self._options()
        if root.parent == root:
            raise ConnectorConfigError("不允许授权整个盘符根目录，请选择具体文件夹")
        if not root.exists():
            raise ConnectorConfigError("授权目录不存在")
        if not root.is_dir():
            raise ConnectorConfigError("授权路径不是目录")
        # Explicit carve-out: the OS temp directory is ephemeral and non-secret
        # (also where test sandboxes live). It still can never be a drive root.
        temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
        if not _is_within(root, temp_root):
            for denied in _denied_roots():
                try:
                    denied_resolved = denied.resolve(strict=False)
                except OSError:
                    continue
                if _is_within(root, denied_resolved) or root == denied_resolved:
                    raise ConnectorConfigError(
                        "出于安全考虑，不允许授权系统目录、用户主目录、AppData 或本项目目录"
                    )

    def _eligible_files(self, root: Path, recursive: bool, max_depth: int) -> list[Path]:
        allowed = document_parse.ALLOWED_EXTENSIONS
        collected: list[Path] = []
        if recursive:
            for current, dirnames, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                rel_parts = current_path.relative_to(root).parts
                depth = len(rel_parts)
                # Never descend through a symlinked directory or past max_depth.
                kept_dirs = []
                for d in dirnames:
                    child = current_path / d
                    if depth + 1 > max_depth or os.path.islink(child):
                        continue
                    kept_dirs.append(d)
                dirnames[:] = kept_dirs
                for name in filenames:
                    if Path(name).suffix.lower() not in allowed:
                        continue
                    collected.append(current_path / name)
                    if len(collected) > MAX_LOCAL_FILES:
                        raise ConnectorConfigError(
                            f"授权目录内可同步文件超过 {MAX_LOCAL_FILES} 个上限，请缩小授权范围"
                        )
        else:
            for child in root.iterdir():
                if child.is_file() and child.suffix.lower() in allowed:
                    collected.append(child)
                    if len(collected) > MAX_LOCAL_FILES:
                        raise ConnectorConfigError(
                            f"授权目录内可同步文件超过 {MAX_LOCAL_FILES} 个上限，请缩小授权范围"
                        )
        return collected

    async def fetch_changes(self, cursor: str | None = None) -> SyncResult:
        await self.validate_config()
        root = self._resolved_root()
        recursive, max_depth = self._options()

        previous: dict[str, dict[str, Any]] = {}
        if cursor:
            try:
                parsed = json.loads(cursor)
                if isinstance(parsed, dict) and isinstance(parsed.get("files"), dict):
                    previous = parsed["files"]
            except (json.JSONDecodeError, TypeError):
                previous = {}

        try:
            files = self._eligible_files(root, recursive, max_depth)
        except ConnectorConfigError:
            raise
        except OSError as exc:
            raise ConnectorFetchError(f"枚举授权目录失败：{type(exc).__name__}") from exc

        items: list[StandardItem] = []
        unchanged_ids: list[str] = []
        index: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()

        for path in files:
            try:
                if os.path.islink(path):
                    continue
                resolved = path.resolve(strict=False)
                if not _is_within(resolved, root):
                    continue  # symlink/hardlink escape attempt: skip
                stat = path.stat()
                rel = path.relative_to(root).as_posix()
                seen.add(rel)

                prev = previous.get(rel)
                unchanged = (
                    prev is not None
                    and prev.get("m") == stat.st_mtime_ns
                    and prev.get("s") == stat.st_size
                    and prev.get("h")
                )
                if unchanged:
                    index[rel] = {"m": stat.st_mtime_ns, "s": stat.st_size, "h": prev.get("h")}
                    unchanged_ids.append(rel)
                    continue

                data = path.read_bytes()
                pages = document_parse.parse_document(path.name, data)
                content = "\n\n".join(page.text for page in pages).strip()
                if not content:
                    continue
                ext = document_parse.normalize_extension(path.name) or ".txt"
                item = StandardItem(
                    external_id=rel,
                    title=rel,
                    content=content,
                    metadata={"ext": ext, "path": rel, "size": stat.st_size, "mtime": stat.st_mtime_ns},
                )
                items.append(item)
                index[rel] = {"m": stat.st_mtime_ns, "s": stat.st_size, "h": item.content_hash}
            except (DocumentParseError, OSError, ValueError) as exc:
                # One unreadable/empty/locked file must not abort the whole sync.
                logger.info("local connector skipped %s: %s", getattr(path, "name", "?"), type(exc).__name__)
                continue

        deleted = [rel for rel in previous.keys() if rel not in seen]
        next_cursor = json.dumps({"files": index}, ensure_ascii=False, sort_keys=True)
        return SyncResult(
            items=items,
            next_cursor=next_cursor,
            deleted_external_ids=deleted,
            unchanged_external_ids=unchanged_ids,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, type[BaseConnector]] = {}

    def register(self, kind: str, connector_cls: type[BaseConnector], *, override: bool = False) -> None:
        kind = (kind or "").strip().lower()
        if not kind:
            raise ValueError("连接器类型不能为空")
        if not isinstance(connector_cls, type) or not issubclass(connector_cls, BaseConnector):
            raise ValueError("只能注册 BaseConnector 的子类")
        if kind in self._connectors and not override:
            raise ValueError(f"连接器类型已注册：{kind}")
        self._connectors[kind] = connector_cls

    def unregister(self, kind: str) -> None:
        self._connectors.pop((kind or "").strip().lower(), None)

    def is_registered(self, kind: str) -> bool:
        return (kind or "").strip().lower() in self._connectors

    def available(self) -> list[dict[str, str]]:
        return [{"kind": k, "label": getattr(v, "label", k)} for k, v in sorted(self._connectors.items())]

    def create(
        self,
        kind: str,
        config: dict[str, Any] | None = None,
        *,
        scope: str = "",
        source_id: str = "",
        secret: str = "",
    ) -> BaseConnector:
        key = (kind or "").strip().lower()
        connector_cls = self._connectors.get(key)
        if connector_cls is None:
            raise ConnectorUnknownError(f"未知的连接器类型：{kind!r}，可用类型：{sorted(self._connectors)}")
        return connector_cls(config=config, scope=scope, source_id=source_id, secret=secret)


registry = ConnectorRegistry()
registry.register("mock", MockConnector)
registry.register("local_dir", LocalDirectoryConnector)
