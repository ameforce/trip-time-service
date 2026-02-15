from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trip_time_service.core.models import (
    DriveDuration,
    Route,
    normalize_place,
)

KST = ZoneInfo("Asia/Seoul")


class TestNormalizePlace:
    def test_strips_whitespace(self) -> None:
        assert normalize_place("  강남역  ") == "강남역"

    def test_collapses_inner_spaces(self) -> None:
        assert normalize_place("서울  강남구   강남역") == "서울 강남구 강남역"

    def test_empty_string_stays_empty(self) -> None:
        assert normalize_place("   ") == ""


class TestRoute:
    def test_of_normalizes(self) -> None:
        route = Route.of("  강남역 ", " 판교역  ")
        assert route.origin == "강남역"
        assert route.destination == "판교역"

    def test_routes_with_same_places_are_equal(self) -> None:
        r1 = Route.of("강남역", "판교역")
        r2 = Route.of("  강남역  ", "  판교역  ")
        assert r1 == r2

    def test_routes_are_hashable(self) -> None:
        r = Route.of("강남역", "판교역")
        d = {r: 1}
        assert d[r] == 1


class TestDriveDuration:
    def test_valid_duration(self) -> None:
        now = datetime.now(tz=KST)
        d = DriveDuration(duration_seconds=1800, fetched_at=now)
        assert d.duration_seconds == 1800
        assert d.raw_text is None

    def test_negative_duration_raises(self) -> None:
        now = datetime.now(tz=KST)
        with pytest.raises(ValueError, match="non-negative"):
            DriveDuration(duration_seconds=-1, fetched_at=now)

    def test_zero_duration_is_valid(self) -> None:
        now = datetime.now(tz=KST)
        d = DriveDuration(duration_seconds=0, fetched_at=now)
        assert d.duration_seconds == 0
