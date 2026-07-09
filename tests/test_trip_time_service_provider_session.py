from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trip_time_service.config import Settings
from trip_time_service.core.models import DriveDuration, Route
from trip_time_service.providers.base import ProviderError
from trip_time_service.services.trip_time_service import TripTimeService


class _FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.reusable_calls: list[tuple[Route, datetime]] = []
        self.one_shot_calls: list[tuple[Route, datetime]] = []

    def get_drive_duration(
        self,
        route: Route,
        departure_time: datetime,
    ) -> DriveDuration:
        self.reusable_calls.append((route, departure_time))
        return DriveDuration(
            duration_seconds=900,
            fetched_at=departure_time,
            raw_text="fake reusable",
        )

    def get_drive_duration_once(
        self,
        route: Route,
        departure_time: datetime,
    ) -> DriveDuration:
        self.one_shot_calls.append((route, departure_time))
        return DriveDuration(
            duration_seconds=900,
            fetched_at=departure_time,
            raw_text="fake one-shot",
        )

    def close(self) -> None:
        return


class _RetryFailingProvider:
    name = "retry-failing"

    def __init__(self) -> None:
        self.calls = 0

    def get_drive_duration(
        self,
        route: Route,
        departure_time: datetime,
    ) -> DriveDuration:
        self.calls += 1
        raise ProviderError(
            "panel drift",
            is_retryable=True,
            code="panel_parse_timeout",
        )

    def close(self) -> None:
        return


def _make_settings() -> Settings:
    return Settings(
        timezone=ZoneInfo("Asia/Seoul"),
        headless=True,
        cache_ttl=timedelta(seconds=600),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_playwright",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
        cors_allowed_origins=(),
        recommend_min_samples=12,
    )


def _make_settings_with_overrides(**overrides: object) -> Settings:
    values = {
        "timezone": ZoneInfo("Asia/Seoul"),
        "headless": True,
        "cache_ttl": timedelta(seconds=600),
        "step_minutes": 10,
        "lookback_hours": 3,
        "max_queries": 120,
        "provider": "naver_playwright",
        "chrome_binary_path": None,
        "chrome_user_data_dir": None,
        "naver_map_client_id": None,
        "recommend_workers": 1,
        "naver_session_pool_size": 1,
        "cors_allowed_origins": (),
        "recommend_min_samples": 12,
    }
    values.update(overrides)
    return Settings(**values)


def test_get_duration_cached_prefers_reusable_provider_method() -> None:
    provider = _FakeProvider()
    service = TripTimeService(settings=_make_settings(), provider=provider)
    route = Route.of("강남역", "판교역")
    base_time = datetime(2026, 4, 1, 9, 7, tzinfo=ZoneInfo("Asia/Seoul"))

    service._get_duration_cached(route=route, departure_time=base_time)
    service._get_duration_cached(
        route=route,
        departure_time=base_time + timedelta(minutes=10),
    )
    service._get_duration_cached(
        route=route,
        departure_time=base_time + timedelta(minutes=2),
    )

    assert len(provider.reusable_calls) == 2
    assert len(provider.one_shot_calls) == 0


def test_recommend_departure_honors_max_queries_budget() -> None:
    provider = _FakeProvider()
    service = TripTimeService(
        settings=_make_settings_with_overrides(lookback_hours=12, max_queries=3),
        provider=provider,
    )
    desired = datetime.now(tz=ZoneInfo("Asia/Seoul")) + timedelta(hours=2)

    recommendation = service.recommend_departure(
        origin="강남역",
        destination="판교역",
        desired_arrival_time=desired,
    )

    assert recommendation.candidates_checked <= 3
    assert len(provider.reusable_calls) <= 3


def test_retryable_provider_failure_is_bucketed_after_retry_exhaustion() -> None:
    provider = _RetryFailingProvider()
    service = TripTimeService(settings=_make_settings(), provider=provider)

    try:
        service.estimate_arrival(
            origin="강남역",
            destination="판교역",
            departure_time=datetime(2026, 4, 1, 9, 7, tzinfo=ZoneInfo("Asia/Seoul")),
        )
    except ProviderError as exc:
        assert exc.code == "provider_retry_exhausted"
        assert exc.bucket == "provider_retry_exhausted"
        assert isinstance(exc.__cause__, ProviderError)
        assert exc.__cause__.code == "panel_parse_timeout"
    else:  # pragma: no cover
        raise AssertionError("retry exhaustion should raise ProviderError")

    assert provider.calls == 2
