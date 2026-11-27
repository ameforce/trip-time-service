from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event, get_ident
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.config import Settings
from trip_time_service.core.cache import LruTtlCache
from trip_time_service.core.models import Route
from trip_time_service.providers import naver_playwright
from trip_time_service.providers.base import ProviderError
from trip_time_service.providers.naver_playwright import (
    NaverMapsPlaywrightPoolProvider,
    NaverMapsPlaywrightProvider,
)
from trip_time_service.services.trip_time_service import TripTimeService


def _make_settings(**overrides) -> Settings:
    base = dict(
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
    base.update(overrides)
    return Settings(**base)


@dataclass
class _FakeSearchAdapter:
    calls: list[tuple[object, Route]]

    def navigate_and_search(self, page: object, route: Route) -> None:
        self.calls.append((page, route))


@dataclass
class _FakeDeparturePicker:
    open_calls: list[object]
    read_calls: list[tuple[object, int, int, datetime]]

    def open_later_modal(self, page: object) -> None:
        self.open_calls.append(page)

    def set_time_and_read(
        self,
        page: object,
        hour_24: int,
        minute_10: int,
        departure_time: datetime | None = None,
    ) -> int:
        assert departure_time is not None
        self.read_calls.append((page, hour_24, minute_10, departure_time))
        return 1800


# ── fakes for DOM-verified place selection ──


@dataclass
class _FakeInput:
    events: list[tuple[str, str]] = field(default_factory=list)

    def focus(self) -> None:
        self.events.append(("focus", ""))

    def fill(self, value: str) -> None:
        self.events.append(("fill", value))

    def type(self, value: str) -> None:
        self.events.append(("type", value))

    def press(self, key: str) -> None:
        self.events.append(("press", key))


@dataclass
class _FakeItem:
    visible: bool = True
    clicks: int = 0

    def is_visible(self) -> bool:
        return self.visible

    def click(self) -> None:
        self.clicks += 1


@dataclass
class _FakeAcPage:
    items: list[_FakeItem]

    def query_selector_all(self, selector: str) -> list[_FakeItem]:
        if selector == ".list_place li.item_place":
            return list(self.items)
        return []


def test_provider_name_is_playwright() -> None:
    assert NaverMapsPlaywrightProvider.name == "naver_playwright"
    assert NaverMapsPlaywrightPoolProvider.name == "naver_playwright"


def test_provider_constructs_private_search_and_departure_picker_seams() -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())

    assert callable(
        getattr(
            getattr(provider, "_search_adapter", None),
            "navigate_and_search",
            None,
        )
    )
    assert callable(
        getattr(
            getattr(provider, "_departure_picker", None),
            "set_time_and_read",
            None,
        )
    )


def test_query_locked_uses_search_and_departure_picker_adapters(monkeypatch) -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())
    route = Route.of("강남역", "판교역")
    departure_time = datetime(2026, 4, 1, 9, 30, 0)
    page = object()
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
        page=page,
        route=route,
        departure_time=departure_time,
        m10=30,
    )

    assert duration == 1800
    assert search.calls == [(page, route)]
    assert departure_picker.open_calls == [page]
    assert departure_picker.read_calls == [
        (page, departure_time.hour, 30, departure_time)
    ]
    assert provider._current_route == route
    assert provider._modal_open is True


def test_build_directions_url_uses_coord_first_epsg3857_path() -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())
    route = Route.of("강남역", "판교역")

    assert provider._build_directions_url(route) is None

    provider.set_coords("강남역", 37.4979, 127.0276)
    provider.set_coords("판교역", 37.3947, 127.1112)

    url = provider._build_directions_url(route)
    assert url is not None
    assert url.startswith("https://map.naver.com/p/directions/")
    assert url.endswith("/-/car")


def test_build_directions_url_preserves_order_and_escapes_route_labels() -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())
    route = Route.of(
        "입구/후문", "입구/후문",
        origin_coords=(37.1, 127.1), dest_coords=(37.2, 127.2),
    )
    try:
        url = provider._build_directions_url(route)
        assert url is not None
        origin, destination, _, _ = url.split("/p/directions/")[1].split("/")
        assert float(origin.split(",")[0]) < float(destination.split(",")[0])
        assert "%2F" in origin and "%2F" in destination
    finally:
        provider.close()


