from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from functools import lru_cache

from trip_time_service.api.e2e_fixtures import is_fixture_mode_enabled
from trip_time_service.api.naver_playwright_autocomplete import (
    autocomplete_naver_browser_pool,
    fetch_autocomplete_from_instant_search,
    get_naver_browser_pool_metrics,
    reset_naver_browser_pool_runtime,
    shutdown_naver_browser_pool,
    warmup_naver_browser_pool,
)
from trip_time_service.api.naver_playwright_geo import geocode_naver
from trip_time_service.privacy import redact_text
from trip_time_service.providers.base import CoordinateAwareProvider
from trip_time_service.services.trip_time_service import TripTimeService

_log = logging.getLogger(__name__)

_GEOCODE_UA = "TripTimeService/0.1 (https://triptime.co.kr)"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

_NAVER_MAP_SEARCH_URL = "https://map.naver.com/p/api/search/allSearch"
_NAVER_MAP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_SEARCH_LON = 126.978
_DEFAULT_SEARCH_LAT = 37.5665
_AUTOCOMPLETE_MIN_QUERY_LEN = 2
_NAVER_SEARCH_TIMEOUT_SECONDS = 1.5
_NOMINATIM_TIMEOUT_SECONDS = 1.5
_PHOTON_TIMEOUT_SECONDS = 0.8
_NAVER_NCAPTCHA_BACKOFF_SECONDS = 300
_AUTOCOMPLETE_EXPENSIVE_PROVIDER_BUDGET_SECONDS = 3.0
_AUTOCOMPLETE_BROWSER_RETRY_MIN_REMAINING_SECONDS = 0.5
_WS_RE = re.compile(r"\s+")
_HANGUL_RE = re.compile(r"[가-힣]")
_ROAD_ADDRESS_RE = re.compile(r"(로|길|대로|번길|번지)")
_ROAD_ADDRESS_CORE_RE = re.compile(
    r"^(.*?(?:번길|대로|로|길)\s*\d+(?:-\d+)?)(?:\s+.*)?$"
)
_ROAD_ADDRESS_SEGMENT_RE = re.compile(
    r"([^\s,()]+(?:번길|대로|로|길))\s*(\d+(?:-\d+)?)"
)
_NAVER_NCAPTCHA_RETRY_AFTER_TS = 0.0
_AUTOCOMPLETE_WARMUP_DRAIN_SECONDS = 5.0
_PRE_GEOCODE_TIMEOUT_SECONDS = 30.0

_GEO_POOL_LOCK = threading.Lock()
_GEO_POOL: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="geo",
)
_GEO_POOL_DISABLED = False
_AUTOCOMPLETE_RUNTIME_LOCK = threading.Lock()
_AUTOCOMPLETE_RUNTIME_DISABLED = False
_AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN = False
_AUTOCOMPLETE_WARMUP_FUTURES: set[Future[object]] = set()
_AUTOCOMPLETE_WARMUP_ACTIVE = 0
_AUTOCOMPLETE_CALL_CONTEXT = threading.local()
_RUNTIME_METRICS_LOCK = threading.Lock()
_EXTERNAL_PROVIDER_CALL_COUNTS: dict[str, int] = {}
_AUTOCOMPLETE_SOURCE_COUNTS: dict[str, int] = {}
_AUTOCOMPLETE_DEGRADED_COUNTS: dict[str, int] = {}
_AUTOCOMPLETE_STAGE_METRICS: dict[str, dict[str, object]] = {}
_GEOCODE_SOURCE_COUNTS: dict[str, int] = {}


def _increment_runtime_counter(target: dict[str, int], key: str) -> None:
    with _RUNTIME_METRICS_LOCK:
        target[key] = target.get(key, 0) + 1


def _runtime_counter_snapshot(target: dict[str, int]) -> dict[str, int]:
    with _RUNTIME_METRICS_LOCK:
        return dict(sorted(target.items()))


def _record_autocomplete_stage_metric(
    stage: str,
    *,
    duration_ms: float,
    outcome: str,
) -> None:
    with _RUNTIME_METRICS_LOCK:
        current = _AUTOCOMPLETE_STAGE_METRICS.setdefault(
            stage,
            {
                "count": 0,
                "total_ms": 0.0,
                "max_ms": 0.0,
                "outcomes": {},
            },
        )
        current["count"] = int(current.get("count", 0)) + 1
        total_ms = float(current.get("total_ms", 0.0)) + duration_ms
        current["total_ms"] = total_ms
        current["max_ms"] = max(float(current.get("max_ms", 0.0)), duration_ms)
        outcomes = current.setdefault("outcomes", {})
        if isinstance(outcomes, dict):
            outcomes[outcome] = int(outcomes.get(outcome, 0)) + 1


def _autocomplete_stage_metrics_snapshot() -> dict[str, dict[str, object]]:
    with _RUNTIME_METRICS_LOCK:
        snapshot: dict[str, dict[str, object]] = {}
        for stage, values in sorted(_AUTOCOMPLETE_STAGE_METRICS.items()):
            count = int(values.get("count", 0))
            total_ms = float(values.get("total_ms", 0.0))
            max_ms = float(values.get("max_ms", 0.0))
            snapshot[stage] = {
                "count": count,
                "avg_ms": round(total_ms / count, 2) if count else 0.0,
                "max_ms": round(max_ms, 2),
                "outcomes": dict(values.get("outcomes", {})),
            }
        return snapshot


def _time_autocomplete_stage(
    stage: str,
    call: Callable[[], tuple[dict, ...]],
) -> tuple[dict, ...]:
    start = time.perf_counter()
    outcome = "empty"
    try:
        result = call()
        outcome = "hit" if result else "empty"
        return result
    except Exception:
        outcome = "error"
        raise
    finally:
        _record_autocomplete_stage_metric(
            stage,
            duration_ms=(time.perf_counter() - start) * 1000,
            outcome=outcome,
        )


def _reset_runtime_counters() -> None:
    with _RUNTIME_METRICS_LOCK:
        _EXTERNAL_PROVIDER_CALL_COUNTS.clear()
        _AUTOCOMPLETE_SOURCE_COUNTS.clear()
        _AUTOCOMPLETE_DEGRADED_COUNTS.clear()
        _AUTOCOMPLETE_STAGE_METRICS.clear()
        _GEOCODE_SOURCE_COUNTS.clear()


