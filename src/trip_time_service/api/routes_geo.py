from __future__ import annotations

import hmac
import logging
import math
import os

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from trip_time_service.api.e2e_fixtures import (
    autocomplete_fixtures,
    fixture_route_payload,
    fixture_runtime_metrics,
    geocode_fixture,
    is_fixture_mode_enabled,
)
from trip_time_service.api.geocode_services import (
    _trim_to_core_road_address,
    autocomplete_naver_map,
    clear_autocomplete_cache,
    fetch_osrm_route,
    geocode_one,
    get_autocomplete_runtime_metrics,
    warmup_autocomplete_cache,
)
from trip_time_service.api.schemas import FrontendConfig
from trip_time_service.privacy import redact_text
from trip_time_service.versioning import resolve_display_version

router = APIRouter()
_log = logging.getLogger(__name__)
_LOCAL_DEBUG_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_WARMUP_QUERIES = 8
_MAX_WARMUP_QUERY_LENGTH = 120
_MAX_WARMUP_DUPLICATES_PER_QUERY = 2


class AutocompleteWarmupRequest(BaseModel):
    queries: list[str] = Field(default_factory=list)
    blocking: bool = False
    center_lat: float | None = Field(default=None, ge=-90, le=90)
    center_lon: float | None = Field(default=None, ge=-180, le=180)


