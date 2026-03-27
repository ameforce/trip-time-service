from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from trip_time_service.api.geocode_services import (
    autocomplete_naver_map,
    fetch_osrm_route,
    geocode_one,
)
from trip_time_service.api.schemas import FrontendConfig

router = APIRouter()
_log = logging.getLogger(__name__)


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
    )


@router.get("/api/autocomplete")
def autocomplete(
    q: str = Query(..., min_length=1, max_length=200),
) -> JSONResponse:
    results = autocomplete_naver_map(q, 5)
    return JSONResponse(content=list(results))


@router.get("/api/geocode")
def geocode(q: str = Query(..., min_length=1, max_length=200)) -> JSONResponse:
    result = geocode_one(q)
    if result:
        _log.info("geocode q=%r → %s", q, result["display_name"])
        return JSONResponse(content=[result])
    _log.warning("geocode q=%r → no results from any source", q)
    return JSONResponse(content=[])


@router.get("/api/route")
def route_between(
    olat: float = Query(..., ge=-90, le=90),
    olon: float = Query(..., ge=-180, le=180),
    dlat: float = Query(..., ge=-90, le=90),
    dlon: float = Query(..., ge=-180, le=180),
) -> JSONResponse:
    route_payload = fetch_osrm_route(olat=olat, olon=olon, dlat=dlat, dlon=dlon)
    return JSONResponse(content=route_payload)
