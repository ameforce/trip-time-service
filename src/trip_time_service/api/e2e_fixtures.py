from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

_FIXTURE_ENV = "TTS_E2E_FIXTURE_MODE"


def is_fixture_mode_enabled() -> bool:
    return os.getenv(_FIXTURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_query(query: str) -> str:
    return "".join(str(query or "").strip().lower().split())


def _entry(
    display_name: str,
    address: str,
    item_type: str,
    lat: float | None,
    lon: float | None,
    *,
    aliases: tuple[str, ...] = (),
    confidence: float = 0.99,
    coords_status: str = "ready",
    degraded_reason: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "display_name": display_name,
        "address": address,
        "type": item_type,
        "lat": lat,
        "lon": lon,
        "source": "e2e_fixture",
        "confidence": confidence,
        "coords_status": coords_status,
    }
    if degraded_reason:
        item["degraded_reason"] = degraded_reason
    item["_aliases"] = aliases
    return item


_FIXTURE_ENTRIES: tuple[dict[str, Any], ...] = (
    _entry("강남역", "서울 강남구 강남대로 396", "역", 37.4979, 127.0276),
    _entry("서울역", "서울 용산구 한강대로 405", "역", 37.5547, 126.9707),
    _entry("판교역", "경기 성남시 분당구 판교역로 160", "역", 37.3948, 127.1112),
    _entry("수서역", "서울 강남구 밤고개로 99", "역", 37.4875, 127.1019),
    _entry("잠실역", "서울 송파구 올림픽로 265", "역", 37.5133, 127.1002),
    _entry(
        "코엑스",
        "서울 강남구 영동대로 513",
        "복합문화공간",
        37.5117,
        127.0592,
        aliases=("coex",),
    ),
    _entry(
        "스타벅스 강남",
        "서울 강남구 강남대로 390",
        "카페",
        37.4974,
        127.0280,
        aliases=("스타벅스강남",),
    ),
    _entry(
        "네이버 1784",
        "경기 성남시 분당구 정자일로 95",
        "회사",
        37.3595,
        127.1052,
        aliases=("네이버1784",),
    ),
    _entry(
        "경수대로680번길 40",
        "경기 수원시 팔달구 경수대로680번길 40 센트럴하우스",
        "주소",
        37.2801,
        127.0312,
        aliases=("경수대로680번길40", "경수대로680"),
    ),
    _entry(
        "테헤란로 152",
        "서울 강남구 테헤란로 152",
        "주소",
        37.5008,
        127.0365,
    ),
    _entry(
        "한강대로 405",
        "서울 용산구 한강대로 405",
        "주소",
        37.5547,
        126.9707,
    ),
    _entry(
        "세종대로 110",
        "서울 중구 세종대로 110",
        "주소",
        37.5663,
        126.9780,
    ),
    _entry(
        "판교역로 235",
        "경기 성남시 분당구 판교역로 235",
        "주소",
        37.4010,
        127.1086,
    ),
    _entry(
        "좌표없는장소",
        "좌표 확인 불가",
        "검색어",
        None,
        None,
        confidence=0.0,
        coords_status="unresolved",
        degraded_reason="coords_unresolved",
    ),
)


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    copied = deepcopy(item)
    copied.pop("_aliases", None)
    return copied


def _matches_query(item: dict[str, Any], query_key: str) -> bool:
    values = (
        str(item["display_name"]),
        str(item["address"]),
        *(str(alias) for alias in item.get("_aliases", ())),
    )
    return any(_normalize_query(value) == query_key for value in values)


def autocomplete_fixtures(query: str, *, limit: int) -> tuple[dict[str, Any], ...]:
    query_key = _normalize_query(query)
    if not query_key:
        return ()
    matches = [
        _public_item(item)
        for item in _FIXTURE_ENTRIES
        if _matches_query(item, query_key)
    ]
    return tuple(matches[:limit])


def geocode_fixture(query: str) -> dict[str, Any] | None:
    query_key = _normalize_query(query)
    if not query_key:
        return None
    for item in _FIXTURE_ENTRIES:
        if _matches_query(item, query_key):
            return _public_item(item)
    return None


def fixture_route_payload(
    *,
    olat: float,
    olon: float,
    dlat: float,
    dlon: float,
) -> dict[str, object]:
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[olon, olat], [dlon, dlat]],
                },
                "distance": 0,
                "duration": 0,
                "source": "e2e_fixture",
            }
        ],
    }


def fixture_runtime_metrics(*, trip_provider: str | None = None) -> dict[str, object]:
    external_provider_call_breakdown = {
        "naver_all_search": 0,
        "browser_autocomplete": 0,
        "geocode_naver": 0,
        "geocode_nominatim": 0,
        "geocode_photon": 0,
        "osrm_route": 0,
    }
    if (trip_provider or "").strip().lower() == "mock":
        external_provider_call_breakdown["selenium_route_provider"] = 0
    return {
        "fixture_mode": True,
        "mode": "fixture",
        "external_provider_calls": 0,
        "external_provider_call_breakdown": external_provider_call_breakdown,
        "autocomplete_degraded_counts": {"coords_unresolved": 0},
        "provider_degraded_counts": {},
    }
