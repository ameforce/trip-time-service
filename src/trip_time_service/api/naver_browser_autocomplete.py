from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from trip_time_service.chrome_driver import (
    build_chrome_options,
    close_webdriver_with_timeout,
    force_kill_webdriver_process,
)
from trip_time_service.privacy import redact_text

_log = logging.getLogger(__name__)

_NAVER_MAP_URL = "https://map.naver.com/"
_MIN_QUERY_LEN = 2
_SUGGEST_WAIT_SECONDS = 5.0
_LOCK_WAIT_SECONDS = 0.0
_SCALE_COOLDOWN_SECONDS = 20.0
_DEFAULT_SCALE_INTERVAL_SECONDS = 10.0
_SUGGEST_OPTION_SELECTOR = ".scroll_box [role='option']"
_WS_RE = re.compile(r"\s+")
_HANGUL_RE = re.compile(r"[가-힣]")
_BUSY = object()
_CLOSE_LOCK_TIMEOUT_SECONDS = 1.0
_DRIVER_QUIT_TIMEOUT_SECONDS = 2.0
_POOL_CLOSE_TIMEOUT_SECONDS = 6.0


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_text(value: str) -> str:
    return _WS_RE.sub(" ", value.strip().lower())


def _compact_text(value: str) -> str:
    return _WS_RE.sub("", value.strip().lower())


def _contains_hangul(value: str) -> bool:
    return bool(_HANGUL_RE.search(value))


def _looks_like_query_match(query: str, *candidate_parts: str) -> bool:
    compact_query = _compact_text(query)
    if not compact_query:
        return False
    candidate_text = " ".join(part for part in candidate_parts if part)
    compact_candidate = _compact_text(candidate_text)
    if compact_query in compact_candidate:
        return True
    if _contains_hangul(query) and not _contains_hangul(candidate_text):
        return False
    normalized_query = _normalize_text(query)
    normalized_candidate = _normalize_text(candidate_text)
    return normalized_query in normalized_candidate


def _read_host_logical_cpus() -> int:
    return max(1, os.cpu_count() or 1)


