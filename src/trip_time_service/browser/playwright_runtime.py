from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, TypeVar

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

_T = TypeVar("_T")


@dataclass(frozen=True)
class PlaywrightLaunchOptions:
    headless: bool = True
    locale: str = DEFAULT_LOCALE
    viewport: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_VIEWPORT))
    user_agent: str = DEFAULT_USER_AGENT
    user_data_dir: str | None = None
    chrome_no_sandbox: bool = False


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

        # Skip explicit page.close(); context/browser close covers pages and
        # avoids an unbounded hang before the timeout-protected paths run.
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


def _chromium_launch_args(*, chrome_no_sandbox: bool) -> list[str] | None:
    if not chrome_no_sandbox:
        return None
    return ["--no-sandbox", "--disable-dev-shm-usage"]


def launch_browser_session(
    options: PlaywrightLaunchOptions | None = None,
) -> PlaywrightBrowserSession:
    opts = options or PlaywrightLaunchOptions()
    manager = sync_playwright()
    playwright = manager.start()
    launch_args = _chromium_launch_args(chrome_no_sandbox=opts.chrome_no_sandbox)

    try:
        if opts.user_data_dir:
            launch_kwargs: dict[str, Any] = {
                "headless": opts.headless,
                "locale": opts.locale,
                "viewport": opts.viewport,
                "user_agent": opts.user_agent,
            }
            if launch_args is not None:
                launch_kwargs["args"] = launch_args
            context = playwright.chromium.launch_persistent_context(
                opts.user_data_dir,
                **launch_kwargs,
            )
            page = context.new_page()
            return PlaywrightBrowserSession(
                playwright=playwright,
                browser=None,
                context=context,
                page=page,
            )

        launch_kwargs = {"headless": opts.headless}
        if launch_args is not None:
            launch_kwargs["args"] = launch_args
        browser = playwright.chromium.launch(**launch_kwargs)
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
    except Exception:
        try:
            playwright.stop()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        raise


class PlaywrightOwnerThread:
    """Serialize Playwright create/use/close onto one dedicated thread.

    Playwright's sync API is greenlet-bound to the thread that started it.
    Callers from FastAPI/thread pools must submit work here instead of touching
    a cached session directly.
    """

    def __init__(self, name: str = "playwright-owner") -> None:
        self._jobs: queue.Queue[
            tuple[Callable[[], Any], Future[Any]] | None
        ] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._closed = False
        self._thread_ident: int | None = None
        self._thread.start()

    @property
    def thread_ident(self) -> int | None:
        return self._thread_ident

    def call(self, fn: Callable[[], _T], *, timeout: float | None = None) -> _T:
        if self._closed:
            raise RuntimeError("Playwright owner thread is closed")
        if threading.get_ident() == self._thread_ident:
            return fn()

        future: Future[_T] = Future()
        self._jobs.put((fn, future))
        return future.result(timeout=timeout)

    def close(self, *, join_timeout_seconds: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._jobs.put(None)
        self._thread.join(timeout=join_timeout_seconds)

    def _run(self) -> None:
        self._thread_ident = threading.get_ident()
        while True:
            item = self._jobs.get()
            if item is None:
                return
            fn, future = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except Exception as exc:  # noqa: BLE001 - propagate to caller
                    future.set_exception(exc)


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


def _interrupt_thread(thread_ident: int, exc_type: type[BaseException]) -> None:
    """Best-effort interrupt for a hung owner-thread close during shutdown."""
    try:
        result = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread_ident),
            ctypes.py_object(exc_type),
        )
        if result > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_ident),
                None,
            )
    except Exception:
        pass


class _PlaywrightCloseTimeout(TimeoutError):
    """Raised into the owner thread when close exceeds the shutdown budget."""


def _close_with_timeout(
    target: Any,
    *,
    close_timeout_seconds: float,
    close_thread_name: str,
) -> PlaywrightCloseResult:
    """Close on the calling (owner) thread; force-kill + interrupt on hang.

    Playwright close/stop must not run on a helper thread. The watchdog thread
    only force-kills the OS process and interrupts the hung owner-thread close
    so callers can return within ``close_timeout_seconds``.
    """
    close_done = threading.Event()
    timed_out = threading.Event()
    close_errors: list[Exception] = []
    close_thread_ident = threading.get_ident()

    def _watchdog() -> None:
        if not close_done.wait(timeout=close_timeout_seconds):
            timed_out.set()
            force_kill_playwright_process(target)
            _interrupt_thread(close_thread_ident, _PlaywrightCloseTimeout)

    watchdog = threading.Thread(
        target=_watchdog,
        name=close_thread_name,
        daemon=True,
    )
    watchdog.start()
    try:
        # Playwright API must run on the owner/caller thread (not the watchdog).
        assert threading.get_ident() == close_thread_ident
        target.close()
    except _PlaywrightCloseTimeout:
        pass
    except Exception as exc:
        close_errors.append(exc)
    finally:
        close_done.set()

    return PlaywrightCloseResult(
        timed_out=timed_out.is_set(),
        close_error=close_errors[0] if close_errors else None,
    )


def close_page_with_timeout(
    page: Any,
    *,
    close_timeout_seconds: float,
    close_thread_name: str,
) -> PlaywrightCloseResult:
    return _close_with_timeout(
        page,
        close_timeout_seconds=close_timeout_seconds,
        close_thread_name=close_thread_name,
    )


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
