"""Generic read-only HTTP/JSON API connector (Personal AI OS 1.2, 3.5.3 batch 1).

The user explicitly configures ONE GET endpoint that returns a JSON list
(or an object containing a list). A small field map normalises each entry to
a StandardItem. The connector never follows instructions contained in the
remote payload: field values are treated strictly as untrusted data.

Security highlights:
* https only, except loopback http when ``allow_localhost`` is explicitly set;
* SSRF guard via DNS resolution rejecting private/loopback/link-local/
  multicast/reserved ranges (169.254.169.254 metadata included);
* no redirects, hard timeouts, response size and item-count caps;
* credentials only travel as ``Authorization: Bearer`` (injected from the
  encrypted secret store) and never enter items/cursors/logs.

Tests inject ``httpx.MockTransport`` through ``build_transport`` (zero network).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from app.connectors import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorFetchError,
    StandardItem,
    SyncResult,
)

logger = logging.getLogger("personal_ai_os.httpapi_connector")

REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_RESPONSE_BYTES = 6_000_000
MAX_ITEMS_PER_SYNC = 200
DEFAULT_FIELDS = {
    "external_id": "id",
    "title": "title",
    "content": "content",
    "updated_at": "updated_at",
    "source_url": "url",
}
REQUIRED_MAP_KEYS = ("external_id", "content")
_HEADER_RE = re.compile(r"^[A-Za-z0-9\-]{1,50}$")


def build_transport() -> httpx.BaseTransport | None:
    """Real runs use the default TCP transport; tests monkeypatch this."""
    return None


def resolve_ips(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve a hostname to candidate IPs. Tests monkeypatch this hook."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ConnectorFetchError(f"域名解析失败：{host}") from exc
    result: list[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            result.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return result


def _walk_path(payload: Any, dotted: str) -> Any:
    node = payload
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


_MISSING = object()


def _extract(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    # Render simple scalars/objects predictably; remote content stays data.
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class HttpApiConnector(BaseConnector):
    """Fetch one configured GET list endpoint and map entries to items."""

    kind = "http_api"
    label = "通用 HTTP API（只读 GET）"

    # ------------------------------------------------------------- validation
    def _parsed_url(self) -> tuple[str, str, str | None]:
        raw = str(self.config.get("url", "")).strip()
        if not raw:
            raise ConnectorConfigError("必须配置 url")
        try:
            parsed = urlparse(raw)
        except ValueError as exc:
            raise ConnectorConfigError("url 无法解析") from exc
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        if scheme not in ("http", "https") or not host:
            raise ConnectorConfigError("url 仅允许 http(s) 且必须包含主机名")
        return raw, scheme, host

    def _fields(self) -> dict[str, str]:
        custom = self.config.get("fields", {})
        if not isinstance(custom, dict):
            raise ConnectorConfigError("fields 必须是对象")
        if not custom:
            return dict(DEFAULT_FIELDS)
        # A custom map must name both mandatory fields; optional fields fall
        # back to their conventional names when omitted.
        cleaned = {k: str(v).strip() for k, v in custom.items() if str(v).strip()}
        for key in REQUIRED_MAP_KEYS:
            if not cleaned.get(key):
                raise ConnectorConfigError(f"fields 必须显式映射 {key}")
        merged = {**DEFAULT_FIELDS, **cleaned}
        return merged

    def _header_name(self) -> str:
        name = str(self.config.get("auth_header_name", "Authorization")).strip() or "Authorization"
        if not _HEADER_RE.match(name):
            raise ConnectorConfigError("auth_header_name 非法")
        return name

    @staticmethod
    def _is_loopback(host: str) -> bool:
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host.lower() in ("localhost", "localhost.localdomain")

    def _assert_host_allowed(self, scheme: str, host: str, allow_localhost: bool) -> None:
        # Literal IP literal: classify directly, no DNS needed.
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None

        if ip is not None:
            ips = [ip]
        else:
            if host.lower() == "localhost" and allow_localhost and scheme == "http":
                return  # localhost name explicitly allowed
            ips = resolve_ips(host)

        for addr in ips:
            if addr.is_loopback:
                if not (allow_localhost and scheme == "http"):
                    raise ConnectorConfigError("默认禁止访问本机地址（需 allow_localhost 且 http loopback）")
                continue
            blocked = (
                addr.is_private
                or addr.is_link_local
                or addr.is_multicast
                or addr.is_reserved
                or addr.is_unspecified
            )
            if blocked:
                raise ConnectorConfigError(f"目标地址落在禁止访问的内网/保留地址段：{addr}")
            if scheme != "https" and not addr.is_loopback:
                raise ConnectorConfigError("非 loopback 目标必须使用 https")

    async def validate_config(self) -> None:
        raw_url, scheme, host = self._parsed_url()
        self._fields()
        self._header_name()
        allow_localhost = bool(self.config.get("allow_localhost", False))
        self._assert_host_allowed(scheme, host, allow_localhost)
        if not self.secret.strip():
            # Public endpoints are fine; only validate reachability when a
            # credential is configured to distinguish auth problems early.
            return
        async with self._client() as client:
            await self._get(client, raw_url, validate=True)

    # ------------------------------------------------------------------ fetch
    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json", "User-Agent": "personal-ai-os/0.3"}
        if self.secret.strip():
            headers[self._header_name()] = f"Bearer {self.secret.strip()}"
        return httpx.AsyncClient(
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
            transport=build_transport(),
        )

    async def _get(self, client: httpx.AsyncClient, url: str, *, validate: bool = False) -> httpx.Response:
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as exc:
            raise ConnectorFetchError("HTTP API 请求超时") from exc
        except httpx.HTTPError as exc:
            raise ConnectorFetchError(f"HTTP API 网络错误：{type(exc).__name__}") from exc

        if resp.status_code in (401, 403):
            if resp.headers.get("retry-after") or resp.status_code == 429:
                raise ConnectorFetchError("HTTP API 限流，请稍后重试")
            raise ConnectorAuthError(f"HTTP API 鉴权失败（{resp.status_code}）")
        if resp.status_code == 429:
            raise ConnectorFetchError("HTTP API 限流，请稍后重试")
        if resp.status_code == 404:
            if validate:
                raise ConnectorConfigError("接口不存在（404），请检查 url")
            raise ConnectorFetchError("HTTP API 资源不存在（404）")
        if resp.status_code >= 500:
            raise ConnectorFetchError(f"HTTP API 服务异常（{resp.status_code}）")
        if 300 <= resp.status_code < 400:
            raise ConnectorFetchError("HTTP API 返回了重定向，已拒绝跟随")
        if resp.status_code != 200:
            raise ConnectorFetchError(f"HTTP API 返回非预期状态码 {resp.status_code}")
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise ConnectorFetchError("HTTP API 响应超过大小上限")
        return resp

    async def fetch_changes(self, cursor: str | None = None) -> SyncResult:
        raw_url, scheme, host = self._parsed_url()
        mapping = self._fields()
        allow_localhost = bool(self.config.get("allow_localhost", False))
        self._assert_host_allowed(scheme, host, allow_localhost)

        prev: dict[str, str] = {}
        if cursor:
            try:
                parsed = json.loads(cursor)
                if isinstance(parsed, dict) and isinstance(parsed.get("ids"), dict):
                    prev = parsed["ids"]
            except (json.JSONDecodeError, TypeError):
                prev = {}

        async with self._client() as client:
            resp = await self._get(client, raw_url)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ConnectorFetchError("HTTP API 返回的不是合法 JSON") from exc

        items_path = str(self.config.get("items_path", "")).strip()
        entries = _walk_path(payload, items_path) if items_path else payload
        if entries is _MISSING or not isinstance(entries, list):
            raise ConnectorFetchError(f"未在 items_path={items_path or '<root>'} 找到条目数组")

        index: dict[str, str] = {}
        items: list[StandardItem] = []
        unchanged: list[str] = []
        seen: set[str] = set()

        for raw_entry in entries[:MAX_ITEMS_PER_SYNC]:
            if not isinstance(raw_entry, dict):
                continue
            external_id = _extract(raw_entry.get(mapping["external_id"]))
            content = _extract(raw_entry.get(mapping["content"]))
            if not external_id or not content:
                continue  # mandatory fields missing: skip this entry
            if external_id in seen:
                continue
            seen.add(external_id)

            title = _extract(raw_entry.get(mapping.get("title", ""), "")) if mapping.get("title") else ""
            updated_at = _extract(raw_entry.get(mapping.get("updated_at", ""), "")) if mapping.get("updated_at") else ""
            source_url = _extract(raw_entry.get(mapping.get("source_url", ""), "")) if mapping.get("source_url") else ""

            if external_id in prev:
                # Cheap change check without re-storing: compare against hash
                # stored in the previous cursor.
                candidate_hash = StandardItem(external_id=external_id, content=content).content_hash
                if prev[external_id] == candidate_hash:
                    unchanged.append(external_id)
                    index[external_id] = prev[external_id]
                    continue

            item = StandardItem(
                external_id=external_id,
                title=title or external_id,
                content=content,
                source_url=source_url[:500],
                updated_at=updated_at[:40],
                metadata={"path": external_id, "ext": ".txt", "via": "http_api"},
            )
            index[external_id] = item.content_hash
            items.append(item)

        deleted = [ext_id for ext_id in prev if ext_id not in index]
        next_cursor = json.dumps({"ids": index}, ensure_ascii=False, sort_keys=True)
        return SyncResult(
            items=items,
            next_cursor=next_cursor,
            deleted_external_ids=deleted,
            unchanged_external_ids=unchanged,
        )


def _register() -> None:
    from app.connectors import registry
    if not registry.is_registered("http_api"):
        registry.register("http_api", HttpApiConnector)


_register()
