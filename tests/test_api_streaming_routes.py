from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import CancelledError
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.api import routes_trip, streaming
from trip_time_service.api.schemas import (
    ArrivalTimeRequest,
    DepartureRecommendationRequest,
)
from trip_time_service.config import Settings
from trip_time_service.core.models import (
    ArrivalEstimate,
    DepartureRecommendation,
    DriveDuration,
    RecommendationCandidate,
    Route,
)
from trip_time_service.providers.base import ProviderError

_TZ = ZoneInfo("Asia/Seoul")


def _settings() -> Settings:
    return Settings(
        timezone=_TZ,
        headless=True,
        cache_ttl=timedelta(seconds=600),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="fake",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
        cors_allowed_origins=(),
        recommend_min_samples=2,
    )


def _request(service: object) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=_settings(),
                trip_time_service=service,
            ),
        )
    )


async def _next_text(response: object) -> str:
    chunk = await anext(response.body_iterator)
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8")
    return str(chunk)


async def _read_remaining(response: object) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
    return "".join(chunks)


async def _first_then_rest(
    response: object,
    after_first=None,
    before_rest=None,
) -> tuple[str, str, object]:
    first = await _next_text(response)
    observed = before_rest() if before_rest is not None else None
    if after_first is not None:
        after_first()
    rest = await _read_remaining(response)
    return first, rest, observed


