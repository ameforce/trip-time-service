from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trip_time_service.api import geocode_services, routes_geo


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes_geo.router)
    return TestClient(app)


def test_browser_poi_autocomplete_returns_progressive_without_geocode_promotion(
    monkeypatch,
) -> None:
    browser_results = (
        {
            "display_name": "카카오 판교아지트",
            "address": "경기 성남시 분당구 판교역로",
            "type": "회사",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.84,
        },
    )
    promotion_calls: list[str] = []

    monkeypatch.setattr(
        geocode_services,
        "_search_local_hints",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_browser_pool",
        lambda *args, **kwargs: browser_results,
    )

    def _promote(query: str, *args, **kwargs):  # pragma: no cover - must not run
        promotion_calls.append(query)
        raise AssertionError("POI autocomplete must not block on geocode promotion")

    monkeypatch.setattr(
        geocode_services,
        "_promote_browser_autocomplete_results",
        _promote,
    )

    results = geocode_services._autocomplete_naver_map_uncached(
        "카카오 판교아지트",
        limit=12,
    )

    assert promotion_calls == []
    assert len(results) == 1
    assert results[0]["autocomplete_mode"] == "progressive"
    assert results[0]["degraded_reason"] == "progressive_browser_suggest"
    assert results[0]["deadline_hit"] is False


def test_autocomplete_response_exposes_progressive_headers(monkeypatch) -> None:
    monkeypatch.delenv("TTS_E2E_FIXTURE_MODE", raising=False)
    monkeypatch.setattr(
        routes_geo,
        "autocomplete_naver_map",
        lambda *args, **kwargs: (
            {
                "display_name": "카카오 판교아지트",
                "address": "경기 성남시 분당구 판교역로",
                "type": "회사",
                "lat": "",
                "lon": "",
                "source": "naver_browser_suggest",
                "confidence": 0.84,
                "autocomplete_mode": "progressive",
                "degraded_reason": "progressive_browser_suggest",
                "deadline_hit": False,
            },
        ),
    )

    response = _client().get("/api/autocomplete", params={"q": "카카오 판교아지트"})

    assert response.status_code == 200
    assert response.headers["X-TTS-Autocomplete-Mode"] == "progressive"
    assert (
        response.headers["X-TTS-Autocomplete-Degraded"]
        == "progressive_browser_suggest"
    )
    payload = response.json()
    assert payload[0]["coords_ready"] is False
    assert payload[0]["selection_kind"] == "poi"
    assert payload[0]["autocomplete_mode"] == "progressive"


def test_autocomplete_budget_constant_matches_evaluator_contract() -> None:
    assert geocode_services._AUTOCOMPLETE_EXPENSIVE_PROVIDER_BUDGET_SECONDS == 3.0
