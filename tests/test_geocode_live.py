from __future__ import annotations

import os

import pytest

from trip_time_service.api.geocode_services import (
    autocomplete_nominatim,
    autocomplete_photon,
    geocode_one,
)


@pytest.mark.skipif(
    not os.getenv("TTS_LIVE_EXTENDED"),
    reason="live geocode fallback tests require TTS_LIVE_EXTENDED=1",
)
@pytest.mark.parametrize(
    "query",
    [
        "Seoul National University Hospital",
        "Seoul City Hall",
        "COEX",
    ],
)
def test_live_english_queries_resolve_via_nominatim_and_geocode(query: str) -> None:
    nominatim_results = autocomplete_nominatim(query, limit=3)

    assert nominatim_results
    assert float(nominatim_results[0]["lat"])
    assert float(nominatim_results[0]["lon"])

    result = geocode_one(query)

    assert result is not None
    assert float(result["lat"])
    assert float(result["lon"])


@pytest.mark.skipif(
    not os.getenv("TTS_LIVE_EXTENDED"),
    reason="live geocode fallback tests require TTS_LIVE_EXTENDED=1",
)
@pytest.mark.parametrize(
    "query",
    [
        "Seoul National University Hospital",
        "Seoul City Hall",
    ],
)
def test_live_photon_fallback_handles_english_queries(query: str) -> None:
    photon_results = autocomplete_photon(query, limit=3)

    assert photon_results
    assert float(photon_results[0]["lat"])
    assert float(photon_results[0]["lon"])
