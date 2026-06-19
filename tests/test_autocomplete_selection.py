from __future__ import annotations

from trip_time_service.api import geocode_services, routes_geo


def test_serialize_autocomplete_items_preserves_stable_fields_for_unresolved_candidates(
) -> None:
    items = (
        {
            "display_name": "경수대로680번길 40",
            "address": "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스",
            "type": "주소",
            "lat": "37.2801",
            "lon": "127.0312",
            "source": "naver_all_search",
            "confidence": 0.91,
        },
        {
            "display_name": "경수대로680번길 40",
            "address": "경기 수원시 팔달구 경수대로680번길 40",
            "type": "검색어",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.62,
        },
        {
            "display_name": "수원역",
            "address": "경기 수원시 팔달구 덕영대로 924",
            "type": "역",
            "lat": "37.2659",
            "lon": "126.9990",
            "source": "naver_all_search",
            "confidence": 0.95,
        },
    )

    serialized = routes_geo._serialize_autocomplete_items(items)

    assert len(serialized) == 3
    assert serialized[0]["coords_ready"] is True
    assert serialized[0]["selection_kind"] == "address"
    assert serialized[0]["canonical_query"] == "경수대로680번길 40"
    assert serialized[1]["coords_ready"] is False
    assert serialized[1]["selection_kind"] == "poi"
    assert serialized[1]["canonical_query"] == "경수대로680번길 40"
    assert serialized[2]["coords_ready"] is True
    assert serialized[2]["selection_kind"] == "station"
    assert serialized[2]["canonical_query"] == "수원역"


def test_merge_browser_candidate_promotes_text_only_suggestion() -> None:
    candidate = {
        "display_name": "테헤란로 152",
        "address": "테헤란로 152",
        "type": "검색어",
        "lat": "",
        "lon": "",
        "source": "naver_browser_suggest",
        "confidence": 0.62,
    }
    geocoded = {
        "display_name": "테헤란로 152",
        "address": "서울 강남구 테헤란로 152",
        "type": "주소",
        "lat": "37.5000",
        "lon": "127.0360",
        "source": "naver_browser",
        "confidence": 0.71,
    }

    merged = geocode_services._merge_browser_autocomplete_candidate(
        candidate,
        geocoded,
    )

    assert merged["lat"] == "37.5000"
    assert merged["lon"] == "127.0360"
    assert merged["address"] == "서울 강남구 테헤란로 152"
    assert merged["source"] == "naver_browser_suggest_geocoded"
    assert merged["geocode_source"] == "naver_browser"
    assert merged["confidence"] == 0.71


def test_iter_browser_autocomplete_queries_adds_trimmed_road_address() -> None:
    candidate = {
        "display_name": "경수대로680번길40",
        "address": "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스",
        "type": "검색어",
    }

    queries = geocode_services._iter_browser_autocomplete_queries(candidate)

    assert queries[0] == "경수대로680번길40"
    assert "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스" in queries
    assert "경수대로680번길 40" in queries


def test_trim_to_core_road_address_extracts_searchable_segment() -> None:
    assert (
        geocode_services._trim_to_core_road_address(
            "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스"
        )
        == "경수대로680번길 40"
    )
    assert (
        geocode_services._trim_to_core_road_address("테헤란로 152 강남파이낸스센터")
        == "테헤란로 152"
    )


def test_autocomplete_does_not_return_curated_local_hint_when_live_providers_miss(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_browser_pool",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_nominatim",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_photon",
        lambda *args, **kwargs: (),
    )

    results = geocode_services._autocomplete_naver_map_uncached(
        "경수대로680번길40",
        limit=5,
    )

    assert results == ()


def test_geocode_one_does_not_return_curated_local_hint_when_live_providers_miss(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "geocode_naver",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        geocode_services,
        "geocode_photon",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        geocode_services,
        "geocode_nominatim",
        lambda *args, **kwargs: None,
    )

    assert geocode_services.geocode_one("강남역") is None


