from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from trip_time_service.api import geocode_services


class _CoordinateAwareProvider:
    def __init__(self) -> None:
        self.coords: dict[str, tuple[float, float]] = {}

    def set_coords(self, place: str, lat: float, lon: float) -> None:
        self.coords[place] = (lat, lon)


def _service(provider: object) -> SimpleNamespace:
    return SimpleNamespace(_provider=provider)


def test_pre_geocode_timeout_does_not_raise_for_two_unfinished_futures(
    monkeypatch,
) -> None:
    provider = _CoordinateAwareProvider()
    futures = [Future(), Future()]

    def submit(_fn, _place):
        return futures.pop(0)

    monkeypatch.setattr(geocode_services, "_PRE_GEOCODE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", submit)

    geocode_services.pre_geocode_for_provider(
        _service(provider),
        "강남역",
        "판교역",
    )

    assert provider.coords == {}


def test_pre_geocode_timeout_does_not_raise_for_single_unfinished_future(
    monkeypatch,
) -> None:
    provider = _CoordinateAwareProvider()
    future = Future()
    geocode_called = False

    def geocode_one(_query: str):
        nonlocal geocode_called
        geocode_called = True
        return {"lat": "37.0", "lon": "127.0"}

    monkeypatch.setattr(geocode_services, "_PRE_GEOCODE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        geocode_services,
        "_submit_geo_pool_task",
        lambda *_args: future,
    )
    monkeypatch.setattr(geocode_services, "geocode_one", geocode_one)

    geocode_services.pre_geocode_for_provider(_service(provider), "강남역")

    assert provider.coords == {}
    assert geocode_called is False


def test_pre_geocode_skips_sync_fallback_when_geo_pool_unavailable(
    monkeypatch,
) -> None:
    provider = _CoordinateAwareProvider()

    def geocode_one(_query: str):  # pragma: no cover - should not be called
        raise AssertionError("synchronous geocode fallback must not run")

    monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", lambda *_args: None)
    monkeypatch.setattr(geocode_services, "geocode_one", geocode_one)

    geocode_services.pre_geocode_for_provider(_service(provider), "강남역")

    assert provider.coords == {}


def test_pre_geocode_uses_frontend_coords_without_external_lookup(monkeypatch) -> None:
    provider = _CoordinateAwareProvider()

    def submit(*_args):  # pragma: no cover - should not be called
        raise AssertionError("frontend coords should avoid geocode pool")

    monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", submit)

    geocode_services.pre_geocode_for_provider(
        _service(provider),
        "강남역",
        coords_map={"강남역": (37.1, 127.1)},
    )

    assert provider.coords == {"강남역": (37.1, 127.1)}


def test_resolve_route_keeps_same_label_endpoints_in_order(monkeypatch) -> None:
    provider = _CoordinateAwareProvider()

    def fail_submit(*_args):
        pytest.fail("resolved endpoints must avoid the geocode pool")

    monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", fail_submit)
    route = geocode_services.resolve_route_for_provider(
        _service(provider),
        "같은 이름",
        "같은 이름",
        origin_coords=(37.1, 127.1),
        dest_coords=(37.2, 127.2),
    )

    assert route.origin == route.destination == "같은 이름"
    assert route.origin_coords == (37.1, 127.1)
    assert route.dest_coords == (37.2, 127.2)
    assert provider.coords == {}


def test_resolve_route_looks_up_only_missing_endpoint_even_with_same_label(
    monkeypatch,
) -> None:
    provider = _CoordinateAwareProvider()
    queries: list[str] = []

    def geocode(query: str) -> dict:
        queries.append(query)
        return {"lat": 37.2, "lon": 127.2}

    monkeypatch.setattr(geocode_services, "geocode_one", geocode)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", pool.submit)
        route = geocode_services.resolve_route_for_provider(
            _service(provider), "같은 이름", "같은 이름", origin_coords=(37.1, 127.1)
        )

    assert queries == ["같은 이름"]
    assert route.origin_coords == (37.1, 127.1)
    assert route.dest_coords == (37.2, 127.2)
    assert provider.coords == {}


def test_resolve_route_discarded_late_geocode_stays_request_local(monkeypatch) -> None:
    provider = _CoordinateAwareProvider()
    query_started = Event()
    release_query = Event()
    cancel = Event()

    def geocode(query: str) -> dict:
        query_started.set()
        if not release_query.wait(3):
            pytest.fail(f"timed out waiting for test to release {query}")
        return {"lat": 37.2, "lon": 127.2}

    monkeypatch.setattr(geocode_services, "geocode_one", geocode)
    with ThreadPoolExecutor(max_workers=1) as geo_pool:
        monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", geo_pool.submit)
        with ThreadPoolExecutor(max_workers=1) as request_pool:
            pending = request_pool.submit(
                geocode_services.resolve_route_for_provider,
                _service(provider),
                "출발",
                "도착",
                origin_coords=(37.1, 127.1),
                cancel_event=cancel,
            )
            try:
                assert query_started.wait(2)
                cancel.set()
                with pytest.raises(CancelledError):
                    pending.result(timeout=2)
            finally:
                release_query.set()

    assert provider.coords == {}


def test_resolve_text_route_keeps_browser_search_if_only_one_lookup_succeeds(
    monkeypatch,
) -> None:
    provider = _CoordinateAwareProvider()

    def geocode(query: str):
        return {"lat": 37.1, "lon": 127.1} if query == "출발" else None

    monkeypatch.setattr(geocode_services, "geocode_one", geocode)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(geocode_services, "_submit_geo_pool_task", pool.submit)
        route = geocode_services.resolve_route_for_provider(
            _service(provider), "출발", "도착",
        )

    assert route.origin == "출발"
    assert route.destination == "도착"
    assert route.origin_coords is None
    assert route.dest_coords is None
    assert provider.coords == {}
