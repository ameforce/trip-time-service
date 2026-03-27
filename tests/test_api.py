from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from trip_time_service.api import routes
from trip_time_service.api.main import create_app
from trip_time_service.core.models import DriveDuration, Route
from trip_time_service.providers.base import ProviderError
from trip_time_service.services.trip_time_service import TripTimeService

KST = ZoneInfo("Asia/Seoul")


class _MockProvider:
    name = "test_mock"

    def __init__(self, seconds: int = 1800) -> None:
        self._seconds = seconds

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _FailingProvider:
    name = "failing"

    def __init__(self, *, retryable: bool = True) -> None:
        self._retryable = retryable

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        raise ProviderError("Provider failure", is_retryable=self._retryable)

    def close(self) -> None:
        return


class _TransientRetryableProvider:
    name = "transient_retryable"
    max_parallel_sessions = 4

    def __init__(self, seconds: int = 1800) -> None:
        self._seconds = seconds
        self._failed_buckets: set[tuple[int, int]] = set()

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        bucket = (departure_time.hour, departure_time.minute)
        if departure_time.minute % 20 == 0 and bucket not in self._failed_buckets:
            self._failed_buckets.add(bucket)
            raise ProviderError("Transient provider failure", is_retryable=True)
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


class _FailOnceRetryableProvider:
    name = "fail_once_retryable"

    def __init__(self, seconds: int = 1800) -> None:
        self._seconds = seconds
        self._calls = 0

    def get_drive_duration(
        self, route: Route, departure_time: datetime
    ) -> DriveDuration:
        _ = route
        self._calls += 1
        if self._calls == 1:
            raise ProviderError("transient first call failure", is_retryable=True)
        return DriveDuration(
            duration_seconds=self._seconds,
            fetched_at=departure_time,
        )

    def close(self) -> None:
        return


def _build_client(provider: object | None = None) -> TestClient:
    app = create_app()
    from trip_time_service.config import Settings

    settings = Settings(
        timezone=KST,
        headless=True,
        cache_ttl=timedelta(seconds=3600),
        step_minutes=5,
        lookback_hours=1,
        max_queries=1000,
        provider="test",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
    )
    prov = provider or _MockProvider()
    service = TripTimeService(settings=settings, provider=prov)
    app.state.trip_time_service = service
    app.state.settings = settings
    return TestClient(app)


class TestHealthEndpoint:
    def test_healthz_returns_ok(self) -> None:
        client = _build_client()
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestArrivalTimeEndpoint:
    def test_valid_request(self) -> None:
        client = _build_client(_MockProvider(seconds=1800))
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["duration_seconds"] == 1800
        assert body["provider"] == "test_mock"
        assert "arrival_time" in body

    def test_missing_origin_returns_422(self) -> None:
        client = _build_client()
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 422

    def test_empty_origin_returns_422(self) -> None:
        client = _build_client()
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 422

    def test_extra_field_returns_422(self) -> None:
        client = _build_client()
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
                "extra": "not_allowed",
            },
        )
        assert resp.status_code == 422

    def test_retryable_provider_error_is_retried(self) -> None:
        client = _build_client(_FailOnceRetryableProvider(seconds=900))
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2099-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["duration_seconds"] == 900

    def test_out_of_range_coords_return_422(self) -> None:
        client = _build_client()
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2099-01-24T09:00:00+09:00",
                "origin_coords": {"lat": 200, "lon": 127.0},
            },
        )
        assert resp.status_code == 422