def _create_geo_pool() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2, thread_name_prefix="geo")


def _reset_geo_pool_runtime() -> None:
    global _GEO_POOL_DISABLED
    with _GEO_POOL_LOCK:
        _GEO_POOL_DISABLED = False


def _shutdown_geo_pool_runtime(*, wait_seconds: float) -> None:
    global _GEO_POOL, _GEO_POOL_DISABLED
    with _GEO_POOL_LOCK:
        _GEO_POOL_DISABLED = True
        pool = _GEO_POOL
        _GEO_POOL = None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
        if wait_seconds > 0:
            deadline = time.monotonic() + wait_seconds
            threads = tuple(getattr(pool, "_threads", ()))
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(timeout=remaining)
            alive_threads = sum(1 for thread in threads if thread.is_alive())
            if alive_threads:
                _log.warning(
                    "geo pool shutdown exceeded %.1fs; %d threads still active",
                    wait_seconds,
                    alive_threads,
                )


def _get_geo_pool() -> ThreadPoolExecutor | None:
    global _GEO_POOL
    with _GEO_POOL_LOCK:
        if _GEO_POOL_DISABLED:
            return None
        if _GEO_POOL is None:
            _GEO_POOL = _create_geo_pool()
        return _GEO_POOL


def _submit_geo_pool_task(
    func: Callable[..., object],
    *args: object,
) -> Future[object] | None:
    global _GEO_POOL
    while True:
        pool = _get_geo_pool()
        if pool is None:
            return None
        try:
            return pool.submit(func, *args)
        except RuntimeError:
            with _GEO_POOL_LOCK:
                if pool is _GEO_POOL:
                    _GEO_POOL = None
                if _GEO_POOL_DISABLED:
                    return None


def _get_autocomplete_context_bool(name: str, default: bool) -> bool:
    value = getattr(_AUTOCOMPLETE_CALL_CONTEXT, name, default)
    return bool(value)


@contextmanager
def _autocomplete_call_context(
    *,
    record_ncaptcha_backoff: bool | None = None,
    allow_instant_search_fallback: bool | None = None,
):
    previous_value = getattr(
        _AUTOCOMPLETE_CALL_CONTEXT,
        "record_ncaptcha_backoff",
        None,
    )
    previous_instant_fallback_value = getattr(
        _AUTOCOMPLETE_CALL_CONTEXT,
        "allow_instant_search_fallback",
        None,
    )
    if record_ncaptcha_backoff is not None:
        _AUTOCOMPLETE_CALL_CONTEXT.record_ncaptcha_backoff = (
            record_ncaptcha_backoff
        )
    if allow_instant_search_fallback is not None:
        _AUTOCOMPLETE_CALL_CONTEXT.allow_instant_search_fallback = (
            allow_instant_search_fallback
        )
    try:
        yield
    finally:
        if previous_value is None:
            if hasattr(_AUTOCOMPLETE_CALL_CONTEXT, "record_ncaptcha_backoff"):
                delattr(_AUTOCOMPLETE_CALL_CONTEXT, "record_ncaptcha_backoff")
        else:
            _AUTOCOMPLETE_CALL_CONTEXT.record_ncaptcha_backoff = previous_value
        if previous_instant_fallback_value is None:
            if hasattr(
                _AUTOCOMPLETE_CALL_CONTEXT,
                "allow_instant_search_fallback",
            ):
                delattr(
                    _AUTOCOMPLETE_CALL_CONTEXT,
                    "allow_instant_search_fallback",
                )
        else:
            _AUTOCOMPLETE_CALL_CONTEXT.allow_instant_search_fallback = (
                previous_instant_fallback_value
            )


def _normalize_text(value: str) -> str:
    return _WS_RE.sub(" ", value.strip().lower())


def _compact_text(value: str) -> str:
    return _WS_RE.sub("", value.strip().lower())


_redact_query = redact_text


def _should_skip_autocomplete_warmup_query(query: str) -> bool:
    normalized = query.strip()
    if normalized.count(",") < 2:
        return False
    return "대한민국" in normalized


def _contains_hangul(value: str) -> bool:
    return bool(_HANGUL_RE.search(value))


def _is_address_like_query(query: str) -> bool:
    compact_query = _compact_text(query)
    if len(compact_query) < 4:
        return False
    has_digit = any(char.isdigit() for char in query)
    if not has_digit:
        return False
    return bool(_ROAD_ADDRESS_RE.search(query))


def _fallback_query_miss_confidence(
    query: str,
    *,
    index: int,
    address_like_query: bool = False,
) -> float | None:
    if address_like_query and index == 0:
        return 0.31
    if not _contains_hangul(query) and index == 0:
        return 0.33
    return None


