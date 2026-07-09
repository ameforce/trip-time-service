from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.config import Settings
from trip_time_service.core.models import Route
from trip_time_service.providers import naver_playwright
from trip_time_service.providers.naver_playwright import (
    NaverMapsPlaywrightPoolProvider,
    NaverMapsPlaywrightProvider,
)


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
