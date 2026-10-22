"""Opt-in lifecycle service for the fake-test notification delivery worker.

The asyncio task owns timing only. Synchronous SQLite work and the fake channel
call run serially on one worker. Cross-process claiming is enforced by a
conditional SQLite UPDATE in ``notification_delivery``; this service never
loads a real email, webhook, desktop, SMS, or cloud channel.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import notification_delivery as delivery

logger = logging.getLogger("personal_ai_os.notification_delivery")

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_STOPPED_ACTIVE_DELIVERY = "stopped_with_active_delivery"
STATE_FAILED = "failed"

ENABLED_ENV = "NOTIFICATION_DELIVERY_ENABLED"
POLL_ENV = "NOTIFICATION_DELIVERY_POLL_SECONDS"
SHUTDOWN_ENV = "NOTIFICATION_DELIVERY_SHUTDOWN_TIMEOUT_SECONDS"

DEFAULT_POLL_SECONDS = 30.0
MIN_CONFIGURED_POLL_SECONDS = 5.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 35.0

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

BatchRunner = Callable[..., dict[str, Any]]


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
    source = os.environ if env is None else env
    return Config(
        enabled=_parse_bool(source.get(ENABLED_ENV)),
        poll_seconds=_parse_float(
            POLL_ENV, source.get(POLL_ENV), DEFAULT_POLL_SECONDS,
            minimum=MIN_CONFIGURED_POLL_SECONDS, maximum=3600.0,
        ),
        shutdown_timeout_seconds=_parse_float(
            SHUTDOWN_ENV, source.get(SHUTDOWN_ENV), DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
            minimum=1.0, maximum=300.0,
        ),
    )


def default_snapshot(*, enabled: bool = False) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "state": STATE_STOPPED,
        "loop_running": False,
        "active_delivery": False,
        "channel": delivery.CHANNEL_FAKE_TEST,
        "last_result": "",
        "last_status": "",
        "last_error": "",
        "last_run_at": "",
        "last_success_at": "",
        "consecutive_database_errors": 0,
        "processed_count": 0,
        "recovered_count": 0,
        "shutdown_timed_out": False,
    }


def run_delivery_batch(
    db_path: str | Path,
    channel: delivery.NotificationChannel,
    *,
    worker: str,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    if stop_event is not None and stop_event.is_set():
        raise delivery.NotificationError("stopped", "通知投递器正在关闭", status_code=503)
    return delivery.process_delivery_batch(db_path, channel, worker=worker)


class NotificationDeliveryService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        enabled: bool = True,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        initial_delay: float = 0.0,
        channel: delivery.NotificationChannel | None = None,
        runner: BatchRunner | None = None,
        worker_name: str = "",
    ) -> None:
        selected_channel = channel or delivery.FakeNotificationChannel()
        if getattr(selected_channel, "name", "") != delivery.CHANNEL_FAKE_TEST:
            raise ValueError("本阶段通知投递器只允许 fake_test 测试渠道")
        self._db_path = Path(db_path)
        self._channel = selected_channel
        self._enabled = bool(enabled)
        self._poll = max(0.01, float(poll_seconds))
        self._initial_delay = max(0.0, float(initial_delay))
        self._shutdown_timeout = max(0.01, float(shutdown_timeout_seconds))
        self._runner = runner or run_delivery_batch
        self._worker = (worker_name or delivery.default_worker_name())[:120]
        self._state = STATE_STOPPED
        self._task: asyncio.Task | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._active_future: Future[dict[str, Any]] | None = None
        self._stopping = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._batch_lock = asyncio.Lock()
        self._stop_event = threading.Event()
        self._database_errors = 0
        self._snapshot = default_snapshot(enabled=self._enabled)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def active_delivery_future(self) -> Future[dict[str, Any]] | None:
        future = self._active_future
        return future if future is not None and not future.done() else None

    def snapshot(self) -> dict[str, Any]:
        result = dict(self._snapshot)
        result.update({
            "state": self._state,
            "loop_running": self.is_running(),
            "active_delivery": self.active_delivery_future() is not None,
            "consecutive_database_errors": self._database_errors,
        })
        return result

    async def start(self) -> None:
        if not self._enabled:
            self._state = STATE_STOPPED
            return
        async with self._lifecycle_lock:
            if self.is_running():
                return
            if self.active_delivery_future() is not None:
                raise RuntimeError("上一个通知投递仍在执行，不能启动新循环")
            self._state = STATE_STARTING
            self._stopping.clear()
            self._stop_event.clear()
            try:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="notification-delivery",
                )
                self._task = asyncio.create_task(self._loop(), name="notification-delivery")
            except BaseException:
                self._cleanup_executor(wait=False)
                self._state = STATE_FAILED
                logger.exception("notification delivery startup failed")
                raise
            self._state = STATE_RUNNING
            logger.info(
                "notification delivery service started",
                extra={"poll_seconds": self._poll, "channel": delivery.CHANNEL_FAKE_TEST},
            )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._state == STATE_STOPPED and not self.is_running() \
                    and self.active_delivery_future() is None:
                return
            self._state = STATE_STOPPING
            self._stopping.set()
            self._stop_event.set()
            task = self._task
            active_future = self.active_delivery_future()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            timed_out = False
            active_future = active_future or self.active_delivery_future()
            if active_future is not None:
                wrapper = asyncio.wrap_future(active_future)
                wrapper.add_done_callback(
                    lambda item: item.exception() if not item.cancelled() else None
                )
                try:
                    await asyncio.wait_for(asyncio.shield(wrapper), timeout=self._shutdown_timeout)
                except TimeoutError:
                    timed_out = True
                    self._snapshot["shutdown_timed_out"] = True
                    logger.error(
                        "notification delivery shutdown timed out; fake channel call remains active",
                        extra={"timeout_seconds": self._shutdown_timeout},
                    )
                except BaseException:
                    active_future.exception()

            self._task = None
            self._cleanup_executor(wait=self.active_delivery_future() is None)
            if self.active_delivery_future() is None:
                self._state = STATE_STOPPED
                self._snapshot["shutdown_timed_out"] = (
                    self._snapshot["shutdown_timed_out"] or timed_out
                )
            else:
                self._state = STATE_STOPPED_ACTIVE_DELIVERY
            logger.info("notification delivery service stopped", extra={"state": self._state})

    def _cleanup_executor(self, *, wait: bool) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None

    async def tick_once(self) -> dict[str, Any]:
        """Execute one batch serially; primarily for tests."""
        async with self._batch_lock:
            temporary = self._executor
            if temporary is None:
                temporary = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="notification-delivery-once",
                )
            try:
                return await self._submit_batch(temporary)
            finally:
                if self._executor is None:
                    temporary.shutdown(wait=True, cancel_futures=False)

    def _execute_batch_job(self) -> dict[str, Any]:
        return self._runner(
            self._db_path, self._channel, worker=self._worker,
            stop_event=self._stop_event,
        )

    @staticmethod
    def _consume_future(future: Future) -> None:
        try:
            future.exception()
        except InvalidStateError:
            pass

    async def _submit_batch(self, executor: ThreadPoolExecutor) -> dict[str, Any]:
        future = executor.submit(self._execute_batch_job)
        started = time.perf_counter()
        self._active_future = future
        future.add_done_callback(self._consume_future)
        wrapper = asyncio.wrap_future(future)
        wrapper.add_done_callback(
            lambda item: item.exception() if not item.cancelled() else None
        )
        self._snapshot["last_run_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        try:
            result = await asyncio.shield(wrapper)
            self._record_success(result, started=started)
            return result
        except asyncio.CancelledError:
            if future.done():
                self._active_future = None
            raise
        except BaseException as exc:
            self._record_failure(exc, started=started)
            raise
        finally:
            if future.done():
                self._active_future = None

    def _record_success(self, result: dict[str, Any], *, started: float) -> None:
        status = str(result.get("result_status") or result.get("status") or "idle")
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000)
        self._database_errors = 0
        self._snapshot.update({
            "last_result": status,
            "last_status": status,
            "last_error": str(result.get("last_error", "") or ""),
            "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "processed_count": int(self._snapshot["processed_count"]) + int(result.get("processed", 0)),
            "recovered_count": int(self._snapshot["recovered_count"]) + int(result.get("recovered", 0)),
        })
        if status == delivery.STATUS_SUCCEEDED:
            self._snapshot["last_success_at"] = self._snapshot["last_run_at"]
        logger.info(
            "notification delivery batch completed",
            extra={"status": status, "elapsed_ms": elapsed_ms},
        )

    def _record_failure(self, exc: BaseException, *, started: float) -> None:
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000)
        if isinstance(exc, sqlite3.Error):
            result_name = "database_error"
            self._database_errors += 1
        else:
            result_name = "failed"
        error = delivery.sanitize_error_message(exc)
        self._snapshot.update({
            "last_result": result_name,
            "last_status": result_name,
            "last_error": error,
            "last_run_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        })
        logger.warning(
            "notification delivery batch failed",
            extra={"result": result_name, "elapsed_ms": elapsed_ms},
        )

    async def _loop(self) -> None:
        try:
            if self._initial_delay:
                await asyncio.sleep(self._initial_delay)
            while not self._stopping.is_set():
                await self._batch_lock.acquire()
                try:
                    if self._stopping.is_set():
                        break
                    if self._executor is None:
                        raise RuntimeError("通知投递执行器不可用")
                    try:
                        await self._submit_batch(self._executor)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("notification delivery batch failed")
                finally:
                    self._batch_lock.release()
                sleep_task = asyncio.create_task(asyncio.sleep(self._poll))
                stop_task = asyncio.create_task(self._stopping.wait())
                try:
                    await asyncio.wait({sleep_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    sleep_task.cancel()
                    stop_task.cancel()
        except asyncio.CancelledError:
            raise

