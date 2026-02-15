from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trip_time_service.api import routes
from trip_time_service.config import Settings
from trip_time_service.core.models import DriveDuration, Route
from trip_time_service.providers.naver_selenium import (
    NaverMapsSeleniumPoolProvider,
    NaverMapsSeleniumProvider,
)

KST = ZoneInfo("Asia/Seoul")


def _settings() -> Settings:
    return Settings(
        timezone=KST,
        headless=True,
        cache_ttl=timedelta(seconds=300),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_selenium",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
    )


class _BlockingProcess:
    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> int | None:
        return 0 if self.killed else None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return 0


class _BlockingService:
    def __init__(self) -> None:
        self.process = _BlockingProcess()


class _BlockingDriver:
    def __init__(self) -> None:
        self.service = _BlockingService()
        self.quit_started = threading.Event()
        self.quit_released = threading.Event()

    def quit(self) -> None:
        self.quit_started.set()
        self.quit_released.wait(timeout=10.0)


class _AliveWorker:
    def is_alive(self) -> bool:
        return True


class _DeadWorker:
    def is_alive(self) -> bool:
        return False


class _SlowCloseWorker:
    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def terminate_session(self) -> None:
        return

    def close(self) -> None:
        time.sleep(self._delay_seconds)


class _NeverCloseWorker:
    def __init__(self) -> None:
        self.terminated = threading.Event()

    def terminate_session(self) -> None:
        self.terminated.set()

    def close(self) -> None:
        while True:
            time.sleep(0.05)


class _TrackedTerminateWorker:
    def __init__(self) -> None:
        self.terminated = threading.Event()

    def terminate_session(self) -> None:
        self.terminated.set()

    def close(self) -> None:
        time.sleep(0.01)


class _OneShotWorker:
    def __init__(self) -> None:
        self.release_count = 0

    def get_drive_duration(
        self, route: Route, departure_time: datetime,
    ) -> DriveDuration:
        _ = route
        return DriveDuration(
            duration_seconds=600,
            fetched_at=departure_time,
        )

    def release_session(self) -> None:
        self.release_count += 1

    def close(self) -> None:
        return


def test_close_returns_when_driver_quit_hangs(monkeypatch) -> None:
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._DRIVER_QUIT_TIMEOUT_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._CLOSE_LOCK_TIMEOUT_SECONDS",
        0.05,
    )
    provider = NaverMapsSeleniumProvider(_settings())
    blocking_driver = _BlockingDriver()
    provider._driver = blocking_driver  # type: ignore[assignment]

    started = time.monotonic()
    provider.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert blocking_driver.quit_started.wait(timeout=0.1)
    assert blocking_driver.service.process.killed is True
    assert provider._driver is None
    blocking_driver.quit_released.set()


def test_close_returns_when_provider_lock_is_busy(monkeypatch) -> None:
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._CLOSE_LOCK_TIMEOUT_SECONDS",
        0.05,
    )
    provider = NaverMapsSeleniumProvider(_settings())

    acquired = provider._lock.acquire(timeout=0.1)  # type: ignore[attr-defined]
    assert acquired is True
    try:
        started = time.monotonic()
        provider.close()
        elapsed = time.monotonic() - started
    finally:
        provider._lock.release()  # type: ignore[attr-defined]

    assert elapsed < 0.3


def test_iter_stream_events_emits_error_when_worker_stalls(monkeypatch) -> None:
    monkeypatch.setattr(routes, "_STREAM_QUEUE_POLL_SECONDS", 0.01)

    done_marker = object()
    event_queue: queue.Queue[object] = queue.Queue()
    events = list(
        routes._iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=_AliveWorker(),
            idle_timeout_seconds=0.03,
        )
    )

    assert len(events) == 1
    assert events[0]["event"] == "error"


def test_iter_stream_events_stops_on_done_marker() -> None:
    done_marker = object()
    event_queue: queue.Queue[object] = queue.Queue()
    event_queue.put({"event": "plan", "data": {"planned": 1}})
    event_queue.put(done_marker)

    events = list(
        routes._iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=_DeadWorker(),
            idle_timeout_seconds=1.0,
        )
    )

    assert events == [{"event": "plan", "data": {"planned": 1}}]


def test_pool_close_runs_workers_in_parallel() -> None:
    pool_settings = replace(_settings(), naver_session_pool_size=1)
    provider = NaverMapsSeleniumPoolProvider(pool_settings)
    provider._workers = tuple(_SlowCloseWorker(0.05) for _ in range(8))  # type: ignore[assignment]

    started = time.monotonic()
    provider.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25


def test_pool_close_returns_when_one_worker_hangs(monkeypatch) -> None:
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._POOL_CLOSE_TIMEOUT_SECONDS",
        0.08,
    )
    pool_settings = replace(_settings(), naver_session_pool_size=1)
    provider = NaverMapsSeleniumPoolProvider(pool_settings)
    provider._workers = (  # type: ignore[assignment]
        _NeverCloseWorker(),
        _SlowCloseWorker(0.02),
    )

    started = time.monotonic()
    provider.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert provider._workers[0].terminated.is_set() is True  # type: ignore[attr-defined]


def test_pool_close_invokes_bulk_terminate_first(monkeypatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER", "naver_selenium")
    monkeypatch.setattr(
        "trip_time_service.providers.naver_selenium._POOL_BULK_TERMINATE_TIMEOUT_SECONDS",
        0.1,
    )
    pool_settings = replace(_settings(), naver_session_pool_size=1)
    provider = NaverMapsSeleniumPoolProvider(pool_settings)
    tracked_1 = _TrackedTerminateWorker()
    tracked_2 = _TrackedTerminateWorker()
    provider._workers = (tracked_1, tracked_2)  # type: ignore[assignment]

    provider.close()

    assert tracked_1.terminated.is_set() is True
    assert tracked_2.terminated.is_set() is True


def test_pool_get_drive_duration_once_releases_worker_session() -> None:
    pool_settings = replace(_settings(), naver_session_pool_size=1)
    provider = NaverMapsSeleniumPoolProvider(pool_settings)
    worker = _OneShotWorker()
    provider._workers = (worker,)  # type: ignore[assignment]

    duration = provider.get_drive_duration_once(
        Route.of("A", "B"),
        datetime.now(tz=KST),
    )

    assert duration.duration_seconds == 600
    assert worker.release_count == 1


def test_default_naver_parallelism_uses_cpu_count(monkeypatch) -> None:
    from trip_time_service import config

    config.load_settings.cache_clear()
    monkeypatch.setenv("TTS_PROVIDER", "naver_selenium")
    monkeypatch.delenv("TTS_NAVER_SESSION_POOL_SIZE", raising=False)
    monkeypatch.delenv("TTS_RECOMMEND_WORKERS", raising=False)
    monkeypatch.setattr("trip_time_service.config.os.cpu_count", lambda: 32)

    settings = config.load_settings()

    assert settings.naver_session_pool_size == 26
    assert settings.recommend_workers == 26
    config.load_settings.cache_clear()