@pytest.mark.parametrize("concurrent", [False, True])
def test_same_names_separate_service_cache_naver_cache_and_prepared_route(
    monkeypatch, concurrent: bool,
) -> None:
    settings = _make_settings()
    provider = NaverMapsPlaywrightProvider(settings)
    service = TripTimeService(settings=settings, provider=provider)
    clock = [0.0]
    provider._dur_cache = LruTtlCache(
        maxsize=512, ttl_seconds=600, clock=lambda: clock[0]
    )
    page = object()
    monkeypatch.setattr(
        provider,
        "_ensure_session_locked",
        lambda: SimpleNamespace(page=page),
    )
    search = _FakeSearchAdapter(calls=[])
    picker = _FakeDeparturePicker(open_calls=[], read_calls=[])
    provider._search_adapter = search
    provider._departure_picker = picker

    routes = (
        Route.of(
            "지점", "지점", origin_coords=(37.1, 127.1), dest_coords=(37.2, 127.2)
        ),
        Route.of(
            "지점", "지점", origin_coords=(37.3, 127.3), dest_coords=(37.4, 127.4)
        ),
    )
    durations = dict(zip(routes, (900, 1800), strict=True))
    departure = datetime(2026, 4, 1, 9, 30, tzinfo=settings.timezone)
    original_read = picker.set_time_and_read

    def read(*args) -> int:
        original_read(*args)
        return durations[search.calls[-1][1]]

    monkeypatch.setattr(picker, "set_time_and_read", read)

    def estimate(route: Route):
        return service.estimate_arrival(
            origin=route.origin,
            destination=route.destination,
            route=route,
            departure_time=departure,
        )

    try:
        if concurrent:
            with ThreadPoolExecutor(max_workers=2) as executor:
                estimates = list(executor.map(estimate, routes))
        else:
            estimates = [estimate(route) for route in routes]

        assert [estimate.duration.duration_seconds for estimate in estimates] == [
            durations[route] for route in routes
        ]
        assert {route for _, route in search.calls} == set(routes)
        assert len(search.calls) == len(picker.read_calls) == 2

        for route, first in zip(routes, estimates, strict=True):
            repeated = estimate(route)
            assert repeated.cache_hit is True
            assert provider.get_drive_duration(route, departure) is first.duration
        assert len(picker.read_calls) == 2

        current_route = search.calls[-1][1]
        other_route = routes[1] if current_route == routes[0] else routes[0]
        later = departure + timedelta(minutes=10)
        assert provider.get_drive_duration(current_route, later).duration_seconds == (
            durations[current_route]
        )
        assert len(search.calls) == 2
        assert provider.get_drive_duration(other_route, later).duration_seconds == (
            durations[other_route]
        )
        assert search.calls[-1] == (page, other_route)
        assert len(search.calls) == 3
        assert len(picker.read_calls) == 4

        clock[0] += 601
        refreshed = provider.get_drive_duration(other_route, departure)
        original = estimates[routes.index(other_route)].duration
        assert refreshed is not original
        assert refreshed.fetched_at >= original.fetched_at
        assert len(picker.read_calls) == 5
    finally:
        service.close()


def test_provider_cancellation_releases_session_on_owner(monkeypatch) -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())
    cancelled = Event()
    owner_thread_calls: list[int] = []
    page = object()

    def close(**_kwargs):
        owner_thread_calls.append(get_ident())
        return SimpleNamespace(timed_out=False, close_error=None)

    provider._session = SimpleNamespace(page=page, close=close)

    def search(*_args):
        cancelled.set()

    monkeypatch.setattr(provider._search_adapter, "navigate_and_search", search)
    monkeypatch.setattr(
        provider._departure_picker,
        "open_later_modal",
        lambda *_args: pytest.fail("cancelled route must not enter the picker"),
    )
    try:
        with pytest.raises(CancelledError):
            provider.get_drive_duration_for_route(
                Route.of("출발", "도착"),
                datetime(2026, 4, 1, 9, 30),
                cancel_event=cancelled,
            )
        assert owner_thread_calls == [provider._owner.thread_ident]
        assert provider._session is None
    finally:
        provider.close()


def test_coordinate_route_search_failure_does_not_select_by_name(monkeypatch) -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())

    def fail_goto(*_args, **_kwargs):
        raise TimeoutError("navigation failed")

    monkeypatch.setattr(
        provider._search_adapter,
        "navigate_and_search_ac",
        lambda *_args: pytest.fail("must preserve the supplied endpoints"),
    )
    try:
        route = Route.of(
            "지점", "지점", origin_coords=(37.1, 127.1), dest_coords=(37.2, 127.2)
        )
        with pytest.raises(ProviderError):
            provider._navigate_and_search(SimpleNamespace(goto=fail_goto), route)
    finally:
        provider.close()


def test_provider_fails_before_browser_when_only_one_coordinate_is_available(
    monkeypatch,
) -> None:
    provider = NaverMapsPlaywrightProvider(_make_settings())
    monkeypatch.setattr(
        provider,
        "_ensure_session_locked",
        lambda: pytest.fail("must resolve endpoints before browser search"),
    )
    try:
        with pytest.raises(ProviderError) as exc:
            provider.get_drive_duration_for_route(
                Route.of("출발", "도착", origin_coords=(37.1, 127.1)),
                datetime(2026, 4, 1, 9, 30),
            )
        assert exc.value.is_retryable is False
    finally:
        provider.close()


def test_select_place_ac_clicks_visible_dom_item_not_arrow_keys(monkeypatch) -> None:
    monkeypatch.setattr(naver_playwright, "_sleep", lambda _seconds: None)
    provider = NaverMapsPlaywrightProvider(_make_settings())

    item = _FakeItem(visible=True)
    hidden = _FakeItem(visible=False)
    page = _FakeAcPage(items=[hidden, item])
    input_el = _FakeInput()

    provider._select_place_ac(page, input_el, "강남역")

    assert item.clicks == 1
    assert hidden.clicks == 0
    # DOM-verified selection must NOT rely on blind key navigation.
    pressed_keys = [value for kind, value in input_el.events if kind == "press"]
    assert pressed_keys == []
    assert ("type", "강남역") in input_el.events


def test_select_place_ac_falls_back_to_enter_when_no_visible_items(
    monkeypatch,
) -> None:
    monkeypatch.setattr(naver_playwright, "_sleep", lambda _seconds: None)
    provider = NaverMapsPlaywrightProvider(_make_settings())

    hidden = _FakeItem(visible=False)
    page = _FakeAcPage(items=[hidden])
    input_el = _FakeInput()

    provider._select_place_ac(page, input_el, "없는장소")

    assert hidden.clicks == 0
    pressed_keys = [value for kind, value in input_el.events if kind == "press"]
    assert pressed_keys == ["Enter"]