def test_autocomplete_browser_poi_fallback_returns_progressive_suggestion(
    monkeypatch,
) -> None:
    browser_results = (
        {
            "display_name": "센트럴시티터미널",
            "address": "서울 서초구 신반포로 194",
            "type": "버스터미널",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.82,
        },
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

    promotion_calls: list[str] = []

    def _promote(query: str, *args, **kwargs):  # pragma: no cover - must not run
        promotion_calls.append(query)
        raise AssertionError("POI autocomplete should return progressive suggestions")

    monkeypatch.setattr(
        geocode_services,
        "_promote_browser_autocomplete_results",
        _promote,
    )

    results = geocode_services._autocomplete_naver_map_uncached("센트럴", limit=12)

    assert len(results) == 1
    assert results[0]["source"] == "naver_browser_suggest"
    assert results[0]["autocomplete_mode"] == "progressive"
    assert results[0]["degraded_reason"] == "progressive_browser_suggest"
    assert results[0]["deadline_hit"] is False
    assert promotion_calls == []


def test_autocomplete_single_address_like_browser_result_uses_promotion(
    monkeypatch,
) -> None:
    browser_results = (
        {
            "display_name": "경수대로680번길40",
            "address": "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스",
            "type": "검색어",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.62,
        },
    )
    promoted_results = (
        {
            "display_name": "경수대로680번길40",
            "address": "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스",
            "type": "주소",
            "lat": "37.2801",
            "lon": "127.0312",
            "source": "naver_browser_suggest_geocoded",
            "confidence": 0.71,
        },
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
    monkeypatch.setattr(
        geocode_services,
        "_promote_browser_autocomplete_results",
        lambda *args, **kwargs: promoted_results,
    )

    results = geocode_services._autocomplete_naver_map_uncached(
        "경수대로680번길40",
        limit=12,
    )

    assert results == promoted_results


def test_autocomplete_address_like_query_still_uses_browser_promotion(
    monkeypatch,
) -> None:
    browser_results = (
        {
            "display_name": "테헤란로 152",
            "address": "테헤란로 152",
            "type": "검색어",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.62,
        },
        {
            "display_name": "테헤란로 152 인근",
            "address": "서울 강남구 테헤란로 152",
            "type": "주소",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.58,
        },
    )
    promoted_results = (
        {
            "display_name": "테헤란로 152",
            "address": "서울 강남구 테헤란로 152",
            "type": "주소",
            "lat": "37.5000",
            "lon": "127.0360",
            "source": "naver_browser_suggest_geocoded",
            "confidence": 0.71,
        },
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
    monkeypatch.setattr(
        geocode_services,
        "_promote_browser_autocomplete_results",
        lambda *args, **kwargs: promoted_results,
    )

    results = geocode_services._autocomplete_naver_map_uncached(
        "테헤란로 152",
        limit=12,
    )

    assert results == promoted_results


def test_autocomplete_prefers_naver_map_browser_ui_over_all_search(
    monkeypatch,
) -> None:
    geocode_services._reset_runtime_counters()
    browser_results = (
        {
            "display_name": "강남역",
            "address": "강남역",
            "type": "검색어",
            "lat": "",
            "lon": "",
            "source": "naver_browser_suggest",
            "confidence": 0.9,
        },
    )
    all_search_results = (
        {
            "display_name": "강남역 HTTP",
            "address": "서울 강남구 강남대로 396",
            "type": "역",
            "lat": "37.4979",
            "lon": "127.0276",
            "source": "naver_all_search",
            "confidence": 0.95,
        },
    )
    calls: list[str] = []

    def _browser(*args, **kwargs):
        calls.append("browser")
        return browser_results

    def _all_search(*args, **kwargs):
        calls.append("all_search")
        return all_search_results

    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_browser_pool",
        _browser,
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        _all_search,
    )

    results = geocode_services._autocomplete_naver_map_uncached("강남역", limit=5)

    assert results[0]["source"] == "naver_browser_suggest"
    assert calls == ["browser"]
    metrics = geocode_services.get_autocomplete_runtime_metrics()
    stage_metrics = metrics["autocomplete_stage_metrics"]
    assert "naver_all_search" not in stage_metrics
    assert stage_metrics["browser_autocomplete"]["outcomes"]["hit"] >= 1


def test_autocomplete_does_not_show_non_naver_fallbacks_when_naver_map_misses(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_browser_pool",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_nominatim",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("autocomplete must not show non-Naver candidates")
        ),
    )
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_photon",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("autocomplete must not show non-Naver candidates")
        ),
    )

    assert geocode_services._autocomplete_naver_map_uncached("강남역", limit=5) == ()


def test_autocomplete_returns_empty_when_naver_map_browser_ui_misses(
    monkeypatch,
) -> None:
    geocode_services._reset_runtime_counters()
    calls: list[str] = []
    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_browser_pool",
        lambda *args, **kwargs: (),
    )

    def _all_search(*args, **kwargs):
        calls.append("all_search")
        raise AssertionError("browser UI miss must not show allSearch candidates")

    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        _all_search,
    )

    results = geocode_services._autocomplete_naver_map_uncached("강남역", limit=5)

    assert results == ()
    assert calls == []
    metrics = geocode_services.get_autocomplete_runtime_metrics()
    stage_metrics = metrics["autocomplete_stage_metrics"]
    assert "local_hint" not in stage_metrics
    assert "naver_all_search" not in stage_metrics
