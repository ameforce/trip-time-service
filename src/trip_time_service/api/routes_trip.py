from __future__ import annotations

import asyncio
import logging
import math
import queue
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import CancelledError
from contextlib import suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from trip_time_service.api.common import ensure_future_time, service_from_request
from trip_time_service.api.geocode_services import resolve_route_for_provider
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
    STREAM_BUSY_ERROR_MESSAGE,
    STREAM_IDLE_TIMEOUT_SECONDS,
    TripStreamingResponse,
    iter_stream_events,
    make_stream_queue,
    put_stream_item,
    route_work_slot,
    sse_encode,
    start_bounded_stream_worker,
)
from trip_time_service.core.models import (
    ArrivalEstimate,
    DepartureRecommendation,
    Route,
)
from trip_time_service.core.time_utils import ensure_tzaware
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
type PlaceCoords = tuple[float, float]
type RouteCoords = tuple[PlaceCoords | None, PlaceCoords | None]


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


def _build_route_coords(
    *,
    payload: ArrivalTimeRequest | DepartureRecommendationRequest,
    request: Request,
) -> RouteCoords:
    mode = _resolve_route_input_contract_mode(request)
    origin_coords = None
    dest_coords = None

    if payload.origin_place is not None:
        origin_coords = _extract_route_place_coords(
            "origin_place",
            payload.origin_place,
            payload.origin_coords,
        )
    elif payload.origin_coords is not None:
        origin_coords = _extract_legacy_coords(payload.origin_coords)

    if payload.dest_place is not None:
        dest_coords = _extract_route_place_coords(
            "dest_place",
            payload.dest_place,
            payload.dest_coords,
        )
    elif payload.dest_coords is not None:
        dest_coords = _extract_legacy_coords(payload.dest_coords)

    if mode == "strict" and (origin_coords is None or dest_coords is None):
        raise _contract_error(
            "coords_required",
            "Strict route input contract requires resolved origin and "
            "destination coordinates.",
        )
    if mode == "warn" and origin_coords is None and dest_coords is None:
        _log.warning("legacy_text_route_input mode=warn")
    return origin_coords, dest_coords


def _to_arrival_response(result: ArrivalEstimate) -> ArrivalTimeResponse:
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


def _to_departure_response(
    result: DepartureRecommendation,
) -> DepartureRecommendationResponse:
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
    departure_time: datetime,
    base_duration_seconds: int,
) -> SafeDeparturePreviewResponse:
    return SafeDeparturePreviewResponse(
        safe_departure_time=departure_time,
        safe_duration_seconds=math.ceil(base_duration_seconds * 1.25),
        clamped_to_now=False,
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


def trip_error_data(exc: Exception) -> dict[str, str]:
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


def _resolve_trip_route(
    service: TripTimeService,
    payload: ArrivalTimeRequest | DepartureRecommendationRequest,
    coords: RouteCoords,
    cancel_event: threading.Event,
) -> Route:
    return resolve_route_for_provider(
        service,
        payload.origin,
        payload.destination,
        origin_coords=coords[0],
        dest_coords=coords[1],
        cancel_event=cancel_event,
    )


def _desired_arrival_time(value: datetime, tz: ZoneInfo) -> datetime:
    desired = ensure_tzaware(value, tz)
    if desired <= datetime.now(tz=tz):
        raise _contract_error(
            "arrival_deadline_in_past",
            "희망 도착 시각은 현재 시각 이후로 입력해 주세요.",
        )
    return desired


async def _cancel_on_disconnect(
    request: Request, cancel_event: threading.Event
) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass
    cancel_event.set()


async def _run_trip_work[T](
    request: Request, operation: Callable[[threading.Event], T]
) -> T:
    cancel_event = threading.Event()

    def _admitted() -> T:
        with route_work_slot() as admitted:
            if not admitted:
                raise _contract_error(
                    "capacity_exhausted", STREAM_BUSY_ERROR_MESSAGE, status_code=503
                )
            if cancel_event.is_set():
                raise CancelledError
            return operation(cancel_event)

    disconnect = asyncio.create_task(_cancel_on_disconnect(request, cancel_event))
    try:
        return await run_in_threadpool(_admitted)
    except CancelledError as exc:
        raise _contract_error(
            "request_cancelled", "검색이 중단되었습니다.", status_code=503
        ) from exc
    finally:
        cancel_event.set()
        disconnect.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect


def _trip_stream_response(
    event_queue: queue.Queue[object],
    done_marker: object,
    worker: object,
    progress: dict[str, int],
    cancel_event: threading.Event,
) -> StreamingResponse:
    def _stream() -> Iterator[str]:
        status: dict[str, bool | str] = {"ok": False, "reason": "worker_failed"}
        try:
            yield sse_encode("plan", progress.copy())
            for item in iter_stream_events(
                event_queue=event_queue,
                done_marker=done_marker,
                worker=worker,
                cancel_event=cancel_event,
                idle_timeout_seconds=STREAM_IDLE_TIMEOUT_SECONDS,
            ):
                event = item["event"]
                data = item["data"]
                if event == "keepalive":
                    yield ": keep-alive\n\n"
                    continue
                if event in {"busy", "error"}:
                    status = {"ok": False, "reason": str(data.get("reason") or event)}
                elif event == "recommendation":
                    status = {"ok": True}
                yield sse_encode(event, data)
            yield sse_encode("end", status)
        finally:
            cancel_event.set()

    return TripStreamingResponse(_stream(), cancel_event=cancel_event)


@router.post("/v1/trip/arrival-time", response_model=ArrivalTimeResponse)
async def estimate_arrival_time(
    payload: ArrivalTimeRequest,
    request: Request,
) -> ArrivalTimeResponse:
    service = service_from_request(request)
    settings = request.app.state.settings
    coords = _build_route_coords(payload=payload, request=request)
    departure = ensure_future_time(
        payload.departure_time,
        settings.timezone,
        step_minutes=settings.step_minutes,
    )

    def _estimate(cancel_event: threading.Event) -> ArrivalEstimate:
        route = _resolve_trip_route(service, payload, coords, cancel_event)
        return service.estimate_arrival(
            origin=payload.origin,
            destination=payload.destination,
            departure_time=departure,
            route=route,
            cancel_event=cancel_event,
        )

    result = await _run_trip_work(request, _estimate)
    return _to_arrival_response(result)


@router.post(
    "/v1/trip/recommended-departure-time",
    response_model=DepartureRecommendationResponse,
)
async def recommend_departure_time(
    payload: DepartureRecommendationRequest,
    request: Request,
) -> DepartureRecommendationResponse:
    service = service_from_request(request)
    coords = _build_route_coords(payload=payload, request=request)
    desired = _desired_arrival_time(
        payload.desired_arrival_time, request.app.state.settings.timezone
    )

    def _recommend(cancel_event: threading.Event) -> DepartureRecommendation:
        route = _resolve_trip_route(service, payload, coords, cancel_event)
        return service.recommend_departure(
            origin=payload.origin,
            destination=payload.destination,
            desired_arrival_time=desired,
            route=route,
            cancel_event=cancel_event,
        )

    result = await _run_trip_work(request, _recommend)
    return _to_departure_response(result)


@router.post("/v1/trip/recommended-departure-time/stream")
def stream_recommended_departure_time(
    payload: DepartureRecommendationRequest,
    request: Request,
) -> StreamingResponse:
    service = service_from_request(request)
    coords = _build_route_coords(payload=payload, request=request)
    desired = _desired_arrival_time(
        payload.desired_arrival_time, request.app.state.settings.timezone
    )

    done_marker = object()
    cancel_event = threading.Event()
    event_queue: queue.Queue[object] = make_stream_queue()
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
            route = _resolve_trip_route(service, payload, coords, cancel_event)
            recommendation = service.recommend_departure(
                origin=payload.origin,
                destination=payload.destination,
                desired_arrival_time=desired,
                route=route,
                cancel_event=cancel_event,
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
                },
            )
        except CancelledError:
            return
        except Exception as exc:
            _log_stream_worker_failure("departure recommendation", exc)
            put_stream_item(
                event_queue,
                {
                    "event": "error",
                    "data": trip_error_data(exc),
                },
            )
        finally:
            put_stream_item(event_queue, done_marker)

    worker = start_bounded_stream_worker(
        target=_worker,
        event_queue=event_queue,
        done_marker=done_marker,
        thread_name="departure-recommendation-stream",
    )

    return _trip_stream_response(
        event_queue, done_marker, worker, progress, cancel_event
    )


