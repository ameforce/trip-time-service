"""Playwright 기반 Naver 지도 geocode fallback.

`geocode_naver`, `shutdown_naver_driver`, `extract_coords_from_naver_url`,
`extract_addr_from_naver_url` 공개 계약을 유지하면서 브라우저 자동화는
Playwright로 수행한다. 좌표/주소 파싱 헬퍼는 브라우저 무관하므로 그대로
유지하고, autocomplete 모듈이 여기서 import해 재사용할 수 있게 한다.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from trip_time_service.browser.playwright_runtime import (
    PlaywrightBrowserSession,
    PlaywrightLaunchOptions,
    PlaywrightOwnerThread,
    launch_browser_session,
)
from trip_time_service.privacy import redact_text

_log = logging.getLogger(__name__)

_naver_lock = threading.Lock()
_naver_session: PlaywrightBrowserSession | None = None
_naver_owner: PlaywrightOwnerThread | None = None

_NAVER_MAP_URL = "https://map.naver.com/"
_PAGE_NAVIGATION_TIMEOUT_MS = 20000
_PAGE_DEFAULT_TIMEOUT_MS = 10000
_SESSION_CLOSE_TIMEOUT_SECONDS = 3.0

_ROAD_CORE_RE = re.compile(r"(.+(?:로|길)\s*\d+(?:-\d+)?)")
_NAVER_ADDRESS_PATH_RE = re.compile(
    r"/(?:address|place)/(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,([^/?#]+))?"
)
_NAVER_C_RE = re.compile(r"[?&]c=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)")

_ENTRY_COORDS_JS = (
    "() => {"
    "var scripts = document.querySelectorAll("
    "'script[type=\"application/ld+json\"]');"
    "for (var i = 0; i < scripts.length; i++) {"
    "  try {"
    "    var data = JSON.parse(scripts[i].textContent);"
    "    if (data.geo) return JSON.stringify("
    "      {lat: data.geo.latitude, lon: data.geo.longitude});"
    "  } catch(e) {}"
    "}"
    "return null;"
    "}"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _get_or_create_owner_locked() -> PlaywrightOwnerThread:
    global _naver_owner
    if _naver_owner is None:
        _naver_owner = PlaywrightOwnerThread(name="naver-geo-playwright-owner")
    return _naver_owner


def _ensure_naver_session() -> PlaywrightBrowserSession:
    global _naver_session
    if _naver_session is not None:
        return _naver_session

    options = PlaywrightLaunchOptions(
        headless=_env_bool("TTS_HEADLESS", True),
        user_data_dir=os.getenv("TTS_CHROME_USER_DATA_DIR"),
        chrome_no_sandbox=_env_bool("TTS_CHROME_NO_SANDBOX", False),
    )
    session = launch_browser_session(options)
    try:
        session.page.set_default_navigation_timeout(_PAGE_NAVIGATION_TIMEOUT_MS)
        session.page.set_default_timeout(_PAGE_DEFAULT_TIMEOUT_MS)
    except Exception:
        pass
    _naver_session = session
    return session


def _close_naver_session_on_owner() -> None:
    global _naver_session
    session = _naver_session
    _naver_session = None
    if session is None:
        return
    result = session.close(close_timeout_seconds=_SESSION_CLOSE_TIMEOUT_SECONDS)
    if result.timed_out:
        _log.warning("Naver geo session close timed out and was force-killed")
    elif result.close_error is not None:
        _log.warning(
            "Naver geo session close failed: %s",
            result.close_error,
        )


def shutdown_naver_driver() -> None:
    global _naver_owner, _naver_session
    with _naver_lock:
        owner = _naver_owner
        session = _naver_session
        _naver_owner = None
        if owner is None:
            _naver_session = None
    if owner is not None:
        try:
            owner.call(
                _close_naver_session_on_owner,
                timeout=_SESSION_CLOSE_TIMEOUT_SECONDS + 1.0,
            )
        except Exception:
            _log.warning("Naver geo shutdown failed", exc_info=True)
        finally:
            owner.close(join_timeout_seconds=_SESSION_CLOSE_TIMEOUT_SECONDS)
        return
    if session is None:
        return
    # Session without owner (tests / edge): close on this thread.
    result = session.close(close_timeout_seconds=_SESSION_CLOSE_TIMEOUT_SECONDS)
    if result.timed_out:
        _log.warning("Naver geo session close timed out and was force-killed")
    elif result.close_error is not None:
        _log.warning(
            "Naver geo session close failed: %s",
            result.close_error,
        )



def _extract_road_addr_from_body(body: str) -> str | None:
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != "주소":
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            candidate = lines[j].strip()
            if not candidate:
                continue
            match = _ROAD_CORE_RE.match(candidate)
            if match:
                return match.group(1).strip()
            return candidate
    return None


def _entry_frame(page: Any) -> Any | None:
    try:
        element = page.query_selector("#entryIframe")
    except Exception:
        return None
    if element is None:
        return None
    try:
        return element.content_frame()
    except Exception:
        return None


def _naver_search(page: Any, query: str) -> None:
    page.goto(
        _NAVER_MAP_URL,
        wait_until="domcontentloaded",
        timeout=_PAGE_NAVIGATION_TIMEOUT_MS,
    )
    _sleep(1)

    search_input = page.wait_for_selector("input.input_search", timeout=5000)
    if search_input is None:
        raise RuntimeError("Naver map search input not found")
    search_input.fill("")
    search_input.type(query)
    _sleep(0.3)
    search_input.press("Enter")


def _naver_read_entry_detail(page: Any) -> str | None:
    frame = _entry_frame(page)
    if frame is None:
        return None
    _sleep(1.5)
    try:
        body = frame.evaluate("() => document.body.innerText")
    except Exception:
        return None
    return _extract_road_addr_from_body(body or "")


def _mercator_to_wgs84(x: float, y: float) -> tuple[float, float] | None:
    if abs(x) < 1000 or abs(y) < 1000:
        return None

    lon = (x / 20037508.34) * 180
    lat_rad = (y / 20037508.34) * math.pi
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat_rad)) - math.pi / 2.0)

    if 33 <= lat <= 43 and 124 <= lon <= 132:
        return lat, lon
    return None


def extract_coords_from_naver_url(url: str) -> tuple[float, float] | None:
    path_match = _NAVER_ADDRESS_PATH_RE.search(url)
    if path_match:
        coords = _mercator_to_wgs84(
            float(path_match.group(1)),
            float(path_match.group(2)),
        )
        if coords:
            return coords

    c_match = _NAVER_C_RE.search(url)
    if not c_match:
        return None
    return _mercator_to_wgs84(float(c_match.group(1)), float(c_match.group(2)))


def extract_addr_from_naver_url(url: str) -> str | None:
    path_match = _NAVER_ADDRESS_PATH_RE.search(url)
    if not path_match:
        return None
    encoded_addr = path_match.group(3)
    if not encoded_addr:
        return None
    decoded = urllib.parse.unquote(encoded_addr).strip()
    return decoded or None


def _naver_extract_entry_coords(page: Any) -> dict | None:
    frame = _entry_frame(page)
    if frame is None:
        return None
    try:
        coords_json = frame.evaluate(_ENTRY_COORDS_JS)
        if coords_json:
            coords = json.loads(coords_json)
            return {"lat": str(coords["lat"]), "lon": str(coords["lon"])}
    except Exception:
        _log.debug("entryIframe coord extraction failed", exc_info=True)
    return None


def _click_first_search_result(page: Any) -> None:
    try:
        element = page.query_selector("#searchIframe")
    except Exception:
        return
    if element is None:
        return
    try:
        frame = element.content_frame()
    except Exception:
        frame = None
    if frame is None:
        return
    try:
        frame.wait_for_selector("a", timeout=5000)
        links = frame.query_selector_all("a")
    except Exception:
        links = []
    if links:
        try:
            links[0].click()
        except Exception:
            _log.debug("Naver search result click failed", exc_info=True)


def _geocode_naver_on_owner(
    query: str,
    *,
    fallback_geocode: Callable[[str], dict | None] | None,
) -> dict | None:
    try:
        session = _ensure_naver_session()
    except Exception:
        _log.warning("Naver session init failed", exc_info=True)
        return None

    page = session.page
    try:
        _naver_search(page, query)
        _sleep(3)

        road_addr = _naver_read_entry_detail(page)
        if not road_addr:
            _click_first_search_result(page)
            _sleep(3)
            road_addr = _naver_read_entry_detail(page)

        coords = extract_coords_from_naver_url(page.url)
        if coords:
            lat, lon = coords
            if not road_addr:
                road_addr = extract_addr_from_naver_url(page.url)
            display_name = f"{query} ({road_addr})" if road_addr else query
            _log.info(
                "Naver geocode query=%s source=url_coords",
                redact_text(query),
            )
            return {
                "lat": str(lat),
                "lon": str(lon),
                "display_name": display_name,
            }

        entry_coords = _naver_extract_entry_coords(page)
        if entry_coords:
            display_name = f"{query} ({road_addr})" if road_addr else query
            entry_coords["display_name"] = display_name
            _log.info(
                "Naver geocode query=%s source=entry_coords",
                redact_text(query),
            )
            return entry_coords

        if not road_addr:
            _log.info(
                "Naver geocode query=%s road_address=false",
                redact_text(query),
            )
            return None

        _log.info(
            "Naver geocode query=%s road_address=%s fallback=true",
            redact_text(query),
            redact_text(road_addr),
        )
        if fallback_geocode is None:
            return None

        result = fallback_geocode(road_addr)
        if result:
            result["display_name"] = f"{query} ({road_addr})"
            return result
        _log.info(
            "Naver geocode failed address=%s",
            redact_text(road_addr),
        )
    except Exception:
        _log.debug(
            "Naver geocode failed query=%s",
            redact_text(query),
            exc_info=True,
        )
    return None


def geocode_naver(
    query: str,
    *,
    fallback_geocode: Callable[[str], dict | None] | None = None,
) -> dict | None:
    with _naver_lock:
        owner = _get_or_create_owner_locked()
        return owner.call(
            lambda: _geocode_naver_on_owner(
                query,
                fallback_geocode=fallback_geocode,
            )
        )
