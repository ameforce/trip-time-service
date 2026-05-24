from __future__ import annotations

import threading
import time

from trip_time_service.chrome_driver import (
    build_chrome_options,
    close_webdriver_with_timeout,
)


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 1234
        self._polled = False
        self.killed = False
        self.wait_timeout: float | None = None

    def poll(self):
        return None if not self._polled else 0

    def kill(self) -> None:
        self.killed = True
        self._polled = True

    def wait(self, timeout: float) -> None:
        self.wait_timeout = timeout


class _FakeService:
    def __init__(self, process: _FakeProcess) -> None:
        self.process = process


class _FakeDriver:
    def __init__(self, *, quit_behavior: str = "ok") -> None:
        self.service = _FakeService(_FakeProcess())
        self.quit_behavior = quit_behavior
        self.quit_calls = 0
        self.quit_started = threading.Event()

    def quit(self) -> None:
        self.quit_calls += 1
        self.quit_started.set()
        if self.quit_behavior == "raise":
            raise RuntimeError("quit failed")
        if self.quit_behavior == "slow":
            time.sleep(0.1)


def test_build_chrome_options_sets_common_flags() -> None:
    options = build_chrome_options(
        headless=True,
        window_size="1280,960",
        user_agent="TestAgent/1.0",
        chrome_binary_path="C:\\Chrome\\chrome.exe",
        chrome_user_data_dir="C:\\profiles\\worker-1",
    )

    assert options.binary_location == "C:\\Chrome\\chrome.exe"
    assert "--headless=new" in options.arguments
    assert "--disable-gpu" in options.arguments
    assert "--no-sandbox" not in options.arguments
    assert "--disable-dev-shm-usage" in options.arguments
    assert "--lang=ko-KR" in options.arguments
    assert "--window-size=1280,960" in options.arguments
    assert "--user-agent=TestAgent/1.0" in options.arguments
    assert "--user-data-dir=C:\\profiles\\worker-1" in options.arguments


def test_build_chrome_options_can_opt_into_no_sandbox() -> None:
    options = build_chrome_options(
        headless=True,
        window_size="1280,960",
        user_agent="TestAgent/1.0",
        no_sandbox=True,
    )

    assert "--no-sandbox" in options.arguments


def test_close_webdriver_with_timeout_force_kills_hung_driver(monkeypatch) -> None:
    driver = _FakeDriver(quit_behavior="slow")
    killed: list[_FakeDriver] = []

    monkeypatch.setattr(
        "trip_time_service.chrome_driver.force_kill_webdriver_process",
        lambda current_driver: killed.append(current_driver),
    )

    result = close_webdriver_with_timeout(
        driver,
        quit_timeout_seconds=0.01,
        quit_thread_name="test-hung-quit",
    )

    assert result.timed_out is True
    assert result.quit_error is None
    assert killed == [driver]


def test_close_webdriver_with_timeout_returns_quit_error() -> None:
    driver = _FakeDriver(quit_behavior="raise")

    result = close_webdriver_with_timeout(
        driver,
        quit_timeout_seconds=0.05,
        quit_thread_name="test-raise-quit",
    )

    assert result.timed_out is False
    assert isinstance(result.quit_error, RuntimeError)
    assert str(result.quit_error) == "quit failed"