def _parse_coord(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _autocomplete_selection_kind(item: dict[str, object]) -> str:
    item_type = str(item.get("type") or "").strip()
    display_name = str(item.get("display_name") or "").strip()
    if item_type == "주소":
        return "address"
    if item_type.endswith("역") or display_name.endswith("역"):
        return "station"
    return "poi"


def _autocomplete_canonical_query(
    item: dict[str, object],
    *,
    selection_kind: str,
) -> str:
    display_name = str(item.get("display_name") or "").strip()
    address = str(item.get("address") or "").strip()
    if selection_kind == "station":
        return display_name or address
    core_road_query = (
        _trim_to_core_road_address(address)
        or _trim_to_core_road_address(display_name)
    )
    if core_road_query:
        return core_road_query
    if selection_kind == "address":
        return address or display_name
    if address and address != display_name and _looks_like_address_text(address):
        return address
    return display_name or address


def _looks_like_address_text(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return any(token in text for token in ("대로", "로", "길", "번길", "번지")) or any(
        char.isdigit() for char in text
    )


def _serialize_autocomplete_items(
    items: tuple[dict, ...] | list[dict],
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for item in items:
        lat = _parse_coord(item.get("lat"))
        lon = _parse_coord(item.get("lon"))
        selection_kind = _autocomplete_selection_kind(item)
        canonical_query = _autocomplete_canonical_query(
            item,
            selection_kind=selection_kind,
        )
        if not canonical_query:
            continue
        enriched = dict(item)
        enriched["lat"] = lat
        enriched["lon"] = lon
        enriched["coords_ready"] = lat is not None and lon is not None
        enriched["selection_kind"] = selection_kind
        enriched["canonical_query"] = canonical_query
        serialized.append(enriched)
    return serialized


def _debug_route_allowed(
    request: Request,
    *,
    debug_token: str | None = None,
) -> bool:
    expected_token = os.getenv("TTS_DEBUG_TOKEN", "").strip()
    token_matches = bool(
        expected_token
        and debug_token
        and hmac.compare_digest(debug_token, expected_token)
    )
    if os.getenv("TTS_ENABLE_DEBUG_ROUTES") == "1" and token_matches:
        return True
    client_host = (request.client.host if request.client else "") or ""
    if (
        os.getenv("TTS_ALLOW_LOCAL_DEBUG_ROUTES") == "1"
        and client_host in _LOCAL_DEBUG_HOSTS
    ):
        return True
    return False


def _ensure_debug_route_allowed(
    request: Request,
    *,
    debug_token: str | None = None,
) -> None:
    if _debug_route_allowed(request, debug_token=debug_token):
        return
    raise HTTPException(status_code=403, detail="debug route is disabled")


def _validate_warmup_payload(
    payload: AutocompleteWarmupRequest,
    *,
    request: Request,
    debug_token: str | None,
) -> None:
    duplicate_counts: dict[str, int] = {}
    for query in payload.queries:
        compact = "".join(query.strip().lower().split())
        if not compact:
            continue
        duplicate_counts[compact] = duplicate_counts.get(compact, 0) + 1

    if len(payload.queries) > _MAX_WARMUP_QUERIES:
        raise HTTPException(
            status_code=422,
            detail=f"warmup accepts at most {_MAX_WARMUP_QUERIES} queries",
        )
    if any(
        len(query.strip()) > _MAX_WARMUP_QUERY_LENGTH
        for query in payload.queries
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "warmup query length must be "
                f"<= {_MAX_WARMUP_QUERY_LENGTH} characters"
            ),
        )
    if any(
        count > _MAX_WARMUP_DUPLICATES_PER_QUERY
        for count in duplicate_counts.values()
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "warmup accepts at most "
                f"{_MAX_WARMUP_DUPLICATES_PER_QUERY} duplicate queries"
            ),
        )
    has_live_warmup_work = any(duplicate_counts)
    requires_debug_authorization = (
        payload.blocking
        or (has_live_warmup_work and not is_fixture_mode_enabled())
    )
    if requires_debug_authorization and not _debug_route_allowed(
        request,
        debug_token=debug_token,
    ):
        raise HTTPException(
            status_code=403,
            detail="warmup requires debug authorization",
        )


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/config", response_model=FrontendConfig)
def frontend_config(request: Request) -> FrontendConfig:
    settings = request.app.state.settings
    return FrontendConfig(
        naver_map_client_id=settings.naver_map_client_id,
        timezone=str(settings.timezone),
        provider=settings.provider,
        version=resolve_display_version(),
        step_minutes=settings.step_minutes,
    )


@router.get("/api/autocomplete")
def autocomplete(
    q: str = Query(..., min_length=1, max_length=200),
    center_lat: float | None = Query(default=None, ge=-90, le=90),
    center_lon: float | None = Query(default=None, ge=-180, le=180),
) -> JSONResponse:
    search_coord: tuple[float, float] | None = None
    if center_lat is not None and center_lon is not None:
        search_coord = (center_lon, center_lat)
    if is_fixture_mode_enabled():
        results = autocomplete_fixtures(q, limit=12)
        _log.info(
            "autocomplete query=%s center_present=%s count=%d mode=fixture "
            "external_provider_calls=0",
            redact_text(q),
            center_lat is not None and center_lon is not None,
            len(results),
        )
        return JSONResponse(content=_serialize_autocomplete_items(results))

    results = autocomplete_naver_map(q, 12, search_coord=search_coord)
    runtime_metrics = get_autocomplete_runtime_metrics()
    _log.info(
        "autocomplete query=%s center_present=%s count=%d mode=live "
        "workers=%s upper=%s miss_ratio=%s",
        redact_text(q),
        center_lat is not None and center_lon is not None,
        len(results),
        runtime_metrics.get("workers"),
        runtime_metrics.get("upper_bound"),
        runtime_metrics.get("window_busy_miss_ratio"),
    )
    return JSONResponse(content=_serialize_autocomplete_items(results))


@router.post("/api/autocomplete/warmup")
def autocomplete_warmup(
    payload: AutocompleteWarmupRequest,
    request: Request,
    x_tts_debug_token: str | None = Header(default=None),
) -> JSONResponse:
    _validate_warmup_payload(
        payload,
        request=request,
        debug_token=x_tts_debug_token,
    )
    if is_fixture_mode_enabled():
        _log.info(
            "autocomplete warmup skipped query_count=%d mode=fixture "
            "external_provider_calls=0",
            len(payload.queries),
        )
        return JSONResponse(
            content={
                "queued": 0,
                "fixture_mode": True,
                "external_provider_calls": 0,
            }
        )

    search_coord: tuple[float, float] | None = None
    if payload.center_lat is not None and payload.center_lon is not None:
        search_coord = (payload.center_lon, payload.center_lat)

    queued = warmup_autocomplete_cache(
        payload.queries,
        search_coord=search_coord,
        limit=12,
        background=not payload.blocking,
    )
    _log.info(
        "autocomplete warmup queued=%d query_count=%d center_present=%s",
        queued,
        len(payload.queries),
        payload.center_lat is not None and payload.center_lon is not None,
    )
    return JSONResponse(content={"queued": queued})


@router.post("/api/debug/autocomplete/cache-clear")
def autocomplete_cache_clear(
    request: Request,
    x_tts_debug_token: str | None = Header(default=None),
) -> JSONResponse:
    _ensure_debug_route_allowed(request, debug_token=x_tts_debug_token)
    clear_autocomplete_cache()
    _log.info("autocomplete cache cleared")
    return JSONResponse(content={"ok": True})


@router.get("/api/debug/autocomplete/runtime")
def autocomplete_runtime(
    request: Request,
    x_tts_debug_token: str | None = Header(default=None),
) -> JSONResponse:
    _ensure_debug_route_allowed(request, debug_token=x_tts_debug_token)
    metrics = dict(get_autocomplete_runtime_metrics())
    settings = getattr(request.app.state, "settings", None)
    trip_provider = getattr(settings, "provider", None)
    if trip_provider:
        metrics["trip_provider"] = trip_provider
    if is_fixture_mode_enabled():
        metrics.update(fixture_runtime_metrics(trip_provider=trip_provider))
    return JSONResponse(content=metrics)


@router.get("/api/geocode")
def geocode(
    q: str = Query(..., min_length=1, max_length=200),
    center_lat: float | None = Query(default=None, ge=-90, le=90),
    center_lon: float | None = Query(default=None, ge=-180, le=180),
) -> JSONResponse:
    search_coord: tuple[float, float] | None = None
    if center_lat is not None and center_lon is not None:
        search_coord = (center_lon, center_lat)

    if is_fixture_mode_enabled():
        result = geocode_fixture(q)
        if result:
            _log.info(
                "geocode query=%s center_present=%s source=%s confidence=%s "
                "mode=fixture external_provider_calls=0",
                redact_text(q),
                center_lat is not None and center_lon is not None,
                result.get("source"),
                result.get("confidence"),
            )
            return JSONResponse(content=[result])
        _log.warning(
            "geocode query=%s source=none mode=fixture external_provider_calls=0",
            redact_text(q),
        )
        return JSONResponse(content=[])

    result = geocode_one(q, search_coord=search_coord)
    if result:
        _log.info(
            "geocode query=%s center_present=%s source=%s confidence=%s mode=live",
            redact_text(q),
            center_lat is not None and center_lon is not None,
            result.get("source"),
            result.get("confidence"),
        )
        return JSONResponse(content=[result])
    _log.warning("geocode query=%s source=none mode=live", redact_text(q))
    return JSONResponse(content=[])


@router.get("/api/route")
def route_between(
    olat: float = Query(..., ge=-90, le=90),
    olon: float = Query(..., ge=-180, le=180),
    dlat: float = Query(..., ge=-90, le=90),
    dlon: float = Query(..., ge=-180, le=180),
) -> JSONResponse:
    if is_fixture_mode_enabled():
        return JSONResponse(
            content=fixture_route_payload(
                olat=olat,
                olon=olon,
                dlat=dlat,
                dlon=dlon,
            )
        )
    route_payload = fetch_osrm_route(olat=olat, olon=olon, dlat=dlat, dlon=dlon)
    return JSONResponse(content=route_payload)
