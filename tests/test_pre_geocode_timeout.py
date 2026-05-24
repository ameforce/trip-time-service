from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace

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
