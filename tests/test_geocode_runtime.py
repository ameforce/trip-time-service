from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from trip_time_service.api import geocode_services, naver_playwright_geo


class _FakeJsonResponse:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeJsonResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.parametrize("source", ["url", "entry", "road"])
def test_browser_geocode_keeps_road_address_separate_from_label(
    monkeypatch, source
) -> None:
    geo = naver_playwright_geo
    name, road = "서울시청", "서울 중구 세종대로 110"
    coords = {"lat": "37.566295", "lon": "126.977945"}
    session = SimpleNamespace(page=SimpleNamespace(url="https://map.naver.com"))
    monkeypatch.setattr(geo, "_ensure_naver_session", lambda: session)
    monkeypatch.setattr(geo, "_naver_search", lambda *_: None)
    monkeypatch.setattr(geo, "_sleep", lambda _: None)
    monkeypatch.setattr(geo, "_naver_read_entry_detail", lambda _: road)
    url_coords = (37.566295, 126.977945) if source == "url" else None
    monkeypatch.setattr(geo, "extract_coords_from_naver_url", lambda _: url_coords)
    monkeypatch.setattr(
        geo,
        "_naver_extract_entry_coords",
        lambda _: coords if source == "entry" else None,
    )
    result = geo._geocode_naver_on_owner(name, fallback_geocode=lambda _: coords)
    assert result == {**coords, "display_name": f"{name} ({road})", "address": road}
    assert "display_name" not in coords


@pytest.fixture(autouse=True)
def _reset_autocomplete_runtime_state() -> None:
    geocode_services.autocomplete_naver_map.cache_clear()
    geocode_services._NAVER_NCAPTCHA_RETRY_AFTER_TS = 0.0
    geocode_services.startup_autocomplete_runtime()
    try:
        yield
    finally:
        geocode_services.autocomplete_naver_map.cache_clear()
        geocode_services._NAVER_NCAPTCHA_RETRY_AFTER_TS = 0.0
        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)


def test_geo_pool_runtime_recreates_after_shutdown() -> None:
    geocode_services.startup_autocomplete_runtime()
    try:
        initial_future = geocode_services._submit_geo_pool_task(lambda: "initial")
        assert initial_future is not None
        assert initial_future.result(timeout=5) == "initial"

        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)
        shutdown_metrics = geocode_services.get_autocomplete_runtime_metrics()
        assert shutdown_metrics["geo_pool_disabled"] is True
        assert shutdown_metrics["geo_pool_present"] is False
        assert geocode_services._submit_geo_pool_task(lambda: "shutdown") is None

        geocode_services.startup_autocomplete_runtime()
        restarted_metrics = geocode_services.get_autocomplete_runtime_metrics()
        assert restarted_metrics["geo_pool_disabled"] is False

        restarted_future = geocode_services._submit_geo_pool_task(lambda: "restart")
        assert restarted_future is not None
        assert restarted_future.result(timeout=5) == "restart"
    finally:
        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)


def test_shutdown_runtime_respects_wait_budget_for_geo_pool() -> None:
    future = None
    geocode_services.startup_autocomplete_runtime()
    try:
        future = geocode_services._submit_geo_pool_task(time.sleep, 1.5)
        assert future is not None

        started = time.monotonic()
        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        future.result(timeout=5)
    finally:
        geocode_services.startup_autocomplete_runtime()
        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)


def test_cache_clear_drains_tracked_warmup_future() -> None:
    future = None
    geocode_services.startup_autocomplete_runtime()
    try:
        future = geocode_services._submit_geo_pool_task(time.sleep, 1.0)
        assert future is not None
        geocode_services._track_autocomplete_warmup_future(future)

        geocode_services.clear_autocomplete_cache()

        metrics = geocode_services.get_autocomplete_runtime_metrics()
        assert metrics["warmup_futures"] == 0
        assert metrics["runtime_disabled"] is False
        future.result(timeout=5)
    finally:
        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)


def test_warmup_does_not_call_all_search_or_enable_shared_backoff(
    monkeypatch,
) -> None:
    urlopen_calls: list[object] = []
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.warmup_naver_browser_pool",
        lambda: 0,
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.urllib.request.urlopen",
        lambda *args, **_kwargs: (
            urlopen_calls.append(args[0]),
            _FakeJsonResponse(
                {
                    "result": {
                        "place": {"list": []},
                        "address": {"list": []},
                        "ncaptcha": True,
                    }
                }
            ),
        )[1],
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.autocomplete_naver_browser_pool",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.autocomplete_nominatim",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.autocomplete_photon",
        lambda *_args, **_kwargs: (),
    )

    geocode_services.warmup_autocomplete_cache(
        ["경수대로680번길40"],
        background=False,
    )

    assert len(urlopen_calls) == 0
    assert (
        geocode_services.get_autocomplete_runtime_metrics()["ncaptcha_backoff_active"]
        is False
    )


def test_interactive_autocomplete_miss_does_not_call_all_search_backoff(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.warmup_naver_browser_pool",
        lambda: 0,
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.urllib.request.urlopen",
        lambda *_args, **_kwargs: _FakeJsonResponse(
            {
                "result": {
                    "place": {"list": []},
                    "address": {"list": []},
                    "ncaptcha": True,
                }
            }
        ),
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.autocomplete_naver_browser_pool",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.autocomplete_nominatim",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "trip_time_service.api.geocode_services.autocomplete_photon",
        lambda *_args, **_kwargs: (),
    )

    assert geocode_services.autocomplete_naver_map("경수대로680번길40", limit=12) == ()
    assert (
        geocode_services.get_autocomplete_runtime_metrics()["ncaptcha_backoff_active"]
        is False
    )
