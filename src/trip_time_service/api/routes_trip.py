from __future__ import annotations

import logging
import math
import queue
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from trip_time_service.api.common import ensure_future_time, service_from_request
from trip_time_service.api.geocode_services import pre_geocode_for_provider
from trip_time_service.api.schemas import (
    ArrivalTimeRequest,
    ArrivalTimeResponse,
    ArrivalWithRecommendationResponse,
    DepartureRecommendationRequest,
    DepartureRecommendationResponse,
    RecommendationCandidateModel,
    RouteModel,
    SafeDeparturePreviewResponse,
)
from trip_time_service.api.streaming import (
    STREAM_IDLE_TIMEOUT_SECONDS,
    iter_stream_events,
    make_stream_queue,
    put_stream_item,
    sse_encode,
    start_bounded_stream_worker,
)
from trip_time_service.providers.base import ProviderError
from trip_time_service.services.trip_time_service import (
    NoFeasibleDepartureError,
    TripTimeService,
)

router = APIRouter()
_log = logging.getLogger(__name__)

_SAFE_PROVIDER_ERROR_BUCKETS = {
    "panel_parse_timeout",
    "provider_retry_exhausted",
    "ncaptcha_backoff",
    "coords_unresolved",
}
_ROUTE_INPUT_CONTRACT_HEADER = "x-tts-route-input-contract"
_ROUTE_INPUT_CONTRACT_MODES = {"warn", "strict"}
_COORD_TOLERANCE = 1e-6


class RouteInputContractError(Exception):
    def __init__(
        self,
        *,
        reason: str,
        detail: str,
        status_code: int = 422,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code


async def route_input_contract_exception_handler(
    request: Request,
    exc: RouteInputContractError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "reason": exc.reason},
    )


def _contract_error(
    reason: str,
    detail: str,
    *,
    status_code: int = 422,
) -> RouteInputContractError:
    return RouteInputContractError(
        reason=reason,
        detail=detail,
        status_code=status_code,
    )


def _resolve_route_input_contract_mode(request: Request) -> str:
    headers = getattr(request, "headers", {}) or {}
    header_value = headers.get(_ROUTE_INPUT_CONTRACT_HEADER)
    if header_value is not None and header_value.strip():
        normalized = header_value.strip().lower()
        if normalized not in _ROUTE_INPUT_CONTRACT_MODES:
            raise _contract_error(
                "coords_contract_invalid",
                "X-TTS-Route-Input-Contract must be one of: warn, strict.",
                status_code=400,
            )
        return normalized

    settings = request.app.state.settings
    configured = str(getattr(settings, "route_input_contract", "warn") or "warn")
    normalized = configured.strip().lower()
    if normalized in _ROUTE_INPUT_CONTRACT_MODES:
        return normalized

    _log.warning(
        "Invalid route_input_contract=%r; falling back to warn",
        configured,
    )
    return "warn"


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _extract_legacy_coords(value: object | None) -> tuple[float, float] | None:
    if value is None:
        return None
    lat = _finite_float(getattr(value, "lat", None))
    lon = _finite_float(getattr(value, "lon", None))
    if lat is None or lon is None:
        raise _contract_error(
            "coords_invalid",
            "Route coordinate fields must be finite lat/lon values.",
        )
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise _contract_error(
            "coords_invalid",
            "Route coordinate fields are outside valid lat/lon bounds.",
        )
    return lat, lon


def _coords_conflict(
    left: tuple[float, float],
    right: tuple[float, float],
) -> bool:
    return (
        abs(left[0] - right[0]) > _COORD_TOLERANCE
        or abs(left[1] - right[1]) > _COORD_TOLERANCE
    )


