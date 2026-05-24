from __future__ import annotations

from trip_time_service.api import geocode_services


def _fail_browser_warmup() -> int:
    raise AssertionError("browser warmup must not run in fixture mode")


def test_fixture_mode_skips_browser_runtime_warmup(monkeypatch) -> None:
    monkeypatch.setenv("TTS_E2E_FIXTURE_MODE", "1")
    monkeypatch.setattr(
        geocode_services,
        "warmup_naver_browser_pool",
        _fail_browser_warmup,
    )

    geocode_services.startup_autocomplete_runtime()
    try:
        assert geocode_services.warmup_autocomplete_runtime() == 0
        assert geocode_services.warmup_autocomplete_cache(
            ["강남역"],
            background=False,
        ) == 0
    finally:
        geocode_services.shutdown_autocomplete_runtime(wait_seconds=0.0)
