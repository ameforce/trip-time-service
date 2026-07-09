"""Playwright 기반 Naver 지도 browser autocomplete pool.

pool API, suggestion payload, dynamic upper bound / idle TTL / metrics
계약을 유지하면서 브라우저 자동화는 Playwright로 수행한다. instant-search
enrichment/synthesis 헬퍼는 대부분 브라우저 무관하므로 그대로 유지하고,
DOM 조회 경로만 Playwright locator를 쓴다. 좌표 파싱은 `naver_playwright_geo`
에서 공유한다.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from trip_time_service.api.naver_playwright_geo import extract_coords_from_naver_url
from trip_time_service.browser.playwright_runtime import (
    PlaywrightBrowserSession,
    PlaywrightLaunchOptions,
    force_kill_playwright_process,
    launch_browser_session,
)
from trip_time_service.privacy import redact_text

_log = logging.getLogger(__name__)

_NAVER_MAP_URL = "https://map.naver.com/"
_MIN_QUERY_LEN = 2
_SUGGEST_WAIT_SECONDS = 5.0
_SUGGEST_POLL_SECONDS = 0.05
_LOCK_WAIT_SECONDS = 0.0
_SCALE_COOLDOWN_SECONDS = 20.0
_DEFAULT_SCALE_INTERVAL_SECONDS = 10.0
_SUGGEST_OPTION_SELECTOR = ".scroll_box [role='option']"
_INPUT_WAIT_MS = 2500
_WS_RE = re.compile(r"\s+")
_HANGUL_RE = re.compile(r"[가-힣]")
_BUSY = object()
_CLOSE_LOCK_TIMEOUT_SECONDS = 1.0
_SESSION_CLOSE_TIMEOUT_SECONDS = 2.0
_POOL_CLOSE_TIMEOUT_SECONDS = 6.0
_PAGE_NAVIGATION_TIMEOUT_MS = 12000
_PAGE_DEFAULT_TIMEOUT_MS = 4000
_SUGGEST_COORD_ATTRS = (
    "href",
    "data-url",
    "data-href",
    "data-link",
    "data-nclk",
    "data-nclicks",
)
_INSTANT_SEARCH_TIMEOUT_MS = 1800
_INSTANT_SEARCH_DIRECT_TIMEOUT_SECONDS = 1.8
_INSTANT_SEARCH_DEFAULT_COORDS = "37.40607799999982,127.12057619212703"

_INSTANT_SEARCH_JS = """
async ({query, defaultCoords, timeoutMs}) => {
  function readCenterCoords() {
    try {
      const naver = window.naver;
      const maps = naver && naver.maps;
      const map = window.map || window.__naver_map__ || window.__map;
      if (maps && map && typeof map.getCenter === "function") {
        const center = map.getCenter();
        const lat = typeof center.lat === "function" ? center.lat() : center.y;
        const lon = typeof center.lng === "function" ? center.lng() : center.x;
        if (Number.isFinite(lat) && Number.isFinite(lon)) {
          return `${lat},${lon}`;
        }
      }
    } catch (_err) {
    }
    return defaultCoords;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const params = new URLSearchParams({query, coords: readCenterCoords()});
    const response = await fetch(
      `/p/api/search/instant-search?${params.toString()}`,
      {
        credentials: "include",
        signal: controller.signal,
        headers: {"accept": "application/json,text/plain,*/*"},
      },
    );
    const text = await response.text();
    if (!response.ok) {
      return {ok: false, status: response.status, text};
    }
    try {
      return {ok: true, status: response.status, json: JSON.parse(text)};
    } catch (err) {
      return {ok: false, status: response.status, text};
    }
  } catch (err) {
    return {ok: false, error: String(err && err.message || err)};
  } finally {
    clearTimeout(timer);
  }
}
"""


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


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    poll: float = _SUGGEST_POLL_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        _sleep(poll)
    try:
        return bool(predicate())
    except Exception:
        return False


def _query_all(page: Any, selector: str) -> list:
    try:
        return list(page.query_selector_all(selector))
    except Exception:
        return []


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


def _option_inner_text(option: Any) -> str:
    try:
        value = option.inner_text()
    except Exception:
        return ""
    return str(value or "")


def _iter_option_coordinate_urls(option: Any) -> tuple[str, ...]:
    urls: list[str] = []
    for attr in _SUGGEST_COORD_ATTRS:
        try:
            value = option.get_attribute(attr)
        except Exception:
            value = None
        if value:
            urls.append(str(value))

    try:
        anchors = option.query_selector_all("a[href]")
    except Exception:
        anchors = []
    for anchor in anchors:
        try:
            href = anchor.get_attribute("href")
        except Exception:
            href = None
        if href:
            urls.append(str(href))
    return tuple(urls)


def _extract_option_link_coords(option: Any) -> tuple[str, str] | None:
    for url in _iter_option_coordinate_urls(option):
        coords = extract_coords_from_naver_url(url)
        if coords:
            lat, lon = coords
            return str(lat), str(lon)
    return None


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
        self._session: PlaywrightBrowserSession | None = None
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
            session = self._ensure_session_locked()
            return self._query_locked(
                session.page,
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
            self._close_session_locked()
            return ()
        finally:
            self._last_used_monotonic = time.monotonic()
            self._lock.release()

    def close(self, *, terminal: bool = False) -> None:
        if terminal:
            self._terminal_close_requested = True
        acquired = self._lock.acquire(timeout=_CLOSE_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            session = self._session
            if terminal:
                if session is not None:
                    self._force_kill_session(session)
                    self._session = None
                self._force_kill_profile_processes()
            return
        try:
            self._close_session_locked()
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
            self._ensure_session_locked()
        except Exception:
            _log.debug(
                "browser autocomplete warmup failed idx=%d",
                self._worker_index,
                exc_info=True,
            )
            self._close_session_locked()
        finally:
            self._lock.release()

    def close_if_idle(self, *, idle_seconds: float) -> None:
        if self._session is None:
            return
        if (time.monotonic() - self._last_used_monotonic) < idle_seconds:
            return
        acquired = self._lock.acquire(timeout=0.0)
        if not acquired:
            return
        try:
            if self._session is not None and (
                time.monotonic() - self._last_used_monotonic
            ) >= idle_seconds:
                self._close_session_locked()
        finally:
            self._lock.release()

    def _ensure_session_locked(self) -> PlaywrightBrowserSession:
        if self._should_abort_locked():
            raise RuntimeError("browser autocomplete worker is shutting down")
        if self._session is not None:
            return self._session

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

        options = PlaywrightLaunchOptions(
            headless=_env_bool("TTS_HEADLESS", True),
            user_data_dir=worker_dir,
        )

        session = launch_browser_session(options)
        try:
            session.page.set_default_navigation_timeout(
                _PAGE_NAVIGATION_TIMEOUT_MS,
            )
            session.page.set_default_timeout(_PAGE_DEFAULT_TIMEOUT_MS)
            session.page.goto(
                _NAVER_MAP_URL,
                wait_until="domcontentloaded",
                timeout=_PAGE_NAVIGATION_TIMEOUT_MS,
            )
            if self._should_abort_locked():
                raise RuntimeError("browser autocomplete worker shutdown during init")
            self._session = session
            return session
        except Exception:
            self._close_session_instance(session)
            raise

    def _should_abort_locked(self) -> bool:
        return self._terminal_close_requested or _pool_runtime_disabled()

    def _close_session_locked(self) -> None:
        if self._session is None:
            return
        session = self._session
        self._session = None
        self._close_session_instance(session)

    def _close_session_instance(self, session: PlaywrightBrowserSession) -> None:
        result = session.close(close_timeout_seconds=_SESSION_CLOSE_TIMEOUT_SECONDS)
        if result.timed_out:
            self._force_kill_profile_processes()
            _log.warning(
                "browser autocomplete session close timeout idx=%d after %.1fs",
                self._worker_index,
                _SESSION_CLOSE_TIMEOUT_SECONDS,
            )
        elif result.close_error is not None:
            _log.debug(
                "browser autocomplete session close failed idx=%d: %s",
                self._worker_index,
                result.close_error,
            )

    def _force_kill_session(self, session: PlaywrightBrowserSession) -> None:
        try:
            target = (
                session.browser if session.browser is not None else session.context
            )
            force_kill_playwright_process(target)
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

    def _prepare_search_input(self, page: Any, query: str) -> None:
        search_input = page.wait_for_selector(
            "input.input_search",
            timeout=_INPUT_WAIT_MS,
        )
        if search_input is None:
            raise RuntimeError("Naver autocomplete search input not found")
        search_input.click()
        try:
            search_input.fill("")
        except Exception:
            pass
        search_input.type(query)

    def _query_locked(
        self,
        page: Any,
        query: str,
        *,
        limit: int,
        wait_seconds: float,
    ) -> tuple[dict, ...]:
        self._prepare_search_input(page, query)

        matched: list[dict] = []

        def _has_suggestions() -> bool:
            options = _query_all(page, _SUGGEST_OPTION_SELECTOR)
            suggestions = _extract_suggestions_from_options(
                query,
                options,
                limit=limit,
            )
            if suggestions:
                matched.clear()
                matched.extend(suggestions)
                return True
            return False

        try:
            if _wait_until(
                _has_suggestions,
                timeout=wait_seconds,
                poll=_SUGGEST_POLL_SECONDS,
            ):
                return _enrich_suggestions_from_instant_search(
                    page,
                    query,
                    tuple(matched),
                )
            return _synthesize_suggestions_from_instant_search(
                page,
                query,
                limit=limit,
            )
        except Exception:
            return _synthesize_suggestions_from_instant_search(
                page,
                query,
                limit=limit,
            )


def _extract_suggestions_from_options(
    query: str,
    options: list[Any],
    *,
    limit: int,
) -> tuple[dict, ...]:
    return tuple(
        suggestion
        for suggestion, _option in _extract_suggestion_records_from_options(
            query,
            options,
            limit=limit,
        )
    )


def _extract_suggestion_records_from_options(
    query: str,
    options: list[Any],
    *,
    limit: int,
) -> tuple[tuple[dict, Any], ...]:
    results: list[dict] = []
    records: list[tuple[dict, Any]] = []
    seen_keys: set[str] = set()
    for index, option in enumerate(options):
        if len(results) >= limit:
            break

        raw_text = _option_inner_text(option).strip()
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
        coords = _extract_option_link_coords(option)
        suggestion = {
            "lat": coords[0] if coords else "",
            "lon": coords[1] if coords else "",
            "display_name": display_name,
            "address": address,
            "type": kind,
            "source": "naver_browser_suggest",
            "confidence": round(confidence, 2),
        }
        results.append(suggestion)
        records.append((suggestion, option))
    return tuple(records)


def _suggestion_has_coords(suggestion: dict) -> bool:
    try:
        lat = float(str(suggestion.get("lat", "")).strip())
        lon = float(str(suggestion.get("lon", "")).strip())
    except (TypeError, ValueError):
        return False
    return 33 <= lat <= 43 and 124 <= lon <= 132


def _fetch_instant_search_json(
    page: Any,
    query: str,
) -> dict | list | None:
    fetch_status: object = "exception"
    try:
        payload = page.evaluate(
            _INSTANT_SEARCH_JS,
            {
                "query": query,
                "defaultCoords": _INSTANT_SEARCH_DEFAULT_COORDS,
                "timeoutMs": _INSTANT_SEARCH_TIMEOUT_MS,
            },
        )
    except Exception:
        _log.debug(
            "browser instant-search js fetch failed query=%s",
            redact_text(query),
        )
        return _direct_fetch_instant_search_json(query)

    if not isinstance(payload, dict) or not payload.get("ok"):
        if isinstance(payload, dict):
            fetch_status = payload.get("status") or payload.get("error") or "non-ok"
        _log.debug(
            "browser instant-search js fetch non-ok status=%s query=%s",
            fetch_status,
            redact_text(query),
        )
        return _direct_fetch_instant_search_json(query)
    data = payload.get("json")
    if isinstance(data, dict | list):
        return data
    _log.debug("browser instant-search js fetch malformed query=%s", redact_text(query))
    return _direct_fetch_instant_search_json(query)


def _direct_fetch_instant_search_json(query: str) -> dict | list | None:
    params = urllib.parse.urlencode(
        {"query": query, "coords": _INSTANT_SEARCH_DEFAULT_COORDS}
    )
    request = urllib.request.Request(
        f"https://map.naver.com/p/api/search/instant-search?{params}",
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://map.naver.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=_INSTANT_SEARCH_DIRECT_TIMEOUT_SECONDS,
        ) as response:
            status = getattr(response, "status", None) or response.getcode()
            if status != 200:
                _log.debug(
                    "direct instant-search fallback non-ok status=%s query=%s",
                    status,
                    redact_text(query),
                )
                return None
            content_type = response.headers.get("Content-Type", "")
            if "json" not in content_type.lower():
                _log.debug(
                    "direct instant-search fallback non-json content-type=%s query=%s",
                    content_type,
                    redact_text(query),
                )
                return None
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _log.debug(
            "direct instant-search fallback http status=%s query=%s",
            exc.code,
            redact_text(query),
        )
        return None
    except Exception:
        _log.debug(
            "direct instant-search fallback failed query=%s",
            redact_text(query),
        )
        return None
    if isinstance(data, dict | list):
        return data
    _log.debug(
        "direct instant-search fallback malformed payload query=%s",
        redact_text(query),
    )
    return None


def _candidate_text_parts(candidate: dict) -> tuple[str, ...]:
    parts: list[str] = []
    for key in (
        "name",
        "displayName",
        "title",
        "label",
        "address",
        "roadAddress",
        "fullAddress",
        "newAddress",
        "jibunAddress",
    ):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return tuple(parts)


def _parse_instant_candidate(candidate: object, *, kind: str) -> dict | None:
    if not isinstance(candidate, dict):
        return None
    try:
        lon = float(str(candidate.get("x", "")).strip())
        lat = float(str(candidate.get("y", "")).strip())
    except (TypeError, ValueError):
        return None
    if not (33 <= lat <= 43 and 124 <= lon <= 132):
        return None
    text_parts = _candidate_text_parts(candidate)
    if not text_parts:
        return None
    display_name = next(
        (
            str(candidate.get(key, "")).strip()
            for key in ("label", "name", "displayName", "title", "address")
            if str(candidate.get(key, "")).strip()
        ),
        text_parts[0],
    )
    address = next(
        (
            str(candidate.get(key, "")).strip()
            for key in (
                "address",
                "roadAddress",
                "fullAddress",
                "newAddress",
                "jibunAddress",
            )
            if str(candidate.get(key, "")).strip()
        ),
        display_name,
    )
    return {
        "lat": str(candidate.get("y", "")).strip(),
        "lon": str(candidate.get("x", "")).strip(),
        "text_parts": text_parts,
        "display_name": display_name,
        "address": address,
        "type": "주소" if kind == "address" else "장소",
        "kind": kind,
    }


def _iter_instant_search_candidates(payload: object) -> tuple[dict, ...]:
    if not isinstance(payload, dict):
        return ()

    candidates: list[dict] = []

    def _append(candidate: object, *, kind: str) -> None:
        parsed = _parse_instant_candidate(candidate, kind=kind)
        if parsed is not None:
            candidates.append(parsed)

    for key in ("address", "place"):
        entries = payload.get(key)
        if isinstance(entries, list):
            for entry in entries:
                _append(entry, kind=key)

    all_entries = payload.get("all")
    if isinstance(all_entries, list):
        for entry in all_entries:
            if not isinstance(entry, dict):
                continue
            for key in ("address", "place"):
                nested = entry.get(key)
                if isinstance(nested, list):
                    for nested_entry in nested:
                        _append(nested_entry, kind=key)
                else:
                    _append(nested, kind=key)

    return tuple(candidates)


def _instant_synthesis_match_score(*, query: str, candidate: dict) -> int:
    candidate_parts = tuple(str(part) for part in candidate.get("text_parts", ()))
    if not candidate_parts:
        return 0
    query_compact = _compact_text(query)
    candidate_compacts = {_compact_text(part) for part in candidate_parts if part}
    candidate_combined = _compact_text(" ".join(candidate_parts))
    if not query_compact or not candidate_combined:
        return 0
    if query_compact in candidate_compacts:
        return 130
    if query_compact in candidate_combined:
        return 110 if candidate.get("kind") == "address" else 105
    if any(
        candidate_compact in query_compact
        for candidate_compact in candidate_compacts
    ):
        return 90
    return 0


def _synthesize_suggestions_from_instant_payload(
    query: str,
    payload: object,
    *,
    limit: int,
) -> tuple[dict, ...]:
    candidates = _iter_instant_search_candidates(payload)
    if not candidates:
        return ()

    ranked: list[tuple[int, int, dict]] = []
    seen_keys: set[str] = set()
    for index, candidate in enumerate(candidates):
        score = _instant_synthesis_match_score(query=query, candidate=candidate)
        if score <= 0:
            continue
        dedupe_key = (
            f"{_compact_text(str(candidate.get('display_name', '')))}|"
            f"{_compact_text(str(candidate.get('address', '')))}"
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        ranked.append((score, index, candidate))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    results: list[dict] = []
    for score, _index, candidate in ranked[: max(0, limit)]:
        results.append(
            {
                "lat": str(candidate["lat"]),
                "lon": str(candidate["lon"]),
                "display_name": str(candidate["display_name"]),
                "address": str(candidate["address"]),
                "type": str(candidate["type"]),
                "source": "naver_browser_suggest",
                "confidence": round(min(0.94, max(0.7, score / 140)), 2),
            }
        )
    return tuple(results)


def fetch_autocomplete_from_instant_search(
    query: str,
    *,
    limit: int = 12,
) -> tuple[dict, ...]:
    payload = _direct_fetch_instant_search_json(query)
    return _synthesize_suggestions_from_instant_payload(query, payload, limit=limit)


def _synthesize_suggestions_from_instant_search(
    page: Any,
    query: str,
    *,
    limit: int,
) -> tuple[dict, ...]:
    try:
        payload = _fetch_instant_search_json(page, query)
    except Exception:
        return ()
    return _synthesize_suggestions_from_instant_payload(query, payload, limit=limit)


def _instant_candidate_match_score(
    *,
    query: str,
    suggestion: dict,
    candidate: dict,
) -> int:
    candidate_parts = tuple(str(part) for part in candidate.get("text_parts", ()))
    candidate_compacts = {_compact_text(part) for part in candidate_parts if part}
    candidate_combined = _compact_text(" ".join(candidate_parts))
    if not candidate_combined:
        return 0

    display = str(suggestion.get("display_name", ""))
    address = str(suggestion.get("address", ""))
    display_compact = _compact_text(display)
    address_compact = _compact_text(address)
    combined_compact = _compact_text(f"{display} {address}")
    suggestion_compacts = {
        part
        for part in (display_compact, address_compact, combined_compact)
        if part
    }
    query_compact = _compact_text(query)

    if display_compact and display_compact in candidate_compacts:
        return 120
    if address_compact and address_compact in candidate_compacts:
        return 95
    if candidate_compacts & suggestion_compacts:
        return 90
    if any(
        part and part in suggestion_compact
        for part in candidate_compacts
        for suggestion_compact in suggestion_compacts
    ):
        if query_compact and query_compact not in candidate_combined:
            return 0
        return 80
    if any(
        suggestion_compact and suggestion_compact in candidate_combined
        for suggestion_compact in suggestion_compacts
    ):
        return 70
    return 0


def _enrich_suggestions_from_instant_search(
    page: Any,
    query: str,
    suggestions: tuple[dict, ...],
) -> tuple[dict, ...]:
    if not suggestions or all(
        _suggestion_has_coords(suggestion) for suggestion in suggestions
    ):
        return suggestions

    try:
        payload = _fetch_instant_search_json(page, query)
    except Exception:
        return suggestions

    candidates = _iter_instant_search_candidates(payload)
    if not candidates:
        return suggestions

    promoted = [dict(suggestion) for suggestion in suggestions]
    used_candidate_indexes: set[int] = set()
    for suggestion_index, suggestion in enumerate(suggestions):
        if _suggestion_has_coords(suggestion):
            continue
        best_score = 0
        best_candidate_index: int | None = None
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index in used_candidate_indexes:
                continue
            score = _instant_candidate_match_score(
                query=query,
                suggestion=suggestion,
                candidate=candidate,
            )
            if score > best_score:
                best_score = score
                best_candidate_index = candidate_index
        if best_candidate_index is None or best_score < 70:
            continue
        candidate = candidates[best_candidate_index]
        promoted[suggestion_index]["lat"] = str(candidate["lat"])
        promoted[suggestion_index]["lon"] = str(candidate["lon"])
        used_candidate_indexes.add(best_candidate_index)

    return tuple(promoted)


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