def _extract_route_place_coords(
    field_name: str,
    place: object,
    legacy_coords: object | None,
) -> tuple[float, float]:
    if not isinstance(place, Mapping):
        raise _contract_error(
            "coords_contract_invalid",
            f"{field_name} must be an object with coords_ready metadata.",
        )
    coords_ready = place.get("coords_ready")
    if not isinstance(coords_ready, bool):
        raise _contract_error(
            "coords_contract_invalid",
            f"{field_name}.coords_ready must be a boolean.",
        )
    if not coords_ready:
        raise _contract_error(
            "coords_unresolved",
            f"{field_name} coordinates are not resolved.",
        )

    lat = _finite_float(place.get("lat"))
    lon = _finite_float(place.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise _contract_error(
            "coords_invalid",
            f"{field_name} requires finite lat/lon when coords_ready is true.",
        )

    metadata_coords = (lat, lon)
    legacy = _extract_legacy_coords(legacy_coords)
    if legacy is not None and _coords_conflict(metadata_coords, legacy):
        raise _contract_error(
            "coords_conflict",
            f"{field_name} route-place coordinates conflict with legacy coords.",
        )
    return metadata_coords


def _build_route_coords_map(
    *,
    payload: ArrivalTimeRequest | DepartureRecommendationRequest,
    request: Request,
) -> dict[str, tuple[float, float]]:
    mode = _resolve_route_input_contract_mode(request)
    coords_map: dict[str, tuple[float, float]] = {}

    if payload.origin_place is not None:
        coords_map[payload.origin] = _extract_route_place_coords(
            "origin_place",
            payload.origin_place,
            payload.origin_coords,
        )
    elif payload.origin_coords is not None:
        coords_map[payload.origin] = _extract_legacy_coords(payload.origin_coords)

    if payload.dest_place is not None:
        coords_map[payload.destination] = _extract_route_place_coords(
            "dest_place",
            payload.dest_place,
            payload.dest_coords,
        )
    elif payload.dest_coords is not None:
        coords_map[payload.destination] = _extract_legacy_coords(payload.dest_coords)

    missing_origin = payload.origin not in coords_map
    missing_destination = payload.destination not in coords_map
    if mode == "strict" and (missing_origin or missing_destination):
        raise _contract_error(
            "coords_required",
            "Strict route input contract requires resolved origin and "
            "destination coordinates.",
        )
    if mode == "warn" and missing_origin and missing_destination:
        _log.warning("legacy_text_route_input mode=warn")
    return coords_map


def _to_arrival_response(result: object) -> ArrivalTimeResponse:
    return ArrivalTimeResponse(
        route=RouteModel(
            origin=result.route.origin,
            destination=result.route.destination,
        ),
        departure_time=result.departure_time,
        arrival_time=result.arrival_time,
        duration_seconds=result.duration.duration_seconds,
        provider=result.provider,
        cache_hit=result.cache_hit,
    )


def _to_departure_response(result: object) -> DepartureRecommendationResponse:
    candidates = [
        RecommendationCandidateModel(
            departure_time=item.departure_time,
            arrival_time=item.arrival_time,
            duration_seconds=item.duration_seconds,
            meets_deadline=item.meets_deadline,
            phase=item.phase,
            score_total=item.score_total,
            score_duration=item.score_duration,
            score_time_proximity=item.score_time_proximity,
            score_night_drive=item.score_night_drive,
            score_stability=item.score_stability,
            score_improvement_efficiency=item.score_improvement_efficiency,
        )
        for item in result.candidate_evaluations
    ]
    return DepartureRecommendationResponse(
        route=RouteModel(
            origin=result.route.origin,
            destination=result.route.destination,
        ),
        desired_arrival_time=result.desired_arrival_time,
        recommended_departure_time=result.recommended_departure_time,
        expected_arrival_time=result.expected_arrival_time,
        duration_seconds=result.duration.duration_seconds,
        meets_deadline=result.meets_deadline,
        provider=result.provider,
        provider_calls=result.provider_calls,
        candidates_checked=result.candidates_checked,
        planned_queries=result.planned_queries,
        total_candidates=result.total_candidates,
        latest_departure_time=result.latest_departure_time,
        latest_departure_arrival_time=result.latest_departure_arrival_time,
        latest_departure_duration_seconds=result.latest_departure_duration_seconds,
        safe_departure_time=result.safe_departure_time,
        safe_departure_duration_seconds=result.safe_departure_duration_seconds,
        recommended_score_total=result.recommended_score_total,
        baseline_score_total=result.baseline_score_total,
        candidate_evaluations=candidates,
    )


def _compute_safe_preview(
    *,
    desired_arrival_time: datetime,
    base_duration_seconds: int,
    tz: ZoneInfo,
    step_minutes: int,
) -> SafeDeparturePreviewResponse:
    safe_duration = math.ceil(base_duration_seconds * 1.25)
    baseline_departure = desired_arrival_time - timedelta(seconds=base_duration_seconds)
    now_floor = ensure_future_time(
        datetime.now(tz=tz),
        tz,
        step_minutes=step_minutes,
    )
    clamped = baseline_departure < now_floor
    safe_departure = now_floor if clamped else baseline_departure
    return SafeDeparturePreviewResponse(
        safe_departure_time=safe_departure,
        safe_duration_seconds=safe_duration,
        clamped_to_now=clamped,
    )


def _public_error_detail(exc: Exception) -> str:
    if isinstance(exc, NoFeasibleDepartureError):
        return "입력 조건에서 유효한 추천 출발 후보를 찾지 못했습니다."
    if isinstance(exc, ProviderError):
        return "교통 정보 제공자 호출 중 오류가 발생했습니다."
    return "추천 계산 중 오류가 발생했습니다."


def _public_error_reason(exc: Exception) -> str:
    if isinstance(exc, NoFeasibleDepartureError):
        return "no_feasible_departure"
    if isinstance(exc, ProviderError):
        return "provider_degraded"
    return "worker_failed"


def _safe_provider_error_bucket(exc: Exception) -> str | None:
    if not isinstance(exc, ProviderError):
        return None
    if exc.bucket in _SAFE_PROVIDER_ERROR_BUCKETS:
        return exc.bucket
    if exc.code in _SAFE_PROVIDER_ERROR_BUCKETS:
        return exc.code
    return None


def _stream_error_data(exc: Exception) -> dict[str, str]:
    data = {
        "detail": _public_error_detail(exc),
        "reason": _public_error_reason(exc),
    }
    provider_bucket = _safe_provider_error_bucket(exc)
    if provider_bucket is not None:
        data["provider_bucket"] = provider_bucket
    return data


def _log_stream_worker_failure(context: str, exc: Exception) -> None:
    reason = _public_error_reason(exc)
    if isinstance(exc, (NoFeasibleDepartureError, ProviderError)):
        _log.warning(
            "%s stream worker degraded reason=%s bucket=%s error=%s",
            context,
            reason,
            _safe_provider_error_bucket(exc),
            exc,
        )
        return
    _log.exception("%s stream worker failed reason=%s", context, reason)


def _prepare_service_and_coords(
    *,
    payload: ArrivalTimeRequest | DepartureRecommendationRequest,
    request: Request,
) -> tuple[TripTimeService, ZoneInfo]:
    service = service_from_request(request)
    tz = request.app.state.settings.timezone
    coords_map = _build_route_coords_map(payload=payload, request=request)
    pre_geocode_for_provider(
        service,
        payload.origin,
        payload.destination,
        coords_map=coords_map,
    )
    return service, tz


@router.post("/v1/trip/arrival-time", response_model=ArrivalTimeResponse)
def estimate_arrival_time(
    payload: ArrivalTimeRequest,
    request: Request,
) -> ArrivalTimeResponse:
    service, tz = _prepare_service_and_coords(payload=payload, request=request)
    departure = ensure_future_time(
        payload.departure_time,
        tz,
        step_minutes=request.app.state.settings.step_minutes,
    )
    result = service.estimate_arrival(
        origin=payload.origin,
        destination=payload.destination,
        departure_time=departure,
    )
    return _to_arrival_response(result)


@router.post(
    "/v1/trip/recommended-departure-time",
    response_model=DepartureRecommendationResponse,
)
def recommend_departure_time(
    payload: DepartureRecommendationRequest,
    request: Request,
) -> DepartureRecommendationResponse:
    service, tz = _prepare_service_and_coords(payload=payload, request=request)
    desired = ensure_future_time(
        payload.desired_arrival_time,
        tz,
        step_minutes=request.app.state.settings.step_minutes,
    )
    result = service.recommend_departure(
        origin=payload.origin,
        destination=payload.destination,
        desired_arrival_time=desired,
    )
    return _to_departure_response(result)


@router.post("/v1/trip/recommended-departure-time/stream")
def stream_recommended_departure_time(
    payload: DepartureRecommendationRequest,
    request: Request,
) -> StreamingResponse:
    service = service_from_request(request)
    settings = request.app.state.settings
    tz = settings.timezone
    desired = ensure_future_time(
        payload.desired_arrival_time,
        tz,
        step_minutes=settings.step_minutes,
    )
    coords_map = _build_route_coords_map(payload=payload, request=request)

    done_marker = object()
    event_queue: queue.Queue[object] = make_stream_queue()
    stream_status = {"ok": True}
    progress = {
        "checked": 0,
        "planned": 0,
        "remaining": 0,
        "total_candidates": 0,
    }

    def _on_initialized(total_candidates: int, planned_queries: int) -> None:
        progress["total_candidates"] = total_candidates
        progress["planned"] = planned_queries
        progress["remaining"] = max(0, planned_queries - progress["checked"])
        put_stream_item(event_queue, {"event": "plan", "data": progress.copy()})

    def _on_candidate(candidate: object) -> None:
        progress["checked"] += 1
        progress["remaining"] = max(0, progress["planned"] - progress["checked"])
        put_stream_item(
            event_queue,
            {
                "event": "candidate",
                "data": {
                    "candidate": RecommendationCandidateModel(
                        departure_time=candidate.departure_time,
                        arrival_time=candidate.arrival_time,
                        duration_seconds=candidate.duration_seconds,
                        meets_deadline=candidate.meets_deadline,
                        phase=candidate.phase,
                        score_total=candidate.score_total,
                        score_duration=candidate.score_duration,
                        score_time_proximity=candidate.score_time_proximity,
                        score_night_drive=candidate.score_night_drive,
                        score_stability=candidate.score_stability,
                        score_improvement_efficiency=(
                            candidate.score_improvement_efficiency
                        ),
                    ).model_dump(mode="json"),
                    "progress": progress.copy(),
                },
            },
            preserve=False,
        )

    def _worker() -> None:
        try:
            pre_geocode_for_provider(
                service,
                payload.origin,
                payload.destination,
                coords_map=coords_map,
            )
            recommendation = service.recommend_departure(
                origin=payload.origin,
                destination=payload.destination,
                desired_arrival_time=desired,
                on_search_initialized=_on_initialized,
                on_candidate_evaluated=_on_candidate,
            )
            put_stream_item(
                event_queue,
                {
                    "event": "recommendation",
                    "data": _to_departure_response(recommendation).model_dump(
                        mode="json"
                    ),
                }
            )
        except Exception as exc:
            stream_status.update({"ok": False, "reason": _public_error_reason(exc)})
            _log_stream_worker_failure("departure recommendation", exc)
            put_stream_item(
                event_queue,
                {
                    "event": "error",
                    "data": _stream_error_data(exc),
                }
            )
        finally:
            put_stream_item(event_queue, done_marker)

    worker = start_bounded_stream_worker(
        target=_worker,
        event_queue=event_queue,
        done_marker=done_marker,
        thread_name="departure-recommendation-stream",
    )

    def _stream() -> object:
        yield sse_encode("plan", progress.copy())
        for item in iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=worker,
            idle_timeout_seconds=STREAM_IDLE_TIMEOUT_SECONDS,
        ):
            if item["event"] in {"busy", "error"}:
                reason = str(item.get("data", {}).get("reason") or item["event"])
                stream_status.update({"ok": False, "reason": reason})
            yield sse_encode(item["event"], item["data"])
        yield sse_encode("end", stream_status.copy())

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/v1/trip/arrival-time-with-recommendation",
    response_model=ArrivalWithRecommendationResponse,
)
def estimate_arrival_with_recommendation(
    payload: ArrivalTimeRequest,
    request: Request,
) -> ArrivalWithRecommendationResponse:
    service, tz = _prepare_service_and_coords(payload=payload, request=request)
    departure = ensure_future_time(
        payload.departure_time,
        tz,
        step_minutes=request.app.state.settings.step_minutes,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        recommendation_future = executor.submit(
            service.recommend_departure,
            origin=payload.origin,
            destination=payload.destination,
            desired_arrival_time=departure,
            analysis_start_time=departure,
        )
        arrival = service.estimate_arrival(
            origin=payload.origin,
            destination=payload.destination,
            departure_time=departure,
        )
        recommendation = recommendation_future.result()

    immediate_safe = _compute_safe_preview(
        desired_arrival_time=arrival.arrival_time,
        base_duration_seconds=arrival.duration.duration_seconds,
        tz=tz,
        step_minutes=request.app.state.settings.step_minutes,
    )
    return ArrivalWithRecommendationResponse(
        arrival=_to_arrival_response(arrival),
        recommendation=_to_departure_response(recommendation),
        immediate_safe_departure=immediate_safe,
    )


@router.post("/v1/trip/arrival-time-with-recommendation/stream")
def stream_arrival_with_recommendation(
    payload: ArrivalTimeRequest,
    request: Request,
) -> StreamingResponse:
    service = service_from_request(request)
    settings = request.app.state.settings
    tz = settings.timezone
    departure = ensure_future_time(
        payload.departure_time,
        tz,
        step_minutes=settings.step_minutes,
    )
    coords_map = _build_route_coords_map(payload=payload, request=request)

    done_marker = object()
    event_queue: queue.Queue[object] = make_stream_queue()
    stream_status = {"ok": True}
    progress = {
        "checked": 0,
        "planned": 0,
        "remaining": 0,
        "total_candidates": 0,
    }

    def _on_initialized(total_candidates: int, planned_queries: int) -> None:
        progress["total_candidates"] = total_candidates
        progress["planned"] = planned_queries
        progress["remaining"] = max(0, planned_queries - progress["checked"])
        put_stream_item(event_queue, {"event": "plan", "data": progress.copy()})

    def _on_candidate(candidate: object) -> None:
        progress["checked"] += 1
        progress["remaining"] = max(0, progress["planned"] - progress["checked"])
        put_stream_item(
            event_queue,
            {
                "event": "candidate",
                "data": {
                    "candidate": RecommendationCandidateModel(
                        departure_time=candidate.departure_time,
                        arrival_time=candidate.arrival_time,
                        duration_seconds=candidate.duration_seconds,
                        meets_deadline=candidate.meets_deadline,
                        phase=candidate.phase,
                        score_total=candidate.score_total,
                        score_duration=candidate.score_duration,
                        score_time_proximity=candidate.score_time_proximity,
                        score_night_drive=candidate.score_night_drive,
                        score_stability=candidate.score_stability,
                        score_improvement_efficiency=(
                            candidate.score_improvement_efficiency
                        ),
                    ).model_dump(mode="json"),
                    "progress": progress.copy(),
                },
            },
            preserve=False,
        )

    def _worker() -> None:
        try:
            pre_geocode_for_provider(
                service,
                payload.origin,
                payload.destination,
                coords_map=coords_map,
            )
            arrival = service.estimate_arrival(
                origin=payload.origin,
                destination=payload.destination,
                departure_time=departure,
            )
            immediate_safe = _compute_safe_preview(
                desired_arrival_time=arrival.arrival_time,
                base_duration_seconds=arrival.duration.duration_seconds,
                tz=tz,
                step_minutes=settings.step_minutes,
            )
            put_stream_item(
                event_queue,
                {
                    "event": "arrival",
                    "data": {
                        "arrival": _to_arrival_response(arrival).model_dump(
                            mode="json",
                        ),
                        "immediate_safe_departure": immediate_safe.model_dump(
                            mode="json",
                        ),
                        "progress": progress.copy(),
                    },
                },
            )
            recommendation = service.recommend_departure(
                origin=payload.origin,
                destination=payload.destination,
                desired_arrival_time=departure,
                analysis_start_time=departure,
                on_search_initialized=_on_initialized,
                on_candidate_evaluated=_on_candidate,
            )
            put_stream_item(
                event_queue,
                {
                    "event": "recommendation",
                    "data": _to_departure_response(recommendation).model_dump(
                        mode="json"
                    ),
                }
            )
        except Exception as exc:
            stream_status.update({"ok": False, "reason": _public_error_reason(exc)})
            _log_stream_worker_failure("arrival recommendation", exc)
            put_stream_item(
                event_queue,
                {
                    "event": "error",
                    "data": _stream_error_data(exc),
                }
            )
        finally:
            put_stream_item(event_queue, done_marker)

    worker = start_bounded_stream_worker(
        target=_worker,
        event_queue=event_queue,
        done_marker=done_marker,
        thread_name="arrival-recommendation-stream",
    )

    def _stream() -> object:
        yield sse_encode("plan", progress.copy())
        for item in iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=worker,
            idle_timeout_seconds=STREAM_IDLE_TIMEOUT_SECONDS,
        ):
            if item["event"] in {"busy", "error"}:
                reason = str(item.get("data", {}).get("reason") or item["event"])
                stream_status.update({"ok": False, "reason": reason})
            yield sse_encode(item["event"], item["data"])
        yield sse_encode("end", stream_status.copy())

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
