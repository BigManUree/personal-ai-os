"""Opt-in lifecycle service for the deterministic reminder scheduler.

The asyncio task owns wake-up timing and in-process state only. Each synchronous
``reminder_scheduler.run_tick`` SQLite call runs on one dedicated daemon worker;
cross-process mutual exclusion remains the responsibility of the SQLite lease.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue as queue_module
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import reminder_scheduler as scheduler
from app import reminder_scheduler_lock as lease_svc

logger = logging.getLogger("personal_ai_os.reminder_scheduler")

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_STOPPED_ACTIVE_TICK = "stopped_with_active_tick"
STATE_FAILED = "failed"

ENABLED_ENV = "REMINDER_SCHEDULER_ENABLED"
POLL_ENV = "REMINDER_SCHEDULER_POLL_SECONDS"
SHUTDOWN_ENV = "REMINDER_SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS"

DEFAULT_POLL_SECONDS = 60.0
MIN_CONFIGURED_POLL_SECONDS = 30.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 35.0
HEARTBEAT_INTERVAL_SECONDS = 10.0
MAX_TICK_SECONDS = 90.0

# Bounded retry for transient SQLite concurrency errors during lease acquisition.
# Worst case adds 0.6s to one tick, versus a 60s poll interval of skipped work.
TICK_RETRY_ATTEMPTS = 4
TICK_RETRY_BACKOFF_SECONDS = (0.05, 0.15, 0.4)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

TickRunner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    poll_seconds: float = DEFAULT_POLL_SECONDS
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ValueError(
        f"{ENABLED_ENV} 必须是 1/true/yes/on（启用）或 0/false/no/off（关闭）"
    )


def _parse_float(name: str, raw: str | None, default: float,
                 *, minimum: float, maximum: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是秒数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum} 秒之间")
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    """Read scheduler configuration from process environment by default."""
    source = os.environ if env is None else env
    enabled = _parse_bool(source.get(ENABLED_ENV))
    poll_seconds = _parse_float(
        POLL_ENV, source.get(POLL_ENV), DEFAULT_POLL_SECONDS,
        minimum=MIN_CONFIGURED_POLL_SECONDS, maximum=3600.0,
    )
    shutdown_timeout = _parse_float(
        SHUTDOWN_ENV, source.get(SHUTDOWN_ENV), DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        minimum=1.0, maximum=300.0,
    )
    return Config(enabled=enabled, poll_seconds=poll_seconds,
                  shutdown_timeout_seconds=shutdown_timeout)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds") if value else ""


def default_snapshot(*, enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "state": STATE_STOPPED,
        "loop_running": False,
        "active_tick": False,
        "last_result": "",
        "last_status": "",
        "last_error": "",
        "last_tick_at": "",
        "last_success_at": "",
        "consecutive_database_errors": 0,
        "consecutive_busy_count": 0,
        "shutdown_timed_out": False,
    }


class _SingleThreadTickExecutor:
    """A daemon, max-workers-one executor that Python will not wait on at exit."""

    _SENTINEL = object()

    def __init__(self, name: str) -> None:
        self._queue: queue_module.SimpleQueue[tuple[Any, ...]] = queue_module.SimpleQueue()
        self._thread = threading.Thread(
            target=self._worker, name=name, daemon=True,
        )
        self._closed = False
        self._thread.start()

    @property
    def name(self) -> str:
        return self._thread.name

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[dict[str, Any]]:
        if self._closed:
            raise RuntimeError("调度执行器已关闭")
        future: Future[dict[str, Any]] = Future()
        self._queue.put((fn, args, kwargs, future))
        return future

    def shutdown(self, *, join_timeout: float | None = 1.0) -> None:
        if self._closed:
            return
        self._closed = True
        # Sentinel is behind any in-flight tick, so the worker exits after that
        # tick reaches a safe boundary. Callers pass None for non-blocking
        # shutdown; Python cannot safely terminate an arbitrary sqlite3 call.
        self._queue.put((self._SENTINEL, (), {}, None))
        if join_timeout is not None:
            self._thread.join(timeout=join_timeout)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            fn, args, kwargs, future = item
            if fn is self._SENTINEL:
                return
            try:
                future.set_result(fn(*args, **kwargs))
            except InvalidStateError:
                # Asyncio cancellation must never cancel a running synchronous
                # SQLite tick. The service retains a non-cancelled active future.
                pass
            except BaseException as exc:  # propagated through the future
                try:
                    future.set_exception(exc)
                except InvalidStateError:
                    pass


def _run_tick_once(
    db_path: str | Path,
    *,
    run_key: str,
    owner_token: str,
    stop_event: threading.Event | None,
    heartbeat_interval_seconds: float,
    max_tick_seconds: float,
) -> dict[str, Any]:
    connection = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        return scheduler.run_tick(
            connection,
            now=_utc_now(),
            trigger_type=scheduler.TRIGGER_SCHEDULED,
            run_key=run_key,
            owner_token=owner_token,
            clock=_utc_now,
            should_stop=(lambda _stage: stop_event.is_set()) if stop_event is not None else None,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            max_tick_seconds=max_tick_seconds,
        )
    finally:
        connection.close()


def run_scheduled_tick(
    db_path: str | Path,
    *,
    run_key: str | None,
    owner_token: str,
    stop_event: threading.Event | None = None,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    max_tick_seconds: float = MAX_TICK_SECONDS,
) -> dict[str, Any]:
    """Open an isolated SQLite connection and execute one synchronous tick.

    A transient SQLite condition during lease acquisition (BUSY / LOCKED /
    READONLY) is retried on a fresh connection. Nothing has been committed at
    that point, so the retry cannot duplicate work; the same ``run_key`` also
    makes a re-run collapse onto an existing run row if one was already
    recorded. Failing without a retry would defer every due reminder by a full
    poll interval because of a single losing race with another connection.
    """
    if stop_event is not None and stop_event.is_set():
        raise scheduler.SchedulerStopped()
    if run_key is None:
        bucket = int(_utc_now().timestamp() // DEFAULT_POLL_SECONDS)
        run_key = f"scheduled:{bucket}"

    last_error: scheduler.LeaseDatabaseError | None = None
    for attempt in range(TICK_RETRY_ATTEMPTS):
        try:
            return _run_tick_once(
                db_path,
                run_key=run_key,
                owner_token=owner_token,
                stop_event=stop_event,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                max_tick_seconds=max_tick_seconds,
            )
        except scheduler.LeaseDatabaseError as exc:
            if not exc.retryable or attempt + 1 >= TICK_RETRY_ATTEMPTS:
                raise
            last_error = exc
            if stop_event is not None and stop_event.is_set():
                raise scheduler.SchedulerStopped() from exc
            delay = TICK_RETRY_BACKOFF_SECONDS[attempt]
            logger.warning(
                "reminder scheduler tick retrying after transient sqlite error",
                extra={"attempt": attempt + 1, "delay_seconds": delay,
                       "error_code": exc.code},
            )
            time.sleep(delay)
    raise last_error  # pragma: no cover - loop always returns or raises


class ReminderSchedulerService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        enabled: bool = True,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        initial_delay: float = 0.0,
        runner: TickRunner | None = None,
        owner_token: str = "",
    ) -> None:
        self._db_path = Path(db_path)
        self._enabled = bool(enabled)
        self._poll = max(0.01, float(poll_seconds))
        self._initial_delay = max(0.0, float(initial_delay))
        self._shutdown_timeout = max(0.01, float(shutdown_timeout_seconds))
        self._runner = runner or run_scheduled_tick
        self._owner_token = owner_token or lease_svc.default_owner_token()
        self._state = STATE_STOPPED
        self._task: asyncio.Task | None = None
        self._executor: _SingleThreadTickExecutor | None = None
        self._active_future: Future[dict[str, Any]] | None = None
        self._stopping = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._tick_lock = asyncio.Lock()
        self._stop_event = threading.Event()
        self._snapshot = default_snapshot(enabled=self._enabled)
        self._snapshot["state"] = STATE_STOPPED
        self._database_errors = 0
        self._busy_count = 0

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_loop_running(self) -> bool:
        return self.is_running()

    def active_tick_future(self) -> Future[dict[str, Any]] | None:
        future = self._active_future
        return future if future is not None and not future.done() else None

    def snapshot(self) -> dict[str, Any]:
        result = dict(self._snapshot)
        result.update({
            "state": self._state,
            "loop_running": self.is_loop_running(),
            "active_tick": self.active_tick_future() is not None,
            "consecutive_database_errors": self._database_errors,
            "consecutive_busy_count": self._busy_count,
        })
        return result

    async def start(self) -> None:
        if not self._enabled:
            self._state = STATE_STOPPED
            return
        async with self._lifecycle_lock:
            if self.is_running():
                return
            if self.active_tick_future() is not None:
                raise RuntimeError("上一个调度 tick 仍在执行，不能启动新循环")
            self._state = STATE_STARTING
            self._stopping.clear()
            self._stop_event.clear()
            try:
                self._executor = _SingleThreadTickExecutor(
                    f"reminder-scheduler-{uuid.uuid4().hex[:8]}"
                )
                self._task = asyncio.create_task(self._loop(), name="reminder-scheduler")
            except BaseException:
                self._cleanup_executor(join_timeout=0.1)
                self._state = STATE_FAILED
                logger.exception("reminder scheduler startup failed")
                raise
            self._state = STATE_RUNNING
            logger.info(
                "reminder scheduler service started",
                extra={"poll_seconds": self._poll, "state": self._state},
            )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._state in {STATE_STOPPED, STATE_STOPPING, STATE_STOPPED_ACTIVE_TICK} \
                    and not self.is_running() and self.active_tick_future() is None:
                self._state = STATE_STOPPED
                return

            self._state = STATE_STOPPING
            self._stopping.set()
            self._stop_event.set()
            task = self._task
            active_future = self.active_tick_future()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            timed_out = False
            active_future = active_future or self.active_tick_future()
            if active_future is not None:
                wrapper = asyncio.wrap_future(active_future)
                wrapper.add_done_callback(
                    lambda item: item.exception() if not item.cancelled() else None
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wrapper),
                        timeout=self._shutdown_timeout,
                    )
                except TimeoutError:
                    timed_out = True
                    self._snapshot["shutdown_timed_out"] = True
                    logger.error(
                        "reminder scheduler shutdown timed out; daemon tick remains active",
                        extra={"timeout_seconds": self._shutdown_timeout},
                    )
                except BaseException:
                    active_future.exception()

            self._task = None
            if self.active_tick_future() is None:
                self._cleanup_executor(join_timeout=1.0)
                self._state = STATE_STOPPED
                self._snapshot["shutdown_timed_out"] = self._snapshot["shutdown_timed_out"] or timed_out
            else:
                # The daemon worker is deliberately not force-killed. Once the lease
                # TTL passes, guarded writes reject it; the next owner recovers the run.
                if self._executor is not None:
                    # Do not wait here: stop() must remain bounded. The daemon
                    # thread cannot accept new work, and lease TTL/fencing blocks
                    # stale writes after takeover.
                    self._executor.shutdown(join_timeout=0)
                self._state = STATE_STOPPED_ACTIVE_TICK
            logger.info("reminder scheduler service stopped", extra={"state": self._state})

    async def tick_once(self, run_key: str | None = None) -> dict[str, Any]:
        """Execute one tick serially; intended for tests and a future guarded API."""
        async with self._tick_lock:
            temporary = self._executor
            if temporary is None:
                temporary = _SingleThreadTickExecutor(
                    f"reminder-scheduler-once-{uuid.uuid4().hex[:8]}"
                )
            try:
                return await self._submit_tick(temporary, run_key=run_key)
            finally:
                if self._executor is None:
                    temporary.shutdown(join_timeout=1.0)

    @staticmethod
    def _consume_thread_future(future: Future) -> None:
        try:
            future.exception()
        except InvalidStateError:
            pass

    def _execute_tick_job(self, run_key: str) -> dict[str, Any]:
        if self._stop_event.is_set():
            raise scheduler.SchedulerStopped()
        return self._runner(
            self._db_path,
            run_key=run_key,
            owner_token=self._owner_token,
            stop_event=self._stop_event,
        )

    async def _loop(self) -> None:
        try:
            if self._initial_delay:
                await asyncio.sleep(self._initial_delay)
            while not self._stopping.is_set():
                await self._tick_lock.acquire()
                try:
                    if self._stopping.is_set():
                        break
                    try:
                        await self._submit_tick(self._executor, run_key=None)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Failure classification and logging happen in submit;
                        # the control loop should survive one retriable tick.
                        if self._stopping.is_set():
                            break
                finally:
                    self._tick_lock.release()
                sleep_task = asyncio.create_task(asyncio.sleep(self._poll))
                stop_task = asyncio.create_task(self._stopping.wait())
                try:
                    await asyncio.wait({sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    sleep_task.cancel()
                    stop_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder scheduler loop failed")
            raise

    async def _submit_tick(self, executor: _SingleThreadTickExecutor | None,
                           *, run_key: str | None) -> dict[str, Any]:
        if executor is None:
            raise RuntimeError("调度执行器不可用")
        scheduled_key = run_key
        if scheduled_key is None:
            scheduled_at = _utc_now()
            scheduled_key = f"scheduled:{int(scheduled_at.timestamp() // self._poll)}"
        future = executor.submit(
            self._execute_tick_job,
            scheduled_key,
        )
        tick_started = time.perf_counter()
        logger.info(
            "reminder scheduler tick started",
            extra={"run_key": scheduled_key},
        )
        self._active_future = future
        # Retrieve exceptions even when stop() stops waiting before this thread
        # tick reaches its cooperative boundary.
        future.add_done_callback(self._consume_thread_future)
        wrapper = asyncio.wrap_future(future)
        wrapper.add_done_callback(
            lambda item: item.exception() if not item.cancelled() else None
        )
        self._snapshot["last_tick_at"] = _iso(_utc_now())
        try:
            # shield() is essential: cancelling the asyncio control task only
            # stops waiting; it must not cancel a sqlite3 call running on a thread.
            result = await asyncio.shield(wrapper)
            self._record_success(result, started=tick_started)
            return result
        except asyncio.CancelledError:
            if future.done():
                self._active_future = None
            raise
        except BaseException as exc:
            self._record_failure(exc, started=tick_started)
            raise
        finally:
            if future.done():
                self._active_future = None

    def _record_success(self, result: dict[str, Any], *, started: float | None = None) -> None:
        now = _utc_now()
        status = str(result.get("status", "succeeded"))
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0) if started is not None else None
        self._snapshot.update({
            "last_result": status,
            "last_status": status,
            "last_tick_at": _iso(now),
            "last_success_at": _iso(now),
            "last_error": str(result.get("last_error", "") or ""),
            "shutdown_timed_out": False if self._state == STATE_RUNNING else self._snapshot["shutdown_timed_out"],
        })
        self._database_errors = 0
        self._busy_count = 0
        logger.info(
            "reminder scheduler tick completed",
            extra={"status": status, "elapsed_ms": elapsed_ms},
        )

    def _record_failure(self, exc: BaseException, *, started: float | None = None) -> None:
        now = _utc_now()
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0) if started is not None else None
        if isinstance(exc, scheduler.LeaseBusyError):
            result_name, error = "lease_busy", ""
            self._busy_count += 1
        elif isinstance(exc, scheduler.LeaseLostError):
            result_name, error = "lease_lost", scheduler.sanitize_error(exc)
        elif isinstance(exc, (scheduler.LeaseDatabaseError, sqlite3.Error)):
            result_name, error = "database_error", scheduler.sanitize_error(exc)
            self._database_errors += 1
        elif isinstance(exc, scheduler.SchedulerStopped):
            result_name, error = "stopped", scheduler.sanitize_error(exc)
        else:
            result_name, error = "failed", scheduler.sanitize_error(exc)
        self._snapshot.update({
            "last_result": result_name,
            "last_status": result_name,
            "last_tick_at": _iso(now),
            "last_error": error,
        })
        logger.warning(
            "reminder scheduler tick failed",
            extra={
                "result": result_name,
                "error_code": getattr(exc, "code", result_name),
                "elapsed_ms": elapsed_ms,
            },
        )

    def _cleanup_executor(self, *, join_timeout: float | None) -> None:
        if self._executor is not None:
            self._executor.shutdown(join_timeout=join_timeout)
            self._executor = None