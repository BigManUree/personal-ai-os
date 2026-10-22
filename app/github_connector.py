"""Read-only GitHub repository connector (Personal AI OS 1.2, 3.5.2 batch 2).

Scope (deliberately narrow):
* one explicitly authorised repository (owner/name), never the whole account;
* read-only access via a fine-grained PAT stored in the secret store;
* syncs the root README*, Markdown/TXT/PDF/DOCX under ``docs/`` and, when
  enabled, recent commit messages;
* manual sync only (no scheduler, no webhook).

Security:
* the HTTP base host is hard-coded to api.github.com (SSRF allow-list);
* redirects are not followed, URLs are built by code, never from remote data;
* timeouts and response/file size limits are enforced;
* the token is only ever sent in the Authorization header and never placed
  into items, metadata, cursors, logs or errors.

Network access is injected through :func:`build_transport` so tests can use
``httpx.MockTransport`` with zero real network calls.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from app import document_parse
from app.connectors import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorFetchError,
    StandardItem,
    SyncResult,
)

logger = logging.getLogger("personal_ai_os.github_connector")

API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
MAX_RESPONSE_BYTES = 6_000_000   # per single API response
MAX_FILE_BYTES = 1_000_000       # raw blob size (contents API hard limit ~1MB)
MAX_FILES_PER_SYNC = 100
MAX_COMMITS = 50
DEFAULT_COMMITS = 20
ALLOWED_DOC_EXTS = {".md", ".txt", ".pdf", ".docx"}

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._\-]{0,37}[A-Za-z0-9])?$|^[A-Za-z0-9]$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._\-]{1,100}$")
_README_RE = re.compile(r"^readme(?:\.[A-Za-z0-9]+)?$", re.IGNORECASE)

VALID_INCLUDES = {"readme", "docs", "commits"}


def build_transport() -> httpx.BaseTransport | None:
    """Return a custom httpx transport. Real runs return None (TCP transport).

    Tests monkeypatch this function to inject an ``httpx.MockTransport``.
    """
    return None


def _owner_repo(config: dict[str, Any]) -> tuple[str, str]:
    owner = str(config.get("owner", "")).strip()
    repo = str(config.get("repo", "")).strip()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not _OWNER_RE.match(owner):
        raise ConnectorConfigError("owner 非法（仅允许字母数字与 ._-，最长 39）")
    if not _REPO_RE.match(repo) or repo in (".", ".."):
        raise ConnectorConfigError("repo 非法（仅允许字母数字与 ._-，最长 100）")
    return owner, repo


def _includes(config: dict[str, Any]) -> set[str]:
    raw = config.get("include", ["readme", "docs"])
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
        raise ConnectorConfigError("include 必须是非空字符串数组，可选 readme/docs/commits")
    result = {x.strip().lower() for x in raw}
    if not result <= VALID_INCLUDES:
        raise ConnectorConfigError(f"include 仅支持 {sorted(VALID_INCLUDES)}")
    return result


def _commits_limit(config: dict[str, Any]) -> int:
    value = config.get("commits_limit", DEFAULT_COMMITS)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_COMMITS:
        raise ConnectorConfigError(f"commits_limit 必须是 1..{MAX_COMMITS} 的整数")
    return value


class GitHubConnector(BaseConnector):
    """Read-only sync of one authorised GitHub repository's docs/commits."""

    kind = "github"
    label = "GitHub 仓库（只读）"

    # ------------------------------------------------------------------ setup
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "personal-ai-os/0.3",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
            transport=build_transport(),
        )

    async def validate_config(self) -> None:
        owner, repo = _owner_repo(self.config)
        _includes(self.config)
        _commits_limit(self.config)
        if not self.secret.strip():
            raise ConnectorConfigError("尚未配置 GitHub 访问令牌（请在数据源创建后设置凭据）")
        async with self._client() as client:
            await self._get_json(client, f"/repos/{owner}/{repo}", allow_404_config=True)

    # ----------------------------------------------------------------- helpers
    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        allow_404_config: bool = False,
    ) -> Any:
        try:
            resp = await client.get(path)
        except httpx.TimeoutException as exc:
            raise ConnectorFetchError("GitHub 请求超时") from exc
        except httpx.HTTPError as exc:
            raise ConnectorFetchError(f"GitHub 网络错误：{type(exc).__name__}") from exc

        if resp.status_code == 401:
            raise ConnectorAuthError("GitHub 令牌无效或已过期（401）")
        if resp.status_code == 403:
            if resp.headers.get("x-ratelimit-remaining") == "0":
                raise ConnectorFetchError("GitHub API 限流，请稍后重试")
            raise ConnectorAuthError("GitHub 令牌无权访问该资源（403）")
        if resp.status_code == 404:
            if allow_404_config:
                raise ConnectorConfigError("仓库不存在或令牌无权访问（404）")
            raise ConnectorFetchError("GitHub 资源不存在（404）")
        if resp.status_code >= 500:
            raise ConnectorFetchError(f"GitHub 服务暂时异常（{resp.status_code}）")
        if resp.status_code != 200:
            raise ConnectorFetchError(f"GitHub 返回非预期状态码 {resp.status_code}")
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise ConnectorFetchError("GitHub 响应超过大小上限")
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorFetchError("GitHub 返回了无法解析的 JSON") from exc

    @staticmethod
    def _decode_content(payload: dict[str, Any], path: str) -> bytes:
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise ConnectorFetchError(f"文件 {path} 不是可读取的 base64 内容（可能过大）")
        try:
            raw = base64.b64decode(payload["content"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConnectorFetchError(f"文件 {path} 内容解码失败") from exc
        if len(raw) > MAX_FILE_BYTES:
            raise ConnectorFetchError(f"文件 {path} 超过单文件大小上限")
        return raw

    # -------------------------------------------------------------------- sync
    async def fetch_changes(self, cursor: str | None = None) -> SyncResult:
        owner, repo = _owner_repo(self.config)
        includes = _includes(self.config)
        limit = _commits_limit(self.config)

        previous: dict[str, Any] = {}
        if cursor:
            try:
                parsed = json.loads(cursor)
                if isinstance(parsed, dict) and isinstance(parsed.get("files"), dict):
                    previous = parsed
            except (json.JSONDecodeError, TypeError):
                previous = {}
        prev_files: dict[str, str] = previous.get("files", {})
        prev_commit_date = previous.get("last_commit_date") or None

        async with self._client() as client:
            repo_info = await self._get_json(client, f"/repos/{owner}/{repo}", allow_404_config=True)
            branch = self.config.get("branch") or repo_info.get("default_branch") or "main"
            if not isinstance(branch, str) or not re.match(r"^[A-Za-z0-9._\-/]{1,200}$", branch):
                raise ConnectorConfigError("分支名非法")

            tree = await self._get_json(
                client, f"/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
            )
            entries = tree.get("tree")
            if not isinstance(entries, list):
                raise ConnectorFetchError("GitHub tree 响应结构异常")

            eligible: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                path = entry.get("path")
                if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
                    continue
                lower = path.lower()
                ext = "." + lower.rsplit(".", 1)[-1] if "." in path else ""
                wanted = (
                    ("readme" in includes and "/" not in path and _README_RE.match(path))
                    or ("docs" in includes and lower.startswith("docs/") and ext in ALLOWED_DOC_EXTS)
                )
                if wanted:
                    eligible.append({"path": path, "sha": str(entry.get("sha") or ""), "ext": ext})
                if len(eligible) >= MAX_FILES_PER_SYNC:
                    break

            index: dict[str, str] = {e["path"]: e["sha"] for e in eligible}

            items: list[StandardItem] = []
            unchanged: list[str] = []
            bytes_fetched = 0

            for entry in eligible:
                path, sha = entry["path"], entry["sha"]
                if prev_files.get(path) == sha:
                    unchanged.append(path)
                    continue
                payload = await self._get_json(
                    client,
                    f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}?ref={quote(branch, safe='')}",
                )
                data = self._decode_content(payload, path)
                bytes_fetched += len(data)
                if bytes_fetched > MAX_RESPONSE_BYTES:
                    raise ConnectorFetchError("本次同步拉取内容总量超过上限")
                content = self._render_file(path, data)
                if content is None:
                    continue
                items.append(
                    StandardItem(
                        external_id=path,
                        title=path,
                        content=content,
                        source_url=f"{WEB_BASE}/{owner}/{repo}/blob/{branch}/{path}",
                        metadata={"path": path, "sha": sha, "size": len(data), "ext": entry["ext"]},
                    )
                )

            truncated = bool(tree.get("truncated"))
            if truncated:
                # GitHub caps recursive trees at 100k entries; do not pretend
                # the snapshot is complete. The flag is persisted in the cursor.
                logger.warning("github tree truncated for %s/%s; some files may be missing", owner, repo)
            next_cursor = {
                "files": index,
                "last_commit_date": prev_commit_date,
                "branch": branch,
                "truncated": truncated,
            }

            if "commits" in includes:
                commits_item, newest_date, had_commits = await self._fetch_commits(
                    client, owner, repo, branch, prev_commit_date, limit
                )
                if commits_item is not None:
                    items.append(commits_item)
                    index["__commits__/recent.md"] = commits_item.content_hash
                    next_cursor["last_commit_date"] = newest_date
                elif "__commits__/recent.md" in prev_files:
                    unchanged.append("__commits__/recent.md")
                    index["__commits__/recent.md"] = prev_files["__commits__/recent.md"]

        deleted = [p for p in prev_files if p not in index]
        return SyncResult(
            items=items,
            next_cursor=json.dumps(next_cursor, ensure_ascii=False, sort_keys=True),
            deleted_external_ids=deleted,
            unchanged_external_ids=unchanged,
        )

    @staticmethod
    def _render_file(path: str, data: bytes) -> str | None:
        lower = path.lower()
        if lower.endswith((".md", ".txt")):
            try:
                return data.decode("utf-8").strip() or None
            except UnicodeDecodeError:
                try:
                    return data.decode("gb18030").strip() or None
                except UnicodeDecodeError:
                    return None
        if lower.endswith((".pdf", ".docx")):
            pages = document_parse.parse_document(path, data)
            text = "\n\n".join(page.text for page in pages).strip()
            return text or None
        return None

    async def _fetch_commits(
        self,
        client: httpx.AsyncClient,
        owner: str,
        repo: str,
        branch: str,
        since: str | None,
        limit: int,
    ) -> tuple[StandardItem | None, str | None, bool]:
        query = f"sha={branch}&per_page={limit}"
        if since:
            query += f"&since={since}"
        data = await self._get_json(client, f"/repos/{owner}/{repo}/commits?{query}")
        if not isinstance(data, list) or not data:
            return None, since, False

        lines = [f"# {owner}/{repo} 最近提交记录（分支 {branch}）", ""]
        newest = since
        for entry in data:
            if not isinstance(entry, dict):
                continue
            commit = entry.get("commit") or {}
            author = commit.get("author") or {}
            date = author.get("date") or ""
            message = str(commit.get("message") or "").strip().splitlines()[0] if commit.get("message") else ""
            sha = str(entry.get("sha") or "")[:10]
            name = str(author.get("name") or "unknown")
            lines.append(f"- {date} `{sha}` {name}: {message}")
            if date and (newest is None or date > newest):
                newest = date
        if newest is None:
            newest = datetime.now(timezone.utc).isoformat(timespec="seconds")

        content = "\n".join(lines).strip()
        item = StandardItem(
            external_id="__commits__/recent.md",
            title=f"{owner}/{repo} 最近提交记录",
            content=content,
            source_url=f"{WEB_BASE}/{owner}/{repo}/commits/{branch}",
            metadata={"path": "__commits__/recent.md", "kind": "commits", "ext": ".md", "count": len(data)},
        )
        return item, newest, True


def _register() -> None:
    from app.connectors import registry
    if not registry.is_registered("github"):
        registry.register("github", GitHubConnector)


_register()
