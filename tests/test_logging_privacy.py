from __future__ import annotations

import logging
from types import SimpleNamespace

from trip_time_service.api import geocode_services, routes_geo
from trip_time_service.privacy import redact_route, redact_text
from trip_time_service.providers import osrm
from trip_time_service.providers.naver_playwright import (
    _log as naver_log,
)
from trip_time_service.providers.naver_playwright import (
    _NaverDirectionsSearchAdapter,
)


def test_redact_text_omits_raw_value() -> None:
    raw = "강남역"

    redacted = redact_text(raw)

    assert raw not in redacted
    assert "sha256=" in redacted
    assert "len=3" in redacted


def test_geocode_route_log_omits_raw_query(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        routes_geo,
        "geocode_one",
        lambda *_args, **_kwargs: {
            "lat": "37.0",
            "lon": "127.0",
            "display_name": "강남역",
            "source": "fake",
            "confidence": 1.0,
        },
    )

    with caplog.at_level(logging.INFO, logger=routes_geo.__name__):
        routes_geo.geocode("강남역", SimpleNamespace())

    assert "강남역" not in caplog.text
    assert "sha256=" in caplog.text


def test_geocode_service_log_omits_raw_query(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        lambda *_args, **_kwargs: (
            {
                "lat": "37.0",
                "lon": "127.0",
                "display_name": "강남역",
                "address": "서울 강남구 강남대로 396",
                "type": "역",
                "source": "naver_all_search",
                "confidence": 0.95,
            },
        ),
    )

    with caplog.at_level(logging.INFO, logger=geocode_services.__name__):
        geocode_services.geocode_one("강남역")

    assert "강남역" not in caplog.text
    assert "sha256=" in caplog.text


def test_provider_route_redaction_omits_raw_route(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=naver_log.name):
        naver_log.info(
            "route=%s",
            redact_route("강남역", "판교역"),
        )

    assert "강남역" not in caplog.text
    assert "판교역" not in caplog.text
    assert caplog.text.count("sha256=") == 2


def test_osrm_geocode_failure_log_omits_raw_query(monkeypatch, caplog) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(osrm.urllib.request, "urlopen", fail)

    with caplog.at_level(logging.DEBUG, logger=osrm.__name__):
        assert osrm._geocode_place("강남역") is None

    assert "강남역" not in caplog.text
    assert "sha256=" in caplog.text


def test_osrm_route_logs_omit_raw_route(monkeypatch, caplog) -> None:
    provider = osrm.OsrmTravelTimeProvider()
    route = osrm.Route.of("강남역", "판교역")

    monkeypatch.setattr(provider, "_geocode", lambda *_args: None)

    with caplog.at_level(logging.WARNING, logger=osrm.__name__):
        assert provider._get_base_duration(route) == 1800.0

    assert "강남역" not in caplog.text
    assert "판교역" not in caplog.text
    assert caplog.text.count("sha256=") == 2


def test_naver_autocomplete_fallback_log_omits_raw_query(monkeypatch, caplog) -> None:
    class _Input:
        def focus(self) -> None:
            return None

        def fill(self, *_args) -> None:
            return None

        def type(self, *_args) -> None:
            return None

        def press(self, *_args) -> None:
            return None

    class _Page:
        def query_selector(self, *_args):
            return None

        def query_selector_all(self, *_args) -> list:
            return []

    monkeypatch.setattr(
        "trip_time_service.providers.naver_playwright._sleep",
        lambda *_args, **_kwargs: None,
    )
    adapter = _NaverDirectionsSearchAdapter(provider=object())

    with caplog.at_level(logging.WARNING, logger=naver_log.name):
        adapter.select_place_ac(_Page(), _Input(), "강남역")

    assert "강남역" not in caplog.text
    assert "sha256=" in caplog.text