@router.post(
    "/v1/trip/arrival-time-with-recommendation",
    response_model=ArrivalWithRecommendationResponse,
)
async def estimate_arrival_with_recommendation(
    payload: ArrivalTimeRequest,
    request: Request,
) -> ArrivalWithRecommendationResponse:
    service = service_from_request(request)
    settings = request.app.state.settings
    coords = _build_route_coords(payload=payload, request=request)
    departure = ensure_future_time(
        payload.departure_time,
        settings.timezone,
        step_minutes=settings.step_minutes,
    )

    def _estimate(cancel_event: threading.Event) -> ArrivalWithRecommendationResponse:
        route = _resolve_trip_route(service, payload, coords, cancel_event)
        arrival = service.estimate_arrival(
            origin=payload.origin,
            destination=payload.destination,
            departure_time=departure,
            route=route,
            cancel_event=cancel_event,
        )
        recommendation = service.recommend_departure(
            origin=payload.origin,
            destination=payload.destination,
            desired_arrival_time=departure,
            analysis_start_time=departure,
            route=route,
            cancel_event=cancel_event,
        )
        return ArrivalWithRecommendationResponse(
            arrival=_to_arrival_response(arrival),
            recommendation=_to_departure_response(recommendation),
            immediate_safe_departure=_compute_safe_preview(
                departure_time=arrival.departure_time,
                base_duration_seconds=arrival.duration.duration_seconds,
            ),
        )

    return await _run_trip_work(request, _estimate)


@router.post("/v1/trip/arrival-time-with-recommendation/stream")
def stream_arrival_with_recommendation(
    payload: ArrivalTimeRequest,
    request: Request,
) -> StreamingResponse:
    service = service_from_request(request)
    settings = request.app.state.settings
    coords = _build_route_coords(payload=payload, request=request)
    departure = ensure_future_time(
        payload.departure_time,
        settings.timezone,
        step_minutes=settings.step_minutes,
    )

    done_marker = object()
    cancel_event = threading.Event()
    event_queue: queue.Queue[object] = make_stream_queue()
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
            route = _resolve_trip_route(service, payload, coords, cancel_event)
            arrival = service.estimate_arrival(
                origin=payload.origin,
                destination=payload.destination,
                departure_time=departure,
                route=route,
                cancel_event=cancel_event,
            )
            immediate_safe = _compute_safe_preview(
                departure_time=arrival.departure_time,
                base_duration_seconds=arrival.duration.duration_seconds,
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
                route=route,
                cancel_event=cancel_event,
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
        except CancelledError:
            return
        except Exception as exc:
            _log_stream_worker_failure("arrival recommendation", exc)
            put_stream_item(
                event_queue,
                {
                    "event": "error",
                    "data": trip_error_data(exc),
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

    return _trip_stream_response(
        event_queue, done_marker, worker, progress, cancel_event
    )
