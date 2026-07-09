from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from trip_time_service.browser.playwright_runtime import (
    DEFAULT_LOCALE,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    PlaywrightBrowserSession,
    PlaywrightCloseResult,
    PlaywrightLaunchOptions,
    close_browser_with_timeout,
    close_context_with_timeout,
    force_kill_playwright_process,
    launch_browser_session,
)


class _FakeCloseable:
    def __init__(self, *, close_behavior: str = "ok") -> None:
        self.close_behavior = close_behavior
        self.close_calls = 0
        self.close_started = threading.Event()

    def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.close_behavior == "raise":
            raise RuntimeError("close failed")
        if self.close_behavior == "slow":
            time.sleep(0.1)


def test_default_launch_options_match_naver_flow() -> None:
    options = PlaywrightLaunchOptions()

    assert options.headless is True
    assert options.locale == DEFAULT_LOCALE == "ko-KR"
    assert options.viewport == DEFAULT_VIEWPORT == {"width": 1920, "height": 1080}
    assert options.user_agent == DEFAULT_USER_AGENT
    assert "Chrome/120.0.0.0" in options.user_agent
    assert options.user_data_dir is None


def test_launch_browser_session_creates_browser_context_and_page(monkeypatch) -> None:
    fake_page = object()
    fake_context = MagicMock()
    fake_context.new_page.return_value = fake_page
    fake_browser = MagicMock()
    fake_browser.new_context.return_value = fake_context
    fake_chromium = MagicMock()
    fake_chromium.launch.return_value = fake_browser
    fake_playwright = MagicMock()
    fake_playwright.chromium = fake_chromium
    fake_manager = MagicMock()
    fake_manager.start.return_value = fake_playwright

    monkeypatch.setattr(
        "trip_time_service.browser.playwright_runtime.sync_playwright",
        lambda: fake_manager,
    )

    session = launch_browser_session(
        PlaywrightLaunchOptions(headless=True, user_data_dir=None)
    )

    assert isinstance(session, PlaywrightBrowserSession)
    assert session.playwright is fake_playwright
    assert session.browser is fake_browser
    assert session.context is fake_context
    assert session.page is fake_page
    fake_chromium.launch.assert_called_once_with(headless=True)
    fake_browser.new_context.assert_called_once_with(
        locale="ko-KR",
        viewport={"width": 1920, "height": 1080},
        user_agent=DEFAULT_USER_AGENT,
    )
    fake_context.new_page.assert_called_once_with()


def test_launch_browser_session_uses_persistent_context_for_user_data_dir(
    monkeypatch,
) -> None:
    fake_page = object()
    fake_context = MagicMock()
    fake_context.new_page.return_value = fake_page
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context.return_value = fake_context
    fake_playwright = MagicMock()
    fake_playwright.chromium = fake_chromium
    fake_manager = MagicMock()
    fake_manager.start.return_value = fake_playwright

    monkeypatch.setattr(
        "trip_time_service.browser.playwright_runtime.sync_playwright",
        lambda: fake_manager,
    )

    session = launch_browser_session(
        PlaywrightLaunchOptions(
            headless=False,
            user_data_dir="C:\\profiles\\worker-1",
            user_agent="TestAgent/1.0",
        )
    )

    assert session.browser is None
    assert session.context is fake_context
    assert session.page is fake_page
    fake_chromium.launch_persistent_context.assert_called_once_with(
        "C:\\profiles\\worker-1",
        headless=False,
        locale="ko-KR",
        viewport={"width": 1920, "height": 1080},
        user_agent="TestAgent/1.0",
    )


def test_close_browser_with_timeout_force_kills_hung_browser(monkeypatch) -> None:
    browser = _FakeCloseable(close_behavior="slow")
    killed: list[object] = []

    monkeypatch.setattr(
        "trip_time_service.browser.playwright_runtime.force_kill_playwright_process",
        lambda target: killed.append(target),
    )

    result = close_browser_with_timeout(
        browser,
        close_timeout_seconds=0.01,
        close_thread_name="test-hung-browser-close",
    )

    assert isinstance(result, PlaywrightCloseResult)
    assert result.timed_out is True
    assert result.close_error is None
    assert killed == [browser]


def test_close_context_with_timeout_returns_close_error() -> None:
    context = _FakeCloseable(close_behavior="raise")

    result = close_context_with_timeout(
        context,
        close_timeout_seconds=0.05,
        close_thread_name="test-raise-context-close",
    )

    assert result.timed_out is False
    assert isinstance(result.close_error, RuntimeError)
    assert str(result.close_error) == "close failed"


def test_force_kill_playwright_process_kills_browser_process(monkeypatch) -> None:
    process = MagicMock()
    process.poll.return_value = None
    process.pid = 4321
    browser = SimpleNamespace(_impl_obj=SimpleNamespace(_process=process))
    killed_pids: list[int] = []

    def _fake_taskkill(pid: int) -> None:
        killed_pids.append(pid)
        process.poll.return_value = 0

    monkeypatch.setattr(
        "trip_time_service.browser.playwright_runtime._taskkill_pid_tree",
        _fake_taskkill,
    )

    force_kill_playwright_process(browser)

    assert killed_pids == [4321]


def test_session_close_closes_page_context_browser_and_playwright(monkeypatch) -> None:
    page = MagicMock()
    context = MagicMock()
    browser = MagicMock()
    playwright = MagicMock()
    close_calls: list[str] = []

    def _track_close(name: str, target: object):
        def _close(*_args, **_kwargs):
            close_calls.append(name)
            return PlaywrightCloseResult(timed_out=False)

        return _close

    monkeypatch.setattr(
        "trip_time_service.browser.playwright_runtime.close_context_with_timeout",
        _track_close("context", context),
    )
    monkeypatch.setattr(
        "trip_time_service.browser.playwright_runtime.close_browser_with_timeout",
        _track_close("browser", browser),
    )

    session = PlaywrightBrowserSession(
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
    )
    result = session.close(close_timeout_seconds=0.05)

    assert result.timed_out is False
    page.close.assert_called_once_with()
    assert close_calls == ["context", "browser"]
    playwright.stop.assert_called_once_with()