def _build_query_variants(query: str) -> tuple[str, ...]:
    normalized = query.strip()
    if not normalized:
        return ()

    variants = (
        normalized,
        re.sub(r"(?<=[가-힣A-Za-z])(?=\d)", " ", normalized),
        re.sub(r"(?<=\d)(?=[가-힣A-Za-z])", " ", normalized),
        normalized.replace(" ", ""),
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        candidate = variant.strip()
        if not candidate:
            continue
        normalized_key = _normalize_text(candidate)
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        deduped.append(candidate)
    return tuple(deduped)


def _query_for_fallback_provider(query: str) -> str:
    variants = _build_query_variants(query)
    if not variants:
        return query.strip()
    for variant in variants:
        if " " in variant:
            return variant
    return variants[0]


def _looks_like_query_match(query: str, *candidate_parts: str) -> bool:
    candidate_text = " ".join(part for part in candidate_parts if part)
    compact_candidate = _compact_text(candidate_text)
    normalized_candidate = _normalize_text(candidate_text)

    for variant in _build_query_variants(query):
        compact_query = _compact_text(variant)
        if not compact_query:
            continue
        if compact_query in compact_candidate:
            return True

        if _contains_hangul(variant) and not _contains_hangul(candidate_text):
            continue

        normalized_query = _normalize_text(variant)
        if normalized_query and normalized_query in normalized_candidate:
            return True
    return False


def _format_search_coord(search_coord: tuple[float, float] | None) -> str:
    if search_coord is None:
        return f"{_DEFAULT_SEARCH_LON:.6f};{_DEFAULT_SEARCH_LAT:.6f}"

    lon, lat = search_coord
    clamped_lon = max(-180.0, min(180.0, float(lon)))
    clamped_lat = max(-90.0, min(90.0, float(lat)))
    return f"{clamped_lon:.6f};{clamped_lat:.6f}"


def autocomplete_nominatim(query: str, limit: int = 5) -> tuple[dict, ...]:
    provider_query = _query_for_fallback_provider(query)
    qs = urllib.parse.urlencode(
        {
            "format": "json",
            "countrycodes": "kr",
            "limit": str(limit),
            "q": provider_query,
        }
    )
    request = urllib.request.Request(
        f"{_NOMINATIM_URL}?{qs}",
        headers={"Accept-Language": "ko", "User-Agent": _GEOCODE_UA},
    )
    try:
        _increment_runtime_counter(_EXTERNAL_PROVIDER_CALL_COUNTS, "geocode_nominatim")
        with urllib.request.urlopen(
            request,
            timeout=_NOMINATIM_TIMEOUT_SECONDS,
        ) as response:
            data = json.loads(response.read())
        results: list[dict] = []
        address_like_query = _is_address_like_query(query)
        for index, item in enumerate(data or []):
            confidence = max(0.25, 0.45 - (index * 0.05))
            display_name = item.get("display_name", "")
            is_match = _looks_like_query_match(query, display_name)
            if not is_match:
                fallback_confidence = _fallback_query_miss_confidence(
                    query,
                    index=index,
                    address_like_query=address_like_query,
                )
                if fallback_confidence is None:
                    continue
                confidence = min(confidence, fallback_confidence)
            results.append(
                {
                    "lat": item["lat"],
                    "lon": item["lon"],
                    "display_name": display_name,
                    "address": display_name,
                    "type": "주소",
                    "source": "nominatim",
                    "confidence": round(confidence, 2),
                }
            )
            if len(results) >= limit:
                break
        return tuple(results)
    except Exception:
        _log.debug("Nominatim failed query=%s", _redact_query(query), exc_info=True)
    return ()


def geocode_nominatim(query: str) -> dict | None:
    results = autocomplete_nominatim(query, limit=1)
    return results[0] if results else None


def autocomplete_photon(
    query: str,
    limit: int = 5,
    *,
    search_coord: tuple[float, float] | None = None,
) -> tuple[dict, ...]:
    provider_query = _query_for_fallback_provider(query)
    qs = urllib.parse.urlencode(
        {
            "q": provider_query,
            "limit": str(limit),
        }
    )
    request = urllib.request.Request(
        f"{_PHOTON_URL}?{qs}",
        headers={"User-Agent": _GEOCODE_UA},
    )
    try:
        _increment_runtime_counter(_EXTERNAL_PROVIDER_CALL_COUNTS, "geocode_photon")
        with urllib.request.urlopen(
            request,
            timeout=_PHOTON_TIMEOUT_SECONDS,
        ) as response:
            data = json.loads(response.read())
        features = data.get("features", [])
        results: list[dict] = []
        for index, feature in enumerate(features):
            props = feature.get("properties", {})
            geometry = feature.get("geometry", {})
            coords = geometry.get("coordinates") or []
            if len(coords) < 2:
                continue
            if len(results) >= limit:
                break
            country_code = str(props.get("countrycode") or "").upper()
            if country_code and country_code != "KR":
                continue
            name = props.get("name", "")
            city = props.get("city", "")
            state = props.get("state", "")
            country = props.get("country", "")
            display_parts = [part for part in [name, city, state] if part]
            display_name = ", ".join(display_parts) or query
            addr_parts = [part for part in [name, city, state, country] if part]
            confidence = max(0.3, 0.5 - (index * 0.05))
            if not _looks_like_query_match(query, display_name, ", ".join(addr_parts)):
                fallback_confidence = _fallback_query_miss_confidence(
                    query,
                    index=index,
                )
                if fallback_confidence is None:
                    continue
                confidence = min(confidence, fallback_confidence)
            results.append(
                {
                    "lat": str(coords[1]),
                    "lon": str(coords[0]),
                    "display_name": display_name,
                    "address": ", ".join(addr_parts),
                    "type": "장소",
                    "source": "photon",
                    "confidence": round(confidence, 2),
                }
            )
        return tuple(results)
    except Exception:
        _log.debug("Photon failed query=%s", _redact_query(query), exc_info=True)
    return ()


def geocode_photon(
    query: str,
    *,
    search_coord: tuple[float, float] | None = None,
) -> dict | None:
    results = autocomplete_photon(query, limit=1, search_coord=search_coord)
    return results[0] if results else None


def _fallback_address_geocode(address: str) -> dict | None:
    result = geocode_photon(address)
    if not result:
        result = geocode_nominatim(address)
    return result


def _rank_naver_candidate(query: str, candidate: dict, index: int) -> float:
    q_norm = _normalize_text(query)
    q_compact = _compact_text(query)
    name = str(candidate.get("display_name") or candidate.get("address") or "")
    address = str(candidate.get("address") or "")
    category = str(candidate.get("type") or "")
    name_norm = _normalize_text(name)
    name_compact = _compact_text(name)
    address_norm = _normalize_text(address)
    category_norm = _normalize_text(category)

    score = 0.0
    if q_norm and name_norm == q_norm:
        score += 120
    elif q_norm and name_norm.startswith(q_norm):
        score += 95
    elif q_norm and q_norm in name_norm:
        score += 75

    if q_compact and name_compact == q_compact:
        score += 20
    elif q_compact and q_compact in name_compact:
        score += 10

    if q_norm and q_norm in address_norm:
        score += 14
    if q_norm and q_norm in category_norm:
        score += 4
    if candidate.get("type") == "주소":
        score -= 8

    score -= index * 1.5
    return score


def _select_best_naver_candidate(
    query: str,
    candidates: tuple[dict, ...],
) -> dict | None:
    if not candidates:
        return None

    scored: list[tuple[float, int, dict]] = []
    for index, candidate in enumerate(candidates):
        score = _rank_naver_candidate(query, candidate, index)
        confidence = max(0.5, min(0.99, score / 130))
        enriched = dict(candidate)
        enriched["source"] = "naver_all_search"
        enriched["confidence"] = round(confidence, 2)
        scored.append((score, index, enriched))

    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return scored[0][2]


def geocode_one(
    place: str,
    *,
    search_coord: tuple[float, float] | None = None,
) -> dict | None:
    naver_candidates = autocomplete_naver_map_raw(
        place,
        limit=5,
        search_coord=search_coord,
    )
    best_naver = _select_best_naver_candidate(place, naver_candidates)
    if best_naver:
        _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "naver_all_search")
        _log.info(
            "geocode query=%s source=%s confidence=%.2f",
            _redact_query(place),
            best_naver.get("source"),
            best_naver.get("confidence", 0.0),
        )
        return best_naver

    _increment_runtime_counter(_EXTERNAL_PROVIDER_CALL_COUNTS, "geocode_naver")
    naver_browser = geocode_naver(place, fallback_geocode=_fallback_address_geocode)
    if naver_browser:
        naver_browser["source"] = "naver_browser"
        naver_browser["confidence"] = 0.62
        _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "naver_browser")
        _log.info(
            "geocode query=%s source=naver_browser",
            _redact_query(place),
        )
        return naver_browser

    photon_result = geocode_photon(place, search_coord=search_coord)
    if photon_result:
        _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "photon")
        _log.info(
            "geocode query=%s source=photon",
            _redact_query(place),
        )
        return photon_result

    nominatim_result = geocode_nominatim(place)
    if nominatim_result:
        _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "nominatim")
        _log.info(
            "geocode query=%s source=nominatim",
            _redact_query(place),
        )
        return nominatim_result

    _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "none")
    _increment_runtime_counter(_AUTOCOMPLETE_DEGRADED_COUNTS, "geocode_miss")
    _log.warning("geocode query=%s source=none", _redact_query(place))
    return None


