from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from trip_time_service.api import geocode_services, routes_geo

_DATASET_PATH = Path(__file__).parent / "fixtures" / "autocomplete-v2-dataset.json"
_DATASET = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
_CASES = tuple(_DATASET["cases"])


def _case_id(case: dict[str, str]) -> str:
    return case["id"]


def test_autocomplete_v2_dataset_contains_hundreds_of_cases() -> None:
    assert _DATASET["case_count"] == len(_CASES)
    assert len(_CASES) >= 300
    counts = Counter(case["category"] for case in _CASES)
    assert counts == {
        "trailing_jamo": 120,
        "compositionend": 120,
        "plain_input": 120,
    }
    assert len({case["expected_query"] for case in _CASES}) == len(_CASES)


@pytest.mark.parametrize("case", _CASES, ids=_case_id)
def test_generated_autocomplete_cases_serialize_as_route_safe_candidates(
    case: dict[str, str],
) -> None:
    index = int(case["id"].rsplit("-", 1)[1])
    item = {
        "display_name": case["response_name"],
        "address": f"서울 테스트로 {100 + index}",
        "type": "장소",
        "lat": str(37.0 + (index / 10000)),
        "lon": str(127.0 + (index / 10000)),
        "source": "dataset",
        "confidence": 0.9,
    }

    serialized = routes_geo._serialize_autocomplete_items((item,))

    assert len(serialized) == 1
    assert serialized[0]["display_name"] == case["response_name"]
    assert serialized[0]["coords_ready"] is True
    assert serialized[0]["selection_kind"] == "poi"
    assert serialized[0]["canonical_query"] == f"테스트로 {100 + index}"


@pytest.mark.parametrize("case", _CASES, ids=_case_id)
def test_generated_autocomplete_cases_preserve_progressive_metadata(
    case: dict[str, str],
) -> None:
    progressive = geocode_services._mark_progressive_browser_autocomplete_results(
        (
            {
                "display_name": case["response_name"],
                "address": case["expected_query"],
                "type": "검색어",
                "lat": "",
                "lon": "",
                "source": "naver_browser_suggest",
                "confidence": 0.72,
            },
        ),
        reason="progressive_browser_suggest",
    )

    serialized = routes_geo._serialize_autocomplete_items(progressive)

    assert len(serialized) == 1
    assert serialized[0]["coords_ready"] is False
    assert serialized[0]["selection_kind"] == "poi"
    assert serialized[0]["degraded_reason"] == "progressive_browser_suggest"
    assert serialized[0]["autocomplete_mode"] == "progressive"
    assert serialized[0]["deadline_hit"] is False
