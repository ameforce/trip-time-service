from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.config import Settings
from trip_time_service.core.models import Route
from trip_time_service.providers.naver_selenium import (
    NaverMapsSeleniumProvider,
)


def _make_settings() -> Settings:
    return Settings(
        timezone=ZoneInfo("Asia/Seoul"),
        headless=True,
        cache_ttl=timedelta(seconds=600),
        step_minutes=10,
        lookback_hours=3,
        max_queries=120,
        provider="naver_selenium",
        chrome_binary_path=None,
        chrome_user_data_dir=None,
        naver_map_client_id=None,
        recommend_workers=1,
        naver_session_pool_size=1,
        cors_allowed_origins=(),
        recommend_min_samples=12,
    )


@dataclass
class _FakeSearchAdapter:
    calls: list[tuple[object, Route]]

    def navigate_and_search(self, driver: object, route: Route) -> None:
        self.calls.append((driver, route))


@dataclass
class _FakeDeparturePicker:
    open_calls: list[object]
    read_calls: list[tuple[object, int, int, datetime]]

    def open_later_modal(self, driver: object) -> None:
        self.open_calls.append(driver)

    def set_time_and_read(
        self,
        driver: object,
        hour_24: int,
        minute_10: int,
        departure_time: datetime | None = None,
    ) -> int:
        assert departure_time is not None
        self.read_calls.append((driver, hour_24, minute_10, departure_time))
        return 1800


def test_provider_constructs_private_search_and_departure_picker_seams() -> None:
    provider = NaverMapsSeleniumProvider(_make_settings())

    assert callable(
        getattr(getattr(provider, "_search_adapter", None), "navigate_and_search", None)
    )
    assert callable(
        getattr(getattr(provider, "_departure_picker", None), "set_time_and_read", None)
    )


def test_query_locked_uses_search_and_departure_picker_adapters(monkeypatch) -> None:
    provider = NaverMapsSeleniumProvider(_make_settings())
    route = Route.of("강남역", "판교역")
    departure_time = datetime(2026, 4, 1, 9, 30, 0)
    driver = object()
    search = _FakeSearchAdapter(calls=[])
    departure_picker = _FakeDeparturePicker(open_calls=[], read_calls=[])

    provider._search_adapter = search
    provider._departure_picker = departure_picker

    monkeypatch.setattr(
        provider,
        "_navigate_and_search",
        lambda *_args, **_kwargs: pytest.fail("legacy search path should not be used"),
    )
    monkeypatch.setattr(
        provider,
        "_open_later_modal",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy departure modal path should not be used"
        ),
    )
    monkeypatch.setattr(
        provider,
        "_set_time_and_read",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy time-picker path should not be used"
        ),
    )

    duration = provider._query_locked(
        driver=driver,
        route=route,
        departure_time=departure_time,
        m10=30,
    )

    assert duration == 1800
    assert search.calls == [(driver, route)]
    assert departure_picker.open_calls == [driver]
    assert departure_picker.read_calls == [
        (driver, departure_time.hour, 30, departure_time)
    ]
    assert provider._current_route == route
    assert provider._modal_open is True