def autocomplete_naver_map_raw(
    query: str,
    limit: int = 5,
    *,
    search_coord: tuple[float, float] | None = None,
    record_ncaptcha_backoff: bool | None = None,
) -> tuple[dict, ...]:
    global _NAVER_NCAPTCHA_RETRY_AFTER_TS
    if len(_compact_text(query)) < _AUTOCOMPLETE_MIN_QUERY_LEN:
        return ()

    if record_ncaptcha_backoff is None:
        record_ncaptcha_backoff = _get_autocomplete_context_bool(
            "record_ncaptcha_backoff",
            True,
        )
    now = time.monotonic()
    if now < _NAVER_NCAPTCHA_RETRY_AFTER_TS:
        return ()

    search_coord_text = _format_search_coord(search_coord)
    qs = urllib.parse.urlencode(
        {
            "query": query,
            "type": "all",
            "searchCoord": search_coord_text,
            "boundary": "",
        }
    )
    request = urllib.request.Request(
        f"{_NAVER_MAP_SEARCH_URL}?{qs}",
        headers={
            "User-Agent": _NAVER_MAP_UA,
            "Referer": "https://map.naver.com/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        _increment_runtime_counter(_EXTERNAL_PROVIDER_CALL_COUNTS, "naver_all_search")
        with urllib.request.urlopen(
            request,
            timeout=_NAVER_SEARCH_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read()
            _log.info("Naver map resp status=%d len=%d", response.status, len(raw))
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300] if hasattr(exc, "read") else b""
        _log.warning(
            "Naver map search HTTP %d query=%s body_len=%d",
            exc.code,
            _redact_query(query),
            len(body),
        )
        return ()
    except Exception:
        _log.warning(
            "Naver map search failed query=%s",
            _redact_query(query),
            exc_info=True,
        )
        return ()

    result_root = data.get("result") or {}
    _log.info(
        "Naver map AC query=%s keys=%s search_coord_present=%s",
        _redact_query(query),
        (
            list(result_root.keys())
            if isinstance(result_root, dict)
            else type(result_root).__name__
        ),
        bool(search_coord_text),
    )
    results: list[dict] = []

    place_section = result_root.get("place") or {}
    place_list = place_section.get("list") or []
    for index, item in enumerate(place_section.get("list") or []):
        if len(results) >= limit:
            break
        name = item.get("name", "")
        road = item.get("roadAddress", "") or item.get("address", "")
        x_coord = item.get("x", "")
        y_coord = item.get("y", "")
        if not x_coord or not y_coord:
            continue
        categories = item.get("category") or []
        if isinstance(categories, list):
            category = categories[-1] if categories else ""
        else:
            category = str(categories).split(">")[-1].strip()
        confidence = max(0.62, 0.95 - (index * 0.07))
        results.append(
            {
                "lat": y_coord,
                "lon": x_coord,
                "display_name": name,
                "address": road,
                "type": category,
                "source": "naver_all_search",
                "confidence": round(confidence, 2),
            }
        )

    addr_section = result_root.get("address") or {}
    addr_list = addr_section.get("list") or []
    for index, item in enumerate(addr_section.get("list") or []):
        if len(results) >= limit:
            break
        name = item.get("name", "") or item.get("roadAddress", "")
        road = item.get("roadAddress", "") or item.get("address", "")
        x_coord = item.get("x", "")
        y_coord = item.get("y", "")
        if not x_coord or not y_coord:
            continue
        confidence = max(0.45, 0.72 - (index * 0.05))
        results.append(
            {
                "lat": y_coord,
                "lon": x_coord,
                "display_name": name,
                "address": road,
                "type": "주소",
                "source": "naver_all_search",
                "confidence": round(confidence, 2),
            }
        )

    if result_root.get("ncaptcha") and not place_list and not addr_list:
        _increment_runtime_counter(_AUTOCOMPLETE_DEGRADED_COUNTS, "ncaptcha_backoff")
        if record_ncaptcha_backoff:
            _NAVER_NCAPTCHA_RETRY_AFTER_TS = (
                now + _NAVER_NCAPTCHA_BACKOFF_SECONDS
            )
            _log.warning(
                "Naver allSearch blocked by ncaptcha query=%s; backoff=%ss",
                _redact_query(query),
                _NAVER_NCAPTCHA_BACKOFF_SECONDS,
            )
        else:
            _log.info(
                "Naver allSearch warmup hit ncaptcha query=%s; "
                "shared backoff unchanged",
                _redact_query(query),
            )
        return ()

    return tuple(results)


def _iter_browser_autocomplete_queries(candidate: dict) -> tuple[str, ...]:
    item_type = str(candidate.get("type") or "").strip()
    display_name = str(candidate.get("display_name") or "").strip()
    address = str(candidate.get("address") or "").strip()
    canonical_query = str(candidate.get("canonical_query") or "").strip()
    raw_candidates: list[str] = []
    if item_type == "주소":
        raw_candidates.extend((address, display_name))
    else:
        raw_candidates.extend((display_name, address))
    raw_candidates.append(canonical_query)
    for value in (display_name, address):
        core_road_address = _trim_to_core_road_address(value)
        if core_road_address:
            raw_candidates.append(core_road_address)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in raw_candidates:
        normalized = _normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value)
    return tuple(deduped)


def _trim_to_core_road_address(value: str) -> str:
    normalized = _WS_RE.sub(" ", value.strip())
    if not normalized:
        return ""
    match = _ROAD_ADDRESS_SEGMENT_RE.search(normalized)
    if match:
        return f"{match.group(1).strip()} {match.group(2).strip()}"
    match = _ROAD_ADDRESS_CORE_RE.match(normalized)
    if not match:
        return ""
    return _WS_RE.sub(" ", match.group(1).strip())


def _iter_provider_geocode_queries(place_name: str) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in (
        place_name.strip(),
        _trim_to_core_road_address(place_name),
    ):
        normalized = _normalize_text(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return tuple(deduped)


def _merge_browser_autocomplete_candidate(
    candidate: dict,
    geocoded: dict,
) -> dict:
    merged = dict(candidate)
    geocoded_address = str(geocoded.get("address") or "").strip()
    candidate_address = str(candidate.get("address") or "").strip()
    candidate_display = str(candidate.get("display_name") or "").strip()
    if geocoded_address and (
        not candidate_address
        or candidate_address == candidate_display
        or candidate.get("type") in {"주소", "검색어"}
    ):
        merged["address"] = geocoded_address
    merged["lat"] = geocoded["lat"]
    merged["lon"] = geocoded["lon"]
    merged["coords_ready"] = True
    merged["source"] = "naver_browser_suggest_geocoded"
    merged["geocode_source"] = geocoded.get("source")
    candidate_confidence = candidate.get("confidence")
    geocode_confidence = geocoded.get("confidence")
    confidence_values = [
        float(value)
        for value in (candidate_confidence, geocode_confidence)
        if isinstance(value, (int, float))
    ]
    merged["confidence"] = round(max(confidence_values, default=0.6), 2)
    return merged


def _geocode_naver_only(
    place: str,
    *,
    search_coord: tuple[float, float] | None = None,
) -> dict | None:
    naver_candidates = autocomplete_naver_map_raw(
        place,
        limit=5,
        search_coord=search_coord,
    )
    best_naver = _select_best_naver_candidate(place, naver_candidates)
    if best_naver:
        _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "naver_all_search")
        return best_naver

    _increment_runtime_counter(_EXTERNAL_PROVIDER_CALL_COUNTS, "geocode_naver")
    naver_browser = geocode_naver(place)
    if naver_browser:
        naver_browser["source"] = "naver_browser"
        naver_browser["confidence"] = 0.62
        _increment_runtime_counter(_GEOCODE_SOURCE_COUNTS, "naver_browser")
        return naver_browser
    return None


def _geocoded_matches_browser_candidate(
    query: str,
    candidate: dict,
    geocode_query: str,
    geocoded: dict,
) -> bool:
    geocoded_display = str(geocoded.get("display_name") or "")
    geocoded_address = str(geocoded.get("address") or "")
    if _looks_like_query_match(query, geocoded_display, geocoded_address):
        return True
    if _looks_like_query_match(geocode_query, geocoded_display, geocoded_address):
        return True
    candidate_display = str(candidate.get("display_name") or "")
    candidate_address = str(candidate.get("address") or "")
    return _looks_like_query_match(
        geocode_query,
        candidate_display,
        candidate_address,
    )


def _select_browser_autocomplete_candidate(
    query: str,
    candidates: tuple[dict, ...],
) -> dict:
    for candidate in candidates:
        if _looks_like_query_match(
            query,
            str(candidate.get("display_name") or ""),
            str(candidate.get("address") or ""),
        ):
            return candidate
    return (
        candidates[0]
        if candidates
        else {
            "display_name": query,
            "address": query,
            "type": "검색어",
            "source": "naver_browser_suggest",
            "confidence": 0.6,
        }
    )


def _should_use_browser_poi_fast_path(
    query: str,
    candidates: tuple[dict, ...],
) -> bool:
    if not candidates:
        return False
    if _ROAD_ADDRESS_RE.search(query) or re.search(r"\d", query):
        return False
    if not _contains_hangul(query):
        return False
    anchor_candidate = _select_browser_autocomplete_candidate(query, candidates)
    candidate_type = str(anchor_candidate.get("type") or "").strip()
    if candidate_type == "주소":
        return False
    return _looks_like_query_match(
        query,
        str(anchor_candidate.get("display_name") or ""),
        str(anchor_candidate.get("address") or ""),
    )


def _mark_progressive_browser_autocomplete_results(
    candidates: tuple[dict, ...],
    *,
    reason: str,
    deadline_hit: bool = False,
) -> tuple[dict, ...]:
    progressive: list[dict] = []
    for candidate in candidates:
        item = dict(candidate)
        item.setdefault("coords_status", "unresolved")
        item.setdefault("degraded_reason", reason)
        item["deadline_hit"] = bool(deadline_hit)
        item["autocomplete_mode"] = "progressive"
        progressive.append(item)
    return tuple(progressive)


def _promote_browser_autocomplete_results(
    query: str,
    candidates: tuple[dict, ...],
    *,
    limit: int,
    search_coord: tuple[float, float] | None = None,
) -> tuple[dict, ...]:
    candidate_slice = tuple(candidates[: max(1, min(limit, 4))])
    geocode_cache: dict[str, dict | None] = {}
    query_looks_like_address = bool(
        _ROAD_ADDRESS_RE.search(query) or re.search(r"\d", query)
    )

    direct_geocoded = _geocode_naver_only(query, search_coord=search_coord)
    geocode_cache[query] = direct_geocoded
    if direct_geocoded and (
        query_looks_like_address
        or _looks_like_query_match(
            query,
            str(direct_geocoded.get("display_name") or ""),
            str(direct_geocoded.get("address") or ""),
        )
    ):
        anchor_candidate = _select_browser_autocomplete_candidate(
            query,
            candidate_slice,
        )
        return (
            _merge_browser_autocomplete_candidate(
                anchor_candidate,
                direct_geocoded,
            ),
        )

    promoted: list[dict] = []
    seen_keys: set[str] = set()
    for candidate in candidate_slice:
        for geocode_query in _iter_browser_autocomplete_queries(candidate):
            if geocode_query not in geocode_cache:
                geocode_cache[geocode_query] = _geocode_naver_only(
                    geocode_query,
                    search_coord=search_coord,
                )
            geocoded = geocode_cache[geocode_query]
            if not geocoded:
                continue
            if not _geocoded_matches_browser_candidate(
                query,
                candidate,
                geocode_query,
                geocoded,
            ):
                continue
            merged = _merge_browser_autocomplete_candidate(candidate, geocoded)
            dedupe_key = (
                f"{_compact_text(str(merged.get('display_name') or ''))}"
                f"|{_compact_text(str(merged.get('address') or ''))}"
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            promoted.append(merged)
            break
        if len(promoted) >= limit:
            break
    return tuple(promoted)


def _autocomplete_naver_map_uncached(
    query: str,
    limit: int = 5,
    *,
    search_coord: tuple[float, float] | None = None,
    record_ncaptcha_backoff: bool | None = None,
) -> tuple[dict, ...]:
    started_at = time.perf_counter()
    if len(_compact_text(query)) < _AUTOCOMPLETE_MIN_QUERY_LEN:
        return ()

    def _query_browser_autocomplete(stage: str) -> tuple[dict, ...]:
        _increment_runtime_counter(
            _EXTERNAL_PROVIDER_CALL_COUNTS,
            "browser_autocomplete",
        )
        return _time_autocomplete_stage(
            stage,
            lambda: autocomplete_naver_browser_pool(query, limit=limit),
        )

    browser_results = _query_browser_autocomplete("browser_autocomplete")
    remaining_budget_seconds = _AUTOCOMPLETE_EXPENSIVE_PROVIDER_BUDGET_SECONDS - (
        time.perf_counter() - started_at
    )
    if (
        not browser_results
        and remaining_budget_seconds
        >= _AUTOCOMPLETE_BROWSER_RETRY_MIN_REMAINING_SECONDS
    ):
        browser_results = _query_browser_autocomplete("browser_autocomplete_retry")

    if browser_results:
        browser_fast_path = len(browser_results) == 1 and bool(
            _ROAD_ADDRESS_RE.search(query) or re.search(r"\d", query)
        )
        browser_poi_fast_path = _should_use_browser_poi_fast_path(
            query,
            browser_results,
        )
        if browser_fast_path:
            promoted_browser_results = _time_autocomplete_stage(
                "browser_geocode_promotion",
                lambda: _promote_browser_autocomplete_results(
                    query,
                    browser_results,
                    limit=limit,
                    search_coord=search_coord,
                ),
            )
            if promoted_browser_results:
                _increment_runtime_counter(
                    _AUTOCOMPLETE_SOURCE_COUNTS,
                    "naver_browser_suggest_geocoded",
                )
                _log.info(
                    "autocomplete query=%s source=naver_browser_suggest_geocoded "
                    "fallback_count=%d raw_count=%d",
                    _redact_query(query),
                    len(promoted_browser_results),
                    len(browser_results),
                )
                return promoted_browser_results
        if browser_fast_path:
            _increment_runtime_counter(
                _AUTOCOMPLETE_SOURCE_COUNTS,
                "naver_browser_suggest_fast_path",
            )
            _increment_runtime_counter(
                _AUTOCOMPLETE_DEGRADED_COUNTS,
                "coords_unresolved",
            )
            _log.info(
                "autocomplete query=%s source=naver_browser_suggest_fast_path "
                "fallback_count=%d",
                _redact_query(query),
                len(browser_results),
            )
            return browser_results
        if browser_poi_fast_path:
            progressive_results = _mark_progressive_browser_autocomplete_results(
                browser_results,
                reason="progressive_browser_suggest",
            )
            _increment_runtime_counter(
                _AUTOCOMPLETE_SOURCE_COUNTS,
                "naver_browser_suggest_progressive_poi",
            )
            _increment_runtime_counter(
                _AUTOCOMPLETE_DEGRADED_COUNTS,
                "coords_unresolved",
            )
            _log.info(
                "autocomplete query=%s source=naver_browser_suggest_progressive_poi "
                "fallback_count=%d",
                _redact_query(query),
                len(progressive_results),
            )
            return progressive_results
        if (
            _contains_hangul(query)
            and not (_ROAD_ADDRESS_RE.search(query) or re.search(r"\d", query))
        ):
            deadline_hit = (
                time.perf_counter() - started_at
                >= _AUTOCOMPLETE_EXPENSIVE_PROVIDER_BUDGET_SECONDS
            )
            progressive_results = _mark_progressive_browser_autocomplete_results(
                browser_results,
                reason=(
                    "autocomplete_provider_budget"
                    if deadline_hit
                    else "progressive_browser_suggest"
                ),
                deadline_hit=deadline_hit,
            )
            _increment_runtime_counter(
                _AUTOCOMPLETE_SOURCE_COUNTS,
                "naver_browser_suggest_progressive",
            )
            _increment_runtime_counter(
                _AUTOCOMPLETE_DEGRADED_COUNTS,
                "coords_unresolved",
            )
            if deadline_hit:
                _increment_runtime_counter(
                    _AUTOCOMPLETE_DEGRADED_COUNTS,
                    "autocomplete_provider_budget",
                )
            _log.info(
                "autocomplete query=%s source=naver_browser_suggest_progressive "
                "fallback_count=%d deadline_hit=%s",
                _redact_query(query),
                len(progressive_results),
                deadline_hit,
            )
            return progressive_results
        promoted_browser_results = _time_autocomplete_stage(
            "browser_geocode_promotion",
            lambda: _promote_browser_autocomplete_results(
                query,
                browser_results,
                limit=limit,
                search_coord=search_coord,
            ),
        )
        if promoted_browser_results:
            _increment_runtime_counter(
                _AUTOCOMPLETE_SOURCE_COUNTS,
                "naver_browser_suggest_geocoded",
            )
            _log.info(
                "autocomplete query=%s source=naver_browser_suggest_geocoded "
                "fallback_count=%d raw_count=%d",
                _redact_query(query),
                len(promoted_browser_results),
                len(browser_results),
            )
            return promoted_browser_results
        _increment_runtime_counter(
            _AUTOCOMPLETE_SOURCE_COUNTS,
            "naver_browser_suggest_unresolved",
        )
        _increment_runtime_counter(
            _AUTOCOMPLETE_DEGRADED_COUNTS,
            "coords_unresolved",
        )
        _log.info(
            "autocomplete query=%s source=naver_browser_suggest_unresolved "
            "fallback_count=%d",
            _redact_query(query),
            len(browser_results),
        )
        return browser_results

    if _get_autocomplete_context_bool("allow_instant_search_fallback", True):
        try:
            instant_results = _time_autocomplete_stage(
                "instant_search_service_fallback",
                lambda: fetch_autocomplete_from_instant_search(query, limit=limit),
            )
        except Exception:
            instant_results = ()

        if instant_results:
            marked_instant_results = tuple(
                {
                    **candidate,
                    "source": "naver_browser_suggest_instant_fallback",
                }
                for candidate in instant_results
            )
            _increment_runtime_counter(
                _AUTOCOMPLETE_SOURCE_COUNTS,
                "naver_browser_suggest_instant_fallback",
            )
            _log.info(
                "autocomplete query=%s source=naver_browser_suggest_instant_fallback "
                "fallback_count=%d",
                _redact_query(query),
                len(marked_instant_results),
            )
            return marked_instant_results

    _increment_runtime_counter(_AUTOCOMPLETE_SOURCE_COUNTS, "none")
    return ()


@lru_cache(maxsize=2048)
def _autocomplete_naver_map_cached(
    query: str,
    limit: int = 5,
    *,
    search_coord: tuple[float, float] | None = None,
    record_ncaptcha_backoff: bool | None = None,
) -> tuple[dict, ...]:
    results = _autocomplete_naver_map_uncached(
        query,
        limit=limit,
        search_coord=search_coord,
        record_ncaptcha_backoff=record_ncaptcha_backoff,
    )
    if not results:
        raise LookupError("autocomplete produced no cacheable results")
    return results


def autocomplete_naver_map(
    query: str,
    limit: int = 5,
    *,
    search_coord: tuple[float, float] | None = None,
    record_ncaptcha_backoff: bool | None = None,
) -> tuple[dict, ...]:
    try:
        return _autocomplete_naver_map_cached(
            query,
            limit=limit,
            search_coord=search_coord,
            record_ncaptcha_backoff=record_ncaptcha_backoff,
        )
    except LookupError:
        # 빈 결과는 캐시하지 않는다. (cold-start/일시 실패 복구)
        return ()


autocomplete_naver_map.cache_clear = _autocomplete_naver_map_cached.cache_clear


def _is_autocomplete_runtime_disabled() -> bool:
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        return (
            _AUTOCOMPLETE_RUNTIME_DISABLED
            or _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN
        )


def startup_autocomplete_runtime() -> None:
    global _AUTOCOMPLETE_RUNTIME_DISABLED, _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        _AUTOCOMPLETE_RUNTIME_DISABLED = False
        _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN = False
    _reset_runtime_counters()
    _reset_geo_pool_runtime()
    reset_naver_browser_pool_runtime()


def warmup_autocomplete_runtime() -> int:
    if _is_autocomplete_runtime_disabled():
        return 0
    if is_fixture_mode_enabled():
        return 0
    return warmup_naver_browser_pool()


def shutdown_autocomplete_runtime(
    *,
    startup_thread: threading.Thread | None = None,
    wait_seconds: float = _AUTOCOMPLETE_WARMUP_DRAIN_SECONDS,
) -> None:
    global _AUTOCOMPLETE_RUNTIME_DISABLED, _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        _AUTOCOMPLETE_RUNTIME_DISABLED = True
        _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN = True
    if startup_thread is not None and startup_thread.is_alive():
        startup_thread.join(timeout=min(2.0, max(0.0, wait_seconds)))
    _drain_autocomplete_warmup_futures(wait_seconds=wait_seconds)
    shutdown_naver_browser_pool()
    _shutdown_geo_pool_runtime(wait_seconds=wait_seconds)


def _track_autocomplete_warmup_future(future: Future[object]) -> None:
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        _AUTOCOMPLETE_WARMUP_FUTURES.add(future)

    def _cleanup(done_future: Future[object]) -> None:
        with _AUTOCOMPLETE_RUNTIME_LOCK:
            _AUTOCOMPLETE_WARMUP_FUTURES.discard(done_future)

    future.add_done_callback(_cleanup)


def _drain_autocomplete_warmup_futures(*, wait_seconds: float) -> None:
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        futures = tuple(_AUTOCOMPLETE_WARMUP_FUTURES)
    for future in futures:
        future.cancel()
    if futures:
        wait(futures, timeout=max(0.0, wait_seconds))
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        finished = tuple(
            future
            for future in _AUTOCOMPLETE_WARMUP_FUTURES
            if future.done()
        )
        for future in finished:
            _AUTOCOMPLETE_WARMUP_FUTURES.discard(future)


def clear_autocomplete_cache() -> None:
    global _AUTOCOMPLETE_RUNTIME_DISABLED, _NAVER_NCAPTCHA_RETRY_AFTER_TS
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        if _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN:
            previous_runtime_disabled = True
        else:
            previous_runtime_disabled = _AUTOCOMPLETE_RUNTIME_DISABLED
        if not previous_runtime_disabled:
            _AUTOCOMPLETE_RUNTIME_DISABLED = True
    try:
        _drain_autocomplete_warmup_futures(
            wait_seconds=_AUTOCOMPLETE_WARMUP_DRAIN_SECONDS,
        )
        _autocomplete_naver_map_cached.cache_clear()
        shutdown_naver_browser_pool()
        _NAVER_NCAPTCHA_RETRY_AFTER_TS = 0.0
        _reset_runtime_counters()
    finally:
        with _AUTOCOMPLETE_RUNTIME_LOCK:
            should_restore_runtime = (
                not previous_runtime_disabled
                and not _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN
            )
            if should_restore_runtime:
                _AUTOCOMPLETE_RUNTIME_DISABLED = previous_runtime_disabled
        if should_restore_runtime:
            reset_naver_browser_pool_runtime()


def warmup_autocomplete_cache(
    queries: list[str],
    *,
    search_coord: tuple[float, float] | None = None,
    limit: int = 12,
    background: bool = True,
) -> int:
    if _is_autocomplete_runtime_disabled():
        return 0
    if is_fixture_mode_enabled():
        return 0

    deduped_queries: list[str] = []
    seen_queries: set[str] = set()
    for query in queries:
        normalized = query.strip()
        compact = _compact_text(normalized)
        if len(compact) < 2:
            continue
        if _should_skip_autocomplete_warmup_query(normalized):
            continue
        if compact in seen_queries:
            continue
        seen_queries.add(compact)
        deduped_queries.append(normalized)

    def _warmup() -> None:
        global _AUTOCOMPLETE_WARMUP_ACTIVE
        with _AUTOCOMPLETE_RUNTIME_LOCK:
            if (
                _AUTOCOMPLETE_RUNTIME_DISABLED
                or _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN
            ):
                return
            _AUTOCOMPLETE_WARMUP_ACTIVE += 1
        try:
            if _is_autocomplete_runtime_disabled():
                return
            warmup_naver_browser_pool()
            if _is_autocomplete_runtime_disabled():
                return
            with _autocomplete_call_context(
                record_ncaptcha_backoff=False,
                allow_instant_search_fallback=False,
            ):
                for query in deduped_queries:
                    if _is_autocomplete_runtime_disabled():
                        return
                    try:
                        autocomplete_naver_map(
                            query,
                            limit=limit,
                            search_coord=search_coord,
                            record_ncaptcha_backoff=False,
                        )
                    except Exception:
                        _log.debug(
                            "autocomplete warmup failed query=%s",
                            _redact_query(query),
                            exc_info=True,
                        )
        finally:
            with _AUTOCOMPLETE_RUNTIME_LOCK:
                _AUTOCOMPLETE_WARMUP_ACTIVE = max(
                    0,
                    _AUTOCOMPLETE_WARMUP_ACTIVE - 1,
                )

    if background:
        future = _submit_geo_pool_task(_warmup)
        if future is None:
            return 0
        _track_autocomplete_warmup_future(future)
    else:
        _warmup()
    return len(deduped_queries)


def get_autocomplete_runtime_metrics() -> dict:
    metrics = get_naver_browser_pool_metrics()
    now = time.monotonic()
    metrics["ncaptcha_backoff_active"] = (
        now < _NAVER_NCAPTCHA_RETRY_AFTER_TS
    )
    metrics["ncaptcha_backoff_remaining_seconds"] = max(
        0,
        int(_NAVER_NCAPTCHA_RETRY_AFTER_TS - now),
    )
    metrics["mode"] = "live"
    metrics["fixture_mode"] = False
    metrics["external_provider_call_counts"] = _runtime_counter_snapshot(
        _EXTERNAL_PROVIDER_CALL_COUNTS
    )
    metrics["autocomplete_source_counts"] = _runtime_counter_snapshot(
        _AUTOCOMPLETE_SOURCE_COUNTS
    )
    metrics["autocomplete_degraded_counts"] = _runtime_counter_snapshot(
        _AUTOCOMPLETE_DEGRADED_COUNTS
    )
    metrics["autocomplete_stage_metrics"] = _autocomplete_stage_metrics_snapshot()
    metrics["provider_degraded_counts"] = _runtime_counter_snapshot(
        _AUTOCOMPLETE_DEGRADED_COUNTS
    )
    metrics["geocode_source_counts"] = _runtime_counter_snapshot(
        _GEOCODE_SOURCE_COUNTS
    )
    with _GEO_POOL_LOCK:
        metrics["geo_pool_present"] = _GEO_POOL is not None
        metrics["geo_pool_disabled"] = _GEO_POOL_DISABLED
    with _AUTOCOMPLETE_RUNTIME_LOCK:
        metrics["runtime_disabled"] = _AUTOCOMPLETE_RUNTIME_DISABLED
        metrics["runtime_terminal_shutdown"] = (
            _AUTOCOMPLETE_RUNTIME_TERMINAL_SHUTDOWN
        )
        metrics["warmup_futures"] = len(_AUTOCOMPLETE_WARMUP_FUTURES)
        metrics["warmup_active"] = _AUTOCOMPLETE_WARMUP_ACTIVE
    return metrics


def fetch_osrm_route(
    *,
    olat: float,
    olon: float,
    dlat: float,
    dlon: float,
) -> dict:
    url = f"{_OSRM_URL}/{olon},{olat};{dlon},{dlat}?overview=full&geometries=geojson"
    request = urllib.request.Request(url, headers={"User-Agent": _GEOCODE_UA})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except Exception:
        _log.exception("OSRM route failed")
        return {"code": "Error", "routes": []}


def pre_geocode_for_provider(
    service: TripTimeService,
    *places: str,
    coords_map: dict[str, tuple[float, float]] | None = None,
) -> None:
    provider = service._provider
    if not isinstance(provider, CoordinateAwareProvider):
        return

    coords_map = coords_map or {}
    need_geocode: list[str] = []
    for place in places:
        if place in coords_map:
            lat, lon = coords_map[place]
            provider.set_coords(place, lat, lon)
            _log.info(
                "Pre-geocoded frontend coords query=%s",
                _redact_query(place),
            )
            continue
        need_geocode.append(place)

    if not need_geocode:
        return

    def _do_geocode(place_name: str) -> None:
        for query in _iter_provider_geocode_queries(place_name):
            result = geocode_one(query)
            if not result:
                continue
            provider.set_coords(place_name, float(result["lat"]), float(result["lon"]))
            if query == place_name:
                _log.info(
                    "Pre-geocoded query=%s",
                    _redact_query(place_name),
                )
            else:
                _log.info(
                    "Pre-geocoded query=%s via variant=%s",
                    _redact_query(place_name),
                    _redact_query(query),
                )
            return

    futures: list[Future[object]] = []
    for place in need_geocode:
        future = _submit_geo_pool_task(_do_geocode, place)
        if future is None:
            _log.warning(
                "Pre-geocode skipped because geo pool is unavailable query=%s",
                _redact_query(place),
            )
            continue
        futures.append(future)

    if futures:
        done, pending = wait(futures, timeout=_PRE_GEOCODE_TIMEOUT_SECONDS)
        for future in pending:
            future.cancel()
        if pending:
            _log.warning(
                "Pre-geocode timed out unfinished=%d total=%d",
                len(pending),
                len(futures),
            )
        for future in done:
            try:
                future.result()
            except Exception:
                _log.warning("Pre-geocode task failed", exc_info=True)
