from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trip_time_service.api import routes_geo


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes_geo.router)
    return TestClient(app)


def _fail_live_provider(*_args, **_kwargs):  # pragma: no cover - must not be called
    raise AssertionError("live provider must not be called in fixture mode")


def test_fixture_mode_autocomplete_returns_route_corpus_without_live_calls(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(routes_geo, "autocomplete_naver_map", _fail_live_provider)

    client = _client()

    route_queries = [
        "강남역",
        "서울역",
        "판교역",
        "수서역",
        "코엑스",
        "경수대로680번길40",
        "네이버 1784",
    ]
    for query in route_queries:
        response = client.get("/api/autocomplete", params={"q": query})

        assert response.status_code == 200
        payload = response.json()
        assert payload, query
        assert payload[0]["coords_ready"] is True
        assert payload[0]["selection_kind"] in {"station", "poi", "address"}
        assert payload[0]["canonical_query"]
        assert isinstance(payload[0]["lat"], float)
        assert isinstance(payload[0]["lon"], float)


def test_fixture_mode_preserves_unresolved_autocomplete_fields(monkeypatch) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(routes_geo, "autocomplete_naver_map", _fail_live_provider)

    response = _client().get("/api/autocomplete", params={"q": "좌표없는장소"})

    assert response.status_code == 200
    payload = response.json()
    assert payload == [
        {
            "display_name": "좌표없는장소",
            "address": "좌표 확인 불가",
            "type": "검색어",
            "lat": None,
            "lon": None,
            "source": "e2e_fixture",
            "confidence": 0.0,
            "coords_status": "unresolved",
            "degraded_reason": "coords_unresolved",
            "coords_ready": False,
            "selection_kind": "poi",
            "canonical_query": "좌표없는장소",
        }
    ]


def test_fixture_mode_geocode_returns_fixture_before_live_calls(monkeypatch) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(routes_geo, "geocode_one", _fail_live_provider)

    response = _client().get("/api/geocode", params={"q": "테헤란로 152"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "display_name": "테헤란로 152",
            "address": "서울 강남구 테헤란로 152",
            "type": "주소",
            "lat": 37.5008,
            "lon": 127.0365,
            "source": "e2e_fixture",
            "confidence": 0.99,
            "coords_status": "ready",
        }
    ]


def test_fixture_mode_misses_do_not_call_live_providers(monkeypatch) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(routes_geo, "autocomplete_naver_map", _fail_live_provider)
    monkeypatch.setattr(routes_geo, "geocode_one", _fail_live_provider)

    client = _client()

    autocomplete_response = client.get(
        "/api/autocomplete",
        params={"q": "fixture miss"},
    )
    geocode_response = client.get("/api/geocode", params={"q": "fixture miss"})

    assert autocomplete_response.status_code == 200
    assert autocomplete_response.json() == []
    assert geocode_response.status_code == 200
    assert geocode_response.json() == []


def test_fixture_mode_warmup_does_not_call_live_warmup(monkeypatch) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(routes_geo, "warmup_autocomplete_cache", _fail_live_provider)

    response = _client().post(
        "/api/autocomplete/warmup",
        json={"queries": ["강남역"], "blocking": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "queued": 0,
        "fixture_mode": True,
        "external_provider_calls": 0,
    }


def test_fixture_mode_route_does_not_call_osrm(monkeypatch) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(routes_geo, "fetch_osrm_route", _fail_live_provider)

    response = _client().get(
        "/api/route",
        params={"olat": 37.1, "olon": 127.1, "dlat": 37.2, "dlon": 127.2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["routes"][0]["source"] == "e2e_fixture"
    assert payload["routes"][0]["geometry"] == {
        "type": "LineString",
        "coordinates": [[127.1, 37.1], [127.2, 37.2]],
    }


def test_fixture_mode_disabled_by_default_uses_live_paths(monkeypatch) -> None:
    autocomplete_calls: list[str] = []
    geocode_calls: list[str] = []

    monkeypatch.delenv("TTS_E2E_FIXTURE_MODE", raising=False)
    monkeypatch.setattr(
        routes_geo,
        "autocomplete_naver_map",
        lambda q, *_args, **_kwargs: autocomplete_calls.append(q) or (),
    )
    monkeypatch.setattr(
        routes_geo,
        "geocode_one",
        lambda q, **_kwargs: geocode_calls.append(q) or None,
    )

    client = _client()
    assert client.get("/api/autocomplete", params={"q": "강남역"}).status_code == 200
    assert client.get("/api/geocode", params={"q": "강남역"}).status_code == 200

    assert autocomplete_calls == ["강남역"]
    assert geocode_calls == ["강남역"]


def test_fixture_mode_runtime_debug_reports_zero_external_provider_calls(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setenv("TTS_ENABLE_DEBUG_ROUTES", "1")
    monkeypatch.setenv("TTS_DEBUG_TOKEN", "secret")
    monkeypatch.setattr(
        routes_geo,
        "get_autocomplete_runtime_metrics",
        lambda: {"workers": 2},
    )

    response = _client().get(
        "/api/debug/autocomplete/runtime",
        headers={"X-TTS-Debug-Token": "secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["workers"] == 2
    assert payload["fixture_mode"] is True
    assert payload["mode"] == "fixture"
    assert payload["external_provider_calls"] == 0
    assert payload["external_provider_call_breakdown"] == {
        "naver_all_search": 0,
        "browser_autocomplete": 0,
        "geocode_naver": 0,
        "geocode_nominatim": 0,
        "geocode_photon": 0,
        "osrm_route": 0,
    }
