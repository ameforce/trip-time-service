from __future__ import annotations

import json

import pytest

from trip_time_service.api import geocode_services


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


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
    responses: list[dict],
    calls: list[str],
) -> None:
    payloads = iter(responses)

    def _fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
        del timeout
        query = request.full_url.split("query=", 1)[1].split("&", 1)[0]
        calls.append(geocode_services.urllib.parse.unquote_plus(query))
        return _FakeResponse(next(payloads))

    monkeypatch.setattr(
        geocode_services.urllib.request,
        "urlopen",
        _fake_urlopen,
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
    calls: list[str] = []
    _patch_autocomplete_fallbacks(
        monkeypatch,
        responses=[
            {
                "result": {
                    "ncaptcha": True,
                    "place": {"list": []},
                    "address": {"list": []},
                }
            },
            {
                "result": {
                    "place": {"list": []},
                    "address": {
                        "list": [
                            {
                                "name": "세종대로 110",
                                "roadAddress": "서울 중구 세종대로 110",
                                "x": "126.9783882",
                                "y": "37.5666103",
                            }
                        ]
                    },
                }
            },
        ],
        calls=calls,
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
        assert calls == []
    finally:
        geocode_services.clear_autocomplete_cache()


def test_interactive_autocomplete_miss_does_not_call_all_search_backoff(
    monkeypatch,
) -> None:
    calls: list[str] = []
    _patch_autocomplete_fallbacks(
        monkeypatch,
        responses=[
            {
                "result": {
                    "ncaptcha": True,
                    "place": {"list": []},
                    "address": {"list": []},
                }
            },
            {
                "result": {
                    "place": {"list": []},
                    "address": {
                        "list": [
                            {
                                "name": "세종대로 110",
                                "roadAddress": "서울 중구 세종대로 110",
                                "x": "126.9783882",
                                "y": "37.5666103",
                            }
                        ]
                    },
                }
            },
        ],
        calls=calls,
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
        assert calls == []
    finally:
        geocode_services.clear_autocomplete_cache()


def test_warmup_skips_verbose_reverse_geocoded_address_queries(monkeypatch) -> None:
    calls: list[str] = []
    _patch_autocomplete_fallbacks(
        monkeypatch,
        responses=[
            {
                "result": {
                    "place": {"list": []},
                    "address": {
                        "list": [
                            {
                                "name": "세종대로 110",
                                "roadAddress": "서울 중구 세종대로 110",
                                "x": "126.9783882",
                                "y": "37.5666103",
                            }
                        ]
                    },
                }
            },
            {
                "result": {
                    "place": {"list": []},
                    "address": {
                        "list": [
                            {
                                "name": "테스트",
                                "roadAddress": "테스트",
                                "x": "126.9783882",
                                "y": "37.5666103",
                            }
                        ]
                    },
                }
            },
        ],
        calls=calls,
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
        assert calls == []
    finally:
        geocode_services.clear_autocomplete_cache()
