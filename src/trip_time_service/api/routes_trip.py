from __future__ import annotations

import logging
import math
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

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
    sse_encode,
)
from trip_time_service.providers.base import ProviderError
from trip_time_service.services.trip_time_service import (
    NoFeasibleDepartureError,
    TripTimeService,
)

router = APIRouter()
_log = logging.getLogger(__name__)


def _extract_coords_map(
    origin: str,
    destination: str,
    origin_coords: object | None,
    dest_coords: object | None,
) -> dict[str, tuple[float, float]]:
    coords_map: dict[str, tuple[float, float]] = {}
    if origin_coords:
        coords_map[origin] = (origin_coords.lat, origin_coords.lon)
    if dest_coords:
        coords_map[destination] = (dest_coords.lat, dest_coords.lon)
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
) -> SafeDeparturePreviewResponse:
    safe_duration = math.ceil(base_duration_seconds * 1.25)
    baseline_departure = desired_arrival_time - timedelta(seconds=base_duration_seconds)
    now_floor = ensure_future_time(datetime.now(tz=tz), tz)
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


def _prepare_service_and_coords(
    *,
    payload: ArrivalTimeRequest | DepartureRecommendationRequest,
    request: Request,
) -> tuple[TripTimeService, ZoneInfo]:
    service = service_from_request(request)
    tz = request.app.state.settings.timezone
    coords_map = _extract_coords_map(
        payload.origin,
        payload.destination,
        payload.origin_coords,
        payload.dest_coords,
    )
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
    departure = ensure_future_time(payload.departure_time, tz)
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
    desired = ensure_future_time(payload.desired_arrival_time, tz)
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
    service, tz = _prepare_service_and_coords(payload=payload, request=request)
    desired = ensure_future_time(payload.desired_arrival_time, tz)

    done_marker = object()
    event_queue: queue.Queue[object] = queue.Queue()
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
        event_queue.put({"event": "plan", "data": progress.copy()})

    def _on_candidate(candidate: object) -> None:
        progress["checked"] += 1
        progress["remaining"] = max(0, progress["planned"] - progress["checked"])
        event_queue.put(
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
            }
        )

    def _worker() -> None:
        try:
            recommendation = service.recommend_departure(
                origin=payload.origin,
                destination=payload.destination,
                desired_arrival_time=desired,
                on_search_initialized=_on_initialized,
                on_candidate_evaluated=_on_candidate,
            )
            event_queue.put(
                {
                    "event": "recommendation",
                    "data": _to_departure_response(recommendation).model_dump(
                        mode="json"
                    ),
                }
            )
        except Exception as exc:
            _log.exception("departure recommendation stream worker failed")
            event_queue.put(
                {
                    "event": "error",
                    "data": {"detail": _public_error_detail(exc)},
                }
            )
        finally:
            event_queue.put(done_marker)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    def _stream() -> object:
        yield sse_encode("plan", progress.copy())
        for item in iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=worker,
            idle_timeout_seconds=STREAM_IDLE_TIMEOUT_SECONDS,
        ):
            yield sse_encode(item["event"], item["data"])
        yield sse_encode("end", {"ok": True})

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
    departure = ensure_future_time(payload.departure_time, tz)

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
    service, tz = _prepare_service_and_coords(payload=payload, request=request)
    departure = ensure_future_time(payload.departure_time, tz)

    done_marker = object()
    event_queue: queue.Queue[object] = queue.Queue()
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
        event_queue.put({"event": "plan", "data": progress.copy()})

    def _on_candidate(candidate: object) -> None:
        progress["checked"] += 1
        progress["remaining"] = max(0, progress["planned"] - progress["checked"])
        event_queue.put(
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
            }
        )

    def _worker() -> None:
        try:
            recommendation = service.recommend_departure(
                origin=payload.origin,
                destination=payload.destination,
                desired_arrival_time=departure,
                analysis_start_time=departure,
                on_search_initialized=_on_initialized,
                on_candidate_evaluated=_on_candidate,
            )
            event_queue.put(
                {
                    "event": "recommendation",
                    "data": _to_departure_response(recommendation).model_dump(
                        mode="json"
                    ),
                }
            )
        except Exception as exc:
            _log.exception("arrival recommendation stream worker failed")
            event_queue.put(
                {
                    "event": "error",
                    "data": {"detail": _public_error_detail(exc)},
                }
            )
        finally:
            event_queue.put(done_marker)

    arrival = service.estimate_arrival(
        origin=payload.origin,
        destination=payload.destination,
        departure_time=departure,
    )
    immediate_safe = _compute_safe_preview(
        desired_arrival_time=arrival.arrival_time,
        base_duration_seconds=arrival.duration.duration_seconds,
        tz=tz,
    )
    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()

    def _stream() -> object:
        first_payload = {
            "arrival": _to_arrival_response(arrival).model_dump(mode="json"),
            "immediate_safe_departure": immediate_safe.model_dump(mode="json"),
            "progress": progress.copy(),
        }
        yield sse_encode("arrival", first_payload)
        for item in iter_stream_events(
            event_queue=event_queue,
            done_marker=done_marker,
            worker=worker,
            idle_timeout_seconds=STREAM_IDLE_TIMEOUT_SECONDS,
        ):
            yield sse_encode(item["event"], item["data"])
        yield sse_encode("end", {"ok": True})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
