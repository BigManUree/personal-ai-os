"""Background scheduler for read-only data-source sync (3.5.3 batch 2).

Design constraints (local single-user, single uvicorn process):
* scheduling is OPT-IN per source and OFF by default;
* one background asyncio task polls due sources and runs them SERIALLY, so a
  misbehaving/rate-limited connector cannot fan out into concurrent requests;
* no distributed lock / cron syntax / webhooks (single process only);
* scheduling is pure coordination. It reuses the same sync service and sync
  logs as a manual "立即同步", and never fetches data itself.

The pure helpers (``next_run_after_success`` / ``next_run_after_failure``) are
kept free of DB/async so they are trivially unit-tested without sleeping.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 7 * 24 * 60          # 10080 = one week
MAX_BACKOFF_MINUTES = 24 * 60               # cap delay at one day
DEFAULT_INTERVAL_MINUTES = 60

logger = logging.getLogger("personal_ai_os.scheduler")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalise_interval(minutes: int) -> int:
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise ValueError("同步间隔必须是整数分钟")
    if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
        raise ValueError(f"同步间隔必须在 {MIN_INTERVAL_MINUTES}..{MAX_INTERVAL_MINUTES} 分钟之间")
    return minutes


def next_run_after_success(interval_minutes: int, *, now: datetime | None = None) -> str:
    normalise_interval(interval_minutes)
    now = now or utc_now()
    return iso(now + timedelta(minutes=interval_minutes))


def next_run_after_failure(
    interval_minutes: int,
    consecutive_failures: int,
    *,
    now: datetime | None = None,
) -> str:
    """Exponential backoff: interval * 2**(failures-1), capped at 24h."""
    normalise_interval(interval_minutes)
    failures = max(1, int(consecutive_failures))
    delay = min(interval_minutes * (2 ** (failures - 1)), MAX_BACKOFF_MINUTES)
    now = now or utc_now()
    return iso(now + timedelta(minutes=delay))


# Callable returning the list of due source ids (DB access lives in main.py).
DueProvider = Callable[[], Iterable[int]]
# Callable performing one sync exactly like the HTTP endpoint.
SyncRunner = Callable[[int], Awaitable[dict]]


class ConnectorScheduler:
    def __init__(
        self,
        due_provider: DueProvider,
        sync_runner: SyncRunner,
        *,
        poll_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._due = due_provider
        self._run = sync_runner
        self._poll = max(1.0, float(poll_seconds))
        self._sleep = sleep or asyncio.sleep
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._tick_lock = asyncio.Lock()

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="connector-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ loop
    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed; continuing")
            try:
                await asyncio.wait_for(self._sleep(self._poll), timeout=self._poll + 1)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def tick(self) -> dict[str, list[int]]:
        """Run every currently due source serially. Safe to call in tests."""
        if self._tick_lock.locked():
            return {"ran": [], "failed": []}
        async with self._tick_lock:
            ran: list[int] = []
            failed: list[int] = []
            try:
                due_ids = list(await asyncio.to_thread(self._due))
            except Exception:
                logger.exception("failed to list due sources")
                return {"ran": ran, "failed": failed}
            for source_id in due_ids:
                if self._stopping.is_set():
                    break
                try:
                    result = await self._run(source_id)
                    if isinstance(result, dict) and result.get("status") == "success":
                        ran.append(source_id)
                    else:
                        failed.append(source_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("scheduled sync failed source=%s", source_id)
                    failed.append(source_id)
            return {"ran": ran, "failed": failed}
