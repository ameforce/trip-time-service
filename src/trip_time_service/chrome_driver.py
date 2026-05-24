from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


@dataclass(frozen=True)
class ChromeDriverCloseResult:
    timed_out: bool
    quit_error: Exception | None = None


def build_chrome_options(
    *,
    headless: bool,
    window_size: str,
    user_agent: str,
    chrome_binary_path: str | None = None,
    chrome_user_data_dir: str | None = None,
    no_sandbox: bool = False,
) -> Options:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    if no_sandbox:
        options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=ko-KR")
    options.add_argument(f"--window-size={window_size}")
    options.add_argument(f"--user-agent={user_agent}")
    if chrome_binary_path:
        options.binary_location = chrome_binary_path
    if chrome_user_data_dir:
        options.add_argument(f"--user-data-dir={chrome_user_data_dir}")
    return options


def force_kill_webdriver_process(driver: webdriver.Chrome) -> None:
    service = getattr(driver, "service", None)
    process = getattr(service, "process", None)
    if process is None:
        return

    pid = getattr(process, "pid", None)
    try:
        if process.poll() is not None:
            return
        if isinstance(pid, int) and os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            return
        process.kill()
        process.wait(timeout=1.0)
    except Exception:
        pass


def close_webdriver_with_timeout(
    driver: webdriver.Chrome,
    *,
    quit_timeout_seconds: float,
    quit_thread_name: str,
) -> ChromeDriverCloseResult:
    quit_errors: list[Exception] = []
    quit_done = threading.Event()

    def _run_quit() -> None:
        try:
            driver.quit()
        except Exception as exc:
            quit_errors.append(exc)
        finally:
            quit_done.set()

    quit_thread = threading.Thread(
        target=_run_quit,
        name=quit_thread_name,
        daemon=True,
    )
    quit_thread.start()
    if not quit_done.wait(timeout=quit_timeout_seconds):
        force_kill_webdriver_process(driver)
        return ChromeDriverCloseResult(timed_out=True)
    if quit_errors:
        return ChromeDriverCloseResult(
            timed_out=False,
            quit_error=quit_errors[0],
        )
    return ChromeDriverCloseResult(timed_out=False)
