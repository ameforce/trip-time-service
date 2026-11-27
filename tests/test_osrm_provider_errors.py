from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.core.models import Route
from trip_time_service.providers import osrm as osrm_module
from trip_time_service.providers.base import ProviderError
from trip_time_service.providers.osrm import OsrmTravelTimeProvider

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> OsrmTravelTimeProvider:
    monkeypatch.setattr(
        osrm_module, "_geocode_place", lambda query: (37.5, 127.0)
    )
    return OsrmTravelTimeProvider()


def test_osrm_route_failure_raises_provider_error(
    provider: OsrmTravelTimeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osrm_module, "_osrm_duration", lambda *args: None)

    with pytest.raises(ProviderError) as exc_info:
        provider.get_drive_duration(
            Route.of("강남역", "판교역"),
            datetime(2026, 1, 24, 9, 0, tzinfo=KST),
        )

    assert exc_info.value.is_retryable is True
    assert exc_info.value.code == "osrm_route_failed"


def test_osrm_geocode_failure_raises_provider_error(
    provider: OsrmTravelTimeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osrm_module, "_geocode_place", lambda query: None)

    with pytest.raises(ProviderError) as exc_info:
        provider.get_drive_duration(
            Route.of("강남역", "판교역"),
            datetime(2026, 1, 24, 9, 0, tzinfo=KST),
        )

    assert exc_info.value.is_retryable is True
    assert exc_info.value.code == "osrm_geocode_failed"


def test_osrm_success_returns_traffic_adjusted_duration(
    provider: OsrmTravelTimeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osrm_module, "_osrm_duration", lambda *args: 1800.0)

    duration = provider.get_drive_duration(
        Route.of("강남역", "판교역"),
        datetime(2026, 1, 24, 8, 0, tzinfo=KST),
    )

    # 08시 피크 배수(2.0) 적용
    assert duration.duration_seconds == 3600


def test_osrm_failure_result_is_not_cached(
    provider: OsrmTravelTimeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osrm_module, "_osrm_duration", lambda *args: None)

    with pytest.raises(ProviderError):
        provider.get_drive_duration(
            Route.of("강남역", "판교역"),
            datetime(2026, 1, 24, 9, 0, tzinfo=KST),
        )

    monkeypatch.setattr(osrm_module, "_osrm_duration", lambda *args: 900.0)
    duration = provider.get_drive_duration(
        Route.of("강남역", "판교역"),
        datetime(2026, 1, 24, 9, 0, tzinfo=KST),
    )
    assert duration.duration_seconds > 0


def test_osrm_uses_the_supplied_coordinates_without_geocoding(
    provider: OsrmTravelTimeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = Route.of(
        "같은 이름", "같은 이름", origin_coords=(37.1, 127.1), dest_coords=(37.2, 127.2)
    )
    actual_routes = []

    def duration(*coords: float) -> float:
        actual_routes.append(coords)
        return 1200.0

    monkeypatch.setattr(
        osrm_module,
        "_geocode_place",
        lambda *_args: pytest.fail("known coordinates must skip geocoding"),
    )
    monkeypatch.setattr(osrm_module, "_osrm_duration", duration)
    provider.get_drive_duration(route, datetime(2026, 1, 24, 8, 0, tzinfo=KST))
    assert actual_routes == [(37.1, 127.1, 37.2, 127.2)]


def test_osrm_geocode_failure_is_retryable_not_poisoned(
    provider: OsrmTravelTimeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """지오코딩 실패가 음성 캐시되어 재시도를 막지 않는다."""
    # 1차 요청: origin 지오코딩 실패 → 즉시 ProviderError
    # 2차 요청: origin/destination 모두 성공
    responses = iter([None, (37.5, 127.0), (37.39, 127.11)])
    monkeypatch.setattr(
        osrm_module, "_geocode_place", lambda query: next(responses)
    )
    monkeypatch.setattr(osrm_module, "_osrm_duration", lambda *args: 900.0)
    route = Route.of("강남역", "판교역")

    with pytest.raises(ProviderError):
        provider.get_drive_duration(
            route, datetime(2026, 1, 24, 9, 0, tzinfo=KST)
        )

    duration = provider.get_drive_duration(
        route, datetime(2026, 1, 24, 9, 0, tzinfo=KST)
    )
    assert duration.duration_seconds > 0
