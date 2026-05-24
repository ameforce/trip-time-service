from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trip_time_service.api import routes_trip
from trip_time_service.api.schemas import DepartureRecommendationRequest
from trip_time_service.config import Settings
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


async def _read_remaining(response: object) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
    return "".join(chunks)


class _PanelParseFailingTripService:
    def recommend_departure(self, **kwargs):
        raise ProviderError(
            "raw provider panel text: 강남역 -> 판교역 secret-token",
            code="panel_parse_timeout",
        )


class _RetryExhaustedTripService:
    def recommend_departure(self, **kwargs):
        raise ProviderError(
            "all retry attempts failed for raw route 강남역 -> 판교역",
            code="provider_retry_exhausted",
            cause=ProviderError("inner raw route", code="panel_parse_timeout"),
        )


def test_provider_error_keeps_legacy_constructor_and_optional_code_bucket() -> None:
    cause = ValueError("cause")
    legacy = ProviderError("legacy", is_retryable=False, cause=cause)
    coded = ProviderError("coded", code="panel_parse_timeout")
    bucketed = ProviderError(
        "bucketed",
        code="provider_retry_exhausted",
        bucket="naver_retry_exhausted",
    )

    assert legacy.is_retryable is False
    assert legacy.__cause__ is cause
    assert legacy.code is None
    assert legacy.bucket is None
    assert coded.code == "panel_parse_timeout"
    assert coded.bucket == "panel_parse_timeout"
    assert bucketed.code == "provider_retry_exhausted"
    assert bucketed.bucket == "naver_retry_exhausted"
    assert (
        routes_trip._safe_provider_error_bucket(bucketed)
        == "provider_retry_exhausted"
    )


def test_stream_error_exposes_safe_bucket_not_raw_provider_text(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: None,
    )
    payload = DepartureRecommendationRequest(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
    )
    response = routes_trip.stream_recommended_departure_time(
        payload,
        _request(_PanelParseFailingTripService()),
    )

    body = asyncio.run(_read_remaining(response))

    assert "event: error" in body
    assert "provider_degraded" in body
    assert "panel_parse_timeout" in body
    assert "raw provider panel text" not in body
    assert "secret-token" not in body
    assert "강남역" not in body
    assert "판교역" not in body


def test_stream_error_exposes_retry_exhausted_bucket(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_trip,
        "pre_geocode_for_provider",
        lambda *args, **kwargs: None,
    )
    payload = DepartureRecommendationRequest(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=datetime.now(tz=_TZ) + timedelta(hours=1),
    )
    response = routes_trip.stream_recommended_departure_time(
        payload,
        _request(_RetryExhaustedTripService()),
    )

    body = asyncio.run(_read_remaining(response))

    assert "event: error" in body
    assert "provider_degraded" in body
    assert "provider_retry_exhausted" in body
    assert "all retry attempts failed" not in body
    assert "inner raw route" not in body
    assert "강남역" not in body
    assert "판교역" not in body