class TestDepartureRecommendationEndpoint:
    def test_valid_request(self) -> None:
        client = _build_client(_MockProvider(seconds=1800))
        resp = client.post(
            "/v1/trip/recommended-departure-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "desired_arrival_time": "2099-01-24T10:00:00+09:00",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "recommended_departure_time" in body
        assert "meets_deadline" in body
        assert body["duration_seconds"] == 1800
        assert "recommended_score_total" in body
        assert "baseline_score_total" in body

    def test_missing_desired_arrival_time_returns_422(self) -> None:
        client = _build_client()
        resp = client.post(
            "/v1/trip/recommended-departure-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
            },
        )
        assert resp.status_code == 422

    def test_impossible_deadline_returns_422(self) -> None:
        client = _build_client(_MockProvider(seconds=2 * 3600))
        resp = client.post(
            "/v1/trip/recommended-departure-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "desired_arrival_time": "2099-01-24T10:00:00+09:00",
            },
        )
        assert resp.status_code == 422

    def test_stream_endpoint_emits_progress_and_scores(self) -> None:
        client = _build_client(_MockProvider(seconds=1800))
        with client.stream(
            "POST",
            "/v1/trip/recommended-departure-time/stream",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "desired_arrival_time": "2099-01-24T10:00:00+09:00",
            },
        ) as resp:
            assert resp.status_code == 200
            payload = "".join(resp.iter_text())

        assert "event: plan" in payload
        assert "event: candidate" in payload
        assert "event: recommendation" in payload
        assert "event: end" in payload

        recommendation_payload = None
        for block in payload.split("\n\n"):
            if "event: recommendation" not in block:
                continue
            data_lines = [
                line[len("data:") :].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            recommendation_payload = json.loads("\n".join(data_lines))
            break

        assert recommendation_payload is not None
        assert recommendation_payload["recommended_score_total"] is not None
        assert recommendation_payload["baseline_score_total"] is not None
        candidates = recommendation_payload["candidate_evaluations"]
        assert len(candidates) > 0
        assert candidates[0]["score_total"] is not None


class TestArrivalWithRecommendationEndpoint:
    def test_valid_request(self) -> None:
        client = _build_client(_MockProvider(seconds=1800))
        resp = client.post(
            "/v1/trip/arrival-time-with-recommendation",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "arrival" in body
        assert "recommendation" in body
        assert "immediate_safe_departure" in body
        assert "safe_departure_time" in body["immediate_safe_departure"]
        assert (
            body["immediate_safe_departure"]["safe_departure_time"]
            == body["arrival"]["departure_time"]
        )
        candidates = body["recommendation"]["candidate_evaluations"]
        assert len(candidates) > 0
        assert "score_total" in candidates[0]
        assert "recommended_score_total" in body["recommendation"]
        assert "baseline_score_total" in body["recommendation"]

    def test_stream_endpoint_emits_arrival_and_recommendation(self) -> None:
        client = _build_client(_MockProvider(seconds=1800))
        with client.stream(
            "POST",
            "/v1/trip/arrival-time-with-recommendation/stream",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        ) as resp:
            assert resp.status_code == 200
            payload = "".join(resp.iter_text())

        assert "event: arrival" in payload
        assert "event: recommendation" in payload
        assert "event: end" in payload

    def test_stream_endpoint_tolerates_transient_candidate_errors(self) -> None:
        client = _build_client(_TransientRetryableProvider(seconds=1800))
        with client.stream(
            "POST",
            "/v1/trip/arrival-time-with-recommendation/stream",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        ) as resp:
            assert resp.status_code == 200
            payload = "".join(resp.iter_text())

        assert "event: arrival" in payload
        assert "event: recommendation" in payload
        assert "event: end" in payload


class TestFrontendConfig:
    def test_config_returns_defaults(self) -> None:
        client = _build_client()
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["naver_map_client_id"] is None
        assert body["timezone"] == "Asia/Seoul"
        assert body["provider"] == "test"


class TestRouteEndpointValidation:
    def test_invalid_route_query_returns_422(self) -> None:
        client = _build_client()
        resp = client.get("/api/route?olat=999&olon=127.0&dlat=37.5&dlon=127.1")
        assert resp.status_code == 422


class TestNaverUrlCoordExtraction:
    def test_extract_coords_from_address_path_url(self) -> None:
        url = (
            "https://map.naver.com/p/search/%EC%96%B4%EC%84%B1%EC%A0%84%EA%B8%B8130/"
            "address/14323006.0508333,4572449.7534866,"
            "%EA%B0%95%EC%9B%90%ED%8A%B9%EB%B3%84%EC%9E%90%EC%B9%98%EB%8F%84%20"
            "%EC%96%91%EC%96%91%EA%B5%B0%20%ED%98%84%EB%B6%81%EB%A9%B4%20"
            "%EC%96%B4%EC%84%B1%EC%A0%84%EA%B8%B8%20130?c=15.00,0,0,0,dh"
            "&isCorrectAnswer=true"
        )
        coords = routes._extract_coords_from_naver_url(url)
        assert coords is not None
        lat, lon = coords
        assert 37.9 < lat < 38.0
        assert 128.6 < lon < 128.7

    def test_extract_address_from_address_path_url(self) -> None:
        url = (
            "https://map.naver.com/p/search/%EC%96%B4%EC%84%B1%EC%A0%84%EA%B8%B8130/"
            "address/14323006.0508333,4572449.7534866,"
            "%EA%B0%95%EC%9B%90%ED%8A%B9%EB%B3%84%EC%9E%90%EC%B9%98%EB%8F%84%20"
            "%EC%96%91%EC%96%91%EA%B5%B0%20%ED%98%84%EB%B6%81%EB%A9%B4%20"
            "%EC%96%B4%EC%84%B1%EC%A0%84%EA%B8%B8%20130?c=15.00,0,0,0,dh"
            "&isCorrectAnswer=true"
        )
        addr = routes._extract_addr_from_naver_url(url)
        assert addr == "강원특별자치도 양양군 현북면 어성전길 130"

    def test_ignore_zoom_only_c_query(self) -> None:
        url = "https://map.naver.com/p/search/foo?c=15.00,0,0,0,dh"
        assert routes._extract_coords_from_naver_url(url) is None


class TestIndexPage:
    def test_index_returns_html(self) -> None:
        client = _build_client()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Trip Time" in resp.text


class TestProviderErrorHandling:
    def test_retryable_provider_error_returns_503(self) -> None:
        client = _build_client(_FailingProvider(retryable=True))
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 503
        body = resp.json()
        assert body["retryable"] is True
        assert body["detail"] == "교통 정보 제공자 호출 중 오류가 발생했습니다."

    def test_non_retryable_provider_error_returns_502(self) -> None:
        client = _build_client(_FailingProvider(retryable=False))
        resp = client.post(
            "/v1/trip/arrival-time",
            json={
                "origin": "강남역",
                "destination": "판교역",
                "departure_time": "2026-01-24T09:00:00+09:00",
            },
        )
        assert resp.status_code == 502
        body = resp.json()
        assert body["retryable"] is False
        assert body["detail"] == "교통 정보 제공자 호출 중 오류가 발생했습니다."
