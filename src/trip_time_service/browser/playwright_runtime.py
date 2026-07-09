from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

DEFAULT_LOCALE = "ko-KR"
DEFAULT_VIEWPORT: dict[str, int] = {"width": 1920, "height": 1080}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class PlaywrightLaunchOptions:
    headless: bool = True
    locale: str = DEFAULT_LOCALE
    viewport: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_VIEWPORT))
    user_agent: str = DEFAULT_USER_AGENT
    user_data_dir: str | None = None


@dataclass(frozen=True)
class PlaywrightCloseResult:
    timed_out: bool
    close_error: Exception | None = None


@dataclass
class PlaywrightBrowserSession:
    playwright: Playwright
    browser: Browser | None
    context: BrowserContext
    page: Page

    def close(self, *, close_timeout_seconds: float = 5.0) -> PlaywrightCloseResult:
        timed_out = False
        close_error: Exception | None = None

        try:
            self.page.close()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            close_error = close_error or exc

        context_result = close_context_with_timeout(
            self.context,
            close_timeout_seconds=close_timeout_seconds,
            close_thread_name="playwright-context-close",
        )
        timed_out = timed_out or context_result.timed_out
        close_error = close_error or context_result.close_error

        if self.browser is not None:
            browser_result = close_browser_with_timeout(
                self.browser,
                close_timeout_seconds=close_timeout_seconds,
                close_thread_name="playwright-browser-close",
            )
            timed_out = timed_out or browser_result.timed_out
            close_error = close_error or browser_result.close_error

        try:
            self.playwright.stop()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            close_error = close_error or exc

        return PlaywrightCloseResult(timed_out=timed_out, close_error=close_error)


def launch_browser_session(
    options: PlaywrightLaunchOptions | None = None,
) -> PlaywrightBrowserSession:
    opts = options or PlaywrightLaunchOptions()
    manager = sync_playwright()
    playwright = manager.start()

    if opts.user_data_dir:
        context = playwright.chromium.launch_persistent_context(
            opts.user_data_dir,
            headless=opts.headless,
            locale=opts.locale,
            viewport=opts.viewport,
            user_agent=opts.user_agent,
        )
        page = context.new_page()
        return PlaywrightBrowserSession(
            playwright=playwright,
            browser=None,
            context=context,
            page=page,
        )

    browser = playwright.chromium.launch(headless=opts.headless)
    context = browser.new_context(
        locale=opts.locale,
        viewport=opts.viewport,
        user_agent=opts.user_agent,
    )
    page = context.new_page()
    return PlaywrightBrowserSession(
        playwright=playwright,
        browser=browser,
        context=context,
        page=page,
    )


def _taskkill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _extract_browser_process(target: Any) -> Any | None:
    impl = getattr(target, "_impl_obj", None)
    if impl is None:
        return None
    process = getattr(impl, "_process", None)
    if process is not None:
        return process
    # Persistent contexts may expose the browser process via browser.
    browser = getattr(impl, "_browser", None) or getattr(target, "browser", None)
    if browser is None:
        return None
    browser_impl = getattr(browser, "_impl_obj", browser)
    return getattr(browser_impl, "_process", None)


def force_kill_playwright_process(target: Any) -> None:
    process = _extract_browser_process(target)
    if process is None:
        return

    pid = getattr(process, "pid", None)
    try:
        if process.poll() is not None:
            return
        if isinstance(pid, int):
            _taskkill_pid_tree(pid)
            return
        process.kill()
        process.wait(timeout=1.0)
    except Exception:
        pass


def _close_with_timeout(
    target: Any,
    *,
    close_timeout_seconds: float,
    close_thread_name: str,
) -> PlaywrightCloseResult:
    close_errors: list[Exception] = []
    close_done = threading.Event()

    def _run_close() -> None:
        try:
            target.close()
        except Exception as exc:
            close_errors.append(exc)
        finally:
            close_done.set()

    close_thread = threading.Thread(
        target=_run_close,
        name=close_thread_name,
        daemon=True,
    )
    close_thread.start()
    if not close_done.wait(timeout=close_timeout_seconds):
        force_kill_playwright_process(target)
        return PlaywrightCloseResult(timed_out=True)
    if close_errors:
        return PlaywrightCloseResult(
            timed_out=False,
            close_error=close_errors[0],
        )
    return PlaywrightCloseResult(timed_out=False)


def close_browser_with_timeout(
    browser: Any,
    *,
    close_timeout_seconds: float,
    close_thread_name: str,
) -> PlaywrightCloseResult:
    return _close_with_timeout(
        browser,
        close_timeout_seconds=close_timeout_seconds,
        close_thread_name=close_thread_name,
    )


def close_context_with_timeout(
    context: Any,
    *,
    close_timeout_seconds: float,
    close_thread_name: str,
) -> PlaywrightCloseResult:
    return _close_with_timeout(
        context,
        close_timeout_seconds=close_timeout_seconds,
        close_thread_name=close_thread_name,
    )
