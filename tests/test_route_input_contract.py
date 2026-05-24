from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trip_time_service import config
from trip_time_service.api import routes_trip
from trip_time_service.config import Settings
from trip_time_service.core.models import (
    ArrivalEstimate,
    DepartureRecommendation,
    DriveDuration,
    RecommendationCandidate,
    Route,
)

_TZ = ZoneInfo("Asia/Seoul")


def _settings(route_input_contract: str = "warn") -> Settings:
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
        route_input_contract=route_input_contract,
    )


class _FakeTripService:
    def __init__(self) -> None:
        self.arrival_calls = 0
        self.recommend_calls = 0

    def estimate_arrival(
        self,
        *,
        origin: str,
        destination: str,
        departure_time: datetime,
    ) -> ArrivalEstimate:
        self.arrival_calls += 1
        duration = DriveDuration(duration_seconds=900, fetched_at=departure_time)
        return ArrivalEstimate(
            route=Route.of(origin, destination),
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
        on_search_initialized=None,
        on_candidate_evaluated=None,
    ) -> DepartureRecommendation:
        self.recommend_calls += 1
        route = Route.of(origin, destination)
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


def _client(
    service: _FakeTripService,
    *,
    route_input_contract: str = "warn",
) -> TestClient:
    app = FastAPI()
    app.state.settings = _settings(route_input_contract=route_input_contract)
    app.state.trip_time_service = service
    app.include_router(routes_trip.router)
    app.add_exception_handler(
        routes_trip.RouteInputContractError,
        routes_trip.route_input_contract_exception_handler,
    )
    return TestClient(app)


def _arrival_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin": "강남역",
        "destination": "판교역",
        "departure_time": (datetime.now(tz=_TZ) + timedelta(hours=1)).isoformat(),
    }
    body.update(overrides)
    return body


def _departure_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "origin": "강남역",
        "destination": "판교역",
        "desired_arrival_time": (
            datetime.now(tz=_TZ) + timedelta(hours=1)
        ).isoformat(),
    }
    body.update(overrides)
    return body


def _ready_place(lat: float = 37.4979, lon: float = 127.0276) -> dict[str, Any]:
    return {
        "query": "강남역",
        "display_name": "강남역",
        "canonical_query": "강남역",
        "selection_kind": "poi",
        "coords_ready": True,
        "lat": lat,
        "lon": lon,
    }


def _unresolved_place() -> dict[str, Any]:
    return {
        "query": "강남역",
        "display_name": "강남역",
        "canonical_query": "강남역",
        "selection_kind": "poi",
        "coords_ready": False,
        "degraded_reason": "coords_unresolved",
    }


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/trip/arrival-time", _arrival_body),
        ("/v1/trip/recommended-departure-time", _departure_body),
        ("/v1/trip/arrival-time-with-recommendation", _arrival_body),
        ("/v1/trip/recommended-departure-time/stream", _departure_body),
        ("/v1/trip/arrival-time-with-recommendation/stream", _arrival_body),
    ],
)
def test_unresolved_route_place_rejects_before_provider_for_all_trip_routes(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    body,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: pre_geocode_calls.append(kwargs),
    )
    client = _client(service)

    response = client.post(
        path,
        json=body(origin_place=_unresolved_place(), dest_place=_ready_place()),
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "coords_unresolved"
    assert "text/event-stream" not in response.headers.get("content-type", "")
    assert pre_geocode_calls == []
    assert service.arrival_calls == 0
    assert service.recommend_calls == 0


def test_strict_text_only_request_requires_coords_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: pre_geocode_calls.append(kwargs),
    )
    client = _client(service)

    response = client.post(
        "/v1/trip/arrival-time",
        json=_arrival_body(),
        headers={"X-TTS-Route-Input-Contract": "strict"},
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "coords_required"
    assert pre_geocode_calls == []
    assert service.arrival_calls == 0


def test_default_warn_text_only_request_preserves_legacy_provider_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []

    def record_pre_geocode(*args, **kwargs) -> None:
        pre_geocode_calls.append(kwargs)

    monkeypatch.setattr(routes_trip, "pre_geocode_for_provider", record_pre_geocode)
    client = _client(service)

    response = client.post("/v1/trip/arrival-time", json=_arrival_body())

    assert response.status_code == 200
    assert pre_geocode_calls == [{"coords_map": {}}]
    assert service.arrival_calls == 1


def test_conflicting_route_place_and_legacy_coords_rejects_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: pre_geocode_calls.append(kwargs),
    )
    client = _client(service)

    response = client.post(
        "/v1/trip/arrival-time",
        json=_arrival_body(
            origin_place=_ready_place(37.4979, 127.0276),
            dest_place=_ready_place(37.3947, 127.1112),
            origin_coords={"lat": 37.1, "lon": 127.1},
        ),
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "coords_conflict"
    assert pre_geocode_calls == []
    assert service.arrival_calls == 0


def test_malformed_route_place_returns_stable_contract_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: pre_geocode_calls.append(kwargs),
    )
    client = _client(service)

    response = client.post(
        "/v1/trip/arrival-time",
        json=_arrival_body(
            origin_place={"coords_ready": "true", "lat": "bad", "lon": 127.0},
            dest_place=_ready_place(37.3947, 127.1112),
        ),
    )

    assert response.status_code == 422
    assert response.json()["reason"] == "coords_contract_invalid"
    assert pre_geocode_calls == []
    assert service.arrival_calls == 0


def test_invalid_contract_header_returns_400_stable_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: pre_geocode_calls.append(kwargs),
    )
    client = _client(service)

    response = client.post(
        "/v1/trip/arrival-time",
        json=_arrival_body(
            origin_place=_ready_place(37.4979, 127.0276),
            dest_place=_ready_place(37.3947, 127.1112),
        ),
        headers={"X-TTS-Route-Input-Contract": "maybe"},
    )

    assert response.status_code == 400
    assert response.json()["reason"] == "coords_contract_invalid"
    assert pre_geocode_calls == []
    assert service.arrival_calls == 0


def test_invalid_env_contract_mode_defaults_to_warn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("TTS_ROUTE_INPUT_CONTRACT", "maybe")
    config.load_settings.cache_clear()
    try:
        with caplog.at_level("WARNING", logger=config.__name__):
            settings = config.load_settings()
    finally:
        config.load_settings.cache_clear()

    assert settings.route_input_contract == "warn"
    assert "Invalid TTS_ROUTE_INPUT_CONTRACT" in caplog.text


def test_route_place_coords_populate_provider_coordinate_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeTripService()
    pre_geocode_calls: list[dict[str, Any]] = []

    def record_pre_geocode(*args, **kwargs) -> None:
        pre_geocode_calls.append(kwargs)

    monkeypatch.setattr(routes_trip, "pre_geocode_for_provider", record_pre_geocode)
    client = _client(service)

    response = client.post(
        "/v1/trip/arrival-time",
        json=_arrival_body(
            origin_place=_ready_place(37.4979, 127.0276),
            dest_place=_ready_place(37.3947, 127.1112),
        ),
        headers={"X-TTS-Route-Input-Contract": "strict"},
    )

    assert response.status_code == 200
    assert pre_geocode_calls == [
        {"coords_map": {"강남역": (37.4979, 127.0276), "판교역": (37.3947, 127.1112)}}
    ]
    assert service.arrival_calls == 1