class _FakeTripService:
    def __init__(self) -> None:
        self.recommend_called = False
        self.arrival_called = False

    def estimate_arrival(
        self,
        *,
        origin: str,
        destination: str,
        departure_time: datetime,
        route: Route | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ArrivalEstimate:
        self.arrival_called = True
        duration = DriveDuration(duration_seconds=900, fetched_at=departure_time)
        return ArrivalEstimate(
            route=route or Route.of(origin, destination),
            departure_time=departure_time,
            arrival_time=departure_time + timedelta(seconds=duration.duration_seconds),
            duration=duration,
            provider="fake",
            cache_hit=False,
        )

    def recommend_departure(
        self,
        *,
        origin: str,
        destination: str,
        desired_arrival_time: datetime,
        analysis_start_time: datetime | None = None,
        route: Route | None = None,
        cancel_event: threading.Event | None = None,
        on_search_initialized=None,
        on_candidate_evaluated=None,
    ) -> DepartureRecommendation:
        self.recommend_called = True
        route = route or Route.of(origin, destination)
        departure = analysis_start_time or desired_arrival_time - timedelta(minutes=30)
        duration = DriveDuration(duration_seconds=900, fetched_at=departure)
        arrival = departure + timedelta(seconds=duration.duration_seconds)
        if on_search_initialized is not None:
            on_search_initialized(1, 1)
        candidate = RecommendationCandidate(
            departure_time=departure,
            arrival_time=arrival,
            duration_seconds=duration.duration_seconds,
            meets_deadline=True,
            phase="fake",
        )
        if on_candidate_evaluated is not None:
            on_candidate_evaluated(candidate)
        return DepartureRecommendation(
            route=route,
            desired_arrival_time=desired_arrival_time,
            recommended_departure_time=departure,
            expected_arrival_time=arrival,
            duration=duration,
            provider="fake",
            provider_calls=1,
            candidates_checked=1,
            meets_deadline=True,
            planned_queries=1,
            total_candidates=1,
            latest_departure_time=departure,
            latest_departure_arrival_time=arrival,
            latest_departure_duration_seconds=duration.duration_seconds,
            safe_departure_time=departure,
            safe_departure_duration_seconds=duration.duration_seconds,
            candidate_evaluations=(candidate,),
        )


class _FailingTripService(_FakeTripService):
    def recommend_departure(self, **kwargs) -> DepartureRecommendation:  # type: ignore[override]
        raise ProviderError("provider panel read failed")


def test_departure_stream_yields_plan_before_slow_pre_geocode(monkeypatch) -> None:
    service = _FakeTripService()
    release = threading.Event()

    def slow_resolve(service, origin, destination, **kwargs) -> Route:
        release.wait(timeout=2.0)
        return Route.of(origin, destination)

    monkeypatch.setattr(routes_trip, "resolve_route_for_provider", slow_resolve)
    payload = DepartureRecommendationRequest(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
    )

    started = time.perf_counter()
    response = routes_trip.stream_recommended_departure_time(payload, _request(service))
    first, rest, _ = asyncio.run(_first_then_rest(response, release.set))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert first.startswith("event: plan")
    assert "event: recommendation" in rest


def test_arrival_stream_yields_plan_before_arrival_estimate(monkeypatch) -> None:
    service = _FakeTripService()
    release = threading.Event()

    def slow_resolve(service, origin, destination, **kwargs) -> Route:
        release.wait(timeout=2.0)
        return Route.of(origin, destination)

    monkeypatch.setattr(routes_trip, "resolve_route_for_provider", slow_resolve)
    payload = ArrivalTimeRequest(
        origin="강남역",
        destination="판교역",
        departure_time=datetime.now(tz=_TZ) + timedelta(minutes=20),
    )

    started = time.perf_counter()
    response = routes_trip.stream_arrival_with_recommendation(
        payload,
        _request(service),
    )
    first, rest, arrival_called_before_release = asyncio.run(
        _first_then_rest(
            response,
            release.set,
            lambda: service.arrival_called,
        )
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2
    assert first.startswith("event: plan")
    assert arrival_called_before_release is False

    assert "event: arrival" in rest
    assert "event: recommendation" in rest


def test_stream_provider_error_has_non_success_terminal_event() -> None:
    payload = DepartureRecommendationRequest(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
    )
    response = routes_trip.stream_recommended_departure_time(
        payload,
        _request(_FailingTripService()),
    )

    body = asyncio.run(_read_remaining(response))

    assert "event: error" in body
    assert "provider_degraded" in body
    assert '"ok": false' in body


def test_stream_capacity_exhaustion_returns_busy(monkeypatch) -> None:
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(streaming, "_STREAM_WORKER_SEMAPHORE", semaphore)
    with semaphore:
        payload = DepartureRecommendationRequest(
            origin="강남역",
            destination="판교역",
            desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
        )
        response = routes_trip.stream_recommended_departure_time(
            payload,
            _request(_FakeTripService()),
        )
        body = asyncio.run(_read_remaining(response))
    assert "event: busy" in body
    assert "capacity_exhausted" in body
    assert '"ok": false' in body


def test_stream_idle_timeout_uses_stable_reason(monkeypatch) -> None:
    service = _FakeTripService()
    stopped = threading.Event()

    def slow_resolve(service, origin, destination, *, cancel_event, **kwargs) -> Route:
        if cancel_event.wait(timeout=2.0):
            stopped.set()
            raise CancelledError
        return Route.of(origin, destination)

    monkeypatch.setattr(routes_trip, "resolve_route_for_provider", slow_resolve)
    monkeypatch.setattr(routes_trip, "STREAM_IDLE_TIMEOUT_SECONDS", 0.01)
    payload = DepartureRecommendationRequest(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
    )
    response = routes_trip.stream_recommended_departure_time(
        payload,
        _request(service),
    )

    first, rest, _ = asyncio.run(_first_then_rest(response))

    assert stopped.wait(timeout=1.0)
    assert not service.recommend_called
    assert first.startswith("event: plan")
    assert "event: error" in rest
    assert "stream_stall_timeout" in rest
    assert "event: end" in rest
    assert '"ok": false' in rest
    assert '"reason": "stream_stall_timeout"' in rest


def test_disconnect_cancels_stream_work_and_releases_admission(monkeypatch) -> None:
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(streaming, "_STREAM_WORKER_SEMAPHORE", semaphore)
    workers: list[threading.Thread] = []

    def observe_worker(**kwargs):
        worker = streaming.start_bounded_stream_worker(**kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(routes_trip, "start_bounded_stream_worker", observe_worker)
    cancelled = threading.Event()

    class _SlowService(_FakeTripService):
        def recommend_departure(self, *, cancel_event, **kwargs):
            if cancel_event.wait(2):
                cancelled.set()
                raise CancelledError
            raise AssertionError("stream worker did not receive disconnect")

    payload = DepartureRecommendationRequest(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
    )
    response = routes_trip.stream_recommended_departure_time(
        payload, _request(_SlowService())
    )

    async def disconnect_after_plan():
        plan_sent = asyncio.Event()

        async def receive():
            await plan_sent.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message.get("body", b"").startswith(b"event: plan"):
                plan_sent.set()

        await asyncio.wait_for(
            response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send),
            timeout=2,
        )

    asyncio.run(disconnect_after_plan())
    assert len(workers) == 1
    workers[0].join(timeout=1)
    assert not workers[0].is_alive()
    assert cancelled.is_set()
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_non_streaming_disconnect_releases_slot_after_work_stops(monkeypatch) -> None:
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(streaming, "_STREAM_WORKER_SEMAPHORE", semaphore)
    worker_started = threading.Event()

    async def receive():
        assert await asyncio.to_thread(worker_started.wait, 1)
        return {"type": "http.disconnect"}

    def operation(cancel_event: threading.Event) -> None:
        worker_started.set()
        assert cancel_event.wait(1)
        assert not semaphore.acquire(blocking=False)
        raise CancelledError

    with pytest.raises(routes_trip.RouteInputContractError) as raised:
        asyncio.run(
            routes_trip._run_trip_work(SimpleNamespace(receive=receive), operation)
        )

    assert raised.value.reason == "request_cancelled"
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_stream_queue_has_room_for_error_and_terminal_marker(monkeypatch) -> None:
    monkeypatch.setenv("TTS_STREAM_EVENT_QUEUE_SIZE", "1")

    event_queue = streaming.make_stream_queue()
    done_marker = object()

    streaming.put_stream_item(
        event_queue,
        {"event": "busy", "data": {"reason": "capacity_exhausted"}},
    )
    streaming.put_stream_item(event_queue, done_marker)

    assert event_queue.get_nowait() == {
        "event": "busy",
        "data": {"reason": "capacity_exhausted"},
    }
    assert event_queue.get_nowait() is done_marker
