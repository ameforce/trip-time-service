from __future__ import annotations

import pytest

from trip_time_service.api import geocode_services


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


def _patch_autocomplete_fallbacks(
    monkeypatch,
    instant_calls: list[str],
) -> None:
    def _forbid_all_search(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("autocomplete warmup/backoff must not call allSearch")

    monkeypatch.setattr(
        geocode_services,
        "autocomplete_naver_map_raw",
        _forbid_all_search,
    )

    def _instant_search(query: str, *, limit: int) -> tuple[dict, ...]:
        del limit
        instant_calls.append(query)
        return ()

    monkeypatch.setattr(
        geocode_services,
        "fetch_autocomplete_from_instant_search",
        _instant_search,
    )
    monkeypatch.setattr(geocode_services, "warmup_naver_browser_pool", lambda: None)
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


def test_warmup_does_not_call_all_search_or_arm_global_backoff(monkeypatch) -> None:
    instant_calls: list[str] = []
    _patch_autocomplete_fallbacks(
        monkeypatch,
        instant_calls=instant_calls,
    )

    geocode_services.clear_autocomplete_cache()
    try:
        geocode_services.warmup_autocomplete_cache(
            ["경수대로680번길40"],
            background=False,
        )

        assert (
            geocode_services.get_autocomplete_runtime_metrics()[
                "ncaptcha_backoff_active"
            ]
            is False
        )

        results = geocode_services.autocomplete_naver_map("세종대로 110", limit=5)

        assert results == ()
        assert instant_calls == ["세종대로 110"]
    finally:
        geocode_services.clear_autocomplete_cache()


def test_interactive_autocomplete_miss_does_not_call_all_search_backoff(
    monkeypatch,
) -> None:
    instant_calls: list[str] = []
    _patch_autocomplete_fallbacks(
        monkeypatch,
        instant_calls=instant_calls,
    )

    geocode_services.clear_autocomplete_cache()
    try:
        assert (
            geocode_services.autocomplete_naver_map(
                "경수대로680번길40",
                limit=5,
            )
            == ()
        )
        assert (
            geocode_services.get_autocomplete_runtime_metrics()[
                "ncaptcha_backoff_active"
            ]
            is False
        )

        assert geocode_services.autocomplete_naver_map("세종대로 110", limit=5) == ()
        assert instant_calls == ["경수대로680번길40", "세종대로 110"]
    finally:
        geocode_services.clear_autocomplete_cache()


def test_warmup_skips_verbose_reverse_geocoded_address_queries(monkeypatch) -> None:
    instant_calls: list[str] = []
    _patch_autocomplete_fallbacks(
        monkeypatch,
        instant_calls=instant_calls,
    )

    geocode_services.clear_autocomplete_cache()
    try:
        queued = geocode_services.warmup_autocomplete_cache(
            [
                "경수대로680번길, 우만동, 팔달구, 수원시, 16235, 대한민국",
                "세종대로 110",
            ],
            background=False,
        )

        assert queued == 1
        assert instant_calls == []
    finally:
        geocode_services.clear_autocomplete_cache()