def _read_cgroup_cpu_limit() -> int | None:
    # cgroup v2
    cpu_max_path = "/sys/fs/cgroup/cpu.max"
    if os.path.exists(cpu_max_path):
        try:
            raw = open(cpu_max_path, encoding="utf-8").read().strip()
            quota, period = raw.split()
            if quota != "max":
                quota_val = int(quota)
                period_val = int(period)
                if quota_val > 0 and period_val > 0:
                    return max(1, quota_val // period_val)
        except Exception:
            pass

    # cgroup v1
    quota_path = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
    period_path = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
    if os.path.exists(quota_path) and os.path.exists(period_path):
        try:
            quota_val = int(open(quota_path, encoding="utf-8").read().strip())
            period_val = int(open(period_path, encoding="utf-8").read().strip())
            if quota_val > 0 and period_val > 0:
                return max(1, quota_val // period_val)
        except Exception:
            pass
    return None


def _read_host_available_memory_mb() -> int | None:
    if os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo", encoding="utf-8") as fp:
                for line in fp:
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return max(1, int(parts[1]) // 1024)
        except Exception:
            pass

    if os.name == "nt":
        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return max(1, int(stat.ullAvailPhys // (1024 * 1024)))
    return None


def _read_cgroup_memory_limit_mb() -> int | None:
    # cgroup v2
    mem_max_path = "/sys/fs/cgroup/memory.max"
    if os.path.exists(mem_max_path):
        try:
            raw = open(mem_max_path, encoding="utf-8").read().strip()
            if raw != "max":
                limit_bytes = int(raw)
                if limit_bytes > 0:
                    return max(1, limit_bytes // (1024 * 1024))
        except Exception:
            pass

    # cgroup v1
    mem_limit_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    if os.path.exists(mem_limit_path):
        try:
            limit_bytes = int(open(mem_limit_path, encoding="utf-8").read().strip())
            if 0 < limit_bytes < (1 << 60):
                return max(1, limit_bytes // (1024 * 1024))
        except Exception:
            pass
    return None


def _detect_dynamic_upper_bound() -> int:
    hard_cap = max(1, _env_int("TTS_AUTOCOMPLETE_BROWSER_HARD_CAP", 6))
    min_warm = max(1, _env_int("TTS_AUTOCOMPLETE_BROWSER_MIN_WARM", 1))
    worker_mem_budget_mb = max(
        128,
        _env_int("TTS_AUTOCOMPLETE_BROWSER_WORKER_MEM_MB", 550),
    )
    mem_reserve_mb = max(
        128,
        _env_int("TTS_AUTOCOMPLETE_BROWSER_MEM_RESERVE_MB", 1024),
    )

    host_cpu = _read_host_logical_cpus()
    cgroup_cpu = _read_cgroup_cpu_limit()
    effective_cpu = min(host_cpu, cgroup_cpu) if cgroup_cpu else host_cpu
    cpu_cap = max(1, int(effective_cpu * 0.6))

    host_mem = _read_host_available_memory_mb()
    cgroup_mem = _read_cgroup_memory_limit_mb()
    if host_mem is None:
        effective_mem = cgroup_mem
    elif cgroup_mem is None:
        effective_mem = host_mem
    else:
        effective_mem = min(host_mem, cgroup_mem)

    if effective_mem is None:
        memory_cap = hard_cap
    else:
        memory_cap = max(
            1,
            int(max(0, effective_mem - mem_reserve_mb) / worker_mem_budget_mb),
        )

    upper = min(hard_cap, cpu_cap, memory_cap)
    return max(min_warm, upper)


class _BrowserAutocompleteWorker:
    def __init__(self, worker_index: int) -> None:
        self._worker_index = worker_index
        self._lock = threading.Lock()
        self._driver: webdriver.Chrome | None = None
        self._last_used_monotonic = 0.0
        self._terminal_close_requested = False
        self._profile_dir: str | None = None

    @property
    def last_used_monotonic(self) -> float:
        return self._last_used_monotonic

    def try_query(
        self,
        query: str,
        *,
        limit: int,
        wait_seconds: float,
    ) -> tuple[dict, ...] | object:
        acquired = self._lock.acquire(timeout=_LOCK_WAIT_SECONDS)
        if not acquired:
            return _BUSY

        try:
            self._last_used_monotonic = time.monotonic()
            driver = self._ensure_driver_locked()
            return self._query_locked(
                driver,
                query,
                limit=limit,
                wait_seconds=wait_seconds,
            )
        except Exception:
            _log.debug(
                "browser autocomplete worker query failed idx=%d query=%s",
                self._worker_index,
                redact_text(query),
                exc_info=True,
            )
            self._close_driver_locked()
            return ()
        finally:
            self._last_used_monotonic = time.monotonic()
            self._lock.release()

    def close(self, *, terminal: bool = False) -> None:
        if terminal:
            self._terminal_close_requested = True
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            driver = self._driver
            if terminal:
                if driver is not None:
                    self._force_kill_driver_process(driver)
                    self._driver = None
                self._force_kill_profile_processes()
            return
        try:
            self._close_driver_locked()
            if terminal:
                self._force_kill_profile_processes()
        finally:
            self._lock.release()

    def warmup(self) -> None:
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            return
        try:
            self._last_used_monotonic = time.monotonic()
            self._ensure_driver_locked()
        except Exception:
            _log.debug(
                "browser autocomplete warmup failed idx=%d",
                self._worker_index,
                exc_info=True,
            )
            self._close_driver_locked()
        finally:
            self._lock.release()

    def close_if_idle(self, *, idle_seconds: float) -> None:
        if self._driver is None:
            return
        if (time.monotonic() - self._last_used_monotonic) < idle_seconds:
            return
        acquired = self._lock.acquire(timeout=0.0)
        if not acquired:
            return
        try:
            if self._driver is not None and (
                time.monotonic() - self._last_used_monotonic
            ) >= idle_seconds:
                self._close_driver_locked()
        finally:
            self._lock.release()

    def _ensure_driver_locked(self) -> webdriver.Chrome:
        if self._should_abort_locked():
            raise RuntimeError("browser autocomplete worker is shutting down")
        if self._driver is not None:
            return self._driver

        chrome_binary_path = os.getenv("TTS_CHROME_BINARY_PATH")

        self._profile_dir = None
        chrome_user_data_dir = os.getenv("TTS_CHROME_USER_DATA_DIR")
        worker_dir: str | None = None
        if chrome_user_data_dir:
            worker_dir = os.path.join(
                chrome_user_data_dir,
                f"ac-worker-{self._worker_index}",
            )
            os.makedirs(worker_dir, exist_ok=True)
            self._profile_dir = worker_dir

        opts = build_chrome_options(
            headless=_env_bool("TTS_HEADLESS", True),
            window_size="1280,960",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            chrome_binary_path=chrome_binary_path,
            chrome_user_data_dir=worker_dir,
            no_sandbox=_env_bool("TTS_CHROME_NO_SANDBOX", False),
        )

        driver = webdriver.Chrome(options=opts)
        try:
            driver.set_page_load_timeout(12)
            driver.set_script_timeout(4)
            driver.get(_NAVER_MAP_URL)
            if self._should_abort_locked():
                raise RuntimeError("browser autocomplete worker shutdown during init")
            self._driver = driver
            return driver
        except Exception:
            self._close_driver_instance(driver)
            raise

    def _should_abort_locked(self) -> bool:
        return self._terminal_close_requested or _pool_runtime_disabled()

    def _close_driver_locked(self) -> None:
        if self._driver is None:
            return
        driver = self._driver
        self._driver = None
        self._close_driver_instance(driver)

    def _close_driver_instance(self, driver: webdriver.Chrome) -> None:
        result = close_webdriver_with_timeout(
            driver,
            quit_timeout_seconds=_DRIVER_QUIT_TIMEOUT_SECONDS,
            quit_thread_name=f"browser-autocomplete-quit-{self._worker_index}",
        )
        if result.timed_out:
            self._force_kill_profile_processes()
            _log.warning(
                "browser autocomplete driver quit timeout idx=%d after %.1fs",
                self._worker_index,
                _DRIVER_QUIT_TIMEOUT_SECONDS,
            )
        elif result.quit_error is not None:
            _log.debug(
                "browser autocomplete driver quit failed idx=%d: %s",
                self._worker_index,
                result.quit_error,
            )

    def _force_kill_driver_process(self, driver: webdriver.Chrome) -> None:
        try:
            force_kill_webdriver_process(driver)
        except Exception:
            pass
        finally:
            self._force_kill_profile_processes()

    def _force_kill_profile_processes(self) -> None:
        if os.name != "nt" or not self._profile_dir:
            return
        escaped_profile = self._profile_dir.replace("'", "''")
        script = (
            f"$target = '{escaped_profile}'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { "
            "$_.CommandLine -and "
            "$_.CommandLine -like \"*$target*\" -and "
            "($_.Name -match 'chrome|chromedriver|chromium') "
            "} | "
            "ForEach-Object { "
            "try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } "
            "catch {} "
            "}"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.5,
            )
        except Exception:
            pass

    def _query_locked(
        self,
        driver: webdriver.Chrome,
        query: str,
        *,
        limit: int,
        wait_seconds: float,
    ) -> tuple[dict, ...]:
        search_input = WebDriverWait(driver, 2.5).until(
            lambda cur: cur.find_element(By.CSS_SELECTOR, "input.input_search")
        )
        search_input.click()
        search_input.send_keys(Keys.CONTROL, "a")
        search_input.send_keys(Keys.DELETE)
        search_input.send_keys(query)

        def _matching_suggestions(cur: webdriver.Chrome) -> tuple[dict, ...]:
            options = cur.find_elements(By.CSS_SELECTOR, _SUGGEST_OPTION_SELECTOR)
            return _extract_suggestions_from_options(query, options, limit=limit)

        try:
            return WebDriverWait(driver, wait_seconds).until(_matching_suggestions)
        except Exception:
            return ()


def _extract_suggestions_from_options(
    query: str,
    options: list[object],
    *,
    limit: int,
) -> tuple[dict, ...]:
    results: list[dict] = []
    seen_keys: set[str] = set()
    for index, option in enumerate(options):
        if len(results) >= limit:
            break

        raw_text = str(getattr(option, "text", "") or "").strip()
        if not raw_text:
            continue
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not lines:
            continue

        kind = "장소"
        display_name = lines[0]
        address = lines[1] if len(lines) > 1 else display_name
        if lines[0] == "검색어" and len(lines) >= 2:
            kind = "검색어"
            display_name = lines[1]
            address = display_name
        elif lines[0] == "장소" and len(lines) >= 3:
            kind = lines[1]
            display_name = lines[2]
            address = lines[3] if len(lines) >= 4 else display_name
        elif lines[0] == "장소" and len(lines) == 2:
            kind = "장소"
            display_name = lines[1]
            address = display_name
        elif lines[0] == "주소" and len(lines) >= 2:
            kind = "주소"
            display_name = lines[1]
            address = lines[2] if len(lines) >= 3 else display_name

        if not _looks_like_query_match(query, display_name, address):
            continue

        dedupe_key = f"{_compact_text(display_name)}|{_compact_text(address)}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        confidence = max(0.55, 0.9 - (index * 0.04))
        results.append(
            {
                "lat": "",
                "lon": "",
                "display_name": display_name,
                "address": address,
                "type": kind,
                "source": "naver_browser_suggest",
                "confidence": round(confidence, 2),
            }
        )
    return tuple(results)


class NaverBrowserAutocompletePool:
    def __init__(
        self,
        *,
        worker_factory: Callable[[int], _BrowserAutocompleteWorker] | None = None,
        upper_bound_resolver: Callable[[], int] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._workers: list[_BrowserAutocompleteWorker] = []
        self._closed = False
        self._next_worker_index = 1
        self._window_requests = 0
        self._window_busy_miss = 0
        self._last_rebalance_monotonic = 0.0
        self._last_scale_action_monotonic = 0.0
        self._worker_factory = worker_factory or _BrowserAutocompleteWorker
        self._upper_bound_resolver = upper_bound_resolver or _detect_dynamic_upper_bound
        self._scale_interval_seconds = max(
            1.0,
            float(_env_int("TTS_AUTOCOMPLETE_BROWSER_SCALE_INTERVAL_SECONDS", 10)),
        )
        self._idle_ttl_seconds = max(
            30.0,
            float(_env_int("TTS_AUTOCOMPLETE_BROWSER_IDLE_TTL_SECONDS", 180)),
        )
        self._min_warm = max(1, _env_int("TTS_AUTOCOMPLETE_BROWSER_MIN_WARM", 1))

    def query(self, query: str, *, limit: int = 12) -> tuple[dict, ...]:
        if _pool_runtime_disabled():
            return ()
        if not _env_bool("TTS_AUTOCOMPLETE_BROWSER_ENABLE", True):
            return ()
        if len(_compact_text(query)) < _MIN_QUERY_LEN:
            return ()

        with self._lock:
            if self._closed:
                return ()
            self._window_requests += 1
            self._rebalance_locked()
            workers = sorted(
                self._workers,
                key=lambda worker: worker.last_used_monotonic,
            )

        saw_busy = False
        for worker in workers:
            result = worker.try_query(
                query,
                limit=limit,
                wait_seconds=_SUGGEST_WAIT_SECONDS,
            )
            if result is _BUSY:
                saw_busy = True
                continue
            self._cleanup_idle_workers()
            return result

        if saw_busy:
            with self._lock:
                self._window_busy_miss += 1
                self._rebalance_locked(force_expand=True)
        self._cleanup_idle_workers()
        return ()

    def snapshot_metrics(self) -> dict:
        with self._lock:
            if self._closed:
                return {
                    "workers": 0,
                    "upper_bound": self._upper_bound_resolver(),
                    "window_requests": 0,
                    "window_busy_miss": 0,
                    "window_busy_miss_ratio": 0.0,
                }
            upper_bound = self._upper_bound_resolver()
            requests = self._window_requests
            busy_miss = self._window_busy_miss
            miss_ratio = (busy_miss / requests) if requests else 0.0
            return {
                "workers": len(self._workers),
                "upper_bound": upper_bound,
                "window_requests": requests,
                "window_busy_miss": busy_miss,
                "window_busy_miss_ratio": round(miss_ratio, 4),
            }

    def warmup(self) -> int:
        if _pool_runtime_disabled():
            return 0
        if not _env_bool("TTS_AUTOCOMPLETE_BROWSER_ENABLE", True):
            return 0
        with self._lock:
            if self._closed:
                return 0
            self._rebalance_locked(force_expand=True)
            warmup_workers = tuple(self._workers[: self._min_warm])
        for worker in warmup_workers:
            worker.warmup()
        return len(warmup_workers)

    def close(self, *, terminal: bool = False) -> None:
        with self._lock:
            if self._closed and not self._workers:
                return
            self._closed = True
            workers = tuple(self._workers)
            self._workers.clear()
        close_threads: list[threading.Thread] = []

        def _close_worker(worker: _BrowserAutocompleteWorker) -> None:
            try:
                worker.close(terminal=terminal)
            except TypeError:
                worker.close()

        for index, worker in enumerate(workers, start=1):
            thread = threading.Thread(
                target=_close_worker,
                args=(worker,),
                name=f"autocomplete-pool-close-{index}",
                daemon=True,
            )
            thread.start()
            close_threads.append(thread)

        deadline = time.monotonic() + _POOL_CLOSE_TIMEOUT_SECONDS
        for thread in close_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        alive_threads = sum(1 for thread in close_threads if thread.is_alive())
        if alive_threads:
            _log.warning(
                "browser autocomplete pool close timeout after %.1fs; "
                "%d workers still active",
                _POOL_CLOSE_TIMEOUT_SECONDS,
                alive_threads,
            )

    def _rebalance_locked(self, *, force_expand: bool = False) -> None:
        if self._closed:
            return
        now = time.monotonic()
        if (
            not force_expand
            and (now - self._last_rebalance_monotonic) < self._scale_interval_seconds
        ):
            return

        upper_bound = max(self._min_warm, self._upper_bound_resolver())
        current = len(self._workers)
        requests = max(1, self._window_requests)
        miss_ratio = self._window_busy_miss / requests

        desired = current
        if current < self._min_warm:
            desired = self._min_warm
        elif force_expand and current < upper_bound:
            desired = current + 1
        elif (
            miss_ratio > 0.2
            and current < upper_bound
            and (now - self._last_scale_action_monotonic) >= _SCALE_COOLDOWN_SECONDS
        ):
            desired = current + 1
        elif (
            miss_ratio < 0.03
            and current > self._min_warm
            and (now - self._last_scale_action_monotonic) >= _SCALE_COOLDOWN_SECONDS
        ):
            desired = current - 1

        desired = max(self._min_warm, min(desired, upper_bound))

        if desired > current:
            for _ in range(desired - current):
                self._workers.append(self._new_worker_locked())
            self._last_scale_action_monotonic = now
        elif desired < current:
            for _ in range(current - desired):
                worker = self._workers.pop()
                worker.close()
            self._last_scale_action_monotonic = now

        self._window_requests = 0
        self._window_busy_miss = 0
        self._last_rebalance_monotonic = now

    def _cleanup_idle_workers(self) -> None:
        with self._lock:
            workers = tuple(self._workers[self._min_warm :])
        for worker in workers:
            worker.close_if_idle(idle_seconds=self._idle_ttl_seconds)

    def _new_worker_locked(self) -> _BrowserAutocompleteWorker:
        worker = self._worker_factory(self._next_worker_index)
        self._next_worker_index += 1
        return worker


_POOL_LOCK = threading.Lock()
_POOL: NaverBrowserAutocompletePool | None = None
_POOL_DISABLED = False


def _pool_runtime_disabled() -> bool:
    with _POOL_LOCK:
        return _POOL_DISABLED


def _default_metrics(*, disabled: bool = False) -> dict:
    return {
        "workers": 0,
        "upper_bound": _detect_dynamic_upper_bound(),
        "window_requests": 0,
        "window_busy_miss": 0,
        "window_busy_miss_ratio": 0.0,
        "terminally_disabled": disabled,
    }


def reset_naver_browser_pool_runtime() -> None:
    global _POOL_DISABLED
    with _POOL_LOCK:
        _POOL_DISABLED = False


def _get_pool() -> NaverBrowserAutocompletePool | None:
    global _POOL, _POOL_DISABLED
    with _POOL_LOCK:
        if _POOL_DISABLED:
            return None
        if _POOL is None:
            _POOL = NaverBrowserAutocompletePool()
        return _POOL


def autocomplete_naver_browser_pool(query: str, *, limit: int = 12) -> tuple[dict, ...]:
    pool = _get_pool()
    if pool is None:
        return ()
    return pool.query(query, limit=limit)


def get_naver_browser_pool_metrics() -> dict:
    pool = _get_pool()
    if pool is None:
        return _default_metrics(disabled=True)
    metrics = pool.snapshot_metrics()
    metrics["terminally_disabled"] = False
    return metrics


def warmup_naver_browser_pool() -> int:
    pool = _get_pool()
    if pool is None:
        return 0
    return pool.warmup()


def close_naver_browser_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        pool = _POOL
        _POOL = None
    if pool is not None:
        pool.close(terminal=False)


def shutdown_naver_browser_pool() -> None:
    global _POOL, _POOL_DISABLED
    with _POOL_LOCK:
        _POOL_DISABLED = True
        pool = _POOL
        _POOL = None
    if pool is not None:
        pool.close(terminal=True)
